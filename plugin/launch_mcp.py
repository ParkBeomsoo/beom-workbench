"""Claude Code 플러그인용 MCP 실행기 — 이미 설치된 툴킷을 찾아 띄운다. 아무것도 설치하지 않는다.

찾는 순서:
1. 이 Python 에 beom_workbench 가 있으면 그것 (플러그인 버전과 다르면 stderr 로 경고)
2. PATH 의 beom-workbench-mcp (pipx·uv tool 로 설치한 경우)
없으면 설치 안내를 stderr 에 쓰고 종료한다 (Claude Code 의 /mcp 에 실패로 보인다).
"""
from __future__ import annotations

import shutil
import subprocess
import sys

WANT = sys.argv[1] if len(sys.argv) > 1 else None
HINT = ("beom-workbench 툴킷이 설치되어 있지 않음 — 툴킷 저장소에서 `pipx install <저장소 경로 또는 git URL>` "
        "(또는 uv tool install) 로 설치한 뒤 Claude Code 를 다시 시작한다.")


def main() -> int:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # 안내 문구가 콘솔 코드페이지에서 깨지지 않게
    try:
        import beom_workbench
    except ImportError:
        beom_workbench = None
    if beom_workbench is not None:
        have = getattr(beom_workbench, "__version__", None)
        if WANT and have != WANT:
            sys.stderr.write(f"경고: 플러그인 {WANT} / 설치된 툴킷 {have} — 툴킷을 업데이트하거나 플러그인을 맞춘다\n")
        from beom_workbench.main import main as run

        run()
        return 0
    exe = shutil.which("beom-workbench-mcp")
    if exe:
        return subprocess.call([exe])  # 표준 입출력을 그대로 물려준다 (MCP stdio)
    sys.stderr.write(HINT + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
