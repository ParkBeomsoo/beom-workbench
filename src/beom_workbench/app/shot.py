"""화면 확인 — 페이지를 보이지 않는 창(Qt WebEngine)에 띄워 글자·요소가 있는지 보고 스크린샷을 남긴다.

    python -m beom_workbench.app.shot  < '{"pages": [{"url": "...", "out": "a.png", "width": 1280, "height": 800,
                                                    "visible": ["주문하기"], "selectors": ["form button"], "wait_ms": 800}]}'

명세는 표준 입력으로 받는다 — 명령줄 인자로 주면 화면 케이스가 많을 때 Windows 명령줄 한도(32K 글자)를 넘어 실행 자체가
안 된다 (WinError 206). 인자로 준 것도 받는다 (손으로 한 장 찍을 때).

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


# 화면 점검 — 대비·누르는 곳 크기·넘침·잘림·겹침·글꼴 등. 앱 자신의 화면 점검(tests/ui_audit)도 같은 것을 쓴다
AUDIT_JS = r"""(() => {
      const vw = innerWidth, mobile = vw <= 480, cap = (a) => a.slice(0, 15);
      const vis = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && parseFloat(s.opacity) > 0.05; };
      const name = (el) => el.id ? "#" + el.id : el.tagName.toLowerCase() + (el.classList.length ? "." + [...el.classList].slice(0, 2).join(".") : "");
      // 글자 단위로 자른다 — UTF-16 단위로 자르면 이모지가 반쪽(외톨이 서로게이트)이 되어 결과 JSON 전체를 쓰지 못했다
      const txt = (el) => [...(el.innerText || el.value || el.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ")].slice(0, 30).join("");
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
      const t0 = performance.now(), white = { r: 255, g: 255, b: 255 };
      // 글자 밑에 실제로 그려진 바탕 — 조상이 아닌 요소(겹쳐 올라온 머리 띠 등) 위에 놓인 글자는 조상의 바탕만 보면 대비를 잘못 잰다
      // (실험 148: 어두운 머리 띠에 걸친 회색 안내 글을 '통과'로 쟀다). 화면에 보이는 자리에서만 잰다
      const paintedUnder = (el) => {
        const r = el.getBoundingClientRect(), x = r.left + Math.min(r.width / 2, 24), y = r.top + Math.min(r.height / 2, 10);
        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
        for (const e of document.elementsFromPoint(x, y)) {
          if (e === el || el.contains(e)) continue;
          const c = parse(getComputedStyle(e).backgroundColor);
          if (!c || c.a <= 0.5) continue;
          return e.contains(el) ? null : bgOf(e);  // 처음 만난 칠한 요소가 조상이면 원래 계산 그대로
        }
        return null;
      };
      const ratioOf = (a, b) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05);
      const mix = (c, b, a) => ({ r: c.r * a + b.r * (1 - a), g: c.g * a + b.g * (1 - a), b: c.b * a + b.b * (1 - a) });
      const hex = (c) => "#" + [c.r, c.g, c.b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
      const outerOf = (el) => (el.parentElement && bgOf(el.parentElement)) || null;
      // 입력칸 (글자를 넣는 칸·고르는 칸) — 체크박스·버튼 모양 입력은 뺀다
      const FIELD = (el) => /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) && !/^(checkbox|radio|range|color|file|button|submit|reset|image|hidden)$/.test(el.type);
      const BTN = "button,[role=button],input[type=button],input[type=submit],input[type=reset],a[class*=btn],a[class*=button]";
      const out = { contrast: [], small_text: [], tiny_targets: [], small_targets: [], clipped: [], overlap: [], broken_wrap: [], overflow: null,
                    control_border: [], focus_hidden: [], placeholder: [], button_mismatch: [], font_missing: [], font_fallback: [] };
      const fonts = new Set(), sizes = new Set(), colors = new Set(), hits = [], fields = [], ctrls = [], btns = {}, fontUse = {};
      // 팝업이 열려 있으면 그 뒤(어둡게 가린 배경)는 지금 누를 수도 읽을 수도 없다 — 재지 않는다 (겹침·작은 버튼으로 잘못 알렸다)
      const layers = [...(document.body ? document.body.querySelectorAll("*") : [])].filter((e) => {
        if (!vis(e)) return false;
        const st = getComputedStyle(e); if (st.position !== "fixed") return false;
        const r = e.getBoundingClientRect(); return r.width >= vw * 0.9 && r.height >= innerHeight * 0.9; });
      const zOf = (e) => parseInt(getComputedStyle(e).zIndex) || 0;
      const POP = "[role=dialog],[role=alertdialog],[aria-modal=true],dialog,[class*=modal],[class*=popup],[class*=dialog]";
      const pops = layers.filter((e) => e.matches(POP) || e.querySelector(POP) || /modal|popup|dim|overlay|backdrop|dialog/i.test(e.className || ""));
      const layer = pops.length ? pops.reduce((a, b) => (zOf(b) >= zOf(a) ? b : a)) : null;
      const behind = (el) => layer && !layer.contains(el) && !el.contains(layer);
      // 입력칸의 경계 (WCAG 1.4.11) — 가장 또렷한 쪽 테두리, 또는 칸 바탕이 바깥 바탕과 3:1 이어야 칸이 어디 있는지 보인다
      const edge = (el, s) => {
        const outer = outerOf(el), inner = bgOf(el); if (!outer || !inner) return null;
        let best = ratioOf(inner, outer), line = "테두리 없음";
        for (const side of ["Top", "Right", "Bottom", "Left"]) {
          if (parseFloat(s["border" + side + "Width"]) < 1 || /none|hidden/.test(s["border" + side + "Style"])) continue;
          const c = parse(s["border" + side + "Color"]); if (!c || c.a <= 0.05) continue;
          const r = ratioOf(mix(c, outer, c.a), outer);
          if (line === "테두리 없음" || r > best) line = "테두리 " + hex(c);
          best = Math.max(best, r);
        }
        return { ratio: +best.toFixed(2), line };
      };
      // 안내 글자(placeholder) — 칸이 비어 보일 때만, 칸과 그 위 요소의 투명도까지 넣어 잰다 (opacity .5 인 꺼진 칸)
      const hint = (el) => {
        if (!el.placeholder || el.value || !el.matches(":placeholder-shown")) return null;
        const ps = getComputedStyle(el, "::placeholder"), fg = parse(ps.color), inner = bgOf(el), outer = outerOf(el) || inner;
        if (!fg || !inner) return null;
        let op = 1; for (let e = el; e && e.nodeType === 1; e = e.parentElement) op *= parseFloat(getComputedStyle(e).opacity);
        const po = parseFloat(ps.opacity), text = mix(mix(fg, inner, fg.a * (isNaN(po) ? 1 : po)), outer, op);
        return { ratio: +ratioOf(text, mix(inner, outer, op)).toFixed(2) };
      };
      // 포커스가 보이나 — 만든 사람이 외곽선(outline)을 없앤 요소만 본다 (기본 포커스 표시는 브라우저가 그린다). 화면 엔진의 페이지는
      // 창 포커스가 없어 :focus 스타일이 걸리지 않는다 — 그래서 페이지 스타일의 :focus·:focus-visible 규칙에 적힌 대신할 표시를 읽어 잰다
      const focusRules = [], withinRules = [], noSel = [], strip = (one) => one.replace(/:focus(-visible|-within)?/g, "").trim() || "*";
      // @media·@layer·@supports 안의 규칙까지 (Tailwind 4 처럼 @layer 안에 outline 을 없애는 규칙이 있다)
      const walk = (rules, depth) => {
        for (const r of rules || []) {
          if (r.cssRules && !r.selectorText && depth < 4) { walk(r.cssRules, depth + 1); continue; }
          if (!r.selectorText || !r.style) continue;
          const parts = r.selectorText.split(",");
          if (/none|hidden/.test(r.style.outlineStyle) || /^(0|none)\b/.test(String(r.style.outline || "").trim()) || r.style.outlineWidth === "0px")
            for (const one of parts) noSel.push(strip(one));
          for (const one of parts) if (/:focus(?!-within)/.test(one)) focusRules.push({ sel: strip(one), st: r.style });
          for (const one of parts) if (/:focus-within/.test(one)) withinRules.push({ sel: strip(one), st: r.style });
        }
      };
      for (const sh of document.styleSheets) {
        let rules; try { rules = sh.cssRules; } catch (e) { continue; }
        walk(rules, 0);
      }
      const matches = (el, sel) => { try { return el.matches(sel); } catch (e) { return false; } };
      const noOutline = (el) => /outline\s*:\s*(0|none)/.test(el.getAttribute("style") || "") || noSel.some((sel) => matches(el, sel));
      // 테두리 없는 칸이 테두리 있는 상자 안에 있으면(대화 입력창·검색 막대) 그 상자가 칸의 경계다 — 세 단계 위까지
      const frameOf = (el) => {
        const own = getComputedStyle(el);
        if (["Top", "Right", "Bottom", "Left"].some((sd) => parseFloat(own["border" + sd + "Width"]) >= 1 && !/none|hidden/.test(own["border" + sd + "Style"]))) return null;
        const r = el.getBoundingClientRect();
        for (let a = el.parentElement, i = 0; a && i < 3; a = a.parentElement, i++) {
          const st = getComputedStyle(a), q = a.getBoundingClientRect();
          if (q.width > r.width * 3 || q.height > r.height * 8) break;  // 칸을 감싼 상자이지, 화면 전체 판이 아니다
          if (["Top", "Right", "Bottom", "Left"].some((sd) => parseFloat(st["border" + sd + "Width"]) >= 1 && !/none|hidden/.test(st["border" + sd + "Style"])))
            return a;
        }
        return null;
      };
      const ring = (el) => {
        if (!noOutline(el)) return null;
        const fr = frameOf(el);
        if (fr) {  // 상자의 :focus-within 표시가 있으면 그것이 포커스 표시다
          const outer = outerOf(fr) || white, cs = getComputedStyle(fr);
          // 규칙에 적힌 값은 var(--x) 그대로다 — 그 상자에서 풀린 색으로 바꿔 잰다
          const hexRgb = (h) => { const x = h.length <= 4 ? h.split("").map((c) => c + c).join("") : h;
            return `rgba(${parseInt(x.slice(0, 2), 16)}, ${parseInt(x.slice(2, 4), 16)}, ${parseInt(x.slice(4, 6), 16)}, ${x.length >= 8 ? parseInt(x.slice(6, 8), 16) / 255 : 1})`; };
          const rv = (v) => String(v || "").replace(/var\((--[\w-]+)[^)]*\)/g, (m0, n) => cs.getPropertyValue(n).trim() || m0)
            .replace(/#([0-9a-f]{3,8})\b/gi, (m0, h) => hexRgb(h));
          for (const { sel, st: raw } of withinRules) {
            if (!matches(fr, sel)) continue;
            // var() 가 든 줄임 속성은 낱개 속성(borderTopColor)이 비어 나온다 — 규칙 글에서 직접 읽는다
            const ct = raw.cssText || "", pick = (re) => (ct.match(re) || [])[1] || "";
            const st = { boxShadow: rv(raw.boxShadow || pick(/box-shadow\s*:\s*([^;]+)/)),
                         borderColor: rv(raw.borderColor || raw.borderTopColor || pick(/border(?:-top)?-color\s*:\s*([^;]+)/) || pick(/border\s*:[^;]*?(var\([^)]+\)|#[0-9a-f]{3,8}\b|rgba?\([^)]+\))/i)),
                         outlineColor: rv(raw.outlineColor), outlineStyle: raw.outlineStyle };
            for (const m of String(st.boxShadow || "").matchAll(/rgba?\([^)]+\)|#[0-9a-f]{3,8}\b/gi)) {
              const c = parse(m[0]); if (c && c.a > 0.05 && ratioOf(mix(c, outer, c.a), outer) >= 1.3) return null;
            }
            const bc = parse(st.borderColor); if (bc && bc.a > 0.05 && ratioOf(mix(bc, outer, bc.a), outer) >= 1.5) return null;
            const oc = parse(st.outlineColor); if (st.outlineStyle && !/none|hidden/.test(st.outlineStyle) && oc && oc.a > 0.05) return null;
          }
        }
        const outer = outerOf(el) || white, base = getComputedStyle(el), vs = (c) => ratioOf(mix(c, outer, c.a), outer);
        // 규칙에 var(--x) 가 있으면 그 칸에서 풀린 색으로 — 줄임 속성(outline: 2px solid var(--x))은 낱개 속성이 비어 나와 규칙 글에서 읽는다
        // (전에는 변수로 쓴 포커스 표시를 '없음'으로 잘못 잡았다)
        const hexRgb2 = (h) => { const x = h.length <= 4 ? h.split("").map((c) => c + c).join("") : h;
          return `rgba(${parseInt(x.slice(0, 2), 16)}, ${parseInt(x.slice(2, 4), 16)}, ${parseInt(x.slice(4, 6), 16)}, ${x.length >= 8 ? parseInt(x.slice(6, 8), 16) / 255 : 1})`; };
        const rvEl = (v) => String(v || "").replace(/var\((--[\w-]+)[^)]*\)/g, (m0, n) => base.getPropertyValue(n).trim() || m0)
          .replace(/#([0-9a-f]{3,8})\b/gi, (m0, h) => hexRgb2(h));
        const seen = [[0, "표시 없음"]];
        for (const { sel, st: raw } of focusRules) {
          if (!matches(el, sel)) continue;
          const ct = raw.cssText || "", pick = (re) => (ct.match(re) || [])[1] || "";
          const oShort = pick(/(?:^|;)\s*outline\s*:\s*([^;]+)/);
          const colorIn = (t) => (rvEl(t).match(/rgba?\([^)]+\)/) || [])[0] || "";
          const st = { outlineStyle: raw.outlineStyle || ((oShort.match(/\b(solid|dashed|dotted|double|auto|groove|ridge|inset|outset)\b/) || [])[1]) || (/\bnone\b/.test(oShort) ? "none" : ""),
                       outlineColor: rvEl(raw.outlineColor) || colorIn(oShort), outlineWidth: raw.outlineWidth || (oShort.match(/(\d+(?:\.\d+)?)px/) || [])[1] || "",
                       boxShadow: rvEl(raw.boxShadow || pick(/box-shadow\s*:\s*([^;]+)/)),
                       borderBottomColor: rvEl(raw.borderBottomColor || pick(/border-bottom-color\s*:\s*([^;]+)/) || colorIn(pick(/border-bottom\s*:\s*([^;]+)/))),
                       borderColor: rvEl(raw.borderColor || pick(/border-color\s*:\s*([^;]+)/) || colorIn(pick(/(?:^|;)\s*border\s*:\s*([^;]+)/))),
                       textDecoration: raw.textDecoration, textDecorationLine: raw.textDecorationLine, backgroundColor: rvEl(raw.backgroundColor) };
          const oc = parse(st.outlineColor);
          if (st.outlineStyle && !/none|hidden/.test(st.outlineStyle) && oc && oc.a > 0.05 && (parseFloat(st.outlineWidth) || 1) >= 1)
            seen.push([vs(oc), "외곽선 " + hex(oc)]);
          for (const m of String(st.boxShadow || "").matchAll(/rgba?\([^)]+\)|#[0-9a-f]{3,8}\b/gi)) {
            const c = parse(m[0]); if (c && c.a > 0.05) seen.push([vs(c), "그림자 " + hex(c)]);
          }
          const bc = parse(st.borderBottomColor || st.borderColor), ac = parse(base.borderBottomColor);
          if (bc && ac && (parseFloat(base.borderBottomWidth) || 0) >= 1) {  // 테두리 색만 바뀌는 표시 — 밝기나 색이 뚜렷이 달라져야 보인다
            const nb = mix(bc, outer, bc.a), ob = mix(ac, outer, ac.a);
            if (ratioOf(nb, ob) >= 1.5 || Math.hypot(nb.r - ob.r, nb.g - ob.g, nb.b - ob.b) >= 80) seen.push([vs(bc), "테두리 " + hex(bc)]);
          }
          if (/underline/.test(st.textDecoration || st.textDecorationLine || "") && !/underline/.test(base.textDecorationLine)) seen.push([3, "밑줄"]);
          const bb = parse(st.backgroundColor), ab = parse(base.backgroundColor);
          if (bb && ab) seen.push([ratioOf(mix(bb, outer, bb.a), mix(ab, outer, ab.a)), "바탕 " + hex(bb)]);
        }
        const best = seen.reduce((x, y) => (y[0] > x[0] ? y : x));
        return best[0] >= 3 ? null : { el: name(el), text: txt(el), ratio: +best[0].toFixed(2), cue: best[1] };
      };
      const rings = (els) => {
        const kinds = new Set(), found = [];
        for (const el of els) {
          const k = el.tagName + "." + (el.classList[0] || "") + "." + (el.type || "");
          if (kinds.has(k) || kinds.size >= 12) continue;
          kinds.add(k);
          const r = ring(el); if (r) found.push(r);
        }
        return found;
      };
      // 같은 부품(첫 클래스가 같은 버튼)이 한 화면에서 다른 크기·굵기 — font: inherit 가 둘레(제목 등)의 글자를 받아 온 흔적
      const mismatch = (items) => {
        const big = items.reduce((x, y) => (y.fs > x.fs ? y : x)), small = items.reduce((x, y) => (y.fs < x.fs ? y : x));
        if (big.fs - small.fs > 2) return { a: big, b: small };
        const other = items.find((x) => x.fw !== items[0].fw);
        return other ? { a: items[0], b: other } : null;
      };
      for (const el of (document.body ? document.body.querySelectorAll("*") : [])) {
        if (!vis(el) || ["SCRIPT", "STYLE", "SVG", "PATH"].includes(el.tagName.toUpperCase()) || behind(el)) continue;
        const s = getComputedStyle(el);
        const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
        const off = el.disabled || el.closest("[disabled],[aria-disabled='true']");
        if (own) {
          const fs = parseFloat(s.fontSize), fw = parseInt(s.fontWeight) || 400;
          const ff = s.fontFamily.split(",")[0].replace(/["']/g, "").trim();
          fonts.add(ff); sizes.add(Math.round(fs)); colors.add(s.color);
          (fontUse[s.fontFamily] = fontUse[s.fontFamily] || { n: 0, el }).n++;
          const fg = parse(s.color), bg = paintedUnder(el) || bgOf(el);
          if (fg && bg && fg.a > 0.05 && !off) {
            const ratio = (Math.max(lum(fg), lum(bg)) + 0.05) / (Math.min(lum(fg), lum(bg)) + 0.05);
            const need = fs >= 24 || (fs >= 18.66 && fw >= 700) ? 3 : 4.5;
            if (ratio < need) out.contrast.push({ el: name(el), text: txt(el), ratio: +ratio.toFixed(2), need });
          }
          if (fs < 12) out.small_text.push({ el: name(el), text: txt(el), size: fs });
          // 말줄임(…)으로 줄이고 전체 글을 title 로 보여 주면 잘린 것이 아니다 (좁은 목록의 긴 이름)
          if (/(hidden|clip)/.test(s.overflow + s.overflowX) && el.scrollWidth > el.clientWidth + 1 && !(s.textOverflow === "ellipsis" && (el.title || "").trim())) out.clipped.push({ el: name(el), text: txt(el) });
          // 한두 글자씩 줄이 바뀜 (칸이 좁아 '꽃다/발'처럼 낱말이 쪼개짐) — 글 줄 상자를 실제로 센다
          for (const n of el.childNodes) {
            if (n.nodeType !== 3) continue;
            const t = n.textContent.replace(/\s+/g, "");
            if (t.length < 3) continue;
            const rg = document.createRange(); rg.selectNodeContents(n);
            // 한 줄 안에서도 대체 글꼴 조각은 위치가 조금씩 다르다 — 줄 높이의 절반 넘게 떨어져야 다른 줄
            const lh = parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.2;
            const tops = [...rg.getClientRects()].filter((r) => r.width > 0).map((r) => r.top).sort((a, b) => a - b);
            let lines = tops.length ? 1 : 0;
            for (let i = 1; i < tops.length; i++) if (tops[i] - tops[i - 1] > lh * 0.5) lines++;
            if (lines >= 2 && t.length / lines < 3) out.broken_wrap.push({ el: name(el), text: txt(el), lines });
            break;
          }
        }
        if (el.matches("a[href],button,input:not([type=hidden]),select,textarea,[role=button],[onclick],summary") && !off) {
          let r = el.getBoundingClientRect();
          // 체크박스·라디오는 연결된 글자(label)를 눌러도 고른다 — 실제로 누르는 곳은 둘을 합친 영역이다 (실험 129: 상자만 재어
          // '너무 작음'이 나오자 체크박스를 Ctrl+클릭 다중 선택 상자로 바꿔 쓰기 더 나빠졌다)
          if (el.matches("input[type=checkbox],input[type=radio]") && el.labels && el.labels.length) {
            let [l, t, rr, b] = [r.left, r.top, r.right, r.bottom];
            for (const lab of el.labels) { const q = lab.getBoundingClientRect(); if (q.width && q.height) { l = Math.min(l, q.left); t = Math.min(t, q.top); rr = Math.max(rr, q.right); b = Math.max(b, q.bottom); } }
            r = { left: l, top: t, right: rr, bottom: b, width: rr - l, height: b - t };
          }
          const inline = s.display === "inline" && el.closest("p,li,td,blockquote,dd,figcaption");  // 문장 안 링크는 크기 기준에서 뺀다 (WCAG 예외)
          if (!inline) {
            const sz = { el: name(el), text: txt(el), w: Math.round(r.width), h: Math.round(r.height) };
            if (r.width < 24 || r.height < 24) out.tiny_targets.push(sz);
            else if (mobile && (r.width < 44 || r.height < 44)) out.small_targets.push(sz);
            if (hits.length < 150) hits.push([el, r]);
          }
          if (ctrls.length < 200) ctrls.push(el);
        }
        if (FIELD(el)) fields.push([el, s, off]);
        if (el.classList.length && el.matches(BTN))
          (btns[el.classList[0]] = btns[el.classList[0]] || []).push({ el: name(el), text: txt(el), fs: Math.round(parseFloat(s.fontSize) * 10) / 10, fw: parseInt(s.fontWeight) || 400 });
      }
      for (const [el, s, off] of fields) {
        if (!off) {
          const fr = frameOf(el), e = fr ? edge(fr, getComputedStyle(fr)) : edge(el, s);
          if (e && e.ratio < 3) out.control_border.push({ el: name(fr || el), text: txt(el) || el.placeholder || "", ...e });
        }
        const h = hint(el); if (h && h.ratio < 4.5) out.placeholder.push({ el: name(el), text: (el.placeholder || "").trim().replace(/\s+/g, " ").slice(0, 30), ratio: h.ratio, off: !!off });
      }
      out.focus_hidden = rings(ctrls);
      for (const [cls, items] of Object.entries(btns)) { const m = mismatch(items); if (m) out.button_mismatch.push({ cls, ...m }); }
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
      // 'AI 티' — 모델이 기본값으로 만드는 모습 (여러 디자인 도구의 금지 목록이 겹치는 것만): 제목 위 작은 라벨, 둥근 상자 한쪽의 색 띠,
      // 그라데이션 글자, 이모지 아이콘, 카드 안 카드, 같은 모양 카드 나열, 장식 유리·빛 번짐, 굵은 오프셋 그림자, 튀는 움직임, 시스템
      // 글꼴 제목, 가운데 정렬 남발. 취향이라 '고칠 것'과 따로 알린다.
      const tells = [], seenT = new Set();
      const tell = (kind, el, detail) => {
        const k = kind + "|" + (el ? name(el) : "");
        if (seenT.has(k) || tells.filter((t) => t.kind === kind).length >= 3) return;
        seenT.add(k); tells.push({ kind, el: el ? name(el) : "", text: el ? txt(el) : "", detail: detail || "" }); };
      const everyEl = [...(document.body ? document.body.querySelectorAll("*") : [])]
        .filter((e) => vis(e) && !behind(e) && !["SCRIPT", "STYLE", "NOSCRIPT"].includes(e.tagName.toUpperCase()));
      const ownText = (e) => [...e.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();
      const solid = (c) => c && c.a > 0.3;
      const boxy = (e, st) => {  // 따로 떨어진 상자로 보이나 — 네 변 테두리·그림자·다른 바탕 중 하나 + 모서리나 안쪽 여백
        const bw = ["Top", "Right", "Bottom", "Left"].filter((x) => parseFloat(st["border" + x + "Width"]) >= 1 && !/none|hidden/.test(st["border" + x + "Style"])).length;
        if (e.matches("input,button,select,textarea,a,label,summary,[role=button]") || !e.firstElementChild) return false;  // 입력·버튼은 상자가 아니다
        const outer = outerOf(e), r = e.getBoundingClientRect(), inner = bgOf(e);
        const filled = solid(parse(st.backgroundColor)) && outer && inner && ratioOf(inner, outer) > 1.04;
        return r.width >= 120 && r.height >= 48 && (bw === 4 || st.boxShadow !== "none" || filled)
          && (parseFloat(st.borderTopLeftRadius) >= 4 || parseFloat(st.paddingTop) >= 12);
      };
      const EMOJI = /[\u{1F300}-\u{1FAFF}\u{1F000}-\u{1F2FF}☀-➿⭐✅]/u;
      const heads = everyEl.filter((e) => /^H[1-3]$/.test(e.tagName.toUpperCase()) || (parseFloat(getComputedStyle(e).fontSize) >= 24 && ownText(e).length >= 2));
      for (const hd of heads) {
        const p = hd.previousElementSibling, hs = parseFloat(getComputedStyle(hd).fontSize);
        if (!p || !vis(p)) continue;
        const ps = getComputedStyle(p), t = (p.textContent || "").trim(), fs = parseFloat(ps.fontSize);
        if (t && t.length <= 30 && fs <= 14.5 && fs <= hs * 0.62 && p.children.length <= 1
            && (ps.textTransform === "uppercase" || parseFloat(ps.letterSpacing) >= 0.5 || (parseInt(ps.fontWeight) || 400) >= 600))
          tell("eyebrow", p, "제목 '" + (hd.textContent || "").trim().slice(0, 20) + "' 위");
      }
      const cards = [];
      for (const e of everyEl) {
        const st = getComputedStyle(e), r = e.getBoundingClientRect();
        // 한쪽 색 띠 — 한 변만 굵은 테두리, 또는 상자 끝에 붙은 가는 막대(자식·::before·::after)
        const sides = ["Top", "Right", "Bottom", "Left"].map((x) => [x, parseFloat(st["border" + x + "Width"]) || 0]);
        const thick = sides.filter(([, v]) => v >= 3), thin = sides.filter(([, v]) => v <= 1.5);
        if (thick.length === 1 && thin.length === 3 && thick[0][0] !== "Bottom" && r.height >= 32
            && solid(parse(st["border" + thick[0][0] + "Color"])) && (parseFloat(st.borderTopLeftRadius) >= 3 || boxy(e, st)))
          tell("side_stripe", e, (thick[0][0] === "Top" ? "위쪽" : "한쪽") + " 테두리 " + thick[0][1] + "px");
        for (const pe of ["::before", "::after"]) {
          const ps = getComputedStyle(e, pe);
          if (!ps.content || ps.content === "none" || ps.content === "normal" || ps.position !== "absolute") continue;
          const pw = parseFloat(ps.width), ph = parseFloat(ps.height);
          if (pw > 0 && pw <= 8 && (ph >= r.height * 0.5 || (ps.top === "0px" && ps.bottom === "0px")) && solid(parse(ps.backgroundColor)) && r.height >= 32)
            tell("side_stripe", e, "가장자리 막대(" + pe + ")");
        }
        const par = e.parentElement;
        if (par && r.width > 0 && r.width <= 24 && r.height >= 32 && solid(parse(st.backgroundColor)) && !ownText(e) && !e.firstElementChild) {
          const pr = par.getBoundingClientRect();
          if (r.width <= pr.width * 0.1 && (Math.abs(r.left - pr.left) <= 3 || Math.abs(r.right - pr.right) <= 3) && r.height >= pr.height * 0.5 && boxy(par, getComputedStyle(par)))
            tell("side_stripe", par, "가장자리 막대");
        }
        if (/text/.test(String(st.backgroundClip) + " " + String(st.webkitBackgroundClip || "")) && /gradient/.test(st.backgroundImage)) tell("gradient_text", e);
        if (st.backdropFilter && st.backdropFilter !== "none" && !/fixed|sticky/.test(st.position)) tell("glass", e, st.backdropFilter);
        for (const sh of String(st.boxShadow || "").split(/,(?![^(]*\))/)) {
          if (/inset/.test(sh) || sh.trim() === "none") continue;
          const c = parse((sh.match(/rgba?\([^)]+\)/) || [""])[0]);
          const nums = sh.replace(/rgba?\([^)]+\)/, "").match(/-?[\d.]+px/g) || [];
          const [x, y, blur] = nums.map(parseFloat);
          if (nums.length >= 2 && Math.abs(x) >= 2 && Math.abs(y) >= 2 && !(blur > 0) && c && c.a > 0.5) tell("hard_shadow", e, sh.trim());
          if (nums.length >= 3 && !x && !y && blur >= 12 && c && c.a > 0.25 && Math.max(c.r, c.g, c.b) - Math.min(c.r, c.g, c.b) > 60) tell("glow", e, sh.trim());
        }
        if (boxy(e, st)) cards.push([e, st, r]);
      }
      for (const [e] of cards) { const inner = cards.find(([c]) => c !== e && e.contains(c)); if (inner) tell("nested_cards", e, name(inner[0]) + " 을 감쌈"); }
      // 같은 모양 카드 나열 — 같은 부모 아래 3개 이상, 폭·모서리·그림자가 같고 각각 작은 아이콘·제목이 있다
      const groups = new Map();
      for (const it of cards) { const k = it[0].parentElement; if (!k) continue; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(it); }
      for (const [par, g] of groups) {
        if (g.length < 3) continue;
        const same = g.filter(([, st, r]) => Math.abs(r.width - g[0][2].width) <= 4 && st.borderTopLeftRadius === g[0][1].borderTopLeftRadius && st.boxShadow === g[0][1].boxShadow);
        const iconish = same.filter(([e]) => e.querySelector("h2,h3,h4,strong,b") && [...e.querySelectorAll("svg,img,i,span")].some((x) => {
          const q = x.getBoundingClientRect();
          return q.width > 0 && q.width <= 64 && q.height <= 64 && (x.tagName.toUpperCase() !== "SPAN" || EMOJI.test(x.textContent || "")); }));
        if (same.length >= 3 && iconish.length >= 3) tell("card_grid", par, same.length + "개의 같은 아이콘·제목·글 카드");
      }
      for (const e of everyEl) { const t = ownText(e); if (t && EMOJI.test(t)) tell("emoji_icon", e); }
      for (const e of everyEl.slice(0, 1500)) {
        const st = getComputedStyle(e), tf = st.transitionTimingFunction + " " + st.animationTimingFunction;
        for (const m of tf.matchAll(/cubic-bezier\(([^)]+)\)/g)) {
          const v = m[1].split(",").map(parseFloat);
          if (v[1] > 1.05 || v[3] > 1.05 || v[1] < -0.05 || v[3] < -0.05) { tell("bounce", e, m[0]); break; }
        }
      }
      const topHead = heads.reduce((a, b) => (!a || parseFloat(getComputedStyle(b).fontSize) > parseFloat(getComputedStyle(a).fontSize) ? b : a), null);
      if (topHead) {
        const f = getComputedStyle(topHead).fontFamily.split(",")[0].replace(/["']/g, "").trim();
        if (/^(system-ui|-apple-system|BlinkMacSystemFont|Segoe UI|Arial|Helvetica|Helvetica Neue|sans-serif|serif|Malgun Gothic|맑은 고딕|Apple SD Gothic Neo|Roboto|Inter)$/i.test(f))
          tell("system_display", topHead, f);
      }
      const blocks = everyEl.filter((e) => ownText(e).length >= 12 && e.getBoundingClientRect().width >= 240);
      const centered = blocks.filter((e) => getComputedStyle(e).textAlign === "center");
      if (blocks.length >= 6 && centered.length / blocks.length > 0.6) tell("all_centered", null, centered.length + "/" + blocks.length + " 덩어리");
      // 파비콘 — 없으면 탭에 기본 아이콘이 뜨고, 이모지 글자 파비콘은 만든 티가 난다
      const icoLink = document.querySelector('link[rel~="icon" i]');
      if (!icoLink) tell("no_favicon", null, "");
      else {
        let href = icoLink.getAttribute("href") || "";
        try { href = decodeURIComponent(href); } catch (e) { /* 그대로 */ }
        if (/<text/i.test(href) && EMOJI.test(href)) tell("emoji_favicon", null, "");
        const hexes = [...href.replace(/%23/g, "#").matchAll(/#([0-9a-f]{6}|[0-9a-f]{3})(?![0-9a-f])/gi)].map((m) => {
          const x = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
          return { r: parseInt(x.slice(0, 2), 16), g: parseInt(x.slice(2, 4), 16), b: parseInt(x.slice(4, 6), 16), hex: "#" + x }; })
          .filter((c) => Math.max(c.r, c.g, c.b) - Math.min(c.r, c.g, c.b) > 40);  // 흰색·검정·회색은 어느 화면에나 맞는다
        if (hexes.length) {
          const used = new Set();
          for (const e of everyEl.slice(0, 1500)) { const st = getComputedStyle(e); [st.color, st.backgroundColor, st.borderTopColor, st.fill, st.stroke].forEach((v) => used.add(v)); }
          const pc = [...used].map(parse).filter((c) => c && c.a > 0.3);
          const near = (a, b) => Math.abs(a.r - b.r) + Math.abs(a.g - b.g) + Math.abs(a.b - b.b) <= 60;
          if (!hexes.some((x) => pc.some((c) => near(x, c)))) tell("favicon_off_palette", null, [...new Set(hexes.map((x) => x.hex))].slice(0, 3).join(" "));
        }
      }
      // 아이콘 한 벌 — 작은 SVG 아이콘의 선 굵기(보이는 크기로 환산)나 선·채움 방식이 섞였나
      const icons = [...document.querySelectorAll("svg")].filter((sv) => { const q = sv.getBoundingClientRect(); return vis(sv) && q.width >= 10 && q.width <= 40 && q.height <= 40; });
      const outline = [], filled = [];
      for (const sv of icons) {
        const shape = sv.querySelector("path,circle,rect,line,polyline,polygon,ellipse");
        if (!shape) continue;
        const st = getComputedStyle(shape), q = sv.getBoundingClientRect();
        const vb = sv.viewBox && sv.viewBox.baseVal && sv.viewBox.baseVal.width ? sv.viewBox.baseVal.width : q.width;
        const swd = parseFloat(st.strokeWidth) || 0;
        const seen = st.vectorEffect === "non-scaling-stroke" ? swd : swd * q.width / vb;
        if (st.stroke !== "none" && swd > 0 && st.fill === "none") outline.push([sv, Math.round(seen * 4) / 4]);
        else if (st.fill !== "none" && (st.stroke === "none" || !swd)) filled.push(sv);
      }
      if (outline.length >= 3) {
        const ws = outline.map((x) => x[1]).sort((a, b) => a - b);
        if (ws[0] > 0 && ws[ws.length - 1] / ws[0] >= 1.6) tell("mixed_icons", outline[0][0], `선 굵기 ${ws[0]}–${ws[ws.length - 1]}px`);
      }
      const few = Math.min(outline.length, filled.length);
      if (few >= 2 && few / (outline.length + filled.length) >= 0.25) tell("mixed_icons", filled[0], `선 아이콘 ${outline.length}개 · 채운 아이콘 ${filled.length}개`);
      // 고른 글꼴이 실제로 왔는가 — CSS 에 이름만 있고 받지 못하면 화면은 대체 글꼴로 보인다 (이름이 있는지가 아니라 브라우저가 쓴 것을 본다)
      const GENERIC = /^(serif|sans-serif|monospace|cursive|fantasy|system-ui|ui-sans-serif|ui-serif|ui-monospace|ui-rounded|-apple-system|blinkmacsystemfont|emoji|math|fangsong|inherit|initial)$/i;
      const famOf = (f) => String(f.family || "").replace(/["']/g, "").trim().toLowerCase();
      const faces = document.fonts ? [...document.fonts] : [];
      const came = [...new Set(faces.filter((f) => f.status === "loaded").map((f) => String(f.family).replace(/["']/g, "").trim()))];
      // 글꼴 스타일시트를 받았는가 — 같은 곳의 파일은 응답 코드로, 다른 곳(글꼴 서비스)은 받은 크기가 0 인지로 본다.
      // 다른 곳은 받았어도 크기를 숨길 수 있어 '받지 못했거나 그 안에 이 이름이 없음'으로만 말한다
      const fontLinks = [...document.querySelectorAll("link[rel~=stylesheet][href]")].filter((l) => /font|family=/i.test(l.href)).map((l) => {
        const e = performance.getEntriesByName(l.href).pop(), same = new URL(l.href, location.href).origin === location.origin;
        const dead = !e || (e.responseStatus >= 400) || (!e.responseStatus && !e.transferSize && !e.decodedBodySize);
        return { href: l.href.slice(0, 120), dead, sure: !e || e.responseStatus >= 400 || same, code: e && e.responseStatus };
      });
      const deadLink = fontLinks.find((l) => l.dead);
      // 이 컴퓨터에 깔린 글꼴인가 — 찍기 전에 local() 로 물어본 결과. 물어보지 못했으면(페이지가 바뀜) 깔린 것으로 본다 (모르면 지적하지 않는다)
      const probed = (window.__wbFontProbe && window.__wbFontProbe.local) || {};
      const installed = (f) => probed[f] !== false;
      // 한 이름이 왜 안 보이는가 — 보이면 빈 글자
      const whyNot = (f) => {
        const mine = faces.filter((x) => famOf(x) === f.toLowerCase());
        if (mine.some((x) => x.status === "loaded")) return "";
        if (mine.some((x) => x.status === "error")) return "@font-face 의 글꼴 파일을 받지 못함 (주소가 틀렸거나 파일이 없음)";
        if (mine.length) return "글꼴 파일을 4초 안에 받지 못함 (너무 크거나 느린 주소)";
        if (installed(f)) return "";
        return deadLink ? (deadLink.sure ? `글꼴 주소를 받지 못함${deadLink.code ? " (응답 " + deadLink.code + ")" : ""} — ${deadLink.href}`
                                         : `글꼴 주소를 받지 못했거나 그 안에 이 이름이 없음 — ${deadLink.href}`)
          : `이 이름을 불러오는 곳이 없음 (글꼴 링크·@font-face 에 없고 이 컴퓨터에도 없음${came.length ? " — 실제로 온 글꼴: " + came.slice(0, 4).join(", ") : ""})`;
      };
      // 글꼴 목록을 앞에서부터 — 처음 보이는 이름이 실제로 쓰인 글꼴이다. 이름 붙은 글꼴이 하나도 안 보여 기본 글꼴로 떨어지면 고칠 것,
      // 앞 이름은 없지만 뒤 이름으로 보이면 참고(다른 컴퓨터용 이름일 수 있다 — 고른 글꼴이 맞는지만 확인)
      for (const [stack, use] of Object.entries(fontUse)) {
        const fams = stack.split(",").map((x) => x.replace(/["']/g, "").trim()).filter(Boolean);
        const missing = [];
        let shown = null;
        for (const f of fams) {
          if (GENERIC.test(f)) { shown = { generic: f }; break; }
          const w = whyNot(f);
          if (!w) { shown = { font: f }; break; }
          missing.push({ font: f, why: w });
        }
        if (!missing.length) continue;
        const row = { font: missing[0].font, why: missing[0].why, count: use.n, el: name(use.el), text: txt(use.el),
                      shown: shown && shown.font ? shown.font : "", missing: missing.map((m) => m.font) };
        (shown && shown.font ? out.font_fallback : out.font_missing).push(row);
      }
      out.tells = tells;
      for (const k of Object.keys(out)) if (Array.isArray(out[k])) out[k] = cap(out[k]);
      return JSON.stringify({ issues: out, variety: { fonts: [...fonts], sizes: [...sizes].sort((a, b) => a - b), text_colors: colors.size },
                              ms: Math.round(performance.now() - t0) });
    })()"""


def main() -> int:
    spec = json.loads(sys.argv[1] if len(sys.argv) > 1 else sys.stdin.buffer.read().decode("utf-8"))
    pages = spec.get("pages") or [spec]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-lcd-text")  # 스크린샷에 글자 색 번짐(서브픽셀) 없이
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

        # 브라우저 기본 대화 상자(alert·confirm·prompt) — 기본 처리는 창을 띄워 답을 기다리는데, 보이지 않는 엔진에서는 아무도 닫지
        # 않아 시간 초과로 끝났다 (실험: 삭제 확인창이 있는 화면을 확인하지 못해 실행이 멈췄다). 툴킷이 답하고 그 글을 남긴다 —
        # 확인 창은 '확인'이 기본, 앞 동작에 {"dialog": "dismiss"} 를 두면 '취소'. 글은 화면에 보인 글과 함께 visible 로 확인할 수 있다.
        def _said(self, kind, msg):
            if state["cur"] is not None:
                state["cur"].setdefault("dialogs", []).append({"kind": kind, "text": str(msg)[:300]})

        def javaScriptAlert(self, origin, msg):  # noqa: N802
            self._said("alert", msg)

        def javaScriptConfirm(self, origin, msg):  # noqa: N802
            self._said("confirm", msg)
            answer = state.get("confirm", True)
            state["confirm"] = True  # 한 번만 — 다음 확인 창은 다시 '확인'
            return answer

        def javaScriptPrompt(self, origin, msg, default):  # noqa: N802
            self._said("prompt", msg)
            answer = state.get("prompt")
            state["prompt"] = None
            return (answer is not None, str(answer if answer is not None else default or ""))

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
            from beom_workbench.core import sourcemap

            def fetch(u):
                with urllib.request.urlopen(u, timeout=5) as resp:  # noqa: S310 — 검증 서버(127.0.0.1)
                    return resp.read()
            sources, maps = {}, {}
            for x in scripts:
                try:
                    body = fetch(x["url"])
                except Exception:  # noqa: BLE001 — 못 받으면 그 스크립트는 파일과 맞춰 보지 못한다
                    continue
                sources[x["url"]] = hashlib.sha256(body).hexdigest()
                path = urlsplit(x["url"]).path
                if "/node_modules/" in path or path.startswith(("/@vite/", "/@id/", "/@react-refresh")):
                    continue  # 개발 서버의 라이브러리·자체 스크립트 — 프로젝트 파일이 아니다
                seen_maps = cov.setdefault("maps_seen", set())
                if x["url"] in seen_maps:
                    continue  # 같은 검증에서 앞 화면이 이미 남긴 맵 — 툴킷은 화면들의 맵을 합쳐 쓴다 (화면마다 같은 맵을 다시 쓰지 않는다)
                # 빌드 도구가 바꿔 보낸 코드(Vue·JSX·TS)는 소스맵으로 원래 파일의 줄에 맞춘다 — 받은 코드의 줄 시작 자리와 맵을 남긴다
                text = body.decode("utf-8", "replace")
                try:
                    m = sourcemap.find(text, x["url"], fetch)
                except Exception:  # noqa: BLE001 — 맵이 깨졌으면 그 스크립트는 원래 파일로 못 맞춘다 (전처럼 '못 잼')
                    m = None
                if m:
                    seen_maps.add(x["url"])
                    maps[x["url"]] = {"map": {k: m.get(k) for k in ("sources", "sourceRoot", "sourcesContent", "mappings")},
                                      "starts": sourcemap.u16_line_starts(text)}
            try:
                Path(cov_dir, f"page-{idx}.json").write_text(json.dumps({"url": page_url, "result": scripts, "sources": sources,
                                                                         "maps": maps}), encoding="utf-8")
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
        state["confirm"], state["prompt"] = True, None
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
        if p.get("cookies") and target.scheme() in ("http", "https"):
            # 사용자가 앱 브라우저에서 로그인하고 허락한 사이트 — 그 로그인 상태로 연다 (쿠키는 툴킷 홈에서, 에이전트는 받지 않는다)
            from PySide6.QtNetwork import QNetworkCookie
            from PySide6.QtCore import QByteArray, QDateTime
            store = view.page().profile().cookieStore()
            origin = QUrl(f"{target.scheme()}://{target.host()}")
            for c in p["cookies"]:
                ck = QNetworkCookie(QByteArray(str(c.get("name", "")).encode()), QByteArray(str(c.get("value", "")).encode()))
                if c.get("domain"):
                    ck.setDomain(str(c["domain"]))
                ck.setPath(str(c.get("path") or "/"))
                ck.setSecure(bool(c.get("secure")))
                ck.setHttpOnly(bool(c.get("httpOnly")))
                if c.get("expires"):
                    ck.setExpirationDate(QDateTime.fromSecsSinceEpoch(int(c["expires"])))
                store.setCookie(ck, origin)
            QTimer.singleShot(250, lambda: view.load(target))  # 쿠키를 넣는 일은 비동기 — 들어간 뒤에 연다
        elif cov_dir and dbg and target.scheme() in ("http", "https"):
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
        said = " ".join(d["text"] for d in cur.get("dialogs") or [])
        miss = [t for t in res.get("missing", []) if not (said and str(t) in said)]  # 대화 상자에 뜬 글도 사용자가 본 글이다
        if p.get("dump"):  # 문서 받기 — 스크립트로 그린 페이지의 글과 링크
            cur.update(text=res.get("text") or "", links=res.get("links") or [], pw=bool(res.get("pw")))
        cur.update(ok=True, title=res.get("title"), found=res.get("found", []) + [t for t in res.get("missing", []) if t not in miss],
                   missing=miss, selectors_missing=res.get("smissing", []))
        w, h = int(p.get("width") or 1280), int(p.get("height") or 800)
        full = min(max(h, int(res.get("height") or h)), 6000) if p.get("full_page", True) else h
        view.resize(w, full)

        def shoot():
            if idx != state["i"]:
                return
            # 웹 글꼴이 다 올 때까지 (최대 4초) — 한글 웹 글꼴은 글자 범위마다 나눠 받아서, 일찍 찍으면 몇 글자만 대체 글꼴로 보이고
            # 디자인 점검도 대체 글꼴 크기로 잰다. 화면 크기를 바꾸면 새로 보이는 글자의 글꼴 조각을 더 받는다.
            if not cur.get("_fonts_done"):
                cur.setdefault("_fonts_t", 0)

                def fonts(status):
                    if idx != state["i"]:
                        return
                    if status == "loading" and cur["_fonts_t"] < 4000:
                        cur["_fonts_t"] += 150
                        QTimer.singleShot(150, shoot)
                        return
                    cur["_fonts_done"] = True
                    shoot()
                view.page().runJavaScript(FONTS_JS, 0, fonts)
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
    if sys.platform == "darwin":  # macOS 의 Qt 는 ControlModifier 가 Cmd — 'ctrl+s' 케이스가 OS 마다 다른 키가 되지 않게 바꿔 둔다
        MODS.update({"ctrl": "MetaModifier", "control": "MetaModifier", "meta": "ControlModifier", "cmd": "ControlModifier"})

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
    # 글꼴을 다 받았는지 + 페이지가 쓰는 글꼴 이름이 이 컴퓨터에 깔려 있는지(local() 로 물어본다 — 화면 엔진에서는 글자 폭으로는
    # 대체 글꼴과 구별되지 않았다). 결과는 디자인 점검이 읽는다.
    FONTS_JS = r"""(() => {
      if (!window.__wbFontProbe) {
        const p = window.__wbFontProbe = { done: false, local: {} }, fams = new Set();
        for (const el of (document.body ? document.body.querySelectorAll("*") : [])) {
          for (const x of getComputedStyle(el).fontFamily.split(",")) { const f = x.replace(/["']/g, "").trim(); if (f) fams.add(f); } }
        Promise.all([...fams].slice(0, 40).map((f) => new FontFace("__wb_probe", `local("${f.replace(/["\\]/g, "")}")`).load()
          .then(() => { p.local[f] = true; }, () => { p.local[f] = false; }))).then(() => { p.done = true; });
      }
      if (!window.__wbFontProbe.done) return "loading";
      return document.fonts ? document.fonts.status : "loaded";
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
        elif a.get("dialog") is not None:  # 다음 대화 상자의 답 — "accept"(기본)·"dismiss"·그 밖의 글은 prompt 에 넣을 값
            d = str(a["dialog"])
            state["confirm"] = d.lower() != "dismiss"
            state["prompt"] = None if d.lower() in ("accept", "dismiss") else d
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
        js = """(() => { const t = document.body ? document.body.innerText : ""; const want = %s, sel = %s, dump = %s;
          return JSON.stringify({ title: document.title, height: document.documentElement.scrollHeight,
                   found: want.filter((w) => t.includes(w)), missing: want.filter((w) => !t.includes(w)),
                   smissing: sel.filter((s) => { try { return !document.querySelector(s); } catch (e) { return true; } }),
                   text: dump ? t.slice(0, 200000) : undefined, pw: dump ? !!document.querySelector("input[type=password]") : undefined,
                   links: dump ? [...document.querySelectorAll("a[href]")].slice(0, 400).map((a) => [(a.innerText || a.title || a.getAttribute("aria-label") || "").trim().replace(/\\s+/g, " ").slice(0, 80), a.href]) : undefined }); })()""" % (
            json.dumps(p.get("visible") or [], ensure_ascii=False), json.dumps(p.get("selectors") or [], ensure_ascii=False),
            "true" if p.get("dump") else "false")
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
    # 페이지가 준 글에 반쪽 글자가 섞여도 결과는 쓴다 (바꿀 수 없는 글자는 대체 문자로)
    sys.stdout.buffer.write(json.dumps({"pages": results}, ensure_ascii=False).encode("utf-8", "replace") + b"\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
