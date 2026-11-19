"""프로젝트 구조 분석 — 폴더 이름으로 짐작하지 않고, 각 생태계가 스스로 쓰는 규칙으로 정한다.

어느 프로젝트가 이 폴더를 소유하나 (그 프로젝트의 자리에서 테스트·설치를 돌린다):
- Node: 가장 가까운 package.json — Node 의 패키지 범위 규칙과 같다. Rust: Cargo.toml · Go: go.mod · Maven: pom.xml ·
  Gradle: build.gradle(.kts)/settings.gradle(.kts) · .NET: *.csproj/*.sln — 각 도구가 위로 찾는 파일.
- 파이썬: pytest 의 rootdir 규칙 (가장 가까운 pytest.ini·pyproject.toml·tox.ini·setup.cfg, 다음 setup.py). 없으면 패키지(__init__.py)
  사슬 맨 위의 바깥 폴더 (import 기준 — api/search 가 api 패키지 안이면 그 위), 그도 없으면 의존성 파일(requirements*.txt·Pipfile).
- 위로 찾는 범위는 이 폴더를 품은 git 저장소의 맨 위까지 (저장소가 아니면 이 폴더만) — 저장소 밖 사용자 폴더의 package.json 을
  잡지 않게. 위쪽에서 찾은 프로젝트는 이 폴더에 그 언어 파일이 있을 때만 — 빈 새 폴더가 저장소 맨 위의 다른 언어 프로젝트에
  묶이지 않게.

테스트 파일은 그 실행기가 찾는 규칙으로: pytest (python_files·testpaths·norecursedirs 설정 포함), node --test (기본 규칙·스크립트에
적은 경로), jest (testMatch 기본·package.json 설정), vitest (include 기본), mocha (기본 test/ · 설정의 spec).
"""
from __future__ import annotations

import configparser
import fnmatch
import json
import os
import re
import time
from pathlib import Path

SKIP_DIRS = {"node_modules", ".git", ".beom", "dist", "build", "target", ".venv", "venv", "env", "__pycache__", "coverage",
             ".pytest_cache", ".mypy_cache", ".tox", ".next", ".nuxt", "out"}
LANG_EXT = {
    "node": (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx", ".vue", ".svelte"),
    "python": (".py",),
    "rust": (".rs",),
    "go": (".go",),
    "maven": (".java", ".kt"),
    "gradle": (".java", ".kt", ".kts"),
    "dotnet": (".cs", ".fs", ".vb"),
}
MARKERS = {
    "node": ("package.json",),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "maven": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
}
PY_ROOT_FILES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")  # pytest 가 rootdir 를 정할 때 보는 설정 파일
PY_DEP_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "dev-requirements.txt",
                "test-requirements.txt", "Pipfile")
ORDER = ("node", "python", "rust", "go", "maven", "gradle", "dotnet")
_CACHE: dict[str, tuple[float, list]] = {}
_TTL = 3.0  # 한 검증·한 권한 판정 안에서 여러 번 묻는 것만 아낀다 (에이전트가 파일을 만들면 곧 다시 본다)


def repo_top(path: Path) -> Path | None:
    """이 폴더를 품은 git 저장소의 맨 위 (없으면 None)."""
    p = Path(path).resolve()
    return next((q for q in (p, *p.parents) if (q / ".git").exists()), None)


def _upward(target: Path):
    """target 부터 위로 — 저장소 맨 위까지 (저장소가 아니면 target 만)."""
    t = Path(target).resolve()
    top = repo_top(t)
    yield t
    if top is None or top == t:
        return
    for p in t.parents:
        yield p
        if p == top:
            return


def _has_marker(d: Path, kind: str) -> bool:
    if kind == "dotnet":
        return any(d.glob("*.csproj")) or any(d.glob("*.sln")) or any(d.glob("*.fsproj"))
    return any((d / n).is_file() for n in MARKERS.get(kind, ()))


def has_lang_files(target: Path, kind: str, limit: int = 3000) -> bool:
    """이 폴더(아래)에 그 언어의 파일이 있나 — 큰 폴더(node_modules 등)는 건너뛰고, 처음 찾으면 멈춘다."""
    exts = LANG_EXT.get(kind, ())
    seen = 0
    for dirpath, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if any(f.endswith(exts) for f in files):
            return True
        seen += len(files)
        if seen > limit:
            return False
    return False


