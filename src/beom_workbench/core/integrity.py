"""사람 결정의 무결성 — 에이전트가 앱을 거치지 않고 승인·규칙을 바꾸면 알아채고 되돌린 뒤 멈춘다.

에이전트는 사용자와 같은 계정으로 돌아서, 테스트·빌드 명령으로 자기가 쓴 코드를 실행하면 툴킷 기록 파일(run.json, 원장)에 직접
쓸 수 있다. 서명·암호로는 막을 수 없다 (같은 계정이면 열쇠도 읽는다). 그래서 앱(실행기와 같은 프로세스)을 유일한 권한자로 둔다:

- 앱이 사람의 결정을 기록할 때(승인·계획 승인·규칙 바꾸기·항상 허용·수락) 이 프로세스의 메모리에 남긴다 (`record`).
- 라운드 앞에 사람 결정 상태를 찍어 두고(`snapshot`), 라운드 뒤에 바뀐 것 중 앱이 하지 않은 것을 찾는다(`changes`).
- 찾으면 계획 승인·규칙·수락은 찍어 둔 값으로 되돌리고, 실행을 멈춘 채 사용자가 확인할 때까지 다시 돌지 않는다.

OS 샌드박스(Codex 의 workspace-write, macOS·Linux 의 Claude Code 샌드박스)가 1차 방어이고, 이것은 샌드박스가 없는 경로
(Windows 의 Claude Code, 툴킷 도구 서버가 대신 돌리는 검증 명령)까지 덮는 2차 방어다. 실행기가 앱 밖(CLI)에서 돌면 앱의 기록을
볼 수 없어 쓰지 않는다.
"""
from __future__ import annotations

import threading
import time

from beom_workbench.core import inbox

IN_APP = {"on": False}            # 앱(브리지)이 켜져 있고 실행기가 같은 프로세스에서 돈다
_ACTIONS: dict[str, list[tuple[str, float]]] = {}
_LOCK = threading.Lock()


def enable() -> None:
    IN_APP["on"] = True


def record(run_id: str, kind: str) -> None:
    """앱이 사람의 결정을 기록했다 — kind: reviews · plan · policy · accept."""
    with _LOCK:
        _ACTIONS.setdefault(run_id, []).append((kind, time.time()))
        _ACTIONS[run_id] = _ACTIONS[run_id][-200:]


def _done_since(run_id: str, kind: str, since: float) -> bool:
    with _LOCK:
        return any(k == kind and t >= since - 1 for k, t in _ACTIONS.get(run_id, []))


def _approvals(settings, run: dict) -> int | None:
    if not (run.get("project_id") and run.get("port_id")):
        return None
    try:
        from beom_workbench.core import project
        conn, _ = project.load(settings, run["project_id"])
        return conn.execute("SELECT COUNT(*) FROM port_log WHERE port_id = ? AND action IN ('approve', 'license_ack')", (run["port_id"],)).fetchone()[0]
    except Exception:  # noqa: BLE001 — 원장이 아직 없으면 비교하지 않는다
        return None


def snapshot(settings, run: dict) -> dict:
    plan = run.get("plan") or {}
    return {"at": time.time(), "plan_approved": bool(plan.get("approved")), "plan_approved_at": plan.get("approved_at"),
            "policy": dict(run.get("policy") or {}), "accepted": bool(run.get("accepted")),
            "port_id": run.get("port_id"), "approvals": _approvals(settings, run)}


def changes(settings, run_id: str, snap: dict) -> list[str]:
    """찍어 둔 뒤 앱을 거치지 않고 바뀐 사람 결정 — 사람이 읽는 문장 목록 (없으면 빈 목록)."""
    run = inbox.load_run(settings, run_id)
    since, out = snap["at"], []
    plan = run.get("plan") or {}
    if plan.get("approved") and not snap["plan_approved"] and not _done_since(run_id, "plan", since):
        out.append("계획이 앱 밖에서 '승인됨'으로 바뀌었습니다")
    strict = inbox.STRICTNESS
    loosened = [k for k, v in (run.get("policy") or {}).items()
                if strict.get(v, 1) < strict.get(snap["policy"].get(k, "ask"), 1)]
    if loosened and not _done_since(run_id, "policy", since):
        out.append(f"권한 규칙이 앱 밖에서 느슨해졌습니다 ({', '.join(inbox.KINDS.get(k, k) for k in loosened)})")
    if run.get("accepted") and not snap["accepted"] and not _done_since(run_id, "accept", since):
        out.append("인수인계가 앱 밖에서 '수락'으로 바뀌었습니다")
    if snap["approvals"] is not None and run.get("port_id") == snap["port_id"]:
        now = _approvals(settings, run)
        if now is not None and now > snap["approvals"] and not _done_since(run_id, "reviews", since):
            out.append(f"사람 확인(기준 승인·라이선스) {now - snap['approvals']}건이 앱 밖에서 기록됐습니다")
    return out


def restore(settings, run_id: str, snap: dict, found: list[str]) -> None:
    """되돌릴 수 있는 것(계획 승인·규칙·수락)은 찍어 둔 값으로, 실행은 멈춤 — 사용자가 확인하기 전에는 다시 돌지 않는다."""
    hit = lambda word: any(f.startswith(word) for f in found)  # noqa: E731 — 잡힌 것만 되돌린다 (같은 라운드에 앱에서 바꾼 것은 그대로)

    def put(r):
        plan = r.get("plan") or {}
        if hit("계획이") and plan and not snap["plan_approved"]:
            plan["approved"] = False
            plan.pop("approved_at", None)
            plan.pop("approved_by", None)
        if hit("권한 규칙"):
            strict = inbox.STRICTNESS
            for k, v in list((r.get("policy") or {}).items()):
                old = snap["policy"].get(k, "ask")
                if strict.get(v, 1) < strict.get(old, 1):  # 느슨해진 것만 — 더 엄격해진 것은 그대로
                    r["policy"][k] = old
        if hit("인수인계") and not snap["accepted"]:
            r["accepted"] = False
            r.pop("accepted_at", None)
        r["tampered"] = {"at": inbox._now(), "what": found}
        r["status"] = "stopped"

    inbox.update_run(settings, run_id, put)
    inbox.log(settings, run_id, "tampered", what=found)
