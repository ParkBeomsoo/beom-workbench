"""Toolchains a project asks for — the version it declares, not whatever is newest.

Each ecosystem already writes down what it needs: a JDK release in pom.xml or a Gradle toolchain, `.nvmrc` or `engines.node`,
`go.mod`'s `go` line, a .NET `global.json` or `TargetFramework`, Python's `requires-python`, and the Maven/Gradle wrapper
properties. The toolkit reads that, uses an installed tool when it already satisfies it, and otherwise fetches a matching
build from the official source (Adoptium, nodejs.org, go.dev, Microsoft's .NET release metadata) into the toolkit's own
tools folder, checks the published checksum, and puts it first on PATH for the toolkit's commands and the agent. Nothing is
installed system-wide. Python interpreters are not fetched (a mismatch is reported instead)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

def _arch() -> str:
    import platform
    m = platform.machine().lower()
    return "aarch64" if m in ("arm64", "aarch64") else "x64"


def _os() -> str:
    return "windows" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "linux"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- 프로젝트가 정한 버전


def _up(root: Path, *names: str) -> Path | None:
    """root 에서 저장소 맨 위(.git 이 있는 폴더)까지 올라가며 처음 보이는 그 파일 — nvm·dotnet·Maven·Gradle 이 찾는 방식대로.
    .nvmrc·global.json·래퍼 설정이 모노레포 맨 위에만 있으면 하위 프로젝트에서 못 읽어 다른 버전으로 돌았다."""
    d = Path(root)
    for _ in range(8):
        for n in names:
            if (d / n).is_file():
                return d / n
        if (d / ".git").exists() or d.parent == d:
            return None
        d = d.parent
    return None


# Node LTS 이름 (.nvmrc 의 lts/iron) — 'lts' 하나로 읽어 아무 Node 나 받던 것을 그 주 버전으로
NODE_LTS = {"argon": 4, "boron": 6, "carbon": 8, "dubnium": 10, "erbium": 12, "fermium": 14, "gallium": 16, "hydrogen": 18,
            "iron": 20, "jod": 22, "krypton": 24, "lithium": 26}


def _node_spec(v: str) -> str | None:
    v = v.strip().lstrip("v")
    if v.lower().startswith("lts/"):
        name = v[4:].strip().lower()
        return str(NODE_LTS[name]) if name in NODE_LTS else "lts"
    if v.lower() in ("node", "stable", "latest", "current", "lts", "*"):
        return "lts"
    return v if re.match(r"\d", v) else None


def _java_major(text: str) -> str | None:
    """'21' · '1.8' · '17.0.2' · 'temurin-21' · 'openjdk64-17.0.2'(jenv) → 주 버전. 마지막 버전 꼴을 쓴다 ('openjdk64' 의 64 가 아니라)."""
    toks = re.findall(r"(?<![\d.])(\d+(?:\.\d+)*)", text or "")
    if not toks:
        return None
    t = next((x for x in reversed(toks) if "." in x), toks[-1])
    parts = t.split(".")
    return parts[1] if parts[0] == "1" and len(parts) > 1 else parts[0]


def _pom_props(text: str, into: dict) -> None:
    for body in re.findall(r"<properties>(.*?)</properties>", text, re.S):
        for k, v in re.findall(r"<([\w.\-]+)>\s*([^<]*?)\s*</\1>", body):
            into.setdefault(k, v)


def _pom_java(root: Path) -> tuple[str, str] | None:
    """pom.xml 의 Java 버전 — 주석은 빼고, ${속성} 은 이 pom 과 부모 pom 의 properties 로 풀어서. (주석 처리한 옛 값이 이기거나,
    속성 이름이 java.version 이 아니면 못 읽었다.)"""
    pom = root / "pom.xml"
    texts = []
    for _ in range(4):  # 이 pom → <parent><relativePath>(기본 ../pom.xml) 의 부모 pom
        t = re.sub(r"<!--.*?-->", "", _read(pom), flags=re.S)
        if not t:
            break
        texts.append(t)
        par = re.search(r"<parent>(.*?)</parent>", t, re.S)
        if not par:
            break
        if re.search(r"<relativePath\s*/>", par.group(1)):
            break  # 빈 relativePath — 부모를 저장소(원격)에서 찾는다는 뜻
        rel = re.search(r"<relativePath>\s*([^<]*?)\s*</relativePath>", par.group(1))
        if rel and not rel.group(1):
            break
        nxt = pom.parent / (rel.group(1) if rel else "..")
        nxt = nxt / "pom.xml" if not str(nxt).endswith(".xml") else nxt
        if not nxt.is_file() or nxt.resolve() == pom.resolve():
            break
        pom = nxt
    if not texts:
        return None
    props: dict = {}
    for t in texts:
        _pom_props(t, props)

    def val(s: str) -> str:
        for _ in range(5):
            m = re.fullmatch(r"\$\{([\w.\-]+)\}", s.strip())
            if not m:
                break
            s = props.get(m.group(1), "")
        return s
    for t in texts:
        for rx in (r"<maven\.compiler\.release>\s*([^<]+)<", r"<release>\s*([^<]+)</release>", r"<java\.version>\s*([^<]+)<",
                   r"<maven\.compiler\.source>\s*([^<]+)<", r"<maven\.compiler\.target>\s*([^<]+)<",
                   r"<source>\s*([^<]+)</source>", r"<target>\s*([^<]+)</target>"):
            m = re.search(rx, t)
            if m:
                major = _java_major(val(m.group(1)))
                if major and re.fullmatch(r"\d+", major):
                    return major, "pom.xml"
    return None


def _gradle_java(root: Path) -> dict | None:
    """Gradle 의 Java — toolchain(정확히 그 JDK)이 어느 파일에 있든 compat(그 이상)보다 앞선다. 이 프로젝트 → 하위 모듈 →
    buildSrc → (하위 프로젝트면) settings.gradle 이 있는 맨 위."""
    files = []
    tops = [root]
    s = _up(root, "settings.gradle", "settings.gradle.kts")
    if s and s.parent != root:
        tops.append(s.parent)
    for top in tops:
        for g in ("build.gradle", "build.gradle.kts"):
            files.append(top / g)
        files += sorted(top.glob("*/build.gradle")) + sorted(top.glob("*/build.gradle.kts"))
        files += sorted((top / "buildSrc" / "src" / "main").glob("*/*.gradle*"))
    texts = [(f, _read(f)) for f in files if f.is_file()]
    for f, text in texts:
        m = re.search(r"JavaLanguageVersion\.of\(\s*(\d+)", text) or re.search(r"jvmToolchain\(\s*(\d+)", text)
        if m:
            return {"spec": m.group(1), "why": f"{f.name} toolchain"}
    for f, text in texts:
        m = (re.search(r"JavaVersion\.VERSION_(?:1_)?(\d+)", text)
             or re.search(r"(?:source|target)Compatibility\s*=\s*['\"]?(?:1\.)?(\d+)", text))
        if m:
            return {"spec": f">={m.group(1)}", "why": f.name}
    return None


def _tool_versions(root: Path) -> dict[str, str]:
    f = _up(root, ".tool-versions")
    out = {}
    for line in _read(f).splitlines() if f else []:
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2:
            out[parts[0].lower()] = parts[1]
    return out


_SKIP_GLOB = {"node_modules", "bin", "obj", ".git", ".beom", "dist", "build", "target", ".venv", "venv"}


def _csprojs(root: Path) -> list[Path]:
    out = list(root.glob("*.csproj")) + list(root.glob("*.fsproj"))
    for d in [x for x in root.iterdir() if x.is_dir() and x.name not in _SKIP_GLOB] if root.is_dir() else []:
        out += list(d.glob("*.csproj")) + list(d.glob("*.fsproj"))
        for d2 in [x for x in d.iterdir() if x.is_dir() and x.name not in _SKIP_GLOB]:
            out += list(d2.glob("*.csproj")) + list(d2.glob("*.fsproj"))
            if len(out) > 60:
                return out
    return out


def _dotnet_spec(version: str, roll: str | None) -> str:
    """global.json 의 sdk.version 과 rollForward → 버전 조건. 기본(latestPatch)은 같은 기능 대역(8.0.1xx)만 된다 — 그 채널의 최신
    SDK(8.0.4xx)를 받으면 'A compatible .NET SDK was not found' 로 멈췄다."""
    n = [int(x) for x in re.findall(r"\d+", version)[:3]]
    if len(n) < 3:
        return ".".join(str(x) for x in n[:2]) or version
    roll = (roll or "latestPatch").strip()
    if roll in ("patch", "latestPatch"):
        return f"{n[0]}.{n[1]}.{n[2] // 100}xx"
    if roll in ("feature", "latestFeature"):
        return f"~{n[0]}.{n[1]}.{n[2]}"
    if roll in ("minor", "latestMinor"):
        return f"^{n[0]}.{n[1]}.{n[2]}"
    if roll in ("major", "latestMajor"):
        return f">={n[0]}.{n[1]}.{n[2]}"
    return f"={n[0]}.{n[1]}.{n[2]}"  # disable — 정확히 그 SDK


def required(root: Path) -> dict[str, dict]:
    """{도구: {"spec": "21" | ">=20" | "8.0" | "8.0.1xx" | "3.9.6", "why": "어디에 적혀 있나"}} — 적혀 있는 것만."""
    root = Path(root)
    out: dict[str, dict] = {}
    tv = _tool_versions(root)
    # Java — pom.xml(부모 pom 포함), Gradle, .java-version, .sdkmanrc, .tool-versions
    pj_java = _pom_java(root)
    if pj_java:
        out["java"] = {"spec": f">={pj_java[0]}", "why": pj_java[1]}
    g = _gradle_java(root)
    if g:
        out["java"] = g
    f = _up(root, ".java-version")
    m = _java_major(_read(f)) if f else None
    if not m:
        f = _up(root, ".sdkmanrc")
        mm = re.search(r"^\s*java\s*=\s*(\S+)", _read(f), re.M) if f else None
        m = _java_major(mm.group(1)) if mm else None
    if not m and tv.get("java"):
        f, m = Path(".tool-versions"), _java_major(tv["java"])
    if m and f:
        out["java"] = {"spec": m, "why": f.name}
    # Node — .nvmrc, .node-version (위 폴더까지), .tool-versions, volta, engines
    pj = {}
    try:
        pj = json.loads(_read(root / "package.json") or "{}")
    except ValueError:
        pass
    f = _up(root, ".nvmrc", ".node-version")
    spec = _node_spec(_read(f)) if f else None
    if spec:
        out["node"] = {"spec": spec, "why": f.name}
    elif tv.get("nodejs") and _node_spec(tv["nodejs"]):
        out["node"] = {"spec": _node_spec(tv["nodejs"]), "why": ".tool-versions"}
    elif isinstance(pj, dict):
        v = ((pj.get("volta") or {}).get("node") if isinstance(pj.get("volta"), dict) else None) or \
            ((pj.get("engines") or {}).get("node") if isinstance(pj.get("engines"), dict) else None)
        if v and not _any_spec(str(v)):
            out["node"] = {"spec": str(v), "why": "package.json"}
    # Go — go.mod 의 go/toolchain (Go 1.21+ 는 go.mod 의 toolchain 을 스스로 받는다 — 1.21 이상이면 된다)
    gm = _read(root / "go.mod")
    m = re.search(r"^toolchain\s+go(\d+\.\d+(?:\.\d+)?)", gm, re.M) or re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)", gm, re.M)
    if m:
        out["go"] = {"spec": f">={m.group(1)}", "why": "go.mod"}
    elif tv.get("golang") and re.match(r"\d", tv["golang"]):
        out["go"] = {"spec": tv["golang"], "why": ".tool-versions"}
    # .NET — global.json(위 폴더까지 — dotnet 도 그렇게 찾는다) sdk.version·rollForward, 그다음 TargetFramework(s)
    f = _up(root, "global.json")
    try:
        gj = json.loads(_read(f) or "{}") if f else {}
        sdk = (gj.get("sdk") or {}) if isinstance(gj, dict) else {}
        v = sdk.get("version") if isinstance(sdk, dict) else None
    except ValueError:
        v, sdk = None, {}
    if v:
        out["dotnet"] = {"spec": _dotnet_spec(str(v), sdk.get("rollForward")), "why": "global.json"}
    else:
        tfs = []
        for p in _csprojs(root):
            for body in re.findall(r"<TargetFrameworks?>([^<]*)<", _read(p)):  # net6.0;net8.0 — 모두 읽어 가장 높은 것
                tfs += [int(x) for x in re.findall(r"net(\d+)\.\d", body)]
        if tfs:  # net8.0 테스트는 .NET 8 런타임에서 돈다 — 더 새 SDK(10)는 빌드만 되고 테스트 실행이 멈췄다. 그 채널을 정확히
            out["dotnet"] = {"spec": f"{max(tfs)}.0", "why": "*.csproj TargetFramework"}
        elif re.match(r"\d", tv.get("dotnet", "") or tv.get("dotnet-core", "")):
            out["dotnet"] = {"spec": ".".join((tv.get("dotnet") or tv["dotnet-core"]).split(".")[:2]), "why": ".tool-versions"}
    # Python — .python-version(위 폴더까지), requires-python, .tool-versions
    f = _up(root, ".python-version")
    v = _read(f).strip().splitlines()[0].strip() if f and _read(f).strip() else ""
    if re.match(r"\d+\.\d+", v):
        out["python"] = {"spec": ".".join(v.split(".")[:2]), "why": ".python-version"}
    else:
        m = re.search(r"requires-python\s*=\s*['\"]([^'\"]+)", _read(root / "pyproject.toml"))
        if m:
            out["python"] = {"spec": m.group(1), "why": "pyproject.toml requires-python"}
        elif re.match(r"\d+\.\d+", tv.get("python", "")):
            out["python"] = {"spec": ".".join(tv["python"].split(".")[:2]), "why": ".tool-versions"}
    # 래퍼 설정 — 래퍼 스크립트가 없어도 설정이 정한 Maven·Gradle 버전을 쓴다 (다중 모듈이면 맨 위에 있다)
    f = _up(root, "gradle/wrapper/gradle-wrapper.properties")
    m = re.search(r"distributionUrl=\S*gradle-(\d+(?:\.\d+)+)-(?:bin|all)\.zip", _read(f)) if f else None
    if m:
        out["gradle"] = {"spec": m.group(1), "why": "gradle-wrapper.properties"}
    f = _up(root, ".mvn/wrapper/maven-wrapper.properties")
    m = re.search(r"distributionUrl=\S*apache-maven-(\d+(?:\.\d+)+)-bin\.zip", _read(f)) if f else None
    if m:
        out["mvn"] = {"spec": m.group(1), "why": "maven-wrapper.properties"}
    return out


def _any_spec(spec: str) -> bool:
    """'*' · 'x' · 'latest' · '' — 아무 버전이나 된다는 뜻."""
    return str(spec).strip().lower() in ("", "*", "x", "latest", "any", ">=0", "lts/*")


def uses(root: Path) -> set[str]:
    """이 프로젝트가 쓰는 받을 수 있는 도구 (버전이 적혀 있지 않아도) — 받아 둔 것을 PATH 에 두려면 무엇을 쓰는지 알아야 한다."""
    root = Path(root)
    out = set()
    if (root / "package.json").is_file():
        out.add("node")
    if any((root / n).is_file() for n in ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        out.add("java")
    if (root / "go.mod").is_file():
        out.add("go")
    if list(root.glob("*.sln")) or list(root.glob("*.slnx")) or _csprojs(root):
        out.add("dotnet")
    return out


# ---------------------------------------------------------------- 버전 비교


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(s))[:3]) or (0,)


def satisfies(version: str, spec: str) -> bool:
    """흔한 꼴만 — '21'(그 주 버전), '>=20', '^18', '~1.2', '18.x', '20 || 22', '>=18 <21', 'lts' 는 따로.
    파이썬(PEP 440)의 '~=3.11'(3.11 이상 4 미만)·'==3.11.*'·'!=3.9.0'·'>=3.10,<3.13' 도 — '!=' 를 '=' 로 읽어 '정확히 그 버전'을
    요구하는 거꾸로 된 판단이 됐었다."""
    v = _ver(version)
    if _any_spec(spec):
        return True
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d)xx\s*", str(spec))
    if m:  # .NET SDK 기능 대역 — 8.0.1xx 는 8.0.100~8.0.199
        a, b, c = (int(x) for x in m.groups())
        return len(v) >= 3 and v[:2] == (a, b) and v[2] // 100 == c
    for alt in str(spec).split("||"):
        ok = True
        hy = re.fullmatch(r"\s*v?(\d+(?:\.\d+)*)\s+-\s+v?(\d+(?:\.\d+)*)\s*", alt)
        if hy:  # '18 - 20' — 18 이상 20.x 까지
            lo, hi = _ver(hy.group(1)), _ver(hy.group(2))
            if v >= lo and v[:len(hi)] <= hi:
                return True
            continue
        parts = re.findall(r"(~=|===|==|!=|>=|<=|>|<|\^|~|=)?\s*v?(\d+(?:\.(?:\d+|x|\*))*)", alt)
        if not parts:
            continue
        for op, num in parts:
            n = tuple(int(x) for x in re.findall(r"\d+", num))
            if op == "~=":  # 마지막 자리만 올라갈 수 있다 — ~=3.11 은 3.11 이상 4 미만, ~=3.11.2 는 3.11.2 이상 3.12 미만
                ok &= v >= n and v[:max(len(n) - 1, 1)] == n[:max(len(n) - 1, 1)]
            elif op == "!=":
                ok &= v[:len(n)] != n
            elif op == ">=":
                ok &= v >= n
            elif op == ">":  # '>18' 은 19 이상 — 18.0.0 을 18 보다 크다고 보지 않는다
                ok &= v[:len(n)] > n
            elif op == "<=":
                ok &= v[:len(n)] <= n
            elif op == "<":
                ok &= v < n
            elif op == "^":
                ok &= v >= n and v[0] == n[0]
            elif op == "~":  # ~1.2.3 · ~1.2 → 1.2.x, ~20 → 20.x
                k = min(len(n), 2)
                ok &= v >= n and v[:k] == n[:k]
            else:  # '21' · '18.x' · '8.0' — 적힌 자리까지 같으면
                ok &= v[:len(n)] == n
        if ok:
            return True
    return False


def installed(tool: str) -> str | None:
    """시스템(또는 이미 붙인 PATH)에 있는 그 도구의 버전."""
    cmds = {"java": [_java_bin("javac"), "-version"], "node": ["node", "-v"], "go": ["go", "version"],
            "dotnet": ["dotnet", "--list-sdks"], "python": [_user_python(), "--version"], "mvn": ["mvn", "-v"], "gradle": ["gradle", "-v"]}
    argv = cmds.get(tool)
    if not argv or not shutil.which(argv[0]) and not Path(argv[0]).is_file():
        return None
    try:
        r = subprocess.run(argv, capture_output=True, timeout=60, shell=os.name == "nt" and tool in ("mvn", "gradle"))
    except (OSError, subprocess.SubprocessError):
        return None
    text = (r.stdout + r.stderr).decode("utf-8", "replace")
    if tool == "dotnet":  # 가장 높은 SDK
        vs = re.findall(r"^(\d+\.\d+\.\d+)", text, re.M)
        return max(vs, key=_ver) if vs else None
    m = re.search(r"(\d+(?:\.\d+)+)", text)
    if m and tool == "java" and m.group(1).startswith("1."):
        return m.group(1)[2:]  # 1.8.0_392 → 8.0 (Java 8 은 1.x 로 적는다 — '>=8' 을 영영 못 채워 다시 받았다)
    return m.group(1) if m else None


def _user_python() -> str:
    """사용자가 치는 python (Windows 스토어 바로가기는 아니다), 없으면 툴킷의 파이썬."""
    p = shutil.which("python") or shutil.which("python3")
    if p and sys.platform == "darwin" and os.path.realpath(p) == "/usr/bin/python3":
        # macOS 의 /usr/bin/python3 는 개발 도구(Command Line Tools)가 없으면 설치 창을 띄우는 껍데기다
        import subprocess
        try:
            if subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=10).returncode != 0:
                return sys.executable
        except (OSError, subprocess.SubprocessError):
            return sys.executable
    return p if p and "WindowsApps" not in p else sys.executable


_PYV: dict[str, str | None] = {}


def _py_version(exe: str) -> str | None:
    key = os.path.normcase(exe)
    if key not in _PYV:
        try:
            r = subprocess.run([exe, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"], capture_output=True, timeout=30)
            _PYV[key] = r.stdout.decode().strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            _PYV[key] = None
    return _PYV[key]


_PYC: list = []


def _python_candidates() -> list[str]:
    """이 PC 의 파이썬들 — PATH 의 python, py 런처가 아는 설치들(Windows), 툴킷의 파이썬 (1분 기억 — 명령마다 py 를 띄우지 않게)."""
    import time
    if _PYC and time.monotonic() - _PYC[0] < 60:
        return _PYC[1]
    found = _scan_pythons()
    _PYC[:] = [time.monotonic(), found]
    return found


def _scan_pythons() -> list[str]:
    out = [_user_python()]
    if os.name == "nt" and shutil.which("py"):
        try:
            r = subprocess.run(["py", "-0p"], capture_output=True, timeout=30)
            out += re.findall(r"^\s*-V?:?\S+\s+\*?\s*(\S.*?python[w]?\.exe)\s*$", r.stdout.decode("utf-8", "replace"), re.M | re.I)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        for minor in range(20, 7, -1):
            p = shutil.which(f"python3.{minor}")
            if p:
                out.append(p)
    out.append(sys.executable)
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen and Path(p).is_file():
            seen.add(k)
            uniq.append(p)
    return uniq


def python_for(root: Path) -> str | None:
    """이 프로젝트에 쓸 파이썬 — 사용자 가상 환경(.venv)이 있으면 그것, 없으면 요구 버전(.python-version·requires-python)을
    만족하는 것 중 PATH 의 python 을 먼저. 맞는 것이 없으면 None. 버전 확인과 가상 환경 만들기가 이 하나를 본다 — 확인은 툴킷의
    파이썬으로, 만들기는 PATH 의 python 으로 해서 서로 다른 파이썬을 봤었다."""
    from beom_workbench.core import deps
    spec = (required(root).get("python") or {}).get("spec")
    user = deps.user_venv(root)
    cands = ([str(user / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))] if user else []) + _python_candidates()
    for exe in cands:
        v = _py_version(exe)
        if v and (not spec or satisfies(v, spec)):
            return exe
    return None


def _java_bin(name: str) -> str:
    jh = os.environ.get("JAVA_HOME")
    if jh:
        for s in (".exe", ""):
            if (Path(jh) / "bin" / f"{name}{s}").is_file():
                return str(Path(jh) / "bin" / f"{name}{s}")
    return name


# ---------------------------------------------------------------- 툴킷 폴더에 받아 둔 것


def home() -> Path:
    from beom_workbench.config import load_settings
    return load_settings().home / "tools"


def _managed(tool: str) -> list[tuple[str, Path]]:
    """툴킷 폴더에 받아 둔 그 도구 [(버전, 폴더)]."""
    base = home() / tool
    out = []
    try:
        for d in base.iterdir():
            if (d / ".ok").is_file():
                out.append(((d / ".ok").read_text(encoding="utf-8").strip(), d))
    except OSError:
        pass
    return sorted(out, key=lambda x: _ver(x[0]), reverse=True)


def pick(tool: str, spec: str) -> tuple[str, Path] | None:
    """요구를 만족하는 받아 둔 것 (가장 높은 것)."""
    for v, d in _managed(tool):
        if spec == "lts" or satisfies(v, spec):
            return v, d
    return None


def _bin(tool: str, d: Path) -> Path:
    inner = next((x for x in d.iterdir() if x.is_dir() and x.name != "__MACOSX"), d) if tool in ("java", "go", "node") else d
    if tool == "java":
        inner = inner / "Contents" / "Home" if (inner / "Contents" / "Home").is_dir() else inner
        return inner / "bin"
    if tool == "go":
        return inner / "bin"
    if tool == "node":
        return inner if os.name == "nt" else inner / "bin"
    return d  # dotnet: 맨 위에 dotnet.exe


def choose(tool: str, root: Path) -> tuple[str, str, Path | None] | None:
    """이 프로젝트에서 그 도구로 무엇을 쓰나 — ("system", 버전, None) · ("managed", 버전, 폴더) · None(맞는 것이 없다).
    준비됐는지(ok)와 PATH 에 무엇을 두는지(env_for)가 이 하나를 본다 — 따로 판단해서, 버전이 적혀 있지 않은 프로젝트에 받아 둔
    Node 는 '있다'고 하고 PATH 에는 두지 않아 'npm 을 찾을 수 없음'으로 멈췄다."""
    spec = (required(root).get(tool) or {}).get("spec")
    if tool == "dotnet":
        sdks = _dotnet_sdks()
        if spec and re.fullmatch(r"\d+\.\d+", spec) and sdks and _ver(max(sdks, key=_ver)) >= _ver(spec) and _dotnet_runtime(spec):
            return "system", max(sdks, key=_ver), None  # 더 새 SDK 와 그 런타임이 함께 깔려 있다
        hit = next((v for v in sorted(sdks, key=_ver, reverse=True) if not spec or spec == "lts" or satisfies(v, spec)), None)
        if hit:
            return "system", hit, None
    else:
        sysv = _sys_cache(tool)
        if sysv and (not spec or spec == "lts" or satisfies(sysv, spec)):
            return "system", sysv, None
    if tool in FETCH:
        got = pick(tool, spec or ">=0")
        if got:
            return "managed", got[0], got[1]
    return None


def env_for(root: Path, base: dict | None = None) -> dict:
    """이 프로젝트가 쓰는 도구(버전이 적혀 있든 아니든)에 맞는 시스템 것이 없고 받아 둔 것이 있으면 그것을 PATH 맨 앞에
    (JAVA_HOME·DOTNET_ROOT 도). 시스템 JDK 를 쓰면 JAVA_HOME 의 bin 을 앞에 — java 와 javac 가 다른 설치에서 오지 않게. 바꿀 것만 돌려준다."""
    env = dict(base or {})
    src = base if base is not None else os.environ
    path_key = next((k for k in src if k.upper() == "PATH"), "PATH")
    front = []
    for tool in sorted(set(required(root)) & set(FETCH) | uses(root)):
        c = choose(tool, root)
        if not c:
            continue
        if c[0] == "system":
            jh = src.get("JAVA_HOME") or os.environ.get("JAVA_HOME")
            if tool == "java" and jh and Path(jh, "bin").is_dir():
                front.append(str(Path(jh, "bin")))
            continue
        b = _bin(tool, c[2])
        front.append(str(b))
        if tool == "java":
            env["JAVA_HOME"] = str(b.parent)
        if tool == "dotnet":
            env["DOTNET_ROOT"] = str(c[2])
            env["DOTNET_MULTILEVEL_LOOKUP"] = "0"
            # 첫 실행 안내·원격 측정을 끈다 (안내를 쓰려다 사용자 폴더에서 멈추지 않게, 사용 기록을 보내지 않게)
            env.update(DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1", DOTNET_NOLOGO="1", DOTNET_CLI_TELEMETRY_OPTOUT="1")
    if front:
        env[path_key] = os.pathsep.join(front + [src.get(path_key, "")])
    if (Path(root) / "package.json").is_file():
        env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"  # 비대화식에서 '내려받을까요?' 를 묻다 멈추지 않게 (packageManager 의 버전을 받는다)
    return env


_SYS: dict[str, tuple[float, str | None]] = {}


def _sys_cache(tool: str) -> str | None:
    """시스템 버전 — 명령마다 프로세스를 띄우지 않게 잠깐 기억하되(1분), 앱이 켜진 동안 사용자가 설치한 것도 곧 보이게."""
    import time
    hit = _SYS.get(tool)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    v = installed(tool)
    _SYS[tool] = (time.monotonic(), v)
    return v


def _dotnet_sdks() -> list[str]:
    """시스템 dotnet 의 SDK 들 — global.json 은 가장 높은 것이 아니라 그 대역의 것을 고른다 (1분 기억)."""
    import time
    hit = _SYS.get("dotnet-sdks")
    if hit and time.monotonic() - hit[0] < 60:
        return list(hit[1] or [])
    vs: list[str] = []
    if shutil.which("dotnet"):
        try:
            r = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, timeout=30)
            vs = re.findall(r"^(\d+\.\d+\.\d+)", r.stdout.decode("utf-8", "replace"), re.M)
        except (OSError, subprocess.SubprocessError):
            pass
    _SYS["dotnet-sdks"] = (time.monotonic(), vs)  # type: ignore[assignment]
    return vs


def _dotnet_runtime(major_minor: str) -> bool:
    """시스템 dotnet 에 그 런타임(Microsoft.NETCore.App X.Y)이 있나 — 더 새 SDK 와 함께 깔려 있으면 따로 받지 않는다."""
    if not shutil.which("dotnet"):
        return False
    try:
        r = subprocess.run(["dotnet", "--list-runtimes"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(re.search(r"^Microsoft\.NETCore\.App " + re.escape(major_minor) + r"\.", r.stdout.decode("utf-8", "replace"), re.M))


def ok(tool: str, root: Path) -> bool:
    """이 프로젝트에 그 도구가 준비됐나 — 요구 버전이 있으면 그것을 만족하는 시스템 것이나 받아 둔 것, 없으면 아무 버전."""
    if tool == "python":
        return python_for(root) is not None
    return choose(tool, root) is not None


def missing(root: Path) -> list[dict]:
    """요구 버전이 적혀 있는데 시스템에도 툴킷 폴더에도 맞는 것이 없는 도구 [{tool, spec, why, have}]."""
    return [{"tool": t, **req, "have": _sys_cache(t)} for t, req in required(root).items()
            if t in (*FETCH, "python") and not ok(t, root)]


def forget() -> None:
    """설치한 뒤 — 시스템 버전을 다시 본다."""
    _SYS.clear()
    _PYV.clear()
    _PYC.clear()


# ---------------------------------------------------------------- 공식 배포처에서 받기


def _get(url: str, timeout: float = 60) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "beom-workbench"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — 아래 고정한 공식 주소만
        return r.read()


def _resolve_java(spec: str) -> dict:
    rel = json.loads(_get("https://api.adoptium.net/v3/info/available_releases"))
    want = _ver(spec)[0] if spec != "lts" else rel["most_recent_lts"]
    lts = rel.get("available_lts_releases") or []
    cands = [v for v in rel.get("available_releases") or [] if v >= want]
    # '>=17' 이면 그 이상의 LTS 를 먼저 (오래 지원), 정확히 '17' 이면 17
    exact = not str(spec).startswith(">")
    major = want if exact else next((v for v in lts if v >= want), cands[0] if cands else want)
    data = json.loads(_get(f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot?architecture={_arch()}&image_type=jdk"
                           f"&os={_os()}&vendor=eclipse"))
    pkg = data[0]["binary"]["package"]
    return {"version": data[0]["version"]["semver"].split("+")[0], "url": pkg["link"], "sha256": pkg["checksum"]}


def _resolve_node(spec: str) -> dict:
    idx = json.loads(_get("https://nodejs.org/dist/index.json"))
    ok = [r for r in idx if not re.search(r"-(?:alpha|beta|rc|nightly)", r["version"])]
    if spec == "lts":
        choice = next(r for r in ok if r.get("lts"))
    else:
        lts = [r for r in ok if r.get("lts") and satisfies(r["version"], spec)]
        choice = (lts or [r for r in ok if satisfies(r["version"], spec)])[0]
    v = choice["version"]
    plat = {"windows": "win", "mac": "darwin", "linux": "linux"}[_os()]
    arch = "arm64" if _arch() == "aarch64" else "x64"
    name = f"node-{v}-{plat}-{arch}." + ("zip" if _os() == "windows" else "tar.gz")
    sums = _get(f"https://nodejs.org/dist/{v}/SHASUMS256.txt").decode()
    sha = next(line.split()[0] for line in sums.splitlines() if line.endswith(" " + name))
    return {"version": v.lstrip("v"), "url": f"https://nodejs.org/dist/{v}/{name}", "sha256": sha}


def _resolve_go(spec: str) -> dict:
    rels = json.loads(_get("https://go.dev/dl/?mode=json&include=all"))
    stable = [r for r in rels if r.get("stable")]
    choice = next(r for r in stable if spec == "lts" or satisfies(r["version"].lstrip("go"), spec))
    goarch = "arm64" if _arch() == "aarch64" else "amd64"
    goos = {"windows": "windows", "mac": "darwin", "linux": "linux"}[_os()]
    f = next(x for x in choice["files"] if x["os"] == goos and x["arch"] == goarch and x["kind"] == "archive")
    return {"version": choice["version"].lstrip("go"), "url": "https://go.dev/dl/" + f["filename"], "sha256": f["sha256"]}


def _resolve_dotnet(spec: str) -> dict:
    idx = json.loads(_get("https://builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json"))
    chans = [c for c in idx["releases-index"] if c.get("support-phase") not in ("preview", "go-live")]
    want = _ver(spec)
    if spec == "lts":
        ok = sorted([c for c in chans if c.get("release-type") == "lts" and c.get("support-phase") == "active"],
                    key=lambda c: _ver(c["channel-version"]), reverse=True)
    elif str(spec).startswith(">"):  # 그 이상에서 지원 중인 것 중 가장 낮은 LTS → 없으면 가장 낮은 것
        ok = [c for c in chans if _ver(c["channel-version"]) >= want and c.get("support-phase") == "active"]
        ok = sorted(ok, key=lambda c: (c.get("release-type") != "lts", _ver(c["channel-version"])))
    else:  # '8.0' · '8.0.1xx' · '~8.0.100' · '=8.0.100' — 채널은 앞 두 자리
        ok = [c for c in chans if _ver(c["channel-version"])[:2] == want[:2]]
    chan = ok[0]
    rel = json.loads(_get(chan["releases.json"]))
    sdks = [x for r in rel["releases"] for x in (r.get("sdks") or [r.get("sdk")]) if x and x.get("files")]
    exact_chan = re.fullmatch(r"\d+\.\d+", str(spec)) or spec == "lts" or str(spec).startswith(">")
    sdk = next((x for x in sdks if exact_chan or satisfies(x["version"], spec)), None)
    if sdk is None:
        raise ValueError(f"{chan['channel-version']} 채널에 '{spec}' 에 맞는 SDK 가 없음")
    rid = {"windows": "win", "mac": "osx", "linux": "linux"}[_os()] + "-" + ("arm64" if _arch() == "aarch64" else "x64")
    f = next(x for x in sdk["files"] if x.get("rid") == rid and x["name"].endswith((".zip", ".tar.gz")))
    return {"version": sdk["version"], "url": f["url"], "sha512": f["hash"]}


FETCH = {"java": _resolve_java, "node": _resolve_node, "go": _resolve_go, "dotnet": _resolve_dotnet}
NAMES = {"java": "JDK", "node": "Node.js", "go": "Go", "dotnet": ".NET SDK", "python": "Python"}


def describe(tool: str, spec: str) -> str:
    src = {"java": "Adoptium(Eclipse Temurin)", "node": "nodejs.org", "go": "go.dev", "dotnet": "Microsoft .NET 릴리스"}.get(tool, "")
    return f"{NAMES.get(tool, tool)} ({spec}) 공식 배포본({src})을 받아 체크섬을 확인하고 툴킷 폴더에 둠 — 시스템에는 설치하지 않음"


def fetch(tool: str, spec: str, timeout: float = 1800) -> dict:
    """요구에 맞는 버전을 받아 툴킷 폴더에 — {ok, version, error?}."""
    try:
        info = FETCH[tool](spec)
    except Exception as e:  # noqa: BLE001 — 네트워크·형식 오류는 사람에게 알린다
        return {"ok": False, "error": f"버전 정보를 못 읽음: {type(e).__name__}: {e}"[:300]}
    dest = home() / tool / info["version"]
    if (dest / ".ok").is_file():
        return {"ok": True, "version": info["version"]}
    dest.parent.mkdir(parents=True, exist_ok=True)
    lock = dest.with_name(dest.name + ".lock")
    import time
    deadline = time.monotonic() + timeout
    while True:  # 두 실행이 같은 버전을 함께 받으면 서로의 .part 를 지운다 — 하나가 받는 동안 다른 하나는 기다렸다가 그 결과를 쓴다
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            if (dest / ".ok").is_file():
                return {"ok": True, "version": info["version"]}
            if _lock_stale(lock):
                lock.unlink(missing_ok=True)  # 받던 쪽이 멈췄거나 죽었다 — 30분을 기다리지 않는다
                continue
            if time.monotonic() > deadline:
                return {"ok": False, "error": f"다른 실행이 {NAMES[tool]} {info['version']} 을(를) 받는 중이라 기다리다 멈춤"}
            time.sleep(1)
    try:
        _sweep(dest.parent)
        return _fetch_locked(tool, info, dest, timeout, lock)
    finally:
        lock.unlink(missing_ok=True)


LOCK_BEAT = 15  # 받는 동안 잠금 파일을 이만큼마다 새로 — 이보다 훨씬 오래 그대로면 받던 쪽이 멈춘 것


def _lock_stale(lock: Path) -> bool:
    import time
    try:
        age = time.time() - lock.stat().st_mtime
        pid = int((lock.read_text(encoding="utf-8") or "0").strip() or 0)
    except (OSError, ValueError):
        return False
    return age > LOCK_BEAT * 8 or (pid > 0 and age > 5 and not _alive(pid))


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep(folder: Path) -> None:
    """멈춘 실행이 남긴 받다 만 것(tmp*·*.part) — 잠금이 없는 것만, 한 시간 지난 것만 치운다 (한 번에 수백 MB씩 쌓였다)."""
    import time
    try:
        for p in folder.iterdir():
            if not (p.name.startswith("tmp") or p.name.endswith(".part")) or not p.is_dir():
                continue
            if p.name.endswith(".part") and p.with_name(p.name[:-5] + ".lock").exists():
                continue
            if time.time() - p.stat().st_mtime > 3600:
                shutil.rmtree(_long(p), ignore_errors=True)
    except OSError:
        pass


def _fetch_locked(tool: str, info: dict, dest: Path, timeout: float, lock: Path | None = None) -> dict:
    import hashlib
    import tarfile
    import tempfile
    import zipfile
    if (dest / ".ok").is_file():
        return {"ok": True, "version": info["version"]}
    algo = "sha512" if "sha512" in info else "sha256"
    h = hashlib.new(algo)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            arch = Path(tmp) / ("dist.zip" if info["url"].endswith(".zip") else "dist.tar.gz")
            req = urllib.request.Request(info["url"], headers={"User-Agent": "beom-workbench"})
            import time
            beat = time.monotonic()
            with urllib.request.urlopen(req, timeout=timeout) as r, arch.open("wb") as f:  # noqa: S310
                while chunk := r.read(1 << 20):
                    h.update(chunk)
                    f.write(chunk)
                    if lock is not None and time.monotonic() - beat > LOCK_BEAT:
                        beat = time.monotonic()
                        try:
                            os.utime(lock)  # 아직 받는 중 — 기다리는 쪽이 멈춘 것으로 보지 않게
                        except OSError:
                            pass
            if h.hexdigest().lower() != str(info[algo]).lower():
                return {"ok": False, "error": f"{NAMES[tool]} {info['version']} 배포본의 체크섬이 맞지 않음 — 쓰지 않았다"}
            # 받은 자리 바로 옆에 풀고(임시 폴더 안에 한 겹 더 두지 않는다) Windows 는 긴 경로 접두사로 —
            # Node 배포본의 node_modules 는 깊어서 260자 한도를 넘었다
            part = dest.with_name(dest.name + ".part")
            shutil.rmtree(_long(part), ignore_errors=True)
            out = Path(_long(part))
            out.mkdir()
            if arch.suffix == ".zip":
                with zipfile.ZipFile(arch) as z:
                    if any(n.startswith(("/", "\\")) or ".." in Path(n).parts for n in z.namelist()):
                        return {"ok": False, "error": "압축 파일 안의 경로가 이상함"}
                    z.extractall(out)
            else:
                with tarfile.open(arch) as t:
                    if hasattr(tarfile, "data_filter"):
                        t.extractall(out, filter="data")
                    else:  # 3.11.4 전 — 폴더 밖으로 나가는 경로·링크만 거른다 (Node·JDK 배포본 안의 상대 링크는 정상)
                        top = Path(out).resolve()

                        def escapes(m) -> bool:
                            if m.name.startswith("/") or ".." in Path(m.name).parts:
                                return True
                            if m.issym():
                                return not (top / Path(m.name).parent / m.linkname).resolve().is_relative_to(top)
                            if m.islnk():
                                return not (top / m.linkname).resolve().is_relative_to(top)
                            return False
                        bad = [m for m in t.getmembers() if escapes(m)]
                        if bad:
                            return {"ok": False, "error": "압축 파일 안의 경로가 이상함"}
                        t.extractall(out)
            (out / ".ok").write_text(info["version"], encoding="utf-8")
            for i in range(10):  # 막 푼 폴더는 백신·색인이 잠깐 잡고 있어 이름 바꾸기가 '접근 거부'로 실패한다 — 다시 받지 않고 기다렸다 다시
                try:
                    os.replace(_long(part), _long(dest))
                    break
                except PermissionError:
                    if i == 9:
                        raise
                    time.sleep(0.5 * (i + 1))
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(_long(dest.with_name(dest.name + ".part")), ignore_errors=True)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    return {"ok": True, "version": info["version"]}


def _long(p: Path) -> str:
    """Windows 긴 경로 (앞에 붙이는 확장 경로 접두사) — 다른 OS 는 그대로."""
    s = str(Path(p).resolve())
    return "\\\\?\\" + s if os.name == "nt" and not s.startswith("\\\\?\\") else s
