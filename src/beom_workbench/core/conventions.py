"""기존 화면의 규칙 — 이 프로젝트의 다른 화면들이 실제로 어떻게 만들어졌는지를 코드에서 뽑는다.

새 화면·기능은 '원래 있던 사람이 만든 것처럼' 들어가야 한다. 규칙은 툴킷이 정하지 않는다 — 프로젝트가 이미 정해 두었다:
목록에서 상세로 갈 때 행을 한 번 누르는지 두 번 누르는지, 그리드는 어떤 공통 컴포넌트를 어떤 옵션으로 쓰는지, 팝업 안의
그리드는 어떻게 다른지, 확인 창은 무엇을 쓰는지. 그래서 여기서는 범주를 미리 정해 두지 않고, 여러 화면이 함께 쓰는 것
(공통 컴포넌트·함수·라이브러리 위젯)과 그것을 쓰는 방식(속성·옵션·이벤트, 팝업 안/밖)을 세어 근거(파일:줄)와 함께 낸다.

- similar(): 지금 만들 화면과 비슷한 기존 화면 — 같은·옆 폴더, 같은 이름 꼴(*List.vue · list.html), 같은 공통 부품, 이름·내용이
  닮은 화면 (만들 파일이 아직 없어도, 요청 낱말이 한국어여도).
- parts()·unstyled(): 쓰는 곳이 한 곳뿐인 부품까지 받는 옵션(props·기본값)과 함께, 그리고 규칙이 없는 클래스.
- differences(): 만든 뒤 — 새로 만들거나 고친 화면이 같은 부품을 다른 화면들과 다르게 쓰는 곳 (다른 곳 대부분이 쓰는 이벤트·
  옵션이 없거나, 거의 아무도 안 쓰는 이벤트를 씀, 공통 부품 대신 직접 만듦).
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
from collections import Counter
from pathlib import Path

from beom_workbench.core import layout

UI_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".htm", ".jsp", ".ejs", ".hbs",
           ".njk", ".twig", ".erb", ".cshtml", ".razor", ".php", ".ftl", ".vm", ".j2", ".jinja", ".jinja2", ".mustache", ".liquid")
# 화면 파일 — 이것이 있어야 화면이 있는 프로젝트·작업이다. .js/.ts 는 서버·CLI 에도 있어서 화면 판단에 쓰지 않는다
# (화면 코드 분석에는 UI_EXTS 전체를 읽는다).
SCREEN_EXTS = (".jsx", ".tsx", ".vue", ".svelte", ".html", ".htm", ".jsp", ".ejs", ".hbs", ".njk", ".twig", ".erb", ".cshtml",
               ".razor", ".ftl", ".vm", ".j2", ".jinja", ".jinja2", ".mustache", ".liquid")
FILE_MAX, COUNT_MAX = 400_000, 3000
POPUP_RX = re.compile(r"popup|modal|dialog|layer|drawer|bottomsheet|팝업", re.I)
# 팝업을 '여는' 호출 — 이름에 팝업 낱말이 있고 닫기·숨기기·더하기가 아닌 것 (closePopup·hideModal·map.addLayer·player 는 아니다)
POPUP_CALL_RX = re.compile(r"(?i:popup|modal|dialog|drawer|bottomsheet|팝업)|(?:^|[a-z_])Layer|^layer")
POPUP_CALL_SKIP = re.compile(r"^(?i:close|hide|dismiss|remove|destroy|add|is|has|get)(?![a-z])")  # 뒤 글자는 대소문자 구분 (closePopup 의 P)

# 컴포넌트 태그: <DataGrid …>, <data-grid …>, <v-data-table …> — 앞이 글자·점·괄호면 타입 인자(Array<Foo>)라 뺀다
TAG_RX = re.compile(r"(?<![\w.$\])])<([A-Z][\w.]*|[a-z][\w]*(?:-[\w]+)+|[a-z]\w*:[A-Za-z][\w.\-]*)(?=[\s/>])")
TAG_PREFIX_SKIP = {"c", "fn", "fmt", "sql", "x", "jsp", "th", "svelte", "xsl", "xlink", "xml", "v", "o", "w", "spring", "form", "sec",
                   "tiles", "layout", "sj", "s"}  # 프레임워크가 주는 태그 라이브러리 (프로젝트의 공통 부품이 아니다)
# 공통 조각을 끼워 넣는 꼴 — 화면들이 같은 조각(검색 영역·그리드·페이지 이동)을 쓰는 것도 규칙이다
INCLUDE_RXS = [
    re.compile(r"<%@\s*include\s+file\s*=\s*[\"']([^\"']+)"),                                   # JSP 정적 include
    re.compile(r"<jsp:include\s+page\s*=\s*[\"']([^\"'?]+)"),                                    # JSP 동적 include
    re.compile(r"\{%-?\s*(?:include|extends|embed)\s+[\"']([^\"']+)[\"']"),                       # Jinja·Django·Twig·Nunjucks
    re.compile(r"\{\{>\s*([\w./-]+)"),                                                         # Handlebars·Mustache 조각
    re.compile(r"th:(?:replace|insert|include)\s*=\s*[\"']~?\{?\s*([\w./-]+(?:\s*::\s*[\w-]+)?)"),  # Thymeleaf
    re.compile(r"<%[-=]?\s*include\(?\s*[\"']([^\"']+)"),                                      # EJS
    re.compile(r"Html\.(?:Render)?Partial(?:Async)?\(\s*\"([^\"]+)\"|<partial\s+name\s*=\s*\"([^\"]+)\""),  # Razor
    re.compile(r"\b(?:include|require)(?:_once)?\s*\(?\s*[\"']([^\"']+\.php)"),                   # PHP
    re.compile(r"<#include\s+[\"']([^\"']+)|#parse\s*\(\s*[\"']([^\"']+)"),                      # FreeMarker·Velocity
    re.compile(r"layout:decorate\s*=\s*[\"']~?\{?\s*([\w./-]+)"),                                # Thymeleaf 레이아웃
    re.compile(r"\bLayout\s*=\s*\"([^\"]+)\""),                                                 # Razor 레이아웃
    re.compile(r"@(?:include|extends|component)\s*\(\s*[\"']([\w./-]+)[\"']"),                    # Blade
    re.compile(r"\{\{-?\s*include\s*\(\s*[\"']([^\"']+)[\"']"),                                  # Twig include()
    re.compile(r"\{%-?\s*render\s+[\"']([^\"']+)[\"']"),                                          # Liquid
    re.compile(r"<%[-=]?\s*include\s+([\w./-]+)\s*%>"),                                            # EJS 옛 문법
    re.compile(r"<tiles:insertAttribute\s+name\s*=\s*[\"']([^\"']+)"),                              # Apache Tiles
    re.compile(r"\{\{#>\s*([\w./-]+)"),                                                          # Handlebars 레이아웃 조각
]
# 템플릿 매크로 정의 (Jinja·Twig·Nunjucks) — 공통 함수처럼 여러 화면이 부른다
MACRO_RX = re.compile(r"\{%-?\s*macro\s+(\w+)\s*\(")
MACRO_ALIAS_RX = re.compile(r"\{%-?\s*import\s+[\"'][^\"']+[\"']\s+as\s+(\w+)")
TEMPLATE_EXTS = (".html", ".htm", ".j2", ".jinja", ".jinja2", ".njk", ".twig", ".jsp", ".ftl", ".vm", ".hbs", ".mustache",
                 ".liquid", ".ejs", ".erb", ".cshtml", ".razor", ".php")


def _include_name(path: str) -> str:
    """끼워 넣은 조각 이름 — 경로 꼴이 화면마다 달라도(../partials/x.html, /WEB-INF/views/partials/x.jsp) 같은 조각은 같게:
    마지막 두 마디, 확장자와 Razor·Rails 의 앞 밑줄은 뺀다."""
    p = path.strip().replace("\\", "/")
    frag = ""
    if "::" in p:
        p, frag = (x.strip() for x in p.split("::", 1))
    parts = [x for x in p.split("/") if x and x not in (".", "..")]
    parts = parts[-2:] if len(parts) >= 2 else parts
    if parts:
        parts[-1] = re.sub(r"\.[A-Za-z0-9]+$", "", parts[-1]).lstrip("_")
    return "include:" + "/".join(parts) + (f" :: {frag}" if frag else "")
NEW_RX = re.compile(r"\bnew\s+([A-Z][\w.]*)\s*\(")
# 플러그인 위젯: $('#grid').grid({ … }) — 옵션 객체를 받는 것만 (css·attr 같은 기본 메서드 빼고)
PLUGIN_RX = re.compile(r"\$\(\s*[^()]{1,80}\)\s*\.\s*([A-Za-z]\w*)\s*\(\s*\{")
PLUGIN_SKIP = {"css", "attr", "prop", "animate", "data", "ajax", "extend", "each", "on", "off", "one", "bind", "trigger"}
ASSIGN_RX = re.compile(r"(?:\b(?:const|let|var)\s+|\bthis\.|\b)([A-Za-z_$][\w$]*)\s*=\s*(?:await\s+)?(?:new\s+([A-Z][\w.]*)\s*\(|"
                       r"\$\(\s*[^()]{1,80}\)\s*\.\s*([A-Za-z]\w*)\s*\(\s*\{|([A-Za-z_$][\w$.]*)\s*\()")
SUBSCRIBE_RX = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\.\s*(?:on|bind|addEventListener|subscribe|attachEvent)\s*\(\s*['\"]([\w:.\-]+)['\"]")
DEF_RX = re.compile(r"(?:\bfunction\s+([A-Za-z_$][\w$]*)\s*\(|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;\n]{1,80})?=\s*(?:async\s*)?"
                    r"(?:function\b|(?:<[^<>()=;]{1,60}>\s*)?\([^()]*\)\s*(?::\s*[^=;{}()]{1,80}?)?\s*=>|[A-Za-z_$][\w$]*\s*=>))")
CALL_RX = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
IMPORT_RX = re.compile(r"import\s+(?:type\s+)?(?:(\w+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from\s*['\"]([^'\"]+)['\"]")
# 언어·런타임이 주는 생성자 — 프로젝트의 규칙(공통 부품)이 아니다
BUILTIN_CTORS = {"Date", "Map", "Set", "WeakMap", "WeakSet", "Error", "TypeError", "RangeError", "SyntaxError", "Promise", "URL",
                 "URLSearchParams", "RegExp", "Blob", "File", "FileReader", "FormData", "Headers", "Request", "Response", "Image",
                 "Audio", "Array", "Object", "Function", "Proxy", "Intl.NumberFormat", "Intl.DateTimeFormat", "Intl.Collator",
                 "XMLHttpRequest", "Worker", "WebSocket", "EventSource", "AbortController", "MutationObserver", "ResizeObserver",
                 "IntersectionObserver", "Event", "CustomEvent", "TextEncoder", "TextDecoder", "Uint8Array", "ArrayBuffer",
                 "DataView", "Notification", "BroadcastChannel", "DOMParser", "Option", "Symbol", "BigInt", "Number", "String",
                 "Boolean", "Buffer"}
# 프레임워크 기본 함수를 내주는 곳 — 이것을 부르는 건 프로젝트의 공통 함수가 아니다
FRAMEWORK_SOURCES = re.compile(r"^(?:vue|react|react-dom(?:/client)?|preact(?:/hooks)?|svelte(?:/\w+)?|solid-js|@angular/core|vitest|jest|"
                               r"@jest/globals|node:.*|fs|path|os|util|assert|events|url|crypto|child_process)$")
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
            p = Path(dirpath) / n  # base 가 이미 resolve 됨 — 파일마다 resolve 하면 큰 저장소에서 느리다
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
    # 마크업으로 만든 팝업 — <div class="modal">, <div id="layerPopup">, <dialog> (부트스트랩·jQuery·서버 템플릿 화면)
    for m in HTML_POPUP_RX.finditer(text):
        tag = (m.group(1) or m.group(2)).lower()
        depth, j = 1, m.end()
        for t in re.finditer(r"<(/?)" + tag + r"\b[^>]*?(/?)>", text[m.end():], re.I):
            if t.group(2):
                continue
            depth += -1 if t.group(1) else 1
            if depth == 0:
                j = m.end() + t.start()
                break
        else:
            j = len(text)
        out.append((m.end(), j))
    return out


HTML_POPUP_RX = re.compile(r"<(div|section|aside|form)\b(?=[^>]*\b(?:class|id)\s*=\s*[\"'][^\"']*"
                           r"(?:modal|popup|pop-|layer|dialog|팝업))[^>]*>|<(dialog)\b[^>]*>", re.I)


_PARSED: dict[tuple, dict] = {}  # (경로, 이름, 수정 시각, 크기) → 결과 — 도구 서버는 오래 살아 한 실행에서 같은 파일을 여러 번 읽는다


def parse(path: Path, rel: str) -> dict:
    """한 파일 — 부품을 쓴 곳 [{name, kind, line, attrs, events, popup}], 이벤트 이름, 정의한 함수, 불러온 이름.
    바뀌지 않은 파일은 앞서 읽은 결과를 쓴다 (uses 는 뒤에서 덧붙이므로 목록만 새로)."""
    try:
        st = os.stat(path)
        key = (str(path), rel, st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    got = _PARSED.get(key) if key else None
    if got is None:
        got = _parse(path, rel)
        if key:
            if len(_PARSED) > 5000:
                _PARSED.clear()
            _PARSED[key] = got
    return {**got, "uses": list(got["uses"])}


def _parse(path: Path, rel: str) -> dict:
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

    if rel.lower().endswith(TEMPLATE_EXTS) or "{%" in text:
        # 템플릿 매크로 별칭 호출 — {% import "macros/ui.html" as ui %} … {{ ui.data_table(…) }} 을 data_table( 로 센다
        aliases = set(MACRO_ALIAS_RX.findall(text))
        if aliases:
            text_calls = [(m.start(2), m.group(2)) for m in re.finditer(r"(?<![\w$.])(" + "|".join(map(re.escape, aliases))
                                                                        + r")\.(\w+)\s*\(", text)]
        else:
            text_calls = []
    else:
        text_calls = []

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
        if ":" in name and name.split(":")[0] in TAG_PREFIX_SKIP:
            continue
        body, _ = _tag_body(text, m.end())
        attrs = _attrs(body)
        at = {_event_name(a): ex(m.end() + max(0, body.find(a)))[0] for a in attrs}
        events = sorted({_event_name(a) for a in attrs if _is_event(a)})
        ln, code = ex(m.start())
        uses.append({"name": name, "kind": "component", "line": ln, "code": code, "events": events, "at": at,
                     "attrs": {k: v for k, v in attrs.items() if not _is_event(k)},
                     "popup": in_popup(m.start()) and not POPUP_RX.search(name)})
    for rx in INCLUDE_RXS:
        for m in rx.finditer(text):
            target = next((g for g in m.groups() if g), "")
            if not target or "${" in target or "{{" in target:
                continue  # 이름을 식으로 만든 조각은 무엇인지 모른다
            ln, code = ex(m.start())
            uses.append({"name": _include_name(target), "kind": "include", "line": ln, "code": code, "events": [], "at": {},
                         "attrs": {}, "popup": in_popup(m.start())})
    var_of: dict[str, dict] = {}
    for rx, kind in ((NEW_RX, "constructor"), (PLUGIN_RX, "widget")):
        for m in rx.finditer(text):
            name = m.group(1)
            if kind == "widget" and name in PLUGIN_SKIP:
                continue
            if kind == "constructor" and name in BUILTIN_CTORS and name not in imports:
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
    defs = {a or b for a, b in DEF_RX.findall(text)} | set(MACRO_RX.findall(text))
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
    for m in re.finditer(r"\)\s*\.\s*(?:on|one|bind|delegate|live)\s*\(\s*['\"]([\w:.\- ]+)['\"]", text):
        for ev in m.group(1).split():
            ev = ev.split(".")[0]
            if ev:
                evs[ev] += 1
                ev_ex.setdefault(ev, ex(m.start()))
    for m in re.finditer(r"\)\s*\.\s*(click|dblclick|change|submit|keydown|keyup|keypress|focus|blur|mouseenter|mouseleave|"
                         r"contextmenu)\s*\(\s*(?=function\b|\(|[A-Za-z_$])", text):
        evs[m.group(1)] += 1
        ev_ex.setdefault(m.group(1), ex(m.start()))
    # onclick= · onClick= · @click= · @row-dblclick.stop= · v-on:keyup.enter= · on:click= (@ 앞은 낱말 경계가 아니다)
    for m in re.finditer(r"(?:\b(?:on|v-on:|on:)|(?<![\w@])@)([a-z][\w-]*)(?:\.[\w.]+)?\s*=(?!=)|\bon([A-Z]\w+)\s*=(?!=)", text):
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
    for pos, c in text_calls:
        call_at.setdefault(c, []).append(pos)
    for c in set(MACRO_RX.findall(text)):  # {% macro data_table( %} 는 정의지 부름이 아니다
        call_at[c] = [p for p in call_at.get(c, []) if not re.search(r"macro\s+$", text[max(0, p - 12):p])]
        if not call_at[c]:
            del call_at[c]
    calls = Counter({c: len(v) for c, v in call_at.items()})
    import_spans = [(m.start(), m.end()) for m in re.finditer(r"\b(?:import|export)\s*(?:type\s+)?\{[^}]*\}[^;\n]*|"
                                                                 r"\bimport\s+\w+[^;\n]*from[^;\n]*", text)]
    return {"uses": uses, "defs": defs, "imports": imports, "events": evs, "event_ex": ev_ex, "calls": calls, "call_at": call_at,
            "in_popup": in_popup, "starts": starts, "text": text, "lines": lines, "import_spans": import_spans}


# ---------------------------------------------------------------- 모으기


def _kwargs(text: str, open_paren: int) -> dict[str, str | None]:
    """이름 붙인 인자 — f(a, height=280, dblclick='open') → {height: '280', dblclick: 'open'} (식이면 값은 None)."""
    close = _match_close(text, open_paren, 2000)
    inner = text[open_paren + 1:close]
    out: dict[str, str | None] = {}
    depth, j, start, pieces = 0, 0, 0, []
    while j < len(inner):
        k = _skip(inner, j)
        if k != j:
            j = k
            continue
        c = inner[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            pieces.append(inner[start:j])
            start = j + 1
        j += 1
    pieces.append(inner[start:])
    for piece in pieces:
        m = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.+?)\s*$", piece, re.S)
        if m:
            v = m.group(2)
            lit = re.fullmatch(r"\"([^\"]*)\"|'([^']*)'|(-?\d+(?:\.\d+)?|true|false|True|False|none|None|null)", v)
            out[m.group(1)] = next((g for g in lit.groups() if g is not None), None) if lit else None
    return out


def _value_refs(p: dict, name: str) -> list[int]:
    """함수를 부르지 않고 값으로 넘긴 곳 — fmt: gfnDate · format: won · .map(won) · onClick={save} (import 줄은 빼고)."""
    out = []
    for m in re.finditer(r"(?:[:,(=\[{]|\breturn)\s*(?<![\w$.])" + re.escape(name) + r"(?![\w$])(?!\s*[(=:])", p["text"]):
        pos = m.end() - len(name)
        if not any(a <= pos < b for a, b in p.get("import_spans") or []):
            out.append(pos)
    return out


def _local_function_uses(parsed: dict[str, dict]) -> dict[str, str]:
    """여러 화면이 쓰는 함수 (openPopup·createGrid·formatWon 같은 공통 함수) — 쓴 곳을 쓰임으로 더한다.
    프로젝트가 한 곳에서 정의한 함수(부른 곳과 값으로 넘긴 곳), 그리고 외부 라이브러리에서 불러와 여러 파일이 부르는 함수.
    돌려주는 값: 공통 함수 이름 → 정의한 파일."""
    defined: dict[str, str] = {}
    def_count: Counter = Counter()
    for rel, p in parsed.items():
        for d in p["defs"]:
            defined.setdefault(d, rel)
            def_count[d] += 1

    def imported_here(p: dict, name: str) -> bool:
        """이 파일이 그 이름을 정의한 파일에서 불러왔나 (경로 끝 이름이 정의한 파일 이름 · index · 패키지 경로)."""
        src = p["imports"].get(name)
        if not src or def_count[name] != 1:
            return False
        last = src.rstrip("/").split("/")[-1].split(".")[0]
        return last == Path(defined[name]).stem or last == "index" or not src.startswith(".")

    users: dict[str, set] = {}
    for rel, p in parsed.items():
        for name in set(p["calls"]) | set(p["imports"]) | {d for d in defined if d.startswith(("gfn", "cfn", "fn_"))}:
            if name not in defined or defined[name] == rel or def_count[name] != 1:
                continue
            if name in p["calls"] or ((imported_here(p, name) or name.startswith(("gfn", "cfn", "fn_"))) and _value_refs(p, name)):
                users.setdefault(name, set()).add(rel)
    # 공통 함수: 한 곳에서 정의하고 여러 파일이 쓰는 것. 화면마다 같은 이름으로 따로 정의한 함수(fn_search)는 공통이 아니다.
    # 짧은 이름(ok·won)은 그 파일에서 불러온(import) 곳이 있을 때만 — 우연히 같은 이름인 지역 함수와 섞지 않는다.
    shared = {name for name, us in users.items()
              if len(us) >= 2 and (len(name) >= 4 or any(imported_here(parsed[r], name) for r in us)) and not name.startswith("_")}
    # 외부 라이브러리(dayjs·axios·useQuery …)에서 불러와 여러 파일이 부르는 함수 — 프레임워크 기본(react·vue …)은 빼고
    lib_users: dict[str, set] = {}
    for rel, p in parsed.items():
        for name, src in p["imports"].items():
            if name in defined or name in JS_WORDS or src.startswith((".", "/", "@/", "~/")) or FRAMEWORK_SOURCES.match(src):
                continue
            if name in p["calls"]:
                lib_users.setdefault(name, set()).add(rel)
    libs = {name for name, us in lib_users.items() if len(us) >= 2}
    for rel, p in parsed.items():
        text, starts = p["text"], p["starts"]
        for name in libs & set(p["call_at"]):
            for pos in p["call_at"][name]:
                open_paren = text.find("(", pos)
                ln = _line_of(starts, pos)
                p["uses"].append({"name": name + "()", "kind": "function", "line": ln, "source": p["imports"][name],
                                  "code": p["lines"][ln - 1].strip()[:110] if 0 < ln <= len(p["lines"]) else "",
                                  "events": [], "attrs": {}, "popup": p["in_popup"](pos, open_paren)})
        for name in shared:
            if defined[name] == rel or rel not in users[name]:
                continue
            calls_at = p["call_at"].get(name, [])
            for pos in calls_at + ([] if calls_at else _value_refs(p, name)):
                open_paren = text.find("(", pos) if calls_at else -1  # 값으로 넘긴 곳에는 이 함수의 인자가 없다
                # 인자 중 객체 리터럴이 있으면 그 키를 옵션으로 (첫 인자가 식이어도), 없으면 이름 붙인 인자(height=280)를
                ob = _arg_object(text, open_paren) if open_paren >= 0 else -1
                opts = _object_keys(text, ob) if ob >= 0 else (_kwargs(text, open_paren) if open_paren >= 0 else {})
                events = sorted(x for x in opts if _is_event(x))
                ln = _line_of(starts, pos)
                p["uses"].append({"name": name + "()", "kind": "function", "line": ln, "source": defined[name],
                                  "code": p["lines"][ln - 1].strip()[:110] if 0 < ln <= len(p["lines"]) else "",
                                  "events": events, "attrs": {x: v for x, v in opts.items() if x not in events},
                                  "popup": p["in_popup"](pos, open_paren)})
    for name in shared:
        parsed[defined[name]].setdefault("lib_shared", set()).add(name)
    return {name: defined[name] for name in shared}


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
                "options": [_option(a, c, pr) for a, c in pr["attrs"].most_common(12)],
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
    # 재사용 목록 — 번역 키·CSS 클래스·여러 파일의 상수·데이터 쪽 함수·서버 서비스 (화면 부품만 보여 주면 나머지는 새로 만들었다)
    part_list = parts(parsed, _file_users(t, parsed))
    lib = library(parsed)
    files = {rel: _read(p) for rel, p in src_files(t).items()}
    uns = unstyled(files, {x["file"] for x in part_list} | {x["file"] for x in lib})
    return {"root": str(base), "files": len(parsed), "shared": items, "events": events, "library": lib,
            **({"parts": part_list} if part_list else {}), **({"unstyled": uns} if uns else {}), **_inventory(files),
            "similar": similar(t, parsed, shared, near or [], like, part_names=_part_names(parsed, part_list))}


def _option(a: str, c: int, pr: dict) -> str:
    """옵션 한 줄 — 'height=280 2/2'. 값이 갈리면 가장 많은 값의 수와 그 밖의 값을 함께 ('width=720 2/3 (그 밖: 640 1)')."""
    n = pr["n"]
    vals = pr["values"].get(a)
    if vals:
        v, vc = vals.most_common(1)[0]
        if vc * 2 > c:
            rest = [f"{x} {k}" for x, k in vals.most_common()[1:3]]
            return f"{a}={v} {vc}/{n}" + (f" (그 밖: {', '.join(rest)})" if rest else "")
    return f"{a} {c}/{n}"


def library(parsed: dict[str, dict], top_files: int = 6, top_names: int = 40) -> list[dict]:
    """공통 함수 파일(gfn.js·utils/format.js 처럼 여러 화면이 쓰는 함수를 둔 곳)의 함수 목록 — 지금은 한 화면만 쓰거나
    아무도 안 쓰는 함수도 이 프로젝트가 이미 가진 공통 기능이다 (엑셀 내려받기·공통코드 콤보·기간 검사 …). 새로 만들기 전에 본다."""
    def common_file(rel: str, p: dict) -> bool:
        """부품 폴더(js/common·utils·macros …)의 스크립트·템플릿 — 여러 화면이 쓰는 함수가 아직 없어도 공통 기능이다
        (popup.js 의 openPopup 을 한 화면만 불러도 공통 팝업이다)."""
        return (bool(PART_DIR_RX.search("/" + rel)) and bool(p["defs"]) and not _data_module(rel) and not _is_catalog(rel)
                and rel.lower().endswith((".js", ".mjs", ".cjs", ".ts") + TEMPLATE_EXTS))
    out = []
    for rel, p in sorted(parsed.items(), key=lambda kv: (-len(kv[1].get("lib_shared") or ()), kv[0])):
        shared = p.get("lib_shared") or set()
        if not shared and not common_file(rel, p):
            continue
        others = []
        for d in sorted(x for x in p["defs"] if not x.startswith("_") and x not in shared):
            users, example = 0, ""
            for r2, q in parsed.items():
                if r2 == rel or d not in q["text"]:
                    continue
                pos = (q["call_at"].get(d) or _value_refs(q, d) or [None])[0]
                if pos is None:
                    continue
                users += 1
                if not example:
                    ln = _line_of(q["starts"], pos)
                    example = f"{r2}:{ln} {q['lines'][ln - 1].strip()[:100] if 0 < ln <= len(q['lines']) else ''}".rstrip()
            others.append({"name": d, "files": users, **({"example": example} if example else {})})
        if others:
            out.append({"file": rel, "shared": sorted(shared), "others": others[:top_names]})
        if len(out) >= top_files:
            break
    return out


# 한국어 요청 낱말 → 영어 파일 이름 낱말 (목록 → *List.vue · list.html). 에이전트는 like 를 한국어로 준다.
KO_PATH_WORDS = {"목록": ("list",), "리스트": ("list",), "상세": ("detail", "view"), "팝업": ("popup", "modal", "dialog", "layer"),
                 "모달": ("modal", "popup", "dialog"), "레이어": ("layer", "popup", "modal"), "검색": ("search",), "조회": ("list", "search"),
                 "등록": ("new", "create", "form", "edit", "register"), "작성": ("new", "create", "form", "write", "edit"),
                 "수정": ("edit", "update", "form"), "삭제": ("delete", "remove"), "선택": ("picker", "select")}
# 파일 이름에서 화면의 역할을 말하는 낱말 — CouponList 의 list, MemberDetailModal 의 detail·modal (나머지는 대상 이름)
ROLE_WORDS = {"list", "detail", "view", "modal", "popup", "dialog", "layer", "picker", "select", "form", "edit", "new", "create",
              "register", "write", "search", "delete", "index", "page", "table", "grid", "info", "manage"}
# 공통 부품을 두는 폴더 — 여기 있는 파일은 화면이 아니라 부품이다 (쓰는 곳이 한 곳뿐이어도 목록에 낸다)
PART_DIR_RX = re.compile(r"/(?:common|commons|shared|utils?|lib|libs|components?|composables|hooks|widgets|partials|macros|fragments|"
                         r"includes)/", re.I)
PART_NAME_RX = re.compile(r"modal|popup|picker|dialog|drawer|layer", re.I)
COMPONENT_EXTS = (".vue", ".jsx", ".tsx", ".svelte")


def _name_words(rel: str) -> list[str]:
    """파일 이름의 낱말 — CouponDetailModal.vue → coupon·detail·modal, member-detail.js → member·detail."""
    stem = Path(rel).name.split(".")[0]
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", stem)]


def _like_groups(like: str) -> list[set[str]]:
    """요청 낱말마다 [그 낱말 + 같은 뜻의 파일 이름 낱말] — 한 낱말은 한 번만 점수를 얻는다."""
    out = []
    for w in (w.lower() for w in re.split(r"[\s,/]+", like or "") if len(w) >= 2):
        out.append({w, *(x for k, v in KO_PATH_WORDS.items() if k in w for x in v)})
    return out


def _word_in(word: str, text: str) -> bool:
    """내용에 낱말이 낱말로 있나 — 영어는 낱말 경계로 (MemberList·member-detail 의 list·detail 은 되고 addEventListener·viewport 는
    아니다), 한국어는 그대로."""
    if not word.isascii() or not word[0].isalpha():
        return word in text.lower()
    w = re.escape(word[1:])
    return bool(re.search(rf"(?:(?<![A-Za-z])[{word[0]}{word[0].upper()}]|(?<=[a-z]){word[0].upper()}){w}(?![a-z])", text))


def _near_fit(rel: str, near: str) -> int:
    """만들 파일 하나와 기존 파일이 닮은 정도 — 같은 폴더 +3, 옆 폴더(src/views/* · templates/*) +2, 이름의 역할 낱말이 같음 +4
    (둘 다 없음도 같음: notices.js ↔ members.js), 일부만 같으면 낱말마다 +2. 만들 파일이 아직 없어도 이름과 자리로 견준다."""
    p, n = Path(rel).parent, Path(near).parent
    score = 3 if p == n else 2 if n.parent != n and p.parent == n.parent and p != n.parent else 0
    mine = set(_name_words(rel)) & ROLE_WORDS
    want = set(_name_words(near)) & ROLE_WORDS
    return score + (4 if mine == want else 2 * len(mine & want))


def similar(t: Path, parsed: dict[str, dict], shared: dict[str, dict], near: list[str], like: str, top: int = 6,
            part_names: set[str] | frozenset = frozenset()) -> list[dict]:
    """지금 만들·고칠 화면과 비슷한 기존 화면 — 자리·이름 꼴(_near_fit), 같은 공통 부품(+2씩), 요청 낱말이 이름에(+4) · 내용에(+1).
    만들 파일이 아직 없으면(새 폴더) 공통 부품을 많이 쓰는 화면을 앞에 (+1씩). 부품 폴더·번역·데이터 모듈은 화면이 아니라 뺀다.
    part_names: 한 곳만 쓰는 부품 이름 (ProductPickerModal · openPopup()) — 그것을 쓰는 화면의 uses 에도 낸다."""
    near_rel = []
    for n in near:
        try:
            near_rel.append(os.path.relpath((t / n).resolve(), t).replace("\\", "/"))
        except ValueError:  # 다른 드라이브 — 이 프로젝트의 파일이 아니다
            continue
    near_parts = set()
    for n in near_rel:
        if n in parsed:
            near_parts |= {u["name"] for u in parsed[n]["uses"] if u["name"] in shared}
    groups = _like_groups(like)
    near_exts = {Path(n).suffix.lower() for n in near_rel}
    rows = []
    for rel, p in parsed.items():
        if rel in near_rel or PART_DIR_RX.search("/" + rel) or _is_catalog(rel) or _data_module(rel):
            continue
        parts = sorted({u["name"] for u in p["uses"] if u["name"] in shared or u["name"] in part_names}
                       | {c + "()" for c in p["calls"] if c + "()" in part_names})
        low = rel.lower()
        score = max((_near_fit(rel, n) for n in near_rel), default=0) + 2 * len(near_parts & set(parts))
        score += sum(4 for g in groups if any(w in low for w in g)) + sum(1 for g in groups if any(_word_in(w, p["text"]) for w in g))
        if score > 0:  # 같은 확장자, 그리고 (만들 파일이 아직 없으면) 공통 부품을 많이 쓰는 화면이 앞에
            score += (Path(rel).suffix.lower() in near_exts) + (0 if near_parts else len(parts))
        if not near and not groups:
            score = len(parts)
        if score <= 0:
            continue
        ev = sorted({e for u in p["uses"] if u["name"] in shared for e in u["events"]})
        rows.append({"file": rel, "score": score, "uses": parts[:8], "events": ev[:10]})
    if not rows and (near or groups):  # 아무것도 닮지 않았으면 공통 부품을 가장 많이 쓰는 화면
        return similar(t, parsed, shared, [], "", top, part_names)
    rows.sort(key=lambda r: (-r["score"], r["file"]))
    return rows[:top]


def _by_tail(rels) -> dict[str, list[str]]:
    """경로 끝 이름 → 파일들 — 불러온 경로(@/components/BaseGrid · partials/_search.html)를 파일로 바꾸는 데."""
    by_tail: dict[str, list[str]] = {}
    for r in rels:
        p = Path(r)
        by_tail.setdefault("/".join(p.with_suffix("").parts[-2:]), []).append(r)
        by_tail.setdefault(p.stem.lstrip("_"), []).append(r)
        if p.stem.startswith("_"):
            by_tail.setdefault("/".join([*p.parts[-2:-1], p.stem.lstrip("_")]), []).append(r)
    return by_tail


GLOBAL_DEF_RX = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|^(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
                           r"(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)", re.M)


def _file_users(t: Path, parsed: dict[str, dict]) -> dict[str, set[str]]:
    """파일마다 그것을 쓰는 파일 — 불러옴(import·require·src·include·extends, 메뉴 링크는 빼고)과, 모듈 없이 전역 함수를 두는
    스크립트(js/common/popup.js 의 openPopup)를 부르는 곳. 쓰는 곳이 한 곳뿐인 부품(ProductPickerModal)도 여기서 보인다."""
    by_tail = _by_tail(parsed)
    users: dict[str, set[str]] = {}
    for r, p in parsed.items():
        text = p["text"]
        refs = [m.group(1) for m in REF_RX.finditer(text) if not re.search(r"<a\b[^<>]*$", text[max(0, m.start() - 300):m.start()], re.I)]
        for rx in INCLUDE_RXS:
            refs += [next((g for g in m.groups() if g), "") for m in rx.finditer(text)]
        for ref in refs:
            if not ref or "${" in ref or "{{" in ref:
                continue
            got = _resolve_ref(t, r, ref.split("::")[0].strip(), by_tail, parsed)
            if got and got != r and got in parsed:
                users.setdefault(got, set()).add(r)
    # 모듈이 아닌 스크립트의 맨 위 함수 — 화면이 <script src> 로 싣고 다른 스크립트가 이름으로 부른다
    owner: dict[str, str] = {}
    count: Counter = Counter()
    for r, p in parsed.items():
        if r.lower().endswith((".js", ".mjs", ".cjs")) and not p["imports"] and not re.search(r"^\s*export\b", p["text"], re.M):
            for a, b in GLOBAL_DEF_RX.findall(p["text"]):
                owner.setdefault(a or b, r)
                count[a or b] += 1
    for r, p in parsed.items():
        for c in p["calls"]:
            if c in owner and count[c] == 1 and len(c) >= 4 and owner[c] != r:
                users.setdefault(owner[c], set()).add(r)
    return users


def parts(parsed: dict[str, dict], users: dict[str, set[str]], top: int = 20) -> list[dict]:
    """부품 목록 — 여러 파일이 쓰는 컴포넌트·스크립트·템플릿 조각, 부품 폴더(common·utils·components·macros …)의 파일, 이름이
    팝업·고르기 창(*Modal·*Picker·*Dialog)인 컴포넌트. 쓰는 곳이 한 곳뿐이어도 낸다 (users 로 몇 곳인지). 받는 옵션(props·기본값)도."""
    out = []
    for rel, p in parsed.items():
        low = rel.lower()
        if _is_catalog(rel) or _data_module(rel) or ENTRY_RX.search(rel) or TYPE_DIRS.search("/" + rel):
            continue
        us = users.get(rel, set())
        in_dir = bool(PART_DIR_RX.search("/" + rel))
        named = low.endswith(COMPONENT_EXTS) and bool(PART_NAME_RX.search(Path(rel).stem))
        if not (in_dir or (named and us) or len(us) >= 2) or not (low.endswith(COMPONENT_EXTS + TEMPLATE_EXTS) or p["defs"]):
            continue
        out.append({"file": rel, "users": len(us), **({"used_by": sorted(us)[:4]} if us else {}), **_options(rel, p["text"])})
    out.sort(key=lambda x: (-x["users"], x["file"]))
    return out[:top]


def _part_names(parsed: dict[str, dict], part_list: list[dict]) -> set[str]:
    """부품을 쓴 곳에 나오는 이름 — 컴포넌트 태그(ProductPickerModal)와 부품 스크립트의 함수(openPopup())."""
    out = set()
    for x in part_list:
        rel = x["file"]
        if rel.lower().endswith(COMPONENT_EXTS):
            out.add(Path(rel).name.split(".")[0])
        elif rel in parsed:
            out |= {(a or b) + "()" for a, b in GLOBAL_DEF_RX.findall(parsed[rel]["text"]) if len(a or b) >= 4}
            out |= {m + "()" for m in MACRO_RX.findall(parsed[rel]["text"])}
    return out


# ---- 부품이 받는 옵션 — Vue props·emits, React 인자·defaultProps, 일반 함수의 옵션 기본값, 템플릿 매크로 인자


def _short(v: str, n: int = 30) -> str:
    v = " ".join(v.split())
    return v if len(v) <= n else v[:n - 1] + "…"


def _obj_pairs(text: str, i: int, limit: int = 30) -> list[tuple[str, str]]:
    """text[i] 가 '{' 인 객체 리터럴의 맨 위 [(키, 값 글)] — 펼침(...x)·줄임 키는 뺀다."""
    close = _match_close(text, i, 20000)
    out, j = [], i + 1
    while j < close and len(out) < limit:
        j = _GAP_RX.match(text, j).end()
        if j >= close:
            break
        km = _KEY_RX.match(text, j)
        end = _next_comma(text, km.end() if km else j, close)
        if km:
            out.append((km.group(2), text[km.end():end].strip().rstrip(",").strip()))
        j = max(end, j + 1)
    return out


def _split_top(s: str) -> list[str]:
    """맨 위 단계의 쉼표로 나눈다 — { a = 1, b = [1, 2] } 의 안쪽."""
    out, j = [], 0
    while j < len(s):
        k = _next_comma(s, j, len(s))
        piece = s[j:k].strip().rstrip(",").strip()
        if piece:
            out.append(piece)
        j = max(k, j + 1)
    return out


def _vue_prop(name: str, raw: str) -> str:
    """Vue prop 한 줄 — 'rowKey: String = 'id'' · 'columns: Array (필수)'."""
    if not raw.startswith("{"):
        return f"{name}: {_short(raw)}"
    inner = dict(_obj_pairs(raw, 0))
    s = name + (f": {inner['type']}" if "type" in inner else "")
    if "default" in inner:
        s += f" = {_short(inner['default'])}"
    return s + (" (필수)" if inner.get("required") == "true" else "")


def _vue_options(text: str) -> dict:
    out: dict = {}
    m = re.search(r"\bdefineProps\s*(?:<\s*\{(.*?)\}\s*>\s*)?\(|\bprops\s*:\s*(?=[\[{])", text, re.S)
    if m and m.group(1) is not None:  # defineProps<{ a?: string }>() — 타입으로 적은 props, withDefaults 의 기본값
        dm = re.search(r"withDefaults\s*\(\s*defineProps[^)]*\)\s*,\s*\{", text)
        defaults = dict(_obj_pairs(text, dm.end() - 1)) if dm else {}
        out["props"] = [f"{a}{'' if q else ' (필수)'}: {_short(b)}" + (f" = {_short(defaults[a])}" if a in defaults else "")
                        for a, q, b in re.findall(r"([A-Za-z_$][\w$]*)\s*(\??)\s*:\s*([^;,\n]+)", m.group(1))]
    elif m:
        i = m.end()
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text) and text[i] == "{":
            out["props"] = [_vue_prop(k, v) for k, v in _obj_pairs(text, i)]
        elif i < len(text) and text[i] == "[":
            out["props"] = re.findall(r"['\"]([\w-]+)['\"]", text[i:_match_close(text, i, 2000)])
    em = re.search(r"\b(?:defineEmits\s*\(|emits\s*:)\s*\[([^\]]*)\]", text)
    if em:
        out["emits"] = re.findall(r"['\"]([\w:-]+)['\"]", em.group(1))
    if m:
        out["at"] = f":{_lineno(text, m.start())}"
    return {k: v for k, v in out.items() if v}


def _react_options(text: str) -> dict:
    m = re.search(r"\bfunction\s+([A-Z]\w*)\s*\(\s*\{|\b(?:const|let)\s+([A-Z]\w*)\s*=\s*(?:\(\s*\{|(?:React\.)?(?:memo|forwardRef)\(\s*\(\s*\{)",
                  text)
    out: dict = {}
    if m:
        i = m.end() - 1
        out["props"] = [_short(x, 40) for x in _split_top(text[i + 1:_match_close(text, i, 4000)])][:20]
        out["at"] = f":{_lineno(text, m.start())}"
    d = re.search(r"\b[A-Z]\w*\.defaultProps\s*=\s*\{", text)
    if d:
        out["defaults"] = [f"{k} = {_short(v)}" for k, v in _obj_pairs(text, d.end() - 1)]
    return out


def _body_after(text: str, j: int) -> str:
    """함수 머리 끝(j) 다음의 본문 — { … } 또는 화살표의 식 하나."""
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    if j < len(text) and text[j] == "{":
        return text[j:_match_close(text, j, 200_000) + 1]
    return _expr_end(text, j)


def _function_options(text: str, top: int = 12) -> list[str]:
    """맨 위 함수의 머리와 옵션 기본값 — 'createGrid(el, opts) :2 — 기본값 pageSize=20, height=null'. 기본값은
    Object.assign({ … }, opts) 와 opts.placeholder || '검색어' 꼴에서."""
    out = []
    for m in FN_SIG_RX.finditer(text):
        ls = text.rfind("\n", 0, m.start()) + 1
        if not re.fullmatch(r"(?:export\s+(?:default\s+)?)?(?:async\s+)?", text[ls:m.start()]):
            continue
        name = m.group(1) or m.group(3)
        params = next((g for g in (m.group(2), m.group(4), m.group(5), m.group(6)) if g is not None), "")
        body = _body_after(text, m.end())
        opts = []
        om = re.search(r"Object\.assign\(\s*\{", body)
        if om:
            opts += [f"{k}={_short(v, 24)}" for k, v in _obj_pairs(body, om.end() - 1)]
        names = [x.strip().split("=")[0].strip() for x in params.split(",") if x.strip()]
        for pn in names:
            if re.fullmatch(r"[A-Za-z_$][\w$]*", pn):
                opts += [f"{a}={_short(b, 24)}" for a, b in re.findall(r"(?<![\w$.])" + re.escape(pn) + r"\.(\w+)\s*(?:\|\||\?\?)\s*(" + LIT + ")", body)]
        sig = f"{name}({_short_params(params)}) :{_lineno(text, m.start())}"
        rm = list(re.finditer(r"\breturn\s*\{([^{}]*)\}", body))  # 컴포저블·팩토리가 돌려주는 함수 — useAlert() → { alertMsg(key, vars) }
        if rm:
            got = []
            for nm in re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$]*)\s*(?=,|$)", rm[-1].group(1).strip()):
                im = re.search(r"\b(?:function\s+" + re.escape(nm) + r"|(?:const|let)\s+" + re.escape(nm) + r"\s*=\s*(?:async\s*)?)\s*\(([^()]*)\)", body)
                got.append(f"{nm}({_short_params(im.group(1))})" if im else nm)
            sig += f" → {{ {', '.join(got)} }}" if got else ""
        out.append(sig + (f" — 기본값 {', '.join(dict.fromkeys(opts))}" if opts else ""))
    return out[:top]


def _options(rel: str, text: str) -> dict:
    """부품이 받는 것 — props·emits(Vue)·props·defaults(React)·functions(일반 스크립트의 함수와 옵션 기본값, 템플릿 매크로 인자)."""
    low = rel.lower()
    if low.endswith((".vue", ".jsx", ".tsx")):
        res = _vue_options(text) if low.endswith(".vue") else _react_options(text)
        return {**res, "at": rel + res["at"]} if "at" in res else res
    if low.endswith(TEMPLATE_EXTS) or "{%" in text:
        fns = [f"{m.group(1)}({_short(m.group(2), 80)}) :{_lineno(text, m.start())}"
               for m in re.finditer(r"\{%-?\s*macro\s+(\w+)\s*\(([^)]*)\)", text)]
        return {"functions": fns[:12]} if fns else {}
    fns = _function_options(text) if low.endswith((".js", ".mjs", ".cjs", ".ts")) else []
    return {"functions": fns} if fns else {}


# ---- 스타일 규칙이 없는 클래스 — 부품·화면이 그리는데 어디에도 규칙이 없다 (useAlert.js 의 toast·confirm-dim)


def _emitted(text: str) -> list[tuple[str, int]]:
    """코드가 그리는 클래스 [(이름, 자리)] — class="…" · className = '…' · classList.add('…') · addClass('…').
    식으로 만든 것({{ }} · ${ } · {% %})은 지운다."""
    out = []
    for rx in EMIT_RXS:
        for m in rx.finditer(text):
            val = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\$\{.*?\}|<%.*?%>", lambda x: " " * len(x.group(0)), m.group(2), flags=re.S)
            for tk in re.finditer(r"\S+", val):
                if re.fullmatch(r"-?[_a-zA-Z][\w-]*", tk.group(0)):
                    out.append((tk.group(0), m.start(2) + tk.start()))
    return sorted(out, key=lambda x: x[1])


def unstyled(files: dict[str, str], shared: set[str], top: int = 20, per_file: int = 6) -> list[dict]:
    """부품(shared)과 화면이 그리는 클래스 중 닿는 스타일 규칙이 없는 것 [{class, at, shared?}] — 공통 스타일(global)에도, 그 파일의
    scoped·page 스타일에도 없다. 고르는 데만 쓰는 클래스(querySelector('.x'))는 뺀다. 화면이 쓰는 클래스 대부분에 규칙이 있는
    프로젝트에서만 (외부 CSS 를 쓰는 프로젝트는 규칙이 없는 것이 보통이다)."""
    glob: set[str] = set()
    local: dict[str, set] = {}
    for rel, text in files.items():
        for scope, css, _ in style_sources(rel, text):
            (glob if scope == "global" else local.setdefault(rel, set())).update(css_classes(css))
    used = defined = 0
    for rel, text in files.items():
        if rel.lower().endswith(SCREEN_EXTS):
            for c, _ in _emitted(text):
                used += 1
                defined += c in glob or c in local.get(rel, ())
    if used < 5 or defined / used < 0.7:
        return []
    out = []
    for rel in sorted(files, key=lambda r: (r not in shared, r)):
        low = rel.lower()
        if not low.endswith(UI_EXTS) or not (rel in shared or low.endswith(SCREEN_EXTS)):
            continue
        text, seen = files[rel], set()
        for c, pos in _emitted(text):
            if c in seen or c in glob or c in local.get(rel, ()) or len(seen) >= per_file:
                continue
            if re.search(r"['\"`][^'\"`\n]*\." + re.escape(c) + r"(?![\w-])", text):
                continue  # 고르는 데 쓰는 표지
            seen.add(c)
            out.append({"class": c, "at": f"{rel}:{_lineno(text, pos)}", **({"shared": True} if rel in shared else {})})
    return out[:top]


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


STYLE_EXTS = (".css", ".scss", ".sass", ".less")
REF_RX = re.compile(r"""(?:from\s+|import\s*\(?\s*|require\s*\(\s*|src\s*=\s*|href\s*=\s*|@import\s+(?:url\()?|"""
                    r"""include\s+file\s*=\s*|page\s*=\s*|filename\s*=\s*|\{%-?\s*(?:include|extends|import|from)\s+)["']([^"'?#]+)["']""")
# 앱 전체에 걸리는 자리 — 여기서 불러오는 스타일·조각은 한 곳에서만 불러도 모든 화면에 적용된다
ENTRY_RX = re.compile(r"(^|/)(main|index|app|App|_app|_document|layout|_layout|Layout|base|header|default|root|application)\.[A-Za-z]+$")


def _resolve_ref(t: Path, user: str, ref: str, by_tail: dict[str, list[str]], known=None) -> str | None:
    """불러온 경로 → 대상 폴더 기준 파일 경로 (못 찾으면 None). user 는 부르는 파일의 대상 폴더 기준 경로.
    known(대상 폴더 기준 경로 모음)을 주면 파일 시스템을 묻지 않고 글자로만 맞춘다 — 큰 저장소에서 경로마다 resolve 가 느렸다."""
    ref = ref.strip()
    if ref.startswith("."):
        r = posixpath.normpath(posixpath.join(posixpath.dirname(user.replace("\\", "/")), ref.replace("\\", "/")))
        cands = (r, *(r + e for e in (".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte", ".mjs")),
                 *(r + "/index" + e for e in (".js", ".ts", ".jsx", ".tsx")))
        if known is not None:
            return next((c for c in cands if c in known), None)
        for c in cands:
            try:
                if (t / c).is_file():
                    return c
            except (OSError, ValueError):
                return None
        return None
    norm = ref.replace("\\", "/").lstrip("@~/")
    parts = Path(norm).parts
    key = "/".join(Path(norm).with_suffix("").parts[-2:]) if len(parts) >= 2 else Path(norm).stem
    hits = by_tail.get(key) or []
    return hits[0] if len(hits) == 1 else None


TYPE_DIRS = re.compile(r"/(?:types?|typings|@types|constants?|consts|locales?|lang|i18n/(?:locales?|messages?|[a-z]{2}(?:-[A-Z]{2})?))/"
                       r"|/(?:types?|constants?)\.[jt]sx?$", re.I)
DATA_DIRS = re.compile(r"/(?:api|apis|services?|stores?|repositor(?:y|ies)|models?|dao|mocks?|data|queries|graphql|slices|reducers)/", re.I)


def shared_parts(target: Path, min_users: int = 2, top: int = 300) -> dict[str, int]:
    """화면 쪽 공통 부품 {경로: 쓰는 파일 수} — 여러 화면이 불러 쓰는 컴포넌트·템플릿 조각·레이아웃, 화면들이 불러 쓰는 공통 JS
    (그리고 공통 조각이 불러오는 파일: header.jsp 가 싣는 gfn.js). 스타일은 뺀다 — 새 화면의 클래스를 더하는 것이 그 프로젝트의
    방식인 경우가 많아 고친 뒤 선택자 단위로 본다(shared_edits). 서버 코드도 뺀다 — 오류 코드·라우트 등록처럼 공통 파일에 더하는 것이
    정해진 방식인 경우가 많다."""
    t = Path(target).resolve()
    _, files = ui_files(t)
    rels = {os.path.relpath(f, t).replace("\\", "/"): f for f in files if "/.beom/" not in "/" + os.path.relpath(f, t)}
    by_tail = _by_tail(rels)
    users: dict[str, set[str]] = {}
    for r, f in rels.items():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs = [m.group(1) for m in REF_RX.finditer(text)]
        for rx in INCLUDE_RXS:  # Thymeleaf·Razor·Blade·EJS·Handlebars 같은 include·레이아웃도
            refs += [next((g for g in m.groups() if g), "") for m in rx.finditer(text)]
        for ref in refs:
            if not ref or "${" in ref or "{{" in ref:
                continue
            got = _resolve_ref(t, r, ref.split("::")[0].strip(), by_tail, rels)
            if got and got != r and got in rels:
                users.setdefault(got, set()).add(r)
    screen = lambda r: r.lower().endswith(SCREEN_EXTS)  # noqa: E731

    def additive(r: str) -> bool:
        """새 화면이 덧붙이는 것이 보통인 파일 — 타입·상수·번역, 다시 내보내기만 하는 index (잠그면 새 타입·새 부품 하나에도 막힌다)."""
        if TYPE_DIRS.search("/" + r) or r.endswith(".d.ts"):
            return True
        if Path(r).stem == "index":
            try:
                body = [x.strip() for x in (t / r).read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
            except OSError:
                return False
            return bool(body) and all(x.startswith(("export ", "//", "/*", "*")) and (" from " in x or not x.startswith("export "))
                                      for x in body)
        return False
    out: dict[str, int] = {}
    for r, us in users.items():
        # 데이터 쪽 모듈(api·services·store·mock)은 새 화면이 함수를 더하는 것이 보통의 확장 방식이다 — 화면 쪽 공통 부품이 아니다
        if r.lower().endswith(STYLE_EXTS) or (not screen(r) and (DATA_DIRS.search("/" + r) or DATA_FILE_RX.search(r))) or additive(r):
            continue  # data.js·mock.js·api.js 처럼 이름이 데이터 모듈인 파일도 (폴더가 없는 작은 프로젝트)
        # 화면 파일(컴포넌트·템플릿)이거나, 화면 파일들이 불러 쓰는 JS — 서버 코드끼리만 쓰는 파일은 아니다
        if len(us) >= min_users and (screen(r) or sum(1 for u in us if screen(u)) >= min_users):
            out[r] = len(us)
    for r, us in users.items():  # 공통 조각이 불러오는 것 (header.jsp → gfn.js) — 그 조각을 쓰는 화면 모두가 쓴다
        if r not in out and not r.lower().endswith(STYLE_EXTS) and not DATA_DIRS.search("/" + r) and not DATA_FILE_RX.search(r) and any(u in out and screen(u) for u in us):
            out[r] = max(out[u] for u in us if u in out)
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:top])


def _data_module(rel: str) -> bool:
    """데이터 쪽 모듈 — api·services·store·mock 폴더, 또는 data.js·mock.js·api.js 처럼 이름이 데이터 모듈인 파일 (화면 파일은 아니다)."""
    rel = rel.replace("\\", "/")
    return not rel.lower().endswith(SCREEN_EXTS + STYLE_EXTS) and bool(DATA_DIRS.search("/" + rel) or DATA_FILE_RX.search(rel))


def _only_added(old: str, new: str) -> bool:
    """기존 줄을 하나도 바꾸거나 지우지 않고 줄을 더하기만 했나 (빈 줄·줄 끝 공백은 보지 않는다)."""
    import difflib
    # 끝에 더하면서 앞 줄에 붙인 쉼표는 바꾼 것이 아니다
    a = [x.rstrip().rstrip(",") for x in old.replace("\r\n", "\n").split("\n") if x.strip()]
    b = [x.rstrip().rstrip(",") for x in new.replace("\r\n", "\n").split("\n") if x.strip()]
    i = 0  # 같은 앞·뒤를 먼저 덜어 낸다 — 큰 데이터 파일에서 줄 비교가 몇 초씩 걸렸다
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    a, b = a[i:len(a) - j], b[i:len(b) - j]
    if not a:
        return True  # 기존 줄은 모두 그대로 — 사이에 더하기만 했다
    if len(a) * len(b) > 4_000_000:
        return False  # 너무 크게 바뀌었다 — 더하기만 했다고 보지 않는다 (알린다)
    return all(op in ("equal", "insert") for op, *_ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes())


def shared_edits(target: Path, base: dict, min_users: int = 2, allowed: set[str] | frozenset = frozenset()) -> list[dict]:
    """원래 있던 공통 부품(여러 파일이 불러 쓰는 컴포넌트·공통 함수 파일·공통 스타일·템플릿 조각)을 이번 작업이 고친 곳.
    고치면 그것을 쓰는 다른 화면들이 모두 함께 바뀐다 — 요청이 시킨 일이 아니면 사용자가 알아야 한다."""
    t = Path(target).resolve()
    if not base:
        return []
    edited = []
    for rel, ent in base.items():
        if "text" not in ent or "/.beom/" in "/" + rel or rel in allowed:  # 사용자가 계획에서 승인한 공통 부품 변경
            continue
        p = t / rel
        try:
            now = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # 지운 파일은 다른 검사가 본다
        if now.replace("\r\n", "\n") != str(ent["text"]).replace("\r\n", "\n"):
            if not rel.lower().endswith(STYLE_EXTS) and _link_only_edit(str(ent["text"]), now):
                continue  # 새 화면으로 가는 링크(메뉴 한 칸)만 더한 화면 — 공통 부품을 바꾼 것이 아니다
            if _data_module(rel) and _only_added(str(ent["text"]), now):
                continue  # 데이터·목업·API 모듈에 새 목록·함수를 더하기만 했다 — 보통의 확장 방식이다 (기존 줄을 바꾸면 알린다)
            edited.append(rel)
    if not edited:
        return []
    _, files = ui_files(t)
    others = [f for f in files] + [f for f in _style_files(t)]
    out = []
    emitted = None
    for rel in edited:
        target_abs = (t / rel).resolve()
        stem = target_abs.with_suffix("")
        tail = "/".join(Path(rel).parts[-2:])
        users = []
        for f in others:
            if f.resolve() == target_abs:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if target_abs.name.split(".")[0] not in text:
                continue
            for m in REF_RX.finditer(text):
                if re.search(r"<a\b[^<>]*$", text[max(0, m.start() - 300):m.start()], re.I):
                    continue  # 메뉴 링크(<a href="members.html">)로 가는 화면은 그 화면을 불러 쓰는 것이 아니다
                ref = m.group(1).strip()
                if ref.startswith("."):
                    r = (f.parent / ref).resolve()
                    hit = r == target_abs or r == stem or r / "index" == stem
                else:  # /resources/js/common/gfn.js · @/components/BaseGrid.vue · partials/_search.html
                    norm = ref.replace("\\", "/").lstrip("@~/")
                    hit = norm.endswith(tail) or (len(Path(rel).parts) == 1 and norm.endswith(rel))
                    hit = hit or "/".join(Path(norm).with_suffix("").parts[-2:]) == "/".join(Path(rel).with_suffix("").parts[-2:])
                    hit = hit or ("/" not in norm and norm == Path(rel).name)  # url_for('static', filename='admin.css')
                if hit:
                    users.append(os.path.relpath(f, t).replace("\\", "/"))
                    break
        entry = [u for u in users if ENTRY_RX.search(u)]
        if rel.lower().endswith(STYLE_EXTS):
            if not entry and len(users) < min_users:
                continue
            # 새 화면에만 걸리는 새 클래스를 더한 것은 그 프로젝트의 방식이다 — 기존 규칙을 바꾸거나 기존 선택자에 걸리는 규칙을 더한 것만
            old_css, new_css = str(base[rel]["text"]), (t / rel).read_text(encoding="utf-8", errors="replace")
            hits = _css_global_edits(old_css, new_css)
            if emitted is None:
                emitted = _emitted_classes(base)
            extra = _css_shared_class_rules(old_css, new_css, emitted)
            if hits or extra:
                where = f"{', '.join(entry[:2])} 가 불러와 모든 화면에 적용된다" if entry else f"이것을 쓰는 다른 파일 {len(users)}곳에 걸린다"
                what = []
                if hits:
                    what.append(f"기존 규칙을 바꾸거나 기존 선택자에 규칙을 더함 ({', '.join(hits[:4])}{' …' if len(hits) > 4 else ''})")
                if extra:
                    what.append(f"기존 코드가 이미 그리는 클래스나 공통 클래스의 변형에 규칙을 더함 ({', '.join(extra[:4])}"
                                f"{' …' if len(extra) > 4 else ''})")
                out.append({"at": rel, "summary": f"원래 있던 공통 스타일 {rel} 의 {' · '.join(what)} — {where}. 요청이 시킨 변경이 아니면 "
                                                  "되돌리고 새 화면에만 걸리는 새 클래스로 해결한다",
                            "evidence": (entry or sorted(users))[:3]})
        elif len(users) >= min_users:
            out.append({"at": rel, "summary": f"원래 있던 공통 부품 {rel} 을 고침 — 이것을 쓰는 다른 파일 {len(users)}곳도 함께 바뀐다 "
                                              f"({', '.join(sorted(users)[:4])}{' …' if len(users) > 4 else ''}). 요청이 시킨 변경이 "
                                              "아니면 되돌리고 새 화면 쪽에서 해결한다",
                        "evidence": sorted(users)[:3]})
    return out


def _css_rules(text: str) -> list[tuple[str, str]]:
    """CSS 규칙 [(선택자, 내용)] — @media 안의 규칙도 (선택자 앞에 그 @media 를 붙여). 주석은 뺀다."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out, media = [], []
    for m in re.finditer(r"([^{}]*)\{|\}", text):
        if m.group(0) == "}":
            if media:
                media.pop()
            continue
        # '@import "x";'·'$c: red;' 같은 앞 문장은 선택자가 아니다 — 마지막 ';' 뒤만 (앞 문장에 묻혀 다음 규칙을 놓쳤다)
        head = " ".join(m.group(1).rsplit(";", 1)[-1].split())
        if head.startswith("@"):
            media.append(head)
            continue
        end = text.find("}", m.end())
        body = " ".join(text[m.end():end].split()) if end >= 0 else ""
        out.append((" ".join([*media, head]).strip(), body))
        media.append("")  # 이 규칙의 닫는 } 짝
    return out


def _css_global_edits(old: str, new: str) -> list[str]:
    """기존 규칙을 바꾸거나 지운 선택자, 기존 선택자(또는 전체에 걸리는 선택자)에 더한 규칙 — 다른 화면에도 걸리는 변경."""
    old_rules, new_rules = _css_rules(old), _css_rules(new)
    old_set, new_set = set(old_rules), set(new_rules)
    old_sels = [sel for sel, _ in old_rules]
    old_names = {n for x in old_sels for n in _sel_names(x)}
    out = [sel for sel, body in old_rules if (sel, body) not in new_set]
    for sel, body in new_rules:
        if (sel, body) in old_set:
            continue
        parts = _sel_parts(re.sub(r"^@\S+[^{]*?\)\s*", "", sel))
        # 새 것(새 클래스·id, 또는 전에 스타일하지 않던 요소)이 들어간 선택자는 그 새 것에만 걸린다 — .member-page .btn·.span-2·
        # .field textarea(전에 textarea 스타일 없음) 는 새 것, .btn:hover·:focus-visible·body·.field input 은 다른 화면에도 걸린다
        if not parts or not all(any(n not in old_names for n in _sel_names(x)) for x in parts):
            out.append(sel)
    return list(dict.fromkeys(out))


ANCHOR_RX = re.compile(r"<a\b[^>]*\bhref\s*=[^>]*>.*?</a\s*>", re.I | re.S)


def _link_only_edit(old: str, new: str) -> bool:
    """링크(<a href>)만 더한 변경인가 — 메뉴에 새 화면으로 가는 칸 하나를 더한 화면들 (원래 링크는 그대로)."""
    old_a = Counter(_squash_ws(a) for a in ANCHOR_RX.findall(old))
    new_a = Counter(_squash_ws(a) for a in ANCHOR_RX.findall(new))
    if not new_a or sum(new_a.values()) <= sum(old_a.values()) or old_a - new_a:
        return False
    strip = lambda s: re.sub(r"<li\b[^>]*>\s*</li\s*>", "", _squash_ws(ANCHOR_RX.sub("", s)), flags=re.I)  # noqa: E731
    return strip(old) == strip(new)


def _squash_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


EMIT_RXS = [
    re.compile(r"(?<![:\w-])class(?:Name)?\s*=\s*([\"'`])(.*?)\1", re.S),      # class="toast" · box.className = 'toast' · innerHTML 글 안
    re.compile(r"(?:add|toggle|remove|has)Class\(\s*([\"'])(.*?)\1"),           # jQuery
    re.compile(r"classList\.(?:add|toggle|replace)\(\s*([\"'])(.*?)\1"),
]


def _emitted_classes(base: dict) -> dict[str, str]:
    """원래 있던 화면·공통 코드가 그리는 클래스 {이름: 파일} — 스타일 파일은 빼고 (원래 내용 기준)."""
    out: dict[str, str] = {}
    for rel, ent in base.items():
        if "text" not in ent or rel.lower().endswith(STYLE_EXTS) or "/.beom/" in "/" + rel:
            continue
        for rx in EMIT_RXS:
            for m in rx.finditer(str(ent["text"])):
                for tk in m.group(2).split():
                    if re.fullmatch(r"-?[_a-zA-Z][\w-]*", tk):
                        out.setdefault(tk, rel)
    return out


def _css_shared_class_rules(old: str, new: str, emitted: dict[str, str]) -> list[str]:
    """공통 스타일에 새로 더한 규칙 중 다른 화면에도 걸리는 것 — 원래 코드가 이미 그리는 클래스(useAlert.js 의 .toast)를 처음
    꾸미는 규칙, 공통 클래스의 변형(dl.detail 에 .form 을 붙인 dl.detail.form). 새 화면 클래스로 감싼 것(.coupon-page .toast)은 빼고."""
    old_rules = set(_css_rules(old))
    old_names = {n for sel, _ in old_rules for n in _sel_names(sel)}
    out = []
    for sel, body in _css_rules(new):
        if (sel, body) in old_rules:
            continue
        for part in _sel_parts(re.sub(r"^@\S+[^{]*?\)\s*", "", sel)):
            bare = re.sub(r"\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?", " ", part)
            fresh = [c for c in CLASS_SEL_RX.findall(bare) if "." + c not in old_names]
            if fresh and all(c in emitted for c in fresh):
                out.append(f"{sel} ← {emitted[fresh[0]]}")
                break
            variant = None
            for comp in re.split(r"[\s>+~]+", bare.strip()):
                cs = CLASS_SEL_RX.findall(comp)
                if len(cs) >= 2 and "." + cs[0] in old_names and any("." + c not in old_names for c in cs[1:]):
                    variant = cs[0]
                    break
            if variant:
                out.append(f"{sel} (공통 .{variant} 의 변형)")
                break
    return list(dict.fromkeys(out))


def _sel_parts(sel: str) -> list[str]:
    """맨 바깥 쉼표로만 나눈다 — :is(a, b) 안의 쉼표는 그대로."""
    parts, depth, cur = [], 0, ""
    for c in sel:
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    return [x.strip() for x in parts + [cur] if x.strip()]


def _sel_names(sel: str) -> set[str]:
    """선택자의 클래스·id·요소 이름 — 가상 클래스(:hover)·속성 값은 빼고."""
    bare = re.sub(r"\[[^\]]*\]|::?[\w-]+", " ", re.sub(r":(?:is|where|not|has)\(", " ", sel))
    return set(re.findall(r"[.#][\w-]+", bare)) | {m for m in re.findall(r"(?:^|[\s>+~(])([a-z][a-z0-9-]*)", bare)}


def _style_files(t: Path, limit: int = 500) -> list[Path]:
    out = []
    for d, dirs, names in os.walk(t):
        dirs[:] = [x for x in dirs if x not in layout.SKIP_DIRS and not x.startswith(".")]
        out += [Path(d, n) for n in names if n.lower().endswith(STYLE_EXTS)]
        if len(out) >= limit:
            break
    return out


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


# ---------------------------------------------------------------- 이미 있는 것 목록 (재사용) — 번역 키·클래스·상수·데이터 함수·서버 서비스
# 실제 작업에서 에이전트는 분석이 보여 준 것은 다시 쓰고, 보여 주지 않은 것(번역 키·CSS 클래스·상수 목록·mock/api 의 도우미 함수·
# 서버 서비스와 쿼리)은 새로 만들었다. 그래서 그것들도 목록으로 낸다 — 짧게, 정의한 파일(:줄)과 함께.

SERVER_EXTS = (".py", ".java", ".kt", ".xml", ".cs", ".go", ".rb")
CATALOG_DIR = re.compile(r"(?:^|/)(?:i18n|locales?|lang|langs|messages?|translations?|nls)/", re.I)
CATALOG_STEM = re.compile(r"^(?:messages?|msgs?|lang|i18n|labels?)(?:[_.-][\w-]+)?$", re.I)
TEST_PATH = re.compile(r"(?:^|/)(?:src/test|tests?|__tests__|spec|e2e|cypress)/|\.(?:test|spec)\.[cm]?[jt]sx?$|(?:^|/)test_[^/]*\.py$|"
                       r"_test\.(?:py|go)$|(?:Test|Tests|IT)\.(?:java|kt)$")
JS_LIKE = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte")
CODE_LIT_EXTS = JS_LIKE + (".py", ".java", ".kt", ".cs", ".go", ".rb", ".php")


def _is_catalog(rel: str) -> bool:
    """번역·메시지 목록 파일인가 — i18n/locales/lang 아래의 js·json, message.js, messages*.properties."""
    p = Path(rel)
    ext = p.suffix.lower()
    if ext == ".properties":
        return bool(CATALOG_STEM.match(p.stem) or CATALOG_DIR.search("/" + rel))
    if ext not in (".js", ".mjs", ".cjs", ".ts", ".json", ".yml", ".yaml"):
        return False
    if p.name.lower() in ("package.json", "tsconfig.json", "jsconfig.json") or p.stem.lower() == "index":
        return False
    return bool(CATALOG_DIR.search("/" + rel) or CATALOG_STEM.match(p.stem))


def _src_root(t: Path) -> Path:
    """ui_files 와 같은 자리 — 대상 폴더가 속한 프로젝트의 가장 바깥 자리."""
    roots = sorted({r for _, r in layout.projects(t)} or {t}, key=lambda p: len(p.parts))
    base = roots[0] if roots else t
    top = layout.repo_top(t)
    return t if top and not base.is_relative_to(top) else base


def src_files(target: Path, limit: int = COUNT_MAX * 2) -> dict[str, Path]:
    """재사용 목록·중복 검사에 쓰는 파일 {대상 폴더 기준 경로: 경로} — 화면 코드·스타일·서버 코드·번역 목록 (테스트·의존성 빼고)."""
    t = Path(target).resolve()
    base = _src_root(t)
    tests = layout.test_files(t)
    exts = UI_EXTS + STYLE_EXTS + SERVER_EXTS + (".json", ".properties", ".yml", ".yaml")
    out: dict[str, Path] = {}
    for dirpath, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in layout.SKIP_DIRS and not d.startswith("."))
        for n in sorted(names):
            low = n.lower()
            if not low.endswith(exts) or low.endswith((".min.js", ".min.css", ".d.ts", ".lock")):
                continue
            p = Path(dirpath) / n
            rel = os.path.relpath(p, t).replace("\\", "/")
            if str(p) in tests or TEST_PATH.search(rel) or "/.beom/" in "/" + rel:
                continue
            if low.endswith((".json", ".yml", ".yaml", ".properties")) and not _is_catalog(rel):
                continue
            if low.endswith(".xml") and not re.search(r"mapper|dao|sql|quer", rel, re.I):
                continue
            try:
                if p.stat().st_size > FILE_MAX:
                    continue
            except OSError:
                continue
            out[rel] = p
            if len(out) >= limit:
                return out
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _lineno(text: str, pos: int) -> int:
    return text.count("\n", 0, max(0, pos)) + 1


def _code_at(text: str, ln: int) -> str:
    lines = text.splitlines()
    return lines[ln - 1].strip()[:100] if 0 < ln <= len(lines) else ""


# ---- 번역·메시지 목록


def _next_comma(text: str, j: int, close: int) -> int:
    depth = 0
    while j < close:
        k = _skip(text, j)
        if k != j:
            j = k
            continue
        c = text[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            return j + 1
        j += 1
    return close


_GAP_RX = re.compile(r"(?:\s|,|//[^\n]*|/\*.*?\*/)*", re.S)
_KEY_RX = re.compile(r"(['\"]?)([\w$.\-]+)\1\s*:\s*")


