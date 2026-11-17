# beom-workbench 설치 (Windows) — 앱과 필수 엔진까지 설치하고, 시작 메뉴·바탕화면 바로가기를 만들지 묻는다.
#   irm https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.ps1 | iex
# 묻지 않고 정하려면 먼저 $env:BEOM_SHORTCUT = "start,desktop" (또는 "start", "none") 를 두고 실행한다.
# 다른 브랜치·로컬 폴더에서 설치하려면 $env:BEOM_SOURCE = "git+https://github.com/ParkBeomsoo/beom-workbench.git@<브랜치>" (또는 폴더 경로)
# 안내 문구는 운영체제 언어(한국어·영어·일본어·중국어)로, 그 밖의 언어면 한국어로 보인다 — 앱과 같은 규칙.
$ErrorActionPreference = "Stop"
$source = if ($env:BEOM_SOURCE) { $env:BEOM_SOURCE } else { "git+https://github.com/ParkBeomsoo/beom-workbench.git" }
$spec = if (Test-Path $source) { "$((Resolve-Path $source).Path)[app]" } else { "beom-workbench[app] @ $source" }

$lang = (Get-UICulture).TwoLetterISOLanguageName
if ($lang -notin @("ko", "en", "ja", "zh")) { $lang = "ko" }
$M = @{
  needPy    = @{ ko = "Python 3.11 이상이 필요합니다 — https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행해 주세요."; en = "Python 3.11 or later is required — install it from https://www.python.org/downloads/ and run this again."; ja = "Python 3.11 以上が必要です — https://www.python.org/downloads/ からインストールしてから、もう一度実行してください。"; zh = "需要 Python 3.11 或更高版本 — 请从 https://www.python.org/downloads/ 安装后重新运行。" }
  pipx      = @{ ko = "pipx 를 설치합니다…"; en = "Installing pipx…"; ja = "pipx をインストールします…"; zh = "正在安装 pipx…" }
  pipxFail  = @{ ko = "pipx 설치에 실패했습니다: python -m pip install --user pipx"; en = "Could not install pipx: python -m pip install --user pipx"; ja = "pipx のインストールに失敗しました: python -m pip install --user pipx"; zh = "pipx 安装失败: python -m pip install --user pipx" }
  install   = @{ ko = "beom-workbench 를 설치합니다 (앱 포함)…"; en = "Installing beom-workbench (with the app)…"; ja = "beom-workbench をインストールします (アプリを含む)…"; zh = "正在安装 beom-workbench (含应用)…" }
  fail      = @{ ko = "설치에 실패했습니다."; en = "Installation failed."; ja = "インストールに失敗しました。"; zh = "安装失败。" }
  engines   = @{ ko = "필수 엔진(구조 지도·진입점 규칙)을 설치합니다 — 내려받은 파일은 체크섬으로 확인합니다…"; en = "Installing the required engines (structure map, entry-point rules) — downloads are checked against checksums…"; ja = "必須エンジン (構造マップ・エントリポイントルール) をインストールします — ダウンロードしたファイルはチェックサムで確認します…"; zh = "正在安装必需引擎 (结构图、入口规则) — 下载的文件会用校验和核对…" }
  engFail   = @{ ko = "엔진 확인에 실패했습니다 — 'beom-workbench doctor' 로 무엇이 빠졌는지 보세요."; en = "Engine check failed — run 'beom-workbench doctor' to see what is missing."; ja = "エンジンの確認に失敗しました — 'beom-workbench doctor' で不足しているものを確認してください。"; zh = "引擎检查失败 — 运行 'beom-workbench doctor' 查看缺少什么。" }
  ask       = @{ ko = "시작 메뉴와 바탕화면에 바로가기를 만들까요? [Y: 둘 다 / s: 시작 메뉴만 / n: 안 함]"; en = "Create shortcuts in the Start menu and on the desktop? [Y: both / s: Start menu only / n: none]"; ja = "スタートメニューとデスクトップにショートカットを作成しますか? [Y: 両方 / s: スタートメニューのみ / n: 作成しない]"; zh = "要在开始菜单和桌面创建快捷方式吗? [Y: 两处都要 / s: 仅开始菜单 / n: 不创建]" }
  scOk      = @{ ko = "바로가기를 만들었습니다 — 시작 메뉴에서 'beom' 으로 검색하세요."; en = "Shortcuts created — search for 'beom' in the Start menu."; ja = "ショートカットを作成しました — スタートメニューで 'beom' を検索してください。"; zh = "已创建快捷方式 — 在开始菜单中搜索 'beom'。" }
  scFail    = @{ ko = "바로가기를 만들지 못했습니다. 나중에 'beom-workbench app --shortcut' 으로 만들 수 있습니다."; en = "Could not create shortcuts. You can create them later with 'beom-workbench app --shortcut'."; ja = "ショートカットを作成できませんでした。後で 'beom-workbench app --shortcut' で作成できます。"; zh = "无法创建快捷方式。之后可以用 'beom-workbench app --shortcut' 创建。" }
  done      = @{ ko = "앱 열기: beom-workbench-app    /    상태 확인: beom-workbench doctor   (새 터미널에서 PATH 가 반영됩니다)"; en = "Open the app: beom-workbench-app    /    Check status: beom-workbench doctor   (PATH applies in a new terminal)"; ja = "アプリを開く: beom-workbench-app    /    状態確認: beom-workbench doctor   (PATH は新しいターミナルで反映されます)"; zh = "打开应用: beom-workbench-app    /    检查状态: beom-workbench doctor   (PATH 在新终端中生效)" }
}
function T($key) { $M[$key][$lang] }

# pipx — 없으면 Python 으로 설치한다 (PATH 반영 전이라도 python -m pipx 로 바로 쓴다)
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
function Invoke-Pipx { if (Get-Command pipx -ErrorAction SilentlyContinue) { pipx @args } else { & $py.Source -m pipx @args } }
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    if (-not $py) { Write-Host (T "needPy") -ForegroundColor Yellow; exit 1 }
    Write-Host (T "pipx")
    & $py.Source -m pip install --user --quiet pipx
    if ($LASTEXITCODE -ne 0) { Write-Host (T "pipxFail") -ForegroundColor Red; exit 1 }
    & $py.Source -m pipx ensurepath | Out-Null
}

Write-Host (T "install")
Invoke-Pipx install --force $spec
if ($LASTEXITCODE -ne 0) { Write-Host (T "fail") -ForegroundColor Red; exit 1 }
$bin = (Invoke-Pipx environment --value PIPX_BIN_DIR).Trim()
$wb = Join-Path $bin "beom-workbench.exe"

Write-Host (T "engines")
& $wb setup --apply codebase-memory-mcp ast-grep | Out-Null
& $wb doctor | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host (T "engFail") -ForegroundColor Yellow }

$choice = $env:BEOM_SHORTCUT
if (-not $choice) {
    $ans = Read-Host (T "ask")
    $choice = switch -Regex ($ans) { "^[sS]" { "start" } "^[nN]" { "none" } default { "start,desktop" } }
}
if ($choice -ne "none") {
    & $wb app --shortcut $choice | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host (T "scOk") -ForegroundColor Green }
    else { Write-Host (T "scFail") -ForegroundColor Yellow }
}
Write-Host (T "done")
