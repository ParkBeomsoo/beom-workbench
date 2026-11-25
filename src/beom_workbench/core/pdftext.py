"""PDF 에서 글만 뽑는다 — 표준 라이브러리만으로 (따로 설치할 것 없이).

공식 규격 가이드가 PDF 로만 있는 경우가 많다 (예: 쇼핑 채널의 피드 제작 가이드). 에이전트의 PDF 읽기는 PC 에 따로 프로그램이 있어야
쪽을 열 수 있어 실패했다 — 그래서 툴킷이 글을 뽑아 준다. 압축(FlateDecode)된 쪽 내용에서 글 그리기 명령(Tj·TJ·'·")을 읽고,
글꼴의 글자 대응표(ToUnicode)로 한글·기호를 되살린다. 그림으로만 된 쪽(스캔)은 글이 없다 — 그때는 비어 있다고 알린다.
모든 PDF 를 완벽히 읽지는 않는다 (암호화·특수 인코딩) — 읽지 못한 쪽은 비운다.
"""
from __future__ import annotations

import re
import zlib

_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def _objects(data: bytes) -> dict[int, bytes]:
    return {int(m.group(1)): m.group(3) for m in _OBJ.finditer(data)}


def _stream(body: bytes) -> bytes | None:
    m = _STREAM.search(body)
    if not m:
        return None
    raw = m.group(1)
    head = body[: m.start()]
    if b"/FlateDecode" in head:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None
    if b"/Filter" in head:
        return None  # 다른 압축 방식 — 읽지 않는다
    return raw


def _ref(body: bytes, key: bytes) -> int | None:
    m = re.search(rb"/" + key + rb"\s+(\d+)\s+0\s+R", body)
    return int(m.group(1)) if m else None


def _cmap(text: bytes) -> dict[bytes, str]:
    """ToUnicode CMap — 글자 코드(바이트) → 유니코드 글."""
    out: dict[bytes, str] = {}

    def uni(h: bytes) -> str:
        h = h.decode("ascii", "ignore")
        try:
            return bytes.fromhex(h).decode("utf-16-be", "replace")
        except ValueError:
            return ""
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.S):
        for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            out[bytes.fromhex(a.decode())] = uni(b)
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.S):
        for a, b, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", blk):
            lo, hi, n = int(a, 16), int(b, 16), len(a) // 2
            if rest.startswith(b"["):
                for i, h in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", rest)):
                    out[(lo + i).to_bytes(n, "big")] = uni(h)
            else:
                start = int(rest[1:-1], 16)
                width = len(rest[1:-1]) // 2
                for i in range(min(hi - lo + 1, 65536)):
                    try:
                        out[(lo + i).to_bytes(n, "big")] = (start + i).to_bytes(width, "big").decode("utf-16-be", "replace")
                    except OverflowError:
                        break
    return out


def _decode(s: bytes, cmap: dict[bytes, str] | None) -> str:
    if not cmap:
        return s.decode("latin-1", "replace")
    sizes = sorted({len(k) for k in cmap}, reverse=True) or [1]
    out, i = [], 0
    while i < len(s):
        for n in sizes:
            if s[i:i + n] in cmap:
                out.append(cmap[s[i:i + n]])
                i += n
                break
        else:
            i += 1
    if not out and s and all(32 <= b < 127 for b in s):  # 대응표에 없는 그냥 글자(ASCII) — 그대로
        return s.decode("ascii")
    return "".join(out)


def _literal(tok: bytes) -> bytes:
    """(…) 글 — 이스케이프를 푼다."""
    out, i = bytearray(), 1
    while i < len(tok) - 1:
        c = tok[i]
        if c == 0x5C and i + 1 < len(tok) - 1:  # 역슬래시
            n = tok[i + 1]
            esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
            if n in esc:
                out.append(esc[n]); i += 2
            elif 0x30 <= n <= 0x37:
                m = re.match(rb"[0-7]{1,3}", tok[i + 1:i + 4])
                out.append(int(m.group(0), 8) & 0xFF); i += 1 + len(m.group(0))
            else:
                out.append(n); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


_TOK = re.compile(rb"\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)|<[0-9A-Fa-f\s]*>|/[^\s/<>\[\]()]+|\[|\]|[A-Za-z'\"]+\*?|[-+]?\d*\.?\d+")


