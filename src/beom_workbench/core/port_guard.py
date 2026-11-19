"""포팅 지킴이 — AI 가 혼자 정하면 안 되는 것은 사람 확인을 받게 하고, 검증 뒤에 바뀐 것은 게이트에서 잡는다.

1. 사람 확인 (approval): 차이 허용(port-accept)·생략(omitted)·케이스 약화(capture 사이에 케이스가 빠지거나 비교 규칙이 바뀜)는
   AI 가 기록할 수는 있지만 '제안'일 뿐이다. 사용자가 MCP 확인 창(elicitation) 또는 터미널 직접 입력(port-approve)으로
   승인해야 게이트가 통과한다. 승인은 그때 보여 준 내용의 지문에 묶인다 — 차이 내용·사유·케이스가 바뀌면 다시 승인.
2. 변조 탐지 (port_log): 원장 쓰기마다 해시 사슬에 기록 — 매핑 상태 지문, 검사 기록 지문, 승인.
   툴킷을 거치지 않고 원장(SQLite)이나 골든 파일을 고치면 지문이 어긋나 게이트가 실패한다.
3. 낡은 검증 (target fingerprint): 검증 때 대상 저장소 지문을 남기고, 그 뒤 코드가 바뀌면 검증을 다시 요구한다.

한계: 툴킷은 사용자 권한으로 도는 로컬 프로그램이다. 같은 권한을 가진 쪽이 작정하고 사슬까지 다시 계산하면 막을 수 없다.
목표는 '모르게 지나가는 것'을 없애는 것 — 지름길은 실패로 드러나고, 최종 판정은 CI 같은 깨끗한 환경에서 다시 돌릴 수 있다.
"""
from __future__ import annotations

import fnmatch
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from beom_workbench.core import ledger

SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", ".venv", "venv", "dist", "build",
             ".pytest_cache", ".mypy_cache", ".next", ".gradle"}
MAX_FILES = 20000
MAX_FILE_BYTES = 4 * 1024 * 1024
HUMAN_CHANNELS = {"mcp-elicitation", "cli-tty", "desktop-app"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(obj) -> str:
    data = obj if isinstance(obj, (bytes, str)) else json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


# ---------------------------------------------------------------- 대상 저장소 지문

def _git_files(root: Path) -> list[str] | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "-t", "--exclude-standard", "-z"],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    # -C 로 들어간 폴더 기준 상대 경로로 나온다 (하위 폴더가 대상이면 그 아래만). -t 로 추적('H' 등)·미추적('?')을 가른다
    files = []
    for entry in out.stdout.decode("utf-8", "replace").split("\0"):
        tag, _, rel = entry.partition(" ")
        if not rel or _toolkit_path(rel) or (tag == "?" and _generated(rel)):
            continue
        files.append(rel)
    return sorted(files)


def _generated(rel: str) -> bool:
    """추적하지 않는 빌드·캐시 산출물 — .gitignore 가 없어도 지문에 넣지 않는다. 검증·테스트가 돌 때마다 생기는
    __pycache__ 같은 것이 지문을 바꾸면 '검증 뒤 대상 코드가 바뀜' 으로 방금 한 검증이 무효가 된다."""
    parts = rel.split("/")
    return rel.endswith((".pyc", ".pyo")) or any(p in SKIP_DIRS for p in parts[:-1])


def _toolkit_path(rel: str) -> bool:
    return rel.startswith(".beom/") or "/.beom/" in rel


def _walk_files(root: Path) -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d != ".beom")
        for f in sorted(filenames):
            files.append(Path(dirpath, f).relative_to(root).as_posix())
            if len(files) >= MAX_FILES:
                return files
    return files


# 파일 내용 지문 캐시 — (크기, 수정 시각)이 그대로면 다시 읽지 않는다 (git 의 인덱스와 같은 판단). 앱이 1.5초마다
# 상태를 물을 때 큰 저장소 전체를 매번 읽어 창이 멈추던 것을 막는다.
_DIGEST: dict[str, tuple[int, int, bytes]] = {}


