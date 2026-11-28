import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

function episode(t, extra = {}) {
  const ep = mkdtempSync(path.join(os.tmpdir(), 'story-video-check-'));
  t.after(() => rmSync(ep, { recursive: true, force: true }));
  mkdirSync(path.join(ep, 'out'));
  const line = { who: 'narr', t: '나무가 있다.' };
  const scenes = [{ id: 's01', beat: '기', lines: [line] }];
  writeFileSync(path.join(ep, 'story.json'), JSON.stringify({ format: 'shorts', scenes, ...extra }));
  const timeline = data => writeFileSync(path.join(ep, 'out', 'timeline.json'), JSON.stringify(data));
  const check = (...flags) => spawnSync(process.execPath, [path.join(import.meta.dirname, '../check.mjs'), ep, ...flags], { encoding: 'utf8' });
  return { ep, timeline, check };
}

test('쇼츠 상한은 시간표 길이에 적용하며 옵션으로 올릴 수 있다', t => {
  const { timeline, check } = episode(t);
  timeline({ total: 200 });
  const standard = check();
  assert.equal(standard.status, 1, standard.stderr);
  assert.match(standard.stdout, /시간표 길이가.*범위를 벗어났다/);
  assert.match(standard.stdout, /쇼츠 길이.*상한 180초/);

  const long = check('--min', '190', '--max', '660');
  assert.equal(long.status, 0, long.stderr);
  assert.doesNotMatch(long.stdout, /길이가.*범위를 벗어났다|쇼츠 길이.*상한/);
  assert.equal(check('--max', '200').status, 0);
});

test('시간표가 없으면 어림 길이로 범위를 판정한다', t => {
  const { check } = episode(t);
  const result = check('--min', '100');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /어림 길이가.*범위를 벗어났다/);
});

test('motion 점검은 배정된 전환만 세고 recap은 전환 없는 장면으로 센다', t => {
  const lines = [{ who: 'narr', t: '나무가 있다.' }];
  const { ep, timeline, check } = episode(t, { style: { mode: 'motion' }, recap: '지난 이야기.',
    scenes: [{ id: 's01', keys: ['turn', 'look'], lines }, { id: 's02', keys: [], lines }] });
  timeline({ total: 15, scenes: [
    { id: 'recap', art: 's01', dur: 3, lines: [] },
    { id: 's01', art: 's01', dur: 10, lines: [] },
    { id: 's02', art: 's02', dur: 2, lines: [] },
  ] });
  const dir = path.join(ep, 'art', 'shorts');
  mkdirSync(path.join(dir, 'keys'), { recursive: true });
  for (const file of ['s01.png', 's02.png', 'keys/s01-1.png', 'keys/s01-3.png']) writeFileSync(path.join(dir, file), '');
  const result = check();
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /s01 키프레임 2번이 없다/);
  assert.doesNotMatch(result.stdout, /키프레임 [13]번이 없다|정지 구간/);
  assert.match(result.stdout, /장면당 키프레임 전환 평균 0\.3회 · 키프레임 없는 장면 2개/);
});

test('motion 대본의 키프레임 설정이 잘못되면 점검이 실패한다', t => {
  const { check } = episode(t, { style: { mode: 'motion' },
    scenes: [{ id: 's01', keys: [''], lines: [{ who: 'narr', t: '나무가 있다.' }] }] });
  const result = check();
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stdout, /대본이나 화면 형식.*s01.*keys/);
});
