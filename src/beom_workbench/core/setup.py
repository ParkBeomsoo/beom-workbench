"""엔진 설치 — '계획 → 사용자 승인 → 실행' 두 단계.

plan  : 분석할 언어에 필요한 구성 요소마다 상태(installed/missing)·할 일·라이선스·출처를 보여 준다. 아무것도 바꾸지 않는다.
apply : 사용자가 이름을 찍은 것만 설치한다. 내려받는 파일은 배포처가 함께 올린 체크섬(sha256/sha512)으로 검증하고,
        검증에 실패하면 풀지 않고 지운다. 이미 있는 것은 건너뛴다.

툴킷이 직접 설치하지 않는 것: JDK·Node.js·Go·Rust 툴체인 같은 언어 런타임 (설치 안내만), Docker.
"""
from __future__ import annotations

import hashlib
import json
import platform
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings
from beom_workbench.core import container, doctor, integration


def _pip(*pkgs: str) -> list[str]:
    return [sys.executable, "-m", "pip", "install", "-q", *pkgs]


def components(settings: Settings, languages: list[str] | None, scope: str = "user",
               project_dir: Path | None = None) -> list[dict]:
    """구성 요소 목록 — (이름, 필요 이유, 설치 방법, 라이선스, 현재 상태)."""
    doc = doctor.run(settings, languages)
    eng, lsp = doc["engines"], doc["lsp"]
    langs = set(languages or lsp)
    out = [
        {"id": "codebase-memory-mcp", "why": "구조 지도·심볼·호출 간선 (필수)", "license": "MIT",
         "installed": bool(eng.get("codebase-memory-mcp")), "kind": "github-release",
         "source": "DeusData/codebase-memory-mcp", "checksum": "checksums.txt (sha256)", "target": str(settings.home / "bin")},
        {"id": "ast-grep", "why": "진입점 규칙팩 (필수)", "license": "MIT", "installed": bool(eng.get("ast-grep")),
         "kind": "pip", "command": _pip("ast-grep-cli")},
        {"id": "joern", "why": "데이터 흐름 L2 (JDK 21+ 별도 필요)", "license": "Apache-2.0", "installed": bool(eng.get("joern")),
         "kind": "github-release", "source": "joernio/joern", "asset": "joern-cli.zip", "checksum": "joern-cli.zip.sha512",
         "target": str(settings.home / "engines")},
        {"id": "coverage", "why": "Python 실행 검증 L3 (coverage.py)", "license": "Apache-2.0",
         "installed": _module_available("coverage"), "kind": "pip", "command": _pip("coverage")},
    ]
    if "python" in langs:
        out.append({"id": "lsp:python", "why": "Python LSP (L1)", "license": "MIT", "installed": lsp.get("python", {}).get("ok", False),
                    "kind": "pip", "command": _pip("jedi-language-server")})
    if "rust" in langs:
        out.append({"id": "lsp:rust", "why": "Rust LSP (L1)", "license": "MIT/Apache-2.0", "installed": lsp.get("rust", {}).get("ok", False),
                    "kind": "command", "command": ["rustup", "component", "add", "rust-analyzer"], "needs": "rustup"})
    if langs & {"typescript", "javascript"}:
        ok = lsp.get("typescript", lsp.get("javascript", {})).get("ok", False)
        out.append({"id": "lsp:typescript", "why": "TS·JS LSP (L1) — 툴킷 전용 TypeScript 5 포함", "license": "MIT/Apache-2.0",
                    "installed": ok and (settings.home / "node" / "node_modules" / "typescript").exists(), "kind": "commands",
                    "commands": [["npm", "i", "-g", "typescript-language-server"],
                                 ["npm", "install", "--prefix", str(settings.home / "node"), "typescript@5"]], "needs": "npm"})
    if "go" in langs:
        out.append({"id": "lsp:go", "why": "Go LSP (L1)", "license": "BSD-3-Clause", "installed": lsp.get("go", {}).get("ok", False),
                    "kind": "command", "command": ["go", "install", "golang.org/x/tools/gopls@latest"], "needs": "go"})
    for lang, info in lsp.items():
        if lang in langs and not info["ok"] and not any(c["id"] == f"lsp:{lang}" for c in out):
            out.append({"id": f"lsp:{lang}", "why": f"{lang} LSP (L1)", "installed": False, "kind": "manual",
                        "hint": doctor.INSTALL_HINTS.get(f"lsp:{lang}", info["reason"])})
    cli = integration.cli_status()
    out.append({"id": "cli", "why": "어느 폴더·세션에서나 `beom-workbench` 명령으로 부르기 (스킬은 경로를 적지 않는다)",
                "installed": cli["state"] != "missing", "kind": "manual", "state": cli["state"],
                "hint": cli.get("hint") or cli.get("note") or cli.get("path")})
    mcp = integration.mcp_status(scope, project_dir)
    out.append({"id": "mcp-register", "why": "Claude Code 에 MCP 서버 등록 (사용자 범위, 이 설치의 Python 으로)",
                "installed": mcp["state"] == "current", "state": mcp["state"], "kind": "mcp", "needs": "claude",
                "command": ["claude", "mcp", "add", "-s", scope, integration.MCP_NAME, "--", *integration.mcp_command()],
                **({"note": mcp["note"]} if mcp.get("note") else {})})
    sk = integration.skill_status(integration.skill_destination(scope, project_dir))
    out.append({"id": "skill", "why": "분석·포팅 절차 스킬 (이 설치에 들어 있는 사본을 Claude Code 개인 스킬 폴더로)",
                "installed": sk["state"] == "current", "state": sk["state"], "kind": "skill", "target": sk["target"],
                **({"note": sk["note"]} if sk.get("note") else {})})
    sandbox = {"id": "sandbox", "why": "남의 코드 격리 실행 (runtime-run, 포팅 원본 --source-image)",
               "license": "Apache-2.0 (Docker Engine/Moby — Docker Desktop 의 유료 구독 조건 없음)",
               "installed": doc["runtime"]["docker_daemon"]}
    if os.name == "nt":
        d = container.DEFAULT_DISTRO
        sandbox.update(kind="commands", needs="wsl", commands=[
            *([] if d in container.wsl_distros() else [["wsl", "--install", "Debian", "--name", d, "--no-launch"]]),
            ["wsl", "-d", d, "-u", "root", "--", "sh", "-c",
             "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq docker.io"
             " && (systemctl enable --now docker || (dockerd >/var/log/dockerd.log 2>&1 &))"]])
    else:
        sandbox.update(kind="manual", hint="Docker Engine 설치 (예: apt install docker.io) 후 현재 사용자를 docker 그룹에")
    out.append(sandbox)
    for c in out:
        if c.get("needs") and not shutil.which(c["needs"]):
            c["blocked"] = f"{c['needs']} 가 PATH 에 없음 — 먼저 설치"
    return out


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def plan(settings: Settings, languages: list[str] | None = None, scope: str = "user",
         project_dir: Path | None = None) -> dict:
    comps = components(settings, languages, scope, project_dir)
    missing = [c["id"] for c in comps if not c["installed"]]
    return {"components": comps, "missing": missing,
            "approve": f"beom-workbench setup --apply {' '.join(m for m in missing if not _blocked(comps, m))}" if missing else None,
            "note": "apply 에 이름을 준 것만 설치한다. 내려받는 파일은 배포처 체크섬으로 검증"}


