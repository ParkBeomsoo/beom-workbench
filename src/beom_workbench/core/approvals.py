"""사람의 결정 — 기준 승인·계획 승인·라이선스 확인·답·수락. 앱(화면)과 CLI(터미널)가 같은 규칙으로 쓴다.

어느 경로든 '보여 준 것'의 서명(sig)이 지금 내용과 같을 때만 승인한다 — 보는 사이 에이전트가 바꾼 것을 승인하지 않게.
채널(channel)은 원장에 남는다: desktop-app · cli-tty. 터미널 승인은 실제 터미널에서만 (에이전트가 파이프로 부르면 거절).
"""
from __future__ import annotations

import getpass
import json
import re
from datetime import datetime, timezone

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings
from beom_workbench.core import designscope, inbox, integrity, port, project
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
        new_look = False
        moved: list[str] = []
        for d in plan.get("decisions") or []:
            pick = changes.get(d["topic"])
            if pick and pick != d["choice"] and pick in (d.get("options") or []) and not d.get("settled"):
                if d.get("kind") == "design_scope" and designscope.scope_of(pick) in ("restyle", "redesign"):
                    new_look = True  # 디자인을 바꾸기로 했다 — 시안을 만들어 사용자가 보고 고른다
                changed.append(f"{d['topic']}: {d['choice']} → {pick}")
                moved.append(d["topic"])
                d["options"] = [d["choice"]] + [o for o in d["options"] if o != pick]
                d["choice"], d["changed_by_user"] = pick, True
        if changed:
            # 그 결정에 딸린 요구사항 — 출처에 적힌 결정 이름으로 찾는다 (무엇을 고칠지 에이전트가 짐작하지 않게)
            meta = r.get("req_meta") if isinstance(r.get("req_meta"), dict) else {}
            squash = lambda t: re.sub(r"[\W_]+", "", str(t or "")).lower()  # noqa: E731
            tied = []
            r["decision_changes"] = {**(r.get("decision_changes") or {}), **{t: _now() for t in moved}}  # 인계 때 다시 저장했는지 본다
            for topic in moved:
                ids = [k for k, m in meta.items() if isinstance(m, dict) and m.get("from") == "decision" and squash(m.get("decision")) == squash(topic)]
                if ids:
                    tied.append(f"- '{topic}' 에 딸린 요구사항 {', '.join(ids)} — 새 안에 맞게 고치거나 빼고, 새 안에 필요한 요구사항을 "
                                f"from=decision·decision='{topic}' 으로 더해 다시 저장한다")
            r.setdefault("messages", []).append({
                "text": "계획을 승인하면서 사전 설계 결정을 바꿨다 — 요구사항·합격 케이스·계획·구현을 이 결정에 맞춘다. "
                        "이번 라운드에 고치는 요구사항·케이스는 사용자 결정의 결과라 툴킷이 자동 승인한다 (다시 확인을 기다리지 않는다):\n"
                        + "\n".join(f"- {c}" for c in changed) + ("\n" + "\n".join(tied) if tied else ""), "at": _now(), "delivered": False,
                "from": "decision"})  # 사용자가 카드에서 고른 결과 — 새 요청이 아니라 승인의 일부 (진행 단계를 처음으로 되돌리지 않는다)
            if new_look:
                # 디자인 범위를 바꾸기로 했다 — 다음 계획에 붙일 시안은 사용자가 보고 골라야 한다 (자동 승인하면 시안을 고를 기회가 없다)
                r["messages"][-1]["text"] += ("\n- 디자인을 바꾸기로 했으니 시안 2개 이상을 만들어 '디자인 방향' 결정의 looks 로 붙인 계획을 다시 "
                                              "낸다 — 사용자가 시안을 보고 고른 뒤 구현한다")
            else:
                r["auto_approve_round"] = int(r.get("round") or 0) + 1
        plan["approved"] = True  # 다시 낸 계획(시안)은 자동 승인되지 않아 사용자가 다시 본다
        # 이 계획 카드에서 본 공통 부품 신고 — 사용자가 승인한 것으로 남긴다 (뒤에 계획이 바뀌어도 이 파일들은 승인된 그대로)
        r["shared_approved"] = sorted(set(r.get("shared_approved") or []) | {c["file"] for c in plan.get("shared_changes") or []})
        plan["approved_by"] = getpass.getuser()
        plan["approved_at"] = _now()
        plan["approved_via"] = channel

    integrity.record(run_id, "plan")
    inbox.update_run(settings, run_id, ok)
    plan = inbox.load_run(settings, run_id).get("plan") or {}
    summary = [f"{d['topic']}: {d['choice']}" for d in plan.get("decisions") or []]
    summary += [f"{i + 1}단계 — {s.get('title')}" for i, s in enumerate(plan.get("steps") or [])]
    inbox.log(settings, run_id, "plan_approved", channel=channel, **({"changed": changed} if changed else {}), summary=summary[:30])
    if changed:  # 추천 대신 고른 안 — 같은 폴더의 다음 계획이 추천할 때 참고한다 (규칙으로 쓰지 않는다)
        try:
            from beom_workbench.core import lessons
            run = inbox.load_run(settings, run_id)
            lessons.add_picks(settings, run.get("target_root") or "", [
                {"topic": d["topic"], "choice": d["choice"], "recommended": (d.get("options") or [""])[0]}
                for d in plan.get("decisions") or [] if d.get("changed_by_user")], run_id)
        except Exception:  # noqa: BLE001 — 기억하지 못해도 승인은 그대로
            pass
    return {"approved": True, "changed": changed}


