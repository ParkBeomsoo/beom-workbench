"""기획 모드의 산출물과 점검 — 기획자가 개발·디자인·QA 에 넘기는 '기획 패키지'.

대상 폴더의 .beom/brief/ 에 둔다:
  기획서.md        한 장 요약·목표·참고 서비스·사용자·기능 목록(P0/P1/P2)·요구사항(R1…)·지표와 이벤트·범위 밖·열린 질문
  정책서.md        규칙을 표로 — 조건별 결과(결정표), 계산식과 숫자 예시, 상태 변화, 예외, 데이터와 API 초안
  화면설계.md      화면 목록(S1…) — 화면마다 목적·들어오는 길·보이는 것(번호 설명)·상태(빈·오류·로딩)·관련 R
  wireframes/S1-이름.html   흑백 와이어프레임 (화면마다, 번호 표시) — 툴킷이 스크린샷으로 남긴다(shots/)
  테스트시나리오.md T1… — 요구사항(R)마다 주어진 상황·행동·기대 결과. '이 기획으로 만들기'의 합격 기준이 된다

check() 는 문서끼리 맞물리는지 기계적으로 본다 — 빠진 문서·절, 시나리오가 없는 요구사항, 와이어프레임이 없는 화면,
화면이 안 붙은 요구사항, 정의 안 된 번호(Q·R·S·T), 화면 이름 어긋남, 상태 표·번호 표시, 근거 없는 '그대로 따름',
기획서 길이·개발 말, 보기 틀에서 깨지는 와이어프레임 스크립트. 기획 에이전트는 인계 전에 이것을 통과해야 한다(wb_handoff).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from beom_workbench.common.response import ToolError

DOCS = {
    "기획서.md": ["요약", "목표", "참고", "사용자", "기능", "요구사항", "지표", "범위", "질문"],
    "정책서.md": ["데이터", "외부 연동"],
    "화면설계.md": [],
    "테스트시나리오.md": [],
}
TITLES = {"기획서.md": "기획서", "정책서.md": "정책서", "화면설계.md": "화면 설계", "테스트시나리오.md": "테스트 시나리오"}
_R, _T = "R", "T"  # 요구사항·테스트 시나리오 번호의 머리글자


WORK = ".beom"            # 대상 폴더 안 툴킷 작업 폴더 (기획 패키지·합격 케이스·임시 스크립트)
def folder(target_root: str | Path) -> Path:
    return Path(target_root) / WORK / "brief"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() and not p.is_symlink() else ""
    except OSError:
        return ""


def _section(md: str, keyword: str) -> str:
    """'## …keyword…' 절의 본문 (다음 ## 전까지)."""
    m = re.search(rf"^##+[^\n]*{re.escape(keyword)}[^\n]*\n(.*?)(?=^##\s|\Z)", md, re.M | re.S)
    return m.group(1) if m else ""


def _ids(prefix: str, text: str) -> list[str]:
    """R1·T3 같은 번호 (R01 도 R1 로). 앞뒤가 글자·숫자가 아닌 곳만 (PR12·R2D2 는 아님)."""
    pat = r"(?<![A-Za-z0-9])" + prefix + r"(\d{1,3})(?![A-Za-z0-9])"
    return sorted({f"{prefix}{int(n)}" for n in re.findall(pat, text)}, key=lambda x: int(x[1:]))


def _defined_screens(md: str) -> list[str]:
    """화면설계.md 에서 제목(#)이나 표 첫 칸에 나온 S 번호 = 정의된 화면."""
    found = set()
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("#") or s.startswith("|"):
            head = s.lstrip("#").strip() if s.startswith("#") else s.strip("|").split("|")[0]
            m = re.match(r"\s*S(\d{1,3})(?![0-9A-Za-z])", head)
            if m:
                found.add(f"S{int(m.group(1))}")
    return sorted(found, key=lambda x: int(x[1:]))


def _first_cell_ids(md: str, prefix: str) -> set[str]:
    """제목(#)이나 표 첫 칸에 나온 번호 = 그 문서가 정의한 번호 (T1… 같은). 본문 속 언급은 정의가 아니다."""
    out = set()
    for line in md.splitlines():
        s = line.strip()
        head = s.lstrip("#").strip() if s.startswith("#") else s.strip("|").split("|")[0] if s.startswith("|") else ""
        m = re.match(rf"\s*\**{prefix}(\d{{1,3}})(?![0-9A-Za-z])", head)
        if m:
            out.add(f"{prefix}{int(m.group(1))}")
    return out


def _refs(prefix: str, text: str) -> set[str]:
    """번호 언급 — 'R3~R7'·'R3–7' 같은 범위는 사이 번호까지 (화면설계에 'R3~R7' 로 붙인 R5 를 '안 나옴'으로 보지 않게)."""
    got = set(_ids(prefix, text))
    for a, b in re.findall(rf"(?<![A-Za-z0-9]){prefix}(\d{{1,3}})\s*[~～–—-]\s*{prefix}?(\d{{1,3}})(?![0-9])", text):
        if 0 < int(b) - int(a) <= 60:
            got |= {f"{prefix}{n}" for n in range(int(a), int(b) + 1)}
    return got


def _rows(md: str) -> list[list[str]]:
    """표의 본문 줄(머리줄·구분줄 빼고) — 칸 목록으로. 표가 여러 개면 이어서."""
    out, prev = [], False
    for line in md.splitlines():
        s = line.strip()
        is_row = s.startswith("|")
        if is_row and not re.fullmatch(r"\|?[\s:|-]+\|?", s) and prev:
            out.append([c.strip() for c in s.strip("|").split("|")])
        prev = is_row  # 표의 첫 줄(머리줄)은 앞줄이 표가 아니다
    return out


def _tables(md: str) -> list[tuple[list[str], list[list[str]]]]:
    """[(머리줄 칸, 본문 줄들)] — 표마다."""
    out, cur = [], None
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            cur = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cur is None:
            cur = (cells, [])
            out.append(cur)
        elif not re.fullmatch(r"\|?[\s:|-]+\|?", s):
            cur[1].append(cells)
    return out


def _screen_sections(md: str) -> dict[str, str]:
    """'## S1 화면 이름' 절마다 본문."""
    return {f"S{int(m.group(1))}": m.group(2)
            for m in re.finditer(r"^##\s+S(\d{1,3})(?![0-9A-Za-z])[^\n]*\n(.*?)(?=^##\s|\Z)", md, re.M | re.S)}


def _norm(s: str) -> str:
    return re.sub(r"[\s·*`_\[\]()\-—:,.'\"]+", "", re.sub(r"\]\([^)]*\)", "]", s))


def _alike(a: str, b: str) -> bool:
    """두 이름이 같은 화면을 말하나 — 한쪽이 다른 쪽을 품거나 두 글자 이상 겹치면 같은 것으로 (말투 차이까지 막지 않게)."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return True
    return a in b or b in a or any(a[i:i + 2] in b for i in range(len(a) - 1))


def _screen_names(md: str) -> dict[str, list[str]]:
    """화면 번호 → 이름들 (화면 목록 표의 '화면' 칸과 '## S1 이름' 제목)."""
    names: dict[str, list[str]] = {}
    for head, rows in _tables(md):
        ni = next((k for k, c in enumerate(head) if k > 0 and re.search(r"화면|이름", c)), -1)
        if not head or not re.fullmatch(r"\**S\**", head[0].strip()) or ni < 0:
            continue
        for c in rows:
            m = re.match(r"\**S(\d{1,3})\**$", c[0]) if c else None
            if m and len(c) > ni and c[ni]:
                names.setdefault(f"S{int(m.group(1))}", []).append(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", c[ni]).replace("**", ""))
    for m in re.finditer(r"^##\s+S(\d{1,3})(?![0-9A-Za-z])\s*[·:\-—]?\s*(.+)$", md, re.M):
        names.setdefault(f"S{int(m.group(1))}", []).append(m.group(2).strip())
    return names


# 상태 표 — 화면마다 이 네 상태는 꼭 정한다 (없으면 그 줄에 '해당 없음'). 같은 뜻의 말도 받는다
STATES = {"기본": ("기본", "초기", "평소"), "빈": ("빈", "비어", "없음", "0개", "0건"),
          "오류": ("오류", "에러", "실패"), "로딩": ("로딩", "불러오", "확인 중", "처리 중", "저장 중", "기다리는 중")}
NO_SCREEN = "화면 없음"   # 기획서 요구사항 줄에 '(화면 없음)' — 자동 처리·서버 규칙처럼 화면이 없는 요구사항
STATUSES = ("그대로 따름", "변경", "신규(추천)", "외부 규격", "가정(기존 문서 없음)", "사용자 결정")
_EXT_CITE = re.compile(r"https?://|\d+\s*쪽|\d+\s*p\b|절|가이드|문서|규격서|레퍼런스|reference|guide|docs?", re.I)
# 기획서 글자 수 상한 (공백·마크다운 기호·링크 주소 빼고, 표 안 글자 포함) — '한두 화면'. 표를 빼고 세면 막지 못한다: 실제 기획서는
# 글이 300~800자, 표가 1,500~3,000자였고 읽기 어렵다던 것은 표가 길어진 쪽이었다. 넘는 설명·규칙은 정책서·화면설계로
PLAN_BODY_LIMIT = 3500
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
# 와이어프레임 공용 도우미 — 각 화면 <head> 맨 앞에 그대로 둔다. 데모 값은 창 이름(window.name)에 두어, 직접 열어 시험할 때도
# (같은 탭에서 화면을 옮겨도 남는다) 앱 보기에서도(보기가 화면 사이에 이어 주고, proto-frame.js 가 이 위에 덮는다) 같게 동작한다.
# 점검은 이 줄의 location.assign 만 봐준다 — 그대로 복사해야 한다. runner.PLAN_RULES 에 같은 줄이 실린다(테스트가 맞춰 본다).
PV_SHIM = ('<script>window.pv=window.pv||(function(){function r(){try{return name.indexOf("pv-state:")?{}:JSON.parse(name.slice(9))}'
           'catch(e){return{}}}return{state:{get:function(k){var v=r()[k];return v===undefined?null:v},set:function(k,v){var o=r();'
           'o[k]=v;name="pv-state:"+JSON.stringify(o)}},go:function(h){location.assign(h)}}})()</script>')
# 보기 틀(<iframe sandbox="allow-scripts">) 안에서 깨지는 것 — 저장소는 막혀 예외가 나고, 스크립트로 옮긴 화면은 보기가 모른다
_WF_BANNED = [("localStorage", re.compile(r"\blocalStorage\b")), ("sessionStorage", re.compile(r"\bsessionStorage\b")),
              ("location 이동", re.compile(r"\blocation\s*(?:\.\s*(?:href|pathname|search)\s*)?=(?!=)|"
                                          r"\blocation\s*\.\s*(?:assign|replace)\s*\(|\bwindow\s*\.\s*navigate\s*\("))]
# 어떤 말이 어려운지는 낱말 목록으로 정하지 않는다 (에이전트가 읽는 사람을 보고 정한다) — 코드 이름 모양(closesAt)만 본다.
_CAMEL = re.compile(r"(?<![A-Za-z0-9_/.])[a-z]{2,}(?:[A-Z][a-z0-9]+)+(?![A-Za-z0-9_/])")  # closesAt 같은 코드 이름


def _strip_urls(md: str) -> str:
    return re.sub(r"https?://\S+", " ", re.sub(r"\]\([^)]*\)", "]", md))


def _body_len(md: str) -> int:
    """읽을 글자 수 — 공백·마크다운 기호(#·>·|·*·`·-)·링크 주소를 뺀다. 표 안 글자는 센다 (읽는 양은 표가 대부분이다)."""
    return len(re.sub(r"\s+|[#>|*_`:\-]", "", _strip_urls(md)))


SRC_SKIP = {".beom", ".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", "target", ".idea", ".vscode"}
SRC_DOC_EXT = {".md", ".pdf", ".docx", ".doc", ".hwp", ".hwpx", ".txt", ".xlsx", ".pptx"}
SRC_CODE_EXT = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".java", ".kt", ".go", ".rb", ".php", ".cs",
                ".swift", ".dart", ".rs", ".scala", ".sql", ".html", ".jsp", ".c", ".cpp", ".h"}


def sources(target_root: str | Path, limit: int = 3000) -> dict:
    """대상 폴더(.beom 밖)에 이미 있던 문서·코드 — '그대로 따름'이 인용할 수 있는 것. {"docs", "code", "truncated"}."""
    root = Path(target_root)
    docs, code, n = [], [], 0
    if not root.is_dir():
        return {"docs": docs, "code": code, "truncated": False}
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SRC_SKIP and not x.startswith(".")]
        for name in names:
            n += 1
            if n > limit:
                return {"docs": docs, "code": code, "truncated": True}
            ext = Path(name).suffix.lower()
            rel = Path(d, name).relative_to(root).as_posix()
            if ext in SRC_DOC_EXT:
                docs.append(rel)
            elif ext in SRC_CODE_EXT:
                code.append(rel)
    return {"docs": docs, "code": code, "truncated": False}


