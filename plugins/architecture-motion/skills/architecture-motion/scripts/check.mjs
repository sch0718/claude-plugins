// 사용: node check.mjs <프로젝트 폴더|index.html> [--out 캡처폴더] [--no-shots]
// 관점마다 전 구간 렌더링 오류, 글자 넘침·말풍선 겹침·자막 넘침을 점검하고 멈춤 구간마다 화면을 캡처한다.
import { openViewer } from './lib.mjs';
import { mkdirSync } from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const target = args[0];
if (!target) { console.error('사용: node check.mjs <프로젝트 폴더|index.html> [--out 폴더] [--no-shots]'); process.exit(2); }
const outIdx = args.indexOf('--out');
const out = path.resolve(outIdx > 0 ? args[outIdx + 1] : path.join(path.dirname(path.resolve(target)), 'check-shots'));
const shots = !args.includes('--no-shots');
if (shots) mkdirSync(out, { recursive: true });

let failed = false;
const first = await openViewer(target, {});
const views = await first.page.evaluate(() => Object.keys(window.SCENES || {}));
await first.browser.close();
if (!views.length) { console.error('✕ window.SCENES 가 비어 있습니다'); process.exit(1); }

for (const view of views) {
  const { browser, page, errors } = await openViewer(target, { view });
  try {
    const info = await page.evaluate(() => {
      const fmt = v => Math.floor(v / M.base / 60) + ':' + String(Math.round(v / M.base % 60)).padStart(2, '0');
      let err = null;
      try { for (let t = 0; t <= M.dur; t += 0.05) M.render(t); } catch (e) { err = String(e && e.stack || e); }
      return {
        err, story: M.story, real: M.dur / M.base,
        chapters: M.chapters.map(c => `${c.label} ${fmt(M.toPlay(c.t))}`),
        holds: M.holds.length, caps: M.caps.length,
        gaps: (() => {
          // 자막이 없는 이야기 구간(장 경계 사이 빈틈)
          const g = []; const cs = M.caps.slice().sort((a, b) => a.t0 - b.t0);
          let last = 0;
          for (const c of cs) { if (c.t0 - last > 0.05) g.push(`${last.toFixed(2)}~${c.t0.toFixed(2)}`); last = Math.max(last, c.t1); }
          if (M.story - last > 0.05) g.push(`${last.toFixed(2)}~${M.story}`);
          return g;
        })(),
      };
    });
    console.log(`\n■ ${view}: 이야기 ${info.story}초 · 실제 재생 ${Math.floor(info.real / 60)}분 ${Math.round(info.real % 60)}초 · 장 ${info.chapters.length} · 멈춤 ${info.holds} · 자막 ${info.caps}`);
    console.log('  장: ' + info.chapters.join(' | '));
    if (info.err) { failed = true; console.log('  ✕ 렌더 오류: ' + info.err); }
    if (info.gaps.length) console.log('  ⚠ 자막 없는 구간(이야기 초): ' + info.gaps.join(', '));

    // 점검 시점: 각 멈춤 구간의 중간(내용이 다 나온 상태) + 마지막 장면
    const points = await page.evaluate(() => {
      const p = M.holds.map(([ht, hd]) => ({ st: ht, rt: M.toPlay(ht) + hd * 0.6 }));
      p.push({ st: M.story, rt: M.dur });
      return p;
    });
    const warnings = new Map();
    for (const [i, pt] of points.entries()) {
      const found = await page.evaluate(rt => {
        M.render(rt);
        for (const a of document.getAnimations()) a.finish();
        const w = [];
        const shown = el => { for (let e = el; e && e.nodeType === 1; e = e.parentElement || e.parentNode) { const cs = getComputedStyle(e); if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) < 0.5) return false; } return true; };
        const name = t => t.textContent.trim().slice(0, 30);
        // 말풍선 글자 넘침
        for (const g of document.querySelectorAll('g.co')) {
          if (!shown(g)) continue;
          const box = g.querySelector('.co-box'); if (!box) continue;
          const bw = +box.getAttribute('width');
          for (const t of g.querySelectorAll('text')) {
            const b = t.getBBox();
            if (b.x + b.width > bw - 4 || b.x < 2) w.push(`말풍선 글자 넘침: "${name(t)}" (글자 ${Math.round(b.x + b.width)}px / 상자 ${bw}px)`);
          }
        }
        // 칩·상자 글자 넘침
        for (const g of document.querySelectorAll('g.pod, g.ext')) {
          if (!shown(g)) continue;
          const box = g.querySelector('rect.body, rect.ext-box'); if (!box) continue;
          const bw = +box.getAttribute('width');
          for (const t of g.querySelectorAll('text.lbl, .ext-title, .ext-sub, .ext-subdown')) {
            if (!shown(t)) continue;
            const b = t.getBBox();
            if (b.width > bw - 6) w.push(`칩·상자 글자 넘침: "${name(t)}" (글자 ${Math.round(b.width)}px / 상자 ${bw}px)`);
          }
        }
        // 말풍선끼리 겹침 · 무대 밖
        const boxes = [...document.querySelectorAll('g.co .co-box')].filter(shown).map(r => ({ r: r.getBoundingClientRect(), t: name(r.parentNode.querySelector('text')) }));
        const stage = document.getElementById('stage').getBoundingClientRect();
        boxes.forEach((a, i) => {
          if (a.r.left < stage.left - 1 || a.r.right > stage.right + 1 || a.r.top < stage.top - 1 || a.r.bottom > stage.bottom + 1) w.push(`무대 밖으로 나감: "${a.t}"`);
          for (const b of boxes.slice(i + 1)) {
            const ox = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
            const oy = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
            if (ox > 2 && oy > 2) w.push(`말풍선 겹침: "${a.t}" ↔ "${b.t}"`);
          }
        });
        // 멈춤 구간에서 반쯤만 보이는 말풍선·카운터 — 나타나거나 사라지는 도중에 멈춤이 걸렸다
        for (const it of M.items) {
          if (!it.textEls && !it.num) continue;
          if (it.cur.o > 0.05 && it.cur.o < 0.95) w.push(`반투명한 채 멈춤: "${(it.g.querySelector('text') || {}).textContent || ''}" — 멈춤은 나타난 시각+0.4초 뒤, 사라지기 시작 전에 둔다`);
        }
        // 자막 넘침·자막과 말풍선 겹침
        const cap = document.getElementById('cap');
        if (cap && cap.style.visibility !== 'hidden') {
          if (cap.scrollHeight > cap.clientHeight + 1) w.push(`자막 넘침: "${cap.textContent.trim().slice(0, 30)}"`);
          const cr = cap.getBoundingClientRect();
          for (const b of boxes) if (b.r.bottom > cr.top + 2 && b.r.top < cr.bottom) w.push(`말풍선이 자막을 가림: "${b.t}"`);
        }
        return w;
      }, pt.rt);
      for (const x of found) { if (!warnings.has(x)) warnings.set(x, []); warnings.get(x).push(pt.st.toFixed(2)); }
      if (shots) await page.screenshot({ path: path.join(out, `${view}-${String(i).padStart(2, '0')}-${pt.st.toFixed(2)}s.jpg`), type: 'jpeg', quality: 85 });
    }
    for (const [w, ts] of warnings) console.log(`  ⚠ ${w}  @이야기 ${ts.slice(0, 4).join(', ')}초${ts.length > 4 ? ` 외 ${ts.length - 4}곳` : ''}`);
    if (!warnings.size) console.log('  ✓ 글자 넘침·겹침 없음');
    if (errors.length) { failed = true; for (const e of new Set(errors)) console.log('  ✕ 브라우저 오류: ' + e); }
  } finally {
    await browser.close();
  }
}
if (shots) console.log(`\n캡처: ${out}`);
process.exit(failed ? 1 : 0);
