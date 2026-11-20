"""체크포인트 — 라운드마다 대상 폴더의 스냅샷을 남기고, 변경 사항(diff)을 보여 주고, 되돌린다.

사용자의 git 저장소(.git, 인덱스, 브랜치)는 건드리지 않는다. 실행 폴더 안에 따로 둔 숨은 저장소
(<run>/shadow.git)를 GIT_DIR 로, 대상 폴더를 작업 트리로 쓴다. 대상의 .gitignore 는 그대로 존중된다.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from beom_workbench.common.response import ToolError

# 스냅샷에 넣지 않는 것 — 빌드 결과·의존성·캐시, 그리고 툴킷의 임시 폴더(명령 스크립트·내보내기). 라운드마다 쌓여 숨은 저장소가 불지 않게.
EXCLUDE = [".git/", ".beom-workbench/", "node_modules/", "target/", "__pycache__/", ".venv/", "venv/", "dist/", "build/",
           ".beom/tmp/", ".next/", ".nuxt/", ".turbo/", ".gradle/", ".pytest_cache/", "coverage/", ".cache/"]
# 변경 탭·파일 되돌리기에서 빼는 것 — 프로그램이 아닌 툴킷 작업 파일(.beom/: 합격 케이스·첨부)과 검증이 남기는 커버리지 데이터.
# 작업 브랜치 커밋과 같은 기준이다 (프로그램만). 예전 실행의 숨은 저장소가 이미 추적하는 파일도 여기서 걸러진다.
HIDDEN = [":(exclude).beom", ":(exclude,glob)**/.coverage", ":(exclude,glob)**/.coverage.*", ":(exclude,glob)**/htmlcov/**",
          ":(exclude,glob)**/.nyc_output/**"]
MAX_DIFF = 400_000


def hidden(path: str) -> bool:
    """변경 탭에 보이지 않는(되돌리지 않는) 경로인가."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    name = p.rsplit("/", 1)[-1]
    return p == ".beom" or p.startswith(".beom/") or name == ".coverage" or name.startswith(".coverage.") \
        or "/htmlcov/" in "/" + p or "/.nyc_output/" in "/" + p


_LIST_CACHE: dict[str, tuple] = {}


def _git(shadow: Path, root: Path, *args: str, check: bool = True, text: bool = True,
         index: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_DIR=str(shadow), GIT_WORK_TREE=str(root), GIT_TERMINAL_PROMPT="0",
               **({"GIT_INDEX_FILE": str(index)} if index else {}),
               GIT_AUTHOR_NAME="beom-workbench", GIT_AUTHOR_EMAIL="checkpoint@beom-workbench.local",
               GIT_COMMITTER_NAME="beom-workbench", GIT_COMMITTER_EMAIL="checkpoint@beom-workbench.local")
    proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args], cwd=root, env=env,
                          capture_output=True, text=text, encoding="utf-8" if text else None, errors="replace" if text else None,
                          timeout=300)
    if check and proc.returncode != 0:
        raise ToolError("internal", f"git {' '.join(args[:2])} 실패: {(proc.stderr or '')[-400:]}")
    return proc


