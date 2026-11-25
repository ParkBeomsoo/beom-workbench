"""앱 화면용 데이터 — 원장·실행 기록을 사람이 보는 모양으로 정리한다 (화면 기술과 무관, 테스트 가능)."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from beom_workbench.common.response import ToolError
from beom_workbench.core import brief, inbox, port, project, runner
from beom_workbench.core.checkpoints import Checkpoints

STAGES = {
    "request": ["기준·계획", "확인", "구현", "검증", "인수인계"],
    "reference": ["분석", "범위", "원본 기록", "계획", "구현", "검증", "인수인계"],
    "plan": ["파악", "기획서", "정책", "화면", "시나리오", "점검·인계"],
    "free": ["대화"],
}
MODE_TEXT = {"request": "요청사항", "reference": "레퍼런스", "plan": "기획", "free": "자유 대화"}
TOOL_TEXT = {
    "wb_make_requirements": "요구사항 정리", "wb_port_capture": "합격 기준 저장", "wb_port_verify": "검증 실행",
    "wb_port_map": "구현 위치 기록", "wb_port_status": "진행 상태 확인", "wb_plan_set": "계획 작성",
    "wb_plan_progress": "계획 진행 표시", "wb_handoff": "인수인계 작성", "wb_port_slice": "옮길 범위 계산",
    "wb_port_create": "원장 만들기", "wb_features": "기능 목록 확인", "wb_person_ask": "질문",
    "wb_port_accept": "차이 허용 제안", "wb_port_finish": "완료 판정", "wb_inventory_build": "저장소 분석",
    "wb_activity": "진행 상황 알림", "wb_fetch_doc": "공식 문서 받기", "wb_brief_check": "기획 패키지 점검", "wb_brief_screens": "화면 그림 만들기",
    "wb_screen_shot": "화면 찍기", "wb_design_refs": "참고 화면 찍기", "wb_design_check": "디자인 점검", "wb_review": "독립 리뷰", "wb_ui_conventions": "기존 화면 규칙 확인", "wb_run": "격리 밖 실행", "wb_license_scan": "라이선스 확인", "wb_capabilities": "쓸 수 있는 기능 확인",
    "wb_repo_open": "저장소 열기", "wb_repo_status": "저장소 상태 확인", "wb_repo_list": "저장소 목록 확인",
    "wb_graph_query": "구조 지도 찾기", "wb_impact_analyze": "영향 범위 확인", "wb_extension_points": "확장 지점 찾기",
    "wb_report_features": "기능 목록 확인", "wb_report_coverage": "커버리지 확인", "wb_runtime_run": "실행해서 확인",
    "wb_make_create": "원장 만들기", "wb_run_policy": "권한 규칙 확인", "wb_customization_record": "커스텀 기록",
}
# 이름이 이 접두로 시작하는 도구는 한 묶음으로 — 도구 이름이 그대로 보이지 않게
TOOL_GROUP = (("wb_lsp_", "코드 관계 확인"), ("wb_unit", "코드 분석"), ("wb_dataflow_", "데이터 흐름 확인"),
              ("wb_unknown", "확인할 점 기록"), ("wb_claim", "결론 검토"), ("wb_finding", "결론 기록"), ("wb_", "도구 작업"))


# 완료 조건을 보여 줄 순서 — 일이 진행되는 순서대로 (기준 → 확인 → 구현 → 검증 → 기록)
GATE_ORDER = [port.GATE_REQ_CASES, port.GATE_HUMAN, port.GATE_MAPPED, port.GATE_TARGETS, port.GATE_DIFF, port.GATE_ADEQUACY,
              port.GATE_LICENSE, port.GATE_INTEGRITY]

GATE_LABEL = {
    port.GATE_MAPPED: ("모든 요구사항 구현", "옮길 항목 모두 처리"),
    port.GATE_TARGETS: ("구현 위치가 그대로 있음", "옮긴 위치가 그대로 있음"),
    port.GATE_DIFF: ("검증 통과 (지금 코드 기준)", "원본과 같음 (지금 코드 기준)"),
    port.GATE_ADEQUACY: ("케이스가 구현 코드를 모두 실행", "케이스가 옮길 코드를 모두 실행"),
    port.GATE_HUMAN: ("사람 확인 모두 승인", "사람 확인 모두 승인"),
    port.GATE_INTEGRITY: ("기록 무결", "기록 무결"),
    port.GATE_LICENSE: ("라이선스 문제 없음", "라이선스 문제 없음"),
    port.GATE_REQ_CASES: ("요구사항마다 합격 기준", "요구사항마다 합격 기준"),
}


def sig(value) -> str:
    """화면에 보여 준 내용의 지문 — 승인은 이 지문이 지금 내용과 같을 때만 기록한다."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def plan_core(plan: dict) -> dict:
    return {"steps": [{k: s.get(k) for k in ("title", "files", "commands", "requirements")} for s in plan.get("steps") or []],
            "summary": plan.get("summary"), "design": plan.get("design"), "decisions": plan.get("decisions"), "look": plan.get("look"), "previews": plan.get("previews"), "at": plan.get("at"),
            **({"research": plan["research"]} if plan.get("research") else {})}


def _holds_for_plan(run: dict, reviews: int) -> bool:
    """기준은 정리됐지만 계획까지 세우는 중 — 화면이 기준을 계획과 한 장으로 띄우려고 아직 보여 주지 않는 때.
    이때는 사용자 차례가 아니다 (상태·알림이 '내 차례'를 먼저 내지 않게)."""
    return bool(reviews) and run.get("status") == "running" and run.get("mode") in ("request", "reference") \
        and run.get("plan_approval", True) and run.get("confirm_at", "start") != "end" and not (run.get("plan") or {}).get("steps")


def _status(run: dict, asks: int, reviews: int, alive: bool = True) -> tuple[str, str]:
    s = run.get("status")
    if s == "running":
        # 라운드 도중 에이전트가 올린 확인 항목은 에이전트를 멈추지 않는다 — 라운드가 끝나 실행기가 승인을 기다릴 때
        # (waiting_approval) 비로소 사용자 차례다. '내 차례'는 사용자 없이는 더 진행할 수 없을 때만 (질문·권한·승인 대기·수락).
        reviews = 0
    if s in ("running", "waiting_answer") and not alive and not (asks or reviews):
        return "중단됨", "idle"  # 진행하던 실행기가 사라짐 (앱을 닫았거나 비정상 종료) — 이어서 진행으로 재개
    if s not in ("done", "error") and (asks or reviews or s in ("waiting_approval", "waiting_plan")):
        return "내 차례", "turn"
    if s == "waiting_limit":
        return ("한도 대기", "wait") if alive else ("중단됨", "idle")
    if s == "needs_login":
        return "로그인 필요", "check"
    if s == "running":
        return "실행 중", "run"
    if s == "done":
        return ("완료", "ok") if run.get("accepted") else ("내 차례", "turn")
    if s == "stopped_max_rounds":
        return "확인 필요", "check"
    if s == "error":
        return "실패", "fail"
    if s == "waiting_input":
        return ("내 차례", "turn") if alive else ("중단됨", "idle")
    if s == "created":
        return "준비됨", "idle"
    if s == "preparing":
        return "분석 중", "run"
    return "중단됨", "idle"


