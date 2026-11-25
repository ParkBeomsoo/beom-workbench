(function () {
  // 화면 설계 프로토타입 안에서 도는 작은 스크립트 — 다른 화면 링크를 보기 화면으로 넘기고, 크기와 번호(①…) 위에 올린 것을 알린다.
  // 화면은 <iframe sandbox="allow-scripts"> 안에서 돈다: 저장소(localStorage·sessionStorage)는 막혀 있고, 화면이 스스로 옮겨 가면
  // 보기 화면(탭·설명)은 모른다. 그래서 이동은 모두 보기 화면에 맡기고(go), 화면 사이 데모 값은 pv.state 로 보기 화면이 이어 준다.
  if (window.__pvFrame) return;
  window.__pvFrame = true;
  var PFX = "pv-state:";  // 데모 값은 창 이름에 둔다 — 화면 <head> 의 도우미 줄(brief.PV_SHIM)과 같은 자리라 섞여도 이어진다
  function sid(h) {
    var s = String(h || "");
    var m = s.match(/(?:^|[\/])S(\d{1,3})(?![0-9])[^\/?#]*\.html?(?:[#?].*)?$/i) || s.match(/^#?S(\d{1,3})(?![0-9])(?:[?#].*)?$/i);
    return m ? "S" + parseInt(m[1], 10) : null;
  }
  function parts(h) {  // ?질의·#조각 — 화면 사이에 넘기는 값이 사라지지 않게 그대로 들고 간다
    var s = String(h || ""), x = s.indexOf("#"), q = s.indexOf("?");
    var hash = x >= 0 ? s.slice(x) : "", search = q >= 0 && (x < 0 || q < x) ? s.slice(q, x >= 0 ? x : s.length) : "";
    return { search: search.length > 1 ? search : "", hash: hash.length > 1 ? hash : "" };
  }
  function read() {
    try {
      var n = String(window.name || "");
      if (n.indexOf(PFX) === 0) { var o = JSON.parse(n.slice(PFX.length)); if (o && typeof o === "object" && !Array.isArray(o)) return o; }
    } catch (e) { /* 깨진 값은 빈 것으로 */ }
    return {};
  }
  function sync() { parent.postMessage({ pv: "state", data: read() }, "*"); }
  function write(o) { try { window.name = PFX + JSON.stringify(o); } catch (e) { return; } sync(); }
  var pv = window.pv = window.pv || {};  // 화면이 <head> 에 둔 도우미 줄 위에 덮는다 (값 자리가 같아 앞서 넣은 값도 남는다)
  pv.state = {
    get: function (k) { var v = read()[k]; return v === undefined ? null : v; },
    set: function (k, v) { var o = read(); if (v === undefined) delete o[k]; else o[k] = v; write(o); return v; },
    all: function () { return read(); },
    clear: function () { write({}); }
  };
  function go(id, href) {
    var p = parts(href);
    sync();  // 도우미 줄로 쓴 값(창 이름에만 있음)도 옮기기 전에 보기 화면에 맡긴다
    parent.postMessage({ pv: "go", id: id, search: p.search, hash: p.hash }, "*");
  }
  pv.go = function (href) { var id = sid(href); if (id) go(id, href); return !!id; };
  var q0 = parts(location.href);
  pv.search = q0.search || (window.__pvNav && window.__pvNav.search) || "";  // 내보낸 파일(srcdoc)에서는 주소에 질의를 못 붙인다
  try { pv.params = new URLSearchParams(pv.search); } catch (e) { pv.params = null; }

  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href],[data-go]");
    if (!a) return;
    var href = a.getAttribute("data-go") || a.getAttribute("href");
    var id = sid(href);
    if (id) { e.preventDefault(); go(id, href); }
    else if (a.tagName === "A" && !/^#/.test(a.getAttribute("href") || "")) e.preventDefault();
  }, true);
  document.addEventListener("submit", function (e) { e.preventDefault(); }, true);
  // 스크립트로 옮기는 것(location.href = …·assign·replace)은 미리 잡을 수 없다 — Qt WebEngine 6.12(Chromium 140)에서 확인:
  //  · location 은 덮어쓸 수 없다 ([LegacyUnforgeable] — assign·replace·href 세터를 바꿔 끼울 수 없음).
  //  · Navigation API 의 navigate 사건은 이 틀(sandbox, 출처 없음) 안에서 일어나지 않는다.
  //  · 앱의 beomproto:(로컬 주소)·내보낸 파일의 file: 로 가는 이동은 엔진이 'Not allowed to load local resource' 로 그냥 막는다
  //    (beforeunload·pagehide 도 없다) — 화면이 그대로 남는다. 그래서 점검(brief.check)이 막고, 스크립트에는 pv.go 를 준다.
  // 이동이 막히지 않는 곳(로컬이 아닌 주소로 띄울 때)의 대비: 새 화면이 열리자마자 '지금 여기'를 알린다 — 보기 화면은 틀을 다시
  // 띄우지 않고 탭·설명만 맞춘다. 떠나기 전에는 데모 값을 맡긴다 (이것도 같은 엔진에서 로컬이 아닌 주소로 확인).
  parent.postMessage({ pv: "at", id: sid(location.pathname), search: location.search, hash: location.hash }, "*");
  addEventListener("pagehide", sync);
  document.addEventListener("mouseover", function (e) {
    var n = e.target.closest && e.target.closest(".n");
    parent.postMessage({ pv: "hover", n: n ? n.textContent.trim() : null }, "*");
  });
  var marked = null;
  addEventListener("message", function (e) {
    var d = e.data || {};
    if (d.pv === "mark" || d.pv === "unmark") {  // 설명 번호에 올리면 화면의 그 자리를 표시 (옮기지 않는다)
      if (marked) { marked.t.style.outline = marked.o; marked = null; }
      if (d.pv === "mark") {
        var all = document.querySelectorAll(".n");
        for (var j = 0; j < all.length; j++) if (all[j].textContent.trim() === d.n) {
          var tt = all[j].parentElement || all[j]; marked = { t: tt, o: tt.style.outline };
          tt.style.outline = "3px solid #e8590c"; tt.style.outlineOffset = "2px"; break;
        }
      }
      return;
    }
    if (d.pv !== "flash") return;
    var ns = document.querySelectorAll(".n");
    for (var i = 0; i < ns.length; i++) {
      if (ns[i].textContent.trim() !== d.n) continue;
      var t = ns[i].parentElement || ns[i], o = t.style.outline;
      t.scrollIntoView({ block: "center" }); t.style.outline = "3px solid #19735f"; t.style.outlineOffset = "2px";
      setTimeout(function () { t.style.outline = o; }, 1400);
      break;
    }
  });
  // 틀은 페이지 높이만큼 커지므로 안에서 스크롤하지 않는다 — 안쪽 스크롤 막대가 생겼다 사라지며 폭이 흔들리지 않게
  document.documentElement.style.overflow = "hidden";
  var last = { w: 0, h: 0 };
  function size() {
    var d = document.documentElement, b = document.body;
    var w = Math.max(d.scrollWidth, b ? b.scrollWidth : 0), h = Math.max(d.scrollHeight, b ? b.scrollHeight : 0);
    if (Math.abs(w - last.w) < 4 && Math.abs(h - last.h) < 4) return;  // 몇 px 차이는 알리지 않는다 (되먹임으로 꿈틀거림)
    last = { w: w, h: h };
    parent.postMessage({ pv: "size", w: w, h: h }, "*");
  }
  addEventListener("load", function () { size(); setTimeout(size, 250); });
  if (window.ResizeObserver) new ResizeObserver(size).observe(document.documentElement);
})();
