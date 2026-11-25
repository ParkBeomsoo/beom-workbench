"""이 실행이 띄운 프로세스 — 에이전트와 그 자식(개발 서버·감시 명령·빌드), 에이전트가 끝난 뒤에도 남은 것까지.

앱이 '백그라운드'로 보여 주고 사람이 확인해 끌 수 있게 한다 (Claude Code 데스크톱의 백그라운드 작업처럼).
끄기는 이 목록에 든 프로세스만 — 다른 프로그램은 건드리지 않는다.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

# 대상 폴더에서 남아 돌 수 있는 개발용 실행기 — 이것들만 작업 폴더(cwd)를 들여다본다 (모든 프로세스를 보면 느리다)
RUNTIMES = {"node", "python", "pythonw", "py", "deno", "bun", "java", "javaw", "dotnet", "go", "cargo", "ruby", "php", "electron",
            "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "fish", "uvicorn", "gunicorn", "vite", "esbuild", "msedge", "chrome",
            "python3", "Google Chrome"}
_SELF = ("beom-workbench-mcp", "beom_workbench.main", "beom_workbench\\main", "beom_workbench/main")
# 콘솔 창 호스트 — 명령마다 따라 뜨는 Windows 부속이라 '백그라운드'로 세지 않는다
_INFRA = {"conhost", "openconsole"}
_MEMO: dict = {}


def _agent_dirs() -> list[str]:
    """에이전트 자신의 설치·실행 폴더 — 여기서 도는 것(플러그인·런타임·도구 서버)은 에이전트의 일부이지 작업이 띄운 것이 아니다."""
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    dirs = [home / ".codex", home / ".claude", local / "OpenAI", local / "AnthropicClaude", local / "Programs" / "claude",
            Path(os.environ.get("CODEX_HOME") or home / ".codex"), Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")]
    return list(dict.fromkeys(_norm(d) for d in dirs))


def _agent_part(p) -> bool:
    """에이전트 자신의 부속 프로세스인가 (콘솔 호스트, 에이전트 폴더의 실행 파일이나 그 폴더의 스크립트를 도는 것)."""
    try:
        name = p.name().lower().removesuffix(".exe")
        if name in _INFRA:
            return True
        dirs = _agent_dirs()
        exe = p.exe()
        if exe and any(_inside(exe, d) for d in dirs):
            return True
        line = os.path.normcase(" ".join(p.cmdline()[1:3])).replace("/", os.sep)
        return any(d in line for d in dirs)
    except Exception:  # noqa: BLE001 — 끝났거나 볼 권한이 없음
        return False


def _psutil():
    try:
        import psutil  # noqa: PLC0415 — 없으면 기능만 끈다
    except ImportError:
        return None
    return psutil


def _norm(p: str | Path) -> str:
    import unicodedata  # macOS 한글 폴더 이름(NFD)도 같은 이름으로
    try:
        return unicodedata.normalize("NFC", os.path.normcase(str(Path(p).resolve())))
    except (OSError, ValueError):
        return unicodedata.normalize("NFC", os.path.normcase(str(p)))


def _inside(path: str, root: str) -> bool:
    a = _norm(path)
    return a == root or a.startswith(root.rstrip("\\/") + os.sep)


def _row(p, kind: str, root: str) -> dict | None:
    try:
        with p.oneshot():
            cmd = p.cmdline() or [p.name()]
            line = " ".join(cmd)
            if any(x in line for x in _SELF):
                return None
            mem = p.memory_info().rss
            since = datetime.fromtimestamp(p.create_time(), timezone.utc).isoformat(timespec="seconds")
            name = p.name()
    except Exception:  # noqa: BLE001 — 그 사이 끝났거나 볼 권한이 없음
        return None
    short = line.replace(str(Path(root)), ".")
    return {"pid": p.pid, "name": name.removesuffix(".exe"), "cmd": short[:240], "since": since, "kind": kind,
            "mem_mb": round(mem / 1048576)}


def list_for(run: dict) -> list[dict]:
    """에이전트(도는 중이면)·그 자식·대상 폴더에서 남아 도는 개발용 프로세스. 3초 안에 다시 물으면 지난 결과."""
    ps = _psutil()
    if ps is None or not run.get("target_root"):
        return []
    key = (run.get("id"), run.get("agent_pid"))
    hit = _MEMO.get(key)
    if hit and time.monotonic() - hit[0] < 3:
        return hit[1]
    root = _norm(run["target_root"])
    me = os.getpid()
    mine = {me}
    try:
        mine |= {x.pid for x in ps.Process(me).parents()}
    except Exception:  # noqa: BLE001
        pass
    out: dict[int, dict] = {}
    apid = run.get("agent_pid")
    if apid:
        try:
            ap = ps.Process(int(apid))
            # PID 는 다시 쓰인다 — 기록한 시작 시각과 맞을 때만 그 에이전트로 본다 (예전 기록이면 도는 중일 때만)
            ct = run.get("agent_ctime")
            same = abs(ap.create_time() - float(ct)) < 2 if ct else run.get("status") in ("running", "waiting_answer")
            if same and ap.is_running() and ap.status() != getattr(ps, "STATUS_ZOMBIE", "zombie"):
                r = _row(ap, "agent", root)
                if r:
                    out[ap.pid] = r
                for k in ap.children(recursive=True):
                    if k.pid not in mine and not _agent_part(k) and (r := _row(k, "child", root)):
                        out[k.pid] = r
        except Exception:  # noqa: BLE001 — 에이전트가 이미 끝남
            pass
    out.update({r["pid"]: r for r in _leftovers(ps, root, mine) if r["pid"] not in out})
    rows = sorted(out.values(), key=lambda r: ({"agent": 0, "child": 1, "left": 2}[r["kind"]], r["since"]))
    _MEMO[key] = (time.monotonic(), rows)
    return rows


def _leftovers(ps, root: str, mine: set) -> list[dict]:
    """대상 폴더에서 남아 도는 것 — 모든 프로세스를 훑어 비싸다 (Windows 에서 exe·cwd 조회) — 15초에 한 번만."""
    hit = _MEMO.get(("left", root))
    if hit and time.monotonic() - hit[0] < 15:
        return hit[1]
    import re
    # 대상 폴더 경로가 경계까지 그대로 나올 때만 (D:\proj 가 D:\proj-x 에 걸리지 않게)
    in_cmd = re.compile(re.escape(root.rstrip("\\/")) + r"""(?=[\\/"'\s]|$)""")
    out: dict[int, dict] = {}
    for p in ps.process_iter(["pid", "name"]):
        if p.pid in mine:
            continue
        name = (p.info.get("name") or "").lower().removesuffix(".exe")
        try:
            exe_in = False
            if name not in RUNTIMES:
                exe = p.exe() if name else ""
                exe_in = bool(exe) and _inside(exe, root)  # 이번에 만든 앱(dist/App.exe 등)이 떠 있음
                if not exe_in:
                    continue
            if exe_in or _inside(p.cwd(), root) or in_cmd.search(os.path.normcase(" ".join(p.cmdline()))):
                if r := _row(p, "left", root):
                    out[p.pid] = r
        except Exception:  # noqa: BLE001 — 볼 권한 없음·끝남
            continue
    rows = list(out.values())
    _MEMO[("left", root)] = (time.monotonic(), rows)
    return rows


def reap_orphans(root: str, since: float, keep: set[int] | None = None) -> list[dict]:
    """라운드가 끝난 뒤 — 그 라운드 동안 생겨 작업 폴더에서 도는데 띄운 부모가 이미 없는 프로세스를 끈다.
    에이전트는 '닫히면 함께 끝나는 Job' 안에서 돌지만, 스스로 Job 밖으로 나가겠다고 한 프로세스(분리해서 띄운 명령·개발 서버)는
    빠져나가 실행이 끝난 뒤에도 남았다 (집 폴더 전체를 훑는 find, 며칠째 떠 있던 개발 서버 — 전체 기능 시험 164).
    사용자가 그 폴더에서 연 터미널·편집기는 부모(터미널·편집기)가 살아 있어 건드리지 않는다. 끈 것의 목록."""
    ps = _psutil()
    if ps is None or not root:
        return []
    rootn = _norm(root)
    mine = {os.getpid()} | set(keep or ())
    try:
        mine |= {x.pid for x in ps.Process(os.getpid()).parents()}
    except Exception:  # noqa: BLE001
        pass
    killed = []
    for p in ps.process_iter(["create_time"]):  # 부모 번호는 후보만 — 모두에게 물으면 Windows 에서 프로세스마다 전체 표를 다시 읽어 17초 걸렸다
        try:
            if p.pid in mine or (p.info.get("create_time") or 0) < since - 1:
                continue
            if not _inside(p.cwd(), rootn) or _agent_part(p):  # 작업 폴더부터 — 에이전트 부속인지 보는 쪽이 더 비싸다
                continue
            ppid = p.ppid()
            # macOS·Linux 에서 부모가 끝난 프로세스는 launchd·init(1번)이 맡는다 — 1번은 늘 먼저 떠 있어 '부모가 살아 있다'로 보였다.
            # 단 Finder·Dock 에서 연 앱(.app 안의 실행 파일)도 부모가 1번이다 — 그것은 사용자가 연 것이라 두고 간다
            adopted = os.name != "nt" and ppid <= 1
            if adopted:
                try:
                    if ".app/Contents/" in (p.exe() or ""):
                        continue
                except Exception:  # noqa: BLE001
                    continue
            parent = ps.Process(ppid) if ppid and not adopted and ps.pid_exists(ppid) else None
            if parent is not None and parent.create_time() <= p.info["create_time"]:
                continue  # 부모가 살아 있다 — 누군가(사용자의 터미널 등)가 아직 돌보고 있다
            row = _row(p, "left", root) or {"pid": p.pid, "name": p.info.get("name") or "", "cmd": ""}
            for k in p.children(recursive=True):
                try:
                    k.kill()
                except Exception:  # noqa: BLE001
                    pass
            p.kill()
            killed.append(row)
        except Exception:  # noqa: BLE001 — 그 사이 끝났거나 볼 권한이 없음
            continue
    _MEMO.clear()
    return killed


def kill(run: dict, pid: int) -> dict:
    """목록에 든 프로세스만, 자식까지 끈다."""
    from beom_workbench.common.response import ToolError

    ps = _psutil()
    if ps is None:
        raise ToolError("not_ready", "프로세스 목록을 볼 수 없습니다 (psutil 없음)")
    if int(pid) not in {r["pid"] for r in list_for(run)}:
        _MEMO.clear()
        if int(pid) not in {r["pid"] for r in list_for(run)}:
            raise ToolError("not_found", "이 작업이 띄운 프로세스가 아닙니다")
    try:
        p = ps.Process(int(pid))
        kids = p.children(recursive=True)
        for k in kids:
            try:
                k.kill()
            except Exception:  # noqa: BLE001
                pass
        p.kill()
    except ps.NoSuchProcess:
        pass
    _MEMO.clear()
    return {"killed": int(pid)}
