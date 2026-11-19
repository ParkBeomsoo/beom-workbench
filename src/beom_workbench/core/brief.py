"""기획 모드의 산출물과 점검 — 기획자가 개발·디자인·QA 에 넘기는 '기획 패키지'.

대상 폴더의 .beom/brief/ 에 둔다:
  기획서.md        한 장 요약·목표·참고 서비스·사용자·기능 목록(P0/P1/P2)·요구사항(R1…)·지표와 이벤트·범위 밖·열린 질문
  정책서.md        규칙을 표로 — 조건별 결과(결정표), 계산식과 숫자 예시, 상태 변화, 예외, 데이터와 API 초안
  화면설계.md      화면 목록(S1…) — 화면마다 목적·들어오는 길·보이는 것(번호 설명)·상태(빈·오류·로딩)·관련 R
  wireframes/S1-이름.html   흑백 와이어프레임 (화면마다, 번호 표시) — 툴킷이 스크린샷으로 남긴다(shots/)
  테스트시나리오.md T1… — 요구사항(R)마다 주어진 상황·행동·기대 결과. '이 기획으로 만들기'의 합격 기준이 된다

check() 는 문서끼리 맞물리는지 기계적으로 본다 — 빠진 문서·절, 시나리오가 없는 요구사항, 와이어프레임이 없는 화면,
화면이 안 붙은 요구사항, 남은 열린 질문. 기획 에이전트는 인계 전에 이것을 통과해야 한다(wb_handoff).
"""
from __future__ import annotations

import re
from pathlib import Path

from beom_workbench.common.response import ToolError

