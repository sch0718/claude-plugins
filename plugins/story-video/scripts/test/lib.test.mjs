import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import * as lib from '../lib.mjs';
import { buildTimeline, estimateSeconds, estimateStory, toSrt, wrapLines } from '../lib.mjs';
import * as tts from '../tts.mjs';

const story = {
  scenes: [
    { id: 's01', art: '첫 그림', lines: [{ who: 'narr', t: '10 개', say: '열 개' }, { who: 'jun', t: ' 안 녕 ' }] },
    { id: 's02', kb: 'left', lead: 0.2, tail: 0.4, lines: [{ who: 'narr', t: '다음' }] },
  ],
};

test('읽을 글자와 문장 사이 쉼까지 센다', () => {
  assert.equal(estimateSeconds(' 열 개\n🙂 '), 3 * 0.133);
  const result = estimateStory(story);
  assert.equal(result.chars, 6);
  assert.equal(result.scenes, 2);
  assert.equal(result.lines, 3);
  assert.ok(Math.abs(result.seconds - (6 * 0.133 + 0.35 + 0.6 + 0.8 + 0.2 + 0.4)) < 1e-9);
});

test('시간표는 장면을 이어 붙이고 모든 경계를 프레임에 맞춘다', () => {
  const timeline = buildTimeline(story, [[1.043, 0.977], [1.031]]);
  const [first, second] = timeline.scenes;
  assert.equal(timeline.fps, 30);
  assert.equal(first.start, 0);
  assert.equal(first.lines[0].start, 0.6);
  assert.equal(first.lines[1].start, 60 / 30);
  assert.equal(first.kb, 'in');
  assert.equal(second.kb, 'left');
  assert.equal(second.start, first.end);
  assert.equal(timeline.total, second.end);
  const rawTotal = 0.6 + 1.043 + 0.35 + 0.977 + 0.8 + 0.2 + 1.031 + 0.4;
  assert.equal(timeline.total, Math.round(rawTotal * 30) / 30);
  for (const scene of timeline.scenes) {
    for (const time of [scene.start, scene.end, scene.dur, ...scene.lines.flatMap(l => [l.start, l.end])]) {
      assert.ok(Math.abs(time * 30 - Math.round(time * 30)) < 1e-9);
    }
  }
  const halfFrame = buildTimeline({ scenes: [{ ...story.scenes[0], lead: 0 }] }, [[2.05, 1]]);
  assert.equal(halfFrame.scenes[0].lines[0].end, 62 / 30);
});

test('SRT는 자막 원문과 밀리초 단위의 시각을 기록한다', () => {
  const timeline = buildTimeline(story, [[1.043, 0.977], [1.031]]);
  assert.match(toSrt(timeline), /^1\n00:00:00,600 --> 00:00:01,633\n10 개\n\n2\n/);
  assert.match(toSrt(timeline), /\n3\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\n다음\n$/);
});

test('지난 이야기는 첫 그림을 쓰는 별도 장면이다', () => {
  const episode = { ...story, recap: '지난 이야기' };
  const result = buildTimeline(episode, [[0.75], [1, 1], [1]]);
  const [recap, first] = result.scenes;
  assert.equal(recap.id, 'recap');
  assert.equal(recap.art, 's01');
  assert.equal(recap.lines[0].who, 'narr');
  assert.equal(recap.lines[0].t, '지난 이야기');
  assert.equal(recap.end, first.start);
  assert.equal(estimateStory(episode).scenes, 3);
  assert.equal(estimateStory(episode).lines, 4);
});

test('자막을 단어 단위로 나누고 긴 단어는 자른다', () => {
  assert.deepEqual(wrapLines('가나 다라 마바사아자차 카', 4), ['가나', '다라', '마바사아', '자차 카']);
});

