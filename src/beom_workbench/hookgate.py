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


def _res(p) -> Path:
    """resolve + 한글 정규화(NFC) — macOS Finder 가 만든 한글 이름은 풀어 쓴 꼴(NFD)이라, 모델이 쓴 모아 쓴 꼴과 비교하면
    '저장소 밖'으로 보여 계획 순서 점검이 그냥 통과했다."""
    import unicodedata
    return Path(unicodedata.normalize("NFC", str(Path(p).resolve())))

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


# 기획 패키지는 이 순서로 쓴다 — 뒤 문서는 앞 문서의 요구사항(R 번호)·규칙을 가져다 쓴다
PLAN_ORDER = [("기획서.md",), ("정책서.md",), ("화면설계.md", "wireframes"), ("테스트시나리오.md",)]


def plan_order(run: dict, tool: str, tool_input: dict) -> str | None:
    """기획 모드 — 앞 문서가 없는데 뒤 문서를 쓰면 막는다 (기획서 없이 정책서·와이어프레임부터 쓰고, 단계 표시는 기획서가 끝난 것처럼
    보였다). 이미 있는 문서를 고치는 것은 막지 않는다."""
    if tool not in EDIT_TOOLS or run.get("mode") != "plan":
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return None
    brief = _res(run["target_root"]) / ".beom" / "brief"
    try:
        rel = _res(path).relative_to(brief)
    except (OSError, ValueError):
        return None
    head = rel.parts[0] if rel.parts else ""
    step = next((i for i, names in enumerate(PLAN_ORDER) if head in names), None)
    if not step or (brief / rel).exists():
        return None
    def made(n: str) -> bool:  # 폴더(wireframes)는 화면 HTML 이 하나라도 있을 때 — 툴킷이 미리 깐 공통 부품(_kit.css)만으로는 아니다
        f = brief / n
        return any(x.suffix.lower() in (".html", ".htm") for x in f.iterdir()) if f.is_dir() else f.exists()
    missing = [names[0] for names in PLAN_ORDER[:step] if not any(made(n) for n in names)]
    if not missing:
        return None
    return (f"기획 패키지는 기획서 → 정책서 → 화면설계(와이어프레임) → 테스트시나리오 순서로 쓴다 — 아직 {', '.join(missing)} 이(가) 없어 "
            f"{head} 를 먼저 만들 수 없다. 뒤 문서는 앞 문서의 요구사항(R 번호)·규칙을 가져다 쓰니 {missing[0]} 부터 쓴다.")


def verdict(run: dict, tool: str, tool_input: dict) -> str | None:
    """막을 까닭 (막지 않으면 None)."""
    if run.get("mode") == "plan":
        return plan_order(run, tool, tool_input)
    # 참고(포팅) 모드도 같은 순서 — 원본을 분석해 계획을 승인받은 뒤에 고친다 (다른 이식 도구들도 계획 승인을 구현 앞에 둔다)
    if tool not in EDIT_TOOLS or run.get("mode") not in ("request", "reference"):
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return None
    root = _res(run["target_root"])
    roots = [root] + [_res(r["repo"]) for r in ((run.get("work_branch") or {}).get("repos") or []) if r.get("repo")]
    rel = None
    for base in roots:  # 대상 폴더, 그리고 같은 저장소의 다른 폴더(허락받고 고치는 곳)도 제품 파일이다
        try:
            rel = _res(path).relative_to(base)
            break
        except (OSError, ValueError):
            continue
    if rel is None:
        return None  # 저장소 밖은 권한 규칙이 따로 본다
    if rel.parts and rel.parts[0] == ".beom":
        return None  # 합격 케이스·시안은 계획 전에 만든다
    plan = run.get("plan") or {}
    if not plan.get("steps"):
        return ("계획 전에는 제품 파일을 만들거나 고치지 않는다 — 순서: 요구사항 → 합격 케이스(.beom/cases.json) → (새 디자인을 정할 때만 "
                "시안 .beom/looks/) → 계획(wb_plan_set). 계획 전에 만드는 것은 .beom/ 아래뿐이다.")
    waits = run.get("plan_approval", True) and run.get("confirm_at", "start") != "end"
    if waits and not plan.get("approved"):
        return "사용자가 계획을 승인하기 전에는 제품 파일을 고치지 않는다 — 계획을 냈으면 라운드를 끝내고 승인을 기다린다."
    return None


