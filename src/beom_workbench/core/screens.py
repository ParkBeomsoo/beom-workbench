"""화면 케이스 실행 — 페이지를 보이지 않는 브라우저 엔진에 띄워 확인하고 스크린샷을 남긴다 (app/shot.py 를 한 번 띄워 여러 장).

케이스: {"name": "home", "description": "첫 화면", "screen": {"path": "/"} 또는 {"file": "index.html"},
         "expect": {"visible": ["주문하기"], "selectors": ["form button"], "no_errors": true}}
선택: screen.width·height (기본 1280×800), screen.mobile=true (390×844), screen.wait_ms.
file 에 #조각·?질의 를 붙일 수 있다 ({"file": "index.html#q=패딩"}).
screen.actions — 열고 나서 차례로: {"fill": "CSS 선택자", "value": "글자"}, {"click": "선택자"},
{"press": "Enter", "on": "선택자"(생략하면 포커스된 요소)}, {"wait_ms": 300}. 그 뒤의 모습을 확인·촬영한다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from beom_workbench.common.response import ToolError

MOBILE = (390, 844)
DESKTOP = (1280, 800)


def available() -> bool:
    return importlib.util.find_spec("PySide6") is not None and importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None


def page_spec(spec: dict, url: str | None, root, out: str, file_base: str | None = None) -> dict:
    """file_base: 대상 폴더를 내보내는 로컬 주소 (serve_folder) — 파일 화면은 file:// 이 아니라 이 주소로 연다."""
    w, h = MOBILE if spec.get("mobile") else DESKTOP
    if spec.get("file"):
        import re
        from urllib.parse import quote
        m = re.match(r"([^#?]*)(.*)$", str(spec["file"]))
        path, suffix = m.group(1), m.group(2)  # index.html#q=패딩 — 조각·질의는 주소에 붙인다
        full = (root / path).resolve()
        if not full.is_relative_to(root.resolve()) or not full.is_file():
            raise ToolError("invalid_request", f"screen.file 은 대상 폴더 안의 파일이어야 함: {spec['file']}")
        rel = full.relative_to(root.resolve()).as_posix()
        if file_base:
            target = f"{file_base}/{quote(rel)}{quote(suffix, safe='#?=&/%+:,;@')}"
        else:
            target = full.as_uri() + quote(suffix, safe="#?=&/%+:,;@")
    elif url is not None:
        from beom_workbench.core.port import check_url
        target = url.rstrip("/") + (spec.get("path") or "/")
        check_url(target)
    else:
        raise ToolError("invalid_request", "화면 케이스에는 screen.file(로컬 파일) 또는 screen.path + 서버 주소(*_url)가 필요함")
    acts = spec.get("actions") or []
    if not isinstance(acts, list) or any(not isinstance(a, dict) or not (set(a) & {"fill", "click", "press", "wait_ms"})
                                         or set(a) - {"fill", "value", "click", "press", "on", "wait_ms"} for a in acts):
        raise ToolError("invalid_request", "웹 화면의 actions 는 [{fill, value} | {click} | {press, on?} | {wait_ms}] 목록")
    return {"url": target, "out": out, "width": int(spec.get("width") or w), "height": int(spec.get("height") or h),
            "wait_ms": int(spec.get("wait_ms") or 800), "actions": acts[:30]}


class serve_folder:
    """대상 폴더만 127.0.0.1 로 내보낸다 — 페이지가 file:// 로 디스크의 다른 파일을 읽지 못하게."""

    def __init__(self, root):
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def parse_request(self):  # 이 서버 주소로 온 요청만 — 다른 이름으로 우회(DNS rebinding)해 오는 페이지는 거절
                ok = super().parse_request()
                if ok and (self.headers.get("Host") or "") != f"127.0.0.1:{self.server.server_address[1]}":
                    self.send_error(403)
                    return False
                return ok

            def translate_path(self, path):  # 심볼릭 링크로 대상 폴더 밖 파일을 읽지 못하게 — 실제 위치가 밖이면 없는 파일로
                full = super().translate_path(path)
                real, base = os.path.realpath(full), os.path.realpath(root_str)
                if real != base and not real.startswith(base.rstrip(os.sep) + os.sep):
                    return os.path.join(base, "__outside__")
                # 화면에 필요 없는 비밀·기록은 내보내지 않는다 — git 내부, .env, 툴킷 기록(.beom 의 brief 화면 설계는 예외)
                parts = [x.lower() for x in os.path.relpath(real, base).split(os.sep)]
                if ".git" in parts or any(x.startswith(".env") for x in parts) or (parts[:1] == [".beom"] and parts[1:2] != ["brief"]):
                    return os.path.join(base, "__hidden__")
                return full

            def guess_type(self, path):  # 글자 파일은 UTF-8 이라고 알려 준다 (meta charset 이 없는 페이지의 한글이 깨지지 않게)
                t = super().guess_type(path)
                if t.startswith("text/") or t in ("application/javascript", "application/json", "image/svg+xml"):
                    t += "; charset=utf-8"
                return t

        root_str = str(root)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=root_str))
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def __enter__(self):
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()


def capture(pages: list[dict], timeout: float = 300, coverage_dir: Path | None = None) -> list[dict]:
    """여러 페이지를 브라우저 엔진 하나로 차례대로. 결과는 pages 와 같은 순서.
    coverage_dir: 검증 중 — 페이지 JS 의 실행 범위를 케이스마다 그 폴더에 남긴다 (브라우저 엔진의 개발자 도구 연결을 이 컴퓨터
    안(127.0.0.1)에서만, 이 실행 동안만 연다)."""
    if not pages:
        return []
    if not available():
        raise ToolError("capability_unavailable", "화면 케이스에는 앱 구성요소가 필요함",
                        recovery="pip install 'beom-workbench[app]' (PySide6 WebEngine)")
    # 결과는 UTF-8 로 받는다 — Windows 기본 코드 페이지(cp949)로 나오면 한글 확인 결과가 깨진다
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if coverage_dir is not None:
        import socket
        Path(coverage_dir).mkdir(parents=True, exist_ok=True)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env.update(BEOM_SHOT_COVERAGE=str(coverage_dir), QTWEBENGINE_REMOTE_DEBUGGING=f"127.0.0.1:{port}")
    else:
        env.pop("BEOM_SHOT_COVERAGE", None)
        env.pop("QTWEBENGINE_REMOTE_DEBUGGING", None)
    proc = subprocess.run([sys.executable, "-m", "beom_workbench.app.shot", json.dumps({"pages": pages})],
                          capture_output=True, timeout=timeout, env=env)
    for line in reversed(proc.stdout.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                got = json.loads(line)
            except ValueError:
                continue
            res = got.get("pages") or []
            return res + [{"ok": False, "errors": ["결과 없음"]}] * (len(pages) - len(res))
    tail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
    raise ToolError("internal", f"화면 확인 실패 (exit {proc.returncode}): {tail}")


def to_result(r: dict) -> dict:
    """케이스 결과 모양으로 — status 는 열림(200)/못 염(None), body 는 확인 결과, screenshot 은 이미지 경로."""
    loaded = r.get("ok") and "페이지를 열지 못함" not in (r.get("errors") or [])
    return {"status": 200 if loaded else None,
            "body": {"title": r.get("title"), "visible": r.get("found", []), "missing": r.get("missing", []),
                     "selectors_missing": r.get("selectors_missing", []), "errors": (r.get("errors") or [])[:5]},
            "screenshot": r.get("out") if loaded else None,
            **({} if loaded else {"error": "; ".join(r.get("errors") or ["페이지를 열지 못함"])[:300]})}
