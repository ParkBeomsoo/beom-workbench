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
            return {"ready": True, "default_on": False, "how": "Windows 격리 실행 (툴킷 내장 — 쓰기 제한 토큰, 설치할 것 없음)"}
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


def default_on() -> bool:
    """새 실행에서 격리를 기본으로 켜는가. macOS·Linux 는 Claude Code 자체 샌드박스(막히면 '격리 없이 다시'를 사용자에게 묻는다)라
    켠다. Windows 는 끈다 — Claude Code 도 네이티브 Windows 에서는 격리 없이 돌고, 툴킷이 따로 만든 격리는 도구마다 막히는 자리를
    미리 맞혀야 해서 거부가 끊이지 않았다. Windows 에서는 권한 규칙(종류별 허용·묻기·거절)과 편집 직전 확인으로 지키고, 더 강한
    보호가 필요하면 실행 설정에서 격리를 켠다 (그때 막힌 명령은 wb_run 으로 사용자 허락을 받아 격리 밖에서)."""
    return sys.platform != "win32"


def active(run: dict) -> bool:
    """이 실행에서 Claude 의 명령을 격리하는가 — 이 PC 에 샌드박스가 준비됐고 격리를 켰을 때 (켰는지 적혀 있지 않으면 기본값)."""
    return run.get("sandbox", default_on()) is not False and available()


_NODE_ISO: dict[str, str] = {}


def node_test_env(env: dict) -> dict:
    """격리 안에서는 Node 가 하위 프로세스를 띄우지 못해(spawn EPERM) node --test 가 테스트 파일마다 실패한다 — 한 프로세스에서
    돌게(--test-isolation=none) NODE_OPTIONS 에 넣는다. 에이전트가 명령마다 옵션을 붙이거나 우회 스크립트를 만들지 않아도 된다.
    이 옵션을 모르는 Node(22.8 이전)는 NODE_OPTIONS 에 있으면 아예 뜨지 않는다 — 그래서 이 환경의 PATH 로 실제로 쓰일 node 가
    받는지 확인하고, 받으면 넣고 받지 않으면 뺀다. PATH 를 바꾼 뒤(프로젝트가 정한 Node 를 앞에 둔 뒤) 다시 부르면 맞춰진다 —
    시스템 node(24)로 한 번만 확인해 넣었더니, 프로젝트용으로 받아 둔 Node 20 이 그 옵션 때문에 매번 뜨지 않았다."""
    import shutil
    path = next((v for k, v in env.items() if k.upper() == "PATH"), None) or os.environ.get("PATH", "")
    node = shutil.which("node", path=path)
    key = next((k for k in env if k.upper() == "NODE_OPTIONS"), "NODE_OPTIONS")
    import re
    # 나누지 않고 그 옵션만 지운다 — --require "C:/a b/hook.cjs" 처럼 따옴표 안 공백이 있는 값이 함께 들어 있다
    cur = re.sub(r"(?:^|\s)--(?:experimental-)?test-isolation(?:=\S*)?", "", env.get(key) or "").strip()
    flag = _iso_ok(node) if node else ""
    if flag:
        cur = (cur + " " + (flag if isinstance(flag, str) else "--test-isolation=none")).strip()
    if cur:
        env[key] = cur
    else:
        env.pop(key, None)
    return env


def _iso_ok(node: str) -> str:
    """이 node 실행 파일이 NODE_OPTIONS 로 받는 한 프로세스 실행 옵션 — 23.6+ 는 --test-isolation, 22.8~23.5 는
    --experimental-test-isolation, 그 전(20)은 없음(""). 실행 파일마다 한 번 확인해 기억."""
    key = os.path.normcase(os.path.abspath(node))
    if key not in _NODE_ISO:
        import subprocess
        _NODE_ISO[key] = ""
        for flag in ("--test-isolation=none", "--experimental-test-isolation=none"):
            probe = {k: v for k, v in os.environ.items() if k.upper() != "NODE_OPTIONS"}
            probe["NODE_OPTIONS"] = flag
            try:
                if subprocess.run([node, "-e", "0"], capture_output=True, timeout=20, env=probe).returncode == 0:
                    _NODE_ISO[key] = flag
                    break
            except (OSError, subprocess.SubprocessError):
                break
    return _NODE_ISO[key]