def _file_digest(p: Path, st) -> bytes:
    key = str(p)
    hit = _DIGEST.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    d = hashlib.sha256(p.read_bytes()).digest()
    if len(_DIGEST) > 200_000:
        _DIGEST.clear()
    _DIGEST[key] = (st.st_size, st.st_mtime_ns, d)
    return d


_FP: dict[str, tuple[float, str]] = {}


def target_fingerprint(root: str | Path, fresh: bool = False, max_age: float = 0) -> str:
    """대상 저장소의 파일 목록·내용 지문 (git 이면 추적+미추적(무시 제외) 파일, 아니면 흔한 빌드 폴더 제외).
    max_age: 화면 표시용 — 그 초 안에 잰 값이 있으면 다시 걷지 않는다 (검증·완료 판정은 언제나 새로)."""
    # 파일 목록은 매번 새로 (다른 프로세스 — 에이전트의 MCP 서버 — 가 남긴 지문과 맞아야 한다). 내용은 _file_digest 캐시로.
    import time
    key = str(Path(root).resolve())
    hit = _FP.get(key)
    if max_age and not fresh and hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    value = _fingerprint(Path(root))
    _FP[key] = (time.monotonic(), value)
    return value


def _fingerprint(root: Path) -> str:
    files = _git_files(root)
    if files is None:
        files = _walk_files(root)
    h = hashlib.sha256()
    for rel in files[:MAX_FILES]:
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(rel.encode() + b"\0")
        if st.st_size > MAX_FILE_BYTES:
            h.update(f"size:{st.st_size}:{st.st_mtime_ns}".encode())
        else:
            try:
                h.update(_file_digest(p, st))
            except OSError:
                h.update(b"unreadable")
    return h.hexdigest()


# ---------------------------------------------------------------- 케이스 규칙

def case_rules(cases: list[dict]) -> dict[str, str]:
    """케이스 이름 → 내용(입력·비교 규칙) 지문. 이름이 같은데 지문이 다르면 케이스가 바뀐 것."""
    return {c.get("name") or f"#{i}": _sha({k: v for k, v in c.items() if k != "name"}) for i, c in enumerate(cases)}


def case_change(baseline: dict[str, str], rules: dict[str, str]) -> dict | None:
    removed = sorted(set(baseline) - set(rules))
    changed = sorted(n for n in rules if n in baseline and baseline[n] != rules[n])
    return {"removed": removed, "changed": changed} if removed or changed else None


# ---------------------------------------------------------------- 해시 사슬 기록

def _check_digest(row) -> str:
    r = dict(row)
    return _sha({k: r.get(k) for k in ("id", "port_id", "kind", "cases", "passed", "failed", "detail", "evidence_id", "created_at")})


def map_digest(conn, port_id: str) -> str:
    rows = conn.execute("SELECT item_id, kind, label, status, target_ref, target_text, note FROM port_map "
                        "WHERE port_id = ? ORDER BY item_id", (port_id,)).fetchall()
    return _sha([list(r) for r in rows])


def _head(conn, port_id: str) -> str | None:
    row = conn.execute("SELECT log_head FROM port WHERE id = ?", (port_id,)).fetchone()
    return row["log_head"] if row else None


def record(conn, port_id: str, action: str, payload: dict | None = None, checks: list[str] = ()) -> str:
    """원장 쓰기 뒤에 호출 — 매핑 상태 지문과 이번에 만든 검사 기록 지문을 사슬에 잇는다."""
    with ledger.tx(conn):
        last = conn.execute("SELECT hash FROM port_log WHERE port_id = ? ORDER BY seq DESC LIMIT 1", (port_id,)).fetchone()
        body = dict(payload or {})
        if last is None and action != "create":
            # v9 이전에 만든 포팅: 지금 상태를 출발점으로 삼는다 (이전 기록은 검사 밖)
            body["adopted_checks"] = {r["id"]: _check_digest(r) for r in conn.execute(
                "SELECT * FROM port_check WHERE port_id = ?", (port_id,)).fetchall()}
        body["map_digest"] = map_digest(conn, port_id)
        body["checks"] = {cid: _check_digest(conn.execute("SELECT * FROM port_check WHERE id = ?", (cid,)).fetchone())
                          for cid in checks if cid}
        prev = last["hash"] if last else f"genesis:{port_id}"
        at = _now()
        text = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
        h = _sha(prev + "|" + action + "|" + text + "|" + at)
        conn.execute("INSERT INTO port_log (port_id, action, payload, at, prev, hash) VALUES (?, ?, ?, ?, ?, ?)",
                     (port_id, action, text, at, prev, h))
        conn.execute("UPDATE port SET log_head = ? WHERE id = ?", (h, port_id))
    return h


