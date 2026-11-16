"use strict";
// 화면 설계 프로토타입 보기 — 위에는 기능별 탭, 가운데는 눌러 볼 수 있는 화면(HTML), 오른쪽은 그 화면의 설명.
// 화면 안의 링크(S2-이름.html)를 누르거나 탭·화면 칩을 누르면 화면이 바뀌고 설명도 따라 바뀐다.
// 앱과 '내보내기' 파일이 같은 코드를 쓴다 — 바깥 라이브러리 없이, 이 파일 하나로.
// data: { overview: "<html>", groups: [{ name, ids: ["S1", …] }], screens: { S1: { name, html, notes: "<html>", links: ["S2"] } } }
(function () {
  const CSS = `
.pv{--pv-line:var(--line,#e6e6e1);--pv-line2:var(--line2,#d6d6cf);--pv-text:var(--text,#1d1d1b);--pv-muted:var(--muted,#5f5f58);--pv-faint:var(--faint,#8e8e86);
--pv-sunk:var(--sunk,#f6f6f3);--pv-panel:var(--panel,#fff);--pv-acc:var(--signal,#19735f);--pv-acc-soft:var(--signal-soft,#e5f1ed);display:flex;flex-direction:column;gap:12px}
.pv-tabs{display:flex;gap:2px;border-bottom:1px solid var(--pv-line);overflow-x:auto;scrollbar-width:none}
.pv-tabs button{border:none;background:none;font:inherit;font-size:13px;color:var(--pv-muted);padding:8px 12px 9px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap}
.pv-tabs button:hover{color:var(--pv-text)}.pv-tabs button.on{color:var(--pv-text);font-weight:650;border-bottom-color:var(--pv-text)}
.pv-tabs button em{font-style:normal;color:var(--pv-faint);font-size:11px;margin-left:5px;font-weight:500}
.pv-strip{display:flex;gap:6px;flex-wrap:wrap}
.pv-strip button{border:1px solid var(--pv-line2);background:var(--pv-panel);color:var(--pv-muted);font:inherit;font-size:12.5px;padding:5px 11px;border-radius:999px;cursor:pointer}
.pv-strip button b{font-weight:650;color:var(--pv-faint);margin-right:5px;font-size:11px}
.pv-strip button:hover{color:var(--pv-text);border-color:var(--pv-muted)}.pv-strip button.on{background:var(--pv-text);color:var(--pv-panel);border-color:var(--pv-text)}
.pv-strip button.on b{color:inherit;opacity:.7}
.pv-stage{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,340px);gap:20px;align-items:start}
.pv.narrow .pv-stage{grid-template-columns:minmax(0,1fr)}
.pv-frame{min-width:0}
.pv-bar{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:12px;color:var(--pv-faint)}
.pv-bar button{border:1px solid var(--pv-line2);background:var(--pv-panel);color:var(--pv-muted);font:inherit;font-size:12px;border-radius:7px;padding:3px 9px;cursor:pointer}
.pv-bar button:disabled{opacity:.4;cursor:default}.pv-bar .pv-hint{margin-left:auto}
.pv-device{position:relative;overflow:hidden;border-radius:12px;background:#fff;box-shadow:0 0 0 1px var(--pv-line2),0 8px 28px rgba(0,0,0,.08);margin:0 auto}
.pv-device iframe{position:absolute;left:0;top:0;border:0;transform-origin:0 0;background:#fff}
.pv-notes{position:sticky;top:8px;border:1px solid var(--pv-line);border-radius:12px;background:var(--pv-panel);padding:14px 16px;max-height:calc(100vh - 40px);overflow:auto}
.pv.narrow .pv-notes{position:static;max-height:none}
.pv-notes .pv-id{font-size:11px;font-weight:700;color:var(--pv-acc);letter-spacing:.04em}
.pv-notes h3{margin:2px 0 10px;font-size:16px;letter-spacing:-.01em}
.pv-notes .md{font-size:13px;line-height:1.65}.pv-notes .md h2{display:none}.pv-notes .md table{font-size:12px}
.pv-notes .md li.hl,.pv-notes .md p.hl{background:var(--pv-acc-soft);border-radius:6px;box-shadow:0 0 0 4px var(--pv-acc-soft)}
.pv-notes .md ol,.pv-notes .md ul{padding-left:18px}.pv-notes .md li{cursor:default}
.pv-go{margin-top:12px;padding-top:10px;border-top:1px solid var(--pv-line);font-size:12px;color:var(--pv-faint)}
.pv-go div{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.pv-link{border:1px solid var(--pv-line2);background:var(--pv-sunk);color:var(--pv-text);font:inherit;font-size:12px;border-radius:7px;padding:2px 8px;cursor:pointer}
.pv-link:hover{border-color:var(--pv-acc);color:var(--pv-acc)}
span.pv-sref{color:var(--pv-acc);font-weight:600;cursor:pointer;border-bottom:1px dotted currentColor}
.pv-over{max-width:860px}
@media print{.pv{display:none}}`;

  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function injectStyle() { if (document.getElementById("pv-style")) return; const s = el("style"); s.id = "pv-style"; s.textContent = CSS; document.head.append(s); }
  // 화면 안에 넣는 작은 스크립트(proto-frame.js) — 앱에서는 앱이 화면을 내줄 때 넣고(src), 내보낸 파일에서는 여기서 넣는다(srcdoc)
  function withInject(html, inject) {
    if (!inject) return html;
    const tag = "<script>" + inject + "<\/script>";
    return /<\/body>/i.test(html) ? html.replace(/<\/body>/i, tag + "</body>") : html + tag;
  }

  function OssProto(root, data) {
    injectStyle();
    const ids = Object.keys(data.screens || {});
    const groups = (data.groups || []).filter((g) => g.ids.some((i) => data.screens[i]));
    const pv = el("div", "pv"); root.replaceChildren(pv);
    const tabs = el("div", "pv-tabs"), body = el("div", "pv-body"); pv.append(tabs, body);
    const state = { tab: groups.length ? -1 : 0, cur: null, hist: [], fwd: [] };
    const groupOf = (id) => groups.findIndex((g) => g.ids.includes(id));
    let frame = null, device = null, notes = null, strip = null, back = null, size = { w: 1280, h: 800 };

    let raf = 0;  // 크기 맞춤은 한 프레임에 한 번 (ResizeObserver 되먹임 경고가 나지 않게)
    const ro = window.ResizeObserver ? new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { pv.classList.toggle("narrow", pv.clientWidth < 760); fit(); }); }) : null;
    if (ro) ro.observe(pv);
    window.addEventListener("message", (e) => {
      if (!frame || e.source !== frame.contentWindow) return;
      const d = e.data || {};
      if (d.pv === "go" && data.screens[d.id]) go(d.id, true);
      else if (d.pv === "size") { size = { w: Math.max(320, d.w || 1280), h: Math.max(200, d.h || 800) }; fit(); }
      else if (d.pv === "hover" && notes) for (const x of notes.querySelectorAll("li,p")) x.classList.toggle("hl", !!d.n && x.textContent.trim().startsWith(d.n));
    });
    function fit() {
      if (!frame || !device) return;
      const avail = device.parentElement.clientWidth || size.w;
      const k = Math.min(1, avail / size.w);
      frame.style.width = size.w + "px"; frame.style.height = size.h + "px"; frame.style.transform = `scale(${k})`;
      device.style.width = Math.round(size.w * k) + "px"; device.style.height = Math.round(size.h * k) + "px";
    }
    function drawTabs() {
      tabs.replaceChildren();
      if (data.overview) { const b = el("button", state.tab === -1 ? "on" : "", "개요"); b.onclick = () => { state.tab = -1; draw(); }; tabs.append(b); }
      groups.forEach((g, i) => {
        const b = el("button", state.tab === i ? "on" : "", g.name); b.append(el("em", null, String(g.ids.filter((x) => data.screens[x]).length)));
        b.onclick = () => { state.tab = i; const first = g.ids.find((x) => data.screens[x]); if (first) go(first, false); else draw(); }; tabs.append(b);
      });
    }
    function linkify(box) {  // 설명 속 'S3' 를 누르면 그 화면으로
      const walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT); const hits = [];
      while (walk.nextNode()) if (/\bS\d{1,3}\b/.test(walk.currentNode.nodeValue)) hits.push(walk.currentNode);
      for (const t of hits) {
        const frag = document.createDocumentFragment();
        t.nodeValue.split(/(\bS\d{1,3}\b)/).forEach((part) => {
          const id = /^S\d{1,3}$/.test(part) ? "S" + parseInt(part.slice(1), 10) : null;
          if (id && data.screens[id] && id !== state.cur) { const s = el("span", "pv-sref", part); s.title = data.screens[id].name; s.onclick = () => go(id, true); frag.append(s); }
          else frag.append(document.createTextNode(part));
        });
        t.replaceWith(frag);
      }
    }
    function go(id, push) {
      if (push && state.cur && state.cur !== id) { state.hist.push(state.cur); state.fwd = []; }
      state.cur = id; const g = groupOf(id); if (g >= 0) state.tab = g; else if (!groups.length) state.tab = 0;
      draw();
    }
    function draw() {
      drawTabs();
      body.replaceChildren(); frame = device = notes = null;
      if (state.tab === -1) { const o = el("div", "pv-over md"); o.innerHTML = data.overview || ""; linkify(o); body.append(o); return; }
      const g = groups[state.tab];
      const list = (g ? g.ids : ids).filter((x) => data.screens[x]);
      if (!state.cur || !list.includes(state.cur)) state.cur = list[0];
      if (!state.cur) { body.append(el("div", "pv-hint", "화면이 없습니다")); return; }
      strip = el("div", "pv-strip");
      for (const x of list) { const b = el("button", x === state.cur ? "on" : ""); b.append(el("b", null, x), document.createTextNode(data.screens[x].name || "")); b.onclick = () => go(x, true); strip.append(b); }
      const sc = data.screens[state.cur];
      const stage = el("div", "pv-stage"), wrap = el("div", "pv-frame"), bar = el("div", "pv-bar");
      back = el("button", null, "← 뒤로"); back.disabled = !state.hist.length;
      back.onclick = () => { const p = state.hist.pop(); if (p) { state.fwd.push(state.cur); state.cur = p; const gg = groupOf(p); if (gg >= 0) state.tab = gg; draw(); } };
      bar.append(back, el("span", null, `${state.cur} ${sc.name || ""}`), el("span", "pv-hint", "화면 안 버튼을 눌러 이동해 보세요"));
      device = el("div", "pv-device"); frame = el("iframe"); frame.setAttribute("sandbox", "allow-scripts"); frame.setAttribute("title", sc.name || state.cur);
      if (sc.src) frame.src = sc.src;
      else frame.srcdoc = withInject(sc.html || "<p style='font:14px sans-serif;padding:24px'>화면 파일이 없습니다</p>", data.inject);
      device.append(frame); wrap.append(bar, device);
      notes = el("aside", "pv-notes"); notes.append(el("div", "pv-id", state.cur), el("h3", null, sc.name || ""));
      const md = el("div", "md"); md.innerHTML = sc.notes || "<p>설명이 없습니다</p>"; notes.append(md);
      for (const li of md.querySelectorAll("li,p")) {  // 설명 번호(①…)를 누르면 화면에서 그 자리를 짚어 준다
        const n = (li.textContent.trim().match(/^[①-⑳]/) || [])[0];
        if (n) { li.style.cursor = "pointer"; li.onclick = (e) => { if (e.target.closest(".pv-sref")) return; frame.contentWindow && frame.contentWindow.postMessage({ pv: "flash", n }, "*"); }; }
      }
      const outs = (sc.links || []).filter((x) => data.screens[x] && x !== state.cur);
      if (outs.length) {
        const go2 = el("div", "pv-go", "이 화면에서 갈 수 있는 곳"); const row = el("div");
        for (const x of outs) { const b = el("button", "pv-link", `${x} ${data.screens[x].name || ""}`); b.onclick = () => go(x, true); row.append(b); }
        go2.append(row); notes.append(go2);
      }
      linkify(md);
      stage.append(wrap, notes); body.append(strip, stage);
      size = { w: sc.width || 1280, h: sc.height || 800 }; fit();
    }
    if (groups.length && !data.overview) state.tab = 0;
    if (state.tab >= 0) { const g = groups[state.tab]; state.cur = (g ? g.ids : ids).find((x) => data.screens[x]) || null; }
    draw();
    return { go };
  }

  // 내보낸 파일: 문서마다 탭으로 (스크롤로 이어 붙이지 않고), 화면 설계는 프로토타입으로. 인쇄(PDF)할 때는 모두 펼친다
  function OssExport() {
    let data = {};
    try { data = JSON.parse(document.getElementById("beom-data").textContent || "{}"); } catch (e) { data = {}; }
    const secs = [...document.querySelectorAll("section.doc")], links = [...document.querySelectorAll(".toc a")];
    if (secs.length > 1) {
      document.body.classList.add("tabbed");
      const show = (i) => { secs.forEach((s, k) => s.classList.toggle("on", k === i)); links.forEach((a, k) => a.classList.toggle("on", k === i)); window.scrollTo(0, 0); };
      links.forEach((a, i) => a.addEventListener("click", (e) => { e.preventDefault(); show(i); history.replaceState(null, "", "#doc-" + i); }));
      document.addEventListener("click", (e) => { const a = e.target.closest && e.target.closest("a[href^='#doc-']"); if (a && !a.closest(".toc")) { e.preventDefault(); show(+a.getAttribute("href").slice(5) || 0); } });
      const m = location.hash.match(/^#doc-(\d+)/); show(m ? Math.min(+m[1], secs.length - 1) : 0);
    }
    for (const mount of document.querySelectorAll(".proto-mount")) if (data.proto) OssProto(mount, data.proto);
  }

  window.OssProto = OssProto;
  window.OssExport = OssExport;
})();
