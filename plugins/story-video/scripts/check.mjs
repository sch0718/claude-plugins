import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { artPath, estimateStory, FORMATS, FPS, selectVoices, speechLines, storyFormat, storyScenes, validateStory, wrapLines } from './lib.mjs';
import { estimate } from './tts.mjs';
import { animDir, animSequence, captionBox, captionLayout, cutsceneMode, gridCell, insideSafe, motionReport,
  resolveScenes, validateCutscene } from './cutscene.mjs';
import { keyPlan, motionImages, motionMode, validateMotion } from './keyframes.mjs';

const run = (command, args) => execFileSync(command, args, { stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8' });
const readJson = file => JSON.parse(readFileSync(file, 'utf8'));

function minSec(seconds) {
  const rounded = Math.round(seconds);
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`;
}

function probe(file) {
  const { format, streams } = JSON.parse(run('ffprobe', ['-v', 'error', '-show_entries',
    'format=duration:stream=codec_type,width,height,avg_frame_rate', '-of', 'json', file]));
  const video = streams.find(stream => stream.codec_type === 'video');
  const duration = Number(format?.duration);
  if (!video || !Number.isFinite(duration)) throw new Error('영상 길이나 해상도를 읽을 수 없다.');
  const [numerator, denominator] = video.avg_frame_rate.split('/').map(Number);
  return {
    duration, width: video.width, height: video.height, fps: numerator / denominator,
    audio: streams.some(stream => stream.codec_type === 'audio'),
  };
}

function extract(final, out, at, timeline, width) {
  const file = path.join(out, `t${at}.png`);
  const tmp = path.join(out, `t${at}.${process.pid}.tmp.png`);
  try {
    run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-ss', String(at), '-i', final,
      '-vf', `scale=${width}:-1`, '-frames:v', '1', '-update', '1', tmp]);
    if (!existsSync(tmp)) throw new Error('해당 시각에 영상 프레임이 없다.');
    renameSync(tmp, file);
  } finally {
    rmSync(tmp, { force: true });
  }
  const frameTime = timeline ? Math.ceil(at * timeline.fps - 1e-9) / timeline.fps : at;
  const line = timeline?.scenes.flatMap(scene => scene.lines).find(l => l.start <= frameTime && frameTime < l.end);
  console.log(`${at}초 · ${path.relative(process.cwd(), file)} · 자막 ${timeline ? line?.t ?? '없음' : '확인 불가'}`);
}

function checkPanels(story, ep, format, timeline, warn) {
  const playing = new Map();
  try {
    validateCutscene(story);
  } catch (error) {
    warn(error.message);
    return playing;
  }
  const lengths = new Map(timeline?.scenes?.map(scene => [scene.id, scene.dur]));
  for (const { id, anim } of resolveScenes(story)) {
    if (!anim) continue;
    const dir = animDir(ep, format, id);
    if (existsSync(dir + '.png')) playing.set(id, anim);
    else warn(`${id} 시트가 없다(정지 그림으로 대신한다).`);
    const { cell, readable } = gridCell(format, anim.cols, anim.rows);
    if (!readable) warn(`${id} 시트 칸(${cell.join('×')})의 비율이나 크기가 권장 범위를 벗어난다.`);
    if (lengths.get(id) < animSequence(anim).length / anim.fps) {
      if (anim.loop === false) warn(`${id} 시트가 끝까지 재생되지 않는다.`);
      else console.log(`${id} 장면이 시트 한 주기보다 짧다.`);
    }
  }
  return playing;
}

function checkMotion(timeline, cutscene, playing, format, warn) {
  let cuts = 0;
  let longest = 0;
  const stills = [];
  for (const scene of timeline.scenes) {
    const report = motionReport(scene, { cutscene, anim: playing.get(scene.id) });
    cuts += report.cuts;
    for (const { from, to } of report.stills) {
      longest = Math.max(longest, to - from);
      stills.push({ id: scene.id, from, to });
    }
  }
  const average = cuts / timeline.scenes.length;
  console.log(`움직임 · 장면당 컷 평균 ${average.toFixed(1)} · 정지 구간 ${stills.length}개(최장 ${longest.toFixed(1)}초)`);
  if (format === 'shorts') {
    for (const { id, from, to } of stills) {
      warn(`${id} ${from.toFixed(1)}~${to.toFixed(1)}초가 정지 화면이다`);
    }
  }
}

function checkKeyMotion(timeline, story, ep, formatName) {
  const byId = new Map(story.scenes.map(scene => [scene.id, scene]));
  const transitions = timeline.scenes.map(scene => {
    const images = motionImages({ ep, formatName, storyScene: byId.get(scene.id), art: artPath(ep, formatName, scene.art) });
    return keyPlan(Math.round(scene.dur * FPS), images.length).changes.length;
  });
  const average = transitions.reduce((sum, count) => sum + count, 0) / transitions.length;
  const withoutKeys = transitions.filter(count => count === 0).length;
  console.log(`움직임 · 장면당 키프레임 전환 평균 ${average.toFixed(1)}회 · 키프레임 없는 장면 ${withoutKeys}개`);
}

function main() {
  const { positionals: [dir, ...extra], values } = parseArgs({
    options: {
      frames: { type: 'string' }, min: { type: 'string' }, max: { type: 'string' }, format: { type: 'string' },
    },
    allowPositionals: true,
  });
  const frames = values.frames?.split(',').map(at => at.trim()) ?? [];
  const usage = '쓰는 법 · node check.mjs <에피소드> [--format wide|shorts] [--frames 3,12.5,25] [--min <초>] [--max <초>]';
  const usageError = () => {
    console.error(usage);
    process.exitCode = 2;
  };
  if (!dir || extra.length || frames.some(at => !at || !Number.isFinite(Number(at)) || Number(at) < 0)) return usageError();
  const ep = path.resolve(dir);
  const out = path.join(ep, 'out');
  let problems = 0;
  const warn = message => {
    problems++;
    console.log(`경고 · ${message}`);
  };

  const storyFile = path.join(ep, 'story.json');
  let story, format;
  try {
    story = validateStory(readJson(storyFile));
    validateMotion(story);
    format = storyFormat(story, values.format);
  } catch (error) {
    warn(`대본이나 화면 형식을 읽을 수 없다(${error.code ?? error.message}).`);
    console.log(`문제 ${problems}개.`);
    process.exitCode = 1;
    return;
  }
  const { w, h, phone } = FORMATS[format];
  const cutscene = cutsceneMode(story);
  const motion = motionMode(story);
  const subs = captionLayout(format, cutscene);
  const min = Number(values.min ?? (format === 'shorts' ? 0 : 300));
  const max = Number(values.max ?? (format === 'shorts' ? 180 : 600));
  if (!Number.isFinite(min) || !Number.isFinite(max) || min < 0 || max < min) return usageError();

  const counts = estimateStory(story);
  const beats = ['기', '승', '전', '결'];
  const present = beats.filter(beat => story.scenes.some(scene => scene.beat === beat));
  console.log(`장면 ${counts.scenes}개 · 문장 ${counts.lines}개 · 기승전결 ${present.join('·') || '없음'}.`);
  console.log(`읽는 글 ${counts.chars}자 · 어림 길이 ${minSec(counts.seconds)}.`);
  for (const beat of beats.filter(beat => !present.includes(beat))) warn(`${beat} 비트가 없다.`);

  let timeline;
  const timelineFile = path.join(out, 'timeline.json');
  if (existsSync(timelineFile)) {
    try {
      const data = readJson(timelineFile);
      if (!Number.isFinite(data?.total) || data.total < 0) throw new Error('시간표 길이가 잘못됐다.');
      timeline = data;
      console.log(`시간표 ${timeline.total.toFixed(3)}초.`);
    } catch {
      warn('시간표를 읽을 수 없다.');
    }
  } else {
    warn('시간표가 없다.');
  }
  const length = timeline?.total ?? counts.seconds;
  if (length < min || length > max) {
    const label = timeline ? '시간표 길이' : '어림 길이';
    warn(`${label}가 ${minSec(min)}~${minSec(max)} 범위를 벗어났다.`);
  }

  for (const scene of storyScenes(story)) {
    if (scene.id !== 'recap') {
      if (!existsSync(artPath(ep, format, scene.id))) warn(`${scene.id} 그림이 없다.`);
      if (motion) motionImages({ ep, formatName: format, storyScene: scene, art: artPath(ep, format, scene.id) }, warn);
    }
    scene.lines.forEach(({ t }, index) => {
      const label = `${scene.id} ${index + 1}번 문장`;
      if (wrapLines(t, subs.maxChars).length > subs.preferredLines) warn(`${label}의 자막이 ${subs.preferredLines}줄을 넘을 수 있다.`);
      if ((t.match(/[.?!]+/g)?.length ?? 0) >= 2) warn(`${label}의 자막을 문장 단위로 나눈다.`);
    });
  }

  try {
    const setup = path.join(out, 'voices.json');
    const remembered = statSync(setup, { throwIfNoEntry: false })?.mtimeMs >= statSync(storyFile).mtimeMs;
    const lines = speechLines(story);
    // build.mjs와 같은 규칙(pace의 speed 채우기)으로 목소리를 고른다. voices.json은 이미 고른 값이다.
    const voices = selectVoices(story, lines, { voices: remembered ? readJson(setup) : undefined });
    const needs = Object.entries(estimate(lines, voices, path.join(ep, 'voice')));
    if (needs.length) warn(`빠진 음성 · ${needs.map(([provider, n]) => `${provider} ${n.requests}문장`).join(', ')}.`);
    else console.log('문장 음성이 모두 있다.');
  } catch (error) {
    warn(`문장 음성을 확인할 수 없다(${error.message}).`);
  }

  const playing = checkPanels(story, ep, format, timeline, warn);
  if (timeline?.scenes?.length) {
    if (motion) checkKeyMotion(timeline, story, ep, format);
    else checkMotion(timeline, cutscene, playing, format, warn);
  }
  if (cutscene && format === 'shorts' && !insideSafe(captionBox(subs, 3), format)) warn('세 줄 자막 띠가 쇼츠 안전 영역을 벗어난다.');

  const final = path.join(out, format, 'final.mp4');
  let video;
  if (existsSync(final)) {
    try {
      video = probe(final);
      console.log(`최종 영상 ${video.duration.toFixed(3)}초 · ${video.width}×${video.height} · ${Number(video.fps.toFixed(3))}fps · 음성 ${video.audio ? '있음' : '없음'}.`);
      if (!video.audio) warn('최종 영상에 음성 트랙이 없다.');
      if (video.width !== w || video.height !== h) {
        warn(`${format} 영상 해상도는 ${w}×${h}여야 한다.`);
        process.exitCode = 1;
      }
      if (timeline) {
        const difference = Math.abs(video.duration - timeline.total);
        console.log(`영상과 시간표 길이 차이 ${difference.toFixed(3)}초.`);
        if (difference > 0.5) {
          warn('final.mp4 길이가 시간표와 0.5초 넘게 차이 난다.');
          process.exitCode = 1;
        }
      }
    } catch {
      warn('최종 영상 정보를 읽을 수 없다.');
    }
  } else if (frames.length) {
    warn('프레임을 뽑을 최종 영상이 없다.');
  }
  const total = timeline || video ? Math.max(timeline?.total ?? 0, video?.duration ?? 0) : counts.seconds;
  const hardMax = Math.max(180, max);
  if (format === 'shorts' && total > hardMax) {
    warn(`쇼츠 길이 ${total.toFixed(3)}초가 상한 ${hardMax}초를 넘었다.`);
    process.exitCode = 1;
  }

  if (video && frames.length) {
    const frameDir = path.join(out, format, 'frames');
    mkdirSync(frameDir, { recursive: true });
    for (const value of frames) {
      const at = Number(value);
      if (at >= video.duration) {
        warn(`${at}초는 영상 길이를 벗어났다.`);
        continue;
      }
      try {
        extract(final, frameDir, at, timeline, phone[0]);
      } catch {
        warn(`${at}초의 프레임을 뽑을 수 없다.`);
      }
    }
  }
  console.log(`문제 ${problems}개.`);
}

main();
