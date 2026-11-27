import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildTimeline, estimateSeconds, estimateStory, toSrt, wrapLines } from '../lib.mjs';

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