def _clock(iso: str | None) -> str:
    """'2026-11-14T15:13:00+09:00' → '오후 3:13' (오늘이 아니면 '11/15 오후 3:13')."""
    try:
        t = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return "잠시 뒤"
    hm = f"{'오전' if t.hour < 12 else '오후'} {t.hour % 12 or 12}:{t.minute:02d}"
    return hm if t.date() == datetime.now().astimezone().date() else f"{t.month}/{t.day} {hm}"


PLAN_DOCS = [("기획서", ("기획서.md",)), ("정책서", ("정책서.md",)), ("화면 설계", ("화면설계.md", "wireframes")),
             ("테스트 시나리오", ("테스트시나리오.md",))]


def plan_docs(run: dict) -> list[dict]:
    """기획 문서마다 지금 상태 하나 — 단계 표시줄·'○○ 단계 진행 중'·오른쪽 기획 패키지가 모두 이것을 본다 (각자 세면 서로 달랐다:
    위는 '정책', 아래는 '와이어프레임', 오른쪽은 다 쓴 문서까지 '쓰는 중').
    state: none(없음) · old(이번 실행 전에 있던 문서) · writing(지금 쓰는 문서 — 이번 라운드에 가장 최근에 바뀐 것) ·
    written(이번 실행에 썼고 지금은 다른 문서로 넘어감) · checked(인계 — 점검을 마침)."""
    import time as _t
    from datetime import datetime
    d = brief.folder(run["target_root"])
    try:
        started = datetime.fromisoformat(run["created_at"]).timestamp()
    except (KeyError, ValueError, TypeError):
        started = 0
    rows = []
    for label, names in PLAN_DOCS:
        ts = []
        for n in names:
            f = d / n
            if f.is_dir():  # 화면 HTML 만 — 툴킷이 깐 공통 부품(_kit.css)은 에이전트가 쓴 것이 아니다
                ts += [x.stat().st_mtime for x in f.iterdir() if x.is_file() and x.suffix.lower() in (".html", ".htm")]
            elif f.exists():
                ts.append(f.stat().st_mtime)
        rows.append({"label": label, "file": names[0], "mtime": max(ts) if ts else None})
    fresh = [r for r in rows if r["mtime"] and r["mtime"] >= started - 1]
    busy = run.get("status") == "running" and not run.get("handoff")
    # 지금 쓰는 문서는 '가장 멀리 간' 문서 — 가장 최근에 바뀐 문서로 하면 뒤 문서를 쓰다 앞 문서를 고칠 때 단계가 뒤로 튀었다
    # (전체 기능 시험 165: 시나리오 → 화면). 네 문서가 다 있으면 쓰는 문서는 없다 — 앞 문서를 고치는 것은 '점검·인계'다
    now = fresh[-1] if busy and fresh and len(fresh) < len(rows) and _t.time() - max(r["mtime"] for r in fresh) < 600 else None
    for r in rows:
        if not r["mtime"]:
            r["state"] = "none"
        elif r["mtime"] < started - 1:
            r["state"] = "old"
        elif run.get("handoff") or run.get("accepted"):
            r["state"] = "checked"
        else:
            r["state"] = "writing" if r is now else "written"
    return rows


def _plan_marks(run: dict) -> list[str]:
    """기획 단계마다 'done'·'now'·'' — plan_docs 하나에서. 쓰는 문서가 '지금', 이번에 쓴 문서는 끝남, 없거나 앞 작업의 문서는 비움.
    쓰는 문서가 없으면(라운드 사이) 처음 빠진 단계가 '지금'."""
    if run.get("accepted"):
        return ["done"] * len(STAGES["plan"])
    docs = plan_docs(run)
    marks = ["done" if any(r["state"] != "none" for r in docs) else "now"]
    marks += ["now" if r["state"] == "writing" else "done" if r["state"] in ("written", "checked") else "" for r in docs] + [""]
    if run.get("handoff") or (docs and all(r["state"] in ("written", "checked") for r in docs)):
        marks[-1] = "now"  # 네 문서를 다 썼다 — 점검·인계 (앞 문서를 다시 고쳐도 단계는 뒤로 가지 않는다)
    elif "now" not in marks:
        first = next((i for i, m in enumerate(marks[1:-1], 1) if not m), None)
        marks[first if first is not None else len(marks) - 1] = "now"
    return marks


def _stage(run: dict, st: dict | None) -> int:
    """지금 단계 번호. 인수인계를 수락하면 마지막 단계까지 끝난 것(= 단계 수)."""
    mode = run["mode"]
    last = len(STAGES[mode]) - 1
    if mode == "free":
        return 0
    if run.get("accepted"):
        return last + 1
    if mode == "plan":  # 지금 쓰고 있는 문서 — 단계 표시줄(_plan_marks)과 같은 판단 (따로 세면 위·아래가 다른 단계를 말했다)
        marks = _plan_marks(run)
        return marks.index("now") if "now" in marks else last
    if run.get("status") == "done" or run.get("handoff"):
        return last
    if st is None:
        return 0
    if run.get("revision_round") and run.get("revision_round") == run.get("round") and not run.get("revision_touched") \
            and run.get("status") == "running":
        return 0  # 추가 요청을 받은 새 한 바퀴 — 손대기 전에는 처음 단계부터 (기준을 고치면 0→1→…, 바로 고치면 0→3 으로 앞으로만)
    g, plan = st["gates"], run.get("plan") or {}
    if mode == "request":  # 기준·계획(함께 세움) → 확인(한 번에) → 구현 → 검증 → 인수인계
        req = st.get("requirements") or {}
        if run.get("revision_round") and run.get("revision_round") == run.get("round") and run.get("revision_touched")                 and st["approvals"].get("criteria", st["approvals"]["pending"]):
            # 추가 요청 라운드에서 기준을 고쳐 확인을 기다린다 — 고친 기준의 케이스를 다시 맞추는 동안(게이트가 잠시 비어도) '기준'으로
            # 되돌아가 보이지 않게 먼저 본다 (전체 기능 시험 164: 같은 라운드에서 3 → 0 으로 뒤로 튐)
            return 1
        if not req.get("total") or not g.get(port.GATE_REQ_CASES) or not plan.get("steps"):
            return 0
        # 확인 단계 — 승인을 기다리는 실행은 계획 승인 전, 계획 승인만 끈 실행은 기준 승인 전. 끝까지 맡김은 마지막에 확인하니 없다.
        # 승인한 뒤 구현하면서 케이스를 고쳐 생긴 확인 카드는 구현 단계 안의 카드다 (단계를 뒤로 돌리지 않는다)
        criteria_open = st["approvals"].get("criteria", st["approvals"]["pending"]) and not req.get("implemented")
        if run.get("revision_round") and run.get("revision_round") == run.get("round") and run.get("revision_touched") \
                and st["approvals"].get("criteria", st["approvals"]["pending"]):
            return 1  # 추가 요청 라운드에서 기준을 고쳐 확인을 기다린다 — 예전 승인·매핑을 보고 검증 단계로 건너뛰지 않는다
        if run.get("confirm_at", "start") != "end":
            if run.get("plan_approval", True) and not plan.get("approved"):
                return 1
            if not run.get("plan_approval", True) and criteria_open:
                return 1
        if not g[port.GATE_MAPPED]:
            return 2
        return 3
    if st["counts"].get("pending"):
        return 1
    if st.get("last_check") is None and "capture" in st["next_step"]:
        return 2
    if not plan.get("approved"):
        return 3
    if not g[port.GATE_MAPPED] or st.get("last_check") is None:
        return 4
    return 5


