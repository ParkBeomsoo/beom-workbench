"""Source maps — map the browser's run ranges on built code (Vue · JSX · TypeScript served by a dev server) back to the lines of
the project file, so screen code that a build tool rewrites can be measured like plain JS.

A map's original text is lined up with the file on disk (align): only lines that are the same text in both are credited — a
stale or foreign map, or lines a dev server rewrote, never credit the wrong lines.
"""
from __future__ import annotations

import base64
import bisect
import difflib
import json
import re
import urllib.parse

_B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}
_MAP_REF = re.compile(r"[#@]\s*sourceMappingURL\s*=\s*(\S+)\s*(?:\*/)?\s*$")
MAX_MAP = 4_000_000


def _vlq(seg: str) -> list[int]:
    out, val, shift = [], 0, 0
    for ch in seg:
        d = _B64.get(ch)
        if d is None:
            return []
        val += (d & 31) << shift
        if d & 32:
            shift += 5
            continue
        out.append(-(val >> 1) if val & 1 else val >> 1)
        val, shift = 0, 0
    return out


def decode(mappings: str) -> list[list[tuple[int, int, int, int]]]:
    """'mappings' → per generated line: [(gen_col, source_index, orig_line, orig_col)] (0-based, columns in UTF-16 units)."""
    lines: list[list[tuple[int, int, int, int]]] = []
    src = oline = ocol = 0
    for text in mappings.split(";"):
        gcol = 0
        row = []
        for seg in text.split(","):
            if not seg:
                continue
            v = _vlq(seg)
            if not v:
                continue
            gcol += v[0]
            if len(v) >= 4:
                src += v[1]
                oline += v[2]
                ocol += v[3]
                row.append((gcol, src, oline, ocol))
        lines.append(row)
    return lines


def find(text: str, url: str, fetch) -> dict | None:
    """The map a served script points at — inline (data: URL) or a separate file on the same server (fetch(url) → bytes)."""
    tail = text[-MAX_MAP:] if len(text) > MAX_MAP else text
    ref = None
    for line in reversed(tail.rstrip().splitlines()[-5:]):
        m = _MAP_REF.search(line)
        if m:
            ref = m.group(1)
            break
    if not ref:
        return None
    try:
        if ref.startswith("data:"):
            head, _, body = ref.partition(",")
            raw = base64.b64decode(body) if head.endswith(";base64") else urllib.parse.unquote(body).encode("utf-8")
        else:
            target = urllib.parse.urljoin(url, ref)
            if urllib.parse.urlsplit(target).netloc != urllib.parse.urlsplit(url).netloc:
                return None  # 다른 서버의 맵은 받지 않는다
            raw = fetch(target)
        if not raw or len(raw) > MAX_MAP:
            return None
        m = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, OSError, UnicodeError):
        return None
    if not isinstance(m, dict) or m.get("sections") or not isinstance(m.get("mappings"), str):
        return None  # 여러 맵을 묶은 형식(sections)은 다루지 않는다
    return m


def u16_line_starts(text: str) -> list[int]:
    """Generated text → where each line starts, in UTF-16 units (V8's offsets and the map's columns both count that way)."""
    starts, acc = [0], 0
    for ch in text:
        acc += 2 if ord(ch) > 0xFFFF else 1
        if ch == "\n":
            starts.append(acc)
    return starts


def align(original: str | None, on_disk: str) -> dict[int, int] | None:
    """The map's original lines → the same lines of the file on disk (1-based), by a line diff: only lines that are the same text
    in both are carried over — whatever a dev server rewrote (import paths, injected code) or the file changed since (a stale map)
    simply has no counterpart, and is not credited. None when the map has no original text (the file found by address is trusted).

    (Earlier: the whole file had to match, after replacing import paths, or 80% of its lines — a dev server rewriting anything
    else, or one more import, put the file back to 'cannot measure'.)"""
    if original is None:
        return None
    a, b = original.splitlines(), on_disk.splitlines()
    out: dict[int, int] = {}
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            if b[blk.b + k].strip():  # 빈 줄은 짝이라 할 근거가 없다 (다른 파일끼리도 맞는다)
                out[blk.a + k + 1] = blk.b + k + 1
    return out


def mapped_lines(smap: dict, starts: list[int], runs: list[list[dict]]) -> dict[int, dict]:
    """Run ranges on the generated script (V8 'functions' lists, one per page) → {source_index: {"lines": {line: count},
    "functions": [(name, line, count)]}} with 1-based original lines. A line takes the count of the innermost range that holds a
    segment mapped to it (the largest count across segments and pages)."""
    rows = decode(smap.get("mappings") or "")
    segs = []  # (generated offset, source index, original line)
    for gl, row in enumerate(rows):
        if gl >= len(starts):
            break
        base = starts[gl]
        segs.extend((base + gc, si, ol) for gc, si, ol, _ in row)
    segs.sort()
    offs = [s[0] for s in segs]
    out: dict[int, dict] = {}
    for fns in runs:
        spans = []
        for fn in fns or []:
            rs = fn.get("ranges") or []
            if not rs:
                continue
            spans.extend((int(r["startOffset"]), int(r["endOffset"]), int(r["count"])) for r in rs)
            st = int(rs[0]["startOffset"])
            if fn.get("functionName") or st > 0:  # 스크립트 전체(맨 위)는 함수가 아니다
                i = bisect.bisect_right(offs, st) - 1
                j = bisect.bisect_left(offs, st)
                hit = segs[j] if j < len(segs) and segs[j][0] < int(rs[0]["endOffset"]) else (segs[i] if i >= 0 else None)
                if hit:
                    d = out.setdefault(hit[1], {"lines": {}, "functions": []})
                    d["functions"].append((fn.get("functionName") or "(anonymous)", hit[2] + 1, int(rs[0]["count"])))
        paint: dict[int, int] = {}
        for st, en, cnt in sorted(spans, key=lambda x: x[0] - x[1]):  # 넓은 범위부터 — 안쪽 범위가 덮어쓴다
            for k in range(bisect.bisect_left(offs, st), bisect.bisect_left(offs, en)):
                paint[k] = cnt
        for k, cnt in paint.items():
            _, si, ol = segs[k]
            lines = out.setdefault(si, {"lines": {}, "functions": []})["lines"]
            lines[ol + 1] = max(lines.get(ol + 1, 0), cnt)
    return out
