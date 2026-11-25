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

TASKS = ("test", "typecheck", "build", "lint", "format")
# 프로젝트가 정해 둔 포매터 설정 — 있을 때만 형식 검사를 돌린다 (설정 없는 프로젝트에 툴킷이 형식을 강요하지 않는다)
_PRETTIER = (".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.mjs", ".prettierrc.yaml", ".prettierrc.yml",
             ".prettierrc.toml", "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SAFE_PATH = re.compile(r"^(?!.*\.\.)(?![\\/])(?![A-Za-z]:)[\w.\-/\\]+$")
_SAFE_NAME = re.compile(r"^[\w .:\-\[\]]{1,120}$")
# package.json 의 기본 test 스크립트 (npm init) — 테스트가 없다는 뜻이다


def _bin(root: Path, name: str) -> bool:
    b = root / "node_modules" / ".bin"
    return any((b / (name + ext)).is_file() for ext in ("", ".cmd", ".ps1"))


def _scripts(root: Path) -> dict:
    try:
        return json.loads((root / "package.json").read_text(encoding="utf-8-sig")).get("scripts") or {}
    except (OSError, ValueError):
        return {}


def kinds(root: Path) -> list[str]:
    """이 폴더가 속한 프로젝트의 종류 (여럿일 수 있다 — 파이썬 서버 + Node 화면). 자리는 layout.projects."""
    from beom_workbench.core import layout
    return [k for k, _ in layout.projects(root)]


def _python(root: Path) -> str:
    from beom_workbench.core import deps
    py = deps.venv_python(root)
    return f'"{py}"' if py.is_file() else f'"{sys.executable}"'  # 툴킷의 파이썬에도 pytest·coverage 가 들어 있다


def _node_runner(root: Path) -> str | None:
    from beom_workbench.core import layout
    return layout.node_runner(root)


def command(root: Path, kind: str, task: str, paths: list[str], name: str) -> str | None:
    """이 종류·작업의 명령 — 없으면 None. root 는 그 프로젝트의 자리 (layout.projects). paths(테스트 파일)·name(테스트 이름
    거르기)은 실행기에 맞는 꼴로 넣는다. 테스트는 그 실행기가 모을 테스트가 있을 때만 — 없는데 돌리면 pytest 는 '테스트 없음'(종료
    코드 5)으로 실패해, 테스트를 요청받지 않은 프로젝트도 에이전트가 테스트를 만들어야 통과했다."""
    from beom_workbench.core import layout
    root = Path(root)
    if task in ("lint", "format") and paths:  # 그 도구가 읽는 파일만 (ruff 에 .js 를, eslint 에 .md 를 주면 오류가 난다)
        keep = {("python", "lint"): (".py",), ("python", "format"): (".py",),
                ("node", "lint"): (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")}.get((kind, task))
        paths = [x for x in paths if not keep or x.lower().endswith(keep)]
        if not paths:
            return None
    p = " ".join(f'"{x}"' for x in paths)
    if kind == "node":
        sc = _scripts(root)
        if task == "test":
            runner = _node_runner(root)
            if paths and runner in ("node", "mocha") and (layout._script_paths(root) or runner == "mocha"):
                # 스크립트가 테스트 경로를 정해 두면(node --test test/ · mocha 설정의 spec) 'npm test -- 파일' 은 원래 테스트까지
                # 함께 돈다 — 고른 파일만 실행기에 바로 준다
                return ("node --test" + (f' --test-name-pattern="{name}"' if name else "") + f" {p}" if runner == "node"
                        else f"mocha {p}" + (f' --grep "{name}"' if name else ""))
            if layout.test_script(root):
                extra = []
                if paths:
                    extra.append(p)
                if name:
                    extra.append({"vitest": f'-t "{name}"', "jest": f'-t "{name}"', "mocha": f'--grep "{name}"',
                                  "node": f'--test-name-pattern="{name}"'}.get(runner or "", ""))
                extra = [x for x in extra if x]
                return _pm_run(root, "test", extra)
            if _bin(root, "vitest"):
                return "vitest run" + (f" {p}" if p else "") + (f' -t "{name}"' if name else "")
            if _bin(root, "jest"):
                return "jest" + (f" {p}" if p else "") + (f' -t "{name}"' if name else "")
            if paths or layout.node_test_files(root):  # node 가 인자 없이 모을 테스트 (node 의 기본 규칙)
                return "node --test" + (f' --test-name-pattern="{name}"' if name else "") + (f" {p}" if p else "")
            return None
        if task == "typecheck":
            if sc.get("typecheck"):
                return _pm_run(root, "typecheck", [])
            if (root / "tsconfig.json").is_file() and _bin(root, "tsc"):
                return "tsc --noEmit"
            return None
        if task == "lint" and paths and _bin(root, "eslint"):
            return "eslint " + p  # 바뀐 파일만 — 스크립트는 파일을 받는지 알 수 없다
        if task in ("build", "lint") and sc.get(task):
            return _pm_run(root, task, [])
        if task == "lint" and _bin(root, "eslint"):
            return "eslint ."
        if task == "format":
            if sc.get("format:check"):
                return _pm_run(root, "format:check", [])
            pj = {}
            try:
                pj = json.loads((root / "package.json").read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
            if _bin(root, "prettier") and (any((root / f).is_file() for f in _PRETTIER) or "prettier" in pj):
                return "prettier --check " + (p or ".")
        return None
    if kind == "python":
        py = _python(root)
        if task == "test":
            if not paths and not layout.pytest_files(root):
                return None  # pytest 가 모을 테스트가 없다
            return f"{py} -m pytest -q -p no:cacheprovider" + (f" {p}" if p else "") + (f' -k "{name}"' if name else "")
        from beom_workbench.core import deps
        bin_dir = deps.venv_dir(root) / ("Scripts" if os.name == "nt" else "bin")
        if task == "typecheck" and (bin_dir / ("mypy.exe" if os.name == "nt" else "mypy")).is_file():
            return f"{py} -m mypy ."
        if task == "lint" and (bin_dir / ("ruff.exe" if os.name == "nt" else "ruff")).is_file():
            return f"{py} -m ruff check " + (p or ".")
        if task == "format":
            try:
                cfg = (root / "pyproject.toml").read_text(encoding="utf-8")
            except OSError:
                cfg = ""
            has = lambda tool: (bin_dir / (tool + (".exe" if os.name == "nt" else ""))).is_file()  # noqa: E731
            if has("ruff") and ("[tool.ruff.format]" in cfg or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file()):
                return f"{py} -m ruff format --check " + (p or ".")
            if has("black") and "[tool.black]" in cfg:
                return f"{py} -m black --check " + (p or ".")
        return None
    if kind == "rust":
        return {"test": "cargo test" + (f" {name}" if name else ""), "typecheck": "cargo check", "build": "cargo build",
                "lint": "cargo clippy", "format": "cargo fmt --check"}.get(task)
    if kind == "go":
        return {"test": "go test ./..." + (f' -run "{name}"' if name else ""), "typecheck": "go vet ./...",
                "build": "go build ./...", "lint": "go vet ./..."}.get(task)
    if kind == "maven":
        wrap = (root / "mvnw.cmd").is_file() if os.name == "nt" else (root / "mvnw").is_file()
        m = ("mvnw.cmd" if os.name == "nt" else "./mvnw") if wrap else "mvn"
        one = f' -Dtest="{name}" -Dsurefire.failIfNoSpecifiedTests=false' if name else ""  # 여러 모듈이면 그 테스트가 없는 모듈도 있다
        return {"test": f"{m} -q test{one}", "build": f"{m} -q package -DskipTests", "typecheck": f"{m} -q compile"}.get(task)
    if kind == "gradle":
        wrap = (root / "gradlew.bat").is_file() if os.name == "nt" else (root / "gradlew").is_file()
        g = ("gradlew.bat" if os.name == "nt" else "./gradlew") if wrap else "gradle"
        return {"test": f"{g} test" + (f' --tests "{name}"' if name else ""), "build": f"{g} build -x test",
                "typecheck": f"{g} compileJava"}.get(task)
    if kind == "dotnet":
        return _dotnet(root, task, name)
    return None


def _dotnet(root: Path, task: str, name: str) -> str | None:
    """dotnet test·build 를 어디에 — 맨 위에 .sln·.csproj 가 있으면 거기서, 없으면(하위 폴더에만 프로젝트) 테스트 프로젝트를 골라서.
    맨 위에서 그냥 'dotnet test' 를 돌리면 '프로젝트 파일이 없다'(MSB1003)로 끝났다."""
    root = Path(root)
    f = f' --filter "{name}"' if name else ""
    if any(root.glob("*.sln")) or any(root.glob("*.slnx")) or any(root.glob("*.csproj")) or any(root.glob("*.fsproj")):
        return {"test": f"dotnet test{f}", "build": "dotnet build", "typecheck": "dotnet build"}.get(task)
    projs = sorted(p for d in ("*", "*/*", "*/*/*") for p in root.glob(d + ".csproj") if "bin" not in p.parts and "obj" not in p.parts)
    tests = [p for p in projs if re.search(r"Microsoft\.NET\.Test\.Sdk|xunit|nunit|MSTest", _read_text(p), re.I)]
    rel = lambda p: p.relative_to(root).as_posix()  # noqa: E731
    if task == "test":
        return " && ".join(f'dotnet test "{rel(p)}"{f}' for p in tests) or None
    return " && ".join(f'dotnet build "{rel(p)}"' for p in (tests or projs)) or None


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _pm_run(root: Path, script: str, extra: list[str]) -> str:
    """프로젝트의 패키지 관리자로 스크립트 — npm test -- a · pnpm test a · yarn test a · bun run test a."""
    from beom_workbench.core import deps
    pm = deps.node_manager(Path(root))
    head = " ".join(deps.pm_command(Path(root)))
    if pm == "npm":
        return (f"{head} test" if script == "test" else f"{head} run {script}") + (" -- " + " ".join(extra) if extra else "")
    run = "run " if pm == "bun" or script not in ("test",) else ""
    return f"{head} {run}{script}" + (" " + " ".join(extra) if extra else "")


def available(root: Path, task: str) -> bool:
    from beom_workbench.core import layout
    return any(command(r, k, task, [], "") for k, r in layout.projects(root))


def _plan(root: Path, task: str, paths: list[str], name: str) -> list[tuple[str, str, str, Path]]:
    """(종류, 작업, 명령, 돌릴 자리) — 이 폴더가 속한 프로젝트마다 그 자리에서. paths 는 이 폴더 기준이라 자리 기준으로 바꾼다."""
    from beom_workbench.core import layout
    out = []
    for k, r in layout.projects(root):
        rel = [os.path.relpath(Path(root) / x, r).replace("\\", "/") for x in paths]
        if c := command(r, k, task, rel, name):
            out.append((k, task, c, r))
    return out


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


def needs_outside(cmd: str, env: dict | None = None) -> bool:
    """Windows 격리 안에서 돌지 않는 명령인가 — 판단은 execenv.outside_reason 하나 (툴킷의 모든 실행 자리가 같은 기준)."""
    from beom_workbench.core import execenv
    return execenv.outside_reason(cmd, env) is not None


OUTSIDE_WHY = {"node": "Node 도구는 자식 프로세스를 띄우지 못함", "jvm": "Maven·Gradle 은 사용자 폴더의 캐시·내려받기가 필요함",
               "dotnet": ".NET 은 사용자 폴더의 SDK 상태·NuGet 이 필요함", "go": "Go 는 사용자 폴더의 모듈 캐시가 필요함",
               "cargo": "Rust 는 rustup·Cargo 의 사용자 폴더가 필요함",
               "python": "Python 3.12.4 이상은 임시 폴더를 소유자 전용으로 만들어 격리 안에서 쓰지 못함",
               "denied": "격리 안에서 '접근 거부'로 실패함",
               "escalation": "에이전트의 격리 안에서 막혀 격리 밖 실행을 요청함"}


def permit_outside(settings, run: dict, what: str, key: str = "outside", why: str | None = None) -> bool:
    """프로젝트 명령을 격리 밖에서 돌려도 되나 — '빌드'·'격리 없이 실행' 규칙대로. 묻는 경우엔 프로젝트에서 한 번 (테스트·타입 검사·
    빌드·검증 케이스·커버리지 측정이 같은 승인을 쓴다 — 모두 프로젝트의 도구를 그 폴더에서 돌리는 일이다)."""
    from beom_workbench.core import inbox
    kd, ku = inbox.decide(run, "build"), inbox.decide(run, "unsandboxed")
    if "refuse" in (kd, ku):
        return False
    approved = run.get("approved_cmds") or []
    if kd == "allow" and ku == "allow" or key in approved or "outside" in approved or "outside:node" in approved:
        return True
    reason = OUTSIDE_WHY.get(why or "", "Windows 격리 안에서 돌지 않음")
    ans = inbox.ask(settings, run["id"], "permission", {
        "summary": f"툴킷이 프로젝트 명령을 격리 밖에서 돌립니다 ({reason} — 이 프로젝트에서 한 번만 묻습니다): {what}",
        "tool": "mcp:wb_task", "input": {"command": what}, "permission_kind": "unsandboxed"})
    if not ans.get("allow"):
        return False
    if ans.get("always"):
        inbox.allow_always(settings, run["id"], "unsandboxed")
    inbox.update_run(settings, run["id"], lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + [key]))))
    return True


def permit_build(settings, run: dict | None, keys: list[str], summary: str, tool: str = "mcp:wb_port_verify") -> bool:
    """툴킷이 프로젝트 테스트·타입 검사를 돌려도 되나 — '빌드' 규칙대로. 묻는 규칙이면 작업마다 한 번 (wb_task·검증·실행 범위 측정이
    같은 승인을 쓴다). 묻는 규칙을 '허용 아님'으로 보고 조용히 건너뛰면 측정도 카드도 없는 막다른 길이 된다."""
    from beom_workbench.core import inbox
    if run is None:
        return True
    d = inbox.decide(run, "build")
    if d == "allow":
        return True
    if d == "refuse":
        return False
    if all(k in (run.get("approved_cmds") or []) for k in keys):
        return True
    ans = inbox.ask(settings, run["id"], "permission", {"summary": summary, "tool": tool, "input": {"keys": keys},
                                                        "permission_kind": "build"})
    if not ans.get("allow"):
        return False
    inbox.update_run(settings, run["id"], lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + keys))))
    return True


