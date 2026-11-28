import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  LOCAL_PRESETS, LOCAL_SIZES, artEngine, editPrompt, flux2EditArgs, localPrompt, mfluxEnv,
  modelPath, readArtEnv, sceneSeed, zimageArgs,
} from '../local-art.mjs';

test('그림 엔진은 옵션, 대본, 기본값 순으로 고른다', () => {
  assert.equal(artEngine({}), 'codex');
  assert.equal(artEngine({ style: 'local' }), 'codex');
  assert.equal(artEngine({ style: { engine: 'local' } }), 'local');
  assert.equal(artEngine({ style: { engine: 'local' } }, 'codex'), 'codex');
  assert.throws(() => artEngine({}, 'gpu'), /codex.*local/);
});

test('로컬 요청문에 화풍, 장면, 인물, 금지 문구와 세로 구도를 넣는다', () => {
  const story = {
    style: { preset: 'photoreal', prompt: 'soft daylight' },
    characters: [{ id: 'c1', name: '인물 1', look: 'short hair' }],
  };
  const scene = { id: 's01', art: 'a quiet bus stop', cast: ['c1'] };
  const shorts = localPrompt(story, scene, 'shorts');
  const parts = [LOCAL_PRESETS.photoreal, 'soft daylight', scene.art, '인물 1: short hair',
    'fictional', 'no text', 'upper center'];
  let previous = -1;
  for (const part of parts) {
    const index = shorts.toLowerCase().indexOf(part.toLowerCase());
    assert.ok(index > previous, part);
    previous = index;
  }
  assert.doesNotMatch(localPrompt({ ...story, style: 'watercolor' }, scene, 'wide'), /upper center/);
  assert.match(localPrompt({ style: 'watercolor' }, { art: 'a tree' }, 'wide'), /^watercolor/);
  assert.throws(() => localPrompt(story, { ...scene, cast: ['missing'] }, 'wide'),
    /s01에 없는 인물 id가 있다\(missing\)/);
  assert.match(editPrompt('turn the head'), /same.*face.*turn the head.*no text/i);
  assert.doesNotMatch(editPrompt('turn the head.'), /\.\./);
});

test('장면 seed가 없으면 장면 id에서 고정된 seed를 구한다', () => {
  const seed = sceneSeed({ id: 's01' });
  assert.ok(Number.isSafeInteger(seed) && seed >= 0 && seed < 2 ** 31);
  assert.equal(sceneSeed({ id: 's01', seed: -1 }), seed);
  assert.equal(sceneSeed({ id: 's01' }), seed);
  assert.notEqual(sceneSeed({ id: 's02' }), seed);
  assert.equal(sceneSeed({ id: 's01', seed: 7 }), 7);
});

test('설정에서 두 모델 키만 읽고 실제 로컬 절대 경로인지 확인한다', t => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'story-video-env-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const model = path.join(dir, 'model #1');
  const file = path.join(dir, 'art.env');
  mkdirSync(model);
  writeFileSync(file, `# 모델 설정\nZIMAGE_MODEL="${model}" # 생성\nFLUX2_MODEL='~/models/flux'\nOTHER=ignored\n`);
  const env = readArtEnv(file);
  assert.deepEqual(env, { ZIMAGE_MODEL: model, FLUX2_MODEL: path.join(os.homedir(), 'models/flux') });
  assert.equal(modelPath(env, 'ZIMAGE_MODEL'), model);
  for (const value of [undefined, '', 'relative/path', path.join(dir, 'missing')]) {
    assert.throws(() => modelPath({ FLUX2_MODEL: value }, 'FLUX2_MODEL'), /FLUX2_MODEL/);
  }
  assert.deepEqual(readArtEnv(path.join(dir, 'missing.env')), {});
});

