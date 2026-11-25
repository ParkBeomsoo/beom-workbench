"""What the user taught the toolkit about a project — carried into the next runs on the same folder.

Two kinds, both from the user's own actions (the toolkit does not guess them):
- rules: something the user said and marked "keep doing this next time" (a follow-up or a correction). Followed in later runs
  unless the new request says otherwise.
- picks: plan decisions where the user chose something other than the recommendation. Not rules — the next plan sees them as
  the user's earlier preference when it recommends.

Stored in the toolkit's home per target folder, never in the user's repository."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_RULES = 30
MAX_PICKS = 20
_LOCK = threading.Lock()


def _key(root: str | Path) -> str:
    p = os.path.normcase(os.path.abspath(str(root))).rstrip("\\/")
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:16]


def _file(settings, root: str | Path) -> Path:
    return Path(settings.home) / "lessons" / f"{_key(root)}.json"


def load(settings, root: str | Path) -> dict:
    try:
        data = json.loads(_file(settings, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"root": str(root), "rules": list(data.get("rules") or []), "picks": list(data.get("picks") or [])}


def _save(settings, root: str | Path, data: dict) -> None:
    f = _file(settings, root)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({**data, "root": str(root)}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_rule(settings, root: str | Path, text: str, run_id: str = "") -> dict:
    text = " ".join(str(text or "").split())[:500]
    if not text:
        raise ValueError("빈 글은 남기지 않는다")
    with _LOCK:
        data = load(settings, root)
        if not any(r.get("text") == text for r in data["rules"]):
            rid = hashlib.sha1((text + _now()).encode("utf-8")).hexdigest()[:10]
            data["rules"].append({"id": rid, "text": text, "run": run_id, "at": _now()})
            data["rules"] = data["rules"][-MAX_RULES:]
            _save(settings, root, data)
        return data


def remove_rule(settings, root: str | Path, rule_id: str) -> dict:
    with _LOCK:
        data = load(settings, root)
        data["rules"] = [r for r in data["rules"] if r.get("id") != rule_id]
        _save(settings, root, data)
        return data


def add_picks(settings, root: str | Path, picks: list[dict], run_id: str = "") -> None:
    """[{topic, choice, recommended}] — the user changed a recommendation on the plan card."""
    picks = [p for p in picks if p.get("topic") and p.get("choice")]
    if not picks:
        return
    with _LOCK:
        data = load(settings, root)
        for p in picks:
            data["picks"] = [x for x in data["picks"] if x.get("topic") != p["topic"]]
            data["picks"].append({"topic": str(p["topic"])[:120], "choice": str(p["choice"])[:200],
                                  "recommended": str(p.get("recommended") or "")[:200], "run": run_id, "at": _now()})
        data["picks"] = data["picks"][-MAX_PICKS:]
        _save(settings, root, data)


def brief_lines(settings, root: str | Path) -> list[str]:
    """What the next run on this folder reads first."""
    data = load(settings, root)
    out = []
    if data["rules"]:
        out.append("[이 프로젝트에서 사용자가 '다음에도 이렇게'라고 남긴 것 — 따른다. 이번 요청이 다르게 말하면 이번 요청이 먼저다]\n"
                   + "\n".join(f"- {r['text']}" for r in data["rules"]))
    if data["picks"]:
        out.append("[이 프로젝트의 앞 작업에서 사용자가 추천 대신 고른 안 — 비슷한 결정을 추천할 때 참고한다 (규칙은 아니다)]\n"
                   + "\n".join(f"- {p['topic']}: '{p['recommended']}' 대신 '{p['choice']}'" if p.get("recommended")
                               else f"- {p['topic']}: '{p['choice']}'" for p in data["picks"][-10:]))
    return out
