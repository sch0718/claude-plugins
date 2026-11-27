import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { FORMATS } from '../lib.mjs';

import {
  animCell, animSequence, captionBox, captionLayout, cellRects, cutsceneMode, cutSheet,
  frameOrder, fxFilter, insideSafe, mixSfx, renderPanel, sfxEvents, sheetSize, styleText, validateCutscene,
} from '../cutscene.mjs';
const anim = { cols: 2, rows: 2, fps: 4, seq: [0, 2, 1] };
const story = scene => ({ style: { mode: 'cutscene', prompt: 'original style' }, scenes: [{ id: 's01', ...scene }] });

test('컷씬 모드와 그림 요청문', () => {
  assert.equal(cutsceneMode({ style: 'cutscene' }), false);
  assert.equal(cutsceneMode({ style: { mode: 'other' } }), false);
  assert.equal(cutsceneMode(story()), true);
  assert.equal(styleText({ style: 'ink' }), 'ink');
  assert.equal(styleText({ style: { prompt: 'ink' } }), 'ink');
  assert.equal(styleText(story()), 'original style');
  assert.doesNotThrow(() => validateCutscene({ style: 'ink', scenes: [{ fx: 'rain' }] }));
});

test('잘못된 컷씬 설정에는 장면 id가 나온다', () => {
  assert.doesNotThrow(() => validateCutscene(story({ fx: ['rain', 'dark'], sfx: [{ t: 0, name: 'a-b' }], anim })));
  for (const fx of ['rain', ['unknown'], null]) assert.throws(() => validateCutscene(story({ fx })), /s01.*fx/);
  for (const sfx of [[{ t: -1, name: 'a' }], [{ t: 0, name: '../a' }], [null]]) {
    assert.throws(() => validateCutscene(story({ sfx })), /s01.*sfx/);
  }
  for (const change of [{ cols: 0 }, { rows: 5 }, { fps: 0.49 }, { fps: 31 }, { loop: 1 },
    { seq: [] }, { seq: [4] }, { seq: [0.5] }, { seq: undefined, frames: 5 }, { seq: undefined }]) {
    assert.throws(() => validateCutscene(story({ anim: { ...anim, ...change } })), /s01.*anim/);
  }
});

test('칸 순서, 반복, 마지막 칸 멈춤', () => {
  assert.deepEqual(animSequence({ frames: 3 }), [0, 1, 2]);
  assert.equal(animCell(anim, 0.249), 0);
  assert.equal(animCell(anim, 0.25), 2);
  assert.equal(animCell(anim, 0.75), 0);
  assert.equal(animCell({ ...anim, loop: false }, 5), 1);
  assert.deepEqual(frameOrder(anim, 8, 8), [0, 0, 2, 2, 1, 1, 0, 0]);
  assert.deepEqual(frameOrder({ ...anim, fps: 30 }, 5, 30), [0, 2, 1, 0, 2]);
});

test('시트 크기와 나누어떨어지지 않는 격자', () => {
  assert.deepEqual(cellRects(11, 9, 2, 2), [
    { x: 0, y: 0, w: 5, h: 4 }, { x: 5, y: 0, w: 6, h: 4 },
    { x: 0, y: 4, w: 5, h: 5 }, { x: 5, y: 4, w: 6, h: 5 },
  ]);
  assert.deepEqual(sheetSize([1536, 1024], 2, 2), [1536, 1024]);
  assert.deepEqual(sheetSize([1024, 1536], 2, 2), [1024, 1536]);
  assert.deepEqual(sheetSize([1024, 1536], 3, 2), [1024, 1024]);
});

test('효과 필터와 30fps 기준 페이드', () => {
  const format = { w: 1080, h: 1920, frames: 30, fps: 30 };
  const { filter, textures } = fxFilter(['fog', 'dark', 'snow', 'rain', 'redmoon', 'flash'], format);
  for (const part of ['0x741b2c@0.25', '0x080817@0.46', 'fade=in:s=0:n=9:color=white', 'fade=out:s=21:n=8']) {
    assert.ok(filter.includes(part), part);
  }
  assert.equal((filter.match(/overlay=/g) ?? []).length, 3);
  assert.deepEqual(textures, ['fog', 'snow', 'rain']);
  assert.deepEqual(fxFilter(['shake'], format), fxFilter([], format));
  assert.match(fxFilter([], { ...format, frames: 8 }).filter, /fade=in:s=0:n=4,fade=out:s=4:n=3/);
  assert.throws(() => fxFilter(['unknown'], format), /효과/);
});

test('효과음 시각은 장면 id와 시작 시각으로 계산한다', () => {
  const source = story({ sfx: [{ t: 0.5, name: 'thunder' }] });
  source.scenes.unshift({ id: 's02', sfx: [{ t: 0, name: 'wind' }] });
  const timeline = { scenes: [{ id: 'recap', start: 0 }, { id: 's01', start: 2 }, { id: 's02', start: 5 }] };
  assert.deepEqual(sfxEvents(timeline, source), [{ t: 2.5, name: 'thunder' }, { t: 5, name: 'wind' }]);
  assert.deepEqual(sfxEvents(timeline, { ...source, style: 'ink' }), []);
});