DOCS = {
    "기획서.md": ["요약", "목표", "참고", "사용자", "기능", "요구사항", "지표", "범위", "질문"],
    "정책서.md": ["데이터"],
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


def check(target_root: str | Path, policies: list[str] | None = None, changes: list[dict] | None = None) -> dict:
    """policies: 대상 폴더의 정책·규칙 문서 (있으면 정책서에 '기존 정책 대조' 절이 있어야 한다).
    changes: 기획을 고칠 때 바꾼 값 [{"old": "1시간", "new": "2시간", "keep": ["취소 마감"]}] — 옛 값이 남은 곳이 있으면 문제로."""
    d = folder(target_root)
    texts = {name: _read(d / name) for name in DOCS}
    problems: list[str] = []
    notes: list[str] = []
    missing_docs = [n for n, t in texts.items() if not t.strip()]
    for n in missing_docs:
        problems.append(f"{n} 가 없음")
    for name, kws in DOCS.items():
        heads = [l for l in texts[name].splitlines() if l.startswith("#")]
        for kw in kws if texts[name] else []:
            if not any(kw in h for h in heads):
                problems.append(f"{name} 에 '{kw}' 절이 없음")
    plan = texts["기획서.md"]
    reqs = _ids(_R, _section(plan, "요구사항")) if plan else []
    if plan and not reqs:
        problems.append("기획서.md 의 요구사항 절에 R1, R2 … 번호가 없음")
    policy = texts["정책서.md"]
    if policies and policy:
        heads = [h for h in policy.splitlines() if h.startswith("#")]
        if not any("기존 정책" in h for h in heads):
            problems.append("정책서.md 에 '기존 정책 대조' 절이 없음 — 대상 폴더의 정책·규칙 문서(" + ", ".join(policies[:4])
                            + ")를 인용해 그대로 따름·변경·신규(추천)과 상충 여부를 표로 쓴다")
    if policy and "|" not in policy:
        notes.append("정책서.md 에 표가 없음 — 조건별 결과는 표(결정표)로 쓰면 개발·QA 가 그대로 확인할 수 있음")
    tests = texts["테스트시나리오.md"]
    uncovered = [r for r in reqs if tests and r not in _ids(_R, tests)] if tests else []
    if uncovered:
        problems.append(f"테스트 시나리오가 없는 요구사항: {', '.join(uncovered)}")
    if tests and not _ids(_T, tests):
        problems.append("테스트시나리오.md 에 T1, T2 … 번호가 없음")
    screens_md = texts["화면설계.md"]
    screens = _defined_screens(screens_md)
    if screens_md and not screens:
        problems.append("화면설계.md 에 S1, S2 … 화면 번호가 없음 (제목이나 표 첫 칸)")
    wf = d / "wireframes"
    htmls = sorted(p for p in wf.glob("*.html") if p.is_file()) if wf.is_dir() else []
    wf_ids = {f"S{int(m.group(1))}" for p in htmls if (m := re.match(r"S(\d{1,3})", p.stem))}
    no_wf = [s for s in screens if s not in wf_ids]
    if no_wf:
        problems.append(f"와이어프레임이 없는 화면: {', '.join(no_wf)} (wireframes/S1-이름.html)")
    # 프로토타입: 화면끼리 링크(S2-이름.html)가 없는 화면을 가리키면 눌렀을 때 아무 데도 못 간다
    known = set(screens) | wf_ids
    broken = sorted({f"{p.stem} → S{int(n)}" for p in htmls
                     for n in re.findall(r"""(?:href|data-go)\s*=\s*["']?(?:[^"'>]*[\\/])?S(\d{1,3})(?![0-9])""", _read(p), re.I)
                     if f"S{int(n)}" not in known})
    if broken:
        problems.append(f"없는 화면으로 가는 링크: {', '.join(broken[:8])}")
    if htmls and not any(re.search(r"""href\s*=\s*["']?(?:[^"'>]*[\\/])?S\d{1,3}""", _read(p), re.I) for p in htmls) and len(htmls) > 1:
        notes.append("화면끼리 이어지는 링크가 없음 — 버튼·메뉴를 <a href=\"S2-이름.html\"> 로 이으면 앱에서 눌러 보며 이동할 수 있음")
    if screens_md and screens and not re.search(r"^\s*\|\s*S\s*\|[^\n]*기능", screens_md, re.M):
        notes.append("화면 목록 표에 '기능' 칸이 없음 — 있으면 앱의 화면 설계가 기능별 탭으로 나뉨")
    shots_dir = d / "shots"
    no_shot = [p.name for p in htmls if not any((shots_dir / f"{p.stem}{sfx}.png").is_file() for sfx in ("", "-mobile"))]
    if no_shot:
        notes.append(f"스크린샷을 아직 안 찍은 와이어프레임: {', '.join(no_shot)} — wb_brief_screens 로 찍는다")
    # 요구사항이 어느 화면에도 안 붙었으면 (화면 없는 기능일 수도 있어 참고만)
    if screens_md and reqs:
        unscreened = [r for r in reqs if r not in _ids(_R, screens_md)]
        if unscreened:
            notes.append(f"화면설계에 안 나오는 요구사항: {', '.join(unscreened)} (화면 없는 규칙이면 괜찮음)")
    open_q = [l.strip() for l in _section(plan, "질문").splitlines() if re.match(r"\s*([-*]|\d+\.)\s+", l)] if plan else []
    left = leftovers(target_root, changes) if changes else []
    for old in dict.fromkeys(x["old"] for x in left):
        hits = [x for x in left if x["old"] == old]
        problems.append(f"바꾼 값 '{old}' 이 남은 곳 {len(hits)}곳 — 고치거나, 일부러 두는 것이면 그 줄의 말을 keep 에 넣는다: "
                        + "; ".join(f"{x['file']}:{x['line']} {x['text'][:70]}" for x in hits[:6]))
    return {"ok": not problems, "problems": problems, "notes": notes, "folder": str(d), **({"leftovers": left[:40]} if left else {}),
            "docs": {n: bool(t.strip()) for n, t in texts.items()}, "requirements": reqs, "screens": screens,
            "wireframes": [p.name for p in htmls], "shots": sorted(p.name for p in shots_dir.glob("*.png")) if shots_dir.is_dir() else [],
            "tests": _ids(_T, tests), "open_questions": len(open_q)}


def screens(target_root: str | Path, files: list[str] | None = None, mobile: bool = False) -> dict:
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
        pages = [scr.page_spec({"file": p.relative_to(root).as_posix(), "mobile": mobile, "wait_ms": 500}, None, root,
                               str(out_dir / f"{p.stem}{'-mobile' if mobile else ''}.png"), file_base=srv.base) for p in picks]
        got = scr.capture(pages)
    res = []
    for p, r in zip(picks, got):
        res.append({"file": p.name, "ok": bool(r.get("ok")), "shot": r.get("out") if r.get("ok") else None,
                    "title": r.get("title"), "errors": (r.get("errors") or [])[:3]})
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
    docs = [{"name": n, "title": TITLES[n]} for n in DOCS if (d / n).is_file()]
    known = set(DOCS)
    extra = sorted((p for p in d.glob("*.md") if p.name not in known and p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    docs += [{"name": p.name, "title": p.stem, "attachment": True} for p in extra]
    shots = sorted(str(p) for p in (d / "shots").glob("*.png")) if (d / "shots").is_dir() else []
    wfs = sorted(str(p) for p in (d / "wireframes").glob("*.html")) if (d / "wireframes").is_dir() else []
    return {"folder": str(d), "docs": docs, "shots": shots, "wireframes": wfs}
