"""New projects: each language's standard folder layout.

An existing project keeps its own layout (the agent follows what is there). A project made from an empty folder gets the
layout its ecosystem expects — unless the user asked for a different one. The table here is the single source: it goes
into the agent's instructions, and the plan check compares the planned files with it. Only clear departures are flagged
(a missing project file, tests mixed into the code, loose modules at the top); anything else is the agent's call."""
from __future__ import annotations

import os
import re
from pathlib import Path

# (스택 이름, 정석 구조) — 지시에 그대로 싣는다
STANDARDS: list[tuple[str, str]] = [
    ("파이썬", "pyproject.toml(이름·의존성·pytest 설정, CLI 면 [project.scripts]) · 코드는 패키지 폴더 <패키지>/ (배포할 라이브러리면 "
              "src/<패키지>/) · 테스트는 tests/test_*.py · 웹(Flask·FastAPI·Django)은 패키지 안에 앱 팩토리·라우트(views·routers)·"
              "templates/·static/ · 실행 진입점은 <패키지>/__main__.py 나 [project.scripts] (파일 하나짜리 스크립트는 그 파일 하나로)"),
    ("Node·TypeScript", "package.json(scripts: start·test·build) · 코드는 src/ · 테스트는 test/ 나 tests/ (러너 관례대로 *.test.*) · "
                        "TypeScript 면 tsconfig.json · 서버는 src/ 안을 역할별로(routes·services·repositories)"),
    ("React·Vue·Svelte(Vite)", "Vite 기본 틀 — index.html · src/main.* · src/App.* · src/components/ · src/pages/(Vue 는 views/) · "
                               "src/hooks/(Vue 는 composables/) · src/api/ · src/styles/ · public/"),
    ("빌드 없는 정적 웹", "index.html 을 맨 위에 — 파일이 늘면 css/ · js/ · images/ (서너 개면 맨 위에 그대로)"),
    ("Java·Kotlin", "Maven/Gradle 표준 — src/main/java(kotlin)/<패키지 경로>/ · src/main/resources/ · src/test/java/ "
                    "(빌드 도구 없이 javac 로 하라면 src/<패키지 경로>/ 와 test/)"),
    ("Go", "go.mod · 실행 파일은 cmd/<이름>/main.go (하나뿐이면 맨 위 main.go) · 내부 패키지는 internal/<이름>/ · "
           "테스트는 같은 폴더의 *_test.go"),
    ("Rust", "cargo 기본 — Cargo.toml · src/main.rs(실행) 나 src/lib.rs(라이브러리) · 통합 테스트는 tests/ · 단위 테스트는 같은 파일의 #[cfg(test)]"),
    ("C#·.NET", "<이름>.sln · src/<프로젝트>/<프로젝트>.csproj · tests/<프로젝트>.Tests/"),
    ("PHP", "composer.json · src/(PSR-4) · public/index.php · tests/"),
    ("공통", "README.md(설치·실행·테스트 명령) · 스택에 맞는 .gitignore (node_modules·__pycache__·.venv·target·bin/obj 등) · "
            "비밀값은 .env.example 로 이름만"),
]

# 의존성 버전을 고정하는 잠금 파일 — 커밋하고, 지우거나 되돌리지 않는다 (한 곳에서)
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb", "Cargo.lock",
             "poetry.lock", "uv.lock", "go.sum", "Pipfile.lock", "composer.lock", "Gemfile.lock", "packages.lock.json"}
NODE_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
CODE_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".go", ".rs", ".java", ".kt", ".cs", ".php",
             ".rb", ".swift", ".c", ".cpp", ".h")