def wraps_powershell(run: dict) -> bool:
    """Windows: Claude 의 PowerShell 명령을 Codex 샌드박스로 감싼다 (Claude 자체 샌드박스가 Windows 에서 꺼져 있어서)."""
    return sys.platform == "win32" and active(run)


_REGISTRIES = ["registry.npmjs.org", "*.npmjs.org", "registry.yarnpkg.com", "pypi.org", "files.pythonhosted.org",
               "crates.io", "static.crates.io", "index.crates.io", "github.com", "*.githubusercontent.com",
               "repo1.maven.org", "repo.maven.apache.org", "plugins.gradle.org", "services.gradle.org",
               "proxy.golang.org", "sum.golang.org", "api.nuget.org",
               # 래퍼·도구가 넘겨 보내는 곳 (Gradle 래퍼, corepack 의 Yarn 2+, node-gyp 헤더, GitHub 묶음, rustup, Android)
               "downloads.gradle.org", "plugins-artifacts.gradle.org", "repo.yarnpkg.com", "nodejs.org", "codeload.github.com",
               "static.rust-lang.org", "dl.google.com", "maven.google.com", "globalcdn.nuget.org"]


def claude_settings(run: dict) -> dict:
    """macOS·Linux: Claude Code 자체 샌드박스 설정 (--settings). 샌드박스 밖 재시도는 막고, 명령 허용은 우리 권한 규칙이 정한다."""
    import tempfile
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    caches = [home / ".npm", xdg, home / ".cargo", home / ".rustup", home / ".gradle", home / ".m2", home / "go",
              home / ".nuget", home / ".dotnet", home / ".local" / "share" / "NuGet", home / ".templateengine",
              home / ".local" / "share" / "pnpm", home / "Library" / "pnpm", home / ".bun", home / ".local" / "share" / "uv",
              home / "Library" / "Caches", home / ".yarn", home / ".pnpm-store", Path(tempfile.gettempdir())]
    caches += [Path(c) for c in cache_roots() if Path(c) not in caches]  # 사용자가 바꿔 둔 캐시 자리 (GOPATH·GRADLE_USER_HOME …)
    # bubblewrap 은 없는 폴더를 연결하지 못해 첫 실행의 ~/.gradle·~/.m2 쓰기가 거부됐다 — 그 도구가 있으면 미리 만든다
    import shutil
    need = {".gradle": ("gradle", "java"), ".m2": ("mvn", "java"), ".nuget": ("dotnet",), ".dotnet": ("dotnet",),
            ".rustup": ("cargo", "rustup"), ".cargo": ("cargo",), "go": ("go",), ".bun": ("bun",)}
    for c in caches:
        tools = need.get(c.name)
        if sys.platform == "win32" or not tools or c.exists() or not any(shutil.which(t) or _toolkit_has(t) for t in tools):
            continue
        try:
            c.mkdir(exist_ok=True)
        except OSError:
            pass
    # 패키지 저장소는 설치(빌드)에 필요하다 — '웹 접속'을 물어보기로 둔 기본 모드에서도 npm·pip install 이 되게.
    # 사용자가 웹을 거절로 둔 때만 막는다. (에이전트의 웹 도구는 샌드박스 밖이라 따로 '웹 접속' 규칙으로 묻는다)
    web_refused = (run.get("policy") or {}).get("web") == "refuse"
    # allowUnsandboxedCommands: 격리 안에서 막힌 명령을 Claude 가 '격리 없이'(dangerouslyDisableSandbox)로 다시 요청할 수 있게 —
    # 그 요청은 권한 처리기(툴킷)로 와서 '격리 없이 실행' 규칙대로 허락하거나 사용자에게 묻는다 (Claude Code 의 기본 동작).
    # 막아 두면 처음 보는 도구가 막힐 때마다 길이 없었다
    return {"sandbox": {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": False,
                        "allowUnsandboxedCommands": True,
                        "filesystem": {"allowWrite": [c.as_posix() for c in caches],
                                       "denyWrite": [(home / ".beom-workbench").as_posix()]},
                        # allowLocalBinding: 테스트·미리보기가 127.0.0.1 에 서버를 띄울 수 있게 (기본은 막혀 있다)
                        "network": {"allowedDomains": [] if web_refused else _REGISTRIES, "allowLocalBinding": True}}}


