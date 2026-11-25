"""와이어프레임 공통 부품 — 툴킷이 .beom/brief/wireframes/_kit.css 로 깔아 둔다.

화면마다 HTML 한 파일에 같은 CSS 를 통째로 다시 쓰느라 와이어프레임이 오래 걸렸다 (깊게 기획 14화면에 2분, 거의 모델이 글을 쓰는 시간).
화면은 <link rel="stylesheet" href="_kit.css"> 로 이 부품을 쓰고 내용만 쓴다. 앱의 눌러 보기·내보내기는 이 파일을 화면 안에 넣어
보여 주고(격리된 틀은 바깥 파일을 읽지 않는다), 직접 열면 같은 폴더의 파일을 그대로 읽는다.
기존 제품 톤은 화면의 <style> 에서 :root 변수(--accent 등)만 바꾼다.
"""
from __future__ import annotations

import re
from pathlib import Path

NAME = "_kit.css"

KIT = """/* beom-workbench 와이어프레임 부품 — 바꾸지 말고, 톤은 화면의 <style> 에서 :root 변수로 */
:root { --accent: #2f5bd3; --accent-ink: #fff; --bg: #f5f6f8; --panel: #fff; --line: #dfe2e7; --text: #1f2328; --muted: #5b6370;
  --ok: #1f7a46; --warn: #a15c00; --fail: #c03a2b; --radius: 6px; --font: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 var(--font); color: var(--text); background: var(--bg); }
a { color: var(--accent); }
/* 관리자 화면 틀 — 위 머리줄 · 왼쪽 메뉴 · 본문 */
.wf-app { display: grid; grid-template-columns: 220px 1fr; grid-template-rows: 52px 1fr; min-height: 100vh; }
.wf-top { grid-column: 1 / -1; display: flex; align-items: center; gap: 16px; padding: 0 20px; background: #20242c; color: #fff; }
.wf-top .logo { font-weight: 700; }
.wf-top .sp { flex: 1; }
.wf-side { background: var(--panel); border-right: 1px solid var(--line); padding: 12px 0; }
.wf-side a, .wf-side .m { display: block; padding: 8px 18px; color: var(--text); text-decoration: none; }
.wf-side .g { padding: 12px 18px 4px; font-size: 12px; color: var(--muted); font-weight: 600; }
.wf-side .on { background: color-mix(in srgb, var(--accent) 10%, transparent); color: var(--accent); font-weight: 600; border-right: 3px solid var(--accent); }
.wf-main { padding: 20px 24px; min-width: 0; }
.wf-crumb { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.wf-title { display: flex; align-items: center; gap: 12px; margin: 0 0 16px; font-size: 20px; font-weight: 700; }
.wf-title .sp { flex: 1; }
.wf-title .btn, .modal > header .btn { font-size: 14px; font-weight: 600; }  /* 제목 글자 크기를 받지 않게 */
/* 카드·검색 영역 */
.card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px; margin-bottom: 16px; }
.wf-search { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px 20px; }
.wf-field { display: grid; grid-template-columns: 96px 1fr; align-items: center; gap: 8px; }
.wf-field > label { font-size: 13px; color: var(--muted); }
input, select, textarea { font: inherit; border: 1px solid #8a919c; border-radius: var(--radius); padding: 6px 9px; background: #fff; color: var(--text); min-width: 0; }
input:focus, select:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.wf-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; flex-wrap: wrap; }
.btn { display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 14px; border: 1px solid #8a919c; border-radius: var(--radius);
  background: #fff; color: var(--text); font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.btn.danger { color: var(--fail); border-color: var(--fail); }
.btn:disabled { opacity: .5; cursor: default; }
/* 목록(그리드) */
.wf-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.wf-bar .sp { flex: 1; }
.wf-bar .count { color: var(--muted); font-size: 13px; }
.wf-table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); font-size: 13px; }
.wf-table th { background: #eef0f3; font-weight: 600; text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
.wf-table td { padding: 8px 10px; border-bottom: 1px solid var(--line); }
.wf-table th, .wf-table td { word-break: keep-all; overflow-wrap: break-word; }  /* 한글이 한두 글자씩 꺾이지 않게 */
.wf-table tr.on td { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.wf-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.wf-pager { display: flex; justify-content: center; gap: 4px; margin-top: 10px; }
.wf-pager .btn { min-height: 28px; padding: 0 10px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #eef0f3; color: var(--muted); }
.badge.ok { background: #e6f3eb; color: var(--ok); } .badge.warn { background: #fbf1df; color: var(--warn); } .badge.fail { background: #fbeae7; color: var(--fail); }
/* 탭 */
.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin-bottom: 12px; }
.tabs > * { padding: 8px 14px; border: 0; background: none; font: inherit; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; }
.tabs .on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
/* 팝업 — 화면 위에 열린 모습 */
.modal-bg { position: fixed; inset: 0; background: rgba(15, 18, 24, .45); display: grid; place-items: center; }
.modal { width: min(560px, 92vw); background: var(--panel); border-radius: 10px; box-shadow: 0 20px 50px rgba(0, 0, 0, .25); }
.modal > header { display: flex; align-items: center; padding: 14px 18px; border-bottom: 1px solid var(--line); font-weight: 700; }
.modal > header .sp { flex: 1; }
.modal > .body { padding: 16px 18px; }
.modal > footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--line); }
/* 모바일 화면 — 폭 390px */
.mobile { width: 390px; min-height: 100vh; margin: 0 auto; background: var(--panel); }
.mobile .m-top { display: flex; align-items: center; height: 52px; padding: 0 16px; border-bottom: 1px solid var(--line); font-weight: 700; }
.mobile .m-body { padding: 16px; }
.mobile .m-foot { position: sticky; bottom: 0; padding: 12px 16px; background: var(--panel); border-top: 1px solid var(--line); }
.mobile .btn { width: 100%; justify-content: center; min-height: 44px; }
/* 상태·안내 */
.empty { padding: 40px 16px; text-align: center; color: var(--muted); }
.note { font-size: 12px; color: var(--muted); }
.err { color: var(--fail); font-size: 12px; }
.toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); background: #20242c; color: #fff; padding: 10px 16px; border-radius: 8px; }
/* 설명 번호 — 앱이 오른쪽 설명과 맞춰 강조한다 */
.n { display: inline-grid; place-items: center; min-width: 20px; height: 20px; padding: 0 4px; border-radius: 10px; background: #c2410c; color: #fff;
  font-size: 12px; font-weight: 700; vertical-align: middle; margin: 0 2px; }  /* 흰 글자 대비 5.2:1 · 12px — 화면 점검을 통과해야 한다 */
input[type=checkbox], input[type=radio] { width: 18px; height: 18px; margin: 0; accent-color: var(--accent); }
.ck { display: inline-grid; place-items: center; min-width: 28px; min-height: 28px; cursor: pointer; }  /* 표의 선택 칸: <label class="ck"><input type="checkbox"></label> */
"""

