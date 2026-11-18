"""의존성 준비 — 에이전트가 쓴 의존성 파일대로 툴킷이 정해진 곳에 설치한다.

에이전트는 package.json·requirements.txt·pyproject.toml 같은 의존성 파일만 쓴다. 설치 방법·위치를 에이전트가 고르면 실행마다
달라지고(전역·.venv·.beom/tmp/deps …), 권한 카드가 여러 번 뜨고, 자체 샌드박스에서 인터넷이 막힌 에이전트(Codex)는 설치를 못 한다.
그래서 툴킷이 같은 자리에 설치한다:
- Node: 프로젝트의 node_modules (Node 가 프로젝트 폴더에서 찾는다) — npm install
- Python: 대상 폴더의 .beom/venv (커밋되지 않는다) — 의존성 + 검증용 pytest·coverage
의존성 파일 내용이 바뀔 때만 다시 설치한다 (.beom/deps/ 에 내용 지문). 설치는 에이전트 샌드박스 밖에서 돌고 권한 규칙을 거친다
(빌드·'격리 없이 실행' 둘 다 허용이면 바로, 아니면 카드 한 번). 내려받은 파일은 npm·pip 의 PC 캐시를 그대로 쓴다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from beom_workbench.common.response import ToolError

REQ_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "dev-requirements.txt", "test-requirements.txt")
VERIFY_TOOLS = ("pytest", "coverage")


def venv_dir(root: Path) -> Path:
    return Path(root) / ".beom" / "venv"


def venv_python(root: Path) -> Path:
    v = venv_dir(root)
    return v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def env_paths(root: Path) -> list[str]:
    """툴킷이 돌리는 명령의 PATH 앞에 붙일 자리 — 준비된 .beom/venv 와 node_modules/.bin."""
    root = Path(root)
    out = []
    v = venv_dir(root) / ("Scripts" if os.name == "nt" else "bin")
    if v.is_dir():
        out.append(str(v))
    nb = root / "node_modules" / ".bin"
    if nb.is_dir():
        out.append(str(nb))
    return out


def _node_needed(root: Path) -> list[str] | None:
    pj = root / "package.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not (data.get("dependencies") or data.get("devDependencies")):
        return None
    return [n for n in ("package.json", "package-lock.json") if (root / n).is_file()]


def _python_reqs(root: Path) -> tuple[list[str], list[str]]:
    """(의존성 파일들, pyproject 에 적힌 패키지들)."""
    files = [n for n in REQ_FILES if (root / n).is_file()]
    pkgs: list[str] = []
    pp = root / "pyproject.toml"
    if pp.is_file():
        import tomllib
        try:
            proj = tomllib.loads(pp.read_text(encoding="utf-8")).get("project") or {}
        except (OSError, ValueError):
            proj = {}
        pkgs += [str(x) for x in proj.get("dependencies") or []]
        for k, v in (proj.get("optional-dependencies") or {}).items():
            if k in ("test", "tests", "dev", "testing"):
                pkgs += [str(x) for x in v or []]
    return files, pkgs


def _stamp(root: Path, files: list[str], extra: list[str] = ()) -> str:
    h = hashlib.sha256()
    for n in files:
        h.update(n.encode())
        h.update((root / n).read_bytes())
    for x in extra:
        h.update(x.encode())
    return h.hexdigest()


def _stamp_file(root: Path, kind: str) -> Path:
    return Path(root) / ".beom" / "deps" / f"{kind}.sha"


def plan(root: Path) -> list[dict]:
    """설치가 필요한 것 — 의존성 파일이 있고, 지문이 지난번 설치와 다르거나 설치된 자리가 없을 때."""
    root = Path(root)
    out = []
    node = _node_needed(root)
    if node:
        # 지문은 package.json 만으로 — package-lock.json 은 npm 이 설치하면서 새로 쓰거나 고쳐서, 넣으면 매번 다시 설치하게 된다
        sig = _stamp(root, ["package.json"])
        done = _stamp_file(root, "node")
        if not (root / "node_modules").is_dir() or not done.is_file() or done.read_text().strip() != sig:
            out.append({"kind": "node", "files": node, "sig": sig, "command": "npm install --no-audit --no-fund"})
    files, pkgs = _python_reqs(root)
    if files or pkgs:
        sig = _stamp(root, files, pkgs + list(VERIFY_TOOLS))
        done = _stamp_file(root, "python")
        if not venv_python(root).is_file() or not done.is_file() or done.read_text().strip() != sig:
            what = " ".join([*(f"-r {f}" for f in files), *pkgs])
            out.append({"kind": "python", "files": files + (["pyproject.toml"] if pkgs else []), "sig": sig, "pkgs": pkgs,
                        "command": f"pip install {what} pytest coverage  (.beom/venv)"})
    return out


def _base_python() -> str:
    """가상 환경을 만들 파이썬 — 사용자가 쓰는 python (Windows 스토어 바로가기는 아니다), 없으면 툴킷의 파이썬."""
    p = shutil.which("python") or shutil.which("python3")
    if p and "WindowsApps" not in p:
        return p
    return sys.executable


def _run(argv: list[str], root: Path, timeout: float) -> tuple[int, str]:
    from beom_workbench.core import port, runtime
    env = runtime.safe_env({"PYTHONIOENCODING": "utf-8", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    try:
        cp = port.run_tree(argv, cwd=str(root), timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        return 124, ((e.output or b"") + (e.stderr or b"")).decode("utf-8", "replace")[-2000:] + f"\n({timeout:.0f}초 안에 끝나지 않음)"
    return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", "replace")[-3000:]


def _install(item: dict, root: Path, timeout: float) -> dict:
    if item["kind"] == "node":
        argv = (["cmd", "/d", "/c", "npm", "install", "--no-audit", "--no-fund"] if os.name == "nt"
                else ["npm", "install", "--no-audit", "--no-fund"])
        code, out = _run(argv, root, timeout)
    else:
        py = venv_python(root)
        if not py.is_file():
            code, out = _run([_base_python(), "-m", "venv", str(venv_dir(root))], root, 300)
            if code != 0:
                return {"kind": "python", "ok": False, "output_tail": out}
        args = [str(py), "-m", "pip", "install", "-q"]
        for f in item["files"]:
            if f != "pyproject.toml":
                args += ["-r", f]
        args += [*item.get("pkgs", []), *VERIFY_TOOLS]
        code, out = _run(args, root, timeout)
    ok = code == 0
    if ok:
        sf = _stamp_file(root, item["kind"])
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(item["sig"], encoding="utf-8")
    return {"kind": item["kind"], "ok": ok, "exit_code": code, **({} if ok else {"output_tail": out})}


def _permit(settings, run: dict | None, item: dict) -> None:
    """자동 실행 중이면 권한 규칙대로 — 빌드와 '격리 없이 실행' 둘 다 허용이면 바로, 아니면 카드 한 번 (같은 지문이면 다시 묻지 않는다)."""
    if run is None:
        return
    from beom_workbench.core import inbox
    kd, ku = inbox.decide(run, "build"), inbox.decide(run, "unsandboxed")
    if "refuse" in (kd, ku):
        raise ToolError("denied", "규칙상 의존성 설치가 허용되지 않음 (빌드 또는 격리 없이 실행이 거절)",
                        recovery="사용자에게 권한 설정을 바꿔 달라고 하거나 의존성 없이 만든다")
    key = f"deps:{item['kind']}:{item['sig'][:16]}"
    if kd == "allow" and ku == "allow" or key in (run.get("approved_cmds") or []):
        return
    files = ", ".join(item["files"])
    ans = inbox.ask(settings, run["id"], "permission", {
        "summary": f"의존성 설치 (툴킷이 격리 밖에서): {item['command']} — {files}",
        "tool": "mcp:deps", "input": {"command": item["command"]}, "permission_kind": "unsandboxed"})
    if not ans.get("allow"):
        raise ToolError("denied", "사용자가 의존성 설치를 거절함", recovery="의존성 없이 만들거나 wb_person_ask 로 이유와 함께 묻는다")
    if ans.get("always"):
        inbox.update_run(settings, run["id"], lambda r: r["policy"].__setitem__("unsandboxed", "allow"))
    inbox.update_run(settings, run["id"], lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + [key]))))


def ensure(settings, run: dict | None, root: Path, timeout: float = 900) -> dict:
    """필요한 설치를 하고 결과를 돌려준다. 설치할 것이 없으면 바로 끝난다."""
    root = Path(root)
    done = []
    for item in plan(root):
        _permit(settings, run, item)
        r = _install(item, root, timeout)
        done.append(r)
        if run is not None:
            from beom_workbench.core import inbox
            inbox.log(settings, run["id"], "deps", kind=item["kind"], ok=r["ok"], files=item["files"])
    return {"installed": done, **status(root)}


JUNK_DIRS = {"__pycache__", ".pytest_cache"}
_SKIP = {".git", ".beom", "node_modules", ".venv", "venv"}


def junk(root: Path) -> set[str]:
    """테스트·실행이 남기는 찌꺼기 — __pycache__·.pytest_cache 폴더와 .coverage 기록 (상대 경로)."""
    root = Path(root)
    out: set[str] = set()
    if not root.is_dir():
        return out
    for dirpath, dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        for d in list(dirs):
            if d in JUNK_DIRS:
                out.add((rel / d).as_posix())
                dirs.remove(d)
            elif d in _SKIP:
                dirs.remove(d)
        out.update((rel / f).as_posix() for f in files if f == ".coverage" or f.startswith(".coverage."))
        if len(out) > 5000:
            break
    return out


def clean_junk(root: Path, before: set[str] | None) -> list[str]:
    """이번 라운드에 새로 생긴 찌꺼기만 지운다 — 라운드 시작에 있던 것(사용자 것일 수 있다)은 그대로. 에이전트가 지우는 명령을 돌리지 않게."""
    if before is None:
        return []
    root = Path(root)
    gone = []
    for rel in sorted(junk(root) - before):
        p = root / rel
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            gone.append(rel)
        except OSError:
            pass
    return gone


def status(root: Path) -> dict:
    root = Path(root)
    py = venv_python(root)
    out: dict = {}
    if (root / "node_modules").is_dir():
        out["node"] = {"node_modules": "node_modules"}
    if py.is_file():
        out["python"] = {"python": py.relative_to(root).as_posix(),
                         "note": "파이썬 테스트·실행은 이 python 으로 — 의존성과 pytest·coverage 가 들어 있다"}
    return out
