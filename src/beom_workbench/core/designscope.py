"""디자인 범위 — 기존 화면이 있는 제품에서 디자인을 어디까지 바꾸는가.

기능을 더하는 요청이면 기존 디자인을 따르고(새 화면은 기존 화면처럼), 디자인을 바꾸라는 요청이면 '느낌만'(배치·부품·문구는 두고
디자인 토큰만)과 '다시 디자인'(기능·문구·데이터는 두고 모습을 새로)이 하는 일이 다르다.

범위는 글을 읽어 짐작하지 않는다 — 에이전트가 계획의 design_scope 칸(keep·restyle·redesign)에 적고, 사용자가 계획 카드에서 고른다.
카드의 선택지는 툴킷이 정한 세 문구(SCOPE_LABELS)뿐이라 고른 결과는 문구를 그대로 견주어 읽는다. 요청의 말(design_intent)은 아무것도
정하거나 막지 않는다 — 계획 전 첫 라운드에 실을 규칙 글을 고르는 데만 쓴다 (칸은 기존 화면을 고치는 계획이면 늘 적고, 카드에 늘 실린다).
"""
from __future__ import annotations

import re

# 바꾸라는 말(동사)이 함께 있어야 한다 — '다크 모드 테마 추가'·'폰트 크기 옵션'·'리뉴얼 알림 기능' 같은 기능 요청을 디자인 변경으로
# 읽지 않게. 추천일 뿐이고, 정하는 것은 계획 카드의 '디자인 범위'다
_CHANGE = r"(?:바꿔|바꾸|바꿀|변경|수정|개선|개편|교체|다듬|손봐|고쳐|새롭게|새로)"
REDESIGN_RX = re.compile(r"리디자인|re-?design|리뉴얼\s*(?:해|하)|renewal|"
                         r"(?:디자인|UI|화면)\s*(?:을|를)?\s*(?:전체|전면|전반)\S*\s*" + _CHANGE + r"|"
                         r"디자인\s*(?:을|를)?\s*(?:새로|새롭게|다시|개편|개선|확\s*바)|(?:UI|화면)\s*(?:을|를)?\s*개편|새\s*디자인으로|"
                         r"(?:예쁘|이쁘|세련되|모던하|트렌디하|고급스럽)게\s*(?:좀\s*)?(?:바꿔|바꾸|만들어|고쳐|개선|다듬|해)", re.I)
RESTYLE_RX = re.compile(r"(?:느낌|분위기|톤\s*앤\s*매너|톤|색감|색깔|컬러|테마|글꼴|폰트|스타일)\s*(?:만|을|를|이|가)?\s*(?:좀\s*)?(?:\S+\s+)?"
                        + _CHANGE + r"|디자인\s*(?:을|를)?\s*(?:좀\s*)?" + _CHANGE + r"|"
                        r"(?:깔끔|따뜻|밝|어둡|심플|부드럽|산뜻|차분|깨끗)\S*\s*(?:하게|게)\s*(?:좀\s*)?(?:바꿔|바꾸|해|만들어|다듬|고쳐)|"
                        r"look\s*(?:and|&)\s*feel|re-?style|change\s+the\s+(?:design|look|style|theme)", re.I)

SCOPES = ("keep", "restyle", "redesign")
SCOPE_TOPIC_LABEL = "디자인 범위"
SCOPE_KEEP, SCOPE_RESTYLE, SCOPE_REDESIGN = "기존 디자인 그대로", "기존 구조에 느낌만 바꿈", "새 방향으로 다시 디자인"
SCOPE_LABELS = {"keep": SCOPE_KEEP, "restyle": SCOPE_RESTYLE, "redesign": SCOPE_REDESIGN}
_BY_LABEL = {v: k for k, v in SCOPE_LABELS.items()}


def design_intent(run: dict) -> str | None:
    """요청이 디자인을 바꾸라고 하나 — 'redesign'·'restyle'·None (추천과 계획 전 규칙 고르기에만 쓴다)."""
    text = str(run.get("request") or "")
    return "redesign" if REDESIGN_RX.search(text) else "restyle" if RESTYLE_RX.search(text) else None


def normalize(value) -> str | None:
    """design_scope 칸의 값 — 'keep'·'restyle'·'redesign' 또는 카드 문구 그대로. 그 밖의 값은 None (짐작하지 않는다)."""
    v = str(value or "").strip()
    if v.lower() in SCOPES:
        return v.lower()
    return _BY_LABEL.get(v)


def scope_of(choice: str) -> str | None:
    """카드에서 고른 선택지 → 범위. 선택지는 툴킷이 정한 세 문구라 그대로 견준다."""
    return _BY_LABEL.get(str(choice or "").strip())


def scope_decision(scope: str, why: str = "") -> dict:
    """계획 카드에 보일 '디자인 범위' 결정 — 선택지는 툴킷이 정한 세 문구 (사용자가 고르면 그 문구가 choice 가 된다)."""
    return {"topic": SCOPE_TOPIC_LABEL, "choice": SCOPE_LABELS[scope], "options": [SCOPE_LABELS[s] for s in SCOPES if s != scope],
            "why": str(why or "").strip()[:300], "kind": "design_scope"}


def design_scope(run: dict) -> str | None:
    """계획에서 정한 디자인 범위 — 'keep'·'restyle'·'redesign' (정하지 않았으면 None). 사용자가 카드에서 바꾼 결정이 먼저다."""
    plan = run.get("plan") or {}
    for d in plan.get("decisions") or []:
        if d.get("kind") == "design_scope" or str(d.get("topic", "")).strip() == SCOPE_TOPIC_LABEL:
            got = scope_of(d.get("choice"))
            if got:
                return got
    return normalize(plan.get("design_scope"))