def _js_obj(text: str, i: int, prefix: str, out: list, depth: int = 0) -> None:
    """text[i] 가 '{' 인 객체 리터럴의 글 값 — [(점으로 이은 키, 값, 자리)]. 안쪽 객체는 키를 이어 붙여 내려간다."""
    close = _match_close(text, i, FILE_MAX)
    j = i + 1
    while j < close and depth < 8:
        j = _GAP_RX.match(text, j).end()
        if j >= close:
            break
        km = _KEY_RX.match(text, j)
        if not km:
            j = _next_comma(text, j, close)
            continue
        key, v = prefix + km.group(2), km.end()
        c = text[v] if v < close else ""
        if c == "{":
            end = _match_close(text, v, FILE_MAX)
            _js_obj(text, v, key + ".", out, depth + 1)
            j = end + 1
        elif c and c in "'\"`":
            end = _skip(text, v)
            val = text[v + 1:end - 1]
            if c != "`" or "${" not in val:
                out.append((key, val.replace("\\'", "'").replace('\\"', '"'), km.start(2)))
            j = end
        else:
            j = _next_comma(text, v, close)


def catalog_entries(rel: str, text: str) -> list[tuple[str, str, int]]:
    """번역 목록 파일의 [(키, 값, 줄)] — js·ts 객체(export default {…} · var MSG = {…}), json, properties, 간단한 yml."""
    ext = Path(rel).suffix.lower()
    out: list[tuple[str, str, int]] = []
    if ext == ".json":
        import json
        try:
            data = json.loads(text)
        except ValueError:
            return []
        pos = [0]

        def walk(o, prefix):
            for k, v in (o.items() if isinstance(o, dict) else ()):
                if isinstance(v, dict):
                    walk(v, f"{prefix}{k}.")
                elif isinstance(v, str):
                    at = text.find(json.dumps(k, ensure_ascii=False), pos[0])
                    if at >= 0:
                        pos[0] = at
                    out.append((prefix + k, v, _lineno(text, max(at, 0))))
        walk(data, "")
        return out
    if ext == ".properties":
        for n, line in enumerate(text.splitlines(), 1):
            m = re.match(r"\s*([^#!\s=:][^=:\s]*)\s*[=:]\s*(.*)$", line)
            if m:
                val = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), m.group(2).strip())
                out.append((m.group(1), val, n))
        return out
    if ext in (".yml", ".yaml"):
        stack: list[tuple[int, str]] = []
        for n, line in enumerate(text.splitlines(), 1):
            m = re.match(r"(\s*)([\w.-]+)\s*:\s*(.*)$", line)
            if not m or line.lstrip().startswith("#"):
                continue
            ind = len(m.group(1))
            while stack and stack[-1][0] >= ind:
                stack.pop()
            val = m.group(3).strip()
            if val:
                out.append((".".join([s for _, s in stack] + [m.group(2)]), val.strip("'\""), n))
            else:
                stack.append((ind, m.group(2)))
        return out
    m = re.search(r"\bexport\s+default\s*(?:\w+\s*\()?\s*\{|\bmodule\.exports\s*=\s*\{|"
                  r"\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*(?::[^=]{1,40})?=\s*\{", text)
    if not m:
        return []
    raw: list = []
    _js_obj(text, m.end() - 1, "", raw)
    return [(k, v, _lineno(text, p)) for k, v, p in raw]