def _logs(conn, port_id: str) -> list[dict]:
    return [{**dict(r), "payload": json.loads(r["payload"])}
            for r in conn.execute("SELECT * FROM port_log WHERE port_id = ? ORDER BY seq", (port_id,)).fetchall()]


def integrity(conn, port_id: str) -> dict:
    """사슬이 이어지는지, 지금 매핑 상태·검사 기록이 마지막으로 기록된 것과 같은지."""
    logs = _logs(conn, port_id)
    head = _head(conn, port_id)
    problems: list[str] = []
    if not logs:
        if head:
            problems.append("기록 사슬이 지워짐 (원장의 마지막 기록 표시는 남아 있음)")
        return {"ok": not problems, "legacy": not head, "problems": problems}
    prev = f"genesis:{port_id}"
    known: dict[str, str] = {}
    for lg in logs:
        text = json.dumps(lg["payload"], ensure_ascii=False, sort_keys=True, default=str)
        if lg["prev"] != prev or _sha(prev + "|" + lg["action"] + "|" + text + "|" + lg["at"]) != lg["hash"]:
            problems.append(f"기록 사슬 끊김 (seq {lg['seq']}, {lg['action']})")
            break
        prev = lg["hash"]
        known.update(lg["payload"].get("adopted_checks") or {})
        known.update(lg["payload"].get("checks") or {})
    if head != prev:
        problems.append("원장의 마지막 기록 표시가 사슬 끝과 다름")
    if logs[-1]["payload"].get("map_digest") != map_digest(conn, port_id):
        problems.append("매핑 상태가 툴킷 밖에서 바뀜 (마지막 기록과 다름)")
    for r in conn.execute("SELECT * FROM port_check WHERE port_id = ?", (port_id,)).fetchall():
        if r["id"] not in known:
            problems.append(f"기록에 없는 검사 결과 {r['id']} ({r['kind']})")
        elif known[r["id"]] != _check_digest(r):
            problems.append(f"검사 결과 {r['id']} ({r['kind']}) 가 기록 뒤에 바뀜")
    return {"ok": not problems, "legacy": False, "problems": problems[:20], "entries": len(logs)}


# ---------------------------------------------------------------- 사람 확인

def divergence_cases(rows: list[dict], pattern: str) -> list[dict]:
    return [{"name": r["name"], "diffs": r.get("accepted_diffs") or []}
            for r in rows if r.get("accepted") is not None and fnmatch.fnmatchcase(r["name"], pattern)]


def divergence_fp(pattern: str, reason: str, cases: list[dict]) -> str:
    return _sha({"pattern": pattern, "reason": reason, "cases": sorted((c["name"], c["diffs"]) for c in cases)})


def fingerprint(obj) -> str:
    return _sha(obj)


def _approved(logs: list[dict]) -> set[tuple[str, str]]:
    return {(lg["payload"]["item_id"], lg["payload"]["fingerprint"]) for lg in logs
            if lg["action"] == "approve" and lg["payload"].get("channel") in HUMAN_CHANNELS}


def is_approved(conn, port_id: str, item_id: str, fp: str) -> bool:
    return (item_id, fp) in _approved(_logs(conn, port_id))


TS_EXTS = (".ts", ".mts", ".cts", ".tsx")


def local_bin(root: Path, name: str) -> bool:
    b = Path(root) / "node_modules" / ".bin"
    return any((b / (name + ext)).is_file() for ext in ("", ".cmd", ".ps1"))