test('세로 화면에는 한 줄에 최대 14자 자막을 쓴다', () => {
  const shorts = lib.FORMATS.shorts;
  assert.deepEqual([shorts.w, shorts.h, shorts.art, shorts.phone], [1080, 1920, [1024, 1536], [540, 960]]);
  assert.deepEqual(wrapLines('가'.repeat(15), shorts.subs.maxChars), ['가'.repeat(14), '가']);
  assert.equal(lib.storyFormat({ format: 'shorts' }, 'wide'), 'wide');
  assert.throws(() => lib.validateStory({ ...story, format: 'square' }), /format/);
  assert.throws(() => lib.validateStory({
    scenes: [{ id: 's01', lines: [{ who: 'narr', t: '해설', direction: 1 }] }],
  }), /문장 형식/);
});

test('한국어와 영어 연기 지시에서 공급자에 넘길 값을 뽑는다', () => {
  assert.deepEqual(lib.directionParams('아주 천천히 낮은 목소리로 속삭이듯 작게'), {
    whisper: true, pitch: 'low', speed: 0.85, volume: 'soft',
  });
  assert.deepEqual(lib.directionParams('Urgent, high pitch and shout'), {
    whisper: false, pitch: 'high', speed: 1.1, volume: 'loud',
  });
  assert.equal(lib.directionParams('느리게').speed, 0.92);
  assert.equal(lib.directionParams('very slow').speed, 0.85);
  assert.deepEqual(lib.directionParams('새벽에 회상하며'), { whisper: false, pitch: null, speed: 1, volume: null });
});

test('음성 해시는 0.1.0과 호환되고 실제 반영된 지시와 take만 구분한다', () => {
  const line = ' 조금만…  더 ';
  const legacy = voice => createHash('md5').update(JSON.stringify(voice) + '|' + tts.clean(line)).digest('hex').slice(0, 12);
  const say = { provider: 'say', id: 'Yuna' };
  const vstudio = { provider: 'vstudio', style: 'female, low pitch' };
  const eleven = { provider: 'eleven', id: 'demo' };
  for (const voice of [say, vstudio, eleven, { provider: 'edge' }, { provider: 'gemini' }]) {
    assert.equal(tts.voiceHash(voice, line), legacy(voice));
    assert.equal(tts.voiceHash(voice, line, { take: 0 }), legacy(voice));
    assert.notEqual(tts.voiceHash(voice, line, { take: 1 }), legacy(voice));
  }
  assert.equal(tts.voiceHash(say, line, { direction: '속삭이며 낮게', take: 0 }), legacy(say));
  assert.equal(tts.voiceHash(vstudio, line, { direction: '낮은 목소리' }), legacy(vstudio));
  assert.equal(tts.voiceHash(eleven, line, { direction: '아주 천천히' }), legacy(eleven));
  assert.equal(tts.voiceHash(say, line, { direction: '천천히' }), tts.voiceHash(say, line, { direction: 'slow, softly' }));
  assert.notEqual(tts.voiceHash(say, line, { direction: '천천히' }), legacy(say));
  assert.notEqual(tts.voiceHash(vstudio, line, { direction: '높은 목소리' }), legacy(vstudio));
});

test('낭독을 비교할 때 글자 모양과 숫자 읽기를 통일한다', () => {
  assert.equal(lib.readable('0, 1914년 10년 9999 10000'), '영, 천구백십사년 십년 구천구백구십구 10000');
  assert.equal(lib.normalizeSpeech('「1914년」 — “ABC”, 가!'), '천구백십사년abc가');
  assert.equal(lib.similarity(lib.normalizeSpeech('10년'), lib.normalizeSpeech('십 년')), 1);
  assert.equal(lib.similarity('가나다라', '가나나라마'), 0.6);
  assert.equal(lib.similarity('🙂', '🙂🙂'), 0.5);
  assert.equal(lib.similarity('', ''), 1);
});

test('speechScore는 숫자를 고유어로 읽은 경우도 맞춘다', () => {
  assert.equal(lib.nativeReadable('19살, 3시, 21명'), '열아홉살, 세시, 스물한명');
  assert.equal(lib.speechScore('윌은 열아홉 살이었다.', '윌은 19살이었다.'), 1);
  assert.equal(lib.speechScore('1914년 겨울', '천구백십사년 겨울'), 1);
});