class Checkpoints:
    def __init__(self, run_dir: Path, target_root: str | Path):
        self.shadow = Path(run_dir) / "shadow.git"
        self.root = Path(target_root)

    def _init(self) -> None:
        if not (self.shadow / "HEAD").exists():
            self.shadow.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--bare", "-q", str(self.shadow)], check=True, capture_output=True, timeout=60)
        info = self.shadow / "info"
        info.mkdir(exist_ok=True)
        want = "\n".join(EXCLUDE) + "\n"
        f = info / "exclude"
        try:
            if f.read_text(encoding="utf-8") == want:
                return
        except OSError:
            pass
        f.write_text(want, encoding="utf-8")  # 예전 실행의 숨은 저장소도 새 제외 목록을 쓰게

    def snapshot(self, label: str) -> str | None:
        """지금 대상 폴더 상태를 기록. 바뀐 게 없으면 새로 만들지 않고 마지막 것을 돌려준다."""
        self.root.mkdir(parents=True, exist_ok=True)
        self._init()
        _git(self.shadow, self.root, "add", "-A", ".")
        head = _git(self.shadow, self.root, "rev-parse", "-q", "--verify", "HEAD", check=False).stdout.strip()
        if head and not _git(self.shadow, self.root, "diff", "--cached", "--quiet", check=False).returncode:
            return head
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _git(self.shadow, self.root, "commit", "-q", "--allow-empty", "-m", f"{label}\n\n{stamp}")
        _git(self.shadow, self.root, "gc", "--auto", "--quiet", check=False)  # 라운드가 쌓여도 크기가 불지 않게 (필요할 때만 압축)
        return _git(self.shadow, self.root, "rev-parse", "HEAD").stdout.strip()

    def _stamp(self) -> tuple:
        """체크포인트가 새로 생겼는지 — 브랜치 참조 파일의 수정 시각 (앱이 자주 다시 읽어도 git 을 부르지 않게)."""
        out = []
        for p in (self.shadow / "refs" / "heads", self.shadow / "packed-refs"):
            try:
                if p.is_dir():
                    out += sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in p.iterdir())
                else:
                    out.append((p.name, p.stat().st_mtime_ns))
            except OSError:
                pass
        return tuple(out)

    def list(self) -> list[dict]:
        if not (self.shadow / "HEAD").exists():
            return []
        key = str(self.shadow)
        stamp = self._stamp()
        hit = _LIST_CACHE.get(key)
        if hit and hit[0] == stamp:
            return [dict(r) for r in hit[1]]
        out = _git(self.shadow, self.root, "log", "--format=%H%x1f%s%x1f%cI", check=False).stdout
        rows = []
        for line in out.splitlines():
            h, s, at = (line.split("\x1f") + ["", ""])[:3]
            if h:
                rows.append({"id": h, "label": s, "at": at})
        _LIST_CACHE[key] = (stamp, rows)
        return [dict(r) for r in rows]

    def changes(self, base: str | None = None) -> dict:
        """처음(또는 base) 체크포인트 대비 지금 대상 폴더의 변경 — 파일 목록과 diff."""
        cps = self.list()
        if not cps:
            return {"files": [], "diff": "", "base": None}
        base = base or cps[-1]["id"]
        self._init()
        # 화면용 비교는 따로 둔 인덱스로 — 라운드 스냅샷(실행기)과 같은 인덱스를 동시에 잠그지 않게
        idx = self.shadow / "index.view"
        _git(self.shadow, self.root, "add", "-A", ".", index=idx)
        spec = ["--", ".", *HIDDEN]
        stat = _git(self.shadow, self.root, "diff", "--cached", "--numstat", "--no-renames", base, *spec, check=False, index=idx).stdout
        names = _git(self.shadow, self.root, "diff", "--cached", "--name-status", "--no-renames", base, *spec, check=False,
                     index=idx).stdout
        status = {}
        for line in names.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                status[parts[-1]] = parts[0][0]
        files = []
        for line in stat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                add, rem, path = parts
                path = path.split(" => ")[-1].rstrip("}") if " => " in path else path
                files.append({"path": path, "added": None if add == "-" else int(add), "removed": None if rem == "-" else int(rem),
                              "status": status.get(path, "M")})
        diff = _git(self.shadow, self.root, "diff", "--cached", "--no-renames", base, *spec, check=False, index=idx).stdout
        return {"files": files, "diff": diff[:MAX_DIFF], "truncated": len(diff) > MAX_DIFF, "base": base}

    def revert_paths(self, base: str, paths: list[str]) -> list[str]:
        """파일 몇 개만 그 체크포인트 상태로 — 그때 없던 파일은 지운다. 툴킷 작업 파일(.beom)은 건드리지 않는다."""
        done = []
        for rel in paths:
            if hidden(rel):
                continue
            target = (self.root / rel).resolve()
            if not str(target).startswith(str(self.root.resolve())) or target == self.root.resolve():
                continue
            if _git(self.shadow, self.root, "cat-file", "-e", f"{base}:{rel}", check=False).returncode == 0:
                _git(self.shadow, self.root, "checkout", "-q", base, "--", rel)
            elif target.is_file():
                target.unlink()
            done.append(rel)
        return done

    def restore(self, checkpoint: str) -> dict:
        """대상 폴더를 그 체크포인트 상태로 되돌린다. 되돌리기 전 상태도 체크포인트로 남겨 다시 되돌릴 수 있다."""
        known = {c["id"] for c in self.list()}
        if checkpoint not in known:
            raise ToolError("not_found", "없는 체크포인트")
        before = self.snapshot("되돌리기 전")
        # 툴킷 작업 폴더(.beom — 합격 케이스·보조 파일)와 커버리지 기록은 되돌리지 않는다 — 변경 탭·파일 되돌리기와 같은 기준
        added = _git(self.shadow, self.root, "diff", "--name-only", "--no-renames", "--diff-filter=A", checkpoint, before,
                     "--", ".", *HIDDEN).stdout.splitlines()
        added = [rel for rel in added if not hidden(rel)]
        for rel in added:
            p = (self.root / rel)
            if p.is_file() and str(p.resolve()).startswith(str(self.root.resolve())):
                p.unlink()
        _git(self.shadow, self.root, "checkout", "-q", checkpoint, "--", ".", *HIDDEN)
        after = self.snapshot("되돌림")
        return {"restored": checkpoint, "removed_files": added, "undo": before, "now": after}