_SHELL_WRAP = re.compile(r"""^\s*"?[^"]*?(?:powershell|pwsh|cmd|bash|sh)(?:\.exe)?"?\s+(?:-NoProfile\s+)?(?:-Command|-c|/c)\s+(.*)$""", re.I | re.S)


def _inner_command(cmd: str) -> str:
    """에이전트가 셸로 감싼 명령에서 실제 명령만 — '"...powershell.exe" -Command 'npm test'' → 'npm test'."""
    m = _SHELL_WRAP.match(cmd)
    if not m:
        return cmd
    inner = m.group(1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        inner = inner[1:-1]
    return inner


_SCRATCH = re.compile(r"(^|/)(tool-results|tmp|temp|scratchpad)/", re.I)  # 에이전트가 긴 결과를 적어 두는 곳 (.beom/tmp 포함)
_LOOK = {"ls": "폴더 살펴보기", "dir": "폴더 살펴보기", "tree": "폴더 살펴보기", "find": "파일 찾기", "get-childitem": "폴더 살펴보기",
         "cat": "파일 읽기", "head": "파일 읽기", "tail": "파일 읽기", "type": "파일 읽기", "get-content": "파일 읽기", "sed": "파일 읽기",
         "wc": "파일 읽기", "grep": "코드 찾아보기", "rg": "코드 찾아보기", "select-string": "코드 찾아보기", "findstr": "코드 찾아보기",
         "git": "저장소 기록 확인", "for": "여러 파일 살펴보기", "while": "여러 파일 살펴보기", "mkdir": "폴더 만들기",
         "echo": "", "cd": "", "set-location": "", "cut": "파일 읽기", "sort": "파일 읽기", "uniq": "파일 읽기", "awk": "파일 읽기", "xargs": "", "tr": "", "do": "", "done": ""}


def _scratch_file(path: str) -> bool:
    return bool(path) and bool(_SCRATCH.search(path.replace("\\", "/")))


def _command_label(cmd: str) -> str:
    """셸 명령을 사람이 읽는 한 줄로 — 'cd /d/work/admin-web && ls -a && cat package.json' → '폴더 살펴보기 · work/admin-web'.
    살펴보기·읽기·찾기가 아닌 명령(빌드·테스트·설치)은 그 프로그램 이름을 보인다. 명령 전체는 펼친 기록에 남는다."""
    cmd = cmd.strip()
    if not cmd:
        return "명령 실행"
    where = ""
    m = re.match(r"\s*(cd|set-location)\s+(\"[^\"]+\"|'[^']+'|\S+)", cmd, re.I)
    if m:
        parts = [x for x in m.group(2).strip("\"'").replace("\\", "/").split("/") if x]
        if parts and re.fullmatch(r"[a-zA-Z]:?", parts[0]):  # 드라이브 (/d/…, D:/…)
            parts = parts[1:]
        where = "/".join(parts[-2:])
    bare = re.sub(r'"(?:\\.|[^"\\])*"|\'[^\']*\'', "Q", cmd)  # 따옴표 안의 | ; 는 구분자가 아니다 (grep "a\|b")
    words = [w.split()[0].lower() for w in re.split(r"&&|;|\|\||\|", bare) if w.strip()]
    kinds = [_LOOK.get(w.split("/")[-1], None) for w in words]
    real = [k for k in kinds if k != ""]
    if real and all(k for k in real):
        label = real[0] if len(set(real)) == 1 else ("폴더 살펴보기" if "폴더 살펴보기" in real else real[0])
    elif not real:
        label = "명령 실행"
    else:
        prog = next((w.split("/")[-1] for w, k in zip(words, kinds) if k is None), words[0] if words else "")
        label = f"실행 · {prog}" if re.fullmatch(r"[\w.+-]+", prog or "") else "명령 실행"  # 잘린 기록의 조각(target\")은 이름이 아니다
    return f"{label} · {where}" if where else label


def _tool_label(name: str | None, raw: str | None) -> str:
    name = name or ""
    short = name.split("__")[-1]
    if short in TOOL_TEXT:
        return TOOL_TEXT[short]
    try:
        inp = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        inp = {}
        # 기록은 입력을 잘라 둔다 — 깨진 JSON 에서도 이름 붙일 칸만 꺼낸다
        for k in ("command", "description", "file_path", "path", "url", "query"):
            m = re.search(rf'"{k}":\s*"((?:\\.|[^"\\])*)', str(raw or ""))
            if m:
                try:
                    inp[k] = json.loads('"' + m.group(1).rstrip("\\") + '"')
                except json.JSONDecodeError:
                    inp[k] = m.group(1)
    inp = inp if isinstance(inp, dict) else {}
    path = inp.get("file_path") or inp.get("path") or ""
    tail = path.replace("\\", "/").split("/")[-1] if path else ""
    if short in ("Write", "Edit", "MultiEdit"):
        return f"{tail} 수정" if tail else "파일 수정"
    if short in ("Read",):
        # 에이전트가 긴 결과를 잠시 적어 둔 파일(무작위 이름·임시 폴더) — 이름이 깨진 글자처럼 보인다
        if _scratch_file(path):
            return "작업 메모 읽기"
        return f"{tail} 읽기" if tail else "파일 읽기"
    if short in ("Glob", "Grep", "LS"):
        return "코드 찾아보기"
    if short in ("Bash", "PowerShell"):
        return _command_label(_inner_command(str(inp.get("command", ""))))
    if short == "WebSearch":
        q = str(inp.get("query") or "").strip()
        return f"웹 검색 · {q[:40]}" if q else "웹 검색"
    if short == "WebFetch":
        host = re.sub(r"^https?://(www\.)?", "", str(inp.get("url") or "")).split("/")[0]
        return f"웹 페이지 읽기 · {host}" if host else "웹 페이지 읽기"
    if short in ("Agent", "Task"):  # 에이전트가 일부를 도우미 에이전트에게 맡김 (조사 등)
        what = str(inp.get("description") or "").strip()
        return f"도우미에게 맡김 · {what[:40]}" if what else "도우미에게 맡김"
    if short in ("ToolSearch",):
        return "쓸 도구 불러오기"
    if short in ("Skill",):
        return "스킬 사용"
    if short in ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "update_plan"):
        return "할 일 정리"
    for prefix, label in TOOL_GROUP:
        if short.startswith(prefix):
            return label
    return short or "도구"


_HANGUL = re.compile(r"[가-힣]")
_EVENTS: dict[str, tuple[int, list[dict]]] = {}


def _events(settings, run_id: str) -> list[dict]:
    """실행 기록 — 지난번에 읽은 곳부터 새로 붙은 줄만 읽는다 (긴 실행에서 1.5초마다 처음부터 다시 읽지 않게)."""
    off, evs = _EVENTS.get(run_id, (0, []))
    try:
        size = (inbox.run_dir(settings, run_id) / "events.jsonl").stat().st_size
    except OSError:
        size = 0
    if size < off:  # 파일이 새로 시작됐다 (지우고 다시 만듦)
        off, evs = 0, []
    if size > off:
        new, off = inbox.events(settings, run_id, off)
        evs = evs + new
        if len(_EVENTS) > 50:
            _EVENTS.clear()
        _EVENTS[run_id] = (off, evs)
    return evs


