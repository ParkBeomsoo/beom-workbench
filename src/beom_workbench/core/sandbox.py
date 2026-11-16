"""격리 실행 — 에이전트가 실행하는 명령을 운영체제 샌드박스 안에서 돌려, 대상 폴더·임시 폴더·도구 캐시 밖에는 쓰지 못하게 한다.

- Codex 는 자체 샌드박스(workspace-write)로 돈다.
- Claude Code 는 macOS·Linux 에서 자체 샌드박스를 켠다 (--settings 의 sandbox — macOS 는 기본 내장, Linux 는 bubblewrap·socat 필요).
- Antigravity CLI(agy)는 자체 --sandbox (터미널 제한)를 켠다.
- Claude Code 는 Windows 에서 자체 샌드박스가 꺼져 있다 ("Windows sandbox is not active … feature gate off" — 직접 확인함).
  그래서 Windows 에서는 Claude 의 셸 명령을 PowerShell 도구로 받고(Git Bash 는 제한 토큰 안에서 시작조차 못 함), 권한 처리기가
  허용한 명령을 툴킷에 든 Windows 격리 실행(winsandbox — 쓰기 제한 토큰, 관리자 권한·추가 설치 없음)으로 감싸 실행한다.
- 툴킷이 대신 돌리는 검증 명령(합격 케이스·캡처)도 같은 샌드박스로 감싼다.

쓸 수 있는 곳: 대상 폴더, 임시 폴더, 흔한 도구 캐시(npm·yarn·pnpm·pip·uv·cargo·Gradle·Maven·Go·NuGet) — 캐시를 막으면 npm install·
cargo build 같은 평소 명령이 실패한다 (직접 확인함). 툴킷 홈(~/.beom-workbench)·에이전트 설정(~/.claude·~/.codex)은 넣지 않는다.
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

_COUNTER = itertools.count(1)


_WIN_OK: dict = {}


def _win_ready() -> bool:
    """내장 Windows 격리 실행이 이 PC 에서 되는가 — 쓰기 제한 토큰을 한 번 만들어 본다 (앱이 켜져 있는 동안 기억)."""
    if "ok" not in _WIN_OK:
        try:
            from beom_workbench.core import winsandbox
            winsandbox.k32.CloseHandle(winsandbox.restricted_token())
            _WIN_OK["ok"] = True
        except Exception:  # noqa: BLE001
            _WIN_OK["ok"] = False
    return _WIN_OK["ok"]


def _console_python() -> str:
    """감싼 명령을 돌릴 파이썬 — 앱은 pythonw(콘솔 없음)로 돌 수 있어 출력이 이어지는 python.exe 를 쓴다."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def _linux_install() -> str:
    import shutil
    if shutil.which("dnf"):
        return "sudo dnf install -y bubblewrap socat"
    if shutil.which("pacman"):
        return "sudo pacman -S --needed bubblewrap socat"
    return "sudo apt-get install -y bubblewrap socat"


def status() -> dict:
    """이 PC 에서 격리 실행이 준비됐는가 — {ready, how, install, note}. 준비 안 됐으면 install 이 처음 설정에서 돌릴 명령."""
    import shutil
    if sys.platform == "win32":
        if _win_ready():
            return {"ready": True, "how": "Windows 격리 실행 (툴킷 내장 — 쓰기 제한 토큰, 설치할 것 없음)"}
        return {"ready": False, "how": "Windows 격리 실행", "note": "이 PC 에서 쓰기 제한 토큰을 만들지 못했습니다"}
    if sys.platform == "darwin":
        return {"ready": True, "how": "macOS 내장 샌드박스 (Seatbelt)"}
    miss = [n for n in ("bwrap", "socat") if not shutil.which(n)]
    if not miss:
        why = _bwrap_blocked()
        if why:
            out = {"ready": False, "how": "Linux 샌드박스 (bubblewrap)", "note": why}
            fix = _userns_fix()
            if fix:
                out["install"] = fix
            return out
        return {"ready": True, "how": "Linux 샌드박스 (bubblewrap)"}
    return {"ready": False, "how": "Linux 샌드박스 (bubblewrap)", "install": _linux_install(),
            "note": f"없음: {', '.join(miss)} — 설치하면 Claude Code 의 샌드박스가 켜집니다"}