def owner(target: Path, kind: str) -> Path | None:
    """이 폴더를 소유하는 그 언어의 프로젝트 자리 (모듈 설명의 규칙) — 없으면 None."""
    ups = list(_upward(target))
    if kind == "python":
        for names in (PY_ROOT_FILES, ("setup.py",)):
            hit = next((d for d in ups if any((d / n).is_file() for n in names)), None)
            if hit:
                return hit
        d = ups[0]
        while (d / "__init__.py").is_file() and d.parent in ups:
            d = d.parent
        if d != ups[0]:
            return d  # 패키지 안의 폴더 — import 기준 자리 (그 위에서 'import api.search' 로 불린다)
        return next((d for d in ups if any((d / n).is_file() for n in PY_DEP_FILES)), None)
    return next((d for d in ups if _has_marker(d, kind)), None)


def projects(target: Path) -> list[tuple[str, Path]]:
    """이 폴더가 속한 프로젝트들 [(종류, 자리)] — 여럿일 수 있다 (파이썬 서버 + Node 화면)."""
    t = Path(target).resolve()
    key = str(t)
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return list(hit[1])
    out = []
    if t.is_dir():
        for kind in ORDER:
            root = owner(t, kind)
            if root is not None and (root == t or has_lang_files(t, kind)):
                out.append((kind, root))
            elif root is None and kind == "python" and (pytest_files(t) or has_lang_files(t, "python")):
                out.append((kind, t))  # 설정 파일 없는 파이썬 폴더 — pytest·실행은 그 자리에서
    _CACHE[key] = (time.monotonic(), out)
    return list(out)


def root_for(target: Path, kind: str) -> Path:
    """그 언어의 프로젝트 자리 — 이 폴더가 그 언어 프로젝트에 속하지 않으면 이 폴더."""
    return next((r for k, r in projects(target) if k == kind), Path(target).resolve())


def forget(target: Path | None = None) -> None:
    """파일을 막 만들거나 지운 뒤 — 다음 질문은 새로 본다."""
    if target is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(Path(target).resolve()), None)


# ---------------------------------------------------------------- 테스트 파일 — 실행기의 규칙으로

def _walk(root: Path, starts: list[Path], skip_dir, limit: int = 6000):
    seen = 0
    for s in starts:
        if s.is_file():
            yield s
            continue
        for dirpath, dirs, files in os.walk(s):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not skip_dir(Path(dirpath) / d, d)]
            for f in files:
                yield Path(dirpath) / f
            seen += len(files)
            if seen > limit:
                return


def _pytest_config(root: Path) -> dict:
    """pytest 설정 (testpaths·python_files·norecursedirs) — pytest 가 읽는 순서대로 처음 찾은 것."""
    def split(v):
        return [x for x in re.split(r"[\s,]+", v or "") if x] if isinstance(v, str) else list(v or [])
    ini = root / "pytest.ini"
    if ini.is_file():
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(ini, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            return {}
        sec = cp["pytest"] if cp.has_section("pytest") else {}
        return {k: split(sec.get(k)) for k in ("testpaths", "python_files", "norecursedirs") if sec.get(k)}
    pp = root / "pyproject.toml"
    if pp.is_file():
        try:
            import tomllib
            opts = tomllib.loads(pp.read_text(encoding="utf-8")).get("tool", {}).get("pytest", {}).get("ini_options")
        except (ValueError, OSError, ImportError):
            opts = None
        if opts is not None:
            return {k: split(opts.get(k)) for k in ("testpaths", "python_files", "norecursedirs") if opts.get(k)}
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        f = root / name
        if f.is_file():
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read(f, encoding="utf-8")
            except (configparser.Error, OSError, UnicodeDecodeError):
                continue
            if cp.has_section(section):
                sec = cp[section]
                return {k: split(sec.get(k)) for k in ("testpaths", "python_files", "norecursedirs") if sec.get(k)}
    return {}


PYTEST_NORECURSE = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}")


