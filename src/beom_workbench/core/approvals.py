"""사람의 결정 — 기준 승인·계획 승인·라이선스 확인·답·수락. 앱(화면)과 CLI(터미널)가 같은 규칙으로 쓴다.

어느 경로든 '보여 준 것'의 서명(sig)이 지금 내용과 같을 때만 승인한다 — 보는 사이 에이전트가 바꾼 것을 승인하지 않게.
채널(channel)은 원장에 남는다: desktop-app · cli-tty. 터미널 승인은 실제 터미널에서만 (에이전트가 파이프로 부르면 거절).
"""
from __future__ import annotations

import getpass
import json
from datetime import datetime, timezone

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings
from beom_workbench.core import inbox, integrity, port, project
from beom_workbench.core import port_guard as guard


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sig(value) -> str:
    from beom_workbench.app import view
    return view.sig(value)


def approve_reviews(settings: Settings, run_id: str, shown_sig: str, channel: str = "desktop-app") -> dict:
    run = inbox.load_run(settings, run_id)
    conn, _ = project.load(settings, run["project_id"])
    reviews = port.pending_reviews(conn, run["port_id"])
    if _sig(reviews) != shown_sig:  # 화면을 본 뒤 에이전트가 내용을 바꿨다 — 본 것만 승인한다
        raise ToolError("conflict", "확인하는 사이에 내용이 바뀌었습니다. 새 내용을 보고 다시 승인해 주세요")
    integrity.record(run_id, "reviews")
    guard.approve(conn, run["port_id"], reviews, channel=channel, approved_by=getpass.getuser())
    inbox.log(settings, run_id, "approved", items=[r["item_id"] for r in reviews], channel=channel)
    return {"approved": len(reviews)}


def ack_license(settings: Settings, run_id: str, shown_warning: str, channel: str = "desktop-app") -> dict:
    run = inbox.load_run(settings, run_id)
    conn, _ = project.load(settings, run["project_id"])
    st = port.status_(conn, run["port_id"], brief=True)
    if ((st.get("license") or {}).get("warning") or "") != shown_warning:
        raise ToolError("conflict", "확인하는 사이에 라이선스 경고가 바뀌었습니다. 새 내용을 보고 다시 확인해 주세요")
    integrity.record(run_id, "reviews")
    out = port.ack_license(conn, run["port_id"], channel=channel, approved_by=getpass.getuser())
    inbox.log(settings, run_id, "license_ack", warning=shown_warning[:500], channel=channel)
    return out


def approve_plan(settings: Settings, run_id: str, shown_sig: str, changes: dict | None = None, channel: str = "desktop-app") -> dict:
    """계획 승인 — changes 로 사전 설계 결정 몇 개를 대안으로 바꿀 수 있다 ({주제: 고른 대안})."""
    from beom_workbench.app import view
    changes = changes or {}
    changed: list[str] = []

    def ok(r):
        plan = r.get("plan") or {}
        if not plan.get("steps"):
            raise ToolError("invalid_request", "승인할 계획이 없습니다")
        if view.sig(view.plan_core(plan)) != shown_sig:
            raise ToolError("conflict", "확인하는 사이에 계획이 바뀌었습니다. 새 계획을 보고 다시 승인해 주세요")
        for d in plan.get("decisions") or []:
            pick = changes.get(d["topic"])
            if pick and pick != d["choice"] and pick in (d.get("options") or []):
                changed.append(f"{d['topic']}: {d['choice']} → {pick}")
                d["options"] = [d["choice"]] + [o for o in d["options"] if o != pick]
                d["choice"], d["changed_by_user"] = pick, True
        if changed:
            r.setdefault("messages", []).append({
                "text": "계획을 승인하면서 사전 설계 결정을 바꿨다 — 요구사항·합격 케이스·계획·구현을 이 결정에 맞춘다. "
                        "이번 라운드에 고치는 요구사항·케이스는 사용자 결정의 결과라 툴킷이 자동 승인한다 (다시 확인을 기다리지 않는다):\n"
                        + "\n".join(f"- {c}" for c in changed), "at": _now(), "delivered": False})
            r["auto_approve_round"] = int(r.get("round") or 0) + 1
        plan["approved"] = True
        plan["approved_by"] = getpass.getuser()
        plan["approved_at"] = _now()
        plan["approved_via"] = channel

    integrity.record(run_id, "plan")
    inbox.update_run(settings, run_id, ok)
    inbox.log(settings, run_id, "plan_approved", channel=channel, **({"changed": changed} if changed else {}))
    return {"approved": True, "changed": changed}


def answer(settings: Settings, run_id: str, request_id: str, data: dict) -> dict:
    if data.get("always") and data.get("allow"):
        integrity.record(run_id, "policy")  # '항상 허용' — 도구 서버가 규칙을 허용으로 바꾼다
    inbox.answer(settings, run_id, request_id, data)
    return {"answered": True}


def accept(settings: Settings, run_id: str) -> dict:
    integrity.record(run_id, "accept")
    inbox.update_run(settings, run_id, lambda r: r.update(accepted=True, accepted_at=_now()))
    inbox.log(settings, run_id, "accepted")
    return {"accepted": True}


def send_message(settings: Settings, run_id: str, text: str) -> dict:
    """고칠 점 — 진행 중이면 다음 라운드에, 멈춰 있으면 이어서 진행할 때 전한다 (끝난 작업이면 다시 수락받는다)."""
    text = (text or "").strip()[:4000]
    if not text:
        raise ToolError("invalid_request", "내용이 비어 있습니다")

    def add(run):
        run.setdefault("messages", []).append({"text": text, "at": _now(), "delivered": False})
        if run.get("status") == "done":
            run["status"] = "stopped"
            run.pop("handoff", None)
            run["revision"] = True
            if run.get("accepted"):
                run.setdefault("accepted_before", []).append(run.get("accepted_at") or _now())
                run["accepted"] = False
                run.pop("accepted_at", None)

    inbox.update_run(settings, run_id, add)
    inbox.log(settings, run_id, "user_message", text=text)
    return {"queued": True}


__all__ = ["approve_reviews", "approve_plan", "ack_license", "answer", "accept", "send_message", "json"]
