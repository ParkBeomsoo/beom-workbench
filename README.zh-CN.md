# beom-workbench

[English](README.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | **简体中文**

> 此译文可能落后于最新版本。最新内容请参阅 [English](README.md) 或 [한국어](README.ko.md)。

**编码代理的工作台 — 先规划，再按需求或参考仓库实现，并按你批准的验收标准验证**


> 应用界面支持韩语、英语、日语和简体中文。首次启动时跟随系统语言 (其他语言则为韩语)，可在侧边栏的语言按钮中更改。代理使用的语言遵循代理自身的规则 — 需求语言明确时告诉它使用该语言；无法判断时 (只有附件或链接)，或你在代理设置中指定了回复语言时 (Claude Code `language`)，以该设置为准。命令行帮助 (`--help`) 和安装脚本的提示也跟随同一语言。

![version](https://img.shields.io/badge/version-0.3.1-blue) [![smoke](https://github.com/ParkBeomsoo/beom-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/ParkBeomsoo/beom-workbench/actions/workflows/ci.yml) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-use%20only%2C%20no%20modification-lightgrey)

与其“粗略读一遍别人写的功能再照着做”，不如对整个代码库进行分析并为每条结论附上证据（文件:行号），
再通过程序确认移植后的结果与原项目行为一致。本工具包同时提供技能、MCP 工具和 CLI，让 Claude Code 等 AI 代理能够按流程使用；
对于不应由 AI 独自决定的事项，未经人工批准就无法完成。

## 目录

- [它能做什么](#它能做什么)
- [核心概念](#核心概念)
- [环境要求](#环境要求)
- [安装](#安装)
- [桌面应用与自动运行](#桌面应用与自动运行)
- [快速开始 1: 全量分析仓库](#快速开始-1-全量分析仓库)
- [快速开始 2: 移植功能并验证](#快速开始-2-移植功能并验证)
- [快速开始 3: 按需求构建](#快速开始-3-按需求构建)
- [修改原项目后使用 (定制)](#修改原项目后使用-定制)
- [用例文件格式](#用例文件格式)
- [命令一览](#命令一览)
- [数据与存储位置](#数据与存储位置)
- [配置](#配置)
- [安全原则](#安全原则)
- [已知限制](#已知限制)
- [许可](#许可)

## 它能做什么

| 想做的事 | 工具包的做法 |
|---|---|
| **完整掌握功能** | 对所有文件分类，把所有函数拆分为审查单元，并为每条结论附上证据（文件:行号）。无法确定的内容会留在“未确认列表”中。 |
| **真实的 API 列表** | 按各框架的规则查找 HTTP·CLI 入口点，并展开代码中拼接而成的 URL，生成真实的路径列表。 |
| **修改前确认影响** | 展示要修改的函数会影响哪些地方（调用方·实现·入口点·测试），以及在不触碰核心代码的前提下可以插入的扩展点。 |
| **跟进原项目更新** | 比较两个版本，找出与我们的修改发生冲突的地方（函数删除·重命名·签名变更）。 |
| **只挑选功能进行移植** | 从一个入口点切出所需的代码·状态·配置·外部依赖，并在台账中逐项记录移植到了哪里。也可以用其他语言重新实现。 |
| **验证移植结果** | 向原项目和自己的代码输入相同的数据并比较结果（HTTP·CLI·函数级别）。有意修改的行为用期望值验证，原项目中不存在的新行为也用期望值验证。 |
| **构建想要的功能** | 无需参考原项目，从“帮我做一个这样的功能”开始，先让要求和验收标准获得批准再构建，然后验证是否符合标准、面对异常输入能否稳定运行。 |
| **阻止 AI 走捷径** | 允许差异·省略功能·缩减测试·期望值在人工批准之前都无法完成。验证后代码发生变化，或在工具包之外修改记录，都会被检测出来。 |

## 核心概念

**证据等级** — 以等级记录结论的确定程度。

| 等级 | 含义 | 获取方式 |
|---|---|---|
| L0 | 阅读代码并通过结构图确认 | 默认 |
| L1 | 通过语言服务器确认调用·引用 | `lsp` |
| L2 | 确认值从哪里流向哪里 | `dataflow` |
| L3 | 确认实际被执行 | 覆盖率报告、差分验证 |

**台账** — 分析·移植状态累积在每个项目独立的 SQLite 台账（`~/.beom-workbench/projects/<id>/ledger.db`）中。
即使会话切换，或多个代理同时工作，也会延续同一份记录。

**移植完成条件（7 道门禁）** — 全部满足后，`port-finish` 才会给出完成判定。

1. 所有待移植项均已处理（移植 / 替代 / 省略）
2. 移植位置至今仍保持原样（精确到行内容）
3. 差分验证通过 — 最后保存的全部用例，**以当前代码为准**（验证后代码发生变化则失效）
4. 用例执行到了所有待移植函数（原项目覆盖率，不可行时用自己代码的覆盖率）
5. 需要人工确认的项目均已批准（允许差异·省略·用例变更·期望值；批准后内容发生变化则失效）
6. 记录未在工具包之外被修改（台账哈希链·黄金基准哈希）
7. 没有需要确认许可的文件

所有移植命令的响应都会附带 `next_step`（下一步要做的事）。

## 环境要求

| 类别 | 内容 |
|---|---|
| 必需 | Python 3.11+、git |
| 必需引擎 | 结构图引擎、入口点规则引擎 — 由 `setup` 安装（校验和验证） |
| 可选 | 语言服务器（L1）、数据流引擎（L2，JDK 21+）、覆盖率工具（L3）、容器引擎（隔离运行他人的代码） |
| 语言服务器（L1） | Rust · Java · Kotlin · TypeScript/JavaScript · Python · Go · C/C++ · C# · Dart · Ruby（其他语言最高到 L0，文件·符号分类与语言无关） |
| 入口点规则 | Axum · Spring · Express · FastAPI/Flask · Gin · clap · Next.js（没有规则的框架会自动标记为未确认） |

引擎名称·许可·安装位置见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 安装

```bash
# 1. 安装工具包 — 在任意目录都可以使用 beom-workbench 命令
pipx install git+https://github.com/ParkBeomsoo/beom-workbench.git
# （或）uv tool install git+https://github.com/ParkBeomsoo/beom-workbench.git

# 2. 状态检查 — 引擎·语言服务器·技能·MCP 注册
beom-workbench doctor

# 3. 安装引擎 — 先只查看计划，再只安装选中的项
beom-workbench setup --languages python typescript
beom-workbench setup --apply codebase-memory-mcp ast-grep lsp:python lsp:typescript coverage
```

- 不带任何选项运行 `setup` 时，**只显示计划**，不做任何更改。只安装 `--apply` 中列出的项。
- 要隔离运行他人的代码，需要容器引擎。在 Windows 上，`setup --apply sandbox` 会在 WSL 发行版中
  安装容器引擎（无需桌面应用）。如果已有 `docker`，则直接使用它。
- JDK·Node.js·Go·Rust 等语言运行时需要自行安装。

### AI 代理(Claude Code) 集成

二选一即可。

```bash
# A. 通过 setup 注册技能 + MCP
beom-workbench setup --apply skill mcp-register
beom-workbench setup --apply skill mcp-register --scope project --project-dir <项目>   # 仅用于单个项目

# B. Claude Code 插件（需先按上面第 1 步安装工具包）
claude plugin marketplace add ParkBeomsoo/beom-workbench
claude plugin install beom-workbench@beom-workbench
```

- 技能会在安装的副本中记录版本，工具包更新后，`doctor` 会以 `outdated` 提示。
- 如果已自行创建了同名技能，或存在指向其他安装的 MCP 注册，不会覆盖，而是给出提示。
- 也可以不用 MCP，只用 CLI。批准·完成判定等规则在 CLI 中同样生效。

### 更新与卸载

```bash
pipx upgrade beom-workbench
beom-workbench setup --update                      # 仅将已安装的技能更新到新版本（不会新安装）
beom-workbench setup --remove skill mcp-register   # 仅撤销本工具包安装·注册的内容
pipx uninstall beom-workbench
```

## 桌面应用与自动运行

**Windows** (PowerShell) — 安装应用，并询问是否创建开始菜单和桌面快捷方式:

```powershell
irm https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.ps1 | iex
```

**macOS · Linux**: `curl -fsSL https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.sh | sh`

手动安装:

```bash
pipx install "beom-workbench[app] @ git+https://github.com/ParkBeomsoo/beom-workbench.git"
beom-workbench                    # 打开应用
beom-workbench app --shortcut          # 创建开始菜单(搜索) + 桌面快捷方式，--remove-shortcut 删除
```

首次打开应用时也会询问是否创建快捷方式。Linux 上会创建 `.desktop` 条目；macOS 上安装脚本会创建
`~/Applications/beom-workbench.app` (可从 Finder、Launchpad、Spotlight 打开；`BEOM_SHORTCUT=none` 则不创建)。
快捷键在 macOS 上用 ⌘ (⌘N 新任务，⌘Enter 开始·批准)，其他系统用 Ctrl。

**在首页选择模式**

| 模式 | 适用场景 | 阶段 |
|---|---|---|
| **按需求** | 帮我做这样的功能 (没有可参照的原项目) | 需求 → 验收标准 → 计划 → 实现 → 验证 → 交接 |
| **基于参考** | 把另一个仓库的功能搬到我的项目 | 分析 → 范围 → 记录原项目 → 计划 → 实现 → 与原项目对比 → 交接 |
| **策划** | 把功能或服务做成交给开发、设计、测试的策划包 | 了解 → 策划书 → 规则 → 界面(线框图) → 测试场景 → 一致性检查与交接 → *按此策划构建* |
| **自由对话** | 不走本流程，直接使用 Claude Code、Codex | 消息 → 工作 → 消息 … |

**附件** — 任何模式都可附加策划书、文档、链接: md · txt · pdf · 图片，以及 Word · PowerPoint · Excel · 韩文 (HWPX)
(同时提供转换后的文本)。公开网页会保存内容，需要登录的页面 (如内部 Wiki、问题跟踪) 只传链接 — 这时导出为文件再附加最可靠。
代理会先读附件。也可以把文件拖放到窗口上，或粘贴截图 (Ctrl+V)；发给进行中或已完成任务的消息也可以带附件 — 例如要修改之处的截图。

**浏览器面板** — 任务标题栏的 *浏览器* 按钮会在窗口右侧打开浏览器，再按一次 (或 ✕) 关闭。在那里登录管理后台或预发布站点后，
*获取此画面* 会把看到的画面 (截图和内容) 附加到输入框；打开 *也让代理查看此站点* 后，代理可在 12 小时内截取该已登录站点的画面，
使原型和检查以真实画面为准。密码和 Cookie 不会交给代理，登录状态只保存在工具包文件夹中。

**策划包的画面** — 原型的外观按事实决定：你附加的图片·设计稿，或“重新设计·只改感觉”之类的要求优先；否则若有产品代码，工具包会从源码
读取 UI 组件库和主题值 (主色、字体、圆角) 并套用到原型部件上；两者都没有时使用简洁的中性外观。用 ```` ```flow ```` 代码块写的业务流程会画成图。
界面设计中的“图与规则对照”表会与工具包从原型读取的数字和按钮状态核对；标为“用户决定”的行必须引用用户原话。交接时工具包会打开所有原型，
若有溢出、文字被截断、对比度低或点击区域过小则拒绝交接。导出文件占满窗口宽度、文档标签固定在顶部，并附带 **开发交接清单** 标签
(每条需求对应的画面、规则和场景)。

参考模式只需要仓库 URL (或路径)。要搬的功能可以用文字写在输入框里，留空则由代理列出仓库功能让你挑选。

**代理 · 模型 · 思考力度** — 在发送按钮旁的一个按钮里选择。

| 代理 | 说明 |
|---|---|
| Claude Code | 每个操作都按工具包的权限规则确认。模型: 默认 · Opus · Sonnet · Haiku (别名始终指向最新)，力度 低 → 最高 |
| Codex | 在自己的沙箱内工作 (只能写目标文件夹)，并通过其 app-server 运行：需要超出沙箱允许的操作时 (在沙箱外运行命令、写入其他文件夹、访问网络) 会询问，工具包按与 Claude 权限请求相同的规则作答 — 规则为*询问*时显示卡片。没有 app-server 的 Codex 版本使用 `codex exec` 运行，无法询问 (被拦截的命令直接失败)。模型和力度级别直接使用 Codex 为你的账号保存的列表，新模型会自动出现。与 Claude 一样，运行中只使用本工具包的工具 — Codex 配置中的其他 MCP 服务器和插件 (浏览器、计算机操作等) 在该运行中关闭；未允许网络访问时，Codex 自带的网络搜索也会关闭。不会修改配置文件。 未允许网络访问时 Codex 沙箱没有网络，包安装和测试通过工具包进行 (`wb_deps`、`wb_task`) — 与 Claude 的*不隔离运行*相同的规则 (编辑自动允许时询问，自动模式下允许)。 |
| Gemini (Antigravity CLI `agy`) | 实验性。自 2026-06 起个人版 Gemini CLI 改为 Antigravity CLI — 有 `agy` 就用它，否则使用企业版的 `gemini` |

列表中没有的模型名可以直接输入。模型和力度在运行中也能修改 (从下一轮生效)。

**权限** — 选择模式，按类别的细则在 *选项 → 权限*。

| 模式 | 行为 |
|---|---|
| 全部询问 | 每次修改和命令都确认 |
| 自动允许编辑 (默认) | 目标文件夹内的修改、构建/测试和只读命令直接执行，其他命令 (运行自己的脚本、`python -c`、网络、文件夹外路径等) 和网络需确认 |
| 自动 | 命令也不询问直接执行，只确认网络和文件夹外的文件 — 仅用于可信任务 |

push、发布、文件夹外删除和系统更改始终拒绝。在前台启动开发服务器 (`npm run dev`、`python -m http.server` 等) 会一直不返回、让本轮卡住，因此会附上做法退回 — 可以在后台启动，验证工具会自己启动并关闭服务器。在 Windows 内置隔离中，Node 构建工具 (npm、vite、esbuild 等) 无法启动子进程而不能运行，因此这类命令会询问是否*不隔离运行* — 自动模式直接允许，选择*在此项目中始终*后不再询问。代理传给验证工具的命令 (包括用例文件里的命令) 也走同样的规则。
认证: *我的登录* (订阅) 或 *API 密钥* (读取环境变量中的密钥，按量计费) — 能做的事情相同。

**把仓库的子文件夹选为工作文件夹时** (例如同一仓库里的 `api/search`)，同一仓库的其他文件夹无需询问即可读取 (参考其他模块)，只有修改工作文件夹外的文件时才询问。经你允许修改的文件也会进入同一个工作分支的提交，合并时一起带上。如果要自由修改多个模块，请选择仓库的顶层文件夹。

**流程由工具包负责，文件由代理编写。** 过去因代理做法不同而结果不同的步骤改由工具包完成 — 按代理写的依赖文件安装 (`package.json` → 项目的 `node_modules`，`requirements.txt`・`pyproject.toml` → 含 pytest、coverage 的 `.beom/venv`；仅在文件变化时重新安装，权限卡只出现一次)，用项目的测试运行已映射的文件来测量覆盖率，为验证服务器命令中的 `{port}` 填入空闲端口，并清理本轮产生的测试残留 (`__pycache__`、`.pytest_cache`、`.coverage`)。仍有待批准项时不接受交接。

**由人决定的事** — 只有这些会以卡片出现。

- **开始时一次**: 需求、验收标准、计划以及 (有界面时) 设计方向一起出现 — 批准一次后会一直进行到满足所有完成条件。
  每张卡片: *批准*、*修改* (自己改需求、用例或计划步骤后 *保存并批准* — 以用户编写记入账本并转告代理)，
  或在输入框写下要改的地方 (不批准，由代理修改)。
- 代理的 **提问** — 选项是按钮 (单选或多选)，推荐项附带理由，也可以自己输入。超过 4 个时每 4 个一张卡片，最后汇总确认
  (一次最多 12 个，其余在回答后再问)。
- **权限请求** (`Y` 允许 · `N` 拒绝)。*此命令始终允许* 会记住命令的开头 (例如 `python notes.py …`)，在此任务中同样形式不再询问 — 仅限单行命令，不记住内联代码 (`python -c`) 或串联命令。记住的命令显示在任务的运行选项中，可在那里删除。*在此项目中始终* 允许该类别的全部操作。
- 最后的 **交接**: 接受，或发送消息继续 — 给已停止或已完成的运行发送消息即可接着进行。

批准只在应用内、按显示的内容原样记入账本。代理没有可触及的批准途径。

**右侧面板**

| 标签 | 内容 |
|---|---|
| 概况 | 进度、计划步骤、需求、完成条件 |
| 标准 | 约定了什么 — 需求、验收用例 (输入 → 期望)、计划、谁在何时批准 |
| 验证 | 验证了什么 — 按需求列出每个用例的输入 → 期望 → 实际结果，以及每个完成条件的含义 |
| 变更 | 变更文件和 diff、按文件撤销、检查点 (每轮记录目标文件夹，可回到任意时点) |

**轮次** — 代理会话一次即为一轮，运行器会一轮接一轮地进行，直到满足所有完成条件。
默认不限次数，连续三轮没有进展会自动停止。遇到使用额度上限 (usage limit · 429) 的轮次不计数，会等到解除时间后重跑同一轮；临时故障按间隔重试；登录失效会立即停止并显示 *需要登录*。可以在 *选项* 中设置上限，或选择 *最后确认一次*，先构建并验证再查看。

**界面与设计** — 有界面的工作，计划中会附带设计方向 (风格、颜色、字体、参考、要避免的东西)，与标准一起批准。
代理遵循避免常见 "AI 味" 的设计规则。界面用例用真实的浏览器引擎打开页面，检查文字、元素和脚本错误并保存截图 — 可在验证标签中放大查看。

**数据存放位置** — 全部留在本机。见[数据与存储位置](#数据与存储位置)。

不用应用: `beom-workbench run --mode request --target-root <文件夹> --request "<需求>"` (`--agent codex`、`--resume <运行>`)。在终端运行时会显示进度，并当场询问权限、问题、开始前确认和交接 (也可以在应用中回答)；`--answer app` 则全部在应用中回答。终端批准只能在真实终端中进行，因此代理无法通过管道调用 CLI 代你回答。

按[官方指南](https://code.claude.com/docs)安装 Claude Code 并执行 `claude auth login`；Codex 用 `npm i -g @openai/codex` 后执行 `codex login`
(检查: `beom-workbench agent-status`)。自己在自己的电脑上使用时用订阅登录，为他人提供服务时用 API 密钥
([Anthropic 条款](https://code.claude.com/docs/en/legal-and-compliance))。

## 快速开始 1: 全量分析仓库

**交给 AI 代理时** — 在 Claude Code 中：

> 请全量分析这个仓库 https://github.com/<组织>/<仓库>

技能会按照下面的流程进行，大型仓库会由多个子代理分摊单元并行分析。

**直接使用 CLI 时**

```bash
beom-workbench open https://github.com/<组织>/<仓库> --ref v1.2.0   # 分析专用 clone，固定提交 → project_id
beom-workbench inventory <pid>                    # 文件分类·符号·结构图·入口点·分析单元
beom-workbench features <pid>                     # 真实 URL·CLI 入口点列表

# 分析循环（重复直到没有单元为止）
beom-workbench next <pid> --agent a1              # 领取下一个单元
beom-workbench context <pid> <unit_id> --agent a1 --raw
beom-workbench finding <pid> <unit_id> --agent a1 --json finding.json   # 结论 + 证据
beom-workbench unknown-add <pid> --agent a1 --json unknown.json        # 无法确定的内容
beom-workbench complete <pid> <unit_id> --agent a1 --summary "该单元的作用"

beom-workbench coverage <pid>                     # 文件·符号两个维度的完成率与失败项
beom-workbench unknowns <pid>                     # 剩余的未确认项
beom-workbench export <pid>                       # Markdown + JSON 报告
```

`finding.json` 示例：

```json
{"text": "创建订单时先锁定库存，支付失败时释放", "severity": "important",
 "evidence": [{"path": "src/order/service.rs", "start_line": 120, "end_line": 158}],
 "symbol_ids": ["s_1a2b3c"]}
```

## 快速开始 2: 移植功能并验证

示例：将原项目的 `POST /api/v1/orders` 用另一种语言移植到自己的仓库。

```bash
# 1. 查看移植范围 → 创建移植台账
beom-workbench slice <pid> "POST /api/v1/orders"
beom-workbench port-create <pid> --name 订单 --targets "POST /api/v1/orders" --language javascript --target-root <我的仓库>

# 2. 保存原项目响应（黄金基准） — 原项目在容器中隔离运行
beom-workbench port-capture <pid> <port_id> --cases cases.json \
    --source-image <原项目运行镜像> --source-url http://127.0.0.1:18001 --source-cmd "<原项目服务器启动命令>" \
    --coverage-cmd "<生成覆盖率报告的命令>" --coverage-report cov.json --explore 40

# 3. 编写自己的代码 → 逐项记录移植到了哪里
beom-workbench port-map <pid> <port_id> --json map.json

# 4. 比较验证（包括并发请求）
beom-workbench port-verify <pid> <port_id> --target-url http://127.0.0.1:18002 --target-cmd "node server.js" --concurrency 8

# 5. 进度与下一步
beom-workbench port-status <pid> <port_id>

# 6. 人工批准 — 由用户在终端中亲自执行（代理代为执行会被拒绝）
beom-workbench port-approve <pid> <port_id>

# 7. 完成判定 — 仅在 7 道门禁全部满足时签发
beom-workbench port-finish <pid> <port_id>
```

`map.json` 示例 — 移植（`ported`）需填写自己代码的 `路径:行号`，替代·省略需要填写理由。

```json
[{"item": "s_1a2b3c", "status": "ported", "target": "src/orders/create.js:12"},
 {"item": "s_4d5e6f", "status": "replaced", "note": "使用现有消息服务代替原项目的队列"},
 {"item": "s_7a8b9c", "status": "omitted", "note": "管理员统计不在本次范围内"}]
```

**修改后再引入移植的功能**

| 情况 | 方法 | 判定 |
|---|---|---|
| 必须与原项目一致 | 只编写用例 | 与原项目响应比较 |
| 有意做成不同（可以确定值） | 在用例中写 `expect` | 用期望值代替原项目进行比较，期望值需人工批准 |
| 原项目中没有的新行为 | `"new": true` + `expect` | 不运行原项目，按期望值判定 |
| 仅格式不同，无法确定值 | `port-accept --case <名称> --reason "<理由>"` | 提议允许差异 → 人工批准 |
| 去掉部分功能 | 在 `port-map` 中设为 `omitted` + 理由 | 人工批准 |

- 批准界面会并排显示原项目响应与期望值（或实际差异）。批准后内容若发生变化，需要重新批准。
- 如果无法对原项目进行覆盖率插桩，可用 `port-verify --target-coverage-cmd ... --target-coverage-report lcov.info` 测量自己代码一侧的执行范围。
- 修改自己的代码导致行号偏移时，用 `port-map --follow` 更新移植位置。

## 快速开始 3: 按需求构建

在应用中选择 **基于需求**，或使用 CLI：

```bash
beom-workbench make-create --name 订单总额 --request "接收数量并计算总额。每个 2000 韩元，10 个及以上优惠 10%，0 及以下返回 400" --target-root <目录>
beom-workbench make-req <pid> <port_id> --json reqs.json          # [{"text": "总额 = 数量 x 2000 韩元"}, …] → R1, R2 …
beom-workbench port-capture <pid> <port_id> --cases cases.json --explore 20   # 每条要求写 expect + req
beom-workbench port-approve <pid> <port_id>                       # 实现之前批准要求·验收标准
beom-workbench port-map <pid> <port_id> --json map.json           # 要求 → 实现位置 "路径:行号"
beom-workbench port-verify <pid> <port_id> --target-url http://127.0.0.1:8080 --target-cmd "<服务器启动命令>"
beom-workbench port-finish <pid> <port_id>
```

- 每个用例都需要 `req`（所验证的要求）和 `expect`（期望值），并且每条要求至少要有一个用例。
- 输入自动变异用例不看期望值，而是按**不崩溃·不返回 5xx** 判定（异常输入必须以 4xx 拒绝才算通过）。
- 如果已有要放入的仓库，先对其执行 `open`·`inventory`，再通过 `--context <该 project_id>` 传入，即可按现有结构构建。

在应用中，需求运行还会自己做以下事情:

- **工作深度**(输入框下方的 *深度* 按钮 — 需求·参考·策划): *简单* 不做参考界面调研和设计方案，只做所要求的。*标准* 每个决定都看各选项的优缺点
  来选，有界面时从两个设计方案中选择。*深入* 先审查八个方面 — 功能、使用流程·无障碍、集成·API、数据、性能·效率、成本·维护、安全·个人信息、
  运维·错误·验证 — 以及该领域特有的规定、惯例和陷阱，并针对第一轮调查产生的每个疑问再查一次。计划卡片会列出查证了什么、在哪里、决定了什么、
  还剩什么风险以及来源页面(工具包会核对这些页面确实被打开过)；找不到的就写找不到。需求里没有的功能作为可选建议给出。
- **交接前检查**: 对本次修改的文件运行项目自带的 lint、格式检查和类型检查(仅在项目具备时)；一个没看过对话的审查者用所涉及语言的检查
  清单阅读 diff。设计检查在设计方案和完成界面上测量无障碍与布局缺陷，以及 AI 默认生成的样子(标题上方的小标签、圆角卡片单侧色条、渐变
  文字、表情符号图标、卡片套卡片 …)，并由外部设计反模式检测器补充。
- **你教的内容**: 标为“应用到以后的任务”的话会被同一文件夹之后的运行遵循，你替换推荐所选的方案会作为偏好提供给下一个计划。对话超过模型
  上限的 60% 时，下一轮会带着工具的记录在新对话中开始。

## 修改原项目后使用 (定制)

```bash
beom-workbench extpoints <pid>                                  # 先找出不触碰核心即可插入的位置
beom-workbench impact <pid> pricing.calc_total --verify         # 修改后受影响的入口点·测试·结论
beom-workbench custom-add <pid> --description "修改折扣计算" --kind modify --targets pricing.calc_total

# 原项目发布新版本时
beom-workbench open <仓库> --ref v1.3.0 && beom-workbench inventory <新 pid>
beom-workbench compare <基准 pid> <新 pid> --save                # 变更之处 + 与我们的修改冲突之处
```

如需将修改后的原项目与原始原项目进行比较验证，请把修改后的仓库设为 `--target-root`，然后按照与 [快速开始 2](#快速开始-2-移植功能并验证) 相同的方法验证。

## 用例文件格式

```json
{
  "defaults": {"headers": {"Content-Type": "application/json"}},
  "cases": [
    {"name": "创建订单", "method": "POST", "path": "/api/v1/orders", "json": {"sku": "A1", "qty": 2},
     "match": {"id": "[0-9a-f]{32}"}, "ignore": ["createdAt"]},
    {"name": "查询订单", "method": "GET", "path": "/api/v1/orders/{{创建订单.id}}"},
    {"name": "数量为0", "method": "POST", "path": "/api/v1/orders", "json": {"sku": "A1", "qty": 0}, "compare": "status"},
    {"name": "应用折扣", "method": "POST", "path": "/api/v1/orders", "json": {"sku": "A1", "qty": 10},
     "expect": {"status": 201, "equals": {"total": 18000}}},
    {"name": "优惠券_新功能", "new": true, "method": "POST", "path": "/api/v1/orders",
     "json": {"sku": "A1", "qty": 1, "coupon": "WELCOME"}, "expect": {"equals": {"discount": 1000}}}
  ]
}
```

| 字段 | 含义 |
|---|---|
| `method`·`path`·`json`·`headers` | HTTP 用例 |
| `argv`·`stdin` | CLI 用例 |
| `call: {source, target}`·`args`·`kwargs` | 函数级别比较（`路径:函数名`，语言不同也可以） |
| `{{用例名称.字段}}` | 使用前面用例的结果值（名称中不能有空格） |
| `ignore` | 不参与比较的路径（`items.*.t`） |
| `match` | 用正则表达式代替值进行校验（id·时间等每次都不同的值） |
| `compare: "status"` | 只比较状态码 |
| `expect` | 用期望值代替原项目进行比较 — `status`·`body`·`equals`（路径: 值，数组写作 `items.0.price`）·`match` |
| `new: true` | 原项目中没有的行为（必须有 `expect`，不在原项目上运行） |
| `screen: {path \| file, mobile}` · `expect: {visible, selectors, no_errors}` | 界面用例 — 打开页面，检查文字、元素和脚本错误，并保存截图 |
| `screen: {app, actions}` · `expect: {visible, selectors: ["Button:计算"]}` | 原生应用界面 (Windows) — 启动应用，通过 UI Automation 读取文字和控件，输入、点击后保存窗口截图 |
| `description` | 用自然语言说明这个用例检查什么 — 代替名称显示 |
| `explore: false`·`concurrent: false` | 排除在输入自动变异·并发请求检查之外 |

表中没有的键在保存时会被拒绝 (像用 `args` 代替 `argv` 这样的错误键若被悄悄忽略，就会用别的输入来验证)。按需求模式下每个用例还需要 `req`。

## 命令一览

| 分类 | 命令 |
|---|---|
| 准备 | `doctor` `setup` `open` `list` `inventory` `classify` `agent-status` |
| 应用·自动运行 | `app` `run` `runs` `data` |
| 分析循环 | `next` `claim` `renew` `context` `finding` `withdraw` `mark` `unknown-add` `unknown-update` `complete` `review` `pending` |
| 精确确认 | `lsp`(L1) `dataflow`(L2) `runtime-ingest` `runtime-run` `runtime-runs`(L3) |
| 结果 | `coverage` `features` `units` `unknowns` `export` `license` |
| 定制 | `extpoints` `impact` `custom-add` `custom-list` `custom-update` `compare` |
| 按需求构建 | `make-create` `make-req`（之后与 port-* 命令通用） |
| 移植 | `slice` `port-create` `port-map` `port-capture` `port-verify` `port-accept` `port-status` `port-approve` `port-finish` |

各命令的选项见 `beom-workbench <命令> --help`。所有命令都会输出 `{ok, data, warnings, errors}` JSON，
失败时 `errors[0].recovery` 中会给出解决方法。面向代理的详细流程见技能文档（`beom-workbench` 技能的 SKILL.md·CLI.md）。

## 数据与存储位置

和 Claude Code (`~/.claude`)、Codex (`~/.codex`) 一样，所有数据放在主目录下的一个文件夹 (`~/.beom-workbench`，可用 `BEOM_WORKBENCH_HOME` 修改)，
内部按性质划分。不发送到任何地方，也不保存机密 — 密钥和令牌只从环境变量读取，并在输出中遮蔽。

| 文件夹 | 内容 | 可否删除 |
|---|---|---|
| `settings.json` | 用户设置 (清理周期、默认值) | 可以 — 恢复默认 |
| `projects/<id>/` | 分析/移植账本 `ledger.db` (SQLite、带模式版本、哈希链)、固定提交的原项目克隆、黄金响应 | **不可以** — 批准与验证记录 |
| `runs/<运行>/` | 一次自动运行 = 一段对话: `run.json` 状态、`events.jsonl` 流程 (只追加)、`inbox/` 提问和批准请求、`shadow.git` 目标文件夹检查点、`agent/` 每轮重新生成的代理设置 | 移到回收站 |
| `trash/` | 已删除的运行，7 天后自动清空 | 可以 |
| `app/` | 快捷方式使用的应用图标 | 不可以 |
| `cache/` · `cbm-cache/` | 代理状态、验证 harness、结构图索引 | 可以 — 会重建 |
| `work/` | 验证和覆盖率用的临时副本，用完即删 | 可以 |
| `bin/` · `engines/` · `node/` | `setup` 安装的分析引擎 | 可以 — 用 `setup` 重装 |
| `agent-home/` | 工具运行用的代理配置文件夹 — 登录与你的共用，设置·指令文件·技能·命令每轮复制，工具与代理的对话 | 可以 — 会重建 (登录从你的复制) |
| `lessons/` | 每个文件夹中标为“应用到以后的任务”的话，以及你替换推荐所选的方案 | 可以 — 之后的任务不带它们开始 |
| `tools/` | 外部设计反模式检测器 — 首次设计检查时安装 (固定版本，需要 npm) | 可以 — 需要时重装 |

工具运行时把代理的配置文件夹指向 `agent-home/` (`CLAUDE_CONFIG_DIR`、`CODEX_HOME`) 启动，因此这些对话不会出现在你自己的 Claude Code·Codex 历史中，并在那里通过会话 id 继续。
工具把工作文件放在目标文件夹的 `.beom/` 中 (验收用例、策划文档、截图)，不会提交它们 — 只提交程序本身。在 git 仓库中，`.beom/` 会写入该仓库的本地排除列表，因此 `git status` 中也看不到。

| | Claude Code | Codex | 本工具包 |
|---|---|---|---|
| 对话 | `projects/<文件夹>/<会话>.jsonl` | `sessions/YYYY/MM/DD/*.jsonl` | `runs/<运行>/events.jsonl` (+ 代理自己的对话记录) |
| 设置 | `settings.json` | `config.toml` | `settings.json` |
| 状态 | 文件变更历史 (检查点) | 带版本的 SQLite | 各项目的 SQLite 账本 (模式版本、哈希链) + 每次运行的 git 检查点 |
| 清理 | 默认 30 天后自动删除旧对话 | 保留 | 默认保留；可选在 30/90 天后自动清理已完成或已隐藏的运行；回收站 7 天后清空 |

删除运行不会删除批准和验证结果 — 它们留在项目账本里。

```bash
beom-workbench data                        # 什么东西在哪里、有多大
beom-workbench data --trash <运行>          # 把运行移到回收站 (--restore <名称>、--empty-trash)
beom-workbench data --prune 30 --dry-run   # 30 天未变动的已完成或已隐藏运行
beom-workbench data --set-cleanup 30       # 启动时自动清理 (0 = 关闭)
```

在应用中: 侧边栏底部的 **存储空间**，运行的 ⋯ 菜单里的 **移到回收站**。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `BEOM_WORKBENCH_HOME` | `~/.beom-workbench` | 台账·clone·引擎·备份的位置 |
| `BEOM_WORKBENCH_CONTAINER` | `auto` | 隔离运行引擎 `auto`·`docker`·`wsl`·`none` |
| `BEOM_WORKBENCH_WSL_DISTRO` | `beom-sandbox` | 在 Windows 上使用的 WSL 发行版 |
| `BEOM_WORKBENCH_SANDBOX_DIR` | `%LOCALAPPDATA%\beom-workbench-sandbox` | Windows：内置隔离运行记录已授权文件夹的位置 |
| `BEOM_WORKBENCH_NO_UPDATE_CHECK` | — | 设为 `1` 时应用不再检查新版本 (公开发布列表) |
| `BEOM_WORKBENCH_LEASE_SECONDS` | `3600` | 分析单元的租用时长 |
| `BEOM_WORKBENCH_UNIT_MAX_LINES` / `_UNIT_MAX_CHARS` | `1500` / `45000` | 分析单元的大小上限 |
| `BEOM_WORKBENCH_CONTEXT_MAX_CHARS` | `24000` | `context` 单页大小 |
| `BEOM_WORKBENCH_LSP_TIMEOUT_SECONDS` / `_LSP_IDLE_SECONDS` | `900` / `600` | 语言服务器响应等待·空闲退出 |
| `BEOM_WORKBENCH_RULEPACKS` | — | 额外的入口点规则目录 |
| `BEOM_WORKBENCH_CBM` · `_ASTGREP` · `_JOERN` · `_JAVA_HOME` | 自动查找 | 直接指定引擎可执行文件 |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | 安装技能的 Claude Code 配置目录 |

## 安全原则

- **不运行被分析的代码。** 语言服务器的构建脚本·宏执行也会被关闭。需要运行的验证（覆盖率·移植原项目）
  在容器（网络隔离）中运行，或仅对用户声明信任的仓库在临时副本中运行。
- **不留下任何进程。** 一轮中代理在目标文件夹启动并遗留的进程 (开发服务器、监视器) 会在该轮结束时停止；你在该文件夹打开的终端和编辑器不受影响。
- **不读取机密文件**，普通文件中的机密值（密码·密钥·令牌·URL 凭据）在输出时以 `****` 遮盖。
  运行环境变量中的令牌·密钥类也会被剔除后再传递。
- 分析**只在专用 clone 的固定提交**上进行。正在工作的原项目目录中未提交的更改不会被反映。
- 移植的人工批准只通过桌面应用、MCP 确认窗口或用户的终端输入获取 — 不接受自动运行中的代理。

## 已知限制

| 限制 | 应对 |
|---|---|
| 差分验证只能证明实际运行过的输入 | 通过输入自动变异·覆盖率扩大范围，剩余范围记录在证据的 limitations 中。不比较性能 |
| 覆盖率只能证明“被执行过” | 具体是哪个分支·输入，需要用用例另行确认 |
| 基于名称的调用关系只是候选 | 需通过语言服务器（L1）或运行（L3）确认后才算证据 |
| 动态分派·反射·宏生成的代码 | 自动标记为未确认 |
| 数据流引擎首次运行较慢（1 万行 Java 约 15 分钟） | 缓存，可用 `dataflow --build-only` 预先生成 |
| CLI 每次调用都会重新启动语言服务器 | 同一语言在进程间排队。MCP 模式会复用服务器 |
| 随配置值变化的路径 | 按默认值展开并标记为未确认 |
| 由于是本地程序，若有人以相同权限蓄意伪造记录则无法阻止 | 伪造尝试会以门禁失败的形式暴露。最终判定可以在干净的环境中重新运行 |

## 许可

**允许使用，禁止修改** — [LICENSE](LICENSE)（这不是开源许可证）。本译文仅供参考，以韩文 LICENSE 原文为准。

- 任何人都可以在未修改的状态下安装·使用（包括公司业务·商业用途）。使用本工具包产出的成果归用户所有。
- 修改·创作衍生作品·分发修改版·将部分代码纳入其他软件，均需获得作者的书面许可。
- 改进建议请通过本仓库的 Issue·Pull Request 提交 (Pull Request 不会直接合并，由维护者采纳并反映)。

所使用的库·外部引擎各自遵循其自身的许可 — [THIRD_PARTY.md](THIRD_PARTY.md)。