def _blocked(comps: list[dict], cid: str) -> bool:
    c = next(c for c in comps if c["id"] == cid)
    return bool(c.get("blocked")) or c["kind"] == "manual"


# ---------------------------------------------------------------- 실행

def _run(argv: list[str], timeout: int = 1800) -> dict:
    exe = shutil.which(argv[0]) or argv[0]
    proc = subprocess.run([exe, *argv[1:]], capture_output=True, timeout=timeout)
    return {"argv": argv, "exit_code": proc.returncode,
            "output_tail": (proc.stdout + proc.stderr).decode("utf-8", "replace")[-800:]}


def _get(url: str, accept: str = "application/octet-stream") -> bytes:
    headers = {"User-Agent": "beom-workbench-setup", "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):  # 있으면 API 한도(시간당 60회, 같은 IP 공유)를 넉넉히
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 — 고정 https 출처(github.com)만 사용
        return resp.read()


def _latest_assets(repo: str) -> dict[str, str]:
    # 릴리스 정보(JSON)는 JSON 으로 받는다 — octet-stream 을 달면 GitHub API 가 415 로 거절한다
    data = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest", accept="application/vnd.github+json"))
    return {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}


def _direct_assets(repo: str, names: list[str]) -> dict[str, str]:
    """API 를 거치지 않는 최신 릴리스 내려받기 주소 — API 한도에 걸렸을 때 (회사망처럼 여러 PC 가 IP 하나를 쓰면 흔하다).
    파일은 똑같이 체크섬으로 확인한다."""
    return {n: f"https://github.com/{repo}/releases/latest/download/{n}" for n in names}


def _cbm_exact_name() -> str:
    os_key = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    return f"codebase-memory-mcp-{os_key}-{arch}{'.zip' if os_key == 'windows' else '.tar.gz'}"


def verify_checksum(blob: bytes, name: str, checksum_text: str) -> str:
    """'<hex>  <파일명>' 줄 목록(checksums.txt) 또는 hex 하나(.sha512)에서 name 의 값을 찾아 대조. 알고리즘은 길이로 판별."""
    expected = None
    lines = [ln.strip() for ln in checksum_text.splitlines() if ln.strip()]
    for ln in lines:
        parts = ln.replace("*", " ").split()
        if len(parts) >= 2 and Path(parts[-1]).name == name:
            expected = parts[0].lower()
    if expected is None and len(lines) == 1:
        expected = lines[0].split()[0].lower()
    if not expected:
        raise ToolError("checksum_failed", f"체크섬 파일에 {name} 항목이 없음")
    algo = {64: "sha256", 128: "sha512"}.get(len(expected))
    if algo is None:
        raise ToolError("checksum_failed", f"알 수 없는 체크섬 길이: {len(expected)}")
    actual = hashlib.new(algo, blob).hexdigest()
    if actual != expected:
        raise ToolError("checksum_failed", f"{name} {algo} 불일치 — 내려받은 파일을 버림")
    return algo


def _pick_cbm_asset(assets: dict[str, str]) -> str:
    """이 OS·CPU 용 CLI 압축 파일. 같은 릴리스의 '-ui-' (화면 포함) 변형은 고르지 않는다 — 크고 이름순으로 먼저 나온다."""
    os_key = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    exact = _cbm_exact_name()
    if exact in assets:
        return exact
    cands = [n for n in assets if os_key in n.lower() and n.endswith((".zip", ".tar.gz")) and "-ui-" not in n.lower()]
    cands.sort(key=lambda n: (not n.endswith(".zip"), "portable" in n, len(n)))  # zip 먼저, 짧은(기본) 이름 먼저
    pref = [n for n in cands if arch in n or ("x86_64" in n and arch == "amd64")]
    if not (pref or cands):
        raise ToolError("not_found", f"{os_key} 용 codebase-memory-mcp 릴리스 파일을 찾지 못함: {sorted(assets)[:10]}")
    return (pref or cands)[0]


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if not str(target).startswith(str(root)):
            raise ToolError("checksum_failed", f"압축 안의 경로가 대상 밖을 가리킴: {info.filename}")
    zf.extractall(dest)


def _safe_extract_tar(archive: Path, dest: Path) -> None:
    """tar.gz (macOS·Linux 배포본) — 대상 밖을 가리키는 경로·링크·장치 파일은 거절한다."""
    root = dest.resolve()
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(root)) or not (m.isfile() or m.isdir()):
                raise ToolError("checksum_failed", f"압축 안의 항목을 풀 수 없음 (대상 밖·링크·장치): {m.name}")
        dest.mkdir(parents=True, exist_ok=True)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:  # 파이썬 3.11 까지 — 위에서 이미 모두 확인했다
            tf.extractall(dest, members=members)  # noqa: S202