def cache_roots() -> list[str]:
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    import shutil
    # (캐시 폴더, 그 캐시를 쓰는 도구) — 폴더가 있거나, 도구가 깔려 있어 곧 만들 것만 (안 쓰는 도구의 빈 폴더를 만들지 않게)
    cands = [(local / "npm-cache", "npm"), (roaming / "npm-cache", None), (local / "Yarn", "yarn"), (local / "pnpm", "pnpm"),
             (local / "pnpm-cache", "pnpm"), (local / "node" / "corepack", "node"), (local / "pip" / "Cache", "pip"),
             (local / "uv", "uv"), (home / ".cargo", "cargo"), (local / "cargo", None), (home / ".gradle", "gradle"),
             (home / ".gradle", "java"), (home / ".m2", "mvn"), (home / ".m2", "java"), (home / "go", "go"), (local / "go-build", "go"),
             (home / ".dotnet", "dotnet"), (home / ".nuget", "dotnet"), (local / "NuGet", "dotnet"), (local / "ms-playwright", None)]
    cands += [(home / ".rustup", "cargo"), (home / ".rustup", "rustup"), (roaming / "NuGet", "dotnet"), (home / ".templateengine", "dotnet"),
              (roaming / "uv", "uv"), (home / ".bun", "bun")]
    if sys.platform == "darwin":  # macOS 의 캐시 자리 — 위 목록은 Windows(AppData) 자리라 Codex 실행에서 npm·go·pip 가 '권한 없음'으로 멈췄다
        lib = home / "Library"
        cands += [(home / ".npm", "npm"), (lib / "Caches" / "pip", "pip"), (lib / "Caches" / "go-build", "go"),
                  (lib / "Caches" / "Yarn", "yarn"), (lib / "Caches" / "ms-playwright", None), (lib / "pnpm", "pnpm"),
                  (lib / "Caches" / "pnpm", "pnpm"), (lib / "Caches" / "uv", "uv"), (home / ".cache" / "uv", "uv"),
                  (lib / "Caches" / "node" / "corepack", "node"), (home / ".cache" / "node" / "corepack", "node")]
    elif sys.platform.startswith("linux"):
        cands += [(home / ".npm", "npm"), (home / ".cache" / "pip", "pip"), (home / ".cache" / "go-build", "go"),
                  (home / ".cache" / "yarn", "yarn"), (home / ".cache" / "ms-playwright", None), (home / ".local" / "share" / "pnpm", "pnpm"),
                  (home / ".cache" / "uv", "uv")]
    # 사용자가 바꿔 둔 캐시 자리 (GOPATH·GRADLE_USER_HOME·NUGET_PACKAGES …) — 기본 자리만 허락해 '접근 거부'가 났다
    for var, tool in (("GOPATH", "go"), ("GOMODCACHE", "go"), ("GOCACHE", "go"), ("CARGO_HOME", "cargo"), ("RUSTUP_HOME", "cargo"),
                      ("CARGO_TARGET_DIR", "cargo"), ("GRADLE_USER_HOME", "java"), ("NUGET_PACKAGES", "dotnet"),
                      ("NUGET_HTTP_CACHE_PATH", "dotnet"), ("npm_config_cache", "npm"), ("YARN_CACHE_FOLDER", "yarn"),
                      ("PIP_CACHE_DIR", "pip"), ("UV_CACHE_DIR", "uv"), ("MAVEN_REPO_LOCAL", "java")):
        v = os.environ.get(var)
        if v:
            cands.append((Path(v.split(os.pathsep)[0]), tool))
    out = []
    for c, tool in cands:
        # 그 도구가 시스템에 있거나 툴킷 폴더에 받아 둔 것이면 — 받아 둔 Go·.NET·Maven·Gradle 은 시스템 PATH 에 없어서 캐시를
        # 허락하지 않았고, 처음 쓰려는 순간 '접근 거부'로 멈췄다 (go-build · ~/.dotnet)
        if c.exists() or (tool and c.parent.exists() and (shutil.which(tool) or _toolkit_has(tool))):
            if c.as_posix() not in out:
                out.append(c.as_posix())
    return out


