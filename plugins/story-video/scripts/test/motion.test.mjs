import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import test from 'node:test';
import * as motion from '../cutscene.mjs';

const scene = (dur, starts = [], kb, start = 0) => ({
  start, dur, kb, lines: starts.map(at => ({ start: start + at })),
});
const anim = { cols: 2, rows: 2, fps: 8, seq: [0, 1, 2, 3, 2, 1] };
const cuts = shot => shot.map(({ from, to }) => [from, to]);

test('짧은 장면은 한 컷이고 문장 경계에 맞추되 끝을 짧게 자르지 않는다', () => {
  assert.deepEqual(cuts(motion.shotPlan(scene(3.9, [0.6, 2]), {})), [[0, 117]]);
  assert.deepEqual(cuts(motion.shotPlan(scene(9, [0.6, 2.4, 5.1, 7.8], 'in', 17.4), {})),
    [[0, 153], [153, 270]]);
  assert.deepEqual(cuts(motion.shotPlan(scene(6, [0.6, 2, 4.1]), {})), [[0, 180]]);
  const frameAligned = { start: 68 / 30, dur: 9, lines: [{ start: 86 / 30 }, { start: 248 / 30 }] };
  assert.deepEqual(cuts(motion.shotPlan(frameAligned, {})),
    [[0, 180], [180, 270]]);
});

test('긴 컷은 균등하게 나누고 이동 방향은 번갈아 쓴다', () => {
  const shots = motion.shotPlan(scene(17.5, [0.6], 'left'), {});
  assert.deepEqual(cuts(shots), [[0, 131], [131, 263], [263, 394], [394, 525]]);
  assert.deepEqual(shots.map(shot => shot.kind), ['left', 'close', 'right', 'close']);
  assert.deepEqual(motion.shotPlan(scene(4.4, [], 'right'), {}), [{ from: 0, to: 132, kind: 'right' }]);
  for (const kb of [undefined, 'unexpected', '']) {
    assert.equal(motion.shotPlan(scene(2, [], kb), {})[0].kind, 'in');
  }
  assert.deepEqual(motion.shotPlan(scene(17.5), { anim }), [{ from: 0, to: 525, kind: 'drift' }]);
});

test('격자는 그림 비율과 칸 크기를 함께 따진다', () => {
  for (const [format, cols, rows, cell] of [
    ['shorts', 2, 2, [512, 768]], ['shorts', 3, 2, [341, 512]], ['shorts', 3, 3, [341, 512]],
    ['wide', 2, 2, [768, 512]], ['wide', 2, 3, [512, 341]], ['wide', 3, 3, [512, 341]],
  ]) {
    const result = motion.gridCell(format, cols, rows);
    assert.deepEqual(result.cell, cell);
    assert.ok(result.aspectError <= 0.12);
    assert.equal(result.readable, true);
  }
  assert.deepEqual(motion.gridCell('shorts', 2, 3),
    { sheet: [1024, 1536], cell: [512, 512], aspectError: 0.5, readable: false });
  for (const format of ['wide', 'shorts']) assert.equal(motion.gridCell(format, 4, 4).readable, false);
});

const exclamation = {
  start: 522 / 30, dur: 4,
  lines: [
    { start: 540 / 30, t: '“거기 서!”  ' },
    { start: 18.5, t: '정말!?' },
    { start: 19, t: '진짜?!”' },
    { start: 20, t: '놀랐지만! 계속 갔다.' },
  ],
};

test('느낌표 확대는 장면마다 첫 번째 한 번이고 shake와 함께 장면 기준 프레임으로 센다', () => {
  assert.deepEqual(motion.hitEvents(exclamation, []), [18]);
  assert.deepEqual(motion.hitEvents(exclamation, ['shake']), [9, 18]);
  assert.deepEqual(motion.hitEvents({ ...exclamation, dur: 0.4 }, ['shake']).slice(0, 1), [6]);
  assert.deepEqual(motion.hitEvents({ ...exclamation, start: 18 }, ['shake']), [0, 9]);
});

test('일반 모드는 Ken Burns, 컷씬은 컷과 시트 칸으로 정지 구간을 잰다', () => {
  assert.deepEqual(motion.motionReport(scene(17.5), { cutscene: false, anim }),
    { cuts: 1, stills: [{ from: 0, to: 17.5 }] });
  assert.deepEqual(motion.motionReport(scene(4.4, [], 'left'), { cutscene: true }), { cuts: 1, stills: [] });
  assert.deepEqual(motion.motionReport(scene(17.5), { cutscene: true }), { cuts: 4, stills: [] });
  assert.deepEqual(motion.motionReport(scene(13, [0.2, 6.6], 'in'), { cutscene: true }), { cuts: 2, stills: [] });
  assert.deepEqual(motion.motionReport(scene(5), { cutscene: true, anim }), { cuts: 1, stills: [] });
  assert.deepEqual(motion.motionReport(scene(5), { cutscene: true, anim: { ...anim, seq: [0] } }),
    { cuts: 1, stills: [{ from: 0, to: 5 }] });
  assert.deepEqual(motion.motionReport(scene(5), {
    cutscene: true, anim: { ...anim, fps: 4, seq: [0, 1], loop: false },
  }), { cuts: 1, stills: [{ from: 0.26666666666666666, to: 5 }] });
});

