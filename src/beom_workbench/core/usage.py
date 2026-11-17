"""작업 전체의 사용량 — 실행마다 남긴 비용(Claude Code)·토큰(Codex)을 모아 본다.

비용은 에이전트가 보고한 값이다. 구독 로그인이면 실제 청구액이 아니라 같은 사용량을 API 가격으로 셈한 값이다.
기간은 작업을 만든 날 기준 (이어서 한 라운드도 그 작업에 들어간다).
"""
from __future__ import annotations

from datetime import datetime

from beom_workbench.config import Settings
from beom_workbench.core import inbox


def _month(at: str | None) -> str:
    return (at or "")[:7]


def summary(settings: Settings, now: datetime | None = None) -> dict:
    month = (now or datetime.now()).strftime("%Y-%m")
    total = {"cost_usd": 0.0, "tokens": 0, "runs": 0, "rounds": 0}
    this_month = {"cost_usd": 0.0, "tokens": 0, "runs": 0, "rounds": 0}
    agents: dict[str, dict] = {}
    rows = []
    for r in inbox.list_runs(settings):
        cost, tokens, rounds = float(r.get("cost_usd") or 0), int(r.get("tokens") or 0), int(r.get("round") or 0)
        if not (cost or tokens or rounds):
            continue
        for bucket in (total, this_month) if _month(r.get("created_at")) == month else (total,):
            bucket["cost_usd"] += cost
            bucket["tokens"] += tokens
            bucket["runs"] += 1
            bucket["rounds"] += rounds
        a = agents.setdefault(r.get("agent") or "claude", {"cost_usd": 0.0, "tokens": 0, "runs": 0, "subscription": 0})
        a["cost_usd"] += cost
        a["tokens"] += tokens
        a["runs"] += 1
        a["subscription"] += 1 if (r.get("auth") or "subscription") == "subscription" else 0
        rows.append({"id": r["id"], "name": r.get("name") or "", "agent": r.get("agent") or "claude", "cost_usd": cost,
                     "tokens": tokens, "rounds": rounds, "created_at": r.get("created_at"), "archived": bool(r.get("archived"))})
    rows.sort(key=lambda x: (x["cost_usd"], x["tokens"]), reverse=True)
    for b in (total, this_month):
        b["cost_usd"] = round(b["cost_usd"], 4)
    for a in agents.values():
        a["cost_usd"] = round(a["cost_usd"], 4)
    return {"month": month, "total": total, "this_month": this_month, "agents": agents, "top": rows[:5]}
