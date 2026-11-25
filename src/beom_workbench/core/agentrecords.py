"""에이전트 대화 기록을 사용자의 에이전트 앱에 남기지 않는다.

지금은 에이전트가 처음부터 툴킷의 폴더에서 돈다 (agenthome — CLAUDE_CONFIG_DIR·CODEX_HOME). 여기서는 라운드가 끝난 대화를 그 폴더에서
실행 폴더로 옮겨(실행을 지우면 함께 지워지게) 다음 라운드 전에 되돌린다. 로그인을 파일로 나눌 수 없어 사용자의 폴더에서 돌았던
실행(macOS 키체인 등)·예전 판이 남긴 대화도 같은 방법으로 옮긴다 — 아래는 그 방법의 설명이다.

툴킷은 사용자의 claude·codex·agy 를 그대로 띄우므로, 그대로 두면 툴킷과 에이전트가 주고받은 대화(지시·도구 결과·다시 낸 계획)가
사용자의 Claude Code·Codex 기록에 쌓인다 (실험: 실험 실행 수백 개가 ~/.claude/projects 에 1GB 넘게, Codex 보관함에 남았다).
그 대화는 앱의 실행 기록(대화 흐름)에서 본다 — 에이전트 쪽에는 남기지 않는다.

라운드마다 대화를 이어 받아야 해서(resume) 저장을 아예 끌 수는 없다 (Claude 의 --no-session-persistence, Codex 의 ephemeral 은
다음 프로세스에서 이어 받지 못한다). 그래서 라운드가 끝나면 그 대화 파일을 툴킷의 실행 폴더로 옮기고(Codex 는 복사한 뒤 Codex 에서
지운다 — 목록·색인까지 함께 지워진다), 다음 라운드 전에 제자리에 되돌린다. 라운드가 도는 동안만 에이전트 쪽에 있다.
옮기는 것은 이 실행이 만든 대화(실행에 남은 session_id) 하나뿐이다 — 사용자의 다른 대화는 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from beom_workbench.core import inbox

_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _store(settings, run_id: str, agent: str) -> Path:
    return inbox.run_dir(settings, run_id) / "agent-session" / agent


def _homes(settings, agent: str) -> list[Path]:
    """이 에이전트의 대화가 있을 수 있는 폴더 — 툴킷의 폴더(agenthome)와 사용자의 폴더."""
    from beom_workbench.core import agenthome
    own, user = agenthome.own_home(settings, agent), agenthome.user_home(agent)
    return [h for h in (own, user) if h is not None and (h != own or h.is_dir())]


def _target(settings, run: dict) -> Path:
    """이번 라운드가 대화를 읽고 쓸 폴더."""
    from beom_workbench.core import agenthome
    agent = run.get("agent") or "claude"
    return agenthome.home(settings, agent, run) or agenthome.user_home(agent)


def _agy_home() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def _safe_id(sid) -> str | None:
    s = str(sid or "").strip()
    return s if s and all(c.isalnum() or c in "-_" for c in s) and len(s) <= 80 else None


def _note(store: Path, data: dict) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / "where.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _where(store: Path) -> dict:
    try:
        return json.loads((store / "where.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _move(src: Path, dst: Path) -> None:
    """옮긴다 — 자리에 이미 있으면 지우지 않는다: 파일이면 더 큰 쪽(대화가 더 긴 쪽)을 그 자리에 두고 다른 쪽은 옆에 남긴다 (대화가 이어
    쌓이므로 짧은 쪽은 앞부분이다). 폴더면 합친다."""
    import time
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.is_dir() and src.is_dir():
            for child in list(src.iterdir()):
                _move(child, dst / child.name)
            shutil.rmtree(src, ignore_errors=True)
            return
        aside = dst.with_name(f"{dst.stem}.{int(time.time())}.prev{dst.suffix}")
        if dst.is_file() and src.is_file() and dst.stat().st_size > src.stat().st_size:
            shutil.move(str(src), str(aside))  # 자리의 것이 더 길다 — 그대로 두고 새 조각을 옆에
            return
        shutil.move(str(dst), str(aside))
    shutil.move(str(src), str(dst))


# ---------------------------------------------------------------- Claude Code

def _claude_stash(store: Path, sid: str, home: Path) -> list[str]:
    moved = []
    for f in (home / "projects").glob(f"*/{sid}.jsonl"):
        proj = f.parent
        _note(store, {"project": proj.name})  # 폴더 이름만 — 되돌릴 때는 그 라운드가 쓰는 폴더 아래로
        _move(f, store / f.name)
        moved.append(f.name)
        side = proj / sid  # 하위 에이전트·긴 도구 결과
        if side.is_dir():
            _move(side, store / sid)
        mem = proj / "memory"
        try:
            if mem.is_dir() and not any(mem.iterdir()):
                mem.rmdir()
            if not any(proj.iterdir()):
                proj.rmdir()
        except OSError:
            pass
    # 파일 되돌리기용 사본·세션 환경 — 이어 받는 데 필요 없다 (툴킷은 자기 체크포인트로 되돌린다)
    for extra in (home / "file-history" / sid, home / "session-env" / sid):
        if extra.exists():
            shutil.rmtree(extra, ignore_errors=True)
    for pat in ("todos", "debug"):
        for x in (home / pat).glob(f"{sid}*"):
            try:
                x.unlink()
            except OSError:
                pass
    return moved


def _claude_restore(store: Path, sid: str, home: Path) -> bool:
    if any((home / "projects").glob(f"*/{sid}.jsonl")):
        return True
    f = store / f"{sid}.jsonl"
    proj = _where(store).get("project")
    if not f.is_file() or not proj:
        return False
    proj = home / "projects" / Path(proj).name  # 예전 기록은 전체 경로였다
    if (proj / f.name).exists():
        return True
    _move(f, proj / f.name)
    if (store / sid).is_dir():
        _move(store / sid, proj / sid)
    return True


# ---------------------------------------------------------------- Codex

def _codex_rollouts(sid: str, home: Path) -> list[Path]:
    return [p for sub in ("sessions", "archived_sessions") for p in (home / sub).rglob(f"rollout-*{sid}.jsonl")]


def _codex_stash(store: Path, sid: str, codex: list[str] | None, home: Path) -> list[str]:
    found = _codex_rollouts(sid, home)
    if not found:
        return []
    src = max(found, key=lambda p: p.stat().st_mtime)
    store.mkdir(parents=True, exist_ok=True)
    dst = store / src.name
    shutil.copyfile(src, dst)  # 먼저 복사하고 확인한 뒤 지운다 — 지우다 실패해도 대화는 그대로 남아 다음에 다시 지운다
    if dst.stat().st_size != src.stat().st_size:
        return []
    _note(store, {"rollout": src.name})
    if codex:
        try:
            subprocess.run([*codex, "delete", "--force", sid], capture_output=True, timeout=60,
                           env={**os.environ, "CODEX_HOME": str(home)}, **_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    if any(p.exists() for p in _codex_rollouts(sid, home)):
        for p in _codex_rollouts(sid, home):  # 지우기 명령이 없는 버전 — 파일만이라도 뺀다 (목록에는 남을 수 있다)
            try:
                p.unlink()
            except OSError:
                pass
    return [src.name]


def _codex_restore(store: Path, sid: str, home: Path) -> bool:
    if _codex_rollouts(sid, home):
        return True
    name = _where(store).get("rollout")
    f = store / name if name else None
    if not f or not f.is_file():
        return False
    dst = home / "sessions" / f.name  # sessions 아래 어디든 된다 — 이어 받을 때 Codex 가 목록·색인을 다시 만든다
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(f, dst)
    return True


# ---------------------------------------------------------------- Antigravity CLI (agy)

def _agy_paths(cid: str) -> list[Path]:
    home = _agy_home()
    return [p for p in [*(home / "conversations").glob(f"{cid}*"), home / "brain" / cid] if p.exists()]


def _agy_stash(store: Path, cid: str) -> list[str]:
    moved = []
    for p in _agy_paths(cid):
        rel = p.relative_to(_agy_home()).as_posix()
        _move(p, store / "files" / rel)
        moved.append(rel)
    if moved:
        _note(store, {"files": moved})
    return moved


def _agy_restore(store: Path, cid: str) -> bool:
    for rel in _where(store).get("files") or []:
        src = store / "files" / rel
        if src.exists() and not (_agy_home() / rel).exists():
            _move(src, _agy_home() / rel)
    return True


def _agy_mcp_file(run: dict, restore: bool) -> None:
    """agy 는 대상 폴더의 .agents/mcp_config.json 을 읽는다 — 라운드가 끝나면 툴킷이 넣은 항목을 빼서 사용자의 저장소에 남기지 않는다."""
    from beom_workbench.core.agents import MCP_NAME
    f = Path(run["target_root"]) / ".agents" / "mcp_config.json"
    if restore or not f.is_file():
        return
    try:
        cur = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    servers = cur.get("mcpServers") if isinstance(cur, dict) else None
    if not isinstance(servers, dict) or MCP_NAME not in servers:
        return
    servers.pop(MCP_NAME)
    if servers or len(cur) > 1:
        f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        f.unlink()
        try:
            f.parent.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- 실행기가 부르는 것

def keep(run: dict) -> bool:
    """사용자가 에이전트 앱에도 대화를 남기라고 했나 (실행 옵션 keep_agent_history) — 기본은 남기지 않는다."""
    return bool(run.get("keep_agent_history"))


def stash(settings, run: dict, codex: list[str] | None = None) -> list[str]:
    """라운드가 끝난 뒤 — 이 실행의 대화를 에이전트 쪽에서 툴킷의 실행 폴더로 옮긴다. 실패해도 실행에는 영향이 없다."""
    agent = run.get("agent") or "claude"
    sid = _safe_id(run.get("session_id"))
    out: list[str] = []
    try:
        if agent == "gemini":
            _agy_mcp_file(run, restore=False)
        if not sid or keep(run):
            return out
        store = _store(settings, run["id"], agent)
        if agent in ("claude", "codex"):
            for home in _homes(settings, agent):
                out += _claude_stash(store, sid, home) if agent == "claude" else _codex_stash(store, sid, codex, home)
        elif agent == "gemini":
            out = _agy_stash(store, sid)
    except Exception as e:  # noqa: BLE001
        inbox.log(settings, run["id"], "note", tone="", text=f"에이전트 대화 기록을 옮기지 못했습니다 — {e}"[:200])
    return out


def restore(settings, run: dict, codex: list[str] | None = None) -> bool:
    """라운드를 시작하기 전 — 이어 받을 대화를 제자리에 되돌린다 (없으면 그대로)."""
    agent = run.get("agent") or "claude"
    sid = _safe_id(run.get("session_id"))
    if not sid:
        return False
    try:
        store = _store(settings, run["id"], agent)
        if agent in ("claude", "codex"):
            target = _target(settings, run)
            for home in _homes(settings, agent):
                if home != target:  # 다른 폴더에 남은 것(예전 판·로그인을 나눌 수 없던 라운드) — 실행 폴더를 거쳐 이번 폴더로
                    _claude_stash(store, sid, home) if agent == "claude" else _codex_stash(store, sid, codex, home)
            if not store.is_dir():
                return False
            return _claude_restore(store, sid, target) if agent == "claude" else _codex_restore(store, sid, target)
        if not store.is_dir():
            return False
        if agent == "gemini":
            return _agy_restore(store, sid)
    except Exception as e:  # noqa: BLE001
        inbox.log(settings, run["id"], "note", tone="warn", text=f"에이전트 대화를 되돌리지 못했습니다 — 새 대화로 이어 갑니다 ({e})"[:200])
    return False


def sweep(settings, codex_for=None) -> int:
    """돌고 있지 않은 실행의 대화가 에이전트 쪽에 남아 있으면 옮긴다 — 앱이 라운드 도중에 꺼져 옮기지 못한 것. 실행기가 잡고 있는 실행
    (지금 라운드가 도는 실행)은 건드리지 않는다."""
    from beom_workbench.core import runner
    n = 0
    for r in inbox.list_runs(settings):
        if not _safe_id(r.get("session_id")) or keep(r):
            continue
        try:
            if runner.lock_owner(settings, r["id"]) is not None:
                continue
        except Exception:  # noqa: BLE001 — 잠금을 모르면 건드리지 않는다
            continue
        try:
            run = inbox.load_run(settings, r["id"])
        except Exception:  # noqa: BLE001
            continue
        if run.get("status") == "running":
            continue
        codex = codex_for(run) if codex_for and run.get("agent") == "codex" else None
        n += len(stash(settings, run, codex))
    return n