def _install_release(c: dict, settings: Settings) -> dict:
    sum_name = c["checksum"].split()[0]
    try:
        assets = _latest_assets(c["source"])
        name = c.get("asset") or _pick_cbm_asset(assets)
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):
            raise
        name = c.get("asset") or _cbm_exact_name()
        assets = _direct_assets(c["source"], [name, sum_name])
    if name not in assets or sum_name not in assets:
        raise ToolError("not_found", f"릴리스에 {name} 또는 {sum_name} 가 없음")
    blob = _get(assets[name])
    algo = verify_checksum(blob, name, _get(assets[sum_name]).decode("utf-8", "replace"))
    target = Path(c["target"])
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target) as tmp:
        archive = Path(tmp) / name
        archive.write_bytes(blob)
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                _safe_extract(zf, Path(tmp) / "x")
        elif name.endswith((".tar.gz", ".tgz")):
            _safe_extract_tar(archive, Path(tmp) / "x")
        else:
            raise ToolError("capability_unavailable", f"{name}: 알 수 없는 배포 형식")
        if c["id"] == "joern":
            src = next(Path(tmp, "x").rglob("joern-cli"), None) or Path(tmp, "x")
            dst = target / "joern-cli"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), dst)
        else:
            for f in Path(tmp, "x").rglob("*"):
                if f.is_file() and (f.name.startswith("codebase-memory-mcp") or f.name in ("LICENSE", "THIRD_PARTY_NOTICES.md")):
                    shutil.copy2(f, target / f.name)
                    if os.name != "nt" and f.name.startswith("codebase-memory-mcp"):
                        (target / f.name).chmod(0o755)
    return {"asset": name, "verified": algo, "target": str(target)}


