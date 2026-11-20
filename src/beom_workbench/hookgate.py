"""Claude Code PreToolUse hook — the toolkit, not the model, keeps the order of a request run.

Product files are not edited before the plan is in (and, when the run waits for it, before the user approves it).
Only the toolkit's own work files (.beom/: acceptance cases, design sians) may be written before that.
Claude Code runs this before every Edit/Write/MultiEdit/NotebookEdit; exit code 2 blocks the call and shows stderr to
the model. Any failure here allows the call — a broken gate must not stop the work.

usage: python -m beom_workbench.hookgate <home> <run_id>   (hook JSON on stdin)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def verdict(run: dict, tool: str, tool_input: dict) -> str | None:
    """막을 까닭 (막지 않으면 None)."""
    if tool not in EDIT_TOOLS or run.get("mode") != "request":
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return None
    root = Path(run["target_root"]).resolve()
    roots = [root] + [Path(r["repo"]).resolve() for r in ((run.get("work_branch") or {}).get("repos") or []) if r.get("repo")]
    rel = None
    for base in roots:  # 대상 폴더, 그리고 같은 저장소의 다른 폴더(허락받고 고치는 곳)도 제품 파일이다
        try:
            rel = Path(path).resolve().relative_to(base)
            break
        except (OSError, ValueError):
            continue
    if rel is None:
        return None  # 저장소 밖은 권한 규칙이 따로 본다
    if rel.parts and rel.parts[0] == ".beom":
        return None  # 합격 케이스·시안은 계획 전에 만든다
    plan = run.get("plan") or {}
    if not plan.get("steps"):
        return ("계획 전에는 제품 파일을 만들거나 고치지 않는다 — 순서: 요구사항 → 합격 케이스(.beom/cases.json) → (새 화면이면 시안 "
                ".beom/looks/) → 계획(wb_plan_set). 계획을 낸 뒤에 구현한다.")
    waits = run.get("plan_approval", True) and run.get("confirm_at", "start") != "end"
    if waits and not plan.get("approved"):
        return "사용자가 계획을 승인하기 전에는 제품 파일을 고치지 않는다 — 계획을 냈으면 라운드를 끝내고 승인을 기다린다."
    return None


def main(argv: list[str]) -> int:
    import time
    try:
        home, run_id = argv[1], argv[2]
        raw = sys.stdin.buffer.read().decode("utf-8") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        data = json.loads(raw or "{}")  # Claude Code 는 UTF-8 로 보낸다 (콘솔 코드 페이지 cp949 로 읽으면 한글 경로가 깨진다)
        f = Path(home) / "runs" / run_id / "run.json"
        run = None
        for _ in range(40):  # 실행기가 run.json 을 바꾸는 순간(os.replace)이면 잠깐 뒤 다시
            try:
                run = json.loads(f.read_text(encoding="utf-8"))
                break
            except (PermissionError, ValueError):
                time.sleep(0.025)
        if run is None:
            return 0
        why = verdict(run, str(data.get("tool_name") or ""), data.get("tool_input") or {})
    except Exception:  # noqa: BLE001 — 문지기가 고장 나도 일은 막지 않는다
        return 0
    if why:
        sys.stderr.buffer.write((why + "\n").encode("utf-8"))  # Claude Code 는 UTF-8 로 읽는다 (Windows 콘솔 코드 페이지 아님)
        sys.stderr.flush()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
