"""MCP 서버 진입점 — modules/ 의 모든 파일을 import 해서 도구를 등록한다.

새 도구 묶음 추가: modules/ 에 파일을 만들고 `from beom_workbench.server import mcp` 후 @mcp.tool() 로 정의.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys

import beom_workbench.modules as modules_pkg
from beom_workbench.server import mcp


LOAD_FILE = "toolserver.json"


def register_all(failed: dict[str, str] | None = None) -> list[str]:
    loaded = []
    for _, name, _ in pkgutil.iter_modules(modules_pkg.__path__):
        try:
            importlib.import_module(f"beom_workbench.modules.{name}")
            loaded.append(name)
        except Exception as e:  # noqa: BLE001 — 한 모듈 실패가 서버 전체를 막지 않도록
            print(f"  FAIL [{name}]: {e}", file=sys.stderr)
            if failed is not None:
                failed[name] = f"{type(e).__name__}: {e}"[:300]
    return loaded


def report_load(failed: dict[str, str]) -> None:
    """이 실행의 도구 서버가 무엇을 불러오지 못했는지 실행 폴더에 남긴다 — 실행기는 에이전트의 오류 문구가 아니라 이 기록으로
    '툴킷 도구 일부가 빠진 채 떴다'를 안다 (에이전트·버전마다 문구가 다르다)."""
    import json
    import os
    import time
    from pathlib import Path

    run, home = os.environ.get("BEOM_WORKBENCH_RUN"), os.environ.get("BEOM_WORKBENCH_HOME")
    if not run or not home:
        return
    d = Path(home) / "runs" / run
    if not d.is_dir():
        return
    try:
        (d / LOAD_FILE).write_text(json.dumps({"at": time.time(), "pid": os.getpid(), "failed": failed}, ensure_ascii=False),
                                   encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    from beom_workbench.common import nowindow

    nowindow.install()  # 에이전트가 띄운 MCP 서버 — 화면 확인·분석 엔진·검증 명령이 창을 열지 않게
    failed: dict[str, str] = {}
    register_all(failed)
    report_load(failed)
    mcp.run()


if __name__ == "__main__":
    main()