NS_RX = re.compile(r"(?://|/\*|\*|#|<!--|\{#|<%--)[^\n]*?(?:키|key|code|코드|message|메시지|msg)[^\n]*?(?<![\w.])([A-Za-z_]\w*)\.\*", re.I)


def _namespaces(files: dict[str, str]) -> list[dict]:
    """주석에 적힌 키 이름 규칙 — '키는 i18n 의 msg.*' 처럼. 그 파일이 정의한 함수에 넘기는 키가 따라야 한다."""
    out = []
    for rel, text in files.items():
        if not rel.lower().endswith(JS_LIKE) or _is_catalog(rel):
            continue
        for m in NS_RX.finditer(text):
            fns = sorted({a or b for a, b in DEF_RX.findall(text)})
            if fns:
                out.append({"rule": m.group(1) + ".*", "at": f"{rel}:{_lineno(text, m.start())}", "for": fns[:8]})
    return out[:10]


def _catalog_lookups(files: dict[str, str], names: set[str]) -> dict[str, list[str]]:
    """목록 이름(var MSG = {…} 의 MSG)으로 값을 찾는 함수 — gfnAlert(code) 가 MSG[code] 를 읽는다. {목록 이름: [함수…]}."""
    out: dict[str, list[str]] = {}
    for name in names:
        rx = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*[\[.]")
        for rel, text in files.items():
            if _is_catalog(rel) or not rel.lower().endswith(JS_LIKE + TEMPLATE_EXTS) or not rx.search(text):
                continue
            for m in DEF_RX.finditer(text):
                body = _fn_body(text, m)
                if body and rx.search(body[0]):
                    out.setdefault(name, []).append(m.group(1) or m.group(2))
    return {k: sorted(set(v))[:8] for k, v in out.items()}


def _catalog_groups(files: dict[str, str], keys: list[str], pri: re.Pattern, top: int = 40) -> dict[str, str]:
    """키 묶음(맨 앞 마디)마다 공통인지 한 대상 전용인지 — common·msg 처럼 공통 이름이거나 여러 대상의 화면이 쓰면 공통,
    member.* 를 member 화면만 쓰면 전용 (쿠폰 화면에 member.name 을 가져다 쓰지 않게)."""
    prefixes = list(dict.fromkeys(k.split(".")[0] for k in keys if "." in k))
    if not prefixes or len(prefixes) > top:
        return {}
    texts = {r: tx for r, tx in files.items() if r.lower().endswith(UI_EXTS) and not _is_catalog(r)}
    out = {}
    for pre in prefixes:
        rx = re.compile(r"['\"`]" + re.escape(pre) + r"\.[\w$]")
        us = sorted(r for r, tx in texts.items() if pre + "." in tx and rx.search(tx))
        stem = pre.lower()[:-1] if len(pre) > 3 and pre.lower().endswith("s") else pre.lower()
        if pri.match(pre):
            out[pre] = f"공통 ({len(us)}파일)"
        elif not us:
            out[pre] = "쓰는 곳 없음"
        elif all(stem in r.lower() for r in us):
            out[pre] = f"{pre} 전용 — {', '.join(us[:2])}{' …' if len(us) > 2 else ''} 만 쓴다 (다른 대상의 화면에 가져다 쓰지 않는다)"
        else:
            out[pre] = f"여러 화면 ({len(us)}파일)"
    return out


def catalog(files: dict[str, str], top: int = 150) -> dict | None:
    """번역·메시지 목록 — 파일, 키:값 (많으면 공통 쪽 먼저), 키 이름 규칙, 목록을 읽는 함수."""
    cats = {rel: catalog_entries(rel, text) for rel, text in files.items() if _is_catalog(rel)}
    cats = {k: v for k, v in cats.items() if v}
    if not cats:
        return None
    main = max(cats, key=lambda r: len(cats[r]))
    pri = re.compile(r"^(?:common|msg|message|messages|label|labels|button|btn|status|code|codes|validation|error|errors)\b", re.I)
    entries = sorted(cats[main], key=lambda e: (not pri.match(e[0]), e[2]))[:top]
    out: dict = {"files": [f"{r} ({len(v)})" for r, v in sorted(cats.items(), key=lambda kv: -len(kv[1]))][:10],
                 "keys": sum(len(v) for v in cats.values()), "from": main,
                 "entries": {k: v[:60] for k, v, _ in sorted(entries, key=lambda e: e[2])}}
    groups = _catalog_groups(files, [k for k, _, _ in cats[main]], pri)
    if groups:
        out["groups"] = groups
    ns = _namespaces(files)
    if ns:
        out["namespaces"] = ns
    names = {m.group(1) for r in cats for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{", files.get(r, ""))}
    look = _catalog_lookups(files, names) if names else {}
    if look:
        out["lookups"] = look
    return out


# ---- 스타일 (클래스)


STYLE_BLOCK_RX = re.compile(r"<style\b([^>]*)>(.*?)</style\s*>", re.I | re.S)
CLASS_SEL_RX = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")


def style_sources(rel: str, text: str) -> list[tuple[str, str, int]]:
    """파일 안의 스타일 [(범위, css, 시작 자리)] — 범위: global(모든 화면) · scoped(그 컴포넌트만) · page(그 화면만) · module.
    .css 파일은 global (.module.css 는 module), Vue 의 <style scoped|module> 는 scoped, Svelte 는 scoped, 화면 틀의 <style> 은
    그 화면만 (레이아웃·헤더 조각이면 global)."""
    low = rel.lower()
    if low.endswith(STYLE_EXTS):
        return [("module" if ".module." in low else "global", text, 0)]
    out = []
    for m in STYLE_BLOCK_RX.finditer(text):
        attrs = m.group(1).lower()
        if low.endswith(".vue"):
            scope = "scoped" if ("scoped" in attrs or "module" in attrs) else "global"
        elif low.endswith(".svelte"):
            scope = "scoped"
        else:
            scope = "global" if ENTRY_RX.search(rel) else "page"
        out.append((scope, m.group(2), m.start(2)))
    return out


def css_classes(css: str) -> set[str]:
    """선택자에 나온 클래스 이름 (값 안의 .5em 같은 것은 빼고)."""
    out: set[str] = set()
    for sel, _ in _css_rules(css):
        out |= set(CLASS_SEL_RX.findall(re.sub(r"^@\S+[^{]*?\)\s*", "", sel)))
    return out


def _strip_media(sel: str) -> str:
    return re.sub(r"^@\S+[^{]*?\)\s*", "", sel)


def _rule_lines(css: str, top: int = 40, width: int = 100) -> list[str]:
    """클래스 규칙의 짧은 내용 — '.field em {color: var(--danger); font-style: normal;}' (클래스가 없는 규칙은 빼고)."""
    out = []
    for sel, body in _css_rules(css):
        if body and CLASS_SEL_RX.search(_strip_media(sel)):
            s = f"{sel} {{{body}}}"
            out.append(s if len(s) <= width else s[:width - 1] + "…")
    return out[:top]


def styles(files: dict[str, str], top_files: int = 20, top_classes: int = 80) -> list[dict]:
    """이미 있는 CSS 클래스 — 정의한 파일과 범위(global/scoped/page). 공통(global)을 먼저. 공통 스타일은 규칙 내용(rules)도 짧게 —
    클래스 이름만으로는 무엇을 하는지 몰라 같은 일을 하는 클래스를 새로 만들었다."""
    out = []
    for rel, text in files.items():
        by: dict[str, set] = {}
        css_of: dict[str, list[str]] = {}
        for scope, css, _ in style_sources(rel, text):
            by.setdefault(scope, set()).update(css_classes(css))
            css_of.setdefault(scope, []).append(css)
        for scope, cls in by.items():
            if cls:
                rules = _rule_lines("\n".join(css_of[scope])) if scope == "global" else []
                out.append({"file": rel, "scope": scope, "classes": sorted(cls)[:top_classes],
                            **({"more": len(cls) - top_classes} if len(cls) > top_classes else {}), **({"rules": rules} if rules else {})})
    out.sort(key=lambda x: (x["scope"] != "global", -len(x["classes"]), x["file"]))
    return out[:top_files]


def style_repeats(files: dict[str, str], top: int = 15) -> list[dict]:
    """여러 화면의 scoped·page 스타일에 따로 있는 같은 클래스 (.sub 를 상세 팝업마다) — 공통 클래스로 둘 후보. 새 화면에서 또
    만들지 말고, 공통 스타일에 같은 일을 하는 클래스가 있는지 먼저 본다."""
    by: dict[str, dict[str, str]] = {}
    for rel, text in files.items():
        for scope, css, _ in style_sources(rel, text):
            if scope not in ("scoped", "page"):
                continue
            for sel, body in _css_rules(css):
                for c in set(CLASS_SEL_RX.findall(_strip_media(sel))):
                    by.setdefault(c, {}).setdefault(rel, _short(f"{sel} {{{body}}}", 100))
    out = [{"class": c, "files": sorted(v)[:6], "rule": v[sorted(v)[0]]} for c, v in by.items() if len(v) >= 2]
    out.sort(key=lambda x: (-len(x["files"]), x["class"]))
    return out[:top]


# ---- 상수·값 목록


LIT = r"(?:'[^'\n]*'|\"[^\"\n]*\"|-?\d+(?:\.\d+)?)"
LIST_RX = re.compile(r"(?:\[|(?<![\w$])\(|\b(?:List|Set|Arrays)\.(?:of|asList)\()\s*((?:" + LIT + r"\s*,\s*){2,}" + LIT + r")\s*,?\s*[\])]")
OPTION_SELECT_RX = re.compile(r"<select\b[^>]*>(.*?)</select\s*>", re.I | re.S)
OPTION_RX = re.compile(r"<option\b([^>]*)>([^<{}$]*)</option\s*>", re.I)


def literal_lists(rel: str, text: str) -> list[tuple[tuple, int]]:
    """값만으로 된 목록 [(값들, 줄)] — ['VIP', 'GOLD', 'BASIC'] · ("a", "b", "c") · List.of(…) · 정해진 <option> 들. 서로 다른 값 3개 이상."""
    out = []
    for m in LIST_RX.finditer(text):
        vals = tuple(v.strip("'\"") for v in re.findall(LIT, m.group(1)))
        if len(set(vals)) >= 3:
            out.append((vals, _lineno(text, m.start())))
    if rel.lower().endswith(SCREEN_EXTS):
        for m in OPTION_SELECT_RX.finditer(text):
            opts = []
            for o in OPTION_RX.finditer(m.group(1)):
                v = re.search(r"\bvalue\s*=\s*[\"']([^\"'{}$]*)[\"']", o.group(1))
                opts.append((v.group(1) if v else o.group(2)).strip())
            opts = [o for o in opts if o]
            if len(set(opts)) >= 3:
                out.append((tuple(opts), _lineno(text, m.start())))
    return out


CONST_RXS = [
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Z][A-Z0-9_]{2,})\s*(?::[^=\n]{1,60})?=", re.M),  # JS·TS
    re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=\n]{1,60})?=(?!=)", re.M),                               # 파이썬
    re.compile(r"\bstatic\s+final\s+[\w<>\[\], ]+?\s([A-Z][A-Z0-9_]{2,})\s*="),                          # 자바
]


