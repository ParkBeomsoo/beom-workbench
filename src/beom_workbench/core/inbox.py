"""실행(run) 상태·질문함·승인 규칙 — 자동 실행기와 데스크톱 앱이 함께 쓴다.

실행 폴더: <home>/runs/<run_id>/
  run.json       모드·프로젝트·원장·상태·라운드·규칙
  events.jsonl   실행 로그 (에이전트 이벤트·검증 결과·단계 변화)
  inbox/<id>.json        에이전트가 기다리는 질문·권한 요청 (앱이 카드로 보여 줌)
  inbox/<id>.answer.json 사람의 답 (앱이 씀)

권한 요청은 종류별 규칙으로 먼저 판단한다 — allow / ask / refuse. ask 만 사람에게 간다.
에이전트는 규칙을 더 엄격하게만 바꿀 수 있다. 실행 폴더는 대상 저장소 밖이라 에이전트의 파일 쓰기 규칙으로는 닿지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings
from beom_workbench.core import workbranch

RUN_SCHEMA = 1  # run.json 형식 — 바뀌면 load_run 에서 올린다
DECISIONS = ("allow", "ask", "refuse")
KINDS = {
    "edit": "대상 폴더 안 파일 수정",
    "build": "정해진 빌드·테스트 명령",
    "commands": "그 밖의 명령 실행",
    "web": "웹 접속",
    "outside": "대상 폴더 밖 파일 읽기·쓰기",
    "publish": "push·배포·패키지 게시",
    "branch": "작업 브랜치 밖으로 (브랜치 바꾸기·병합·pull·reset --hard)",
    "delete": "대상 폴더 밖 삭제",
    "system": "시스템·설정 변경 (툴킷 원장·질문함 포함)",
    "other": "그 밖의 도구",
}
DEFAULT_POLICY = {"edit": "allow", "build": "allow", "commands": "ask", "web": "ask", "outside": "ask",
                  "publish": "refuse", "branch": "refuse", "delete": "refuse", "system": "refuse", "other": "ask"}
STRICTNESS = {"allow": 0, "ask": 1, "refuse": 2}
# 권한 모드 — 대부분은 셋 중 하나로 충분하다. 세부(종류별) 규칙은 "직접 설정"으로.
_NEVER = {"publish": "refuse", "branch": "refuse", "delete": "refuse", "system": "refuse"}
PERMISSION_MODES = {
    "ask": {"label": "모두 물어보기", "desc": "파일 수정·명령 실행마다 확인합니다",
            "policy": {"edit": "ask", "build": "ask", "commands": "ask", "web": "ask", "outside": "ask", "other": "ask", **_NEVER}},
    "edits": {"label": "편집 자동 허용", "desc": "대상 폴더 안 수정과 빌드·테스트는 바로, 그 밖의 명령·웹은 확인합니다",
              "policy": dict(DEFAULT_POLICY)},
    "auto": {"label": "자동", "desc": "명령·웹 조사까지 묻지 않고 진행합니다 (폴더 밖 파일·배포·시스템 변경만 확인). 믿을 수 있는 작업에만",
             "policy": {"edit": "allow", "build": "allow", "commands": "allow", "web": "allow", "outside": "ask", "other": "ask",
                        **_NEVER}},
}


def mode_of(policy: dict) -> str:
    """규칙이 어느 모드와 같은지 — 어느 것과도 다르면 custom."""
    for k, m in PERMISSION_MODES.items():
        if all(policy.get(kind) == v for kind, v in m["policy"].items()):
            return k
    return "custom"


# 정확히 이 줄만 '빌드'로 바로 허용 — 덧붙인 인자는 실행기를 바꿀 수 있다 (go test -exec, yarn dlx, pytest -p 등).
# 그 밖의 줄은 '그 밖의 명령'으로 규칙에 따라 묻는다.
BUILD_COMMANDS = re.compile(
    r"^(npm (ci|install|test|run (build|test|lint))|pnpm (install|test|build)|yarn (install|test|build)|"
    r"cargo (build|test|check|fmt|clippy)|go (build|test|vet)( \./\.\.\.)?|mvn( -q)? (test|package|verify)|gradlew? (test|build)|"
    r"\./gradlew (test|build)|pytest|python -m pytest|py -m pytest|uv run pytest|pip install -e \.|dotnet (build|test))\s*$")
PUBLISH = re.compile(r"\bgit(?:\.exe)?\b[^;&|\n]*\bpush\b|\b(?:npm|pnpm|yarn|bun)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b"
                     r"|\bdocker\s+push\b|\bgh\s+(?:release|pr\s+(?:create|merge)|repo\s+create)\b|\bkubectl\s+apply\b|\bhelm\s+(?:install|upgrade)\b", re.I)
# 시스템·설정 — 레지스트리·예약 작업·서비스, git 설정 바꾸기(훅 경로·fsmonitor 로 앱의 커밋 때 명령을 심을 수 있다)
SYSTEM = re.compile(r"\b(?:setx|reg(?:\.exe)?\s+(?:add|delete|import)|sudo|chmod\s+-R\s+/|systemctl|launchctl|netsh|Set-ExecutionPolicy"
                    r"|schtasks|sc(?:\.exe)?\s+(?:create|config|delete|start|stop)|bcdedit|(?:New|Set|Remove)-ItemProperty|BEOM_WORKBENCH_)\b"
                    r"|HKCU:|HKLM:|Registry::"
                    r"|\bgit(?:\.exe)?\s+(?:-C\s+\S+\s+)?config\s+(?!(?:--get|--get-all|--get-regexp|--list|-l)\b)\S+\s+\S", re.I)
# 대상 폴더 안이라도 에이전트가 쓰면 안 되는 곳 — 에이전트 자신의 권한·훅 설정, git 내부(HEAD·hooks·config)
CONFIG_PATHS = re.compile(r"(^|/)(\.claude|\.git|\.codex|\.gemini|\.cursor)(/|$)|(^|/)\.mcp\.json$", re.I)
CHAINING = re.compile(r"[\n\r&|;`<>]|\$\(|\$\{")
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runs_dir(settings: Settings) -> Path:
    d = settings.home / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(settings: Settings, run_id: str) -> Path:
    if not re.fullmatch(r"run_[0-9a-f]{12}", run_id):
        raise ToolError("invalid_request", f"실행 id 형식이 아님: {run_id}")
    return runs_dir(settings) / run_id


def new_run(settings: Settings, meta: dict) -> dict:
    run_id = "run_" + secrets.token_hex(6)
    d = run_dir(settings, run_id)
    (d / "inbox").mkdir(parents=True)
    run = {"id": run_id, "schema": RUN_SCHEMA, "created_at": _now(), "status": "created", "round": 0,
           "policy": {**DEFAULT_POLICY, **(meta.pop("policy", None) or {})}, **meta}
    save_run(settings, run)
    return run


def _retry(fn, tries: int = 40, wait: float = 0.025):
    """Windows 는 다른 프로세스가 읽는 중인 파일을 바꾸거나(replace) 바뀌는 중인 파일을 읽으면 실패한다 — 잠깐 뒤 다시."""
    for i in range(tries):
        try:
            return fn()
        except (PermissionError, json.JSONDecodeError):
            if i == tries - 1:
                raise
            time.sleep(wait)


def load_run(settings: Settings, run_id: str) -> dict:
    f = run_dir(settings, run_id) / "run.json"
    if not f.exists():
        raise ToolError("not_found", f"실행 없음: {run_id}")
    return _retry(lambda: json.loads(f.read_text(encoding="utf-8")))


def save_run(settings: Settings, run: dict) -> None:
    f = run_dir(settings, run["id"]) / "run.json"
    tmp = f.with_name(f"run.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        _retry(lambda: os.replace(tmp, f))
    finally:
        tmp.unlink(missing_ok=True)


class _RunLock:
    """run.json 을 읽고-고치고-쓰는 동안 다른 프로세스(앱·실행기·MCP 서버)가 끼어들지 않게 하는 짧은 잠금."""

    def __init__(self, settings: Settings, run_id: str, timeout: float = 10.0, stale: float = 15.0):
        self.path = run_dir(settings, run_id) / "run.json.lock"
        self.timeout, self.stale = timeout, stale

    def __enter__(self):
        end = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale:
                        self.path.unlink(missing_ok=True)  # 잡은 쪽이 죽었다
                        continue
                except OSError:
                    continue
                if time.monotonic() > end:
                    raise ToolError("timeout", "실행 상태 파일 잠금을 얻지 못함") from None
                time.sleep(0.02)
            except PermissionError:
                if time.monotonic() > end:
                    raise ToolError("timeout", "실행 상태 파일 잠금을 얻지 못함") from None
                time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def update_run(settings: Settings, run_id: str, fn) -> dict:
    """run.json 을 잠근 채로 읽어 fn(run) 으로 고치고 저장한다 — 여러 곳에서 동시에 고쳐도 서로의 변경을 잃지 않는다."""
    with _RunLock(settings, run_id):
        run = load_run(settings, run_id)
        fn(run)
        save_run(settings, run)
        return run


_LIST: dict[str, tuple[int, dict]] = {}


def list_runs(settings: Settings) -> list[dict]:
    """실행 목록 (최근 것 먼저). 화면이 몇 초마다 부르므로 run.json 이 그대로면 지난번 읽은 것을 쓴다.
    도중에 휴지통으로 옮겨진 폴더·쓰는 중인 파일은 건너뛴다 (목록 전체가 실패하지 않게)."""
    rows = []
    for d in runs_dir(settings).iterdir():
        f = d / "run.json"
        try:
            st = f.stat()
        except OSError:
            continue
        key = str(f)
        hit = _LIST.get(key)
        if hit and hit[0] == st.st_mtime_ns:
            run = hit[1]
        else:
            try:
                run = _retry(lambda f=f: json.loads(f.read_text(encoding="utf-8")), tries=4)
            except (OSError, json.JSONDecodeError):
                continue
            _LIST[key] = (st.st_mtime_ns, run)
        rows.append((max(st.st_mtime, _mtime(d)), run))
    rows.sort(key=lambda x: x[0], reverse=True)
    return [json.loads(json.dumps(r)) for _, r in rows]  # 부르는 쪽이 고쳐도 캐시는 그대로


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def log(settings: Settings, run_id: str, kind: str, **data) -> None:
    # 실행기와 MCP 서버(다른 프로세스)가 같은 파일에 덧붙인다 — 한 줄을 한 번의 쓰기로 (버퍼를 거치면 긴 줄이 여러 번에
    # 나뉘어 쓰이고 그 사이에 다른 프로세스의 줄이 끼어 둘 다 깨진다)
    line = (json.dumps({"at": _now(), "kind": kind, **data}, ensure_ascii=False) + "\n").encode("utf-8")
    _append(run_dir(settings, run_id) / "events.jsonl", line)


def _append(path: Path, data: bytes) -> None:
    """여러 프로세스가 같은 파일 끝에 덧붙여도 줄이 겹쳐 쓰이지 않게. Windows 의 O_APPEND 는 '끝으로 옮기고 쓰기' 두 단계라
    프로세스끼리 같은 자리에 덮어쓸 수 있다 — '덧붙이기 권한만'으로 열면 운영체제가 한 번에 끝에 붙인다."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.HANDLE]
        k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        # FILE_APPEND_DATA | SYNCHRONIZE, 공유 읽기·쓰기·지우기, OPEN_ALWAYS
        h = k32.CreateFileW(str(path), 0x0004 | 0x00100000, 0x7, None, 4, 0x80, None)
        if h and h != ctypes.c_void_p(-1).value:
            try:
                n = wintypes.DWORD()
                if k32.WriteFile(h, data, len(data), ctypes.byref(n), None) and n.value == len(data):
                    return
            finally:
                k32.CloseHandle(h)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def events(settings: Settings, run_id: str, offset: int = 0) -> tuple[list[dict], int]:
    f = run_dir(settings, run_id) / "events.jsonl"
    if not f.exists():
        return [], offset
    with open(f, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    cut = data.rfind(b"\n") + 1  # 쓰는 중인 마지막 줄은 다음에 — 위치는 완성된 줄 끝까지만 옮긴다
    out = []
    for line in data[:cut].splitlines():
        try:
            out.append(json.loads(line.decode("utf-8", "replace")))
        except json.JSONDecodeError:
            continue
    return out, offset + cut


def tighten(policy: dict, changes: dict) -> dict:
    """에이전트가 바꾸는 규칙 — 더 엄격한 쪽만 받아들인다."""
    out = dict(policy)
    for k, v in changes.items():
        if k in KINDS and v in DECISIONS and STRICTNESS[v] > STRICTNESS[out.get(k, "ask")]:
            out[k] = v
    return out


def _scratch(path: str | None) -> bool:
    """에이전트 자신의 임시 작업 폴더 (Claude Code 는 %TEMP%/claude/… 에 메모·스크립트를 둔다) — 대상 폴더처럼 다룬다."""
    return bool(path) and _inside(path, Path(tempfile.gettempdir()) / "claude")


def _inside(path: str | None, root: Path) -> bool:
    """path 가 root 안(또는 root 자체)인지. 문자열 앞부분 비교가 아니라 경로 단위 — app 과 app2 를 구분한다."""
    if not path:
        return False
    try:
        p = Path(path)
        p = (root / p) if not p.is_absolute() else p
        a, b = os.path.normcase(str(p.resolve())), os.path.normcase(str(Path(root).resolve()))
        return a == b or a.startswith(b.rstrip("\\/") + os.sep)
    except (OSError, ValueError):
        return False


def decide(run: dict, kind: str) -> str:
    """종류별 규칙으로 허용·물어보기·거절. 'branch' 는 작업 브랜치가 있을 때만 — 새 프로젝트(저장소 아님)나
    사용자가 '새 브랜치에서 작업'을 끈 실행에서는 그 밖의 명령과 같이 본다."""
    if kind == "branch" and not run.get("work_branch"):
        kind = "commands"
    return run["policy"].get(kind, "ask")


def classify(tool: str, tool_input: dict, target_root: Path, protected: list[Path]) -> str:
    """권한 요청을 종류로 나눈다. protected: 툴킷 홈·실행 폴더 — 여기 닿으면 system."""
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    if path and any(_inside(path, p) for p in protected):
        return "system"
    if tool in FILE_TOOLS:
        if path and CONFIG_PATHS.search(str(path).replace("\\", "/")) and not _scratch(path):
            return "system"
        return "edit" if _inside(path, target_root) or _scratch(path) else "outside"
    if tool in READ_TOOLS:
        return "edit" if (not path or _inside(path, target_root) or _scratch(path)) else "outside"
    if tool in ("WebFetch", "WebSearch"):
        return "web"
    if tool in ("Bash", "PowerShell"):
        cmd = str(tool_input.get("command", "")).strip()
        if _touches(cmd, protected) or SYSTEM.search(cmd):
            return "system"
        if PUBLISH.search(cmd):
            return "publish"
        if workbranch.moves_branch(cmd):
            return "branch"
        if DELETE_CMD.search(cmd) and not _deletes_inside(cmd, target_root):
            return "delete"
        # 이어 붙이기·치환·리다이렉트가 있으면 빌드 명령 뒤에 다른 명령이 숨을 수 있다 — 물어보는 쪽으로
        if BUILD_COMMANDS.match(cmd) and not CHAINING.search(cmd):
            return "build"
        # 버전·설치 여부만 보는 명령(node --version; Test-Path …)은 아무것도 바꾸지 않는다 — 묻지 않고 빌드와 같이 둔다
        return "build" if _inspect_only(cmd) else "commands"
    if tool.startswith("mcp__beom-workbench__"):
        return "other" if tool.endswith(("port_approve",)) else "edit"
    return "other"


INSPECT_COMMANDS = re.compile(
    r"^(?:(?:node|npm|npx|pnpm|yarn|bun|deno|python3?|py|pip3?|uv|git|java|javac|dotnet|cargo|rustc|go|gcc|cmake|docker|code)"
    r"(?:\.exe)? (?:--version|-v|-V|version)"
    r"|(?:where(?:\.exe)?|which|command -v|type) [\w.\-]+"
    r"|\(?Get-Command [\w.\-]+(?: -ErrorAction (?:SilentlyContinue|Ignore))?\)?(?:\.(?:Source|Path|Version))?"
    r"|Test-Path (?:-LiteralPath |-Path )?(?:\"[^\"$`;|&<>]*\"|'[^'$`;|&<>]*'|[^\s\"'$`;|&<>]+)"
    r"|\$PSVersionTable(?:\.PSVersion)?|uname(?: -[a-z]+)?|echo \$env:[A-Z_]+)$", re.I)


def _inspect_only(cmd: str) -> bool:
    """세미콜론·줄바꿈으로 이은 조각이 모두 버전·설치 확인뿐인가. 파이프·리다이렉트·치환이 있으면 아니다."""
    if re.search(r"[|&<>`]|\$\(|\$\{", cmd):
        return False
    parts = [p.strip() for p in re.split(r"[;\n\r]+", cmd) if p.strip()]
    return bool(parts) and all(INSPECT_COMMANDS.match(p) for p in parts)


def _touches(cmd: str, protected: list[Path]) -> bool:
    """명령이 툴킷 홈(원장·질문함)을 건드리는지 — 슬래시 방향·~·환경 변수 표기까지."""
    low = cmd.lower().replace("\\", "/")
    # 이어 붙인 문자열('.beom-'+'workbench')·8.3 짧은 이름(BEOM-W~1)으로 돌려 적은 것도
    if ".beom-workbench" in low or "beom_workbench_home" in low or "beom-w~" in low or re.search(r"beom.{0,8}workbench", low):
        return True
    if CONFIG_PATHS.search(re.sub(r"[\"']", " ", low).replace(" ", "/")) and re.search(r"[>]|set-content|out-file|add-content|copy|move|mv |cp |tee", low):
        return True  # .claude/·.git/ 안에 명령으로 쓰기
    for p in protected:
        s = str(p).lower().replace("\\", "/")
        if s in low:
            return True
    return False


DELETE_CMD = re.compile(r"\brm\s+(?:-{1,2}[\w-]+\s+)*(?:-[a-zA-Z]*[rR][a-zA-Z]*\b|--recursive\b)|\brmdir\b|\bRemove-Item\b"
                        r"|\b(?:ri|erase|del|rd|rm)\b[^;&|\n]*(?:-Recurse\b|\s/s\b)", re.I)


def _deletes_inside(cmd: str, root: Path) -> bool:
    """명령을 ; && || | 줄바꿈으로 나눠 지우는 조각만 본다 — 그 조각이 지우는 경로가 모두 대상 폴더 안이면 안쪽 정리다.
    (앞뒤에 다른 명령이 이어져 있다고 대상 폴더 안 임시 폴더 정리까지 '밖 삭제'로 막지 않게. 변수·와일드카드는 알 수 없어 밖으로 본다.)"""
    segs = [x.strip() for x in re.split(r"[;\n\r|]+|&&", cmd) if x.strip()]
    dels = [x[DELETE_CMD.search(x).start():] for x in segs if DELETE_CMD.search(x)]
    return bool(dels) and all(_cmd_inside(x, root) for x in dels)


def _cmd_inside(cmd: str, root: Path) -> bool:
    try:
        parts = shlex.split(cmd, posix=os.name != "nt")
    except ValueError:
        return False
    paths = [p for p in parts[1:] if not p.startswith("-") and p.lower() not in ("/s", "/q")]
    if any(re.search(r"[$`]|%\w+%", p) for p in paths):
        return False  # 변수·치환 — 실제로 무엇을 지울지 모른다

    def ok(p: str) -> bool:
        if re.search(r"[*?]", p):  # 와일드카드는 그 앞 폴더가 대상 폴더 '안쪽'(대상 폴더 자체는 아님)일 때만
            head = re.split(r"[*?]", p, 1)[0]
            base = head.rstrip("\\/") if head.endswith(("\\", "/")) else str(Path(head).parent) if head else ""
            if base in ("", ".") or not _inside(base, root):
                return False
            full = Path(base) if Path(base).is_absolute() else root / base
            return os.path.normcase(str(full.resolve())) != os.path.normcase(str(root.resolve()))
        return _inside(p, root)
    return bool(paths) and all(ok(p) for p in paths)


# ---------------------------------------------------------------- 질문함

def ask(settings: Settings, run_id: str, kind: str, payload: dict, timeout: float = 24 * 3600, poll: float = 0.5) -> dict:
    """사람에게 묻고 답을 기다린다. kind: permission | question. 답이 없으면 timeout 뒤 거절로 본다."""
    rid = secrets.token_hex(6)
    d = run_dir(settings, run_id) / "inbox"
    req = {"id": rid, "kind": kind, "at": _now(), **payload}
    (d / f"{rid}.json").write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    log(settings, run_id, "ask", id=rid, ask_kind=kind, summary=payload.get("summary") or payload.get("question"))
    ans_f = d / f"{rid}.answer.json"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if ans_f.exists():
            ans = take_answer(ans_f)
            if ans is None:
                if not (d / f"{rid}.json").exists():  # 실행기가 먼저 받아 갔다 (이 MCP 서버가 늦게 깨어남)
                    return {"timeout": True, "taken": True}
                time.sleep(poll)
                continue
            (d / f"{rid}.json").unlink(missing_ok=True)
            log(settings, run_id, "answer", id=rid, answer=ans)
            return ans
        time.sleep(poll)
    (d / f"{rid}.json").unlink(missing_ok=True)
    take_answer(ans_f)  # 시간이 지난 뒤 온 답 — 남겨 두지 않는다
    log(settings, run_id, "answer", id=rid, answer={"timeout": True})
    return {"timeout": True}


def take_answer(f: Path) -> dict | None:
    """답 파일을 한쪽만 가져가게 — 이름을 바꿔(원자적) 차지한 쪽이 읽고 지운다. 다른 쪽이 먼저 가져갔거나 쓰는 중이면 None."""
    mine = f.with_name(f"{f.name}.{os.getpid()}.{secrets.token_hex(3)}.taken")
    try:
        os.replace(f, mine)
    except OSError:
        return None
    try:
        return json.loads(mine.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            os.replace(mine, f)  # 아직 쓰는 중이던 답 — 되돌려 둔다
        except OSError:
            pass
        return None
    finally:
        mine.unlink(missing_ok=True)


def pending(settings: Settings, run_id: str) -> list[dict]:
    d = run_dir(settings, run_id) / "inbox"
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.endswith(".answer.json"):
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def answer(settings: Settings, run_id: str, request_id: str, data: dict) -> None:
    if not re.fullmatch(r"[0-9a-f]{12}", request_id):
        raise ToolError("invalid_request", "요청 id 형식이 아님")
    d = run_dir(settings, run_id) / "inbox"
    if not (d / f"{request_id}.json").exists():
        raise ToolError("not_found", "이미 답했거나 없는 요청")
    tmp = d / f"{request_id}.answer.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / f"{request_id}.answer.json")


# ---------------------------------------------------------------- MCP 서버가 대신 실행하는 명령

def _norm_cmd(c: str) -> str:
    return " ".join(str(c).strip().lstrip("$").split())


def planned_command(run: dict, cmd: str) -> bool:
    """사람이 승인한 계획에 그대로 적혀 있던 명령인가 — 승인할 때 본 명령은 실행할 때 다시 묻지 않는다.
    (규칙상 '거절'인 종류는 계획에 있어도 거절한다 — 호출하는 쪽에서 먼저 거른다.)"""
    plan = run.get("plan") or {}
    if not plan.get("approved") or not cmd:
        return False
    want = _norm_cmd(cmd)
    return any(_norm_cmd(c) == want for st in plan.get("steps") or [] for c in st.get("commands") or [])


def gate_commands(settings: Settings, commands: dict[str, str | None]) -> None:
    """자동 실행 중에 에이전트가 MCP 도구(캡처·검증·커버리지)로 넘긴 명령을 규칙대로 거른다.

    이 명령들은 MCP 서버 프로세스에서 돌아 에이전트 쪽 샌드박스·권한 확인을 거치지 않는다. 그래서 여기서 같은 규칙
    (build 허용 · 그 밖은 물어보기 · 시스템/게시는 거절)을 적용하고, 사람이 허용한 명령 문자열은 이 실행 동안 기억한다.
    자동 실행 밖(사람이 직접 CLI·대화로 돌릴 때)은 대상이 아니다.
    """
    run_id = os.environ.get("BEOM_WORKBENCH_RUN")
    if not run_id:
        return
    run = load_run(settings, run_id)
    todo = []
    for field, cmd in commands.items():
        if not cmd or cmd in (run.get("approved_cmds") or []):
            continue
        kind = classify("Bash", {"command": cmd}, Path(run["target_root"]), [settings.home])
        decision = decide(run, kind)
        if decision == "ask" and planned_command(run, cmd):
            decision = "allow"  # 승인한 계획에 있던 명령
            log(settings, run_id, "permission", tool=f"mcp:{field}", permission_kind=kind, decision="allow", why="approved_plan")
            continue
        log(settings, run_id, "permission", tool=f"mcp:{field}", permission_kind=kind, decision=decision)
        if decision != "allow" and decision != "ask":
            raise ToolError("denied", f"{field} 명령이 규칙상 허용되지 않음 ({KINDS.get(kind, kind)}): {cmd[:200]}",
                            recovery="다른 방법을 쓰거나 wb_person_ask 로 사용자에게 이유와 함께 묻는다")
        todo.append((field, cmd, kind, decision))
    asks = [t for t in todo if t[3] == "ask"]
    if asks:  # 한 번의 도구 호출에 들어 있는 명령은 카드 하나로 묻는다
        cmds = [c for _, c, _, _ in asks]
        ans = ask(settings, run_id, "permission", {
            "summary": "검증 도구가 실행할 명령: " + " / ".join(f"{f}: {c[:200]}" for f, c, _, _ in asks),
            "tool": "mcp:" + ",".join(f for f, _, _, _ in asks), "input": {"command": cmds[0], "commands": cmds},
            "permission_kind": asks[0][2]})
        if not ans.get("allow"):
            raise ToolError("denied", "사용자가 명령 실행을 거절함: " + " / ".join(c[:120] for c in cmds),
                            recovery="다른 방법을 쓰거나 wb_person_ask 로 사용자에게 이유와 함께 묻는다")
    new = [c for _, c, _, d in todo if d == "ask"]  # 규칙이 허용한 명령은 기억하지 않는다 — 나중에 규칙을 조이면 다시 거른다
    if new:
        update_run(settings, run_id, lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + new))))