def _toolkit_has(tool: str) -> bool:
    try:
        from beom_workbench.core import prereqs, toolchains
        if tool in toolchains.FETCH:
            return bool(toolchains._managed(tool))
        if tool in prereqs.MANAGED:
            return bool(prereqs._installed(tool))
    except Exception:  # noqa: BLE001
        return False
    return False


def roots(run: dict, extra=()) -> list[str]:
    """쓸 수 있게 할 폴더 — 대상 폴더, 임시 폴더, 도구 캐시, 그리고 툴킷이 그 명령을 돌리는 자리(extra)."""
    import tempfile
    out = [str(Path(run["target_root"]).resolve()), tempfile.gettempdir(), *[str(Path(c)) for c in cache_roots()]]
    for e in extra:
        s = Path(e).resolve()
        if not any(s.is_relative_to(Path(o)) for o in out):  # 이미 허락한 곳 안이면 따로 주지 않는다 (기록이 쌓이지 않게)
            out.append(str(s))
    return out


def _win_prefix(run: dict, extra=()) -> list[str]:
    out = [_console_python(), "-m", "beom_workbench.core.winsandbox"]
    for r in roots(run, extra):
        out += ["--root", r]
    return out + ["--"]


# PowerShell 에서 npm·npx·pnpm·yarn 은 .ps1 스크립트로 먼저 연결돼 기본 실행 정책(Restricted)에 막힌다 — 같은 이름의 실행 파일(.cmd)로 부른다
NPM_SHIMS = "".join(f"function {n} {{ & {n}.cmd @args }}; " for n in ("npm", "npx", "pnpm", "yarn"))
# 마지막 문장이 실패했으면 그 종료 코드(없으면 1)로 — 셸처럼 마지막 명령이 결과를 정한다
EXIT_TRAILER = "if ($?) { exit 0 } elseif ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }"


def ps_script(command: str) -> str:
    """에이전트 명령 하나를 담는 PowerShell 본문 — npm 실행 파일 연결과 마지막 명령 기준 종료 코드."""
    return ("$global:LASTEXITCODE = 0; $ErrorActionPreference = 'Continue'\r\n" + NPM_SHIMS + "\r\n"
            + command + "\r\n" + EXIT_TRAILER + "\r\n")


def ps_run_file(path: str) -> str:
    """스크립트 파일을 실행 정책과 상관없이 '친 명령'처럼 돌리는 한 줄 — -ExecutionPolicy Bypass 를 쓰지 않는다.
    Bypass 는 그 안에서 부르는 다른 .ps1 까지 사용자 정책을 무시하게 하고, 그룹 정책으로 정한 PC 에서는 듣지도 않는다.
    이렇게 하면 명령 본문은 사용자가 직접 친 것과 같고, 그 안에서 부르는 .ps1 파일에는 사용자 정책이 그대로 적용된다."""
    return "& ([scriptblock]::Create([IO.File]::ReadAllText(" + _ps_quote(path) + ")))"