_LINK = re.compile(r"""<link\b[^>]*href=["']?(_kit|_theme)\.css["']?[^>]*>""", re.I)
THEME = "_theme.css"
_STYLE_EXT = {".css", ".scss", ".sass", ".less", ".vue", ".svelte", ".jsx", ".tsx", ".html", ".jsp", ".ftl", ".hbs"}
_SKIP_DIRS = {"node_modules", ".git", ".beom", "dist", "build", ".next", "out", "target", "vendor", ".venv", "venv", "coverage"}


def ensure(brief_folder: Path) -> Path:
    """와이어프레임 폴더에 부품 파일을 깐다. 부품은 툴킷 것이라(화면은 고치지 않는다) 늘 지금 판으로 맞춘다 — 부품의 결함을 고쳐도
    이미 깔린 옛 판이 남아 모든 화면이 같은 점검에 걸렸다 (설명 번호 대비 3.58:1). 부품은 클래스를 더하기만 해서 옛 화면도 그대로 그려진다."""
    wf = Path(brief_folder) / "wireframes"
    wf.mkdir(parents=True, exist_ok=True)
    f = wf / NAME
    if not f.is_file() or f.read_text(encoding="utf-8", errors="replace") != KIT:
        f.write_text(KIT, encoding="utf-8")
    return f


def inline(html: str, folder: Path) -> str:
    """앱의 격리된 틀·내보내기용 — <link href="_kit.css">·<link href="_theme.css"> 를 그 내용으로 바꾼다 (틀은 바깥 파일을 읽지 않는다)."""
    if "_kit.css" not in html and "_theme.css" not in html:
        return html

    def put(m):
        f = Path(folder) / (m.group(1) + ".css")
        css = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else (KIT if m.group(1) == "_kit" else "")
        return "<style>" + css + "</style>"
    return _LINK.sub(put, html)