def constants(files: dict[str, str], top: int = 30, top_single: int = 15) -> dict:
    """여러 파일에 걸친 상수 — 이름 붙인 모듈 상수 중 다른 파일이 쓰는 것, 두 파일 이상에 같은 값 목록. 화면·템플릿에만 한 번
    있는 값 목록(<option> 들 · {% for g in ['VIP', 'GOLD', 'BASIC'] %})도 — 새 화면이 같은 목록을 또 쓰기 전에 본다.
    값은 처음 나온 곳의 차례 그대로."""
    defs: dict[str, tuple[str, int]] = {}
    for rel, text in files.items():
        if _is_catalog(rel) or not rel.lower().endswith(CODE_LIT_EXTS):
            continue
        for rx in CONST_RXS:
            for m in rx.finditer(text):
                defs.setdefault(m.group(1), (rel, _lineno(text, m.start())))
    named = []
    for name, (rel, ln) in defs.items():
        rx = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
        users = [r for r, tx in files.items() if r != rel and name in tx and rx.search(tx)]
        if users:
            named.append({"name": name, "at": f"{rel}:{ln}", "used_in": len(users) + 1})
    named.sort(key=lambda x: (-x["used_in"], x["name"]))
    seen: dict[tuple, list[tuple[str, int, tuple]]] = {}
    for rel, text in files.items():
        if _is_catalog(rel):
            continue
        for vals, ln in literal_lists(rel, text):
            hits = seen.setdefault(tuple(sorted(vals)), [])
            if not any(h[0] == rel for h in hits):
                hits.append((rel, ln, vals))
    lists = [{"values": list(v[0][2])[:12], "at": [f"{r}:{n}" for r, n, _ in v[:4]]} for v in seen.values() if len(v) >= 2]
    lists.sort(key=lambda x: -len(x["at"]))
    single = [{"values": list(v[0][2])[:12], "at": [f"{v[0][0]}:{v[0][1]}"]} for v in seen.values()
              if len(v) == 1 and v[0][0].lower().endswith(SCREEN_EXTS)
              and not re.search(r"\b(?:defineEmits|defineProps|emits|props|inject)\b", _code_at(files[v[0][0]], v[0][1]))]
    lists = lists[:top] + single[:top_single]
    return {**({"named": named[:top]} if named else {}), **({"lists": lists} if lists else {})}


