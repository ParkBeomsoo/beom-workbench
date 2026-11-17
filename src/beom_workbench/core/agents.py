"""에이전트 연결부 — Claude Code · Codex · Gemini CLI 를 같은 모양으로 다룬다.

각 연결부가 하는 일: 설치·로그인 확인, 한 라운드 실행 명령 만들기(이어서 실행 포함), 출력 이벤트를 공통 형식으로 바꾸기.
공통 이벤트: {"kind": "init"|"text"|"tool"|"result"|"error", ...}

권한 처리 차이:
- Claude Code: --permission-prompt-tool 로 작업마다 우리 규칙(inbox.py)이 허용·물어보기·거절을 정한다.
- Codex: 헤드리스 실행에서 작업별 승인 연결이 없어 샌드박스로 막는다 — 대상 폴더 안에서만 쓰기, 웹은 규칙이 허용일 때만.
- Gemini CLI: 헤드리스에서 물어볼 작업은 거절된다 — 샌드박스 + 허용 목록. (실험적)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MCP_NAME = "beom-workbench"
_EFFORT_KO = {"minimal": "최소", "low": "낮음", "medium": "보통", "high": "높음", "xhigh": "매우 높음", "max": "최대", "ultra": "울트라"}
MCP_STARTUP_SEC = 60
MCP_TOOL_SEC = 24 * 3600  # wb_person_ask·wb_action_approve 는 사람이 답할 때까지 기다린다


def _mcp_server(settings, run: dict) -> dict:
    # 사용자가 정한 툴킷 설정(BEOM_WORKBENCH_* — 엔진 경로·격리 기록 위치·시간 제한 등)을 도구 서버에 그대로 넘긴다.
    # Codex 는 도구 서버를 여기 적은 환경 변수로만 띄워서, 넘기지 않으면 설정이 무시된다 (예: 격리 기록이 기본 위치로 감)
    env = {k: v for k, v in os.environ.items() if k.startswith("BEOM_WORKBENCH_") and k != "BEOM_WORKBENCH_RUN"}
    env.update({"BEOM_WORKBENCH_RUN": run["id"], "BEOM_WORKBENCH_HOME": str(settings.home)})
    return {"command": sys.executable, "args": ["-m", "beom_workbench.main"], "env": env}


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _tail_value(files: list[Path], pick, tail: int = 256_000):
    """JSONL 파일 끝부분에서 pick(event) 가 돌려주는 마지막 값 — 큰 대화 기록도 끝만 읽는다."""
    for f in files:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - tail))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                v = pick(json.loads(line))
            except (ValueError, AttributeError):
                continue
            if v:
                return v
    return None


def _newest_codex_sessions(root: Path, n: int = 3) -> list[Path]:
    """sessions/YYYY/MM/DD/*.jsonl — 가장 최근 날짜 폴더부터 몇 개만 (전체를 훑지 않는다)."""
    out: list[Path] = []
    try:
        for y in sorted(root.iterdir(), reverse=True):
            for m in sorted(y.iterdir(), reverse=True):
                for d in sorted(m.iterdir(), reverse=True):
                    out += sorted(d.glob("*.jsonl"), key=_mtime, reverse=True)
                    if len(out) >= n:
                        return out[:n]
    except OSError:
        pass
    return out[:n]


_FAMILY_KO = {"opus": "복잡한 작업·일상 작업", "fable": "가장 강함 — 사용량이 훨씬 빨리 줄어듦", "sonnet": "간단한 작업에 효율적",
              "haiku": "가장 빠름"}
_EFFORT_DESC = {"low": "빠르게", "medium": "일상 작업", "high": "복잡한 작업", "xhigh": "더 깊게", "max": "가장 어려운 문제"}


def _claude_catalog() -> list[dict]:
    """Claude Code 가 계정 기준으로 받아 둔 모델 목록 (~/.claude/cache/model-catalog/*.json) — 쓸 수 있는 모델·이름·설명·
    생각 깊이 단계·기본 깊이. 로그인 정보는 읽지 않는다. CLI 화면(cc)의 가장 최근 목록을 먼저, 없으면 다른 화면의 목록."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "cache" / "model-catalog"
    try:
        files = [f for f in home.glob("*.json") if not f.name.startswith("published")]
    except OSError:
        return []
    files.sort(key=lambda f: (f.stem.endswith("-cc"), _mtime(f)), reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            models = ((data.get("catalog") or {}).get("config") or {}).get("models") or []
        except (OSError, ValueError, AttributeError):
            continue
        out = []
        for m in models:
            if not isinstance(m, dict) or not str(m.get("id", "")).startswith("claude-"):
                continue
            rt = m.get("runtime") or {}
            opts = ((m.get("thinking") or {}).get("effort_options") or [])
            levels = [x.get("id") for x in opts if isinstance(x, dict) and x.get("id")] or rt.get("effort_levels") or []
            fam = rt.get("family") or (m.get("short_name") or "").lower()
            out.append({"id": m["id"], "label": m.get("name") or m["id"], "short": m.get("short_name") or "", "main": m.get("section") == "main",
                        "note": _FAMILY_KO.get(fam) or m.get("description") or "", "family": fam,
                        "default_effort": rt.get("default_effort"),
                        "levels": [{"id": x, "label": _EFFORT_KO.get(x, x), "desc": _EFFORT_DESC.get(x, "")} for x in levels]})
        if out:
            # 최신 모델이 먼저, 이전 모델은 '이전 모델 더 보기'로 접어 둔다 (Claude Code 의 모델 고르기와 같은 구성)
            for m in out:
                if not m["main"] and any(x["main"] for x in out):
                    m["older"] = True
            return sorted(out, key=lambda m: not m["main"])
    return []


# 저장소 분석용 도구가 든 모듈 — 원본·넣을 저장소가 있을 때(오픈소스 모드, 분석 붙은 요청사항·기획)만 쓴다
ANALYSIS_MODULES = ("customize", "engines", "findings", "inventory", "loop", "report", "runtime", "session")


def analysis_tools() -> list[str]:
    """그 모듈들의 MCP 도구 이름 — 모듈 소스에서 읽는다 (도구를 더하면 저절로 따라온다)."""
    import re

    base = Path(__file__).resolve().parent.parent / "modules"
    names: list[str] = []
    for m in ANALYSIS_MODULES:
        try:
            src = (base / f"{m}.py").read_text(encoding="utf-8")
        except OSError:
            continue
        names += re.findall(r"@mcp\.tool[^\n]*\n(?:@[^\n]*\n)*(?:async\s+)?def\s+(wb_\w+)", src)
    return names


def needs_analysis(run: dict) -> bool:
    return run.get("mode") == "reference" or bool(run.get("context_project"))


def _claude_alias(model: str) -> str:
    """claude-opus-5-5 → opus (Claude Code 가 받는 별칭). 별칭이 없는 모델은 그대로."""
    m = model.lower()
    for a in ("fable", "opus", "sonnet", "haiku"):
        if a in m:
            return a
    return model


def _version_key(p: Path) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", p.parent.name)[:3]) or (0,)


def _node_global_bins(name: str) -> list[Path]:
    """npm 전역 설치 위치 후보 — PATH 의 Node 가 바뀌어도(nvm use 등) 다른 버전에 깔린 CLI 를 찾는다. 새 버전 먼저.
    Windows: %APPDATA%\\npm, nvm-windows(NVM_HOME\\v*), fnm, volta. 그 밖: ~/.npm-global, nvm(~/.nvm/versions/node/*/bin), volta, Homebrew."""
    home, out = Path.home(), []
    if sys.platform == "win32":
        exe = [f"{name}.cmd", f"{name}.exe"]
        appdata, local = Path(os.environ.get("APPDATA", "")), Path(os.environ.get("LOCALAPPDATA", ""))
        dirs = [appdata / "npm", home / ".volta" / "bin", local / "Volta" / "bin"]
        nvm = Path(os.environ.get("NVM_HOME") or appdata / "nvm")
        dirs += sorted((d for d in nvm.glob("v*") if d.is_dir()), key=lambda d: _version_key(d / "x"), reverse=True)
        dirs += sorted((d for d in (appdata / "fnm" / "node-versions").glob("*/installation")), key=lambda d: _version_key(d), reverse=True)
    else:
        exe = [name]
        dirs = [home / ".npm-global" / "bin", home / ".volta" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
        dirs += sorted((home / ".nvm" / "versions" / "node").glob("*/bin"), key=_version_key, reverse=True)
    for d in dirs:
        out += [d / e for e in exe]
    return out


# 공식 단독 설치 명령 — Node·npm 없이 설치된다 (각 회사 문서의 첫 번째 설치 방법)
INSTALL = {
    "claude": {"win32": "irm https://claude.ai/install.ps1 | iex", "other": "curl -fsSL https://claude.ai/install.sh | bash"},
    "codex": {"win32": "irm https://chatgpt.com/codex/install.ps1 | iex", "other": "curl -fsSL https://chatgpt.com/codex/install.sh | sh"},
}


def install_command(agent_id: str) -> str | None:
    table = INSTALL.get(agent_id) or ({"win32": AGY_INSTALL["win32"], "other": AGY_INSTALL["other"]} if agent_id == "gemini" else None)
    return table["win32" if sys.platform == "win32" else "other"] if table else None


_PATH_AT = {"t": 0.0}


def refresh_path(max_age: float = 30) -> None:
    """PATH 를 저장된 설정에서 다시 읽어 합친다 — 앱을 켠 뒤에 설치했거나 nvm use 로 Node 를 바꿔도 다시 켜지 않고 찾게.
    Windows: 사용자·시스템 환경 변수(레지스트리) — 새 폴더만 뒤에 붙인다. macOS·Linux: 로그인 셸의 PATH 를 앞에 둔다."""
    import time
    if time.monotonic() - _PATH_AT["t"] < max_age:
        return
    _PATH_AT["t"] = time.monotonic()
    fresh: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        val, _ = winreg.QueryValueEx(k, "Path")
                except OSError:
                    continue
                fresh += [os.path.expandvars(x) for x in str(val).split(";") if x.strip()]
        except ImportError:
            return
    else:
        shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
        try:
            out = subprocess.run([shell, "-ilc", 'printf "__OSS_PATH__%s" "$PATH"'], capture_output=True, timeout=8,
                                 stdin=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return
        if "__OSS_PATH__" in out:
            fresh = [x for x in out.rsplit("__OSS_PATH__", 1)[-1].strip().split(os.pathsep) if x]
    have = os.environ.get("PATH", "").split(os.pathsep)
    if sys.platform == "win32":
        # 레지스트리 값(사용자 → 시스템)은 실제 순서가 아니다 — 지금 순서는 그대로 두고 새로 생긴 폴더만 뒤에 붙인다.
        # 앞에 끼우면 같은 이름의 명령이 다른 것으로 바뀐다 (예: bash 가 Git Bash 대신 WSL 실행기로, python 이 스토어 안내 파일로)
        seen = {os.path.normcase(os.path.normpath(x)) for x in have if x}
        merged = have + [x for x in dict.fromkeys(fresh) if os.path.normcase(os.path.normpath(x)) not in seen]
    else:  # 로그인 셸의 PATH 가 사용자가 정한 순서 그대로 — 앞에 둔다 (nvm use 로 바꾼 Node 가 이기게)
        merged = list(dict.fromkeys(fresh + have))
    if merged != have:
        os.environ["PATH"] = os.pathsep.join(merged)


def _codex_native(shim: str | None) -> str | None:
    """npm 으로 깐 codex(.cmd·셸 스크립트 → node → 네이티브)에서 패키지 안의 네이티브 실행 파일을 찾는다 — Node 없이 바로 돈다."""
    if not shim:
        return None
    base = Path(shim).resolve().parent
    exe = "codex.exe" if sys.platform == "win32" else "codex"
    roots = [base / "node_modules" / "@openai" / "codex", base.parent / "lib" / "node_modules" / "@openai" / "codex"]
    for r in roots:
        hits = sorted(r.glob(f"node_modules/@openai/codex-*/vendor/*/bin/{exe}")) + sorted(r.glob(f"vendor/*/bin/{exe}"))
        if hits:
            return str(hits[0])
    return None


def _codex_standalone() -> list[Path]:
    """공식 단독 설치본·Codex 데스크톱 앱에 든 CLI 후보."""
    home, exe = Path.home(), ("codex.exe" if sys.platform == "win32" else "codex")
    out = [home / ".local" / "bin" / exe, home / ".codex" / "bin" / exe]
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        out += [local / "Programs" / "OpenAI" / "Codex" / "bin" / exe,  # 공식 단독 설치 기본 위치
                local / "Programs" / "codex" / exe, local / "OpenAI" / "Codex" / "cli" / exe]
        app = sorted((local / "OpenAI" / "Codex" / "bin").glob(f"*/{exe}"), key=lambda p: p.stat().st_mtime, reverse=True)
        out += app  # Codex 데스크톱 앱이 쓰는 CLI (가장 최근 것)
    else:
        out += [Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")]
    return out


def _which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# 에이전트 CLI 의 자동 업데이트 끄기 — 앱이 로그인 확인·실행으로 띄울 때 업데이트 프로세스가 '자기 창'으로 떠서
# (agy: auto_updater 가 새 콘솔로 띄움 — 기록으로 확인함) 사용자 화면에 터미널이 튀어나왔다. 업데이트는 사용자가 직접 실행할 때.
QUIET_ENV = {"AGY_CLI_DISABLE_AUTO_UPDATE": "true", "DISABLE_AUTOUPDATER": "1"}


def quiet_env(base: dict | None = None) -> dict:
    return {**(base if base is not None else os.environ), **QUIET_ENV}


def _run_quiet(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, env=quiet_env())
    except (OSError, subprocess.TimeoutExpired):
        return None


class Agent:
    id = ""
    name = ""
    permission_hook = False
    note = ""
    # 지시문을 표준 입력으로 넘긴다 — Windows 의 .cmd 실행 파일(npm 설치)은 여러 줄 인자를 첫 줄에서 자른다
    stdin_prompt = False
    # 'API 키' 인증일 때 읽는 환경 변수 — 로그인 대신 키로 과금될 뿐 할 수 있는 일은 같다
    api_key_env: tuple[str, ...] = ()

    def __init__(self, exe=None):
        self.exe = exe or self.find()

    def find(self) -> str | None:
        return None

    def status(self) -> dict:
        return {"id": self.id, "name": self.name, "ready": False, "exe": self.exe}

    def base(self) -> list[str]:
        return self.exe if isinstance(self.exe, list) else [self.exe]

    def command(self, settings, run: dict, prompt: str, rules: str) -> tuple[list[str], dict]:
        raise NotImplementedError

    def parse(self, ev: dict) -> list[dict]:
        return []

    def models(self) -> list[dict]:
        """고를 수 있는 모델 — 첫 항목(id 빈 값)은 에이전트 기본값."""
        return [{"id": "", "label": "기본값"}]

    def efforts(self) -> dict[str, list[dict]]:
        """모델별로 고를 수 있는 생각 깊이(노력). 키 "" 는 기본 모델. 비어 있으면 이 에이전트는 설정 없음."""
        return {}

    def current(self) -> dict:
        """모델·노력을 고르지 않았을 때 실제로 쓰이는 값 — 에이전트 설정 파일, 없으면 최근에 실제로 쓴 모델."""
        return {}

    def env(self, run: dict) -> dict:
        return quiet_env()


# ---------------------------------------------------------------- Claude Code

class ClaudeCode(Agent):
    # 이어 받은 세션(--resume)의 total_cost_usd 는 그 세션 전체 누적값이다 (호출마다가 아님) — 실행기가 차이만 더한다
    cost_cumulative = True
    id, name, permission_hook = "claude", "Claude Code", True
    api_key_env = ("ANTHROPIC_API_KEY",)
    TOOL_PREFIX = f"mcp__{MCP_NAME}__"

    def find(self) -> str | None:
        from beom_workbench.core import runner

        return runner.find_claude()

    def status(self) -> dict:
        from beom_workbench.core import runner

        st = runner.agent_status()
        return {"id": self.id, "name": self.name, "ready": st["ready"], "exe": st.get("claude"),
                "detail": st.get("auth_method"), "hint": st.get("hint"), "login": st.get("login_command"), "install": st.get("install")}

    stdin_prompt = True

    def command(self, settings, run, prompt, rules):
        from beom_workbench.core import inbox

        # 여러 줄·따옴표 인자는 npm 설치본(claude.cmd)에서 깨진다 — 규칙·MCP 설정은 파일로, 지시는 표준 입력으로
        d = inbox.run_dir(settings, run["id"]) / "agent"  # 라운드마다 다시 만드는 설정 — 기록과 분리
        d.mkdir(parents=True, exist_ok=True)
        mcp_file, rules_file = d / "claude-mcp.json", d / "claude-rules.md"
        mcp_file.write_text(json.dumps({"mcpServers": {MCP_NAME: _mcp_server(settings, run)}}, ensure_ascii=False),
                            encoding="utf-8")
        rules_file.write_text(rules, encoding="utf-8")
        from beom_workbench.core import sandbox
        sandboxed = sandbox.wraps_powershell(run)
        # --include-partial-messages: 긴 문서를 한 번에 쓰는 동안에도 조각이 흘러 '쓰는 중 · N자'를 보여 주고 무응답 감시가 끊지 않게
        argv = [*self.base(), "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--mcp-config", str(mcp_file), "--strict-mcp-config",
                "--permission-prompt-tool", f"{self.TOOL_PREFIX}wb_action_approve",
                # 읽기도 규칙을 거친다 (대상 폴더 안은 허용, 밖은 물어보기) — 미리 허용하면 툴킷 원장·질문함까지 읽힌다
                "--allowedTools", ",".join(["TodoWrite", f"{self.TOOL_PREFIX}*"]),
                "--disallowedTools", ",".join(["AskUserQuestion", f"{self.TOOL_PREFIX}wb_port_approve",
                                               *(["Bash"] if sandboxed else []),  # 격리 실행: Git Bash 는 샌드박스에서 못 돈다 → PowerShell
                                               *([] if needs_analysis(run) else [self.TOOL_PREFIX + t for t in analysis_tools()])]),
                "--append-system-prompt-file", str(rules_file), "--max-turns", str(run.get("max_turns", 60))]
        if run.get("model"):
            argv += ["--model", run["model"]]
        if run.get("effort"):
            argv += ["--effort", run["effort"]]
        if run.get("session_id"):
            argv += ["--resume", run["session_id"]]
        if sys.platform != "win32" and sandbox.active(run):  # macOS·Linux: Claude Code 자체 샌드박스
            sfile = d / "claude-settings.json"
            sfile.write_text(json.dumps(sandbox.claude_settings(run), ensure_ascii=False), encoding="utf-8")
            argv += ["--settings", str(sfile)]
        env = self.env(run)
        if sandboxed:
            env["CLAUDE_CODE_USE_POWERSHELL_TOOL"] = "1"  # 셸 도구를 PowerShell 로 — 권한 처리기가 샌드박스로 감싼다
        return argv, env

    def stdin_text(self, run: dict, prompt: str, rules: str) -> str:
        return prompt

    def models(self) -> list[dict]:
        """계정에서 쓸 수 있는 모델 — Claude Code 가 받아 둔 목록에서. 목록이 없으면(한 번도 안 열었거나 오래된 버전) 별칭으로
        (별칭은 Claude Code 가 그때의 최신 모델로 풀어 준다)."""
        base = [{"id": "", "label": "기본값", "note": "계정 설정을 따름"}]
        cat = _claude_catalog()
        if cat:
            return base + cat
        return base + [{"id": "opus", "label": "Opus", "note": "강함"}, {"id": "sonnet", "label": "Sonnet", "note": "균형"},
                       {"id": "haiku", "label": "Haiku", "note": "빠름"}]

    def current(self) -> dict:
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        try:
            s = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        if not isinstance(s, dict):
            s = {}
        def effort_for(model: str) -> str | None:
            """생각 깊이 — 모델별 설정(modelSettings["claude-opus-5-5"].effortLevel)이 있으면 그것, 없으면 전체 값."""
            per = s.get("modelSettings") if isinstance(s.get("modelSettings"), dict) else {}
            alias = _claude_alias(model or "")
            for key, v in per.items():
                if isinstance(v, dict) and v.get("effortLevel") and (key == model or _claude_alias(key) == alias):
                    return v["effortLevel"]
            return s.get("effortLevel")

        def resolve(model: str) -> str:
            alias = _claude_alias(model)
            for m in _claude_catalog():
                if model == m["id"] or (alias in (m["family"], m["short"].lower()) and alias != model) or model == m["family"]:
                    return m["id"]
            return alias

        if s.get("model"):
            return {"model": resolve(s["model"]), "effort": effort_for(s["model"]), "source": "Claude 설정"}
        dirs = sorted((d for d in (home / "projects").glob("*") if d.is_dir()), key=_mtime, reverse=True)[:3]
        files = sorted((f for d in dirs for f in d.glob("*.jsonl")), key=_mtime, reverse=True)[:3]
        used = _tail_value(files, lambda e: (e.get("message") or {}).get("model"))
        if used and used != "<synthetic>":
            return {"model": resolve(used), "full": used, "effort": effort_for(used), "source": "최근 사용"}
        return {}

    def efforts(self) -> dict[str, list[dict]]:
        levels = [{"id": x, "label": _EFFORT_KO[x], "desc": _EFFORT_DESC[x]} for x in ("low", "medium", "high", "xhigh", "max")]
        out = {}
        for m in self.models():
            out[m["id"]] = m.get("levels") or levels
            if m.get("family"):  # 설정·기록의 별칭(opus 등)으로도 찾게
                out.setdefault(m["family"], out[m["id"]])
        return out

    def env(self, run):
        env = quiet_env()
        if run.get("auth") != "api_key":
            env.pop("ANTHROPIC_API_KEY", None)  # 키가 있으면 -p 는 항상 키를 쓴다 — 구독 로그인으로 돌게 뺀다
        env.setdefault("MCP_TIMEOUT", str(MCP_STARTUP_SEC * 1000))
        env.setdefault("MCP_TOOL_TIMEOUT", str(MCP_TOOL_SEC * 1000))
        return env

    def parse(self, ev):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            return [{"kind": "init", "session_id": ev.get("session_id"),
                     "mcp": {s.get("name"): s.get("status") for s in ev.get("mcp_servers") or []}}]
        if t == "assistant":
            out = []
            sub = bool(ev.get("parent_tool_use_id"))  # 하위 에이전트(Task·Agent)의 글 — 사용자에게 하는 말이 아니다 (영어 보고 등)
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip() and not sub:
                    out.append({"kind": "text", "text": b["text"].strip()[:4000]})
                elif b.get("type") == "tool_use":
                    out.append({"kind": "tool", "tool": b.get("name"), "input": b.get("input")})
            return out
        if t == "stream_event":  # 조각 — 기록하지 않고 '쓰는 중' 표시에만 쓴다
            e = ev.get("event") or {}
            if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "tool_use":
                return [{"kind": "progress", "tool": e["content_block"].get("name"), "chars": 0}]
            d = e.get("delta") or {}
            piece = d.get("text") or d.get("partial_json") or d.get("thinking") or ""
            return [{"kind": "progress", "chars": len(piece), "thinking": d.get("type") == "thinking_delta"}] if piece else []
        if t == "result":
            return [{"kind": "result", "ok": not ev.get("is_error"), "subtype": ev.get("subtype"),
                     "session_id": ev.get("session_id"), "cost_usd": ev.get("total_cost_usd"), "turns": ev.get("num_turns"),
                     "text": (ev.get("result") or "")[:2000]}]
        return []


# ---------------------------------------------------------------- Codex

class Codex(Agent):
    id, name = "codex", "Codex"
    api_key_env = ("CODEX_API_KEY", "OPENAI_API_KEY")
    stdin_prompt = True
    note = "작업별 승인 대신 샌드박스로 막습니다 (대상 폴더 안에서만 쓰기, 웹은 규칙이 허용일 때만)."

    def find(self) -> str | None:
        """네이티브 실행 파일을 먼저 — npm 껍데기는 PATH 의 Node 버전에 따라 깨진다 (nvm use 등)."""
        refresh_path()
        found = _which("codex")
        if found and Path(found).suffix.lower() not in (".cmd", ".ps1", ".bat") and "node_modules" not in found \
                and not (Path(found).resolve().parent / "node_modules").exists():
            return found  # 단독 설치본·Homebrew
        native = _codex_native(found) or next((n for p in _node_global_bins("codex") if p.is_file() and (n := _codex_native(str(p)))), None)
        if native:
            return native
        return next((str(p) for p in _codex_standalone() if p.is_file()), None) or found \
            or next((str(p) for p in _node_global_bins("codex") if p.is_file()), None)

    def status(self) -> dict:
        if not self.exe:
            return {"id": self.id, "name": self.name, "ready": False, "exe": None,
                    "hint": f"Codex CLI 가 없음 — '설치'를 누르면 공식 단독 설치(Node 필요 없음): `{install_command('codex')}` 뒤 `codex login`",
                    "install": install_command("codex")}
        proc = _run_quiet([*self.base(), "login", "status"])
        ok = proc is not None and proc.returncode == 0
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()[:200] if proc else ""
        out = {"id": self.id, "name": self.name, "ready": ok, "exe": self.exe, "detail": text,
               "hint": None if ok else "로그인 필요 — 터미널에서 `codex login`", "login": [*self.base(), "login"]}
        if "node_modules" in str(self.exe):  # npm 패키지 안의 네이티브 실행 파일을 직접 쓰는 중 — Node 버전과 상관없이 돈다
            out["note"] = (f"npm 으로 설치된 Codex 의 실행 파일을 Node 없이 바로 씁니다 ({self.exe}). "
                           f"npm 패키지를 지우면 같이 사라지니, 따로 두려면 공식 단독 설치: `{install_command('codex')}`")
        return out

    def command(self, settings, run, prompt, rules):
        srv = _mcp_server(settings, run)
        pol = run.get("policy") or {}
        cfg = [f'mcp_servers.{MCP_NAME}.command={json.dumps(srv["command"])}',
               f'mcp_servers.{MCP_NAME}.args={json.dumps(srv["args"])}',
               "mcp_servers.%s.env={%s}" % (MCP_NAME, ", ".join(f'{k}={json.dumps(v)}' for k, v in srv["env"].items())),
               # "auto" 는 도구마다 승인을 물어보는데 approval_policy=never 라 전부 거절된다 — 우리 도구는 항상 허용
               f"mcp_servers.{MCP_NAME}.default_tools_approval_mode=\"approve\"",
               # 서버 기동은 mcp SDK import 만 수 초 (기본 10초를 넘길 때가 있음), 사람에게 묻는 도구는 답을 기다린다
               f"mcp_servers.{MCP_NAME}.startup_timeout_sec={MCP_STARTUP_SEC}",
               f"mcp_servers.{MCP_NAME}.tool_timeout_sec={MCP_TOOL_SEC}",
               # 기본("connection")은 도구 목록이 오기 전에 작업을 시작해 버린다 — 목록까지 기다리고, 못 붙으면 실패로 끝낸다
               f'mcp_servers.{MCP_NAME}.startup_readiness="catalog"', f"mcp_servers.{MCP_NAME}.required=true",
               'sandbox_mode="workspace-write"', 'approval_policy="never"',
               f"sandbox_workspace_write.network_access={'true' if pol.get('web') == 'allow' else 'false'}"]
        if sys.platform == "win32":
            # Windows 는 샌드박스 방식을 정하지 않으면 쓰기가 모두 막힌다 — 관리자 설치가 필요 없는 방식
            cfg.append('windows.sandbox="unelevated"')
        # exec resume 는 -s·-C 를 받지 않는다 (codex 0.167) — 샌드박스는 설정 값으로, 작업 폴더는 프로세스 cwd 로
        flags = ["--json", "--skip-git-repo-check"]
        for c in cfg:
            flags += ["-c", c]
        if run.get("model"):
            flags += ["-m", run["model"]]
        if run.get("effort"):
            flags += ["-c", f'model_reasoning_effort="{run["effort"]}"']
        if run.get("session_id"):
            return [*self.base(), "exec", "resume", *flags, run["session_id"], "-"], self.env(run)
        return [*self.base(), "exec", *flags, "-"], self.env(run)

    def stdin_text(self, run: dict, prompt: str, rules: str) -> str:
        return prompt if run.get("session_id") else f"{rules}\n\n{prompt}"

    def models(self) -> list[dict]:
        """Codex 가 로그인 계정 기준으로 받아 둔 모델 목록(~/.codex/models_cache.json) — 없으면 기본값만."""
        out = [{"id": "", "label": "기본값", "note": "Codex 설정을 따름"}]
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return out
        rows = [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
        for m in sorted(rows, key=lambda m: m.get("priority") or 999)[:12]:
            out.append({"id": m["slug"], "label": m.get("display_name") or m["slug"], "default_effort": m.get("default_reasoning_level"),
                        "levels": [{"id": x.get("effort"), "label": _EFFORT_KO.get(x.get("effort"), x.get("effort")), "desc": x.get("description", "")}
                                   for x in m.get("supported_reasoning_levels") or [] if x.get("effort")]})
        return out

    def current(self) -> dict:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        cfg = {}
        try:
            import tomllib
            cfg = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError, ImportError):
            pass
        model, source = cfg.get("model"), "Codex 설정"
        if not model:
            files = _newest_codex_sessions(home / "sessions")
            model = _tail_value(files, lambda e: (e.get("payload") or {}).get("model") if e.get("type") == "turn_context" else None)
            source = "최근 사용"
        if not model:
            ms = self.models()
            model, source = (ms[1]["id"] if len(ms) > 1 else None), "Codex 기본"
        effort = cfg.get("model_reasoning_effort")
        if not effort:
            m = next((x for x in self.models() if x["id"] == model), {})
            effort = m.get("default_effort")
        return {"model": model, "effort": effort, "source": source} if model else {}

    def efforts(self) -> dict[str, list[dict]]:
        ms = self.models()
        out = {m["id"]: m.get("levels") or [] for m in ms if m["id"]}
        if len(ms) > 1:  # 기본 모델은 Codex 설정의 모델 — 목록 첫 모델의 단계로 안내
            out[""] = ms[1].get("levels") or []
        return out

    def env(self, run):
        env = quiet_env()
        if run.get("auth") != "api_key":
            env.pop("CODEX_API_KEY", None)
            env.pop("OPENAI_API_KEY", None)
        return env

    def parse(self, ev):
        t = ev.get("type")
        if t == "thread.started":
            return [{"kind": "init", "session_id": ev.get("thread_id")}]
        if t in ("item.completed", "item.started"):
            item = ev.get("item") or {}
            it = item.get("type") or item.get("item_type")
            if t == "item.completed" and it in ("agent_message", "assistant_message") and item.get("text"):
                return [{"kind": "text", "text": item["text"][:4000]}]
            if t == "item.started" and it == "command_execution":
                return [{"kind": "tool", "tool": "Bash", "input": {"command": item.get("command")}}]
            if t == "item.completed" and it in ("mcp_tool_call", "file_change"):
                name = item.get("tool") or item.get("name") or it
                out = [{"kind": "tool", "tool": f"mcp__{MCP_NAME}__{name}" if it == "mcp_tool_call" else "Edit",
                        "input": item.get("arguments") or {"changes": item.get("changes")}}]
                err = item.get("error")
                if item.get("status") == "failed" and err:
                    out.append({"kind": "text", "text": f"[도구 실패] {name}: {err.get('message') if isinstance(err, dict) else err}"[:2000]})
                return out
        if t == "turn.completed":
            return [{"kind": "result", "ok": True, "usage": ev.get("usage")}]
        if t in ("turn.failed", "error"):
            msg = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
            return [{"kind": "result", "ok": False, "text": str(msg)[:2000]}]
        return []


# ---------------------------------------------------------------- Gemini — Antigravity CLI (agy) · 예전 Gemini CLI (실험적)

AGY_INSTALL = {"win32": "irm https://antigravity.google/cli/install.ps1 | iex",
               "other": "curl -fsSL https://antigravity.google/cli/install.sh | bash"}
AGY_PROMPT_MAX = 24_000  # Windows 명령줄 한도(32K자) 안 — 지시는 인자로 넘긴다 (agy 는 .cmd 가 아닌 실행 파일)


def _agy_paths() -> list[Path]:
    home = Path.home()
    out = [home / ".local" / "bin" / ("agy.exe" if os.name == "nt" else "agy")]
    if os.environ.get("LOCALAPPDATA"):
        out.insert(0, Path(os.environ["LOCALAPPDATA"]) / "agy" / "bin" / "agy.exe")
    return out


class GeminiCLI(Agent):
    """Google 의 Gemini 코딩 에이전트. 2026-06 부터 개인 요금제의 Gemini CLI 는 Antigravity CLI(agy)로 바뀌었다 —
    agy 가 있으면 agy 를, 없고 예전 gemini 만 있으면(기업 요금제) 그것을 쓴다."""
    id, name = "gemini", "Gemini (Antigravity CLI)"
    api_key_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    note = ("Antigravity CLI(agy)로 연결합니다. 헤드리스 실행에서는 확인이 필요한 셸 명령이 거절돼 대상 폴더 안 편집과 "
            "툴킷 도구(검증 등)로 진행합니다. MCP 는 대상 폴더의 .agents/mcp_config.json 으로 붙입니다. (실험적)")

    def find(self) -> str | None:
        found = _which("agy") or next((str(p) for p in _agy_paths() if p.is_file()), None)
        return found or _which("gemini") or next((str(p) for p in _node_global_bins("gemini") if p.is_file()), None)

    @property
    def legacy(self) -> bool:
        return bool(self.exe) and "gemini" in Path(str(self.exe)).name.lower()

    def status(self) -> dict:
        refresh_path()
        if not self.exe:
            cmd = AGY_INSTALL["win32" if sys.platform == "win32" else "other"]
            return {"id": self.id, "name": self.name, "ready": False, "exe": None,
                    "hint": f"Antigravity CLI(agy)가 없음 — 터미널에서 `{cmd}` 로 설치한 뒤 `agy` 를 한 번 실행해 Google 계정으로 로그인",
                    "install": cmd}
        if self.legacy:
            return {"id": self.id, "name": "Gemini CLI", "ready": True, "exe": self.exe,
                    "detail": "예전 Gemini CLI — 로그인 상태는 실행할 때 확인됩니다 (개인 요금제는 2026-06 종료, agy 설치 권장)"}
        proc = _run_quiet([*self.base(), "models"], timeout=60)  # 로그인해야 목록이 나온다 — 로그인 확인 대신
        text = (proc.stdout + proc.stderr).decode("utf-8", "replace") if proc else ""
        ok = bool(proc) and proc.returncode == 0 and not re.search(r"sign ?in|log ?in|not authenticated|unauthori", text, re.I)
        return {"id": self.id, "name": self.name, "ready": ok, "exe": self.exe, "detail": text.strip()[:200],
                "hint": None if ok else "로그인 필요 — 터미널에서 `agy` 를 한 번 실행해 Google 계정으로 로그인 (API 키는 GEMINI_API_KEY)",
                "login": [*self.base()]}

    # -- 한 라운드 명령
    def _mcp_file(self, settings, run: dict) -> None:
        """agy 는 작업 폴더의 .agents/mcp_config.json 을 읽는다 — 사용자의 다른 서버 설정은 그대로 두고 우리 항목만 넣는다."""
        f = Path(run["target_root"]) / ".agents" / "mcp_config.json"
        try:
            cur = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        except (OSError, ValueError):
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        cur.setdefault("mcpServers", {})[MCP_NAME] = _mcp_server(settings, run)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

    def command(self, settings, run, prompt, rules):
        if self.legacy:
            return self._legacy_command(settings, run, prompt, rules)
        self._mcp_file(settings, run)
        text = prompt if run.get("session_id") else f"{rules}\n\n{prompt}"
        if len(text) > AGY_PROMPT_MAX:
            text = text[:AGY_PROMPT_MAX] + "\n…(지시가 길어 잘림 — 원장·첨부 파일을 직접 읽는다)"
        argv = [*self.base(), "-p", text, "--output-format", "stream-json", "--mode", "accept-edits",
                "--print-timeout", "60m", "--disable-slash-commands"]
        if run.get("model"):
            argv += ["--model", run["model"]]
        if run.get("effort") in ("low", "medium", "high"):
            argv += ["--effort", run["effort"]]
        if run.get("session_id"):
            argv += ["--conversation", run["session_id"]]
        if run.get("sandbox", True) is not False:
            argv.append("--sandbox")  # agy 자체 격리 (터미널 제한)
        env = quiet_env()  # 자동 업데이트 끔 — agy 업데이트 확인이 보이는 콘솔을 띄운다
        if run.get("auth") != "api_key":
            for k in self.api_key_env:
                env.pop(k, None)
        return argv, env

    @property
    def stdin_prompt(self) -> bool:  # type: ignore[override]
        return self.legacy

    def stdin_text(self, run: dict, prompt: str, rules: str) -> str:
        return f"{rules}\n\n{prompt}"

    def models(self) -> list[dict]:
        if self.legacy:
            return [{"id": "", "label": "기본값"}, {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
                    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"}]
        out = [{"id": "", "label": "기본값", "note": "agy 설정을 따름"}]
        cache = Path.home() / ".gemini" / "antigravity-cli" / "models_cache.json"
        try:  # agy models 결과를 앱이 저장해 둔 것 — 없으면 기본값만 (목록은 계정마다 다르다)
            for m in json.loads(cache.read_text(encoding="utf-8")):
                out.append({"id": m, "label": m})
        except (OSError, ValueError, TypeError):
            pass
        return out

    def efforts(self) -> dict[str, list[dict]]:
        if self.legacy:
            return {}
        levels = [{"id": "low", "label": "낮음", "desc": "빠르게"}, {"id": "medium", "label": "보통", "desc": "일상 작업"},
                  {"id": "high", "label": "높음", "desc": "복잡한 작업"}]
        return {m["id"]: levels for m in self.models()}

    def parse(self, ev):
        if self.legacy:
            return self._legacy_parse(ev)
        # stream-json 이벤트 모양이 문서화돼 있지 않아 이름이 조금 달라도 받아들인다 (실험적)
        t = str(ev.get("type") or ev.get("event") or ev.get("kind") or "").lower()
        cid = ev.get("conversation_id") or ev.get("conversationId") or ev.get("session_id")
        out: list[dict] = []
        if cid and not getattr(self, "_cid", None):
            self._cid = cid
            out.append({"kind": "init", "session_id": cid})
        status = str(ev.get("status") or "").upper()
        if status in ("SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID") and ("response" in ev or "usage" in ev or t in ("result", "final", "done", "")):
            self._cid = None
            err = ev.get("error")
            text = ev.get("response") if isinstance(ev.get("response"), str) else (err.get("message") if isinstance(err, dict) else err)
            if status == "SUCCESS" and text:
                out.append({"kind": "text", "text": str(text)[:4000]})
            out.append({"kind": "result", "ok": status == "SUCCESS", "session_id": cid, "usage": ev.get("usage"),
                        "subtype": status.lower(), "text": "" if status == "SUCCESS" else str(text or status)[:2000]})
            return out
        if "delta" in t or "chunk" in t or "partial" in t:
            return out  # 조각 — 완성된 메시지만 보여 준다
        if t in ("tool_use", "tool_call", "tool", "tool_start", "function_call") or ev.get("tool_name"):
            tool = ev.get("tool")
            name = ev.get("tool_name") or ev.get("name") or (tool.get("name") if isinstance(tool, dict) else tool)
            if name and str(name).startswith(MCP_NAME):
                name = f"mcp__{MCP_NAME}__" + str(name).split("__")[-1].split("/")[-1].split(":")[-1]
            out.append({"kind": "tool", "tool": name or "도구", "input": ev.get("args") or ev.get("arguments") or ev.get("input") or ev.get("parameters")})
            return out
        if t in ("message", "assistant", "text", "agent_message", "response") and ev.get("role", "assistant") == "assistant":
            text = ev.get("text") or ev.get("content") or ev.get("message")
            if isinstance(text, list):
                text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
            if text and str(text).strip():
                out.append({"kind": "text", "text": str(text).strip()[:4000]})
            return out
        if t == "error":
            err = ev.get("error")
            msg = ev.get("message") or (err.get("message") if isinstance(err, dict) else err)
            out.append({"kind": "result", "ok": False, "text": str(msg)[:2000]})
        return out

    # -- 예전 Gemini CLI (기업 요금제에 남아 있는 경우)
    def _legacy_command(self, settings, run, prompt, rules):
        from beom_workbench.core import inbox

        d = inbox.run_dir(settings, run["id"]) / "agent"
        d.mkdir(parents=True, exist_ok=True)
        conf = d / "gemini-settings.json"
        conf.write_text(json.dumps({"mcpServers": {MCP_NAME: {**_mcp_server(settings, run), "trust": True}}},
                                   ensure_ascii=False), encoding="utf-8")
        argv = [*self.base(), "-p", "표준 입력의 지시를 따른다.", "-o", "stream-json", "--approval-mode", "auto_edit",
                "--allowed-mcp-server-names", MCP_NAME]
        if run.get("model"):
            argv += ["-m", run["model"]]
        if run.get("session_id"):
            argv += ["-r", run["session_id"]]
        env = dict(os.environ, GEMINI_CLI_SYSTEM_SETTINGS_PATH=str(conf))
        return argv, env

    def _legacy_parse(self, ev):
        t = ev.get("type")
        if t == "init":
            return [{"kind": "init", "session_id": ev.get("session_id")}]
        if t == "message" and ev.get("role", "assistant") == "assistant" and ev.get("content"):
            return [{"kind": "text", "text": str(ev["content"])[:4000]}]
        if t == "tool_use":
            return [{"kind": "tool", "tool": ev.get("tool_name") or ev.get("name"), "input": ev.get("parameters") or ev.get("args")}]
        if t == "result":
            return [{"kind": "result", "ok": ev.get("status", "success") == "success", "usage": ev.get("stats")}]
        if t == "error":
            return [{"kind": "result", "ok": False, "text": str(ev.get("message"))[:2000]}]
        return []


AGENTS = {a.id: a for a in (ClaudeCode, Codex, GeminiCLI)}


def get(agent_id: str | None, exe=None) -> Agent:
    cls = AGENTS.get(agent_id or "claude")
    if cls is None:
        raise ValueError(f"모르는 에이전트: {agent_id}")
    return cls(exe)


def statuses(check: bool = True) -> list[dict]:
    """에이전트 목록. check=False 면 설치 여부만 빠르게 (로그인 확인은 몇 초 걸린다) — ready 는 None(확인 중)."""
    refresh_path(0 if check else 30)
    out = []
    for cls in AGENTS.values():
        if not check:
            a = cls()
            try:
                cur = a.current()
            except Exception:  # noqa: BLE001 — 설정 파일이 이상해도 앱은 연다
                cur = {}
            out.append({"id": cls.id, "name": cls.name, "ready": None, "exe": a.exe, "permission_hook": cls.permission_hook,
                        "note": cls.note, "models": a.models(), "efforts": a.efforts(), "current": cur})
            continue
        try:
            a = cls()
            out.append({**a.status(), "permission_hook": cls.permission_hook, "note": cls.note, "models": a.models(),
                        "efforts": a.efforts(), "current": a.current()})
        except Exception as e:  # noqa: BLE001 — 한 에이전트 확인 실패가 목록을 막지 않게
            out.append({"id": cls.id, "name": cls.name, "ready": False, "hint": f"{type(e).__name__}: {e}"})
    return out
