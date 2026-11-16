"""브랜치 흐름 점검 (PR 마다) — feature 는 main 에서 만들고, develop 은 실험 공간이다.

- main 으로 가는 PR: develop 에서 오면 안 되고, develop 전용 커밋(develop 줄기에만 있는 커밋 — feature 를 develop 에 합친
  커밋, develop 에 바로 한 실험)이 섞여 있으면 안 된다. feature 자신의 커밋은 develop 에 합쳐졌더라도 괜찮다.
- develop 으로 가는 PR: feature/·fix/·hotfix/ 브랜치만 (시험용으로 합친다).

usage: python .github/branch_policy.py BASE HEAD_REF   (origin/main·origin/develop·PR 머리 커밋이 받아져 있어야 한다)
"""
import subprocess
import sys

ALLOWED_INTO_DEVELOP = ("feature/", "fix/", "hotfix/")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def develop_only_commits(head: str) -> list[str]:
    mine = set(git("rev-list", f"origin/main..{head}").split())
    try:
        line = git("rev-list", "--first-parent", "origin/main..origin/develop").split()
    except subprocess.CalledProcessError:
        return []  # develop 이 없으면 섞일 것도 없다
    return [c for c in line if c in mine]


def main(base: str, head_ref: str, head: str = "HEAD") -> int:
    if base == "main":
        if head_ref in ("develop", "dev"):
            print("::error::develop is the experiment branch and is never merged into main. Merge the tested feature branch into main instead.")
            return 1
        bad = develop_only_commits(head)
        if bad:
            print(f"::error::This branch contains {len(bad)} develop-only commit(s) ({', '.join(c[:8] for c in bad[:5])}). "
                  "Create a new feature branch from main and move only the changes you need.")
            return 1
    elif base == "develop":
        if not head_ref.startswith(ALLOWED_INTO_DEVELOP):
            print(f"::error::Only {', '.join(ALLOWED_INTO_DEVELOP)} branches are merged into develop (got: {head_ref}).")
            return 1
    print(f"branch policy ok: {head_ref} -> {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
