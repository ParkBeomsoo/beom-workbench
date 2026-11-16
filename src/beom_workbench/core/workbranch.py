"""작업 브랜치 — 기존 git 저장소에서 일할 때는 main 에서 새 브랜치(feature/<작업명>)를 만들어 그 안에서만 작업한다.

브랜치 흐름: feature 는 언제나 main 에서 만든다. develop 은 실험 공간 — feature 를 develop 에 합쳐 시험하고, 적용은 feature 를
main 에 합친다 (merge — 사용자가 누를 때). develop 의 내용이 feature·main 으로 넘어가면 안 된다:
- 지금 develop(또는 develop 이 섞인 브랜치)에 있어도 새 작업은 막지 않는다 — main 에서 깨끗한 feature 를 만들어 시작한다.
  그 브랜치에 있던 커밋 안 된 변경은 실험이라 feature 로 가져가지 않고 stash 에 보관한다 (되돌릴 수 있게 이름을 붙여).
- develop 이 섞인 feature 는 main 에 합치지 않는다.

- 대상 폴더가 git 저장소 안이면 실행을 만들 때 main(없으면 master, 둘 다 없으면 지금 브랜치 — develop 은 제외)에서 `feature/<작업명>` 을 만들어 옮긴다.
  커밋하지 않은 변경이 있으면 함께 가져가 새 브랜치의 첫 커밋 '작업 전 변경'으로 따로 담는다 — 에이전트 변경과 섞이지 않게.
- 대상 폴더가 저장소가 아니고 그 아래에 저장소가 여럿이면(작업 공간 — 예: 상위 폴더 아래 api-search·next-bo …) 에이전트가 고친
  저장소마다 라운드가 끝날 때 같은 이름의 작업 브랜치를 만들어 그 변경을 옮긴다 (손대지 않은 저장소는 그대로).
- 라운드마다 앞뒤로 브랜치를 확인해 벗어났으면 되돌리고, 라운드가 끝나면 바뀐 것을 작업 브랜치에 커밋한다.
- 브랜치를 바꾸는 명령(switch·checkout <브랜치>·merge·rebase·pull·reset --hard·브랜치 삭제)은 권한 규칙 'branch' 로 막는다.
git 이 없거나 저장소가 하나도 없으면(새로 만드는 프로젝트) 아무것도 하지 않는다.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from beom_workbench.common.response import ToolError

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
EXCLUDE = [":(exclude,glob)**/.beom/tmp/**"]  # 에이전트 임시 스크립트·메모는 커밋하지 않는다 (.beom/brief 같은 결과물은 담는다)
BASES = ("main", "master")  # feature 는 언제나 여기서 — develop 은 실험 공간이라 기준으로 쓰지 않는다
DEVELOP = ("develop", "dev", "development")
_SKIP = {"node_modules", ".git", ".beom", "dist", "build", "target", ".venv", "venv", "__pycache__", ".next"}


_HOOKS_DIR: list = []


def _no_hooks() -> Path:
    """빈 훅 폴더 — 툴킷 홈 안에 둔다 (에이전트 샌드박스가 쓸 수 없는 곳). 임시 폴더는 에이전트가 쓸 수 있어
    거기에 훅을 심으면 앱이 샌드박스 밖에서 실행하게 된다. 쓰기 전에 비어 있는지 확인한다."""
    if not _HOOKS_DIR:
        from beom_workbench.config import _home
        _HOOKS_DIR.append(_home() / "cache" / "nohooks")
    d = _HOOKS_DIR[0]
    d.mkdir(parents=True, exist_ok=True)
    for f in d.iterdir():
        try:
            f.unlink() if f.is_file() or f.is_symlink() else __import__("shutil").rmtree(f)
        except OSError:
            pass
    return d


def _git(cwd: str | Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    # 앱이 하는 git(브랜치·라운드 커밋)은 저장소의 훅·fsmonitor 를 돌리지 않는다 — 에이전트가 .git 에 심어 둔 명령이
    # 에이전트 샌드박스 밖(앱)에서 실행되지 않게. 라운드 커밋은 작업 중 기록이라 린트 훅이 필요 없다.
    _NO_HOOKS = _no_hooks()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-c", "core.quotepath=off", "-c", f"core.hooksPath={_NO_HOOKS}", "-c", "core.fsmonitor=false",
                           *args], cwd=str(cwd), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                          **_NO_WINDOW)


def repo_top(path: str | Path) -> Path | None:
    """path 가 들어 있는 git 작업 트리의 맨 위. 저장소가 아니거나 git 이 없으면 None."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        out = _git(p, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def child_repos(root: str | Path, depth: int = 2) -> list[Path]:
    """저장소가 아닌 폴더 아래의 git 저장소들 (두 단계까지) — 여러 저장소를 모아 둔 작업 공간."""
    out: list[Path] = []
    base = Path(root)

    def walk(d: Path, left: int) -> None:
        try:
            subs = sorted(x for x in d.iterdir() if x.is_dir() and x.name not in _SKIP and not x.name.startswith("."))
        except OSError:
            return
        for x in subs:
            if (x / ".git").exists():
                out.append(x)
            elif left > 1:
                walk(x, left - 1)

    if base.is_dir():
        walk(base, depth)
    return out


