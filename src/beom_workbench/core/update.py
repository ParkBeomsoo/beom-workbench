"""새 버전 알림 — 공개 릴리스 목록에서 최신 버전 하나만 읽는다 (사용자 정보는 보내지 않는다).

BEOM_WORKBENCH_NO_UPDATE_CHECK=1 이면 확인하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

RELEASES_API = "https://api.github.com/repos/ParkBeomsoo/beom-workbench/releases/latest"


def _parts(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def newer(latest: str, current: str) -> bool:
    """latest 가 current 보다 새 버전인가 (숫자 세 자리 비교 — 읽을 수 없으면 아니라고 본다)."""
    a, b = _parts(latest), _parts(current)
    return bool(a) and bool(b) and a > b


def enabled() -> bool:
    return not os.environ.get("BEOM_WORKBENCH_NO_UPDATE_CHECK")


def latest(current: str, timeout: float = 6) -> dict | None:
    """current 보다 새 릴리스가 있으면 {version, url}, 없거나 확인하지 못하면 None."""
    if not enabled():
        return None
    req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                        "User-Agent": f"beom-workbench/{current}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — 고정된 https 주소
            data = json.loads(r.read(200_000).decode("utf-8"))
    except Exception:  # noqa: BLE001 — 오프라인·제한·형식 오류는 조용히 넘긴다 (알림일 뿐)
        return None
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or not newer(tag, current):
        return None
    url = str(data.get("html_url") or "")
    return {"version": tag.lstrip("vV"), "url": url if url.startswith("https://github.com/") else ""}
