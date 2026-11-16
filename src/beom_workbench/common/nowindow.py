"""Windows — 자식 프로세스를 창 없이 띄운다.

콘솔 없는 프로세스(앱 창, 에이전트가 띄운 MCP 서버, 앱이 띄운 실행기)가 콘솔 프로그램(git·node·cargo·claude …)을 띄우면
Windows 가 그때마다 터미널 창을 잠깐 연다 (Windows Terminal 이 기본이면 창이 앞으로 튀어나온다). 따로 창을 정하지 않은
자식 프로세스는 창 없이(CREATE_NO_WINDOW) 띄우고, 일부러 새 콘솔·분리 실행을 고른 것(로그인 창 등)만 그대로 둔다.
앱·CLI·MCP 서버가 시작할 때 한 번 건다.

같은 때 PowerShell 7 이 남긴 모듈 경로(PSModulePath)도 지운다 — 앱을 pwsh 터미널에서 켜면 그 값이 자식에게 이어져, 앱·에이전트가
띄우는 Windows PowerShell 5 가 기본 명령(Get-FileHash 등)을 못 찾는다 (Claude 설치 스크립트가 그렇게 실패하는 것을 CI 에서 직접 확인함).
값이 없으면 PowerShell 이 스스로 기본값을 만든다.
"""
from __future__ import annotations

import subprocess
import sys


def clean_ps_module_path(env=None) -> None:
    """PSModulePath 에 PowerShell 7 경로가 섞여 있으면 지운다 (env 가 없으면 이 프로세스의 환경)."""
    import os
    env = os.environ if env is None else env
    for k in [k for k in env if k.upper() == "PSMODULEPATH"]:
        # PowerShell 7 의 모듈 폴더는 '...\PowerShell\...' (Windows PowerShell 은 '...\WindowsPowerShell\...')
        parts = [p.replace("/", "\\").lower() for p in env[k].split(";") if p]
        if any("\\powershell\\" in p for p in parts):
            del env[k]


def install() -> None:
    if sys.platform != "win32" or getattr(subprocess.Popen, "_wb_no_window", False):
        return
    clean_ps_module_path()
    base = subprocess.Popen

    class Popen(base):  # type: ignore[misc, valid-type]
        _wb_no_window = True

        def __init__(self, *args, **kwargs):
            flags = kwargs.get("creationflags") or 0
            if not flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS):
                kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = Popen