test('mflux 인자는 로컬 모델과 크기를 넘기고 요청한 경우에만 저메모리 옵션을 붙인다', () => {
  const options = { model: '/path/to/model', promptFile: '/path/to/prompt.txt', seed: 7, output: '/path/to/raw.png' };
  const zimage = ['--model', options.model, '--base-model', 'z-image-turbo',
    '--prompt-file', options.promptFile, '--width', '1152', '--height', '768',
    '--steps', '9', '--seed', '7', '--output', options.output];
  const image = '/path/to/scene.png';
  const flux = ['--model', options.model, '--base-model', 'flux2-klein-4b', '--image-paths', image,
    '--prompt-file', options.promptFile, '--width', '768', '--height', '1152',
    '--seed', '7', '--output', options.output];
  for (const lowRam of [undefined, false, true]) {
    const extra = lowRam ? ['--low-ram'] : [];
    assert.deepEqual(zimageArgs({ ...options, size: LOCAL_SIZES.wide, lowRam }), [...zimage, ...extra]);
    assert.deepEqual(flux2EditArgs({ ...options, image, size: LOCAL_SIZES.shorts, lowRam }), [...flux, ...extra]);
  }
  assert.deepEqual(mfluxEnv({ HF_HUB_OFFLINE: '0', OTHER: '1' }), { HF_HUB_OFFLINE: '1', OTHER: '1' });
});

function artFixture(t, style, keys) {
  const ep = mkdtempSync(path.join(os.tmpdir(), 'story-video-art-'));
  t.after(() => rmSync(ep, { recursive: true, force: true }));
  const scene = { id: 's01', art: 'a tree', keys, lines: [{ who: 'narr', t: '나무가 있다.' }] };
  writeFileSync(path.join(ep, 'story.json'), JSON.stringify({ format: 'shorts', style, scenes: [scene] }));
  const art = (...flags) => spawnSync(process.execPath,
    [path.join(import.meta.dirname, '../art.mjs'), ep, '--dry-run', ...flags], {
      encoding: 'utf8', env: { ...process.env, STORY_VIDEO_ART_ENV: ep },
    });
  const prompts = path.join(ep, 'art', 'shorts', 'prompts');
  return { art, prompts };
}

test('그림 CLI에서 로컬 시트와 키프레임을 만들 수 없는 모드를 거부한다', t => {
  const cases = [
    [{ mode: 'cutscene' }, ['--engine', 'local', '--anim'], /로컬 엔진.*--anim.*--engine codex/],
    [{ mode: 'motion' }, ['--keys'], /--keys.*로컬/],
    ['watercolor', ['--engine', 'local', '--keys'], /--keys.*motion/],
  ];
  for (const [style, flags, message] of cases) {
    const { art, prompts } = artFixture(t, style);
    const result = art(...flags);
    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, message);
    assert.ok(!existsSync(prompts));
  }
});

test('로컬 그림 dry-run은 모델 설정을 읽지 않고 장면 요청문을 쓴다', t => {
  const { art, prompts } = artFixture(t, 'watercolor');
  const result = art('--engine', 'local');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /로컬.*장면 그림 1장.*어림 5분/);
  assert.match(readFileSync(path.join(prompts, 's01.txt'), 'utf8'), /^watercolor\na tree/);
});

test('키프레임 dry-run은 원본 그림이 없어도 별도 경로에 요청문을 쓴다', t => {
  const { art, prompts } = artFixture(t, { mode: 'motion', engine: 'local' }, ['turn the head']);
  const result = art('--keys');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /키프레임 1장.*어림 3분/);
  assert.equal(readFileSync(path.join(prompts, 'keys', 's01-1.txt'), 'utf8'), editPrompt('turn the head'));
});

test('키프레임 dry-run은 잘못된 변화 목록을 요청문을 쓰기 전에 거부한다', t => {
  const { art, prompts } = artFixture(t, { mode: 'motion', engine: 'local' }, ['a', 'b', 'c', 'd']);
  const result = art('--keys');
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /s01.*keys/);
  assert.ok(!existsSync(prompts));
});