def measurable(root: Path, rel: str) -> bool:
    """툴킷이 이 파일의 실행 범위를 스스로 잴 수 있나 (port.auto_coverage 와 같은 기준)."""
    if rel.endswith((".py", ".js", ".mjs", ".cjs")):
        return True
    if rel.endswith(TS_EXTS):  # vitest 프로젝트에 v8 커버리지 패키지가 깔려 있을 때
        return (Path(root) / "node_modules" / "@vitest" / "coverage-v8").is_dir() and local_bin(root, "vitest")
    if rel.endswith(".rs"):
        return shutil.which("cargo-llvm-cov") is not None
    return False


def coverage_gap(conn, port_id: str) -> dict | None:
    """구현 파일이 모두 툴킷이 잴 수 없는 언어이고 측정 기록도 없으면 그 파일들 — 사람 확인으로 넘길 것.
    없으면 에이전트가 커버리지 도구를 찾고 설치하려 하거나, 없는 '생략 승인' 을 사용자에게 요청하며 같은 자리를 돈다."""
    last = conn.execute("SELECT detail FROM port_check WHERE port_id = ? AND kind = 'adequacy:target' ORDER BY rowid DESC LIMIT 1",
                        (port_id,)).fetchone()
    if last:
        # 측정은 됐지만 도구가 읽지 못한 화면 코드(JSX 등)가 있으면 그것만 사람 확인으로
        rows = (json.loads(last["detail"]) if isinstance(last["detail"], str) else last["detail"] or {}).get("rows") or []
        files = sorted({r["target"].rsplit(":", 1)[0] for r in rows if r.get("unmeasurable")})
        return {"files": files, "exts": sorted({PurePosixPath(f).suffix for f in files})} if files else None
    if conn.execute("SELECT 1 FROM port_check WHERE port_id = ? AND kind LIKE 'adequacy%' LIMIT 1", (port_id,)).fetchone():
        return None
    row = conn.execute("SELECT target_root FROM port WHERE id = ?", (port_id,)).fetchone()
    if not row:
        return None
    root = Path(row["target_root"])
    files = sorted({m.group(1).replace("\\", "/") for r in conn.execute(
        "SELECT target_ref FROM port_map WHERE port_id = ? AND status = 'ported'", (port_id,)).fetchall()
        if (m := re.fullmatch(r"(.+?):\d+", r["target_ref"] or ""))})
    files = [f for f in files if not f.startswith(".beom/")]
    if not files or any(measurable(root, f) for f in files):
        return None
    return {"files": files, "exts": sorted({PurePosixPath(f).suffix or PurePosixPath(f).name for f in files})}


