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
.pv-tabs button:hover{color:var(--pv-text)}.pv-tabs button.on{color:var(--pv-text);font-weight:620;border-bottom-color:var(--pv-text)}
.pv-tabs button em{font-style:normal;color:var(--pv-muted);font-size:12px;margin-left:5px;font-weight:500}
.pv-strip{display:flex;gap:6px;flex-wrap:wrap}
.pv-strip button{border:1px solid var(--pv-line2);background:var(--pv-panel);color:var(--pv-muted);font:inherit;font-size:12.5px;padding:5px 11px;border-radius:999px;cursor:pointer}
.pv-strip button b{font-weight:650;color:var(--pv-faint);margin-right:5px;font-size:12px}
.pv-strip button:hover{color:var(--pv-text);border-color:var(--pv-muted)}.pv-strip button.on{background:var(--pv-text);color:var(--pv-panel);border-color:var(--pv-text)}
.pv-strip button.on b{color:inherit;opacity:.7}
.pv-stage{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,320px);gap:18px;align-items:start}
.pv.narrow .pv-stage{grid-template-columns:minmax(0,1fr)}
.pv-frame{min-width:0}
.pv-bar{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:12px;color:var(--pv-faint)}
.pv-bar button{border:1px solid var(--pv-line2);background:var(--pv-panel);color:var(--pv-muted);font:inherit;font-size:12px;border-radius:7px;padding:3px 9px;cursor:pointer}
.pv-bar button:disabled{opacity:.4;cursor:default}.pv-bar .pv-hint{margin-left:auto}
.pv-device{position:relative;overflow:hidden;border-radius:12px;background:#fff;box-shadow:0 0 0 1px var(--pv-line2),0 8px 28px rgba(0,0,0,.08);margin:0 auto}
.pv-device iframe{position:absolute;left:0;top:0;border:0;background:#fff}
.pv-notes{position:sticky;top:8px;border:1px solid var(--pv-line);border-radius:12px;background:var(--pv-panel);padding:14px 16px;max-height:calc(100vh - 40px);overflow:auto}
.pv.narrow .pv-notes{position:static;max-height:none}
.pv-notes .pv-id{font-size:12px;font-weight:700;color:var(--pv-acc);letter-spacing:.04em}
.pv-notes h3{margin:2px 0 10px;font-size:16px;letter-spacing:-.01em}
.pv-notes .md{font-size:13px;line-height:1.65}.pv-notes .md h2{display:none}.pv-notes .md table{font-size:12px}
.pv-notes .md [data-n].hl{background:var(--pv-acc-soft);border-radius:6px;box-shadow:0 0 0 3px var(--pv-acc-soft)}
.pv-notes .md [data-n]{cursor:pointer}.pv-notes .md .pv-ni{border-radius:4px}
.pv-notes .md td:first-child{white-space:nowrap}.pv-notes .md th,.pv-notes .md td{word-break:keep-all;overflow-wrap:break-word}
.pv-notes .md ol,.pv-notes .md ul{padding-left:18px}.pv-notes .md li{cursor:default}
.pv-go{margin-top:12px;padding-top:10px;border-top:1px solid var(--pv-line);font-size:12px;color:var(--pv-faint)}
.pv-go div{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.pv-link{border:1px solid var(--pv-line2);background:var(--pv-sunk);color:var(--pv-text);font:inherit;font-size:12px;border-radius:7px;padding:2px 8px;cursor:pointer}
.pv-link:hover{border-color:var(--pv-acc);color:var(--pv-acc)}
span.pv-sref{color:var(--pv-acc);font-weight:600;cursor:pointer;border-bottom:1px dotted currentColor}
.pv-over{max-width:1100px}.pv-over .md>h1:first-child{display:none}  /* 개요 맨 위 제목은 문서 탭과 겹친다 */
.pv-reqs{margin:0 0 12px;padding:10px 12px;border-radius:10px;background:var(--pv-sunk);font-size:12.5px;line-height:1.55}
.pv-reqs b{display:block;font-size:12px;color:var(--pv-muted);margin-bottom:4px}.pv-reqs ul{margin:0;padding:0;list-style:none}
.pv-reqs li{margin:3px 0}.pv-rid{display:inline-block;min-width:30px;font-weight:700;color:var(--pv-acc)}
@media print{.pv{display:none}}`;

  // 앱 안에서는 앱의 화면 언어로 (window.__t), 내보낸 파일에서는 그대로
  const tt = (s) => (typeof window !== "undefined" && window.__t ? window.__t(s) : s);
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = tt(text); return e; }
  function injectStyle() { if (document.getElementById("pv-style")) return; const s = el("style"); s.id = "pv-style"; s.textContent = CSS; document.head.append(s); }
  // 화면 안에 넣는 작은 스크립트(proto-frame.js) — 앱에서는 앱이 화면을 내줄 때 넣고(src), 내보낸 파일에서는 여기서 넣는다(srcdoc)
  function withInject(html, inject, nav) {
    if (!inject) return html;
    const tag = "<script>" + inject + "<\/script>";
    html = /<\/body>/i.test(html) ? html.replace(/<\/body>/i, () => tag + "</body>") : html + tag;
    if (!nav || (!nav.search && !nav.hash)) return html;
    // srcdoc 화면은 주소에 ?질의를 붙일 수 없다 — 화면 스크립트보다 먼저 값을 놓고(pv.search), #조각은 주소에 맞춰 둔다
    const pre = "<script>" + "window.__pvNav=" + JSON.stringify(nav).replace(/</g, "\\u003c") +
      ";if(__pvNav.hash)try{history.replaceState(null,'',location.href.replace(/#.*$/,'')+__pvNav.hash)}catch(e){try{location.replace(location.href.replace(/#.*$/,'')+__pvNav.hash)}catch(e2){}}<\/script>";
    return /<head[^>]*>/i.test(html) ? html.replace(/<head[^>]*>/i, (m) => m + pre) : pre + html;
  }
  // 화면 사이 데모 값(pv.state) — 보기 화면을 연 동안 남긴다. 앱은 문서를 다시 그릴 때 보기를 새로 만들므로 모듈에 둔다(화면 묶음마다)
  const DEMO = new Map();
  const PFX = "pv-state:";
  function cleanNav(d) {  // 화면이 보낸 ?질의·#조각 — 모양이 맞는 것만, 길이는 줄여서
    const s = typeof d.search === "string" && /^\?/.test(d.search) ? d.search.slice(0, 2000) : "";
    const h = typeof d.hash === "string" && /^#/.test(d.hash) ? d.hash.slice(0, 2000) : "";
    return s || h ? { search: s, hash: h } : null;
  }

  function OssProto(root, data) {
    injectStyle();
    const ids = Object.keys(data.screens || {});
    const groups = (data.groups || []).filter((g) => g.ids.some((i) => data.screens[i]));
    const pv = el("div", "pv"); root.replaceChildren(pv);
    const tabs = el("div", "pv-tabs"), body = el("div", "pv-body"); pv.append(tabs, body);
    // hist·fwd 는 {id, nav} — 뒤로 가면 그 화면에 넘겼던 ?질의·#조각도 되살린다
    const state = { tab: groups.length ? -1 : 0, cur: null, nav: null, hist: [], fwd: [] };
    const groupOf = (id) => groups.findIndex((g) => g.ids.includes(id));
    const demoKey = ids.map((x) => (data.screens[x].src || "").replace(/[^/]*$/, "")).find(Boolean) + "|" + ids.join(",");
    if (!DEMO.has(demoKey)) DEMO.set(demoKey, {});
    let frame = null, device = null, notes = null, strip = null, back = null, label = null, stage = null, size = { w: 1280, h: 800 };

    let raf = 0;  // 크기 맞춤은 한 프레임에 한 번 (ResizeObserver 되먹임 경고가 나지 않게)
    const ro = window.ResizeObserver ? new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { pv.classList.toggle("narrow", pv.clientWidth < 760); fit(); }); }) : null;
    if (ro) ro.observe(pv);
    window.addEventListener("message", (e) => {
      if (!frame || e.source !== frame.contentWindow) return;
      const d = e.data || {};
      if (d.pv === "go" && data.screens[d.id]) go(d.id, true, cleanNav(d));
      else if (d.pv === "at" && data.screens[d.id] && d.id !== state.cur) go(d.id, true, cleanNav(d), true);  // 화면이 스스로 옮겨 감
      else if (d.pv === "state" && d.data && typeof d.data === "object" && !Array.isArray(d.data)) {
        try { if (JSON.stringify(d.data).length <= 200000) DEMO.set(demoKey, d.data); } catch (err) { /* 순환 등은 버린다 */ }
      }
      else if (d.pv === "size") { size = { w: Math.max(320, d.w || 1280), h: Math.max(200, d.h || 800) }; fit(); }
      else if (d.pv === "hover" && notes) for (const x of notes.querySelectorAll("[data-n]")) x.classList.toggle("hl", !!d.n && x.dataset.n === d.n);
    });
    let lastK = 0, lastSize = "";
    function fit() {
      if (!frame || !device) return;
      const avail = device.parentElement.clientWidth || size.w;
      let k = Math.min(1, avail / size.w);
      // 비율이 1% 안으로만 바뀌면 그대로 둔다 — 높이가 바뀌어 바깥 스크롤 막대가 생겼다 사라지는 되먹임을 끊는다
      const key = size.w + "x" + size.h;
      if (key === lastSize && lastK && k > lastK && k - lastK < 0.012) k = lastK;  // 줄어드는 것은 따르고, 살짝 커지는 것은 무시
      lastK = k; lastSize = key;
      frame.style.width = size.w + "px"; frame.style.height = size.h + "px"; frame.style.transform = ""; frame.style.zoom = String(k);
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
    // keep: 화면이 이미 그 화면으로 옮겨 가 있다 — 틀을 다시 띄우지 않고(다시 띄우면 화면 속 입력이 사라진다) 탭·칩·설명만 맞춘다
    function go(id, push, nav, keep) {
      if (push && state.cur && (state.cur !== id || nav)) { state.hist.push({ id: state.cur, nav: state.nav }); state.fwd = []; }
      state.cur = id; state.nav = nav || null; const g = groupOf(id); if (g >= 0) state.tab = g; else if (!groups.length) state.tab = 0;
      if (keep && frame && stage && stage.isConnected) redraw(); else draw();
    }
    function listNow() { const g = groups[state.tab]; return (g ? g.ids : ids).filter((x) => data.screens[x]); }
    function makeStrip(list) {
      const s = el("div", "pv-strip");
      for (const x of list) { const b = el("button", x === state.cur ? "on" : ""); b.append(el("b", null, x), document.createTextNode(data.screens[x].name || "")); b.onclick = () => go(x, true); s.append(b); }
      return s;
    }
    function redraw() {  // 틀은 두고 나머지만 — 화면이 스스로 옮겨 간 뒤
      drawTabs();
      const sc = data.screens[state.cur], s2 = makeStrip(listNow());
      strip.replaceWith(s2); strip = s2;
      label.textContent = `${state.cur} ${sc.name || ""}`; back.disabled = !state.hist.length;
      const n2 = makeNotes(sc); notes.replaceWith(n2); notes = n2;
    }
    function draw() {
      drawTabs();
      body.replaceChildren(); frame = device = notes = stage = null;
      if (state.tab === -1) { const o = el("div", "pv-over md"); o.innerHTML = data.overview || ""; linkify(o); body.append(o); return; }
      const list = listNow();
      if (!state.cur || !list.includes(state.cur)) { state.cur = list[0]; state.nav = null; }
      if (!state.cur) { body.append(el("div", "pv-hint", "화면이 없습니다")); return; }
      strip = makeStrip(list);
      const sc = data.screens[state.cur], nav = state.nav || {};
      stage = el("div", "pv-stage"); const wrap = el("div", "pv-frame"), bar = el("div", "pv-bar");
      // '뒤로' 단추는 두지 않는다 — 화면은 위 칩·탭으로 고르고, 화면 안의 버튼으로 옮겨 간다 (화면 이름 앞에 뒤로가 있으면 화면의 일부처럼 보였다)
      back = { disabled: true };
      label = el("span", null, `${state.cur} ${sc.name || ""}`);
      // 화면 이름은 위 칩이 이미 보여 준다 — 여기서 다시 쓰면 제목이 두 번 붙는다 (label 은 이동할 때 값만 갱신)
      bar.append(el("span", "pv-hint", "화면 안 버튼을 눌러 이동해 보세요"));
      device = el("div", "pv-device"); frame = el("iframe"); frame.setAttribute("sandbox", "allow-scripts"); frame.setAttribute("title", sc.name || state.cur);
      // 데모 값은 틀의 이름으로 건넨다 — 화면 스크립트가 읽는 창 이름(window.name)이 되어, 화면이 처음 그릴 때부터 바로 읽힌다
      try { frame.name = PFX + JSON.stringify(DEMO.get(demoKey) || {}); } catch (err) { frame.name = PFX + "{}"; }
      if (sc.src) frame.src = sc.src + (nav.search || "") + (nav.hash || "");
      else frame.srcdoc = withInject(sc.html || "<p style='font:14px sans-serif;padding:24px'>화면 파일이 없습니다</p>", data.inject, state.nav);
      device.append(frame); wrap.append(bar, device);
      notes = makeNotes(sc);
      stage.append(wrap, notes); body.append(strip, stage);
      size = { w: sc.width || 1280, h: sc.height || 800 }; fit();
    }
    function makeNotes(sc) {
      const notes = el("aside", "pv-notes"); notes.append(el("div", "pv-id", state.cur), el("h3", null, sc.name || ""));
      const md = el("div", "md"); md.innerHTML = sc.notes || "<p>설명이 없습니다</p>"; notes.append(md);
      for (const blk of md.querySelectorAll("li,p,td")) {  // 번호(①…)마다 — 한 문단에 여럿이면 번호마다 나눈다
        const marks = blk.textContent.match(/[①-⑳]/g) || [];
        if (marks.length === 1 && /^[①-⑳]/.test(blk.textContent.trim())) { blk.dataset.n = marks[0]; continue; }
        if (marks.length < 2) continue;
        const nodes = [...blk.childNodes]; let cur = null;
        blk.replaceChildren();
        for (const node of nodes) {
          if (node.nodeType !== 3) { (cur || blk).append(node); continue; }
          for (const part of node.nodeValue.split(/(?=[①-⑳])/)) {
            const m = part.match(/^[①-⑳]/);
            if (m) { cur = el("span", "pv-ni"); cur.dataset.n = m[0]; blk.append(cur); }
            (cur || blk).append(document.createTextNode(part));
          }
        }
      }
      for (const x of md.querySelectorAll("[data-n]")) {  // 설명 번호를 누르면 화면의 그 자리로, 올리면 화면에 표시
        const send = (pv) => frame && frame.contentWindow && frame.contentWindow.postMessage({ pv, n: pv === "unmark" ? null : x.dataset.n }, "*");
        x.onclick = (e) => { if (!e.target.closest(".pv-sref")) send("flash"); };
        x.onmouseenter = () => { x.classList.add("hl"); send("mark"); };
        x.onmouseleave = () => { x.classList.remove("hl"); send("unmark"); };
      }
      const outs = (sc.links || []).filter((x) => data.screens[x] && x !== state.cur);
      if (outs.length) {
        const go2 = el("div", "pv-go", "이 화면에서 갈 수 있는 곳"); const row = el("div");
        for (const x of outs) { const b = el("button", "pv-link", `${x} ${data.screens[x].name || ""}`); b.onclick = () => go(x, true); row.append(b); }
        go2.append(row); notes.append(go2);
      }
      linkify(md);
      return notes;
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
