"""연관된 곳 — 바꾼 기존 함수를 부르는 기존 코드를 구조 분석으로 찾고, 합격 케이스가 그 코드를 실제로 거쳤는지 본다.

원래 있던 테스트는 예전 동작을 기대해서 바뀐 기능의 기준이 되지 못한다. 그래서 완료 판정은 우리가 만든 합격 케이스로 하고,
바꾼 함수의 영향이 닿는 곳(그 함수를 부르는 기존 코드)도 우리 케이스로 확인한다.

- 원래 소스는 작업을 시작할 때(코드를 고치기 전) 기록 사슬에 남긴다 (port_guard.record_baseline 의 sources).
- 바뀐 줄 → 그 줄을 품은 함수(바꾼 함수) → 그 함수를 부르는 함수(연관된 곳): 구조 지도 엔진(codebase-memory)의 함수 위치와
  호출 관계로. 엔진이 없으면 파이썬은 표준 ast 로 (이름으로 이은 호출), 다른 언어는 '분석 못 함' 으로 알린다.
- 거쳤는지는 케이스 실행 계측(port._CaseCoverage)의 함수 단위 기록으로.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import os
from pathlib import Path

from beom_workbench.core import layout

SOURCE_FILE_MAX, SOURCE_TOTAL_MAX, SOURCE_COUNT_MAX = 300_000, 5_000_000, 3000
CODE_EXTS = tuple(sorted({e for k, exts in layout.LANG_EXT.items() for e in exts}))


def scan_roots(target: Path) -> list[Path]:
    """원래 소스를 기록·분석할 자리 — 대상 폴더가 속한 프로젝트들의 자리 (없으면 대상 폴더)."""
    roots = {r for _, r in layout.projects(target)} or {Path(target).resolve()}
    out = []
    for r in sorted(roots, key=lambda p: len(p.parts)):
        if not any(r.is_relative_to(o) for o in out):
            out.append(r)
    return out


def source_files(target: Path) -> list[Path]:
    """기록할 소스 파일 (테스트 파일·작업 폴더·의존성 폴더 빼고)."""
    tests = layout.test_files(target)
    out: list[Path] = []
    for root in scan_roots(target):
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in layout.SKIP_DIRS and not d.startswith(".")]
            for f in files:
                p = Path(dirpath) / f
                if f.endswith(CODE_EXTS) and str(p.resolve()) not in tests:
                    out.append(p.resolve())
                    if len(out) >= SOURCE_COUNT_MAX:
                        return out
    return out


def snapshot(target: Path) -> dict:
    """{대상 폴더 기준 경로: {sha, text?}} — 큰 파일·전체가 크면 지문만 (그 파일이 바뀌면 파일 전체를 바뀐 것으로 본다)."""
    out, total = {}, 0
    for p in source_files(target):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        ent = {"sha": hashlib.sha256(data).hexdigest()}
        if len(data) <= SOURCE_FILE_MAX and total + len(data) <= SOURCE_TOTAL_MAX:
            ent["text"] = data.decode("utf-8", "replace")
            total += len(data)
        out[os.path.relpath(p, target).replace("\\", "/")] = ent
    return out


def changed_ranges(target: Path, base: dict) -> dict[str, list[tuple[int, int]]]:
    """원래 있던 소스 중 바뀐 파일 → 지금 파일에서 바뀐 줄 범위 [(시작, 끝)] (1부터). 지운 파일은 빼고 (부르는 쪽이 깨지면 케이스가 잡는다)."""
    out: dict[str, list[tuple[int, int]]] = {}
    for rel, ent in base.items():
        p = Path(target) / rel
        try:
            now = p.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(now).hexdigest() == ent["sha"]:
            continue
        new_l = now.decode("utf-8", "replace").splitlines()
        if "text" not in ent:
            out[rel] = [(1, max(1, len(new_l)))]
            continue
        ranges = []
        sm = difflib.SequenceMatcher(None, ent["text"].splitlines(), new_l, autojunk=False)
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            ranges.append((j1 + 1, max(j1 + 1, j2)))  # 지운 줄은 그 자리(다음 줄)를 바뀐 것으로
        if ranges:
            out[rel] = ranges
    return out


def _abs(target: Path, rel: str) -> Path:
    return (Path(target) / rel).resolve()


def _overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= end and start <= b for a, b in ranges)


# ---------------------------------------------------------------- 함수와 호출 관계

def _source_sig(root: Path) -> str:
    """색인할 소스의 지문 (경로·크기·수정 시각) — 같으면 다시 색인하지 않는다."""
    h = hashlib.sha256()
    for f in sorted(source_files(root)):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _graph_cbm(settings, index_root: Path, name: str, target: Path | None = None) -> tuple[list[dict], list[dict]] | None:
    from beom_workbench.adapters.cbm import CodebaseMemory
    cbm = CodebaseMemory(settings)
    if not cbm.available:
        return None
    # 검증마다 저장소 전체를 다시 색인하지 않는다 — 소스가 그대로면 지난 색인을 쓴다
    mark = Path(settings.home) / "cache" / "impact" / f"{name}.sig"
    sig = _source_sig(Path(target or index_root)) + "|" + str(index_root)
    try:
        fresh = mark.read_text(encoding="utf-8") != sig
    except OSError:
        fresh = True
    if fresh:
        cbm.index(index_root, name)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(sig, encoding="utf-8")
    syms = [s for s in cbm.symbols(name) if s["label"] in ("Function", "Method")]
    for s in syms:
        s["abs"] = str((index_root / s["path"]).resolve())
    by_qn = {s["qualified_name"]: s for s in syms}
    calls = []
    for c in cbm.all_calls(name):
        a, b = by_qn.get(c["caller"]), by_qn.get(c["callee"])
        if a and b:
            calls.append({"caller": a, "callee": b, "line": c.get("call_line")})
    return syms, calls


def _graph_ast(files: list[Path]) -> tuple[list[dict], list[dict]]:
    """엔진이 없을 때 — 파이썬 파일만. 함수 위치는 ast 로 정확히, 호출은 실제로 이어지는 것만 잇는다: 같은 모듈의 이름 호출
    (`f()`), 불러온 이름(`from m import f` 뒤 `f()`), 불러온 모듈의 속성(`import m` 뒤 `m.f()`), 같은 클래스의 `self.f()`.
    예전에는 이름만 같으면 이어서 `get`·`run` 같은 함수를 바꾸면 `dict.get` 을 부르는 곳까지 연관된 곳으로 잡혔다."""
    syms, calls_by = [], []
    for f in files:
        if f.suffix != ".py":
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        imported: dict[str, tuple[str, str | None]] = {}  # 이 파일에서 쓰는 이름 → (모듈 끝 이름, 함수 이름 또는 None=모듈)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                for a in node.names:
                    imported[a.asname or a.name] = (node.module.split(".")[-1], a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported[a.asname or a.name.split(".")[0]] = (a.name.split(".")[-1], None)
        for cls in [None, *[n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]]:
            body = tree.body if cls is None else cls.body
            for node in body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                s = {"qualified_name": f"{f.stem}.{cls.name + '.' if cls else ''}{node.name}", "name": node.name, "abs": str(f),
                     "module": f.stem, "cls": cls.name if cls else None,
                     "start_line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno)}
                syms.append(s)
                refs = set()
                for c in ast.walk(node):
                    if not isinstance(c, ast.Call):
                        continue
                    fn = c.func
                    if isinstance(fn, ast.Name):
                        refs.add(imported.get(fn.id, (f.stem, fn.id)) if fn.id in imported else (f.stem, fn.id))
                    elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                        base = fn.value.id
                        if base == "self" and cls is not None:
                            refs.add((f.stem, fn.attr, cls.name))
                        elif base in imported and imported[base][1] is None:
                            refs.add((imported[base][0], fn.attr))
                calls_by.append((s, refs))
    index: dict[tuple, list[dict]] = {}
    for s in syms:
        index.setdefault((s["module"], s["name"], s["cls"]) if s["cls"] else (s["module"], s["name"]), []).append(s)
    calls = [{"caller": s, "callee": t, "line": None} for s, refs in calls_by for r in refs for t in index.get(r, []) if t is not s]
    return syms, calls


def analyze(settings, port_id: str, target: Path, base: dict, measured) -> dict | None:
    """바꾼 기존 함수와 그 함수를 부르는 기존 코드(연관된 곳), 그리고 케이스가 거쳤는지.
    measured(절대 경로, 줄) → True(거침)·False(안 거침)·None(잴 수 없음). 원래 소스 기록이 없으면 None (새 프로젝트)."""
    if not base:
        return None
    target = Path(target).resolve()
    ranges = changed_ranges(target, base)
    if not ranges:
        return {"changed": [], "callers": []}
    roots = scan_roots(target)
    index_root = roots[0] if len(roots) == 1 else Path(os.path.commonpath([str(r) for r in roots]))
    engine = "codebase-memory"
    try:
        got = _graph_cbm(settings, index_root, "impact-" + port_id.removeprefix("port_"), target)
    except Exception as e:  # noqa: BLE001 — 엔진이 실패해도 검증은 이어 간다 (분석 못 함으로 알린다)
        got, engine = None, f"codebase-memory 실패: {str(e)[:120]}"
    if got is None:
        files = [_abs(target, r) for r in base]
        got = _graph_ast(files)
        engine = "ast (파이썬만 — 구조 지도 엔진 없음)" if engine == "codebase-memory" else engine + " → ast (파이썬만)"
    syms, calls = got
    changed_abs = {str(_abs(target, rel)): rs for rel, rs in ranges.items()}
    changed = [s for s in syms if s["abs"] in changed_abs and _overlaps(s["start_line"], s["end_line"], changed_abs[s["abs"]])]
    changed_ids = {id(s) for s in changed}
    base_abs = {str(_abs(target, rel)) for rel in base}
    tests = layout.test_files(target)
    callers: dict[int, dict] = {}
    for c in calls:
        a, b = c["caller"], c["callee"]
        if id(b) not in changed_ids or id(a) in changed_ids:
            continue
        if a["abs"] not in base_abs or a["abs"] in tests or "/.beom/" in a["abs"].replace("\\", "/"):
            continue  # 새로 만든 코드·테스트는 연관된 '기존' 코드가 아니다
        ent = callers.setdefault(id(a), {"sym": a, "calls": set()})
        ent["calls"].add(b["name"])
    rows = []
    for ent in callers.values():
        a = ent["sym"]
        rel = os.path.relpath(a["abs"], target).replace("\\", "/")
        rows.append({"caller": a["name"], "target": f"{rel}:{a['start_line']}", "calls": sorted(ent["calls"]),
                     "executed": measured(a["abs"], a["start_line"])})
    rows.sort(key=lambda r: r["target"])
    return {"engine": engine,
            "changed": sorted({f"{os.path.relpath(s['abs'], target).replace(chr(92), '/')}:{s['start_line']} {s['name']}" for s in changed}),
            "callers": rows}
