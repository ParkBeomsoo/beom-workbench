"""Which existing shared parts may not be edited right now — one small module so the Claude edit hook can ask the same
question without importing the runner (the hook runs before every edit)."""
from __future__ import annotations


def shared_locked(run: dict) -> dict:
    """지금 고치면 안 되는 공통 부품 {경로: 쓰는 파일 수} — 사용자가 그 파일을 승인했으면(계획 카드·공통 부품 카드) 풀린다."""
    parts = run.get("shared_parts") or {}
    if not parts or (run.get("policy") or {}).get("shared") == "allow" or "shared" in (run.get("always_allow") or []):
        # 사용자가 이 실행에서 공통 부품 수정을 '항상' 허락했다
        return {}
    return {k: v for k, v in parts.items() if k not in shared_approved(run)}


def shared_approved(run: dict) -> set[str]:
    """사용자가 실제로 승인한 공통 부품 — 계획 카드를 승인할 때 그 계획에 신고돼 있던 것, 또는 공통 부품 카드에서 허락한 것.
    신고만으로는 풀지 않는다: 계획 승인을 기다리지 않는 실행(끝에 한 번 확인·계획 승인 없음)에서 신고하자마자 풀려, 사용자가 본
    적 없는 공통 부품이 바뀌었다. 결정 변경으로 승인이 이어진 계획에 새로 더한 신고도 마찬가지."""
    plan = run.get("plan") or {}
    ok = set(run.get("shared_approved") or [])
    if plan.get("approved") and plan.get("approved_via") != "decision-change":
        ok |= {c["file"] for c in plan.get("shared_changes") or []}
    return ok
