"""화면 확인 — 페이지를 보이지 않는 창(Qt WebEngine)에 띄워 글자·요소가 있는지 보고 스크린샷을 남긴다.

    python -m beom_workbench.app.shot '{"pages": [{"url": "...", "out": "a.png", "width": 1280, "height": 800,
                                                  "visible": ["주문하기"], "selectors": ["form button"], "wait_ms": 800}]}'

여러 페이지를 브라우저 엔진 하나로 차례대로 연다 (엔진 시작이 가장 오래 걸린다). 페이지마다 새 비공개 프로필이라 앞 케이스가
저장한 것(localStorage·쿠키)이 다음 케이스로 넘어가지 않는다 — 케이스는 서로 독립이다. 결과는 표준 출력 마지막 줄 JSON:
{"pages": [{"ok", "title", "found", "missing", "selectors_missing", "errors", "out", "height"}]}.
따로 설치할 것 없이 앱 구성요소(PySide6 WebEngine)만 쓴다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    spec = json.loads(sys.argv[1])
    pages = spec.get("pages") or [spec]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    results: list[dict] = []
    state = {"i": -1, "cur": None, "timer": None}
    # 검증 중이면 페이지 JS 의 실행 범위도 잰다 — 개발자 도구 연결(CDP)로 V8 커버리지를 받아 케이스마다 파일로 남긴다
    cov_dir = os.environ.get("BEOM_SHOT_COVERAGE")
    dbg = (os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING") or "").rsplit(":", 1)[-1]
    cov = {"ws": None, "pending": {}, "seq": 0}

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 — Qt 이름
            if state["cur"] is not None and level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                state["cur"]["errors"].append(str(message)[:300])

    view = QWebEngineView()
    view.setPage(Page(view))
    view.show()
    kept: list = []  # 지난 페이지·프로필 — 끝날 때까지 붙잡아 둔다 (쓰는 중에 지우면 엔진이 죽는다)

    def fresh_page():
        """새 비공개 프로필의 페이지 — 저장소가 비어 있다 (같은 주소의 앞 케이스가 남긴 기록이 보이지 않게)."""
        prof = QWebEngineProfile(view)  # 이름 없는 프로필은 디스크에 남기지 않는 비공개 프로필
        page = Page(prof, view)
        kept.extend([view.page(), prof])
        view.setPage(page)

    def cdp_send(method, params=None, cb=None):
        cov["seq"] += 1
        cov["pending"][cov["seq"]] = cb
        cov["ws"].sendTextMessage(json.dumps({"id": cov["seq"], "method": method, "params": params or {}}))

    def cdp_msg(text):
        try:
            m = json.loads(text)
        except ValueError:
            return
        if m.get("method") == "Debugger.scriptParsed":
            # 스크립트가 문서 어디서 시작하는지 — HTML 안에 직접 쓴 <script> 의 실행 범위를 HTML 파일의 줄에 맞출 때 쓴다
            p = m.get("params") or {}
            cov.setdefault("scripts", {})[p.get("scriptId")] = [p.get("startLine", 0), p.get("startColumn", 0)]
            return
        cb = cov["pending"].pop(m.get("id"), None) if "id" in m else None
        if cb:
            cb(m.get("result") if "result" in m else {"error": m.get("error")})

    def cov_start(idx, then):
        """빈 페이지(about:blank)에서 측정을 켠 뒤 대상 주소로 간다 — 처음 불릴 때 도는 코드까지 잰다. 예전에는 대상을 연 뒤 측정을
        켜고 다시 불러와서, 서버의 GET 이 두 번 일어나 결과가 측정하지 않은 쪽과 달라질 수 있었다. 안 되면 재지 않고 이어 간다."""
        from PySide6.QtWebSockets import QWebSocket
        cur = state["cur"]
        ws = QWebSocket()
        cov.update(ws=ws, pending={}, scripts={})
        ws.textMessageReceived.connect(cdp_msg)

        def go(on):
            if idx == state["i"] and cur.get("_cov") == "connecting":
                cur["_cov"] = "on" if on else "failed"
                QTimer.singleShot(0, then)
        QTimer.singleShot(8000, lambda: go(False))
        # 디버거는 스크립트 위치를 받으려고만 켠다 — 페이지의 debugger 문에서 멈추지 않게 모든 멈춤을 건너뛴다
        ws.connected.connect(lambda: cdp_send("Profiler.enable", {}, lambda r: cdp_send("Debugger.enable", {}, lambda rd: cdp_send(
            "Debugger.setSkipAllPauses", {"skip": True}, lambda rs: cdp_send(
                "Profiler.startPreciseCoverage", {"callCount": True, "detailed": True}, lambda r2: go(True))))))
        ws.open(QUrl(f"ws://127.0.0.1:{dbg}/devtools/page/{view.page().devToolsId()}"))

    def cov_take(idx, then):
        """케이스가 끝날 때 — 실행 범위와, 브라우저가 받은 스크립트의 내용 지문(파일과 같은지 툴킷이 확인한다)을 남긴다."""
        cur = state["cur"]
        if cur.get("_cov") != "on" or cov["ws"] is None:
            then()
            return
        page_url = view.page().url().toString()

        def done(r):
            import hashlib
            import urllib.request
            from urllib.parse import urlsplit
            here = urlsplit(page_url).netloc
            # 같은 서버(검증 서버)의 스크립트만 — 바깥(CDN) 스크립트는 프로젝트 파일이 아니고, 받으러 가면 느려진다
            scripts = [x for x in (r or {}).get("result") or []
                       if str(x.get("url", "")).startswith(("http://", "https://")) and urlsplit(x["url"]).netloc == here]
            for x in scripts:  # 문서 안 시작 위치 (외부 .js 파일은 0,0 — HTML 안 <script> 는 그 블록이 시작하는 줄·칸)
                x["start"] = (cov.get("scripts") or {}).get(x.get("scriptId"), [0, 0])
            sources = {}
            for x in scripts:
                try:
                    with urllib.request.urlopen(x["url"], timeout=5) as resp:  # noqa: S310 — 검증 서버(127.0.0.1)
                        sources[x["url"]] = hashlib.sha256(resp.read()).hexdigest()
                except Exception:  # noqa: BLE001 — 못 받으면 그 스크립트는 파일과 맞춰 보지 못한다
                    continue
            try:
                Path(cov_dir, f"page-{idx}.json").write_text(json.dumps({"url": page_url, "result": scripts, "sources": sources}),
                                                            encoding="utf-8")
            except OSError:
                pass
            ws = cov["ws"]
            cov["ws"] = None
            kept.append(ws)  # 자기 신호를 처리하는 중에 지우면 엔진이 죽는다 — 끝날 때까지 붙잡고, 닫기·다음 케이스는 다음 차례에
            QTimer.singleShot(0, lambda: (ws.close(), then()))
        cdp_send("Profiler.takePreciseCoverage", {}, done)
        QTimer.singleShot(8000, lambda: idx == state["i"] and cov["ws"] is not None and (cov.update(ws=None), then()))

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
        if state["i"] > 0:
            fresh_page()  # 케이스마다 빈 저장소에서 — 첫 페이지는 방금 만든 페이지 그대로
        if cov_dir and dbg and target.scheme() in ("http", "https"):
            state["cur"]["_cov"] = "starting"
            state["cur"]["_target"] = target
            view.load(QUrl("about:blank"))  # 측정을 켤 빈 페이지 — 켜고 나서 대상으로 간다 (loaded 에서)
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
            if p.get("audit") and "audit" not in cur:  # 디자인 점검 — 찍기 전에 이 크기 그대로 잰다
                def audited(r):
                    try:
                        cur["audit"] = json.loads(r) if isinstance(r, str) else None
                    except ValueError:
                        cur["audit"] = None
                    shoot()
                view.page().runJavaScript(AUDIT_JS, 0, audited)
                return
            if p.get("out"):
                view.grab().save(p["out"])
            cur["height"] = full
            if state["timer"]:
                state["timer"].stop()
            cov_take(idx, next_page)

        QTimer.singleShot(300, shoot)

    # 화면 동작 — 사람이 하듯 진짜 입력으로. 예전에는 자바스크립트로 만든 가짜 키보드·클릭 이벤트를 보내, Tab 으로 포커스가 옮겨
    # 가지 않고(:focus-visible 확인이 실패) 기본 동작(Enter·Space)을 흉내 내야 했다. 이제 키는 브라우저 엔진에 진짜 키 입력으로,
    # 클릭은 요소 한가운데를 진짜 마우스로 누른다 (다른 요소에 가려져 있으면 사람도 못 누른다 — 그렇다고 알린다).
    # 값 채우기(fill)는 입력칸에 값을 넣고 input·change 를 알린다 (한글 같은 글자는 키 입력으로 보낼 수 없어서).
    FILL_JS = """((a) => {
      const el = (() => { try { return document.querySelector(a.fill); } catch (e) { return null; } })();
      if (!el) return "입력할 요소 없음: " + a.fill;
      el.focus();
      const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
      if (d && d.set) d.set.call(el, String(a.value ?? "")); else el.value = String(a.value ?? "");
      el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true }));
      return "";
    })(%s)"""
    AIM_JS = """((sel, focus) => {
      let el; try { el = sel ? document.querySelector(sel) : document.activeElement; } catch (e) { el = null; }
      if (!el) return JSON.stringify({ missing: true });
      el.scrollIntoView({ block: "center", inline: "center" });
      if (focus) { if (el.focus) el.focus(); return JSON.stringify({ ok: true }); }
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return JSON.stringify({ hidden: true });
      const x = r.left + r.width / 2, y = r.top + r.height / 2, top = document.elementFromPoint(x, y);
      return JSON.stringify({ x, y, covered: !(top && (top === el || el.contains(top))),
                              by: top && top !== el && !el.contains(top) ? (top.id ? "#" + top.id : top.tagName.toLowerCase()) : null });
    })(%s, %s)"""
    KEYS = {"enter": "Key_Return", "return": "Key_Return", "tab": "Key_Tab", "escape": "Key_Escape", "esc": "Key_Escape",
            "space": "Key_Space", "spacebar": "Key_Space", " ": "Key_Space", "backspace": "Key_Backspace", "delete": "Key_Delete",
            "arrowup": "Key_Up", "arrowdown": "Key_Down", "arrowleft": "Key_Left", "arrowright": "Key_Right", "up": "Key_Up",
            "down": "Key_Down", "left": "Key_Left", "right": "Key_Right", "home": "Key_Home", "end": "Key_End",
            "pageup": "Key_PageUp", "pagedown": "Key_PageDown"}
    MODS = {"shift": "ShiftModifier", "ctrl": "ControlModifier", "control": "ControlModifier", "alt": "AltModifier",
            "meta": "MetaModifier", "cmd": "MetaModifier"}

    def double_click(w, at) -> None:
        """진짜 더블클릭 — 누르기·떼기 두 번을 짧은 간격의 시각으로 보낸다. 브라우저 엔진은 이벤트 시각과 자리로 클릭 횟수를 세어
        dblclick 을 만든다. QTest.mouseDClick 은 Qt 의 '더블클릭' 이벤트 하나를 보내는데 엔진이 이를 클릭 횟수로 세지 않고, QTest 의
        클릭 두 번은 우연한 더블클릭을 막으려 시각을 더블클릭 간격만큼 벌려 두 번의 한 번 클릭이 된다."""
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication
        import time
        local = QPointF(at)
        glob = QPointF(w.mapToGlobal(at))
        t0 = int(time.monotonic() * 1000)
        seq = [(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton), (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
               (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton), (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)]
        for i, (kind, buttons) in enumerate(seq):
            ev = QMouseEvent(kind, local, glob, Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)
            ev.setTimestamp(t0 + 40 * i)
            QApplication.sendEvent(w, ev)


    def send_key(spec: str) -> str:
        """'Tab' · 'Shift+Tab' · 'Enter' · 'a' 같은 키 하나를 진짜 키 입력으로."""
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        parts = [x for x in str(spec).split("+") if x] if str(spec).strip() != "+" else ["+"]
        if str(spec) in (" ", "Space"):
            parts = [" "]
        mods = Qt.KeyboardModifier.NoModifier
        for m in parts[:-1]:
            if m.strip().lower() not in MODS:
                return f"알 수 없는 키 조합: {spec}"
            mods |= getattr(Qt.KeyboardModifier, MODS[m.strip().lower()])
        key = parts[-1] if parts else ""
        w = view.focusProxy() or view
        view.activateWindow()
        w.setFocus()
        name = KEYS.get(key.lower()) if key != " " else KEYS[" "]
        if name:
            QTest.keyClick(w, getattr(Qt.Key, name), mods)
        elif len(key) == 1 and key.isascii():
            QTest.keyClick(w, key, mods)
        else:
            return f"보낼 수 없는 키: {spec} (글자 입력은 fill 로)"
        return ""

    # 디자인 점검 — 사람이 눈으로 놓치기 쉬운 것을 실제 계산된 스타일·위치로 잰다 (WCAG 대비·누르는 크기 등)
    AUDIT_JS = r"""(() => {
      const vw = innerWidth, mobile = vw <= 480, cap = (a) => a.slice(0, 15);
      const vis = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && parseFloat(s.opacity) > 0.05; };
      const name = (el) => el.id ? "#" + el.id : el.tagName.toLowerCase() + (el.classList.length ? "." + [...el.classList].slice(0, 2).join(".") : "");
      const txt = (el) => (el.innerText || el.value || el.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 30);
      const parse = (c) => { const m = String(c).match(/rgba?\(([^)]+)\)/); if (!m) return null;
        const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(parseFloat); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
      const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
        return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
      const bgOf = (el) => { const layers = []; for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
          const s = getComputedStyle(e); if (s.backgroundImage && s.backgroundImage !== "none") return null;  // 그림 위 글자는 계산하지 않는다
          const c = parse(s.backgroundColor); if (c && c.a > 0) { layers.push(c); if (c.a >= 0.99) break; } }
        let b = { r: 255, g: 255, b: 255 };
        for (let i = layers.length - 1; i >= 0; i--) { const c = layers[i]; b = { r: c.r * c.a + b.r * (1 - c.a), g: c.g * c.a + b.g * (1 - c.a), b: c.b * c.a + b.b * (1 - c.a) }; }
        return b; };
      const out = { contrast: [], small_text: [], tiny_targets: [], small_targets: [], clipped: [], overlap: [], overflow: null };
      const fonts = new Set(), sizes = new Set(), colors = new Set(), hits = [];
      for (const el of (document.body ? document.body.querySelectorAll("*") : [])) {
        if (!vis(el) || ["SCRIPT", "STYLE", "SVG", "PATH"].includes(el.tagName.toUpperCase())) continue;
        const s = getComputedStyle(el);
        const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
        const off = el.disabled || el.closest("[disabled],[aria-disabled='true']");
        if (own) {
          const fs = parseFloat(s.fontSize), fw = parseInt(s.fontWeight) || 400;
          fonts.add(s.fontFamily.split(",")[0].replace(/["']/g, "").trim()); sizes.add(Math.round(fs)); colors.add(s.color);
          const fg = parse(s.color), bg = bgOf(el);
          if (fg && bg && fg.a > 0.05 && !off) {
            const ratio = (Math.max(lum(fg), lum(bg)) + 0.05) / (Math.min(lum(fg), lum(bg)) + 0.05);
            const need = fs >= 24 || (fs >= 18.66 && fw >= 700) ? 3 : 4.5;
            if (ratio < need) out.contrast.push({ el: name(el), text: txt(el), ratio: +ratio.toFixed(2), need });
          }
          if (fs < 12) out.small_text.push({ el: name(el), text: txt(el), size: fs });
          if (/(hidden|clip)/.test(s.overflow + s.overflowX) && el.scrollWidth > el.clientWidth + 1) out.clipped.push({ el: name(el), text: txt(el) });
        }
        if (el.matches("a[href],button,input:not([type=hidden]),select,textarea,[role=button],[onclick],summary") && !off) {
          const r = el.getBoundingClientRect();
          const inline = s.display === "inline" && el.closest("p,li,td");  // 문장 안 링크는 크기 기준에서 뺀다 (WCAG 예외)
          if (!inline) {
            const sz = { el: name(el), text: txt(el), w: Math.round(r.width), h: Math.round(r.height) };
            if (r.width < 24 || r.height < 24) out.tiny_targets.push(sz);
            else if (mobile && (r.width < 44 || r.height < 44)) out.small_targets.push(sz);
            if (hits.length < 150) hits.push([el, r]);
          }
        }
      }
      for (let i = 0; i < hits.length; i++) for (let j = i + 1; j < hits.length; j++) {
        const [a, ra] = hits[i], [b, rb] = hits[j];
        if (a.contains(b) || b.contains(a)) continue;
        const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        if (w > 4 && h > 4) out.overlap.push({ a: name(a), b: name(b) });
      }
      const sw = document.documentElement.scrollWidth;
      if (sw > vw + 1) {
        const wide = [...document.body.querySelectorAll("*")].filter((e) => vis(e) && e.getBoundingClientRect().right > vw + 1).slice(0, 5).map(name);
        out.overflow = { page: sw, viewport: vw, wide };
      }
      for (const k of Object.keys(out)) if (Array.isArray(out[k])) out[k] = cap(out[k]);
      return JSON.stringify({ issues: out, variety: { fonts: [...fonts], sizes: [...sizes].sort((a, b) => a - b), text_colors: colors.size } });
    })()"""

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
        elif a.get("fill") is not None:
            view.page().runJavaScript(FILL_JS % json.dumps(a, ensure_ascii=False), 0, after)
        elif a.get("click") is not None or a.get("dblclick") is not None:
            target_sel = a.get("click") if a.get("click") is not None else a.get("dblclick")
            double = a.get("click") is None

            def clicked(raw):
                from PySide6.QtCore import QPoint, Qt
                from PySide6.QtTest import QTest
                try:
                    r = json.loads(raw) if isinstance(raw, str) else {}
                except ValueError:
                    r = {}
                if r.get("missing"):
                    return after("누를 요소 없음: " + str(target_sel))
                if r.get("hidden"):
                    return after("누를 요소가 화면에 보이지 않음 (크기 0): " + str(target_sel))
                if r.get("covered"):
                    return after(f"누를 요소가 다른 요소({r.get('by')})에 가려져 있음: {target_sel}")
                z = view.page().zoomFactor() or 1.0
                w = view.focusProxy() or view
                view.activateWindow()
                at = QPoint(int(r["x"] * z), int(r["y"] * z))
                if double:
                    double_click(w, at)
                else:
                    QTest.mouseClick(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at)
                after("")
            # 스크롤이 끝난 뒤 자리를 다시 재서 누른다
            view.page().runJavaScript(AIM_JS % (json.dumps(target_sel), "false"), 0,
                                      lambda _r: QTimer.singleShot(60, lambda: idx == state["i"] and view.page().runJavaScript(
                                          AIM_JS % (json.dumps(target_sel), "false"), 0, clicked)))
        elif a.get("press") is not None:
            def focused(raw):
                try:
                    r = json.loads(raw) if isinstance(raw, str) else {}
                except ValueError:
                    r = {}
                if a.get("on") and r.get("missing"):
                    return after("키를 누를 요소 없음: " + str(a["on"]))
                after(send_key(a["press"]))
            if a.get("on"):
                view.page().runJavaScript(AIM_JS % (json.dumps(a["on"]), "true"), 0, focused)
            else:
                focused("{}")
        else:
            after("알 수 없는 동작: " + json.dumps(a, ensure_ascii=False)[:80])

    def loaded(ok):
        if state["cur"] is None or state["i"] >= len(pages) or state["cur"].get("_loaded"):
            return  # 페이지가 스스로 다시 이동해도 한 번만 확인한다
        cur = state["cur"]
        if cur.get("_cov") == "connecting":
            return
        if cur.get("_cov") == "starting":
            if view.page().url().toString() in ("about:blank", ""):
                i0 = state["i"]
                cur["_cov"] = "connecting"  # 빈 페이지 불러오기 알림이 두 번 와도 한 번만 연결한다
                cov_start(i0, lambda: idx_ok(i0) and view.load(cur["_target"]))  # 측정을 켜고 대상으로 (안 되면 재지 않고)
            return
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

    def idx_ok(i0):
        return i0 == state["i"]

    view.loadFinished.connect(loaded)
    QTimer.singleShot(0, next_page)
    app.exec()
    print(json.dumps({"pages": results}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
