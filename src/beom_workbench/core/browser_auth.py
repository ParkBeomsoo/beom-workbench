"""앱 브라우저에서 로그인한 사이트 — 사용자가 '에이전트가 화면을 찍어도 됨'을 허락한 곳만, 그 로그인 상태(쿠키)를 툴킷 홈에 둔다.

에이전트는 비밀번호도 쿠키도 받지 않는다 — 툴킷의 화면 엔진이 찍을 때만 그 쿠키로 연다 (wb_browser_capture). 툴킷 홈은 에이전트가
읽을 수 없는 곳이다. 허락은 사이트(호스트)마다, 12시간 뒤에는 다시 로그인·허락해야 한다 (세션이 오래 남지 않게).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

TTL = 12 * 3600


def _dir(settings) -> Path:
    return Path(settings.home) / "browser" / "allowed"


def _host(url_or_host: str) -> str:
    s = str(url_or_host or "").strip().lower()
    host = urlsplit(s).hostname if "://" in s else s.split("/")[0].split(":")[0]
    return host or ""


def _file(settings, host: str) -> Path:
    return _dir(settings) / (re.sub(r"[^a-z0-9.-]+", "_", host) + ".json")


def allow(settings, host: str, cookies: list[dict]) -> dict:
    """허락 — 그 사이트의 쿠키를 저장한다 (허락한 순간의 로그인 상태)."""
    host = _host(host)
    if not host:
        raise ValueError("호스트가 없음")
    _dir(settings).mkdir(parents=True, exist_ok=True)
    keep = [c for c in cookies if _matches(host, str(c.get("domain") or host))]
    row = {"host": host, "at": time.time(), "cookies": keep}
    _file(settings, host).write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    return {"host": host, "cookies": len(keep)}


def forget(settings, host: str) -> bool:
    f = _file(settings, _host(host))
    if f.is_file():
        f.unlink()
        return True
    return False


def hosts(settings) -> list[dict]:
    """허락된 사이트 — 기한이 지난 것은 지운다."""
    out = []
    d = _dir(settings)
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if time.time() - float(row.get("at") or 0) > TTL:
            f.unlink(missing_ok=True)
            continue
        out.append({"host": row["host"], "at": row["at"], "cookies": len(row.get("cookies") or [])})
    return out


def _matches(host: str, domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return host == d or host.endswith("." + d) or d.endswith("." + host) or d == host


def cookies_for(settings, url: str) -> list[dict] | None:
    """이 주소를 열 때 쓸 쿠키 — 허락된 사이트가 아니면 None (그 주소는 찍지 않는다)."""
    host = _host(url)
    for row in hosts(settings):
        if host == row["host"] or host.endswith("." + row["host"]):
            data = json.loads(_file(settings, row["host"]).read_text(encoding="utf-8"))
            return [c for c in data.get("cookies") or [] if _matches(host, str(c.get("domain") or host))]
    return None