def _hex_rgb(h: str) -> tuple[int, int, int] | None:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def _hsl(rgb: tuple[int, int, int]) -> tuple[float, float]:
    import colorsys
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return s, l


def theme_from(roots: list[str | Path], limit: int = 600) -> dict:
    """저장소의 스타일에서 실제로 많이 쓰는 값 — 주 색(채도 있는 색 중 가장 많이), 글꼴, 모서리, 배경·선 색.
    화면을 띄울 수 없을 때 목업이 그 제품 톤을 따르게 한다 (값을 지어내지 않는다 — 찾은 것만, 없으면 빈 칸)."""
    from collections import Counter
    colors, fonts, radii, files = Counter(), Counter(), Counter(), 0
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for f in _walk(root):
            if files >= limit:
                break
            if f.suffix.lower() not in _STYLE_EXT:
                continue
            try:
                if f.stat().st_size > 400_000:
                    continue
                t = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files += 1
            for h in re.findall(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", t):
                rgb = _hex_rgb(h)
                if rgb:
                    colors["#%02x%02x%02x" % rgb] += 1
            for ff in re.findall(r"font-family\s*:\s*([^;}{\n]+)", t):
                first = ff.split(",")[0].strip().strip("'\"")
                if first and not first.startswith(("var(", "$", "@", "inherit")) and first.lower() not in ("sans-serif", "serif", "monospace", "system-ui"):
                    fonts[first] += 1
            for r in re.findall(r"border-radius\s*:\s*(\d+(?:\.\d+)?)px", t):
                if 2 <= float(r) <= 16:
                    radii[r] += 1
    out: dict = {"files": files}
    sat = [(c, n) for c, n in colors.most_common() if (lambda s, l: s > 0.35 and 0.22 < l < 0.68)(*_hsl(_hex_rgb(c)))]
    if sat:
        out["accent"] = sat[0][0]
    light = [c for c, _ in colors.most_common() if (lambda s, l: l > 0.95 and s < 0.3)(*_hsl(_hex_rgb(c))) and c != "#ffffff"]
    if light:
        out["bg"] = light[0]
    line = [c for c, _ in colors.most_common() if (lambda s, l: 0.8 < l < 0.93 and s < 0.25)(*_hsl(_hex_rgb(c)))]
    if line:
        out["line"] = line[0]
    if fonts:
        out["font"] = fonts.most_common(1)[0][0]
    if radii:
        out["radius"] = radii.most_common(1)[0][0] + "px"
    return out


_VISUAL = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".fig", ".sketch", ".xd", ".pdf"}


def look_basis(request_and_messages: str, attachments: list[str]) -> str:
    """목업이 따를 모양 — 'user'(사용자가 준 그림·시안을 따르거나 새 모양을 원함) 또는 'product'(기존 제품 모양).
    사실로만 정한다: 첨부에 그림·시안 파일이 있거나, 사용자의 말이 디자인을 새로·느낌을 바꾸라고 하면 'user'.
    'user' 면 툴킷이 제품 모양(_theme.css)을 입히지 않는다 — 기존 틀에 가두지 않는다. 메뉴·기능 배치는 여전히 소스를 따른다."""
    from beom_workbench.core.designscope import REDESIGN_RX, RESTYLE_RX
    if any(Path(str(a)).suffix.lower() in _VISUAL for a in attachments or []):
        return "user"
    if REDESIGN_RX.search(request_and_messages or "") or RESTYLE_RX.search(request_and_messages or ""):
        return "user"
    return "product"


def drop_theme(brief_folder: Path) -> bool:
    """사용자가 새 모양을 원하게 됐을 때 — 깔아 둔 제품 모양을 치운다 (점검도 더는 요구하지 않는다)."""
    f = Path(brief_folder) / "wireframes" / THEME
    if f.is_file():
        f.unlink()
        return True
    return False