PY_MANIFESTS = {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile"}
PY_ROOT_OK = {"manage.py", "conftest.py", "setup.py", "noxfile.py", "tasks.py"}
JS_ROOT_OK = re.compile(r"(^|\.)config\.|^\.?eslintrc|^\.?prettierrc|^babel\.|^jest\.|^vitest\.|^webpack\.|^rollup\.|^postcss\.|"
                        r"^tailwind\.|^svelte\.config|^nuxt\.config|^next\.config|^vite-env\.d\.ts$|\.d\.ts$")
SKIP = {".git", ".beom", "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", "bin", "obj", ".pytest_cache"}


def rules_text() -> str:
    """지시에 싣는 정석 구조 — 한 줄에 스택 하나."""
    return "\n".join(f"  · {name}: {how}" for name, how in STANDARDS)


def _norm(f: str) -> str:
    f = str(f).strip().replace("\\", "/")
    f = re.sub(r"(?::\d+(?:[-:]\d+)?|#L\d+.*)$", "", f)
    while f.startswith("./"):
        f = f[2:]
    return f.strip("/")


def files_now(root: Path, limit: int = 3000) -> set[str]:
    """대상 폴더에 지금 있는 파일 (툴킷 작업 폴더·의존성·빌드 결과 빼고)."""
    out: set[str] = set()
    root = Path(root)
    if not root.is_dir():
        return out
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP and not x.startswith(".")]
        for n in names:
            out.add(Path(d, n).relative_to(root).as_posix())
            if len(out) >= limit:
                return out
    return out


