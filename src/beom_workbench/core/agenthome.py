"""Agent homes owned by the toolkit — the toolkit runs the user's claude·codex with their config folder pointed at a folder of its
own (CLAUDE_CONFIG_DIR · CODEX_HOME). The conversations between the toolkit and the agent are then never written where the user's
agent apps look: Claude Code's list, the Codex app's list, and the Codex app's import of Claude Code conversations (it reads
~/.claude/projects — it picked up toolkit runs that were only there while a round ran).

Only the sign-in and the user's own settings are brought over. The sign-in is kept the same both ways while a round runs: the
sign-in services retire a refresh token once it is used, so a sign-in the agent refreshed here is copied back to the user's
folder (and one the user refreshed or changed is copied here) — neither side is left holding a retired token.

When the sign-in is not a file (macOS keychain, Codex 'keyring' store) it cannot be shared — the agent then runs in the user's
own folder as before, and agentrecords moves the conversation out after each round.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

SPEC = {
    "claude": {"env": "CLAUDE_CONFIG_DIR", "default": ".claude", "creds": ".credentials.json",
               "copy": ("settings.json", "CLAUDE.md", "agents"),
               # folders the agent also fills itself (account-synced skills) — the user's entries are brought over one by one
               "merge": {"skills": ("synced",), "commands": ()}},
    "codex": {"env": "CODEX_HOME", "default": ".codex", "creds": "auth.json",
              "copy": ("config.toml", "AGENTS.md", "rules"),
              "merge": {"skills": (".system",), "prompts": ()}},
}
_SYNC = ".beom-sync.json"
_LOCK = threading.Lock()


def user_home(agent_id: str) -> Path | None:
    spec = SPEC.get(agent_id)
    if not spec:
        return None
    return Path(os.environ.get(spec["env"]) or Path.home() / spec["default"])


def own_home(settings, agent_id: str) -> Path | None:
    return Path(settings.home) / "agent-home" / agent_id if agent_id in SPEC else None


def usable(settings, agent_id: str, run: dict | None = None) -> bool:
    """Can this run use the toolkit's own folder — a sign-in file to share, or an API key passed in the environment."""
    spec = SPEC.get(agent_id)
    if not spec:
        return False
    if (run or {}).get("auth") == "api_key":
        return True
    u = user_home(agent_id)
    return bool(u and (u / spec["creds"]).is_file())


def home(settings, agent_id: str, run: dict | None = None) -> Path | None:
    """The folder this run's agent keeps its conversations in — the toolkit's own when it can be used, else None (the user's)."""
    return own_home(settings, agent_id) if usable(settings, agent_id, run) else None


def _digest(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _write(src: Path, dst: Path) -> bool:
    """Copy src over dst in one step (a reader never sees half a file). False if it could not be written now (in use)."""
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, tmp)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def sync_creds(settings, agent_id: str) -> str:
    """Keep the sign-in the same in both folders. The side that changed since the last sync wins (both changed → the newer file).
    Returns what happened: 'same' · 'in' (user's → toolkit's) · 'out' (toolkit's → user's) · 'none' · 'busy'."""
    spec, u_home, o_home = SPEC.get(agent_id), user_home(agent_id), own_home(settings, agent_id)
    if not spec or not u_home or not o_home:
        return "none"
    u, o = u_home / spec["creds"], o_home / spec["creds"]
    state_file = o_home / _SYNC
    with _LOCK:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        last = state.get("creds") or {}
        hu, ho = _digest(u), _digest(o)
        how = "same"
        if hu is None and ho is None:
            return "none"
        if hu != ho:
            user_changed, own_changed = hu != last.get("u"), ho != last.get("o")
            if hu is None:
                # 사용자가 로그아웃했다 — 툴킷 쪽도 지운다 (툴킷 쪽만 남기면 사용자가 끊은 로그인으로 계속 돈다)
                if user_changed or not own_changed:
                    try:
                        o.unlink()
                    except OSError:
                        return "busy"
                    how = "in"
                else:
                    return "none"
            elif ho is None or (user_changed and not own_changed):
                how = "in"
            elif own_changed and not user_changed:
                how = "out"
            else:  # 둘 다 바뀌었다 — 나중에 쓴 쪽
                how = "out" if o.stat().st_mtime > u.stat().st_mtime else "in"
            if how == "in" and hu is not None and not _write(u, o):
                return "busy"
            if how == "out" and not _write(o, u):
                return "busy"
            hu = ho = _digest(u)
        state["creds"] = {"u": hu, "o": ho}
        try:
            o_home.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass
        return how


def _tree(p: Path) -> dict | None:
    """What a folder holds — {relative path: digest}; None if it is not a folder."""
    if not p.is_dir():
        return None
    return {f.relative_to(p).as_posix(): _digest(f) for f in sorted(p.rglob("*")) if f.is_file()}


def _mirror(src: Path, dst: Path) -> None:
    """dst becomes what src is (a file, a folder, or nothing). A folder is replaced only when it differs: another run of the toolkit
    may be reading it right now."""
    try:
        if src.is_dir():
            if _tree(src) == _tree(dst):
                return
            if dst.exists():
                shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
            shutil.copytree(src, dst)
        elif src.is_file():
            if _digest(src) != _digest(dst):
                if dst.is_dir():
                    shutil.rmtree(dst)
                _write(src, dst)
        elif dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    except OSError:
        pass


def _sync_settings(agent_id: str, o_home: Path) -> None:
    """The user's own settings for the agent (instructions file, settings, rules, skills, commands) — brought over each round,
    removed if gone. Skills and commands go entry by entry: the agent keeps entries of its own there (account-synced skills, its
    bundled skills), which are neither replaced nor removed — only entries the toolkit brought over earlier are removed when the
    user removes them."""
    spec, u_home = SPEC[agent_id], user_home(agent_id)
    for name in spec["copy"]:
        _mirror(u_home / name, o_home / name)
    state_file = o_home / _SYNC
    with _LOCK:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        brought = state.get("merged") or {}
        for name, own in spec.get("merge", {}).items():
            src, dst = u_home / name, o_home / name
            have = sorted(c.name for c in src.iterdir() if c.name not in own) if src.is_dir() else []
            for child in have:
                _mirror(src / child, dst / child)
            for gone in set(brought.get(name) or []) - set(have):
                _mirror(src / gone, dst / gone)  # the user removed it — remove the copy
            brought[name] = have
        state["merged"] = brought
        try:
            o_home.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass


def prepare(settings, agent_id: str, run: dict | None = None) -> dict:
    """Before a round — the environment that points the agent at the toolkit's folder ({} when it must use the user's own)."""
    o_home = home(settings, agent_id, run)
    if o_home is None:
        return {}
    o_home.mkdir(parents=True, exist_ok=True)
    _sync_settings(agent_id, o_home)
    sync_creds(settings, agent_id)
    return {SPEC[agent_id]["env"]: str(o_home)}


class Watch:
    """While a round runs — keeps the sign-in the same in both folders every few seconds (a refresh on either side reaches the
    other before it needs it). Stopping syncs once more."""

    def __init__(self, settings, agent_id: str, every: float = 2.0):
        self.settings, self.agent_id, self.every = settings, agent_id, every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Watch":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            try:
                sync_creds(self.settings, self.agent_id)
            except Exception:  # noqa: BLE001 — 다음 차례에 다시
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        for _ in range(3):
            if sync_creds(self.settings, self.agent_id) != "busy":
                break
            time.sleep(0.5)