def _lessons(settings, run: dict) -> list[dict]:
    """이 폴더에서 사용자가 '다음에도 이렇게'라고 남긴 것 — 대화창에서 보이고 지울 수 있다."""
    from beom_workbench.core import lessons
    try:
        return lessons.load(settings, run.get("target_root") or "")["rules"] if run.get("target_root") else []
    except Exception:  # noqa: BLE001
        return []


def feed(settings, run: dict, live: bool = False) -> list[dict]:
    """대화 흐름. live: 지금 라운드가 도는 중 — 마지막 라운드의 작업은 '진행 중' 하나로 접어 지금 하는 일과 함께 보여 준다."""
    evs = _events(settings, run["id"])
    out: list[dict] = []
    asks: dict[str, dict] = {}
    korean = bool(_HANGUL.search(run.get("request") or run.get("feature") or ""))
    # 툴킷이 돌려보낸 도구 — 그 도구를 다시 낼 때까지 에이전트가 남긴 말은 툴킷과 주고받은 말이다 ("계획 검사에 걸려 다시 내는 중").
    # 사용자의 대화 흐름에 남기지 않는다 (실행 기록에는 그대로 있다). 글을 읽지 않고 기록의 순서(거절 → 다시 냄)로 가른다
    refused: set[str] = set()
    if run.get("request"):
        out.append({"type": "request", "text": run["request"], "feature": run.get("feature")})
    elif run.get("feature"):
        out.append({"type": "request", "text": run["feature"]})
    taught = [ln for block in run.get("lessons") or [] for ln in str(block).splitlines()[1:] if ln.startswith("- ")]
    if taught:  # 같은 폴더의 앞 작업에서 사용자가 가르친 것 — 에이전트가 처음부터 읽는다
        out.append({"type": "note", "tone": "", "text": f"이 폴더의 앞 작업에서 사용자가 남긴 것 {len(taught)}개를 처음부터 따릅니다",
                    "list": [ln[2:] for ln in taught][:20]})
    for e in evs:
        k = e.get("kind")
        before = len(out)
        if k == "round_start":
            refused.clear()  # 돌려받은 채 끝난 라운드 — 다음 라운드의 말은 툴킷과 주고받은 말이 아니다
            out.append({"type": "divider", "text": f"라운드 {e.get('round')}"})
        elif k == "refused":
            refused.add(str(e.get("tool") or "").split("__")[-1])
        elif k == "agent":
            for it in e.get("items") or []:
                if it.get("text") and refused:
                    out.append({"type": "message", "text": it["text"], "aside": True})
                elif it.get("text"):
                    # 한국어 요청인데 한글이 하나도 없는 짧은 줄 — 도구 사이의 영어 혼잣말("Now the plan.")이다. 보여 주지 않는다
                    chatter = korean and len(it["text"]) < 300 and not _HANGUL.search(it["text"])
                    out.append({"type": "message", "text": it["text"], **({"chatter": True} if chatter else {})})
                elif it.get("tool") and not str(it["tool"]).endswith(("wb_activity", "wb_port_status")):
                    refused.discard(str(it["tool"]).split("__")[-1])
                    label = _tool_label(it["tool"], it.get("input"))
                    if out and out[-1]["type"] == "tools":
                        if label not in out[-1]["items"]:
                            out[-1]["items"].append(label)
                    else:
                        out.append({"type": "tools", "items": [label]})
        elif k == "activity" and e.get("text"):
            # 진행 알림('정책서.md 작성 중')은 지나가도 남긴다 — 작업 과정을 펼치면 위에서 무엇을 했는지 차례로 보인다
            out.append({"type": "activity", "text": str(e["text"])[:200]})
        elif k == "user_message":
            out.append({"type": "user", "text": e.get("text", "")})
        elif k == "checkpoint":
            out.append({"type": "checkpoint", "id": e.get("id"), "label": e.get("label")})
        elif k == "approved":
            n = len(e.get("items") or [])
            out.append({"type": "note", "tone": "ok", "text": f"바꾼 결정에 맞춘 기준 변경 {n}건 — 사용자 결정이라 자동 승인" if e.get("auto")
                        else f"기준 승인함 · {n}건"})
        elif k == "ask":
            item = {"type": "qa" if e.get("ask_kind") == "question" else "perm", "id": e.get("id"),
                    "question": str(e.get("summary") or ""), "answer": None}
            asks[str(e.get("id"))] = item
            if item["type"] == "qa":
                out.append(item)
        elif k == "answer":
            item = asks.get(str(e.get("id")))
            a = e.get("answer") or {}
            if item and item["type"] == "qa":
                item["answer"] = "(답 없이 시간이 지남)" if a.get("timeout") else str(a.get("text") or a.get("choice") or a.get("answer") or "")
                item["answered_at"] = e.get("at")
            elif item:
                out.append({"type": "note", "tone": "" if a.get("allow") else "warn",
                            "text": f"권한 확인 — {'허용' if a.get('allow') else '거절'}{' (앞으로 항상)' if a.get('always') else ''} · {item['question'][:140]}"})
        elif k == "plan_approved":
            # 승인한 카드는 사라져도 무엇을 승인했는지 펼쳐 볼 수 있게 남긴다
            out.append({"type": "note", "text": "계획 승인함" + (f" — 결정 바꿈: {', '.join(e['changed'])}" if e.get("changed") else ""),
                        **({"list": e["summary"]} if e.get("summary") else {})})
        elif k == "restored":
            out.append({"type": "note", "text": f"체크포인트로 되돌림 · {e.get('label', '')}"})
        elif k == "merged":
            out.append({"type": "note", "tone": "ok" if e.get("ok") else "warn", "text": str(e.get("text") or "")})
        elif k == "permission" and e.get("decision") == "refuse":
            kind = inbox.KINDS.get(e.get("permission_kind"), e.get("permission_kind") or "")
            what = _inner_command(str(e.get("what") or ""))[:120]
            out.append({"type": "note", "tone": "warn",
                        "text": f"권한 규칙으로 막음 — {kind}{' · ' + what if what else ''} (에이전트는 다른 방법으로 이어 갑니다)"})
        elif k == "agent_result" and e.get("is_error"):
            continue  # 원인은 바로 뒤 round_error 가 사람이 읽는 말로 보여 준다
        elif k == "round_error":
            if e.get("cause") not in ("limit", "transient", "auth", "context"):
                out.append({"type": "note", "tone": "fail", "text": f"라운드가 오류로 끝남 · {e.get('text') or e.get('subtype') or ''}"[:300]})
        elif k == "limit_wait":
            when = _clock(e.get("until"))
            out.append({"type": "note", "tone": "wait", "until": e.get("until"),
                        "text": (f"{e.get('agent') or '에이전트'} 사용 한도에 걸렸습니다 — {when}에 이 라운드를 다시 시작합니다"
                                 if e.get("known") else f"{e.get('agent') or '에이전트'} 사용 한도에 걸린 것으로 보입니다 — {when}에 다시 시도합니다")
                                + " (라운드는 쓰지 않습니다. 메시지를 보내면 바로 다시 시도)", "detail": e.get("text")})
        elif k == "retry_wait":
            out.append({"type": "note", "tone": "warn", "text": f"연결 오류로 {e.get('seconds')}초 뒤 다시 시도합니다 ({e.get('attempt')}번째) · {e.get('text') or ''}"[:300]})
        elif k == "auth_needed":
            out.append({"type": "note", "tone": "fail", "text": f"{e.get('agent') or '에이전트'} 로그인이 필요합니다 — {e.get('hint') or '터미널에서 로그인한 뒤'} 이어서 진행을 누르세요"[:300],
                        "detail": e.get("text")})
        elif k in ("agent_error", "error"):
            msg = (e.get("message") or e.get("stderr") or "").strip()
            if not msg and k == "agent_error":  # 에이전트가 까닭을 남기지 않고 끝났다 — '오류' 한 단어로는 무엇을 해야 할지 모른다
                msg = (f"에이전트가 까닭을 남기지 않고 끝났습니다 (종료 코드 {e.get('exit_code')})"
                       + (f" — 마지막 출력: {str(e.get('last_output'))[-200:]}" if e.get("last_output") else "")
                       + " · 이어서 진행을 누르거나, 터미널에서 claude/codex 를 한 번 실행해 로그인·업데이트 상태를 확인하세요")
            out.append({"type": "note", "tone": "fail", "text": (msg or "오류")[-600:]})
        elif k == "finished":
            out.append({"type": "note", "tone": "ok", "text": "완료 조건을 모두 채웠습니다 — 완료 판정 기록"})
        elif k == "max_rounds":
            out.append({"type": "note", "tone": "warn", "text": "정한 반복 횟수를 모두 썼습니다 — 메시지를 보내면 이어서 진행합니다"})
        elif k == "tampered":
            out.append({"type": "note", "tone": "fail", "text": "승인·규칙이 앱을 거치지 않고 바뀌어 되돌리고 멈췄습니다 — " + " / ".join(e.get("what") or [])
                        + ". 에이전트가 실행한 코드가 툴킷 기록을 고친 것일 수 있습니다. 바뀐 내용(변경 탭)을 확인한 뒤 '확인했음'을 누르면 이어 갈 수 있습니다."})
        elif k == "tamper_ack":
            out.append({"type": "note", "text": "앱 밖에서 바뀐 결정을 확인했습니다 — 이어 갈 수 있습니다"})
        elif k == "branch_commit":
            out.append({"type": "note", "text": f"{(e.get('repo') + ': ') if e.get('repo') else ''}작업 브랜치 {e.get('branch')} 에 커밋 · {e.get('sha')} (라운드 {e.get('round')})"})
        elif k == "note" and e.get("text"):
            out.append({"type": "note", "tone": e.get("tone") or "", "text": str(e["text"])[:1000], "at": e.get("at")})
        elif k == "stalled":
            out.append({"type": "note", "tone": "warn", "text": "인수인계를 남긴 뒤 더 진행할 것이 없어 멈췄습니다 — 인수인계의 남은 일(설치·결정 등)을 해결하거나 메시지를 보내면 이어 갑니다"
                        if e.get("handoff") else f"{e.get('rounds') or runner.STALL_ROUNDS}라운드 연속 진행이 없어 멈췄습니다 — 에이전트 응답을 확인하고 지시를 보내 주세요"})
        for item in out[before:]:
            item.setdefault("at", e.get("at"))
    return _fold_work(_drop_chatter(out), live)[-400:]


