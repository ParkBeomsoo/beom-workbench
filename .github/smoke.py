"""CI 점검 — 설치된 패키지가 이 OS 에서 핵심 동작을 하는지 (모듈 불러오기 · 작업 브랜치 · 명령 분류 · 에이전트 찾기 · 앱 시작).

usage: python .github/smoke.py [--app]
"""
import importlib
import os
import pkgutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def core() -> None:
    import beom_workbench
    skip = ("beom_workbench.app",) if sys.platform.startswith("linux") else ()
    for m in pkgutil.walk_packages(beom_workbench.__path__, "beom_workbench."):
        if not m.name.startswith(skip):
            importlib.import_module(m.name)
    from beom_workbench.core import agents, inbox, workbranch

    repo = Path(tempfile.mkdtemp()) / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    wb = workbranch.start(repo, "smoke 작업", "run_000001")
    assert workbranch.current(repo) == wb["branch"] == "feature/smoke-작업", wb

    def kind(cmd: str) -> str:
        return inbox.classify("Bash", {"command": cmd}, repo, [])

    assert kind("git push") == "publish" and kind("git switch main") == "branch" and kind("rm -rf /etc") == "delete"
    print([(s["id"], bool(s.get("exe"))) for s in agents.statuses(check=False)])

    from beom_workbench.core import sandbox
    st = sandbox.status()
    print("sandbox", st)
    assert st["ready"] or st.get("install"), st  # 준비됐거나, 처음 설정에서 돌릴 설치 명령이 있다
    if sys.platform == "darwin":
        assert st["ready"], st  # macOS 는 기본 내장
    if sys.platform == "win32":  # 내장 격리 실행 — 안쪽 쓰기는 되고 바깥은 막힌다
        assert st["ready"], st
        work = Path(tempfile.mkdtemp()) / "work"
        work.mkdir()
        outside = Path.home() / "sbx-smoke-outside.txt"
        run = {"target_root": str(work), "policy": {}}
        cmd = sandbox.wrap_powershell(f"Set-Content inside.txt ok; Set-Content '{outside}' bad -ErrorAction SilentlyContinue; "
                                      "$x = (& cmd /c echo piped); Write-Output \"captured=$x\"", run)
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], cwd=work, capture_output=True, timeout=180)
        print("sandbox run", out.returncode, out.stdout.decode("utf-8", "replace")[-200:])
        assert (work / "inside.txt").is_file() and not outside.exists() and b"captured=piped" in out.stdout
    if sys.platform != "win32":
        s = sandbox.claude_settings({"target_root": str(repo), "policy": {}})["sandbox"]
        # 격리는 켜고, 막힌 명령은 권한 확인을 거쳐 격리 밖에서 다시 시도할 수 있다 (권한 규칙이 허락·카드·거절을 정한다)
        assert s["enabled"] and s["allowUnsandboxedCommands"] is True and s["autoAllowBashIfSandboxed"] is False, s
    print("core ok")


def app() -> None:
    threading.Timer(20, lambda: os._exit(0)).start()  # 창이 뜨고 화면을 그릴 시간을 준 뒤 끝낸다
    from beom_workbench.app import main
    sys.exit(main.main([]))


if __name__ == "__main__":
    app() if "--app" in sys.argv else core()
