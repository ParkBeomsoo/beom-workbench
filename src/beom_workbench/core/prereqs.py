"""빌드 준비물 — 대상 폴더의 저장소를 빌드·실행하는 데 필요한 도구가 이 PC 에 있는지, 작업을 시작하기 전에 본다.

에이전트가 라운드 중간에 "OpenSSL 이 없어 빌드를 못 한다"고 멈추는 대신, 폴더를 고를 때 무엇이 없는지 보여 주고 사람이 한 번에
설치하게 한다 (설치 명령은 각 도구의 공식 패키지 — Windows winget, macOS Homebrew). 저장소 여럿을 모은 폴더면 그 안을 모두 본다.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_SKIP = {"node_modules", ".git", ".beom", "target", "dist", "build", ".venv", "venv", "__pycache__", ".next"}

# id: (보여 줄 이름, 왜 필요한지, Windows 설치, macOS 설치, 그 밖 안내)
TOOLS = {
    "cargo": ("Rust (cargo)", "Rust 저장소 빌드", "winget install -e --id Rustlang.Rustup", "brew install rustup && rustup-init -y",
              "curl https://sh.rustup.rs -sSf | sh"),
    "openssl": ("OpenSSL 개발 파일", "Rust 의 openssl-sys(HTTPS 통신) 빌드 — Windows 에는 기본으로 없다",
                "winget install -e --id ShiningLight.OpenSSL.Dev", "brew install openssl@3", "sudo apt-get install -y libssl-dev pkg-config"),
    "node": ("Node.js", "package.json 저장소 빌드·실행", "winget install -e --id OpenJS.NodeJS.LTS", "brew install node",
             "sudo apt-get install -y nodejs npm"),
    "java": ("JDK", "Maven·Gradle 저장소 빌드", "winget install -e --id EclipseAdoptium.Temurin.21.JDK", "brew install --cask temurin@21",
             "sudo apt-get install -y openjdk-21-jdk"),
    "go": ("Go", "go.mod 저장소 빌드", "winget install -e --id GoLang.Go", "brew install go", "sudo apt-get install -y golang-go"),
}


def _repos(root: Path) -> list[Path]:
    from beom_workbench.core import workbranch
    if workbranch.repo_top(root):
        return [root]
    kids = workbranch.child_repos(root)
    return kids or [root]


def _has(name: str) -> bool:
    if shutil.which(name):
        return True
    home = Path.home()  # 설치기가 PATH 에 넣기 전·새 셸 전에도 흔한 기본 위치
    extra = {"cargo": home / ".cargo" / "bin", "go": home / "go" / "bin"}.get(name)
    return bool(extra and any((extra / f"{name}{s}").is_file() for s in ("", ".exe")))


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
    want: dict[str, list[str]] = {}

    def add(tool: str, repo: Path) -> None:
        want.setdefault(tool, [])
        if repo.name not in want[tool]:
            want[tool].append(repo.name)

    for repo in _repos(root)[:40]:
        files = {p.name for p in repo.iterdir()} if repo.is_dir() else set()
        if "Cargo.toml" in files:
            add("cargo", repo)
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
        if "go.mod" in files:
            add("go", repo)
    have = {"cargo": _has("cargo"), "node": _has("node"), "java": _has("java"), "go": _has("go"),
            "openssl": _openssl_dir() is not None}
    out = []
    for tool, repos in want.items():
        if have.get(tool):
            continue
        name, why, win, mac, other = TOOLS[tool]
        out.append({"id": tool, "name": name, "why": why, "repos": repos[:6],
                    "install": win if sys.platform == "win32" else mac if sys.platform == "darwin" else None,
                    "hint": other if sys.platform not in ("win32", "darwin") else None})
    return out


def agent_env() -> dict:
    """에이전트에게 넘길 환경 — Windows 에 OpenSSL 이 설치돼 있는데 OPENSSL_DIR 이 없으면 넣어 준다 (Rust 빌드가 찾게)."""
    d = _openssl_dir()
    if sys.platform == "win32" and d and not os.environ.get("OPENSSL_DIR"):
        return {"OPENSSL_DIR": d}
    return {}


def brief_line(items: list[dict]) -> str | None:
    if not items:
        return None
    parts = [f"{i['name']}({i['why']}; {', '.join(i['repos'])})" for i in items]
    return ("[빌드 준비물 없음] " + " · ".join(parts) + " — 빌드·실행이 필요한 확인을 하기 전에, 첫 라운드의 질문에서 한 번에 "
            "설치를 요청한다 (앱의 새 작업 화면·에이전트 목록에 설치 단추가 있다). 사용자가 설치하지 않기로 하면 빌드 없이 할 수 있는 만큼 하고 남은 확인을 인수인계에 적는다.")
