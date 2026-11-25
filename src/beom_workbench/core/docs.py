"""첨부 문서를 에이전트가 읽을 수 있는 글로 — 추가 설치 없이 (표준 라이브러리만).

docx · pptx · xlsx · hwpx 는 압축된 XML 이라 글을 바로 뽑는다. PDF·이미지는 원본을 그대로 두고 에이전트가 읽는다
(Claude Code 는 PDF·이미지를 읽는다). 웹 주소는 공개 페이지면 글로 저장하고, 로그인이 필요하면 알려 준다.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

from beom_workbench.common.response import ToolError

TEXT_LIMIT = 400_000
CONVERTIBLE = {".docx", ".pptx", ".xlsx", ".hwpx"}
# 예전 바이너리 형식(OLE 복합 문서) — 글을 뽑을 수 없고 에이전트도 못 읽는다. 받지 않고 새 형식으로 저장하라고 안내한다.
LEGACY = {
    ".hwp": "한글 .hwp 는 읽을 수 없습니다 — 한글에서 [파일 → 다른 이름으로 저장 → 파일 형식: HWPX(*.hwpx)]로 저장해 첨부해 주세요",
    ".doc": "Word 97-2003 .doc 는 읽을 수 없습니다 — Word 에서 .docx 로 저장해 첨부해 주세요",
    ".ppt": "PowerPoint 97-2003 .ppt 는 읽을 수 없습니다 — .pptx 로 저장해 첨부해 주세요",
    ".xls": "Excel 97-2003 .xls 는 읽을 수 없습니다 — .xlsx 로 저장해 첨부해 주세요",
}
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def check_attachable(path: Path) -> None:
    """첨부할 수 있는 파일인지 — 읽을 수 없는 예전 형식이면 ToolError (확장자를 바꿔 붙여도 내용으로 알아본다)."""
    path = Path(path)
    why = LEGACY.get(path.suffix.lower())
    if why is None:
        try:
            with open(path, "rb") as fh:
                head = fh.read(512)
        except OSError:
            return
        if head.startswith(b"HWP Document File"):  # 한글 3.0 형식
            why = LEGACY[".hwp"]
        elif head.startswith(_OLE) and path.suffix.lower() not in (".msg",):
            why = (LEGACY[".hwp"] if b"H\x00W\x00P\x00" in head else
                   f"{path.name} 은 예전 오피스·한글 바이너리 형식이라 읽을 수 없습니다 — docx·pptx·xlsx·hwpx 로 저장해 첨부해 주세요")
    if why:
        raise ToolError("invalid_request", f"{path.name}: {why}")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


PART_LIMIT = 50 * 1024 * 1024  # 압축 폭탄 막기 — 문서 한 부분이 이보다 크면 읽지 않는다
TOTAL_LIMIT = 200 * 1024 * 1024  # 문서 전체로도 이만큼까지만 풀어 본다
_budget = {"left": TOTAL_LIMIT}


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    """신뢰할 수 없는 문서의 XML — DTD·엔티티 선언이 있으면 거절한다 (OOXML·HWPX 는 쓰지 않는다: 엔티티 폭탄·XXE 차단)."""
    try:
        info = z.getinfo(name)
        if info.file_size > PART_LIMIT or info.file_size > _budget["left"]:
            return None
        _budget["left"] -= info.file_size
        data = z.read(name)
    except KeyError:
        return None
    head = data.replace(b"\x00", b"")  # UTF-16 으로 감춘 선언도 본다
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise ToolError("invalid_request", f"문서 안에 허용하지 않는 선언(DTD·엔티티)이 있음: {name}")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _docx(z: zipfile.ZipFile) -> str:
    root = _xml(z, "word/document.xml")
    if root is None:
        return ""
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "tbl":
            for tr in (e for e in el.iter() if _local(e.tag) == "tr"):
                cells = ["".join(t.text or "" for t in tc.iter() if _local(t.tag) == "t").strip()
                         for tc in tr if _local(tc.tag) == "tc"]
                out.append("| " + " | ".join(cells) + " |")
            el.clear()  # 표 안의 문단을 다시 세지 않게
        elif name == "p":
            text = "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t").strip()
            if not text:
                continue
            style = next((s.get(next(iter(s.attrib), ""), "") for s in el.iter() if _local(s.tag) == "pStyle"), "")
            m = re.search(r"(?:Heading|제목)\s*(\d)", style)
            out.append(("#" * int(m.group(1)) + " " if m else "") + text)
    return "\n\n".join(out)


def _pptx(z: zipfile.ZipFile) -> str:
    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)", n).group(1)))
    out = []
    for i, name in enumerate(slides, 1):
        root = _xml(z, name)
        paras = []
        for p in (e for e in (root.iter() if root is not None else []) if _local(e.tag) == "p"):
            t = "".join(x.text or "" for x in p.iter() if _local(x.tag) == "t").strip()
            if t:
                paras.append(t)
        out.append(f"## 슬라이드 {i}\n\n" + "\n".join(f"- {p}" for p in paras))
    return "\n\n".join(out)


def _xlsx(z: zipfile.ZipFile) -> str:
    shared = []
    sroot = _xml(z, "xl/sharedStrings.xml")
    if sroot is not None:
        for si in (e for e in sroot if _local(e.tag) == "si"):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    out = []
    sheets = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    for name in sheets:
        root = _xml(z, name)
        rows = []
        for row in (e for e in (root.iter() if root is not None else []) if _local(e.tag) == "row"):
            cells = []
            for c in (e for e in row if _local(e.tag) == "c"):
                v = next((x.text for x in c.iter() if _local(x.tag) in ("v", "t")), "") or ""
                if c.get("t") == "s" and v.isdigit() and int(v) < len(shared):
                    v = shared[int(v)]
                cells.append(v.strip())
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
            if len(rows) >= 2000:
                break
        out.append(f"## {Path(name).stem}\n\n" + "\n".join(rows))
    return "\n\n".join(out)


def _hwpx(z: zipfile.ZipFile) -> str:
    sections = sorted(n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n))
    out = []
    for name in sections:
        root = _xml(z, name)
        for p in (e for e in (root.iter() if root is not None else []) if _local(e.tag) == "p"):
            t = "".join(x.text or "" for x in p.iter() if _local(x.tag) == "t").strip()
            if t:
                out.append(t)
    return "\n\n".join(out)


def to_text(path: Path) -> str | None:
    """문서에서 글만 — 바꿀 수 없는 형식이면 None."""
    ext = path.suffix.lower()
    check_attachable(path)
    if ext not in CONVERTIBLE:
        return None
    _budget["left"] = TOTAL_LIMIT
    try:
        with zipfile.ZipFile(path) as z:
            text = {".docx": _docx, ".pptx": _pptx, ".xlsx": _xlsx, ".hwpx": _hwpx}[ext](z)
    except (zipfile.BadZipFile, OSError) as e:
        raise ToolError("invalid_request", f"{path.name} 을 읽지 못함 ({e})") from None
    return text[:TEXT_LIMIT]


# ---------------------------------------------------------------- 웹 페이지 · 링크 문서
#
# 기획서·정책 문서는 표·목록·제목 구조와 그림(화면 설계·흐름도)이 내용의 절반이다. 화면 글(innerText)로 납작하게 베끼면
# 표가 무너지고 접힌 내용(펼치기 블록)은 빠진다. 그래서 HTML 을 마크다운으로 바꿔 구조를 살리고, 그림·첨부 파일은 받아서
# 같은 폴더에 둔다 (에이전트가 그림·PDF 도 직접 읽는다).

_VOID = {"br", "img", "hr", "input", "meta", "link", "col", "source", "wbr", "area", "base", "embed", "param", "track"}
_SKIP = {"script", "style", "noscript", "template", "head", "svg", "button", "form", "nav", "footer", "aside", "iframe",
         "select", "textarea", "canvas", "object"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "figure", "figcaption", "details", "summary", "address",
          "dl", "dt", "dd", "center", "fieldset", "legend", "body", "html", "root", "tbody", "thead", "tfoot", "caption"}
_IMG = "\x00IMG{}\x00"
FILE_MAX = 15 * 1024 * 1024      # 파일 하나
FILES_MAX = 60 * 1024 * 1024     # 링크 하나에서 받는 파일 전체
FILES_COUNT = 40
_DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".hwpx", ".csv", ".txt", ".md", ".json", ".yaml", ".yml"}
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


class _Node:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag: str, attrs: dict | None = None):
        self.tag, self.attrs, self.kids = tag, attrs or {}, []

    def cls(self) -> str:
        return self.attrs.get("class") or ""


class _Tree(HTMLParser):
    """느슨한 HTML 트리 — 닫는 태그가 빠진 p·li·td 도 견딘다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.title = ""
        self._title = False

    def _close_implied(self, tag: str) -> None:
        top = self.stack[-1].tag
        if tag == "li" and top == "li" or tag == "p" and top == "p" or tag in ("dt", "dd") and top in ("dt", "dd"):
            self.stack.pop()
        elif tag in ("td", "th", "tr"):
            # 열린 칸·줄을 닫는다 — 단, 이 표 바깥(바깥 표의 칸)까지는 올라가지 않는다
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t in ("table", "tbody", "thead", "tfoot"):
                    break
                if t == "tr" and tag in ("td", "th"):
                    break
                if t in ("td", "th", "tr"):
                    del self.stack[i:]
                    if t == "tr" or tag in ("td", "th"):
                        break

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._title = True
            return
        self._close_implied(tag)
        node = _Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].kids.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(_Node(tag, {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._title:
            self.title += data
        else:
            self.stack[-1].kids.append(data)


def _main_part(root: _Node) -> _Node:
    """일반 웹 페이지는 본문 영역만 — 없으면 전체."""
    def find(n: _Node):
        for k in n.kids:
            if isinstance(k, _Node):
                if k.tag in ("main", "article") or k.attrs.get("role") == "main" or k.attrs.get("id") == "main-content":
                    return k
                got = find(k)
                if got is not None:
                    return got
        return None
    got = find(root)
    return got if got is not None and len(_plain(got).strip()) > 200 else root


def _plain(n) -> str:
    if isinstance(n, str):
        return n
    if n.tag in _SKIP:
        return ""
    return "".join(_plain(k) for k in n.kids)


def _int(v: str, hi: int) -> int:
    d = re.sub(r"\D", "", v or "")
    return max(1, min(int(d) if d else 1, hi))


class _MD:
    """트리 → 마크다운. 그림은 자리표(_IMG)로 두고 받은 뒤 경로로 바꾼다."""

    def __init__(self, base: str, images: list):
        self.base, self.images = base, images

    def url(self, u: str) -> str:
        from urllib.parse import urljoin
        u = (u or "").strip()
        return urljoin(self.base, u) if self.base and u and not u.startswith(("data:", "javascript:", "#", "mailto:")) else u

    def inline(self, n: _Node, pre: bool = False) -> str:
        return "".join(self.node(k, pre) for k in n.kids)

    def node(self, n, pre: bool = False, depth: int = 0) -> str:  # noqa: C901 — 태그별 분기
        if isinstance(n, str):
            return n if pre else re.sub(r"\s+", " ", n)
        t, c = n.tag, n.cls()
        if t in _SKIP:
            return ""
        if re.fullmatch(r"h[1-6]", t):
            return f"\n\n{'#' * int(t[1])} {_tidy(self.inline(n))}\n\n"
        if t == "br":
            return "\n"
        if t == "hr":
            return "\n\n---\n\n"
        if t == "img":
            src = n.attrs.get("data-image-src") or n.attrs.get("src") or ""
            alt = (n.attrs.get("alt") or n.attrs.get("title") or "").strip()
            if "emoticon" in c or "emoji" in c or not src or src.startswith("data:"):
                return alt
            self.images.append((self.url(src), alt))
            return _IMG.format(len(self.images) - 1)
        if t == "a":
            text = _tidy(self.inline(n))
            href = self.url(n.attrs.get("href") or "")
            if not href or href.startswith(("#", "javascript:")) or not text or "\x00IMG" in text:
                return text
            return f"<{href}>" if text == href else f"[{text}]({href})"
        if t in ("strong", "b", "em", "i", "s", "del", "strike"):
            s = self.inline(n)
            mark = "**" if t in ("strong", "b") else "*" if t in ("em", "i") else "~~"
            return f"{mark}{s.strip()}{mark}" if s.strip() and "\n" not in s.strip() else s
        if t == "pre":
            code = _plain(n).strip("\n")
            return f"\n\n```\n{code}\n```\n\n"
        if t == "code":
            s = _plain(n)
            return f"`{s}`" if s.strip() and "\n" not in s else s
        if t in ("ul", "ol"):
            return "\n\n" + self.list(n, depth) + "\n\n"
        if t == "table":
            return "\n\n" + self.table(n) + "\n\n"
        if t == "blockquote" or "information-macro" in c or "panel" in c.split() or "aui-message" in c:
            body = self.blocks(n)
            return "\n\n" + "\n".join("> " + ln if ln else ">" for ln in body.splitlines()) + "\n\n" if body else ""
        if "status-macro" in c or "aui-lozenge" in c:
            return f"[{_plain(n).strip()}]"
        if t in ("p", "summary", "figcaption", "dt"):
            return "\n\n" + self.inline(n) + "\n\n"
        if t in _BLOCK or t in ("li", "tr"):
            return "\n" + self.inline(n) + "\n"
        return self.inline(n, pre)

    def blocks(self, n: _Node) -> str:
        return _tidy(self.inline(n))

    def list(self, n: _Node, depth: int) -> str:
        out, i = [], _int(n.attrs.get("start", "1"), 100000) if n.tag == "ol" else 1
        task_list = "task-list" in n.cls() or "inline-task-list" in n.cls()
        for k in n.kids:
            if not isinstance(k, _Node) or k.tag != "li":
                continue
            subs = [x for x in k.kids if isinstance(x, _Node) and x.tag in ("ul", "ol")]
            body = _Node("li", k.attrs)
            body.kids = [x for x in k.kids if x not in subs]
            text = re.sub(r"\n{2,}", "\n", _tidy(self.inline(body)))
            mark = f"{i}." if n.tag == "ol" else "-"
            if "checked" in k.cls().split() or k.attrs.get("data-inline-task-status") == "complete":
                mark += " [x]"
            elif task_list:
                mark += " [ ]"
            pad = "  " * depth
            lines = text.splitlines() or [""]
            out.append(pad + mark + " " + lines[0] + "".join("\n" + pad + "   " + ln for ln in lines[1:]))
            out += [self.list(s, depth + 1) for s in subs]
            i += 1
        return "\n".join(x for x in out if x.strip())

    def table(self, n: _Node) -> str:
        rows: list[list[str]] = []
        spans: dict[int, int] = {}  # 열 → 위 칸이 아래로 더 덮는 줄 수

        def fill(row: list, col: int) -> int:
            while col in spans:
                row.append("")
                spans[col] -= 1
                if spans[col] <= 0:
                    del spans[col]
                col += 1
            return col

        def walk(x: _Node):
            for k in x.kids:
                if not isinstance(k, _Node) or k.tag == "table":
                    continue
                if k.tag != "tr":
                    walk(k)
                    continue
                row, col = [], 0
                for cell in (c for c in k.kids if isinstance(c, _Node) and c.tag in ("td", "th")):
                    col = fill(row, col)
                    txt = self.cell(cell)
                    cs, rs = _int(cell.attrs.get("colspan", "1"), 50), _int(cell.attrs.get("rowspan", "1"), 500)
                    for j in range(cs):
                        row.append(txt if j == 0 else "")
                        if rs > 1:
                            spans[col] = rs - 1
                        col += 1
                fill(row, col)
                rows.append(row)
        walk(n)
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(out)

    def cell(self, c: _Node) -> str:
        inner = []
        for k in c.kids:
            if isinstance(k, _Node) and k.tag == "table":  # 칸 안의 표 — 줄은 ' / ', 칸은 ' · ' 로 한 줄에
                rows = [ln.strip("| ").split(" | ") for ln in self.table(k).splitlines() if not ln.startswith("|---")]
                inner.append(" " + " / ".join(" · ".join(c for c in r if c) for r in rows) + " ")
            else:
                inner.append(self.node(k))
        s = _tidy("".join(inner))
        return s.replace("|", "\\|").replace("\n\n", "<br>").replace("\n", "<br>")


def _tidy(s: str) -> str:
    """\ube48\uce78\u00b7\ube48 \uc904 \uc815\ub9ac \u2014 \ucf54\ub4dc \ube14\ub85d(```) \uc548\uc740 \uadf8\ub300\ub85c."""
    parts = re.split(r"(\n```\n.*?\n```\n)", "\n" + s + "\n", flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", "".join(p if p.startswith("\n```\n") else _tidy_text(p) for p in parts)).strip()


def _tidy_text(s: str) -> str:
    s = s.replace("\u200b", "").replace("\u00a0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+(?=[^\s\-0-9])", "\n", s)
    s = re.sub(r"(?<=\S)[ \t]{2,}", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s)


def html_to_md(html: str, base: str = "", *, whole_page: bool = False) -> tuple[str, list[tuple[str, str]], str]:
    """HTML → (마크다운, [(그림 주소, 설명)], 제목). 그림 자리는 _IMG 자리표."""
    t = _Tree()
    t.feed(html or "")
    t.close()
    images: list = []
    root = _main_part(t.root) if whole_page else t.root
    return _MD(base, images).blocks(root), images, re.sub(r"\s+", " ", t.title).strip()


def _safe_name(name: str, used: set) -> str:
    name = re.sub(r"[^\w가-힣.\-]+", "_", name).strip("._")[:80] or "file"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    cand, n = name, 2
    while cand.lower() in used:
        cand = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
        n += 1
    used.add(cand.lower())
    return cand


def _ext_of(url: str, media: str = "") -> str:
    from urllib.parse import unquote, urlparse
    ext = Path(unquote(urlparse(url).path)).suffix.lower()
    if ext and len(ext) <= 6:
        return ext
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
            "application/pdf": ".pdf"}.get((media or "").split(";")[0].strip().lower(), "")


class LinkDoc:
    """링크 하나를 읽을 거리 묶음으로 — 본문(마크다운) + 그림 + 첨부 파일. 받기는 바깥(urllib 또는 로그인한 브라우저)이 한다."""

    def __init__(self, url: str, title: str = "", how: str = "page"):
        self.url, self.title, self.how = url, title, how
        self.meta: list[str] = []
        self.parts: list[tuple[str, str]] = []     # (제목, 마크다운 — 그림 자리표 포함)
        self.images: list[tuple[str, str]] = []
        self.attachments: list[dict] = []          # {name, url, media, size}
        self.notes: list[str] = []

    def add_html(self, heading: str, html: str, base: str = "", whole_page: bool = False) -> str:
        offset = len(self.images)
        md, imgs, title = html_to_md(html, base or self.url, whole_page=whole_page)
        md = re.sub(r"\x00IMG(\d+)\x00", lambda m: _IMG.format(int(m.group(1)) + offset), md)
        self.images += imgs
        self.parts.append((heading, md))
        return title

    def add_text(self, heading: str, text: str) -> None:
        self.parts.append((heading, _tidy(text or "")))

    def wanted(self) -> list[dict]:
        """받을 파일 — 본문의 그림 먼저, 그다음 첨부(그림·문서). 중복 없이, 개수 한도 안."""
        out, seen = [], set()
        for u, alt in self.images:
            if u and u not in seen and re.match(r"https?://", u):
                seen.add(u)
                out.append({"url": u, "name": alt, "kind": "image"})
        for a in self.attachments:
            ext = _ext_of(a.get("name") or a["url"], a.get("media") or "")
            if a["url"] not in seen and (ext in _IMG_EXT or ext in _DOC_EXT) and (a.get("size") or 0) <= FILE_MAX:
                seen.add(a["url"])
                out.append({"url": a["url"], "name": a.get("name") or "", "kind": "attachment"})
        return out[:FILES_COUNT]

    def write(self, folder: Path, files: dict[str, tuple[bytes, str]]) -> Path:
        """folder/<제목>.md 와 folder/files/ 를 만든다. files: 주소 → (내용, media type)."""
        from urllib.parse import unquote, urlparse
        folder.mkdir(parents=True, exist_ok=True)
        used: set = set()
        local: dict[str, str] = {}
        total = 0
        for u, (data, media) in files.items():
            if not data or len(data) > FILE_MAX or total + len(data) > FILES_MAX:
                continue
            total += len(data)
            base_name = next((a.get("name") for a in self.attachments if a["url"] == u and a.get("name")), "") or \
                Path(unquote(urlparse(u).path)).name or "file"
            if not Path(base_name).suffix:
                base_name += _ext_of(u, media) or ".bin"
            name = _safe_name(base_name, used)
            (folder / "files").mkdir(exist_ok=True)
            (folder / "files" / name).write_bytes(data)
            local[u] = f"files/{name}"
            if Path(name).suffix.lower() in CONVERTIBLE:  # 첨부 문서는 글로도
                try:
                    text = to_text(folder / "files" / name)
                    if text:
                        (folder / "files" / f"{name}.md").write_text(text, encoding="utf-8")
                except ToolError:
                    pass

        def img(m):
            u, alt = self.images[int(m.group(1))]
            alt = re.sub(r"[\[\]\n]", "", alt or "") or "그림"
            return f"![{alt}]({local[u]})" if u in local else f"![{alt}]({u})"

        lines = [f"<!-- 출처: {self.url} · {self.via} -->", "", f"# {self.title or self.url}", ""]
        lines += [m for m in self.meta if m]
        if self.meta:
            lines.append("")
        for heading, md in self.parts:
            body = re.sub(r"\x00IMG(\d+)\x00", img, md).strip()
            if not body:
                continue
            if heading:
                lines += [f"## {heading}", ""]
            lines += [body, ""]
        if self.attachments:
            lines += ["## 첨부 파일", ""]
            for a in self.attachments:
                p = local.get(a["url"])
                size = f" ({max(1, a['size'] // 1024):,} KB)" if a.get("size") else ""
                if p:
                    extra = f" · 글로 바꾼 것: {p}.md" if (folder / f"{p}.md").exists() else ""
                    lines.append(f"- [{a.get('name') or p}]({p}){size}{extra}")
                else:
                    lines.append(f"- {a.get('name') or a['url']}{size} — 받지 않음 (원본: {a['url']})")
            lines.append("")
        uniq = {u for u, _ in self.images}
        got_imgs = sum(1 for u in uniq if u in local)
        if uniq and got_imgs < len(uniq):
            self.notes.append(f"본문 그림 {len(uniq)}개 중 {got_imgs}개를 받았습니다 — 나머지는 원래 주소로 남겼습니다.")
        if self.notes:
            lines += ["> " + n for n in self.notes] + [""]
        text = _tidy("\n".join(lines)) + "\n"
        slug = re.sub(r"[^\w가-힣-]+", "-", self.title or "page")[:50].strip("-") or "page"
        md_path = folder / f"{slug}.md"
        md_path.write_text(text[:TEXT_LIMIT], encoding="utf-8")
        (folder / ".link.json").write_text(json.dumps({"url": self.url, "how": self.how, "title": self.title,
                                                       "files": len(local)}, ensure_ascii=False), encoding="utf-8")
        return md_path

    @property
    def via(self) -> str:
        return {"confluence-api": "위키 API(로그인한 세션)", "jira-api": "이슈 API(로그인한 세션)",
                "page-login": "앱 브라우저에서 로그인한 페이지", "page": "공개 페이지"}.get(self.how, self.how)


def is_link_package(md: Path) -> bool:
    return (md.parent / ".link.json").is_file()


def _get(url: str, timeout: float, limit: int = 5_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (document fetch)", "Accept": "text/html,text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), resp.headers.get("Content-Type", ""), resp.read(limit + 1)


def fetch_url(url: str, timeout: float = 20) -> dict:
    """공개 웹 페이지를 마크다운으로. 로그인이 필요한 페이지(401·403·로그인 화면으로 이동)는 needs_login 으로 알린다."""
    if not re.match(r"https?://", url):
        raise ToolError("invalid_request", "http(s) 주소만 됩니다")
    try:
        final, ctype, raw = _get(url, timeout)
    except urllib.error.HTTPError as e:
        return {"ok": False, "needs_login": e.code in (401, 403), "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"ok": False, "needs_login": False, "error": str(getattr(e, "reason", e))[:200]}
    charset = re.search(r"charset=([\w-]+)", ctype)
    try:
        body = raw.decode(charset.group(1) if charset else "utf-8", "replace")
    except LookupError:  # 모르는 문자 집합 이름
        body = raw.decode("utf-8", "replace")
    if re.search(r"/login|/signin|login\.action|auth", final, re.I) and final != url:
        return {"ok": False, "needs_login": True, "error": "로그인 화면으로 이동함"}
    if "html" not in ctype and not body.lstrip().startswith("<"):
        return {"ok": True, "title": url, "text": body[:TEXT_LIMIT], "final_url": final, "html": None}
    md, _images, title = html_to_md(body, final, whole_page=True)
    text = re.sub(r"\x00IMG\d+\x00", "", md)
    if len(text) < 200 and re.search(r"(log ?in|sign ?in|로그인|type=.?password)", body, re.I):
        return {"ok": False, "needs_login": True, "error": "로그인이 필요한 페이지로 보임"}
    return {"ok": True, "title": title or url, "text": text[:TEXT_LIMIT], "final_url": final, "html": body}


def fetch_link(url: str, folder: Path, timeout: float = 20) -> dict:
    """공개 링크를 묶음(본문 마크다운 + 그림)으로 folder 에 저장 — {ok, path, title} 또는 fetch_url 의 실패."""
    got = fetch_url(url, timeout)
    if not got.get("ok"):
        return got
    doc = LinkDoc(url, got["title"], "page")
    if got.get("html") is None:
        doc.add_text("", got["text"])
    else:
        doc.add_html("", got["html"], got.get("final_url") or url, whole_page=True)
    files, total = {}, 0
    for w in doc.wanted()[:20]:
        try:
            _, ctype, data = _get(w["url"], min(timeout, 15), FILE_MAX)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
        if len(data) <= FILE_MAX and total + len(data) <= FILES_MAX:
            files[w["url"]] = (data, ctype)
            total += len(data)
    return {"ok": True, "path": str(doc.write(folder, files)), "title": got["title"]}
