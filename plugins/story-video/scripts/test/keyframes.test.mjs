import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  DEFAULT_KEY, keyFresh, keyPath, keyPlan, keyRecord, motionArgs, motionCamera, motionInputs, motionMode, sceneKeys,
  tailFrames, validateMotion,
} from '../keyframes.mjs';
import { FORMATS, FPS } from '../lib.mjs';

test('motion 대본에서 보간 방식과 키프레임 설명을 검증한다', () => {
  for (const keys of [['a', 'b', 'c', 'd'], ['  '], [1], null, 'turn']) {
    assert.throws(() => validateMotion({ style: { mode: 'motion' }, scenes: [{ id: 's01', keys }] }), /s01.*keys/);
  }
  for (const interp of ['blur', null]) {
    assert.throws(() => validateMotion({ style: { mode: 'motion', interp }, scenes: [] }), /style.interp/);
  }
  const valid = { style: { mode: 'motion', interp: 'mci' }, scenes: [{ keys: [] }, { keys: ['turn'] }, {}] };
  assert.equal(motionMode(valid), true);
  assert.equal(validateMotion(valid), valid);
  const cutscene = { style: { mode: 'cutscene', interp: 'blur' }, scenes: [{ keys: [1] }] };
  assert.equal(motionMode(cutscene), false);
  assert.equal(validateMotion(cutscene), cutscene);
});

test('키프레임 설명을 생략한 장면에는 기본값을 쓰고 recap에는 쓰지 않는다', () => {
  assert.deepEqual(sceneKeys(undefined), []);
  assert.deepEqual(sceneKeys({}), [DEFAULT_KEY]);
  assert.deepEqual(sceneKeys({ keys: [] }), []);
  assert.deepEqual(sceneKeys({ keys: ['turn', 'look'] }), ['turn', 'look']);
});

test('그림을 되돌아가지 않고 각각 최소 4초를 배정한다', () => {
  assert.deepEqual(keyPlan(90, 2), { used: 1, changes: [] });
  assert.deepEqual(keyPlan(240, 2), { used: 2, changes: [120] });
  assert.deepEqual(keyPlan(600, 4), { used: 4, changes: [150, 300, 450] });
  assert.deepEqual(keyPlan(600, 1), { used: 1, changes: [] });
  assert.deepEqual(keyPlan(600, 0), { used: 0, changes: [] });
  for (let frames = 120; frames <= 600; frames++) {
    const { changes } = keyPlan(frames, 4);
    const boundaries = [0, ...changes, frames];
    assert.ok(boundaries.slice(1).every((end, i) => end - boundaries[i] >= 120));
  }
});

test('장면 끝 디졸브는 최대 30프레임이며 장면의 절반을 넘지 않는다', () => {
  assert.equal(tailFrames(1), 0);
  assert.equal(tailFrames(3), 1);
  assert.equal(tailFrames(300), 30);
});

test('키프레임은 기본 그림보다 오래되지 않았고 저장된 요청문이 같을 때만 다시 쓴다', t => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'story-video-key-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const base = path.join(dir, 'base.png');
  const key = path.join(dir, 'key.png');
  const record = path.join(dir, 'key.txt');
  const inputs = { key, record, base, prompt: 'turn' };
  assert.equal(keyPath(dir, 'wide', 's01', 1), path.join(dir, 'art/wide/keys/s01-1.png'));
  assert.equal(keyRecord(dir, 'wide', 's01', 1), path.join(dir, 'art/wide/keys/s01-1.txt'));
  assert.equal(keyFresh(inputs), false);
  writeFileSync(base, '');
  writeFileSync(key, '');
  writeFileSync(record, inputs.prompt);
  utimesSync(base, 2, 2);
  utimesSync(key, 1, 1);
  assert.equal(keyFresh(inputs), false);
  utimesSync(key, 2, 2);
  assert.equal(keyFresh({ ...inputs, prompt: 'look' }), false);
  assert.equal(keyFresh(inputs), true);
});

test('느린 카메라는 입력 프레임마다 한 장을 내보내고 축소의 첫 배율은 1.05다', () => {
  const format = { w: 160, h: 90 };
  for (const kb of ['in', 'out', 'left', 'right', undefined]) {
    const camera = motionCamera(120, kb, format);
    assert.match(camera, /^zoompan=/);
    assert.match(camera, /:d=1:s=160x90:fps=30$/);
  }
  assert.match(motionCamera(120, 'out', format), /1\.05-0\.05\*/);
  assert.equal(motionCamera(120, undefined, format), motionCamera(120, 'in', format));
});

const ffmpegAvailable = ['ffmpeg', 'ffprobe'].every(bin => spawnSync(bin, ['-version']).status === 0);
const ffmpeg = args => execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-y', ...args], {
  maxBuffer: 4 * 1024 * 1024,
});