def run_task(settings, run: dict, task: str, *, path: str = "", paths: list[str] | None = None, name: str = "",
             timeout: int = 600) -> dict:
    from beom_workbench.core import deps, inbox
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
    if task in ("lint", "format") and not paths and run:
        paths = changed_code(settings, run, root)  # 이번 실행에서 바꾼 파일만 — 원래 있던 문제로 막히지 않게
        if paths and not _plan(root, task, paths, name) and available(root, task):
            # 바꾼 파일 중 그 도구가 읽는 파일이 없다 — 검사할 것이 없으니 통과로 남긴다
            from beom_workbench.core import inbox
            inbox.log(settings, run["id"], "task", task=task, project="", command="", exit_code=0, outside=False)
            return {"ok": True, "results": [], "note": f"이번에 바꾼 파일 중 '{task}' 도구가 읽는 파일이 없어 돌리지 않음"}
    found = kinds(root)
    if not _plan(root, task, paths, name):
        why = (f"이 폴더의 프로젝트({', '.join(found)})에 '{task}' 작업이 없음" if found else "프로젝트 파일(package.json·pyproject.toml·"
               "Cargo.toml 등)을 찾지 못함")
        hint = {"test": "테스트 파일을 만들거나 package.json 에 test 스크립트를 적는다",
                "typecheck": "TypeScript 면 tsconfig.json 과 typescript 의존성, 또는 package.json 에 typecheck 스크립트",
                "build": "package.json 에 build 스크립트를 적는다", "lint": "package.json 에 lint 스크립트를 적는다",
                "format": "프로젝트에 포매터 설정(prettier·ruff format·black·rustfmt)이 없음 — 설정이 없으면 형식 검사는 하지 않는다"}[task]
        raise ToolError("not_found", why, recovery=hint)
    if inbox.decide(run, "build") == "refuse":
        raise ToolError("denied", "규칙상 빌드·테스트 실행이 허용되지 않음", recovery="사용자에게 권한 설정을 바꿔 달라고 한다")
    # 언어 도구(프로젝트가 정한 버전)를 먼저 — 의존성을 그 버전으로 설치해야 한다 (Node 24 로 깐 뒤 Node 20 을 받으면 어긋난다)
    ensure_tools(settings, run, [(k, task, command(base, k, task, [], "") or "", base) for k in kinds(base) if k in TOOL_OF])
    deps.ensure(settings, run, base)  # 그다음 의존성 — 바뀐 것이 없으면 바로 지나간다
    # 명령은 준비가 끝난 뒤에 고른다 (방금 만든 .beom/venv 의 파이썬·node_modules/.bin 의 도구를 쓰게)
    plan = _plan(root, task, paths, name)
    if inbox.decide(run, "build") == "ask" and f"task:{task}" not in (run.get("approved_cmds") or []):
        ans = inbox.ask(settings, run["id"], "permission", {
            "summary": f"빌드·테스트 실행 (툴킷이 고른 명령): {' ; '.join(c for _, _, c, _ in plan)}",
            "tool": "mcp:wb_task", "input": {"task": task}, "permission_kind": "build"})
        if not ans.get("allow"):
            raise ToolError("denied", f"사용자가 '{task}' 실행을 거절함", recovery="wb_person_ask 로 이유와 함께 묻는다")
        inbox.update_run(settings, run["id"],
                         lambda r: r.update(approved_cmds=list(dict.fromkeys((r.get("approved_cmds") or []) + [f"task:{task}"]))))
        run = inbox.load_run(settings, run["id"])
    plan, missing = ensure_tools(settings, run, plan)
    if not plan:
        names = ", ".join(sorted({m["missing_tool"] for m in missing}))
        return {"ok": False, "task": task, "results": missing, "missing_tools": sorted({m["missing_tool"] for m in missing}),
                "note": f"{names} 이(가) 이 PC 에 없어 돌리지 않았다 — 사용자가 설치하지 않기로 했다. 같은 명령을 셸로 돌리거나 설치를 "
                        "다시 묻지 말고, 빌드 없이 할 수 있는 만큼 한 뒤 남은 확인(빌드·테스트)을 인수인계의 risks 에 적는다"}
    before = deps.junk(base)
    files_before = _tracked_files(base) if task == "test" else None
    results = missing + _execute(settings, run, root, plan, timeout)
    cleaned = deps.clean_junk(base, before)
    left = _drop_test_leftovers(base, files_before) if files_before is not None else []
    if left and run is not None:
        inbox.log(settings, run["id"], "note", tone="warn", text="테스트가 대상 폴더에 남긴 파일을 지웠습니다: " + ", ".join(left[:5]))
    return {"ok": all(r["ok"] for r in results), "task": task, "results": results,
            **({"cleaned": cleaned} if cleaned else {}),
            **({"test_leftovers": left, "note": "테스트가 대상 폴더에 파일을 남겨 .beom/test-leftovers 로 옮겼다 (" + ", ".join(left[:5]) + ") — 테스트는 임시 폴더"
                "(pytest 의 tmp_path·os.tmpdir() 등)를 써야 한다. 앱이 실행 중 만드는 데이터 파일이면 테스트에서 그 경로를 임시 폴더로 "
                "바꾸고, 사용자에게 넘길 폴더에 시험 데이터가 남지 않게 고친다"} if left else {})}