test('컷씬 쇼츠의 세 줄 자막 띠는 안전 영역 안에 든다', () => {
  const { w, h, subs } = FORMATS.shorts;
  assert.deepEqual(captionLayout('shorts', false), { w, h, ...subs });
  const wide = FORMATS.wide;
  assert.deepEqual(captionLayout('wide', true), { w: wide.w, h: wide.h, ...wide.subs });
  const layout = captionLayout('shorts', true);
  assert.equal(layout.cx, 497);
  assert.equal(layout.bottom, 1420);
  assert.equal(layout.maxChars, 12);
  assert.equal(insideSafe(captionBox(layout, 3), 'shorts'), true);
  assert.equal(insideSafe({ x: 0, y: 1000, w: 500, h: 20 }, 'shorts'), false);
});

const ffmpeg = args => execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-y', ...args]);
const available = ['ffmpeg', 'ffprobe'].every(command => spawnSync(command, ['-version']).status === 0);

test('네 색 시트를 자르고, 빠진 칸을 복구하고, 패널 프레임 수를 맞춘다', { skip: !available }, async () => {
  const ep = mkdtempSync(path.join(tmpdir(), 'cutscene-test-'));
  try {
    const cells = path.join(ep, 'art', 'shorts', 'anim', 's01');
    const sheet = cells + '.png';
    const colors = ['red', 'lime', 'blue', 'white'];
    const rgb = [[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]];
    mkdirSync(path.dirname(sheet), { recursive: true });
    ffmpeg(['-f', 'lavfi', '-i', 'color=s=33x33,format=rgb24',
      '-vf', colors.map((color, i) => {
        const col = i % 2, row = Math.floor(i / 2);
        return `drawbox=x=${col * 16}:y=${row * 16}:w=${16 + col}:h=${16 + row}:c=${color}:t=fill`;
      }).join(','),
      '-frames:v', '1', '-update', '1', sheet]);
    const files = await cutSheet(sheet, 2, 2, cells);
    assert.equal(files.length, 4);
    files.forEach((file, i) => {
      const pixel = ffmpeg(['-i', file, '-vf', 'scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']);
      assert.ok(rgb[i].every((value, channel) => Math.abs(value - pixel[channel]) <= 3), file);
    });
    assert.equal(JSON.parse(readFileSync(path.join(cells, 'grid.json'))).cols, 2);
    rmSync(files[1]);
    await cutSheet(sheet, 2, 2, cells);
    assert.ok(existsSync(files[1]));
    const output = path.join(ep, 'panel.mp4');
    await renderPanel({ scene: { id: 's01', start: 0, dur: 1.1, lines: [] },
      storyScene: { id: 's01', anim, fx: ['redmoon', 'flash'] },
      index: 0, ep, formatName: 'shorts', art: path.join(ep, 'missing.png'), output });
    const frames = execFileSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-count_frames',
      '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', output], { encoding: 'utf8' });
    assert.equal(Number(frames), 33);
    const pixel = at => ffmpeg(['-ss', at, '-i', output, '-vf', 'scale=1:1', '-frames:v', '1',
      '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']);
    assert.ok(pixel('0.35')[2] > 150);
    const mixed = pixel('0.5');
    assert.ok(mixed[1] > 30 && mixed[2] > 30);
    assert.ok(pixel('0.55')[1] > 150);
  } finally {
    rmSync(ep, { recursive: true, force: true });
  }
});

test('효과음을 늦춰 섞고 파일을 지우면 원래 음성으로 돌아간다', { skip: !available }, async () => {
  const ep = mkdtempSync(path.join(tmpdir(), 'sfx-test-'));
  try {
    const audio = path.join(ep, 'audio.wav');
    const outDir = path.join(ep, 'out', 'shorts');
    const sound = path.join(ep, 'sfx', 'tone.wav');
    mkdirSync(path.dirname(sound));
    ffmpeg(['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono', '-t', '0.5', audio]);
    ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', sound]);
    const input = { timeline: { scenes: [{ id: 's01', start: 0.1 }] }, ep, audio, outDir,
      story: story({ sfx: [{ t: 0.1, name: 'tone' }] }) };
    const mixed = await mixSfx(input);
    const pcm = ffmpeg(['-i', mixed, '-f', 's16le', 'pipe:1']);
    assert.equal(pcm.length, 48000);
    assert.ok(pcm.subarray(0, 19000).every(byte => byte === 0));
    assert.ok(pcm.subarray(19400).some(byte => byte !== 0));
    const mtime = statSync(mixed).mtimeMs;
    assert.equal(await mixSfx(input), mixed);
    assert.equal(statSync(mixed).mtimeMs, mtime);
    rmSync(sound);
    assert.equal(await mixSfx(input), mixed);
    assert.deepEqual(readFileSync(mixed), readFileSync(audio));
  } finally {
    rmSync(ep, { recursive: true, force: true });
  }
});