def _page_text(content: bytes, fonts: dict[bytes, dict[bytes, str]]) -> str:
    # 줄은 글의 세로 위치가 바뀔 때만 바꾼다 — 발표 자료로 만든 PDF 는 낱말마다 위치를 따로 정해, 명령마다 줄을 바꾸면 낱말마다 줄이 갈렸다
    out: list[str] = []
    cmap = None
    stack: list[bytes] = []
    pos = {"y": None, "last": None}

    def nums(n: int) -> list[float]:
        vals = [x for x in stack if re.fullmatch(rb"[-+]?\d*\.?\d+", x)][-n:]
        return [float(v) for v in vals] if len(vals) == n else []

    def place() -> None:
        if pos["y"] is None or pos["last"] is None:
            sep = ""
        else:
            sep = "\n" if abs(pos["y"] - pos["last"]) > 2 else " "
        if out and sep and not out[-1].endswith(("\n", " ")):
            out.append(sep)
        pos["last"] = pos["y"]

    for t in _TOK.findall(content):
        if t.startswith(b"/"):
            stack.append(t)
        elif t in (b"Tf",):
            name = next((x for x in reversed(stack) if x.startswith(b"/")), b"")
            cmap = fonts.get(name[1:])
            stack = []
        elif t == b"Tm":
            v = nums(6)
            if v:
                pos["y"] = v[5]
            stack = []
        elif t in (b"Td", b"TD"):
            v = nums(2)
            if v and pos["y"] is not None:
                pos["y"] += v[1]
            stack = []
        elif t == b"T*":
            pos["y"] = (pos["y"] or 0) - 10
            stack = []
        elif t in (b"Tj", b"'", b'"', b"TJ"):
            place()
            for x in stack:
                if x.startswith(b"("):
                    out.append(_decode(_literal(x), cmap))
                elif x.startswith(b"<"):
                    h = re.sub(rb"\s", b"", x[1:-1])
                    out.append(_decode(bytes.fromhex((h + b"0" * (len(h) % 2)).decode()), cmap))
                elif t == b"TJ" and re.fullmatch(rb"[-+]?\d*\.?\d+", x) and float(x) < -200:
                    out.append(" ")  # 글자 사이를 크게 띄운 자리 — 낱말 사이
            stack = []
        elif t in (b"BT", b"ET"):
            stack = []
        else:
            stack.append(t)
    text = re.sub(r"[ \t]{2,}", " ", "".join(out))
    return re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"\n{3,}", "\n\n", text)).strip()


def pages(data: bytes, limit: int = 200) -> list[str]:
    """쪽마다 글. 읽지 못하면 빈 목록."""
    if not data.startswith(b"%PDF") or b"/Encrypt" in data[:4096] + data[-4096:]:
        return []
    objs = _objects(data)
    cmaps: dict[int, dict[bytes, str]] = {}

    def font_maps(page: bytes) -> dict[bytes, dict[bytes, str]]:
        res = page
        r = _ref(page, b"Resources")
        if r is not None and r in objs:
            res = objs[r]
        fm = re.search(rb"/Font\s*(<<.*?>>|\d+\s+0\s+R)", res, re.S)
        if not fm:
            return {}
        fdict = fm.group(1)
        if fdict.endswith(b"R"):
            fdict = objs.get(int(fdict.split()[0]), b"")
        out = {}
        for name, num in re.findall(rb"/([^\s/<>]+)\s+(\d+)\s+0\s+R", fdict):
            fobj = objs.get(int(num), b"")
            tu = _ref(fobj, b"ToUnicode")
            if tu is not None:
                if tu not in cmaps:
                    s = _stream(objs.get(tu, b""))
                    cmaps[tu] = _cmap(s) if s else {}
                out[name] = cmaps[tu]
        return out
    found = []
    for num in sorted(objs):
        body = objs[num]
        if not re.search(rb"/Type\s*/Page(?!s)\b", body):
            continue
        contents = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+0\s+R)", body)
        if not contents:
            found.append("")
            continue
        refs = [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", contents.group(1))]
        raw = b"".join((_stream(objs.get(r, b"")) or b"") + b"\n" for r in refs)
        try:
            found.append(_page_text(raw, font_maps(body)))
        except Exception:  # noqa: BLE001 — 한 쪽을 못 읽어도 나머지는 읽는다
            found.append("")
        if len(found) >= limit:
            break
    return found
