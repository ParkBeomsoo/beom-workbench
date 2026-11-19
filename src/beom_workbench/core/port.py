"""포팅 — 분석한 기능을 내 저장소로 옮기거나 다른 언어로 다시 만들 때, 빠짐없이 옮기고 같은 동작인지 검증한다.

1) 슬라이스(slice): 진입점(라우트) 또는 심볼에서 앞으로(호출되는 쪽) 닫힘을 모은다.
   - call      구조 지도 호출 간선
   - dispatch  인터페이스 메서드 호출(obj.method()) → 인터페이스 + 저장소 안 구현 전부
   - register  구현을 등록하는 레지스트리 함수 + 등록 위치 + 그 모듈을 불러오는 배선(import 부수효과)
   그 밖에 슬라이스가 읽는 모듈 상태(상수·전역 컬렉션), 설정 키, 외부 호출·import(대체할 라이브러리),
   슬라이스를 직접 부르는 테스트(동작 명세), 파일별 라이선스, 관련 열린 미확인을 함께 낸다.
2) 포팅 원장(port / port_map): 슬라이스의 항목마다 ported(대상 위치) / omitted / replaced(사유) 를 기록.
   pending 이 하나라도 있으면 완료 게이트 실패. ported 의 대상 위치는 대상 저장소에 실제로 있는지 검사한다.
3) 차등 검증(differential): 언어와 무관한 경계(HTTP 요청·CLI 실행)에서 같은 입력을 원본과 대상에 넣고
   결과를 비교한다. 원본 결과는 골든 파일로 저장해 두면 원본 없이도 대상만 반복 검증할 수 있다.
   통과하면 원본 처리 함수 위치에 L3 근거(provider=differential)를 남긴다.

4) 검증 범위 넓히기:
   - 케이스 충분성: 원본을 케이스로 돌린 커버리지에서 슬라이스 함수가 모두 실행됐는지, 안 거친 줄은 어디인지 (게이트)
   - 입력 탐색: 케이스 입력을 변형(경계값·타입·삭제·빈 값)해 원본으로 기대값을 만들고 대상과 비교
   - 함수 단위: HTTP·CLI 경계 없이 함수끼리 직접 비교 (.py·.js 내장 하네스, 그 밖은 stdin/stdout JSON 명령)
   - 동시 실행: 같은 케이스를 대상에 동시에 보내 결과가 흔들리지 않는지
   - 격리: 원본은 Docker(네트워크 차단) 또는 trusted 일 때 임시 사본. 실행 환경 변수에서 토큰·키류는 뺀다

한계(결과에 그대로 표시): 동등성은 '돌려 본 케이스'에 대해서만 증명된다. 성능은 비교하지 않는다(지연 시간은 참고값).
"""
from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from beom_workbench.common.response import ToolError
from beom_workbench.config import Settings
from beom_workbench.core import container, evidence, ledger, runtime
from beom_workbench.core import make as make_mod
from beom_workbench.core import port_guard as guard
from beom_workbench.core import project as project_mod
from beom_workbench.core.customize import _enclosing, _symbols, feature_symbol, fingerprint, resolve_targets
from beom_workbench.core.loop import WEAK_EDGE
from beom_workbench.core.mask import mask_line

STATUSES = {"pending", "ported", "omitted", "replaced"}
ROUTE_RE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|\*)\s+(/\S*)$", re.I)
METHOD_CALL_RE = re.compile(r"\.(\w+)\s*\(")
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
JS_IMPORT_RE = re.compile(r"""(?:require\(\s*["']([^"'.][^"']*)["']\s*\)|^\s*import\s+[^;]*?from\s+["']([^"'.][^"']*)["'])""", re.M)
RUST_USE_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:use|extern\s+crate)\s+:{0,2}([A-Za-z_]\w*)", re.M)
RUST_LOCAL = {"crate", "self", "super", "std", "core", "alloc"}


def _workspace_crates(proj: dict, categories: dict) -> set[str]:
    """저장소 안 Cargo 패키지 이름(- → _) — 외부 의존이 아니다."""
    names = set()
    for path in categories:
        if PurePosixPath(path).name != "Cargo.toml":
            continue
        text = "\n".join(evidence.read_lines(proj, path))
        m = re.search(r'(?ms)^\[package\].*?^name\s*=\s*"([^"]+)"', text)
        if m:
            names.add(m.group(1).replace("-", "_"))
    return names


