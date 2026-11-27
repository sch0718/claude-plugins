import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { castReport, isDraft, pitchOutliers, validateCast } from '../cast.mjs';
import { selectVoices, speechLines, validateStory } from '../lib.mjs';

const clone = (id, extra) => ({ provider: 'vstudio', model: 'omnivoice', id, seed: 7, ...extra });
const profiles = [
  { id: 'one', kind: 'clone', name: '프로필 하나' },
  { id: 'two', kind: 'clone' },
  { id: 'three', kind: 'clone' },
  { id: 'design', kind: 'design' },
];
const lines = (...roles) => roles.map(who => ({ who, t: '문장' }));
const errors = (voices, script, opts) => validateCast(voices, script, { profiles, ...opts }).errors.join('\n');

test('해설은 두 개까지 배정할 수 있다', () => {
  const voices = { narr: clone('one'), narr_f: clone('two') };
  assert.equal(errors(voices, lines('narr', 'narr_f')), '');
  assert.match(errors({ ...voices, narr_m: clone('three') }, lines('narr')), /해설.*2/);
});

test('지난 이야기에서 선택한 해설도 배정표에 있어야 한다', () => {
  const story = validateStory({ recap: { t: '요약', who: 'narr_f' }, scenes: [{ id: 's01', lines: lines('narr_m') }] });
  const voices = { narr_f: clone('one'), narr_m: clone('two') };
  assert.equal(errors(voices, speechLines(story)), '');
  assert.match(errors({ narr_m: voices.narr_m }, speechLines(story)), /narr_f.*배정/);
  assert.throws(() => validateStory({ ...story, recap: { t: '요약', who: 'jun' } }), /해설/);
});

test('최종본과 임시판 모두 대본의 모든 배역을 확인한다', () => {
  assert.match(errors({ narr: clone('one') }, lines('narr', 'jun')), /jun.*배정/);
  assert.match(errors({ narr: { provider: 'say' } }, lines('narr', 'jun'), { draft: true }), /jun.*배정/);
});

test('최종본은 엔진 하나를 쓰고 서로 다른 배역의 프로필을 구분한다', () => {
  assert.match(errors({ narr: clone('one'), jun: clone('one') }, lines('narr', 'jun')), /프로필.*같/);
  assert.match(errors({ narr: clone('one'), jun: clone('two', { model: 'other' }) }, lines('narr', 'jun')), /엔진/);
});

test('최종본은 명시된 복제 프로필과 정수 seed를 요구한다', () => {
  assert.equal(errors({ narr: clone('one') }, lines('narr')), '');
  assert.match(errors({ narr: clone('one', { model: '', seed: 1.5 }) }, lines('narr')), /model.*seed/s);
  assert.match(errors({ narr: clone('design') }, lines('narr')), /clone/);
  assert.match(errors({ narr: clone('missing') }, lines('narr')), /missing/);
  assert.match(errors({ narr: clone('one') }, lines('narr'), { profiles: undefined }), /프로필 목록 없음/);
});

test('최종본의 배정표는 즉석 설계와 항목별 서버 주소를 허용하지 않는다', () => {
  const voice = clone('one', { instruct: 'freeform', url: 'http://localhost' });
  assert.match(errors({ narr: voice }, lines('narr')), /instruct.*url/s);
});

test('나이별로 나눈 배역은 base를 적고 같은 base에서 프로필을 공유할 수 있다', () => {
  const voices = { narr: clone('one'), mia_child: clone('two', { base: 'mia' }), mia_adult: clone('two', { base: 'mia' }) };
  const script = lines(...Object.keys(voices));
  assert.equal(errors(voices, script), '');
  assert.match(errors({ ...voices, mia_child: clone('two') }, script), /mia_child.*base/);
});

test('say로 만든 예시 임시판은 저장한 뒤에도 임시판으로 인식한다', () => {
  const story = JSON.parse(readFileSync(new URL('../../examples/mini/story.json', import.meta.url)));
  const script = speechLines(story);
  const voices = selectVoices(story, script, { provider: 'say' });
  assert.ok(Object.values(voices).every(voice => voice.id === 'Yuna'));
  assert.equal(isDraft(voices, 'say'), true);
  assert.equal(isDraft(JSON.parse(JSON.stringify(voices))), true);
  assert.equal(isDraft({}), false);
  assert.equal(errors(voices, script, { draft: true, profiles: undefined }), '');
});

test('say 임시판은 원본 배정표에서 나이 배역의 base를 읽는다', () => {
  const voices = { narr: { provider: 'say' }, mia_old: { provider: 'say' } };
  const script = lines('narr', 'mia_old');
  const baseVoices = { mia_old: { base: 'mia' } };
  assert.equal(errors(voices, script, { draft: true, baseVoices }), '');
  assert.match(errors(voices, script, { draft: true }), /mia_old.*base/);
  assert.match(castReport(voices, script, null, baseVoices), /\| mia_old \| mia \| say \|/);
});

test('say와 다른 공급자를 섞으면 최종본 규칙을 검사한다', () => {
  const voices = { narr: clone('one'), jun: { provider: 'say' } };
  assert.equal(isDraft(voices), false);
  assert.match(errors(voices, lines('narr', 'jun')), /jun.*vstudio/);
});

test('배역표 보고서는 서버 이름을 우선 쓰고 배역별 문장을 센다', () => {
  const report = castReport({ narr: clone('one', { name: '이전 이름' }), jun: clone('two', { name: '준' }) },
    lines('narr', 'narr', 'jun'), profiles);
  assert.match(report, /narr.*프로필 하나.*\| 2 \|/);
  assert.match(report, /jun.*준.*\| 1 \|/);
});

test('3반음 경계부터 음높이 이탈로 보고한다', () => {
  const samples = [
    { who: 'narr', pitch: 128 },
    { who: 'narr', pitch: 128 * 2 ** (6 / 12) },
    { who: 'jun', pitch: 128 },
    { who: 'jun', pitch: 128 * 2 ** (5.9 / 12) },
    { who: 'driver', pitch: 128 },
    { who: 'driver', pitch: null },
  ];
  assert.deepEqual(pitchOutliers(samples), samples.slice(0, 2));
});
