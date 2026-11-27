import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { artPath, buildTimeline, FORMATS, FPS, selectVoices, speechLines, storyFormat, storyScenes, toSrt, validateStory } from './lib.mjs';
import { dur, estimate, loadEnv, overFree, speak, voiceFile } from './tts.mjs';

const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
const mtime = file => statSync(file, { throwIfNoEntry: false })?.mtimeMs ?? -Infinity;
const fresh = (file, inputs) => existsSync(file) && inputs.every(input => mtime(file) >= mtime(input));
const script = name => path.join(import.meta.dirname, name);
const SAMPLE_RATE = 48000;

function run(command, args, { env, capture = false } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { env, stdio: ['ignore', capture ? 'pipe' : 'ignore', 'pipe'] });
    const chunks = [];
    let stderr = '';
    child.stdout?.on('data', chunk => chunks.push(chunk));
    child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code, signal) => {
      if (code === 0) resolve({ stdout: Buffer.concat(chunks), stderr });
      else reject(new Error(stderr.trim() || `${command} 실행에 실패했다(${signal ?? code}).`));
    });
  });
}

function save(file, data) {
  const tmp = `${file}.${process.pid}.tmp`;
  try {
    writeFileSync(tmp, data);
    renameSync(tmp, file);
  } finally {
    rmSync(tmp, { force: true });
  }
}

function durationOk(result, who, text) {
  if (!Number.isFinite(result.dur) || result.dur <= 0) {
    throw new Error(`${who}의 음성 길이가 0이거나 잘못됐다(${text.slice(0, 20)}).`);
  }
  return result;
}

async function makeVoice(lines, voices, dir, force) {
  loadEnv(process.env.STORY_VIDEO_VOICE_ENV ?? '~/.config/story-video/voice.env');
  mkdirSync(dir, { recursive: true });
  const scratch = force ? mkdtempSync(path.join(dir, '.force-')) : dir;
  try {
    const need = estimate(lines, voices, scratch);
    const requests = Object.entries(need).map(([provider, n]) => `${provider} ${n.requests}번 ${n.chars}자`);
    console.log(`음성 ${lines.length}문장. 새로 받을 음성 ${requests.join(', ') || '없음(캐시 사용)'}.`);
    const limits = overFree(need);
    for (const limit of limits) console.warn(`무료 한도 초과 · ${limit}`);
    if (limits.length && !force) throw new Error('무료 한도를 넘어서 멈춘다. 계속하려면 --force를 쓴다.');

    const results = [];
    const regenerated = new Set();
    const dropped = new Map();
    for (const line of lines) {
      const voice = voices[line.who] ?? voices.narr;
      const file = voiceFile(voice, line.t, dir, line.who, line);
      const useScratch = force && !regenerated.has(file);
      const result = durationOk(await speak(voice, line.t, useScratch ? scratch : dir, line.who, line), line.who, line.t);
      if (useScratch) {
        renameSync(result.file, file);
        result.file = file;
        regenerated.add(file);
      }
      if (result.dropped?.length) {
        if (!dropped.has(voice.provider)) dropped.set(voice.provider, { count: 0, parts: new Set() });
        const summary = dropped.get(voice.provider);
        summary.count++;
        for (const part of result.dropped) summary.parts.add(part);
      }
      results.push(result);
    }
    console.log(`음성 ${lines.length}문장을 준비했다.`);
    for (const [provider, { count, parts }] of dropped) {
      console.warn(`${provider} · 지시 일부 반영 못 함 ${count}문장(${[...parts].join(', ')})`);
    }
    return results;
  } finally {
    if (force) rmSync(scratch, { recursive: true, force: true });
  }
}

function cachedVoice(lines, voices, dir) {
  if (Object.keys(estimate(lines, voices, dir)).length) throw new Error('빠진 음성이 있다. voice 단계를 먼저 실행한다.');
  return lines.map(line => {
    const { who, t } = line;
    const file = voiceFile(voices[who] ?? voices.narr, t, dir, who, line);
    return durationOk({ file, dur: dur(file) }, who, t);
  });
}