def _drop_chatter(items: list[dict]) -> list[dict]:
    """영어 혼잣말("Now the plan.")은 빼되, 라운드의 마지막 메시지(보고)는 영어여도 남긴다."""
    keep = []
    for i, it in enumerate(items):
        if it.get("aside"):
            later = next((x for x in items[i + 1:] if x.get("type") in ("message", "divider")), None)
            if later is not None and later.get("type") == "message":
                continue
            it = {k: v for k, v in it.items() if k != "aside"}
        if it.get("chatter"):
            later = next((x for x in items[i + 1:] if x.get("type") in ("message", "divider")), None)
            if later is not None and later.get("type") == "message":
                continue
            it = {k: v for k, v in it.items() if k != "chatter"}
        keep.append(it)
    return keep


def _fold_work(items: list[dict], live: bool = False) -> list[dict]:
    """라운드마다 마지막 에이전트 메시지는 '보고'로 보여 주고, 그 앞의 도구 사용·중간 메시지는 '작업 과정' 하나로 접는다.
    (도구 칩과 혼잣말이 줄줄이 쌓여 정작 보고가 묻히지 않게 — 알림·오류·사용자 메시지·체크포인트는 그대로 둔다)"""
    out: list[dict] = []
    seg: list[dict] = []

    def flush(final: bool = False):
        msgs = [i for i, it in enumerate(seg) if it["type"] == "message"]
        last = msgs[-1] if msgs and not (final and live) else None  # 도는 중인 라운드는 아직 '보고'가 없다
        work = None
        for i, it in enumerate(seg):
            if it["type"] in ("tools", "message", "activity") and i != last:
                if work is None:
                    work = {"type": "work", "steps": [], "tools": 0, "notes": 0, "at": it.get("at"), **({"live": True} if final and live else {})}
                    out.append(work)
                if it["type"] == "tools":
                    work["steps"].append({"kind": "tools", "items": it["items"]})
                    work["tools"] += len(it["items"])
                elif it["type"] == "activity":
                    work["steps"].append({"kind": "activity", "text": it["text"]})
                    work["acts"] = work.get("acts", 0) + 1
                else:
                    work["steps"].append({"kind": "note", "text": it["text"]})
                    work["notes"] += 1
            elif i == last:
                out.append({**it, "type": "report"})
            else:
                out.append(it)
        seg.clear()

    for it in items:
        if it["type"] in ("divider", "qa", "user"):  # 질문·사용자 메시지에서 끊는다 — 그 뒤에 한 일이 그 앞에 접혀 순서가 뒤바뀌었다
            flush()
            out.append(it)
        else:
            seg.append(it)
    flush(final=True)
    if live and not any(it.get("live") for it in out):  # 라운드가 막 시작해 아직 아무 출력이 없을 때도 '진행 중' 자리를 둔다
        out.append({"type": "work", "steps": [], "tools": 0, "notes": 0, "live": True})
    return out