# ---- 데이터 쪽 함수 (api·mock·service·store) · 서버 서비스와 쿼리

FN_SIG_RX = re.compile(r"(?:\bfunction\s+([A-Za-z_$][\w$]*)\s*\(([^()]*)\)|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
                       r"(?:function\s*\(([^()]*)\)|\(([^()]*)\)\s*(?::[^=]{1,40})?=>|([A-Za-z_$][\w$]*)\s*=>))")


DATA_FILE_RX = re.compile(r"(?:^|/)(?:data|mocks?|api|services?|stores?|db|repo|fixtures?)\.[cm]?[jt]s$", re.I)


def _short_params(p: str) -> str:
    p = " ".join((p or "").split())
    return p if len(p) <= 40 else p[:37] + "…"


def data_helpers(files: dict[str, str], top_files: int = 12, top_names: int = 30) -> list[dict]:
    """데이터 쪽 모듈(api·services·store·mock·data)의 함수 — 내보내지 않은 도우미(mock 의 like() 같은)도. 화면 쪽 공통 함수
    목록(library)에는 없다. 새 검색·변환 함수를 만들기 전에 본다."""
    out = []
    for rel, text in files.items():
        low = rel.lower()
        if not low.endswith(JS_LIKE) or low.endswith(SCREEN_EXTS) or _is_catalog(rel) \
                or not (DATA_DIRS.search("/" + rel) or DATA_FILE_RX.search(rel)):
            continue
        fns = []
        for m in FN_SIG_RX.finditer(text):
            name = m.group(1) or m.group(3)
            params = next((g for g in (m.group(2), m.group(4), m.group(5), m.group(6)) if g is not None), "")
            exported = bool(re.search(r"\bexport\s+(?:default\s+)?(?:async\s+)?$", text[max(0, m.start() - 30):m.start()]))
            fns.append(f"{name}({_short_params(params)}) :{_lineno(text, m.start())}" + ("" if exported else " (내보내지 않음)"))
        if fns:
            out.append({"file": rel, "functions": fns[:top_names]})
    out.sort(key=lambda x: (not re.search(r"mock|util|helper|common", x["file"], re.I), x["file"]))
    return out[:top_files]


