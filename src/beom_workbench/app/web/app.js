"use strict";
// 화면 상태와 그리기. 데이터는 Qt WebChannel 로 같은 프로세스의 툴킷(Api)에서만 받는다.
let api = null;
const S = { runs: [], agents: [], kinds: {}, policy: {}, current: null, detail: null, tab: "progress", mode: "request",
  draft: { request: "", target_root: "", context_dir: "", source: "", feature: "", agent: "claude", model: "", effort: "", max_rounds: 0, confirm_at: "start",
           auth: "subscription", plan_approval: true, policy: null }, permModes: [], preparing: false,
  changes: null, file: null, sig: { feed: "", right: "", side: "" }, startedAt: null };

const $ = (s) => document.querySelector(s);
// 화면 언어 — 한국어가 원문이고, 다른 언어는 번역표(원문 → 번역)로 바꿔 보여 준다. 번역표에 없는 글(에이전트·사용자가 쓴 글)은 그대로.
// 표의 {0}·{1} 은 바뀌는 부분(숫자·이름) — 그런 항목은 패턴으로 맞춘다.
const I18N = { lang: "ko", map: new Map(), pats: [], cache: new Map() };
const HANGUL = /[가-힣]/;
function setI18n(info) {
  I18N.lang = (info && info.lang) || "ko"; I18N.map = new Map(); I18N.pats = []; I18N.cache = new Map(); I18N.info = info || {};
  document.documentElement.lang = I18N.lang;
  for (const [k, v] of Object.entries((info && info.catalog) || {})) {
    if (!/\{\d\}/.test(k)) { I18N.map.set(k, v); continue; }
    if (!HANGUL.test(k.replace(/\{\d\}/g, ""))) continue;  // 자리표시자뿐인 항목은 아무 글에나 맞으므로 쓰지 않는다
    const rx = k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\\\{(\d)\\\}/g, "((?:(?![.?!]\\s| — )[^\\n])*?)");  // 빈 값도 맞게 — 줄·문장 끝·' — ' 를 넘지 않게 (긴 글은 나눠서)
    I18N.pats.push([new RegExp("^" + rx + "$"), v, k.length]);
  }
  I18N.pats.sort((a, b) => b[2] - a[2]);  // 긴(구체적인) 것부터
}
function t(s) {
  if (I18N.lang === "ko" || typeof s !== "string" || !HANGUL.test(s) || s.length > 700) return s;
  if (I18N.cache.has(s)) return I18N.cache.get(s);
  const core = s.trim();
  let out = s;
  const exact = I18N.map.get(s), hit = exact != null ? null : I18N.map.get(core);
  if (exact != null) out = exact;
  else if (hit != null) out = s.replace(core, hit);
  else {
    let found = false;
    for (const [rx, v] of I18N.pats) {
      const m = core.match(rx);
      if (m) { let r = v; m.slice(1).forEach((g, i) => { r = r.split(`{${i}}`).join(t(g)); }); out = s.replace(core, r); found = true; break; }
    }
    // 번역된 조각을 이어 붙인 글('기획 · 실행 중', 문장 두 개, 설명 두 줄) — 줄 → 문장 → ' — ' → ' · ' 순으로 나눠 조각마다
    // (조각 하나하나는 다시 통째로 먼저 찾으므로, 구분자가 든 항목이 쪼개지지 않는다)
    if (!found) {
      for (const sep of [/(\n)/, /(?<=[.?!)])(\s+)(?=\S)/, /( — )/, /( · )/]) {
        const parts = core.split(sep);
        if (parts.length < 3) continue;
        const joined = parts.map((p, i) => (i % 2 ? p : t(p))).join("");
        if (joined !== core) { out = s.replace(core, joined); break; }
      }
    }
  }
  if (I18N.cache.size > 5000) I18N.cache.clear();
  I18N.cache.set(s, out);
  return out;
}
window.__t = t;  // 화면 설계 보기(proto.js)도 같은 번역을 쓴다
const T_ATTRS = new Set(["title", "placeholder", "aria-label", "alt"]);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = t(v);
    else el.setAttribute(k, v === true ? "" : T_ATTRS.has(k) ? t(v) : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) el.append(k instanceof Node ? k : document.createTextNode(t(String(k))));
  return el;
}
function call(name, ...args) {
  return new Promise((resolve) => api[name](...args, (raw) => {
    const r = JSON.parse(raw);
    if (!r.ok) toast(r.error);
    resolve(r);
  }));
}
let toastTimer;
function toast(text, ms) {
  const el = $("#toast"); el.textContent = t(text); el.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), ms || 3200);
}
const fmtMoney = (v) => v == null ? "—" : "$" + Number(v).toFixed(2);
// 비용을 알려 주지 않는 에이전트(Codex 등)는 토큰 수로 — 사용량이 비어 보이지 않게
const fmtTokens = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);
const usageText = (run) => run.cost_usd ? fmtMoney(run.cost_usd) : run.tokens ? "토큰 " + fmtTokens(run.tokens) : null;
function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "방금"; if (s < 3600) return Math.floor(s / 60) + "분 전";
  if (s < 86400) return Math.floor(s / 3600) + "시간 전"; return Math.floor(s / 86400) + "일 전";
}

// ---------------------------------------------------------------- 공용 조각: 아이콘 · 팝오버 메뉴 · 알약 버튼 · 토글
const ICONS = {
  folder: '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2h8.5A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/>',
  down: '<path d="m7 10 5 5 5-5"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5"/>',
  shield: '<path d="M12 3.5 5 6v5.5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9V6z"/>',
  chip: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
  agent: '<rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4M9 12h.01M15 12h.01"/>',
  play: '<path d="M8 5.5v13l11-6.5z"/>',
  clip: '<path d="m20 11-8.5 8.5a5 5 0 0 1-7-7L13 4a3.3 3.3 0 0 1 4.7 4.7L9.2 17.2a1.7 1.7 0 0 1-2.4-2.4L14.5 7"/>',
  gauge: '<path d="M4.5 16a7.5 7.5 0 1 1 15 0"/><path d="m12 16 3.5-4"/>',
  book: '<path d="M5 4.5h9.5A2.5 2.5 0 0 1 17 7v12.5H7.5A2.5 2.5 0 0 1 5 17z"/><path d="M5 17a2.5 2.5 0 0 1 2.5-2.5H17"/>',
  spark: '<path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4M6 6l2.6 2.6M15.4 15.4 18 18M6 18l2.6-2.6M15.4 8.6 18 6"/>',
  branch: '<circle cx="6.5" cy="6" r="2"/><circle cx="6.5" cy="18" r="2"/><circle cx="17.5" cy="8" r="2"/><path d="M6.5 8v8M17.5 10c0 4-5 3.5-9.6 6.6"/>',
  doc: '<path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 7 20z" transform="translate(-1 0)"/><path d="M13 3.5V8h4M9 12.5h6M9 16h4"/>',
  chat: '<path d="M5 6.5A2.5 2.5 0 0 1 7.5 4h9A2.5 2.5 0 0 1 19 6.5v6a2.5 2.5 0 0 1-2.5 2.5H11l-4 3.5V15h0a2.5 2.5 0 0 1-2-2.5z"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
  alert: '<path d="M12 4 3.5 19h17z"/><path d="M12 10v4M12 16.8v.2"/>',
  list: '<path d="M9 7h10M9 12h10M9 17h10M5 7h.01M5 12h.01M5 17h.01"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  key: '<circle cx="8" cy="15" r="3.5"/><path d="m10.5 12.5 8-8M16 7l2 2M14 9l1.5 1.5"/>',
  download: '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
};
function ico(name, cls) {
  const s = h("span", { class: "ico" + (cls ? " " + cls : "") });
  s.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;
  return s;
}
let openPop = null;
// 화면 언어 적용 — 번역표를 받고, 화면 틀(index.html)의 고정 글자도 바꾼다
function applyLang(info) {
  setI18n(info);
  for (const el of document.querySelectorAll("[data-t]")) { el.dataset.src = el.dataset.src || el.textContent; el.textContent = t(el.dataset.src); }
  for (const el of document.querySelectorAll("[data-tp]")) el.placeholder = t(el.dataset.tp);
  const names = (info && info.languages) || {};
  // 사이드바 아래 줄에 세 단추가 들어가게 — 언어는 코드로 짧게, 이름은 말풍선으로
  const lab = $("#langlabel"); if (lab) { lab.textContent = String(I18N.lang || "ko").toUpperCase(); lab.parentElement.title = names[I18N.lang] || I18N.lang; }
}
function langPop(anchor) {
  const info = I18N.info || {}, names = info.languages || { ko: "한국어" };
  const sys = info.system && names[info.system] ? names[info.system] : null;
  const items = [{ id: "auto", label: t("시스템 언어 따르기"), desc: sys ? sys : t("지원하지 않는 언어면 한국어"), on: (info.choice || "auto") === "auto" },
    ...Object.entries(names).map(([id, label]) => ({ id, label, on: info.choice === id }))];
  popover(anchor, { title: t("화면 언어"), items, width: 240, onPick: async (it) => {
    const r = await call("setLanguage", it.id);
    if (r.ok) { applyLang(r.data); S.sig = { feed: "", right: "", side: "", cbar: "" }; renderSide(); if (S.current) await openRun(S.current); else renderHome(); }
  } });
}
function closePop() { if (openPop) { openPop.remove(); openPop = null; } }
document.addEventListener("mousedown", (e) => { if (openPop && !openPop.contains(e.target) && !e.target.closest(".pill.open")) closePop(); });
// 앵커 위(공간이 없으면 아래)에 뜨는 메뉴. items: [{id, label, desc, note, on}] 또는 content(요소)
function popover(anchor, { title, items, onPick, content, width }) {
  closePop();
  const pop = h("div", { class: "pop", style: width ? `width:${width}px` : null },
    title ? h("div", { class: "pt", text: title }) : null,
    ...(items || []).map((it) => h("button", { class: "pi" + (it.on ? " on" : ""), onclick: () => { closePop(); onPick(it); } },
      h("span", { class: "pl" }, h("b", { text: it.label }), it.note ? h("i", { text: it.note }) : null),
      it.desc ? h("span", { class: "pd", text: it.desc }) : null,
      it.on ? ico("check", "ck") : null)),
    content || null);
  document.body.append(pop);
  const r = anchor.getBoundingClientRect(), pr = pop.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.left, innerWidth - pr.width - 8));
  let top = r.top - pr.height - 8 > 8 ? r.top - pr.height - 8 : r.bottom + 8;
  if (top + pr.height > innerHeight - 8) top = Math.max(8, innerHeight - pr.height - 8);  // 화면 밖으로 나가지 않게
  pop.style.left = left + "px"; pop.style.top = top + "px";
  openPop = pop;
  return pop;
}
function pill(icon, label, value, onclick, cls) {
  const b = h("button", { class: "pill" + (cls ? " " + cls : ""), title: label }, ico(icon), value != null ? h("span", { class: "pv", text: value }) : h("span", { class: "pv", text: label }), ico("down", "dn"));
  b.addEventListener("click", (e) => { e.stopPropagation(); if (openPop && b.classList.contains("open")) { closePop(); return; }
    document.querySelectorAll(".pill.open").forEach((x) => x.classList.remove("open")); b.classList.add("open"); onclick(b);
    const pop = openPop; if (pop) new MutationObserver((_, o) => { if (!pop.isConnected) { b.classList.remove("open"); o.disconnect(); } }).observe(document.body, { childList: true }); });
  return b;
}
function segmented(options, value, onchange) {
  return h("div", { class: "seg" }, ...options.map(([v, t]) => h("button", { class: String(v) === String(value) ? "on" : "", onclick: (e) => { e.stopPropagation(); onchange(v); } }, t)));
}
function toggle(on, onchange) {
  return h("button", { class: "tg" + (on ? " on" : ""), role: "switch", "aria-checked": on ? "true" : "false", onclick: (e) => { e.stopPropagation(); onchange(!on); } }, h("i"));
}
function modeOf(policy) {
  const m = S.permModes.find((x) => Object.entries(x.policy).every(([k, v]) => (policy || {})[k] === v));
  return m ? m.id : "custom";
}
const modeLabel = (policy) => { const id = modeOf(policy); return id === "custom" ? "직접 설정" : S.permModes.find((m) => m.id === id).label; };
// 고르지 않았을 때 실제로 쓰이는 값 — 에이전트 설정 파일, 없으면 최근에 실제로 쓴 모델 (agents.current)
function currentOf(agentId) { const a = S.agents.find((x) => x.id === agentId); return (a && a.current) || {}; }
function modelName(agentId, id) {
  const a = S.agents.find((x) => x.id === agentId), m = a && (a.models || []).find((x) => x.id === id);
  if (m && m.id) return m.label;
  return id ? id.replace(/^claude-/, "").replace(/-(\d+)-(\d+)$/, " $1.$2") : "";
}
function modelLabel(agentId, model) {
  if (model) return modelName(agentId, model);
  const cur = currentOf(agentId);
  return cur.model ? modelName(agentId, cur.model) : "에이전트 설정";
}
// 에이전트 · 모델 · 권한 메뉴 — 홈 초안과 실행 중 설정이 같은 메뉴를 쓴다 (get: 현재 값, set: 바꾸기)
function agentMenu(anchor, cur, set) {
  popover(anchor, { title: "에이전트", width: 300, items: S.agents.filter((a) => a.exe || a.id === "claude").map((a) =>
    ({ id: a.id, label: a.name.replace(" (실험적)", ""), note: a.ready ? "" : (a.exe ? "로그인 필요" : "설치 필요"), desc: a.note || "", on: a.id === cur })),
    onPick: (it) => set(it.id) });
}
function modelMenu(anchor, agentId, cur, set) {
  const a = S.agents.find((x) => x.id === agentId) || { models: [] };
  const custom = h("input", { class: "pin", placeholder: "다른 모델 이름 직접 입력 후 Enter", value: (a.models || []).some((m) => m.id === (cur || "")) ? "" : (cur || ""),
    onkeydown: (e) => { if (e.key === "Enter") { closePop(); set(e.target.value.trim()); } } });
  popover(anchor, { title: `모델 — ${a.name ? a.name.replace(" (실험적)", "") : ""}`, width: 290,
    items: (a.models || [{ id: "", label: "기본값" }]).map((m) => ({ id: m.id, label: m.id ? m.label : "기본 모델", note: m.note || (m.id ? "" : "에이전트 설정"), on: (cur || "") === m.id })),
    onPick: (it) => set(it.id), content: h("div", { class: "pf" }, custom) });
}
function effortLevels(agentId, model) {
  const a = S.agents.find((x) => x.id === agentId); if (!a || !a.efforts) return [];
  return a.efforts[model || ""] || a.efforts[""] || [];
}
function effortLabel(agentId, model, effort) {
  if (!effort) { const cur = currentOf(agentId), lv = cur.effort && effortLevels(agentId, model || cur.model).find((x) => x.id === cur.effort);
    return lv ? `노력 ${lv.label}` : "노력 자동"; }
  const l = effortLevels(agentId, model).find((x) => x.id === effort); return l ? `노력 ${l.label}` : `노력 ${effort}`;
}
function effortMenu(anchor, agentId, model, cur, set) {
  popover(anchor, { title: "노력 — 얼마나 깊이 생각할지", width: 300, items: [
      { id: "", label: "기본", note: "모델 기본값", on: !cur },
      ...effortLevels(agentId, model).map((l) => ({ id: l.id, label: l.label, note: l.id, desc: l.desc, on: cur === l.id }))],
    onPick: (it) => set(it.id) });
}
// 에이전트 · 모델 · 노력을 한 메뉴에서 (Claude 데스크톱의 모델 고르기처럼)
function brainLabel(agentId, model, effort) {
  const cur = currentOf(agentId), eff = effort || cur.effort;
  const lv = effortLevels(agentId, model || cur.model).find((x) => x.id === eff);
  return [agentName(agentId), modelLabel(agentId, model), lv ? lv.label : null].filter(Boolean).join(" · ");
}
function brainPop(anchor, cur, set, allowAgent) {
  // 고를 때마다 같은 창 안에서 내용만 바꾼다. 위 모서리를 고정하고 내용이 바뀌면 아래만 늘고 준다 —
  // 처음 열 때 에이전트마다 내용 높이를 재서 가장 긴 것이 들어갈 자리에 위 모서리를 둔다.
  let pop = null;
  const draw = (probe) => {
    const c = probe || cur();
    const a = S.agents.find((x) => x.id === c.agent) || { models: [] };
    const levels = effortLevels(c.agent, c.model);
    const custom = h("input", { class: "pin", placeholder: "다른 모델 이름 직접 입력 후 Enter",
      value: (a.models || []).some((m) => m.id === (c.model || "")) ? "" : (c.model || ""),
      onkeydown: (e) => { if (e.key === "Enter") { set({ model: e.target.value.trim() }); draw(); } } });
    const body = h("div", { class: "brain" },
      allowAgent ? h("div", { class: "pt", text: "에이전트" }) : null,
      allowAgent ? h("div", { class: "agrid" }, ...S.agents.filter((x) => x.exe || x.id === "claude").map((x) =>
        h("button", { class: "ag" + (x.id === c.agent ? " on" : "") + (x.ready ? "" : " off"), title: x.hint || x.note || "",
          onclick: (e) => { e.stopPropagation(); S.agentPicked = true;
            // 에이전트마다 마지막으로 고른 모델·노력을 기억한다 — 다른 에이전트를 눌렀다 돌아와도 지워지지 않게
            S.agentMem = S.agentMem || {}; if (c.agent) S.agentMem[c.agent] = { model: c.model || "", effort: c.effort || "" };
            const back = S.agentMem[x.id] || (S.defaults && S.defaults.agent === x.id ? { model: S.defaults.model || "", effort: S.defaults.effort || "" } : null);
            set({ agent: x.id, model: back ? back.model : "", effort: back ? back.effort : "" }); draw(); } },
          h("b", { text: x.name.replace(" (실험적)", "").replace(/\s*\(.*\)$/, "") }), h("i", { text: x.ready ? "연결됨" : x.ready === null && x.exe ? "확인 중" : x.exe ? "로그인 필요" : "설치 필요" })))) : null,
      h("div", { class: "pt", text: "모델" }),
      ...(() => {
        const cur = currentOf(c.agent), inUse = c.model || cur.model || "";
        const all = (a.models || []).filter((m) => m.id), older = all.filter((m) => m.older && m.id !== inUse);
        const list = all.filter((m) => !m.older || m.id === inUse || S.showOlder);
        if (inUse && !all.some((m) => m.id === inUse)) list.unshift({ id: inUse, label: modelName(c.agent, inUse) });
        const more = older.length ? h("button", { class: "pmore", onclick: (e) => { e.stopPropagation(); S.showOlder = !S.showOlder; draw(); } },
          S.showOlder ? "이전 모델 접기" : `이전 모델 ${older.length}개 더 보기`) : null;
        return [...list.map((m) => h("button", { class: "pi" + (inUse === m.id ? " on" : ""),
          onclick: (e) => { e.stopPropagation(); const keep = effortLevels(c.agent, m.id).some((l) => l.id === c.effort); set({ model: m.id, ...(keep ? {} : { effort: "" }) }); draw(); } },
          h("span", { class: "pl" }, h("b", { text: m.label }),
            h("i", { text: !c.model && m.id === cur.model ? `현재 · ${cur.source || "설정"}` : (m.note || "") })),
          inUse === m.id ? ico("check", "ck") : null)), more];
      })(),
      h("div", { class: "pf" }, custom),
      levels.length ? h("div", { class: "pt sub2", text: "노력 — 얼마나 깊이 생각할지" }) : null,
      levels.length ? h("div", { class: "effrow" }, segmented(levels.map((l) => [l.id, l.label]), c.effort || currentOf(c.agent).effort || "",
        (v) => { set({ effort: v }); draw(); })) : null,
      levels.length && !c.effort ? h("div", { class: "pnote", text: currentOf(c.agent).effort
        ? "지금은 모델 기본값을 씁니다 — 고르면 그 단계로 고정합니다" : "지금은 에이전트가 정합니다 — 고르면 그 단계로 고정합니다" }) : null,
      levels.length && c.effort ? h("div", { class: "pnote", text: (levels.find((l) => l.id === c.effort) || {}).desc || "" }) : null);
    if (probe) return body;
    if (pop && pop.isConnected) { pop.replaceChildren(body); return pop; }  // 위 모서리는 그대로
    pop = popover(anchor, { width: 340, content: body });
    let tallest = pop.getBoundingClientRect().height;
    if (allowAgent) for (const x of S.agents.filter((y) => (y.exe || y.id === "claude") && y.id !== c.agent)) {
      pop.replaceChildren(draw({ agent: x.id, model: "", effort: "" })); tallest = Math.max(tallest, pop.getBoundingClientRect().height);
    }
    pop.replaceChildren(body);
    const ar = anchor.getBoundingClientRect(), need = Math.min(tallest, innerHeight - 16);
    const top = ar.top - 8 - need >= 8 ? ar.top - 8 - need : Math.max(8, Math.min(ar.bottom + 8, innerHeight - need - 8));
    pop.style.top = top + "px"; pop.style.maxHeight = (innerHeight - top - 8) + "px"; pop.style.overflowY = "auto";
    return pop;
  };
  draw();
}
function permMenu(anchor, policy, set, openDetail) {
  const cur = modeOf(policy);
  popover(anchor, { title: "권한", width: 340, items: [
      ...S.permModes.map((m) => ({ id: m.id, label: m.label, desc: m.desc, on: cur === m.id })),
      { id: "custom", label: "직접 설정…", desc: "작업 종류마다 허용·물어보기·거절을 정합니다", on: cur === "custom" }],
    onPick: (it) => it.id === "custom" ? openDetail() : set({ ...S.permModes.find((m) => m.id === it.id).policy }) });
}
function policyRows(policy, set) {
  return h("div", { class: "prow-list" }, ...Object.entries(S.kinds).map(([k, t]) => h("div", { class: "prow" },
    h("span", { text: t }), segmented([["allow", "허용"], ["ask", "확인"], ["refuse", "거절"]], policy[k], (v) => set({ ...policy, [k]: v })))));
}