# 테스트가 만들어도 되는 것 — 빌드 결과·캐시·스냅숏·잠금 파일 (지우지 않는다)
_TEST_OUTPUT = re.compile(r"(^|/)(target|build|dist|bin|obj|out|coverage|htmlcov|\.next|\.nyc_output|__snapshots__|"
                          r"[^/]+\.egg-info|node_modules|\.venv|venv|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|"
                          r"[^/]+-snapshots|testdata|test-results|playwright-report|reports|\.tox|\.gradle|\.hypothesis|\.svelte-kit|"
                          r"\.angular|\.nuxt|\.turbo|\.cache|migrations)(/|$)|"
                          r"\.(snap|pyc|tsbuildinfo|lcov|golden)$|(^|/)(\.coverage[^/]*|junit[^/]*\.xml|next-env\.d\.ts)$")


def _tracked_files(base: Path, limit: int = 20000) -> set[str] | None:
    out: set[str] = set()
    for dirpath, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", ".beom", "node_modules", ".venv", "venv", "target", "__pycache__")]
        rel = Path(dirpath).relative_to(base).as_posix()
        out.update((f"{rel}/{n}" if rel != "." else n) for n in names)
        if len(out) > limit:
            return None
    return out


def _drop_test_leftovers(base: Path, before: set[str] | None) -> list[str]:
    """테스트를 돌리는 동안 대상 폴더에 새로 생긴 파일 (빌드 결과·캐시·스냅숏 빼고) — 시험 데이터가 결과물에 섞이지 않게 지운다."""
    if before is None:
        return []
    after = _tracked_files(base) or set()
    gone = []
    from beom_workbench.core.scaffold import LOCKFILES
    new = [rel for rel in sorted(after - before)
           if not _TEST_OUTPUT.search(rel) and rel.split("/")[-1] not in LOCKFILES
           and not any(part.startswith(".") for part in rel.split("/")[:-1])]  # 숨은 폴더는 도구들의 자리
    new = [rel for rel in new if not _git_ignored(base, rel)]  # 프로젝트가 무시하기로 한 것은 결과물에 섞이지 않는다
    stash = base / ".beom" / "test-leftovers"
    for rel in new:
        try:  # 지우지 않고 옮긴다 — 정상 산출물을 잘못 골랐어도 되찾을 수 있게
            dst = stash / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            (base / rel).replace(dst)
            gone.append(rel)
        except OSError:
            pass
    return gone


