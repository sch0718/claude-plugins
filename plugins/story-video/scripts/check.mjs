import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { estimateStory, speechLines, storyScenes, validateStory, wrapLines } from './lib.mjs';
import { clean, estimate } from './tts.mjs';

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

function extract(final, out, at, timeline) {
  const file = path.join(out, `t${at}.png`);
  const tmp = path.join(out, `t${at}.${process.pid}.tmp.png`);
  try {
    run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-ss', String(at), '-i', final,
      '-vf', 'scale=960:-1', '-frames:v', '1', '-update', '1', tmp]);
    if (!existsSync(tmp)) throw new Error('해당 시각에 영상 프레임이 없다.');
    renameSync(tmp, file);
  } finally {
    rmSync(tmp, { force: true });
  }
  const frameTime = timeline ? Math.ceil(at * timeline.fps - 1e-9) / timeline.fps : at;
  const line = timeline?.scenes.flatMap(scene => scene.lines).find(l => l.start <= frameTime && frameTime < l.end);
  console.log(`${at}초 · ${path.relative(process.cwd(), file)} · 자막 ${timeline ? line?.t ?? '없음' : '확인 불가'}`);
}

function main() {
  const { positionals: [dir, ...extra], values } = parseArgs({
    options: {
      frames: { type: 'string' }, min: { type: 'string', default: '300' }, max: { type: 'string', default: '600' },
    },
    allowPositionals: true,
  });
  const min = Number(values.min);
  const max = Number(values.max);
  const frames = values.frames?.split(',').map(at => at.trim()) ?? [];
  if (!dir || extra.length || !Number.isFinite(min) || !Number.isFinite(max) || min < 0 || max < min
      || frames.some(at => !at || !Number.isFinite(Number(at)) || Number(at) < 0)) {
    console.error('쓰는 법 · node check.mjs <에피소드> [--frames 3,12.5,25] [--min 300] [--max 600]');
    process.exitCode = 2;
    return;
  }
  const ep = path.resolve(dir);
  const out = path.join(ep, 'out');
  let problems = 0;
  const warn = message => {
    problems++;
    console.log(`경고 · ${message}`);
  };

  let story;
  const storyFile = path.join(ep, 'story.json');
  try {
    story = validateStory(readJson(storyFile));
  } catch (error) {
    warn(`story.json 읽기 실패(${error.code ?? error.message}).`);
    console.log(`문제 ${problems}개.`);
    process.exitCode = 1;
    return;
  }

  const counts = estimateStory(story);
  const beats = ['기', '승', '전', '결'];
  const present = beats.filter(beat => story.scenes.some(scene => scene.beat === beat));
  console.log(`장면 ${counts.scenes}개 · 문장 ${counts.lines}개 · 기승전결 ${present.join('·') || '없음'}.`);
  console.log(`읽는 글 ${counts.chars}자 · 어림 길이 ${minSec(counts.seconds)}.`);
  for (const beat of beats.filter(beat => !present.includes(beat))) warn(`${beat} 비트가 없다.`);
  if (counts.seconds < min || counts.seconds > max) warn(`어림 길이가 ${minSec(min)}~${minSec(max)} 범위를 벗어났다.`);

  for (const scene of storyScenes(story)) {
    if (scene.id !== 'recap' && !existsSync(path.join(ep, 'art', `${scene.id}.png`))) warn(`${scene.id} 그림이 없다.`);
    scene.lines.forEach(({ t }, index) => {
      const label = `${scene.id} ${index + 1}번 문장`;
      if (wrapLines(t).length > 2) warn(`${label}의 자막이 두 줄을 넘을 수 있다.`);
      if ((t.match(/[.?!]/g)?.length ?? 0) >= 2) warn(`${label}의 자막을 문장 단위로 나눈다.`);
    });
  }

  try {
    const setup = path.join(out, 'voices.json');
    const remembered = statSync(setup, { throwIfNoEntry: false })?.mtimeMs >= statSync(storyFile).mtimeMs;
    const voices = Object.assign(Object.create(null), remembered ? readJson(setup) : story.voices);
    const lines = speechLines(story).map(line => ({ ...line, t: clean(line.t) }));
    const needs = Object.entries(estimate(lines, voices, path.join(ep, 'voice')));
    if (needs.length) warn(`빠진 음성 · ${needs.map(([provider, n]) => `${provider} ${n.requests}문장`).join(', ')}.`);
    else console.log('문장 음성이 모두 있다.');
  } catch (error) {
    warn(`문장 음성을 확인할 수 없다(${error.message}).`);
  }

  let timeline;
  const timelineFile = path.join(out, 'timeline.json');
  if (existsSync(timelineFile)) {
    try {
      timeline = readJson(timelineFile);
      console.log(`시간표 ${timeline.total.toFixed(3)}초.`);
    } catch {
      timeline = undefined;
      warn('시간표를 읽을 수 없다.');
    }
  } else {
    warn('시간표가 없다.');
  }

  const final = path.join(out, 'final.mp4');
  let video;
  if (existsSync(final)) {
    try {
      video = probe(final);
      console.log(`최종 영상 ${video.duration.toFixed(3)}초 · ${video.width}×${video.height} · ${Number(video.fps.toFixed(3))}fps · 음성 ${video.audio ? '있음' : '없음'}.`);
      if (!video.audio) warn('최종 영상에 음성 트랙이 없다.');
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

  if (video && frames.length) {
    const frameDir = path.join(out, 'frames');
    mkdirSync(frameDir, { recursive: true });
    for (const value of frames) {
      const at = Number(value);
      if (at >= video.duration) {
        warn(`${at}초는 영상 길이를 벗어났다.`);
        continue;
      }
      try {
        extract(final, frameDir, at, timeline);
      } catch {
        warn(`${at}초의 프레임을 뽑을 수 없다.`);
      }
    }
  }
  console.log(`문제 ${problems}개.`);
}

main();