function renderFixture(t) {
  const ep = mkdtempSync(path.join(os.tmpdir(), 'story-video-motion-'));
  const formatName = 'motion-test';
  const w = 96;
  const h = 64;
  const frameBytes = w * h * 3 / 2;
  FORMATS[formatName] = { w, h };
  t.after(() => {
    delete FORMATS[formatName];
    rmSync(ep, { recursive: true, force: true });
  });
  const artDir = path.join(ep, 'art', formatName);
  mkdirSync(path.join(artDir, 'keys'), { recursive: true });
  const a = path.join(artDir, 's01.png');
  const b = keyPath(ep, formatName, 's01', 1);
  const c = path.join(artDir, 's02.png');
  const d = keyPath(ep, formatName, 's02', 1);
  const placeholder = path.join(artDir, 's03.png');
  const draw = (out, filter) => ffmpeg(['-f', 'lavfi', '-i', filter,
    '-frames:v', '1', '-pix_fmt', 'rgb24', '-update', '1', out]);
  draw(a, 'color=red:s=96x64');
  draw(b, 'color=blue:s=96x64');
  draw(c, 'color=lime:s=128x92,drawbox=x=24:y=9:w=12:h=31:c=white:t=fill,drawgrid=w=30:h=41:t=2:c=black');
  draw(d, 'color=purple:s=96x64');
  const item = (id, art, frames, options = {}) => ({
    scene: { id, dur: frames / FPS, kb: 'in' },
    storyScene: { id, keys: id === 's03' ? [] : ['turn the head'] },
    ep, formatName, art, index: 1, interp: 'fade', first: false, last: false, next: null, ...options,
  });
  const raw = async job => ffmpeg([...await motionArgs(job, ep), '-c:v', 'rawvideo',
    '-threads', '1', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', 'pipe:1']);
  const frame = (video, n) => video.subarray(n * frameBytes, (n + 1) * frameBytes);
  const pixel = buffer => [...execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-f', 'rawvideo', '-pixel_format',
    'yuv420p', '-video_size', `${w}x${h}`, '-i', 'pipe:0', '-frames:v', '1', '-vf', 'scale=1:1',
    '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], { input: buffer })];
  return { ep, formatName, item, a, b, c, d, placeholder, draw, raw, frame, pixel, frameBytes };
}

test('fade는 장면의 프레임 수와 그림 순서를 지키고 다음 장면의 첫 프레임에 도달한다', { skip: !ffmpegAvailable }, async t => {
  const { item, a, c, raw, frame, pixel, frameBytes } = renderFixture(t);
  const current = item('s01', a, 240, { first: true, next: { art: c, dur: 8, kb: 'out' } });
  const next = item('s02', c, 240, { last: true, scene: { id: 's02', dur: 8, kb: 'out' } });
  const video = await raw(current);
  assert.equal(video.length / frameBytes, 240);
  assert.ok(pixel(frame(video, 16))[0] > 210);
  assert.ok(pixel(frame(video, 180))[2] > 210);
  assert.ok(pixel(frame(video, 239))[1] > 150);
  const nextVideo = await raw(next);
  assert.deepEqual(frame(video, 239), frame(nextVideo, 0));
  const short = await raw({ ...current, scene: { ...current.scene, dur: 3 / FPS } });
  assert.equal(short.length / frameBytes, 3);
  assert.deepEqual(frame(short, 2), frame(nextVideo, 0));
});

test('248프레임 장면에서 fade와 mci는 전환 직후 앞 그림으로 되돌아가지 않는다', { skip: !ffmpegAvailable }, async t => {
  const { item, a, b, draw, raw, frame, frameBytes } = renderFixture(t);
  draw(a, 'color=navy:s=96x64,drawbox=x=18:y=16:w=22:h=24:c=yellow:t=fill');
  draw(b, 'color=navy:s=96x64,drawbox=x=30:y=16:w=22:h=24:c=yellow:t=fill');
  const current = item('s01', a, 248);
  const expected = await raw({ ...current, art: b, storyScene: undefined });
  const warn = t.mock.method(console, 'warn', () => {});
  for (const interp of ['fade', 'mci']) {
    const video = await raw({ ...current, interp });
    assert.equal(video.length / frameBytes, 248);
    assert.notDeepEqual(frame(video, 123), frame(expected, 123), interp);
    assert.deepEqual(frame(video, 169), frame(expected, 169), interp);
  }
  const video = await raw({ ...current, interp: 'mci', scene: { ...current.scene, dur: 8 } });
  assert.equal(video.length / frameBytes, 240);
  assert.equal(warn.mock.callCount(), 0);
});

test('그림이 없는 장면 앞뒤에서 검정 페이드와 다음 그림 디졸브를 적용한다', { skip: !ffmpegAvailable }, async t => {
  const { item, a, c, placeholder, raw, frame, pixel } = renderFixture(t);
  const current = item('s01', a, 60, { next: { art: null, dur: 2, kb: 'in' } });
  const color = item('s03', placeholder, 60, { next: { art: c, dur: 8, kb: 'out' } });
  const next = item('s02', c, 240, { last: true, scene: { id: 's02', dur: 8, kb: 'out' } });
  t.mock.method(console, 'warn', () => {});
  const before = await raw(current);
  const middle = await raw(color);
  const after = await raw(next);
  assert.ok(pixel(frame(before, 50))[0] > 210);
  assert.deepEqual(pixel(frame(before, 59)), [0, 0, 0]);
  assert.deepEqual(pixel(frame(middle, 0)), [0, 0, 0]);
  assert.ok(pixel(frame(middle, 10)).every(value => value > 15));
  assert.deepEqual(frame(middle, 59), frame(after, 0));
});

test('렌더 입력은 현재 목록의 있는 그림 중 배정된 경로와 다음 장면 그림을 기록한다', { skip: !ffmpegAvailable }, t => {
  const { ep, formatName, item, a, b, c } = renderFixture(t);
  const current = item('s01', a, 240, { next: { art: c, dur: 8, kb: 'out' },
    storyScene: { id: 's01', keys: ['turn', 'look'] } });
  const stale = keyPath(ep, formatName, 's01', 3);
  writeFileSync(stale, '');
  const { ready, manifest, files } = motionInputs(current);
  assert.equal(ready, true);
  assert.deepEqual(manifest.images, [a, b]);
  assert.deepEqual(manifest.keys, ['turn', 'look']);
  assert.equal(manifest.next, c);
  assert.equal(manifest.nextKb, 'out');
  assert.ok([a, b, c].every(file => files.includes(file)));
  assert.ok(!files.includes(stale));
  assert.deepEqual(motionInputs({ ...current, scene: { id: 's01', dur: 3 } }).manifest.images, [a]);
});