def _git_ignored(base: Path, rel: str) -> bool:
    try:
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=base, capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


TOOL_OF = {"node": "node", "rust": "cargo", "go": "go", "maven": "java", "gradle": "java", "dotnet": "dotnet", "python": "python"}
INSTALL_NOW = "툴킷이 지금 설치 (권장)"
INSTALLED = "직접 설치했습니다 — 계속"
DECLINED = "설치하지 않음 — 빌드 없이 마무리 (완료로 보지 않음)"


def ensure_tools(settings, run: dict | None, plan: list[tuple]) -> tuple[list[tuple], list[dict]]:
    """돌릴 명령의 언어 도구(go·cargo·node·JDK·dotnet)가 이 PC 에 있는지 먼저 본다. 없으면 '명령 실패(종료 코드 1)'로 돌려주지 않고
    툴킷이 사용자에게 설치를 묻는다 — 설치했다고 하면 정말 있는지 다시 보고, 설치하지 않기로 하면 그 실행에서 다시 묻지 않는다.
    (모델이 설치를 묻고 답을 받은 뒤 도구가 없는 채로 라운드를 되풀이하던 것을 막는다.) 돌릴 것과 건너뛴 결과를 돌려준다."""
    from beom_workbench.core import inbox, prereqs
    from beom_workbench.core.agents import refresh_path

    def tools_of(item: tuple) -> set[str]:
        """그 명령에 필요한 도구 — Maven·Gradle 은 래퍼(mvnw·gradlew)가 없으면 mvn·gradle 자체가 있어야 한다 (JDK 만으로는 안 된다)."""
        k, cmd = item[0], str(item[2])
        if k.startswith("_cmd_"):  # 검증 명령의 첫 낱말로 정한 도구
            return {k[5:]}
        if k == "maven":  # mvnw · ./mvnw · .\mvnw.cmd
            return {"java"} if re.search(r"(?:^|[\s\\/])mvnw(?:\.cmd)?\b", cmd) else {"java", "mvn"}
        if k == "gradle":
            return {"java"} if "gradlew" in cmd else {"java", "gradle"}
        if k == "rust" and sys.platform == "win32":
            return {"cargo", "msvc"}  # Windows 의 Rust 는 MSVC 링커(link.exe)가 있어야 링크한다
        return {TOOL_OF[k]} if k in TOOL_OF else set()
    from beom_workbench.core import toolchains
    need = {t for item in plan for t in tools_of(item)}
    if not need:
        return plan, []
    refresh_path()
    roots = {str(item[3]) for item in plan if len(item) > 3} or {str(Path(run["target_root"]) if run else ".")}

    def ready(tool: str) -> bool:
        """Java·Node·Go·.NET·Python 은 프로젝트가 정한 버전까지 (pom.xml 의 release·.nvmrc·go.mod·global.json …)."""
        if tool in toolchains.FETCH or tool == "python":
            return all(toolchains.ok(tool, Path(r)) for r in roots)
        return prereqs._has(tool)

    def spec_of(tool: str) -> tuple[str | None, str | None]:
        for r in roots:
            req = toolchains.required(Path(r)).get(tool)
            if req:
                return req["spec"], req["why"]
        return None, None
    missing = {t for t in need if not ready(t)}

    def other_version(tool: str) -> str | None:
        """프로젝트가 정한 것과 다른 버전이라도 이 PC 에 있으면 그 버전 — 설치를 거절해도 그것으로 돌린다 (빌드를 아예 건너뛰지 않게)."""
        if tool == "python":
            return toolchains._py_version(toolchains._user_python())
        return toolchains.installed(tool) if tool in toolchains.FETCH else None
    for tool in sorted(missing):
        if run is None or tool in (inbox.load_run(settings, run["id"]).get("tools_declined") or []):
            if other_version(tool):
                missing.discard(tool)  # 이미 거절했다 — 있는 버전으로 (경고는 처음 거절할 때 남겼다)
            continue
        spec, where = spec_of(tool)
        name = toolchains.NAMES.get(tool) or prereqs.TOOLS[tool][0]
        why = prereqs.TOOLS[tool][1] if tool in prereqs.TOOLS else ""
        have = toolchains.installed(tool) if tool in toolchains.FETCH or tool == "python" else None
        if spec:  # 프로젝트가 정한 버전 — 'Java 21 이 필요 (pom.xml) · 이 PC 는 17'
            name, why = f"{name} {spec}", f"{where} 에 적힌 버전" + (f" · 이 PC 는 {have}" if have else "")
        if tool in toolchains.FETCH:
            how = toolchains.describe(tool, spec or "lts")
        elif tool == "python":
            how = "python.org 에서 그 버전을 설치하거나 py 런처로 고른다 (툴킷이 파이썬 자체는 받지 않음)"
        else:
            how = prereqs.install_command(tool) or "직접 설치"
        lead = ""
        for _ in range(3):  # 설치가 안 됐으면 두 번 더 묻고, 그래도 없으면 설치하지 않은 것으로 본다
            ans = inbox.ask(settings, run["id"], "question", {
                "apply": {"kind": "tool_choice", "tool": tool, "spec": spec},
                "question": f"{lead}이 프로젝트를 빌드·테스트하려면 {name} 이(가) 필요한데 이 PC 에 맞는 것이 없습니다 ({why}). "
                            f"툴킷이 설치할까요? 방법: {how}" + (" — 패키지 약관에 동의하고 설치하며, 관리자 확인 창이 뜰 수 있습니다"
                                                            if sys.platform == "win32" and tool in ("cargo", "msvc", "openssl") else ""),
                "choices": ([INSTALL_NOW] if tool != "python" else []) + [INSTALLED, DECLINED]})
            choice = ans.get("choice") or ""
            if choice == INSTALL_NOW:
                inbox.log(settings, run["id"], "note", tone="", text=f"{name} 을(를) 설치하는 중입니다 — {how}")
                if tool in toolchains.FETCH:
                    res = toolchains.fetch(tool, spec or "lts")
                else:
                    jdk = next((toolchains.choose("java", Path(r)) for r in roots), None) if tool == "gradle" else None
                    res = prereqs.install(tool, version=spec if tool in prereqs.MANAGED else None,
                                          java=toolchains._ver(jdk[1])[0] if jdk else None)
                inbox.log(settings, run["id"], "deps", dep=tool, ok=res["ok"], files=[], error=res.get("error"))
            elif choice != INSTALLED:
                break
            refresh_path(max_age=0)
            toolchains.forget()
            if ready(tool):
                missing.discard(tool)
                inbox.log(settings, run["id"], "note", tone="", text=f"{name} 설치를 확인했습니다 — 이어서 빌드·테스트합니다")
                break
            err = res.get("error") if choice == INSTALL_NOW else None
            lead = (f"{name} 설치가 끝나지 않았습니다 ({err}). " if err else
                    f"아직 {name} 을(를) 찾지 못했습니다 (설치 뒤 새 PATH 를 못 읽었으면 앱을 다시 켜야 할 수 있습니다). ")
        if tool in missing:
            inbox.update_run(settings, run["id"], lambda r, t=tool: r.update(
                tools_declined=list(dict.fromkeys((r.get("tools_declined") or []) + [t]))))
            alt = other_version(tool)
            if alt:
                missing.discard(tool)
                inbox.log(settings, run["id"], "note", tone="warn",
                          text=f"{name} 대신 이 PC 의 {alt} 로 빌드·테스트합니다 — 결과가 다를 수 있어 인수인계에 적습니다")
            else:
                inbox.log(settings, run["id"], "note", tone="warn", text=f"{name} 없이 진행합니다 — 빌드·테스트는 인수인계에 남은 확인으로 적습니다")
    keep = [p for p in plan if not (tools_of(p) & missing)]
    skipped = []
    for p in plan:
        lack = sorted(tools_of(p) & missing)
        if lack:
            label = toolchains.NAMES.get(lack[0]) or (prereqs.TOOLS[lack[0]][0] if lack[0] in prereqs.TOOLS else lack[0])
            spec, _ = spec_of(lack[0])
            skipped.append({"kind": p[0], "task": p[1], "command": p[2], "ok": False, "skipped": True, "missing_tool": lack[0],
                            "error": f"{label}{' ' + spec if spec else ''} 이(가) 이 PC 에 없어 돌리지 않음"})
    return keep, skipped