test('느린 시트일수록 칸 사이를 길게 섞는다', () => {
  assert.equal(motion.blendFilter(8), "tmix=frames=3:weights='1 2 1',");
  assert.equal(motion.blendFilter(1), "tmix=frames=7:weights='1 2 3 4 3 2 1',");
});

test('두 모드에서 프리셋을 합치고 style의 기존 값은 받는다', () => {
  for (const style of [{ mode: 'cutscene' }, {}, { mode: 'other', prompt: 'ink' }, 'ink']) {
    assert.doesNotThrow(() => motion.validateCutscene({ style, scenes: [] }));
  }
  for (const preset of ['photoreal', 'webtoon']) {
    const style = { preset, prompt: 'ink' };
    assert.equal(motion.styleText({ style }), motion.PRESETS[preset] + ', ink');
    assert.equal(motion.styleText({ style: { preset } }), motion.PRESETS[preset]);
    assert.doesNotThrow(() => motion.validateCutscene({ style, scenes: [] }));
  }
  for (const style of [{ preset: 'unknown' }, { preset: 'toString' }, { prompt: 1 }]) {
    assert.throws(() => motion.validateCutscene({ style, scenes: [] }), /style\.(preset|prompt)/);
  }
});

test('시트 기본값과 장면별 덮어쓰기·해제는 컷씬에만 적용한다', () => {
  const override = { ...anim, fps: 12, blend: false };
  const story = { style: { mode: 'cutscene', anim }, scenes: [
    { id: 's01' }, { id: 's02', anim: override }, { id: 's03', anim: false },
  ] };
  assert.doesNotThrow(() => motion.validateCutscene(story));
  assert.equal(motion.sceneAnim(story, story.scenes[0]), anim);
  assert.deepEqual(motion.resolveScenes(story).map(scene => scene.anim), [anim, override, undefined]);
  assert.equal(story.scenes[2].anim, false);
  const normal = { ...story, style: { anim } };
  assert.deepEqual(motion.resolveScenes(normal).map(scene => scene.anim), [undefined, undefined, undefined]);
  for (const invalid of [false, null, { ...anim, blend: 1 }]) {
    assert.throws(() => motion.validateCutscene({ ...story, style: { ...story.style, anim: invalid } }), /style\.anim/);
  }
  assert.throws(() => motion.validateCutscene({ ...story, scenes: [{ id: 's01', anim: { ...anim, blend: 1 } }] }),
    /s01.*anim/);
});

const available = spawnSync('ffmpeg', ['-version']).status === 0;
const w = 96, h = 96;
function render(filter, frames) {
  return execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=192x192:r=30',
    '-filter_threads', '1', '-vf', filter, '-frames:v', String(frames), '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']);
}

test('ffmpeg에서 18프레임 펀치는 26에서, 흔들림은 27에서 끝난다', { skip: !available }, () => {
  const options = { w, h, frames: 30, still: true, focusY: 0.5 };
  const shots = motion.shotPlan(scene(1), {});
  const original = render(motion.cameraFilter(shots, [], options), 30);
  const hit = motion.hitEvents({ ...exclamation, lines: exclamation.lines.slice(0, 1) }, []);
  const punch = render(motion.cameraFilter(shots, hit, options), 30);
  const shake = render(motion.cameraFilter(shots, hit, { ...options, shake: true }), 30);
  const pixels = (data, frame) => data.subarray(frame * w * h * 3, (frame + 1) * w * h * 3);
  assert.deepEqual(pixels(punch, 17), pixels(original, 17));
  assert.notDeepEqual(pixels(punch, 18), pixels(original, 18));
  assert.notDeepEqual(pixels(punch, 25), pixels(original, 25));
  assert.deepEqual(pixels(punch, 26), pixels(original, 26));
  assert.notDeepEqual(pixels(shake, 26), pixels(original, 26));
  assert.deepEqual(pixels(shake, 27), pixels(original, 27));
});

test('ffmpeg의 시트 시퀀스에서도 on이 장면 전체에 걸쳐 증가한다', { skip: !available }, () => {
  const frames = 30;
  const shots = motion.shotPlan(scene(1), { anim });
  const camera = motion.cameraFilter(shots, [], { w, h, frames, still: false, focusY: 0.5 });
  const sequence = render('loop=loop=-1:size=1:start=0,' + camera, frames);
  const still = render(motion.cameraFilter(shots, [], { w, h, frames, still: true, focusY: 0.5 }), frames);
  assert.notDeepEqual(sequence.subarray(0, w * h * 3), sequence.subarray(-w * h * 3));
  assert.deepEqual(sequence, still);
});

test('ffmpeg가 72컷 카메라 식을 해석한다', { skip: !available }, () => {
  const frames = 360 * 30;
  const shots = motion.shotPlan(scene(360), {});
  assert.equal(shots.length, 72);
  assert.equal(render(motion.cameraFilter(shots, [], { w, h, frames, still: true, focusY: 0.5 }), 1).length,
    w * h * 3);
});
