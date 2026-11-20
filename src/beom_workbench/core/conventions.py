"""기존 화면의 규칙 — 이 프로젝트의 다른 화면들이 실제로 어떻게 만들어졌는지를 코드에서 뽑는다.

새 화면·기능은 '원래 있던 사람이 만든 것처럼' 들어가야 한다. 규칙은 툴킷이 정하지 않는다 — 프로젝트가 이미 정해 두었다:
목록에서 상세로 갈 때 행을 한 번 누르는지 두 번 누르는지, 그리드는 어떤 공통 컴포넌트를 어떤 옵션으로 쓰는지, 팝업 안의
그리드는 어떻게 다른지, 확인 창은 무엇을 쓰는지. 그래서 여기서는 범주를 미리 정해 두지 않고, 여러 화면이 함께 쓰는 것
(공통 컴포넌트·함수·라이브러리 위젯)과 그것을 쓰는 방식(속성·옵션·이벤트, 팝업 안/밖)을 세어 근거(파일:줄)와 함께 낸다.

- similar(): 지금 만들 화면과 비슷한 기존 화면 — 같은 폴더, 같은 공통 부품, 이름·내용이 닮은 화면.
- differences(): 만든 뒤 — 새로 만들거나 고친 화면이 같은 부품을 다른 화면들과 다르게 쓰는 곳 (다른 곳 대부분이 쓰는 이벤트·
  옵션이 없거나, 거의 아무도 안 쓰는 이벤트를 씀, 공통 부품 대신 직접 만듦).
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from pathlib import Path

from beom_workbench.core import layout

UI_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".htm", ".jsp", ".ejs", ".hbs",
           ".njk", ".twig", ".erb", ".cshtml", ".razor", ".php", ".ftl", ".vm")
# 화면 파일 — 이것이 있어야 화면이 있는 프로젝트·작업이다. .js/.ts 는 서버·CLI 에도 있어서 화면 판단에 쓰지 않는다
# (화면 코드 분석에는 UI_EXTS 전체를 읽는다).
SCREEN_EXTS = (".jsx", ".tsx", ".vue", ".svelte", ".html", ".htm", ".jsp", ".ejs", ".hbs", ".njk", ".twig", ".erb", ".cshtml",
               ".razor", ".ftl", ".vm")
FILE_MAX, COUNT_MAX = 400_000, 3000
POPUP_RX = re.compile(r"popup|modal|dialog|layer|drawer|bottomsheet|팝업", re.I)
# 팝업을 '여는' 호출 — 이름에 팝업 낱말이 있고 닫기·숨기기·더하기가 아닌 것 (closePopup·hideModal·map.addLayer·player 는 아니다)
POPUP_CALL_RX = re.compile(r"(?i:popup|modal|dialog|drawer|bottomsheet|팝업)|(?:^|[a-z_])Layer|^layer")
POPUP_CALL_SKIP = re.compile(r"^(?i:close|hide|dismiss|remove|destroy|add|is|has|get)(?![a-z])")  # 뒤 글자는 대소문자 구분 (closePopup 의 P)

# 컴포넌트 태그: <DataGrid …>, <data-grid …>, <v-data-table …> — 앞이 글자·점·괄호면 타입 인자(Array<Foo>)라 뺀다
TAG_RX = re.compile(r"(?<![\w.$\])])<([A-Z][\w.]*|[a-z][\w]*(?:-[\w]+)+)(?=[\s/>])")
CLOSE_RX = re.compile(r"</([A-Z][\w.]*|[a-z][\w]*(?:-[\w]+)+)\s*>")
NEW_RX = re.compile(r"\bnew\s+([A-Z][\w.]*)\s*\(")
# 플러그인 위젯: $('#grid').grid({ … }) — 옵션 객체를 받는 것만 (css·attr 같은 기본 메서드 빼고)
PLUGIN_RX = re.compile(r"\$\(\s*[^()]{1,80}\)\s*\.\s*([A-Za-z]\w*)\s*\(\s*\{")
PLUGIN_SKIP = {"css", "attr", "prop", "animate", "data", "ajax", "extend", "each", "on", "off", "one", "bind", "trigger"}
ASSIGN_RX = re.compile(r"(?:\b(?:const|let|var)\s+|\bthis\.|\b)([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?(?:new\s+([A-Z][\w.]*)\s*\(|"
                       r"\$\(\s*[^()]{1,80}\)\s*\.\s*([A-Za-z]\w*)\s*\(\s*\{|([A-Za-z_$][\w$.]*)\s*\()")
SUBSCRIBE_RX = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\.\s*(?:on|bind|addEventListener|subscribe|attachEvent)\s*\(\s*['\"]([\w:.\-]+)['\"]")
DEF_RX = re.compile(r"(?:\bfunction\s+([A-Za-z_$][\w$]*)\s*\(|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
                    r"(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>))")
CALL_RX = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
IMPORT_RX = re.compile(r"import\s+(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*['\"]([^'\"]+)['\"]")
DOM_EVENTS = {"click", "dblclick", "contextmenu", "change", "input", "submit", "reset", "keydown", "keyup", "keypress", "focus",
              "blur", "mouseover", "mouseout", "mouseenter", "mouseleave", "mousedown", "mouseup", "scroll", "load", "select",
              "drag", "drop", "dragstart", "dragend", "touchstart", "touchend", "wheel", "paste", "copy", "cut"}
# 위젯 옵션의 이벤트 꼴: ondblClickRow·onSelectRow·onclick — onlyLeaf·once·selectable·rowSelection 은 이벤트가 아니다
WIDGET_EVENT = re.compile(r"^on(?:dbl)?(?:click|select|change|key|mouse|focus|blur|submit|input|load|row|cell|drag|drop|sort|page|check)",
                          re.I)


def _is_event(name: str) -> bool:
    """이벤트 이름인가 — JSX onX, Vue @x·v-on:x, Svelte on:x, Angular (x), DOM onx, 위젯 옵션 ondblClickRow 꼴."""
    if name.startswith(("@", "v-on:", "on:")) or (name.startswith("(") and name.endswith(")")):
        return True
    if re.match(r"on[A-Z]", name):
        return True
    low = name.lower()
    return low.startswith("on") and (low[2:] in DOM_EVENTS or bool(WIDGET_EVENT.match(name)))


def _event_name(name: str) -> str:
    """수식어를 뗀 이벤트 이름 — @click.stop → @click, v-on:keyup.enter → v-on:keyup."""
    return name.split(".")[0] if name.startswith(("@", "v-on:", "on:")) else name
RAW_KINDS = [  # 공통 부품을 두고 직접 만드는 흔한 꼴 — 프로젝트에 이름이 닮은 공통 부품이 여럿 쓰일 때만 알린다
    ("표", re.compile(r"<table\b", re.I), re.compile(r"grid|table|sheet|list", re.I)),
    ("팝업·대화 상자", re.compile(r"<dialog\b|\bwindow\.open\s*\(|\bwindow\.confirm\s*\(|(?<![\w.])confirm\s*\(|(?<![\w.])alert\s*\(", re.I),
     re.compile(r"modal|dialog|popup|layer|confirm|alert|message", re.I)),
]
JS_WORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "await", "super", "import", "require",
            "console", "setTimeout", "setInterval", "parseInt", "parseFloat", "String", "Number", "Boolean", "Array", "Object",
            "Promise", "JSON", "Math", "Date", "fetch", "useState", "useEffect", "useMemo", "useCallback", "useRef", "computed",
            "ref", "reactive", "watch", "defineProps", "defineEmits", "describe", "it", "test", "expect"}


# ---------------------------------------------------------------- 파일 읽기


def ui_files(target: Path, limit: int = COUNT_MAX) -> tuple[Path, list[Path]]:
    """대상 폴더가 속한 프로젝트(가장 바깥 자리)의 화면 코드 파일 — 테스트·의존성·작업 폴더는 빼고."""
    t = Path(target).resolve()
    roots = sorted({r for _, r in layout.projects(t)} or {t}, key=lambda p: len(p.parts))
    base = roots[0] if roots else t
    top = layout.repo_top(t)
    if top and not base.is_relative_to(top):
        base = t
    tests = layout.test_files(t)
    out: list[Path] = []
    for dirpath, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in layout.SKIP_DIRS and not d.startswith("."))
        for n in sorted(names):
            if not n.lower().endswith(UI_EXTS) or ".test." in n or ".spec." in n or n.endswith((".d.ts", ".min.js")):
                continue
            p = (Path(dirpath) / n).resolve()
            if str(p) in tests:
                continue
            try:
                if p.stat().st_size > FILE_MAX:
                    continue
            except OSError:
                continue
            out.append(p)
            if len(out) >= limit:
                return base, out
    return base, out


REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")
REGEX_WORDS = ("return", "typeof", "case", "in", "of", "void", "delete", "throw", "yield", "await")


def _skip(text: str, j: int) -> int:
    """text[j] 에서 글(따옴표·템플릿)·주석·정규식 리터럴이 시작하면 그 끝 다음 자리, 아니면 j.
    손으로 만든 스캐너가 /[&<>"']/g 같은 정규식의 따옴표를 글의 시작으로 읽어 구조를 놓쳤다 — 모든 스캐너가 이것을 거친다."""
    n = len(text)
    c = text[j]
    if c in "\"'`":
        k = j + 1
        while k < n:
            if text[k] == "\\":
                k += 2
                continue
            if text[k] == c:
                return k + 1
            if c != "`" and text[k] == "\n":  # 닫히지 않은 글 — 줄 끝에서 멈춘다 (한 줄 망가짐이 파일 전체로 번지지 않게)
                return k
            k += 1
        return n
    if c == "/" and j + 1 < n:
        if text[j + 1] == "/":
            k = text.find("\n", j)
            return n if k < 0 else k
        if text[j + 1] == "*":
            k = text.find("*/", j + 2)
            return n if k < 0 else k + 2
        p = j - 1
        while p >= 0 and text[p] in " \t\r\n":
            p -= 1
        word = re.search(r"([A-Za-z_$][\w$]*)$", text[max(0, p - 10):p + 1]) if p >= 0 else None
        if p < 0 or text[p] in REGEX_BEFORE or (word and word.group(1) in REGEX_WORDS):
            k, cls = j + 1, False
            while k < n and text[k] != "\n":
                ch = text[k]
                if ch == "\\":
                    k += 2
                    continue
                if ch == "[":
                    cls = True
                elif ch == "]":
                    cls = False
                elif ch == "/" and not cls:
                    k += 1
                    while k < n and text[k].isalpha():
                        k += 1
                    return k
                k += 1
            return j  # 줄 안에서 닫히지 않음 — 나눗셈으로 본다
    return j


def _tag_body(text: str, i: int) -> tuple[str, int]:
    """'<Name' 다음부터 태그 끝 '>' 까지 — 중괄호·글 안의 '>' 는 건너뛴다."""
    depth, j, n = 0, i, min(len(text), i + 4000)
    while j < n:
        k = _skip(text, j) if (depth or text[j] in "\"'`") else j
        if k != j:
            j = k
            continue
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == ">" and depth == 0:
            return text[i:j], j
        j += 1
    return text[i:n], n


def _flat(body: str) -> str:
    """속성 이름만 남게 — 중괄호 안·글 안을 지운다 (값은 _attrs 가 따로 읽는다)."""
    out, depth, j = [], 0, 0
    while j < len(body):
        c = body[j]
        if c in "\"'`" or (depth and c == "/"):
            k = _skip(body, j)
            if k != j:
                if not depth and c in "\"'`":
                    out.append(c + c)
                j = k
                continue
        if depth:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if not depth:
                    out.append("}")
        elif c == "{":
            depth = 1
            out.append("{")
        else:
            out.append(c)
        j += 1
    return "".join(out)


ATTR_RX = re.compile(r"(?:^|\s)([@:#]?[A-Za-z_][\w:.\-]*)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|\{[^{}]*\}|[^\s>]+))?")


def _attrs(body: str) -> dict[str, str | None]:
    """태그 속성 {이름: 글자 그대로의 값 | None}. 값은 따옴표 문자열·숫자·true/false 처럼 정해진 것만 남긴다 (식은 None)."""
    out: dict[str, str | None] = {}
    raw = {}
    for m in re.finditer(r"([@:#]?[A-Za-z_][\w:.\-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|\{\s*(?:-?\d+(?:\.\d+)?|true|false|null|"
                         r"'[^']*'|\"[^\"]*\")\s*\})", body):
        raw.setdefault(m.group(1), m.group(2).strip("{} \"'"))
    for m in ATTR_RX.finditer(_flat(body)):
        name = m.group(1)
        if name in out or name.startswith(("...", "/")):
            continue
        out[name] = raw.get(name)
    return out


def _match_close(text: str, open_i: int, limit: int) -> int:
    """text[open_i] 의 괄호({[( 짝이 닫히는 자리 (못 찾으면 limit 안의 끝)."""
    depth, j, n = 0, open_i, min(len(text), open_i + limit)
    while j < n:
        k = _skip(text, j)
        if k != j:
            j = k
            continue
        c = text[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


def _object_keys(text: str, i: int) -> dict[str, str | None]:
    """text[i] 가 '{' 일 때 맨 위 단계의 키 {키: 정해진 값 | None} — 옵션 객체 (그리드 설정 등)."""
    depth, j, n = 0, i, min(len(text), i + 20000)
    keys: dict[str, str | None] = {}
    seg_start = i + 1
    while j < n:
        k = _skip(text, j)
        if k != j:
            j = k
            continue
        c = text[j]
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
            if depth == 0:
                _key(text[seg_start:j], keys)
                return keys
        elif c == "," and depth == 1:
            _key(text[seg_start:j], keys)
            seg_start = j + 1
        j += 1
    return keys


def _arg_object(text: str, open_paren: int) -> int:
    """text[open_paren] 가 '(' 인 호출에서 맨 위 단계 인자 중 첫 객체 리터럴 '{' 의 자리 (없으면 -1).
    첫 인자에 괄호가 있어도 된다 — createGrid(document.getElementById('grid'), { … }) 의 옵션을 정규식으로 찾다가 놓쳤다."""
    depth, j, n = 0, open_paren, min(len(text), open_paren + 4000)
    arg_start = True
    while j < n:
        k = _skip(text, j)
        if k != j:
            j = k
            arg_start = False
            continue
        c = text[j]
        if c in "([{":
            if c == "{" and depth == 1 and arg_start:
                return j
            depth += 1
            arg_start = depth == 1  # 호출 괄호를 막 열었으면 첫 인자의 시작
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return -1
        elif c == "," and depth == 1:
            arg_start = True
        elif not c.isspace():
            arg_start = False
        j += 1
    return -1


def _popup_calls(text: str) -> list[tuple[int, int, str]]:
    """팝업을 여는 함수 호출(openPopup({…}), showModal(…), $('#x').dialog({…}), new Modal(…))의 인자 범위 [(시작, 끝, 인자 글)].
    정의(function openPopup(…))는 뺀다."""
    out = []
    for m in re.finditer(r"(?<![\w$])([A-Za-z_$][\w$.]*)\s*\(", text):
        last = m.group(1).split(".")[-1]
        if (not POPUP_CALL_RX.search(last) or POPUP_CALL_SKIP.match(last)
                or re.search(r"function\s*$", text[max(0, m.start() - 12):m.start()])):
            continue
        open_paren = m.end() - 1
        j = _match_close(text, open_paren, 8000)
        out.append((open_paren, j, text[open_paren:j]))
    return out


def _blocks(text: str) -> list[tuple[int, int]]:
    """중괄호 블록 [(열림, 닫힘)] — 글·주석·정규식 안은 건너뛴다."""
    out, stack, j, n = [], [], 0, len(text)
    while j < n:
        k = _skip(text, j)
        if k != j:
            j = k
            continue
        c = text[j]
        if c == "{":
            stack.append(j)
        elif c == "}" and stack:
            out.append((stack.pop(), j))
        j += 1
    return sorted(out)





class _Blocks:
    """중괄호 블록 묶음 — 자리를 품은 가장 안쪽 블록을 빨리 찾는다 (시작 자리로 이분 탐색 + 부모 사슬)."""

    def __init__(self, blocks: list[tuple[int, int]]):
        import bisect
        self._bisect = bisect.bisect_right
        self.blocks = blocks
        self.starts = [a for a, _ in blocks]
        self.parent = [-1] * len(blocks)
        stack: list[int] = []
        for i, (a, b) in enumerate(blocks):
            while stack and blocks[stack[-1]][1] < a:
                stack.pop()
            self.parent[i] = stack[-1] if stack else -1
            stack.append(i)

    def innermost(self, pos: int) -> tuple[int, int] | None:
        i = self._bisect(self.starts, pos) - 1
        while i >= 0:
            a, b = self.blocks[i]
            if a < pos < b:
                return (a, b)
            i = self.parent[i]
        return None


def _innermost(blocks, pos: int) -> tuple[int, int] | None:
    return (blocks if isinstance(blocks, _Blocks) else _Blocks(sorted(blocks))).innermost(pos)


def _first_arg_name(text: str, open_paren: int) -> str | None:
    """호출의 첫 인자가 이름 하나면 그 이름 (createGrid(box, …) → box)."""
    m = re.match(r"\(\s*([A-Za-z_$][\w$]*)\s*[,)]", text[open_paren:open_paren + 120])
    return m.group(1) if m else None


def _key(seg: str, keys: dict) -> None:
    """옵션 객체의 한 칸 — 'key: 값', 'key() {…}', 줄임 키 'columns', 앞에 붙은 주석은 건너뛴다. 펼침(...x)은 뺀다."""
    while True:  # 앞의 주석만 지운다 (값 안의 'http://' 같은 글은 그대로)
        s2 = seg.lstrip()
        if s2.startswith("//"):
            k = s2.find("\n")
            seg = "" if k < 0 else s2[k + 1:]
        elif s2.startswith("/*"):
            k = s2.find("*/")
            seg = "" if k < 0 else s2[k + 2:]
        else:
            seg = s2
            break
    m = re.match(r"(?:async\s+|get\s+|set\s+)?['\"]?([A-Za-z_$][\w$]*)['\"]?\s*(?::\s*(.*)|\()", seg, re.S)
    if m:
        v = (m.group(2) or "").strip()
        keys.setdefault(m.group(1), v.strip("'\"") if re.fullmatch(r"-?\d+(?:\.\d+)?|true|false|null|'[^']*'|\"[^\"]*\"", v) else None)
        return
    m = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*", seg)
    if m:
        keys.setdefault(m.group(1), None)


def _line_of(starts: list[int], pos: int) -> int:
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= pos:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _popup_ranges(text: str) -> list[tuple[int, int]]:
    """팝업 컴포넌트(<Modal> <XxxPopup> <el-dialog> …) 안쪽 자리 — 그 안에서 쓴 부품은 '팝업 안'."""
    out = []
    for m in TAG_RX.finditer(text):
        if POPUP_RX.search(m.group(1)):
            c = re.search(r"</" + re.escape(m.group(1)) + r"\s*>", text[m.end():])
            if c:
                out.append((m.end(), m.end() + c.start()))
    return out


def parse(path: Path, rel: str) -> dict:
    """한 파일 — 부품을 쓴 곳 [{name, kind, line, attrs, events, popup}], 이벤트 이름, 정의한 함수, 불러온 이름."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"uses": [], "defs": set(), "imports": {}, "events": Counter(), "text": "", "lines": []}
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    lines = text.splitlines()
    file_popup = bool(POPUP_RX.search(Path(rel).stem)) or bool(POPUP_RX.search(Path(rel).parent.name))
    pranges = _popup_ranges(text)

    pcalls = _popup_calls(text)
    blocks = _Blocks(_blocks(text))
    pblocks = {blocks.innermost(a) for a, _, _ in pcalls}
    pnames: set[str] = set()  # 팝업 호출 인자에 나온 이름들 (그릴 자리를 팝업에 넘겼나)
    for _, _, args in pcalls:
        pnames |= set(re.findall(r"(?<![\w$.])[A-Za-z_$][\w$]*", args))

    def in_popup(pos: int, open_paren: int = -1) -> bool:
        """팝업 안 — 팝업 파일, 팝업 컴포넌트 태그 안, 팝업 함수 호출의 인자 안, 또는 첫 인자(그릴 자리)를 팝업에 넘김."""
        if file_popup or any(a <= pos < b for a, b in pranges) or any(a <= pos < b for a, b, _ in pcalls):
            return True
        # 같은 블록(누르면 도는 함수 하나)에서 팝업을 열고 그 안에 그린다 — 그릴 자리가 몇 단계를 거쳐 팝업에 들어가도
        blk = blocks.innermost(pos)
        if blk is not None and blk in pblocks:
            return True
        first = _first_arg_name(text, open_paren) if open_paren >= 0 else None
        return bool(first) and first in pnames

    def ex(pos: int) -> tuple[int, str]:
        ln = _line_of(starts, pos)
        return ln, (lines[ln - 1].strip()[:110] if 0 < ln <= len(lines) else "")

    imports: dict[str, str] = {}
    for m in IMPORT_RX.finditer(text):
        if m.group(1):
            imports[m.group(1)] = m.group(3)
        for part in (m.group(2) or "").split(","):
            nm = part.split(" as ")[-1].strip()
            if nm:
                imports[nm] = m.group(3)
    uses = []
    for m in TAG_RX.finditer(text):
        name = m.group(1)
        body, _ = _tag_body(text, m.end())
        attrs = _attrs(body)
        at = {_event_name(a): ex(m.end() + max(0, body.find(a)))[0] for a in attrs}
        events = sorted({_event_name(a) for a in attrs if _is_event(a)})
        ln, code = ex(m.start())
        uses.append({"name": name, "kind": "component", "line": ln, "code": code, "events": events, "at": at,
                     "attrs": {k: v for k, v in attrs.items() if not _is_event(k)},
                     "popup": in_popup(m.start()) and not POPUP_RX.search(name)})
    var_of: dict[str, dict] = {}
    for rx, kind in ((NEW_RX, "constructor"), (PLUGIN_RX, "widget")):
        for m in rx.finditer(text):
            name = m.group(1)
            if kind == "widget" and name in PLUGIN_SKIP:
                continue
            paren = text.rfind("(", m.start(), m.end())
            brace = _arg_object(text, paren) if paren >= 0 else -1
            opts = _object_keys(text, brace) if brace >= 0 else {}
            events = sorted(k for k in opts if _is_event(k))
            ln, code = ex(m.start())
            at = {k: ex(text.find(k, brace) if brace >= 0 and text.find(k, brace) >= 0 else m.start())[0] for k in opts}
            u = {"name": name if kind == "constructor" else "$." + name, "kind": kind, "line": ln, "code": code, "at": at,
                 "events": events, "attrs": {k: v for k, v in opts.items() if k not in events}, "popup": in_popup(m.start(), paren)}
            uses.append(u)
    defs = {a or b for a, b in DEF_RX.findall(text)}
    # 변수에 담은 부품에 나중에 단 이벤트: const grid = new Grid(…); grid.on('dblclick', …)
    for m in ASSIGN_RX.finditer(text):
        var, ctor, plugin, call = m.groups()
        name = ctor or ("$." + plugin if plugin and plugin not in PLUGIN_SKIP else None)
        if name:
            ln = _line_of(starts, m.start())
            var_of[var] = next((u for u in uses if u["name"] == name and u["line"] == ln), None) or var_of.get(var)
    evs: Counter = Counter()
    ev_ex: dict[str, tuple[int, str]] = {}
    for m in SUBSCRIBE_RX.finditer(text):
        var, ev = m.groups()
        u = var_of.get(var)
        if u is not None and ev not in u["events"]:
            u["events"] = sorted({*u["events"], ev})
            u.setdefault("at", {})[ev] = _line_of(starts, m.start())
        evs[ev] += 1
        ev_ex.setdefault(ev, ex(m.start()))
    for m in re.finditer(r"\b(?:on|v-on:|@|on:)([a-z]+)\s*=|\bon([A-Z]\w+)\s*=", text):
        ev = (m.group(1) or m.group(2) or "").lower()
        if ev:
            evs[ev] += 1
            ev_ex.setdefault(ev, ex(m.start()))
    call_at: dict[str, list[int]] = {}
    for m in CALL_RX.finditer(text):
        c = m.group(1)
        if c in JS_WORDS or re.search(r"\bfunction\s+$", text[max(0, m.start() - 12):m.start()]):
            continue
        call_at.setdefault(c, []).append(m.start())
    calls = Counter({c: len(v) for c, v in call_at.items()})
    return {"uses": uses, "defs": defs, "imports": imports, "events": evs, "event_ex": ev_ex, "calls": calls, "call_at": call_at,
            "in_popup": in_popup, "starts": starts, "text": text, "lines": lines}


# ---------------------------------------------------------------- 모으기


def _local_function_uses(parsed: dict[str, dict]) -> None:
    """프로젝트가 직접 만든 함수 중 여러 화면이 부르는 것 (openPopup·createGrid 같은 공통 함수) — 부른 곳을 쓰임으로 더한다."""
    defined: dict[str, str] = {}
    def_count: Counter = Counter()
    for rel, p in parsed.items():
        for d in p["defs"]:
            defined.setdefault(d, rel)
            def_count[d] += 1
    callers = Counter()
    for rel, p in parsed.items():
        for name in p["calls"]:
            if name in defined and defined[name] != rel:
                callers[name] += 1
    # 공통 함수: 한 곳에서 정의하고 여러 화면이 부르는 것. 화면마다 같은 이름으로 따로 정의한 함수(fn_search)는 공통이 아니다.
    shared = {name for name, n in callers.items() if n >= 2 and len(name) >= 4 and def_count[name] == 1}
    for rel, p in parsed.items():
        text, starts = p["text"], p["starts"]
        for name in shared & set(p["call_at"]):
            if defined[name] == rel:
                continue
            for pos in p["call_at"][name]:
                open_paren = text.find("(", pos)
                # 인자 중 객체 리터럴이 있으면 그 키를 옵션으로 (첫 인자가 식이어도)
                ob = _arg_object(text, open_paren) if open_paren >= 0 else -1
                opts = _object_keys(text, ob) if ob >= 0 else {}
                events = sorted(x for x in opts if _is_event(x))
                ln = _line_of(starts, pos)
                p["uses"].append({"name": name + "()", "kind": "function", "line": ln, "source": defined[name],
                                  "code": p["lines"][ln - 1].strip()[:110] if 0 < ln <= len(p["lines"]) else "",
                                  "events": events, "attrs": {x: v for x, v in opts.items() if x not in events},
                                  "popup": p["in_popup"](pos, open_paren)})


def collect(target: Path, files: list[Path] | None = None) -> tuple[Path, dict[str, dict]]:
    base, all_files = ui_files(target)
    t = Path(target).resolve()
    parsed = {os.path.relpath(p, t).replace("\\", "/"): parse(p, os.path.relpath(p, t).replace("\\", "/"))
              for p in (files if files is not None else all_files)}
    _local_function_uses(parsed)
    return base, parsed


def _shared(parsed: dict[str, dict], exclude: set[str] = frozenset(), min_files: int = 2) -> dict[str, dict]:
    """여러 파일이 쓰는 부품 {이름: {files, uses: [...], source}}."""
    by: dict[str, dict] = {}
    for rel, p in parsed.items():
        if rel in exclude:
            continue
        for u in p["uses"]:
            ent = by.setdefault(u["name"], {"files": set(), "uses": [], "kind": u["kind"], "source": None})
            ent["files"].add(rel)
            ent["uses"].append({**u, "file": rel, "_lines": _LineView(p["lines"])})
            src = u.get("source") or p["imports"].get(u["name"].split(".")[0])
            if src and not ent["source"]:
                ent["source"] = src
    return {k: v for k, v in by.items() if len(v["files"]) >= min_files}


class _LineView:
    """줄 번호 → 그 줄 코드 (근거 표시용, 파일 내용을 복사하지 않고)."""
    __slots__ = ("lines",)

    def __init__(self, lines: list[str]):
        self.lines = lines

    def get(self, ln: int, default: str = "") -> str:
        return self.lines[ln - 1].strip()[:110] if 0 < ln <= len(self.lines) else default


def _profile(uses: list[dict]) -> dict:
    """쓰임 목록 → 이벤트·속성이 몇 곳에서 쓰였는지, 정해진 값이 무엇인지."""
    n = len(uses)
    ev = Counter(e for u in uses for e in set(u["events"]))
    at = Counter(a for u in uses for a in u["attrs"])
    vals: dict[str, Counter] = {}
    for u in uses:
        for a, v in u["attrs"].items():
            if v is not None:
                vals.setdefault(a, Counter())[v] += 1
    return {"n": n, "events": ev, "attrs": at, "values": vals}


def _ex(u: dict, key: str | None = None) -> str:
    """근거 한 줄 — key(이벤트·옵션)가 있으면 그것이 적힌 줄을."""
    ln = (u.get("at") or {}).get(key) if key else None
    if ln and ln != u["line"]:
        return f"{u['file']}:{ln} {u.get('_lines', {}).get(ln, '')}".rstrip()
    return f"{u['file']}:{u['line']} {u['code']}"


def scan(target: Path, near: list[str] | None = None, like: str = "") -> dict:
    """이 프로젝트의 화면 규칙 — 여러 화면이 함께 쓰는 부품과 그 쓰는 방식(팝업 안/밖), 이벤트 이름, 비슷한 기존 화면."""
    t = Path(target).resolve()
    base, parsed = collect(t)
    shared = _shared(parsed)
    items = []
    for name, ent in sorted(shared.items(), key=lambda kv: (-len(kv[1]["files"]), kv[0]))[:20]:
        ctx = {}
        for label, sel in (("화면", [u for u in ent["uses"] if not u["popup"]]), ("팝업 안", [u for u in ent["uses"] if u["popup"]])):
            if not sel:
                continue
            pr = _profile(sel)
            ctx[label] = {
                "uses": pr["n"],
                "events": [f"{e} {c}/{pr['n']}" for e, c in pr["events"].most_common(8)],
                "options": [f"{a}{'=' + pr['values'][a].most_common(1)[0][0] if a in pr['values'] and pr['values'][a].most_common(1)[0][1] * 2 > c else ''} {c}/{pr['n']}"
                            for a, c in pr["attrs"].most_common(12)],
                "examples": [_ex(u) for u in sel[:3]],
            }
        items.append({"name": name, "kind": ent["kind"], "files": len(ent["files"]), **({"source": ent["source"]} if ent["source"] else {}),
                      "how": ctx})
    evs: Counter = Counter()
    ev_files: Counter = Counter()
    ev_ex: dict[str, str] = {}
    for rel, p in parsed.items():
        for e, c in p["events"].items():
            evs[e] += c
            ev_files[e] += 1
            if e not in ev_ex and e in p["event_ex"]:
                ln, code = p["event_ex"][e]
                ev_ex[e] = f"{rel}:{ln} {code}"
    events = [{"event": e, "files": ev_files[e], "example": ev_ex.get(e, "")} for e, _ in ev_files.most_common(15)]
    return {"root": str(base), "files": len(parsed), "shared": items, "events": events,
            "similar": similar(t, parsed, shared, near or [], like)}


def similar(t: Path, parsed: dict[str, dict], shared: dict[str, dict], near: list[str], like: str, top: int = 6) -> list[dict]:
    """지금 만들·고칠 화면과 비슷한 기존 화면 — 같은 폴더(+3), 같은 공통 부품(+2씩), 이름에 말이 들어감(+4), 내용에 들어감(+1)."""
    near_rel = []
    for n in near:
        try:
            near_rel.append(os.path.relpath((t / n).resolve(), t).replace("\\", "/"))
        except ValueError:  # 다른 드라이브 — 이 프로젝트의 파일이 아니다
            continue
    near_dirs = {str(Path(n).parent) for n in near_rel}
    near_parts = set()
    for n in near_rel:
        if n in parsed:
            near_parts |= {u["name"] for u in parsed[n]["uses"] if u["name"] in shared}
    words = [w.lower() for w in re.split(r"[\s,/]+", like or "") if len(w) >= 2]
    rows = []
    for rel, p in parsed.items():
        if rel in near_rel:
            continue
        parts = sorted({u["name"] for u in p["uses"] if u["name"] in shared})
        if not parts and not words:
            continue
        score = 3 * (str(Path(rel).parent) in near_dirs) + 2 * len(near_parts & set(parts))
        low = rel.lower()
        score += sum(4 for w in words if w in low) + sum(1 for w in words if w in p["text"].lower())
        if not near and not words:
            score = len(parts)
        if score <= 0:
            continue
        ev = sorted({e for u in p["uses"] if u["name"] in shared for e in u["events"]})
        rows.append({"file": rel, "score": score, "uses": parts[:8], "events": ev[:10]})
    rows.sort(key=lambda r: (-r["score"], r["file"]))
    return rows[:top]


# ---------------------------------------------------------------- 만든 뒤: 다른 화면들과 다른 곳


def changed_ui(target: Path, base: dict, since: float | None = None) -> list[str]:
    """원래 소스 기록과 비교해 새로 생기거나 바뀐 화면 코드 (대상 폴더 기준 경로).
    기록에 없는 파일이 모두 새 파일은 아니다 — 기록은 파일 수·크기 한도에서 끊기고, 예전 기록에는 화면 틀(HTML·JSP)이 없다.
    since(기록한 시각)가 있으면 기록에 없더라도 그 전부터 그대로 있던 파일은 새 파일로 보지 않는다."""
    t = Path(target).resolve()
    _, files = ui_files(t)
    out = []
    for p in files:
        rel = os.path.relpath(p, t).replace("\\", "/")
        if "/.beom/" in "/" + rel:
            continue
        ent = base.get(rel)
        if ent is None:
            try:
                if since is not None and p.stat().st_mtime < since:
                    continue  # 기록 밖이지만 작업 전부터 그대로 있던 파일
            except OSError:
                continue
            out.append(rel)
            continue
        try:
            if hashlib.sha256(p.read_bytes()).hexdigest() != ent.get("sha"):
                out.append(rel)
        except OSError:
            continue
    return out


def differences(target: Path, base: dict, min_refs: int = 3, since: float | None = None) -> list[dict]:
    """새로 만들거나 고친 화면이 같은 부품을 다른 화면들과 다르게 쓰는 곳. 기준은 다른 화면들의 실제 쓰임 —
    다수(60% 이상)가 쓰는 이벤트가 없음, 거의 아무도(10% 이하) 안 쓰는 이벤트를 씀, 다수(80% 이상)가 같은 값으로 두는 옵션이
    없거나 다름, 공통 부품을 여러 화면이 쓰는데 직접 만듦. 고친 파일은 원래 있던 쓰임과 다른 것만 (원래부터 그랬던 건 빼고)."""
    t = Path(target).resolve()
    changed = set(changed_ui(t, base, since))
    if not changed or not base:
        return []
    # 원래 내용을 기록하지 못한(큰) 파일은 원래부터 있던 차이와 이번 작업의 차이를 가를 수 없다 — 탓하지 않는다
    unknown_old = {rel for rel in changed if rel in base and "text" not in base[rel]}
    _, parsed = collect(t)
    refs = _shared(parsed, exclude=changed, min_files=1)
    old_uses: dict[str, set] = {}
    for rel in changed:  # 고친 파일의 원래 쓰임 — 원래부터 있던 차이는 이번 작업 탓이 아니다
        ent = base.get(rel) or {}
        if "text" in ent:
            tmp = parse_text(ent["text"], rel)
            old_uses[rel] = {(u["name"], tuple(u["events"]), tuple(sorted(u["attrs"].items(), key=str))) for u in tmp["uses"]}
    out = []
    for rel in sorted(changed - unknown_old):
        p = parsed.get(rel)
        if not p:
            continue
        for u in p["uses"]:
            sig = (u["name"], tuple(u["events"]), tuple(sorted(u["attrs"].items(), key=str)))
            if sig in old_uses.get(rel, set()):
                continue
            ref = refs.get(u["name"])
            if not ref:
                continue
            # 같은 자리(화면/팝업 안)끼리만 — 팝업 안 그리드를 화면 그리드와 견주면 '다르다'가 규칙이 아니라 자리 차이다.
            # 같은 자리의 쓰임이 적으면(규칙이라 할 근거가 모자라면) 견주지 않는다.
            pool = [r for r in ref["uses"] if r["popup"] == u["popup"]]
            if len(pool) < min_refs or len({r["file"] for r in pool}) < 2:
                continue
            pr = _profile(pool)
            n = pr["n"]
            where = "팝업 안 " if u["popup"] else ""
            here = f"{rel}:{u['line']}"
            for e, c in pr["events"].items():
                if c / n >= 0.6 and e not in u["events"]:
                    out.append(_finding(here, u, f"{where}{u['name']} — 다른 곳 {c}/{n} 은 {e} 를 쓰는데 여기는 없음",
                                        [r for r in pool if e in r["events"]], e))
            for e in u["events"]:
                c = pr["events"].get(e, 0)
                if c / n <= 0.1:
                    common = ", ".join(x for x, _ in pr["events"].most_common(3)) or "이벤트 없음"
                    out.append(_finding(here, u, f"{where}{u['name']} — {e} 는 다른 곳 {c}/{n} 만 씀 (다른 곳: {common})", pool))
            for a, c in pr["attrs"].items():
                if c / n < 0.8:
                    continue
                vals = pr["values"].get(a)
                top_v = vals.most_common(1)[0] if vals else None
                if a not in u["attrs"]:
                    out.append(_finding(here, u, f"{where}{u['name']} — 다른 곳 {c}/{n} 은 {a}{'=' + top_v[0] if top_v and top_v[1] / n >= 0.8 else ''}"
                                                 f" 를 두는데 여기는 없음", [r for r in pool if a in r["attrs"]], a))
                elif top_v and top_v[1] / n >= 0.8 and u["attrs"][a] is not None and u["attrs"][a] != top_v[0]:
                    out.append(_finding(here, u, f"{where}{u['name']} — {a} 가 다른 곳 {top_v[1]}/{n} 은 {top_v[0]}, 여기는 {u['attrs'][a]}",
                                        [r for r in pool if r["attrs"].get(a) == top_v[0]], a))
        # 공통 부품 대신 직접 만듦
        names = {u["name"] for u in p["uses"]}
        old_text = (base.get(rel) or {}).get("text", "")
        for label, raw_rx, name_rx in RAW_KINDS:
            hits = [m for m in raw_rx.finditer(p["text"])]
            if not hits or (old_text and len(raw_rx.findall(old_text)) >= len(hits)):
                continue
            cands = [(k, v) for k, v in refs.items() if name_rx.search(k) and len(v["files"]) >= min_refs and k not in names]
            if not cands:
                continue
            k, v = max(cands, key=lambda kv: len(kv[1]["files"]))
            raw_files = sum(1 for r2, q in parsed.items() if r2 not in changed and raw_rx.search(q["text"]))
            if raw_files >= len(v["files"]):
                continue  # 직접 만드는 곳도 많은 프로젝트 — 규칙이라 보기 어렵다
            ln = p["text"].count("\n", 0, hits[0].start()) + 1
            out.append({"at": f"{rel}:{ln}", "summary": f"{label}를 직접 만듦 — 다른 화면 {len(v['files'])}곳은 공통 {k} 를 씀"
                                                        f" (직접 만든 곳 {raw_files}곳)",
                        "evidence": [_ex(x) for x in v["uses"][:3]]})
    seen, uniq = set(), []
    for f in out:
        if (f["at"], f["summary"]) not in seen:
            seen.add((f["at"], f["summary"]))
            uniq.append(f)
    return uniq[:30]


def _finding(here: str, u: dict, summary: str, refs: list[dict], key: str | None = None) -> dict:
    return {"at": here, "summary": summary, "evidence": [_ex(r, key) for r in refs[:3]]}


def parse_text(text: str, rel: str) -> dict:
    """기록해 둔 원래 내용을 같은 방식으로 읽는다."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / Path(rel).name
        f.write_text(text, encoding="utf-8")
        return parse(f, rel)


# ---------------------------------------------------------------- 계획의 근거 확인


def check_evidence(target: Path, refs: list[str]) -> tuple[list[str], list[str]]:
    """'파일:줄' 근거가 실제로 있는 기존 코드인지 — (맞는 것, 틀린 것). 경로는 대상 폴더 또는 저장소 맨 위 기준."""
    t = Path(target).resolve()
    top = layout.repo_top(t) or t
    ok, bad = [], []
    for r in refs:
        m = re.match(r"\s*(.+?)(?::(\d+)(?:[-:]\d+)?|#L(\d+)(?:-L?\d+)?)(?:\s.*)?$", str(r))
        if not m:
            bad.append(f"{r} — '파일:줄' 꼴이 아님")
            continue
        rel, line = m.group(1).strip(), int(m.group(2) or m.group(3))
        rel = rel[2:] if rel.startswith(("./", ".\\")) else rel
        f = next((c for c in ((t / rel).resolve(), (top / rel).resolve()) if c.is_file() and c.is_relative_to(top)), None)
        if f is None:
            bad.append(f"{r} — 그런 파일이 없음")
            continue
        try:
            n = len(f.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            n = 0
        if not 1 <= line <= n:
            bad.append(f"{r} — 줄 번호가 파일 밖 (1~{n})")
            continue
        ok.append(f"{os.path.relpath(f, t).replace(chr(92), '/')}:{line}")
    return ok, bad


def has_screens(target: Path, at_least: int = 3, before: float | None = None) -> bool:
    """기존 화면이 여럿 있는 프로젝트인가 (그때만 '기존 규칙 따름' 근거를 요구한다).
    before: 이 시각(작업 시작) 전에 있던 파일만 센다 — 이번 작업이 새로 만든 화면은 '기존'이 아니다.
    화면 파일(HTML·JSX·Vue·템플릿…)만 센다 — .js/.ts 만 있는 서버·CLI 는 화면이 있는 프로젝트가 아니다."""
    _, files = ui_files(target)
    n = 0
    for f in files:
        if not f.name.lower().endswith(SCREEN_EXTS):
            continue
        try:
            if before is None or f.stat().st_mtime < before:
                n += 1
        except OSError:
            continue
        if n >= at_least:
            return True
    return False
