"""프로젝트 작업(테스트·타입 검사·빌드·린트) — 명령은 툴킷이 고르고, 격리·권한·환경·출력 정리도 툴킷이 맡는다.

에이전트가 'cd <폴더>; npm test 2>&1 | Select-Object -Last 15; npx tsc --noEmit' 같은 셸 문자열을 매번 짜면, 표기가 조금씩 달라
권한 판정이 빗나가고(같은 일인데 카드가 뜬다), 격리 안에서 안 도는 도구를 우회하는 스크립트를 만들고, 경로·PATH 를 잘못 잡는다.
그래서 정해진 작업은 이름(task)으로 부르게 한다:
- 프로젝트 종류는 파일로 알아본다 (package.json · pyproject/requirements · Cargo.toml · go.mod · pom.xml · build.gradle · *.csproj).
- 의존성은 먼저 준비한다 (deps.ensure — 바뀐 것이 없으면 바로 지나간다).
- 권한은 '빌드' 규칙 하나로, Node 도구가 격리 밖에서 돌아야 하면 '격리 없이 실행' 규칙으로 — 묻는 경우에도 프로젝트에서 한 번.
- 돌고 나면 결과 줄(통과·실패 수)과 끝 출력만 돌려주고, 테스트가 남긴 찌꺼기(__pycache__ 등)는 치운다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from beom_workbench.common.response import ToolError

TASKS = ("test", "typecheck", "build", "lint")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SAFE_PATH = re.compile(r"^(?!.*\.\.)(?![\\/])(?![A-Za-z]:)[\w.\-/\\]+$")
_SAFE_NAME = re.compile(r"^[\w .:\-\[\]]{1,120}$")
# package.json 의 기본 test 스크립트 (npm init) — 테스트가 없다는 뜻이다
_NPM_PLACEHOLDER = re.compile(r"no test specified", re.I)


_WALK_SKIP = {"node_modules", ".git", ".beom", "dist", "build", "target", ".venv", "venv", "__pycache__", "coverage"}


def _has_files(root: Path, suffixes: tuple[str, ...], limit: int = 4000) -> bool:
    """이 끝을 가진 파일이 있나 — node_modules 같은 큰 폴더는 건너뛰고, 처음 찾으면 멈춘다 (권한 판정마다 불린다)."""
    seen = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP]
        if any(f.endswith(suffixes) for f in files):
            return True
        seen += len(files)
        if seen > limit:
            return False
    return False


def _bin(root: Path, name: str) -> bool:
    b = root / "node_modules" / ".bin"
    return any((b / (name + ext)).is_file() for ext in ("", ".cmd", ".ps1"))


def _scripts(root: Path) -> dict:
    try:
        return json.loads((root / "package.json").read_text(encoding="utf-8-sig")).get("scripts") or {}
    except (OSError, ValueError):
        return {}


def kinds(root: Path) -> list[str]:
    """대상 폴더의 프로젝트 종류 (여럿일 수 있다 — 파이썬 서버 + Node 화면)."""
    root = Path(root)
    out = []
    if (root / "package.json").is_file():
        out.append("node")
    if any((root / n).is_file() for n in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini")) \
            or any(root.glob("test_*.py")) or any(root.glob("tests/test_*.py")) or any(root.glob("*/test_*.py")):
        out.append("python")
    if (root / "Cargo.toml").is_file():
        out.append("rust")
    if (root / "go.mod").is_file():
        out.append("go")
    if (root / "pom.xml").is_file():
        out.append("maven")
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        out.append("gradle")
    if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
        out.append("dotnet")
    return out


def _python(root: Path) -> str:
    from beom_workbench.core import deps
    py = deps.venv_python(root)
    return f'"{py}"' if py.is_file() else f'"{sys.executable}"'  # 툴킷의 파이썬에도 pytest·coverage 가 들어 있다


def _node_runner(root: Path) -> str | None:
    s = _scripts(root).get("test") or ""
    for name in ("vitest", "jest", "mocha"):
        if re.search(rf"\b{name}\b", s):
            return name
    if re.search(r"\bnode\b[^&|;]*--test\b", s):
        return "node"
    return None


def command(root: Path, kind: str, task: str, paths: list[str], name: str) -> str | None:
    """이 종류·작업의 명령 — 없으면 None. paths(테스트 파일)·name(테스트 이름 거르기)은 실행기에 맞는 꼴로 넣는다."""
    root = Path(root)
    p = " ".join(f'"{x}"' for x in paths)
    if kind == "node":
        sc = _scripts(root)
        if task == "test":
            runner = _node_runner(root)
            if sc.get("test") and not _NPM_PLACEHOLDER.search(sc["test"]):
                extra = []
                if paths:
                    extra.append(p)
                if name:
                    extra.append({"vitest": f'-t "{name}"', "jest": f'-t "{name}"', "mocha": f'--grep "{name}"',
                                  "node": f'--test-name-pattern="{name}"'}.get(runner or "", ""))
                extra = [x for x in extra if x]
                return "npm test" + (" -- " + " ".join(extra) if extra else "")
            if _bin(root, "vitest"):
                return "vitest run" + (f" {p}" if p else "") + (f' -t "{name}"' if name else "")
            if _bin(root, "jest"):
                return "jest" + (f" {p}" if p else "") + (f' -t "{name}"' if name else "")
            if paths or (root / "test").is_dir() or _has_files(root, (".test.js", ".test.mjs", ".test.cjs")):
                return "node --test" + (f' --test-name-pattern="{name}"' if name else "") + (f" {p}" if p else "")
            return None
        if task == "typecheck":
            if sc.get("typecheck"):
                return "npm run typecheck"
            if (root / "tsconfig.json").is_file() and _bin(root, "tsc"):
                return "tsc --noEmit"
            return None
        if task in ("build", "lint") and sc.get(task):
            return f"npm run {task}"
        if task == "lint" and _bin(root, "eslint"):
            return "eslint ."
        return None
    if kind == "python":
        py = _python(root)
        if task == "test":
            return f"{py} -m pytest -q -p no:cacheprovider" + (f" {p}" if p else "") + (f' -k "{name}"' if name else "")
        from beom_workbench.core import deps
        bin_dir = deps.venv_dir(root) / ("Scripts" if os.name == "nt" else "bin")
        if task == "typecheck" and (bin_dir / ("mypy.exe" if os.name == "nt" else "mypy")).is_file():
            return f"{py} -m mypy ."
        if task == "lint" and (bin_dir / ("ruff.exe" if os.name == "nt" else "ruff")).is_file():
            return f"{py} -m ruff check ."
        return None
    if kind == "rust":
        return {"test": "cargo test" + (f" {name}" if name else ""), "typecheck": "cargo check", "build": "cargo build",
                "lint": "cargo clippy"}.get(task)
    if kind == "go":
        return {"test": "go test ./..." + (f' -run "{name}"' if name else ""), "typecheck": "go vet ./...",
                "build": "go build ./...", "lint": "go vet ./..."}.get(task)
    if kind == "maven":
        return {"test": "mvn -q test" + (f' -Dtest="{name}"' if name else ""), "build": "mvn -q package -DskipTests",
                "typecheck": "mvn -q compile"}.get(task)
    if kind == "gradle":
        g = "gradlew" if (root / "gradlew").is_file() or (root / "gradlew.bat").is_file() else "gradle"
        return {"test": f"{g} test" + (f' --tests "{name}"' if name else ""), "build": f"{g} build -x test",
                "typecheck": f"{g} compileJava"}.get(task)
    if kind == "dotnet":
        return {"test": "dotnet test" + (f' --filter "{name}"' if name else ""), "build": "dotnet build",
                "typecheck": "dotnet build"}.get(task)
    return None


def available(root: Path, task: str) -> bool:
    return any(command(root, k, task, [], "") for k in kinds(root))


def _summary(kind: str, text: str) -> str | None:
    """결과 한 줄 — 실행기마다 끝에 찍는 요약."""
    pats = [r"=+ (.*(?:passed|failed|error|no tests ran).*?) =+\s*$",          # pytest
            r"^\d+ (?:passed|failed|errors?|skipped|deselected)\b.* in [\d.]+s",  # pytest -q
            r"^FAILED \S+",                                                     # pytest 실패한 테스트 이름
            r"^\s*Tests\s+(.+)$",                                              # vitest
            r"^Tests:\s+(.+)$",                                                # jest
            r"^test result: (.+)$",                                            # cargo
            r"^# (?:pass|fail) \d+.*$",                                        # node --test
            r"^(ok|FAIL)\s+\S+.*$",                                            # go
            r"^(?:Passed|Failed)!.*$",                                         # dotnet
            r"Found \d+ errors?.*$", r"^error TS\d+.*$"]                       # tsc
    hits = []
    for line in text.splitlines():
        for rx in pats:
            m = re.search(rx, line.strip())
            if m:
                hits.append(line.strip())
                break
    return " / ".join(dict.fromkeys(hits[-4:])) or None


def needs_outside(cmd: str) -> bool:
    from beom_workbench.core import inbox
    return inbox.needs_unsandboxed(cmd)


def permit_outside(settings, run: dict, what: str, key: str = "outside:node") -> bool:
    """Node 도구를 격리 밖에서 돌려도 되나 — '빌드'·'격리 없이 실행' 규칙대로. 묻는 경우엔 프로젝트에서 한 번 (테스트·타입 검사·
    빌드·툴킷의 커버리지 측정이 같은 승인을 쓴다 — 모두 프로젝트의 Node 도구를 그 폴더에서 돌리는 일이다)."""
    from beom_workbench.core import inbox
    kd, ku = inbox.decide(run, "build"), inbox.decide(run, "unsandboxed")
    if "refuse" in (kd, ku):
        return False
    if kd == "allow" and ku == "allow" or key in (run.get("approved_cmds") or []):
        return True
    ans = inbox.ask(settings, run["id"], "permission", {
        "summary": f"툴킷이 프로젝트의 Node 도구를 격리 밖에서 돌립니다 (Node 빌드 도구는 Windows 격리 안에서 돌지 않음 — 이 프로젝트에서 "
                   f"한 번만 묻습니다): {what}",
        "tool": "mcp:wb_task", "input": {"command": what}, "permission_kind": "unsandboxed"})
    if not ans.get("allow"):
        return False
    if ans.get("always"):
        inbox.update_run(settings, run["id"], lambda r: r["policy"].__setitem__("unsandboxed", "allow"))
    inbox.update_run(settings, run["id"], lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + [key]))))
    return True


def run_task(settings, run: dict, task: str, *, path: str = "", paths: list[str] | None = None, name: str = "",
             timeout: int = 600) -> dict:
    from beom_workbench.core import deps, inbox, port, sandbox
    if task not in TASKS:
        raise ToolError("invalid_request", f"task 는 {', '.join(TASKS)} 중 하나")
    base = Path(run["target_root"])
    root = (base / path).resolve() if path else base
    if path and (not _SAFE_PATH.match(path) or base.resolve() not in [root, *root.parents] or not root.is_dir()):
        raise ToolError("invalid_request", f"path 는 대상 폴더 안의 하위 폴더 (상대 경로): {path}")
    paths = [x for x in (paths or []) if x]
    bad = [x for x in paths if not _SAFE_PATH.match(x)]
    if bad or (name and not _SAFE_NAME.match(name)):
        raise ToolError("invalid_request", "paths 는 대상 폴더 안의 상대 경로, name 은 글자·숫자·공백·._-:[] 만",
                        recovery="거르지 않고 전체를 돌리거나 이름을 간단히")
    found = kinds(root)
    if not any(command(root, k, task, paths, name) for k in found):
        why = (f"이 폴더의 프로젝트({', '.join(found)})에 '{task}' 작업이 없음" if found else "프로젝트 파일(package.json·pyproject.toml·"
               "Cargo.toml 등)을 찾지 못함")
        hint = {"test": "테스트 파일을 만들거나 package.json 에 test 스크립트를 적는다",
                "typecheck": "TypeScript 면 tsconfig.json 과 typescript 의존성, 또는 package.json 에 typecheck 스크립트",
                "build": "package.json 에 build 스크립트를 적는다", "lint": "package.json 에 lint 스크립트를 적는다"}[task]
        raise ToolError("not_found", why, recovery=hint)
    if inbox.decide(run, "build") == "refuse":
        raise ToolError("denied", "규칙상 빌드·테스트 실행이 허용되지 않음", recovery="사용자에게 권한 설정을 바꿔 달라고 한다")
    deps.ensure(settings, run, base)  # 의존성 먼저 — 바뀐 것이 없으면 바로 지나간다
    # 명령은 준비가 끝난 뒤에 고른다 (방금 만든 .beom/venv 의 파이썬·node_modules/.bin 의 도구를 쓰게)
    plan = [(k, c) for k in found if (c := command(root, k, task, paths, name))]
    if inbox.decide(run, "build") == "ask" and f"task:{task}" not in (run.get("approved_cmds") or []):
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": f"빌드·테스트 실행 (툴킷이 고른 명령): {' ; '.join(c for _, c in plan)}",
            "tool": "mcp:wb_task", "input": {"task": task}, "permission_kind": "build"})
        if not ans.get("allow"):
            raise ToolError("denied", f"사용자가 '{task}' 실행을 거절함", recovery="wb_person_ask 로 이유와 함께 묻는다")
        inbox.update_run(settings, run["id"],
                         lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + [f"task:{task}"]))))
        run = inbox.load_run(settings, run["id"])
    side = port._Side("target", root, port._harness_dir(settings))
    env = side.env()
    before = deps.junk(base)
    results = []
    for kind, cmd in plan:
        outside = False
        if sandbox.wraps_powershell(run) and needs_outside(cmd):
            outside = permit_outside(settings, run, cmd)
            run = inbox.load_run(settings, run["id"])
            if not outside:
                results.append({"kind": kind, "command": cmd, "ok": False, "skipped": True,
                                "error": "격리 밖 실행이 허용되지 않아 돌리지 않음 (Node 도구는 격리 안에서 돌지 않는다)"})
                continue
        if sandbox.wraps_powershell(run) and not outside:
            argv, shell = sandbox.wrap_argv(["cmd", "/d", "/c", cmd], run), False
        else:
            argv, shell = cmd, True  # 문자열 그대로 (목록으로 넘기면 따옴표가 \" 로 바뀌어 cmd 가 경로를 잘못 읽는다)
        t = max(10, min(int(timeout or 600), 1800))
        try:
            cp = port.run_tree(argv, shell=shell, cwd=str(root), timeout=t, env=env)
            code, out = cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", "replace")
        except subprocess.TimeoutExpired as e:
            code, out = 124, ((e.output or b"") + (e.stderr or b"")).decode("utf-8", "replace") + f"\n({t}초 안에 끝나지 않아 멈춤)"
        text = _ANSI.sub("", out).replace("\r\n", "\n")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        results.append({"kind": kind, "command": cmd, "ok": code == 0, "exit_code": code, "summary": _summary(kind, text),
                        "output_tail": "\n".join(lines[-60:])[-5000:]})
        inbox.log(settings, run["id"], "task", task=task, project=kind, command=cmd, exit_code=code, outside=outside)
    cleaned = deps.clean_junk(base, before)
    return {"ok": all(r["ok"] for r in results), "task": task, "results": results,
            **({"cleaned": cleaned} if cleaned else {})}


# ---------------------------------------------------------------- 파일 지우기

_TOOLKIT_SCRATCH = (".beom/tmp/", ".beom/helpers/")


def _start_checkpoint(settings, run: dict) -> str | None:
    from beom_workbench.core import inbox
    evs, _ = inbox.events(settings, run["id"])
    return next((e.get("id") for e in evs if e.get("kind") == "checkpoint" and e.get("label") == "시작 전"), None)


def _existed(settings, run: dict, rel: str) -> bool | None:
    """실행을 시작할 때 있던 경로인가 — 시작 전 체크포인트 기준. 모르면 None."""
    from beom_workbench.core import checkpoints, inbox
    cid = _start_checkpoint(settings, run)
    if not cid:
        return None
    shadow = inbox.run_dir(settings, run["id"]) / "shadow.git"
    try:
        out = checkpoints._git(shadow, Path(run["target_root"]), "ls-tree", "--name-only", cid, "--", rel, check=False)
    except (OSError, ToolError):
        return None
    return bool(out.stdout.strip())


def _snapshot_skips(rel: str) -> bool:
    from beom_workbench.core import checkpoints
    parts = rel.split("/")
    return any(p + "/" in checkpoints.EXCLUDE for p in parts[:-1]) or any(rel + "/" == e for e in checkpoints.EXCLUDE)


def remove(settings, run: dict, paths: list[str]) -> dict:
    """대상 폴더 안의 파일·폴더를 지운다. 이번 실행에서 만든 것과 툴킷 작업 폴더(.beom/tmp·.beom/helpers)는 편집과 같다
    (체크포인트·작업 브랜치에 남는 것은 사용자 것뿐이다). 실행 전부터 있던 것은 셸로 지울 때와 같은 '그 밖의 명령' 규칙대로 —
    편집 자동 허용이면 카드로 묻고, 자동이면 바로 지운다.
    지운 것은 체크포인트로 되돌릴 수 있다."""
    from beom_workbench.core import inbox
    root = Path(run["target_root"]).resolve()
    if not paths:
        raise ToolError("invalid_request", "paths 가 비어 있음")
    own, theirs, missing, refused = [], [], [], []
    for raw in paths:
        p = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if root not in p.parents:
            refused.append(f"{raw} (대상 폴더 밖)")
            continue
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] == ".git" or (rel.startswith(".beom") and not rel.startswith(_TOOLKIT_SCRATCH)):
            refused.append(f"{rel} (툴킷·git 이 관리하는 파일)")
            continue
        if not p.exists():
            missing.append(rel)
            continue
        if rel.startswith(_TOOLKIT_SCRATCH):
            own.append((rel, p))
            continue
        existed = _existed(settings, run, rel)
        if existed is False and not _snapshot_skips(rel):
            own.append((rel, p))
        else:
            theirs.append((rel, p))
    if theirs:
        d = inbox.decide(run, "commands")
        if d == "refuse":
            refused += [f"{r} (실행 전부터 있던 파일 — 규칙상 거절)" for r, _ in theirs]
            theirs = []
        elif d == "ask":
            ans = inbox.ask(settings, run["id"], "permission", {
                "summary": "파일 삭제 (실행 전부터 있던 것 — 체크포인트로 되돌릴 수 있음): " + ", ".join(r for r, _ in theirs)[:400],
                "tool": "mcp:wb_remove", "input": {"paths": [r for r, _ in theirs]}, "permission_kind": "commands"})
            if not ans.get("allow"):
                refused += [f"{r} (사용자가 거절)" for r, _ in theirs]
                theirs = []
    removed = []
    for rel, p in own + theirs:
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            removed.append(rel)
        except OSError as e:
            refused.append(f"{rel} ({e.strerror or e})")
    inbox.log(settings, run["id"], "remove", removed=removed[:50], asked=[r for r, _ in theirs][:50], refused=refused[:20])
    return {"ok": not refused, "removed": removed, **({"missing": missing} if missing else {}),
            **({"refused": refused} if refused else {})}