_FILE_REF = re.compile(r"[\w가-힣.-]+\.(?:" + "|".join(sorted(e[1:] for e in SRC_DOC_EXT | SRC_CODE_EXT)) + r")\b", re.I)


def _cites(cell: str, known: list[str], truncated: bool) -> bool:
    """'기존 정책' 칸이 실제로 있는 문서·코드를 가리키나 — 이름(확장자 빼도)으로. 다 못 훑었으면 파일 이름 꼴이면 믿는다."""
    if re.search(r"문서\s*없음|가정|^\s*없음|^\s*[-—]\s*$|신규\s*프로젝트", cell):
        return False
    low = _norm(cell).lower()
    for k in known:
        p = Path(k.rstrip("/\\"))
        if any(len(x) >= 2 and _norm(x).lower() in low for x in (p.name, p.stem)):
            return True
    return truncated and bool(_FILE_REF.search(cell))


def _wf_markers(html: str) -> set[str]:
    """와이어프레임의 번호 표시 — class 에 n 이 든 요소 바로 안의 ①…. 스크립트가 `<span class="n">${…}` 로 그려 넣는 화면은
    (실제 실행에서는 보기가 그 표시를 찾는다) 스크립트 속 번호 글자도 표시로 친다."""
    out = set()
    for m in re.finditer(r"<\w+([^>]*)>\s*([" + CIRCLED + r"])", html):
        cls = re.search(r"""class\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", m.group(1))
        if cls and "n" in (cls.group(1) or cls.group(2) or cls.group(3) or "").split():
            out.add(m.group(2))
    scripts = " ".join(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I))
    if re.search(r"""class(?:Name)?\s*=\s*\\?["']n(?:\s[^"']*)?\\?["']|classList\.add\(\s*["']n["']""", scripts):
        out |= set(re.findall(f"[{CIRCLED}]", scripts))
    return out


# 이 기획으로 만들 때 툴킷이 확인하는 것 — 다른 도구·사람에게 넘겨도 같은 관점으로 만들게 기획 패키지에 함께 싣는다
BUILD_CHECKS = [
    "테스트 시나리오가 합격 기준이다 — 시나리오마다 실제로 돌려 본 결과로 확인한다 (말로 '확인했다'가 아니라)",
    "화면마다 기본·빈·오류·로딩 상태를 만든다 — 아래 '화면마다 확인할 상태'에 빠진 것이 없는지",
    "오류는 무엇이 잘못됐고 어떻게 고치는지 알려 주는 문구로 — 입력 칸 옆에, 사과나 막연한 말 없이",
    "글자 대비 4.5:1 이상(큰 글자 3:1), 누르는 곳 24px 이상(모바일 44px 권장), 키보드 포커스가 보이게",
    "정한 화면 폭(데스크톱 1280px·모바일 390px)에서 가로로 넘치거나 글자가 잘리지 않게, 한글이 한두 글자씩 꺾이지 않게",
    "고른 글꼴이 실제로 불러와지는지 — 못 불러오면 이 컴퓨터에 있는 글꼴로 바꾼다",
    "기존 화면이 있으면 그 틀·공통 부품·문구 말투를 따른다 — 새 디자인을 섞지 않는다",
    "권한·개인정보: 누가 볼 수 있고 바꿀 수 있는지, 개인정보는 필요한 만큼만 보이고 남긴다 (정책서의 규칙대로)",
    "열린 질문(Q)이 정해지기 전에는 그 값을 확정된 것처럼 만들지 않는다 — 추천안으로 만들고 바꾸기 쉽게 둔다",
]


def requirements(target_root: str | Path) -> dict[str, str]:
    """기획서의 요구사항 — {R1: 내용} (표의 첫 칸이 R 번호인 줄, 또는 '- R1 …' 목록)."""
    plan = _read(folder(target_root) / "기획서.md")
    reqs: dict[str, str] = {}
    for head, rows in _tables(plan):
        for r in rows:
            m = re.match(rf"\s*\**({_R}\d{{1,3}})\**\s*$", r[0]) if r else None
            if m and len(r) > 1:
                reqs.setdefault(f"R{int(m.group(1)[1:])}", re.sub(r"<[^>]+>", "", r[1]).strip()[:160])
    for line in plan.splitlines():
        m = re.match(rf"\s*[-*]\s+\**({_R}\d{{1,3}})\**[\s:.)—-]+(.+)", line)
        if m:
            reqs.setdefault(f"R{int(m.group(1)[1:])}", re.sub(r"<[^>]+>|\*\*", "", m.group(2)).strip()[:160])
    return reqs


def checklist(target_root: str | Path) -> str:
    """개발 인계 체크리스트(마크다운) — 기획 문서에서 툴킷이 모은다 (에이전트가 쓴 글이 아니라 문서의 표·번호에서).
    요구사항마다 확인할 시나리오·화면, 먼저 정할 열린 질문, 화면마다 상태, 만들 때 확인할 것, 개발 참고."""
    d = folder(target_root)
    text = {n: _read(d / n) for n in DOCS}
    plan, policy, scr, tests = (text.get(n, "") for n in ("기획서.md", "정책서.md", "화면설계.md", "테스트시나리오.md"))
    reqs = requirements(target_root)
    by_t: dict[str, list[str]] = {}
    for head, rows in _tables(tests):
        for r in rows:
            t = _ids(_T, r[0]) if r else []
            if t:
                for rid in _refs(_R, " ".join(r[1:])):
                    by_t.setdefault(rid, []).append(t[0])
    by_s: dict[str, list[str]] = {}
    for head, rows in _tables(scr):
        for r in rows:
            m = re.match(r"\s*S(\d{1,3})(?![0-9A-Za-z])", r[0]) if r else None
            if m:
                for rid in _refs(_R, " ".join(r[1:])):
                    by_s.setdefault(rid, []).append(f"S{int(m.group(1))}")
    out = ["# 개발 인계 체크리스트", "",
           "> 이 기획을 만들 사람(또는 에이전트)이 빠뜨리지 않게 기획 문서의 표와 번호에서 툴킷이 모았다. "
           "'이 기획으로 만들기'로 만들면 툴킷이 아래를 실제로 확인하고, 다른 곳에서 만들면 이 목록으로 확인한다.", ""]
    if reqs:
        out += ["## 요구사항과 확인 방법", "", "| R | 요구사항 | 확인할 시나리오 | 화면 |", "|---|---|---|---|"]
        for rid in sorted(reqs, key=lambda x: int(x[1:])):
            t = ", ".join(sorted(set(by_t.get(rid, [])), key=lambda x: int(x[1:]))) or "**없음 — 시나리오를 더한다**"
            s = ", ".join(sorted(set(by_s.get(rid, [])), key=lambda x: int(x[1:]))) or "—"
            out.append(f"| {rid} | {reqs[rid]} | {t} | {s} |")
        out.append("")
    qs = [ln.strip("-* ").strip() for ln in (_section(plan, "열린 질문") or _section(plan, "질문")).splitlines()
          if re.search(r"(?<![A-Za-z0-9])Q\d{1,3}", ln)]
    rows_q = [r for _, rows in _tables(_section(plan, "열린 질문") or _section(plan, "질문")) for r in rows if r and re.match(r"\s*\**Q\d", r[0])]
    if rows_q or qs:
        out += ["## 만들기 전에 정해야 할 것 (열린 질문)", ""]
        out += [f"- [ ] {' — '.join(c for c in r if c)[:220]}" for r in rows_q] if rows_q else [f"- [ ] {q[:220]}" for q in qs[:20] if not q.startswith("|")]
        out.append("")
    secs = _screen_sections(scr)
    if secs:
        out += ["## 화면마다 확인할 상태", "", "| 화면 | 기본 | 빈 | 오류 | 로딩 |", "|---|---|---|---|---|"]
        names = _screen_names(scr)
        for sid in sorted(secs, key=lambda x: int(x[1:])):
            body = secs[sid]
            def has(word, body=body):
                row = next((r for r in _rows(body) if r and _norm(r[0]).startswith(_norm(word))), None)
                return "해당 없음" if row and "해당 없음" in " ".join(row) else ("있음" if row else "**빠짐**")
            nm = (names.get(sid) or [""])[0]
            out.append(f"| {sid} {nm} | {has('기본')} | {has('빈')} | {has('오류')} | {has('로딩')} |")
        out.append("")
    out += ["## 만들 때 확인할 것", ""] + [f"- [ ] {c}" for c in BUILD_CHECKS] + [""]
    dev = _section(plan, "개발 참고")
    if dev.strip():
        out += ["## 개발 참고 — 어디에 무엇을", "", dev.strip(), ""]
    return "\n".join(out)


def leftovers(target_root: str | Path, changes: list[dict]) -> list[dict]:
    """기획을 고친 뒤 옛 값이 남은 곳 — 문서와 와이어프레임에서 old 가 든 줄 (그 줄에 new 나 keep 의 말이 있으면 일부러 둔 것으로 본다).
    [{"old", "file", "line", "text"}]. 사람이 고친 값이 한 곳만 바뀌고 다른 문서·예시·시나리오에 옛 값이 남는 일을 막는다."""
    d = folder(target_root)
    files = [d / n for n in DOCS] + (sorted((d / "wireframes").glob("*.html")) if (d / "wireframes").is_dir() else [])
    out = []
    for ch in changes:
        old, new = str(ch.get("old") or "").strip(), str(ch.get("new") or "").strip()
        keep = [str(k) for k in ch.get("keep") or [] if str(k).strip()]
        if not old:
            continue
        for f in files:
            for i, line in enumerate(_read(f).splitlines(), 1):
                if old in line and not (new and new in line) and not any(k in line for k in keep):
                    out.append({"old": old, "file": f.relative_to(d).as_posix(), "line": i, "text": line.strip()[:160]})
    return out


def check(target_root: str | Path, policies: list[str] | None = None, changes: list[dict] | None = None,
          user_words: str | None = None) -> dict:
    """policies: 대상 폴더의 정책·규칙 문서 (있으면 정책서에 '기존 정책 대조' 절이 있어야 한다).
    user_words: 사용자의 말(요청·이어서 보낸 말·질문의 답) — 주면 '사용자 결정' 줄이 그 말을 인용했는지 본다.
    changes: 기획을 고칠 때 바꾼 값 [{"old": "1시간", "new": "2시간", "keep": ["취소 마감"]}] — 옛 값이 남은 곳이 있으면 문제로.
    돌려주는 것: problems(인계를 막음)·notes(참고)는 글 목록 그대로, findings 는 같은 내용을 {"level", "code", "text"} 로,
    log 는 실행 기록에 남기기 좋은 짧은 요약(개수와 code 목록) — 부르는 쪽이 그대로 inbox.log 에 넘길 수 있다."""
    d = folder(target_root)
    texts = {name: _read(d / name) for name in DOCS}
    problems: list[str] = []
    notes: list[str] = []
    findings: list[dict] = []

    def problem(code: str, text: str) -> None:
        problems.append(text)
        findings.append({"level": "problem", "code": code, "text": text})

    def note(code: str, text: str) -> None:
        notes.append(text)
        findings.append({"level": "note", "code": code, "text": text})

    missing_docs = [n for n, t in texts.items() if not t.strip()]
    for n in missing_docs:
        problem("missing_doc", f"{n} 가 없음")
    for name, kws in DOCS.items():
        heads = [l for l in texts[name].splitlines() if l.startswith("#")]
        for kw in kws if texts[name] else []:
            if not any(kw in h for h in heads):
                problem("missing_section", f"{name} 에 '{kw}' 절이 없음")
    # 외부 연동 확인 — 마지막 칸이 출처(URL)나 '확인 못 함'인가. 값이 맞는지는 보지 않고 칸이 채워졌는지만 본다 (검색 요약으로 정한
    # 외부 규격 값을 '가정'으로만 남겨, 실제로 확인해야 할 것을 사용자가 몰랐다 — PFO 기획)
    ext = _section(texts["정책서.md"], "외부 연동") if texts["정책서.md"] else ""
    head = next((l for l in ext.splitlines() if l.strip().startswith("|")), "")
    cols = [i for i, c in enumerate(head.strip().strip("|").split("|")) if "확인한 곳" in c] or [-1]  # '확인한 곳' 칸마다 (규격·시험 방법)
    bare = []
    for c in _rows(ext):  # 머리줄·구분줄은 이미 빠진다
        if len(c) < 2 or c[0].strip() == "없음":  # '없음' 한 줄 = 외부 연동이 없는 기획
            continue
        miss = [head.strip().strip("|").split("|")[i].strip() if i >= 0 else "확인한 곳" for i in cols
                if i < len(c) and "http" not in c[i] and "확인 못 함" not in c[i]]
        if miss:
            bare.append(f"{c[0].strip()}({', '.join(miss)})")
    if bare:
        problem("external_unverified", "외부 연동 확인 표에 출처가 없는 칸: " + "; ".join(bare[:6])
                + " — 공식 문서(시험 방법은 개발자 문서)를 열어 URL 을 적거나, 찾지 못했으면 '확인 못 함 — 찾아본 곳'으로 적는다")
    plan = texts["기획서.md"]
    req_sec = _section(plan, "요구사항") if plan else ""
    reqs = _ids(_R, req_sec) if plan else []
    if plan and not reqs:
        problem("no_requirements", "기획서.md 의 요구사항 절에 R1, R2 … 번호가 없음")
    policy = texts["정책서.md"]
    if policies and policy:
        heads = [h for h in policy.splitlines() if h.startswith("#")]
        if not any("기존 정책" in h for h in heads):
            problem("no_policy_compare", "정책서.md 에 '기존 정책 대조' 절이 없음 — 대상 폴더의 정책·규칙 문서(" + ", ".join(policies[:4])
                    + ")를 인용해 그대로 따름·변경·신규(추천)과 상충 여부를 표로 쓴다")
    if policy and "|" not in policy:
        note("no_policy_table", "정책서.md 에 표가 없음 — 조건별 결과는 표(결정표)로 쓰면 개발·QA 가 그대로 확인할 수 있음")
    _check_compare(target_root, policy, policies, problem)
    if user_words is not None:
        _user_decision_check(texts, user_words, problem)
    _check_dev_map(target_root, policy, problem)
    tests = texts["테스트시나리오.md"]
    uncovered = [r for r in reqs if r not in _refs(_R, tests)] if tests else []
    if uncovered:
        problem("untested_requirement", f"테스트 시나리오가 없는 요구사항: {', '.join(uncovered)}")
    if tests and not _ids(_T, tests):
        problem("no_tests", "테스트시나리오.md 에 T1, T2 … 번호가 없음")
    screens_md = texts["화면설계.md"]
    screens = _defined_screens(screens_md)
    if screens_md and not screens:
        problem("no_screens", "화면설계.md 에 S1, S2 … 화면 번호가 없음 (제목이나 표 첫 칸)")
    wf = d / "wireframes"
    htmls = sorted(p for p in wf.glob("*.html") if p.is_file()) if wf.is_dir() else []
    wf_ids = {f"S{int(m.group(1))}" for p in htmls if (m := re.match(r"S(\d{1,3})", p.stem))}
    wf_of: dict[str, Path] = {}
    for p in htmls:
        if (m := re.match(r"S(\d{1,3})(?![0-9])", p.stem)):
            wf_of.setdefault(f"S{int(m.group(1))}", p)
    if screens_md:
        _picture_rule_check(screens_md, wf_of, problem, note)
    no_wf = [s for s in screens if s not in wf_ids]
    if no_wf:
        problem("missing_wireframe", f"와이어프레임이 없는 화면: {', '.join(no_wf)} (wireframes/S1-이름.html)")
    # 프로토타입: 화면끼리 링크(S2-이름.html)가 없는 화면을 가리키면 눌렀을 때 아무 데도 못 간다
    known = set(screens) | wf_ids
    broken = sorted({f"{p.stem} → S{int(n)}" for p in htmls
                     for n in re.findall(r"""(?:href|data-go)\s*=\s*["']?(?:[^"'>]*[\\/])?S(\d{1,3})(?![0-9])""", _read(p), re.I)
                     if f"S{int(n)}" not in known})
    if broken:
        problem("broken_link", f"없는 화면으로 가는 링크: {', '.join(broken[:8])}")
    if htmls and not any(re.search(r"""href\s*=\s*["']?(?:[^"'>]*[\\/])?S\d{1,3}""", _read(p), re.I) for p in htmls) and len(htmls) > 1:
        note("no_links", "화면끼리 이어지는 링크가 없음 — 버튼·메뉴를 <a href=\"S2-이름.html\"> 로 이으면 앱에서 눌러 보며 이동할 수 있음")
    # 보기 틀(sandbox) 안에서는 저장소가 막히고, 스크립트로 옮긴 화면은 보기가 모른다 — 에이전트 시험(일반 브라우저)과 달라진다
    bad_js = []
    for p in htmls:
        src = re.sub(r"<!--.*?-->|/\*.*?\*/", " ", _read(p).replace(PV_SHIM, ""), flags=re.S)
        hit = [label for label, pat in _WF_BANNED if pat.search(src)]
        if hit:
            bad_js.append(f"{p.name}({'·'.join(hit)})")
    # 툴킷이 제품 모양(_theme.css)을 깔아 두었는데 화면이 키트만 쓰면 중립 와이어프레임이 된다 — 규칙이 아니라 사실로 막는다
    if wf.is_dir() and (wf / "_theme.css").is_file():
        plain = [p.name for p in htmls if "_kit.css" in _read(p) and "_theme.css" not in _read(p)]
        if plain:
            problem("wireframe_no_product_look", "제품 모양(_theme.css)을 안 쓰는 화면: " + ", ".join(plain[:8])
                    + " — <link href=\"_kit.css\"> 바로 다음에 <link rel=\"stylesheet\" href=\"_theme.css\"> 를 넣는다")
    if bad_js:
        problem("wireframe_storage_or_nav", "와이어프레임이 앱 보기에서 깨지는 것을 씀: " + ", ".join(bad_js[:8])
                + " — 화면 이동은 <a href>·data-go 로만(스크립트에서는 그 링크를 .click() 하거나 pv.go(\"S2-이름.html\")), "
                  "화면 사이 값은 pv.state.get/set 로만 (도우미 줄은 <head> 맨 앞에 그대로)")
    if screens_md and screens and not re.search(r"^\s*\|\s*S\s*\|[^\n]*기능", screens_md, re.M):
        note("no_feature_column", "화면 목록 표에 '기능' 칸이 없음 — 있으면 앱의 화면 설계가 기능별 탭으로 나뉨")
    shots_dir = d / "shots"
    no_shot = [p.name for p in htmls if not any((shots_dir / f"{p.stem}{sfx}.png").is_file() for sfx in ("", "-mobile"))]
    if no_shot:
        note("no_shot", f"스크린샷을 아직 안 찍은 와이어프레임: {', '.join(no_shot)} — wb_brief_screens 로 찍는다")
    # 요구사항이 어느 화면에도 안 붙었으면 막는다 — 화면이 없는 요구사항은 기획서에 '(화면 없음)'으로 밝힌다
    if screens_md and reqs:
        exempt = {i for line in req_sec.splitlines() if re.search(r"화면\s*없음", line) for i in _ids(_R, line)[:1]}
        unscreened = [r for r in reqs if r not in _refs(_R, screens_md) and r not in exempt]
        if unscreened:
            problem("unscreened_requirement", f"화면설계에 안 나오는 요구사항: {', '.join(unscreened)} — 화면 목록의 관련 R·화면 절에 "
                    f"붙이거나, 화면이 없는 규칙이면 기획서 요구사항 줄 끝에 '({NO_SCREEN})'")
    sections = _screen_sections(screens_md)
    # 상태 표: 화면마다 기본·빈·오류·로딩 — 없는 상태도 '해당 없음'으로 적어야 개발·QA 가 빠뜨린 것과 구분한다
    lacking = []
    for sid, body in sections.items():
        heads = [c[0] for _, rows in _tables(body) for c in rows if c]  # 상태 표의 첫 칸 (번호 설명 속 '오류'는 상태가 아니다)
        na = [l for l in body.splitlines() if re.search(r"해당\s*없음", l)]
        miss = [k for k, words in STATES.items()
                if not any(w in h for h in heads for w in words) and not any(k in l for l in na)]
        if miss:
            lacking.append(f"{sid}({'·'.join(miss)})")
    if lacking:
        problem("missing_states", "상태 표에 빠진 상태: " + ", ".join(lacking[:12]) + (" …" if len(lacking) > 12 else "")
                + " — 화면마다 기본·빈·오류·로딩 줄을 두고, 그 화면에 없는 상태는 그 줄에 '해당 없음'")
    # 설명 번호(①…)와 와이어프레임의 번호 표시 — 앱은 <span class="n">① 로 오른쪽 설명과 화면 자리를 잇는다
    unmarked = []
    for sid, body in sections.items():
        p = wf_of.get(sid)
        if not p:
            continue
        want = sorted(set(re.findall(f"[{CIRCLED}]", body)), key=CIRCLED.index)
        have = _wf_markers(_read(p))
        miss = [c for c in want if c not in have]
        if miss:
            unmarked.append(f"{sid} {''.join(miss)}")
    if unmarked:
        problem("missing_markers", "설명 번호가 와이어프레임에 없음: " + ", ".join(unmarked[:10])
                + " — 해당 요소 옆에 <span class=\"n\">①</span> 로 붙인다")
    # 정의 안 된 번호 — 다른 문서가 없는 질문·요구사항·화면·시나리오를 가리키면 읽는 사람이 따라갈 수 없다
    q_sec = _section(plan, "질문") if plan else ""
    defined = {"R": set(reqs) if reqs else None, "S": set(screens) if screens else None,
               "T": _first_cell_ids(tests, "T") or None if tests else None, "Q": set(_ids("Q", q_sec)) if plan else None}
    undefined = []
    for prefix, ok_ids in defined.items():
        if ok_ids is None:
            continue
        for name, text in texts.items():
            body = _strip_urls(text)
            if prefix == "Q":  # '2027년 Q1' 같은 분기는 질문 번호가 아니다
                body = re.sub(r"(?:\d{2,4}\s*년?|FY\s*\d{2,4})\s*Q[1-4]|Q[1-4]\s*분기", " ", body)
            for i in _ids(prefix, body):
                if i not in ok_ids:
                    undefined.append(f"{i}({name})")
    if undefined:
        problem("undefined_id", "정의되지 않은 번호를 가리킴: " + ", ".join(list(dict.fromkeys(undefined))[:16])
                + " — Q 는 기획서 열린 질문, R 은 요구사항, S 는 화면 목록, T 는 시나리오 표 첫 칸에 있어야 한다")
    # 정책서·시나리오가 화면을 이름과 함께 쓸 때 화면 목록과 다른 이름이면 (홈(S2) 처럼)
    names = _screen_names(screens_md)
    wrong = []
    for name in ("정책서.md", "테스트시나리오.md"):
        body = _strip_urls(texts[name])
        for m in re.finditer(r"([가-힣A-Za-z0-9][가-힣A-Za-z0-9 ·]{0,20}?)\s*\(\s*S(\d{1,3})\s*\)", body):
            sid, said = f"S{int(m.group(2))}", " ".join(m.group(1).split()[-3:])
            core = re.sub(r"\s*(화면|시트|창|대화상자|팝업|페이지|탭)$", "", said).strip()
            if sid in names and core and not any(_alike(core, n) for n in names[sid]):
                wrong.append(f"{name} '{said}({sid})' ≠ {names[sid][0]}")
        for m in re.finditer(r"(?<![A-Za-z0-9])S(\d{1,3})\s*\(([^)\n]{1,24})\)", body):
            sid, said = f"S{int(m.group(1))}", m.group(2)
            states = [c[0] for _, rows in _tables(sections.get(sid, "")) for c in rows if c]
            if sid in names and not re.search(r"\d", said) and not any(_alike(said, n) for n in names[sid] + states):
                wrong.append(f"{name} '{sid}({said})' ≠ {names[sid][0]}")
    if wrong:
        problem("screen_name_mismatch", "화면 이름이 화면 목록과 다름: " + "; ".join(list(dict.fromkeys(wrong))[:8])
                + " — 화면 목록의 이름 그대로 쓴다")
    # 기획서는 짧게 — 이해관계자가 한두 화면 안에 읽는다. 표 안 글자도 센다 (PLAN_BODY_LIMIT 설명)
    if plan and (n := _body_len(plan)) > PLAN_BODY_LIMIT:
        problem("plan_too_long", f"기획서.md 가 {n}자 (공백 빼고, 표 포함) — {PLAN_BODY_LIMIT}자 안으로: 한 장 요약·요구사항은 남기고 자세한 설명·규칙 표는 정책서·화면설계로 옮겨 링크한다")
    # 코드 이름 — 기획서(요구사항 문장 포함)는 사용자가 겪는 말로. 지표 절의 이벤트 이름은 코드 이름이어도 된다
    if plan:
        body = _strip_urls(plan)
        metrics = _section(body, "지표")
        found = list(dict.fromkeys(m.group(0) for m in _CAMEL.finditer(body.replace(metrics, " ") if metrics else body)))
        if found:
            problem("plan_jargon", "기획서.md 에 코드 이름: " + ", ".join(found[:10])
                    + " — 화면에 보이는 이름·사용자가 겪는 말로 바꾸고, 엔지니어링 세부는 정책서 '## 개발 참고' 절로")
    open_q = [l.strip() for l in q_sec.splitlines() if re.match(r"\s*([-*]|\d+\.)\s+", l)] + \
        [" | ".join(c) for c in _rows(q_sec) if any(x.strip("-—  ") for x in c)]
    left = leftovers(target_root, changes) if changes else []
    for old in dict.fromkeys(x["old"] for x in left):
        hits = [x for x in left if x["old"] == old]
        problem("leftover_value", f"바꾼 값 '{old}' 이 남은 곳 {len(hits)}곳 — 고치거나, 일부러 두는 것이면 그 줄의 말을 keep 에 넣는다: "
                + "; ".join(f"{x['file']}:{x['line']} {x['text'][:70]}" for x in hits[:6]))
    log = {"ok": not problems, "problems": len(problems), "notes": len(notes),
           "codes": list(dict.fromkeys(f["code"] for f in findings if f["level"] == "problem")), "open_questions": len(open_q)}
    return {"ok": not problems, "problems": problems, "notes": notes, "findings": findings, "log": log, "folder": str(d),
            **({"leftovers": left[:40]} if left else {}),
            "docs": {n: bool(t.strip()) for n, t in texts.items()}, "requirements": reqs, "screens": screens,
            "wireframes": [p.name for p in htmls], "shots": sorted(p.name for p in shots_dir.glob("*.png")) if shots_dir.is_dir() else [],
            "tests": _ids(_T, tests), "open_questions": len(open_q)}


_PATHISH = re.compile(r"`?([\w.@-]+(?:/[\w.@\[\]()-]+)+/?)`?")


def _check_dev_map(target_root, policy: str, problem) -> None:
    """대상 폴더에 코드가 있으면 정책서 '개발 참고'에 '어느 저장소·모듈의 어느 파일에 무엇을' 표가 있고, 거기 적은 경로가 실제로
    있어야 한다 — 길잡이 없는 '개발 참고'(사용자가 어느 모듈을 손대야 하는지 모름)와 지어낸 경로를 막는다. 뜻은 보지 않는다."""
    src = sources(target_root)
    if not src["code"]:
        return
    sec = _section(policy, "개발 참고") if policy else ""
    tables = [(h, rows) for h, rows in _tables(sec) if rows]
    want = [(h, rows) for h, rows in tables if any("저장소" in c or "모듈" in c for c in h) and any("할 일" in c or "작업" in c for c in h)]
    if not want:
        problem("dev_map_missing", "정책서 '개발 참고'에 어디에 무엇을 하는지 표가 없음 — 머리글 '저장소·모듈 | 파일·폴더 | 할 일 | 새로/수정 | 근거' 로, "
                "코드에서 찾은 실제 경로(대상 폴더 기준)를 적는다. 이 폴더에 없는 저장소는 '이 폴더에 없음'과 필요한 것만")
        return
    root = Path(target_root)
    bad = []
    for head, rows in want:
        fi = next((k for k, c in enumerate(head) if "파일" in c or "폴더" in c or "경로" in c), None)
        if fi is None:
            continue
        for c in rows:
            cell = c[fi] if len(c) > fi else ""
            for m in _PATHISH.finditer(cell):
                rel = m.group(1).strip("`").rstrip("/")
                if "(새로)" in cell or "새로" in (c[-2] if len(c) > 1 else "") or "://" in cell:
                    continue
                if not (root / rel).exists() and not any(p.endswith(rel) or p.startswith(rel + "/") for p in src["code"]):
                    bad.append(rel)
    if bad:
        problem("dev_map_bad_path", "개발 참고 표의 경로가 대상 폴더에 없음: " + ", ".join(sorted(set(bad))[:6])
                + " — 실제 경로로 고치거나, 새로 만들 파일이면 그 줄의 '새로/수정' 칸에 '새로'라고 적는다")


# ---- 그림과 규칙 — 판정이 깊이마다 짚은 약점: 스탬프 9개인데 교환 단추가 켜진 그림(정책은 10개부터).
# 그림이 맞는지는 툴킷이 판단하지 않는다 — 에이전트가 화면마다 '예시 값 · 적용 규칙 · 보여야 할 것 · 보이는 것'을 적고,
# 툴킷은 그 표가 그림의 사실(숫자·단추 켜짐/꺼짐)과 맞는지만 본다. 표를 쓰려면 그림을 규칙과 대 봐야 한다.
_TAG = re.compile(r"<[^>]+>")
_BTN = re.compile(r"<(button|a)\b([^>]*)>(.*?)</\1>", re.I | re.S)
_OFF_WORDS = re.compile(r"꺼짐|꺼져|비활성|누를\s*수\s*없|못\s*누|disabled|잠김|잠겨|흐리게|회색\s*단추")
_ON_WORDS = re.compile(r"켜짐|켜져|활성(?!화\s*안)|누를\s*수\s*있|enabled")
_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(개|원|회|명|%|점|일|분|시간|건|장|살|세|자리|번)")
CIRCLED_RE = re.compile("[" + CIRCLED + "]")


def wireframe_facts(html: str) -> dict:
    """그림에 보이는 것 — 단추({글: 켜짐 여부})와 보이는 글(숫자 비교용). 스크립트·스타일·주석은 뺀다."""
    body = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", " ", html, flags=re.S | re.I)
    buttons: dict[str, bool] = {}
    for tag, attrs, inner in _BTN.findall(body):
        if tag.lower() == "a" and not re.search(r"""class\s*=\s*["'][^"']*\bbtn\b""", attrs, re.I):
            continue
        label = re.sub(r"\s+", " ", CIRCLED_RE.sub("", _TAG.sub(" ", inner))).strip()
        if not label:
            continue
        off = bool(re.search(r"\bdisabled\b|aria-disabled\s*=\s*[\"']?true|class\s*=\s*[\"'][^\"']*\bdisabled\b", attrs, re.I))
        buttons[label] = (buttons[label] and not off) if label in buttons else not off  # 같은 이름이 여럿이면 하나라도 꺼져 있으면 꺼짐
    text = re.sub(r"\s+", " ", _TAG.sub(" ", body))
    return {"buttons": buttons, "text": text}


def _picture_rule_check(screens_md: str, wf_of: dict, problem, note) -> None:
    """화면설계의 '그림과 규칙 대조' 표 — 그림이 있는 화면마다 한 줄 이상, 적은 예시 값이 그림에 있고, 적은 단추 상태가 그림과 같은가."""
    if not wf_of:
        return
    sec = _section(screens_md, "그림과 규칙")
    tables = [(h, rows) for h, rows in _tables(sec) if rows]
    if not tables:
        problem("no_picture_rule_check", "화면설계.md 에 '## 그림과 규칙 대조' 절이 없음 — 화면마다 | S | 그림의 예시 값 | 적용 규칙(정책서 절) | "
                "보여야 할 것 | 그림에 보이는 것 | 으로, 그림 속 숫자·상태가 규칙대로 보이는지 대 본다 (예: 스탬프 9개 → 교환 단추 꺼짐·'1개 더' 안내). "
                "규칙과 닿는 값이 없는 화면은 적용 규칙 칸에 '해당 없음'")
        return
    covered, bad_val, bad_btn = set(), [], []
    for head, rows in tables:
        hn = [_norm(c) for c in head]
        vi = next((k for k, c in enumerate(hn) if "예시" in c or "값" in c), None)
        si = next((k for k, c in enumerate(hn) if "보이는" in c), None)
        ei = next((k for k, c in enumerate(hn) if "보여야" in c or "규칙대로" in c), None)
        for c in rows:
            sid = next(iter(_ids("S", c[0] if c else "")), None)
            if not sid:
                continue
            covered.add(sid)
            f = wf_of.get(sid)
            if f is None:
                continue
            facts = wireframe_facts(_read(f))
            plain = facts["text"].replace(",", "")
            if vi is not None and vi < len(c):
                for num, unit in _NUM.findall(c[vi]):
                    n = num.replace(",", "")
                    if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d.])", plain):
                        bad_val.append(f"{sid} '{num}{unit}'")
            # '보여야 할 것'(규칙)과 '그림에 보이는 것'(적은 사실) 둘 다 그림의 단추와 대 본다 — 규칙상 꺼져야 하는데 그림이 켜져 있으면
            # 표에 '켜짐'이라고 솔직히 적어도 막는다
            for col, what in ((ei, "규칙상"), (si, "표는")):
                if col is None or col >= len(c):
                    continue
                for seg in re.split(r"[,;·/]|그리고|\s및\s", c[col]):
                    off, on = bool(_OFF_WORDS.search(seg)), bool(_ON_WORDS.search(seg))
                    if off == on:
                        continue
                    for label, enabled in facts["buttons"].items():
                        if len(label) >= 2 and _norm(label) in _norm(seg) and enabled == off:
                            bad_btn.append(f"{sid} '{label}' — {what} {'꺼짐' if off else '켜짐'}, 그림은 {'켜짐' if enabled else '꺼짐'}")
    missing = sorted(set(wf_of) - covered, key=lambda x: int(x[1:]))
    if missing:
        problem("picture_rule_uncovered", "'그림과 규칙 대조' 표에 없는 화면: " + ", ".join(missing)
                + " — 그 화면의 예시 값이 규칙대로 보이는지 한 줄씩 (닿는 규칙이 없으면 '해당 없음')")
    if bad_val:
        problem("picture_value_missing", "대조 표의 예시 값이 그림에 없음: " + "; ".join(bad_val[:6])
                + " — 그림에 실제로 보이는 값을 적거나, 그림을 그 값으로 고친다")
    if bad_btn:
        problem("picture_button_state", "대조 표와 그림의 단추 상태가 다름: " + "; ".join(bad_btn[:6])
                + " — 규칙대로라면 그림의 단추에 disabled 를 넣거나 켜고, 표도 그림과 같게")


# ---- '사용자 결정' — 사용자가 말한 규칙만. 판정: '두 매장 잔액 공유'·'별도 기기 없음'을 사용자 결정으로 적었는데 요청에 그런 말이 없었다.
_QUOTE = re.compile(r"[\"“”'‘’「『]([^\"“”'‘’「」『』]{2,80})[\"“”'‘’」』]")


def _squash(t: str) -> str:
    return re.sub(r"[\W_]+", "", str(t or "")).lower()


def _user_decision_check(texts: dict, user_words: str, problem) -> None:
    """표에서 '사용자 결정'이라 적은 줄 — 사용자의 말(요청·이어서 보낸 말·질문의 답)을 따옴표로 인용했고, 그 말이 실제로 있는가."""
    said = _squash(user_words)
    bad = []
    for name in ("기획서.md", "정책서.md", "화면설계.md"):
        for head, rows in _tables(texts.get(name) or ""):
            for c in rows:
                row = " ".join(c)
                if "사용자 결정" not in row and "사용자결정" not in row:
                    continue
                quotes = [q for q in _QUOTE.findall(row) if len(_squash(q)) >= 2]
                if not quotes or not any(_squash(q) in said for q in quotes):
                    bad.append(f"{name} '{(c[0] if c else '')[:30]}'")
    if bad:
        problem("user_decision_unbacked", "'사용자 결정'인데 사용자의 말(요청·보낸 말·질문의 답)에서 찾을 수 없음: " + "; ".join(bad[:6])
                + " — 사용자의 말을 따옴표로 그대로 인용한다. 사용자가 말하지 않았으면 '신규(추천)' 또는 '가정(기존 문서 없음)'과 열린 질문 Q 로")


def _check_compare(target_root, policy: str, policies: list[str] | None, problem) -> None:
    """'기존 정책 대조' 표 — 상태는 정해진 다섯 말 중 하나. '그대로 따름'은 실제로 있는 문서·코드를 인용해야 한다:
    기존 문서가 없는데 가정을 '기존 정책'처럼 쓰면 읽는 사람이 확인된 규칙으로 믿는다 (가정은 '가정(기존 문서 없음)')."""
    sec = _section(policy, "기존 정책") if policy else ""
    tables = [(h, rows) for h, rows in _tables(sec) if rows]
    if not tables:
        return
    norm = [_norm(s) for s in STATUSES] + ["신규", "가정"]
    odd, follow, unasked, ext_bare = [], [], [], []
    for head, rows in tables:
        si = next((k for k, c in enumerate(head) if "기존" in c or "근거" in c or "출처" in c), 0)
        for c in rows:
            row = _norm(" ".join(c))
            if not any(s in row for s in norm):
                odd.append((c[0] if c else "")[:40])
            elif _norm("그대로 따름") in row:
                follow.append(c[si] if len(c) > si else "")
            elif _norm("외부 규격") in row and not _EXT_CITE.search(" ".join(c).replace("외부 규격", "")):
                ext_bare.append((c[0] if c else "")[:40])
            elif any(x.strip().startswith("가정") for x in c) and not _ids("Q", " ".join(c)):  # 상태 칸만 ('가정용 요금' 같은 말은 아님)
                unasked.append((c[0] if c else "")[:40])
    if unasked:  # 가정은 확인된 규칙이 아니다 — 사용자가 답할 열린 질문(Q)으로 이어져야 정해진다
        problem("assumption_unasked", "기존 정책 대조 표의 '가정' 줄에 열린 질문 번호가 없음: " + "; ".join(unasked[:5])
                + " — 기획서 '열린 질문·가정' 절에 Q 로 묻고 그 줄에 Q 번호를 적는다 (추천안을 함께)")
    if ext_bare:
        problem("external_without_source", "'외부 규격'인데 어느 문서인지 없는 줄: " + "; ".join(ext_bare[:5])
                + " — 공식 문서 이름과 쪽·절(또는 주소)을 적는다. 열어서 확인하지 못한 값이면 '가정(기존 문서 없음)'과 Q 로")
    if odd:
        problem("compare_status", "기존 정책 대조 표에 상태가 없는 줄: " + "; ".join(odd[:5])
                + " — " + "·".join(f"'{s}'" for s in STATUSES) + " 중 하나로")
    if not follow:
        return
    src = sources(target_root)
    known = [*(policies or []), *src["docs"], *src["code"]]
    if not known and not src["truncated"]:
        problem("follow_without_source", f"'그대로 따름'이 {len(follow)}줄인데 대상 폴더에 기존 문서·코드가 없음 — 확인된 기존 정책이 "
                "아니므로 '가정(기존 문서 없음)'(사용자가 말한 규칙이면 '사용자 결정', 새로 정하면 '신규(추천)')으로")
        return
    bare = [f[:50] for f in follow if not _cites(f, known, src["truncated"])]
    if bare:
        problem("follow_without_source", "'그대로 따름'인데 기존 문서·코드를 인용하지 않은 줄: " + "; ".join(bare[:5])
                + " — 문서 이름·절(또는 파일:줄)을 적거나, 근거가 없으면 '가정(기존 문서 없음)'으로")


def screens(target_root: str | Path, files: list[str] | None = None, mobile: bool = False, audit: bool = False) -> dict:
    """와이어프레임 HTML 을 브라우저 엔진으로 열어 스크린샷을 shots/ 에 남긴다 — 깨지거나 스크립트 오류가 나면 알려 준다."""
    from beom_workbench.core import screens as scr

    d = folder(target_root)
    wf = d / "wireframes"
    if not wf.is_dir():
        raise ToolError("not_found", "와이어프레임 폴더가 없음 — .beom/brief/wireframes/S1-이름.html 로 먼저 쓴다")
    picks = sorted(p for p in wf.glob("*.html") if p.is_file() and not p.is_symlink())
    if files:
        want = {Path(f).name for f in files}
        picks = [p for p in picks if p.name in want]
    if not picks:
        raise ToolError("not_found", "찍을 와이어프레임이 없음")
    out_dir = d / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    root = d.resolve()
    with scr.serve_folder(root) as srv:
        pages = [scr.page_spec({"file": p.relative_to(root).as_posix(), "mobile": mobile, "wait_ms": 500, "audit": audit}, None, root,
                               str(out_dir / f"{p.stem}{'-mobile' if mobile else ''}.png"), file_base=srv.base) for p in picks]
        got = scr.capture(pages)
    res = []
    for p, r in zip(picks, got):
        res.append({"file": p.name, "ok": bool(r.get("ok")), "shot": r.get("out") if r.get("ok") else None,
                    "title": r.get("title"), "errors": (r.get("errors") or [])[:3], **({"audit": r.get("audit")} if audit else {})})
    return {"shots": res, "folder": str(out_dir)}


SCRIPT_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".sh", ".bat", ".cmd", ".json", ".log"}  # 작성용 스크립트·점검 결과