SERVER_CLASS_RX = re.compile(r"(?:^|/)([A-Z]\w*?)(Service|ServiceImpl|Mapper|Dao|DAO|Repository|Repo|Store|Query|Queries)\.(?:java|kt|cs)$")
PY_REPO_RX = re.compile(r"(?:^|/)(?:(\w+?)_)?(repo|repos|repository|repositories|dao|service|services|crud|queries|store|models?)\.py$")
JS_SERVICE_RX = re.compile(r"(?:^|/)([A-Za-z]\w*?)[-_.]?(?:Service|Repository|Repo|Dao|Model|Queries|service|repository|repo|dao|"
                           r"model|queries)\.(?:[cm]?[jt]s)$")
JAVA_METHOD_RX = re.compile(r"^\s*(?:public\s+|protected\s+)?(?:static\s+)?(?:<[^>]+>\s+)?[\w<>\[\],.? ]+?\s+([a-z]\w*)\s*\(([^)]*)\)\s*"
                            r"(?:throws [\w., ]+)?\s*[;{]", re.M)


def _entity(word: str) -> str:
    w = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", word).lower().strip("_")
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def server_services(files: dict[str, str], top: int = 25, per: int = 14) -> list[dict]:
    """서버 쪽 서비스·매퍼·쿼리를 다루는 대상(회원·주문 …)별로 — 자바 서비스·매퍼 인터페이스와 MyBatis XML 의 쿼리 id,
    파이썬 repo·service 모듈의 함수. 새 조회·검사를 만들기 전에 같은 대상에 이미 있는 것을 본다."""
    import ast
    by: dict[str, list[str]] = {}
    for rel, text in files.items():
        m = SERVER_CLASS_RX.search(rel)
        if m:
            cls = Path(rel).stem
            items = []
            for d in JAVA_METHOD_RX.finditer(text):
                if d.group(1) in ("if", "for", "while", "switch", "catch", "return", "new") or re.match(r"\s*private\b", d.group(0)):
                    continue
                items.append(f"{rel}:{_lineno(text, d.start(1))} {cls}.{d.group(1)}({_short_params(d.group(2))})")
            by.setdefault(_entity(m.group(1)), []).extend(items)
            continue
        if rel.lower().endswith(".xml") and "<mapper" in text:
            ns = re.search(r"<mapper\s+namespace\s*=\s*\"([\w.$]+)\"", text)
            ent = re.sub(r"(Mapper|Dao|DAO|Repository)$", "", (ns.group(1).split(".")[-1] if ns else Path(rel).stem))
            items = [f"{rel}:{_lineno(text, q.start())} {q.group(1)} {q.group(2)}"
                     for q in re.finditer(r"<(select|insert|update|delete|sql)\b[^>]*\bid\s*=\s*\"([\w.]+)\"", text)]
            by.setdefault(_entity(ent), []).extend(items)
            continue
        jm = JS_SERVICE_RX.search(rel)
        if jm and not rel.lower().endswith(SCREEN_EXTS):  # memberService.ts · member.repository.js
            names = [f"{rel}:{_lineno(text, f.start())} {f.group(1) or f.group(3)}()" for f in FN_SIG_RX.finditer(text)]
            names += [f"{rel}:{_lineno(text, f.start(1))} {f.group(1)}()" for f in
                      re.finditer(r"^[ \t]+(?:public\s+|static\s+)?(?:async\s+)?([a-z]\w*)\s*\([^()]*\)\s*(?::[^{=;]{1,60})?\{", text, re.M)
                      if f.group(1) not in ("if", "for", "while", "switch", "catch", "constructor", "function")]
            if names:
                by.setdefault(_entity(jm.group(1)), []).extend(names)
            continue
        pm = PY_REPO_RX.search(rel)
        if rel.endswith(".py") and (pm or DATA_DIRS.search("/" + rel)):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            ent = (pm.group(1) if pm and pm.group(1) else None) or (Path(rel).parent.name if pm else Path(rel).stem)
            items = [f"{rel}:{n.lineno} {n.name}({_short_params(', '.join(a.arg for a in n.args.args))})"
                     for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
            for c in (n for n in tree.body if isinstance(n, ast.ClassDef)):
                items += [f"{rel}:{f.lineno} {c.name}.{f.name}()" for f in c.body
                          if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and not f.name.startswith("_")]
            if items:
                by.setdefault(_entity(ent), []).extend(items)
    out = [{"entity": k, "items": v[:per], **({"more": len(v) - per} if len(v) > per else {})} for k, v in by.items() if v]
    out.sort(key=lambda x: (-len(x["items"]), x["entity"]))
    return out[:top]


MSG_FN_RX = re.compile(r"alert|confirm|toast|flash|notif|message|msg", re.I)
HUMAN_RX = re.compile(r"[^\x00-\x7f]|^(?=.*[A-Za-z]{2})(?=.*\s).{6,}$")
STR_LIT_RX = re.compile(r"'([^'\\\n]{2,200})'|\"([^\"\\\n]{2,200})\"|`([^`\\]{2,200})`")


def messages(files: dict[str, str], top: int = 30) -> list[dict]:
    """번역 목록이 없는 프로젝트의 사용자 문구 — flash()·toast()·alert()·gfnAlert()·confirm_form(…) 처럼 알리고 묻는 함수에
    직접 넘긴 글 [{text, at, via}]. 같은 일을 하는 문구가 이미 있으면 그 말투·글을 따른다."""
    out, seen = [], set()
    for rel, text in files.items():
        if _is_catalog(rel) or not rel.lower().endswith(CODE_LIT_EXTS + TEMPLATE_EXTS):
            continue
        for m in re.finditer(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\(", text):
            name = m.group(1)
            if not MSG_FN_RX.search(name) or re.search(r"(?:function|def|macro)\s+$", text[max(0, m.start() - 12):m.start()]):
                continue
            close = _match_close(text, m.end() - 1, 2000)
            if re.match(r"\s*(?:\{|=>)", text[close + 1:close + 5]):
                continue  # 정의 (toast(message) { … })
            lit = next((s.strip() for s in (next(g for g in x.groups() if g is not None) for x in STR_LIT_RX.finditer(text, m.end(), close))
                        if HUMAN_RX.search(s.strip())), None)
            if not lit or lit in seen:
                continue
            seen.add(lit)
            out.append({"text": lit[:80], "at": f"{rel}:{_lineno(text, m.start())}", "via": name})
            if len(out) >= top:
                return out
    return out


FILTER_RXS = [  # (정규식, 이름 묶음, 함수 묶음) — Flask·Jinja·Django 의 템플릿 필터 등록
    (re.compile(r"\badd_template_filter\(\s*([A-Za-z_][\w.]*)\s*(?:,\s*(?:name\s*=\s*)?['\"](\w+)['\"])?"), 2, 1),
    (re.compile(r"@[\w.]*template_filter\(\s*(?:(?:name\s*=\s*)?['\"](\w+)['\"])?\s*\)\s*\n\s*def\s+(\w+)"), 1, 2),
    (re.compile(r"\bfilters\[\s*['\"](\w+)['\"]\s*\]\s*=\s*([A-Za-z_][\w.]*)"), 1, 2),
    (re.compile(r"@register\.filter(?:\(\s*(?:name\s*=\s*)?['\"]?(\w*)['\"]?\s*\))?\s*\n\s*def\s+(\w+)"), 1, 2),
]


def template_filters(files: dict[str, str], top: int = 30) -> list[dict]:
    """템플릿에서 쓰는 필터(|ymd · |won) — 등록한 곳과 함수 [{name, at, def}]. 날짜·금액 모양을 새로 만들기 전에 본다."""
    out = []
    for rel, text in files.items():
        if not rel.endswith(".py"):
            continue
        for rx, ng, fg in FILTER_RXS:
            for m in rx.finditer(text):
                fn = m.group(fg).split(".")[-1]
                d = re.search(r"^[ \t]*def\s+" + re.escape(fn) + r"\s*\(", text, re.M)
                out.append({"name": m.group(ng) or fn, "at": f"{rel}:{_lineno(text, m.start())}",
                            **({"def": f"{rel}:{_lineno(text, d.start())} {_code_at(text, _lineno(text, d.start()))}"} if d else {})})
    return out[:top]


def inventory(target: Path) -> dict:
    """재사용 목록 — 번역 키(catalog), CSS 클래스(styles), 여러 파일의 상수·값 목록(constants), 데이터 쪽 함수(data_helpers),
    서버 서비스·쿼리(server). 비어 있는 것은 뺀다."""
    return _inventory({rel: _read(p) for rel, p in src_files(target).items()})


def _inventory(files: dict[str, str]) -> dict:
    """inventory 의 몸통 — 읽어 둔 파일로. 번역 목록이 없으면 코드에 직접 쓴 사용자 문구(messages)를, 템플릿 필터(filters)도."""
    cat = catalog(files)
    out = {"catalog": cat, "styles": styles(files), "style_repeats": style_repeats(files), "constants": constants(files),
           "data_helpers": data_helpers(files), "server": server_services(files),
           "messages": None if cat else messages(files), "filters": template_filters(files)}
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- 만든 뒤: 이미 있는 것을 다시 만든 곳
# 원래 소스 기록(shared_edits 와 같은 '전' 상태)과 지금을 견준다. 이번 작업이 새로 만들거나 고친 파일에서 —
# 이미 있는 번역 값과 같은 새 키·직접 쓴 글, 키 이름 규칙을 어긴 키, 이미 있는 함수와 같은 새 함수, 닿는 스타일 규칙이 없는 클래스,
# 기존 화면에 없던 인라인 style, 다른 파일에 이미 있는 값 목록, 기존 줄의 공백만 바꾼 곳, 공통 클래스를 이 화면에서만 다시 꾸민 곳.

JS_KEYWORDS = {"const", "let", "var", "return", "if", "else", "for", "while", "do", "switch", "case", "break", "continue", "new",
               "typeof", "instanceof", "in", "of", "function", "async", "await", "true", "false", "null", "undefined", "this", "try",
               "catch", "finally", "throw", "class", "extends", "super", "delete", "void", "yield", "default", "import", "export"}
JS_GLOBALS = {"String", "Number", "Boolean", "Array", "Object", "Math", "JSON", "Date", "Promise", "parseInt", "parseFloat",
              "encodeURIComponent", "decodeURIComponent", "window", "document", "console", "setTimeout", "setInterval", "isNaN",
              "Intl", "RegExp", "Map", "Set", "location", "localStorage", "sessionStorage", "fetch", "$", "jQuery", "navigator"}
TOK_RX = re.compile(r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`|\d[\w.]*|[A-Za-z_$][\w$]*|"""
                    r"""===|!==|=>|\.\.\.|\?\.|\?\?|&&|\|\||[=!<>]=|\*\*|[^\s\w]""")


def _strip_js_comments(text: str) -> str:
    out, j, n = [], 0, len(text)
    while j < n:
        k = _skip(text, j)
        if k != j:
            if not (text[j] == "/" and j + 1 < n and text[j + 1] in "/*"):
                out.append(text[j:k])  # 글·정규식은 그대로, 주석만 뺀다
            j = k
            continue
        out.append(text[j])
        j += 1
    return "".join(out)


def _expr_end(text: str, j: int) -> str:
    """화살표 함수의 식 본문 — 맨 위 단계의 ; , 닫는 괄호, 또는 이어지지 않는 줄 끝까지."""
    depth, k, n = 0, j, min(len(text), j + 4000)
    while k < n:
        s = _skip(text, k)
        if s != k:
            k = s
            continue
        c = text[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c in ";,":
            break
        elif depth == 0 and c == "\n":
            prev, nxt = text[j:k].rstrip()[-1:], text[k:k + 200].lstrip()[:1]
            if prev not in tuple("([{,=+-*/&|?:") and nxt not in tuple(".?:&|+-*/"):
                break
        k += 1
    return text[j:k]


def _fn_body(text: str, m: re.Match) -> tuple[str, int] | None:
    """DEF_RX 로 찾은 정의의 본문 (글, 시작 자리) — function 선언·함수 식은 { … }, 화살표는 { … } 또는 식 하나."""
    seg = m.group(0)
    if seg.rstrip().endswith("=>"):
        j = m.end()
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] == "{":
            return text[j:_match_close(text, j, 200_000) + 1], j
        return _expr_end(text, j), j
    open_p = m.end() - 1 if seg.endswith("(") else text.find("(", m.end())
    if open_p < 0:
        return None
    close = _match_close(text, open_p, 4000)
    brace = text.find("{", close)
    if brace < 0 or brace - close > 120:
        return None
    return text[brace:_match_close(text, brace, 200_000) + 1], brace


def _js_names(text: str) -> tuple[set[str], set[str]]:
    """(이 파일 맨 위 단계의 이름, 불러온 이름)."""
    mod = {a or b for a, b in DEF_RX.findall(text)}
    mod |= set(re.findall(r"^(?:export\s+(?:default\s+)?)?(?:const|let|var|class|function\*?|async\s+function)\s+([A-Za-z_$][\w$]*)",
                          text, re.M))
    imp = set()
    for m in IMPORT_RX.finditer(text):
        if m.group(1):
            imp.add(m.group(1))
        imp |= {p.split(" as ")[-1].strip() for p in (m.group(2) or "").split(",") if p.strip()}
    imp |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(", text))
    return mod, imp


def _norm_tokens(code: str, own: set[str], keep: set[str], rel: str) -> tuple[list[str], set[str]]:
    """본문을 낱말로 — 지역 이름(인자·지역 변수)은 '_', 이 파일 맨 위 이름은 파일을 붙여(다른 파일의 같은 이름과 섞지 않게),
    속성·불러온 이름·내장 이름·글은 그대로. 공백·주석·; 은 뺀다. 돌려주는 두 번째 값: 그대로 둔 이름들 (견줄 후보를 거르는 데)."""
    toks = TOK_RX.findall(code)
    out, kept = [], set()
    for i, tk in enumerate(toks):
        if tk == ";":
            continue
        if re.match(r"[A-Za-z_$]", tk):
            prev = toks[i - 1] if i else ""
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if prev in (".", "?.") or (nxt == ":" and prev in ("{", ",")):
                out.append(tk)
                kept.add(tk)
            elif tk in own:
                out.append(f"{tk}@{rel}")
            elif tk in JS_KEYWORDS:
                out.append(tk)
            elif tk in keep or tk[0].isupper():
                out.append(tk)
                kept.add(tk)
            else:
                out.append("_")
        else:
            out.append(tk)
    return out, kept


def _functions(rel: str, text: str) -> list[dict]:
    """파일의 함수 [{name, line, toks, kept, lang}] — JS 계열(화면 틀 안의 스크립트 포함)과 파이썬."""
    low = rel.lower()
    out = []
    if low.endswith(".py"):
        import ast
        import builtins
        import copy
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        own = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        own |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        imp = {(a.asname or a.name).split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        keep = imp | set(dir(builtins))

        class _N(ast.NodeTransformer):
            def visit_Name(self, node):
                node.id = f"{node.id}@{rel}" if node.id in own else node.id if node.id in keep else "_"
                return node

            def visit_arg(self, node):
                node.arg, node.annotation = "_", None
                return node

        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = list(n.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            if not body:
                continue
            mod = _N().visit(ast.Module(body=[copy.deepcopy(b) for b in body], type_ignores=[]))
            try:
                code = ast.unparse(mod)
            except Exception:  # noqa: BLE001 — 되살리지 못하는 꼴은 건너뛴다
                continue
            toks = TOK_RX.findall(code)
            kept = {x for x in toks if re.match(r"[A-Za-z_]", x) and x != "_" and "@" not in x}
            out.append({"name": n.name, "line": n.lineno, "toks": toks, "kept": kept, "lang": "py"})
        return out
    if not low.endswith(UI_EXTS):
        return []
    own, imp = _js_names(text)
    keep = imp | JS_GLOBALS
    for m in DEF_RX.finditer(text):
        got = _fn_body(text, m)
        if not got:
            continue
        toks, kept = _norm_tokens(_strip_js_comments(got[0]), own, keep, rel)
        out.append({"name": m.group(1) or m.group(2), "line": _lineno(text, m.start()), "toks": toks, "kept": kept, "lang": "js"})
    return out


def _cover(old: list[str], new: list[str]) -> float:
    """old 의 낱말 중 new 와 같은 차례로 겹치는 몫 (가장 긴 겹침이 6낱말 미만이면 0)."""
    import difflib
    blocks = difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks()
    if max((b.size for b in blocks), default=0) < 6:
        return 0.0
    return sum(b.size for b in blocks) / max(1, len(old))


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _added_lines(old: str | None, new: str) -> set[int]:
    """new 에서 이번에 더하거나 바꾼 줄 (1부터) — 공백만 바뀐 줄은 빼고. old 가 없으면(새 파일) 모든 줄."""
    import difflib
    nl = new.splitlines()
    if old is None:
        return set(range(1, len(nl) + 1))
    sm = difflib.SequenceMatcher(None, [_squash(x) for x in old.splitlines()], [_squash(x) for x in nl], autojunk=False)
    out: set[int] = set()
    for tag, _, _, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def _git(t: Path, *args: str) -> str | None:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(t), *args], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def _before_state(t: Path, rel: str, text: str, base: dict, since: float | None, git_base: str | None,
                  git_files: set[str] | None) -> tuple[str, str | None]:
    """원래 상태 — ('same'|'changed'|'new'|'unknown', 원래 글). 원래 소스 기록 → (기록에 없는 번역 json·properties 등은) 작업을
    시작한 커밋 → 기록한 시각보다 오래된 파일이면 그대로. 원래를 알 수 없으면 탓하지 않는다."""
    norm = lambda s: s.replace("\r\n", "\n")  # noqa: E731
    ent = base.get(rel)
    if ent is not None:
        if "text" in ent:
            return ("same", None) if norm(str(ent["text"])) == norm(text) else ("changed", str(ent["text"]))
        try:
            same = hashlib.sha256((t / rel).read_bytes()).hexdigest() == ent.get("sha")
        except OSError:
            same = False
        return ("same", None) if same else ("unknown", None)
    if git_files is not None and not rel.startswith("../"):
        if rel not in git_files:
            return "new", None
        old = _git(t, "show", f"{git_base}:./{rel}")
        if old is None:
            return "unknown", None
        return ("same", None) if norm(old) == norm(text) else ("changed", old)
    try:
        if since is not None and (t / rel).stat().st_mtime < since:
            return "same", None
    except OSError:
        return "unknown", None
    from beom_workbench.core import impact
    return ("new", None) if rel.endswith(impact.SNAP_EXTS) else ("unknown", None)


def _f(at: str, summary: str, evidence: list[str]) -> dict:
    return {"at": at, "summary": summary, "evidence": [e for e in evidence if e][:3], "kind": "reuse"}


def _dup_catalog(now: dict, before: dict, touched: set) -> list[dict]:
    """새 번역 값이 이미 있는 값과 같음 · 코드에 직접 쓴 글이 이미 메시지 목록에 있음."""
    out = []
    by_val: dict[str, tuple[str, str, int]] = {}
    for rel, text in before.items():
        if _is_catalog(rel):
            for k, v, ln in catalog_entries(rel, text):
                by_val.setdefault(v.strip(), (rel, k, ln))
    if not by_val:
        return out
    for rel in sorted(touched):
        if not _is_catalog(rel):
            continue
        old_keys = {k for k, _, _ in catalog_entries(rel, before[rel])} if rel in before else set()
        for k, v, ln in catalog_entries(rel, now[rel]):
            hit = by_val.get(v.strip())
            if k in old_keys or not v.strip() or not hit or hit[1] == k:
                continue
            out.append(_f(f"{rel}:{ln}", f"새 번역 키 {k} 의 값 '{v[:40]}' 이 이미 있는 키 {hit[1]} 와 같음 — 새 키를 만들지 말고 "
                                         f"{hit[1]} 를 쓴다", [f"{hit[0]}:{hit[2]} {hit[1]}: {v[:60]}"]))
    # 코드에 직접 쓴 글 — 한글 같은 글자이거나 띄어쓰기가 있는 긴 글만 (키·이름처럼 생긴 짧은 영문은 빼고)
    vals = {v: h for v, h in by_val.items() if len(v) >= 2 and (re.search(r"[^\x00-\x7f]", v) or (len(v) >= 8 and " " in v))}
    for rel in sorted(touched):
        if _is_catalog(rel) or not rel.lower().endswith(CODE_LIT_EXTS):
            continue
        text = now[rel]
        added = _added_lines(before.get(rel), text)
        seen = set()
        for m in re.finditer(r"'([^'\\\n]{2,200})'|\"([^\"\\\n]{2,200})\"", text):
            lit = (m.group(1) or m.group(2)).strip()
            h = vals.get(lit)
            ln = _lineno(text, m.start())
            if not h or ln not in added or lit in seen:
                continue
            seen.add(lit)
            out.append(_f(f"{rel}:{ln}", f"글 '{lit[:40]}' 을 직접 씀 — 이미 메시지 목록에 키 {h[1]} 로 있다 (목록의 키로 쓴다)",
                          [f"{h[0]}:{h[2]} {h[1]}: {lit[:60]}"]))
    return out


def _dup_namespace(now: dict, before: dict, touched: set) -> list[dict]:
    """주석이 정한 키 이름 규칙('키는 i18n 의 msg.*')을 어긴 새 호출."""
    out = []
    for r in _namespaces(before):
        ns, deffile = r["rule"][:-2], r["at"].rsplit(":", 1)[0]
        fns = [f for f in r["for"] if f]
        rx = re.compile(r"(?<![\w$.])(" + "|".join(map(re.escape, fns)) + r")\s*\(\s*(['\"`])([^'\"`\n]*)")
        for rel in sorted(touched):
            if rel == deffile or not rel.lower().endswith(UI_EXTS):
                continue
            text = now[rel]
            added = _added_lines(before.get(rel), text)
            for m in rx.finditer(text):
                key = m.group(3)
                static = key.split("${")[0]
                ln = _lineno(text, m.start())
                if ln not in added or not static or static.startswith(ns + ".") or not re.match(r"[\w-]+\.", static):
                    continue
                out.append(_f(f"{rel}:{ln}", f"{m.group(1)}('{key[:40]}') — 이 함수에 넘기는 키는 {ns}.* 로 정해져 있다 — {ns} 아래의 "
                                             "기존 키를 쓰거나 거기에 더한다", [f"{r['at']} {_code_at(before[deffile], int(r['at'].rsplit(':', 1)[1]))}"]))
    return out


def _dup_functions(now: dict, before: dict, touched: set) -> list[dict]:
    """이미 있는 함수와 같은(또는 거의 같은) 일을 하는 새 함수 — 이름·공백·주석·지역 이름을 지우고 견준다."""
    existing = []
    for rel, text in before.items():
        for fn in _functions(rel, text):
            existing.append({**fn, "file": rel})
    exact: dict[tuple, list[dict]] = {}
    for e in existing:
        if len(e["toks"]) >= 8:
            exact.setdefault((e["lang"], tuple(e["toks"])), []).append(e)
    # 비슷함은 드문 이름(toLowerCase·includes 처럼 몇 함수만 쓰는 것)이 둘 이상 겹칠 때만 — .value·.map·.code 같은 흔한 이름만
    # 겹치는 짧은 함수는 모양이 닮아도 하는 일이 다르다
    df = Counter(k for e in existing for k in e["kept"])
    rare_max = max(1, len(existing) // 20)
    out = []
    for rel in sorted(touched):
        old_sigs = {(f["name"], tuple(f["toks"])) for f in _functions(rel, before[rel])} if rel in before else set()
        for fn in _functions(rel, now[rel]):
            if (fn["name"], tuple(fn["toks"])) in old_sigs or len(fn["toks"]) < 8:
                continue
            cands = [e for e in exact.get((fn["lang"], tuple(fn["toks"])), []) if not (e["file"] == rel and e["name"] == fn["name"])]
            how = "같은"
            if not cands:
                n = len(fn["toks"])
                best = None
                for e in existing:
                    m = len(e["toks"])
                    if e["lang"] != fn["lang"] or m < 12 or n > 3 * m or not e["kept"] <= fn["kept"] \
                            or sum(df[k] <= rare_max for k in e["kept"]) < 2 or (e["file"] == rel and e["name"] == fn["name"]):
                        continue
                    c = _cover(e["toks"], fn["toks"])
                    if c >= 0.75 and (best is None or c > best[0]):
                        best = (c, e)
                if best:
                    cands, how = [best[1]], "거의 같은"
            if cands:
                e = cands[0]
                out.append(_f(f"{rel}:{fn['line']}", f"새 함수 {fn['name']} 가 이미 있는 {e['name']}({e['file']}) 와 {how} 일을 함 — "
                                                     f"{e['name']} 를 불러 쓴다 (내보내지 않았으면 내보내서)",
                              [f"{e['file']}:{e['line']} {_code_at(before[e['file']], e['line'])}"]))
    return out


TEMPLATE_CLASS_RX = re.compile(r"(?<![:\w-])class(?:Name)?\s*=\s*([\"'])(.*?)\1", re.S)


def _template_classes(text: str) -> list[tuple[str, int]]:
    """화면 틀에 적은 클래스 [(이름, 자리)] — 식으로 만든 것({{ }} · ${ } · :class)은 빼고."""
    out = []
    for m in TEMPLATE_CLASS_RX.finditer(text):
        for tk in re.finditer(r"\S+", m.group(2)):
            name = tk.group(0)
            if re.fullmatch(r"-?[_a-zA-Z][\w-]*", name):
                out.append((name, m.start(2) + tk.start()))
    return out


def _hook_text(t: Path) -> str:
    """테스트·화면 케이스가 고르는 데 쓰는 이름 — 스타일이 없어도 되는 클래스 (.f-name 같은 표지)."""
    parts = []
    for f in list(layout.test_files(t))[:200]:
        parts.append(_read(Path(f))[:100_000])
    d = t / ".beom"
    if d.is_dir():
        for f in sorted(d.glob("*.json"))[:20]:
            parts.append(_read(f)[:400_000])
    return "\n".join(parts)


def _dup_classes(t: Path, now: dict, before: dict, touched: set) -> list[dict]:
    """새로 쓴 클래스에 닿는 스타일 규칙이 없음 — 다른 컴포넌트의 scoped 스타일에만 있으면 여기서는 걸리지 않는다."""
    glob, local, scoped_at = set(), {}, {}
    for rel, text in now.items():
        for scope, css, _ in style_sources(rel, text):
            cls = css_classes(css)
            if scope == "global":
                glob |= cls
            elif scope in ("scoped", "page"):
                local.setdefault(rel, set()).update(cls)
                for c in cls:
                    scoped_at.setdefault(c, []).append(rel)
    used = defined = 0
    for rel, text in before.items():  # 기존 화면이 쓰는 클래스 중 규칙이 닿는 몫 — 적으면 외부 CSS(부트스트랩·테일윈드)를 쓰는 프로젝트
        if rel in touched or not rel.lower().endswith(SCREEN_EXTS):
            continue
        for c, _ in _template_classes(text):
            used += 1
            defined += c in glob or c in local.get(rel, ())
    own_rules = used >= 5 and defined / used >= 0.7
    hooks = _hook_text(t) if own_rules else ""
    globals_files = sorted(r for r, tx in now.items() if any(s == "global" for s, _, _ in style_sources(r, tx)) and r.lower().endswith(STYLE_EXTS))
    out = []
    for rel in sorted(touched):
        if not rel.lower().endswith(SCREEN_EXTS):
            continue
        text = now[rel]
        old = Counter(c for c, _ in _template_classes(before[rel])) if rel in before else Counter()
        seen: Counter = Counter()
        bare = []
        for c, pos in _template_classes(text):
            seen[c] += 1
            if seen[c] <= old[c] or seen[c] > 1 + old[c] or c in glob or c in local.get(rel, ()):
                continue
            ln = _lineno(text, pos)
            others = [r for r in scoped_at.get(c, []) if r != rel]
            if others:
                out.append(_f(f"{rel}:{ln}", f"클래스 {c} 는 {others[0]} 의 scoped 스타일에만 있어 여기서는 걸리지 않는다 — 공통 스타일의 "
                                             "클래스를 쓰거나, 이 화면 스타일에 둔다", [f"{others[0]} .{c}"]))
            elif own_rules and not re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", hooks):
                bare.append((c, ln))
        if bare:
            out.append(_f(f"{rel}:{bare[0][1]}", f"스타일 규칙이 닿지 않는 클래스 {', '.join(c for c, _ in bare[:8])} — 공통 스타일"
                                                 f"({', '.join(globals_files[:2]) or '없음'})에 같은 일을 하는 클래스가 있으면 그것을 쓴다",
                          [f"{rel}:{ln} {_code_at(text, ln)}" for _, ln in bare[:3]]))
    return out


INLINE_STYLE_RX = re.compile(r"(?<![:\w-])style\s*=\s*([\"'])(.*?)\1", re.S)
DYNAMIC_RX = re.compile(r"\{\{|\$\{|<%|#\{|\{%|\[\[")


def _dup_inline_style(now: dict, before: dict, touched: set) -> list[dict]:
    """기존 화면 틀에 (값이 정해진) 인라인 style 이 하나도 없는데 새로 씀."""
    screens = [r for r in before if r not in touched and r.lower().endswith(SCREEN_EXTS)]
    if len(screens) < 2 or any(not DYNAMIC_RX.search(m.group(2)) for r in screens for m in INLINE_STYLE_RX.finditer(before[r])):
        return []
    sheets = sorted(r for r in now if r.lower().endswith(STYLE_EXTS))
    out = []
    for rel in sorted(touched):
        if not rel.lower().endswith(SCREEN_EXTS):
            continue
        text = now[rel]
        added = _added_lines(before.get(rel), text)
        hits = [_lineno(text, m.start()) for m in INLINE_STYLE_RX.finditer(text) if not DYNAMIC_RX.search(m.group(2))]
        hits = [ln for ln in hits if ln in added]
        if hits:
            out.append(_f(f"{rel}:{hits[0]}", f"인라인 style 속성 {len(hits)}개 — 이 프로젝트의 기존 화면에는 인라인 style 이 없다 "
                                              f"(스타일은 {', '.join(sheets[:2]) or '스타일 파일'} 의 클래스로)",
                          [f"{rel}:{ln} {_code_at(text, ln)}" for ln in hits[:3]]))
    return out


def _dup_lists(now: dict, before: dict, touched: set) -> list[dict]:
    """다른 파일에 이미 있는 값 목록을 또 씀 (['VIP', 'GOLD', 'BASIC'] 을 템플릿과 코드에 따로)."""
    idx: dict[tuple, list[tuple[str, int]]] = {}
    for rel, text in list(before.items()) + [(r, now[r]) for r in touched if r not in before]:
        if _is_catalog(rel):
            continue
        for vals, ln in literal_lists(rel, text):
            idx.setdefault(tuple(sorted(vals)), []).append((rel, ln))
    out = []
    for rel in sorted(touched):
        if _is_catalog(rel):
            continue
        text = now[rel]
        added = _added_lines(before.get(rel), text)
        done = set()
        for vals, ln in literal_lists(rel, text):
            key = tuple(sorted(vals))
            others = [(r, n) for r, n in idx.get(key, []) if r != rel]
            if ln not in added or key in done or not others:
                continue
            done.add(key)
            src = {**before, **now}
            # 불러 쓸 수 있는 곳(코드 파일의 상수)에 있으면 그것을 쓰게 하고, 이번 작업의 파일끼리 겹치면 한 곳에 모으게 한다. 원래 있던
            # 다른 화면의 템플릿 안에만 글로 있으면 불러 쓸 수 없다 — 그것을 고치라고 하면 요청 밖의 화면을 고치게 된다 (실험: 회원 상세
            # 템플릿의 등급 목록을 이유로 지적했더니 회원 화면을 고쳐 사용자가 되돌리라고 했다)
            code = [(r, n) for r, n in others if not r.lower().endswith(SCREEN_EXTS)]
            mine = [(r, n) for r, n in others if r in touched]
            if code:
                what = f"이 이미 {code[0][0]} 에 있음 — 그것을 그 자리에서 불러 쓴다"
            elif mine:
                what = f"을 이번 작업의 {mine[0][0]} 에도 따로 씀 — 한 곳에 두고 함께 쓴다"
            else:
                continue
            out.append(_f(f"{rel}:{ln}", f"값 목록 [{', '.join(vals[:6])}] {what}",
                          [f"{r}:{n} {_code_at(src.get(r, ''), n)}" for r, n in (code or mine)[:3]]))
    return out


def _dup_whitespace(now: dict, before: dict, changed: set) -> list[dict]:
    """기존 줄의 공백만 바꾼 곳 — 요청과 상관없는 변경이 차이에 섞인다."""
    import difflib
    out = []
    for rel in sorted(changed):
        if rel not in before:
            continue
        ol, nl = before[rel].replace("\r\n", "\n").splitlines(), now[rel].replace("\r\n", "\n").splitlines()
        hits = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ol, nl, autojunk=False).get_opcodes():
            if tag != "replace":
                continue
            if _squash("".join(ol[i1:i2])) == _squash("".join(nl[j1:j2])) and i2 - i1 != j2 - j1:
                hits.append(j1 + 1)  # 줄 나눔만 바꿈
                continue
            # 바꾼 덩어리 안에서 공백을 지우면 같은 줄 — 새 줄을 더하면서 기존 줄의 공백도 건드린 곳 (const like =(v, k))
            inner = difflib.SequenceMatcher(None, [_squash(x) for x in ol[i1:i2]], [_squash(x) for x in nl[j1:j2]], autojunk=False)
            for t2, a1, a2, b1, b2 in inner.get_opcodes():
                if t2 == "equal":
                    hits += [j1 + b1 + k + 1 for k in range(a2 - a1) if ol[i1 + a1 + k] != nl[j1 + b1 + k] and ol[i1 + a1 + k].strip()]
        if hits:
            out.append(_f(f"{rel}:{hits[0]}", f"기존 줄의 공백만 바꿈 {len(hits)}곳 — 요청과 상관없는 변경이라 되돌린다",
                          [f"{rel}:{ln} {nl[ln - 1].strip()[:100]}" for ln in hits[:3]]))
    return out


ESCAPE_RX = re.compile(r":global\(|:deep\(|::v-deep|/deep/|>>>|::?ng-deep|:has\(", re.I)


def _page_overrides(now: dict, before: dict, touched: set) -> list[dict]:
    """새로 만들거나 고친 화면의 스타일이 :global()·:deep()·body:has(.page) 로 공통 클래스를 이 화면에서만 다시 꾸민 곳.
    (원래 있던 공통 스타일 파일의 변경은 shared_edits 가 본다.)"""
    defined: dict[str, set] = {}
    for rel, text in before.items():
        for _, css, _ in style_sources(rel, text):
            for c in css_classes(css):
                defined.setdefault(c, set()).add(rel)
    out = []
    for rel in sorted(touched):
        if rel in before and rel.lower().endswith(STYLE_EXTS):
            continue
        text = now[rel]
        old = {sel for _, css, _ in style_sources(rel, before.get(rel, "")) for sel, _ in _css_rules(css)}
        classes: dict[str, str] = {}  # 다시 꾸민 공통 클래스 → 정의한 파일
        rules: list[tuple[int, str]] = []
        for _, css, off in style_sources(rel, text):
            for sel, _ in _css_rules(css):
                if sel in old or not ESCAPE_RX.search(sel):
                    continue
                hit = [c for c in CLASS_SEL_RX.findall(sel) if defined.get(c, set()) - {rel}]
                if not hit:
                    continue
                for c in hit:
                    classes.setdefault(c, sorted(defined[c] - {rel})[0])
                pos = text.find(sel.split()[0], off)
                rules.append((_lineno(text, pos if pos >= 0 else off), sel))
        if rules:  # 한 화면에 여럿이면 한 건으로 — 클래스 목록과 규칙 몇 개
            names = list(classes)
            out.append(_f(f"{rel}:{rules[0][0]}", f"공통 클래스 {', '.join('.' + c for c in names[:6])}{' …' if len(names) > 6 else ''} 를 "
                                                  f"이 화면에서만 다시 꾸밈 (:global·:deep·:has 규칙 {len(rules)}개) — 공통 부품의 모양을 화면마다 "
                                                  "바꾸지 않는다. 필요하면 공통 부품의 옵션·변형으로 (요청이 시킨 일이면 이유를 보고에)",
                          [f"{rel}:{ln} {sel[:90]}" for ln, sel in rules[:2]] + [f"{classes[names[0]]} .{names[0]}"]))
    return out


def duplicates(target: Path, base: dict, since: float | None = None, git_base: str | None = None, limit: int = 40) -> list[dict]:
    """이번 작업이 이미 있는 것을 다시 만든 곳 [{at: '파일:줄', summary, evidence: [기존 것 '파일:줄 …'], kind: 'reuse'}].
    base: 원래 소스 기록 (shared_edits 와 같다). git_base: 작업을 시작한 커밋 — 기록에 없는 번역 json·properties 의 원래 내용.
    since: 기록한 시각 — 기록에 없어도 그 전부터 그대로 있던 파일은 원래 것."""
    t = Path(target).resolve()
    if not base:
        return []
    paths = src_files(t)
    now = {rel: _read(p) for rel, p in paths.items()}
    git_files = None
    if git_base:
        prefix = (_git(t, "rev-parse", "--show-prefix") or "").strip()
        listed = _git(t, "ls-tree", "-r", "--name-only", "--full-name", git_base)
        if listed is not None:
            git_files = {x[len(prefix):] for x in listed.splitlines() if x.startswith(prefix)}
    before: dict[str, str] = {}
    changed, new = set(), set()
    for rel, text in now.items():
        state, old = _before_state(t, rel, text, base, since, git_base, git_files)
        if state == "same":
            before[rel] = text
        elif state == "changed":
            before[rel] = old or ""
            changed.add(rel)
        elif state == "new":
            new.add(rel)
    touched = changed | new
    if not touched:
        return []
    out: list[dict] = []
    for fn in (lambda: _dup_catalog(now, before, touched), lambda: _dup_namespace(now, before, touched),
               lambda: _dup_functions(now, before, touched), lambda: _dup_classes(t, now, before, touched),
               lambda: _dup_inline_style(now, before, touched), lambda: _dup_lists(now, before, touched),
               lambda: _dup_whitespace(now, before, changed), lambda: _page_overrides(now, before, touched)):
        out += fn()
    seen, uniq = set(), []
    for f in out:
        if (f["at"], f["summary"]) not in seen:
            seen.add((f["at"], f["summary"]))
            uniq.append(f)
    return uniq[:limit]