def prepare_tools(settings, run: dict) -> None:
    """라운드를 시작하기 전에 — 이 프로젝트를 빌드·테스트할 도구(프로젝트가 정한 버전)가 없으면 지금 묻고 받는다. 라운드 중간에
    받으면 이미 떠 있는 에이전트의 셸은 PATH 가 그대로라 그 라운드 내내 옛 버전(또는 없음)으로 돌았다."""
    from beom_workbench.core import layout
    root = Path(run["target_root"])
    plan = [(k, "test", command(Path(at), k, "test", [], "") or "", Path(at)) for k, at in layout.projects(root) if k in TOOL_OF]
    if plan:
        ensure_tools(settings, run, plan)


CMD_TOOL = {"go": "go", "cargo": "cargo", "mvn": "mvn", "gradle": "gradle", "dotnet": "dotnet", "node": "node", "npm": "node",
            "npx": "node", "java": "java", "javac": "java"}


def ensure_command_tools(settings, run: dict | None, cmds: list[str | None]) -> list[str]:
    """검증 명령(go run . · mvn spring-boot:run …)을 돌릴 도구 — wb_task 와 같이 없으면 툴킷이 설치를 묻는다. 끝내 없는 도구 이름."""
    import shlex
    plan = []
    for c in cmds:
        if not c:
            continue
        try:
            head = Path(shlex.split(str(c), posix=False)[0].strip('"')).name.lower()
        except ValueError:
            continue
        head = re.sub(r"\.(exe|cmd|bat)$", "", head)
        if head in CMD_TOOL:
            plan.append(("_cmd_" + CMD_TOOL[head], "verify", str(c)))
    if not plan:
        return []
    _, skipped = ensure_tools(settings, run, plan)
    from beom_workbench.core import prereqs
    return sorted({prereqs.TOOLS[x["missing_tool"]][0] for x in skipped})