def ensure_theme(brief_folder: Path, roots: list[str | Path]) -> dict | None:
    """저장소에서 뽑은 것을 _theme.css 로 — 화면은 _kit.css 다음에 이것을 링크한다. 뽑을 것이 없으면 만들지 않는다.
    테마 파일에 적힌 값(design_sources)이 자주 쓰인 색(theme_from)보다 먼저다. 라이브러리를 알면 그 모양(SKINS)을 더한다.
    돌려주는 것: {"path", "lib", "theme_files", "layout_files"} — 지시에 근거 파일을 함께 알린다."""
    ds = design_sources(roots)
    t = {**theme_from(roots), **ds["tokens"]}
    vars_ = {"--accent": t.get("accent"), "--bg": t.get("bg"), "--line": t.get("line"), "--radius": t.get("radius"),
             "--font": ", ".join(dict.fromkeys([f'"{t["font"]}"', '"Pretendard"', '"Malgun Gothic"', '"Apple SD Gothic Neo"', "sans-serif"]))
             if t.get("font") else None}
    vars_ = {k: v for k, v in vars_.items() if v}
    skin = SKINS.get(ds["lib"] or "", "")
    if not vars_ and not skin:
        return None
    wf = Path(brief_folder) / "wireframes"
    wf.mkdir(parents=True, exist_ok=True)
    f = wf / THEME
    head = "/* 툴킷이 저장소에서 뽑은 제품 모양 — 화면은 키트 다음에 링크한다. 고치지 않는다."
    if ds["lib"]:
        head += f" UI 라이브러리: {_LIB_NAME[ds['lib']]}."
    if ds["theme_files"]:
        head += " 테마 파일: " + ", ".join(Path(x).name for x in ds["theme_files"][:3]) + "."
    f.write_text(head + " */\n" + (":root { %s }\n" % "; ".join(f"{k}: {v}" for k, v in vars_.items()) if vars_ else "") + skin,
                 encoding="utf-8")
    return {"path": f, "lib": ds["lib"], "lib_name": _LIB_NAME.get(ds["lib"] or "", ""), "theme_files": ds["theme_files"],
            "layout_files": ds["layout_files"]}

# ---- 제품의 디자인 시스템 — 실제 화면을 볼 수 없을 때도 그 제품처럼 보이게
# 규칙끼리 충돌했다: '기존 화면이 있으면 그 톤으로'와 '화면은 _kit.css 로'. 에이전트는 구체적인 쪽(키트)을 따라 중립 와이어프레임이 나왔다.
# 그래서 툴킷이 정한다 — 키트의 구조(클래스)는 그대로 쓰고, 그 위에 제품의 UI 라이브러리 모양과 테마 값을 _theme.css 로 입힌다.
_LIBS = [  # package.json 의 의존성 이름 → 라이브러리 (앞의 것이 먼저)
    ("mui", ("@mui/material", "@material-ui/core")),
    ("antd", ("antd",)),
    ("element", ("element-plus", "element-ui")),
    ("vuetify", ("vuetify",)),
    ("bootstrap", ("bootstrap", "react-bootstrap", "bootstrap-vue", "reactstrap")),
    ("tailwind", ("tailwindcss",)),
]
_LIB_NAME = {"mui": "MUI(Material UI)", "antd": "Ant Design", "element": "Element", "vuetify": "Vuetify", "bootstrap": "Bootstrap",
             "tailwind": "Tailwind"}
_THEME_NAME = re.compile(r"(^|[\\/])(themes?|tokens?|design-?system|styles?[\\/]variables?)([\\/]|$)|"
                         r"(^|[\\/])(colors?|palette|theme|tokens?|_?variables|tailwind\.config)\.[a-z]+$", re.I)
_LAYOUT_NAME = re.compile(r"(^|[\\/])layouts?[\\/].*(nav|side|menu|header|layout|drawer|gnb|lnb)[^\\/]*\.(tsx|jsx|vue|svelte|html|jsp)$", re.I)


def _walk(root: Path, limit: int = 20000):
    """폴더를 훑는다 — node_modules 같은 큰 폴더는 들어가지 않는다 (rglob 은 들어간 뒤에 거른다)."""
    import os
    n = 0
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
        for name in names:
            n += 1
            if n > limit:
                return
            yield Path(d, name)