def _reviews(reviews: list[dict], conn, port_id: str) -> dict:
    cap = port._last_check(conn, port_id, "capture")
    exps = (cap or {}).get("detail", {}).get("expectations") or {}
    req_of: dict[str, list[str]] = {}
    for r, cases in ((cap or {}).get("detail", {}).get("req_cover") or {}).items():
        for c in cases:
            req_of.setdefault(c, []).append(r)
    out = {"requirements": [], "expectations": [], "divergences": [], "omitted": [], "case_changes": [], "checks": [],
           "tests_changed": [], "count": len(reviews)}
    for h in reviews:
        if h["kind"] == "requirements":
            out["requirements"] = [{"id": d["case"], "text": d["diff"]} for d in h["detail"]]
        elif h["kind"] == "expectation":
            name = h["item_id"].split(":", 1)[1]
            ex = exps.get(name) or {}
            out["expectations"].append({"case": name, "description": ex.get("description"), "expect": ex.get("expect"),
                                        "new": ex.get("new"), "input": ex.get("input"),
                                        "order": ex.get("order", 0), "source": ex.get("source"), "reqs": req_of.get(name, [])})
        elif h["kind"] == "divergence":
            out["divergences"].append({"summary": h["summary"], "detail": h.get("detail")})
        elif h["kind"] == "omitted":
            out["omitted"].append({"summary": h["summary"].removeprefix("생략: ")})
        elif h["kind"] == "tests_changed":
            out["tests_changed"] = [{"file": d["case"], "diff": d["diff"]} for d in h.get("detail") or []]
        elif h["kind"] in ("coverage_unmeasured", "impact", "screen_rules"):  # 툴킷이 재지 못한 파일 · 케이스가 거치지 않은 연관된 곳
            out["checks"].append({"summary": h["summary"], "files": (h.get("detail") or {}).get("files") or []})
        else:
            out["case_changes"].append(h.get("detail"))
    # 케이스 파일 순서대로, 순서 기록이 없으면 숫자를 숫자로 비교 (#10 이 #2 앞에 오지 않게)
    out["expectations"].sort(key=lambda e: (e["order"], [(0, int(t), "") if t.isdigit() else (1, 0, t)
                                                          for t in re.split(r"(\d+)", e["case"]) if t]))
    return out


GATE_HELP = {
    port.GATE_MAPPED: "요구사항(또는 옮길 항목)마다 구현했는지·뺐는지 정해졌다",
    port.GATE_TARGETS: "구현했다고 적은 코드 위치(파일:줄)가 지금도 그대로 있다",
    port.GATE_DIFF: "승인된 합격 케이스를 지금 코드로 실제 실행해 기대 결과와 모두 같았다",
    port.GATE_ADEQUACY: "케이스를 돌리는 동안 구현한 코드가 실제로 실행됐다 (커버리지) — 케이스가 헛돌지 않았다",
    port.GATE_HUMAN: "사람이 확인해야 할 항목(요구사항·합격 기준·차이 허용)이 모두 승인됐다",
    port.GATE_INTEGRITY: "원장·골든 기록이 툴킷 밖에서 고쳐지지 않았다 (해시 사슬)",
    port.GATE_LICENSE: "가져온 코드의 라이선스 문제가 없다",
    port.GATE_REQ_CASES: "요구사항마다 그것을 확인하는 합격 케이스가 하나 이상 있다",
    port.GATE_PROJECT: "검증할 때 툴킷이 이번 작업에서 만든 테스트, 원래 있던 테스트(회귀), 타입 검사를 함께 돌려 모두 통과했다 (작업 전부터 실패하던 테스트는 사용자가 확인하면 막지 않는다 · 없으면 해당 없음)",
}


_NO_SERVER = re.compile(r"10061|refused|연결을 거부|ECONNREFUSED|Connection reset|10054", re.I)
_TIMEOUT = re.compile(r"timed? ?out|시간 초과|TimeoutExpired", re.I)


def failure_cause(case: dict) -> dict | None:
    """실패한 케이스의 원인을 사람 말로 — {key, title, detail, hint}. key 가 같은 것끼리 위에서 묶어 보여 준다."""
    if case.get("ok") is not False:
        return None
    act = case.get("actual") or {}
    err = str(act.get("error") or "")
    diffs = [str(d) for d in case.get("diffs") or []]
    first = diffs[0] if diffs else ""
    if err or first.startswith("대상 오류"):
        text = err or first
        if _NO_SERVER.search(text):
            return {"key": "no_server", "title": "서버에 연결하지 못함",
                    "hint": "검증할 때 대상 서버가 떠 있지 않았습니다 — 빌드·실행이 안 됐거나 주소·포트가 다릅니다. 서버를 띄울 수 있게 되면 다시 검증합니다."}
        if _TIMEOUT.search(text):
            return {"key": "timeout", "title": "응답 시간 초과", "hint": "정해진 시간 안에 응답이 오지 않았습니다 — 서버가 멈췄거나 너무 느립니다."}
        short = re.sub(r"^대상 오류:\s*", "", text).split("\n")[0][:160]
        return {"key": "run_error:" + short[:40], "title": "실행 오류", "detail": short, "hint": "명령·요청을 실행하지 못했습니다."}
    m = re.match(r"status (\S+) != 기대 (\S+)", first)
    if m:
        return {"key": f"status:{m.group(2)}:{m.group(1)}", "title": "응답 코드가 다름", "detail": f"기대 {m.group(2)} · 실제 {m.group(1)}"}
    m = re.match(r"(.+?): 대상에 없음 \(기대 (.*)\)$", first)
    if m:
        return {"key": "missing", "title": "응답에 기대한 값이 없음", "detail": f"{m.group(1)} (기대 {m.group(2)})"}
    m = re.match(r"(.+?): (.*) != 기대 (.*)$", first)
    if m:
        return {"key": "value", "title": "값이 다름", "detail": f"{m.group(1)} — 기대 {m.group(3)} · 실제 {m.group(2)}"[:240]}
    m = re.match(r"(.+?): (.*) 가 기대 /", first)
    if m:
        return {"key": "pattern", "title": "응답 내용이 기대 형식과 다름", "detail": f"{m.group(1)} 에서 기대한 내용을 찾지 못함"}
    if first.startswith("화면에 없음"):
        return {"key": "screen_text", "title": "화면에 기대한 글자가 안 보임", "detail": first.split(":", 1)[-1].strip()[:200]}
    if first.startswith("요소 없음"):
        return {"key": "screen_el", "title": "화면에 기대한 요소가 없음", "detail": first.split(":", 1)[-1].strip()[:200]}
    if first.startswith("스크립트 오류"):
        return {"key": "screen_js", "title": "화면 스크립트 오류", "detail": first.split(":", 1)[-1].strip()[:200]}
    if first.startswith("body"):
        return {"key": "body", "title": "응답 본문이 기대와 다름"}
    return {"key": "other", "title": "기대와 다름", "detail": first[:200] or None}