def result_of(kind: str, task: str, cmd: str, code: int, out: str) -> dict:
    text = _ANSI.sub("", out).replace("\r\n", "\n")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {"kind": kind, "task": task, "command": cmd, "ok": code == 0, "exit_code": code, "summary": _summary(kind, text),
            "output_tail": "\n".join(lines[-60:])[-5000:]}


def _execute(settings, run: dict | None, root: Path, plan: list[tuple], timeout: int = 600, regression: bool = False) -> list[dict]:
    """(종류, 작업, 명령[, 자리]) 를 차례로 — 자동 실행 중이면 격리 규칙대로 (Node 도구는 승인받아 격리 밖), 아니면 그냥."""
    from beom_workbench.core import inbox, port, sandbox
    results = []
    for item in plan:
        kind, task, cmd = item[:3]
        cwd = Path(item[3]) if len(item) > 3 else Path(root)
        from beom_workbench.core import execenv
        env = execenv.command_env(cwd)
        boxed = run is not None and sandbox.wraps_powershell(run)
        outside = False
        why = execenv.outside_reason(cmd, env) if boxed else None
        if why:
            outside = permit_outside(settings, run, cmd, why=why)
            run = inbox.load_run(settings, run["id"])
            if not outside:
                results.append({"kind": kind, "task": task, "command": cmd, "ok": False, "skipped": True,
                                "error": f"격리 밖 실행이 허용되지 않아 돌리지 않음 ({OUTSIDE_WHY[why]})"})
                continue
        t = max(10, min(int(timeout or 600), 1800))

        def go(out_of_box: bool) -> tuple[int, str]:
            if boxed and not out_of_box:  # 격리 안 — node --test 를 한 프로세스로 (그 node 가 받는 옵션만)
                argv, shell, e = sandbox.wrap_argv(["cmd", "/d", "/c", cmd], run, cwd), False, sandbox.node_test_env(dict(env))
            else:  # 문자열 그대로 (목록으로 넘기면 따옴표가 \" 로 바뀌어 cmd 가 경로를 잘못 읽는다)
                argv, shell, e = cmd, True, env
            try:
                cp = port.run_tree(argv, shell=shell, cwd=str(cwd), timeout=t, env=e)
                return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", "replace")
            except subprocess.TimeoutExpired as ex:
                return 124, ((ex.output or b"") + (ex.stderr or b"")).decode("utf-8", "replace") + f"\n({t}초 안에 끝나지 않아 멈춤)"
        code, out = go(outside)
        if boxed and not outside and execenv.denied(code, out) and permit_outside(settings, run, cmd, why="denied"):
            # 격리 때문에 실패한 것 — 같은 허락으로 격리 밖에서 한 번 더 (도구마다 막히는 자리를 다 알 수 없다)
            inbox.log(settings, run["id"], "note", tone="", text=f"격리 안에서 '접근 거부'로 실패해 격리 밖에서 다시 돌립니다: {cmd[:120]}")
            outside = True
            code, out = go(True)
        results.append(result_of(kind, task, cmd, code, out))
        if run is not None:
            inbox.log(settings, run["id"], "task", task=task, project=kind, command=cmd, exit_code=code, outside=outside,
                      # 검사 도구의 끝 출력 — 실패가 이번에 바꾼 파일에서 났는지 인계 때 가른다
                      **({"tail": results[-1]["output_tail"][-3000:]} if task in ("lint", "format", "typecheck") else {}),
                      **({"regression": True} if regression else {}))  # 회귀 확인(원래 테스트 전체)은 인계의 '테스트 통과' 판정에 넣지 않는다
    return results