MANIFEST_NAMES = {"package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile", "go.mod", "Cargo.toml",
                  "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "composer.json"}


def _project_roots(files: set[str]) -> list[str]:
    """설정 파일이 있는 폴더 — 하나의 저장소 안 여러 하위 프로젝트(frontend/·backend/, Gradle 멀티 프로젝트, Cargo 워크스페이스)."""
    roots = {""}
    for f in files:
        name = f.split("/")[-1]
        if name in MANIFEST_NAMES or name.endswith((".csproj", ".sln", ".fsproj")):
            roots.add(f.rsplit("/", 1)[0] if "/" in f else "")
    return sorted(roots, key=lambda r: -len(r))


def check(planned: list[str], present: set[str] | None = None, hard_only: bool = False) -> list[str]:
    """계획한 파일(과 이미 만든 파일)이 그 언어의 정석 구조에서 분명히 벗어나는 곳 — 사람이 읽는 한 줄씩.
    설정 파일이 있는 폴더마다(하위 프로젝트마다) 따로 본다. hard_only: 깨지는 것만 (설정 파일 없음 등) — 인수인계 때.
    그 밖의 정리 규칙(모듈 배치·테스트 위치·README·.gitignore)은 계획 때 알린다."""
    files = {_norm(f) for f in planned if _norm(f)} | {_norm(f) for f in present or set()}
    files = {f for f in files if not f.startswith(".beom/") and "*" not in f}
    roots = _project_roots(files)
    groups: dict[str, set[str]] = {r: set() for r in roots}
    for f in files:
        r = next(r for r in roots if r == "" or f.startswith(r + "/"))
        groups[r].add(f[len(r) + 1:] if r else f)
    out: list[str] = []
    for r in sorted(groups, key=len):
        for line in _check_one(groups[r], hard_only):
            out.append(f"{r}/: {line}" if r else line)
    if not hard_only:  # README·.gitignore 는 저장소 맨 위에서 한 번 (하위 프로젝트마다 묻지 않는다)
        top = {f for f in files if "/" not in f}
        code = [f for f in files if f.lower().endswith(CODE_EXTS)]
        deps = any(f.split("/")[-1] in MANIFEST_NAMES for f in files) or any(f.lower().endswith((".py", ".ts", ".go", ".rs", ".java", ".cs"))
                                                                              for f in files)
        if len(code) >= 3 and not any(t.lower().startswith("readme") for t in top):
            out.append("README 가 없음 — 설치·실행·테스트 명령을 적는다")
        if deps and len(code) >= 3 and ".gitignore" not in top:
            out.append(".gitignore 가 없음 — 이 스택의 의존성·캐시·빌드 결과 폴더를 뺀다")
    return out


def _check_one(files: set[str], hard_only: bool) -> list[str]:
    names = {f.split("/")[-1] for f in files}
    top = {f for f in files if "/" not in f}
    ext = lambda e: [f for f in files if f.lower().endswith(e)]  # noqa: E731
    code = [f for f in files if f.lower().endswith(CODE_EXTS)]
    hard: list[str] = []
    soft: list[str] = []

    py = ext(".py")
    if py:
        code_py = [f for f in py if not re.match(r"(test_.*|.*_test|conftest)\.py$", f.split("/")[-1])]
        if len(code_py) >= 2 and not (PY_MANIFESTS & top):  # 파일 하나짜리 스크립트는 (테스트가 붙어도) 그 파일 하나로 충분하다
            hard.append("파이썬 프로젝트 설정 파일이 없음 — 맨 위에 pyproject.toml (의존성·pytest 설정, CLI 면 [project.scripts]) "
                        "또는 requirements.txt")
        tests = [f for f in py if re.match(r"(test_.*|.*_test)\.py$", f.split("/")[-1])]
        loose = [f for f in tests if not re.search(r"(^|/)tests?/", f)]
        if loose and len(code_py) >= 3:  # 작은 앱(main.py + test_main.py)은 나란히 두는 것도 흔하다
            soft.append(f"테스트가 코드 사이에 있음 ({', '.join(sorted(loose)[:3])}) — tests/ 폴더에")
        mods = [f for f in py if f in top and f not in PY_ROOT_OK and f not in tests]
        if len(mods) >= 4:
            soft.append(f"파이썬 모듈 {len(mods)}개가 맨 위에 흩어져 있음 ({', '.join(sorted(mods)[:4])}) — 패키지 폴더 "
                        "<이름>/ (배포할 라이브러리면 src/<이름>/) 에 넣고 __init__.py 를 둔다")

    js_typed = ext(".ts") + ext(".tsx") + ext(".jsx") + ext(".vue") + ext(".svelte")
    if js_typed or "package.json" in top:
        if "package.json" not in top:
            hard.append("package.json 이 없음 — 의존성과 scripts(start·test·build)를 적는다")
        if ([f for f in ext(".ts") if not f.endswith(".d.ts")] or ext(".tsx")) and "tsconfig.json" not in names:
            hard.append("TypeScript 인데 tsconfig.json 이 없음")
        src_top = [f for f in top if f.lower().endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte"))
                   and not JS_ROOT_OK.search(f)]
        # 빌드 없는 정적 웹(맨 위 index.html, 번들러 없음)이 Node 테스트 때문에 package.json 을 둔 것은 '정적 웹' 표를 따른다
        static_site = "index.html" in top and not js_typed and not any(re.match(r"(vite|webpack|rollup|next|nuxt|svelte|astro)\\.config\\.", f)
                                                                       for f in top)
        if len(src_top) >= 4 and not static_site:  # 작은 Node 서버(server.js·db.js)는 맨 위에 두는 것도 흔하다
            soft.append(f"소스 {len(src_top)}개가 맨 위에 있음 ({', '.join(sorted(src_top)[:4])}) — src/ 에")

    if ext(".go") and "go.mod" not in top:
        hard.append("go.mod 가 없음 — go mod init <모듈 경로>")
    rs = ext(".rs")
    if rs:
        if "Cargo.toml" not in top:
            hard.append("Cargo.toml 이 없음 — cargo 프로젝트로")
        odd = [f for f in rs if not re.match(r"(src|tests|benches|examples)/", f) and f != "build.rs"]
        if odd:
            soft.append(f"Rust 소스가 src/·tests/ 밖에 있음 ({', '.join(sorted(odd)[:3])})")
    jv = ext(".java") + ext(".kt")
    if jv:
        odd = [f for f in jv if not re.match(r"(src|test|tests)/", f)]
        if odd:
            soft.append(f"Java·Kotlin 소스가 src/ 밖에 있음 ({', '.join(sorted(odd)[:3])}) — src/main/java/<패키지 경로>/ "
                        "(빌드 도구 없이면 src/<패키지 경로>/)")
    if ext(".cs") and not (ext(".csproj") or ext(".fsproj")):
        hard.append(".csproj 가 없음 — dotnet new 로 만든 프로젝트 틀대로")
    if ext(".php") and len(ext(".php")) >= 3 and "composer.json" not in top:
        soft.append("composer.json 이 없음")

    return hard if hard_only else hard + soft