def apply(settings: Settings, ids: list[str], languages: list[str] | None = None, scope: str = "user",
          project_dir: Path | None = None) -> dict:
    comps = {c["id"]: c for c in components(settings, languages, scope, project_dir)}
    results = []
    for cid in ids:
        c = comps.get(cid)
        if c is None:
            results.append({"id": cid, "ok": False, "error": f"알 수 없는 구성 요소 (plan 목록: {sorted(comps)})"})
            continue
        if c["installed"]:
            results.append({"id": cid, "ok": True, "skipped": "이미 설치됨"})
            continue
        if c.get("blocked") or c["kind"] == "manual":
            results.append({"id": cid, "ok": False, "error": c.get("blocked") or c.get("hint")})
            continue
        try:
            if c["kind"] == "pip" or c["kind"] == "command":
                r = _run(c["command"])
                results.append({"id": cid, "ok": r["exit_code"] == 0, **r})
            elif c["kind"] == "commands":
                rs = [_run(cmd) for cmd in c["commands"]]
                results.append({"id": cid, "ok": all(r["exit_code"] == 0 for r in rs), "steps": rs})
            elif c["kind"] == "skill":
                results.append({"id": cid, **integration.install_skill(
                    settings.home / "backups", integration.skill_destination(scope, project_dir))})
            elif c["kind"] == "mcp":
                results.append({"id": cid, **integration.register_mcp(scope, project_dir)})
            elif c["kind"] == "github-release":
                results.append({"id": cid, "ok": True, **_install_release(c, settings)})
        except ToolError as e:
            results.append({"id": cid, "ok": False, "error": e.message})
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            results.append({"id": cid, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return {"results": results, "after": plan(settings, languages, scope, project_dir)["missing"]}


def remove(settings: Settings, ids: list[str], scope: str = "user", project_dir: Path | None = None) -> dict:
    """에이전트 연동 되돌리기 — 이 툴킷이 설치·등록한 것만 지운다 (소유 표시·등록 대상 확인)."""
    results = []
    for cid in ids:
        if cid == "skill":
            results.append({"id": cid, **integration.remove_skill(integration.skill_destination(scope, project_dir))})
        elif cid == "mcp-register":
            results.append({"id": cid, **integration.unregister_mcp(scope, project_dir)})
        else:
            results.append({"id": cid, "ok": False, "error": "되돌릴 수 있는 것은 skill, mcp-register (엔진은 직접 삭제)"})
    return {"results": results}


def update(settings: Settings, scope: str = "user", project_dir: Path | None = None) -> dict:
    """툴킷을 업데이트한 뒤 — 이 툴킷이 설치한 연동만 새 버전에 맞춘다. 없던 것은 새로 설치하지 않는다."""
    dst = integration.skill_destination(scope, project_dir)
    sk = integration.skill_status(dst)
    results = []
    if sk["state"] == "outdated":
        results.append({"id": "skill", "from": sk.get("installed_version"),
                        **integration.install_skill(settings.home / "backups", dst)})
    else:
        results.append({"id": "skill", "ok": sk["state"] != "foreign", "skipped": sk["state"], **({"note": sk["note"]} if sk.get("note") else {})})
    mcp = integration.mcp_status(scope, project_dir)
    results.append({"id": "mcp-register", "ok": mcp["state"] in ("current", "missing", "unknown"), "state": mcp["state"],
                    **({"note": mcp["note"]} if mcp.get("note") else {}),
                    **({"note": "등록은 실행 중인 Python 경로라 같은 설치를 업그레이드하면 그대로 맞다"} if mcp["state"] == "current" else {})})
    return {"version": integration.__version__, "scope": scope, "results": results}