def pytest_files(root: Path, limit: int = 6000) -> list[str]:
    """pytest 가 이 자리에서 인자 없이 돌 때 모을 테스트 파일 (상대 경로) — pytest 의 기본 규칙과 이 프로젝트의 설정대로."""
    root = Path(root)
    cfg = _pytest_config(root)
    pats = cfg.get("python_files") or ["test_*.py", "*_test.py"]
    norec = cfg.get("norecursedirs") or list(PYTEST_NORECURSE)
    starts = [root / p for p in cfg.get("testpaths", []) if (root / p).exists()] or [root]

    def skip(path: Path, name: str) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in norec) or (path / "pyvenv.cfg").is_file()  # pytest 는 가상 환경도 건너뛴다
    out = []
    for f in _walk(root, starts, skip, limit):
        if any(fnmatch.fnmatch(f.name, p) for p in pats):
            out.append(f.relative_to(root).as_posix())
    return sorted(out)


def _package(root: Path) -> dict:
    try:
        return json.loads((Path(root) / "package.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


NPM_PLACEHOLDER = re.compile(r"no test specified", re.I)


def test_script(root: Path) -> str:
    s = (_package(root).get("scripts") or {}).get("test") or ""
    return "" if NPM_PLACEHOLDER.search(s) else s


def node_runner(root: Path) -> str | None:
    """package.json 의 test 스크립트가 쓰는 실행기 (vitest·jest·mocha·node) — 적힌 것이 없으면 None."""
    s = test_script(root)
    for name in ("vitest", "jest", "mocha"):
        if re.search(rf"\b{name}\b", s):
            return name
    if re.search(r"\bnode\b[^&|;]*--test\b", s):
        return "node"
    return None


def _script_paths(root: Path) -> list[str]:
    """'node --test <경로…>' 의 경로들."""
    m = re.search(r"\bnode(?:\.exe)?\b[^&|;]*?--test\b([^&|;]*)", test_script(root))
    return [t.strip("\"'") for t in m.group(1).split() if not t.startswith("-")] if m else []


_JS = r"\.[cm]?[jt]sx?$"
NODE_DEFAULT = re.compile(r"(?:^|/)test/.+\.[cm]?[jt]s$|[.\-_]test\.[cm]?[jt]s$|(?:^|/)test-[^/]*\.[cm]?[jt]s$|(?:^|/)test\.[cm]?[jt]s$")
JEST_DEFAULT = re.compile(r"(?:^|/)__tests__/.+\.[jt]sx?$|(?:^|/)[^/]*[.](?:spec|test)\.[jt]sx?$")
VITEST_DEFAULT = re.compile(r"(?:^|/)[^/]*[.](?:test|spec)\.[cm]?[jt]sx?$")
MOCHA_DEFAULT = re.compile(r"^test/[^/]+\.[cm]?js$")


def _glob_rx(pattern: str) -> re.Pattern:
    """jest testMatch·mocha spec 같은 glob 을 정규식으로 (**·*·?·{a,b} 만)."""
    p = pattern.lstrip("./").replace("<rootDir>/", "")
    out, i = "", 0
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
            continue
        if p.startswith("**", i):
            out, i = out + ".*", i + 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "{":
            j = p.find("}", i)
            out += "(?:" + "|".join(re.escape(x) for x in p[i + 1:j].split(",")) + ")" if j > i else re.escape(c)
            i = j if j > i else i
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$")


def _node_rules(root: Path) -> list[re.Pattern]:
    runner = node_runner(root)
    pkg = _package(root)
    if runner == "jest":
        cfg = pkg.get("jest") or {}
        jf = Path(root) / "jest.config.json"
        if not cfg and jf.is_file():
            try:
                cfg = json.loads(jf.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                cfg = {}
        if cfg.get("testMatch"):
            return [_glob_rx(x) for x in cfg["testMatch"]]
        if cfg.get("testRegex"):
            rx = cfg["testRegex"]
            return [re.compile(x) for x in (rx if isinstance(rx, list) else [rx])]
        return [JEST_DEFAULT]
    if runner == "vitest":
        return [VITEST_DEFAULT]
    if runner == "mocha":
        spec = (pkg.get("mocha") or {}).get("spec")
        mf = Path(root) / ".mocharc.json"
        if not spec and mf.is_file():
            try:
                spec = json.loads(mf.read_text(encoding="utf-8-sig")).get("spec")
            except (OSError, ValueError):
                spec = None
        return [_glob_rx(x) for x in ([spec] if isinstance(spec, str) else spec)] if spec else [MOCHA_DEFAULT]
    paths = _script_paths(root)
    if paths:
        rules = []
        for x in paths:
            x = x.replace("\\", "/").rstrip("/")
            if any(ch in x for ch in "*?{"):
                rules.append(_glob_rx(x))
            elif (Path(root) / x).is_dir():
                rules.append(re.compile("^" + re.escape(x.lstrip("./")) + r"/.+\.[cm]?[jt]sx?$"))
            else:
                rules.append(re.compile("^" + re.escape(x.lstrip("./")) + "$"))
        return rules
    return [NODE_DEFAULT]


def node_test_files(root: Path, limit: int = 6000) -> list[str]:
    """이 Node 프로젝트의 실행기가 모을 테스트 파일 (상대 경로)."""
    root = Path(root)
    rules = _node_rules(root)
    out = []
    for f in _walk(root, [root], lambda p, n: n.startswith("."), limit):
        rel = f.relative_to(root).as_posix()
        if re.search(_JS, rel) and any(r.search(rel) for r in rules):
            out.append(rel)
    return sorted(out)


def test_files(target: Path) -> set[str]:
    """이 폴더가 속한 프로젝트들의 테스트 파일 (절대 경로) — 기존 테스트를 바꿨는지 볼 때."""
    out: set[str] = set()
    for kind, root in projects(target):
        rels = pytest_files(root) if kind == "python" else node_test_files(root) if kind == "node" else []
        out.update(str((root / r).resolve()) for r in rels)
    return out


# ---------------------------------------------------------------- 브라우저에서 도는 코드

_SCRIPT_SRC = re.compile(r"<script\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)  # src="{{ url_for('static', …) }}" 도
_IMPORT = re.compile(r"""(?:\bimport\s+(?:[^'";]*?\bfrom\s*)?|\bexport\s+[^'";]*?\bfrom\s*|\bimport\s*\(\s*)["'](\.{1,2}/[^"']+)["']""")


def _resolve_js(base: Path, spec: str) -> Path | None:
    p = (base / spec).resolve()
    for cand in (p, *(p.with_name(p.name + e) for e in (".js", ".mjs", ".ts", ".jsx", ".tsx")),
                 *(p / ("index" + e) for e in (".js", ".ts", ".jsx", ".tsx"))):
        if cand.is_file():
            return cand
    return None


def browser_files(target: Path, limit: int = 3000) -> set[str]:
    """페이지(HTML)가 <script src> 로 불러오는 JS 와 그것이 상대 경로로 import 하는 파일 (대상 폴더 기준 상대 경로) —
    브라우저에서 도는 코드다. 툴킷이 케이스로 띄우는 node·python 프로세스에서는 돌지 않으므로 실행 범위를 거기서 잴 수 없다."""
    t = Path(target).resolve()
    every = []
    seen = 0
    for dirpath, dirs, files in os.walk(t):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        every.extend(Path(dirpath) / f for f in files)
        seen += len(files)
        if seen > limit:
            break
    by_tail: dict[str, list[Path]] = {}
    for f in every:
        rel = f.relative_to(t).as_posix()
        parts = rel.split("/")
        for i in range(len(parts)):
            by_tail.setdefault("/".join(parts[i:]).lower(), []).append(f)
    found: set[Path] = set()
    todo: list[Path] = []
    for html in (f for f in every if f.suffix.lower() in (".html", ".htm", ".jinja", ".j2")):
        try:
            text = html.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _q, src in _SCRIPT_SRC.findall(text):
            if re.match(r"^(?:[a-z]+:)?//", src, re.I):
                continue
            m = re.search(r"url_for\(\s*['\"]static['\"]\s*,\s*filename\s*=\s*['\"]([^'\"]+)['\"]", src)  # Flask 템플릿
            if m:
                spec = "static/" + m.group(1)
            elif "{{" in src or "{%" in src:
                continue
            else:
                spec = src.split("?")[0].split("#")[0]
            cands = [c for c in ((html.parent / spec.lstrip("/")).resolve(), (t / spec.lstrip("/")).resolve()) if c.is_file()]
            if not cands:
                cands = by_tail.get(spec.lstrip("/").lower(), [])
                cands = cands if len(cands) == 1 else []
            for c in cands:
                if c not in found:
                    found.add(c)
                    todo.append(c)
    while todo:
        f = todo.pop()
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec in _IMPORT.findall(text):
            c = _resolve_js(f.parent, spec)
            if c and c not in found and c.is_relative_to(t):
                found.add(c)
                todo.append(c)
    return {f.relative_to(t).as_posix() for f in found if f.is_relative_to(t)}