function wavHeader(bytes) {
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(bytes + 36, 4);
  header.write('WAVEfmt ', 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(SAMPLE_RATE, 24);
  header.writeUInt32LE(SAMPLE_RATE * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write('data', 36);
  header.writeUInt32LE(bytes, 40);
  return header;
}

async function makeTimeline(story, ep, out, inputs, results, force) {
  const durs = [];
  const files = [];
  let offset = 0;
  for (const scene of storyScenes(story)) {
    const items = results.slice(offset, offset + scene.lines.length);
    durs.push(items.map(item => item.dur));
    files.push(items.map(item => path.relative(ep, item.file)));
    offset += items.length;
  }
  const timeline = buildTimeline(story, durs, { files });
  const json = JSON.stringify(timeline, null, 2) + '\n';
  const timelinePath = path.join(out, 'timeline.json');
  const audioPath = path.join(out, 'audio.wav');
  const srtPath = path.join(out, 'subtitles.srt');
  const sources = [...inputs, ...results.map(item => item.file)];
  if (!force && [timelinePath, audioPath, srtPath].every(file => fresh(file, sources))
      && readFileSync(timelinePath, 'utf8') === json) {
    console.log('시간표·음성 합성은 기존 파일을 쓴다.');
    return;
  }

  const pcm = Buffer.alloc(Math.round(timeline.total * SAMPLE_RATE) * 2);
  for (const { file, start } of timeline.scenes.flatMap(scene => scene.lines)) {
    const { stdout } = await run('ffmpeg', ['-nostdin', '-v', 'error', '-i', path.join(ep, file),
      '-ar', String(SAMPLE_RATE), '-ac', '1', '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
    { capture: true });
    stdout.copy(pcm, Math.round(start * SAMPLE_RATE) * 2);
  }

  save(audioPath, Buffer.concat([wavHeader(pcm.length), pcm]));
  save(srtPath, toSrt(timeline));
  save(timelinePath, json);
  console.log(`시간표·자막·음성 ${timeline.total.toFixed(3)}초를 저장했다.`);
}

function motion(frames, kb, { w, h }) {
  const progress = `on/${Math.max(1, frames - 1)}`;
  const pan = kb === 'left' || kb === 'right';
  const zoom = pan ? '1.1' : kb === 'out' ? `1.12-0.12*${progress}` : `1+0.12*${progress}`;
  const x = kb === 'right' ? `(iw-iw/zoom)*${progress}`
    : kb === 'left' ? `(iw-iw/zoom)*(1-${progress})` : '(iw-iw/zoom)/2';
  return `scale=${w * 2}:${h * 2}:force_original_aspect_ratio=increase,crop=${w * 2}:${h * 2},setsar=1,`
    + `zoompan=z='${zoom}':x='${x}':y='(ih-ih/zoom)/2':d=${frames}:s=${w}x${h}:fps=${FPS}`;
}

async function makeMp4(output, args) {
  const tmp = output.replace(/\.mp4$/, `.${process.pid}.tmp.mp4`);
  try {
    await run('ffmpeg', ['-nostdin', '-v', 'error', '-y', ...args, tmp]);
    renameSync(tmp, output);
  } finally {
    rmSync(tmp, { force: true });
  }
}

const videoEncoding = crf => ['-an', '-c:v', 'libx264', '-threads', '3', '-preset', 'veryfast', '-crf', String(crf),
  '-pix_fmt', 'yuv420p'];

async function makeScene(scene, index, art, output, format) {
  const frames = Math.round(scene.dur * FPS);
  if (frames < 1) throw new Error(`${scene.id}의 길이가 한 프레임보다 짧다.`);
  const hasArt = existsSync(art);
  const colors = [0x242e40, 0x302638, 0x24372f, 0x3b2c24, 0x242940, 0x393321];
  const color = (colors[index % colors.length] + 0x010101 * (Math.floor(index / colors.length) % 16)).toString(16);
  const input = hasArt ? ['-i', art]
    : ['-f', 'lavfi', '-i', `color=c=0x${color}:s=${format.w}x${format.h}:r=${FPS}`];
  if (!hasArt) console.warn(`${scene.id} 그림이 없어 단색을 쓴다.`);
  const fadeFrames = Math.min(9, Math.floor(frames / 2));
  const fade = fadeFrames > 1 ? `,fade=in:s=0:n=${fadeFrames},fade=out:s=${frames - fadeFrames}:n=${fadeFrames - 1}` : '';
  const filter = `${hasArt ? motion(frames, scene.kb, format) + ',' : ''}format=yuv420p${fade}`;
  await makeMp4(output, [...input, '-filter_threads', '2', '-vf', filter, '-frames:v', String(frames), ...videoEncoding(14)]);
}

async function addSubtitles(scene, video, subs, output, { bottom }) {
  const args = ['-filter_complex_threads', '2', '-i', video];
  const filters = [];
  subs.forEach(({ line, out }, i) => {
    args.push('-i', out);
    const start = (Math.round((line.start - scene.start) * FPS) - 0.5) / FPS;
    const end = (Math.round((line.end - scene.start) * FPS) - 0.5) / FPS;
    filters.push(`[${i === 0 ? '0:v' : 'v' + i}][${i + 1}:v]overlay=0:${bottom}-overlay_h+1`
      + `:enable='between(t,${start},${end})'[v${i + 1}]`);
  });
  if (subs.length) args.push('-filter_complex', filters.join(';'), '-map', `[v${subs.length}]`);
  await makeMp4(output, [...args, ...videoEncoding(20)]);
}

async function parallelScenes(count, jobs, task) {
  let next = 0;
  let failure;
  await Promise.all(Array.from({ length: jobs }, async () => {
    while (!failure && next < count) {
      try {
        await task(next++);
      } catch (error) {
        failure ??= error;
      }
    }
  }));
  if (failure) throw failure;
}

async function joinVideo(output, audio, clips, force) {
  if (!force && fresh(output, [audio, ...clips.map(clip => clip.file)])) {
    console.log(`${path.basename(output)} 기존 영상을 쓴다.`);
    return;
  }
  const list = path.join(path.dirname(output), `.concat-${process.pid}.txt`);
  try {
    writeFileSync(list, clips.map(({ file, scene }) => {
      // MP4에 기록된 길이 대신 시간표의 프레임 경계를 이음새에 쓴다.
      const micros = Math.round(scene.end * 1e6) - Math.round(scene.start * 1e6);
      return `file '${file.replaceAll("'", "'\\''")}'\nduration ${(micros / 1e6).toFixed(6)}\n`;
    }).join(''));
    await makeMp4(output, ['-f', 'concat', '-safe', '0', '-i', list, '-i', audio, '-map', '0:v', '-map', '1:a',
      '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart']);
    console.log(`영상을 저장했다. ${path.relative(process.cwd(), output)}`);
  } finally {
    rmSync(list, { force: true });
  }
}

function visualInputs(ep, art, storyFile, timelinePath) {
  const dir = path.dirname(art);
  const root = path.join(ep, 'art');
  const source = existsSync(art) ? art : [dir, root, ep].find(existsSync);
  return [storyFile, timelinePath, script('build.mjs'), script('lib.mjs'), source, ...(dir === root ? [root] : [])];
}

async function videoStages(ep, out, storyFile, formatName, stage, { jobs, force, noPhone }) {
  const timelinePath = path.join(out, 'timeline.json');
  const audio = path.join(out, 'audio.wav');
  if (!existsSync(audio) || !fresh(timelinePath, [storyFile, path.join(out, 'voices.json'), audio])) {
    throw new Error('voice 또는 timeline 단계를 먼저 실행한다.');
  }
  const timeline = readJson(timelinePath);
  const format = FORMATS[formatName];
  const dir = path.join(out, formatName);
  const scenesDir = path.join(dir, 'scenes');
  const items = timeline.scenes.map((scene, i) => {
    const art = artPath(ep, formatName, scene.art);
    return { scene, i, art, inputs: visualInputs(ep, art, storyFile, timelinePath),
      video: path.join(scenesDir, `${scene.id}.mp4`) };
  });

  if (stage === 'visual' || stage === 'all') {
    mkdirSync(scenesDir, { recursive: true });
    await parallelScenes(items.length, jobs, async index => {
      const { scene, i, art, inputs, video } = items[index];
      if (!force && fresh(video, inputs)) {
        console.log(`${scene.id} 기존 장면을 쓴다.`);
        return;
      }
      await makeScene(scene, i, art, video, format);
      console.log(`${scene.id} 장면을 만들었다.`);
    });
    await joinVideo(path.join(dir, 'video.mp4'), audio, items.map(({ video, scene }) => ({ file: video, scene })), force);
  }
  if (stage === 'finish' || stage === 'all') {
    const stale = items.find(({ video, inputs }) => !fresh(video, inputs));
    if (stale) throw new Error(`${stale.scene.id} 장면 영상이 없거나 오래됐다. visual 단계를 먼저 실행한다.`);
    await finishVideo(items, audio, dir, timelinePath, format, { jobs, force, noPhone });
  }
}

async function finishVideo(items, audio, dir, timelinePath, format, { jobs, force, noPhone }) {
  const subsDir = path.join(dir, 'subs');
  const finishedDir = path.join(dir, 'finished-scenes');
  mkdirSync(subsDir, { recursive: true });
  mkdirSync(finishedDir, { recursive: true });
  const captions = items.map(({ scene }) => scene.lines.map((line, i) => ({
    text: line.t, line, out: path.join(subsDir, `${scene.id}-${i + 1}.png`), color: line.who === 'narr' ? '#ffffff' : '#ffe9a8',
  })).filter(sub => sub.line.start < sub.line.end));
  const captionInputs = [timelinePath, script('subs.py'), script('lib.mjs')];
  const needed = captions.flat().filter(job => force || !fresh(job.out, captionInputs));
  if (needed.length) {
    const jobFile = path.join(dir, `.subs-${process.pid}.json`);
    save(jobFile, JSON.stringify(needed.map(({ text, out, color }) => ({ text, out, color }))));
    try {
      const layout = JSON.stringify({ w: format.w, h: format.h, ...format.subs });
      const { stderr } = await run('uv', ['run', '--with', 'pillow', 'python', script('subs.py'), jobFile, layout]);
      if (stderr.trim()) console.warn(stderr.trim());
    } finally {
      rmSync(jobFile, { force: true });
    }
  }

  const clips = items.map(({ scene }) => path.join(finishedDir, `${scene.id}.mp4`));
  await parallelScenes(items.length, jobs, async i => {
    const { scene, video } = items[i];
    if (!force && fresh(clips[i], [video, ...captions[i].map(sub => sub.out)])) return;
    await addSubtitles(scene, video, captions[i], clips[i], format.subs);
    console.log(`${scene.id} 자막을 영상에 얹었다.`);
  });

  const final = path.join(dir, 'final.mp4');
  await joinVideo(final, audio, items.map(({ scene }, i) => ({ file: clips[i], scene })), force);
  const phone = path.join(dir, 'final-phone.mp4');
  if (!noPhone && (force || !fresh(phone, [final]))) {
    const tmp = mkdtempSync(path.join(dir, '.phone-'));
    try {
      const output = path.join(tmp, 'final-phone.mp4');
      await run('sh', [script('finish.sh'), '--phone-only', final, output], {
        env: { ...process.env, TMPDIR: tmp, PHONE_SCALE: format.phone.join(':') },
      });
      renameSync(output, phone);
      console.log(`휴대폰용 영상을 저장했다. ${path.relative(process.cwd(), phone)}`);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  }
}

async function main() {
  const { positionals: [dir, stage = 'all', ...extra], values } = parseArgs({
    options: {
      voices: { type: 'string' }, provider: { type: 'string' }, force: { type: 'boolean' },
      jobs: { type: 'string', default: '2' }, 'no-phone': { type: 'boolean' }, format: { type: 'string' },
    },
    allowPositionals: true,
  });
  const jobs = Number(values.jobs);
  if (!dir || extra.length || !['voice', 'timeline', 'visual', 'finish', 'all'].includes(stage)
      || ![1, 2].includes(jobs) || (values.provider !== undefined && values.provider !== 'say')) {
    throw new Error('쓰는 법 · node build.mjs <에피소드> [voice|timeline|visual|finish|all] [--format wide|shorts] [--voices <파일>] [--provider say] [--force] [--jobs 1|2] [--no-phone]');
  }
  const ep = path.resolve(dir);
  const out = path.join(ep, 'out');
  const storyFile = path.join(ep, 'story.json');
  const story = validateStory(readJson(storyFile));
  const format = storyFormat(story, values.format);
  mkdirSync(out, { recursive: true });

  if (['voice', 'timeline', 'all'].includes(stage)) {
    const voiceDir = path.join(ep, 'voice');
    const setup = path.join(out, 'voices.json');
    const lines = speechLines(story);
    const remembered = stage === 'timeline' && fresh(setup, [storyFile]) ? readJson(setup) : undefined;
    const voices = selectVoices(story, lines, { provider: values.provider, voices: values.voices ? readJson(values.voices) : remembered });
    const results = stage === 'timeline' ? cachedVoice(lines, voices, voiceDir)
      : await makeVoice(lines, voices, voiceDir, values.force);
    const json = JSON.stringify(voices, null, 2) + '\n';
    if ((stage !== 'timeline' && values.force) || !fresh(setup, [storyFile]) || readFileSync(setup, 'utf8') !== json) {
      save(setup, json);
    }
    await makeTimeline(story, ep, out, [storyFile, setup], results, values.force);
  }
  if (['visual', 'finish', 'all'].includes(stage)) {
    await videoStages(ep, out, storyFile, format, stage, { jobs, force: values.force, noPhone: values['no-phone'] });
  }
}

main().catch(error => {
  console.error(error.stderr?.trim() || error.message);
  process.exitCode = 1;
});