STATE_RE = re.compile(r"^(?:export\s+)?(?:const\s+|let\s+|var\s+|static\s+|pub\s+static\s+|pub\s+const\s+|const\s+)?([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_route(path: str) -> str:
    return (re.sub(r"\{[^}]*\}|:\w+|<[^>]*>|\[[^\]]*\]", "{}", path).rstrip("/")) or "/"


# ---------------------------------------------------------------- 슬라이스

def _entry_symbols(conn, by_path, targets: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    seeds, entries, notes, rest = [], [], [], []
    for t in targets:
        m = ROUTE_RE.match(t.strip())
        if not m:
            rest.append(t)
            continue
        method, route = m.group(1).upper(), _norm_route(m.group(2))
        found = False
        for f in ledger.rows_to_dicts(conn.execute("SELECT * FROM feature WHERE kind = 'http' AND in_test = 0").fetchall()):
            if _norm_route(f["full_path"] or f["name"].strip("\"'")) != route:
                continue
            if method != "*" and method not in (f["method"] or "").upper().split(","):
                continue
            s = feature_symbol(by_path, f)
            entries.append({"method": f["method"], "path": f["full_path"], "location": f"{f['path']}:{f['start_line']}",
                            "handler": s["id"] if s else None, "assumptions": f["assumptions"]})
            if s:
                seeds.append(s)
            else:
                notes.append(f"{t}: 처리 함수 심볼을 찾지 못함 ({f['path']}:{f['start_line']})")
            found = True
        if not found:
            notes.append(f"'{t}' 라우트가 기능 카탈로그에 없음 (features 로 확인)")
    if rest:
        syms, more = resolve_targets(conn, rest)
        seeds += syms
        notes += more
    return seeds, entries, notes


def _interfaces(conn, by_path) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """메서드 이름 → [인터페이스 메서드 + 구현 메서드 심볼], 구현 클래스 id → 인터페이스 확장 포인트."""
    by_method: dict[str, list[dict]] = defaultdict(list)
    impl_of: dict[str, dict] = {}
    for xp in ledger.rows_to_dicts(conn.execute("SELECT * FROM extension_point WHERE kind = 'interface'").fetchall()):
        classes = [c for c in (_class(by_path, xp["path"], xp["name"]),) if c]
        for impl in xp["implementations"]:
            c = _class(by_path, impl["path"], impl["name"])
            if c:
                classes.append(c)
                impl_of[c["id"]] = xp
        for c in classes:
            for s in by_path.get(c["path"], []):
                if s["id"] != c["id"] and c["start_line"] <= s["start_line"] <= c["end_line"] and s["label"] in ("Method", "Function"):
                    by_method[s["name"]].append(s)
    return by_method, impl_of


def _class(by_path, path: str, name: str) -> dict | None:
    return next((s for s in by_path.get(path, []) if s["name"] == name and s["label"] not in ("Method", "Function")), None) \
        or next((s for s in by_path.get(path, []) if s["name"] == name), None)


def _owner_class(by_path, s: dict) -> dict | None:
    if s["label"] != "Method":
        return None
    best = None
    for c in by_path.get(s["path"], []):
        if c["id"] != s["id"] and c["label"] not in ("Method", "Function") and c["start_line"] <= s["start_line"] <= c["end_line"]:
            if best is None or c["start_line"] > best["start_line"]:
                best = c
    return best


def _files_text(paths: list[str], limit: int = 5) -> str:
    """'a.py, b.py 외 3개' — 사람이 읽는 파일 목록."""
    shown = ", ".join(paths[:limit])
    return f"{shown} 외 {len(paths) - limit}개" if len(paths) > limit else shown


def slice_(conn, proj: dict, targets: list[str], depth: int = 10, within: list[str] | None = None) -> dict:
    """within: 포팅 경계(경로 접두 목록). 경계 밖으로 나가는 호출은 하나하나 따라가지 않고 'boundary' 항목으로
    남긴다 — 예) 검색 엔진 전체를 OpenSearch 로 바꿀 때 그 내부는 옮길 대상이 아니라 '대체할 의존'이다."""
    by_qn, by_path = _symbols(conn)
    seeds, entries, notes = _entry_symbols(conn, by_path, targets)
    if not seeds:
        raise ToolError("not_found", "슬라이스 시작점을 찾지 못함", recovery="; ".join(notes) or None)
    categories = {r["path"]: r["category"] for r in conn.execute("SELECT path, category FROM file").fetchall()}
    callees: dict[str, list[dict]] = defaultdict(list)
    for e in conn.execute("SELECT * FROM call_edge").fetchall():
        callees[e["caller"]].append(dict(e))
    by_method, impl_of = _interfaces(conn, by_path)
    registries = ledger.rows_to_dicts(conn.execute("SELECT * FROM extension_point WHERE kind = 'registry'").fetchall())

    members: dict[str, dict] = {}
    weak_skipped: list[str] = []
    boundary: dict[str, dict] = {}
    inside = (lambda p: any(p.startswith(w) for w in within)) if within else (lambda p: True)
    external_calls: dict[str, set] = defaultdict(set)
    registrations: list[dict] = []
    queue = []

    def add(s: dict, via: str, frm: str | None, d: int) -> None:
        if s["id"] in members or categories.get(s["path"]) == "test":
            return
        if via != "entry" and not inside(s["path"]):
            # 경계 밖: 구조 지도로 확정된 호출(call)만 '대체할 의존'으로 남기고, 이름 기반 갈래는 버린다
            if via == "call":
                b = boundary.setdefault(s["id"], {"symbol_id": s["id"], "name": s["name"], "label": s["label"],
                                                  "path": s["path"], "start_line": s["start_line"], "callers": []})
                caller = members.get(frm, {}).get("symbol", {}).get("name")
                if caller and caller not in b["callers"]:
                    b["callers"].append(caller)
            return
        members[s["id"]] = {"symbol": s, "via": via, "from": frm, "depth": d}
        queue.append(s["id"])

    for s in seeds:
        add(s, "entry", None, 0)
    while queue:
        sid = queue.pop(0)
        cur = members[sid]
        s, d = cur["symbol"], cur["depth"]
        if d >= depth:
            continue
        for e in callees.get(s["qualified_name"], []):
            if e["callee"].startswith("__route__"):
                continue
            t = by_qn.get(e["callee"])
            if t is not None:
                if e["confidence"] is not None and e["confidence"] < WEAK_EDGE:
                    weak_skipped.append(f"{s['name']} → {t['name']} ({e['strategy']}, {e['confidence']})")
                    continue  # 이름만 같은 엉뚱한 함수로 이어진 간선일 가능성이 큼
                add(t, "call", sid, d + 1)
            elif (e["callee_path"] or "").startswith("<") or not e["callee_path"]:
                external_calls[e["callee"]].add(s["name"])
        try:
            body = evidence.read_lines(proj, s["path"])[s["start_line"] - 1:s["end_line"]]
        except (OSError, ToolError):
            body = []
        for m in METHOD_CALL_RE.finditer("\n".join(body)):
            for t in by_method.get(m.group(1), []):
                add(t, "dispatch", sid, d + 1)
        owner = _owner_class(by_path, s)
        if owner:
            add(owner, "owner", sid, d + 1)
        if s["id"] in impl_of:
            xp = impl_of[s["id"]]
            iface = _class(by_path, xp["path"], xp["name"])
            if iface:
                add(iface, "interface", sid, d + 1)
            for reg in registries:
                for u in reg["usages"]:
                    if u["path"] == s["path"] and s["start_line"] - 3 <= u["line"] <= s["end_line"]:
                        registrations.append({"registry": reg["name"], "at": f"{u['path']}:{u['line']}", "class": s["name"]})
                        regsym = by_path_symbol(by_path, reg["path"], reg["line"])
                        if regsym:
                            add(regsym, "register", sid, d + 1)

    files = sorted({m["symbol"]["path"] for m in members.values()})
    # 배선: 등록이 일어나는 모듈을 import 하는 곳 (import 부수효과로 등록되는 구조를 놓치지 않도록)
    wiring = []
    reg_modules = {PurePosixPath(r["at"].split(":")[0]).stem for r in registrations}
    if reg_modules:
        for path, cat in categories.items():
            if cat != "code" or not path.endswith((".py", ".js", ".ts", ".mjs", ".cjs")):
                continue
            for n, ln in enumerate(evidence.read_lines(proj, path), start=1):
                if not re.match(r"\s*(?:from\s+\S+\s+import|import|const\s+\w+\s*=\s*require|require)\b", ln):
                    continue
                hit = sorted(m for m in reg_modules if re.search(rf"\b{re.escape(m)}\b", ln))
                if hit:
                    wiring.append({"at": f"{path}:{n}", "imports": ", ".join(hit)})
                    if path not in files:
                        files.append(path)

    # 모듈 상태: 슬라이스 파일의 최상위 대입 중 슬라이스 본문이 쓰는 이름
    body_text = {}
    for m in members.values():
        s = m["symbol"]
        body_text[s["id"]] = "\n".join(evidence.read_lines(proj, s["path"])[s["start_line"] - 1:s["end_line"]])
    all_body = "\n".join(body_text.values())
    state = []
    for path in sorted(files):
        lines = evidence.read_lines(proj, path)
        spans = [(x["start_line"], x["end_line"]) for x in by_path.get(path, [])]
        for i, ln in enumerate(lines, start=1):
            if ln[:1].isspace() or any(a <= i <= b for a, b in spans):
                continue
            m = STATE_RE.match(ln)
            if m and re.search(rf"\b{re.escape(m.group(1))}\b", all_body):
                state.append({"id": f"state:{path}:{m.group(1)}", "name": m.group(1), "at": f"{path}:{i}",
                              "text": mask_line(ln.strip(), code=True)[0][:160]})

    imports: dict[str, set] = defaultdict(set)
    repo_tops = {p.split("/")[0] for p in categories} | {PurePosixPath(p).stem for p in categories}
    crates = _workspace_crates(proj, categories) if any(p.endswith(".rs") for p in files) else set()
    for path in files:
        text = "\n".join(evidence.read_lines(proj, path))
        if path.endswith(".rs"):
            for m in RUST_USE_RE.finditer(text):
                if m.group(1) not in RUST_LOCAL and m.group(1) not in crates and m.group(1) not in repo_tops and m.group(1)[0].islower():
                    imports[m.group(1)].add(path)
            continue
        for m in PY_IMPORT_RE.finditer(text) if path.endswith(".py") else JS_IMPORT_RE.finditer(text):
            mod = (m.group(1) or m.group(2) or "").split(".")[0]
            if mod and mod not in repo_tops:
                imports[mod].add(path)

    config_keys = [{"key": r["name"], "at": f"{r['path']}:{r['line']}"} for r in conn.execute(
        f"SELECT name, path, line FROM extension_point WHERE kind = 'config-key' AND path IN ({','.join('?' * len(files))})",
        files).fetchall()] if files else []
    ids = set(members)
    tests = sorted({f"{e['caller_path']}:{e['call_line']}" for e in conn.execute("SELECT * FROM call_edge").fetchall()
                    if categories.get(e["caller_path"]) == "test" and by_qn.get(e["callee"], {}).get("id") in ids})
    lic = ledger.rows_to_dicts(conn.execute("SELECT * FROM license_finding").fetchall())
    licenses = {}
    for path in files:
        own = [x for x in lic if x["path"] == path and x["scope"] == "file"]
        # 가까운 폴더부터, 같은 폴더면 알아본 판정 먼저 — 짧게 줄인 LICENSE(알 수 없음)가 package.json 의 "MIT" 를 가리지 않게
        dirs = sorted([x for x in lic if x["scope"].startswith("dir:") and (x["scope"] == "dir:" or path.startswith(x["scope"][4:] + "/"))],
                      key=lambda x: (-len(x["scope"]), x["category"] == "unknown"))
        pick = own or dirs[:1]
        licenses[path] = [{"license": x["license"], "category": x["category"], "source": x["source"]} for x in pick] or [
            {"license": "unknown", "category": "unknown", "source": "none"}]
    unknown_lic = sorted(p for p, ls in licenses.items() if any(x["category"] == "unknown" for x in ls))
    # 저작권 줄은 허용형·알 수 없음(본문을 줄인 LICENSE 등) 라이선스 파일에서 — 제한형은 따로 확인 카드로 막는다
    notices = _copyright_lines(proj, [x for x in lic if x["source"] == "text" and x["category"] in ("permissive", "unknown")
                                      and any(x["scope"] == "dir:" or p.startswith(x["scope"][4:] + "/") for p in files)])
    restricted = sorted(p for p, ls in licenses.items() if any(x["category"] not in ("permissive", "unknown") for x in ls))
    open_unknowns = [dict(r) for r in conn.execute("SELECT id, question, severity, scope FROM unknown WHERE status != 'resolved'").fetchall()
                     if r["scope"] and any(f in r["scope"] for f in files)]

    items = []
    for m in sorted(members.values(), key=lambda m: (m["depth"], m["symbol"]["path"], m["symbol"]["start_line"])):
        s = m["symbol"]
        items.append({"symbol_id": s["id"], "name": s["name"], "label": s["label"], "path": s["path"],
                      "start_line": s["start_line"], "end_line": s["end_line"], "via": m["via"], "from": m["from"],
                      "signature": fingerprint(proj, s)["signature"]})
    return {
        "entries": entries, "notes": notes, "symbols": items, "state": state,
        "within": within, "boundary": sorted(boundary.values(), key=lambda b: (b["path"], b["start_line"])),
        "weak_edges_skipped": {"count": len(weak_skipped), "sample": weak_skipped[:20],
                               "note": f"신뢰도 {WEAK_EDGE} 미만 이름 기반 호출 간선은 따라가지 않음 — 필요하면 LSP(L1)로 확인"},
        "registrations": registrations, "wiring": wiring, "files": sorted(files),
        "lines": sum(s["end_line"] - s["start_line"] + 1 for s in (m["symbol"] for m in members.values())),
        "external": {"imports": {k: sorted(v) for k, v in sorted(imports.items())},
                     "calls": {k: sorted(v) for k, v in sorted(external_calls.items())}},
        "config_keys": config_keys, "tests": tests, "licenses": licenses,
        # 사용자가 카드에서 읽는 글 — 파일 목록은 사람이 읽는 꼴로 (파이썬 목록 표기·도구 이름 없이).
        # 막는 것은 허용형이 아닌 라이선스가 '찾아졌을' 때만 — 표기가 없는 것은 막지 않고 알리기만 한다 (사내 저장소는 보통 없다)
        "license_warning": (f"허용형이 아닌 라이선스의 원본 파일 {_files_text(restricted)} — 옮겨 쓰기 전에 조건을 확인하세요 "
                            "(다른 언어로 다시 쓰더라도 구조·표현을 그대로 옮기면 파생 저작물이 될 수 있습니다)") if restricted else None,
        "license_note": (f"원본 파일 {_files_text(unknown_lic)} 에서 라이선스 표기를 찾지 못했습니다 — 사내 코드라면 보통 그렇고, "
                         "공개 저장소라면 옮겨 써도 되는지 확인하세요") if unknown_lic else None,
        "open_unknowns": open_unknowns,
        **({"notices": notices} if notices else {}),
        "limitations": ["dispatch 갈래는 인터페이스 메서드 이름 기준 — 인터페이스로 잡히지 않은 덕 타이핑·콜백은 빠질 수 있음",
                        *(["경계(within) 밖은 구조 지도 호출만 boundary 로 남김 — 이름 기반 갈래는 경계 밖으로 따라가지 않음"]
                          if within else []),
                        "외부 호출 목록은 구조 지도가 해석한 것만 — 슬라이스 코드를 읽어 확인"],
    }


_COPYRIGHT = re.compile(r"^\s*(?:copyright\b|\(c\)\s|©).*\S", re.I)
NOTICE_FILE = re.compile(r"(?i)^(licen[sc]e|copying|notice|third[_-]?party|credits|authors)([._-].*)?$")


def _copyright_lines(proj: dict, findings: list[dict]) -> list[str]:
    """허용형 라이선스 파일의 저작권 줄 — MIT·BSD·Apache 는 옮긴 코드에도 이 표기와 라이선스 문구를 남겨야 한다."""
    out: list[str] = []
    for fd in findings:
        try:
            lines = evidence.read_lines(proj, fd["path"])
        except (OSError, ToolError):
            continue
        for ln in lines[:60]:
            if _COPYRIGHT.match(ln) and "<" not in ln and "[" not in ln:  # 템플릿 자리표시('[year] [fullname]')는 뺀다
                out.append(" ".join(ln.split()))
    return list(dict.fromkeys(out))[:10]


def missing_notices(root: Path, notices: list[str]) -> list[str]:
    """대상 폴더의 라이선스·NOTICE 류 파일과 README 에 원본 저작권 줄이 없으면 그 줄."""
    if not notices:
        return []
    texts = []
    try:
        for p in root.iterdir():
            if p.is_file() and (NOTICE_FILE.match(p.name) or p.name.lower().startswith("readme")) and p.stat().st_size < 2_000_000:
                texts.append(" ".join(p.read_text(encoding="utf-8", errors="replace").split()).casefold())
    except OSError:
        return []
    return [n for n in notices if not any(n.casefold() in t for t in texts)]


def by_path_symbol(by_path, path: str, line: int) -> dict | None:
    return next((s for s in by_path.get(path, []) if s["start_line"] == line), None) or _enclosing(by_path, path, line)


# ---------------------------------------------------------------- 포팅 원장

def create(conn, proj: dict, *, name: str, targets: list[str], target_language: str, target_root: str,
           author: str = "agent", within: list[str] | None = None, depth: int = 10) -> dict:
    sl = slice_(conn, proj, targets, depth=depth, within=within)
    pid = "port_" + hashlib.sha256(f"{name}|{sorted(targets)}|{target_root}".encode()).hexdigest()[:12]
    items = [(s["symbol_id"], "symbol", f"{s['label']} {s['name']} @ {s['path']}:{s['start_line']}") for s in sl["symbols"]]
    items += [(st["id"], "state", f"{st['name']} @ {st['at']}") for st in sl["state"]]
    items += [(f"wiring:{w['at']}", "wiring", f"import {w['imports']} @ {w['at']}") for w in sl["wiring"]]
    items += [(f"boundary:{b['symbol_id']}", "boundary",
               f"경계 밖 의존 {b['label']} {b['name']} @ {b['path']}:{b['start_line']} (호출: {', '.join(b['callers'])})")
              for b in sl.get("boundary", [])]
    with ledger.tx(conn):
        conn.execute("INSERT INTO port (id, name, targets, target_language, target_root, slice, author, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
                     "targets = excluded.targets, target_language = excluded.target_language, "
                     "target_root = excluded.target_root, slice = excluded.slice",
                     (pid, name, ledger.dumps(targets), target_language, target_root, ledger.dumps(sl), author, _now()))
        for item_id, kind, label in items:
            conn.execute("INSERT OR IGNORE INTO port_map (port_id, item_id, kind, label, status, updated_at) "
                         "VALUES (?, ?, ?, ?, 'pending', ?)", (pid, item_id, kind, label, _now()))
    guard.record(conn, pid, "create", {"targets": targets, "items": len(items), "author": author})
    if target_root and Path(target_root).is_dir():
        guard.record_baseline(conn, pid, Path(target_root))  # 코드를 고치기 전 — 대상 저장소에 원래 있던 테스트 (처음 한 번만)
    return {"port_id": pid, "items": len(items), "boundary": len(sl.get("boundary", [])),
            "slice": {k: sl[k] for k in ("entries", "files", "lines", "external",
                                                                              "config_keys", "tests", "license_warning", "license_note",
                                                                              "registrations", "wiring")}}


def _ref_roots(root: Path) -> list[Path]:
    """매핑할 수 있는 자리 — 대상 폴더, 그리고 대상 폴더가 저장소의 하위 폴더면 그 저장소 ('../goods/goods.py:8' 처럼).
    요청이 형제 모듈까지 고치라고 할 때 그 구현 위치를 적을 길이 없어 에이전트가 '대체(replaced)' 로 돌려 적었다."""
    from beom_workbench.core import layout
    top = layout.repo_top(root)
    return [root.resolve()] + ([top] if top and top != root.resolve() and root.resolve().is_relative_to(top) else [])


def _normalize_ref(root: Path, ref: str) -> str:
    """대상 폴더 기준으로 없는 경로가 같은 저장소 맨 위 기준으로는 있으면 대상 폴더 기준으로 바꾼다 ('api/search/search.py:6' →
    'search.py:6'). 저장소의 하위 폴더가 대상일 때 에이전트가 저장소 기준으로 적으면 거절되어 매핑을 다시 했다."""
    m = re.fullmatch(r"(.+?):(\d+)", (ref or "").strip())
    if not m or (root / m.group(1)).is_file():
        return ref
    for base in _ref_roots(root)[1:]:
        f = (base / m.group(1)).resolve()
        if f.is_file() and f.is_relative_to(base):
            return os.path.relpath(f, root.resolve()).replace("\\", "/") + ":" + m.group(2)
    return ref


def _check_ref(root: Path, ref: str) -> str | None:
    m = re.fullmatch(r"(.+?):(\d+)", ref.strip())
    if not m:
        return "target 은 '경로:줄' 형식이어야 함"
    f = (root / m.group(1)).resolve()
    if not any(f.is_relative_to(r) for r in _ref_roots(root)) or not f.is_file():
        return f"대상 저장소에 파일 없음: {m.group(1)}"
    n = len(f.read_text(encoding="utf-8", errors="replace").splitlines())
    if not 1 <= int(m.group(2)) <= n:
        return f"{m.group(1)} 는 {n}줄 — {m.group(2)}줄 없음"
    return None


def _ref_line(root: Path, ref: str) -> tuple[str, list[str], int] | None:
    m = re.fullmatch(r"(.+?):(\d+)", (ref or "").strip())
    if not m:
        return None
    lines = (root / m.group(1)).read_text(encoding="utf-8", errors="replace").splitlines()
    return m.group(1), lines, int(m.group(2))


def _drift(root: Path, ref: str, text: str | None) -> dict | None:
    """매핑할 때 본 줄 내용과 지금 줄 내용 비교. 같은 내용이 한 곳에만 있으면 이동한 위치를 알려 준다."""
    if not text:
        return None
    got = _ref_line(root, ref)
    if got is None:
        return None
    path, lines, ln = got
    if lines[ln - 1].strip() == text:
        return None
    where = [i + 1 for i, x in enumerate(lines) if x.strip() == text]
    out = {"error": "대상 줄 내용이 매핑 때와 다름"}
    if len(where) == 1:
        out.update(error="대상 줄이 이동함", moved_to=f"{path}:{where[0]}")
    return out


def accept(conn, port_id: str, patterns: list[str], reason: str, actor: str = "agent") -> dict:
    """의도적인 동작 차이(예: 원본의 500 을 422 로 고침)를 케이스 이름 패턴으로 허용 '제안'. 사유 필수, 원장에 남는다.
    게이트는 사용자가 port-approve(터미널 직접 입력) 또는 MCP 확인 창으로 승인해야 통과한다 — 누가 제안했든 같다."""
    _port(conn, port_id)
    if not reason or not reason.strip():
        raise ToolError("invalid_request", "허용 사유(reason)가 필요함")
    with ledger.tx(conn):
        for pat in patterns:
            conn.execute("INSERT OR REPLACE INTO port_map (port_id, item_id, kind, label, status, target_ref, note, actor, updated_at) "
                         "VALUES (?, ?, 'divergence', ?, 'replaced', NULL, ?, ?, ?)",
                         (port_id, f"diff:{pat}", pat, reason.strip(), actor, _now()))
    guard.record(conn, port_id, "accept", {"patterns": patterns, "reason": reason.strip(), "actor": actor})
    return {"proposed": patterns, "reason": reason,
            "note": "제안으로 기록 — 다음 verify 부터 이 패턴의 실패는 '허용 제안'으로 따로 집계되고, "
                    "사용자가 차이 내용을 보고 승인(port-approve)해야 게이트가 통과한다"}


def _accepted(conn, port_id: str) -> list[tuple[str, str]]:
    return [(r["label"], r["note"]) for r in conn.execute(
        "SELECT label, note FROM port_map WHERE port_id = ? AND kind = 'divergence'", (port_id,)).fetchall()]


def follow(conn, port_id: str, actor: str = "agent") -> dict:
    """이동한 ported 대상(같은 내용이 한 곳에만 있음)을 새 위치로 갱신. 내용이 바뀐 것은 그대로 둔다(다시 매핑)."""
    port = _port(conn, port_id)
    root = Path(port["target_root"])
    moved, left = [], []
    with ledger.tx(conn):
        for r in conn.execute("SELECT * FROM port_map WHERE port_id = ? AND status = 'ported'", (port_id,)).fetchall():
            if _check_ref(root, r["target_ref"]):
                left.append({"item": r["item_id"], "target": r["target_ref"], "error": _check_ref(root, r["target_ref"])})
                continue
            d = _drift(root, r["target_ref"], r["target_text"])
            if not d:
                continue
            if d.get("moved_to"):
                conn.execute("UPDATE port_map SET target_ref = ?, actor = ?, updated_at = ? WHERE port_id = ? AND item_id = ?",
                             (d["moved_to"], actor, _now(), port_id, r["item_id"]))
                moved.append({"item": r["item_id"], "from": r["target_ref"], "to": d["moved_to"]})
            else:
                left.append({"item": r["item_id"], "target": r["target_ref"], "error": d["error"]})
    guard.record(conn, port_id, "follow", {"moved": len(moved), "actor": actor})
    return {"moved": moved, "needs_remap": left, **status_(conn, port_id, brief=True)}


def map_items(conn, port_id: str, updates: list[dict], actor: str = "agent") -> dict:
    port = _port(conn, port_id)
    root = Path(port["target_root"])
    results = []
    with ledger.tx(conn):
        for u in updates:
            item, status = u.get("item"), u.get("status")
            row = conn.execute("SELECT * FROM port_map WHERE port_id = ? AND item_id = ?", (port_id, item)).fetchone()
            err = None
            if row is None:
                err = f"슬라이스에 없는 항목: {item}"
            elif status not in STATUSES - {"pending"}:
                err = "status 는 ported / omitted / replaced 중 하나"
            elif status == "ported" and not u.get("target"):
                err = "ported 는 target(대상 저장소 '경로:줄')이 필요함"
            elif status == "ported":
                u = {**u, "target": _normalize_ref(root, u["target"])}
                err = _check_ref(root, u["target"])
            elif not u.get("note"):
                err = f"{status} 는 note(사유)가 필요함"
            if err:
                results.append({"item": item, "ok": False, "error": err})
                continue
            text = None
            if status == "ported":
                _, lines, ln = _ref_line(root, u["target"])
                text = lines[ln - 1].strip()
            conn.execute("UPDATE port_map SET status = ?, target_ref = ?, target_text = ?, note = ?, actor = ?, updated_at = ? "
                         "WHERE port_id = ? AND item_id = ?",
                         (status, u.get("target"), text, u.get("note"), actor, _now(), port_id, item))
            results.append({"item": item, "ok": True, "status": status})
    guard.record(conn, port_id, "map", {"updated": [r["item"] for r in results if r["ok"]], "actor": actor})
    out = {"results": results, **status_(conn, port_id, brief=True)}
    if any(u.get("status") == "omitted" for u in updates):
        out["note"] = "omitted(생략)는 사용자 승인(port-approve)을 받아야 게이트가 통과한다"
    return out


def _license_ok(conn, port_id: str, warning: str | None) -> bool:
    """라이선스 경고가 없거나, 사람이 지금 그 경고를 보고 옮겨도 된다고 확인했다 (경고 문구가 바뀌면 다시 확인)."""
    if not warning:
        return True
    for r in conn.execute("SELECT payload FROM port_log WHERE port_id = ? AND action = 'license_ack'", (port_id,)).fetchall():
        try:
            if json.loads(r["payload"]).get("warning") == warning:
                return True
        except (TypeError, ValueError):
            continue
    return False


def ack_license(conn, port_id: str, *, channel: str, approved_by: str | None = None) -> dict:
    """사용자가 라이선스 경고를 확인했다 — 원본을 옮겨 써도 된다는 사람의 판단. 결정 기록(사슬)에 남긴다.
    에이전트 도구에는 없다 (앱의 확인 카드에서만)."""
    port = _port(conn, port_id)
    warning = port["slice"].get("license_warning")
    if not warning:
        return {"acked": False, "reason": "라이선스 경고가 없습니다"}
    if _license_ok(conn, port_id, warning):
        return {"acked": True, "already": True}
    guard.record(conn, port_id, "license_ack", {"warning": warning, "channel": channel, "approved_by": approved_by})
    return {"acked": True}


def _port(conn, port_id: str) -> dict:
    row = ledger.row_to_dict(conn.execute("SELECT * FROM port WHERE id = ?", (port_id,)).fetchone())
    if row is None:
        raise ToolError("not_found", f"포팅 없음: {port_id}")
    return row


GATE_MAPPED = "모든 항목 매핑됨 (pending 0)"
GATE_TARGETS = "ported 대상 위치가 모두 존재 (줄 내용도 그대로)"
GATE_DIFF = "차등 검증 통과 (마지막 capture 의 케이스 전부, 지금 대상 코드 기준)"
GATE_ADEQUACY = "케이스가 슬라이스 함수를 모두 실행 (원본 커버리지, 없으면 대상 커버리지)"
GATE_HUMAN = "사람 확인 항목 모두 승인됨 (차이 허용·생략·케이스 변경·기대값)"
GATE_INTEGRITY = "기록 무결 (툴킷 밖에서 원장·골든을 고치지 않음)"
GATE_LICENSE = "라이선스 확인 필요 없음"
GATE_REQ_CASES = "요구사항마다 합격 케이스가 있음 (expect + req)"
GATE_PROJECT = "새로 만든 테스트·타입 검사 통과 (툴킷이 검증 때 함께 돌림)"


def _diff_state(last: dict | None, capture: dict | None, fp_now: str) -> dict:
    """마지막 차등 검증이 지금도 유효한지 — 통과했는지, capture 뒤에 돌렸는지, 케이스를 빼지 않았는지, 코드가 그대로인지."""
    if not last:
        return {"ok": False, "why": "차등 검증 기록 없음"}
    d = last["detail"]
    if not (last["failed"] == 0 and last["passed"] > 0):
        return {"ok": False, "why": f"마지막 검증 실패 {last['failed']}건"}
    if capture:
        if last["_rowid"] < capture["_rowid"]:
            return {"ok": False, "why": "마지막 capture 뒤에 검증하지 않음"}
        rules = d.get("rules") or {}
        base = capture["detail"].get("rules") or {}
        missing = sorted(n for n in base if rules.get(n) != base[n])
        if missing:
            return {"ok": False, "why": f"capture 케이스 {len(missing)}개를 빼거나 바꿔서 검증함", "cases": missing[:10]}
    if d.get("target_fp") is None:
        return {"ok": False, "why": "검증 때 대상 코드 지문이 없음 (v9 이전 검증) — 다시 verify"}
    if d["target_fp"] != fp_now:
        return {"ok": False, "why": "검증 뒤 대상 코드가 바뀜 — 다시 verify"}
    return {"ok": True}


_PROJECT_WHY: dict[int, str] = {}


def _project_state(chk: dict | None, fp_now: str) -> dict:
    """검증 때 함께 돈 프로젝트 테스트·타입 검사 — 기록이 없으면(예전 실행·테스트 없는 프로젝트) 막지 않는다."""
    if not chk:
        return {"ok": True, "why": None}
    d = chk["detail"]
    bad = [r for r in d.get("results") or [] if not r.get("ok") and not r.get("skipped")]
    if bad:
        why = "프로젝트 " + " · ".join(f"{'테스트' if r.get('task') == 'test' else '타입 검사'} 실패 ({r.get('command')}"
                                      f"{': ' + r['summary'] if r.get('summary') else ''})" for r in bad[:3])
        return {"ok": False, "why": why, "failed": bad[:3]}
    if d.get("target_fp") != fp_now:
        return {"ok": False, "why": "검사 뒤 대상 코드가 바뀜"}
    return {"ok": True, "why": None, "ran": [f"{r.get('task')}: {r.get('command')}" for r in d.get("results") or []][:6]}


def project_why(gates: dict) -> str:
    return gates.get("_project_why") or "프로젝트 테스트·타입 검사 실패"


def _adequacy_state(src: dict | None, tgt: dict | None, fp_now: str) -> dict:
    if src is not None:
        ok = src["failed"] == 0 and src["cases"] > 0
        return {"ok": ok, "basis": "원본 커버리지",
                **({} if ok else {"why": f"케이스가 실행하지 못한 슬라이스 함수 {src['failed']}개"})}
    if tgt is not None:
        if tgt["detail"].get("target_fp") != fp_now:
            return {"ok": False, "basis": "대상 커버리지", "why": "측정 뒤 대상 코드가 바뀜 — 다시 verify --target-coverage-report"}
        ok = tgt["failed"] == 0 and tgt["cases"] > 0
        return {"ok": ok, "basis": "대상 커버리지",
                **({} if ok else {"why": f"케이스가 실행하지 못한 ported 대상 {tgt['failed']}개"})}
    return {"ok": False, "basis": None,
            "why": "커버리지 미측정 (원본 capture --coverage-report 또는 verify --target-coverage-report)"}


def _next_step(gates: dict, *, counts, capture, diff, human, adequacy, finished) -> str:
    if not gates[GATE_INTEGRITY]:
        return "기록이 툴킷 밖에서 바뀜 — 사용자에게 알리고, 바뀐 단계를 툴킷으로 다시 실행 (capture·verify·map)"
    if not gates[GATE_MAPPED]:
        return f"port-map 으로 남은 {counts.get('pending', 0)}개 매핑 (ported 는 '경로:줄', omitted·replaced 는 사유)"
    if not gates[GATE_TARGETS]:
        return "port-map --follow (이동한 대상) 또는 바뀐 대상 다시 매핑"
    if capture is None:
        return "port-capture 로 원본 응답(골든) 저장"
    if not gates[GATE_DIFF]:
        return f"port-verify — {diff['why']}"
    if not gates.get(GATE_PROJECT, True):
        return f"{project_why(gates)} — 고친 뒤 port-verify (테스트·타입 검사는 툴킷이 검증 때 함께 돌린다)"
    if not gates[GATE_ADEQUACY] and not adequacy.get("user_card"):
        return f"케이스 보강 또는 커버리지 측정 — {adequacy['why']}"
    if not gates[GATE_HUMAN]:
        n = sum(1 for h in human if not h["approved"])
        return (f"사용자 승인 대기 {n}건 — 사용자에게 내용을 설명하고 승인을 요청 (MCP wb_port_approve 확인 창, "
                "또는 사용자가 터미널에서 직접 beom-workbench port-approve). AI 가 대신 승인할 수 없음")
    if not gates[GATE_LICENSE]:
        return "라이선스 경고 확인 (사용자 판단 필요)"
    if not finished or not finished.get("valid"):
        return "port-finish 로 완료 판정 받기 (완료 보고는 port-finish 결과로만)"
    return "완료 — port-finish 판정 유효"


def _next_step_request(gates: dict, *, req_state, capture, diff, human, adequacy, finished) -> str:
    """요청사항 모드 순서: 요구사항 → 합격 기준 → 사람 승인(구현 전에 목표 고정) → 구현·매핑 → 검증 → 완료."""
    if not gates[GATE_INTEGRITY]:
        return "기록이 툴킷 밖에서 바뀜 — 사용자에게 알리고, 바뀐 단계를 툴킷으로 다시 실행"
    if not req_state["total"]:
        return "make-req 로 요구사항 목록 작성 (사용자 요청을 확인 가능한 문장으로 나눔)"
    if capture is None or not gates[GATE_REQ_CASES]:
        missing = ", ".join(req_state["uncovered"]) if capture is not None else "전체"
        return (f"요구사항마다 합격 케이스(expect + req) 작성 후 port-capture — 케이스 없는 요구사항: {missing}. "
                "기대값은 사용자 요청에서 정하고 대상 코드 출력을 베끼지 않는다")
    pending = [h for h in human if not h["approved"]]
    if pending and any(h["kind"] in ("requirements", "expectation", "case_change") for h in pending):
        return (f"사용자 승인 대기 {len(pending)}건 — 구현 전에 요구사항·합격 기준을 사용자에게 보여 주고 승인받기 "
                "(MCP wb_port_approve 확인 창, 앱의 승인 카드, 또는 사용자가 터미널에서 port-approve). AI 가 대신 승인할 수 없음")
    if not gates[GATE_MAPPED]:
        left = req_state["total"] - req_state["implemented"]
        return f"구현 후 port-map 으로 요구사항 {left}개의 구현 위치(경로:줄) 기록"
    if not gates[GATE_TARGETS]:
        return "port-map --follow (이동한 구현 위치) 또는 바뀐 위치 다시 매핑"
    if not gates[GATE_DIFF]:
        return f"port-verify — {diff['why']}"
    if not gates.get(GATE_PROJECT, True):
        return f"{project_why(gates)} — 고친 뒤 port-verify (테스트·타입 검사는 툴킷이 검증 때 함께 돌린다)"
    if not gates[GATE_ADEQUACY] and not adequacy.get("user_card"):
        return f"port-verify 로 구현 코드 실행 확인 (측정은 툴킷이 한다) — {adequacy['why']}"
    if not gates[GATE_HUMAN]:
        n = sum(1 for h in human if not h["approved"])
        return f"사용자 승인 대기 {n}건 — 사용자에게 내용을 설명하고 승인 요청. AI 가 대신 승인할 수 없음"
    if not finished or not finished.get("valid"):
        return "port-finish 로 완료 판정 받기 (완료 보고는 port-finish 결과로만)"
    return "완료 — port-finish 판정 유효"


def agent_status(conn, port_id: str) -> dict:
    """에이전트가 다음 할 일을 정하는 데 쓰는 상태 — 게이트·next_step·남은 매핑·승인 대기 항목(짧은 설명)만.
    전체 상태(모든 매핑 줄·모든 승인 항목의 상세)는 매번 문맥에 수만 자가 쌓여, 자주 부르는 에이전트일수록 토큰이 커진다."""
    st = status_(conn, port_id, brief=True)
    rows = conn.execute("SELECT item_id, kind, label FROM port_map WHERE port_id = ? AND status = 'pending' ORDER BY item_id",
                        (port_id,)).fetchall()
    if rows:
        st["pending"] = [{"item": r["item_id"], "label": (r["label"] or "")[:80]} for r in rows[:30]]
    waiting = [h for h in guard.reviews(conn, port_id, _last_check(conn, port_id, "differential:%"),
                                        _last_check(conn, port_id, "capture")) if not h["approved"]]
    if waiting:
        st["waiting_approval"] = [{"item": h["item_id"], "kind": h["kind"], "summary": h["summary"][:140]} for h in waiting[:20]]
    return st


def status_(conn, port_id: str, brief: bool = False, fp_max_age: float = 0) -> dict:
    port = _port(conn, port_id)
    rows = ledger.rows_to_dicts(conn.execute("SELECT * FROM port_map WHERE port_id = ? ORDER BY kind, label", (port_id,)).fetchall())
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["kind"] != "divergence":
            counts[r["status"]] += 1
    # ported 대상이 지금도 있는지 (대상 저장소가 바뀌었을 수 있음)
    root = Path(port["target_root"])
    broken = []
    for r in rows:
        if r["status"] != "ported" or r["kind"] == "divergence":
            continue
        e = _check_ref(root, r["target_ref"])
        d = None if e else _drift(root, r["target_ref"], r.get("target_text"))
        if e or d:
            broken.append({"item": r["item_id"], "label": r["label"], "target": r["target_ref"], **({"error": e} if e else d)})
    fp_now = guard.target_fingerprint(root, max_age=fp_max_age, extra=_outside_refs(conn, port_id, root))  # 앱 화면만 몇 초 안의 값을 다시 쓴다 (판정은 언제나 새로)
    last = _last_check(conn, port_id, "differential:%")
    capture_ = _last_check(conn, port_id, "capture")
    src_adq = None  # 원본 커버리지는 마지막 capture 가 함께 잰 것만 인정 (예전 capture 의 넓은 케이스로 통과하지 않게)
    if capture_ and capture_["detail"].get("adequacy_check"):
        row = conn.execute("SELECT * FROM port_check WHERE id = ?", (capture_["detail"]["adequacy_check"],)).fetchone()
        src_adq = ledger.row_to_dict(row) if row else None
    elif capture_ is None:
        src_adq = _last_check(conn, port_id, "adequacy")  # v9 이전 기록
    tgt_adq = _last_check(conn, port_id, "adequacy:target")
    diff = _diff_state(last, capture_, fp_now)
    project_state = _project_state(_last_check(conn, port_id, "project"), fp_now)
    adequacy = _adequacy_state(src_adq, tgt_adq, fp_now)
    human = guard.reviews(conn, port_id, last, capture_)
    card = next((h for h in human if h["kind"] == "coverage_unmeasured"), None)
    measured_ok = adequacy["ok"] or adequacy.get("basis") is None  # 잰 것은 다 거쳤다 (또는 잴 수 있는 것이 없었다)
    if card and card["approved"] and measured_ok:
        adequacy = {"ok": True, "basis": (adequacy.get("basis") + " + " if adequacy["ok"] else "") + "사용자 확인 (툴킷이 재지 못한 파일)"}
    elif card and not card["approved"] and measured_ok:
        # 잰 것이 통과여도 못 잰 파일이 남았다 — 이 관문은 사용자 확인 뒤에 통과 (예전에는 잰 부분만 보고 통과로 보였다)
        adequacy = {"ok": False, "basis": adequacy.get("basis"), "user_card": True,
                    "why": "툴킷이 일부 파일의 실행 범위를 재지 못함 — 사용자 확인 카드로 넘겼다 (도구를 설치하거나 측정 명령을 짤 일 아님)"}
    integ = guard.integrity(conn, port_id)
    gates = {
        GATE_MAPPED: counts.get("pending", 0) == 0,
        GATE_TARGETS: not broken,
        GATE_DIFF: diff["ok"],
        GATE_ADEQUACY: adequacy["ok"],
        GATE_HUMAN: all(h["approved"] for h in human),
        GATE_INTEGRITY: integ["ok"],
        GATE_LICENSE: _license_ok(conn, port_id, port["slice"].get("license_warning")),
        GATE_PROJECT: project_state["ok"],
    }
    request_mode = make_mod.is_request(port)
    req_state = None
    if request_mode:
        reqs = make_mod.requirements(conn, port_id)
        cover = (capture_ or {}).get("detail", {}).get("req_cover") or {}
        uncovered = [r["id"] for r in reqs if not cover.get(r["id"]) and r["status"] != "omitted"]
        req_state = {"total": len(reqs), "uncovered": uncovered,
                     "implemented": sum(1 for r in reqs if r["status"] == "ported")}
        gates[GATE_REQ_CASES] = bool(reqs) and capture_ is not None and not uncovered
    complete = all(gates.values())
    if not project_state["ok"]:
        gates_for_text = {**gates, "_project_why": project_state["why"]}
    else:
        gates_for_text = gates
    fin = _last_check(conn, port_id, "finish")
    finished = None
    if fin:
        finished = {"id": fin["id"], "at": fin["created_at"], "valid": complete and fin["detail"].get("target_fp") == fp_now}
    out = {"port_id": port_id, "name": port["name"], "target_language": port["target_language"], "counts": dict(counts),
           "gates": gates, "complete": complete, "finished": finished,
           "next_step": (_next_step_request(gates_for_text, req_state=req_state, capture=capture_, diff=diff, human=human,
                                            adequacy=adequacy, finished=finished) if request_mode else
                         _next_step(gates_for_text, counts=counts, capture=capture_, diff=diff, human=human,
                                    adequacy=adequacy, finished=finished)),
           "mode": port.get("mode") or "reference",
           **({"requirements": req_state} if request_mode else {}),
           "broken_targets": broken,
           "differential": diff,
           "project_checks": project_state,
           "adequacy": adequacy,
           "approvals": {"pending": sum(1 for h in human if not h["approved"]),
                         "approved": sum(1 for h in human if h["approved"]),
                         # 그중 기준(요구사항·기대값·케이스 변경)을 다시 정하는 것 — 구현 중에 뜨는 확인(원래 테스트를 바꿈·측정 불가)은
                         # 진행 단계를 '기준 확인' 으로 되돌리지 않는다
                         "criteria": sum(1 for h in human if not h["approved"]
                                         and h["kind"] in ("requirements", "expectation", "case_change"))},
           "last_check": {k: last[k] for k in ("kind", "cases", "passed", "failed", "evidence_id", "created_at")} if last else None}
    warning = port["slice"].get("license_warning")
    if warning:  # 화면이 확인 카드를 그리도록 (간략 판정에도)
        out["license"] = {"warning": warning, "acked": gates[GATE_LICENSE]}
    if port["slice"].get("license_note"):  # 막지 않는 참고 — 화면·인계에 한 줄로
        out["license_note"] = port["slice"]["license_note"]
    missing = missing_notices(Path(port["target_root"]), port["slice"].get("notices") or [])
    if missing:  # 허용형이라도 저작권 표기는 남겨야 한다 — 막지 않고 할 일로 알린다
        out["license_notice"] = {"missing": missing,
                                 "todo": "원본은 허용형 라이선스라 옮긴 코드에도 원본 저작권 표기와 라이선스 문구를 남겨야 한다 — "
                                         "대상에 LICENSE 가 없으면 원본 라이선스 전문(저작권 줄 포함)을 LICENSE 로, 대상에 이미 다른 "
                                         "라이선스가 있으면 THIRD_PARTY_NOTICES 같은 파일에 원본 이름·저작권 줄·라이선스 전문을 둔다"}
    if not integ["ok"] or integ.get("legacy"):
        out["integrity"] = integ
    if not brief:
        out["pending"] = [{"item": r["item_id"], "kind": r["kind"], "label": r["label"]} for r in rows if r["status"] == "pending"]
        out["reviews"] = human
        out["items"] = rows
        out["license_warning"] = port["slice"].get("license_warning")
        if src_adq:
            out["adequacy"]["untested_lines"] = src_adq["detail"].get("untested_lines")
        untracked = [r["item_id"] for r in rows
                     if r["status"] == "ported" and not r.get("target_text") and r["kind"] != "divergence"]
        if untracked:
            out["untracked_targets"] = {"items": untracked,
                                        "note": "줄 내용 기록 없이 매핑됨(v8 이전) — 줄이 밀려도 못 잡음, 다시 매핑 권장"}
        if any(b.get("moved_to") for b in broken):
            out["hint"] = "이동한 대상은 port-map --follow 로 새 위치로 갱신"
    return out


def pending_reviews(conn, port_id: str) -> list[dict]:
    _port(conn, port_id)
    return [h for h in guard.reviews(conn, port_id, _last_check(conn, port_id, "differential:%"),
                                     _last_check(conn, port_id, "capture")) if not h["approved"]]


def finish(conn, port_id: str, actor: str = "agent") -> dict:
    """완료 판정. 게이트가 하나라도 미충족이면 거부 — '완료' 는 이 결과로만 말한다."""
    st = status_(conn, port_id, brief=True)
    if not st["complete"]:
        unmet = [k for k, v in st["gates"].items() if not v]
        raise ToolError("gates_unmet", f"완료 불가 — 미충족 게이트 {len(unmet)}개: " + " / ".join(unmet),
                        recovery=st["next_step"], data={"gates": st["gates"], "next_step": st["next_step"]})
    port = _port(conn, port_id)
    fp = guard.target_fingerprint(Path(port["target_root"]), fresh=True, extra=_outside_refs(conn, port["id"], Path(port["target_root"])))
    detail = {"gates": st["gates"], "target_fp": fp, "last_check": st["last_check"],
              "adequacy_basis": st["adequacy"]["basis"], "approvals": st["approvals"], "actor": actor}
    cid = "fin_" + uuid.uuid4().hex[:12]
    with ledger.tx(conn):
        conn.execute("INSERT INTO port_check (id, port_id, kind, cases, passed, failed, detail, evidence_id, created_at) "
                     "VALUES (?, ?, 'finish', ?, ?, 0, ?, ?, ?)",
                     (cid, port_id, st["last_check"]["cases"], st["last_check"]["passed"], ledger.dumps(detail),
                      st["last_check"]["evidence_id"], _now()))
    guard.record(conn, port_id, "finish", {"id": cid, "actor": actor}, checks=[cid])
    return {"finished": cid, "gates": st["gates"], "evidence_id": st["last_check"]["evidence_id"],
            "note": "대상 코드가 바뀌면 이 판정은 무효가 되고 port-status 의 finished.valid 가 false 가 된다"}


def _last_check(conn, port_id: str, kind: str) -> dict | None:
    row = conn.execute("SELECT rowid AS _rowid, * FROM port_check WHERE port_id = ? AND kind LIKE ? ORDER BY rowid DESC LIMIT 1",
                       (port_id, kind)).fetchone()
    return ledger.row_to_dict(row) if row else None


def list_(conn) -> dict:
    return {"items": [{**status_(conn, r["id"], brief=True)} for r in conn.execute("SELECT id FROM port ORDER BY created_at").fetchall()]}


# ---------------------------------------------------------------- 차등 검증

HARNESS_MARK = "@@BEOM_HARNESS@@"
CLASSLIKE = {"Class", "Module", "Interface", "Trait", "Struct", "Type", "Enum"}

HARNESS_PY = r'''import dataclasses
import importlib
import json
import sys

MARK = "@@BEOM_HARNESS@@"
root, mod, fn = sys.argv[1:4]
sys.path.insert(0, root)
if len(sys.argv) > 4:  # 대상 폴더가 패키지 안 — 패키지 맨 위 폴더 (api.search.search 처럼 불러온다)
    sys.path.insert(0, sys.argv[4])
req = json.loads(sys.stdin.read() or "{}")


def enc(o):
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if hasattr(o, "__dict__"):
        return vars(o)
    return str(o)


try:
    if mod.startswith("file:"):  # .beom/ 아래 케이스용 보조 파일 — 패키지 이름이 될 수 없어 경로로 불러온다 (프로젝트 모듈은 root 로 import)
        import importlib.util
        spec = importlib.util.spec_from_file_location("beom_case_helper", __import__("os").path.join(root, mod[5:]))
        target = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(target)
    else:
        target = importlib.import_module(mod)
    for part in fn.split("."):
        target = getattr(target, part)
    out = {"ok": True, "return": target(*req.get("args", []), **req.get("kwargs", {}))}
except Exception as e:  # noqa: BLE001
    import os
    import traceback
    here = os.path.abspath(__file__)
    # 어디서 났는지 — 프로젝트(와 케이스 보조 파일) 안의 마지막 몇 줄. 이것이 없으면 에이전트가 원인을 찾으려 툴킷 기록을 뒤진다
    frames = [f for f in traceback.extract_tb(e.__traceback__) if os.path.abspath(f.filename) != here
              and os.path.abspath(f.filename).lower().startswith(os.path.abspath(root).lower())]
    where = [f"{os.path.relpath(f.filename, root)}:{f.lineno} {f.name}: {(f.line or '').strip()[:120]}" for f in frames[-3:]]
    out = {"ok": False, "error": type(e).__name__, "message": str(e), "where": where}
sys.stdout.write("\n" + MARK + json.dumps(out, default=enc))  # ASCII 로 — 파이프 인코딩(Windows cp949)과 상관없이 그대로 전해지게
'''

HARNESS_JS = r'''const path = require("path");
const { pathToFileURL } = require("url");
const Module = require("module");
if (typeof Module.registerHooks === "function") {
  // TypeScript 프로젝트의 상대 import 는 확장자를 안 적거나(./cart) .js 로 적는다(./cart.js → cart.ts) — 번들러처럼 풀어 준다
  Module.registerHooks({
    resolve(spec, ctx, next) {
      try {
        return next(spec, ctx);
      } catch (e) {
        if (!/^\.\.?\//.test(spec) || !e || !/MODULE_NOT_FOUND/.test(String(e.code))) throw e;
        const base = spec.replace(/\.[cm]?js$/, "");
        for (const tail of [".ts", ".tsx", ".mts", ".cts", ".js", "/index.ts", "/index.js"]) {
          try {
            return next(base + tail, ctx);
          } catch (_) {}
        }
        throw e;
      }
    },
  });
}
const MARK = "@@BEOM_HARNESS@@";
const [root, file, fn] = process.argv.slice(2);
let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", async () => {
  let out;
  try {
    const req = JSON.parse(input || "{}");
    const full = path.resolve(root, file);
    let mod;
    try {
      mod = require(full);
    } catch (e) {
      if (e && e.code === "ERR_REQUIRE_ESM") mod = await import(pathToFileURL(full).href);
      else throw e;
    }
    let self = mod;
    let target = mod;
    for (const p of fn.split(".")) {
      self = target;
      target = target == null ? undefined : target[p];
    }
    if (typeof target !== "function") throw new TypeError(fn + " is not a function");
    let r = target.apply(self, req.args || []);
    if (r && typeof r.then === "function") r = await r;
    out = { ok: true, return: r === undefined ? null : r };
  } catch (e) {
    // 어디서 났는지 — 프로젝트 안의 스택 몇 줄 (node_modules·이 하네스 제외)
    const where = String((e && e.stack) || "").split("\n").slice(1).map((s) => s.trim())
      .filter((s) => s.includes(root) && !s.includes("node_modules") && !s.includes(__filename)).slice(0, 3).reverse()  // 파이썬과 같게 바깥 → 안쪽
      .map((s) => s.split(root).join("."));
    out = { ok: false, error: (e && e.name) || "Error", message: String((e && e.message) || e), where };
  }
  process.stdout.write("\n" + MARK + JSON.stringify(out));
});
'''


def _harness_dir(settings: Settings) -> Path:
    d = settings.home / "cache" / "harness"  # 매번 다시 쓰는 파일 — 지워도 된다
    d.mkdir(parents=True, exist_ok=True)
    for name, text in (("harness.py", HARNESS_PY), ("harness.cjs", HARNESS_JS), ("casecov-exit.cjs", CASECOV_EXIT_JS)):
        f = d / name
        if not f.exists() or f.read_text(encoding="utf-8") != text:
            f.write_text(text, encoding="utf-8")
    return d


class _Side:
    """한쪽(원본·대상)의 실행 위치 — 로컬 디렉터리 또는 Docker 이미지(디렉터리를 /work 에 마운트, 네트워크 차단)."""

    def __init__(self, name: str, cwd: Path, harness: Path, image: str | None = None) -> None:
        self.name, self.cwd, self.harness, self.image = name, cwd, harness, image
        self.casecov: _CaseCoverage | None = None  # 검증 중 — 케이스를 돌리는 프로세스의 실행 범위를 잰다
        self.engine = container.detect() if image else None
        if image and self.engine is None:
            raise ToolError("capability_unavailable", "컨테이너 엔진(docker)을 찾지 못함",
                            recovery="beom-workbench setup --sandbox 로 WSL 에 Docker Engine 설치, 또는 image 없이 trusted=True")

    def quote(self, a: str) -> str:
        if self.image or os.name != "nt":
            return shlex.quote(a)
        # cmd.exe 는 따옴표 밖의 & | < > ^ 를 명령 구분으로 본다 — 언제나 큰따옴표로 감싸고, 따옴표 안에서도 특별한
        # " % ! 줄바꿈은 받지 않는다 (케이스 argv·앞선 응답으로 채운 값이 명령을 이어 붙이지 못하게)
        if re.search(r'["%!\r\n]', a):
            raise ToolError("invalid_request", f"Windows 에서 argv 값에 \" % ! 줄바꿈은 쓸 수 없음 — stdin 으로 넘긴다: {a[:60]!r}")
        return '"' + re.sub(r"(\\+)$", r"\1\1", a) + '"'  # 끝의 \ 가 닫는 따옴표를 먹지 않게 (두 배로)

    @property
    def root(self) -> str:
        return "/work" if self.image else str(self.cwd)

    def harness_path(self, name: str) -> str:
        return f"/harness/{name}" if self.image else str(self.harness / name)

    def docker_base(self) -> list[str]:
        e = self.engine
        return e.argv("run", "--rm", "-v", f"{e.path(self.cwd)}:/work", "-v", f"{e.path(self.harness)}:/harness:ro", "-w", "/work")

    def shell(self, cmd: str, *, stdin: bytes = b"", timeout: float = 30, outside: bool = False) -> subprocess.CompletedProcess:
        """outside: 격리 밖에서 — 툴킷이 직접 짠 측정 명령 중 Node 빌드 도구(vitest 등)만, 권한 규칙을 거친 뒤 (_outside_ok)."""
        if self.image:
            argv = self.docker_base() + ["-i", "--network", "none", self.image, "sh", "-c", cmd]
            return subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout)
        run = _auto_run()
        from beom_workbench.core import sandbox
        if run and sandbox.wraps_powershell(run) and not outside:
            # 자동 실행 중 툴킷이 대신 돌리는 검증 명령도 같은 샌드박스 안에서 (대상 폴더·임시·도구 캐시 밖에는 못 쓴다)
            return run_tree(sandbox.wrap_argv(["cmd", "/d", "/c", cmd], run), cwd=self.cwd, stdin=stdin, timeout=timeout, env=self.env())
        return run_tree(cmd, shell=True, cwd=self.cwd, stdin=stdin, timeout=timeout, env=self.env())

    def env(self) -> dict:
        # 파이썬 표준 입출력을 UTF-8 로 — 파이프로 돌리면 Windows 는 cp949 라 한글·✓ 가 깨지거나 쓰다가 죽는다
        # (사용자 터미널은 유니코드로 보여 주므로 이쪽이 실제와 같다). 출력은 UTF-8 로 읽는다.
        env = runtime.safe_env({"PYTHONIOENCODING": "utf-8", **runtime.coverage_env(self.cwd)})
        from beom_workbench.core import deps, sandbox
        run = _auto_run()
        if run and sandbox.wraps_powershell(run):
            sandbox.node_test_env(env)  # 격리 안에서 도는 검증 명령의 node --test 도 한 프로세스로
        extra = deps.env_paths(self.cwd)  # 툴킷이 준비한 의존성 — 'python app.py' 가 .beom/venv 의 파이썬으로 돈다
        if extra:
            key = next((k for k in env if k.upper() == "PATH"), "PATH")
            env[key] = os.pathsep.join(extra + [env.get(key, "")])
            from beom_workbench.core import layout
            pyroot = layout.root_for(self.cwd, "python")
            if deps.venv_python(pyroot).is_file():
                env["VIRTUAL_ENV"] = str(deps.venv_dir(pyroot))
        if self.casecov is not None:
            self.casecov.apply(env)
        return env


def run_tree(args, *, shell: bool = False, cwd=None, stdin: bytes = b"", timeout: float = 30, env=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output, timeout) 과 같되, 시간이 넘으면 프로세스 트리 전체를 끝낸다.
    subprocess.run 은 직접 띄운 프로세스만 끝내서, 그 아래 손자(테스트가 띄운 서버·하위 pytest)가 출력 파이프를 쥐고 있으면
    손자가 끝날 때까지 기다린다 — 실험에서 검증 도구가 30분 동안 돌아오지 않았다."""
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(args, shell=shell, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kw)
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out, err = b"", b""  # 트리 밖으로 빠져나간 프로세스가 파이프를 쥐고 있으면 그 출력은 버린다
        raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err) from None
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fill_port(cmd: str | None, url: str | None) -> tuple[str | None, str | None]:
    """명령·주소의 {port} 를 이 PC 의 빈 포트로 — 포트를 에이전트가 고르면 다른 서버와 겹쳐 실패하고 다시 묻게 된다."""
    if not ((cmd and "{port}" in cmd) or (url and "{port}" in url)):
        return cmd, url
    p = str(free_port())
    return (cmd.replace("{port}", p) if cmd else cmd), (url.replace("{port}", p) if url else url)


def _node_major() -> int:
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, timeout=20).stdout.decode().strip().lstrip("v")
        return int(out.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _outside_refs(conn, port_id: str, root: Path) -> list[Path]:
    """대상 폴더 밖(같은 저장소)에 매핑한 구현 파일 — 지문에 넣는다."""
    out = set()
    rr = root.resolve()
    for r in conn.execute("SELECT target_ref FROM port_map WHERE port_id = ? AND status = 'ported'", (port_id,)).fetchall():
        m = re.fullmatch(r"(.+?):\d+", r["target_ref"] or "")
        if m:
            f = (root / m.group(1)).resolve()
            if not f.is_relative_to(rr) and f.is_file():
                out.add(f)
    return sorted(out)


NODE_RUN_EXTS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")  # 케이스가 node 프로세스로 부르는 파일 (화면 코드 .jsx 등은 빼고)

CASECOV_SITE = r"""# beom-workbench: 검증 케이스가 띄운 파이썬 프로세스의 실행 범위를 잰다 (툴킷 작업 폴더에서만 쓰인다)
import os
import sys


def _beom_casecov():
    mark = os.environ.get("BEOM_CASECOV_DIR")
    coverage = None
    try:
        import coverage
    except Exception:
        extra = os.environ.get("BEOM_CASECOV_LIB")
        if extra:
            sys.path.insert(0, extra)
            try:
                import coverage
            except Exception:  # 다른 버전의 파이썬이라 못 불러옴 — 재지 않고 표시만 남긴다
                coverage = None
            finally:
                try:
                    sys.path.remove(extra)
                except ValueError:
                    pass
    tag = "started" if coverage is not None else "nocov"
    cov = None
    if coverage is not None:
        try:
            # coverage 7.x 는 설치할 때 넣는 .pth 로 이미 켜 두었을 수 있다 — 그때는 None 을 돌려주므로 켜 둔 것을 이어받는다
            cov = coverage.process_startup() or getattr(coverage.process_startup, "coverage", None)
        except Exception:
            tag = "nocov"
    stop = os.environ.get("BEOM_CASECOV_STOP")
    if cov is not None and stop:
        # 검증이 서버를 끌 때 — 이 파일이 생기면 기록을 쓰고 끝난다 (콘솔 신호는 앱 안처럼 콘솔이 없으면 닿지 않는다)
        import threading
        import time

        def _watch():
            while True:
                time.sleep(0.2)
                if os.path.exists(stop):
                    try:
                        cov.stop()
                        cov.save()
                    finally:
                        os._exit(130)
        threading.Thread(target=_watch, daemon=True).start()
    if mark:
        try:
            open(os.path.join(mark, "%s-%d" % (tag, os.getpid())), "w").close()
        except OSError:
            pass


def _beom_chain():
    # 이 폴더 밖의 원래 sitecustomize(배포판이 넣어 둔 것 등)도 이어서 — 우리 것이 가리지 않게
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for p in sys.path:
        if not p or os.path.abspath(p) == here:
            continue
        f = os.path.join(p, "sitecustomize.py")
        if os.path.isfile(f):
            spec = importlib.util.spec_from_file_location("_beom_original_sitecustomize", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return


for _beom_step in (_beom_casecov, _beom_chain):
    try:
        _beom_step()
    except Exception:  # 측정 장치 때문에 사용자 프로그램이 오류를 내지 않게
        pass
"""

CASECOV_EXIT_JS = """// beom-workbench: 검증이 서버를 끌 때 정상 종료 — node 가 실행 범위 기록(NODE_V8_COVERAGE)을 쓰고 끝나게.
// 멈춤 파일이 생길 때만 끝낸다 — 프로그램 자신의 종료 신호 처리(정리 작업)는 건드리지 않는다.
const stop = process.env.BEOM_CASECOV_STOP;
if (stop) {
  const fs = require("fs");
  const t = setInterval(() => { if (fs.existsSync(stop)) process.exit(130); }, 200);
  if (t.unref) t.unref();
}
"""


def _mapped_files(conn, port_id: str, root: Path) -> list[Path]:
    """ported 로 매핑한 구현 파일 (절대 경로) — 같은 저장소의 형제 폴더도."""
    out = []
    for r in conn.execute("SELECT target_ref FROM port_map WHERE port_id = ? AND status = 'ported'", (port_id,)).fetchall():
        m = re.fullmatch(r"(.+?):\d+", r["target_ref"] or "")
        if m and not m.group(1).replace("\\", "/").startswith(".beom/"):
            f = (root / m.group(1)).resolve()
            if f.is_file() and f not in out:
                out.append(f)
    return out


class _CaseCoverage:
    """합격 케이스를 돌리는 바로 그 실행의 실행 범위 — 툴킷이 띄우는 프로세스에 측정을 켠다.
    파이썬: sitecustomize 로 coverage 를 켠다 (그 파이썬에 coverage 가 없으면 툴킷의 것을 빌려 쓴다).
    Node: NODE_V8_COVERAGE (node 가 끝날 때 스스로 쓴다). 서버는 끌 때 정상 종료하도록 신호를 받게 한다.
    예전에는 프로젝트 테스트(pytest·jest…)를 따로 돌려 쟀다 — 그러려면 테스트 실행기를 짐작해야 했고(파이썬 tests/ 를 JS 테스트로
    보는 식), 잰 것도 '케이스가 구현을 거쳤나' 가 아니라 '테스트가 거쳤나' 였다."""

    def __init__(self, root: Path, files: list[Path], harness: Path) -> None:
        self.root = root
        tmp = root / ".beom" / "tmp"
        # 검증마다 따로 — 같은 폴더를 두 검증이 함께 쓰면 한쪽이 다른 쪽 기록을 지우고 멈춤 파일로 그쪽 서버까지 끈다.
        # 하루 넘은 것(멈춘 검증이 남긴 것)만 치운다
        for old in tmp.glob("casecov*") if tmp.is_dir() else []:
            try:
                if time.time() - old.stat().st_mtime > 86400:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
        self.dir = tmp / ("casecov-" + uuid.uuid4().hex[:8])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.py = [f for f in files if f.suffix == ".py"]
        self.js = [f for f in files if f.suffix in NODE_RUN_EXTS]
        # 매핑한 파일만이 아니라 프로젝트 소스 전체 — 바꾼 함수를 부르는 기존 코드(연관된 곳)를 케이스가 거쳤는지도 보려고
        from beom_workbench.core import impact
        try:
            project_src = impact.source_files(root)
        except (OSError, ValueError):
            project_src = []
        self.js += [f for f in project_src if f.suffix in NODE_RUN_EXTS and f not in self.js]
        self.py += [f for f in project_src if f.suffix == ".py" and f not in self.py]
        # 페이지가 불러가는 JS (브라우저에서 돈다) — 화면 케이스를 돌릴 때 브라우저 엔진에서 잰다
        from beom_workbench.core import layout
        try:
            self.browser = [(root / r).resolve() for r in sorted(layout.browser_files(root)) if r.endswith((".js", ".mjs"))]
            self.browser += layout.inline_script_pages(root)  # HTML 안에 직접 쓴 <script> — 그 HTML 파일의 줄로 잰다
            py_dirs = sorted({str(r) for r in impact.scan_roots(root)} | {str(f.parent) for f in self.py})
        except (OSError, ValueError):
            self.browser, py_dirs = [], sorted({str(f.parent) for f in self.py})
        self.env: dict[str, str] = {}
        self.stop_file = self.dir / "stop"
        if self.py:
            site = self.dir / "site"
            site.mkdir(exist_ok=True)
            (site / "sitecustomize.py").write_text(CASECOV_SITE, encoding="utf-8")
            rc = self.dir / "coveragerc"
            rc.write_text("[run]\nparallel = True\ndata_file = " + str(self.dir / ".cov") + "\nsource =\n"
                          + "".join(f"    {d}\n" for d in py_dirs)
                          + "omit =\n    */.beom/*\n    */node_modules/*\n    */site-packages/*\n    */venv/*\n    */.venv/*\n",
                          encoding="utf-8")
            try:
                import coverage
                lib = {"BEOM_CASECOV_LIB": str(Path(coverage.__file__).parent.parent)}  # 그 파이썬에 없으면 빌려 쓸 자리
            except ImportError:
                lib = {}
            self.env.update(BEOM_CASECOV_STOP=str(self.stop_file))
            self.env.update(PYTHONPATH=str(site), COVERAGE_PROCESS_START=str(rc), COVERAGE_FILE=str(self.dir / ".cov"),
                            BEOM_CASECOV_DIR=str(self.dir), **lib)
        if self.js:
            (self.dir / "v8").mkdir(exist_ok=True)
            hook = harness / "casecov-exit.cjs"
            # NODE_OPTIONS 의 따옴표 안에서는 역슬래시가 이스케이프로 읽힌다 — 경로는 / 로
            self.env.update(NODE_V8_COVERAGE=str(self.dir / "v8"), NODE_OPTIONS=f'--require "{hook.as_posix()}"',
                            BEOM_CASECOV_STOP=str(self.stop_file))

    def apply(self, env: dict) -> None:
        for k, v in self.env.items():
            if k in ("PYTHONPATH", "NODE_OPTIONS") and env.get(k):
                env[k] = v + (os.pathsep if k == "PYTHONPATH" else " ") + env[k]
            else:
                env[k] = v

    def reports(self) -> tuple[list[Path], dict]:
        """(리포트 파일들, 상태) — 상태: python_nocov(파이썬에서 측정을 못 켬)·python_ran·node_ran."""
        out, state = [], {}
        if self.py:
            state["python_nocov"] = any(self.dir.glob("nocov-*")) and not any(self.dir.glob("started-*"))
            state["python_ran"] = any(self.dir.glob("started-*")) or any(self.dir.glob("nocov-*"))
            if list(self.dir.glob(".cov.*")):
                try:
                    import coverage
                    cov = coverage.Coverage(data_file=str(self.dir / ".cov"), config_file=str(self.dir / "coveragerc"))
                    cov.combine(data_paths=[str(self.dir)], keep=False)
                    cov.save()
                    js = self.dir / "python.json"
                    cov.json_report(outfile=str(js))
                    out.append(js)
                except Exception:  # noqa: BLE001 — 기록이 깨졌으면 그 언어는 못 잰 것으로 (아래 판정이 카드로 넘긴다)
                    state["python_nocov"] = True
        if self.js:
            v8 = list((self.dir / "v8").glob("*.json"))
            state["node_ran"] = bool(v8)
            text = _v8_lcov(v8, self.js)
            if text:
                f = self.dir / "node.info"
                f.write_text(text, encoding="utf-8")
                out.append(f)
        pages = sorted((self.dir / "browser").glob("page-*.json")) if (self.dir / "browser").is_dir() else []
        if pages:
            state["browser_ran"] = True
            resolve, transformed = self._browser_resolver(pages)
            text = _v8_lcov(pages, self.browser, resolve=resolve)
            state["browser_transformed"] = sorted(transformed)
            if text:
                f = self.dir / "browser.info"
                f.write_text(text, encoding="utf-8")
                out.append(f)
        return out, state

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _browser_resolver(self, pages: list[Path]):
        """브라우저가 받은 스크립트 주소 → 대상 폴더의 파일 — 받은 내용의 지문이 파일과 같을 때만 (빌드·변환된 코드는 줄 위치가 달라
        그 파일의 실행 범위로 쓸 수 없다 — transformed 로 따로 알린다)."""
        import hashlib
        import urllib.parse
        sources: dict[str, str] = {}
        for p in pages:
            try:
                sources.update(json.loads(p.read_text(encoding="utf-8")).get("sources") or {})
            except (OSError, ValueError):
                continue
        rels = {f: os.path.relpath(f, self.root).replace("\\", "/") for f in self.browser}
        transformed: set[str] = set()

        def resolve(url: str) -> Path | None:
            path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lstrip("/")
            if not path or path.endswith("/"):
                path += "index.html"  # 폴더 주소는 그 폴더의 index.html (정적 서버의 규칙)
            exact = (self.root / path).resolve()
            hits = [exact] if exact in rels else [f for f, r in rels.items() if r == path or r.endswith("/" + path)]
            if len(hits) != 1:
                return None
            f = hits[0]
            try:
                same = hashlib.sha256(f.read_bytes()).hexdigest() == sources.get(url)
            except OSError:
                same = False
            if not same:
                transformed.add(rels[f])
                return None
            return f
        return resolve, transformed


def _v8_lcov(reports: list[Path], files: list[Path], resolve=None) -> str:
    """V8 실행 범위 기록(node·브라우저) → LCOV (주어진 파일만). 함수는 처음 범위의 호출 수, 줄은 그 줄을 품은 가장 안쪽 범위의 수.
    브라우저 기록의 start(문서 안 시작 줄·칸)가 있으면 그만큼 밀어서 — HTML 안에 직접 쓴 <script> 를 HTML 파일의 줄에 맞춘다."""
    import bisect
    import urllib.parse
    import urllib.request
    want = {os.path.normcase(str(f)): f for f in files}
    runs: dict[str, list[list]] = {}
    for rp in reports:
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for sc in data.get("result") or []:
            url = sc.get("url") or ""
            if url.startswith("file:"):
                path = os.path.normcase(os.path.abspath(urllib.request.url2pathname(urllib.parse.urlparse(url).path)))
            elif resolve is not None and (hit := resolve(url)) is not None:
                path = os.path.normcase(str(hit))  # 브라우저 — 받은 주소를 파일로 (내용이 같을 때만)
            else:
                continue
            if path in want:
                runs.setdefault(path, []).append((sc.get("functions") or [], sc.get("start") or [0, 0]))
    out = []
    for key, per_run in runs.items():
        # 원래 줄바꿈 그대로 읽는다 — V8 의 자리는 받은 소스 그대로이고(CRLF 포함), read_text 는 \r\n 을 \n 으로 바꿔
        # 줄마다 한 글자씩 어긋났다 (Windows 의 CRLF 파일). 자리는 UTF-16 단위라 BMP 밖 글자(이모지)는 두 칸으로 센다
        src = want[key].read_bytes().decode("utf-8", "replace")
        if any(ord(ch) > 0xFFFF for ch in src):
            u16, acc = [], 0
            for ch in src:
                u16.append(acc)
                acc += 2 if ord(ch) > 0xFFFF else 1
            to_idx = lambda off: bisect.bisect_left(u16, off)  # noqa: E731
        else:
            to_idx = lambda off: off  # noqa: E731
        starts = [0] + [i + 1 for i, c in enumerate(src) if c == "\n"]
        firsts = []  # 줄마다 처음 글자 자리 (빈 줄은 None)
        for i, st in enumerate(starts):
            end = starts[i + 1] - 1 if i + 1 < len(starts) else len(src)
            line = src[st:end]
            firsts.append(st + (len(line) - len(line.lstrip())) if line.strip() else None)
        lines_at = [(off, i + 1) for i, off in enumerate(firsts) if off is not None]
        offs = [o for o, _ in lines_at]
        line_of = lambda off: bisect.bisect_right(starts, off)  # noqa: E731 — 1부터
        fns: dict[tuple[str, int], int] = {}
        lines: dict[int, int] = {}
        for fn_list, start in per_run:
            sl, sc0 = (int(start[0]), int(start[1])) if isinstance(start, (list, tuple)) and len(start) == 2 else (0, 0)
            base = (starts[sl] if sl < len(starts) else 0) + sc0  # 스크립트가 파일 안에서 시작하는 자리
            paint: dict[int, int] = {}
            spans = []
            for fn in fn_list:
                rs = fn.get("ranges") or []
                if not rs:
                    continue
                ln = line_of(base + to_idx(rs[0]["startOffset"]))
                name = (fn.get("functionName") or "(anonymous)") + f"@{ln}"
                if fn.get("functionName") or rs[0]["startOffset"] > 0:  # 스크립트 전체(맨 위) 는 함수가 아니다
                    fns[(name, ln)] = fns.get((name, ln), 0) + int(rs[0]["count"])
                spans.extend((base + to_idx(r["startOffset"]), base + to_idx(r["endOffset"]), int(r["count"])) for r in rs)
            for st, en, cnt in sorted(spans, key=lambda x: x[0] - x[1]):  # 넓은 범위부터 — 안쪽 범위가 덮어쓴다
                for j in range(bisect.bisect_left(offs, st), bisect.bisect_left(offs, en)):  # 그 범위 안의 줄만
                    paint[lines_at[j][1]] = cnt
            for ln, cnt in paint.items():
                lines[ln] = max(lines.get(ln, 0), cnt)
        out.append(f"SF:{want[key]}")
        for (name, ln), cnt in sorted(fns.items(), key=lambda x: x[0][1]):
            out.append(f"FN:{ln},{name}")
            out.append(f"FNDA:{cnt},{name}")
        out.extend(f"DA:{ln},{cnt}" for ln, cnt in sorted(lines.items()))
        out.append("end_of_record")
    return "\n".join(out) + ("\n" if out else "")


def tasks_mod():
    from beom_workbench.core import tasks
    return tasks


def _auto_run() -> dict | None:
    rid = os.environ.get("BEOM_WORKBENCH_RUN")
    if not rid:
        return None
    try:
        from beom_workbench.config import load_settings
        from beom_workbench.core import inbox
        return inbox.load_run(load_settings(), rid)
    except Exception:  # noqa: BLE001 — 실행 기록을 못 읽으면 감싸지 않는다 (샌드박스는 추가 방어)
        return None


REPEAT_MAX = 1_000_000


def _expand(value):
    """케이스 값 도우미 — {"$repeat": "b", "times": 201} → 'bbb…' (201자). 길이 제한 확인 같은 케이스에서 긴 입력을 손으로 세거나
    스크립트로 케이스 파일을 고치지 않게 (실험: 에이전트가 201자를 만들려고 PowerShell 정규식으로 cases.json 을 고쳤다)."""
    if isinstance(value, dict):
        if set(value) == {"$repeat", "times"}:
            unit, times = value["$repeat"], value["times"]
            if not isinstance(unit, str) or not isinstance(times, int) or isinstance(times, bool) or not 0 <= times * len(unit) <= REPEAT_MAX:
                raise ToolError("invalid_request", f"$repeat 는 {{\"$repeat\": 문자열, \"times\": 0 이상 정수}} (모두 {REPEAT_MAX}자 이하)")
            return unit * times
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _template(value, saved: dict):
    if isinstance(value, dict) and set(value) == {"$repeat", "times"}:
        return _expand(value)
    if isinstance(value, str):
        def rep(m: re.Match) -> str:
            cur = saved
            for part in m.group(1).split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                raise ToolError("invalid_request", f"앞선 케이스 결과에 {m.group(1)} 가 없음")
            return str(cur)
        return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", rep, value)
    if isinstance(value, dict):
        return {k: _template(v, saved) for k, v in value.items()}
    if isinstance(value, list):
        return [_template(v, saved) for v in value]
    return value


_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


def check_url(url: str) -> None:
    """검증 주소 — http(s) 만 (file:// 로 디스크를 읽지 못하게). 자동 실행 중에는 이 PC(루프백)만, 웹 허용 규칙이면 바깥도."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ToolError("invalid_request", f"검증 주소는 http(s) 만: {url[:80]}")
    rid = os.environ.get("BEOM_WORKBENCH_RUN")
    if rid and u.hostname.lower() not in _LOOPBACK:
        from beom_workbench.config import load_settings
        from beom_workbench.core import inbox
        run = inbox.load_run(load_settings(), rid)
        if (run.get("policy") or {}).get("web") != "allow":
            raise ToolError("denied", f"자동 실행 중 검증 주소는 이 PC(localhost)만 — 웹 허용 규칙이 아님: {u.hostname}",
                            recovery="로컬에 띄운 서버로 검증하거나, 사용자에게 웹 허용을 요청한다")


def _http(base: str, case: dict, timeout: float) -> dict:
    url = base.rstrip("/") + case["path"]
    check_url(url)
    data = None
    headers = dict(case.get("headers") or {})
    if "json" in case:
        data = json.dumps(case["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    elif "body" in case:
        data = str(case["body"]).encode()
    req = urllib.request.Request(url, data=data, method=case.get("method", "GET").upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — 사용자가 준 로컬 주소
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        body = text
    return {"status": status, "body": body}


def _cli(side: _Side, cmd: str, case: dict, timeout: float) -> dict:
    argv = " ".join(side.quote(str(a)) for a in case.get("argv", []))
    proc = side.shell(f"{cmd} {argv}".strip(), stdin=(case.get("stdin") or "").encode(), timeout=timeout)
    out = proc.stdout.decode("utf-8", "replace")
    try:
        body = json.loads(out) if out.strip() else None
    except json.JSONDecodeError:
        return {"status": proc.returncode, "body": out.replace("\r\n", "\n")}
    # JSON 으로 읽혔어도 (숫자 하나 '5' 처럼) 원래 글자를 남긴다 — 기대값을 글자("5\n")로 적은 케이스와 비교할 수 있게
    return {"status": proc.returncode, "body": body, "text": out.replace("\r\n", "\n")}


def _plain(text: str) -> str:
    """글자 출력 비교용 — 줄바꿈 꼴(\r\n)과 끝 줄바꿈은 같은 출력으로 본다 (print 가 붙이는 마지막 줄바꿈까지 맞추게 하지 않는다)."""
    return text.replace("\r\n", "\n").rstrip("\n")


def _call(side: _Side, case: dict, timeout: float) -> dict:
    """함수 직접 호출. call.{source|target} = '파일:함수'(.py·.js 내장 하네스) 또는 {cmd}(stdin JSON {args, kwargs} → stdout JSON)."""
    spec = (case.get("call") or {}).get(side.name)
    if spec is None:
        raise ToolError("invalid_request", f"call.{side.name} 가 없음")
    rt = (case.get("runtime") or {}).get(side.name)
    if isinstance(spec, dict):
        cmd = spec["cmd"]
    else:
        m = re.fullmatch(r"(.+?):([\w.$]+)", str(spec))
        if not m:
            raise ToolError("invalid_request", f"call.{side.name} 는 '파일:함수' 또는 {{cmd}} 형식")
        file, fn = m.group(1).replace("\\", "/"), m.group(2)
        ext = PurePosixPath(file).suffix
        if ext == ".py":
            mod = file[:-3].strip("/").replace("/", ".").removesuffix(".__init__")
            if file.lstrip("/").startswith(".beom/"):  # 케이스용 보조 함수 — 커밋되지 않는 작업 폴더에 둔 것
                mod = "file:" + file.lstrip("/")
            if rt or side.image:
                exe = rt or "python"
            else:
                # 툴킷이 준비한 프로젝트 파이썬(.beom/venv — 프로젝트 의존성이 들어 있다) — 툴킷 자신의 파이썬으로 부르면 FastAPI 같은
                # 프로젝트 의존성을 못 찾아 맞게 만든 코드도 실패한다 (에이전트가 '도구 문제'로 막혀 라운드를 되풀이했다)
                from beom_workbench.core import deps
                from beom_workbench.core import layout
                venv = deps.venv_python(layout.root_for(side.root, "python"))
                exe = side.quote(str(venv) if venv.is_file() else sys.executable)
            pkg_base = ""
            if not side.image and not mod.startswith("file:"):
                b = Path(side.root)
                while (b / "__init__.py").is_file() and b.parent != b:
                    b = b.parent
                if b != Path(side.root):
                    # 대상 폴더가 패키지 안(api/search/__init__.py) — 'search.py' 를 맨 위 이름으로 부르면 그 안의
                    # 'from api.goods import …' 가 실패한다 (실험: 에이전트가 보조 파일을 만들어 돌아갔다)
                    mod = os.path.relpath(Path(side.root) / file, b).replace("\\", "/")[:-3].replace("/", ".").removesuffix(".__init__")
                    pkg_base = " " + side.quote(str(b))
            elif not side.image:
                b = Path(side.root)
                while (b / "__init__.py").is_file() and b.parent != b:
                    b = b.parent
                pkg_base = " " + side.quote(str(b)) if b != Path(side.root) else ""
            cmd = (f"{exe} {side.quote(side.harness_path('harness.py'))} {side.quote(side.root)} {side.quote(mod)} {side.quote(fn)}"
                   f"{pkg_base}")
        elif ext in (".js", ".cjs", ".mjs", ".ts", ".mts", ".cts"):
            exe = rt or "node"
            if not rt and ext in (".ts", ".mts", ".cts"):
                major = _node_major()
                if major and major < 22:
                    raise ToolError("capability_unavailable", f"Node {major} 은 .ts 를 바로 불러오지 못함 (22 이상 필요)",
                                    recovery=f'call.{side.name} 에 {{"cmd": "..."}} 로 빌드한 .js 를 부르는 명령을 지정')
                if major in (22, 23):
                    exe = "node --experimental-strip-types"
            cmd = f"{exe} {side.quote(side.harness_path('harness.cjs'))} {side.quote(side.root)} {side.quote(file)} {side.quote(fn)}"
        else:
            raise ToolError("capability_unavailable", f"{ext} 함수 직접 호출용 내장 하네스 없음",
                            recovery=f'call.{side.name} 에 {{"cmd": "..."}} — stdin JSON {{args, kwargs}} 를 받아 '
                                     'stdout 에 {"ok": true, "return": 값} 을 내는 작은 명령을 지정')
    req = json.dumps({"args": side_value(case.get("args"), side.name, []),
                      "kwargs": side_value(case.get("kwargs"), side.name, {})}).encode()
    proc = side.shell(cmd, stdin=req, timeout=timeout)
    out = proc.stdout.decode("utf-8", "replace")
    payload = out.rsplit(HARNESS_MARK, 1)[-1] if HARNESS_MARK in out else out
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        tail = (out + proc.stderr.decode("utf-8", "replace"))[-400:]
        return {"status": None, "body": None, "error": f"하네스 출력이 JSON 이 아님 (exit {proc.returncode}): {tail}"}
    if isinstance(data, dict) and "ok" in data:
        if data["ok"]:
            return {"status": "ok", "body": data.get("return")}
        # 예외 이름·메시지는 언어마다 달라 비교하지 않는다 (던졌다는 사실만 비교)
        where = " ← ".join(str(x) for x in (data.get("where") or [])[::-1])
        return {"status": "raised", "body": None,
                "detail": f"{data.get('error')}: {data.get('message')}"[:300] + (f" (위치: {where[:400]})" if where else "")}
    return {"status": "ok", "body": data}


def per_side(v) -> bool:
    """쪽별 값인가 — {"source": ..., "target": ...}. args 는 원래 목록이라 객체면 쪽별이고, kwargs 는 두 쪽 값이 모두 객체일 때만
    (함수 인자 이름이 source·target 인 경우와 헷갈리지 않게)."""
    return isinstance(v, dict) and bool(v) and set(v) <= {"source", "target"} and all(isinstance(x, (dict, list)) for x in v.values())


def side_value(v, side: str, empty):
    """원본·대상의 인자 모양이 다를 때(JS 옵션 객체 ↔ 파이썬 키워드 인자) 쪽별로 적은 값에서 이쪽 것."""
    if per_side(v):
        return v.get(side, empty)
    return empty if v is None else v


def _drop(body, path: list[str]) -> None:
    """점 경로 값을 지운다. '*' 는 배열의 모든 원소."""
    if not path or not isinstance(body, (dict, list)):
        return
    head, rest = path[0], path[1:]
    if isinstance(body, list):
        if head == "*" and rest:
            for x in body:
                _drop(x, rest)
        return
    if rest:
        _drop(body.get(head), rest)
    else:
        body.pop(head, None)


def _get_path(body, path: list[str]):
    cur = body
    for p in path:
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur


_MISSING = object()


def _get_any(body, path: str):
    """점 경로 값 (배열은 숫자 인덱스: items.0.price). 없으면 _MISSING."""
    cur = body
    for part in path.split(".") if path else []:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


EXPECT_KEYS = {"status", "body", "equals", "match", "visible", "selectors", "no_errors"}
# 케이스 파일에서 읽는 키 — 모르는 키는 거절한다 (오타·다른 형식의 키가 조용히 무시되면 엉뚱한 입력으로 검증된다)
CASE_KEYS = {"name", "argv", "stdin", "method", "path", "json", "body", "headers", "call", "args", "kwargs", "runtime", "screen",
             "expect", "invariant", "req", "new", "compare", "ignore", "match", "explore", "concurrent", "generated",
             "from", "note", "description"}
CASE_INPUT_KEYS = ("argv", "stdin", "method", "path", "json", "body", "headers", "call", "args", "kwargs", "screen")
CASE_KEY_HINTS = {"id": "케이스 이름은 name", "kind": "종류는 키로 정해짐 (argv/stdin=CLI, path/method=HTTP, call=함수)",
                  "cmd": "실행 명령은 케이스가 아니라 source_cli/target_cli 로", "input": "CLI 표준 입력은 stdin",
                  "expected": "기대값은 expect", "requirement": "요구사항 연결은 req", "reqs": "요구사항 연결은 req"}


def check_expect(dst: dict, case: dict) -> list[str]:
    """기대값 케이스 — 원본 대신 사람이 정한 기대값과 비교한다 (원본과 일부러 다르게 만든 동작, 원본에 없는 새 동작).
    expect: {status?, body?(전체 비교, 케이스 ignore 적용), equals?: {점 경로: 값}, match?: {점 경로: 정규식}}"""
    e = case["expect"]
    diffs = []
    if "status" in e and dst["status"] != e["status"]:
        # 예외로 끝났으면 그 이름·메시지·위치까지 — 이것이 없으면 원인을 찾으려 다시 돌리거나 툴킷 기록을 뒤진다
        why = dst.get("detail") or dst.get("error")
        diffs.append(f"status {dst['status']} != 기대 {e['status']}" + (f" — {why}" if why else ""))
    body = dst["body"]
    shown_text = body if isinstance(body, str) else dst.get("text")
    if "body" in e and isinstance(e["body"], str) and shown_text is not None:
        # 기대값을 글자로 적었으면 글자로 비교한다 — CLI 가 '5' 를 찍으면 JSON 숫자로도 읽히지만 기대 "5\n" 과 같은 출력이다
        if _plain(e["body"]) != _plain(shown_text):
            diffs.append(f"body 가 기대와 다름: 기대 {json.dumps(e['body'], ensure_ascii=False)[:300]} / 대상 {json.dumps(shown_text, ensure_ascii=False)[:300]}")
    elif "body" in e:
        a, b = json.loads(json.dumps(e["body"])), json.loads(json.dumps(body))
        for p in case.get("ignore") or []:
            _drop(a, p.split("."))
            _drop(b, p.split("."))
        if a != b:
            diffs.append(f"body 가 기대와 다름: 기대 {json.dumps(a, ensure_ascii=False)[:300]} / 대상 {json.dumps(b, ensure_ascii=False)[:300]}")
    eq, mt = e.get("equals"), e.get("match")
    if eq is not None and not isinstance(eq, dict):  # 줄임 꼴 — 결과 전체가 그 값
        if body != eq:
            diffs.append(f"결과 {json.dumps(body, ensure_ascii=False, default=str)[:200]} != 기대 {eq!r}")
        eq = None
    if isinstance(mt, (str, list)):  # 줄임 꼴 — 결과 전체(오류면 종류·메시지)에서 정규식을 찾는다
        text = json.dumps(body, ensure_ascii=False, default=str) if not isinstance(body, str) else body
        if dst.get("detail"):  # 예외로 끝난 함수 호출 — 'TypeError: …' 가 여기 있다
            text = f"{dst['detail']} {text}"
        for rx in [mt] if isinstance(mt, str) else mt:
            if not re.search(str(rx), text):
                diffs.append(f"결과 {text[:200]} 에서 기대 /{rx}/ 를 찾지 못함")
        mt = None
    for p, want in (eq or {}).items():
        got = _get_any(body, p)
        if got is _MISSING:
            diffs.append(f"{p}: 대상에 없음 (기대 {want!r})")
        elif got != want:
            diffs.append(f"{p}: {got!r} != 기대 {want!r}")
    for p, rx in (mt or {}).items():
        got = _get_any(body, p)
        if got is _MISSING or not re.fullmatch(rx, str(got)):
            diffs.append(f"{p}: {None if got is _MISSING else got!r} 가 기대 /{rx}/ 와 맞지 않음")
    if "visible" in e or "selectors" in e or e.get("no_errors"):  # 화면 케이스
        shown = body if isinstance(body, dict) else {}
        missing = [t for t in e.get("visible") or [] if t not in (shown.get("visible") or [])]
        if missing:
            diffs.append(f"화면에 없음: {', '.join(missing)[:300]}")
        if e.get("selectors") and shown.get("selectors_missing"):
            diffs.append(f"요소 없음: {', '.join(shown['selectors_missing'])[:300]}")
        if e.get("no_errors") and shown.get("errors"):
            diffs.append(f"스크립트 오류: {shown['errors'][0][:200]}")
    return diffs


def check_invariant(dst: dict, case: dict) -> list[str]:
    """원본도 기대값도 없는 입력(요청사항 모드의 입력 변형)에 대한 최소 조건 — 죽지 않고, HTTP 면 5xx 가 아니다."""
    if dst.get("crashed"):
        return ["대상이 이 입력에서 종료됨"]
    status = dst.get("status")
    if ("path" in case or "method" in case) and isinstance(status, int) and status >= 500:
        return [f"서버 오류 status {status} (이상한 입력도 4xx 로 거절해야 함)"]
    return []


def _case_diffs(s: dict, d: dict, c: dict) -> list[str]:
    """한 케이스 판정 — 기대값 케이스는 기대값과, 불변 조건 케이스는 조건으로, 나머지는 원본과."""
    if d.get("error"):
        return [f"대상 오류: {d['error']}"]
    if c.get("invariant"):
        return check_invariant(d, c)
    if c.get("expect"):
        return check_expect(d, c)
    if s.get("error"):
        return [f"원본 오류: {s['error']}"]
    return compare(s, d, c)


def compare(src: dict, dst: dict, case: dict) -> list[str]:
    """케이스 규칙: compare=full(기본)|status, ignore=[점 경로], match={점 경로: 정규식} (양쪽 각각 검사)."""
    diffs = []
    if src["status"] != dst["status"]:
        diffs.append(f"status {src['status']} != {dst['status']}")
    if case.get("compare", "full") == "status":
        return diffs
    a, b = json.loads(json.dumps(src["body"])), json.loads(json.dumps(dst["body"]))
    for p, rx in (case.get("match") or {}).items():
        va, vb = _get_path(a, p.split(".")), _get_path(b, p.split("."))
        if va is None and vb is None:
            continue  # 양쪽 다 없음 (예: 오류 응답) — 같다
        if (va is None) != (vb is None):
            diffs.append(f"{p}: {'대상' if va is None else '원본'}에만 있음 ({va!r} / {vb!r})")
            continue
        for side, v in (("원본", va), ("대상", vb)):
            if not re.fullmatch(rx, str(v)):
                diffs.append(f"{side} {p}={v!r} 가 /{rx}/ 와 맞지 않음")
    for p in list(case.get("ignore") or []) + list((case.get("match") or {}).keys()):
        _drop(a, p.split("."))
        _drop(b, p.split("."))
    if a != b:
        diffs.append(f"body 다름: 원본 {json.dumps(a, ensure_ascii=False)[:300]} / 대상 {json.dumps(b, ensure_ascii=False)[:300]}")
    return diffs


def _port_in_use(url: str) -> str | None:
    """url 의 호스트:포트에 이미 무언가 떠 있으면 'host:port'."""
    import socket
    u = urllib.parse.urlparse(url)
    host, port = u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
    if host not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return None
    try:
        with socket.create_connection(("127.0.0.1" if host in ("localhost", "0.0.0.0") else host, port), timeout=0.5):
            return f"{host}:{port}"
    except OSError:
        return None


class _Server:
    """검증용으로 잠깐 띄우는 서버. url 이 응답할 때까지(어떤 상태 코드든) 기다린다.

    출력은 임시 파일로 (파이프를 안 읽으면 버퍼가 차서 서버가 멈춤). 종료는 정상 종료 신호 먼저 —
    커버리지 도구는 정상 종료 때 기록을 쓴다 — 안 되면 프로세스 트리 강제 종료.
    """

    def __init__(self, side: _Side, cmd: str, url: str, wait: float = 60) -> None:
        if not url or not str(url).startswith(("http://", "https://")):
            # 주소 없이 띄우면 응답을 기다릴 수 없다 — 띄우기 전에 거절 (예전에는 띄운 뒤 오류로 빠져나가 서버가 남았다)
            raise ToolError("invalid_request", f"서버 명령(target_cmd)에는 그 서버의 주소(target_url, 예: http://127.0.0.1:8000)가 필요함: {cmd}")
        self.side, self.cmd, self.url, self.wait = side, cmd, url, wait
        self.restarts = 0
        self.log = tempfile.TemporaryFile()
        self._start()

    def _start(self) -> None:
        side, cmd, url, wait = self.side, self.cmd, self.url, self.wait
        self.container = None
        held = _port_in_use(url)
        if held:  # 앞선 검증·개발 서버가 그 포트를 쥐고 있으면 새 서버는 못 뜨고 옛 코드로 검증하게 된다
            raise ToolError("conflict", f"{held} 를 이미 다른 프로그램이 쓰고 있어 검증용 서버를 띄우지 않았습니다: {cmd}",
                            recovery="그 포트의 서버(앱의 '백그라운드' 목록에서 보임)를 끄거나 다른 포트를 쓰세요")
        if side.image:
            port = urllib.parse.urlparse(url).port
            if not port:
                raise ToolError("invalid_request", "Docker 로 서버를 띄우려면 url 에 포트가 있어야 함")
            self.container = "beom-port-" + uuid.uuid4().hex[:10]
            inner = cmd if re.search(r"&&|\|\||[;|&]", cmd) else f"exec {cmd}"  # sh 가 PID 1 이면 SIGTERM 을 안 넘김
            argv = side.docker_base() + ["--name", self.container, "-p", f"127.0.0.1:{port}:{port}", side.image, "sh", "-c", inner]
            self.proc = subprocess.Popen(argv, stdout=self.log, stderr=subprocess.STDOUT)
        else:
            kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            env = side.env()
            args, shell = _direct_argv(cmd, env)
            if urllib.parse.urlparse(url).port:
                # 기다리는 주소의 포트를 PORT 로도 — PORT 를 읽는 서버는 명령에 포트를 적지 않아도 그 주소에 뜬다
                # (실험에서 Codex 가 node -e "process.env.PORT=…" 같은 우회 명령을 짰다)
                env["PORT"] = str(urllib.parse.urlparse(url).port)
            self.proc = subprocess.Popen(args, shell=shell, cwd=side.cwd, env=env, stdout=self.log,
                                         stderr=subprocess.STDOUT, **kw)
        self.graceful: bool | None = None
        try:
            self._wait_up(cmd, url, wait)
        except BaseException:
            self.close()  # 어떤 이유로든 기다리다 빠져나가면 띄운 서버를 끈다 — 남은 서버가 포트를 쥐고 다음 검증을 막는다
            raise

    def _wait_up(self, cmd: str, url: str, wait: float) -> None:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ToolError("engine_error", f"서버가 바로 종료됨 (exit {self.proc.returncode}): {cmd}",
                                data={"output_tail": self.tail()})
            try:
                urllib.request.urlopen(url, timeout=2)  # noqa: S310
                up = True
            except urllib.error.HTTPError:
                up = True  # 404 등이어도 떠 있는 것
            except (urllib.error.URLError, OSError):
                up = False
            if up:
                time.sleep(0.3)
                if self.proc.poll() is not None:  # 응답은 왔는데 우리 서버는 끝났다 — 다른 프로그램이 대답한 것
                    raise ToolError("conflict", f"서버가 끝났는데 {url} 는 응답합니다 — 다른 프로그램이 그 포트를 쓰고 있습니다: {cmd}",
                                    data={"output_tail": self.tail()})
                return
            time.sleep(0.5)
        raise ToolError("timeout", f"{wait}초 안에 {url} 가 응답하지 않음: {cmd}", data={"output_tail": self.tail()})

    def alive(self) -> bool:
        return self.proc.poll() is None

    def restart(self) -> None:
        """케이스 하나에 서버가 죽었을 때 — 다시 띄워 나머지 케이스를 계속 돌린다 (메모리 상태는 초기화됨)."""
        self.close()
        self.restarts += 1
        end = time.monotonic() + 10
        while _port_in_use(self.url) and time.monotonic() < end:  # 끈 서버가 포트를 놓을 때까지
            time.sleep(0.3)
        self._start()

    def tail(self, n: int = 1500) -> str:
        self.log.seek(0)
        return self.log.read().decode("utf-8", "replace")[-n:]

    def close(self) -> None:
        cc = getattr(self.side, "casecov", None)
        if self.proc.poll() is None and cc is not None and not self.container:
            # 실행 범위를 재는 중 — 멈춤 파일로 끈다: 계측 장치가 기록을 쓰고 스스로 끝난다 (콘솔 신호가 닿지 않는 환경에서도)
            try:
                cc.stop_file.touch()
                self.proc.wait(timeout=8)
                self.graceful = True
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                cc.stop_file.unlink(missing_ok=True)
        if self.proc.poll() is None:
            try:
                if self.container:
                    subprocess.run(self.side.engine.argv("stop", "-t", "10", self.container), capture_output=True, timeout=40)
                elif os.name == "nt":
                    os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(self.proc.pid, signal.SIGINT)
                self.proc.wait(timeout=15)
                self.graceful = True
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self.graceful = False
        if self.proc.poll() is None:
            if self.container:
                subprocess.run(self.side.engine.argv("kill", self.container), capture_output=True)
            elif os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _direct_argv(cmd: str, env: dict | None = None) -> tuple[str | list[str], bool]:
    """셸 없이 바로 띄울 수 있으면 그렇게 — 종료 신호가 셸이 아니라 서버 프로세스에 바로 간다.
    실행 파일은 띄울 환경의 PATH 로 찾는다 — 툴킷 자신의 PATH 로 찾으면 준비한 .beom/venv 대신 PC 에 전역으로 깔린 python·flask 가
    잡힌다 (실험에서 전역 flask 로 떠서 새로 넣은 패키지를 못 찾고, 에이전트가 명령을 세 번 바꿨다)."""
    if re.search(r"[|&<>;^`$()]", cmd):
        return cmd, True
    try:
        parts = shlex.split(cmd, posix=os.name != "nt")
    except ValueError:
        return cmd, True
    if not parts:
        return cmd, True
    path = next((v for k, v in (env or {}).items() if k.upper() == "PATH"), None)
    exe = shutil.which(parts[0].strip('"'), path=path)
    if not exe or (os.name == "nt" and not exe.lower().endswith(".exe")):
        return cmd, True  # .cmd/.bat(npx 등)은 셸이 필요
    if os.name == "nt":
        return subprocess.list2cmdline([exe]) + cmd[len(parts[0]):], False
    return [exe, *parts[1:]], False


def case_commands(cases: list[dict]) -> dict[str, str]:
    """케이스 안에 들어 있는 실행 명령 — 함수 호출용 call.<쪽>.cmd 와 실행기 runtime.<쪽>. 검증 도구가 셸로 돌린다."""
    out: dict[str, str] = {}
    for i, c in enumerate(cases):
        name = c.get("name") or f"#{i + 1}"
        call = c.get("call") if isinstance(c.get("call"), dict) else {}
        for side, spec in call.items():
            if isinstance(spec, dict) and spec.get("cmd"):
                out[f"case {name} call.{side}.cmd"] = str(spec["cmd"])
        for side, rt in (c.get("runtime") if isinstance(c.get("runtime"), dict) else {}).items():
            if rt:
                out[f"case {name} runtime.{side}"] = str(rt)
        scr = c.get("screen")
        if isinstance(scr, dict) and scr.get("app"):  # 네이티브 앱 화면 케이스는 앱을 실행한다 — 같은 명령 규칙을 거친다
            from beom_workbench.core import native
            out[f"case {name} screen.app"] = native.command_text(scr)
    return out


def commands_in_cases(settings: Settings, proj_id: str, port_id: str, cases_file: str | None) -> dict[str, str]:
    """이번 캡처·검증에서 실행될 케이스 명령 — 넘긴 케이스 파일과 저장된 골든 모두."""
    cases: list[dict] = []
    if cases_file and Path(cases_file).is_file():
        try:
            cases += _load_cases(Path(cases_file))[1]
        except (ToolError, ValueError):
            pass
    golden = project_mod.workspace(settings, proj_id) / "ports" / port_id / "golden.json"
    if golden.is_file():
        try:
            cases += json.loads(golden.read_text(encoding="utf-8")).get("cases") or []
        except (OSError, ValueError):
            pass
    return case_commands(cases)


def _load_cases(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))  # Windows PowerShell 은 BOM 을 붙여 쓴다
    defaults = data.get("defaults", {}) if isinstance(data, dict) else {}
    cases = data["cases"] if isinstance(data, dict) else data
    if not cases:
        raise ToolError("invalid_request", "cases 가 비어 있음")
    # 케이스 값 도우미($repeat)는 읽을 때 한 번 펼친다 — 입력뿐 아니라 기대값·화면 동작 어디서나 같은 값이 되게
    # (예전에는 실행할 입력만 펼쳐서, 기대값에 쓴 $repeat 가 펼쳐지지 않은 채 비교되어 맞는 결과도 실패했다)
    out = [_expand({**defaults, **c}) if isinstance(c, dict) else c for c in cases]
    for i, c in enumerate(out):
        name = c.get("name") or f"#{i + 1}"
        if not isinstance(c, dict):
            raise ToolError("invalid_request", f"케이스 {name}: 객체여야 함")
        unknown = set(c) - CASE_KEYS
        if unknown:
            hints = [CASE_KEY_HINTS[k] for k in sorted(unknown) if k in CASE_KEY_HINTS]
            raise ToolError("invalid_request", f"케이스 {name}: 모르는 키 {sorted(unknown)} — 무시하면 다른 입력으로 실행된다"
                            + (f" ({'; '.join(hints)})" if hints else ""), recovery=f"쓸 수 있는 키: {sorted(CASE_KEYS)}")
        if isinstance(c.get("call"), str):  # "calc.js:total" 한 줄이면 대상(내 코드) 쪽 함수
            if not re.fullmatch(r".+\.\w+:[\w.$]+", c["call"]):
                raise ToolError("invalid_request", f"케이스 {name}: call 은 '파일:함수' 형식 (예: \"calc.js:calculateOrder\") — "
                                f"받은 값 {c['call']!r}")
            c["call"] = {"target": c["call"]}
        if "call" in c and not isinstance(c["call"], dict):
            raise ToolError("invalid_request", f"케이스 {name}: call 은 {{\"target\": \"파일:함수\"}} 또는 \"파일:함수\"")
        if "runtime" in c and not isinstance(c["runtime"], dict):
            raise ToolError("invalid_request", f"케이스 {name}: runtime 은 {{\"target\": \"python3\"}} 처럼 쪽별로")
        if "args" in c and not isinstance(c["args"], list) and not per_side(c["args"]):
            raise ToolError("invalid_request", f"케이스 {name}: args 는 목록 — 원본과 대상의 인자가 다르면 "
                            '{"source": [...], "target": [...]} 처럼 쪽별로')
        if ("args" in c or "kwargs" in c) and "call" not in c:
            raise ToolError("invalid_request", f"케이스 {name}: args·kwargs 는 함수 호출(call) 케이스용 — CLI 인자는 argv 로 적는다")
        if c.get("new") and not c.get("expect") and not c.get("invariant"):
            raise ToolError("invalid_request", f"케이스 {name}: new(원본에 없는 동작)는 expect(기대값)가 필요함")
        if "expect" in c and (not isinstance(c["expect"], dict) or not EXPECT_KEYS & set(c["expect"])):
            raise ToolError("invalid_request", f"케이스 {name}: expect 는 {sorted(EXPECT_KEYS)} 중 하나 이상을 가진 객체")
        # 함수 케이스의 status 는 'ok'(정상 반환)·'raised'(예외) — 숫자를 쓰면 승인된 뒤 검증에서 늘 어긋나 케이스를 다시 고치게 된다
        if "call" in c and isinstance(c.get("expect"), dict) and "status" in c["expect"] and c["expect"]["status"] not in ("ok", "raised"):
            raise ToolError("invalid_request", f"케이스 {name}: 함수 호출 케이스의 expect.status 는 \"ok\"(정상 반환) 또는 \"raised\"(예외) — "
                            f"받은 값 {c['expect']['status']!r}. 반환값은 body 로 비교하므로 정상 반환이면 status 는 빼도 된다")
    return defaults, out


# ---------------------------------------------------------------- 입력 탐색 (케이스 밖 입력)

def _leaf_paths(value, prefix: tuple = ()):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaf_paths(v, prefix + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value[:2]):
            yield from _leaf_paths(v, prefix + (i,))
    if prefix:
        yield prefix, value


def _variants(v, removable: bool) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if removable:
        out.append(("삭제", _REMOVE))
    out.append(("null", None))
    if isinstance(v, bool):
        out.append((str(not v).lower(), not v))
    elif isinstance(v, (int, float)):
        out += [("0", 0), ("음수", -v if v else -1), ("큰값", v * 1000 + 7), ("소수", v + 0.5),
                ("문자열", str(v)), ("소수문자열", f"{v}.5")]
    elif isinstance(v, str):
        if "{{" in v:
            return []
        out += [("빈문자열", ""), ("긴문자열", (v or "x") * 50), ("한글", "가나다"), ("숫자", 0)]
    elif isinstance(v, list):
        out += [("빈배열", []), ("중복", v + v)]
    elif isinstance(v, dict):
        out.append(("빈객체", {}))
    return out


_REMOVE = object()


def _set_path(root, path: tuple, value) -> None:
    cur = root
    for p in path[:-1]:
        cur = cur[p]
    if value is _REMOVE:
        if isinstance(cur, dict):
            cur.pop(path[-1], None)
        else:
            del cur[path[-1]]
    else:
        cur[path[-1]] = value


def explore(cases: list[dict], n: int, seed: int = 0) -> list[dict]:
    """입력 변형으로 케이스를 n 개까지 만든다 (결정적). 경계값·타입 변경·필드 삭제·빈 값.

    원래 케이스 뒤에 붙인다 — 앞 케이스 결과를 참조하는 케이스의 상태를 바꾸지 않게.
    """
    if n <= 0:
        return []
    gen, seen = [], set()
    for c in cases:
        if c.get("generated") or c.get("explore") is False or c.get("expect"):
            continue  # 기대값은 그 입력에 대한 것 — 입력을 바꾼 변형에는 맞지 않는다
        for key in ("json", "args", "kwargs", "argv"):
            if key not in c or per_side(c[key]):
                continue  # 쪽별 인자는 두 쪽을 같이 바꿀 방법이 없어 변형하지 않는다
            for path, v in _leaf_paths({key: c[key]}):
                if len(path) < 2 and key != "json":
                    continue  # args 자체를 없애지는 않는다
                removable = len(path) >= 2
                for label, nv in _variants(v, removable):
                    if key == "argv" and nv is not _REMOVE and not isinstance(nv, str):
                        continue
                    new = json.loads(json.dumps({k: x for k, x in c.items() if k != "name"}))
                    holder = {key: new[key]}
                    _set_path(holder, path, nv)
                    new[key] = holder[key]
                    sig = json.dumps(new, sort_keys=True, ensure_ascii=False)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    name = f"{c.get('name', 'case')}~{'.'.join(map(str, path))}={label}"
                    gen.append({**new, "name": name, "generated": True, "from": c.get("name")})
    if len(gen) > n:
        idx = sorted(random.Random(seed).sample(range(len(gen)), n))
        gen = [gen[i] for i in idx]
    return gen


# ---------------------------------------------------------------- 실행

def _one(side: _Side, case: dict, *, url: str | None, cli: str | None, timeout: float) -> dict:
    t0 = time.perf_counter()
    try:
        if "call" in case:
            res = _call(side, case, timeout)
        elif "argv" in case or "stdin" in case or (cli and not url):
            if not cli:
                raise ToolError("invalid_request", f"{side.name} 에 CLI 명령(*_cli)이 없음")
            res = _cli(side, cli, case, timeout)
        else:
            if not url:
                raise ToolError("invalid_request", f"{side.name} 에 url 이 없음")
            res = _http(url, case, timeout)
    except ToolError as e:
        res = {"status": None, "body": None, "error": e.message}
    except (OSError, subprocess.TimeoutExpired) as e:
        res = {"status": None, "body": None, "error": f"{type(e).__name__}: {e}"}
    res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res


def _run_side(cases: list[dict], side: _Side, *, url: str | None, cli: str | None, timeout: float,
              server: _Server | None = None) -> tuple[list[dict], dict]:
    saved: dict = {}
    out = []
    shots = _screens_for(cases, side, url)  # 화면 케이스는 브라우저 엔진 하나로 한꺼번에
    for i, c in enumerate(cases):
        name = c.get("name") or f"case{i + 1}"
        if "screen" in c:
            res = shots.get(i) or {"status": None, "body": None, "error": "화면 확인 결과 없음"}
            saved[name] = res["body"] if isinstance(res["body"], dict) else {"value": res["body"]}
            out.append({"name": name, **res})
            continue
        try:
            concrete = _template(c, saved)
        except ToolError as e:
            out.append({"name": name, "status": None, "body": None, "error": e.message})
            continue
        res = _one(side, concrete, url=url, cli=cli, timeout=timeout)
        dead = bool(server) and not server.alive()
        if server and not dead and res.get("error"):
            try:  # 연결이 끊긴 직후라면 프로세스가 막 죽는 중일 수 있다 — 이 케이스 탓으로 잡는다
                server.proc.wait(timeout=1.5)
                dead = True
            except subprocess.TimeoutExpired:
                pass
        if dead:
            code = server.proc.returncode
            res = {"status": None, "body": None, "ms": res.get("ms"), "crashed": True,
                   "error": f"{side.name} 서버가 이 케이스에서 종료됨 (exit {code}) — {server.tail(400).strip()[-300:]}"}
            try:
                server.restart()
            except ToolError as e:
                res["error"] += f" / 재기동 실패: {e.message}"
                out.append({"name": name, **res})
                for j, rest in enumerate(cases[i + 1:], i + 1):
                    out.append({"name": rest.get("name") or f"case{j + 1}", "status": None, "body": None,
                                "error": "서버 재기동 실패로 실행 못 함"})
                return out, saved
        saved[name] = res["body"] if isinstance(res["body"], dict) else {"value": res["body"]}
        out.append({"name": name, **res})
    return out, saved


def _screens_for(cases: list[dict], side: _Side, url: str | None) -> dict[int, dict]:
    from beom_workbench.core import screens

    idx = [i for i, c in enumerate(cases) if "screen" in c]
    if not idx:
        return {}
    if side.image:
        return {i: {"status": None, "body": None, "error": "화면 케이스는 컨테이너 안에서는 아직 못 돌림 — trusted 로컬 실행으로"} for i in idx}
    if str(side.cwd) in (".", "") or not Path(side.cwd).is_dir():  # 사본이 없으면 현재 폴더를 내보내지 않는다
        return {i: {"status": None, "body": None, "error": "화면 케이스를 돌릴 폴더(대상 또는 원본 사본)가 없음"} for i in idx}
    folder = getattr(side, "screens", None) or Path(tempfile.mkdtemp(prefix="screens-"))
    folder.mkdir(parents=True, exist_ok=True)
    out: dict[int, dict] = {}
    from beom_workbench.core import native

    for i in [i for i in idx if native.is_native(cases[i]["screen"])]:  # 네이티브 앱 — 하나씩 띄워 확인
        c = cases[i]
        e = c.get("expect") or {}
        name = re.sub(r"[^\w-]+", "_", c.get("name") or f"case{i + 1}")
        try:
            r = native.capture(c["screen"], Path(side.cwd), str(folder / f"{name}.png"),
                               visible=e.get("visible") or [], selectors=e.get("selectors") or [])
            out[i] = native.to_result(r)
        except ToolError as err:
            out[i] = {"status": None, "body": None, "error": err.message}
    idx = [i for i in idx if i not in out]
    if not idx:
        return out
    with screens.serve_folder(side.cwd) as served:
        pages = []
        for i in idx:
            c = cases[i]
            name = re.sub(r"[^\w-]+", "_", c.get("name") or f"case{i + 1}")
            try:
                scr = _expand(c["screen"])  # 화면 동작의 입력값도 ($repeat)
                spec = screens.page_spec(scr if isinstance(scr, dict) else {"path": str(scr)}, url,
                                         side.cwd, str(folder / f"{name}.png"), file_base=served.base)
            except ToolError as e:
                out[i] = {"status": None, "body": None, "error": e.message}
                continue
            e = c.get("expect") or {}
            pages.append((i, {**spec, "visible": e.get("visible") or [], "selectors": e.get("selectors") or []}))
        try:
            cc = getattr(side, "casecov", None)
            got = screens.capture([p for _, p in pages], coverage_dir=(cc.dir / "browser") if cc is not None else None)
        except ToolError as e:
            return {**out, **{i: {"status": None, "body": None, "error": e.message} for i, _ in pages}}
    for (i, _), r in zip(pages, got):
        out[i] = screens.to_result(r)
    return out


def _needs_exec(cases: list[dict], cmd: str | None, cli: str | None) -> bool:
    # 화면 케이스도 원본 코드를 브라우저로 실행한다 — 격리된 사본(또는 trusted)이 있어야 한다
    return bool(cmd or cli or any("call" in c or "screen" in c for c in cases))


def _source_side(settings: Settings, proj: dict, cases: list[dict], *, cmd: str | None, cli: str | None,
                 trusted: bool, image: str | None):
    """원본 실행 위치: 분석 전용 clone 은 쓰지 않고 임시 사본에서 (실행 부산물이 clone 을 더럽히지 않게).
    image 가 있으면 Docker 컨테이너 안에서 (원본 코드 격리 — trusted 불필요)."""
    if not _needs_exec(cases, cmd, cli):
        return None, None
    if image:
        if not runtime.docker_available():
            raise ToolError("capability_unavailable", "Docker 데몬이 없어 원본을 격리 실행할 수 없음",
                            recovery="Docker 를 켠 뒤 재시도, 또는 신뢰하는 저장소면 image 없이 trusted=True")
    elif not trusted:
        raise ToolError("capability_unavailable", "원본 코드를 로컬에서 실행하려면 trusted=True 가 필요함",
                        recovery="신뢰하지 않는 저장소면 source_image 로 Docker 안에서 실행, 또는 원본을 직접 띄우고 source_url 만 넘김")
    (settings.home / "work").mkdir(parents=True, exist_ok=True)  # 임시 사본 — 실행 기록(runs/)과 섞지 않는다
    work = Path(tempfile.mkdtemp(prefix="port-", dir=settings.home / "work"))
    copy = work / "src"
    shutil.copytree(proj["repo_dir"], copy, ignore=shutil.ignore_patterns(".git"))
    return work, copy


def _port_dir(settings: Settings, proj: dict, port_id: str) -> Path:
    d = project_mod.workspace(settings, proj["id"]) / "ports" / port_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_source(conn, proj, settings, port, cases, *, url, cmd, cli, trusted, image, timeout,
                coverage_cmd=None, coverage_report=None) -> dict:
    """원본 실행. new(원본에 없는 동작) 케이스는 돌리지 않고 자리만 둔다."""
    run_cases = [c for c in cases if not c.get("new")]
    if not run_cases:
        return {"results": [{"name": c.get("name") or f"case{i + 1}", "status": None, "body": None, "new": True}
                            for i, c in enumerate(cases)], "adequacy": None, "graceful_stop": None}
    got = _run_source_cases(conn, proj, settings, port, run_cases, url=url, cmd=cmd, cli=cli, trusted=trusted,
                            image=image, timeout=timeout, coverage_cmd=coverage_cmd, coverage_report=coverage_report)
    it = iter(got["results"])
    got["results"] = [{"name": c.get("name") or f"case{i + 1}", "status": None, "body": None, "new": True}
                      if c.get("new") else next(it) for i, c in enumerate(cases)]
    return got


def _run_source_cases(conn, proj, settings, port, cases, *, url, cmd, cli, trusted, image, timeout,
                      coverage_cmd=None, coverage_report=None) -> dict:
    work, copy = _source_side(settings, proj, cases, cmd=cmd, cli=cli, trusted=trusted, image=image)
    side = _Side("source", copy or Path("."), _harness_dir(settings), image)
    side.screens = _port_dir(settings, proj, port["id"]) / "screens" / "source"
    server, adequacy = None, None
    try:
        if cmd:
            server = _Server(side, cmd, url)
        results, _ = _run_side(cases, side, url=url, cli=cli, timeout=timeout, server=server)
        if server:
            server.close()
        if coverage_cmd:
            if copy is None:
                raise ToolError("invalid_request", "coverage_cmd 는 원본을 직접 실행할 때만 (source_cmd/cli/call)")
            side.shell(coverage_cmd, timeout=300)
        if coverage_report:
            if copy is None:
                raise ToolError("invalid_request", "coverage_report 는 원본을 직접 실행할 때만")
            rp = copy / coverage_report
            if not rp.exists():
                hint = "" if not server or server.graceful else " — 서버가 정상 종료되지 않아 커버리지가 기록되지 않았을 수 있음"
                adequacy = {"error": f"커버리지 리포트 {coverage_report} 가 생기지 않음{hint}",
                            "server_tail": server.tail(600) if server else None}
            else:
                kept = _port_dir(settings, proj, port["id"]) / f"source-coverage{rp.suffix or '.txt'}"
                shutil.copy2(rp, kept)
                adequacy = _adequacy(conn, proj, port, kept, roots=[str(copy), "/work"], command=cmd or cli)
    finally:
        if server:
            server.close()
        if work:
            shutil.rmtree(work, ignore_errors=True)
    return {"results": results, "adequacy": adequacy, "graceful_stop": server.graceful if server else None}


# ---------------------------------------------------------------- 케이스 충분성 (원본 커버리지)

def _adequacy(conn, proj: dict, port: dict, report: Path, *, roots: list[str], command: str | None) -> dict:
    """원본을 케이스로 돌린 커버리지에서, 슬라이스 함수가 모두 실행됐는지와 안 거친 줄을 낸다.
    같은 리포트를 coverage 원장에도 적재 → 실행된 심볼에 L3 근거."""
    fmt, data = runtime.parse(report.read_text(encoding="utf-8-sig", errors="replace"))
    files = {r["path"] for r in conn.execute("SELECT path FROM file").fetchall()}
    mapping, _ = runtime._match_paths(list(data), files, roots + [proj["repo_dir"]])
    by_repo = {mapping[k]: data[k] for k in mapping}
    omitted = {r["item_id"] for r in conn.execute("SELECT item_id FROM port_map WHERE port_id = ? AND status = 'omitted'",
                                                    (port["id"],)).fetchall()}
    rows = []
    for s in port["slice"]["symbols"]:
        if s["label"] in CLASSLIKE or s["symbol_id"] in omitted:
            continue
        d = by_repo.get(s["path"])
        row = {"symbol_id": s["symbol_id"], "name": s["name"], "path": s["path"], "start_line": s["start_line"]}
        if d is None:
            rows.append({**row, "executed": None, "reason": "리포트에 이 파일이 없음 (측정 범위 밖)"})
            continue
        body = [ln for ln in range(s["start_line"] + 1, s["end_line"] + 1) if ln in d["lines"]]
        if not body and _declaration_only(proj, s):
            continue  # 추상 메서드·선언만 있는 한 줄 (실행할 몸체가 없음) — 구현 쪽이 따로 슬라이스에 있다
        fn = next((n for name, ln, n in d["functions"]
                   if name.split(".")[-1].split("::")[-1] == s["name"] and abs(ln - s["start_line"]) <= 3), None)
        executed = fn > 0 if fn is not None else (any(d["lines"][ln] > 0 for ln in body) if body else None)
        untested = [ln for ln in body if d["lines"][ln] == 0]
        rows.append({**row, "executed": executed, "untested_lines": untested,
                     **({} if executed is not None else {"reason": "측정 줄 없음"})})
    ingested = runtime.ingest(conn, proj, report, roots=roots, command=command)
    not_run = [r for r in rows if r["executed"] is not True]
    untested_total = sum(len(r.get("untested_lines") or []) for r in rows)
    detail = {"format": fmt, "report": str(report), "coverage_run": ingested["run_id"], "symbols": rows,
              "untested_lines": untested_total}
    with ledger.tx(conn):
        cid = "chk_" + uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO port_check (id, port_id, kind, cases, passed, failed, detail, evidence_id, created_at) "
                     "VALUES (?, ?, 'adequacy', ?, ?, ?, ?, NULL, ?)",
                     (cid, port["id"], len(rows), len(rows) - len(not_run), len(not_run), ledger.dumps(detail), _now()))
    return {"check_id": cid, "symbols": len(rows), "executed": len(rows) - len(not_run),
            "not_executed": [{k: r[k] for k in ("name", "path", "start_line")} | ({"reason": r["reason"]} if r.get("reason") else {})
                             for r in not_run],
            "untested_lines": {f"{r['path']}:{r['name']}": r["untested_lines"] for r in rows if r.get("untested_lines")},
            "coverage_run": ingested["run_id"],
            "note": "untested_lines 는 케이스가 한 번도 거치지 않은 줄 — 그 분기를 거치는 케이스를 추가하면 검증 범위가 넓어진다"}


def _declaration_only(proj: dict, s: dict) -> bool:
    lines = evidence.read_lines(proj, s["path"])
    text = " ".join(lines[max(0, s["start_line"] - 3):s["end_line"]])
    return bool(re.search(r"abstract|:\s*(\.\.\.|pass)\s*$|;\s*$", text.strip()))


def capture(conn, proj: dict, settings: Settings, port_id: str, cases_file: str, *, source_url: str | None = None,
            source_cmd: str | None = None, source_cli: str | None = None, trusted: bool = False, timeout: float = 30,
            source_image: str | None = None, explore_n: int = 0, seed: int = 0,
            coverage_cmd: str | None = None, coverage_report: str | None = None) -> dict:
    """원본에 케이스를 돌려 골든 파일로 저장 — 이후 원본 없이 대상만 검증하거나, 대상 언어의 테스트로 변환.
    explore_n: 입력 변형 케이스를 추가 생성. coverage_report: 원본 커버리지로 케이스 충분성 확인."""
    source_cmd, source_url = fill_port(source_cmd, source_url)
    port = _port(conn, port_id)
    defaults, cases = _load_cases(Path(cases_file))
    request_mode = make_mod.is_request(port)
    if request_mode:
        cases = make_mod.prepare_cases(cases)
        base = [{k: v for k, v in c.items() if k != "expect"} for c in cases]
        generated = make_mod.invariant_variants(cases, explore(base, explore_n, seed))
    else:
        no_src = [c.get("name") or f"#{i + 1}" for i, c in enumerate(cases)
                  if isinstance(c.get("call"), dict) and "source" not in c["call"] and not c.get("new")]
        if no_src:
            raise ToolError("invalid_request", f"케이스 {len(no_src)}개의 call 에 원본(source) 쪽이 없음: {', '.join(no_src[:8])} — "
                            "'파일:함수' 한 줄은 대상(내 코드) 쪽 함수로 읽는다",
                            recovery='원본과 대상을 함께 적는다: "call": {"source": "원본파일:함수", "target": "대상파일:함수"} '
                                     '(예: {"source": "textstat/__init__.py:count_words", "target": "src/index.js:countWords"}). '
                                     '대상을 아직 만들지 않았어도 만들 파일·함수 이름을 적는다. 케이스 파일은 Write 로 통째로 다시 쓴다')
        generated = explore(cases, explore_n, seed)
    cases = cases + generated
    run = _run_source(conn, proj, settings, port, cases, url=source_url, cmd=source_cmd, cli=source_cli, trusted=trusted,
                      image=source_image, timeout=timeout, coverage_cmd=coverage_cmd, coverage_report=coverage_report)
    results = run["results"]
    golden = _port_dir(settings, proj, port_id) / "golden.json"
    golden.write_text(json.dumps({"commit": proj["commit_sha"], "defaults": defaults, "cases": cases, "results": results,
                                  "isolation": f"docker:{source_image}" if source_image else ("local" if source_cmd or source_cli else "external")},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    errors = [r for r in results if r.get("error")]
    rules = guard.case_rules(cases)
    prev = _last_check(conn, port_id, "capture")
    if prev is None:
        baseline = rules
    else:
        pd = prev["detail"]
        prev_ok = not pd.get("change") or guard.is_approved(conn, port_id, f"cases:{prev['id']}", guard.fingerprint(pd["change"]))
        baseline = pd["rules"] if prev_ok else pd.get("baseline", pd["rules"])
    change = guard.case_change(baseline, rules)
    adq_id = run["adequacy"].get("check_id") if isinstance(run["adequacy"], dict) else None
    cid = "cap_" + uuid.uuid4().hex[:12]
    expectations = {c.get("name") or f"#{i}": {"expect": c["expect"], "new": bool(c.get("new")), "order": i,
                                                 "description": c.get("description") or c.get("note"),
                                                 "input": {k: c[k] for k in CASE_INPUT_KEYS if k in c},
                                                 "source": None if r.get("new") else
                                                 {"status": r.get("status"), "body": json.dumps(r.get("body"), ensure_ascii=False)[:300]}}
                    for i, (c, r) in enumerate(zip(cases, results)) if c.get("expect")}
    detail = {"golden": str(golden), "golden_sha": hashlib.sha256(golden.read_bytes()).hexdigest(), "rules": rules,
              "baseline": baseline, "change": change, "adequacy_check": adq_id, "expectations": expectations}
    if request_mode:
        detail["req_cover"] = make_mod.coverage_of(cases, [r["id"] for r in make_mod.requirements(conn, port_id)])
    with ledger.tx(conn):
        conn.execute("INSERT INTO port_check (id, port_id, kind, cases, passed, failed, detail, evidence_id, created_at) "
                     "VALUES (?, ?, 'capture', ?, ?, ?, ?, NULL, ?)",
                     (cid, port_id, len(results), len(results) - len(errors), len(errors), ledger.dumps(detail), _now()))
    guard.record(conn, port_id, "capture", {"id": cid, "cases": len(results)}, checks=[cid, adq_id])
    out = {"capture_id": cid, "golden": str(golden), "cases": len(results), "generated": len(generated), "errors": errors,
           "note": "골든은 원본의 실제 응답 — 대상 언어 테스트의 기대값으로 그대로 쓸 수 있다"}
    if expectations:
        out["expectations"] = {"cases": sorted(expectations),
                               "note": "기대값 케이스는 원본 대신 기대값과 비교 — 기대값 자체는 사용자 승인(port-approve) 대상"}
    if change:
        out["case_change"] = {**change, "note": "이전 capture 대비 케이스가 빠지거나 바뀜 — 사용자 승인(port-approve) 전까지 게이트 미충족"}
    if generated:
        out["generated_sample"] = [{"name": g["name"], "status": r["status"]} for g, r in zip(generated, results[-len(generated):])][:15]
    if run["adequacy"] is not None:
        out["adequacy"] = run["adequacy"]
    if run["graceful_stop"] is False:
        out["warning"] = "원본 서버가 정상 종료 신호에 응답하지 않아 강제 종료함"
    return out


def _latency(rows: list[dict]) -> dict | None:
    ms = sorted(r["ms"] for r in rows if r.get("ms") is not None and not r.get("error"))
    if not ms:
        return None
    return {"p50": ms[len(ms) // 2], "p95": ms[min(len(ms) - 1, int(len(ms) * 0.95))], "max": ms[-1]}


def _concurrent(cases, src, side: _Side, saved: dict, *, url, cli, n: int, timeout: float,
                allowed: list[tuple[str, str]] = ()) -> dict:
    """같은 케이스를 대상에 n 개 동시에 보내 각각을 원본 결과와 비교 — 경쟁 조건·공유 상태 문제를 잡는다."""
    mismatches, runs = [], 0
    jobs = [(c, s) for c, s in zip(cases, src) if not c.get("generated") and c.get("concurrent", True) and not s.get("error")]
    with ThreadPoolExecutor(max_workers=n) as ex:
        for c, s in jobs:
            try:
                concrete = _template(c, saved)
            except ToolError:
                continue
            for r in ex.map(lambda _: _one(side, concrete, url=url, cli=cli, timeout=timeout), range(n)):
                runs += 1
                diffs = _case_diffs(s, r, c)
                if diffs and not r.get("error") and any(fnmatch.fnmatchcase(s["name"], pat) for pat, _ in allowed):
                    continue  # 사유와 함께 허용한 의도적 차이는 동시 실행에서도 같은 기준으로 본다
                if diffs:
                    mismatches.append({"name": s["name"], "diffs": diffs[:2]})
    return {"per_case": n, "runs": runs, "mismatches": len(mismatches), "examples": mismatches[:10]}


def verify(conn, proj: dict, settings: Settings, port_id: str, *, target_url: str | None = None, target_cmd: str | None = None,
           target_cli: str | None = None, cases_file: str | None = None, source_url: str | None = None,
           source_cmd: str | None = None, source_cli: str | None = None, trusted: bool = False, timeout: float = 30,
           source_image: str | None = None, explore_n: int = 0, seed: int = 0, concurrency: int = 0,
           target_coverage_cmd: str | None = None, target_coverage_report: str | None = None) -> dict:
    """대상(내 코드)을 원본과 비교. 원본 쪽은 골든(기본) 또는 실시간(source_*).
    concurrency>1 이면 케이스마다 대상에 동시 요청을 보내 결과가 흔들리지 않는지도 본다.
    target_coverage_report(+cmd): 대상을 커버리지로 돌린 리포트 — ported 대상이 케이스로 모두 실행됐는지(원본 계측이 안 될 때)."""
    target_cmd, target_url = fill_port(target_cmd, target_url)
    port = _port(conn, port_id)
    root = Path(port["target_root"])
    golden_file = project_mod.workspace(settings, proj["id"]) / "ports" / port_id / "golden.json"
    if source_url or source_cmd or source_cli or (cases_file and not golden_file.exists()):
        if not cases_file:
            raise ToolError("invalid_request", "실시간 비교에는 cases_file 이 필요함")
        _, cases = _load_cases(Path(cases_file))
        cases = cases + explore(cases, explore_n, seed)
        src = _run_source(conn, proj, settings, port, cases, url=source_url, cmd=source_cmd, cli=source_cli,
                          trusted=trusted, image=source_image, timeout=timeout)["results"]
        source_kind = "live"
    else:
        if not golden_file.exists():
            raise ToolError("not_found", "골든 파일이 없음", recovery="port-capture 로 원본 응답을 먼저 저장하거나 source_* 로 실시간 비교")
        g = json.loads(golden_file.read_text(encoding="utf-8"))
        if g["commit"] != proj["commit_sha"]:
            raise ToolError("invalid_request", "골든이 다른 커밋에서 만들어짐 — 다시 capture")
        cap = _last_check(conn, port_id, "capture")
        if cap and cap["detail"].get("golden_sha") != hashlib.sha256(golden_file.read_bytes()).hexdigest():
            raise ToolError("tampered", "골든 파일이 capture 뒤에 바뀜 — 툴킷 밖 수정",
                            recovery="사용자에게 알리고 port-capture 로 다시 저장")
        cases, src = g["cases"], g["results"]
        source_kind = "golden"
    static = None
    if not (target_url or target_cli or target_cmd) and any("screen" in c or "path" in c for c in cases) and root.is_dir():
        # 서버 명령 없이 화면·주소 케이스만 — 정적 페이지(HTML·JS·CSS)다. 툴킷이 대상 폴더만 내보낸다 (화면 찍기와 같은 서버).
        # 에이전트가 python -m http.server 같은 명령을 넘기면 프로젝트 밖 명령이라 권한 카드가 뜬다
        from beom_workbench.core import screens
        static = screens.serve_folder(root)
        static.__enter__()
        target_url = static.base
    if not (target_url or target_cli or any("call" in c for c in cases)):
        raise ToolError("invalid_request", "target_url(HTTP) 또는 target_cli(명령) 이 필요함")
    cli_cases = [c.get("name") or f"#{i + 1}" for i, c in enumerate(cases) if "call" not in c and ("argv" in c or "stdin" in c)]
    if cli_cases and not target_cli:
        # 돌리기 전에 — 예전에는 모든 케이스를 돌린 뒤 명령줄 케이스마다 'CLI 명령이 없음' 으로 실패했고, 에이전트가 같은 검증을 되풀이했다
        raise ToolError("invalid_request", f"명령줄 케이스(argv·stdin) {len(cli_cases)}개에 쓸 target_cli 가 없음: {', '.join(cli_cases[:5])}",
                        recovery="target_cli 에 그 프로그램을 실행하는 명령을 함께 넘긴다 (서버와 함께면 target_cmd·target_url 도)")
    side = _Side("target", root, _harness_dir(settings))
    side.screens = _port_dir(settings, proj, port_id) / "screens" / "target"
    if not target_coverage_cmd and not target_coverage_report and not side.image and root.is_dir():
        side.casecov = _CaseCoverage(root, _mapped_files(conn, port_id, root), side.harness)
    server, conc = None, None
    reports, cov_state = [], None
    before = _files(root)
    try:
        if target_cmd:
            server = _Server(side, target_cmd, target_url)
        dst, saved = _run_side(cases, side, url=target_url, cli=target_cli, timeout=timeout, server=server)
        if concurrency and concurrency > 1:
            conc = _concurrent(cases, src, side, saved, url=target_url, cli=target_cli, n=concurrency, timeout=timeout,
                               allowed=_accepted(conn, port_id))
        if server:
            server.close()  # 커버리지 도구는 정상 종료 때 기록을 쓴다
        if side.casecov is not None:
            found, cov_state = side.casecov.reports()
            reports = [str(f.relative_to(root)) for f in found]
        elif target_coverage_cmd or target_coverage_report:
            # 사용자가(에이전트가) 측정 명령·리포트를 직접 준 경우 — 그대로
            if target_coverage_cmd:
                side.shell(target_coverage_cmd, timeout=600)
            if target_coverage_report:
                reports.append(target_coverage_report)
        # 이번 작업에서 새로 만든 테스트만 — 원래 있던 테스트는 예전 동작을 기대한다 (기록이 없는 예전 원장은 전체)
        base = guard.baseline(conn, port_id)
        tests_only = None
        if base is not None and root.is_dir():
            from beom_workbench.core import layout
            pre = {str((root / rel).resolve()) for rel in base.get("files") or {}}
            tests_only = layout.test_files(root) - pre
        project = tasks_mod().project_checks(settings, _auto_run(), root, tests_only=tests_only) if root.is_dir() else []
    finally:
        if server:
            server.close()
        if static:
            static.__exit__(None, None, None)
        # 케이스를 돌리며 프로그램이 새로 만든 파일(데이터 파일 등)은 치운다 — 결과물에 검증 찌꺼기가 남지 않게
        cleaned = _drop_new(root, before, keep=set(reports))
    fp = guard.target_fingerprint(root, fresh=True, extra=_outside_refs(conn, port_id, root))
    tgt_adq = None
    impact_res = None
    if cov_state is not None and side.casecov is not None and (side.casecov.py or side.casecov.js or side.casecov.browser):
        from beom_workbench.core import layout
        cov_state["forced_stop"] = bool(server and server.graceful is False)
        browser = layout.browser_files(root)
        tgt_adq = _target_adequacy(conn, port, [root / r for r in reports], fp, browser=browser, state=cov_state)
        try:
            impact_res = _impact(conn, settings, proj, port_id, root, [root / r for r in reports], browser, cov_state, fp)
        except Exception as e:  # noqa: BLE001 — 분석 도구가 실패해도 검증 결과는 낸다 (분석 못 함으로 알린다)
            impact_res = {"error": f"연관된 곳 분석 실패: {type(e).__name__}: {e}"[:300]}
    if side.casecov is not None:
        side.casecov.cleanup()
    elif reports:
        have = [root / r for r in reports if (root / r).exists()]
        if not have:
            hint = "" if not server or server.graceful else " — 서버가 정상 종료되지 않아 커버리지가 기록되지 않았을 수 있음"
            tgt_adq = {"error": f"대상 커버리지 리포트 {', '.join(reports)} 가 생기지 않음{hint}"}
        else:
            tgt_adq = _target_adequacy(conn, port, have if len(have) > 1 else have[0], fp)

    allowed = _accepted(conn, port_id)
    rows, passed, accepted = [], 0, []
    for c, s, d in zip(cases, src, dst):
        diffs = _case_diffs(s, d, c)
        # 기대값 케이스는 '차이 허용'으로 넘기지 않는다 — 기대값과 달라도 원본과 같아도, 기대값이 기준
        why = None if c.get("expect") else next(
            (reason for pat, reason in allowed if diffs and fnmatch.fnmatchcase(s["name"], pat)), None)
        if why and not s.get("error"):
            accepted.append({"name": s["name"], "reason": why, "diffs": diffs[:1], "diffs_full": diffs})
            diffs = []
        excused = [a["diffs_full"] for a in accepted if a["name"] == s["name"]] if why and not diffs else []
        rows.append({"name": s["name"], "ok": not diffs, "diffs": diffs, **({"generated": True} if c.get("generated") else {}),
                     **({"screenshot": d["screenshot"]} if d.get("screenshot") else {}),
                     "actual": {"status": d.get("status"), "body": json.dumps(d.get("body"), ensure_ascii=False)[:300] if d.get("body") is not None else None,
                                **({"error": str(d["error"])[:300]} if d.get("error") else {})},
                     **({"expected": "new" if c.get("new") else "changed"} if c.get("expect") else {}),
                     **({"accepted": why, "accepted_diffs": excused[0]} if why and not diffs else {})})
        passed += not diffs
    failed = len(rows) - passed + (conc["mismatches"] if conc else 0)
    generated = sum(1 for c in cases if c.get("generated"))
    adequacy = _last_check(conn, port_id, "adequacy")
    limitations = [f"케이스 {len(rows)}개(자동 생성 {generated}개)의 입력에 대해서만 같은 결과"]
    if adequacy is None:
        limitations.append("원본 커버리지 미측정 — 케이스가 슬라이스 코드를 다 거쳤는지 모름")
    else:
        if adequacy["failed"]:
            limitations.append(f"케이스가 실행하지 못한 슬라이스 함수 {adequacy['failed']}개")
        if adequacy["detail"].get("untested_lines"):
            limitations.append(f"케이스가 거치지 않은 원본 줄 {adequacy['detail']['untested_lines']}개")
    unapproved = []
    for pat, reason in allowed:
        hit = guard.divergence_cases(rows, pat)
        if hit and not guard.is_approved(conn, port_id, f"diff:{pat}", guard.divergence_fp(pat, reason, hit)):
            unapproved.append(pat)
    n_expect = sum(1 for c in cases if c.get("expect"))
    if n_expect:
        limitations.append(f"기대값 케이스 {n_expect}개는 원본이 아니라 사용자가 승인한 기대값과 비교")
    if accepted:
        limitations.append(f"의도적 차이로 허용한 케이스 {len(accepted)}개 (사유는 포팅 원장 divergence 항목)")
    if unapproved:
        limitations.append(f"사용자 승인 전인 허용 차이 {len(unapproved)}개 — 승인 후 다시 verify 해야 근거 발급")
    hollow = hollow_calls(cases, root)
    if hollow:
        limitations.append(f"프로그램이 쓰지 않고 값만 돌려주는 함수를 부르는 케이스 {len(hollow)}개 — 실제 동작을 확인하지 않음")
    limitations.append(f"동시 요청 {concurrency}개씩 검사" if conc else "동시 실행 미검사")
    limitations.append("성능은 비교하지 않음 (지연 시간은 참고값)")
    ev_id = None
    entry = next((e for e in port["slice"].get("entries", []) if e.get("handler")), None)
    sym = conn.execute("SELECT * FROM symbol WHERE id = ?", (entry["handler"],)).fetchone() if entry else None
    if sym is None and port["slice"]["symbols"]:
        sym = conn.execute("SELECT * FROM symbol WHERE id = ?", (port["slice"]["symbols"][0]["symbol_id"],)).fetchone()
    with ledger.tx(conn):
        if sym is not None and failed == 0 and passed and not unapproved:
            ev = evidence.record(conn, proj, provider="differential", operation="port_equivalence",
                                 parameters={"port_id": port_id, "target_language": port["target_language"],
                                             "cases": len(rows), "generated": generated, "source": source_kind,
                                             "concurrency": concurrency or 0},
                                 result={"passed": passed, "failed": failed},
                                 locations=[{"path": sym["path"], "start_line": sym["start_line"], "end_line": sym["end_line"]}],
                                 confidence="observed", limitations=limitations)
            ev_id = ev["evidence_id"]
        cid = "chk_" + uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO port_check (id, port_id, kind, cases, passed, failed, detail, evidence_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (cid, port_id, f"differential:{source_kind}", len(rows), passed, failed,
                      ledger.dumps({"rows": rows, "concurrency": conc, "rules": guard.case_rules(cases), "target_fp": fp}),
                      ev_id, _now()))
        pcid = None
        if project:
            pcid = "chk_" + uuid.uuid4().hex[:12]
            slim = [{k: v for k, v in r.items() if k != "output_tail"} | ({"output_tail": r["output_tail"][-1500:]}
                    if not r.get("ok") and r.get("output_tail") else {}) for r in project]
            conn.execute("INSERT INTO port_check (id, port_id, kind, cases, passed, failed, detail, evidence_id, created_at) "
                         "VALUES (?, ?, 'project', ?, ?, ?, ?, NULL, ?)",
                         (pcid, port_id, len(slim), sum(1 for r in slim if r.get("ok")),
                          sum(1 for r in slim if not r.get("ok") and not r.get("skipped")),
                          ledger.dumps({"results": slim, "target_fp": fp}), _now()))
    guard.record(conn, port_id, "verify", {"id": cid, "passed": passed, "failed": failed},
                 checks=[cid, tgt_adq.get("check_id") if isinstance(tgt_adq, dict) else None, pcid])
    fails = [r for r in rows if not r["ok"]]
    crashes = [d["name"] for d in dst if d.get("crashed")]
    return {"check_id": cid, "source": source_kind, "cases": len(rows), "generated": generated, "passed": passed,
            "failed": failed, "evidence_id": ev_id, "target_crashes": crashes, "accepted": accepted[:30],
            "failures": fails[:30],
            "failures_by_origin": _group_failures(fails), "concurrency": conc,
            "latency_ms": {"source": _latency(src), "target": _latency(dst),
                           "note": "참고값 — 골든 기준이면 원본 시간은 capture 시점"},
            "accepted_unapproved": unapproved, "target_adequacy": tgt_adq, **({"impact": impact_res} if impact_res else {}),
            "project_checks": [{k: v for k, v in r.items() if k != "output_tail" or not r.get("ok")} for r in project],
            **({"cleaned": cleaned[:20], "cleaned_note": "케이스를 돌리는 동안 프로그램이 새로 만든 파일이라 지웠다 — 직접 지울 필요 없다"}
               if cleaned else {}),
            **({"hollow_calls": hollow, "hollow_note": "이 케이스들은 프로그램이 쓰지 않는 함수가 적힌 값을 그대로 돌려주는지만 본다 — "
                "프로그램이 실제로 쓰는 함수를 부르거나, 화면 배치처럼 잴 수 없는 것은 .beom/helpers 에서 실제 창·위젯을 조사하게 바꾼다"}
               if hollow else {}),
            "limitations": limitations, **{k: v for k, v in status_(conn, port_id, brief=True).items()
                                           if k in ("gates", "next_step", "approvals")}}


TEST_FILE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/|\.(?:test|spec)\.[cm]?[jt]sx?$|(?:^|/)test_[^/]*\.py$|_test\.(?:py|go)$")
# 실행되는 코드가 아닌 화면·스타일 파일 — 실행 범위로 잴 것이 아니라 화면 케이스로 확인한다
STYLE_FILE = re.compile(r"\.(?:css|scss|sass|less|html?|svg)$", re.I)
# 측정 도구가 읽지 못할 수 있는 화면 코드 — 리포트에 없으면 '측정 못 함' 으로 (실패가 아니라 사용자 확인 카드로 넘긴다)
VIEW_CODE = re.compile(r"\.(?:jsx|tsx|vue|svelte)$", re.I)
# 설정·문서 파일 — 'npm test 로 돈다'·'README 에 사용법' 같은 요구사항의 위치. 실행되는 코드가 아니라 프로젝트 검사(테스트·타입
# 검사)와 인수인계로 확인한다. 실패로 두면 에이전트가 미확인 항목을 올리고 매핑을 '대체' 로 돌려 적었다
CONFIG_DOC = re.compile(r"(?:\.(?:json|jsonc|toml|ya?ml|ini|cfg|lock|md|markdown|txt|rst)|(?:^|/)(?:Dockerfile|Makefile|\.gitignore))$", re.I)


@functools.lru_cache(maxsize=64)
def _py_tree(path: str, stamp: tuple):
    import ast
    try:
        return ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None


def _py_fn_ran(d: dict, path: Path, line: int) -> int | None:
    """파이썬 — 그 줄의 함수(def·장식자 줄이면 그 함수, 몸통 안이면 품은 가장 안쪽 함수)가 돌았나: 몸통 줄 중 실행된 줄의 최대
    횟수 (안쪽 함수의 몸통은 빼고 — 안쪽 def 줄만 바깥 함수가 돌 때 실행된다). 함수가 아니면 None.
    측정 도구의 함수 정보(start_line)는 버전에 따라 없다 (7.6·7.10 에는 없음) — 그러면 'def' 줄은 모듈을 불러오기만 해도
    실행된 줄이라, 부르지 않은 함수도 실행된 것으로 보였다."""
    import ast
    try:
        st = Path(path).stat()
    except OSError:
        return None
    tree = _py_tree(str(path), (st.st_mtime_ns, st.st_size))
    if tree is None:
        return None
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    head = [n for n in fns if line == n.lineno or any(line == dec.lineno for dec in n.decorator_list)]
    inside = [n for n in fns if n.lineno <= line <= (n.end_lineno or n.lineno)]
    f = head[0] if head else (max(inside, key=lambda n: n.lineno) if inside else None)
    if f is None or not f.body:
        return None
    start, end = f.body[0].lineno, f.end_lineno or f.body[-1].lineno
    skip: set[int] = set()
    for n in ast.walk(f):
        if n is not f and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            skip.update(range(n.lineno + 1, (n.end_lineno or n.lineno) + 1))
    counts = [d["lines"][x] for x in range(start, end + 1) if x in d["lines"] and x not in skip]
    return max(counts) if counts else None


def _py_statement(path: Path, line: int) -> int | None:
    """파이썬 파일에서 그 줄을 품은 가장 안쪽 문장의 시작 줄 — 여러 줄에 걸친 문장(긴 문자열·딕셔너리·리스트)은 커버리지에 시작 줄만
    기록된다 (예: 함수 밖의 HTML 템플릿 문자열 안의 줄)."""
    import ast
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or node.lineno >= best.lineno:
                best = node
    return best.lineno if best is not None else None


def _fn_count(d: dict, path: Path, line: int, near: int) -> int | None:
    """그 줄 근처(±near)에서 시작하는 함수, 아니면 (파이썬) 그 줄을 품은 함수의 호출 수, 그도 아니면 (파이썬) 그 줄을 품은 문장의
    시작 줄이 실행된 횟수 — 없으면 None."""
    if str(path).endswith(".py"):
        ran = _py_fn_ran(d, Path(path), line)
        if ran is not None:
            return ran
        stmt = _py_statement(path, line)
        if stmt is not None and stmt in d["lines"]:
            return d["lines"][stmt]
        return None
    fns = [n for _, fl, n in d["functions"] if fl and abs(fl - line) <= near]
    return max(fns) if fns else None


def _merge_cov(data: dict, part: dict, norm=lambda k: k) -> None:
    """측정 기록을 합친다 — 같은 파일이 여러 실행(node·브라우저·파이썬 프로세스)에 나오면 덮어쓰지 않고 줄은 큰 값, 함수는 모두."""
    for k, v in part.items():
        key = norm(k)
        cur = data.get(key)
        if cur is None:
            data[key] = {"lines": dict(v["lines"]), "functions": list(v["functions"])}
            continue
        for ln, c in v["lines"].items():
            cur["lines"][ln] = max(cur["lines"].get(ln, 0), c)
        cur["functions"].extend(v["functions"])


def _measured_fn(reports: list[Path], root: Path, browser: set, state: dict):
    """(절대 경로, 줄) → 케이스 실행이 그 함수를 거쳤나: True·False, 잴 수 없으면 None."""
    data = {}
    for rp in reports:
        try:
            _, part = runtime.parse(rp.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ToolError):
            continue
        _merge_cov(data, part, lambda k: os.path.normcase(os.path.abspath(k)))

    def measured(path: str, line: int) -> bool | None:
        d = data.get(os.path.normcase(os.path.abspath(path)))
        rel = os.path.relpath(path, root).replace("\\", "/")
        if d is None:
            if rel in browser:  # 화면 코드 — 브라우저에서 잰 결과가 있고 내용이 같은 파일이면 '안 거침'
                return False if state.get("browser_ran") and rel not in (state.get("browser_transformed") or ()) else None
            if not guard.measurable(root, rel, browser) or state.get("forced_stop") or (path.endswith(".py") and state.get("python_nocov")):
                return None
            if (path.endswith(".py") and not state.get("python_ran")) or (path.endswith(NODE_RUN_EXTS) and not state.get("node_ran")):
                return None  # 툴킷이 띄운 그 언어의 프로세스가 없었다 — 못 잰 것
            return False
        cnt = _fn_count(d, Path(path), line, 1)
        if cnt is not None:
            return cnt > 0
        near = [d["lines"][x] for x in range(line, line + 9) if x in d["lines"]]
        return any(c > 0 for c in near) if near else None
    return measured


def _impact(conn, settings, proj: dict, port_id: str, root: Path, reports: list[Path], browser: set, state: dict, fp: str) -> dict | None:
    """연관된 곳 — 바꾼 기존 함수를 부르는 기존 코드를 케이스가 거쳤나 (impact 모듈). 원래 소스 기록이 없으면(새 프로젝트) None."""
    from beom_workbench.core import impact
    base = guard.baseline_sources(conn, port_id, _port_dir(settings, proj, port_id))
    if not base:
        return None
    res = impact.analyze(settings, port_id, root, base, _measured_fn(reports, root, browser, state))
    if res is None:
        return None
    cid = "chk_" + uuid.uuid4().hex[:12]
    left = [c for c in res["callers"] if c["executed"] is not True]
    with ledger.tx(conn):
        conn.execute("INSERT INTO port_check (id, port_id, kind, cases, passed, failed, detail, evidence_id, created_at) "
                     "VALUES (?, ?, 'impact', ?, ?, ?, ?, NULL, ?)",
                     (cid, port_id, len(res["callers"]), len(res["callers"]) - len(left), len(left),
                      ledger.dumps({**res, "target_fp": fp}), _now()))
    guard.record(conn, port_id, "coverage", {"id": cid, "kind": "impact"}, checks=[cid])
    out = {"check_id": cid, **res}
    if left:
        out["note"] = ("바꾼 함수를 부르는 기존 코드를 합격 케이스가 거치지 않았다 — 그 동작(원래 하던 일)이 그대로인지 보는 케이스를 "
                       "더한다 (원래 테스트에 기대지 않는다). 영향이 없으면 이유를 보고에 쓴다 — 툴킷이 사용자 확인 카드로 올렸다")
    return out


def _target_adequacy(conn, port: dict, report, fp: str, base: Path | None = None, browser: set | None = None,
                     state: dict | None = None) -> dict:
    """대상(내 코드)을 케이스로 돌린 커버리지에서, ported 로 매핑한 함수가 모두 실행됐는지.
    원본을 계측할 수 없을 때(릴리스 빌드 바이너리 등)의 대안 — 원본 쪽 분기를 다 거쳤다는 뜻은 아니다.
    base: 측정 명령이 돈 자리 — 리포트의 상대 경로는 거기 기준이다.
    browser·state: 툴킷이 케이스 실행을 직접 잰 경우 — 브라우저에서 도는 파일, 언어별로 측정이 켜졌는지."""
    root = Path(port["target_root"])
    base = Path(base or root)
    data, fmts = {}, []
    for rp in (report if isinstance(report, list) else [report]):  # 언어마다 하나씩 잰 리포트를 합친다 (파이썬 + JS)
        fmt, part = runtime.parse(rp.read_text(encoding="utf-8-sig", errors="replace"))
        fmts.append(fmt)
        _merge_cov(data, part)
    fmt = "+".join(dict.fromkeys(fmts))
    norm = {k.replace("\\", "/").lower(): v for k, v in data.items()}
    for k, v in list(data.items()):
        if not os.path.isabs(k):  # 측정 도구가 지금 자리 기준으로 적은 경로
            norm.setdefault(os.path.abspath(k).replace("\\", "/").lower(), v)
    if base.resolve() != root.resolve():
        for k, v in list(data.items()):
            if not os.path.isabs(k):  # 측정한 자리 기준 상대 경로 → 절대 경로로도 찾게
                norm.setdefault(str((base / k).resolve()).replace("\\", "/").lower(), v)
    browser = set(browser or ())

    def unmeasured_why(path: str) -> str | None:
        """케이스 실행을 직접 쟀는데 이 파일 기록이 없을 때 — 툴킷이 잴 수 없었던 이유 (없으면 케이스가 안 거친 것)."""
        rel = os.path.relpath(root / path, root).replace("\\", "/")
        ext = PurePosixPath(path).suffix.lower()
        if VIEW_CODE.search(path):
            return "빌드 도구가 바꿔서 브라우저에 보내는 화면 코드 — 툴킷이 실행 범위를 재지 못함 (화면 케이스로 확인)"
        if rel in browser:
            if rel in (state.get("browser_transformed") or ()):
                return "브라우저가 받은 코드가 이 파일과 다름 (빌드·변환됨) — 툴킷이 실행 범위를 재지 못함 (화면 케이스로 확인)"
            if not state.get("browser_ran"):
                return "브라우저에서 도는 화면 코드 — 이 화면을 여는 화면 케이스가 없어 재지 못함"
            return None
        if ext != ".py" and ext not in NODE_RUN_EXTS:
            return "툴킷이 이 언어의 실행 범위를 재지 못함"
        if ext == ".py" and state.get("python_nocov"):
            return "케이스를 돌린 파이썬에서 측정을 켜지 못함"
        if ext == ".py" and not state.get("python_ran"):
            # 툴킷이 띄운 파이썬이 하나도 없었다 — 밖에서 띄운 서버(target_url 만)거나 PYTHONPATH 를 무시하는 파이썬
            return "케이스가 툴킷이 띄운 파이썬 프로세스에서 돌지 않음 (서버를 밖에서 띄웠으면 target_cmd 로 툴킷이 띄우게)"
        if ext in NODE_RUN_EXTS and not state.get("node_ran"):
            return "케이스가 툴킷이 띄운 node 프로세스에서 돌지 않음 (서버를 밖에서 띄웠으면 target_cmd 로 툴킷이 띄우게)"
        if state.get("forced_stop"):
            return "검증 서버가 정상 종료되지 않아 실행 기록을 쓰지 못함"
        return None

    def find(rel: str):
        # 정확히 같은 경로가 먼저 — 이름 끝만 맞추면 같은 이름의 다른 파일(unittest/main.py·pydantic/main.py)을 고르게 된다.
        # 끝만 맞는 것은 대상 폴더 안의 파일이 하나뿐일 때만 (가상 환경·라이브러리 폴더는 빼고)
        rel_l = rel.replace("\\", "/").lower()
        full = str((root / rel).resolve()).replace("\\", "/").lower()
        for k, v in norm.items():
            if k == rel_l or k == full or k == "./" + rel_l:
                return v
        root_l = str(root.resolve()).replace("\\", "/").lower().rstrip("/") + "/"
        skip = ("/site-packages/", "/node_modules/", "/.beom/", "/lib/python", "/venv/", "/.venv/")
        tails = [v for k, v in norm.items() if k.endswith("/" + rel_l) and (k.startswith(root_l) or not re.match(r"[a-z]:/|/", k))
                 and not any(x in "/" + k for x in skip)]
        return tails[0] if len(tails) == 1 else None

    rows = []
    for r in conn.execute("SELECT * FROM port_map WHERE port_id = ? AND status = 'ported' AND kind IN ('symbol', 'requirement') "
                          "ORDER BY item_id",
                          (port["id"],)).fetchall():
        m = re.fullmatch(r"(.+?):(\d+)", r["target_ref"] or "")
        if not m:
            continue
        path, ln = m.group(1), int(m.group(2))
        d = find(path)
        row = {"item": r["item_id"], "target": r["target_ref"]}
        if TEST_FILE.search(path.replace("\\", "/")) and (root / path).is_file():
            # 테스트 파일 자체 ('테스트가 있다' 같은 요구사항의 위치) — 커버리지 도구는 테스트 파일을 리포트에서 빼지만,
            # 리포트가 생겼다는 것이 테스트가 돌았다는 뜻이다. 빠졌다고 실패로 두면 고칠 길이 없어 에이전트가 같은 자리를 돈다
            # 케이스 실행을 재면 프로젝트의 테스트 파일도 기록에 들어오지만 케이스가 테스트를 부르지는 않는다 — 측정과 상관없이
            rows.append({**row, "executed": True,
                         "reason": "테스트 파일 — 이번에 만든 테스트는 검증 때 돌고, 원래 테스트를 고친 것은 확인 카드로"})
            continue
        if d is None and STYLE_FILE.search(path) and (root / path).is_file():
            # 스타일·마크업 — 실행되는 코드가 아니다. 실패로 두면 에이전트가 측정에 맞추려 화면 구조를 바꾼다 (실험에서 JSX 를 지웠다)
            rows.append({**row, "executed": True, "reason": "화면·스타일 파일 — 실행 범위가 아니라 화면 케이스로 확인"})
            continue
        if d is None and CONFIG_DOC.search(path) and (root / path).is_file():
            rows.append({**row, "executed": True, "reason": "설정·문서 파일 — 실행 범위가 아니라 프로젝트 검사·인수인계로 확인"})
            continue
        if d is None and VIEW_CODE.search(path) and (root / path).is_file() and state is None:
            rows.append({**row, "executed": None, "unmeasurable": True,
                         "reason": "측정 도구가 이 화면 코드를 읽지 못함 — 사용자 확인 카드로 넘김"})
            continue
        if d is None and state is not None and (root / path).is_file():
            why = unmeasured_why(path)
            if why:
                # 못 잰 이유가 툴킷 쪽 — 실패로 두면 에이전트가 측정에 맞추려 매핑·코드를 바꾼다 (실험: app.js → index.html)
                rows.append({**row, "executed": None, "unmeasurable": True, "reason": why + " — 사용자 확인 카드로 넘김"})
            elif os.path.relpath(root / path, root).replace("\\", "/") in browser:
                rows.append({**row, "executed": False, "reason": "화면 케이스가 이 코드를 실행하지 않음 — 이 화면을 열고 그 동작을 하는 화면 케이스가 필요"})
            else:
                rows.append({**row, "executed": False, "reason": "합격 케이스가 이 파일을 실행하지 않음 — 이 동작을 거치는 케이스가 필요"})
            continue
        if d is None:
            rows.append({**row, "executed": None, "reason": "리포트에 이 파일이 없음"})
            continue
        cnt = _fn_count(d, root / path, ln, 3)
        if STYLE_FILE.search(path) and cnt is None and ln not in d["lines"]:
            # HTML 의 마크업 줄 (그 안의 <script> 만 잰다) — 화면 파일 규칙대로
            rows.append({**row, "executed": True, "reason": "화면·스타일 파일 — 실행 범위가 아니라 화면 케이스로 확인"})
            continue
        if cnt is not None:
            executed = cnt > 0
        else:
            near = [d["lines"][x] for x in range(ln, ln + 9) if x in d["lines"]]
            executed = any(c > 0 for c in near) if near else None
        why = None
        if executed is None:
            why = "측정 줄 없음"
        elif executed is False:  # 잰 기록이 있는데 그 함수가 돌지 않았다 — 에이전트가 할 일을 적는다
            on_page = os.path.relpath(root / path, root).replace("\\", "/") in (browser or ())
            why = ("화면 케이스가 이 코드를 실행하지 않음 — 이 화면을 열고 그 동작을 하는 화면 케이스가 필요" if on_page else
                   "합격 케이스가 이 코드를 실행하지 않음 — 이 동작을 거치는 케이스가 필요")
        rows.append({**row, "executed": executed, **({"reason": why} if why else {})})
    not_run = [r for r in rows if r["executed"] is not True and not r.get("unmeasurable")]
    ran = sum(1 for r in rows if r["executed"] is True)
    unread = [r["target"] for r in rows if r.get("unmeasurable")]
    cid = "chk_" + uuid.uuid4().hex[:12]
    with ledger.tx(conn):
        conn.execute("INSERT INTO port_check (id, port_id, kind, cases, passed, failed, detail, evidence_id, created_at) "
                     "VALUES (?, ?, 'adequacy:target', ?, ?, ?, ?, NULL, ?)",
                     (cid, port["id"], len(rows), ran, len(not_run),
                      ledger.dumps({"format": fmt, "report": str(report), "rows": rows, "target_fp": fp,
                                    "basis": "cases" if state is not None else "report"}), _now()))
    # 바로 사슬에 잇는다 — 이어지는 검증이 도중에 실패해도 이 기록이 '기록에 없는 검사 결과'로 남아 무결이 깨지지 않게
    guard.record(conn, port["id"], "coverage", {"id": cid, "kind": "adequacy:target"}, checks=[cid])
    return {"check_id": cid, "ported": len(rows), "executed": ran, "not_executed": not_run[:30],
            **({"unmeasured": unread[:30], "unmeasured_note": "툴킷이 실행 범위를 재지 못한 파일 — 사용자 확인 카드로 넘겼다 "
                                                            "(측정에 맞추려고 코드 구조·매핑을 바꾸지 않는다)"} if unread else {})}


_SKIP_DIRS = {".beom", ".git", "tests", "test", "node_modules", ".venv", "venv", "__pycache__"}
_WALK_SKIP = {".beom", ".git", "node_modules", ".venv", "venv"}


def _files(root: Path) -> set[str] | None:
    """대상 폴더의 파일 목록(상대 경로) — 검증 전후를 비교해 검증이 만든 파일만 치우려고. 너무 크면 None (치우지 않는다)."""
    out: set[str] = set()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP]
        rel = Path(dirpath).relative_to(root)
        out.update(str(rel / f) for f in files)
        out.update(str(rel / d) + os.sep for d in dirs)
        if len(out) > 20000:
            return None
    return out


def _drop_new(root: Path, before: set[str] | None, keep: set[str]) -> list[str]:
    if before is None:
        return []
    after = _files(root) or set()
    keep_n = {str(Path(k)) for k in keep if k}
    # 깊은 것부터, 같은 깊이면 파일 먼저 — 폴더 'a/b/'(끝 구분자)와 그 안의 'a/b/x' 를 같은 깊이로 세면 폴더를 먼저 지우려다 실패한다
    new = sorted(after - before, key=lambda x: (-x.rstrip(os.sep).count(os.sep), x.endswith(os.sep)))
    gone: list[str] = []
    for rel in new:
        p = root / rel.rstrip(os.sep)
        if rel.rstrip(os.sep) in keep_n or "__pycache__" in Path(rel).parts:
            continue
        try:
            if rel.endswith(os.sep):
                p.rmdir()  # 빈 폴더만
            else:
                p.unlink()
            gone.append(rel.replace(os.sep, "/"))
        except OSError:
            pass
    return gone


def hollow_calls(cases: list[dict], root: Path) -> list[str]:
    """케이스가 부르는 파이썬 함수가 '적힌 값을 그대로 돌려주기만' 하고 프로그램 어디에서도 쓰이지 않으면 그 케이스 이름.
    합격 케이스를 통과시키려고 만든 검증 전용 함수(예: 화면 배치를 목록으로 돌려주기)를 잡는다 — 실제 동작을 확인하지 않는다."""
    import ast
    out: list[str] = []
    sources: dict[Path, str] | None = None
    for c in cases:
        spec = c.get("call")
        target = spec.get("target") if isinstance(spec, dict) else spec
        if not isinstance(target, str) or ":" not in target:
            continue
        file, _, func = target.rpartition(":")
        if not file.endswith(".py") or file.replace("\\", "/").startswith(".beom/") or "." in func:
            continue
        path = root / file
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func), None)
        if fn is None:
            continue
        body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant)
                                           and isinstance(n.value.value, str))]
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            continue
        try:
            ast.literal_eval(body[0].value)
        except (ValueError, SyntaxError, TypeError):
            continue
        if sources is None:
            sources = {}
            for p in root.rglob("*.py"):
                if not (_SKIP_DIRS & set(p.relative_to(root).parts)):
                    try:
                        sources[p] = p.read_text(encoding="utf-8")
                    except (OSError, ValueError):
                        pass
        word = re.compile(rf"\b{re.escape(func)}\b")
        used = any(len(word.findall(text)) > (1 if p.resolve() == path.resolve() else 0) for p, text in sources.items())
        if not used:
            out.append(str(c.get("name") or func))
    return out


def _group_failures(fails: list[dict]) -> dict:
    """생성 케이스 실패를 원래 케이스·필드별로 묶는다 (같은 원인의 반복을 한눈에)."""
    groups: dict[str, int] = defaultdict(int)
    for f in fails:
        key = f["name"].split("=", 1)[0] if f.get("generated") else f["name"]
        groups[key] += 1
    return dict(sorted(groups.items(), key=lambda kv: -kv[1]))