_BWRAP: dict = {}
_SYSCTL = Path("/proc/sys")
_BWRAP_PROFILE = Path("/etc/apparmor.d/bwrap-userns-restrict")
_BWRAP_PROFILE_OFF = Path("/etc/apparmor.d/disable/bwrap-userns-restrict")
# 권한 없는 사용자 이름공간을 막는 커널 설정 — (설정 이름, 막힌 값인가, 풀 값). 배포판마다 다르다:
#   Ubuntu 23.10+: AppArmor 제한 / Debian 10 이전 등: unprivileged_userns_clone=0 / RHEL·CentOS 7: max_user_namespaces=0
_USERNS_SYSCTLS = (("kernel.apparmor_restrict_unprivileged_userns", lambda v: v != "0", "0"),
                   ("kernel.unprivileged_userns_clone", lambda v: v == "0", "1"),
                   ("user.max_user_namespaces", lambda v: v == "0", "15000"))


def _sysctl(name: str) -> str | None:
    try:
        return (_SYSCTL / name.replace(".", "/")).read_text().strip()
    except OSError:
        return None


def _userns_blockers() -> tuple[list[str], bool]:
    """지금 이름공간을 막고 있는 커널 설정('이름=풀 값')과, bubblewrap 전용 AppArmor 프로필이 켜져 있는지."""
    sysctls = []
    for name, blocked, want in _USERNS_SYSCTLS:
        v = _sysctl(name)
        if v is not None and blocked(v):
            sysctls.append(f"{name}={want}")
    return sysctls, _BWRAP_PROFILE.is_file() and not _BWRAP_PROFILE_OFF.exists()


def _userns_fix() -> str | None:
    """막고 있는 것만 푸는 명령 (관리자 암호 필요, 다시 켜도 유지). 풀 것이 없으면 None — 컨테이너 안처럼 이 PC 에서 풀 수 없는 경우.
    CI 에서 직접 확인함: Ubuntu 24.04 는 AppArmor sysctl 만, 26.04 는 bubblewrap 전용 프로필(bwrap-userns-restrict)이
    bubblewrap 안의 중첩 이름공간을 막아 그 프로필도 꺼야 한다."""
    sysctls, profile = _userns_blockers()
    steps = []
    if profile:
        p = str(_BWRAP_PROFILE)
        steps.append(f"sudo mkdir -p /etc/apparmor.d/disable && sudo ln -sf {p} /etc/apparmor.d/disable/"
                     f" && (sudo apparmor_parser -R {p} || true)")
    if sysctls:
        lines = r"\n".join(sysctls)  # printf 가 \n 을 줄바꿈으로 바꾼다
        steps.append(f"printf '{lines}" + r"\n" + "' | sudo tee /etc/sysctl.d/60-bwrap-userns.conf && sudo sysctl --system")
    return " && ".join(steps) or None


def _bwrap_probe() -> list[str]:
    """Claude 의 샌드박스와 같은 모양으로 — bubblewrap 안에서 사용자 이름공간을 한 번 더 만든다 (샌드박스 런타임이 seccomp 를
    걸 때 그렇게 한다). Ubuntu 26.04 는 bubblewrap 자체는 AppArmor 프로필로 허용하지만 그 안의 중첩 이름공간은 막아서,
    bubblewrap 만 돌려 보면 '준비됨'인데 실제 명령은 전부 실패한다 (CI 에서 직접 확인함)."""
    import shutil
    inner = ["unshare", "--user", "--map-root-user", "true"] if shutil.which("unshare") else ["true"]
    return ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-user", "--unshare-net", "--", *inner]