def reviews(conn, port_id: str, last_diff: dict | None, last_capture: dict | None) -> list[dict]:
    """사람 확인이 필요한 항목과 승인 여부. 지문은 '지금 보여 줄 내용' 기준 — 내용이 바뀌면 예전 승인은 무효."""
    approved = _approved(_logs(conn, port_id))
    rows = (last_diff or {}).get("detail", {}).get("rows", []) if last_diff else []
    items = []
    reqs = [dict(r) for r in conn.execute("SELECT item_id, label FROM port_map WHERE port_id = ? AND kind = 'requirement'",
                                          (port_id,)).fetchall()]
    if reqs:
        reqs.sort(key=lambda r: int(r["item_id"][1:]))
        listing = [(r["item_id"], r["label"]) for r in reqs]
        items.append({"item_id": "requirements", "kind": "requirements", "fingerprint": _sha(listing),
                      "summary": f"요구사항 {len(reqs)}개 — 이 목록대로 만든다",
                      "detail": [{"case": rid, "diff": text[:240]} for rid, text in listing]})
    for r in conn.execute("SELECT * FROM port_map WHERE port_id = ? AND kind = 'divergence' ORDER BY label",
                          (port_id,)).fetchall():
        cases = divergence_cases(rows, r["label"])
        if not cases:
            continue  # 마지막 검증에서 이 허용으로 넘어간 케이스가 없음 — 확인할 것도 없다
        fp = divergence_fp(r["label"], r["note"], cases)
        items.append({"item_id": r["item_id"], "kind": "divergence", "fingerprint": fp,
                      "summary": f"차이 허용 '{r['label']}' — 사유: {r['note']}",
                      "detail": [{"case": c["name"], "diff": (c["diffs"] or ["?"])[0][:240]} for c in cases[:10]],
                      "proposed_by": r["actor"]})
    for r in conn.execute("SELECT * FROM port_map WHERE port_id = ? AND status = 'omitted' AND kind != 'divergence' "
                          "ORDER BY item_id", (port_id,)).fetchall():
        fp = _sha({"item": r["item_id"], "label": r["label"], "note": r["note"]})
        items.append({"item_id": r["item_id"], "kind": "omitted", "fingerprint": fp,
                      "summary": f"생략: {r['label']} — 사유: {r['note']}", "proposed_by": r["actor"]})
    for name, ex in sorted(((last_capture or {}).get("detail", {}).get("expectations") or {}).items()):
        fp = _sha({"name": name, **ex})
        src = ex["source"]
        was = "원본에 없음(새 동작)" if ex["new"] else f"원본 status {src['status']} {src['body']}"
        items.append({"item_id": f"expect:{name}", "kind": "expectation", "fingerprint": fp,
                      "summary": f"기대값 '{name}' — {'원본에 없는 새 동작' if ex['new'] else '원본과 다르게'}",
                      "detail": [{"case": name, "diff": f"{was[:200]} → 기대 {json.dumps(ex['expect'], ensure_ascii=False)[:200]}"}]})
    change = (last_capture or {}).get("detail", {}).get("change") if last_capture else None
    if change:
        fp = _sha(change)
        items.append({"item_id": f"cases:{last_capture['id']}", "kind": "case_change", "fingerprint": fp,
                      "summary": f"케이스 변경 — 빠짐 {len(change['removed'])}개, 바뀜 {len(change['changed'])}개 (이전 capture 대비)",
                      "detail": change})
    gap = coverage_gap(conn, port_id)
    if gap:
        items.append({"item_id": "coverage", "kind": "coverage_unmeasured", "fingerprint": _sha(gap),
                      "summary": f"실행 범위 측정 불가 ({', '.join(gap['exts'])}) — 툴킷이 이 파일들의 실행 범위를 재지 못해 케이스·테스트가 "
                                 "구현 코드를 모두 거쳤는지 확인하지 못했습니다. 합격 케이스·화면 확인·테스트 결과로 충분하면 승인하세요",
                      "detail": {"files": gap["files"]}})
    for it in items:
        it["approved"] = (it["item_id"], it["fingerprint"]) in approved
    return items


def approve(conn, port_id: str, items: list[dict], *, channel: str, approved_by: str | None = None) -> list[str]:
    """사람이 확인한 항목을 승인으로 기록. 채널이 사람 확인 경로(MCP 확인 창·터미널 직접 입력)일 때만 게이트가 인정한다."""
    if channel not in HUMAN_CHANNELS:
        raise ValueError(f"사람 확인 채널이 아님: {channel}")
    who = approved_by or getpass.getuser()
    done = []
    for it in items:
        record(conn, port_id, "approve", {"item_id": it["item_id"], "kind": it["kind"], "fingerprint": it["fingerprint"],
                                          "shown": it["summary"], "channel": channel, "approved_by": who})
        done.append(it["item_id"])
    return done


def render(items: list[dict]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. [{it['kind']}] {it['summary']}")
        if it["kind"] in ("divergence", "expectation", "requirements"):
            lines += [f"     - {d['case']}: {d['diff']}" for d in it["detail"]]
        elif it["kind"] == "case_change":
            d = it["detail"]
            if d["removed"]:
                lines.append(f"     - 빠진 케이스: {', '.join(d['removed'][:15])}")
            if d["changed"]:
                lines.append(f"     - 바뀐 케이스: {', '.join(d['changed'][:15])}")
    return "\n".join(lines)