def _test_kind(path: str) -> str | None:
    return "python" if path.endswith(".py") else "node" if re.search(r"\.[cm]?[jt]sx?$", path) else None


def project_checks(settings, run: dict | None, root: Path, done: dict | None = None, tests_only: set | None = None,
                   regression: bool = False) -> list[dict]:
    """검증 때 툴킷이 함께 돌리는 프로젝트 테스트·타입 검사 — 에이전트가 따로 돌리지 않아도 완료 판정에 들어간다.
    done: 이미 돈 테스트 결과. tests_only: 돌릴 테스트 파일(절대 경로) — 이번 작업에서 새로 만든 테스트만. 원래 있던 테스트는 예전
    동작을 기대해서 바뀐 기능의 기준이 되지 못한다 (바뀐 기능은 합격 케이스로, 영향이 닿는 곳은 연관된 곳 분석으로 확인).
    None 이면 프로젝트의 테스트 전체. 규칙상 빌드가 '묻기'면 한 번 묻는다.
    regression: 회귀 확인 — 프로젝트 테스트 전체만 (타입 검사 없이). 결과에 regression=True 를 단다."""
    from beom_workbench.core import inbox, layout
    root = Path(root)
    done = done or {}
    plan = []
    for k, r in layout.projects(root):  # 이 폴더가 속한 프로젝트마다 그 자리에서 (저장소의 하위 폴더면 그 프로젝트의 자리)
        if k in done:
            pass
        elif tests_only is None:
            if c := command(r, k, "test", [], ""):
                plan.append((k, "test", c, r))
        else:
            mine = sorted(os.path.relpath(f, r).replace("\\", "/") for f in tests_only
                          if Path(f).resolve().is_relative_to(r) and _test_kind(f) == k)
            if mine and (c := command(r, k, "test", mine, "")):
                plan.append((k, "test", c, r))
        if not regression and k in ("node", "python") and (c := command(r, k, "typecheck", [], "")):
            plan.append((k, "typecheck", c, r))  # 다른 언어는 테스트가 컴파일까지 한다
    results = list(done.values())
    if not plan:
        return results
    if run is not None:
        # wb_task 와 같은 승인 — 같은 작업(테스트·타입 검사)을 이미 허락했으면 다시 묻지 않는다
        ok = permit_build(settings, run, [f"task:{p[1]}" for p in plan],
                          "검증 때 프로젝트 테스트·타입 검사 실행 (툴킷이 고른 명령): " + " ; ".join(c for _, _, c, _ in plan))
        if not ok:
            why = ("규칙상 빌드·테스트 실행이 허용되지 않아 돌리지 않음" if inbox.decide(run, "build") == "refuse"
                   else "사용자가 검증 때 테스트 실행을 거절함")
            return results + [{"kind": k, "task": t, "command": c, "ok": False, "skipped": True, "error": why} for k, t, c, _ in plan]
        run = inbox.load_run(settings, run["id"])
    plan, missing = ensure_tools(settings, run, plan)
    out = _execute(settings, run, root, plan, regression=regression)
    for item, res in zip(plan, out):
        _scope_to_target(res, Path(item[3]), root)
        if regression:
            res["regression"] = True
    return results + missing + out