def design_sources(roots: list[str | Path]) -> dict:
    """저장소에서 화면 모양을 정하는 것 — UI 라이브러리(package.json), 테마 파일, 레이아웃(메뉴·머리줄) 파일, 테마 값.
    값은 테마 파일에 적힌 것만 (주 색 primary.main·colorPrimary·$primary, 글꼴, 모서리) — 지어내지 않는다."""
    import json as _json
    lib, theme_files, layout_files, pkgs = None, [], [], []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for f in _walk(root):
            rel = f.relative_to(root).as_posix()
            if f.name == "package.json" and rel.count("/") <= 3:
                pkgs.append(f)
            elif _THEME_NAME.search(rel) and f.suffix.lower() in {".ts", ".js", ".mjs", ".cjs", ".json", ".scss", ".sass", ".less", ".css"}:
                theme_files.append(f)
            elif _LAYOUT_NAME.search(rel):
                layout_files.append(f)
    for p in pkgs:
        try:
            j = _json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        deps = {**(j.get("dependencies") or {}), **(j.get("devDependencies") or {})}
        for name, keys in _LIBS:
            if any(k in deps for k in keys):
                if lib is None or [n for n, _ in _LIBS].index(name) < [n for n, _ in _LIBS].index(lib):
                    lib = name
                break
    theme_files.sort(key=lambda f: (len(f.parts), str(f)))
    tok: dict = {}
    pats = [
        ("accent", r"""["']?primary["']?\s*:\s*\{[^{}]*?["']?(?:main|DEFAULT|500|base)["']?\s*:\s*["'](#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b"""),
        ("accent", r"""colorPrimary["']?\s*:\s*["'](#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b"""),
        ("accent", r"""\$(?:primary|color-primary|brand-primary)\s*:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b"""),
        ("accent", r"""--(?:primary|color-primary|el-color-primary|bs-primary)\s*:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b"""),
        ("accent", r"""["']?primary["']?\s*:\s*["'](#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b"""),
        ("font", r"""(?:defaultFont|primaryFont|fontFamily|font-family|\$font-family-(?:base|sans-serif))["']?\s*[:=]\s*["']([^"',;{}()$]{2,40})"""),
        ("radius", r"""shape\s*:\s*\{\s*borderRadius\s*:\s*(\d{1,2})\b"""),
        ("radius", r"""(?:\$border-radius|borderRadius|--radius)["']?\s*:\s*["']?(\d{1,2}(?:\.\d+)?)(?:px)?["']?\s*[,;}\n]"""),
    ]
    for f in theme_files[:40]:
        try:
            t = f.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        for key, pat in pats:
            if key in tok:
                continue
            m = re.search(pat, t)
            if m:
                v = m.group(1).strip()
                if key == "font" and v.lower() in ("inherit", "sans-serif", "serif", "monospace", "system-ui"):
                    continue
                tok[key] = (v + "px") if key == "radius" else v
    return {"lib": lib, "tokens": tok, "theme_files": [str(f) for f in theme_files[:6]],
            # 틀 자체(레이아웃·세로 메뉴·머리줄)가 먼저, 메뉴 이름 목록(config-nav)이 다음
            "layout_files": [str(f) for f in sorted(layout_files, key=lambda f: (
                bool(re.search(r"auth|login|sign|error|blank|simple", f.as_posix(), re.I)),  # 로그인·오류 화면 틀은 뒤로
                0 if re.search(r"(^|[-_.])(layout|nav-?vertical|sidebar|side-?nav|header|gnb|lnb)([-_.]|$)", f.stem, re.I) else
                1 if re.search(r"nav|menu", f.stem, re.I) else 2, len(f.parts), str(f)))[:5]]}


