"""How the toolkit runs a project command — one place for the environment and for 'inside or outside the Windows sandbox'.

Every place that runs a project command (wb_task, verification cases, coverage, dependency installs, wb_run, and the check
on Claude's own PowerShell commands) used to build its environment and make the sandbox decision on its own. Each copy
drifted: wb_run skipped the project's JDK/Node, a Node-only test sent `dotnet test` into the sandbox, `.\\gradlew.bat`
was not recognised, and a Python 3.13 test hit 'access denied' on its own temp folder. They all come here now.

- command_env(cwd): the environment for a project command — secrets removed, the toolkit's prepared dependencies
  (.beom/venv, node_modules/.bin) and the project's toolchain versions first on PATH, OpenSSL for Rust builds.
- outside_reason(cmd, env): why this command cannot run inside the Windows sandbox, or None.
  Node tools start child processes over named pipes (spawn EPERM); Maven/Gradle/.NET/Go/Cargo write per-user state that
  the sandbox cannot cover completely; Python 3.12.4+ makes its temp folders owner-only, which a write-restricted token
  can never write (tmp_path, venv, pip).
- denied(output): whether a failed sandboxed run failed because of the sandbox (then it is retried outside, with the
  same one-time permission).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from beom_workbench.core import runtime


def command_env(cwd, extra: dict | None = None) -> dict:
    """프로젝트 명령의 환경 — 비밀 이름은 빼고, 툴킷이 준비한 의존성과 프로젝트가 정한 도구 버전을 PATH 앞에."""
    from beom_workbench.core import deps, layout, prereqs, toolchains
    cwd = Path(cwd)
    env = runtime.safe_env({"PYTHONIOENCODING": "utf-8", **runtime.coverage_env(cwd), **prereqs.agent_env(), **(extra or {})})
    strip_isolation(env)  # 격리 밖에서는 node --test 를 파일마다 따로 (격리용 설정은 격리 안에서만 — sandbox.node_test_env)
    more = deps.env_paths(cwd)  # 'python app.py' 가 .beom/venv 의 파이썬으로, vitest 가 node_modules/.bin 에서
    if more:
        key = next((k for k in env if k.upper() == "PATH"), "PATH")
        env[key] = os.pathsep.join(more + [env.get(key, "")])
        pyroot = layout.root_for(cwd, "python")
        if deps.venv_python(pyroot).is_file():
            env["VIRTUAL_ENV"] = str(deps.venv_dir(pyroot))
    env.update(toolchains.env_for(cwd, env))
    return env


def strip_isolation(env: dict) -> dict:
    key = next((k for k in env if k.upper() == "NODE_OPTIONS"), None)
    if key:
        cur = re.sub(r"(?:^|\s)--(?:experimental-)?test-isolation(?:=\S*)?", "", env[key]).strip()
        if cur:
            env[key] = cur
        else:
            env.pop(key)
    return env


# ---------------------------------------------------------------- inside or outside the sandbox

NODE_STEMS = {"node", "npm", "npx", "pnpm", "pnpx", "yarn", "bun", "bunx", "corepack", "tsx", "ts-node", "vite", "vitest", "jest",
              "mocha", "tsc", "next", "nuxt", "astro", "webpack", "esbuild", "rollup", "playwright", "eslint", "prettier", "ng",
              "turbo", "nx", "react-scripts", "svelte-kit", "remix", "electron", "storybook"}
JVM_STEMS = {"mvn", "mvnw", "gradle", "gradlew"}
PY_STEMS = {"python", "python3", "py", "pytest", "pip", "pip3", "coverage", "tox", "nox"}
_EXT = re.compile(r"\.(?:cmd|bat|exe|ps1|sh)$", re.I)
_VERSION_ONLY = re.compile(r"\S+\s+(?:--version|-v|-V|version)\s*\)?\s*$")
_WRAPPED = re.compile(r"""(?:^|[\s;&|(])(?:cmd(?:\.exe)?\s+/[a-z]\b(?:\s+/[a-z]\b)*|(?:pwsh|powershell)(?:\.exe)?\s+(?:-\w+\s+)*?-c(?:ommand)?)\s+
                          (?P<q>["'])(?P<body>.+?)(?P=q)""", re.I | re.X)


def _segments(cmd: str) -> list[str]:
    out = []
    for m in _WRAPPED.finditer(cmd or ""):  # cmd /c "npm test" · pwsh -c "go test ./..." — 안쪽 명령도 본다
        out += _segments(m.group("body"))
    for seg in re.split(r"&&|\|\||[;|\n]", cmd or ""):
        seg = seg.strip().lstrip("(").strip()
        if seg:
            out.append(seg)
    return out


def _head(seg: str) -> str:
    seg = re.sub(r"^(?:&|call\b|start\s+(?:/\w+\s+)*)\s*", "", seg, flags=re.I).strip()
    m = re.match(r"""\s*(?:"([^"]+)"|'([^']+)'|(\S+))""", seg)
    return (m.group(1) or m.group(2) or m.group(3) or "") if m else ""


def outside_reason(cmd: str, env: dict | None = None) -> str | None:
    """Windows 격리 안에서 돌지 않는 명령이면 그 까닭(node·jvm·dotnet·go·cargo·python), 아니면 None. 명령 줄의 각 부분
    (&&·;·| 로 이은 것, cmd /c "…" 안)의 실행 파일을 본다 — .\\gradlew.bat · node_modules\\.bin\\vitest · & "C:\\…\\node.exe" 도."""
    for seg in _segments(cmd):
        h = _head(seg)
        if not h or _VERSION_ONLY.fullmatch(seg):
            continue
        norm = h.replace("\\", "/")
        stem = _EXT.sub("", norm.rsplit("/", 1)[-1]).lower()
        if stem in NODE_STEMS or "/node_modules/.bin/" in f"/{norm.lower()}":
            return "node"
        if stem in JVM_STEMS:
            return "jvm"
        if stem == "dotnet":
            return "dotnet"
        if stem == "go":
            return "go"
        if stem in ("cargo", "rustup"):
            return "cargo"
        if stem in PY_STEMS and _py_owner_only_temp(h, stem, env):
            return "python"
    return None


def _py_owner_only_temp(head: str, stem: str, env: dict | None) -> bool:
    """이 명령이 쓸 파이썬이 3.12.4 이상인가 — 그 버전부터 임시 폴더를 소유자 전용(0o700 → 보호된 ACL)으로 만들어, 쓰기 제한
    토큰으로는 그 안에 쓸 수 없다 (tmp_path·TemporaryDirectory·venv 의 ensurepip 가 '접근 거부'). 3.11 은 된다."""
    import sys
    if sys.platform != "win32":
        return False
    from beom_workbench.core import toolchains
    path = next((v for k, v in (env or {}).items() if k.upper() == "PATH"), None) or os.environ.get("PATH", "")
    exe = head if ("/" in head or "\\" in head) and Path(head).is_file() else shutil.which(head, path=path)
    if not exe:
        return False
    if stem not in ("python", "python3", "py"):
        sib = Path(exe).with_name("python.exe")  # 가상 환경의 Scripts\pytest.exe → 같은 폴더의 python.exe
        exe = str(sib) if sib.is_file() else (shutil.which("python", path=path) or "")
    elif stem == "py":
        exe = shutil.which("python", path=path) or exe
    v = toolchains._py_version(exe) if exe else None
    return bool(v) and toolchains._ver(v) >= (3, 12, 4)


DENIED = re.compile(r"\bEPERM\b|\bEACCES\b|Access is denied|액세스가 거부|접근이 거부|PermissionError|WinError 5\b|\[Errno 13\]|"
                    r"UnauthorizedAccessException|os error 5\b|Operation not permitted", re.I)


def denied(code: int, output: str) -> bool:
    """격리 안에서 돌린 명령이 격리 때문에 실패한 것 같은가 — 그러면 같은 허락으로 격리 밖에서 한 번 더 돌린다."""
    return code != 0 and bool(DENIED.search(output or ""))
