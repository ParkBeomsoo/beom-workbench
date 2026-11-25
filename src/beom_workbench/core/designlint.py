"""External design anti-pattern detector — a maintained open-source rule set (deterministic, 60+ rules for the 'generated look'
and for craft problems such as occluded text or one-word lines), run by the toolkit on the screens the design check opens.

The toolkit owns the installation: an npm package pinned to one version, installed once into the toolkit's own folder (its engine
is a native binary — no Node is needed to run it afterwards), and its settings folder points inside the toolkit's home so nothing
is written to the user's home. When it cannot be installed (no npm, offline), the design check runs on with its own rules."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = "impeccable@4.10.0"   # the detector, pinned
_TARGET = ({"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux") + "-"
           + ("arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"))
_EXE = "impeccable.exe" if sys.platform == "win32" else "impeccable"
RETRY_AFTER = 24 * 3600         # a failed install is tried again a day later, not on every check
# Rules about the generated look (the rest are craft problems)
SLOP = "slop"


def folder(settings) -> Path:
    return Path(settings.home) / "tools" / "design-detector"


def binary(settings) -> Path | None:
    exe = folder(settings) / "node_modules" / "@impeccable" / f"cli-{_TARGET}" / "bin" / _EXE
    return exe if exe.is_file() else None


def _env(settings) -> dict:
    home = folder(settings) / "home"
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.json"
    if not cfg.is_file():
        cfg.write_text(json.dumps({"telemetry": False}), encoding="utf-8")  # no usage data leaves the machine
    env = {**os.environ, "IMPECCABLE_HOME": str(home), "DO_NOT_TRACK": "1"}
    return env


def ensure(settings, timeout: int = 240) -> Path | None:
    """The detector's binary — installs it once if npm is there. None when it cannot be had now."""
    exe = binary(settings)
    if exe:
        return exe
    root = folder(settings)
    mark = root / ".install-failed"
    try:
        if mark.is_file() and time.time() - mark.stat().st_mtime < RETRY_AFTER:
            return None
    except OSError:
        pass
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return None
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "package.json").is_file():
        (root / "package.json").write_text(json.dumps({"name": "beom-design-detector", "private": True}), encoding="utf-8")
    try:
        cp = subprocess.run([npm, "install", PACKAGE, "--no-audit", "--no-fund", "--ignore-scripts", "--loglevel", "error"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=_env(settings),
                            shell=sys.platform == "win32")
        ok = cp.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    exe = binary(settings)
    if not (ok and exe):
        try:
            mark.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except OSError:
            pass
        return None
    try:
        mark.unlink()
    except OSError:
        pass
    return exe


def scan(settings, targets: list[str], viewport: str | None = None, timeout: int = 180) -> dict:
    """Run the detector on files or URLs → {"ok", "findings": [{rule, name, category, severity, where, snippet}], "error"?}."""
    targets = [t for t in targets if t]
    if not targets:
        return {"ok": True, "findings": []}
    exe = ensure(settings)
    if not exe:
        return {"ok": False, "findings": [], "error": "외부 디자인 검출기를 설치하지 못함 (npm 없음 또는 오프라인)"}
    argv = [str(exe), "detect", "--json", "--no-config", *(["--viewport", viewport] if viewport else []), *targets]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=_env(settings))
    except subprocess.TimeoutExpired:
        return {"ok": False, "findings": [], "error": f"{timeout}초 안에 끝나지 않음"}
    except OSError as e:
        return {"ok": False, "findings": [], "error": str(e)[:200]}
    try:
        raw = json.loads(cp.stdout or "[]")
    except ValueError:
        return {"ok": False, "findings": [], "error": (cp.stderr or "결과를 읽지 못함")[-200:]}
    rows = raw if isinstance(raw, list) else raw.get("findings") or []
    out = []
    for f in rows:
        if not isinstance(f, dict):
            continue
        where = str(f.get("file") or "")
        if f.get("line"):
            where += f":{f['line']}"
        out.append({"rule": str(f.get("antipattern") or f.get("rule") or ""), "name": str(f.get("name") or "")[:120],
                    "category": str(f.get("category") or ""), "severity": str(f.get("severity") or ""),
                    "where": where[-160:], "snippet": str(f.get("snippet") or f.get("selector") or "")[:120],
                    "description": str(f.get("description") or "")[:240]})
    return {"ok": True, "findings": out[:60]}