# 라이브러리마다 키트 부품의 겉모습 — 클래스는 키트 그대로(구조는 키트, 모양은 제품)
SKINS = {
    "mui": """/* MUI 모양 — 흰 머리줄·흰 메뉴, 그림자 있는 Paper, 테두리 입력칸, 줄만 있는 데이터 그리드 */
body { background: #f9fafb; }
.wf-top { background: #fff; color: var(--text); box-shadow: 0 1px 0 var(--line); }
.wf-side { background: #fff; border-right: 1px dashed var(--line); padding: 8px; }
.wf-side a, .wf-side .m { border-radius: 8px; padding: 8px 12px; margin: 2px 0; color: #637381; font-weight: 500; }
.wf-side .on { background: color-mix(in srgb, var(--accent) 8%, transparent); border-right: 0; }
.card { border: 0; border-radius: calc(var(--radius) * 2); box-shadow: 0 0 2px rgba(145,158,171,.2), 0 12px 24px -4px rgba(145,158,171,.12); padding: 20px; }
input, select, textarea { border-color: #7f8a96; border-radius: var(--radius); padding: 8px 12px; min-height: 40px; }  /* 입력칸 경계 3:1 이상 */
.btn { border-radius: var(--radius); min-height: 36px; border-color: rgba(145,158,171,.32); font-weight: 700; }
.btn.primary { box-shadow: none; }
.wf-table { border: 0; border-radius: calc(var(--radius) * 2); overflow: hidden; box-shadow: 0 0 2px rgba(145,158,171,.2), 0 12px 24px -4px rgba(145,158,171,.12); }
.wf-table th { background: #f4f6f8; color: #637381; border-bottom: 0; padding: 12px 14px; }
.wf-table td { padding: 12px 14px; border-bottom: 1px dashed rgba(145,158,171,.2); }
.badge { border-radius: 6px; padding: 2px 8px; background: rgba(145,158,171,.16); color: #637381; }
.tabs { border-bottom: 0; box-shadow: inset 0 -2px 0 rgba(145,158,171,.08); }
.tabs .on { color: var(--text); border-bottom-color: var(--text); }
.modal { border-radius: 16px; }
""",
    "antd": """/* Ant Design 모양 — 어두운 왼쪽 메뉴, 흰 머리줄, 6px 모서리, 옅은 표 머리 */
.wf-top { background: #fff; color: var(--text); box-shadow: 0 1px 4px rgba(0,21,41,.08); }
.wf-side { background: #001529; border-right: 0; }
.wf-side a, .wf-side .m { color: rgba(255,255,255,.65); }
.wf-side .g { color: rgba(255,255,255,.45); }
.wf-side .on { background: var(--accent); color: #fff; border-right: 0; }
.card { border: 1px solid #f0f0f0; border-radius: 8px; }
input, select, textarea { border-color: #d9d9d9; border-radius: 6px; padding: 4px 11px; min-height: 32px; }
.btn { border-color: #d9d9d9; border-radius: 6px; font-weight: 400; box-shadow: 0 2px 0 rgba(0,0,0,.02); }
.btn.primary { box-shadow: 0 2px 0 rgba(5,145,255,.1); }
.wf-table { border: 0; }
.wf-table th { background: #fafafa; color: rgba(0,0,0,.88); border-bottom: 1px solid #f0f0f0; padding: 12px 16px; }
.wf-table td { padding: 12px 16px; border-bottom: 1px solid #f0f0f0; }
.badge { border-radius: 4px; border: 1px solid #d9d9d9; background: #fafafa; font-weight: 400; }
""",
    "element": """/* Element 모양 — 어두운 왼쪽 메뉴, 4px 모서리, 회색 표 머리 */
.wf-top { background: #fff; color: #303133; box-shadow: 0 1px 4px rgba(0,21,41,.08); }
.wf-side { background: #304156; border-right: 0; }
.wf-side a, .wf-side .m { color: #bfcbd9; }
.wf-side .g { color: #8a97a8; }
.wf-side .on { background: #263445; color: var(--accent); border-right: 0; }
.card { border: 1px solid #ebeef5; border-radius: 4px; box-shadow: 0 2px 12px rgba(0,0,0,.06); }
input, select, textarea { border-color: #dcdfe6; border-radius: 4px; min-height: 32px; }
.btn { border-color: #dcdfe6; border-radius: 4px; font-weight: 500; }
.wf-table th { background: #f5f7fa; color: #909399; }
.wf-table td { border-bottom: 1px solid #ebeef5; color: #606266; }
.badge { border-radius: 4px; }
""",
    "bootstrap": """/* Bootstrap 모양 — 어두운 머리줄, 카드 머리, 0.375rem 모서리 */
.wf-top { background: #212529; }
.card { border: 1px solid rgba(0,0,0,.175); border-radius: .375rem; }
input, select, textarea { border-color: #dee2e6; border-radius: .375rem; padding: 6px 12px; min-height: 38px; }
.btn { border-radius: .375rem; font-weight: 400; min-height: 38px; }
.wf-table { border: 0; }
.wf-table th { background: #fff; border-bottom: 2px solid #dee2e6; }
.wf-table td { border-bottom: 1px solid #dee2e6; }
.badge { border-radius: .375rem; }
""",
    "vuetify": """/* Vuetify 모양 — 머티리얼, 둥근 카드와 그림자 */
.wf-top { background: var(--accent); color: #fff; box-shadow: 0 2px 4px rgba(0,0,0,.2); }
.card { border: 0; border-radius: 4px; box-shadow: 0 2px 1px -1px rgba(0,0,0,.2), 0 1px 1px rgba(0,0,0,.14), 0 1px 3px rgba(0,0,0,.12); }
.btn { text-transform: uppercase; letter-spacing: .03em; border-radius: 4px; }
.wf-table { border: 0; }
.wf-table th { background: #fff; color: rgba(0,0,0,.6); }
""",
}