def _bwrap_blocked() -> str | None:
    """bubblewrap 이 깔려 있어도 실제로 격리 공간을 못 만드는 경우. 되면 기억하고, 안 되면 30초 뒤 다시 본다
    ('구성'으로 제한을 푼 뒤 앱을 다시 켜지 않아도 바로 준비됨이 되게)."""
    import subprocess
    import time
    hit = _BWRAP.get("v")
    if hit and (hit[0] is None or time.monotonic() - hit[1] < 30):
        return hit[0]
    try:
        p = subprocess.run(_bwrap_probe(), capture_output=True, text=True, timeout=20)
        ok, err = p.returncode == 0, (p.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        ok, err = False, str(e)
    why = None
    if not ok:
        why = f"bubblewrap 이 격리 공간을 만들지 못합니다 ({err[-160:]})"
        sysctls, profile = _userns_blockers()
        if sysctls or profile:
            why += (" — 이 시스템이 권한 없는 사용자 이름공간을 막고 있습니다 (" + ", ".join(
                    [s.split("=")[0] for s in sysctls] + (["AppArmor bwrap-userns-restrict"] if profile else []))
                    + "). '구성'을 누르면 bubblewrap 이 쓸 수 있게 그 제한만 풉니다 (관리자 암호 필요, 시스템 전체 설정이 바뀝니다)")
        else:
            why += (" — 이 PC 에서 풀 수 있는 제한을 찾지 못했습니다 (컨테이너 안이면 호스트에서 사용자 이름공간을 허용해야 합니다)."
                    " 격리 실행을 끄고 쓰거나 관리자에게 문의하세요")
    _BWRAP["v"] = (why, time.monotonic())
    return why


def available() -> bool:
    return bool(status()["ready"])


def active(run: dict) -> bool:
    """이 실행에서 Claude 의 명령을 격리하는가 — 이 PC 에 샌드박스가 준비됐고 사용자가 끄지 않았을 때."""
    return run.get("sandbox", True) is not False and available()


def wraps_powershell(run: dict) -> bool:
    """Windows: Claude 의 PowerShell 명령을 Codex 샌드박스로 감싼다 (Claude 자체 샌드박스가 Windows 에서 꺼져 있어서)."""
    return sys.platform == "win32" and active(run)


_REGISTRIES = ["registry.npmjs.org", "*.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
               "crates.io", "static.crates.io", "index.crates.io", "github.com", "*.githubusercontent.com",
               "repo1.maven.org", "repo.maven.apache.org", "plugins.gradle.org", "services.gradle.org",
               "proxy.golang.org", "sum.golang.org", "api.nuget.org"]


def claude_settings(run: dict) -> dict:
    """macOS·Linux: Claude Code 자체 샌드박스 설정 (--settings). 샌드박스 밖 재시도는 막고, 명령 허용은 우리 권한 규칙이 정한다."""
    home = Path.home()
    caches = [home / ".npm", home / ".cache", home / ".cargo", home / ".gradle", home / ".m2", home / "go",
              home / ".nuget", home / "Library" / "Caches", home / ".yarn", home / ".pnpm-store"]
    web = (run.get("policy") or {}).get("web") == "allow"
    return {"sandbox": {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": False,
                        "allowUnsandboxedCommands": False,
                        "filesystem": {"allowWrite": [c.as_posix() for c in caches],
                                       "denyWrite": [(home / ".beom-workbench").as_posix()]},
                        "network": {"allowedDomains": _REGISTRIES if web else []}}}


def cache_roots() -> list[str]:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    import shutil
    # (캐시 폴더, 그 캐시를 쓰는 도구) — 폴더가 있거나, 도구가 깔려 있어 곧 만들 것만 (안 쓰는 도구의 빈 폴더를 만들지 않게)
    cands = [(local / "npm-cache", "npm"), (roaming / "npm-cache", None), (local / "Yarn", "yarn"), (local / "pnpm", "pnpm"),
             (local / "pnpm-cache", "pnpm"), (local / "pip" / "Cache", "pip"), (local / "uv", "uv"), (home / ".cargo", "cargo"),
             (local / "cargo", None), (home / ".gradle", "gradle"), (home / ".m2", "mvn"), (home / "go", "go"),
             (local / "go-build", "go"), (home / ".nuget", "dotnet"), (local / "NuGet", "dotnet"), (local / "ms-playwright", None)]
    out = []
    for c, tool in cands:
        if c.exists() or (tool and c.parent.exists() and shutil.which(tool)):
            out.append(c.as_posix())
    return out


def roots(run: dict) -> list[str]:
    """쓸 수 있게 할 폴더 — 대상 폴더, 임시 폴더, 도구 캐시."""
    import tempfile
    return [str(Path(run["target_root"]).resolve()), tempfile.gettempdir(), *[str(Path(c)) for c in cache_roots()]]


def _win_prefix(run: dict) -> list[str]:
    out = [_console_python(), "-m", "beom_workbench.core.winsandbox"]
    for r in roots(run):
        out += ["--root", r]
    return out + ["--"]


def wrap_powershell(command: str, run: dict) -> str:
    """Claude 의 PowerShell 명령을 샌드박스 안에서 돌리는 한 줄로 — 원래 명령은 대상 폴더의 .beom/tmp/sbx/ 에 스크립트로 둔다."""
    d = Path(run["target_root"]) / ".beom" / "tmp" / "sbx"
    d.mkdir(parents=True, exist_ok=True)
    script = d / f"cmd-{os.getpid()}-{next(_COUNTER)}.ps1"
    # 스크립트는 BOM 있는 UTF-8 — Windows PowerShell 5 가 한글 경로·문자열을 깨뜨리지 않게
    script.write_text("$ErrorActionPreference = 'Continue'\r\n" + command + "\r\n", encoding="utf-8-sig")
    pre = _win_prefix(run)
    # 끝나면 스크립트를 지운다 — 명령마다 하나씩 대상 폴더에 쌓이지 않게 (종료 코드는 그대로 돌려준다)
    return (f"& {_ps_quote(pre[0])} " + " ".join(_ps_quote(a) for a in pre[1:])
            + f" powershell -NoProfile -ExecutionPolicy Bypass -File {_ps_quote(str(script))}; $__c = $LASTEXITCODE; "
            + f"Remove-Item -LiteralPath {_ps_quote(str(script))} -Force -ErrorAction SilentlyContinue; if ($__c) {{ exit $__c }}")


def wrap_argv(argv: list[str], run: dict) -> list[str]:
    """툴킷이 돌리는 명령(인자 목록)을 샌드박스로 감싼다."""
    return [*_win_prefix(run), *argv]


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


RULE_POSIX = ("[격리 실행] 셸 명령은 운영체제 샌드박스 안에서 돈다 — 대상 폴더·임시 폴더·도구 캐시(npm·pip·cargo 등)에만 쓸 수 있고, "
              "전역 설치·대상 폴더 밖 쓰기는 실패한다. 웹 허용일 때도 패키지 저장소 같은 정해 둔 곳만 접속된다. 샌드박스 밖으로 다시 "
              "실행해 달라고 하지 않는다 — 막히면 그 사실을 보고한다.")


def rule(run: dict) -> str:
    return RULE if sys.platform == "win32" else RULE_POSIX


RULE = ("[격리 실행] 이 PC 에서는 셸 명령이 운영체제 샌드박스 안에서 돈다 — PowerShell 도구로 실행하고(Bash 없음), 대상 폴더·임시 폴더·"
        "도구 캐시(npm·pip·cargo 등)에만 쓸 수 있다. 전역 설치(npm -g, pip --user)·대상 폴더 밖 쓰기는 거절된다. 명령마다 새 셸이라 "
        "cd·환경 변수는 다음 명령에 이어지지 않는다 — 경로를 명령마다 적는다.")