EXTRA_EXT = {".pdf", ".html", ".htm", ".md"}


def tidy(target_root: str | Path, existed=None) -> list[str]:
    """기획 패키지 폴더를 정해진 결과물만 남게 — 작성용 스크립트·점검 결과(json·log)와, 정해진 문서가 아닌데 이번 실행에서 생긴
    PDF·HTML·문서(한 파일 프로토타입·화면 모음 PDF·검증 기록·인계 문서 등)를 .beom/tmp/brief-tools 로 옮긴다.
    프로토타입은 앱이 와이어프레임으로 보여 주고, 공유용 HTML·PDF 는 사용자가 앱에서 내보낸다 — 에이전트가 따로 만들면 겹치고,
    정해지지 않은 .md 는 앱에서 사용자 첨부처럼 보인다. existed(rel) 이 참인 것(실행 전부터 있던 첨부·지난 기획)은 그대로 둔다.
    하위 폴더(wireframes·shots·export)는 건드리지 않는다."""
    d = folder(target_root)
    if not d.is_dir():
        return []
    dest = Path(target_root) / WORK / "tmp" / "brief-tools"
    moved = []
    for p in sorted(d.iterdir()):
        ext = p.suffix.lower()
        extra = ext in EXTRA_EXT and p.name not in DOCS and existed is not None and \
            existed(f"{WORK}/brief/{p.name}") is False
        if p.is_file() and (ext in SCRIPT_EXT or extra):
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / p.name
            if target.exists():
                target.unlink()
            p.replace(target)
            moved.append(p.name)
    return moved


def files(target_root: str | Path) -> dict:
    """앱이 보여 줄 문서 목록과 스크린샷 — 정해진 문서 순서대로, 그 밖의 .md 는 뒤에."""
    d = folder(target_root)
    if not d.is_dir():
        return {"docs": [], "shots": []}
    docs = [{"name": n, "title": TITLES[n], "mtime": (d / n).stat().st_mtime} for n in DOCS if (d / n).is_file()]
    known = set(DOCS)
    extra = sorted((p for p in d.glob("*.md") if p.name not in known and p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    docs += [{"name": p.name, "title": p.stem, "attachment": True} for p in extra]
    shots = sorted(str(p) for p in (d / "shots").glob("*.png")) if (d / "shots").is_dir() else []
    wfs = sorted(str(p) for p in (d / "wireframes").glob("*.html")) if (d / "wireframes").is_dir() else []
    return {"folder": str(d), "docs": docs, "shots": shots, "wireframes": wfs}
