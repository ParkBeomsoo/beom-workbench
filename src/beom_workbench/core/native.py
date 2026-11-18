"""네이티브 데스크톱 앱 화면 확인 — 앱을 띄워 창을 찾고, 접근성 트리(UI Automation)로 글자·컨트롤을 읽고,
필요하면 값을 넣고 버튼을 누른 뒤 창 스크린샷을 남긴다. 웹 화면(screens.py)과 같은 케이스 모양을 쓴다.

케이스: {"name": "main", "description": "첫 화면 — 수량 칸과 계산 버튼이 보인다",
         "screen": {"app": "dist/OrderApp.exe"  또는  ["python", "main.py"]  또는  "npm start",
                    "title": "주문",            # (선택) 창 제목 정규식 — 창이 여러 개일 때
                    "actions": [{"set": "Edit:수량", "value": "10"}, {"click": "Button:계산"}, {"wait_ms": 300}],
                    "width": 900, "height": 640, "wait_ms": 800, "timeout_ms": 30000},
         "expect": {"visible": ["18,000원"], "selectors": ["Button:계산", "Edit"], "no_errors": true}}

- visible: 창 안의 글자(컨트롤 이름·값)에 들어 있어야 하는 문구
- selectors: "종류" 또는 "종류:이름" (종류는 UI Automation 컨트롤 종류 — Button, Edit, Text, CheckBox, ComboBox, List …)
- no_errors: 앱이 확인 도중 종료되지 않았고, 표준 오류에 예외(Traceback·Exception·Error:)가 없다
- actions 의 대상도 같은 "종류:이름" (이름 대신 AutomationId 도 된다). set 은 값 넣기, click 은 누르기(Invoke·Toggle·Select).

지금은 Windows 만 (UI Automation 은 Windows 에 들어 있어 따로 설치할 것이 없다). Qt·WinForms·WPF·WinUI·Electron 창 모두 된다.
앱은 사용자 화면에 잠깐 떴다가 닫힌다. macOS·Linux 는 capability_unavailable 로 알린다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from beom_workbench.common.response import ToolError

PROBE = Path(__file__).with_name("native_probe.ps1")
_ERR_LINE = re.compile(r"Traceback|Unhandled exception|Exception:|^\s*\w*Error:|FATAL|panicked at", re.M)


def available() -> bool:
    return sys.platform == "win32"


def is_native(spec) -> bool:
    return isinstance(spec, dict) and bool(spec.get("app"))


def command_text(spec: dict) -> str:
    app = spec.get("app")
    return " ".join(shlex.quote(str(x)) for x in app) if isinstance(app, list) else str(app)


def _launch(spec: dict, cwd: Path, log) -> subprocess.Popen:
    app = spec["app"]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if isinstance(app, list):
        argv = [str(x) for x in app]
        exe = (cwd / argv[0])
        if not Path(argv[0]).is_absolute() and exe.is_file():  # 대상 폴더 기준 상대 경로 — 실제 위치가 밖이면 거절
            if not exe.resolve().is_relative_to(cwd.resolve()):
                raise ToolError("invalid_request", f"screen.app 이 대상 폴더 밖을 가리킴: {argv[0]}")
            argv[0] = str(exe.resolve())
        return subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)
    target = cwd / str(app)
    if not Path(str(app)).is_absolute() and target.is_file() and target.suffix.lower() in (".exe", ".bat", ".cmd"):
        if not target.resolve().is_relative_to(cwd.resolve()):
            raise ToolError("invalid_request", f"screen.app 이 대상 폴더 밖을 가리킴: {app}")
        return subprocess.Popen([str(target.resolve())], cwd=cwd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)
    return subprocess.Popen(str(app), cwd=cwd, shell=True, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)


def capture(spec: dict, cwd: Path, out: str, *, visible=(), selectors=(), timeout: float = 120) -> dict:
    """한 앱을 띄워 확인한다. 결과: {ok, title, found, missing, selectors_missing, errors, out, controls}."""
    if not available():
        raise ToolError("capability_unavailable", "네이티브 앱 화면 확인은 지금 Windows 에서만 됩니다",
                        recovery="웹 화면이면 screen.file·screen.path 로, 아니면 CLI·함수 케이스로 확인")
    cwd = Path(cwd)
    log = tempfile.TemporaryFile()
    proc = _launch(spec, cwd, log)
    req = {"pid": proc.pid, "title": spec.get("title") or "", "timeout_ms": int(spec.get("timeout_ms") or 30000),
           "wait_ms": int(spec.get("wait_ms") or 800), "width": int(spec.get("width") or 0), "height": int(spec.get("height") or 0),
           "actions": spec.get("actions") or [], "out": out or ""}
    fd, spec_file = tempfile.mkstemp(suffix=".json", prefix="native-")
    os.close(fd)
    Path(spec_file).write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    got: dict = {}
    try:
        from beom_workbench.core.sandbox import _ps_quote, ps_run_file
        # 툴킷의 스크립트도 실행 정책을 풀지 않고 '친 명령'처럼 돌린다 (-ExecutionPolicy Bypass 를 쓰지 않는다)
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            ps_run_file(str(PROBE)) + " -SpecFile " + _ps_quote(spec_file)], capture_output=True, timeout=timeout)
        for line in reversed(p.stdout.decode("utf-8", "replace").splitlines()):
            if line.strip().startswith("{"):
                try:
                    got = json.loads(line)
                    break
                except ValueError:
                    continue
        if not got:
            got = {"ok": False, "errors": [f"확인 스크립트 실패: {p.stderr.decode('utf-8', 'replace').strip()[-300:]}"]}
    except subprocess.TimeoutExpired:
        got = {"ok": False, "errors": ["시간 초과"]}
    finally:
        exited = proc.poll()
        _kill(proc)
        time.sleep(0.2)
        Path(spec_file).unlink(missing_ok=True)
    log.seek(0)
    tail = log.read()[-20000:].decode("utf-8", "replace")
    log.close()
    ko = {"app window not found (timeout or app exited)": "앱 창을 찾지 못함 (시간 초과 또는 앱 종료)",
          "action target not found": "동작 대상을 찾지 못함", "cannot click": "누를 수 없는 컨트롤",
          "action failed": "동작 실패", "screenshot failed": "스크린샷 실패"}
    errors = []
    for e in got.get("errors") or []:
        head, sep, rest = str(e).partition(":")
        errors.append(ko.get(str(e)) or (f"{ko[head]}:{rest}" if head in ko and sep else str(e)))
    if exited is not None and exited != 0:
        errors.append(f"앱이 확인 도중 종료됨 (exit {exited})")
    for m in _ERR_LINE.finditer(tail):
        line = tail[m.start():].splitlines()[0].strip()
        errors.append(f"표준 오류: {line[:200]}")
        break
    texts = [str(t) for t in got.get("texts") or []]
    hay = "\n".join(texts)
    controls = got.get("controls") or []

    def has(sel: str) -> bool:
        kind, _, name = str(sel).partition(":")
        return any(c.get("type", "").lower() == kind.strip().lower() and
                   (not name or name.strip() in (c.get("name") or "") or name.strip() == c.get("id")) for c in controls)

    return {"ok": bool(got.get("ok")), "title": got.get("title"), "found": [v for v in visible if v in hay],
            "missing": [v for v in visible if v not in hay], "selectors_missing": [s for s in selectors if not has(s)],
            "errors": errors[:5], "out": got.get("out") if got.get("ok") else None, "controls": len(controls)}


def to_result(r: dict) -> dict:
    loaded = bool(r.get("ok"))
    return {"status": 200 if loaded else None,
            "body": {"title": r.get("title"), "visible": r.get("found", []), "missing": r.get("missing", []),
                     "selectors_missing": r.get("selectors_missing", []), "errors": (r.get("errors") or [])[:5], "native": True},
            "screenshot": r.get("out") if loaded else None,
            **({} if loaded else {"error": "; ".join(r.get("errors") or ["앱 창을 찾지 못함"])[:300]})}
