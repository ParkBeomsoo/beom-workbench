"""빌드 준비물 — 대상 폴더의 저장소를 빌드·실행하는 데 필요한 도구가 이 PC 에 있는지, 작업을 시작하기 전에 본다.

에이전트가 라운드 중간에 "OpenSSL 이 없어 빌드를 못 한다"고 멈추는 대신, 폴더를 고를 때 무엇이 없는지 보여 주고 사람이 한 번에
설치하게 한다 (설치 명령은 각 도구의 공식 패키지 — Windows winget, macOS Homebrew). 저장소 여럿을 모은 폴더면 그 안을 모두 본다.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_SKIP = {"node_modules", ".git", ".beom", "target", "dist", "build", ".venv", "venv", "__pycache__", ".next"}

# id: (보여 줄 이름, 왜 필요한지, Windows 설치, macOS 설치, 그 밖 안내)
TOOLS = {
    "cargo": ("Rust (cargo)", "Rust 저장소 빌드", "winget install -e --id Rustlang.Rustup",
              "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y", "curl https://sh.rustup.rs -sSf | sh"),
    "msvc": ("Visual C++ 빌드 도구", "Windows 에서 Rust(MSVC) 가 링크하는 데 필요 (link.exe)",
             "winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override \"--wait --passive --add "
             "Microsoft.VisualStudio.Workload.VCTools --includeRecommended\"", None, None),
    "openssl": ("OpenSSL 개발 파일", "Rust 의 openssl-sys(HTTPS 통신) 빌드 — Windows 에는 기본으로 없다",
                "winget install -e --id ShiningLight.OpenSSL.Dev", "brew install openssl@3", "sudo apt-get install -y libssl-dev pkg-config"),
    "node": ("Node.js", "package.json 저장소 빌드·실행", "winget install -e --id OpenJS.NodeJS.LTS", "brew install node",
             "sudo apt-get install -y nodejs npm"),
    "java": ("JDK", "Maven·Gradle 저장소 빌드 (javac)", "winget install -e --id EclipseAdoptium.Temurin.21.JDK",
             "brew install --cask temurin@21", "sudo apt-get install -y default-jdk"),
    "go": ("Go", "go.mod 저장소 빌드", "winget install -e --id GoLang.Go", "brew install go", "sudo apt-get install -y golang-go"),
    "mvn": ("Maven", "pom.xml 프로젝트 빌드·테스트 (mvnw 래퍼가 없을 때)", None, None, None),
    "gradle": ("Gradle", "build.gradle 프로젝트 빌드·테스트 (gradlew 래퍼가 없을 때)", None, None, None),
    "dotnet": (".NET SDK", ".NET 프로젝트 빌드·테스트 (런타임만으로는 안 된다)", "winget install -e --id Microsoft.DotNet.SDK.10",
               "brew install --cask dotnet-sdk", "sudo apt-get install -y dotnet-sdk-10.0"),
}


def _repos(root: Path) -> list[Path]:
    from beom_workbench.core import workbranch
    if workbranch.repo_top(root):
        return [root]
    kids = workbranch.child_repos(root)
    return kids or [root]


# winget 에 공식 패키지가 없는 도구 — Apache·Gradle 공식 배포본을 버전을 고정해 받고 체크섬을 확인한 뒤 툴킷 폴더에 둔다
# (시스템에 설치하지 않는다 — 툴킷이 돌리는 명령과 에이전트의 PATH 에만 붙인다)
# 버전은 설치할 때 공식 정보에서 고른다 — 고정 버전은 네트워크가 안 될 때의 대안이다 (고정해 두면 새 JDK 에서 낡은 Gradle 이
# 돌지 않는다). Maven 메타데이터의 'release' 는 4.0 후보판(rc)일 수 있어 3.x 정식판 중 가장 높은 것을 고른다.
MANAGED = {
    "mvn": {"name": "Maven", "ver": "3.9.9", "dir": "apache-maven-3.9.9", "algo": "sha512", "prefix": "apache-maven-",
            "url": "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.zip"},
    "gradle": {"name": "Gradle", "ver": "8.14.3", "dir": "gradle-8.14.3", "algo": "sha256", "prefix": "gradle-",
               "url": "https://services.gradle.org/distributions/gradle-8.14.3-bin.zip"},
}
MAVEN_META = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/maven-metadata.xml"
GRADLE_CURRENT = "https://services.gradle.org/versions/current"


def _java_major() -> int | None:
    """Gradle·Maven 이 쓸 JDK 의 주 버전 — 그 스크립트처럼 JAVA_HOME 이 먼저, 없으면 PATH 의 java."""
    import subprocess
    jh = os.environ.get("JAVA_HOME")
    exe = str(Path(jh) / "bin" / "java") if jh and (Path(jh) / "bin").is_dir() else "java"
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', (r.stderr + r.stdout).decode("utf-8", "replace"))
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major  # 1.8 → 8


def resolve_managed(tool: str, version: str | None = None, java: int | None = None) -> dict:
    """설치할 버전 — 공식 정보에서 (Maven: 3.x 정식판 중 가장 높은 것, Gradle: 현재 정식판, 단 JDK 17 미만이면 8.x). 못 읽으면 고정 버전."""
    import json as _json
    import urllib.request
    m = dict(MANAGED[tool])
    if version and re.fullmatch(r"\d+(?:\.\d+){1,2}", version):  # 프로젝트의 래퍼 설정이 정한 버전
        if tool == "mvn":
            return {**m, "ver": version, "dir": f"apache-maven-{version}",
                    "url": f"https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{version}/apache-maven-{version}-bin.zip"}
        return {**m, "ver": version, "dir": f"gradle-{version}", "url": f"https://services.gradle.org/distributions/gradle-{version}-bin.zip"}
    try:
        if tool == "mvn":
            with urllib.request.urlopen(MAVEN_META, timeout=20) as r:  # noqa: S310 — 공식 주소
                vers = re.findall(r"<version>(3\.\d+\.\d+)</version>", r.read().decode("utf-8", "replace"))
            if vers:
                v = max(vers, key=lambda x: tuple(int(p) for p in x.split(".")))
                m.update(ver=v, dir=f"apache-maven-{v}",
                         url=f"https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{v}/apache-maven-{v}-bin.zip")
        # Gradle 9 는 JDK 17 이상에서 돈다 — 프로젝트가 쓸 JDK(java)로 본다. 시스템 JAVA_HOME 으로 보면 JDK 11 프로젝트에 Gradle 9 를 받았다
        elif tool == "gradle" and (java or _java_major() or 0) >= 17:
            with urllib.request.urlopen(GRADLE_CURRENT, timeout=20) as r:  # noqa: S310
                cur = _json.loads(r.read().decode("utf-8"))
            if cur.get("version") and cur.get("downloadUrl") and not cur.get("snapshot") and cur.get("final", True):
                m.update(ver=cur["version"], dir=f"gradle-{cur['version']}", url=cur["downloadUrl"])
    except (OSError, ValueError):
        pass  # 고정 버전으로
    return m


def tools_home() -> Path:
    from beom_workbench.config import load_settings
    return load_settings().home / "tools"


def _installed(tool: str) -> Path | None:
    """툴킷 폴더에 받아 둔 그 도구 (여러 버전이면 가장 높은 것)."""
    try:
        base = tools_home()
        found = [d for d in base.iterdir() if d.name.startswith(MANAGED[tool]["prefix"]) and (d / "bin").is_dir()]
    except (OSError, Exception):  # noqa: BLE001
        return None
    num = lambda d: tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", d.name[len(MANAGED[tool]["prefix"]):]))  # noqa: E731
    return max(found, key=num) if found else None


def managed_bins() -> list[str]:
    """툴킷 폴더에 받아 둔 도구의 bin — PATH 에 붙인다."""
    return [str(d / "bin") for d in (_installed(t) for t in MANAGED) if d]


def install_command(tool: str) -> str | None:
    """사람에게 보여 줄 설치 방법 한 줄."""
    if tool in MANAGED:
        m = MANAGED[tool]
        src = "Maven Central" if tool == "mvn" else "services.gradle.org"
        return f"{m['name']} 공식 배포본({src}, 정식판)을 받아 {m['algo']} 체크섬을 확인하고 툴킷 폴더에 둠 — 시스템에는 설치하지 않음"
    name, why, win, mac, other = TOOLS[tool]
    return win if sys.platform == "win32" else mac if sys.platform == "darwin" else other


def install(tool: str, timeout: float = 1800, version: str | None = None, java: int | None = None) -> dict:
    """도구를 설치한다 — Windows 는 winget 공식 패키지(약관 동의 포함, 관리자 확인 창이 뜰 수 있다), winget 에 없는 Maven·Gradle 은
    공식 배포본(체크섬 확인). macOS 는 Homebrew. Linux 는 관리자 암호가 필요해 직접 설치하게 안내한다. {ok, how, error?}"""
    import subprocess
    how = install_command(tool) or ""
    try:
        if tool in MANAGED:
            _install_managed(tool, timeout, version, java)
        elif sys.platform == "win32":
            win = TOOLS[tool][2]
            if not win or not shutil.which("winget"):
                return {"ok": False, "how": how, "error": "winget 이 없거나 이 도구의 winget 패키지가 없음 — 직접 설치해 주세요"}
            import shlex
            args = shlex.split(win, posix=False)
            pkg = args[args.index("--id") + 1]
            extra = [a.strip('"') for a in args[args.index("--override"):]] if "--override" in args else []
            r = subprocess.run(["winget", "install", "-e", "--id", pkg, "--silent", "--no-upgrade", "--accept-package-agreements",
                                "--accept-source-agreements", "--disable-interactivity", *extra], capture_output=True, timeout=timeout)
            if r.returncode != 0:  # 종료 코드의 뜻(이미 설치됨 등)에 기대지 않는다 — 아래에서 도구가 실제로 보이는지로 판단한다
                from beom_workbench.core.agents import refresh_path as _rp
                _rp(max_age=0)
                if not _has(tool):
                    tail = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()[-3:]
                    return {"ok": False, "how": how, "error": f"winget 설치 실패 (코드 {r.returncode}): {' / '.join(tail)}"[:400]}
        elif sys.platform == "darwin" and TOOLS[tool][3]:
            cmd = TOOLS[tool][3]
            if not shutil.which("brew"):
                return {"ok": False, "how": how, "error": "Homebrew(brew)가 없음 — https://brew.sh 에서 설치한 뒤 다시 누르거나, 직접 설치해 주세요"}
            if "--cask" in cmd:  # .pkg 설치는 관리자 암호를 묻는다 — 창 없이 돌리면 30분 동안 멈춰 있었다
                return {"ok": False, "how": how, "error": f"관리자 암호가 필요한 설치라 터미널에서 직접 실행해 주세요: {cmd}"}
            import shlex
            r = subprocess.run(shlex.split(cmd), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                return {"ok": False, "how": how, "error": (r.stdout + r.stderr).decode("utf-8", "replace")[-400:]}
            from beom_workbench.core.agents import refresh_path as _rp
            _rp(max_age=0)
        else:
            return {"ok": False, "how": how, "error": "이 운영체제에서는 관리자 암호가 필요해 툴킷이 설치하지 않음 — 직접 설치해 주세요"}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"ok": False, "how": how, "error": f"{type(e).__name__}: {e}"[:400]}
    from beom_workbench.core.agents import refresh_path
    refresh_path(max_age=0)
    return {"ok": _has(tool), "how": how, **({} if _has(tool) else {"error": "설치했지만 아직 찾지 못함 (앱을 다시 켜야 할 수 있음)"})}


def _install_managed(tool: str, timeout: float, version: str | None = None, java: int | None = None) -> None:
    import hashlib
    import tempfile
    import urllib.request
    import zipfile
    m = resolve_managed(tool, version, java)
    if (tools_home() / m["dir"] / "bin").is_dir() or (not version and _installed(tool)):
        return
    base = tools_home()
    base.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(m["url"] + "." + m["algo"], timeout=60) as r:  # noqa: S310 — 고정한 공식 주소
        want = r.read().decode("ascii", "replace").split()[0].strip().lower()
    h = hashlib.new(m["algo"])
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        zpath = Path(tmp) / "dist.zip"
        with urllib.request.urlopen(m["url"], timeout=timeout) as r, zpath.open("wb") as f:  # noqa: S310
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest().lower() != want:
            raise ValueError(f"{m['name']} 배포본의 체크섬이 맞지 않음 — 받은 파일을 쓰지 않았다")
        with zipfile.ZipFile(zpath) as z:
            for n in z.namelist():  # 압축 안의 경로가 폴더 밖을 가리키면 쓰지 않는다
                if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                    raise ValueError("압축 파일 안의 경로가 이상함")
            z.extractall(tmp)
        (Path(tmp) / m["dir"]).replace(base / m["dir"])
    if os.name != "nt":  # zip 은 실행 권한을 남기지 않는다 — bin 의 스크립트를 실행할 수 있게
        for f in (base / m["dir"] / "bin").iterdir():
            f.chmod(f.stat().st_mode | 0o755)


def _has(name: str) -> bool:
    if name == "java":
        jh = os.environ.get("JAVA_HOME")
        if jh and any((Path(jh) / "bin" / f"javac{s}").is_file() for s in ("", ".exe")):
            return True
        if sys.platform == "darwin":  # macOS 의 /usr/bin/javac 는 JDK 가 없어도 있는 껍데기 — 실행하면 설치 창을 띄운다
            import subprocess
            try:
                return subprocess.run(["/usr/libexec/java_home"], capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        return bool(shutil.which("javac"))
    if name == "dotnet":
        return _dotnet_sdk()
    if name == "msvc":
        return _msvc_linker()
    if shutil.which(name):
        return True
    home = Path.home()  # 설치기가 PATH 에 넣기 전·새 셸 전에도 흔한 기본 위치
    extra = {"cargo": home / ".cargo" / "bin", "go": home / "go" / "bin"}.get(name)
    return bool(extra and any((extra / f"{name}{s}").is_file() for s in ("", ".exe")))


def _dotnet_sdk() -> bool:
    """dotnet 명령은 런타임만 깔아도 생긴다 — SDK 가 하나라도 있어야 빌드·테스트가 된다."""
    import subprocess
    if not shutil.which("dotnet"):
        return False
    try:
        r = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and bool(r.stdout.decode("utf-8", "replace").strip())


def _msvc_linker() -> bool:
    """Windows 의 Rust(MSVC 대상)가 링크할 Visual C++ 빌드 도구 — 다른 OS 에서는 필요 없다."""
    import subprocess
    if sys.platform != "win32" or shutil.which("link"):
        return True
    vsw = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vsw.is_file():
        return False
    try:
        r = subprocess.run([str(vsw), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                            "-property", "installationPath"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(r.stdout.decode("utf-8", "replace").strip())


def _openssl_dir() -> str | None:
    """Windows 에서 openssl-sys 가 찾을 OpenSSL — OPENSSL_DIR, 흔한 설치 위치(winget·공식 설치본·vcpkg)."""
    if os.environ.get("OPENSSL_DIR"):
        return os.environ["OPENSSL_DIR"]
    if sys.platform != "win32":
        return "system"  # macOS·Linux 는 pkg-config·Homebrew 로 찾는다 (없으면 빌드 오류로 드러남)
    cands = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "OpenSSL-Win64", Path(r"C:\OpenSSL-Win64"),
             Path(os.environ.get("VCPKG_ROOT", r"C:\vcpkg")) / "installed" / "x64-windows"]
    return next((str(c) for c in cands if (c / "include" / "openssl" / "ssl.h").is_file()), None)


def needs(target_root: str | Path) -> list[dict]:
    """대상 폴더에 필요한데 이 PC 에 없는 빌드 도구 — [{id, name, why, install, repos}]. 다 있으면 빈 목록."""
    root = Path(target_root)
    if not root.is_dir():
        return []
    from beom_workbench.core.agents import refresh_path
    refresh_path()  # 앱을 켠 뒤 설치한 도구도 (저장된 PATH 를 다시 읽어) 찾게
    want: dict[str, list[Path]] = {}

    def add(tool: str, repo: Path) -> None:
        want.setdefault(tool, [])
        if repo not in want[tool]:
            want[tool].append(repo)

    for repo in _repos(root)[:40]:
        files = {p.name for p in repo.iterdir()} if repo.is_dir() else set()
        if "Cargo.toml" in files:
            add("cargo", repo)
            if sys.platform == "win32":
                add("msvc", repo)
            lock = repo / "Cargo.lock"
            try:
                text = lock.read_text(encoding="utf-8", errors="replace") if lock.is_file() else (repo / "Cargo.toml").read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if 'name = "openssl-sys"' in text or "openssl" in text and "vendored" not in text:
                add("openssl", repo)
        if "package.json" in files:
            add("node", repo)
        if {"pom.xml", "build.gradle", "build.gradle.kts", "mvnw", "gradlew"} & files:
            add("java", repo)
        if "pom.xml" in files and not {"mvnw", "mvnw.cmd"} & files:
            add("mvn", repo)  # 래퍼가 없으면 Maven 자체가 있어야 한다
        if {"build.gradle", "build.gradle.kts"} & files and not {"gradlew", "gradlew.bat"} & files:
            add("gradle", repo)
        if "go.mod" in files:
            add("go", repo)
        if any(f.endswith((".csproj", ".fsproj", ".sln", ".slnx")) for f in files) and "dotnet" in TOOLS:
            add("dotnet", repo)
    from beom_workbench.core import toolchains
    out = []
    for tool, repos in want.items():
        if tool in toolchains.FETCH:
            # 프로젝트가 정한 버전까지, 툴킷 폴더에 받아 둔 것도 — 받아 둔 Node 를 두고 'Node.js 없음'이라고 브리핑에 계속 적었다
            if all(toolchains.ok(tool, r) for r in repos):
                continue
        elif {"cargo": _has("cargo"), "mvn": _has("mvn"), "gradle": _has("gradle"), "msvc": _has("msvc"),
              "openssl": _openssl_dir() is not None}.get(tool, _has(tool)):
            continue
        name, why, win, mac, other = TOOLS[tool]
        out.append({"id": tool, "name": name, "why": why, "repos": [r.name for r in repos][:6],
                    "install": install_command(tool) if sys.platform in ("win32", "darwin") else None,
                    "hint": other if sys.platform not in ("win32", "darwin") else None})
    return out


def agent_env() -> dict:
    """에이전트에게 넘길 환경 — Windows 에 OpenSSL 이 설치돼 있는데 OPENSSL_DIR 이 없으면 넣어 준다 (Rust 빌드가 찾게)."""
    d = _openssl_dir()
    if sys.platform == "win32" and d and not os.environ.get("OPENSSL_DIR"):
        return {"OPENSSL_DIR": d}
    return {}


def brief_line(items: list[dict], declined: list[str] | None = None) -> str | None:
    if not items:
        return None
    parts = [f"{i['name']}({i['why']}; {', '.join(i['repos'])})" for i in items]
    if declined and all(i["id"] in declined for i in items):
        return ("[빌드 준비물 없음 — 사용자가 설치하지 않기로 함] " + " · ".join(parts) + " — 설치를 다시 묻지 말고 빌드 없이 할 수 있는 만큼 "
                "한 뒤 남은 확인(빌드·테스트)을 인수인계의 risks 에 적는다.")
    return ("[빌드 준비물 없음] " + " · ".join(parts) + " — 설치는 따로 묻지 않는다: 빌드·테스트를 wb_task·wb_port_verify 로 돌리면 툴킷이 "
            "사용자에게 설치를 묻고 설치됐는지 확인한다. 셸로 직접 돌리지 않는다.")