def _web_read(settings: Settings, run_id: str, request_id: str) -> bool:
    """툴킷이 하는 공개 페이지 읽기(문서 받기·참고 화면 찍기)의 웹 접속 카드인가."""
    try:
        req = json.loads((inbox.run_dir(settings, run_id) / "inbox" / f"{request_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return req.get("permission_kind") == "web" and req.get("tool") in ("mcp:wb_fetch_doc", "mcp:wb_design_refs")


def answer(settings: Settings, run_id: str, request_id: str, data: dict) -> dict:
    if data.get("allow"):
        integrity.record(run_id, "shared")  # 공통 부품 카드의 허락 — 도구 서버가 그 파일을 승인 목록에 넣는다
    if data.get("allow") and _web_read(settings, run_id, request_id):
        # 툴킷의 공개 페이지 읽기는 한 번 허락하면 그 작업 동안 다시 묻지 않는다 — 도구 서버가 그렇게 기록하므로 앱이 자기 결정으로
        # 남긴다 (남기지 않으면 앱이 자기가 하지 않은 권한 변경으로 보고 실행을 멈췄다 — 실험 151)
        integrity.record(run_id, "policy")
    if data.get("always") and data.get("allow"):
        integrity.record(run_id, "policy")  # '항상 허용' — 도구 서버가 규칙을 허용으로 바꾼다
    if data.get("remember") and data.get("allow"):
        # '이 명령은 항상' — 카드에 보여 준 규칙(도구 서버가 명령에서 만든 앞부분)을 앱이 기록한다
        try:
            req = json.loads((inbox.run_dir(settings, run_id) / "inbox" / f"{request_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            req = {}
        rule = req.get("rule")
        if rule:
            integrity.record(run_id, "policy")
            inbox.update_run(settings, run_id, lambda r: r.update(cmd_rules=list(dict.fromkeys((r.get("cmd_rules") or []) + [rule]))))
            inbox.log(settings, run_id, "cmd_rule", rule=rule)
    inbox.answer(settings, run_id, request_id, data)
    return {"answered": True}


def forget_rule(settings: Settings, run_id: str, rule: str) -> dict:
    """기억한 명령 지우기 — 다음부터 그 명령은 다시 묻는다."""
    left: list[str] = []

    def drop(r):
        left[:] = [x for x in (r.get("cmd_rules") or []) if x != rule]
        r["cmd_rules"] = left

    inbox.update_run(settings, run_id, drop)
    inbox.log(settings, run_id, "cmd_rule_forgotten", rule=rule)
    return {"forgotten": rule, "cmd_rules": left}


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
        elif run.get("status") != "running" and run.get("handoff"):
            # 멈춘 실행의 인수인계는 이 메시지를 반영하기 전의 것 — 이어서 진행할 때 그대로 받아들여 끝나지 않게
            run.pop("handoff", None)
            if run.get("accepted"):
                run.setdefault("accepted_before", []).append(run.get("accepted_at") or _now())
                run["accepted"] = False
                run.pop("accepted_at", None)

    inbox.update_run(settings, run_id, add)
    inbox.log(settings, run_id, "user_message", text=text)
    return {"queued": True}


__all__ = ["approve_reviews", "approve_plan", "ack_license", "answer", "accept", "send_message", "json"]
