"""라운드(에이전트를 다시 부르는 반복) 규칙 — 사용자가 정하는 값. 모든 실행에 쓰이고, 도는 중에 바꿔도 다음 판단부터 반영된다.

저장은 툴킷 설정 파일(storage.save_prefs 의 "loop")에 한다. 값이 없거나 범위를 벗어나면 기본값을 쓴다.
실행마다 정하는 '반복 횟수(max_rounds)'·'사람 확인 시점'은 실행 옵션에 따로 있다.
"""
from __future__ import annotations

import os

from beom_workbench.common.response import ToolError

# 이름: (기본값, 허용 값 — (최소, 최대) 또는 고를 수 있는 값들, 설명)
FIELDS: dict[str, tuple] = {
    "stall_rounds": (3, (1, 10), "진전(기록·계획·대상 폴더 변화)이 없는 라운드가 이만큼 이어지면 멈춘다"),
    "hard_rounds": (50, (5, 500), "'끝날 때까지'여도 이 라운드를 넘기지 않는다 (비용 안전장치) — 메시지를 보내면 이어 간다"),
    "round_idle_min": (30, (5, 240), "에이전트가 이 시간(분) 동안 아무 출력이 없으면 그 라운드를 끝낸다"),
    "round_max_min": (120, (10, 600), "라운드 하나가 이 시간(분)을 넘기면 끝내고 다음 라운드로 — 출력은 계속 나오는데 같은 일을 되풀이할 때 "
                                     "(사람 답을 기다리는 시간은 빼고 센다)"),
    "on_limit": ("wait", ("wait", "stop"), "사용 한도에 걸리면 wait: 풀릴 때까지 기다렸다 같은 라운드를 다시 / stop: 멈추고 알림"),
    "limit_wait_max_min": (360, (10, 1440), "한도 대기를 이 시간(분)보다 길게 하지 않는다 — 넘으면 멈추고 알림"),
    "transient_retries": (5, (0, 10), "일시 장애(과부하·5xx·네트워크)를 다시 시도하는 횟수 — 30초부터 두 배씩, 최대 10분 간격"),
    "fail_rounds": (2, (1, 5), "아무것도 못 하고 오류로 끝난 라운드가 이만큼 이어지면 멈춘다"),
    "notify": ("all", ("all", "important", "off"), "창이 뒤에 있을 때의 Windows 알림 — all: 확인 요청·끝남·실패 / important: 끝남·실패만 / off: 안 보냄 "
                                                 "(같은 작업은 사용자가 창을 다시 볼 때까지 한 번만 띄우고, 그 사이에는 작업 표시줄만 깜빡인다)"),
    "cost_budget_usd": (0, (0, 1000), "실행 하나가 이 금액(달러)을 쓰면 멈춘다 — 0 은 제한 없음. 비용을 알려 주는 에이전트(Claude Code)만, "
                                     "구독 요금제는 실제 청구가 아니라 같은 양을 API 로 썼을 때의 금액"),
}


def defaults() -> dict:
    out = {k: v[0] for k, v in FIELDS.items()}
    env = os.environ.get("BEOM_WORKBENCH_ROUND_IDLE")  # 예전 환경 변수(초)도 그대로 받는다
    if env and env.isdigit():
        out["round_idle_min"] = max(1, int(env) // 60)
    return out


def _ok(name: str, value) -> bool:
    allowed = FIELDS[name][1]
    if isinstance(allowed[0], str):
        return value in allowed
    return isinstance(value, int) and not isinstance(value, bool) and allowed[0] <= value <= allowed[1]


def validate(patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ToolError("invalid_request", "loop 는 {이름: 값} 이어야 함")
    out = {}
    for k, v in patch.items():
        if k not in FIELDS:
            raise ToolError("invalid_request", f"모르는 라운드 규칙: {k} (가능: {', '.join(FIELDS)})")
        if v is None:  # 기본값으로
            continue
        if isinstance(v, str) and v.isdigit():
            v = int(v)
        if not _ok(k, v):
            allowed = FIELDS[k][1]
            rng = " · ".join(allowed) if isinstance(allowed[0], str) else f"{allowed[0]}~{allowed[1]}"
            raise ToolError("invalid_request", f"{k} 는 {rng}")
        out[k] = v
    return out


def get(settings) -> dict:
    """지금 쓸 규칙 — 기본값 위에 사용자가 정한 값. 파일이 깨졌거나 값이 이상하면 그 값만 기본값."""
    from beom_workbench.core import storage

    out = defaults()
    saved = storage.load_prefs(settings).get("loop") or {}
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in FIELDS and _ok(k, v):
                out[k] = v
    return out


def describe(settings) -> dict:
    cur = get(settings)
    return {"values": cur, "defaults": defaults(),
            "fields": {k: {"default": v[0], "allowed": list(v[1]), "help": v[2]} for k, v in FIELDS.items()}}


def transient_backoff(n: int) -> int:
    """n 번째 재시도 전 기다릴 초 — 30, 60, 120, 240, 480, 600, 600…"""
    return min(600, 30 * 2 ** max(0, n - 1))


def limit_backoff(n: int) -> int:
    """한도 해제 시각을 모를 때 n 번째로 기다릴 초 — 5, 10, 20, 30, 60분, 이후 60분."""
    table = (300, 600, 1200, 1800, 3600)
    return table[min(n, len(table)) - 1]
