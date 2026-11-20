(function () {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';
  const W = 1600, H = 900;

  function E(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function Tx(parent, x, y, s, attrs) {
    const t = E('text', Object.assign({ x, y }, attrs || {}), parent);
    t.textContent = s;
    return t;
  }
  const smooth = p => (p <= 0 ? 0 : p >= 1 ? 1 : p * p * (3 - 2 * p));

  const M = {
    dur: 30, t: 0, playing: false, speed: 1, base: 0.5,
    items: [], states: [], texts: [], nums: [], flows: [], caps: [], chapters: [], hooks: [],
    E, Tx, W, H,
  };

  /* ---------- 타임라인 ---------- */

  M.item = function (g, x, y, w, h, o) {
    const it = { g, w: w || 0, h: h || 0, base: { x: x || 0, y: y || 0, o: o == null ? 1 : o, s: 1 }, frames: [] };
    it.cur = Object.assign({}, it.base);
    M.items.push(it);
    applyItem(it);
    return it;
  };

  function applyItem(it) {
    const c = it.cur;
    if (Math.abs(c.s - 1) < 1e-4) {
      it.g.setAttribute('transform', `translate(${c.x.toFixed(2)} ${c.y.toFixed(2)})`);
    } else {
      const w2 = it.w / 2, h2 = it.h / 2;
      it.g.setAttribute('transform', `translate(${(c.x + w2).toFixed(2)} ${(c.y + h2).toFixed(2)}) scale(${c.s.toFixed(3)}) translate(${-w2} ${-h2})`);
    }
    it.g.style.opacity = c.o.toFixed(3);
    it.g.style.visibility = c.o < 0.005 ? 'hidden' : 'visible';
  }

  M.key = function (it, frames) {
    for (const f of frames) it.frames.push({ t: f[0], v: f[1], e: f[2] || 's' });
    it.frames.sort((a, b) => a.t - b.t);
  };

  function interp(it, p, t) {
    let prev = null, next = null;
    for (const f of it.frames) {
      if (!(p in f.v)) continue;
      if (f.t <= t) prev = f; else { next = f; break; }
    }
    if (!prev && !next) return it.base[p];
    if (!prev) return next.v[p];
    if (!next || next.e === 'step') return prev.v[p];
    const k = (t - prev.t) / (next.t - prev.t);
    return prev.v[p] + (next.v[p] - prev.v[p]) * (next.e === 'l' ? k : smooth(k));
  }

  M.show = function (it, t0, t1, opt) {
    const f = (opt && opt.fade) || 0.35;
    const pop = !(opt && opt.pop === false);
    const fr = [[t0, { o: 0 }], [t0 + f, { o: 1 }]];
    if (pop) fr.push([t0, { s: 0.94 }], [t0 + f, { s: 1 }]);
    if (t1 != null) fr.push([t1 - f, { o: 1 }], [t1, { o: 0 }]);
    M.key(it, fr);
    return it;
  };

  M.state = function (targets, cls, t0, t1) {
    for (const el of [].concat(targets)) M.states.push({ el: el.g || el, cls, t0, t1: t1 == null ? 1e9 : t1 });
  };
  M.text = function (el, frames) { M.texts.push({ el, frames }); };
  M.num = function (el, frames, fmt) { M.nums.push({ el, frames, fmt: fmt || (v => Math.round(v).toLocaleString('ko-KR')) }); };
  M.hook = function (fn) { M.hooks.push(fn); };
  M.cap = function (t0, t1, chip, title, body, kind) { M.caps.push({ t0, t1, chip, title, body, kind: kind || '' }); };

  /* ---------- 흐름(패킷) ---------- */

  M.P = function (it, side, dx, dy) {
    return function () {
      const c = it.cur;
      let x = c.x + it.w / 2, y = c.y + it.h / 2;
      if (side === 'l') x = c.x; else if (side === 'r') x = c.x + it.w;
      if (side === 't') y = c.y; else if (side === 'b') y = c.y + it.h;
      return [x + (dx || 0), y + (dy || 0)];
    };
  };
  function resolve(p) {
    if (typeof p === 'function') return p();
    if (Array.isArray(p)) return p;
    return M.P(p)();
  }

  M.flow = function (o) {
    const f = Object.assign({ n: 3, travel: 1.6, mode: 'loop', gap: 0.3, size: 6, kind: 'req', line: true, alive: null }, o);
    f.lineEl = f.line ? E('polyline', { class: 'fl fl-' + f.kind }, M.layers.lines) : null;
    f.pk = [];
    for (let i = 0; i < f.n; i++) f.pk.push(packet(f));
    M.flows.push(f);
    return f;
  };

  function packet(f) {
    const g = E('g', { class: 'pk pk-' + f.kind }, M.layers.packets);
    const s = f.size;
    if (f.kind === 'req' || f.kind === 'db') E('circle', { r: s }, g);
    else if (f.kind === 'obs') E('rect', { x: -s, y: -s, width: 2 * s, height: 2 * s, transform: 'rotate(45)' }, g);
    else if (f.kind === 'fail') { E('circle', { r: s + 3 }, g); E('path', { d: `M${-s * 0.6},${-s * 0.6}L${s * 0.6},${s * 0.6}M${s * 0.6},${-s * 0.6}L${-s * 0.6},${s * 0.6}` }, g); }
    else E('rect', { x: -s, y: -s, width: 2 * s, height: 2 * s, rx: 1.5 }, g);
    g.style.visibility = 'hidden';
    return g;
  }

  function along(pts, lens, total, frac) {
    let d = frac * total;
    for (let i = 0; i < lens.length; i++) {
      if (d <= lens[i] || i === lens.length - 1) {
        const k = lens[i] ? Math.min(1, d / lens[i]) : 0;
        return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * k, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * k];
      }
      d -= lens[i];
    }
    return pts[pts.length - 1];
  }

  function renderFlow(f, t, rt) {
    let active = t >= f.t0 && t <= f.t1;
    if (active && f.alive) active = f.alive();
    if (!active) {
      if (f.lineEl) f.lineEl.style.visibility = 'hidden';
      for (const g of f.pk) g.style.visibility = 'hidden';
      return;
    }
    const a = Math.max(0, Math.min(1, (t - f.t0) / 0.3, (f.t1 - t) / 0.3));
    const pts = f.pts.map(resolve);
    const lens = [];
    let total = 0;
    for (let i = 0; i < pts.length - 1; i++) {
      const l = Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]);
      lens.push(l); total += l;
    }
    if (f.lineEl) {
      f.lineEl.setAttribute('points', pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' '));
      f.lineEl.style.visibility = 'visible';
      f.lineEl.style.opacity = ((f.lineOpacity || 0.32) * a).toFixed(3);
    }
    f.pk.forEach((g, i) => {
      let frac;
      if (f.mode === 'once') {
        frac = (t - f.t0 - i * f.gap) / f.travel;
        if (frac < 0 || frac > 1) { g.style.visibility = 'hidden'; return; }
      } else {
        if (f.p0 == null) f.p0 = M.toPlay(f.t0);
        const ph = (rt - f.p0) / f.travel - i / f.n;
        if (ph < 0) { g.style.visibility = 'hidden'; return; }
        frac = ph % 1;
      }
      const p = along(pts, lens, total, frac);
      g.setAttribute('transform', `translate(${p[0].toFixed(1)} ${p[1].toFixed(1)})`);
      g.style.visibility = 'visible';
      g.style.opacity = a.toFixed(3);
    });
  }

  /* ---------- 렌더 ---------- */

  // 이야기 시간(장면 정의) ↔ 재생 시간(멈춤 구간 포함)
  M.toStory = function (rt) {
    let st = 0, pt = 0;
    for (const [ht, hd] of M.holds) {
      if (rt < pt + (ht - st)) return st + (rt - pt);
      pt += ht - st; st = ht;
      if (rt < pt + hd) return st;
      pt += hd;
    }
    return Math.min(M.story, st + (rt - pt));
  };
  M.toPlay = function (st) {
    let pt = st;
    for (const [ht, hd] of M.holds) if (ht < st) pt += hd;
    return pt;
  };

  let capIdx = -2;
  M.render = function (rt) {
    M.t = rt;
    const t = M.toStory(rt);
    M.st = t;
    for (const it of M.items) {
      if (it.frames.length) {
        for (const p of ['x', 'y', 'o', 's']) it.cur[p] = interp(it, p, t);
        applyItem(it);
      }
    }
    const on = new Map();
    for (const s of M.states) {
      let m = on.get(s.el);
      if (!m) { m = new Map(); on.set(s.el, m); }
      m.set(s.cls, (m.get(s.cls) || false) || (t >= s.t0 && t < s.t1));
    }
    for (const [el, m] of on) for (const [c, v] of m) el.classList.toggle(c, v);
    for (const x of M.texts) {
      let s = x.frames[0][1];
      for (const f of x.frames) if (f[0] <= t) s = f[1];
      if (x.el.textContent !== s) x.el.textContent = s;
    }
    for (const x of M.nums) {
      const fr = x.frames;
      let v = fr[0][1];
      for (let i = 0; i < fr.length; i++) {
        if (fr[i][0] <= t) v = fr[i][1];
        if (fr[i][0] <= t && fr[i + 1] && fr[i + 1][0] > t) {
          const k = (t - fr[i][0]) / (fr[i + 1][0] - fr[i][0]);
          v = fr[i][1] + (fr[i + 1][1] - fr[i][1]) * k;
          break;
        }
      }
      const s = x.fmt(v);
      if (x.el.textContent !== s) x.el.textContent = s;
    }
    for (const f of M.flows) renderFlow(f, t, rt);
    for (const h of M.hooks) h(t);

    let ci = -1;
    M.caps.forEach((c, i) => { if (t >= c.t0 && t < c.t1) ci = i; });
    if (ci !== capIdx) {
      capIdx = ci;
      const el = document.getElementById('cap');
      if (el) {
        const c = M.caps[ci];
        el.className = '';
        if (c) {
          el.innerHTML = `<div><span class="chip">${c.chip}</span><b>${c.title}</b></div><p>${c.body}</p>`;
          el.style.visibility = 'visible';
          void el.offsetWidth;
          el.className = 'fade' + (c.kind ? ' k-' + c.kind : '');
        } else {
          el.style.visibility = 'hidden';
        }
      }
    }
    updateUI();
  };

  /* ---------- 무대·레이어 ---------- */

  // 장면을 새로 그릴 때마다 무대와 타임라인을 비운다(관점 전환)
  M.setup = function (opt) {
    for (const k of ['items', 'states', 'texts', 'nums', 'flows', 'caps', 'hooks']) M[k] = [];
    capIdx = -2;
    M.t = 0; M.st = 0;
    document.getElementById('svg').innerHTML = '';
    M.story = opt.duration || 30;
    M.holds = (opt.holds || []).slice().sort((a, b) => a[0] - b[0]);
    M.dur = M.story + M.holds.reduce((a, h) => a + h[1], 0);
    M.chapters = opt.chapters || [];
    const svg = document.getElementById('svg');
    M.svg = svg;
    const defs = E('defs', null, svg);
    const hw = E('pattern', { id: 'hatch-warn', width: 10, height: 10, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
    E('rect', { width: 10, height: 10, fill: '#FDE7DA' }, hw);
    E('line', { x1: 0, y1: 0, x2: 0, y2: 10, stroke: '#E8A47F', 'stroke-width': 4 }, hw);
    const hd = E('pattern', { id: 'hatch-dead', width: 8, height: 8, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
    E('rect', { width: 8, height: 8, fill: '#E5E7EB' }, hd);
    E('line', { x1: 0, y1: 0, x2: 0, y2: 8, stroke: '#B8BEC7', 'stroke-width': 3 }, hd);
    M.layers = {};
    for (const k of ['bg', 'lines', 'pods', 'nodeov', 'packets', 'ext', 'over', 'top']) M.layers[k] = E('g', { class: 'layer-' + k }, svg);
    return M.layers;
  };

  M.title = function (title, sub) {
    Tx(M.layers.bg, 28, 40, title, { class: 'ttl' });
    if (sub) Tx(M.layers.bg, 30, 64, sub, { class: 'ttl-sub' });
  };

  // 범례: opt.chips [[cat, 칩 글자, 설명]], opt.packets [[kind, 설명]] — kind: req·val·obs·db·fail
  M.legend = function (x, y, opt) {
    opt = opt || {};
    const g = E('g', { class: 'legend' }, M.layers.bg);
    const fs = opt.fs || 14;
    let cx = x;
    for (const [cat, lb, desc] of opt.chips || []) {
      const pg = E('g', { class: 'pod cat-' + cat, transform: `translate(${cx} ${y})` }, g);
      const rect = E('rect', { class: 'body', width: 40, height: 22, rx: 5 }, pg);
      const lt = Tx(pg, 20, 11, lb, { class: 'lbl', 'font-size': 12 });
      // 칩 글자가 길면 칩을 넓힌다
      const cw = Math.max(40, Math.ceil(lt.getComputedTextLength()) + 14);
      rect.setAttribute('width', cw); lt.setAttribute('x', cw / 2);
      const t = Tx(g, cx + cw + 6, y + 16, desc, { class: 'lg-text', 'font-size': fs });
      cx += cw + 6 + t.getComputedTextLength() + 18;
    }
    for (const [kind, desc] of opt.packets || []) {
      const pg = E('g', { class: 'pk pk-' + kind, transform: `translate(${cx + 7} ${y + 11})` }, g);
      const s = 6;
      if (kind === 'req' || kind === 'db') E('circle', { r: s }, pg);
      else if (kind === 'obs') E('rect', { x: -s, y: -s, width: 2 * s, height: 2 * s, transform: 'rotate(45)' }, pg);
      else if (kind === 'fail') { E('circle', { r: s + 3 }, pg); E('path', { d: `M-3.6,-3.6L3.6,3.6M3.6,-3.6L-3.6,3.6` }, pg); }
      else E('rect', { x: -s, y: -s, width: 2 * s, height: 2 * s, rx: 1.5 }, pg);
      const t = Tx(g, cx + 20, y + 16, desc, { class: 'lg-text', 'font-size': fs });
      cx += 20 + t.getComputedTextLength() + 16;
    }
    return g;
  };

  /* ---------- 말풍선·카운터·시계 ---------- */

  const ICON = { warn: '⚠ ', ok: '✓ ', fail: '✕ ', val: '', obs: '', info: '' };

  M.callout = function (x, y, w, lines, kind, opt) {
    opt = opt || {};
    const fs = opt.fs || 16, lh = Math.round(fs * 1.42), pad = opt.pad || 10;
    const h = pad * 2 + lines.length * lh - (lh - fs) + 2;
    const g = E('g', { class: 'co ' + kind }, opt.layer || M.layers.over);
    if (opt.arrow) {
      const ax = opt.arrow[0] - x, ay = opt.arrow[1] - y;
      const sx = Math.max(0, Math.min(w, ax)), sy = ay > h ? h : ay < 0 ? 0 : h / 2;
      E('path', { class: 'co-arrow', d: `M${sx},${sy}L${ax},${ay}` }, g);
      E('circle', { class: 'co-dot', cx: ax, cy: ay, r: 4.5 }, g);
    }
    E('rect', { class: 'co-box', width: w, height: h, rx: 9 }, g);
    lines.forEach((s, i) => {
      const t = Tx(g, pad + 2, pad + fs * 0.86 + i * lh, (i === 0 ? ICON[kind] || '' : '') + s, { class: i === 0 ? 'co-t1' : 'co-t', 'font-size': i === 0 ? fs : fs - 1 });
      if (opt.center) { t.setAttribute('x', w / 2); t.setAttribute('text-anchor', 'middle'); }
    });
    const it = M.item(g, x, y, w, h, 0);
    it.textEls = g.querySelectorAll('text');
    return it;
  };

  M.counter = function (x, y, w, label, kind, opt) {
    opt = opt || {};
    const g = E('g', { class: 'co ' + kind }, M.layers.over);
    const h = opt.h || 76;
    E('rect', { class: 'co-box', width: w, height: h, rx: 10 }, g);
    Tx(g, 14, 26, (ICON[kind] || '') + label, { class: 'co-t1', 'font-size': opt.fs || 16 });
    const num = Tx(g, 14, h - 14, '0', { class: 'co-t1', 'font-size': opt.nfs || 32 });
    const it = M.item(g, x, y, w, h, 0);
    it.num = num;
    return it;
  };

  M.clock = function (x, y, w, h) {
    const g = E('g', { class: 'clock' }, M.layers.top);
    E('rect', { class: 'clock-box', width: w, height: h, rx: 10 }, g);
    const day = Tx(g, 16, h / 2 + 6, '', { class: 'clock-day', 'font-size': 18 });
    const time = Tx(g, w - 16, h / 2 + 12, '', { class: 'clock-time', 'font-size': 34, 'text-anchor': 'end' });
    const it = M.item(g, x, y, w, h, 0);
    it.day = day; it.time = time;
    return it;
  };

  /* ---------- 보드: 구역(서버·노드) + 칩(Pod·프로세스) ---------- */

  // G: 배치 설정(프레임·구역·칩 크기·글자 크기), opt: { chips, layout, title }
  //   chips: { id: { cat: 'prd'|'dev'|'val'|'obs', label, at: { 레이아웃이름: [구역 번호, 칸 번호] } } }
  //   구역 칸은 cols 개씩 줄을 바꾼다. 어떤 레이아웃에 at 이 없으면 그 레이아웃에서는 보이지 않는다
  M.board = function (G, opt) {
    const L = M.layers;
    const B = { G, chips: {}, groups: [], boxes: {}, layout: opt.layout, defs: opt.chips };
    const fr = G.frame;
    if (fr) {
      E('rect', { class: 'cl-frame', x: fr.x, y: fr.y, width: fr.w, height: fr.h, rx: 14 }, L.bg);
      B.frameTitle = Tx(L.bg, fr.x + 14, fr.y + G.fs.node + 6, opt.title || '', { class: 'cl-title', 'font-size': G.fs.node - 1 });
    }
    const labels = G.groups;
    B.groupX = i => G.nodeX0 + i * (G.nodeW + G.nodeGap);
    labels.forEach((name, i) => {
      const x = B.groupX(i), y = G.nodeY, w = G.nodeW, h = G.nodeH;
      const g = E('g', { class: 'node' }, L.bg);
      const it = M.item(g, x, y, w, h);
      E('rect', { class: 'node-box', width: w, height: h, rx: 10 }, g);
      E('rect', { class: 'band', x: 4, y: 4, width: w - 8, height: G.bandH, rx: 7 }, g);
      Tx(g, 14, 4 + G.bandH / 2 + G.fs.node * 0.36, name, { class: 'node-name', 'font-size': G.fs.node });
      // 역할 띠: 구역에 'prod' 또는 'non' 클래스를 주면 색과 글자가 바뀐다(B.role)
      const roles = G.roles || {};
      if (roles.prod) Tx(g, w - 12, 4 + G.bandH / 2 + G.fs.node * 0.33, roles.prod, { class: 'role role-prod', 'font-size': G.fs.node - 2, 'text-anchor': 'end' });
      if (roles.non) Tx(g, w - 12, 4 + G.bandH / 2 + G.fs.node * 0.33, roles.non, { class: 'role role-non', 'font-size': G.fs.node - 2, 'text-anchor': 'end' });
      if (G.badge) {
        E('line', { class: 'taint-line', x1: 6, y1: G.bandH + 9, x2: w - 6, y2: G.bandH + 9 }, g);
        Tx(g, w / 2, G.bandH + 9 + G.fs.small + 3, G.badge, { class: 'taint', 'font-size': G.fs.small, 'text-anchor': 'middle' });
      }
      if (G.barText) {
        const bar = E('g', { class: 'node-bar' }, g);
        E('rect', { x: 6, y: h - G.barH - 6, width: w - 12, height: G.barH, rx: 5 }, bar);
        Tx(bar, w / 2, h - 6 - G.barH / 2 + G.fs.small * 0.36, G.barText, { 'font-size': G.fs.small, 'text-anchor': 'middle' });
      }
      // 장애 표시는 칩 위·패킷 아래 층에 그린다 — 지나가는 트래픽이 가려지지 않게
      const og = E('g', { class: 'node-ov' }, L.nodeov);
      E('rect', { class: 'ov-area', width: w, height: h, rx: 10 }, og);
      const lines = G.downLines || ['행(hang)', '응답 없음'];
      const lh = G.fs.node + 6, bh = lines.length * lh + 14;
      E('rect', { class: 'ov-label', x: 10, y: h / 2 - bh / 2, width: w - 20, height: bh, rx: 8 }, og);
      const downTexts = lines.map((s, k) => Tx(og, w / 2, h / 2 - bh / 2 + 7 + G.fs.node + k * lh, (k === 0 ? '✕ ' : '') + s, { 'font-size': k === 0 ? G.fs.node + 2 : G.fs.small + 1 }));
      og.setAttribute('transform', `translate(${x} ${y})`);
      B.groups.push({ it, g, ov: og, downTexts, x, y, w, h, bar: () => [x + w / 2, y + h - 6 - (G.barH || 0) / 2] });
    });

    B.slot = function (n, idx) {
      const padX = (G.nodeW - (G.cols * G.podW + (G.cols - 1) * G.podGapX)) / 2;
      return {
        x: B.groupX(n) + padX + (idx % G.cols) * (G.podW + G.podGapX),
        y: G.nodeY + G.podTop + Math.floor(idx / G.cols) * (G.podH + G.podGapY),
      };
    };
    B.slotCenter = (n, idx) => { const p = B.slot(n, idx); return [p.x + G.podW / 2, p.y + G.podH / 2]; };

    const firstAt = d => d.at[opt.layout] || Object.values(d.at).find(Boolean);
    for (const id in opt.chips) {
      const d = opt.chips[id];
      const g = E('g', null, L.pods);
      const inner = E('g', { class: 'pod cat-' + d.cat }, g);
      E('rect', { class: 'body', width: G.podW, height: G.podH, rx: 5 }, inner);
      const lbl = Tx(inner, G.podW / 2, G.podH / 2, d.label, { class: 'lbl', 'font-size': G.fs.pod });
      const mf = E('g', { class: 'mk mk-fail', transform: `translate(${G.podW - 2} 1)` }, inner);
      E('circle', { r: 8 }, mf); Tx(mf, 0, 0.5, '!', { 'font-size': 12 });
      const md = E('g', { class: 'mk mk-dead', transform: `translate(${G.podW - 2} 1)` }, inner);
      E('circle', { r: 8 }, md); Tx(md, 0, 0.5, '✕', { 'font-size': 10 });
      // 글자가 칩보다 길면 칩 안에 들어갈 때까지 글자 크기를 줄인다
      for (let fs = G.fs.pod; fs > 9 && lbl.getComputedTextLength() > G.podW - 10; fs -= 0.5) lbl.setAttribute('font-size', fs - 0.5);
      const at = firstAt(d);
      const p = B.slot(at[0], at[1]);
      const it = M.item(g, p.x, p.y, G.podW, G.podH, d.at[opt.layout] ? 1 : 0);
      it.inner = inner; it.lbl = lbl; it.id = id; it.def = d;
      B.chips[id] = it;
    }
    B.chip = id => B.chips[id];
    // 칩 상태 클래스: fail(주황·!), dead(빗금·✕), hl(테두리 강조)
    B.chipState = (ids, cls, t0, t1) => M.state([].concat(ids).map(id => B.chips[id].inner), cls, t0, t1);
    B.alive = id => () => B.chips[id].cur.o > 0.5;
    // 구역 역할: 'prod'(파랑 띠·badge 표시) 또는 'non'(회색 띠)
    B.role = (i, role, t0, t1) => M.state(B.groups[i].g, role, t0, t1);
    // 구역 장애 표시(빗금 + 문구). 문구는 B.groups[i].downTexts[k].textContent 로 바꾼다
    B.down = (i, t0, t1) => M.state(B.groups[i].ov, 'hang', t0, t1);

    B.moveTo = function (id, t0, t1, n, idx) {
      const it = B.chips[id], p = B.slot(n, idx);
      M.key(it, [[t0, { x: interp(it, 'x', t0), y: interp(it, 'y', t0) }], [t1, { x: p.x, y: p.y }]]);
    };
    B.fadeOut = function (ids, t0, d) {
      for (const id of [].concat(ids)) M.key(B.chips[id], [[t0, { o: 1, s: 1 }], [t0 + (d || 0.4), { o: 0, s: 0.7 }]]);
    };
    B.fadeIn = function (ids, t0, d) {
      for (const id of [].concat(ids)) M.key(B.chips[id], [[0, { o: 0 }], [t0, { o: 0, s: 0.7 }], [t0 + (d || 0.4), { o: 1, s: 1 }]]);
    };

    // 레이아웃 전환: 두 레이아웃에 다 있는 칩은 이동, to 에만 있는 칩은 spawnFrom[id] 칩 자리에서 나타나 이동,
    // from 에만 있는 칩은 사라진다
    B.morph = function (from, to, t0, d, stagger, spawnFrom) {
      let k = 0;
      for (const id in opt.chips) {
        const def = opt.chips[id], it = B.chips[id];
        const s = t0 + (k++) * stagger;
        const a0 = def.at[from], a1 = def.at[to];
        const p0 = a0 && B.slot(a0[0], a0[1]);
        const p1 = a1 && B.slot(a1[0], a1[1]);
        if (p0 && p1) {
          M.key(it, [[s, { x: p0.x, y: p0.y }], [s + d, { x: p1.x, y: p1.y }]]);
        } else if (p1) {
          const src = spawnFrom && spawnFrom[id] && opt.chips[spawnFrom[id]].at[from];
          const sp = src ? B.slot(src[0], src[1]) : p1;
          M.key(it, [[0, { x: sp.x, y: sp.y, o: 0 }], [s, { x: sp.x, y: sp.y, o: 0 }], [s + 0.2, { o: 1 }], [s + d, { x: p1.x, y: p1.y }]]);
        } else if (p0) {
          M.key(it, [[s, { o: 1 }], [s + d * 0.5, { o: 0 }]]);
        }
      }
      return t0 + (k - 1) * stagger + d;
    };

    // 바깥 상자(외부 시스템·DB·저장소). down 상태면 빗금 + subDown 문구
    B.addBox = function (key, r, title, sub, subDown, cls) {
      const g = E('g', { class: 'ext ' + (cls || '') }, L.ext);
      E('rect', { class: 'ext-box', width: r[2], height: r[3], rx: 9 }, g);
      Tx(g, r[2] / 2, sub ? r[3] / 2 - 2 : r[3] / 2 + G.fs.ext * 0.36, title, { class: 'ext-title', 'font-size': G.fs.ext, 'text-anchor': 'middle' });
      if (sub) Tx(g, r[2] / 2, r[3] / 2 + G.fs.extSub + 3, sub, { class: 'ext-sub', 'font-size': G.fs.extSub, 'text-anchor': 'middle' });
      if (subDown) Tx(g, r[2] / 2, r[3] / 2 + G.fs.extSub + 3, subDown, { class: 'ext-subdown', 'font-size': G.fs.extSub, 'text-anchor': 'middle' });
      const it = M.item(g, r[0], r[1], r[2], r[3]);
      B.boxes[key] = it;
      return it;
    };

    // 상태 패널(관측 화면·대시보드). rows: [[이름, 설명], ...], 행에 'down' 상태를 주면 실패 표시
    B.addPanel = function (r, title, rows, words) {
      words = words || { ok: '✓ 정상', down: '✕ 접속 불가' };
      const g = E('g', { class: 'scr' }, L.ext);
      const headH = G.fs.ext + 16;
      E('rect', { class: 'scr-box', width: r[2], height: r[3], rx: 10 }, g);
      E('rect', { class: 'scr-head', x: 2, y: 2, width: r[2] - 4, height: headH, rx: 8 }, g);
      Tx(g, 14, 2 + headH / 2 + G.fs.ext * 0.36, title, { class: 'scr-title', 'font-size': G.fs.ext });
      const rh = (r[3] - headH - 10) / rows.length;
      const out = rows.map((row, i) => {
        const rg = E('g', { class: 'row', transform: `translate(6 ${headH + 6 + i * rh})` }, g);
        E('rect', { class: 'row-bg', width: r[2] - 12, height: rh - 4, rx: 6 }, rg);
        const twoLine = rh > G.fs.row * 2.4;
        Tx(rg, 10, twoLine ? rh / 2 - 4 : rh / 2 + G.fs.row * 0.3, row[0], { class: 'row-name', 'font-size': G.fs.row });
        const note = Tx(rg, 10, twoLine ? rh / 2 + G.fs.row - 2 : rh / 2 + G.fs.row * 0.3, twoLine ? (row[1] || '') : '', { class: 'row-note', 'font-size': G.fs.row - 3 });
        Tx(rg, r[2] - 22, rh / 2 + G.fs.row * 0.3, words.ok, { class: 'st-ok', 'font-size': G.fs.row });
        Tx(rg, r[2] - 22, rh / 2 + G.fs.row * 0.3, words.down, { class: 'st-down', 'font-size': G.fs.row });
        return { g: rg, note, y: r[1] + headH + 6 + i * rh + (rh - 4) / 2 };
      });
      const it = M.item(g, r[0], r[1], r[2], r[3]);
      it.rows = out;
      it.rowAnchor = i => () => [r[0], out[i].y];
      B.panel = it;
      return it;
    };

    return B;
  };

  /* ---------- 표 ---------- */

  M.table = function (x, y, cols, rowH, rows, opt) {
    opt = opt || {};
    const g = E('g', { class: 'sum' }, M.layers.top);
    const w = cols.reduce((a, b) => a + b, 0);
    const h = rowH * rows.length;
    if (opt.backdrop) E('rect', { class: 'sum-bg', x: -x, y: opt.backdrop[0] - y, width: W, height: opt.backdrop[1] }, g);
    E('rect', { class: 'sum-box', x: -16, y: -16, width: w + 32, height: h + 32, rx: 14 }, g);
    rows.forEach((r, i) => {
      if (i > 0) E('line', { class: 'sum-grid', x1: 0, y1: i * rowH, x2: w, y2: i * rowH }, g);
      let cx = 0;
      r.forEach((cell, j) => {
        const lines = [].concat(cell);
        const cls = i === 0 ? 'sum-head' : j === 0 ? 'sum-head' : j === 1 ? 'sum-before' : 'sum-after';
        const fs = i === 0 ? (opt.headFs || 19) : (opt.fs || 17);
        const lh = fs * 1.3;
        lines.forEach((s, k) => Tx(g, cx + 12, i * rowH + rowH / 2 - (lines.length - 1) * lh / 2 + fs * 0.36 + k * lh, s, { class: cls, 'font-size': fs }));
        cx += cols[j];
      });
    });
    return M.item(g, x, y, w, h, 0);
  };

  /* ---------- 재생 UI ---------- */

  let ui = null;
  function buildUI() {
    const c = document.getElementById('controls');
    if (ui) return buildSegs();
    c.innerHTML = '<button id="btnPlay" type="button" aria-label="재생/일시정지">❚❚</button><button id="btnRe" type="button" aria-label="처음부터">↺</button><div id="bar" role="slider" tabindex="0" aria-label="재생 위치"><div id="prog"></div></div><span id="time"></span><label class="spd">속도 <select id="speed" aria-label="재생 속도"></select></label>';
    const sel = c.querySelector('#speed');
    for (const v of [0.5, 0.75, 1, 1.25, 1.5, 2]) {
      const o = document.createElement('option');
      o.value = v; o.textContent = v + '×';
      if (v === M.speed) o.selected = true;
      sel.appendChild(o);
    }
    sel.addEventListener('change', () => { M.speed = parseFloat(sel.value); updateUI(); });
    const bar = c.querySelector('#bar');
    const seekAt = ev => {
      const r = bar.getBoundingClientRect();
      M.seek(Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)) * M.dur);
    };
    let drag = false;
    bar.addEventListener('pointerdown', ev => { drag = true; bar.setPointerCapture(ev.pointerId); seekAt(ev); });
    bar.addEventListener('pointermove', ev => { if (drag) seekAt(ev); });
    bar.addEventListener('pointerup', () => { drag = false; });
    c.querySelector('#btnPlay').addEventListener('click', () => M.toggle());
    c.querySelector('#btnRe').addEventListener('click', () => { M.seek(0); M.play(); });
    ui = { play: c.querySelector('#btnPlay'), prog: c.querySelector('#prog'), time: c.querySelector('#time'), speed: sel, segs: [], bar };
    buildSegs();
  }
  function buildSegs() {
    const bar = ui.bar;
    for (const s of ui.segs) s.el.remove();
    ui.segs = M.chapters.map((ch, i) => {
      const t0 = M.toPlay(ch.t);
      const end = i + 1 < M.chapters.length ? M.toPlay(M.chapters[i + 1].t) : M.dur;
      const d = document.createElement('div');
      d.className = 'seg';
      d.style.width = ((end - t0) / M.dur * 100) + '%';
      d.textContent = ch.label;
      d.title = ch.label;
      bar.appendChild(d);
      return { el: d, t0, t1: end };
    });
  }
  const mmss = v => Math.floor(v / 60) + ':' + String(Math.floor(v % 60)).padStart(2, '0');
  function updateUI() {
    if (!ui) return;
    ui.prog.style.width = (M.t / M.dur * 100) + '%';
    const real = v => v / M.base;
    const left = real(M.dur - M.t) / M.speed;
    ui.time.textContent = mmss(real(M.t)) + ' / ' + mmss(real(M.dur));
    ui.speed.title = M.speed + '×로 남은 시간 ' + mmss(left);
    ui.play.textContent = M.playing ? '❚❚' : '▶';
    ui.bar.setAttribute('aria-valuenow', M.t.toFixed(1));
    for (const s of ui.segs) s.el.classList.toggle('on', M.t >= s.t0 && M.t < s.t1);
  }

  M.seek = function (t) { M.render(Math.max(0, Math.min(M.dur, t))); };
  M.seekStory = function (st) { M.seek(M.toPlay(st)); };
  M.play = function () { if (M.t >= M.dur) M.render(0); M.playing = true; last = null; updateUI(); };
  M.pause = function () { M.playing = false; updateUI(); };
  M.toggle = function () { M.playing ? M.pause() : M.play(); };

  let last = null;
  function loop(ts) {
    if (M.playing) {
      if (last != null) {
        const nt = M.t + (ts - last) / 1000 * M.speed * M.base;
        if (nt >= M.dur) { M.render(M.dur); M.playing = false; updateUI(); }
        else M.render(nt);
      }
      last = ts;
    } else last = null;
    requestAnimationFrame(loop);
  }

  function fit() {
    const vp = document.getElementById('viewport'), st = document.getElementById('stage'), c = document.getElementById('controls');
    const de = document.documentElement;
    const vb = document.getElementById('viewbar');
    const ex = de.classList.contains('export');
    const k = Math.min((de.clientWidth - (ex ? 0 : 16)) / W, (de.clientHeight - c.offsetHeight - (vb ? vb.offsetHeight : 0) - (ex ? 0 : 20)) / H);
    st.style.transform = `scale(${k})`;
    vp.style.width = W * k + 'px';
    vp.style.height = H * k + 'px';
    c.style.width = W * k + 'px';
    if (vb) vb.style.width = W * k + 'px';
  }

  // 장면 함수를 그려 넣는다. 관점 전환 때 다시 부른다
  M.load = function (scene) {
    scene();
    buildUI();
    M.render(0);
  };
  M.chapterAt = function () {
    let i = 0;
    M.chapters.forEach((c, k) => { if (M.st >= c.t) i = k; });
    return i;
  };

  M.start = function () {
    const q0 = new URLSearchParams(location.search);
    M.speed = parseFloat(q0.get('speed')) || 1;
    buildUI();
    fit();
    addEventListener('resize', fit);
    addEventListener('keydown', ev => {
      if (ev.key === ' ') { ev.preventDefault(); M.toggle(); }
      else if (ev.key === 'ArrowRight') M.seek(M.t + (ev.shiftKey ? 0.1 : 3) * M.base);
      else if (ev.key === 'ArrowLeft') M.seek(M.t - (ev.shiftKey ? 0.1 : 3) * M.base);
      else if (ev.key === 'Home') M.seek(0);
    });
    const q = new URLSearchParams(location.search);
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    M.render(q.has('t') ? M.toPlay(parseFloat(q.get('t'))) : 0);
    M.playing = q.get('play') === '1' || (!q.has('t') && q.get('play') !== '0' && !reduce);
    updateUI();
    requestAnimationFrame(loop);
  };

  window.M = M;
})();
