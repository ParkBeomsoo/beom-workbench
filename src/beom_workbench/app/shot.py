"""화면 확인 — 페이지를 보이지 않는 창(Qt WebEngine)에 띄워 글자·요소가 있는지 보고 스크린샷을 남긴다.

    python -m beom_workbench.app.shot '{"pages": [{"url": "...", "out": "a.png", "width": 1280, "height": 800,
                                                  "visible": ["주문하기"], "selectors": ["form button"], "wait_ms": 800}]}'

여러 페이지를 브라우저 엔진 하나로 차례대로 연다 (엔진 시작이 가장 오래 걸린다). 결과는 표준 출력 마지막 줄 JSON:
{"pages": [{"ok", "title", "found", "missing", "selectors_missing", "errors", "out", "height"}]}.
따로 설치할 것 없이 앱 구성요소(PySide6 WebEngine)만 쓴다.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    spec = json.loads(sys.argv[1])
    pages = spec.get("pages") or [spec]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    results: list[dict] = []
    state = {"i": -1, "cur": None, "timer": None, "pending": None}

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 — Qt 이름
            if state["cur"] is not None and level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                state["cur"]["errors"].append(str(message)[:300])

    view = QWebEngineView()
    view.setPage(Page(view))
    view.show()

    def next_page():
        if state["cur"] is not None:
            results.append({k: v for k, v in state["cur"].items() if not k.startswith("_")})
        state["i"] += 1
        if state["i"] >= len(pages):
            app.quit()
            return
        p = pages[state["i"]]
        state["cur"] = {"ok": False, "found": [], "missing": [], "selectors_missing": [], "errors": [], "out": p.get("out")}
        view.resize(int(p.get("width") or 1280), int(p.get("height") or 800))
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: (state["cur"]["errors"].append("시간 초과"), next_page()))
        guard.start(int(p.get("timeout_ms") or 30000))
        state["timer"] = guard
        url = p["url"]
        target = QUrl(url) if "://" in url else QUrl.fromLocalFile(url)
        here = view.url()
        if not here.isEmpty() and here.toString().split("#")[0] == target.toString().split("#")[0]:
            # 같은 파일에 #조각만 다르면 브라우저는 페이지 안 이동으로 보고 '다 열림'을 알리지 않는다 — 빈 페이지를 거쳐 새로 연다
            state["pending"] = target
            view.load(QUrl("about:blank"))
        else:
            view.load(target)

    def checked(raw, idx):
        if idx != state["i"]:  # 늦게 온 결과 (시간 초과로 이미 다음 페이지) — 버린다
            return
        p, cur = pages[state["i"]], state["cur"]
        if cur.get("_checked"):
            return
        cur["_checked"] = True
        try:
            res = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            res = None
        if res is None:  # 페이지가 이동하는 중이라 결과를 못 읽음 — 없는 것으로 본다 (조용히 통과시키지 않게)
            cur["errors"].append("화면 확인 결과를 읽지 못함 (페이지가 이동 중이었을 수 있음)")
            res = {"missing": list(p.get("visible") or []), "smissing": list(p.get("selectors") or [])}
        cur.update(ok=True, title=res.get("title"), found=res.get("found", []), missing=res.get("missing", []),
                   selectors_missing=res.get("smissing", []))
        w, h = int(p.get("width") or 1280), int(p.get("height") or 800)
        full = min(max(h, int(res.get("height") or h)), 6000) if p.get("full_page", True) else h
        view.resize(w, full)

        def shoot():
            if idx != state["i"]:
                return
            if p.get("out"):
                view.grab().save(p["out"])
            cur["height"] = full
            if state["timer"]:
                state["timer"].stop()
            next_page()

        QTimer.singleShot(300, shoot)

    ACT_JS = """((a) => {
      const pick = (s) => { try { return s ? document.querySelector(s) : document.activeElement; } catch (e) { return null; } };
      if (a.fill !== undefined) {
        const el = pick(a.fill); if (!el) return "입력할 요소 없음: " + a.fill;
        el.focus();
        const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
        if (d && d.set) d.set.call(el, String(a.value ?? "")); else el.value = String(a.value ?? "");
        el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true }));
        return "";
      }
      if (a.click !== undefined) { const el = pick(a.click); if (!el) return "누를 요소 없음: " + a.click; el.scrollIntoView({ block: "center" }); el.click(); return ""; }
      if (a.press !== undefined) {
        const el = pick(a.on) || document.body; const key = String(a.press);
        const opt = { key, code: key === "Enter" ? "Enter" : key, keyCode: key === "Enter" ? 13 : key === "Escape" ? 27 : 0, bubbles: true, cancelable: true };
        const go = el.dispatchEvent(new KeyboardEvent("keydown", opt));
        el.dispatchEvent(new KeyboardEvent("keyup", opt));
        if (go && key === "Enter" && el.form) { if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit(); }
        return "";
      }
      return "";
    })(%s)"""

    def run_actions(idx, acts, done):
        if idx != state["i"]:
            return
        if not acts:
            done()
            return
        a = acts[0]

        def after(r):
            if idx != state["i"]:
                return
            if r:
                state["cur"]["errors"].append(f"동작 실패: {r}"[:300])
            QTimer.singleShot(int(a.get("wait_ms") or 250), lambda: run_actions(idx, acts[1:], done))

        if set(a) <= {"wait_ms"}:
            after("")
        else:
            view.page().runJavaScript(ACT_JS % json.dumps(a, ensure_ascii=False), 0, after)

    def loaded(ok):
        if state.get("pending") is not None:  # 빈 페이지를 거쳤다 — 이제 진짜 페이지
            target, state["pending"] = state["pending"], None
            view.load(target)
            return
        if state["cur"] is None or state["i"] >= len(pages) or state["cur"].get("_loaded"):
            return  # 페이지가 스스로 다시 이동해도 한 번만 확인한다
        state["cur"]["_loaded"] = True
        if not ok:
            state["cur"]["errors"].append("페이지를 열지 못함")
            if state["timer"]:
                state["timer"].stop()
            QTimer.singleShot(0, next_page)
            return
        p = pages[state["i"]]
        js = """(() => { const t = document.body ? document.body.innerText : ""; const want = %s, sel = %s;
          return JSON.stringify({ title: document.title, height: document.documentElement.scrollHeight,
                   found: want.filter((w) => t.includes(w)), missing: want.filter((w) => !t.includes(w)),
                   smissing: sel.filter((s) => { try { return !document.querySelector(s); } catch (e) { return true; } }) }); })()""" % (
            json.dumps(p.get("visible") or [], ensure_ascii=False), json.dumps(p.get("selectors") or [], ensure_ascii=False))
        idx = state["i"]

        def check():
            if idx == state["i"]:
                QTimer.singleShot(150 if p.get("actions") else 0, lambda: idx == state["i"] and view.page().runJavaScript(js, 0, lambda r: checked(r, idx)))

        QTimer.singleShot(int(p.get("wait_ms") or 800), lambda: run_actions(idx, list(p.get("actions") or []), check))

    view.loadFinished.connect(loaded)
    QTimer.singleShot(0, next_page)
    app.exec()
    print(json.dumps({"pages": results}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