def shared_ask(run: dict, tool: str, tool_input: dict) -> str | None:
    """승인 안 된 기존 공통 부품을 고치려 하면 — 편집 자동 허용에서도 권한 처리기(사용자 카드)로 보낸다. 실행 기록을 편집하는 그
    순간에 읽으니 라운드 중에 승인된 것은 바로 풀린다. 경로에 [] 같은 글자가 있어 권한 규칙(Edit(./…))으로 적을 수 없던 부품도 덮는다."""
    if tool not in EDIT_TOOLS:
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path or not run.get("shared_parts"):
        return None
    try:
        rel = _res(path).relative_to(_res(run["target_root"])).as_posix()
        from beom_workbench.core.sharedlock import shared_locked
        users = shared_locked(run).get(rel)
    except Exception:  # noqa: BLE001
        return None
    return f"{rel} 은(는) 다른 화면 {users}곳이 쓰는 기존 공통 부품 — 사용자에게 묻는다" if users else None


def after_shell(home: str, run: dict, tool: str) -> str | None:
    """셸 명령 직후 — 승인 안 된 공통 부품이 지난 체크포인트와 달라졌으면(셸·스크립트로 고쳤으면) 그 상태로 되돌리고 까닭을 돌려준다.
    편집 도구는 편집 전에 묻지만 셸로 쓴 것은 그 확인을 지나지 않는다 (Claude Code 도 문서에 그렇게 적는다)."""
    if tool not in ("Bash", "PowerShell") or not run.get("shared_parts"):
        return None
    from beom_workbench.core.sharedlock import shared_locked
    locked = sorted(shared_locked(run))
    if not locked:
        return None
    from beom_workbench.core.checkpoints import _git  # 체크포인트와 같은 방식으로 (줄바꿈 변환을 끈 채 — 켜면 같은 파일도 달라 보인다)
    root = Path(run["target_root"])
    shadow = Path(home) / "runs" / run["id"] / "shadow.git"
    if not (shadow / "HEAD").exists():
        return None
    tree = _git(shadow, root, "ls-tree", "HEAD", "--", *locked, check=False)
    want = {line.split("	", 1)[1]: line.split()[2] for line in tree.stdout.splitlines() if "	" in line}
    changed = []
    for rel in locked:
        if rel not in want:
            continue
        now = _git(shadow, root, "hash-object", "--", rel, check=False).stdout.strip() if (root / rel).is_file() else ""
        if now != want[rel]:
            _git(shadow, root, "checkout", "-q", "HEAD", "--", rel, check=False)
            changed.append(rel)
    if not changed:
        return None
    return ("승인 안 된 기존 공통 부품을 셸 명령으로 바꿔 원래대로 되돌렸다: " + ", ".join(changed[:6]) +
            " — 다른 화면들이 쓰는 부품이다. 고쳐야 하면 계획의 shared_changes 로 신고해 사용자 승인을 받고 편집 도구로 고친다.")


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
        tool, inp = str(data.get("tool_name") or ""), data.get("tool_input") or {}
        if data.get("hook_event_name") == "PostToolUse":
            undo = after_shell(home, run, tool)
            if undo:
                out = {"decision": "block", "reason": undo}
                sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
                sys.stdout.flush()
            return 0
        why = verdict(run, tool, inp)
        ask = None if why else shared_ask(run, tool, inp)
    except Exception:  # noqa: BLE001 — 문지기가 고장 나도 일은 막지 않는다
        return 0
    if ask:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": ask}}
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        sys.stdout.flush()
        return 0
    if why:
        sys.stderr.buffer.write((why + "\n").encode("utf-8"))  # Claude Code 는 UTF-8 로 읽는다 (Windows 콘솔 코드 페이지 아님)
        sys.stderr.flush()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