def _causes(cases: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for c in cases:
        cause = c.get("cause")
        if not cause:
            continue
        g = groups.setdefault(cause["key"], {"title": cause["title"], "hint": cause.get("hint"), "cases": []})
        g["cases"].append(c.get("description") or c["name"])
    return sorted(groups.values(), key=lambda g: -len(g["cases"]))


def _spec_view(conn, port_id: str, rows: list[dict], last: dict | None) -> dict:
    """합의한 것(요구사항·합격 기준·승인)과 검증한 것(케이스별 입력 → 기대 → 실제)을 한 화면에."""
    cap = port._last_check(conn, port_id, "capture") or {}
    exps = (cap.get("detail") or {}).get("expectations") or {}
    cover = (cap.get("detail") or {}).get("req_cover") or {}
    result = {r["name"]: r for r in rows}
    cases = []
    for name, e in sorted(exps.items(), key=lambda kv: kv[1].get("order", 0)):
        r = result.get(name)
        cases.append({"name": name, "description": e.get("description"), "input": e.get("input"), "expect": e.get("expect"),
                      "reqs": [k for k, v in cover.items() if name in v],
                      "ok": r["ok"] if r else None, "actual": (r or {}).get("actual"), "diffs": (r or {}).get("diffs") or [],
                      "screenshot": (r or {}).get("screenshot")})
        cases[-1]["cause"] = failure_cause(cases[-1])
    appr = conn.execute("SELECT at, payload FROM port_log WHERE port_id = ? AND action = 'approve' ORDER BY rowid DESC LIMIT 1",
                        (port_id,)).fetchone()
    who = None
    if appr:
        try:
            p = json.loads(appr["payload"] or "{}")
            who = {"at": appr["at"], "by": p.get("approved_by"), "channel": p.get("channel")}
        except ValueError:
            who = {"at": appr["at"]}
    return {"cases": cases, "causes": _causes(cases), "approved": who, "verified_at": (last or {}).get("created_at"),
            "gates_help": GATE_HELP}


# ---------------------------------------------------------------- 다시 계산 줄이기
# 앱은 1.5초마다 목록·상세를 묻는다. 실행 파일·이벤트·질문함·원장·잠금이 그대로면 지난 결과를 그대로 쓴다.
_MEMO: dict = {}


def _stat(p: Path) -> tuple | None:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _run_stamp(settings, run: dict) -> tuple:
    d = inbox.run_dir(settings, run["id"])
    out = [_stat(d / "run.json"), _stat(d / "events.jsonl"), _stat(d / "inbox"), _stat(d / runner.LOCK_FILE)]
    if run.get("project_id"):
        ws = project.workspace(settings, run["project_id"])
        out += [_stat(ws / "ledger.db"), _stat(ws / "ledger.db-wal")]
    return tuple(out)


def _memo(key, stamp, fn, max_age: float | None = None):
    hit = _MEMO.get(key)
    now = time.monotonic()
    if hit and hit[0] == stamp and (max_age is None or now - hit[2] < max_age):
        return hit[1]
    value = fn()
    if len(_MEMO) > 300:  # 실행이 많아도 메모가 끝없이 커지지 않게 — 오래 안 본 것부터
        for k, _ in sorted(_MEMO.items(), key=lambda kv: kv[1][2])[:100]:
            _MEMO.pop(k, None)
    _MEMO[key] = (stamp, value, now)
    return value


def summary(settings, run: dict) -> dict:
    alive = runner.lock_owner(settings, run["id"]) is not None
    return dict(_memo(("summary", run["id"]), (_run_stamp(settings, run), alive), lambda: _summary(settings, run)))


def _summary(settings, run: dict) -> dict:
    asks = len(inbox.pending(settings, run["id"]))
    reviews = 0
    if run.get("port_id") and run.get("status") not in ("done",):
        try:
            reviews = len(port.pending_reviews(project.load(settings, run["project_id"])[0], run["port_id"]))
        except ToolError:
            reviews = 0
    label, tone = _status(run, asks, reviews, runner.lock_owner(settings, run["id"]) is not None)
    return {"id": run["id"], "name": run.get("name"), "mode": run["mode"], "mode_text": MODE_TEXT[run["mode"]], "archived": bool(run.get("archived")),
            "status": label, "tone": tone, "state": run.get("status"), "created_at": run.get("created_at"), "agent": run.get("agent", "claude"),
            "target_root": run.get("target_root") or ""}  # 작업 목록 찾기 (폴더 이름으로도)


def runs(settings) -> list[dict]:
    return [summary(settings, r) for r in inbox.list_runs(settings)]


def _gate_label(name: str, mode: str, at_finish: bool) -> str:
    label = GATE_LABEL.get(name, (name, name))[0 if mode == "request" else 1]
    return label.replace("(지금 코드 기준)", "(완료할 때 코드)") if at_finish else label


def detail(settings, run_id: str) -> dict:
    run = inbox.load_run(settings, run_id)
    # 대상 폴더를 사람이 직접 고친 것(지문)은 기록 파일에 안 남으니 10초마다는 새로 본다
    return dict(_memo(("detail", run_id), _run_stamp(settings, run), lambda: _detail(settings, run_id), max_age=10))


def _detail(settings, run_id: str) -> dict:
    run = inbox.load_run(settings, run_id)
    try:
        st = runner.status(settings, run, fp_max_age=15)  # 화면 표시 — 큰 폴더를 매번 다 걷지 않게
    except ToolError:
        st = None
    conn = project.load(settings, run["project_id"])[0] if st else None
    reviews = port.pending_reviews(conn, run["port_id"]) if conn is not None else []
    asks = inbox.pending(settings, run_id)
    label, tone = _status(run, len(asks), len(reviews), runner.lock_owner(settings, run_id) is not None)
    mode = run["mode"]
    out = {"run": {k: run.get(k) for k in ("id", "name", "mode", "status", "round", "max_rounds", "target_root", "request",
                                           "feature", "agent", "model", "effort", "accepted", "cost_usd", "created_at",
                                           "limit_until", "error", "streaming", "work_branch", "tampered", "sandbox", "tokens", "cmd_rules", "depth", "auth")},
           "mode_text": MODE_TEXT[mode], "status": label, "tone": tone, "stages": STAGES[mode], "stage": _stage(run, st),
           **({"stage_marks": _plan_marks(run), "plan_docs": plan_docs(run)} if mode == "plan" else {}),
           "activity": _activity(run, asks, len(reviews), bool(st and (st.get("license") or {}).get("acked") is False)), "plan": run.get("plan"), "handoff": run.get("handoff"), "asks": asks,
           "feed": feed(settings, run, live=run.get("status") == "running" and runner.lock_owner(settings, run_id) is not None),
           "messages": run.get("messages") or [],
           "lessons": _lessons(settings, run),
           "plan_sig": sig(plan_core(run["plan"])) if (run.get("plan") or {}).get("steps") else None,
           "policy": run.get("policy") or {}, "perm_mode": inbox.mode_of(run.get("policy") or {}),
           "plan_approval": run.get("plan_approval", True), "confirm_at": run.get("confirm_at", "start")}
    if st:
        last = st.get("last_check")
        rows = (port._last_check(conn, run["port_id"], "differential:%") or {}).get("detail", {}).get("rows", []) if last else []
        cap = (port._last_check(conn, run["port_id"], "capture") or {}).get("detail", {}).get("expectations") or {}
        order = {k: i for i, k in enumerate(GATE_ORDER)}
        meta = run.get("req_meta") if isinstance(run.get("req_meta"), dict) else {}  # 요구사항의 출처 — 카드에 '더함'·'기존 동작' 표시
        # 끝난 작업은 끝낼 때 확인한 결과를 보여 준다 (지금 다시 계산하면 그 뒤 폴더가 바뀐 것만으로 빈칸이 된다).
        # 지금 기준과 다르면 now_ok 로 함께 알린다.
        fin = port._last_check(conn, run["port_id"], "finish") if run.get("status") == "done" else None
        at_finish = (fin or {}).get("detail", {}).get("gates") or {}
        shown = at_finish or st["gates"]
        accepted = bool(run.get("accepted")) and run.get("status") == "done"
        # 사용자가 수락한 작업은 완료다 — 그때의 결과로 모두 채우고, 그 뒤 폴더가 바뀐 것으로 다시 따지지 않는다.
        # 완료할 때 채워지지 않았던 조건이 있으면 '수락으로 완료'라고만 적는다.
        out.update(gates=[{"name": k, "label": _gate_label(k, mode, bool(at_finish)), "ok": True if accepted else v,
                           **({"by_accept": True} if accepted and not v else {}),
                           **({"now_ok": st["gates"].get(k)} if at_finish and not accepted and st["gates"].get(k) != v else {})}
                          for k, v in sorted(shown.items(), key=lambda kv: order.get(kv[0], 99))], next_step=st["next_step"],
                   gates_at=(fin or {}).get("created_at") if at_finish else None, accepted_done=accepted,
                   license=st.get("license"), license_note=st.get("license_note"),
                   reviews=_reviews(reviews, conn, run["port_id"]) if reviews else None, review_sig=sig(reviews),
                   requirements=[{"id": r["item_id"], "text": r["label"], "status": r["status"], "target": r["target_ref"],
                                  **({"origin": meta[r["item_id"]]} if isinstance(meta.get(r["item_id"]), dict) else {})}
                                 for r in conn.execute("SELECT item_id, label, status, target_ref FROM port_map WHERE port_id = ? "
                                                       "AND kind = 'requirement'", (run["port_id"],)).fetchall()],
                   checks={"passed": last["passed"] if last else None, "cases": last["cases"] if last else None,
                           "rows": [{"name": r["name"], "ok": r["ok"], "diffs": (r.get("diffs") or [])[:2],
                                     "generated": bool(r.get("generated")), "expected": r["expected"] if isinstance(r.get("expected"), dict) else (cap.get(r["name"]) or {}).get("expect"),
                                     "input": (cap.get(r["name"]) or {}).get("input")} for r in rows]})
        # 계획 단계 순서대로 (그 요구사항이 처음 나오는 단계) — 같은 단계 안에서는 번호 순
        first: dict[str, int] = {}
        for i, stp in enumerate((run.get("plan") or {}).get("steps") or []):
            for rid in stp.get("requirements") or []:
                first.setdefault(rid, i)
        out["requirements"].sort(key=lambda r: (first.get(r["id"], 99), int(r["id"][1:]) if r["id"][1:].isdigit() else 0))
        out["spec"] = _spec_view(conn, run["port_id"], rows, last)
        if mode == "reference":
            c = st["counts"]
            out["items"] = {"total": sum(c.values()), "done": sum(c.values()) - c.get("pending", 0)}
    st_ = run.get("streaming")
    if st_ and st_.get("tool"):  # '기획서.md 수정 — 쓰는 중' 처럼 사람 말로 (도구 이름은 그대로 보이지 않게)
        out["run"]["streaming"] = {**st_, "label": _tool_label(st_["tool"], None).replace(" 수정", " 작성")}
    if mode == "plan":
        out["brief"] = brief.check(run["target_root"])
    cps = Checkpoints(inbox.run_dir(settings, run_id), run["target_root"])
    out["checkpoints"] = cps.list()[:30]
    out["folder_missing"] = not Path(run["target_root"]).is_dir()  # 사용자가 작업 폴더를 지웠거나 옮겼다 — 기록은 보이고, 머리줄이 알린다
    return out


def changes(settings, run_id: str) -> dict:
    run = inbox.load_run(settings, run_id)
    return Checkpoints(inbox.run_dir(settings, run_id), run["target_root"]).changes()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _activity(run: dict, asks: list, reviews: int = 0, license_open: bool = False) -> dict | None:
    """지금 하는 일 한 줄 — 사람을 기다리는 동안에는 에이전트가 마지막에 남긴 '…하는 중'이 아니라 기다리는 이유를 보여 준다."""
    s = run.get("status")
    if s == "done":  # 끝난 작업에 '…하는 중'(마지막 활동)을 남겨 두지 않는다
        return None if run.get("accepted") else {"text": "끝났습니다 — 인수인계를 보고 수락하거나, 고칠 점을 보내 주세요"}
    if s == "stopped" and not asks:  # 멈춘 작업에 마지막 '검증하는 중' 같은 한 줄이 남으면 아직 도는 것처럼 보인다
        return {"text": "멈췄습니다 — '이어서'를 누르거나 메시지를 보내면 이어서 진행합니다"}
    if s == "stopped_max_rounds":  # 한도에 닿아 멈춤 — 지난 '…정리했습니다'가 남으면 왜 멈췄는지 모른다
        why = run.get("stop_reason")
        if why == "budget":
            return {"text": "비용 한도에 닿아 멈췄습니다 — '이어서'를 누르면 다시 한도까지 진행합니다 (한도는 라운드 규칙에서)"}
        if why == "hard_rounds":
            return {"text": "안전용 최대 라운드에 닿아 멈췄습니다 — 결과를 보고 '이어서'를 누르거나 고칠 점을 보내 주세요"}
        if run.get("stalled"):  # 횟수 한도가 아니라 진전 없이 같은 라운드를 되풀이해서 멈춤
            return {"text": "몇 라운드째 진전이 없어 멈췄습니다 — 마지막 보고에서 막힌 점(설치·결정 등)을 보고 답하거나 '이어서'를 누르세요"}
        return {"text": "반복 횟수 한도({0}회)에 닿아 멈췄습니다 — 실행 옵션에서 횟수를 늘리거나 끄고 '이어서'를 누르세요".replace(
            "{0}", str(run.get("max_rounds") or run.get("round") or 0))}
    if s == "waiting_approval" and license_open and not reviews:
        return {"text": "원본 라이선스 확인을 기다리는 중 — 아래 카드를 보고 옮겨도 되는지 확인해 주세요"}
    plan_open = bool((run.get("plan") or {}).get("steps")) and not (run.get("plan") or {}).get("approved") \
        and run.get("plan_approval", True) and run.get("confirm_at", "start") != "end"
    if s == "waiting_approval" and not reviews and not license_open and not plan_open and not asks:
        return {"text": "확인할 것을 모두 처리했습니다 — 곧 이어서 진행합니다 (멈춰 있으면 '이어서'를 누르세요)"}
    if s in ("waiting_approval", "waiting_plan"):
        if run.get("confirm_at") == "end":  # 다 만든 뒤 한 번 받는 확인
            return {"text": "마지막 확인을 기다리는 중 — 아래 카드를 보고 승인하거나 고칠 점을 보내 주세요"}
        if (run.get("plan") or {}).get("approved"):  # 시작한 뒤의 확인 (추가 요청으로 바뀐 기준 등) — '시작 전 확인' 카드가 아니다
            return {"text": "확인을 기다리는 중 — 아래 카드를 보고 승인하거나 고칠 점을 보내 주세요"}
        return {"text": "확인을 기다리는 중 — 아래 '시작 전 확인'을 보고 승인하거나 고칠 점을 보내 주세요"}
    if s == "waiting_answer" or asks:
        return {"text": "답을 기다리는 중 — 아래 카드에 답해 주세요"}
    if _holds_for_plan(run, reviews):  # 화면도 이 두 모드에서만 기준을 계획과 함께 띄운다
        return {"text": "기준을 정리했습니다 — 계획까지 세우면 한 번에 확인하도록 보여 드립니다"}
    if s == "running" and reviews and run.get("confirm_at") == "end" and not run.get("activity"):
        return {"text": "기준을 정리했습니다 — 멈추지 않고 만든 뒤 마지막에 한 번에 확인을 요청합니다"}
    return run.get("activity")
