(function () {
  // 화면 설계 프로토타입 안에서 도는 작은 스크립트 — 다른 화면 링크를 보기 화면으로 넘기고, 크기와 번호(①…) 위에 올린 것을 알린다.
  function sid(h) {
    var s = String(h || "");
    var m = s.match(/(?:^|[\/])S(\d{1,3})(?![0-9])[^\/]*\.html?(?:[#?].*)?$/i) || s.match(/^#?S(\d{1,3})$/i);
    return m ? "S" + parseInt(m[1], 10) : null;
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href],[data-go]");
    if (!a) return;
    var id = a.getAttribute("data-go") ? sid(a.getAttribute("data-go")) : sid(a.getAttribute("href"));
    if (id) { e.preventDefault(); parent.postMessage({ pv: "go", id: id }, "*"); }
    else if (a.tagName === "A" && !/^#/.test(a.getAttribute("href") || "")) e.preventDefault();
  }, true);
  document.addEventListener("submit", function (e) { e.preventDefault(); }, true);
  document.addEventListener("mouseover", function (e) {
    var n = e.target.closest && e.target.closest(".n");
    parent.postMessage({ pv: "hover", n: n ? n.textContent.trim() : null }, "*");
  });
  addEventListener("message", function (e) {
    var d = e.data || {};
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
  function size() {
    var d = document.documentElement, b = document.body;
    parent.postMessage({ pv: "size", w: Math.max(d.scrollWidth, b ? b.scrollWidth : 0), h: Math.max(d.scrollHeight, b ? b.scrollHeight : 0) }, "*");
  }
  addEventListener("load", function () { size(); setTimeout(size, 250); });
  if (window.ResizeObserver) new ResizeObserver(size).observe(document.documentElement);
})();