// 저장 공간 — 어디에 무엇이 얼마나, 휴지통, 자동 정리
const AREA_KO = { projects: ["원장·원본 클론", "분석·포팅 기록과 원본 저장소 — 핵심 기록"], runs: ["실행 기록", "대화 흐름·질문·체크포인트"],
  trash: ["휴지통", "지운 실행 — 정한 날이 지나면 비움"], cache: ["캐시", "다시 만들 수 있음 — 지워도 됨"], "cbm-cache": ["구조 지도 캐시", "다시 만들 수 있음 — 지워도 됨"],
  work: ["임시 사본", "검증 중에만 쓰고 지움"], engines: ["분석 엔진", "setup 으로 다시 설치 가능"], bin: ["엔진 실행 파일", "setup 으로 다시 설치 가능"],
  node: ["언어 서버", "setup 으로 다시 설치 가능"], backups: ["백업", "스킬을 덮어쓰기 전 보관본"], app: ["앱 아이콘", "바로가기가 가리킴"] };
function fmtBytes(n, atLeast) {
  if (n == null) return "계산 중…";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (atLeast ? "≥ " : "") + (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
}
// 사용량 — 작업마다 남긴 비용·토큰을 모아 본다 (구독 로그인의 비용은 API 가격으로 셈한 값)
async function usagePop(anchor) {
  const r = await call("usageSummary"); if (!r.ok) return;
  const U = r.data;
  const line = (b) => [b.cost_usd ? fmtMoney(b.cost_usd) : null, b.tokens ? "토큰 " + fmtTokens(b.tokens) : null].filter(Boolean).join(" · ") || "—";
  const stat = (label, b) => h("div", { class: "ustat" }, h("span", { text: label }), h("b", { text: line(b) }),
    h("span", { class: "pd", text: `작업 ${b.runs}개 · ${b.rounds}라운드` }));
  const agentRows = Object.entries(U.agents).map(([id, a]) => h("div", { class: "urow" },
    h("span", { text: agentName(id) }), h("span", { class: "uv", text: line(a) }), h("span", { class: "pd", text: `작업 ${a.runs}개` })));
  const top = U.top.map((x) => h("button", { class: "urow link", title: "열기", onclick: () => { closePop(); openRun(x.id); } },
    h("span", { class: "un", text: x.name || "프로젝트" }), h("span", { class: "uv", text: line(x) }), h("span", { class: "pd", text: agentName(x.agent) })));
  const subs = Object.values(U.agents).some((a) => a.subscription && a.cost_usd);
  popover(anchor, { width: 340, content: h("div", { class: "optpop usagepop" },
    h("div", { class: "pt", text: "사용량" }),
    U.total.runs ? h("div", { class: "ustats" }, stat("이번 달", U.this_month), stat("전체", U.total)) : h("div", { class: "pnote", text: "아직 사용 기록이 없습니다" }),
    agentRows.length ? h("div", { class: "pt sub2", text: "에이전트별" }) : null, ...agentRows,
    top.length ? h("div", { class: "pt sub2", text: "많이 쓴 작업" }) : null, ...top,
    h("div", { class: "pnote", text: "에이전트가 보고한 값입니다 — Claude Code 는 비용, Codex 는 토큰. 기간은 작업을 만든 날 기준." +
      (subs ? " 구독 로그인의 비용은 실제 청구액이 아니라 같은 사용량을 API 가격으로 셈한 값입니다." : "") })) });
}
async function dataPop(anchor) {
  const r = await call("dataReport"); if (!r.ok) return;
  S.data = r.data; drawData(anchor);
}
function drawData(anchor) {
  const D = S.data; if (!D) return;
  const prefs = D.settings || {};
  const pop = popover(anchor, { width: 380, content: h("div", { class: "optpop datapop" },
    h("div", { class: "pt", text: "저장 공간 — 모두 이 컴퓨터에만 있습니다" }),
    h("div", { class: "droot" }, h("span", { class: "mono", text: D.root }), h("button", { class: "btn quiet sm", onclick: () => call("openFolder", D.root) }, "폴더 열기")),
    ...D.areas.map((a) => h("div", { class: "darea" }, h("div", {}, h("b", { text: (AREA_KO[a.name] || [a.name])[0] }), h("span", { text: (AREA_KO[a.name] || ["", ""])[1] })),
      h("span", { class: "dsize", text: fmtBytes(a.bytes, a.at_least) }))),
    h("div", { class: "pt sub2", text: `실행 ${D.runs.total}개 · 끝남 ${D.runs.finished} · 숨김 ${D.runs.archived}` }),
    h("div", { class: "orow" }, h("div", {}, h("b", { text: "자동 정리" }), h("span", { text: "끝났거나 숨긴 실행을 이 기간 뒤 휴지통으로 (원장은 남음)" })),
      segmented([["", "끔"], [30, "30일"], [90, "90일"]], prefs.cleanup_days || "", async (v) => {
        const x = await call("setDataPrefs", JSON.stringify({ cleanup_days: v ? +v : null })); if (x.ok) { S.data.settings = x.data; drawData(anchor); } })),
    h("div", { class: "pt sub2", text: `휴지통 ${D.trash.length}개 — ${prefs.trash_days || 7}일 뒤 자동으로 비움` }),
    ...D.trash.slice(0, 6).map((t) => h("div", { class: "darea" }, h("div", {}, h("b", { text: t.title || t.run_id || t.name }), h("span", { text: t.trashed_at ? ago(t.trashed_at) : "" })),
      h("button", { class: "btn quiet sm", onclick: async () => { const x = await call("restoreRun", t.name); if (x.ok) { toast("되살렸습니다"); refresh(); dataPop(anchor); } } }, "되살리기"))),
    h("div", { class: "orow" }, h("div", {}, h("b", { text: "문서 로그인 기록" }), h("span", { text: "링크를 가져올 때 로그인한 사이트의 쿠키 — 지우면 로그아웃됩니다" })),
      h("button", { class: "btn quiet sm", onclick: async () => { if (!confirm("로그인 창의 쿠키·캐시를 지울까요? 다음에 다시 로그인해야 합니다.")) return;
        const x = await call("clearBrowser"); if (x.ok) toast("지웠습니다"); } }, "지우기")),
    (D.orphans || []).length ? h("div", { class: "pt sub2", text: `어느 실행도 쓰지 않는 원장 ${D.orphans.length}개 — ${fmtBytes(D.orphans.reduce((a, o) => a + o.bytes, 0))}` }) : null,
    ...(D.orphans || []).slice(0, 6).map((o) => h("div", { class: "darea" }, h("div", {}, h("b", { text: o.id }), h("span", { text: `${fmtBytes(o.bytes)} · ${ago(o.at)}` })),
      h("button", { class: "btn quiet sm", title: "분석·승인 기록과 구조 지도 캐시를 지웁니다 — 에이전트가 직접 분석한 원장이면 다시 분석해야 합니다", onclick: async () => {
        if (!confirm(`원장 '${o.id}' 를 지울까요? 되돌릴 수 없습니다.`)) return; const x = await call("deleteProject", o.id); if (x.ok) { toast("지웠습니다"); dataPop(anchor); } } }, "지우기"))),
    D.trash.length ? h("div", { class: "droot" }, h("span"), h("button", { class: "btn sm", onclick: async () => {
      if (!confirm("휴지통을 비울까요? 되돌릴 수 없습니다.")) return; const x = await call("emptyTrash"); if (x.ok) { toast("비웠습니다"); dataPop(anchor); } } }, "휴지통 비우기")) : null) });
  return pop;
}

// 실행 하나의 메뉴 — 이름 바꾸기, 목록에서 숨기기
function runMenu(anchor, r) {
  const name = h("input", { class: "pin", value: r.name || "", onkeydown: async (e) => {
    if (e.key === "Enter") { const x = await call("renameRun", r.id, e.target.value); if (x.ok) { closePop(); refresh(); } } } });
  popover(anchor, { width: 260, content: h("div", { class: "optpop" }, h("div", { class: "pt", text: "이름 바꾸기 (Enter)" }), h("div", { class: "pf", style: "border:none;margin:0" }, name),
    h("button", { class: "pi", onclick: () => { closePop(); retryRun(r.id); } },
      h("span", { class: "pl" }, h("b", { text: "같은 요청으로 새 작업" })), h("span", { class: "pd", text: "에이전트·모델을 바꿔 다시 해 보고 결과를 비교합니다 — 저장소면 새 작업 브랜치에서" })),
    h("button", { class: "pi", onclick: async () => { closePop(); const x = await call("archiveRun", r.id, !r.archived); if (x.ok) { if (!r.archived && S.current === r.id) goHome(); refresh(); } } },
      h("span", { class: "pl" }, h("b", { text: r.archived ? "다시 보이기" : "목록에서 숨기기" })), h("span", { class: "pd", text: r.archived ? "" : "기록은 그대로 남고 '숨긴 항목'에서 다시 볼 수 있습니다" })),
    h("button", { class: "pi danger", onclick: async () => { closePop();
        if (!confirm(`'${r.name || "프로젝트"}' 실행을 휴지통으로 옮길까요?

대화 흐름과 체크포인트가 함께 옮겨지고, 승인·검증 원장은 프로젝트에 남습니다. 저장 공간에서 되살릴 수 있습니다.`)) return;
        const x = await call("trashRun", r.id); if (x.ok) { toast("휴지통으로 옮겼습니다"); if (S.current === r.id) goHome(); refresh(); } } },
      h("span", { class: "pl" }, h("b", { text: "휴지통으로" })), h("span", { class: "pd", text: "저장 공간에서 되살리거나 비울 수 있습니다" }))) });
  setTimeout(() => name.focus(), 0);
}

// 같은 요청으로 새 작업 — 처음 입력으로 새 작업 화면을 채우고, 에이전트·모델은 사용자가 바꿔서 시작한다
async function retryRun(id) {
  const x = await call("retryDraft", id); if (!x.ok) return;
  const v = x.data;
  goHome();
  S.mode = v.mode; S.agentPicked = true;
  Object.assign(S.draft, { name: v.name || "", request: v.request, attachments: v.attachments, target_root: v.target_root, context_dir: v.context_dir,
    source: v.source, feature: v.feature, agent: v.agent || S.draft.agent, model: v.model || "", effort: v.effort || "",
    policy: v.policy || S.draft.policy, max_rounds: v.max_rounds || 0, confirm_at: v.confirm_at || "start", auth: v.auth || S.draft.auth,
    plan_approval: v.plan_approval !== false });
  checkLinks(v.attachments.filter((a) => /^https?:\/\//.test(a)));
  renderHome();
  toast((v.kept_folder ? "같은 저장소의 새 작업 브랜치에서 합니다. " : "새 폴더에 만듭니다 (앞의 결과 위에서 시작하지 않게). ") +
    "에이전트·모델을 바꾼 뒤 시작하세요" + (v.missing ? ` — 찾을 수 없는 첨부 ${v.missing}개는 뺐습니다` : ""), 7000);
}

// ---------------------------------------------------------------- 사이드바
function renderSide() {
  const sig = JSON.stringify([S.runs, S.current, S.agents, S.showHidden, S.sandbox, S.engines, S.runQ]);
  if (sig === S.sig.side) return; S.sig.side = sig;
  const box = $("#runs"); box.replaceChildren();
  // 작업이 많아지면 찾기 — 이름·모드·상태·폴더에서 (숨긴 항목도 함께 찾는다)
  const qbox = $("#runq"); if (qbox) qbox.hidden = S.runs.length < 6 && !S.runQ;
  const q = (S.runQ || "").trim().toLowerCase();
  const hit = (r) => !q || [r.name, r.mode_text, r.status, r.target_root].some((v) => String(v || "").toLowerCase().includes(q) || t(String(v || "")).toLowerCase().includes(q));
  const all = S.runs.filter(hit);
  const shown = all.filter((r) => !r.archived || q), hidden = q ? [] : all.filter((r) => r.archived);
  const active = shown.filter((r) => r.status !== "완료"), done = shown.filter((r) => r.status === "완료");
  const item = (r) => {
    const more = h("button", { class: "more", title: "이름 바꾸기 · 숨기기", onclick: (e) => { e.stopPropagation(); runMenu(e.currentTarget, r); } }, "⋯");
    const el = h("div", { class: "run" + (r.id === S.current ? " on" : ""), onclick: () => openRun(r.id),
      oncontextmenu: (e) => { e.preventDefault(); runMenu(more, r); } },
      h("span", { class: "t", text: r.name || "프로젝트" }), h("span", { class: "dot tone-" + r.tone }),
      h("span", { class: "m", text: `${r.mode_text} · ${r.status}` }), more);
    return el;
  };
  if (active.length) box.append(h("div", { class: "group", text: "진행 중" }), ...active.map(item));
  if (done.length) box.append(h("div", { class: "group", text: "완료" }), ...done.map(item));
  if (hidden.length) box.append(h("div", { class: "group toggle", onclick: () => { S.showHidden = !S.showHidden; S.sig.side = ""; renderSide(); } },
    `숨긴 항목 ${hidden.length} ${S.showHidden ? "▾" : "▸"}`), ...(S.showHidden ? hidden.map(item) : []));
  if (!S.runs.length) box.append(h("div", { class: "group", text: "프로젝트" }), h("div", { class: "empty", text: "아직 없습니다" }));
  else if (q && !all.length) box.append(h("div", { class: "empty", text: "찾는 작업이 없습니다" }));
  const ag = $("#agents"); ag.replaceChildren();
  const missing = S.agents.filter((a) => !a.exe && a.install);
  const sb = S.sandbox || {}, sbMissing = !sb.ready && sb.install && !missing.some((a) => a.install === sb.install) ? 1 : 0;
  const en = S.engines || { ready: true }, enMissing = !en.ready && en.install ? 1 : 0;
  if (missing.length + sbMissing + enMissing > 1) ag.append(h("button", { class: "agent-setup", title: [...missing.map((a) => a.install), sbMissing ? sb.install : ""].filter(Boolean).join("\n"),
    onclick: () => call("installAgents").then((r) => r.ok && r.data.opened && toast(`터미널에서 ${r.data.agents.join(", ")} 설치가 끝나면 창으로 돌아오세요 — 자동으로 다시 확인합니다`, 7000)) },
    sbMissing || enMissing ? `처음 설정 ${missing.length + sbMissing + enMissing}개 한 번에 설치` : `없는 에이전트 ${missing.length}개 한 번에 설치`));
  for (const a of S.agents) {
    ag.append(h("div", { class: "agent" + (a.ready ? " ok" : ""), title: [a.hint, a.note, a.detail].filter(Boolean).join("\n") }, h("i"),
      h("span", { class: "an", text: a.name.replace(" (실험적)", "").replace(" (Antigravity CLI)", "") }),
      h("span", { class: "as", text: a.ready ? (a.install ? "연결됨 · 다른 Node" : "연결됨") : a.ready === null && a.exe ? "확인 중" : a.exe ? "로그인 필요" : "설치 필요" }),
      !a.ready && a.login ? h("button", { onclick: () => call("loginAgent", a.id).then((r) => r.ok && toast("터미널에서 로그인을 마쳐 주세요")) }, "로그인") : null,
      !a.ready && !a.exe && a.install ? h("button", { title: a.install, onclick: () => call("installAgent", a.id).then((r) => r.ok && toast("터미널에서 설치가 끝나면 창으로 돌아오세요 — 자동으로 다시 확인합니다", 6000)) }, "설치") : null));
  }
  // 도구 — 에이전트 말고 툴킷이 쓰는 것(격리 실행·분석 엔진)을 한 줄로. 없는 것이 있으면 [설치] 한 번, 누르면 항목별로
  const tools = toolList(sb, en);
  const need = tools.filter((x) => !x.ready), canInstall = need.filter((x) => x.install);
  ag.append(h("div", { class: "agent toolsrow" + (need.length ? "" : " ok"), title: t("눌러서 항목별로 보기"), onclick: (e) => toolsPop(e.currentTarget, tools) }, h("i"),
    h("span", { class: "an", text: "도구" }),
    h("span", { class: "as", text: need.length ? t("{0}개 설치 필요").replace("{0}", need.length) : t("준비됨") }),
    canInstall.length ? h("button", { onclick: (e) => { e.stopPropagation(); canInstall.forEach((x) => x.install()); } }, "설치") : null));
}
function toolList(sb, en) {
  const done = (r) => r.ok && toast("터미널에서 설치가 끝나면 창으로 돌아오세요 — 자동으로 다시 확인합니다", 6000);
  const out = [{ name: "격리 실행", ready: !!sb.ready, what: "에이전트가 실행하는 명령을 운영체제 샌드박스 안에서 돌립니다 — 대상 폴더·임시 폴더·도구 캐시 밖에는 쓰지 못합니다",
    note: [sb.how, sb.note].filter(Boolean).join(" "), cmd: sb.install, install: !sb.ready && sb.install ? () => call("installSandbox").then(done) : null }];
  if (S.engines) out.push({ name: "분석 엔진", ready: !!en.ready, what: "레퍼런스 모드에서 원본 저장소를 분석해 옮길 범위를 찾습니다",
    note: en.ready ? "" : en.note || "", cmd: en.install, install: !en.ready && en.install ? () => call("installEngines").then(done) : null });
  return out;
}
function toolsPop(anchor, tools) {
  popover(anchor, { width: 340, content: h("div", { class: "toolpop" },
    h("div", { class: "pt", text: "도구" }),
    ...tools.map((x) => h("div", { class: "orow" },
      h("div", {}, h("b", {}, h("i", { class: "tdot" + (x.ready ? " ok" : "") }), x.name), h("span", { text: x.what }),
        x.note ? h("span", { class: "tnote", text: x.note }) : null),
      x.ready ? h("span", { class: "tstat", text: "준비됨" })
        : x.install ? h("button", { class: "btn sm", title: x.cmd || "", onclick: (e) => { e.stopPropagation(); x.install(); closePop(); } }, "설치")
          : h("span", { class: "tstat", text: "설치 필요" })))) });
}

// ---------------------------------------------------------------- 홈
const EXAMPLES = [
  "주문 수량을 받아 총액을 계산하는 API. 개당 2,000원, 10개 이상이면 10% 할인, 수량이 0 이하면 400으로 거절.",
  "상품 목록을 CSV 로 내려받는 엔드포인트. 카테고리·가격 범위로 거르고, 1만 건이 넘으면 나눠서.",
  "검색어 자동완성: 입력 앞부분으로 인기 검색어 10개, 초성 검색 지원.",
];
// 처음 한 번: 시작 메뉴(검색)·바탕화면에서 앱을 열 수 있게
function shortcutBanner() {
  const sc = S.shortcut;
  if (!sc || !sc.supported || sc.dismissed || (sc.installed && sc.installed.start)) return null;
  const add = async (where) => { const r = await call("installShortcut", where); if (r.ok) { toast("바로가기를 만들었습니다 — 시작 메뉴 검색에서 beom 으로 찾을 수 있어요"); S.shortcut = (await call("shortcutStatus")).data; renderHome(); } };
  return h("div", { class: "banner" }, ico("book"),
    h("div", {}, h("b", { text: "검색이나 바탕화면에서 바로 열 수 있게 할까요?" }), h("span", { text: "시작 메뉴와 바탕화면에 바로가기를 만듭니다. 언제든 지울 수 있습니다." })),
    h("button", { class: "btn quiet sm", onclick: async () => { await call("dismissShortcut"); S.shortcut.dismissed = true; renderHome(); } }, "닫기"),
    h("button", { class: "btn sm", onclick: () => add("start") }, "시작 메뉴만"),
    h("button", { class: "btn primary sm", onclick: () => add("start,desktop") }, "둘 다 만들기"));
}
// 첨부 — 파일(docx·pptx·xlsx·hwpx 는 글로 바꿔 함께 줌)이나 링크(공개 페이지는 글로 저장)
function checkLinks(items) {
  S.links = S.links || {};
  for (const u of items) if (/^https?:\/\//.test(u) && !S.links[u]) { S.links[u] = { state: "checking" }; call("checkLink", u); }
}
function loginLink(u) {
  call("loginLink", u).then((r) => r.ok && toast("열린 창에서 로그인한 뒤, 문서가 보이면 '이 페이지 가져오기'를 눌러 주세요", 7000));
}
function attachPop(anchor, d) {
  const add = (items) => { d.attachments = [...new Set([...(d.attachments || []), ...items])]; checkLinks(items); closePop(); renderHome(); };
  const link = h("input", { class: "pin", placeholder: "https://… 문서·페이지 주소 붙여넣고 Enter",
    onkeydown: (e) => { if (e.key === "Enter" && /^https?:\/\//.test(e.target.value.trim())) add([e.target.value.trim()]); } });
  popover(anchor, { width: 340, content: h("div", { class: "optpop" },
    h("div", { class: "pt", text: "기획서·문서 첨부" }),
    h("button", { class: "pi", onclick: async () => { const r = await call("pickFiles"); if (!r.ok) return;
        if (r.data.rejected.length) toast(r.data.rejected.join(" / "), 7000);
        if (r.data.files.length) add(r.data.files); } },
      h("span", { class: "pl" }, h("b", { text: "파일 선택" })), h("span", { class: "pd", text: "md · txt · pdf · docx · pptx · xlsx · hwpx · 이미지 — 오피스·한글 문서는 글로 바꿔 함께 넘깁니다. 한글 .hwp 는 HWPX 로 저장해 주세요" })),
    h("div", { class: "pt sub2", text: "링크" }),
    h("div", { class: "pf", style: "border:none;margin:0;padding-top:0" }, link),
    h("div", { class: "pnote", text: "공개 페이지는 내용을 저장해 넘깁니다. 로그인이 필요한 페이지(컨플루언스·지라 등)는 앱 안의 브라우저 창에서 로그인을 요청하고, 로그인한 뒤 보이는 내용을 가져옵니다." })) });
  setTimeout(() => link.focus(), 0);
}
const PLAN_EXAMPLES = [
  "동네 카페 단골 적립 앱. 사장님은 태블릿으로 적립, 손님은 폰 번호로 확인.",
  "사내 회의실 예약 웹. 겹치면 막고, 슬랙으로 알림.",
  "쇼핑몰 관리자에서 상품 일괄 수정 화면. 엑셀 업로드·미리보기·되돌리기.",
];
function renderHome() {
  document.getElementById("app").classList.add("home");
  const m = $("#main"); m.replaceChildren();
  const d = S.draft, req = S.mode === "request";
  const PH = { request: "만들 기능을 적어 주세요 — 무엇을 받아 무엇을 돌려주는지, 예외는 어떻게 할지까지 적을수록 정확해집니다. 기획서를 첨부해도 됩니다.",
    reference: "가져올 기능과 바꿀 점을 적어 주세요 — 예) 이 기능을 참고해 우리 서비스 구조에 맞게, 우리 검색 엔진 기준으로",
    plan: "기획할 기능·서비스를 적어 주세요 — 누가 쓰는지, 왜 필요한지, 꼭 지킬 규칙. 기존 기획서·정책 문서·참고 링크를 붙이면 그 위에서 이어 갑니다.",
    free: "에이전트에게 바로 시킬 일을 적어 주세요 — 우리 절차 없이 그대로 전달합니다." };
  const ta = h("textarea", { id: "homeText", placeholder: PH[S.mode] || PH.request, oninput: (e) => { d.request = e.target.value; } });
  ta.value = d.request;
  ta.addEventListener("input", () => { const b = document.querySelector(".homebox .send"); if (b && req) b.disabled = S.preparing || (!d.request.trim() && !(d.attachments || []).length); });
  // 폴더 칸: 이름표는 줄바꿈 없이, 경로는 끝의 두 단계만 (전체 경로는 마우스를 올리면)
  const folder = (key, label, hint) => h("span", { class: "field" + (d[key] ? "" : " unset"), title: d[key] || hint,
      onclick: async () => { const r = await call("pickFolder"); if (r.ok && r.data) { d[key] = r.data; if (key === "target_root") { d.git = await gitOf(r.data); d.needs = await needsOf(r.data); } renderHome(); } } },
    h("span", { class: "fold" }), h("span", { class: "fl", text: label }), h("b", { text: d[key] ? shortPath(d[key]) : hint }));
  const common = commonFolder(d.target_root);
  d.policy = d.policy || { ...S.policy };
  const mode = (id, icon, title, body, flow) => h("button", { class: "mode" + (S.mode === id ? " on" : ""), onclick: () => { S.mode = id; renderHome(); } },
    h("span", { class: "mi" }, ico(icon)), h("b", { text: title }), h("span", { class: "md2", text: body }), h("span", { class: "flow", text: flow }));
  const srcFields = S.mode !== "reference" ? null : h("div", { class: "srcrow" },
    h("label", { class: "sf" }, h("span", { text: "원본" }), h("input", { placeholder: "참고할 저장소 — github.com/조직/저장소 또는 로컬 경로 (오픈소스·사내)", value: d.source,
      oninput: (e) => { d.source = e.target.value; } })),
    h("label", { class: "sf" }, h("span", { text: "기능" }), h("input", { placeholder: "(선택) 비워 두면 기능 목록을 보여 주고 고르게 합니다", value: d.feature,
      oninput: (e) => { d.feature = e.target.value; } })));
  const brainBtn = pill("agent", "에이전트 · 모델 · 노력", brainLabel(d.agent, d.model, d.effort), (b) => brainPop(b, () => d,
    (patch) => { Object.assign(d, patch); const bb = document.querySelector(".toolbar .pill.brain .pv"); if (bb) bb.textContent = brainLabel(d.agent, d.model, d.effort); }, true), "brain");
  const linkTag = (f) => { const st = (S.links || {})[f]; if (!st) return null;
    if (st.state === "checking") return h("span", { class: "attst", text: "확인 중" });
    if (st.state === "login") return h("button", { class: "attlogin", title: "앱 안의 브라우저에서 로그인하고 내용을 가져옵니다", onclick: () => loginLink(f) }, "로그인 필요 · 로그인");
    if (st.state === "error") return h("span", { class: "attst warn", title: st.error || "", text: "못 읽음" });
    return null; };
  const atts = (d.attachments || []).length ? h("div", { class: "atts" }, ...(d.attachments || []).map((f) => h("span", { class: "att", title: f },
    ico(/^https?:/.test(f) ? "book" : "clip"), /^https?:/.test(f) ? f.replace(/^https?:\/\//, "").slice(0, 40) : f.split(/[\\/]/).pop(), linkTag(f), h("button", { title: "빼기", onclick: () => { d.attachments = d.attachments.filter((x) => x !== f); renderHome(); } }, "×")))) : null;
  const fields = h("div", { class: "toolbar" },
    h("div", { class: "tl" },
      h("button", { class: "iconbtn", title: "기획서·문서·링크 첨부", onclick: (e) => { e.stopPropagation(); attachPop(e.currentTarget, d); } }, ico("clip")),
      folder("target_root", { request: "만들 곳", reference: "옮길 곳", plan: "저장할 곳", free: "작업 폴더" }[S.mode] || "폴더",
        "안 고르면 새 폴더"),
      req ? folder("context_dir", "구조 참고", "저장소 (선택)") : null,
      pill("shield", "권한", modeLabel(d.policy), (b) => permMenu(b, d.policy, (p) => { d.policy = p; renderHome(); }, () => optionsPop(b, true))),
      h("button", { class: "iconbtn", title: "실행 옵션 (라운드·계획 승인·인증·세부 권한)", onclick: (e) => { e.stopPropagation(); optionsPop(e.currentTarget, false); } }, ico("sliders"))),
    h("div", { class: "tr" }, brainBtn,
      h("button", { class: "send", disabled: S.preparing || !d.request.trim() && S.mode !== "reference" && !(d.attachments || []).length, title: "시작 (Ctrl+Enter)", onclick: create },
        S.preparing ? h("span", { class: "spin" }) : ico("up"))));
  const wrap = h("div", { class: "homewrap" }, h("div", { class: "homein" },
    shortcutBanner(),
    h("h1", { text: { request: "무엇을 만들까요?", reference: "어떤 기능을 가져올까요?", plan: "무엇을 기획할까요?", free: "무엇을 할까요?" }[S.mode] || "무엇을 만들까요?" }),
    h("p", { class: "lede", text: { plan: "꼭 필요한 결정만 묻고, 정책·화면·테스트 시나리오가 서로 맞물리는 기획 패키지를 만듭니다.",
      free: "우리 절차 없이 에이전트에게 그대로 맡깁니다." }[S.mode] || "기준을 먼저 합의하고, 그 기준을 채울 때까지 에이전트가 만들고 검증합니다." }),
    h("div", { class: "modes four" },
      mode("request", "spark", "요청사항으로 만들기", "기준과 계획을 처음에 한 번 확인하면, 그 기준대로 될 때까지 만들고 검증합니다.",
           "요구사항 → 합격 기준 → 계획 → 구현 → 검증"),
      mode("reference", "branch", "레퍼런스로 만들기", "오픈소스·사내 저장소의 기능을 참고해 내 프로젝트에 맞게 옮기거나 새로 만들고, 기준대로 되는지 확인합니다.",
           "분석 → 범위 → 기준 → 구현 → 비교"),
      mode("plan", "doc", "기획하기", "기능·서비스를 개발·디자인·QA 에 넘길 기획 패키지로 — 정책 결정표, 화면 와이어프레임, 테스트 시나리오까지.",
           "파악 → 기획서 → 정책 → 화면 → 시나리오 → 점검"),
      mode("free", "chat", "자유 대화", "우리 절차 없이 Claude Code·Codex 를 그대로 — 폴더에서 대화하며 작업합니다.",
           "메시지 → 작업 → 메시지 …")),
    h("div", { class: "homebox" }, h("div", { class: "box" }, srcFields, ta, atts, fields)),
    !d.target_root && S.mode !== "plan" ? h("div", { class: "fnote" }, ico("folder"), h("span", { text: "폴더를 안 고르면 문서 폴더 아래 새 폴더에 새로 만듭니다 — 기존 코드에 넣으려면 그 저장소나, 저장소 여럿이 든 상위 폴더를 고르세요 (요청에 맞는 저장소를 찾아 작업합니다)" })) : null,
    d.git && d.target_root ? h("div", { class: "fnote git" }, ico("branch"), h("span", { text: d.new_branch === false
        ? (d.git.workspace ? `저장소 ${d.git.repos}개가 든 폴더입니다 — 각 저장소의 지금 브랜치에서 그대로 작업합니다`
          : `git 저장소입니다 — 지금 브랜치(${d.git.branch || "브랜치 아님"})에서 그대로 작업합니다`)
        : d.git.workspace
          ? `저장소 ${d.git.repos}개가 든 폴더입니다 (${(d.git.names || []).join(", ")}${d.git.repos > (d.git.names || []).length ? " …" : ""}) — 에이전트가 고친 저장소마다 feature/작업명 브랜치를 만들어 그 안에서만 작업하고 라운드마다 커밋합니다. 손대지 않은 저장소는 그대로입니다.`
          : `git 저장소입니다 — ${d.git.base || d.git.branch || "지금 커밋"} 에서 깨끗한 feature/작업명 브랜치를 만들어 그 안에서만 작업하고 라운드마다 커밋합니다. 지금 브랜치(${d.git.branch || "없음"})는 그대로입니다.`
            + (d.git.on_develop || d.git.mixed ? ` 지금 ${d.git.on_develop ? "develop" : `브랜치에 develop 커밋 ${d.git.mixed}개가 섞여`} 있어도 새 브랜치에는 develop 이 들어가지 않습니다.` : "")
            + (d.git.dirty ? ((d.git.on_develop || d.git.mixed)
              ? ` 커밋하지 않은 변경 ${d.git.dirty}개는 실험이라 가져가지 않고 stash 에 보관합니다 (나중에 그 브랜치에서 git stash pop).`
              : ` 커밋하지 않은 변경 ${d.git.dirty}개는 새 브랜치의 첫 커밋으로 따로 담깁니다.`) : "") }),
      h("label", { class: "fnt" }, h("span", { text: "새 브랜치에서 작업" }), toggle(d.new_branch !== false, (v) => { d.new_branch = v; renderHome(); }))) : null,
    (d.needs || []).length && d.target_root ? h("div", { class: "fnote needs" }, ico("alert"),
      h("div", { class: "nl" }, h("span", { text: "빌드에 필요한데 이 PC 에 없는 것 — 시작 전에 설치하면 작업 중간에 막히지 않습니다" }),
        ...d.needs.map((n) => h("div", { class: "ni" }, h("b", { text: n.name }), h("span", { text: ` ${n.why} · ${n.repos.join(", ")}` }),
          n.install ? h("button", { class: "btn sm", title: n.install, onclick: async () => { const r = await call("installTool", n.id);
            if (r.ok) toast("터미널에서 설치가 끝나면 폴더를 다시 골라 확인하세요", 6000); } }, "설치")
            : h("span", { class: "mono", text: n.hint || "" }))))) : null,
    common && S.mode !== "free" ? h("div", { class: "fnote" }, ico("folder"), h("span", { text: `${common}에는 바로 만들지 않고, 그 안에 작업 이름으로 새 폴더를 만들어 그 안에 만듭니다 — 개인 파일과 섞이지 않게` })) : null,
    h("div", { class: "homehint" }, h("span", { text: `${agentName(d.agent)} · ${modelLabel(d.agent, d.model)}${d.effort ? " · " + effortLabel(d.agent, d.model, d.effort) : ""} · 권한 ${modeLabel(d.policy)} · ${+d.max_rounds ? `최대 ${d.max_rounds}회` : "끝날 때까지"} · ${(d.confirm_at || "start") === "start" ? "처음에 한 번 확인" : "마지막에 한 번 확인"}` }),
      h("span", { class: "k", text: "Ctrl Enter 시작" })),
    (req || S.mode === "plan") && !d.request ? h("div", { class: "examples" }, ...(S.mode === "plan" ? PLAN_EXAMPLES : EXAMPLES).map((x) => h("div", { class: "ex", onclick: () => { d.request = x; renderHome(); } }, x))) : null,
    S.runs.length ? h("div", { class: "recent" }, h("h4", { text: "최근 작업" }),
      h("div", { class: "rtable" }, ...S.runs.slice(0, 6).map((r) => h("div", { class: "rrow", onclick: () => openRun(r.id) },
        h("span", { class: "dot tone-" + r.tone }), h("span", { class: "t", text: r.name }), h("span", { class: "m", text: r.mode_text }),
        h("span", { class: "m", text: r.status }), h("span", { class: "m", text: ago(r.created_at) }))))) : null));
  m.append(wrap);
  $("#right").replaceChildren();
  setTimeout(() => ta.focus(), 0);
}
function wbTitle(wb) {
  const rs = wb.repos || [];
  if (wb.workspace) return rs.length ? "작업 브랜치를 만든 저장소 — " + rs.map((r) => `${r.name} (${r.base || "지금 커밋"}에서)`).join(", ")
    : "여러 저장소 작업 공간 — 에이전트가 고친 저장소마다 이 이름의 브랜치를 만듭니다 (아직 없음)";
  const r = rs[0] || {};
  return `작업 브랜치 — ${r.base || "지금 커밋"} 에서 만들었습니다. 원래 브랜치는 그대로 두고 이 브랜치에서만 작업합니다.` +
    (r.stashed ? ` 원래 브랜치에서 하던 변경은 stash 에 보관했습니다 ("${r.stashed}").` : "") + " 시험은 develop 에, 적용은 main 에 합칩니다";
}
// 합치기 — 시험은 작업 브랜치를 develop 에, 적용은 작업 브랜치를 main 에. 사용자가 누를 때만, 작업 폴더는 그대로 (core/workbranch.merge)
async function mergeMenu(anchor, run, running) {
  const r = await call("branchStatus", run.id);
  const repos = (r.ok && r.data.repos) || [];
  const any = (k) => repos.some((x) => x[k]);
  const dirtyMix = repos.filter((x) => x.contaminated);
  const empty = repos.length && repos.every((x) => !x.ahead);  // 작업 브랜치에 아직 커밋이 없다 — 합칠 것이 없다
  const rows = repos.map((x) => h("div", { class: "mrow" }, h("b", { text: x.name }),
    !x.ahead ? h("span", { class: "mst", text: "아직 커밋 없음" }) : null,
    x.ahead ? h("span", { class: "mst " + (x.in_develop ? "on" : ""), text: x.develop ? (x.in_develop ? `${x.develop} 에 있음` : `${x.develop} 에 없음`) : "develop 없음" }) : null,
    x.ahead ? h("span", { class: "mst " + (x.in_main ? "on" : ""), text: x.main ? (x.in_main ? `${x.main} 에 있음` : `${x.main} 보다 커밋 ${x.ahead}개 앞섬`) : "main 없음" }) : null,
    x.contaminated ? h("span", { class: "mst warn", text: `develop 커밋 ${x.contaminated}개 섞임` }) : null));
  const go = async (into) => {
    closePop();
    const what = into === "develop" ? "작업 브랜치를 develop 에 합쳐 시험할까요?" : "작업 브랜치를 main 에 합쳐 적용할까요?";
    if (!confirm(`${what}

${run.work_branch.branch} → ${into}
체크아웃된 작업 폴더는 그대로 두고 ${into} 브랜치만 앞으로 옮깁니다. 원격(push)에는 올리지 않습니다.`)) return;
    const x = await call("mergeRun", run.id, into);
    if (x.ok) toast(x.data.ok ? "합쳤습니다 — " + x.data.text : x.data.text, 6000);
    refresh();
  };
  const item = (into, label, desc, off) => h("button", { class: "pi", disabled: !!off, title: off || "", onclick: () => go(into) },
    h("span", { class: "pl" }, h("b", { text: label })), h("span", { class: "pd", text: off || desc }));
  popover(anchor, { width: 340, content: h("div", { class: "optpop" },
    h("div", { class: "pt", text: `${run.work_branch.branch} — 합치기` }),
    repos.length ? h("div", { class: "mrows" }, ...rows) : h("div", { class: "pd", style: "padding:6px 12px", text: r.ok ? "작업 브랜치가 아직 없습니다" : (r.error || "") }),
    item("develop", "develop 에 합쳐 시험", "실험 공간에서 다른 작업과 함께 돌려 봅니다",
      running ? "진행 중에는 합치지 않습니다" : empty ? "작업 브랜치에 아직 커밋이 없습니다" : !any("develop") ? "develop 브랜치가 없습니다" : repos.every((x) => x.in_develop) ? "이미 develop 에 들어 있습니다" : ""),
    item("main", "main 에 적용", "검증을 마친 작업을 main 에 넣습니다",
      running ? "진행 중에는 합치지 않습니다" : empty ? "작업 브랜치에 아직 커밋이 없습니다" : dirtyMix.length ? "develop 커밋이 섞여 main 에 합칠 수 없습니다" : repos.every((x) => x.in_main) ? "이미 main 에 들어 있습니다" : ""),
    h("div", { class: "fnote", style: "padding:6px 12px 10px", text: "develop 은 main 이나 작업 브랜치에 합치지 않습니다. 충돌하면 아무것도 바꾸지 않고 충돌한 파일을 알려 줍니다." })) });
}
// 고른 폴더가 git 저장소인지 — 저장소면 새 브랜치를 만들어 그 안에서만 작업한다 (core/workbranch)
async function gitOf(p) { const r = await call("folderGit", p); return r.ok ? r.data : null; }
// 고른 폴더를 빌드하는 데 필요한데 없는 도구 — 작업 중간에 막히지 않게 시작 전에 보여 주고 설치 단추를 둔다
async function needsOf(p) { const r = await call("folderNeeds", p); return r.ok ? r.data : []; }
function shortPath(p) { const parts = String(p).split(/[\\/]/).filter(Boolean); return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : parts.join("/") || p; }
// 바탕 화면·문서·다운로드·홈·드라이브 맨 위 — 실행기는 이런 곳을 고르면 그 안에 새 폴더를 만든다 (core/runner.work_folder)
function commonFolder(p) {
  if (!p) return "";
  const parts = String(p).split(/[\\/]/).filter(Boolean), last = (parts[parts.length - 1] || "").toLowerCase();
  if (parts.length <= 1) return "드라이브 맨 위";
  if (["desktop", "바탕 화면", "바탕화면"].includes(last)) return "바탕 화면";
  if (["documents", "문서"].includes(last)) return "문서 폴더";
  if (["downloads", "다운로드"].includes(last)) return "다운로드 폴더";
  if (parts.length === 3 && /^users$/i.test(parts[1])) return "사용자 폴더";
  if (last.startsWith("onedrive") && parts.length === 4) return "OneDrive 폴더";
  return "";
}
// 실행 옵션 — 홈에서는 초안(S.draft)을, 실행 화면에서는 그 실행을 바꾼다
function optionsPop(anchor, focusPerm, run) {
  const d = run ? { max_rounds: run.max_rounds, plan_approval: run.plan_approval ?? S.detail.plan_approval, confirm_at: run.confirm_at ?? S.detail.confirm_at,
    sandbox: run.sandbox ?? (S.detail.run && S.detail.run.sandbox),
    policy: { ...(run.policy || S.detail.policy) }, auth: run.auth } : S.draft;
  const save = (patch) => {
    if (run) call("setRunOptions", run.id, JSON.stringify(patch)).then(async (r) => {
      if (!r.ok) return;
      const next = { ...run, ...r.data };
      await refresh();
      optionsPop(document.querySelector("#cbar .iconbtn") || anchor, focusPerm, next);
    });
    else { Object.assign(S.draft, patch); renderHome(); optionsPop(document.querySelector(".iconbtn") || anchor, focusPerm); }
  };
  const agent = S.agents.find((a) => a.id === (run ? run.agent : d.agent));
  const pop = popover(anchor, { width: 380, content: h("div", { class: "optpop" },
    h("div", { class: "pt", text: "실행 옵션" }),
    h("div", { class: "orow" }, h("div", {}, h("b", { text: "반복 횟수" }), h("span", { text: +d.max_rounds
        ? "에이전트를 이 횟수까지 다시 부릅니다" : "완료 조건을 채울 때까지 반복 — 진전이 없으면 스스로 멈춥니다" })),
      h("div", { style: "display:flex;gap:6px;align-items:center" },
        toggle(+d.max_rounds > 0, (on) => save({ max_rounds: on ? 20 : 0 })),
        +d.max_rounds > 0 ? h("div", { class: "step" }, h("button", { onclick: (e) => { e.stopPropagation(); save({ max_rounds: Math.max(1, +d.max_rounds - 5) }); } }, "−"),
          h("span", { text: String(d.max_rounds) }), h("button", { onclick: (e) => { e.stopPropagation(); save({ max_rounds: Math.min(500, +d.max_rounds + 5) }); } }, "+")) : null)),
    h("div", { class: "orow" }, h("div", {}, h("b", { text: "사람 확인" }), h("span", { text: (d.confirm_at || "start") === "start"
        ? "처음에 기준과 계획을 한 번에 확인하면 끝날 때까지 맡깁니다" : "멈추지 않고 만들고 검증한 뒤, 마지막에 한 번 확인합니다" })),
      segmented([["start", "처음에 한 번"], ["end", "마지막에 한 번"]], d.confirm_at || "start", (v) => save({ confirm_at: v }))),
    (d.confirm_at || "start") === "start" ? h("div", { class: "orow" }, h("div", {}, h("b", { text: "계획도 함께 확인" }), h("span", { text: "기준과 함께 구현 계획도 처음에 확인" })),
      toggle(d.plan_approval === true || d.plan_approval === "true", (v) => save({ plan_approval: v }))) : null,
    h("div", { class: "orow" }, h("div", {}, h("b", { text: "격리 실행" }),
      h("span", { text: (S.sandbox || {}).ready ? "에이전트가 실행하는 명령을 운영체제 샌드박스 안에서 — 대상 폴더·임시 폴더·도구 캐시 밖에는 쓰지 못합니다"
        : "이 PC 에 아직 구성되지 않았습니다 — '구성'을 누르면 설치합니다" })),
      (S.sandbox || {}).ready ? toggle(d.sandbox !== false, (v) => save({ sandbox: v }))
        : h("button", { class: "btn sm", onclick: () => call("installSandbox").then((r) => r.ok && toast("터미널에서 설치가 끝나면 창으로 돌아오세요", 6000)) }, "구성")),
    roundRows(() => optionsPop(anchor, focusPerm, run)),
    run ? null : h("div", { class: "orow" }, h("div", {}, h("b", { text: "요금" }), h("span", { text: (d.auth || "subscription") === "subscription"
        ? "구독 요금제(Claude Pro·Max, ChatGPT Plus·Pro)로 로그인한 그대로 — 요금제 한도 안에서 추가 비용 없음"
        : "개발자 콘솔에서 발급한 API 키(환경 변수) — 쓴 만큼 따로 청구. 할 수 있는 일은 같습니다" })),
      segmented([["subscription", "구독 로그인"], ["api_key", "API 키 (사용량 청구)"]], d.auth || "subscription", (v) => save({ auth: v }))),
    h("div", { class: "pt sub2", text: "권한 — 작업 종류별" }),
    agent && agent.note ? h("div", { class: "pnote", text: agent.note }) : null,
    policyRows(d.policy, (p) => save({ policy: p })),
    ...ruleRows(run, (rules) => optionsPop(document.querySelector("#cbar .iconbtn") || anchor, focusPerm, { ...run, cmd_rules: rules }))) });
  if (focusPerm) pop.querySelector(".prow-list").scrollIntoView({ block: "nearest" });
}
// '이 명령은 항상'으로 기억한 명령 — 이 작업에서만 쓰이고, 지우면 다음부터 다시 묻는다
function ruleRows(run, rerender) {
  const rules = (run && run.cmd_rules) || [];
  if (!rules.length) return [];
  return [h("div", { class: "pt sub2", text: "이 작업에서 기억한 명령" }),
    h("div", { class: "pnote", text: "이 꼴로 시작하는 한 줄 명령은 묻지 않고 허용합니다. 지우면 다음부터 다시 묻습니다" }),
    ...rules.map((rule) => h("div", { class: "orow" }, h("code", { class: "rulecode", text: rule + " …" }),
      h("button", { class: "btn sm", onclick: async (e) => {
        e.stopPropagation();
        const r = await call("forgetRule", run.id, rule);
        if (r.ok) { await refresh(); rerender(r.data.cmd_rules); }
      } }, "지우기")))];
}
// 라운드 규칙 — 모든 작업에 쓰이고, 도는 중인 작업도 다음 판단부터 따른다 (core/rounds.py)
function roundRows(rerender) {
  const R = S.rounds;
  if (!R) { call("roundRules").then((r) => { if (r.ok) { S.rounds = r.data; rerender(); } }); return null; }
  const v = R.values;
  const set = async (patch) => { const r = await call("setRoundRules", JSON.stringify(patch)); if (r.ok) { S.rounds = r.data; rerender(); } };
  const mins = (m) => m >= 60 && m % 60 === 0 ? `${m / 60}시간` : `${m}분`;
  const opts = (list, cur, fmt) => { const o = list.map((x) => [x, fmt(x)]); if (!list.includes(cur)) o.push([cur, fmt(cur)]); return o; };
  const seg = (key, list, fmt) => segmented(opts(list, v[key], fmt), v[key], (x) => set({ [key]: typeof v[key] === "number" ? +x : x }));
  const row = (title, desc, ctl) => h("div", { class: "orow" }, h("div", {}, h("b", { text: title }), h("span", { text: desc })), ctl);
  const changed = Object.keys(v).some((k) => v[k] !== R.defaults[k]);
  return h("div", { class: "rrules" },
    h("div", { class: "pt sub2 rrh" }, h("span", { text: "라운드 규칙 — 모든 작업" }),
      changed ? h("button", { class: "btn quiet sm", onclick: (e) => { e.stopPropagation(); set(Object.fromEntries(Object.keys(v).map((k) => [k, null]))); } }, "기본값으로") : null),
    row("진전 없으면 멈춤", "기록·계획·대상 폴더가 그대로인 라운드가 이어질 때", seg("stall_rounds", [2, 3, 5], (x) => `${x}회`)),
    row("안전 상한", "'끝날 때까지'여도 이 라운드에서 멈춤 — 메시지를 보내면 이어 감", seg("hard_rounds", [20, 50, 100], (x) => `${x}`)),
    row("비용 상한", "작업 하나가 이만큼 쓰면 멈춤 — 비용을 알려 주는 Claude Code 만 (구독은 API 환산 금액)",
      seg("cost_budget_usd", [0, 10, 30], (x) => (+x ? `$${x}` : "없음"))),
    row("무응답 라운드 끝내기", "에이전트가 이 시간 동안 아무 출력이 없을 때", seg("round_idle_min", [10, 30, 60], mins)),
    row("라운드 시간 상한", "출력은 나오는데 끝나지 않을 때 (사람 답을 기다린 시간은 빼고)", seg("round_max_min", [60, 120, 240], mins)),
    row("사용 한도에 걸리면", v.on_limit === "wait" ? `풀릴 때까지 기다렸다 같은 라운드를 다시 — 최대 ${mins(v.limit_wait_max_min)}` : "바로 멈추고 알림 — 이어서 진행은 직접",
      seg("on_limit", ["wait", "stop"], (x) => (x === "wait" ? "기다림" : "멈춤"))),
    v.on_limit === "wait" ? row("최대 대기", "이보다 늦게 풀리면 멈추고 알림", seg("limit_wait_max_min", [60, 360, 1440], mins)) : null,
    row("일시 장애 재시도", "과부하·네트워크 오류 — 30초부터 두 배씩 늘려 가며", seg("transient_retries", [0, 3, 5], (x) => (x ? `${x}회` : "안 함"))),
    row("창 밖 알림", "창이 뒤에 있을 때 — 같은 작업은 창을 다시 볼 때까지 한 번만, 그 사이엔 작업 표시줄만 깜빡임",
      seg("notify", ["all", "important", "off"], (x) => ({ all: "모두", important: "끝남·실패만", off: "끔" })[x])));
}
async function create() {
  const d = S.draft;
  // 로그인이 필요한 링크가 남았으면 먼저 로그인을 요청한다 (빼면 그냥 진행)
  const needLogin = (d.attachments || []).filter((f) => ((S.links || {})[f] || {}).state === "login");
  if (needLogin.length) { toast("로그인이 필요한 링크가 있습니다 — 열린 창에서 로그인하고 가져오거나, 링크를 빼고 시작하세요", 8000); loginLink(needLogin[0]); return; }
  const payload = { mode: S.mode, request: d.request, target_root: d.target_root, context_dir: d.context_dir, source: d.source,
    feature: d.feature, agent: d.agent, model: d.model, effort: d.effort, confirm_at: d.confirm_at, attachments: d.attachments || [], max_rounds: d.max_rounds, auth: d.auth,
    plan_approval: d.plan_approval === true || d.plan_approval === "true", policy: d.policy || S.policy, new_branch: d.new_branch !== false,
    sandbox: d.sandbox !== false,
    name: d.name || (d.request || d.feature || "새 프로젝트").split(/[.\n]/)[0].slice(0, 28) };  // 같은 요청으로 새 작업이면 원래 이름
  S.preparing = true; renderHome();
  const r = await call("createRun", JSON.stringify(payload));
  if (!r.ok) { S.preparing = false; renderHome(); }
}

// ---------------------------------------------------------------- 프로젝트
async function openRun(id) {
  S.current = id; S.detail = null; S.changes = null; S.file = null; S.msgAtts = []; closePop(); S.edit = null; S.sig = { feed: "", right: "", side: "", cbar: "" };
  const msg = $("#msg"); if (msg) { msg.value = ""; msg.style.height = "auto"; }
  document.getElementById("app").classList.remove("home");
  await refresh();
}
function renderRun() {
  const D = S.detail; if (!D) return;
  const run = D.run, m = $("#main");
  if (!m.querySelector(".feed")) {
    m.replaceChildren(
      h("div", { class: "head", id: "head" }), h("div", { class: "meter", id: "meter" }), h("div", { id: "activity" }),
      h("div", { class: "feed", id: "feed" }, h("div", { class: "col", id: "col" })),
      h("div", { class: "composer" }, h("div", { class: "box" },
        h("div", { class: "atts msgatts", id: "msgatts" }),
        h("textarea", { id: "msg", rows: 1, placeholder: "에이전트에게 지시하거나 고칠 점을 적어 주세요 — 스크린샷은 붙여 넣거나 끌어다 놓아도 됩니다",
          oninput: (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(180, e.target.scrollHeight) + "px"; },
          onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); } },
          onkeyup: () => updateSendMode() }),
        h("div", { class: "bar", id: "cbar" }))));
    drawMsgAtts();  // 불러오는 동안 끌어다 놓은 첨부
  }
  $("#head").replaceChildren(...[
    h("span", { class: "crumb", text: D.mode_text + " /" }), h("h1", { text: run.name || "프로젝트" }),
    h("span", { class: "state " + D.tone }, h("i"), D.status),
    h("span", { class: "sp" }), h("span", { id: "procs" }),
    h("button", { class: "path", title: run.target_root + " — 폴더 열기", onclick: () => call("openFolder", run.target_root) }, ico("folder"), h("span", { text: run.target_root.split(/[\\/]/).slice(-2).join("/") })),
    run.work_branch ? h("button", { class: "path wb", title: wbTitle(run.work_branch) + " — 눌러서 합치기", onclick: (e) => mergeMenu(e.currentTarget, run, D.running) }, ico("branch"),
      h("span", { text: run.work_branch.branch + (run.work_branch.workspace ? ` · 저장소 ${(run.work_branch.repos || []).length}` : "") }), ico("down", "dn")) : null,
  ].filter(Boolean));
  $("#meter").replaceChildren(h("div", { class: "stepper" }, ...D.stages.map((s, i) => h("div", { class: "st " + (i < D.stage ? "done" : i === D.stage ? "now" : "") },
    h("i", {}, i < D.stage ? ico("check") : String(i + 1)), h("span", { text: s })))));
  const act = $("#activity"); act.replaceChildren();
  // 내 차례면 할 일을 다 하고 사용자를 기다리는 것 — 도는 것이 없으니 '진행 중'·작업 시간·멈추기를 보이지 않는다 (카드가 할 일을 말한다)
  if (isBusy()) {
    if (!S.startedAt) S.startedAt = Date.now();
    const sec = Math.floor((Date.now() - S.startedAt) / 1000);
    // 에이전트가 긴 글·파일을 쓰는 중이면 얼마나 썼는지 (조각 출력을 주는 에이전트만)
    const st = run.streaming, fresh = st && Date.now() - new Date(st.at).getTime() < 20000;
    const writing = fresh ? (st.thinking ? "생각하는 중" : `${st.label ? st.label + " — " : ""}쓰는 중 · ${st.chars.toLocaleString()}자`) : "";
    act.append(h("div", { class: "activity" }, h("span", { class: "pulse" }),
      (D.activity && D.activity.text) || `라운드 ${run.round} 진행 중`,
      writing ? h("span", { class: "wr", text: writing }) : null,
      h("span", { class: "el", text: `${agentName(run.agent)} · ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}` })));
  } else S.startedAt = null;
  const cbarSig = JSON.stringify([run.agent, run.model, run.effort, run.round, run.max_rounds, run.cost_usd, run.tokens, D.policy, D.running, D.tone, run.status, run.accepted]);
  if (cbarSig !== S.sig.cbar) {  // 메뉴가 열린 채로 1.5초마다 다시 그려지지 않게
    S.sig.cbar = cbarSig;
    const opt = (patch) => call("setRunOptions", run.id, JSON.stringify(patch)).then((r) => r.ok && (toast("바꿨습니다 — 다음 라운드부터 적용"), refresh()));
    const cur = { agent: run.agent, model: run.model || "", effort: run.effort || "" };
    const resumable = !D.running && !(run.status === "done" && run.accepted);
    $("#cbar").replaceChildren(
      h("div", { class: "tl" },
        h("button", { class: "iconbtn", title: "파일 첨부 — 스크린샷·문서 (붙여 넣기·끌어다 놓기도 됩니다)", onclick: async () => { const r = await call("pickFiles"); if (!r.ok) return;
          if (r.data.rejected.length) toast(r.data.rejected.join(" / "), 7000);
          if (r.data.files.length) addAtts(r.data.files); } }, ico("clip")),
        pill("shield", "권한", modeLabel(D.policy), (b) => permMenu(b, D.policy, (p) => opt({ policy: p }), () => optionsPop(b, true, run))),
        h("button", { class: "iconbtn", title: "실행 옵션 (라운드·계획 승인·세부 권한)", onclick: (e) => { e.stopPropagation(); optionsPop(e.currentTarget, false, { ...run, policy: D.policy, plan_approval: D.plan_approval }); } }, ico("sliders")),
        h("span", { class: "meta", text: `${run.round || 0}회째${run.max_rounds ? ` / ${run.max_rounds}` : ""}` + (usageText(run) ? " · " + usageText(run) : "") })),
      h("div", { class: "tr" },
        resumable && run.round ? h("button", { class: "btn quiet sm", title: "메시지 없이 이어서 진행", onclick: startRun }, ico("play"), "이어서") : null,
        pill("agent", "모델 · 노력 (다음 라운드부터)", brainLabel(run.agent, run.model, run.effort), (b) => brainPop(b, () => cur,
          (patch) => { Object.assign(cur, patch); opt(patch); }, false), "brain"),
        // Claude 데스크톱처럼: 진행 중이고 입력이 비었으면 멈춤, 아니면 보내기
        h("button", { class: "send" + (isBusy() ? " stopmode" : ""),
          title: isBusy() ? "멈추기 (Esc) — 입력하면 보내기로 바뀝니다" : D.running ? "고칠 점을 보내기 — 반영해 다시 확인을 요청합니다" : (run.round ? "보내기 — 보내면 바로 이어서 진행" : "시작"),
          onclick: () => { if (hasMsg()) sendMsg(); else if (isBusy()) call("stop", run.id).then(refresh); else if (!D.running) startRun(); } },
          ico(isBusy() ? "stop" : "up"))));
    updateSendMode();
  }
  drawProcs();
  renderFeed();
  renderRight();
}
// 백그라운드 — 이 작업이 띄운 프로세스(에이전트·개발 서버·남은 앱). 끝없이 도는 것이 있으면 여기서 보고 끈다
function drawProcs() {
  const box = $("#procs"); if (!box) return;
  const list = (S.procs && S.procs.run === S.current ? S.procs.rows : []) || [];
  const extra = list.filter((p) => p.kind !== "agent");
  box.replaceChildren();
  // 내 차례면 에이전트는 멈춰 기다린다 — '에이전트 실행 중'을 띄우지 않는다 (남은 개발 서버 같은 백그라운드는 그대로 보인다)
  if (!extra.length && (!list.length || !isBusy())) return;
  const agent = list.find((p) => p.kind === "agent");
  box.append(h("button", { class: "procchip" + (extra.length ? " has" : ""), title: "이 작업이 띄운 프로세스 — 눌러서 보고 끄기",
    onclick: (e) => { e.stopPropagation(); procPop(e.currentTarget); } },
    h("span", { class: "pd" }), extra.length ? `백그라운드 ${extra.length}` : agent ? "에이전트 실행 중" : ""));
}
function procPop(anchor) {
  const list = (S.procs && S.procs.rows) || [];
  const kill = async (p) => {
    const r = await call("killProc", S.current, p.pid);
    if (r.ok) { toast(p.kind === "agent" ? "멈추는 중입니다" : `${p.name} 를 껐습니다`); S.procsAt = 0; refresh(); closePop(); }
  };
  const label = { agent: "에이전트", child: "에이전트가 띄움", left: "남아서 도는 중" };
  const killAll = list.filter((p) => p.kind !== "agent");
  popover(anchor, { width: 420, content: h("div", { class: "optpop procpop" },
    h("div", { class: "pt", text: "백그라운드 — 이 작업이 띄운 프로세스" }),
    ...list.map((p) => h("div", { class: "prc" },
      h("div", { class: "pm" }, h("b", { text: p.name }), h("span", { class: "pk " + p.kind, text: label[p.kind] }), h("span", { class: "pa", text: `${ago(p.since)} · ${p.mem_mb}MB` })),
      h("div", { class: "pc", text: p.cmd, title: p.cmd }),
      h("button", { class: "btn sm" + (p.kind === "agent" ? "" : " quiet"), onclick: () => kill(p) }, p.kind === "agent" ? "멈추기" : "끄기"))),
    killAll.length > 1 ? h("div", { class: "droot" }, h("span", { class: "pnote", text: "개발 서버·감시 명령처럼 끝나지 않는 것은 여기서 끄면 됩니다" }),
      h("button", { class: "btn sm", onclick: async () => { for (const p of killAll) await call("killProc", S.current, p.pid); S.procsAt = 0; refresh(); closePop(); toast("모두 껐습니다"); } }, "모두 끄기")) : null) });
}
function updateSendMode() {
  const b = document.querySelector(".composer .send"); if (!b || !S.detail) return;
  const typed = hasMsg(), busy = isBusy();
  b.classList.toggle("stopmode", busy && !typed);
  b.replaceChildren(ico(busy && !typed ? "stop" : "up"));
  const r = S.detail.run;
  // 내 차례(승인·답 대기)에 빈 입력은 보낼 것도 멈출 것도 없다 — 카드로 답한다
  b.disabled = !typed && ((S.detail.running && !busy) || (!S.detail.running && r.status === "done" && r.accepted));
}
// 쓰던 에이전트가 연결돼 있지 않으면 연결된 것으로 (Claude 고정이 아니라)
function pickReadyAgent() {
  const d = S.draft, cur = S.agents.find((a) => a.id === d.agent);
  if (S.agentPicked || (cur && cur.ready)) return;
  const ready = S.agents.find((a) => a.ready);
  if (ready && ready.id !== d.agent) { d.agent = ready.id; d.model = ""; d.effort = ""; }
}
function agentName(id) { const a = S.agents.find((x) => x.id === id); return a ? a.name.replace(" (실험적)", "") : (id || "Claude Code"); }
async function startRun() {
  if (hasMsg()) await sendMsg();
  const r = await call("start", S.current); if (r.ok) { S.startedAt = Date.now(); refresh(); }
}
// 보낼 메시지의 첨부 (실행 화면) — 고칠 점을 보여 주는 스크린샷·문서
function hasMsg() { const t = $("#msg"); return !!((t && t.value.trim()) || (S.msgAtts || []).length); }
function drawMsgAtts() {
  const box = $("#msgatts"); if (!box) return;
  box.replaceChildren(...(S.msgAtts || []).map((f) => h("span", { class: "att", title: f }, ico("clip"), f.split(/[\\/]/).pop(),
    h("button", { title: "빼기", onclick: () => { S.msgAtts = S.msgAtts.filter((x) => x !== f); drawMsgAtts(); updateSendMode(); } }, "×"))));
}
function addAtts(files) {
  if (!files.length) return;
  // 실행 화면이면(불러오는 중이어도) 보낼 메시지에 — 새 작업 화면으로 바뀌지 않게
  if (S.current) { S.msgAtts = [...new Set([...(S.msgAtts || []), ...files])]; drawMsgAtts(); if (S.detail) updateSendMode(); return; }
  const d = S.draft; d.attachments = [...new Set([...(d.attachments || []), ...files])]; renderHome();
}
// 끌어다 놓은 파일 — 파일 선택과 같은 규칙으로 걸러서 지금 보이는 입력창에
async function addFiles(paths) {
  const r = await call("checkFiles", JSON.stringify(paths)); if (!r.ok) return;
  if (r.data.rejected.length) toast(r.data.rejected.join(" / "), 7000);
  addAtts(r.data.files);
}
// 붙여 넣은 그림(클립보드 스크린샷) — 파일로 저장해 첨부로
document.addEventListener("paste", (e) => {
  // 요청·메시지 입력창에서만, 글이 함께 없을 때만 — 엑셀·워드에서 복사하면 글과 그림이 함께 오는데 그때는 글을 붙인다
  if (!e.target || !["homeText", "msg"].includes(e.target.id)) return;
  const cd = e.clipboardData; if (!cd || (cd.getData("text/plain") || "").trim()) return;
  const items = [...(cd.items || [])].filter((it) => it.kind === "file" && /^image\/(png|jpe?g|gif|webp)$/.test(it.type));
  if (!items.length) return;
  e.preventDefault();
  for (const it of items) {
    const blob = it.getAsFile(); if (!blob) continue;
    const rd = new FileReader();
    rd.onload = async () => { const r = await call("pasteImage", String(rd.result), it.type.split("/")[1]); if (r.ok) addAtts([r.data.path]); };
    rd.readAsDataURL(blob);
  }
});
async function sendMsg() {
  const t = $("#msg"); const text = t.value.trim(), atts = S.msgAtts || [], id = S.current; if ((!text && !atts.length) || S.sending) return;
  S.sending = true;  // 두 번 눌러 두 번 보내지 않게
  let r;
  try { r = await call("sendMessage", id, text, JSON.stringify(atts)); } finally { S.sending = false; }
  if (id !== S.current) return;  // 보내는 사이 다른 실행으로 옮겼다 — 그 실행의 입력·첨부는 건드리지 않는다
  if (r.ok) { S.msgAtts = []; drawMsgAtts(); if ((r.data.notes || []).length) toast(r.data.notes.join(" / "), 8000); }
  if (r.ok && !S.detail.running && !S.edit) {  // 멈췄거나 끝난 실행에 보내면 대화처럼 바로 이어서 진행
    t.value = ""; t.style.height = "auto";
    const st = await call("start", S.current); if (st.ok) { S.startedAt = Date.now(); toast("보냈습니다 — 이어서 진행합니다"); refresh(); }
    return;
  }
  if (r.ok) { t.value = ""; t.style.height = "auto"; updateSendMode(); toast(S.detail.running ? "다음 라운드에 전달합니다" : "보냈습니다. 진행을 누르면 반영합니다"); refresh(); }
}

function renderFeed() {
  const D = S.detail;
  if (S.edit && S.edit.drawn) return;  // 편집 중에는 다시 그리지 않는다 (입력 중인 내용이 날아가지 않게)
  const sig = JSON.stringify([D.feed, D.reviews, D.asks, D.plan, D.handoff, D.run.status, D.run.accepted, D.running, S.edit && S.edit.kind]);
  if (sig === S.sig.feed) return; S.sig.feed = sig;
  if (S.edit) S.edit.drawn = true;
  const col = $("#col"), feed = $("#feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  col.replaceChildren();
  for (const it of D.feed) col.append(feedItem(it));
  const cs = cards();
  for (const c of cs) col.append(row(null, "decision", c));
  if (!D.feed.length && !D.running) col.append(h("div", { class: "empty", text: "시작하면 에이전트의 작업이 여기에 시간순으로 쌓입니다.\n결정이 필요할 때만 멈추고 묻습니다." }));
  if (atBottom || S.sig.first !== D.run.id) { feed.scrollTop = feed.scrollHeight; S.sig.first = D.run.id; }
}
function hhmm(iso) {
  if (!iso) return "";
  const d = new Date(iso); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
// 에이전트 메시지 — `코드` 와 줄바꿈만 살린다 (HTML 은 그대로 글자로)
function richText(text) {
  const el = h("div", { class: "msg" });
  String(text || "").split(/(`[^`\n]+`)/).forEach((part) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) el.append(h("code", { text: part.slice(1, -1) }));
    else el.append(document.createTextNode(part));
  });
  return el;
}
function row(at, node, content) {
  return h("div", { class: "row" }, h("time", { text: hhmm(at) }), h("span", { class: "node " + (node || "") }), h("div", { class: "body" }, content));
}
function feedItem(it) {
  switch (it.type) {
    case "request": return row(it.at, "message", h("div", { class: "req" }, h("div", { class: "who", text: S.detail.run.mode === "reference" ? "가져올 기능" : "요청" }),
      h("div", { class: "body", text: it.text })));
    case "user": return row(it.at, "user", h("div", { class: "user" }, h("div", { class: "who", text: "추가 지시" }), it.text));
    case "divider": return h("div", { class: "round" }, h("b", { text: it.text.replace("라운드 ", "R") }), it.at ? hhmm(it.at) : "");
    case "message": case "report": return row(it.at, "message", h("div", { class: "report" }, renderMd(String(it.text || ""))));
    case "qa": return row(it.at, "qa", h("div", { class: "qa" },
      h("div", { class: "qa-q" }, h("span", { class: "who", text: "질문" }), renderMd(it.question || "")),
      h("div", { class: "qa-a" + (it.answer == null ? " wait" : "") }, h("span", { class: "who", text: it.answer == null ? "답 기다리는 중" : "내 답" }),
        it.answer == null ? null : h("div", { class: "qa-t", text: it.answer }))));
    case "work": {
      const parts = [it.tools ? `도구 ${it.tools}회` : null, it.notes ? `메모 ${it.notes}개` : null].filter(Boolean).join(" · ");
      const key = "w:" + (it.at || "live");
      const lastNote = [...(it.steps || [])].reverse().find((x) => x.kind === "note");
      const now = it.live ? ((S.detail.activity && S.detail.activity.text) || (lastNote ? String(lastNote.text).split("\n")[0] : "") || "시작하는 중") : "";
      const box = h("details", { class: "work" + (it.live ? " live" : ""), open: S.openWork && S.openWork.has(key) },
        h("summary", {}, it.live ? h("span", { class: "pulse" }) : null, h("span", { text: it.live ? "진행 중" : "작업 과정" }),
          it.live ? h("b", { class: "wnow", text: now }) : null, h("em", { text: parts }), ico("down", "wchev")));
      const fill = () => {  // 펼칠 때만 그린다 — 도구 칩이 수백 개여도 기록이 무겁지 않게
        if (!box.open || box.dataset.drawn) return; box.dataset.drawn = "1";
        box.append(h("div", { class: "wbody" }, ...(it.steps || []).map((st) => st.kind === "note"
          ? h("div", { class: "wnote" }, ...inlineMd(String(st.text || "").replace(/\s*\n+\s*/g, " ")))
          : h("div", { class: "tools" }, ...st.items.map((x) => h("span", { text: x, title: x })))),
          it.live && !(it.steps || []).length ? h("div", { class: "wnote", text: "아직 한 일이 없습니다" }) : null));
      };
      box.addEventListener("toggle", () => { S.openWork = S.openWork || new Set(); box.open ? S.openWork.add(key) : S.openWork.delete(key); fill(); });
      fill();
      return row(it.at, it.live ? "live" : "", box);
    }
    case "tools": {
      const max = 6, el = h("div", { class: "tools" }, ...it.items.slice(0, max).map((x) => h("span", { text: x, title: x })));
      if (it.items.length > max) {
        const more = h("span", { class: "more", text: `+${it.items.length - max}` });
        more.onclick = () => { more.remove(); el.append(...it.items.slice(max).map((x) => h("span", { text: x, title: x }))); };
        el.append(more);
      }
      return row(it.at, "", el);
    }
    case "checkpoint": return row(it.at, "", h("div", { class: "cp" }, ico("book"), h("span", { text: `체크포인트 · ${it.label}` }),
      h("button", { onclick: () => restore(it.id, it.label) }, "이 시점으로 되돌리기")));
    default: {
      const left = it.until ? Math.max(0, Math.round((new Date(it.until) - Date.now()) / 60000)) : null;
      return row(it.at, it.tone === "ok" ? "ok" : it.tone === "fail" ? "fail" : it.tone === "wait" ? "wait" : "",
        h("div", { class: "note " + (it.tone || ""), title: it.detail || "" }, it.tone === "wait" ? ico("clock") : it.tone === "fail" ? ico("alert") : null,
          h("span", { text: it.text }), left != null && left > 0 && S.detail && S.detail.run.status === "waiting_limit" ? h("em", { text: `${left}분 남음` }) : null));
    }
  }
}
async function restore(id, label) {
  if (!confirm(`'${label}' 시점으로 대상 폴더의 파일을 되돌립니다.\n\n그 뒤에 생긴 파일은 지워지고 바뀐 파일은 원래대로 돌아갑니다. 대화 기록과 원장(승인·검증 기록)은 그대로 남습니다. 되돌리기 전 상태도 체크포인트로 남아 다시 되돌릴 수 있습니다.`)) return;
  const r = await call("restore", S.current, id); if (r.ok) { toast("되돌렸습니다"); refresh(); }
}
function cards() {
  const D = S.detail, out = [];
  // 승인·규칙이 앱을 거치지 않고 바뀌어 멈춘 작업 — 사용자가 확인해야 다시 돈다
  if (D.run.tampered) out.push(h("div", { class: "card warn" },
    h("div", { class: "ch" }, ico("shield", "chi"), h("h3", { text: "앱 밖에서 바뀐 결정이 있어 멈췄습니다" })),
    h("p", { class: "lead", text: (D.run.tampered.what || []).join(" / ") }),
    h("p", { class: "f", text: "에이전트가 실행한 코드(테스트·빌드)가 툴킷 기록을 직접 고친 것일 수 있습니다. 계획 승인·권한 규칙·수락은 원래대로 되돌렸습니다 (요구사항·합격 기준 승인은 기록에 남아 있으니 기준 탭에서 다시 보세요). 마지막 라운드에 에이전트가 실행한 명령은 대화 흐름의 작업 과정에 있습니다." }),
    h("div", { class: "acts" }, h("button", { class: "btn primary", onclick: async () => { if ((await call("clearTamper", S.current)).ok) { toast("확인했습니다 — 이어서 진행을 누르면 다시 돕니다"); refresh(); } } }, "확인했음"))));
  // 질문이 있으면 승인 카드가 알려 준다 — 답하면 에이전트가 기준·계획에 반영한 뒤 다시 확인을 요청한다
  const asking = D.asks.some((a) => a.kind === "question");
  const plan = D.plan;
  const planOpen = plan && plan.steps && plan.steps.length && !plan.approved && D.run.status !== "done";  // 끝난 작업에 승인할 계획은 없다
  // 처음 한 번 확인: 기준과 계획이 함께 떠 있으면 한 장으로 — 사용자가 무엇을 승인하는지 위에서부터 읽히게
  // 계획까지 세우는 중이면 기준만 먼저 띄우지 않는다 — 기준과 계획은 한 장으로 한 번에 확인한다
  // 승인 카드는 사용자 차례일 때만 — 라운드가 끝나 실행기가 승인을 기다리거나 멈춰 있을 때. 라운드 도중에 띄우면
  // '승인함'이 에이전트의 '올렸습니다, 확인해 주세요' 보고보다 먼저 쌓여 순서가 거꾸로 읽히고, 상태는 '실행 중'인데 카드가 뜬다
  if (gateOpen()) {
    if (D.reviews && D.reviews.count && planOpen && !S.edit && D.reviews.requirements.length) out.push(startCard(D.reviews, plan, asking));
    else {
      if (D.reviews && D.reviews.count) out.push(reviewCard(D.reviews, asking));
      // 기준과 계획은 함께 승인하므로 둘 다 보여 준다 — 보지 않은 계획(디자인 방향 포함)이 승인되지 않게
      if (planOpen) out.push(planCard(plan, asking));
    }
  }
  // 레퍼런스: 원본 라이선스 경고 — 사람이 보고 옮겨도 된다고 확인해야 완료할 수 있다 (에이전트는 대신 못 한다)
  if (D.license && !D.license.acked && !D.run.accepted) out.push(licenseCard(D.license));
  for (const a of D.asks) out.push(a.kind === "permission" ? permCard(a) : askCard(a));  // 맨 아래 — 화면에 바로 보이게
  if (D.handoff) out.push(handoffCard(D.handoff));
  for (const c of out) if (!c.classList.contains("done")) c.classList.add("decide");
  return out;
}
// 케이스가 무엇을 넣어 보는지 — 입력이 안 보이면 기대값만으로는 기준이 맞는지 판단할 수 없다
function inputText(i) {
  if (!i || !Object.keys(i).length) return "(입력 없음)";
  if (i.screen) { const sc = typeof i.screen === "object" ? i.screen : { path: i.screen };
    return `화면 ${sc.file || sc.path || "/"}${sc.mobile ? " · 모바일" : ""}`; }
  if (i.argv) return i.argv.length ? i.argv.map((a) => (/\s/.test(a) || a === "" ? JSON.stringify(a) : a)).join(" ") : "(인자 없음)";
  if (i.path || i.method) return `${i.method || "GET"} ${i.path || "/"}` + (i.json !== undefined ? " " + JSON.stringify(i.json) : "");
  if (i.call) return `${Object.values(i.call)[0] || "호출"}(${(i.args || []).map((a) => JSON.stringify(a)).join(", ")})`;
  if (i.stdin !== undefined) return "stdin " + JSON.stringify(i.stdin);
  return JSON.stringify(i);
}
// ---------------------------------------------------------------- 직접 고치기 (요구사항·합격 기준·계획)
function argvText(a) { return (a || []).map((x) => (/[\s"']/.test(x) || x === "" ? JSON.stringify(x) : x)).join(" "); }
function parseArgv(t) {
  const out = []; const re = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g; let m;
  while ((m = re.exec(t))) out.push(m[1] !== undefined ? JSON.parse('"' + m[1] + '"') : m[2] !== undefined ? m[2] : m[3]);
  return out;
}
const INPUT_KEYS = ["argv", "stdin", "method", "path", "json", "body", "headers", "call", "args", "kwargs"];
function caseToRow(c) {
  const input = {}; for (const k of INPUT_KEYS) if (k in c) input[k] = c[k];
  const cliOnly = Object.keys(input).every((k) => k === "argv");
  const rest = {}; for (const [k, v] of Object.entries(c)) if (!INPUT_KEYS.includes(k) && !["name", "req", "expect", "description"].includes(k)) rest[k] = v;
  return { name: c.name || "", desc: c.description || "", cli: cliOnly, input: cliOnly ? argvText(input.argv) : JSON.stringify(input),
    expect: JSON.stringify(c.expect || {}), req: [].concat(c.req || []).join(" "), rest };
}
function rowToCase(r, i) {
  const name = r.name.trim() || `case${i + 1}`;
  let input, expect;
  try { input = r.cli ? { argv: parseArgv(r.input) } : JSON.parse(r.input || "{}"); } catch { throw new Error(`${name}: 입력이 JSON 형식이 아닙니다`); }
  try { expect = JSON.parse(r.expect || "{}"); } catch { throw new Error(`${name}: 기대값이 JSON 형식이 아닙니다`); }
  const req = r.req.split(/[\s,]+/).filter(Boolean);
  return { name, ...(r.desc && r.desc.trim() ? { description: r.desc.trim() } : {}), ...input, ...r.rest, req: req.length === 1 ? req[0] : req, expect };
}
async function startEdit(kind) {
  closePop();
  if (kind === "spec") {
    const r = await call("specForEdit", S.current); if (!r.ok) return;
    S.edit = { kind, reqs: r.data.requirements.map((x) => ({ ...x })), cases: r.data.cases.map(caseToRow) };
  } else {
    const p = S.detail.plan;
    S.edit = { kind, summary: p.summary || "", steps: p.steps.map((x) => ({ title: x.title, files: (x.files || []).join(", "),
      commands: (x.commands || []).join("\n"), requirements: x.requirements || [], status: x.status })) };
  }
  S.sig.feed = ""; renderFeed();
}
function cancelEdit() { S.edit = null; S.sig.feed = ""; renderFeed(); }
async function saveEdit(approve) {
  const E = S.edit; let r;
  try {
    if (E.kind === "spec") {
      const reqs = E.reqs.filter((x) => x.text.trim()).map((x) => (x.id ? { id: x.id, text: x.text.trim() } : { text: x.text.trim() }));
      r = await call("saveSpec", S.current, JSON.stringify({ requirements: reqs, cases: E.cases.map(rowToCase) }));
    } else {
      r = await call("savePlan", S.current, JSON.stringify({ summary: E.summary, steps: E.steps.map((x) => ({ title: x.title,
        files: x.files.split(",").map((f) => f.trim()).filter(Boolean), commands: x.commands.split("\n").map((c) => c.trim()).filter(Boolean),
        requirements: x.requirements, status: x.status })) }));
    }
  } catch (err) { toast(err.message); return; }
  if (!r.ok) return;
  S.edit = null;
  if (approve) {
    const a = E.kind === "spec" ? await call("approveReviews", S.current, r.data.review_sig || "") : await call("approvePlan", S.current, r.data.plan_sig || "");
    if (a.ok) toast(E.kind === "spec" ? "고친 내용으로 승인했습니다" : "고친 계획으로 승인했습니다");
  } else toast("저장했습니다 — 확인 후 승인해 주세요");
  S.sig.feed = ""; refresh();
}
function editField(obj, key, attrs) {
  const tag = attrs && attrs.multiline ? "textarea" : "input";
  const el = h(tag, { class: "ef" + (attrs && attrs.mono ? " mono" : ""), placeholder: (attrs && attrs.ph) || "", rows: attrs && attrs.multiline ? 2 : null,
    oninput: (e) => { obj[key] = e.target.value; } });
  el.value = obj[key] ?? "";
  return el;
}
function editFooter() {
  return h("div", { class: "cf" }, h("span", { class: "hint", text: "고친 내용은 사용자 작성으로 원장에 남고, 에이전트에게도 알립니다" }),
    h("button", { class: "btn quiet", onclick: cancelEdit }, "취소"),
    h("button", { class: "btn", onclick: () => saveEdit(false) }, "저장"),
    h("button", { class: "btn primary", onclick: () => saveEdit(true) }, "저장하고 승인"));
}
function specEditCard() {
  const E = S.edit;
  const redraw = () => { S.edit.drawn = false; S.sig.feed = ""; renderFeed(); };
  return h("div", { class: "card editing" },
    h("div", { class: "ch" }, h("h3", { text: "요구사항과 합격 기준 고치기" }), h("span", { class: "tag", text: "편집 중" })),
    h("div", { class: "sub", text: "요구사항" }),
    h("div", { class: "elist" }, ...E.reqs.map((r, i) => h("div", { class: "erow" },
      h("span", { class: "eid", text: r.id || "새" }), editField(r, "text", { ph: "요구사항 문장" }),
      h("button", { class: "ebtn", title: "빼기", onclick: () => { E.reqs.splice(i, 1); redraw(); } }, "×"))),
      h("button", { class: "add", onclick: () => { E.reqs.push({ text: "" }); redraw(); } }, "+ 요구사항 추가")),
    h("div", { class: "sub", text: "합격 기준 — 입력은 CLI 인자(공백으로 구분) 또는 JSON, 기대값은 JSON (status · body · equals · match)" }),
    h("div", { class: "elist" }, h("div", { class: "erow head" }, h("span", { text: "설명" }), h("span", { text: "입력" }), h("span", { text: "기대" }), h("span", { text: "요구사항" }), h("span")),
      ...E.cases.map((c, i) => h("div", { class: "erow case" },
        editField(c, "desc", { ph: "설명 (예: 수량 10개 — 할인 시작)" }), editField(c, "input", { mono: true, ph: c.cli ? "인자" : "{\"path\": \"/\"}" }),
        editField(c, "expect", { mono: true, ph: '{"status": 0}' }), editField(c, "req", { ph: "R1 R2" }),
        h("button", { class: "ebtn", title: "빼기", onclick: () => { E.cases.splice(i, 1); redraw(); } }, "×"))),
      h("button", { class: "add", onclick: () => { const last = E.cases[E.cases.length - 1];
        E.cases.push({ name: "", desc: "", cli: last ? last.cli : true, input: "", expect: last ? last.expect : '{"status": 0}', req: "", rest: {} }); redraw(); } }, "+ 케이스 추가")),
    editFooter());
}
function planEditCard() {
  const E = S.edit;
  const redraw = () => { S.edit.drawn = false; S.sig.feed = ""; renderFeed(); };
  const move = (i, d) => { const j = i + d; if (j < 0 || j >= E.steps.length) return; [E.steps[i], E.steps[j]] = [E.steps[j], E.steps[i]]; redraw(); };
  return h("div", { class: "card editing" },
    h("div", { class: "ch" }, h("h3", { text: "구현 계획 고치기" }), h("span", { class: "tag", text: "편집 중" })),
    h("div", { class: "epad" }, editField(E, "summary", { ph: "계획 요약", multiline: true })),
    h("div", { class: "elist" }, ...E.steps.map((st, i) => h("div", { class: "estep" },
      h("div", { class: "eh" }, h("span", { class: "eid", text: String(i + 1).padStart(2, "0") }), editField(st, "title", { ph: "단계 제목" }),
        h("button", { class: "ebtn", title: "위로", onclick: () => move(i, -1) }, "↑"), h("button", { class: "ebtn", title: "아래로", onclick: () => move(i, 1) }, "↓"),
        h("button", { class: "ebtn", title: "빼기", onclick: () => { E.steps.splice(i, 1); redraw(); } }, "×")),
      h("div", { class: "eb" }, editField(st, "files", { mono: true, ph: "바꿀 파일 (쉼표로 구분)" }),
        editField(st, "commands", { mono: true, ph: "실행할 명령 (한 줄에 하나)", multiline: true })))),
      h("button", { class: "add", onclick: () => { E.steps.push({ title: "", files: "", commands: "", requirements: [], status: "todo" }); redraw(); } }, "+ 단계 추가")),
    editFooter());
}
function reviewCard(R, asking) {
  if (S.edit && S.edit.kind === "spec") return specEditCard();
  const rows = [];
  const perReq = {};
  for (const e of R.expectations) for (const q of e.reqs || []) perReq[q] = (perReq[q] || 0) + 1;
  if (R.requirements.length) rows.push(h("table", { class: "tbl" }, ...R.requirements.map((r) => h("tr", {}, h("td", { class: "k", text: r.id }), h("td", { text: r.text }),
    h("td", { class: "r", text: perReq[r.id] ? `케이스 ${perReq[r.id]}` : "" })))));
  if (R.expectations.length) rows.push(caseFold(R));
  if (R.divergences.length) rows.push(h("div", { class: "sub", text: "원본과 다르게 두는 점" }),
    h("table", { class: "tbl" }, ...R.divergences.map((x) => h("tr", {}, h("td", { text: x.summary })))));
  if (R.omitted.length) rows.push(h("div", { class: "sub", text: "가져오지 않는 부분" }),
    h("table", { class: "tbl" }, ...R.omitted.map((x) => h("tr", {}, h("td", { text: x.summary })))));
  if ((R.checks || []).length) rows.push(h("div", { class: "sub", text: "직접 확인할 점" }),
    h("table", { class: "tbl" }, ...R.checks.map((x) => h("tr", {}, h("td", { text: x.summary + (x.files.length ? " — " + x.files.slice(0, 6).join(", ") : "") })))));
  if (R.case_changes.length) rows.push(h("div", { class: "sub", text: "케이스 변경" }),
    h("table", { class: "tbl" }, ...R.case_changes.flatMap((x) => [
      (x && x.removed && x.removed.length) ? h("tr", {}, h("td", { class: "k", text: "빠짐" }), h("td", { text: x.removed.join(", ") + " — 이전 승인 기준에서 빠진 케이스" })) : null,
      (x && x.changed && x.changed.length) ? h("tr", {}, h("td", { class: "k", text: "바뀜" }), h("td", { text: x.changed.join(", ") + " — 기대값이 이전 승인과 다름" })) : null,
    ].filter(Boolean))));
  const spec = R.requirements.length || R.expectations.length;
  return h("div", { class: "card" },
    h("div", { class: "ch" }, ico("list", "chi"), h("h3", { text: spec ? "요구사항과 합격 기준" : "확인할 항목" }), h("span", { class: "tag", text: "확인 필요" })),
    h("div", { class: "desc", text: "이 내용이 완료 기준이 됩니다. 승인하면 이대로 만들고 검증하며, 나중에 바뀌면 다시 확인을 받습니다." }),
    asking ? h("div", { class: "callout" }, ico("chat"), h("span", { text: "아래 질문에 먼저 답하면 에이전트가 답을 기준에 반영한 뒤 다시 확인을 요청합니다. 지금 승인해도 됩니다." })) : null,
    ...rows,
    h("div", { class: "cf" }, h("span", { class: "hint", text: "직접 고치거나, 고칠 점을 입력창으로 보내세요" }),
      spec && S.detail.run.mode === "request" ? h("button", { class: "btn", onclick: () => startEdit("spec") }, "고치기") : null,
      h("button", { class: "btn primary", onclick: () => approveShown(bothPending() ? "both" : "reviews") },
        bothPending() ? "기준과 계획 승인" : "승인", h("span", { class: "k", text: "Ctrl Enter" }))));
}
function shortInput(i) {
  const t = inputText(i);
  return t.length > 70 ? t.slice(0, 68) + "…" : t;
}
function rawInput(i) {
  if (i && (i.path || i.method)) return { [`${i.method || "GET"} ${i.path || "/"}`]: i.json !== undefined ? i.json : i.body };
  return i;
}
function parseMaybe(b) { if (typeof b !== "string") return b; try { return JSON.parse(b); } catch (e) { return b; } }
function caseDetails(c) {
  const what = [shortInput(c.input), expectText(c.expect)].filter(Boolean).join(" → ");
  const body = h("div", { class: "cdbody" });
  const det = h("details", { class: "cdet" }, h("summary", {}, ico("down", "cchev"), c.screenshot ? "확인한 내용 · 화면" : "확인한 내용"), body);
  det.addEventListener("toggle", () => {
    if (!det.open || body.childElementCount) return;
    body.append(
      what ? h("div", { class: "cline" }, h("span", { class: "k", text: "확인" }), h("span", { text: what })) : null,
      c.screenshot ? shotImg(c.screenshot, c.description || c.name) : null,
      h("details", { class: "cdet raw" }, h("summary", {}, ico("down", "cchev"), "요청 · 기대 · 응답 원문"),
        rawBlock("요청", rawInput(c.input)), rawBlock("기대", c.expect),
        !c.screenshot && c.actual ? rawBlock("응답", c.actual.error ? { 오류: c.actual.error } : { status: c.actual.status, body: parseMaybe(c.actual.body) }) : null));
  });
  return det;
}
function rawBlock(label, v) {
  if (v === undefined || v === null) return null;
  let t = typeof v === "string" ? v : JSON.stringify(v, null, 2);
  if (t.length > 4000) t = t.slice(0, 4000) + "\n… (줄임)";
  return h("div", { class: "raw" }, h("span", { class: "rl", text: label }), h("pre", { text: t }));
}
function expectText(e) {
  if (!e) return "";
  const parts = [];
  if (e.visible) parts.push("보여야 함: " + e.visible.join(", "));
  if (e.selectors) parts.push("요소: " + e.selectors.join(", "));
  if (e.no_errors) parts.push("스크립트 오류 없음");
  if (e.status != null) parts.push(String(e.status));
  for (const [k, v] of Object.entries(e.equals || {})) parts.push(`${k} ${JSON.stringify(v)}`);
  if (typeof e.match === "string") parts.push("내용 형식 일치");  // 본문 전체에 거는 정규식 — 식 자체는 원문 보기에서
  else for (const k of Object.keys(e.match || {})) parts.push(`${k} 형식 일치`);
  if (e.body !== undefined) parts.push("본문 " + JSON.stringify(e.body).slice(0, 80));
  return parts.join(" · ");
}
// ---- 첫 확인 조각들
function lookList(plan) {  // 디자인 컨셉 — 한두 줄 (예전 계획은 앞의 두 줄만)
  return (plan.look || []).length ? h("ul", { class: "looks" }, ...plan.look.slice(0, 2).map((x) => h("li", {}, ...inlineMd(x)))) : null;
}
function reqList(R) {
  const perReq = {};
  for (const e of R.expectations) for (const q of e.reqs || []) perReq[q] = (perReq[q] || 0) + 1;
  return h("ol", { class: "reqs" }, ...R.requirements.map((r) => h("li", {}, h("span", { class: "rid", text: r.id }), h("span", { class: "rt", text: r.text }),
    perReq[r.id] ? h("span", { class: "rc", title: "이 요구사항을 확인하는 합격 케이스 수", text: `확인 ${perReq[r.id]}` }) : null)));
}
function caseFold(R) {  // 합격 기준 — 무엇을 확인하는지만 (입력·기대값 같은 시험 방법은 '검증' 탭에)
  return R.expectations.length ? h("details", { class: "cfold" },
    h("summary", {}, h("span", { text: `합격 기준 ${R.expectations.length}개` }), h("em", { text: "이것이 모두 되면 완료" }), ico("down", "fchev")),
    h("ul", { class: "clist" }, ...R.expectations.map((e) => h("li", {}, h("span", { text: e.description || e.case }),
      (e.reqs || []).length ? h("span", { class: "r", text: e.reqs.join(" ") }) : null)))) : null;
}
function startCard(R, plan, asking) {
  const sec = (title, note, ...kids) => h("div", { class: "ssec" }, h("div", { class: "sh" }, h("b", { text: title }), note ? h("span", { text: note }) : null), ...kids);
  // '마지막에 한 번 확인' — 이미 다 만든 뒤라 '시작 전'이 아니다
  const atEnd = S.detail && S.detail.run && S.detail.run.confirm_at === "end";
  return h("div", { class: "card start" },
    h("div", { class: "ch" }, ico("list", "chi"), h("h3", { text: atEnd ? "마지막 확인" : "시작 전 확인" }), h("span", { class: "tag", text: "확인 필요" })),
    plan.summary ? h("p", { class: "slead" }, ...inlineMd(plan.summary)) : null,
    asking ? h("div", { class: "callout" }, ico("chat"), h("span", { text: "아래 질문에 먼저 답하면 에이전트가 답을 반영한 뒤 다시 확인을 요청합니다. 지금 승인해도 됩니다." })) : null,
    sec("만들 것", `${R.requirements.length}개 — 다 되면 완료`, reqList(R)),
    (plan.decisions || []).length ? sec("정할 것", "추천이 골라져 있습니다 — 바꾸려면 누르세요", decisionTable(plan, true)) : null,
    (plan.look || []).length ? sec("디자인 컨셉", "", lookList(plan)) : null,
    sec("진행 순서", `${plan.steps.length}단계`, h("ol", { class: "sorder" }, ...plan.steps.map((s) => h("li", { text: s.title })))),
    h("div", { class: "cf" }, h("span", { class: "hint", text: "고칠 점은 입력창으로 보내면 반영해 다시 보여 줍니다" }),
      h("button", { class: "btn quiet", onclick: () => startEdit("spec") }, "기준 고치기"),
      h("button", { class: "btn quiet", onclick: () => startEdit("plan") }, "계획 고치기"),
      h("button", { class: "btn primary", onclick: () => approveShown("both") }, atEnd ? "승인" : "승인하고 시작", h("span", { class: "k", text: "Ctrl Enter" }))));
}
function planCard(plan, asking) {
  if (S.edit && S.edit.kind === "plan") return planEditCard();
  return h("div", { class: "card" },
    h("div", { class: "ch" }, ico("book", "chi"), h("h3", { text: "구현 계획" }), h("span", { class: "tag", text: "확인 필요" })),
    plan.summary ? h("p", { class: "slead" }, ...inlineMd(plan.summary)) : null,
    decisionTable(plan),
    lookList(plan) ? h("div", { class: "ssec" }, h("div", { class: "sh" }, h("b", { text: "디자인 컨셉" })), lookList(plan)) : null,
    h("div", { class: "sub", text: `진행 순서 · ${plan.steps.length}단계` }),
    h("div", { class: "steps-list" }, ...plan.steps.map((s) => h("div", { class: "s" }, h("span", { text: s.title })))),
    h("div", { class: "cf" }, h("span", { class: "hint", text: "직접 고치거나, 바꿀 점을 입력창으로 보내세요" }),
      h("button", { class: "btn", onclick: () => startEdit("plan") }, "고치기"),
      h("button", { class: "btn primary", onclick: () => approveShown(bothPending() ? "both" : "plan") },
        bothPending() ? "기준과 계획 승인" : "승인하고 구현 시작", h("span", { class: "k", text: "Ctrl Enter" }))));
}
// 사전 설계 결정 — 추천이 골라져 있고, 대안을 누르면 바꿔서 승인한다 (승인 전 계획에서만 고를 수 있다)
function decisionTable(plan, bare) {
  const list = plan.decisions || [];
  if (!list.length) return null;
  const key = S.detail.plan_sig || "";
  if (!S.picks || S.picks.sig !== key) S.picks = { sig: key, v: {} };
  const open = !plan.approved;
  return h("div", { class: "decisions" },
    bare ? null : h("div", { class: "sub", text: open ? "먼저 정할 것 — 추천이 골라져 있습니다. 바꾸려면 대안을 누르세요" : "정한 것" }),
    ...list.map((d) => {
      const opts = [d.choice, ...(open ? d.options || [] : [])];
      const row = h("div", { class: "dec" },
        h("div", { class: "dt", text: d.topic }),
        h("div", { class: "dv" }, h("div", { class: "dseg" }, ...opts.map((o, i) => {
          const b = h("button", { class: "opt" + ((S.picks.v[d.topic] || d.choice) === o ? " on" : ""), disabled: !open,
            onclick: () => {
              if (o === d.choice) delete S.picks.v[d.topic]; else S.picks.v[d.topic] = o;
              for (const x of row.querySelectorAll(".opt")) x.classList.toggle("on", x === b);
            } }, o, i === 0 && open ? h("span", { class: "rec", text: d.changed_by_user ? "바꿈" : "추천" }) : null);
          return b;
        })), d.why ? h("div", { class: "why", text: d.why }) : null));
      return row;
    }));
}
function licenseCard(L) {
  return h("div", { class: "card warn" },
    h("div", { class: "ch" }, ico("shield", "chi"), h("h3", { text: "원본 라이선스 확인" }), h("span", { class: "tag", text: "직접 판단" })),
    h("p", { class: "lead", text: L.warning }),
    h("p", { class: "f", text: "원본 코드를 이 프로젝트에 옮겨 써도 되는지 직접 확인해 주세요 — 사내 코드라면 사용 허락이 있는지, 공개 코드라면 라이선스 조건을 지킬 수 있는지. 확인하면 남은 완료 판정으로 넘어갑니다. 옮기면 안 되면 작업을 멈추고, 원본을 보지 않고 요구사항만으로 새로 만드는 '요청사항으로 만들기'를 쓰세요." }),
    h("div", { class: "acts" }, h("button", { class: "btn primary", onclick: async () => {
      const r = await call("ackLicense", S.current, L.warning);
      // start 는 이미 돌고 있으면 아무것도 하지 않는다 — 실행기가 기다리는 중이든 앱을 다시 켠 뒤든 바로 이어 간다
      if (r.ok) { toast("확인했습니다 — 완료 판정을 이어 갑니다"); await call("start", S.current); refresh(); }
      else toast(r.error || "확인하지 못했습니다");
    } }, "확인했습니다 — 옮겨도 됩니다")));
}
function permCard(a) {
  const input = a.input || {};
  const what = (input.commands && input.commands.join("\n")) || input.command || input.file_path || input.url || JSON.stringify(input).slice(0, 300);
  const ans = (data) => call("answer", S.current, a.id, JSON.stringify(data)).then(refresh);
  return h("div", { class: "card" },
    h("div", { class: "ch" }, ico("shield", "chi"), h("h3", { text: "작업 허락 요청" }), h("span", { class: "tag", text: S.kinds[a.permission_kind] || a.permission_kind || "" })),
    h("div", { class: "desc", text: String(a.tool || "").startsWith("mcp:") ? "툴킷 검증 도구가 대신 실행할 명령입니다 — 규칙상 확인이 필요합니다."
      : `${a.tool || "도구"} — 규칙상 확인이 필요한 작업입니다.` }),
    h("div", { class: "cmd", text: what }),
    h("div", { class: "cf" }, h("span", { class: "hint", text: "Y 허용 · N 거절" }),
      h("button", { class: "btn quiet", onclick: () => ans({ allow: false }) }, "거절"),
      a.rule ? h("button", { class: "btn", title: `'${a.rule} …' 꼴의 명령은 이 작업에서 다시 묻지 않습니다`, onclick: () => ans({ allow: true, remember: true }) }, "이 명령은 항상") : null,
      h("button", { class: "btn", onclick: () => ans({ allow: true, always: true }) }, "이 프로젝트에서 항상"),
      h("button", { class: "btn primary", onclick: () => ans({ allow: true }) }, "허용")));
}
function askCard(a) {
  const input = h("textarea", { class: "input qin", rows: 1, placeholder: "직접 답하기 — Enter 로 보내기, Shift+Enter 줄바꿈",
    oninput: (e) => { e.target.style.height = "auto"; e.target.style.height = Math.min(160, e.target.scrollHeight) + "px"; },
    onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (input.value.trim()) send({ text: input.value.trim() }); } } });
  const send = (data) => call("answer", S.current, a.id, JSON.stringify(data)).then(refresh);
  const q = String(a.question || ""), recommends = /추천/.test(q);
  return h("div", { class: "card ask" },
    h("div", { class: "ch" }, ico("chat", "chi"), h("h3", { text: "질문" }), h("span", { class: "tag", text: "답해 주세요" })),
    h("div", { class: "qbody" }, renderMd(q)),
    (a.choices || []).length ? h("div", { class: "qopts" }, ...(a.choices || []).map((c, i) => h("button", { class: "qopt", onclick: () => send({ choice: c }) },
      h("span", { class: "qn", text: String(i + 1) }), h("span", { class: "qt", text: c })))) : null,
    h("div", { class: "qreply" }, recommends ? h("button", { class: "btn", onclick: () => send({ text: "추천대로 해 주세요" }) }, "추천대로") : null,
      input, h("button", { class: "btn primary", onclick: () => input.value.trim() && send({ text: input.value.trim() }) }, "보내기")));
}
// 글 한 덩어리를 목록으로 — "1) … 2) …", "(1) …", "Q1 …", 줄바꿈 목록을 항목으로 나눈다 (나눌 수 없으면 그대로)
function splitItems(text) {
  const t = String(text || "").trim();
  if (!t) return { lead: "", items: [] };
  const lines = t.split(/\n+/).map((x) => x.replace(/^\s*[-*•·]\s*/, "").trim()).filter(Boolean);
  if (lines.length > 1) return { lead: "", items: lines };
  const parts = t.split(/\s*(?=(?:\(\d{1,2}\)|(?<![\w.(])\d{1,2}\)|Q\d{1,2}\s|[①-⑳]))/).map((x) => x.trim()).filter(Boolean);
  if (parts.length < 3) return { lead: t, items: [] };
  const marker = /^(?:\(\d{1,2}\)|\d{1,2}\)|[①-⑳])\s*/;
  const lead = marker.test(parts[0]) || /^Q\d/.test(parts[0]) ? "" : parts.shift();
  const l = lead.replace(/[:：]\s*$/, "");
  return { lead: l.length <= 12 ? "" : l, items: parts.map((x) => x.replace(marker, "").replace(/[,，;]\s*$/, "")) };  // '핵심 결정:' 같은 머리말은 절 제목과 겹쳐 뺀다
}
function itemList(text, cls) {
  const { lead, items } = splitItems(text);
  return [lead ? h("p", { class: "ho-lead2", text: lead }) : null,
    items.length ? h("ul", { class: "ho-list" + (cls ? " " + cls : "") }, ...items.map((x) => {
      const m = x.match(/^(Q\d{1,2}|A\d{1,2})\s+(.*)$/);
      return h("li", {}, m ? h("span", { class: "ho-k", text: m[1] }) : null, m ? m[2] : x);
    })) : null].filter(Boolean);
}
function asItems(v) {
  if (Array.isArray(v)) return v.filter((x) => String(x).trim());
  const { lead, items } = splitItems(v); return items.length ? items : lead ? [lead] : [];
}
// 목록 한 줄 — **굵게**·`코드` 를 살리고, 명령(코드)이 있으면 복사 단추를 붙인다
function hoItem(x, copy) {
  const t = String(x), code = (t.match(/`([^`]+)`/) || [])[1];
  const m = t.match(/^(Q\d{1,2}|A\d{1,2})\s+(.*)$/);
  return h("li", {}, m ? h("span", { class: "ho-k", text: m[1] }) : null, h("span", { class: "ho-t" }, ...inlineMd(m ? m[2] : t)),
    copy && code ? h("button", { class: "ho-copy", title: "명령 복사", onclick: (e) => { e.stopPropagation(); copy(code); } }, "복사") : null);
}
function handoffCard(o) {
  const D = S.detail, run = D.run, plan = run.mode === "plan";
  const B = D.brief || {};
  const copy = (txt) => { if (navigator.clipboard) navigator.clipboard.writeText(txt); toast("복사했습니다", 1500); };
  // 새 형식: summary 는 한두 문장, 나머지는 목록. 예전 형식(긴 summary 한 덩어리)은 첫 문장만 크게, 나머지를 '바뀐 것' 목록으로
  const sum = String(o.summary || "").trim();
  let head = sum, rest = "";
  if (!o.changes && sum.length > 160) {
    const m1 = sum.match(/^(.{20,220}?(?:[.。!?]|다\.|함\.))\s/);
    head = m1 ? m1[1].trim() : ""; rest = head ? sum.slice(head.length).trim() : sum;
  }
  const changes = o.changes ? asItems(o.changes) : asItems(rest);
  const steps = asItems(o.how_to_run), checked = asItems(o.checked), risks = asItems(o.risks), next = asItems(o.follow_ups);
  const metric = (n, label, tone) => h("div", { class: "ho-m" + (tone ? " " + tone : "") }, h("b", { text: String(n) }), h("span", { text: label }));
  const metrics = plan
    ? [metric((B.requirements || []).length, "요구사항"), metric((B.screens || []).length, "화면"), metric((B.tests || []).length, "테스트 시나리오"),
       metric(B.open_questions || 0, "열린 질문", B.open_questions ? "warn" : "")]
    : [D.requirements && D.requirements.length ? metric(`${D.requirements.filter((r) => r.status !== "pending").length}/${D.requirements.length}`, "구현된 요구사항")
       : D.items && D.items.total ? metric(`${D.items.done}/${D.items.total}`, "옮긴 항목") : null,  // 레퍼런스는 요구사항이 아니라 옮길 항목
       D.checks && D.checks.cases ? metric(`${D.checks.passed}/${D.checks.cases}`, "합격 케이스", D.checks.passed === D.checks.cases ? "ok" : "warn") : null,
       D.gates ? metric(`${D.gates.filter((g) => g.ok).length}/${D.gates.length}`, "완료 조건") : null].filter(Boolean);
  const sec = (title, list, cls, open, withCopy) => list.length ? h("details", { class: "ho-sec", open: !!open },
    h("summary", {}, h("span", { text: title }), h("em", { text: String(list.length) }), ico("down", "ho-chev")),
    h(cls === "num" ? "ol" : "ul", { class: "ho-list" + (cls ? " " + cls : "") }, ...list.map((x) => hoItem(x, withCopy ? copy : null)))) : null;
  return h("div", { class: "card ho" + (run.accepted ? " done" : "") },
    h("div", { class: "ho-top" },
      h("div", { class: "ho-badge" }, ico("check")),
      h("div", { class: "ho-title" }, h("b", { text: plan ? "기획 패키지가 준비됐습니다" : "만들고 검증까지 끝났습니다" }),
        h("span", { text: [`${run.round}라운드`, usageText(run), o.at ? ago(o.at) : null].filter(Boolean).join(" · ") })),
      h("span", { class: "tag" + (run.accepted ? " ok" : ""), text: run.accepted ? "수락함" : "검토 후 수락" })),
    head ? h("p", { class: "ho-lead" }, ...inlineMd(head)) : null,
    metrics.length ? h("div", { class: "ho-ms" }, ...metrics) : null,
    plan ? null : sec("실행 방법", steps, "num", true, true),  // 기획은 아래 '기획 패키지 보기' 단추가 그 역할
    sec(plan ? "핵심 결정" : "바뀐 것", changes, "", true),
    sec("확인한 것", checked, "ok"),
    sec(plan ? "열린 질문·확인할 가정" : "남은 위험·확인 못 한 것", risks, "warn"),
    sec("이어서 할 일", next, "num"),
    h("div", { class: "cf" }, h("span", { class: "hint", text: "고칠 점은 입력창으로 보내면 이어서 고칩니다" }),
      plan ? h("button", { class: "btn quiet", onclick: () => { S.tab = "brief"; S.sig.right = ""; renderRight(); } }, "기획 패키지 보기")
        : h("button", { class: "btn quiet", onclick: () => { S.tab = "changes"; S.sig.right = ""; loadChanges(); renderRight(); } }, "변경 사항"),
      run.accepted ? null : h("button", { class: "btn", onclick: () => call("accept", S.current).then((r) => r.ok && (toast("수락했습니다"), refresh())) }, "수락"),
      plan ? h("button", { class: "btn primary", onclick: () => call("briefFiles", S.current).then((r) => r.ok && r.data.docs.length && buildFromPlan(r.data.folder + "/기획서.md", r.data)) }, "이 기획으로 만들기")
        : h("button", { class: "btn primary", onclick: () => call("openFolder", run.target_root) }, ico("folder"), "폴더 열기")));
}

// ---------------------------------------------------------------- 기획서 보기 (마크다운을 안전하게 — HTML 은 글자로)
function inlineMd(text) {
  // **굵게** · `코드` · [글자](주소) — 기획 문서끼리의 링크(.md)는 그 문서 탭으로, 웹 주소는 기본 브라우저로 (HTML 은 글자로만)
  const out = []; const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\))/g; let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
    const t = m[0];
    if (t.startsWith("**")) out.push(h("b", { text: t.slice(2, -2) }));
    else if (t.startsWith("`")) out.push(h("code", { text: t.slice(1, -1) }));
    else {
      const [, label, href] = t.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      const doc = decodeURIComponent(href.split("#")[0]).split(/[\\/]/).pop();
      if (/^https?:\/\//.test(href)) out.push(h("a", { href, text: label }));
      else if (/\.md$/i.test(doc)) out.push(h("a", { href: "#", class: "doclink", "data-doc": doc, text: label, onclick: (e) => { e.preventDefault(); e.stopPropagation();
        const rb = document.querySelector(".reader .doctabs"); const tabs = [...document.querySelectorAll(rb ? ".reader .doctabs button" : ".doctabs button")];
        const b = tabs.find((x) => x.dataset.name === doc); if (b) b.click(); } }));
      else out.push(h("span", { class: "doclink", text: label }));
    }
    last = m.index + t.length;
  }
  if (last < text.length) out.push(document.createTextNode(text.slice(last)));
  return out;
}
function renderMd(md) {
  const root = h("div", { class: "md" }); const lines = md.replace(/<!--[\s\S]*?-->/g, "").split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) { const buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++; root.append(h("pre", { text: buf.join("\n") })); continue; }
    const hm = l.match(/^(#{1,6})\s+(.*)/);
    if (hm) { root.append(h("h" + Math.min(4, hm[1].length), {}, ...inlineMd(hm[2]))); i++; continue; }
    if (/^\s*\|/.test(l)) {
      const rows = []; while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
      root.append(h("table", {}, ...body.map((r, k) => h("tr", {}, ...cells(r).map((c) => h(k === 0 ? "th" : "td", {}, ...inlineMd(c)))))));
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
      const ordered = /^\s*\d+\./.test(l); const list = h(ordered ? "ol" : "ul", ordered ? { start: String(parseInt(l, 10) || 1) } : {});
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) list.append(h("li", {}, ...inlineMd(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ""))));
      root.append(list); continue;
    }
    if (/^\s*>/.test(l)) {
      const buf = []; while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      root.append(h("blockquote", {}, ...buf.flatMap((x, k) => [...(k ? [h("br")] : []), ...inlineMd(x)]))); continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(l)) { root.append(h("hr")); i++; continue; }
    if (!l.trim()) { i++; continue; }
    const para = []; while (i < lines.length && lines[i].trim() && !/^(#|```|\s*\||\s*>|\s*([-*]|\d+\.)\s)/.test(lines[i])) para.push(lines[i++]);
    root.append(h("p", {}, ...para.flatMap((x, k) => [...(k ? [h("br")] : []), ...inlineMd(x.trim())])));
  }
  return root;
}
// 기획 패키지 — 기획서·정책서·화면 설계(와이어프레임)·테스트 시나리오와 점검 결과
async function briefPane(p) {
  const r = await call("briefFiles", S.current);
  if (!r.ok) return;
  const B = r.data, chk = B.check || {};
  if (!B.docs.length) { p.append(h("div", { class: "empty", text: "기획 패키지를 만드는 중입니다.\n질문이 오면 답해 주세요." })); return; }
  if (!S.briefDoc || !B.docs.some((d) => d.name === S.briefDoc)) S.briefDoc = B.docs[0].name;
  const main = B.docs.find((d) => d.name === "기획서.md");
  // 위 줄: 보기·내보내기·폴더 세 단추를 같은 너비로 꽉 차게 / 아래 줄: '이 기획으로 만들기'를 따로 한 줄 꽉 차게
  p.append(h("div", { class: "briefbar" },
    h("button", { class: "btn sm", onclick: () => briefReader(B, S.briefDoc) }, ico("doc"), "크게 보기"),
    h("button", { class: "btn sm", onclick: (e) => { e.stopPropagation(); exportMenu(e.currentTarget, B); } }, ico("download"), "내보내기"),
    h("button", { class: "btn sm", title: "기획 패키지 폴더 열기", onclick: () => call("openFolder", B.folder) }, ico("folder"), "폴더 열기"),
    main ? h("button", { class: "btn primary build", onclick: () => buildFromPlan(B.folder + "/기획서.md", B) }, "이 기획으로 만들기") : null));
  p.append(briefCheck(chk));
  p.append(h("div", { class: "doctabs" }, ...B.docs.map((d) => h("button", { "data-name": d.name, class: (d.name === S.briefDoc ? "on" : "") + (d.attachment ? " att" : ""),
    onclick: () => { S.briefDoc = d.name; S.sig.right = ""; renderRight(); } }, d.title))));
  const doc = await call("readBriefDoc", S.current, S.briefDoc);
  if (doc.ok) p.append(docBody(B, S.briefDoc, doc.data.text).el);
}
function briefCheck(chk) {
  const n = (chk.problems || []).length, docs = chk.docs || {};
  const row = (ok, text) => h("div", { class: "gate2" + (ok ? " ok" : "") }, ok ? ico("check", "gi") : h("i"), h("div", { text }));
  const box = h("details", { class: "bcheck" + (n ? "" : " ok"), open: n > 0 && n <= 4 },
    h("summary", {}, h("b", { text: n ? `맞물림 점검 — 고칠 것 ${n}` : "맞물림 점검 통과" }),
      h("span", { text: `요구사항 ${(chk.requirements || []).length} · 화면 ${(chk.screens || []).length} · 시나리오 ${(chk.tests || []).length}${chk.open_questions ? ` · 열린 질문 ${chk.open_questions}` : ""}` })),
    ...Object.entries(docs).map(([k, v]) => row(v, k.replace(".md", ""))),
    ...(chk.problems || []).map((t) => h("div", { class: "bp bad", text: t })),
    ...(chk.notes || []).map((t) => h("div", { class: "bp", text: t })));
  return box;
}
function shotGrid(shots) {
  return h("div", { class: "wfgrid" }, ...shots.map((path) => {
    const name = path.split(/[\\/]/).pop(), img = h("img", { alt: name, title: "크게 보기" });
    call("briefShot", S.current, name).then((r) => { if (r.ok) img.src = r.data.url; });
    img.addEventListener("click", () => { const big = h("div", { class: "lightbox", onclick: () => big.remove() }, h("div", { class: "lbt", text: name.replace(/\.png$/, "") }), h("img", { src: img.src })); document.body.append(big); });
    return h("figure", {}, img, h("figcaption", { text: name.replace(/\.png$/, "") }));
  }));
}
// 화면 설계 — '## S1 …' 절마다 그 화면의 와이어프레임 그림과 설명을 나란히 (그림 나열이 아니라 화면 단위로 읽게)
function screenDesign(md, shots) {
  const lines = String(md || "").split(/\r?\n/), head = [], secs = [];
  for (const l of lines) {
    const m = l.match(/^##\s+S(\d{1,3})(?![0-9A-Za-z])/);
    if (m) secs.push({ id: "S" + (+m[1]), lines: [l] });
    else (secs.length ? secs[secs.length - 1].lines : head).push(l);
  }
  const pick = (id, mobile) => shots.find((p) => { const n = p.split(/[\\/]/).pop(); return new RegExp(`^${id}(?![0-9])[^]*${mobile ? "-mobile" : ""}\\.png$`).test(n) && (mobile || !/-mobile\.png$/.test(n)); });
  const loads = [];
  const fig = (path) => {
    const name = path.split(/[\\/]/).pop(), img = h("img", { alt: name, title: "크게 보기" });
    loads.push(call("briefShot", S.current, name).then((r) => { if (r.ok) img.src = r.data.url; }));
    img.addEventListener("click", () => { const big = h("div", { class: "lightbox", onclick: () => big.remove() }, h("div", { class: "lbt", text: name.replace(/\.png$/, "") }), h("img", { src: img.src })); document.body.append(big); });
    return h("figure", {}, img, h("figcaption", { text: name.replace(/\.png$/, "").replace(/-mobile$/, " · 모바일") }));
  };
  const used = new Set();
  const root = h("div", { class: "scrdoc" }, renderMd(head.join("\n")),
    ...secs.map((sc) => {
      const pc = pick(sc.id, false), mo = pick(sc.id, true);
      [pc, mo].forEach((x) => x && used.add(x));
      return h("section", { class: "scr", id: "screen-" + sc.id }, h("div", { class: "scr-fig" }, ...[pc, mo].filter(Boolean).map(fig)),
        h("div", { class: "scr-txt" }, renderMd(sc.lines.join("\n"))));
    }));
  const rest = shots.filter((x) => !used.has(x));
  if (rest.length) root.append(h("h3", { text: "그 밖의 화면" }), h("div", { class: "wfgrid" }, ...rest.map(fig)));
  return { el: root, ready: Promise.all(loads) };
}
// 화면 설계 프로토타입 자료 — 화면 목록 표(| S | 화면 | 기능 | …)로 기능별 탭을, '## S1 …' 절로 화면마다 설명을, wireframes/*.html 로 화면을
async function protoData(md) {
  const r = await call("briefScreens", S.current); if (!r.ok || !r.data.length) return null;
  const lines = String(md || "").split(/\r?\n/), head = [], secs = {}, order = [];
  let cur = null;
  for (const l of lines) {
    const m = l.match(/^##\s+S(\d{1,3})(?![0-9A-Za-z])\s*(.*)$/);
    if (m) { cur = "S" + (+m[1]); order.push(cur); secs[cur] = { title: m[2].replace(/^[·:\-—\s]+/, "").trim(), lines: [] }; continue; }
    if (/^##\s/.test(l)) cur = null;  // 화면 절이 아닌 다른 절(공통 등)은 개요로
    (cur ? secs[cur].lines : head).push(l);
  }
  // 화면 목록 표: 머리줄에서 '기능'·'화면' 칸을 찾는다
  const rows = [], feat = {}, names = {};
  for (let i = 0; i < lines.length; i++) {
    if (!/^\s*\|/.test(lines[i]) || !/^\s*\|\s*S\s*\|/i.test(lines[i])) continue;
    const cells = (t) => t.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    const hd = cells(lines[i]), fi = hd.findIndex((c) => /기능|그룹|영역|메뉴/.test(c)), ni = hd.findIndex((c, k) => k > 0 && /화면|이름/.test(c));
    for (let j = i + 1; j < lines.length && /^\s*\|/.test(lines[j]); j++) {
      const c = cells(lines[j]); const m = (c[0] || "").match(/^S(\d{1,3})$/); if (!m) continue;
      const id = "S" + (+m[1]); rows.push(id);
      if (fi >= 0 && c[fi]) feat[id] = c[fi].replace(/\*\*/g, "");
      if (ni >= 0 && c[ni]) names[id] = c[ni].replace(/\*\*/g, "");
    }
    break;
  }
  const files = {};
  for (const f of r.data) { const m = f.name.match(/^S(\d{1,3})(?![0-9])/i); if (m && !files["S" + (+m[1])]) files["S" + (+m[1])] = f.html; }
  const ids = [...new Set([...rows, ...order, ...Object.keys(files)])].filter((id) => files[id] || secs[id]);
  const screens = {};
  for (const id of ids) {
    const html = files[id] || "";
    const links = [...new Set([...html.matchAll(/(?:href|data-go)\s*=\s*["']?(?:[^"'>]*[\\/])?S(\d{1,3})(?![0-9])/gi)].map((m) => "S" + (+m[1])))];
    const body = (secs[id] ? secs[id].lines : []).filter((l) => !/^\s*(와이어프레임|프로토타입)\s*[:：]/.test(l)).join("\n");
    const file = r.data.find((f) => new RegExp(`^${id}(?![0-9])`, "i").test(f.name));
    screens[id] = { name: names[id] || (secs[id] && secs[id].title) || id, html, notes: renderMd(body).outerHTML, links,
      src: file ? `beomproto:/${S.current}/${encodeURIComponent(file.name)}` : null };
  }
  const groups = [];
  for (const id of ids) { const g = feat[id] || "화면"; let x = groups.find((y) => y.name === g); if (!x) groups.push(x = { name: g, ids: [] }); x.ids.push(id); }
  return { overview: renderMd(head.join("\n")).outerHTML, groups, screens };
}
function docBody(B, name, text) {
  if (name === "화면설계.md" && (B.wireframes || []).length && window.OssProto) {
    const box = h("div", { class: "protodoc" }, h("div", { class: "empty", text: "화면을 불러오는 중…" }));
    const ready = protoData(text).then((data) => {
      if (data) OssProto(box, data);
      else box.replaceChildren(((B.shots || []).length ? screenDesign(text, B.shots).el : renderMd(text)));
    });
    return { el: box, ready };
  }
  return name === "화면설계.md" && (B.shots || []).length ? screenDesign(text, B.shots) : { el: renderMd(text), ready: Promise.resolve() };
}
// 내보내기 — 지금 보는 모습 그대로 한 파일(HTML, 그림 포함)로. PDF 는 그 HTML 을 인쇄해 만든다. 공유는 이 파일 하나면 된다
const EXPORT_CSS = `
:root{--t:#1d1d1b;--m:#5f5f58;--f:#8e8e86;--l:#e6e6e1;--s:#f6f6f3;--a:#19735f}
*{box-sizing:border-box}body{margin:0;font:14px/1.75 "Pretendard Variable",Pretendard,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif;color:var(--t);background:#fff;word-break:keep-all;overflow-wrap:anywhere}
.wrap{max-width:1240px;margin:0 auto;padding:44px 40px 80px}
.cover{border-bottom:1px solid var(--l);padding-bottom:22px;margin-bottom:8px}.cover small{color:var(--f);font-size:12px}.cover h1{font-size:30px;line-height:1.3;margin:6px 0 4px;letter-spacing:-.03em}
.cover p{color:var(--m);margin:0}.toc{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0 0;padding:0}.toc a{font-size:13px;color:var(--t);text-decoration:none;border:1px solid var(--l);border-radius:999px;padding:4px 12px}
section.doc{padding-top:28px}section.doc>.dt{font-size:12px;color:var(--a);font-weight:700;letter-spacing:.04em;margin:0}
section.doc>.md,section.doc>.scrdoc{max-width:880px}
.tabbed .toc{position:sticky;top:0;z-index:5;background:#fff;margin:0;padding:12px 0;border-bottom:1px solid var(--l)}
.tabbed .toc a.on{background:var(--t);color:#fff;border-color:var(--t)}.tabbed .cover{border-bottom:none;padding-bottom:0}
.tabbed section.doc{display:none}.tabbed section.doc.on{display:block}
.static-only{display:none}
.md h1{font-size:24px;margin:4px 0 10px;letter-spacing:-.02em;line-height:1.35}.md h2{font-size:18px;margin:30px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--l)}.md h3{font-size:15px;margin:20px 0 6px}.md h4{font-size:14px;color:var(--m);margin:16px 0 4px}
.md p{margin:6px 0}.md ul,.md ol{margin:4px 0 10px;padding-left:22px}.md li{margin:3px 0}
.md table{border-collapse:separate;border-spacing:0;width:100%;margin:10px 0;font-size:13px;border:1px solid var(--l);border-radius:10px;overflow:hidden}
.md th,.md td{padding:7px 10px;text-align:left;vertical-align:top;border-top:1px solid var(--l);min-width:3.4em}.md tr:first-child th,.md tr:first-child td{border-top:none}.md th{background:var(--s);font-weight:650}
.md code{font-family:"Cascadia Mono",Consolas,monospace;font-size:12px;background:var(--s);border:1px solid var(--l);border-radius:5px;padding:1px 5px}
.md pre{font-family:"Cascadia Mono",Consolas,monospace;font-size:12px;background:var(--s);border:1px solid var(--l);border-radius:10px;padding:12px 14px;white-space:pre-wrap}
.md blockquote{margin:10px 0;padding:10px 14px;border-left:3px solid var(--a);background:var(--s);border-radius:0 10px 10px 0;color:var(--m)}
.md hr{border:none;border-top:1px solid var(--l);margin:18px 0}.md a{color:var(--a);text-decoration:none;border-bottom:1px solid rgba(25,115,95,.35)}
.scr{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:28px;padding:26px 0;border-top:1px solid var(--l);align-items:start}
.scr-fig{display:flex;flex-direction:column;gap:14px;position:sticky;top:16px}.scr figure,.wfgrid figure{margin:0}
.scr img,.wfgrid img{width:100%;height:auto;border:1px solid var(--l);border-radius:12px;background:#fff;display:block}
figcaption{font-size:12px;color:var(--f);margin-top:4px}.scr-txt .md h2{margin-top:0;border:none;padding:0}
.wfgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
@media print{.tabbed section.doc{display:block!important}.static-only{display:block}.proto-mount{display:none}.wrap{padding:0;max-width:none}section.doc{break-before:page;padding-top:0}.cover+section.doc{break-before:auto}.scr{break-inside:avoid}.scr-fig{position:static}
table,tr,figure,blockquote,pre{break-inside:avoid}.toc{display:none}a{border:none}}
@page{size:A4;margin:14mm 13mm}`;
async function exportBrief(B, fmt) {
  toast(fmt === "pdf" ? "PDF 를 만드는 중입니다…" : "내보내는 중입니다…", 2500);
  const run = S.detail.run, docs = B.docs.filter((d) => !d.attachment);
  const secs = []; let proto = null;
  for (const [i, d] of docs.entries()) {
    const r = await call("readBriefDoc", S.current, d.name); if (!r.ok) continue;
    // 화면 설계: 화면에서는 눌러 보는 프로토타입, 인쇄(PDF)에서는 화면 그림 + 설명
    const isScr = d.name === "화면설계.md";
    if (isScr && (B.wireframes || []).length) proto = await protoData(r.data.text);
    const b = isScr && (B.shots || []).length ? screenDesign(r.data.text, B.shots) : { el: renderMd(r.data.text), ready: Promise.resolve() };
    await b.ready;
    for (const a of b.el.querySelectorAll("a.doclink[data-doc]")) { const k = docs.findIndex((x) => x.name === a.dataset.doc); a.setAttribute("href", k >= 0 ? "#doc-" + k : "#"); }
    for (const img of b.el.querySelectorAll("img")) img.removeAttribute("title");
    const inner = isScr && proto ? `<div class="proto-mount"></div><div class="static-only">${b.el.outerHTML}</div>` : b.el.outerHTML;
    secs.push(`<section class="doc" id="doc-${i}"><p class="dt">${i + 1} · ${esc(d.title)}</p>${inner}</section>`);
  }
  const title = (run.name || "기획") + " — 기획 패키지", day = new Date().toLocaleDateString("ko-KR");
  const html = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title><style>${EXPORT_CSS}</style></head>`
    + `<body><div class="wrap"><header class="cover"><small>${esc(day)} · beom-workbench</small><h1>${esc(run.name || "기획")}</h1><p>기획 패키지 — ${docs.map((d) => esc(d.title)).join(" · ")}</p>`
    + `<nav class="toc">${docs.map((d, i) => `<a href="#doc-${i}">${i + 1}. ${esc(d.title)}</a>`).join("")}</nav></header>${secs.join("")}</div></body></html>`;
  const r = await call("exportBriefWith", S.current, html, fmt, JSON.stringify(proto ? { proto } : {}));
  if (r.ok && fmt !== "pdf") { toast("내보냈습니다 — " + r.data.html, 5000); call("openFolder", r.data.folder); }
}
function esc(t) { return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]); }
function exportMenu(anchor, B) {
  popover(anchor, { title: "내보내기 — 공유용", width: 300, items: [
    { id: "html", label: "HTML 한 파일", desc: "그림까지 들어간 파일 하나 — 브라우저로 열고, 메일·메신저로 그대로 보내면 됩니다" },
    { id: "pdf", label: "PDF", desc: "A4 로 인쇄한 모양 — 문서마다 새 쪽에서 시작합니다" }],
    onPick: (it) => exportBrief(B, it.id) });
}
// 크게 보기 — 창 전체에서 문서를 넘겨 가며 읽는다 (Esc 로 닫기)
async function briefReader(B, name) {
  const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const body = h("div", { class: "rbody" });
  const tabs = h("div", { class: "doctabs" });
  const show = async (n) => {
    tabs.replaceChildren(...B.docs.map((d) => h("button", { "data-name": d.name, class: d.name === n ? "on" : "", onclick: () => show(d.name) }, d.title)));
    const doc = await call("readBriefDoc", S.current, n);
    body.classList.toggle("wide", n === "화면설계.md");
    body.replaceChildren(doc.ok ? docBody(B, n, doc.data.text).el : h("div"));
    body.scrollTop = 0;
  };
  const wrap = h("div", { class: "reader", onclick: (e) => { if (e.target === wrap) close(); } },
    h("div", { class: "rpanel" }, h("div", { class: "rhead" }, tabs,
      h("button", { class: "btn sm", onclick: (e) => { e.stopPropagation(); exportMenu(e.currentTarget, B); } }, ico("download"), "내보내기"),
      h("button", { class: "btn quiet sm", onclick: close }, "닫기 Esc")), body));
  document.body.append(wrap); document.addEventListener("keydown", onKey);
  show(name);
}
// 기획서를 첨부한 '요청사항으로 만들기'로 넘긴다 — 홈에서 폴더·에이전트를 고르고 시작
function buildFromPlan(path, B) {
  const d = S.draft;
  // 기획 패키지 전체를 넘긴다 — 정책서는 규칙, 화면 설계는 화면, 테스트 시나리오는 합격 기준이 된다
  const folder = B ? B.folder : "";
  const pack = B ? [...B.docs.filter((x) => !x.attachment).map((x) => folder + "/" + x.name), ...(B.wireframes || [])] : [path];
  S.mode = "request"; d.attachments = pack;
  // 기획한 폴더(기존 저장소·작업 공간)에 그대로 만든다 — 기획만 하려고 자동으로 만든 폴더였으면 비워 두고 고르게
  const planRoot = S.detail && S.detail.run && S.detail.run.target_root;
  if (planRoot && !/[\\/]beom-workbench[\\/]기획[\\/]/.test(planRoot)) {
    d.target_root = planRoot;
    Promise.all([gitOf(planRoot), needsOf(planRoot)]).then(([g, n]) => { d.git = g; d.needs = n; if (!S.current) renderHome(); });
  }
  d.request = "첨부한 기획 패키지대로 만든다. 기획서의 요구사항(R), 정책서의 규칙·계산식, 화면 설계와 와이어프레임의 화면 구성을 따르고, "
    + "테스트 시나리오(T)를 합격 케이스로 옮긴다. 문서끼리 다르면 묻는다.";
  goHome(); toast(B ? "기획 패키지를 첨부했습니다 — 만들 폴더를 고르고 시작하세요" : "기획서를 첨부했습니다 — 만들 폴더를 고르고 시작하세요");
}

// 스크린샷 — 작게 보여 주고 누르면 크게
function shotImg(path, title) {
  const img = h("img", { class: "shot", alt: title, title: "크게 보기" });
  call("screenshot", path).then((r) => { if (r.ok) img.src = r.data.url; });
  img.addEventListener("click", () => {
    const big = h("div", { class: "lightbox", onclick: () => big.remove() }, h("div", { class: "lbt", text: title }), h("img", { src: img.src }));
    document.body.append(big);
  });
  return img;
}

// ---------------------------------------------------------------- 오른쪽 패널
function renderRight() {
  const D = S.detail; if (!D) return;
  const sig = JSON.stringify([S.tab, D.stage, D.gates, D.requirements, D.checks, D.plan, D.items, D.run.cost_usd, D.run.tokens, D.checkpoints, S.changes, S.file, D.spec, D.handoff, D.run.round]);
  if (sig === S.sig.right) return; S.sig.right = sig;
  const right = $("#right");
  const nfiles = S.changes ? S.changes.files.length : null;
  const tab = (id, label, badge) => h("button", { class: S.tab === id ? "on" : "", onclick: () => { S.tab = id; S.sig.right = ""; if (id === "changes") loadChanges(); renderRight(); } },
    label, badge != null ? h("span", { class: "badge", text: badge }) : null);
  const pane = h("div", { class: "pane" });
  const m = D.run.mode;
  if (m === "plan" && !["brief", "changes"].includes(S.tab)) S.tab = "brief";
  if (m === "free" && S.tab !== "changes") S.tab = "changes";
  const tabs = m === "plan" ? [tab("brief", "기획 패키지"), tab("changes", "변경", nfiles)] : m === "free" ? [tab("changes", "변경", nfiles)]
    : [tab("progress", "현황"), D.spec ? tab("spec", "기준") : null, tab("checks", "검증", D.checks && D.checks.cases ? `${D.checks.passed}/${D.checks.cases}` : null), tab("changes", "변경", nfiles)];
  right.replaceChildren(h("div", { class: "tabs" }, h("div", { class: "seg2" }, ...tabs.filter(Boolean))), pane);
  if (S.tab === "brief") briefPane(pane);
  else if (S.tab === "progress") progressPane(pane);
  else if (S.tab === "changes") changesPane(pane);
  else if (S.tab === "spec" && D.spec) specPane(pane);
  else checksPane(pane);
}
function progressPane(p) {
  const D = S.detail;
  const reqs = D.requirements || [];
  const impl = reqs.filter((r) => r.status === "ported").length;
  const gates = D.gates || [];
  const gok = gates.filter((g) => g.ok).length;
  const ck = D.checks || {};
  p.append(h("div", { class: "sec" }, h("h4", { text: "현황" }), h("div", { class: "stats" },
    stat(D.run.mode === "request" ? `${impl}/${reqs.length}` : D.items ? `${D.items.done}/${D.items.total}` : "—", D.run.mode === "request" ? "구현된 요구사항" : "처리한 항목",
      D.run.mode === "request" ? (reqs.length ? impl / reqs.length : 0) : D.items && D.items.total ? D.items.done / D.items.total : 0),
    stat(ck.cases ? `${ck.passed}/${ck.cases}` : "—", "케이스 통과", ck.cases ? ck.passed / ck.cases : 0),
    stat(gates.length ? `${gok}/${gates.length}` : "—", "완료 조건", gates.length ? gok / gates.length : 0),
    stat(usageText(D.run) || "—", `사용량 · ${D.run.round || 0}라운드`))));
  if (D.plan && D.plan.steps && D.plan.steps.length) {
    const done = D.plan.steps.filter((s) => s.status === "done").length;
    p.append(h("div", { class: "sec plan" }, h("h4", {}, "계획", h("span", { text: D.plan.approved ? `${done}/${D.plan.steps.length}` : "승인 전" })),
      ...D.plan.steps.map((s, i) => h("div", { class: "p " + (s.status || "") }, h("i", { text: s.status === "done" ? "" : String(i + 1) }),
        s.status === "done" ? null : null, h("span", { text: s.title })))));
  }
  if (reqs.length) p.append(h("div", { class: "sec" }, h("h4", { text: "요구사항" }), ...reqs.map((r) => h("div", { class: "li" },
    h("span", { class: "k", text: r.id }), h("span", { class: "x" }, r.text, r.target ? h("span", { class: "f", text: r.target }) : null),
    h("span", { class: "s " + (r.status === "ported" ? "ok" : r.status === "omitted" ? "" : "wait"), text: { ported: "구현됨", omitted: "제외", pending: "남음", replaced: "대체" }[r.status] || r.status })))));
  if (gates.length) p.append(h("div", { class: "sec" }, h("h4", { text: "완료 조건" }),
    ...gates.map((g) => h("div", { class: "gate" + (g.ok ? " ok" : ""), title: g.name }, g.ok ? ico("check", "gi") : h("i"), g.label || g.name))));
  // 막지 않는 참고 — 원본에 라이선스 표기가 없을 때 (완료 조건은 아니다)
  if (D.license_note) p.append(h("div", { class: "sec" }, h("h4", { text: "참고" }), h("p", { class: "f", text: D.license_note })));
}
function stat(v, l, ratio) {
  return h("div", { class: "stat" }, h("div", { class: "v", text: v }), h("div", { class: "l", text: l }),
    ratio != null ? h("div", { class: "bar2" }, h("i", { style: `width:${Math.round((ratio || 0) * 100)}%` })) : null);
}
async function loadChanges() {
  const r = await call("changes", S.current);
  if (r.ok) { S.changes = r.data; if (S.file && !S.changes.files.find((f) => f.path === S.file)) S.file = null; renderRight(); }
}
function changesPane(p) {
  const C = S.changes;
  if (!C) { p.append(h("div", { class: "empty", text: "불러오는 중…" })); if (!S.changesLoading) { S.changesLoading = true; loadChanges().finally(() => { S.changesLoading = false; }); } return; }
  if (!C.files.length) { p.append(h("div", { class: "empty", text: "시작 전과 달라진 파일이 없습니다." })); return; }
  const add = C.files.reduce((a, f) => a + (f.added || 0), 0), rem = C.files.reduce((a, f) => a + (f.removed || 0), 0);
  p.append(h("div", { class: "sec" }, h("h4", {}, `파일 ${C.files.length}개`, h("span", { text: `+${add} −${rem}` })),
    h("div", { class: "files" }, ...C.files.map((f) => h("div", { class: "fl" + (S.file === f.path ? " on" : ""), onclick: () => { S.file = S.file === f.path ? null : f.path; renderRight(); } },
      h("span", { class: "badge-st " + f.status, text: f.status }), h("span", { class: "p", text: f.path, title: f.path }),
      f.added != null ? h("span", { class: "ad", text: "+" + f.added }) : null, f.removed ? h("span", { class: "rm", text: "−" + f.removed }) : null)))));
  if (S.file) {
    p.append(h("div", { style: "display:flex;gap:6px" }, h("span", { class: "sp" }),
      h("button", { class: "btn quiet", onclick: async () => { if (!confirm(`${S.file} 를 시작 전 상태로 되돌릴까요?`)) return;
        const r = await call("revertFile", S.current, S.file); if (r.ok) { toast("되돌렸습니다"); loadChanges(); } } }, "이 파일 되돌리기")));
    p.append(diffView(C.diff, S.file));
  }
  p.append(h("div", { class: "sec", style: "margin-top:20px" }, h("h4", { text: "체크포인트" }),
    ...(S.detail.checkpoints || []).map((c) => h("div", { class: "cpl" }, h("span", { text: c.label }), h("span", { class: "t", text: ago(c.at) }),
      h("button", { class: "btn quiet", style: "padding:2px 8px;font-size:12px", onclick: () => restore(c.id, c.label) }, "되돌리기")))));
}
function diffView(diff, file) {
  const out = h("div", { class: "diff" });
  let on = false;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git")) { on = line.includes(" b/" + file); continue; }
    if (!on || line.startsWith("index ") || line.startsWith("---") || line.startsWith("+++") || line.startsWith("new file") || line.startsWith("deleted file")) continue;
    const cls = line.startsWith("@@") ? "h" : line.startsWith("+") ? "a" : line.startsWith("-") ? "d" : "";
    out.append(h("div", { class: cls, text: line || " " }));
  }
  return out;
}
function stamp(at) { if (!at) return ""; const d = new Date(at); return `${d.getMonth() + 1}/${d.getDate()} ${hhmm(at)}`; }
// 기준 — 무엇을 만들기로 합의했나 (요구사항 · 합격 기준 · 계획, 누가 언제 승인했나)
function specPane(p) {
  const D = S.detail, sp = D.spec, reqs = D.requirements || [];
  const ap = sp.approved;
  p.append(h("div", { class: "explain" }, h("b", { text: "합의한 기준" }),
    h("span", { text: ap ? `${stamp(ap.at)} 승인${ap.channel === "decision-change" ? " · 바꾼 결정 반영(자동)" : ""}` : "아직 승인 전" })));
  p.append(h("div", { class: "sec" }, h("h4", {}, "요구사항", h("span", { text: String(reqs.length) })),
    ...reqs.map((r) => h("div", { class: "li" }, h("span", { class: "k", text: r.id }), h("span", { class: "x", text: r.text }), h("span")))));
  p.append(h("div", { class: "sec" }, h("h4", {}, "합격 기준", h("span", { text: `${sp.cases.length}개` })),
    h("div", { class: "hint2", text: "이것이 모두 되면 완료로 봅니다 — 어떻게 시험했는지는 '검증' 탭에" }),
    ...sp.cases.map((c) => h("div", { class: "case" }, h("div", { class: "ct" }, h("b", { text: c.description || c.name }), h("span", { class: "rq", text: c.reqs.join(" ") }))))));
  if (D.plan && D.plan.steps && D.plan.steps.length) {
    p.append(h("div", { class: "sec" }, h("h4", {}, "계획", h("span", { text: D.plan.approved ? `승인${D.plan.approved_at ? " · " + stamp(D.plan.approved_at) : ""}` : "승인 전" })),
      D.plan.summary ? h("div", { class: "hint2", text: D.plan.summary }) : null,
      ...D.plan.steps.map((st, i) => h("div", { class: "pstep" }, h("span", { class: "k", text: String(i + 1).padStart(2, "0") }),
        h("div", {}, h("div", { text: st.title }))))));
  }
}
// 검증 — 무엇을 어떻게 확인했나 (요구사항별 케이스: 입력 → 기대 → 실제)
function checksPane(p) {
  const D = S.detail, ck = D.checks, sp = D.spec;
  if (!ck || !ck.cases || !sp) { p.append(h("div", { class: "empty", text: "아직 검증 전입니다.\n구현이 끝나면 승인된 합격 기준을 지금 코드에 실제로 실행해 확인합니다." })); return; }
  const fails = sp.cases.filter((c) => c.ok === false).length;
  p.append(h("div", { class: "explain" + (fails ? " bad" : " good") }, h("b", { text: fails ? `${fails}개가 기준과 다릅니다` : `합격 기준 ${ck.passed}/${ck.cases} 모두 통과` }),
    h("span", { text: `승인된 합격 기준을 지금 코드에 실제로 실행해 기대 결과와 비교했습니다${sp.verified_at ? " · " + stamp(sp.verified_at) : ""}.` })));
  if ((sp.causes || []).length) {  // 실패 원인 — 같은 원인끼리 묶어 한눈에 (케이스마다 같은 오류를 되풀이해 읽지 않게)
    p.append(h("div", { class: "causes" }, h("h4", { text: "실패 원인" }), ...sp.causes.map((g) => h("div", { class: "cause" },
      h("div", { class: "ch2" }, h("b", { text: g.title }), h("span", { class: "n", text: `${g.cases.length}건` })),
      g.hint ? h("div", { class: "f", text: g.hint }) : null,
      h("div", { class: "f cl", text: g.cases.slice(0, 4).join(" · ") + (g.cases.length > 4 ? ` 외 ${g.cases.length - 4}건` : "") })))));
  }
  const reqs = D.requirements || [];
  const groups = reqs.map((r) => ({ r, cases: sp.cases.filter((c) => c.reqs.includes(r.id)) }));
  const loose = sp.cases.filter((c) => !c.reqs.length);
  if (loose.length) groups.push({ r: { id: "—", text: "요구사항 연결 없음" }, cases: loose });
  for (const g of groups) {
    const ok = g.cases.length && g.cases.every((c) => c.ok);
    p.append(h("div", { class: "rgroup" }, h("div", { class: "rh" }, ok ? ico("check", "gi") : h("span", { class: "ci fail", text: g.cases.length ? "!" : "?" }),
      h("span", { class: "k", text: g.r.id }), h("span", { text: g.r.text })),
      ...g.cases.map((c) => h("div", { class: "case" + (c.ok === false ? " bad" : "") }, h("div", { class: "ct" }, h("b", { text: c.description || c.name }),
          h("span", { class: "res " + (c.ok ? "ok" : c.ok === false ? "fail" : ""), text: c.ok ? "통과" : c.ok === false ? "실패" : "미실행" })),
        c.cause ? h("div", { class: "cline cause" }, h("span", { class: "k", text: "원인" }),
          h("span", { text: c.cause.title + (c.cause.detail ? " — " + c.cause.detail : "") })) : null,
        // 기본은 이름·결과(실패면 원인)만 — 무엇을 확인했는지·화면·원문은 눌러서 본다 (화면 사진은 펼칠 때 불러온다)
        caseDetails(c)))));
  }
  const help = sp.gates_help || {};
  const stale = (D.gates || []).some((g) => g.now_ok === false);
  p.append(h("div", { class: "sec", style: "margin-top:18px" }, h("h4", { text: D.accepted_done ? "완료 — 수락함" : D.gates_at ? "완료할 때 확인한 조건" : "함께 확인한 완료 조건" }),
    stale ? h("div", { class: "gnote" }, ico("alert"), h("span", { text: "완료한 뒤 대상 폴더의 코드가 바뀌어, 표시한 조건 중 일부는 지금 코드 기준으로는 다시 확인해야 합니다." })) : null,
    ...(D.gates || []).map((g) => h("div", { class: "gate2" + (g.ok ? " ok" : "") }, g.ok ? ico("check", "gi") : h("i"),
      h("div", {}, h("div", { text: g.label || g.name }), help[g.name] ? h("div", { class: "f", text: help[g.name] }) : null,
        g.by_accept ? h("div", { class: "f", text: "수락으로 완료" }) : null,
        g.now_ok === false ? h("div", { class: "f warn", text: "지금 코드 기준으로는 다시 확인 필요" }) : null)))));
}

// ---------------------------------------------------------------- 갱신·단축키
// 화면에 보이는 내용의 지문을 함께 보낸다 — 그 사이 에이전트가 바꿨으면 승인되지 않고 새 내용을 다시 보여 준다
async function approveShown(what) {
  // 처음에 한 번 확인: 기준과 계획이 함께 떠 있으면 한 번에 승인한다 (각각 보인 내용 그대로)
  const D = S.detail;
  const reviews = D.reviews && D.reviews.count, plan = D.plan && D.plan.steps && D.plan.steps.length && !D.plan.approved;
  let ok = true;
  if (reviews && (what === "reviews" || what === "both")) ok = (await call("approveReviews", S.current, D.review_sig || "")).ok && ok;
  const picks = S.picks && S.picks.sig === D.plan_sig ? S.picks.v : {};
  let changed = [];
  if (plan && ok && (what === "plan" || what === "both")) {
    const r = await call("approvePlanWith", S.current, D.plan_sig || "", JSON.stringify(picks));
    ok = r.ok && ok; changed = (r.data && r.data.changed) || [];
  }
  if (ok) toast((reviews && plan && what === "both" ? "기준과 계획을 승인했습니다 — 끝날 때까지 진행합니다" : "승인했습니다")
    + (changed.length ? ` · 결정 ${changed.length}개를 바꿔 전달했습니다` : ""));
  refresh();
}
// 에이전트가 실제로 일하는 중인가 — 실행기가 살아 있어도 사용자를 기다리면(내 차례) 일하는 중이 아니다
function isBusy() { const D = S.detail; return !!D && !!D.running && D.tone !== "turn"; }
function gateOpen() { const D = S.detail; return !!D && (!D.running || ["waiting_approval", "waiting_plan"].includes(D.run.status) || !!S.edit); }
function bothPending() { const D = S.detail; return !!(D && D.reviews && D.reviews.count && D.plan && D.plan.steps && D.plan.steps.length && !D.plan.approved); }
async function refresh() {
  const lr = await call("listRuns"); if (lr.ok) S.runs = lr.data;
  renderSide();
  if (S.current) {
    const want = S.current;
    const r = await call("runDetail", want);
    if (r.ok && want === S.current && r.data.run.id === S.current) { S.detail = r.data; renderRun(); }  // 늦게 온 다른 실행의 응답은 버린다
    // 백그라운드 프로세스 — 진행 중이면 5초, 끝났으면 남은 서버만 가끔(1분) 본다 (모든 프로세스를 훑는 일이라)
    if (Date.now() - (S.procsAt || 0) > (S.detail && S.detail.running ? 5000 : 60000) || S.procsFor !== want) {
      S.procsFor = want;
      S.procsAt = Date.now();
      call("runProcs", want).then((x) => { if (x.ok && want === S.current) { const sig = JSON.stringify(x.data); if (sig !== S.procsSig) { S.procsSig = sig; S.procs = { run: want, rows: x.data }; drawProcs(); } } });
    }
  } else if (!$("#homeText")) renderHome();
}
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && (e.key === "n" || e.key === "N")) { e.preventDefault(); goHome(); }
  if (e.key === "Escape" && openPop) { closePop(); return; }
  if (e.ctrlKey && e.key === "Enter" && !S.current && !S.preparing) { e.preventDefault(); create(); return; }
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || !S.detail) return;  // 입력 중의 Ctrl+Enter 는 보내기 — 승인이 아님, Esc 도 멈춤이 아님
  if (e.key === "Escape" && S.detail.running) { call("stop", S.current).then(refresh); return; }
  if (e.ctrlKey && e.key === "Enter") {
    if (S.edit || !gateOpen()) return;  // 보이지 않는 카드를 단축키로 승인하지 않게
    if (bothPending()) approveShown("both");
    else if (S.detail.reviews && S.detail.reviews.count) approveShown("reviews");
    else if (S.detail.plan && S.detail.plan.steps && !S.detail.plan.approved) approveShown("plan");
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;  // Ctrl+A(전체 선택) 등이 권한 답이 되지 않게
  const perm = (S.detail.asks || []).find((a) => a.kind === "permission");
  if (perm && ["y", "n"].includes(e.key.toLowerCase())) {  // '항상 허용'은 단축키로 주지 않는다 — 버튼으로만
    const data = { y: { allow: true }, n: { allow: false } }[e.key.toLowerCase()];
    call("answer", S.current, perm.id, JSON.stringify(data)).then(refresh);
  }
});
function goHome() { closePop(); delete S.draft.name; S.edit = null; S.current = null; S.detail = null; S.sig = { feed: "", right: "", side: "", cbar: "" }; S.preparing = false; renderSide(); renderHome(); }
$("#newbtn").addEventListener("click", goHome);
$("#runq").addEventListener("input", (e) => { S.runQ = e.target.value; renderSide(); });
$("#runq").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.target.value = ""; S.runQ = ""; renderSide(); e.target.blur(); } });

new QWebChannel(qt.webChannelTransport, async (ch) => {
  api = ch.objects.api;
  api.created.connect((raw) => {
    const r = JSON.parse(raw); S.preparing = false;
    if (r.error) { toast(r.error); renderHome(); return; }
    S.draft.request = ""; S.draft.attachments = []; delete S.draft.name; delete S.draft.new_branch; openRun(r.run_id).then(() => r.start && startRun());
  });
  const b = await call("bootstrap");
  if (b.ok) applyLang(b.data.i18n);
  if (b.ok) { S.agents = b.data.agents; S.runs = b.data.runs; S.kinds = b.data.kinds; S.policy = b.data.default_policy; S.permModes = b.data.permission_modes || []; S.sandbox = b.data.sandbox || { ready: false }; $("#ver").textContent = "v" + b.data.version;
    const def = b.data.defaults || {};  // 지난번 새 작업의 선택
    S.defaults = def;
    for (const k of ["agent", "model", "effort", "max_rounds", "plan_approval", "confirm_at", "auth"]) if (def[k] != null && def[k] !== "") S.draft[k] = def[k];
    if (def.policy) S.draft.policy = { ...def.policy };
    pickReadyAgent(); }
  const sc = await call("shortcutStatus"); if (sc.ok) S.shortcut = sc.data;
  renderSide(); renderHome();
  api.dataReady.connect((raw) => { const r = JSON.parse(raw); if (r.ok && openPop && openPop.querySelector(".datapop")) { S.data = r.data; drawData($("#databtn")); } });
  $("#langbtn").addEventListener("click", (e) => { e.stopPropagation(); if (openPop && openPop.querySelector(".pt") && openPop.querySelector(".pt").textContent === t("화면 언어")) { closePop(); return; } langPop(e.currentTarget); });
  $("#usagebtn").addEventListener("click", (e) => { e.stopPropagation(); if (openPop && openPop.querySelector(".usagepop")) { closePop(); return; } usagePop(e.currentTarget); });
  $("#databtn").addEventListener("click", (e) => { e.stopPropagation(); if (openPop && openPop.querySelector(".datapop")) { closePop(); return; } dataPop(e.currentTarget); });
  if (api.exportDone) api.exportDone.connect((raw) => { const r = JSON.parse(raw); toast(r.ok ? "PDF 를 만들었습니다 — " + r.path : "PDF 를 만들지 못했습니다", 5000); if (r.ok) call("openFolder", r.folder); });
  if (api.filesDropped) api.filesDropped.connect((raw) => addFiles(JSON.parse(raw)));
  if (api.updateFound) {  // 새 버전 — 버전 옆에 작게, 누르면 릴리스 노트 (업데이트 명령은 툴팁)
    api.updateFound.connect((raw) => { const r = JSON.parse(raw), v = $("#ver"); if (!v || document.querySelector(".upd")) return;
      v.after(h("a", { class: "upd", href: r.url || "#", title: "릴리스 노트 열기 — 업데이트: pipx upgrade beom-workbench", text: `새 버전 v${r.version}` })); });
    setTimeout(() => call("checkUpdate"), 4000);
  }
  api.linkChecked.connect((raw) => { const r = JSON.parse(raw); S.links = S.links || {};
    S.links[r.url] = { state: r.ok ? "ok" : r.needs_login ? "login" : "error", error: r.error };
    if (r.needs_login) { toast("로그인이 필요한 링크입니다 — 앱 브라우저 창에서 로그인해 주세요", 7000); loginLink(r.url); }
    if (!S.current) renderHome(); });
  api.linkSaved.connect((raw) => { const r = JSON.parse(raw); if (!r.path) return; const d = S.draft;
    d.attachments = (d.attachments || []).map((x) => (x === r.url ? r.path : x)); delete (S.links || {})[r.url];
    toast("로그인한 페이지의 내용을 첨부했습니다", 5000); if (!S.current) renderHome(); });
  api.agentsChanged.connect((raw) => { S.agents = JSON.parse(raw); pickReadyAgent(); renderSide(); if (!S.current) renderHome(); });
  // 진행 중일 때만 자주(1.5초), 한가하면 5초, 창이 가려져 있으면 쉰다 — 앱이 쓸데없이 일하지 않게
  const tick = async () => {
    if (!document.hidden) { try { await refresh(); } catch (e) { /* 다음 번에 다시 */ } }
    const busy = (S.detail && S.detail.running) || S.runs.some((r) => r.tone === "run");
    setTimeout(tick, document.hidden ? 4000 : busy ? 1500 : 5000);
  };
  setTimeout(tick, 1500);
  // 에이전트 상태는 창으로 돌아왔을 때와 5분마다만 (확인할 때마다 CLI 를 띄운다 — 실행기는 5분 안이면 마지막 값을 준다)
  const agentsNow = () => Promise.all([call("agentStatus"), call("sandboxStatus"), call("engineStatus")]).then(([r, s, e]) => {
    if (r.ok) S.agents = r.data; if (s.ok) S.sandbox = s.data; if (e.ok && e.data) S.engines = e.data; renderSide(); });
  // 분석 엔진 확인은 뒤에서 (프로그램을 띄워 본다) — 값이 나올 때까지 몇 번 다시 묻는다
  const engines = (n) => call("engineStatus").then((e) => { if (e.ok && e.data) { S.engines = e.data; renderSide(); } else if (n > 0) setTimeout(() => engines(n - 1), 2000); });
  setTimeout(() => engines(10), 1500);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); agentsNow(); } });
  setInterval(() => { if (!document.hidden) agentsNow(); }, 300000);
  window.addEventListener("focus", agentsNow);  // 로그인 터미널 등 다른 창에서 돌아왔을 때
});