def wrap_powershell(command: str, run: dict) -> str:
    """Claude 의 PowerShell 명령을 샌드박스 안에서 돌리는 한 줄로 — 원래 명령은 대상 폴더의 .beom/tmp/sbx/ 에 스크립트로 둔다."""
    d = Path(run["target_root"]) / ".beom" / "tmp" / "sbx"
    d.mkdir(parents=True, exist_ok=True)
    script = d / f"cmd-{os.getpid()}-{next(_COUNTER)}.ps1"
    # 스크립트는 BOM 있는 UTF-8 — Windows PowerShell 5 가 한글 경로·문자열을 깨뜨리지 않게
    script.write_text(ps_script(command), encoding="utf-8-sig")
    pre = _win_prefix(run)
    # 끝나면 스크립트를 지운다 — 명령마다 하나씩 대상 폴더에 쌓이지 않게 (종료 코드는 그대로 돌려준다).
    # 예전에는 -File 로 돌려 마지막 명령이 실패해도(예: npm test) 종료 코드가 0 으로 나갔다
    return (f"& {_ps_quote(pre[0])} " + " ".join(_ps_quote(a) for a in pre[1:])
            + f" powershell -NoProfile -Command {_ps_quote(ps_run_file(str(script)))}; $__c = $LASTEXITCODE; "
            + f"Remove-Item -LiteralPath {_ps_quote(str(script))} -Force -ErrorAction SilentlyContinue; if ($__c) {{ exit $__c }}")


def wrap_argv(argv: list[str], run: dict, cwd=None) -> list[str]:
    """툴킷이 돌리는 명령(인자 목록)을 샌드박스로 감싼다. cwd: 그 명령을 돌리는 자리 — 대상 폴더 위의 프로젝트 자리(Cargo 작업 공간·
    모노레포 맨 위)나 원본 사본에서 돌리면 그 자리의 잠금 파일·빌드 출력을 쓰지 못해 '접근 거부'가 났다."""
    return [*_win_prefix(run, [cwd] if cwd else []), *argv]


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


RULE_POSIX = ("[격리 실행] 셸 명령은 운영체제 샌드박스 안에서 돈다 — 대상 폴더·임시 폴더·도구 캐시(npm·pip·cargo 등)에만 쓸 수 있고, "
              "전역 설치·대상 폴더 밖 쓰기는 실패한다. 네트워크는 패키지 저장소 같은 정해 둔 곳만 접속되고, 127.0.0.1 에 서버는 띄울 수 있다. 샌드박스 밖으로 다시 "
              "실행해 달라고 말로 하지 않는다 — 꼭 필요한 명령이 격리 때문에 막히면 그 명령을 dangerouslyDisableSandbox 로 다시 요청한다 "
              "(사용자 허락을 거쳐 격리 밖에서 돈다). 테스트·빌드는 wb_task, 의존성 설치는 wb_deps.")


def rule(run: dict) -> str:
    return RULE if sys.platform == "win32" else RULE_POSIX


RULE = ("[격리 실행] 이 PC 에서는 셸 명령이 운영체제 샌드박스 안에서 돈다 — PowerShell 도구로 실행하고(Bash 없음), 대상 폴더·임시 폴더·"
        "도구 캐시(npm·pip·cargo 등)에만 쓸 수 있다. 전역 설치(npm -g, pip --user)·대상 폴더 밖 쓰기는 거절된다. 명령마다 새 셸이라 "
        "cd·환경 변수는 다음 명령에 이어지지 않는다 — 경로를 명령마다 적는다. "
        "npm·npx·node·vite 같은 Node 도구는 이 격리 안에서 자식 프로세스를 띄우지 못해(spawn EPERM) 실패하므로, 앱이 격리 밖에서 "
        "실행할지 사용자에게 묻는다. 거절되어 EPERM 이 나면 코드를 고치려 하지 말고 자식 프로세스를 띄우지 않는 방법"
        "을 쓰거나, 그 명령을 돌리지 못했다고 사용자에게 알린다. 다른 명령이 격리 때문에 '접근 거부'로 막히면 mcp__beom-workbench__wb_run "
        "으로 (사용자 허락을 거쳐) 격리 밖에서 돌린다 — 테스트·빌드는 wb_task, 의존성 설치는 wb_deps 가 알아서 한다. node --test(npm test)는 툴킷이 이 격리용 설정(NODE_OPTIONS)을 미리 넣어 두어 "
        "그대로 돈다 — --test-isolation 같은 옵션을 붙이거나 우회 스크립트를 만들지 않는다. 우회는 결과물(package.json·README)에 넣지 않는다.")
