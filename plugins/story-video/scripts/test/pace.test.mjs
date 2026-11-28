import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as lib from '../lib.mjs';
import { voiceFile } from '../tts.mjs';

const story = {
  scenes: [{ id: 's01', lines: [{ who: 'narr', t: '한 문장' }, { who: 'guest', t: '두 문장' }] }],
  voices: {
    narr: { provider: 'vstudio', id: 'demo-narr', seed: 7 },
    guest: { provider: 'vstudio', id: 'demo-guest', seed: 8, speed: 1 },
  },
};
const shorts = { ...story, format: 'shorts' };
const lines = lib.speechLines(story);

test('대본 화면 크기로 기본 pace를 고르고 명시한 pace를 우선한다', () => {
  assert.equal(lib.storyPace(story), 'normal');
  assert.equal(lib.storyPace({ ...story, format: 'wide' }), 'normal');
  assert.equal(lib.storyPace(shorts), 'fast');
  assert.equal(lib.storyPace({ ...shorts, pace: 'normal' }), 'normal');
  assert.equal(lib.storyPace({ ...story, format: 'wide', pace: 'fast' }), 'fast');
  for (const pace of ['slow', 'constructor', null]) {
    assert.throws(() => lib.validateStory({ ...story, pace }), /pace/);
  }
});

test('대본 길이 어림에 fast 말 빠르기와 쉼을 반영한다', () => {
  assert.equal(lib.estimateSeconds('한 문장', 'fast'), 3 * 0.15);
  assert.deepEqual(lib.estimateStory(shorts), {
    chars: 6, seconds: 6 * 0.15 + (0.2 + 0.3 + 0.12), scenes: 1, lines: 2,
  });
});

test('시간표는 fast 쉼을 적용하고 합성한 음성의 길이는 유지한다', () => {
  const timeline = lib.buildTimeline(shorts, [[2, 2]]);
  const [first, second] = timeline.scenes[0].lines;
  const frame = seconds => Math.round(seconds * timeline.fps) / timeline.fps;
  assert.equal(first.start, frame(0.2));
  assert.equal(second.start, frame(0.2 + 2 + 0.12));
  assert.equal(timeline.total, frame(0.2 + 2 + 0.12 + 2 + 0.3));
  for (const line of [first, second]) {
    assert.ok(Math.abs(line.end - line.start - 2) <= 1 / timeline.fps);
  }
});

test('장면 앞뒤 쉼을 직접 정할 수 있고 normal 기본값은 기존과 같다', () => {
  const explicit = { ...shorts, scenes: [{ ...story.scenes[0], lead: 0, tail: 0.5 }] };
  const normal = { ...shorts, pace: 'normal' };
  assert.equal(lib.estimateStory(explicit).seconds, 6 * 0.15 + (0.5 + 0.12));
  assert.equal(lib.estimateStory(normal).seconds, 6 * 0.133 + (0.6 + 0.8 + 0.35));
  const fastTimeline = lib.buildTimeline(explicit, [[2, 2]]);
  assert.equal(fastTimeline.scenes[0].lines[0].start, 0);
  assert.equal(fastTimeline.total, 139 / 30);
  const normalTimeline = lib.buildTimeline(normal, [[2, 2]]);
  assert.equal(normalTimeline.scenes[0].lines[0].start, 0.6);
  assert.equal(normalTimeline.scenes[0].lines[1].start, 89 / 30);
  assert.equal(normalTimeline.total, 173 / 30);
});

test('복제 목소리에 기본 속도를 채우고 명시한 속도를 유지한다', () => {
  const voices = lib.selectVoices(shorts, lines);
  assert.deepEqual(voices.narr, { ...story.voices.narr, speed: 1.25 });
  assert.deepEqual(voices.guest, story.voices.guest);
  assert.equal(Object.hasOwn(story.voices.narr, 'speed'), false);
  const reused = lib.selectVoices(shorts, lines, { voices: JSON.parse(JSON.stringify(voices)) });
  assert.deepEqual(reused, voices);
  assert.equal(voiceFile(reused.narr, '한 문장', 'voice', 'narr'), voiceFile(voices.narr, '한 문장', 'voice', 'narr'));
});

test('say 속도는 새 배정과 기존 배정에 한 번만 곱한다', () => {
  const draft = lib.selectVoices(story, lines, { provider: 'say' });
  const generated = lib.selectVoices(shorts, lines, { provider: 'say' });
  assert.equal(generated.narr.rate, 213);
  assert.equal(generated.guest.rate, Math.round(draft.guest.rate * 1.25));
  assert.equal(generated.narr.speed, 1.25);
  const assigned = lib.selectVoices(shorts, lines, { voices: {
    narr: { provider: 'say', id: 'Yuna' },
    guest: { provider: 'say', id: 'Yuna', rate: 180, speed: 1 },
  } });
  assert.deepEqual(assigned.narr, { provider: 'say', id: 'Yuna', rate: 219, speed: 1.25 });
  assert.deepEqual(assigned.guest, { provider: 'say', id: 'Yuna', rate: 180, speed: 1 });
  for (const voices of [generated, assigned]) {
    const reused = lib.selectVoices(shorts, lines, { voices: JSON.parse(JSON.stringify(voices)) });
    assert.deepEqual(reused, voices);
    assert.equal(voiceFile(reused.narr, '한 문장', 'voice', 'narr'), voiceFile(voices.narr, '한 문장', 'voice', 'narr'));
  }
});

test('normal은 지시와 take가 없는 음성의 캐시 파일명을 유지한다', () => {
  const normal = { ...shorts, pace: 'normal' };
  const legacySay = { provider: 'say', id: 'Yuna', rate: 170 };
  for (const [voice, original] of [
    [lib.selectVoices(normal, lines).narr, story.voices.narr],
    [lib.selectVoices(normal, lines, { provider: 'say' }).narr, legacySay],
  ]) {
    assert.deepEqual(voice, original);
    assert.equal(voiceFile(voice, '한 문장', 'voice', 'narr'), voiceFile(original, '한 문장', 'voice', 'narr'));
  }
});