def current(top: str | Path) -> str | None:
    """지금 브랜치 이름 — 브랜치가 아닌 커밋에 있으면(detached) None."""
    out = _git(top, "symbolic-ref", "-q", "--short", "HEAD")
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _head(top: str | Path) -> str | None:
    out = _git(top, "rev-parse", "-q", "--verify", "HEAD")
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def changed(top: str | Path) -> list[str]:
    out = _git(top, "status", "--porcelain", "--untracked-files=all")
    if out.returncode != 0:
        return []
    return [ln[3:] for ln in out.stdout.splitlines() if ln.strip() and not _toolkit(ln[3:].strip('"'))]


def _toolkit(rel: str) -> bool:
    """툴킷 작업 폴더(.beom) 안인가."""
    return rel.startswith(".beom/") or "/.beom/" in rel


def _exists(top: Path, branch: str) -> bool:
    return _git(top, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _base_ref(top: Path) -> str | None:
    """feature 의 기준 — main(또는 master). 로컬에 없으면 원격(origin/main)."""
    local = next((b for b in BASES if _exists(top, b)), None)
    if local:
        return local
    return next((f"origin/{b}" for b in BASES
                 if _git(top, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{b}").returncode == 0), None)


def _develop_ref(top: Path) -> str | None:
    return next((b for b in DEVELOP if _exists(top, b)), None)


def _mixed(top: Path) -> bool:
    """지금 develop 이거나 develop 이 섞인 브랜치인가 — 여기서 하던 일은 feature 로 가져가지 않는다."""
    return (current(top) or "").lower() in DEVELOP or bool(develop_commits(top))


def _stash(top: Path, run_id: str) -> str | None:
    """커밋 안 된 변경(새 파일 포함)을 stash 에 보관 — 이름을 붙여 둔다. 보관한 stash 의 이름, 없거나 실패하면 None."""
    msg = f"beom-workbench: {current(top) or 'HEAD'} 에서 하던 변경 (작업 {run_id} 시작 전 보관)"
    out = _git(top, *_identity(top), "stash", "push", "-u", "-m", msg, "--", ".", *EXCLUDE, timeout=120)
    if out.returncode != 0 or "No local changes" in out.stdout:
        return None
    return msg


def develop_commits(top: Path, ref: str = "HEAD") -> list[str]:
    """ref 에 들어 있는 develop 전용 커밋 — main 에는 없고 develop 의 줄기(first-parent)에 있는 것. 있으면 develop 이 섞인 것이다.
    (feature 를 develop 에 합친 커밋·develop 에 바로 한 실험이 여기에 든다. develop 에 합쳐진 feature 자신의 커밋은 들지 않는다.)"""
    dev, base = _develop_ref(top), _base_ref(top)
    if not dev or not base:
        return []
    mine = set(_git(top, "rev-list", f"{base}..{ref}").stdout.split())
    if not mine:
        return []
    line = _git(top, "rev-list", "--first-parent", f"{base}..{dev}").stdout.split()
    return [c for c in line if c in mine]


def info(path: str | Path) -> dict | None:
    """새 작업 화면용 — 저장소인지(지금 브랜치·기준 브랜치·커밋 안 된 변경 수), 아니면 아래에 저장소가 몇 개인지."""
    top = repo_top(path)
    if top:
        cur = current(top)
        return {"repo": str(top), "branch": cur, "base": _base_ref(top), "dirty": len(changed(top)),
                "on_develop": (cur or "").lower() in DEVELOP,
                "mixed": len(develop_commits(top)) if cur and (cur or "").lower() not in DEVELOP + BASES else 0}
    kids = child_repos(path)
    return {"workspace": str(path), "repos": len(kids), "names": [k.name for k in kids[:6]]} if kids else None


def branch_name(name: str | None, run_id: str = "") -> str:
    """feature/<작업명> — 한글 그대로, git 이 못 쓰는 글자·공백은 '-'."""
    slug = re.sub(r"[^\w.가-힣-]+", "-", (name or "").strip().lower())
    slug = re.sub(r"\.{2,}", ".", re.sub(r"-{2,}", "-", slug)).strip("-.")[:40].strip("-.")
    if slug.endswith(".lock"):
        slug = slug[:-5]
    tail = re.sub(r"[^a-z0-9]", "", run_id.lower())[-6:]
    return f"feature/{slug or ('작업-' + tail if tail else '작업')}"


def _identity(top: Path) -> list[str]:
    """저장소에 커밋 작성자가 없으면(새 PC 등) 툴킷 이름으로 — 사용자 설정은 바꾸지 않는다."""
    if _git(top, "config", "user.email").stdout.strip():
        return []
    return ["-c", "user.name=beom-workbench", "-c", "user.email=beom-workbench@localhost"]


def _commit(top: Path, message: str, path: str = ".") -> str | None:
    """path(대상 폴더, 저장소 맨 위 기준) 안에서 바뀐 것이 있으면 커밋하고 짧은 해시를, 없으면 None. 훅이 막으면 ToolError."""
    add = _git(top, "add", "-A", "--", path, *EXCLUDE, timeout=120)
    if add.returncode != 0:
        raise ToolError("engine_error", f"git add 실패: {add.stderr.strip()[-300:]}")
    if _git(top, "diff", "--cached", "--quiet").returncode == 0:
        return None
    out = _git(top, *_identity(top), "commit", "-q", "-m", message, timeout=300)
    if out.returncode != 0:
        _git(top, "reset", "-q")  # 담아 둔 것은 풀어 둔다 (파일은 그대로)
        raise ToolError("engine_error", f"커밋 실패: {(out.stderr or out.stdout).strip()[-300:]}")
    return (_head(top) or "")[:8] or None


def _branch_repo(top: Path, branch: str, rel: str, *, from_base: bool) -> dict:
    """top 저장소에 작업 브랜치를 만들어 옮긴다. from_base: main(또는 master)에서 — 커밋 안 된 변경은 함께 가져간다.
    가져가다 충돌하면(지금 브랜치가 main 과 많이 다를 때) 지금 커밋에서 만든다."""
    name, n = branch, 2
    while _exists(top, name):
        name, n = f"{branch}-{n}", n + 1
    cur = current(top)
    mixed = _mixed(top)
    base = _base_ref(top) if (from_base or mixed) else None
    if mixed and not base:
        raise ToolError("invalid_request", f"{top.name}: {cur or 'HEAD'} 에 develop 이 섞여 있는데 main 이 없어 깨끗한 작업 브랜치를 만들 곳이 없습니다",
                        recovery="main(또는 master) 브랜치를 만들거나 받아 온 뒤 시작하세요 — develop 은 feature 의 기준으로 쓰지 않습니다")
    used = base if base and base != cur else None
    out = _git(top, "switch", "--no-track", "-c", name, used) if used else _git(top, "switch", "-c", name)
    if out.returncode != 0 and used:
        if mixed:  # develop 위에서 생긴 변경을 main 기반으로 옮기지 못함 — develop 에서 만들면 오염되므로 멈춘다
            raise ToolError("invalid_request", f"{top.name}: {cur} 의 변경을 main 기반 작업 브랜치로 옮기지 못했습니다 (develop 의 코드에 기대는 변경)",
                            recovery="변경을 그 브랜치에 커밋하거나 stash 한 뒤 다시 하세요 — 작업 브랜치는 main 에서 만듭니다")
        used = None  # 깨끗한 브랜치에서 하던 변경이 main 과 충돌 — 지금 브랜치(깨끗함)에서
        out = _git(top, "switch", "-c", name)
    if out.returncode != 0:  # 오래된 git (switch 없음)
        out = _git(top, "checkout", "-q", "-b", name)
    if out.returncode != 0:
        raise ToolError("engine_error", f"{top.name}: 작업 브랜치 {name} 를 만들지 못했습니다: {out.stderr.strip()[-300:]}",
                        recovery="저장소 상태(병합·rebase 중인지)를 확인하거나, 새 작업 화면에서 '새 브랜치에서 작업'을 끄세요")
    return {"repo": str(top), "name": top.name, "path": rel, "branch": name, "base": used or cur, "base_commit": _head(top),
            "from_main": bool(used) or cur in BASES, "carried": 0}


def start(target_root: str | Path, name: str | None, run_id: str) -> dict | None:
    """대상이 git 저장소면 main 에서 작업 브랜치를 만들어 옮긴다 (develop 은 기준으로 쓰지 않는다). 저장소 여럿을 모은 폴더면 작업 공간으로(고칠 때 만든다).
    둘 다 아니면 None. 실패하면 ToolError (작업을 시작하지 않는다)."""
    branch = branch_name(name, run_id)
    top = repo_top(target_root)
    if not top:
        kids = child_repos(target_root)
        if not kids:
            return None
        base = {str(k): sorted(c) for k in kids if (c := changed(k))}  # 사용자가 하던 일 — 에이전트 변경과 나눈다
        return {"branch": branch, "workspace": str(Path(target_root).resolve()), "repos": [], "baseline": base}
    try:
        rel = Path(target_root).resolve().relative_to(top.resolve()).as_posix() or "."
    except ValueError:
        rel = "."
    pending = changed(top)
    stashed = None
    if pending and _mixed(top):
        # develop(또는 develop 이 섞인 브랜치)에서 하던 실험 — feature 로 가져가면 main 까지 오염된다. 막지 않고 stash 에 보관한 뒤
        # main 에서 깨끗한 feature 를 만든다 (보관한 변경은 그 브랜치에서 git stash pop 으로 되살린다)
        if not _base_ref(top):
            raise ToolError("invalid_request", f"{top.name}: main(또는 master) 브랜치가 없어 깨끗한 작업 브랜치를 만들 곳이 없습니다",
                            recovery="main 브랜치를 만들거나 받아 온(fetch) 뒤 시작하세요 — develop 은 feature 의 기준으로 쓰지 않습니다")
        stashed = _stash(top, run_id)
        if not stashed:
            raise ToolError("engine_error", f"{top.name}: {current(top)} 의 커밋 안 된 변경을 보관(stash)하지 못했습니다",
                            recovery="그 변경을 커밋하거나 직접 stash 한 뒤 시작하세요")
        pending = []
    entry = _branch_repo(top, branch, rel, from_base=True)
    if stashed:
        entry["stashed"] = stashed
    if pending and _head(top):
        try:
            if _commit(top, f"작업 전 변경 (커밋하지 않은 채 있던 것)\n\nbeom-workbench run {run_id}"):
                entry["carried"] = len(pending)
        except ToolError:
            pass  # 훅 등으로 못 담으면 그대로 둔다 — 첫 라운드 커밋에 같이 들어간다
    return {"branch": entry["branch"], "workspace": None, "repos": [entry]}


def adopt(wb: dict) -> list[dict]:
    """작업 공간에서 이번 라운드에 바뀐 저장소에 작업 브랜치를 만든다 (변경은 함께 옮겨 감). 새로 만든 것들을 돌려준다.
    에이전트가 본 코드 그대로 이어지게 지금 커밋에서 만든다 — 지금 브랜치가 main 이 아니면 알림에 적는다."""
    if not wb.get("workspace"):
        return []
    have = {os.path.normcase(r["repo"]) for r in wb.get("repos") or []} | {os.path.normcase(x) for x in wb.get("skipped") or []}
    made = []
    base = wb.get("baseline") or {}
    for top in child_repos(wb["workspace"]):
        if os.path.normcase(str(top)) in have:
            continue
        now, before = changed(top), base.get(str(top)) or []
        if not now or sorted(now) == before:  # 그대로이거나 원래 있던 변경뿐 — 에이전트가 손대지 않았다
            continue
        if (current(top) or "").lower() in DEVELOP or develop_commits(top):
            # develop(또는 develop 이 섞인 브랜치) 위에서 고친 것 — main 기반 feature 로 옮겨 본다. 못 옮기면 develop 에 그대로 두고 알린다 (develop 에서 만들지 않음)
            try:
                entry = _branch_repo(top, wb["branch"], ".", from_base=True)
            except ToolError as e:
                made.append({"repo": str(top), "name": top.name, "skipped": True, "reason": e.message})
                continue
        else:
            entry = _branch_repo(top, wb["branch"], ".", from_base=False)
        if before:  # 원래 있던 변경은 첫 커밋 '작업 전 변경'으로 따로 (같은 파일을 에이전트도 고쳤으면 함께 담긴다)
            try:
                _git(top, "add", "--", *before, timeout=120)
                if _git(top, "diff", "--cached", "--quiet").returncode != 0 and \
                        _git(top, *_identity(top), "commit", "-q", "-m", "작업 전 변경 (커밋하지 않은 채 있던 것)", timeout=300).returncode == 0:
                    entry["carried"] = len(before)
                else:
                    _git(top, "reset", "-q")
            except (OSError, subprocess.SubprocessError):
                pass
        made.append(entry)
    return made


def ensure(wb: dict) -> list[str]:
    """작업 브랜치에 있는지 저장소마다 확인하고 벗어났으면 되돌린다. 되돌린 알림 글들. 못 되돌리면 ToolError."""
    notes = []
    for r in wb.get("repos") or []:
        top = Path(r["repo"])
        if not top.exists():
            continue
        cur = current(top)
        if cur == r["branch"]:
            continue
        if not _exists(top, r["branch"]):
            made = _git(top, "switch", "-c", r["branch"])
            if made.returncode != 0:
                raise ToolError("engine_error", f"{top.name}: 작업 브랜치 {r['branch']} 가 없어졌고 다시 만들지도 못했습니다: "
                                                f"{made.stderr.strip()[-200:]}")
            notes.append(f"{top.name}: 작업 브랜치 {r['branch']} 가 없어져 지금 커밋({cur or '브랜치 아님'})에서 다시 만들었습니다")
            continue
        out = _git(top, "switch", r["branch"])
        if out.returncode != 0:
            out = _git(top, "checkout", "-q", r["branch"])
        if out.returncode != 0:
            raise ToolError("engine_error", f"{top.name}: 작업 브랜치 {r['branch']} 로 돌아가지 못했습니다 "
                                            f"(지금 {cur or '브랜치 아님'}): {out.stderr.strip()[-200:]}")
        notes.append(f"{top.name}: 작업 브랜치에서 벗어나 있어({cur or '브랜치 아님'}) {r['branch']} 로 되돌렸습니다")
    return notes


def commit_round(wb: dict, message: str) -> list[tuple[str, str]]:
    """라운드가 끝나면 바뀐 것을 저장소마다 작업 브랜치에 커밋한다 — 작업 브랜치에 있을 때만. [(저장소 이름, 해시)]"""
    out = []
    for r in wb.get("repos") or []:
        top = Path(r["repo"])
        if not top.exists() or current(top) != r["branch"]:
            continue
        sha = _commit(top, message, r.get("path") or ".")
        if sha:
            out.append((top.name, sha))
    return out


# ---------------------------------------------------------------- 합치기 (사용자가 누를 때)
#
# 시험: 작업 브랜치 → develop. 적용: 작업 브랜치 → main. develop 은 어디에도 합치지 않는다.
# 작업 폴더(체크아웃)는 건드리지 않는다 — 합칠 브랜치가 지금 체크아웃된 것이 아니면 git merge-tree 로 합친 결과를 만들어
# 그 브랜치만 앞으로 옮긴다. 충돌하면 아무것도 바꾸지 않고 충돌한 파일을 알려 준다.

_VERSION: list = []


def _git_version() -> tuple[int, int]:
    if not _VERSION:
        m = re.search(r"(\d+)\.(\d+)", _git(".", "--version").stdout)
        _VERSION.append((int(m.group(1)), int(m.group(2))) if m else (0, 0))
    return _VERSION[0]


def _ancestor(top: Path, a: str, b: str) -> bool:
    return _git(top, "merge-base", "--is-ancestor", a, b).returncode == 0


def merge_status(top: str | Path, branch: str) -> dict:
    """작업 브랜치가 develop·main 에 들어갔는지, develop 이 섞였는지."""
    top = Path(top)
    dev, base = _develop_ref(top), next((b for b in BASES if _exists(top, b)), None)
    ok = _exists(top, branch)
    return {"repo": str(top), "name": top.name, "branch": branch, "exists": ok, "develop": dev, "main": base,
            "in_develop": bool(ok and dev and _ancestor(top, branch, dev)),
            "in_main": bool(ok and base and _ancestor(top, branch, base)),
            "contaminated": len(develop_commits(top, branch)) if ok else 0,
            "ahead": int((_git(top, "rev-list", "--count", f"{base}..{branch}").stdout.strip() or 0)) if ok and base else 0,
            "checked_out": current(top), "dirty": len(changed(top))}


def _merge_tree(top: Path, into: str, branch: str) -> tuple[str | None, list[str]]:
    """합친 트리 — (트리, []) 또는 (None, 충돌 파일). git 2.38 이상."""
    out = _git(top, "merge-tree", "--write-tree", "--name-only", "--no-messages", into, branch, timeout=300)
    lines = out.stdout.splitlines()
    if out.returncode == 0 and lines:
        return lines[0].strip(), []
    if out.returncode == 1 and lines:
        return None, [ln for ln in lines[1:] if ln.strip()]
    raise ToolError("engine_error", f"합친 결과를 만들지 못했습니다: {(out.stderr or out.stdout).strip()[-300:]}")


def _merge_in_worktree(top: Path, into: str, branch: str, message: str) -> tuple[str | None, list[str]]:
    """오래된 git(merge-tree 없음) — 임시 작업 폴더에서 합친다 (사용자 폴더는 그대로)."""
    tmp = Path(tempfile.mkdtemp(prefix="beom-merge-"))
    try:
        add = _git(top, "worktree", "add", "-q", "--detach", str(tmp / "w"), into, timeout=300)
        if add.returncode != 0:
            raise ToolError("engine_error", f"임시 작업 폴더를 만들지 못했습니다: {add.stderr.strip()[-300:]}")
        w = tmp / "w"
        out = _git(w, *_identity(top), "merge", "--no-ff", "--no-edit", "-m", message, branch, timeout=600)
        if out.returncode != 0:
            files = _git(w, "diff", "--name-only", "--diff-filter=U").stdout.split()
            _git(w, "merge", "--abort")
            return None, files or [out.stderr.strip()[-200:]]
        return _head(w), []
    finally:
        _git(top, "worktree", "remove", "--force", str(tmp / "w"))
        _git(top, "worktree", "prune")
        from beom_workbench.adapters.git import remove_quietly
        remove_quietly(tmp)


def merge(top: str | Path, branch: str, into: str) -> dict:
    """작업 브랜치를 into(develop 계열 = 시험, main 계열 = 적용)에 합친다. {merged, commit, already, conflicts}"""
    top = Path(top)
    low = into.lower()
    if low not in DEVELOP + BASES:
        raise ToolError("invalid_request", f"{into} 에는 합치지 않습니다 — 시험은 develop, 적용은 main 입니다")
    if branch.lower() in DEVELOP + BASES:
        raise ToolError("invalid_request", f"{branch} 는 작업 브랜치가 아닙니다 — develop·main 은 다른 브랜치에 합치지 않습니다")
    if not _exists(top, branch):
        raise ToolError("not_found", f"{top.name}: 작업 브랜치 {branch} 가 없습니다")
    if not _exists(top, into):
        if low in DEVELOP:
            raise ToolError("not_found", f"{top.name}: develop 브랜치가 없습니다",
                            recovery="시험용 develop 이 필요하면 main 에서 만들어 두세요 (git branch develop main)")
        raise ToolError("not_found", f"{top.name}: {into} 브랜치가 없습니다")
    if low in BASES:
        mixed = develop_commits(top, branch)
        if mixed:
            raise ToolError("invalid_request", f"{top.name}: {branch} 에 develop 의 커밋 {len(mixed)}개가 섞여 있어 {into} 에 합치지 않습니다",
                            recovery="develop 의 실험이 main 에 들어가게 됩니다 — main 에서 새 브랜치를 만들어 필요한 커밋만 옮기세요")
    if _ancestor(top, branch, into):
        return {"repo": top.name, "into": into, "merged": False, "already": True, "commit": None, "conflicts": []}
    message = f"Merge {branch} into {into}"
    cur = current(top)
    if cur == into:  # 합칠 브랜치가 지금 체크아웃돼 있다 — 작업 폴더도 함께 바뀌므로 깨끗할 때만
        if changed(top):
            raise ToolError("invalid_request", f"{top.name}: 지금 {into} 에 커밋 안 된 변경이 있어 합치지 않았습니다",
                            recovery="변경을 커밋하거나 stash 한 뒤 다시 누르세요")
        out = _git(top, *_identity(top), "merge", "--no-ff", "--no-edit", "-m", message, branch, timeout=600)
        if out.returncode != 0:
            files = _git(top, "diff", "--name-only", "--diff-filter=U").stdout.split()
            _git(top, "merge", "--abort")
            return {"repo": top.name, "into": into, "merged": False, "already": False, "commit": None, "conflicts": files}
        return {"repo": top.name, "into": into, "merged": True, "already": False, "commit": (_head(top) or "")[:8], "conflicts": []}
    old = _git(top, "rev-parse", f"refs/heads/{into}").stdout.strip()
    if _git_version() >= (2, 38):
        tree, conflicts = _merge_tree(top, into, branch)
        new = None
        if tree:
            made = _git(top, *_identity(top), "commit-tree", tree, "-p", into, "-p", branch, "-m", message)
            if made.returncode != 0:
                raise ToolError("engine_error", f"합친 커밋을 만들지 못했습니다: {made.stderr.strip()[-300:]}")
            new = made.stdout.strip()
    else:
        new, conflicts = _merge_in_worktree(top, into, branch, message)
    if not new:
        return {"repo": top.name, "into": into, "merged": False, "already": False, "commit": None, "conflicts": conflicts}
    upd = _git(top, "update-ref", "-m", message, f"refs/heads/{into}", new, old)
    if upd.returncode != 0:
        raise ToolError("engine_error", f"{into} 를 옮기지 못했습니다 (그 사이 바뀌었을 수 있음): {upd.stderr.strip()[-300:]}")
    return {"repo": top.name, "into": into, "merged": True, "already": False, "commit": new[:8], "conflicts": []}

# ---------------------------------------------------------------- 명령 분류 (inbox.classify 가 쓴다)

_GIT = re.compile(r"(?:^|[\s&(])git(?:\.exe)?((?:\s+-[cC]\s+(?:\"[^\"]*\"|'[^']*'|\S+)"
                  r"|\s+--(?:git-dir|work-tree|namespace|exec-path|super-prefix|config-env)\s+\S+|\s+--[\w-]+(?:=\S+)?)*)\s+([a-z][\w-]*)(.*)$", re.I)
_MOVES = {"switch", "merge", "rebase", "pull", "cherry-pick", "worktree", "update-ref", "symbolic-ref", "filter-branch",
          "filter-repo"}
_BRANCH_FLAGS = re.compile(r"(?:^|\s)(?:-[a-zA-Z]*[dDmMcCf][a-zA-Z]*|--(?:delete|move|copy|force|set-upstream-to|unset-upstream)\b)")


def moves_branch(cmd: str) -> bool:
    """작업 브랜치를 벗어나거나 다른 브랜치·이력을 건드리는 git 명령인가. 파일 되돌리기(checkout -- 파일, restore)는 아니다."""
    for seg in re.split(r"[;\n\r|]+|&&", cmd):
        m = _GIT.search(seg.strip())
        if not m:
            continue
        if re.search(r"alias\.", m.group(1) or ""):
            return True  # git -c alias.s=switch s — 무엇이 될지 알 수 없다
        sub, rest = m.group(2).lower(), m.group(3)
        args = rest.split()
        if sub in _MOVES:
            return True
        if sub == "checkout":
            if args and "--" not in args and args != ["."] and not (args[:1] == ["-p"]):
                return True
        elif sub == "branch" and _BRANCH_FLAGS.search(rest):
            return True
        elif sub == "reset" and "--hard" in args:
            return True
    return False