_TYPE_ERR = re.compile(r"^(?P<p>[^\s(:][^\s(]*?\.(?:tsx?|mts|cts|jsx?|py))(?:\(\d+,\d+\)|:\d+(?::\d+)?): error", re.M)


def _scope_to_target(res: dict, proot: Path, target: Path) -> None:
    """대상 폴더가 더 큰 프로젝트(저장소 맨 위 등)에 속하면 그 프로젝트의 타입 검사는 저장소 전체를 본다 — 대상 폴더 밖의 오류는
    이 작업의 판정에서 뺀다 (오류가 모두 밖이면 통과로 보고, 몇 개였는지 적는다). 오류 위치를 읽지 못하면 그대로 둔다."""
    if res.get("ok") or res.get("task") != "typecheck" or Path(proot).resolve() == Path(target).resolve():
        return
    paths = [m.group("p") for m in _TYPE_ERR.finditer(res.get("output_tail") or "")]
    if not paths:
        return
    t = Path(target).resolve()
    inside = [x for x in paths if (Path(proot) / x).resolve().is_relative_to(t)]
    if not inside:
        res.update(ok=True, note=f"프로젝트 전체 타입 검사의 오류 {len(paths)}개는 모두 대상 폴더 밖 — 이 작업의 판정에서 뺌")


# ---------------------------------------------------------------- 파일 지우기

_TOOLKIT_SCRATCH = (".beom/tmp/", ".beom/helpers/")


LINTABLE = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".css", ".scss", ".html", ".json", ".md")


def changed_code(settings, run: dict, root: Path) -> list[str]:
    """이번 실행에서 바꾼(지우지 않은) 검사할 파일 — root 기준 상대 경로. 모르면 []."""
    from beom_workbench.core import inbox
    from beom_workbench.core.checkpoints import Checkpoints
    base = _start_checkpoint(settings, run)
    if not base:
        return []
    try:
        files = Checkpoints(inbox.run_dir(settings, run["id"]), run["target_root"]).changes(base).get("files") or []
    except Exception:  # noqa: BLE001
        return []
    top = Path(run["target_root"]).resolve()
    out = []
    for f in files:
        full = (top / f["path"]).resolve()
        if f.get("status") == "D" or not full.is_file() or not f["path"].lower().endswith(LINTABLE) or f["path"].startswith(".beom/"):
            continue
        try:
            out.append(full.relative_to(Path(root).resolve()).as_posix())
        except ValueError:
            continue
    return out[:200]


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


def _born_after_start(p: Path, run: dict) -> bool:
    """체크포인트에 담지 않는 폴더(dist·build·node_modules …)는 실행 전부터 있었는지 체크포인트로 알 수 없다 — 만들어진 시각으로 본다.
    (실험: 이번 빌드가 만든 dist 를 지우려 하자 '실행 전부터 있던 파일' 카드가 두 번 떴다.) 만든 시각을 주지 않는 시스템이면 모른다."""
    from datetime import datetime
    try:
        start = datetime.fromisoformat(run["created_at"]).timestamp()
        st = p.stat()
    except (KeyError, ValueError, TypeError, OSError):
        return False
    born = getattr(st, "st_birthtime", None)
    if born is None and sys.platform == "win32":
        born = st.st_ctime  # Windows 의 st_ctime 은 만든 시각
    return born is not None and born > start + 1  # 기록이 초 단위로 잘린다


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
        # 순서·공통 부품 막기는 지우기에도 — 계획 전(기다리는 실행이면 승인 전)에 원래 있던 제품 파일을, 승인 안 된 공통 부품을 지우지 않는다
        if existed is not False:
            from beom_workbench import hookgate
            from beom_workbench.core import runner as runner_core
            why = hookgate.verdict(run, "Write", {"file_path": str(p)})
            if why:
                refused.append(f"{rel} ({why})")
                continue
            if rel in runner_core.shared_locked(run):
                refused.append(f"{rel} (다른 화면들이 쓰는 기존 공통 부품 — 계획의 shared_changes 로 신고·승인 뒤에)")
                continue
        if existed is False and (not _snapshot_skips(rel) or _born_after_start(p, run)):
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
