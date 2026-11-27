import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { buildTimeline, FPS, speechLines, storyScenes, toSrt, validateStory } from './lib.mjs';
import { clean, estimate, loadEnv, overFree, speak } from './tts.mjs';

const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
const mtime = file => statSync(file, { throwIfNoEntry: false })?.mtimeMs ?? -Infinity;
const fresh = (file, inputs) => existsSync(file) && inputs.every(input => mtime(file) >= mtime(input));
const script = name => path.join(import.meta.dirname, name);
const SAMPLE_RATE = 48000;
const WIDTH = 1920;
const HEIGHT = 1080;
const BAND = 260;

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

function sayVoice(who) {
  let hash = 0;
  for (const char of who) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return { provider: 'say', id: 'Yuna', rate: who === 'narr' ? 170 : 150 + hash % 41 };
}

function durationOk(result, who, text) {
  if (!Number.isFinite(result.dur) || result.dur <= 0) {
    throw new Error(`${who}의 음성 길이가 0이거나 잘못됐다(${text.slice(0, 20)}).`);
  }
  return result;
}

async function makeVoice(lines, voices, dir, out, force) {
  loadEnv(process.env.STORY_VIDEO_VOICE_ENV ?? '~/.config/story-video/voice.env');
  const scratch = mkdtempSync(path.join(out, '.voice-'));
  try {
    const key = ({ who, t }) => JSON.stringify([who, t]);
    const unique = [...new Map(lines.map(line => [key(line), line])).values()];
    const need = estimate(unique, voices, force ? scratch : dir);
    const requests = Object.entries(need).map(([provider, n]) => `${provider} ${n.requests}번 ${n.chars}자`);
    console.log(`음성 ${lines.length}문장. 새로 받을 음성 ${requests.join(', ') || '없음(캐시 사용)'}.`);
    const limits = overFree(need);
    for (const limit of limits) console.warn(`무료 한도 초과 · ${limit}`);
    if (limits.length && !force) throw new Error('무료 한도를 넘어서 멈춘다. 계속하려면 --force를 쓴다.');

    const results = new Map();
    mkdirSync(dir, { recursive: true });
    for (const line of unique) {
      const voice = voices[line.who] ?? voices.narr;
      const useScratch = force || Object.keys(estimate([line], voices, dir)).length > 0;
      const result = durationOk(await speak(voice, line.t, useScratch ? scratch : dir, line.who), line.who, line.t);
      if (useScratch) {
        const file = path.join(dir, path.basename(result.file));
        renameSync(result.file, file);
        result.file = file;
      }
      results.set(key(line), result);
    }
    console.log(`음성 ${lines.length}문장을 준비했다.`);
    return lines.map(line => results.get(key(line)));
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

async function cachedVoice(lines, voices, dir) {
  if (Object.keys(estimate(lines, voices, dir)).length) throw new Error('빠진 음성이 있다. voice 단계를 먼저 실행한다.');
  const results = [];
  for (const { who, t } of lines) {
    results.push(durationOk(await speak(voices[who] ?? voices.narr, t, dir, who), who, t));
  }
  return results;
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

function motion(frames, kb) {
  const progress = `on/${Math.max(1, frames - 1)}`;
  const pan = kb === 'left' || kb === 'right';
  const zoom = pan ? '1.1' : kb === 'out' ? `1.12-0.12*${progress}` : `1+0.12*${progress}`;
  const x = kb === 'right' ? `(iw-iw/zoom)*${progress}`
    : kb === 'left' ? `(iw-iw/zoom)*(1-${progress})` : '(iw-iw/zoom)/2';
  return `scale=3840:2160:force_original_aspect_ratio=increase,crop=3840:2160,setsar=1,`
    + `zoompan=z='${zoom}':x='${x}':y='(ih-ih/zoom)/2':d=${frames}:s=${WIDTH}x${HEIGHT}:fps=${FPS}`;
}

async function makeScene(scene, index, art, subs, output) {
  const frames = Math.round(scene.dur * FPS);
  if (frames < 1) throw new Error(`${scene.id}의 길이가 한 프레임보다 짧다.`);
  const hasArt = existsSync(art);
  const colors = [0x242e40, 0x302638, 0x24372f, 0x3b2c24, 0x242940, 0x393321];
  const color = (colors[index % colors.length] + 0x010101 * (Math.floor(index / colors.length) % 16)).toString(16);
  const args = ['-nostdin', '-v', 'error', '-y', '-filter_complex_threads', '2'];
  if (hasArt) {
    args.push('-i', art);
  } else {
    console.warn(`${scene.id} 그림이 없어 단색을 쓴다.`);
    args.push('-f', 'lavfi', '-i', `color=c=0x${color}:s=${WIDTH}x${HEIGHT}:r=${FPS}`);
  }
  for (const sub of subs) args.push('-i', sub.out);
  const fadeFrames = Math.min(9, Math.floor(frames / 2));
  const fade = fadeFrames > 1 ? `,fade=in:s=0:n=${fadeFrames},fade=out:s=${frames - fadeFrames}:n=${fadeFrames - 1}` : '';
  const filters = [`[0:v]${hasArt ? motion(frames, scene.kb) : 'null'},format=yuv420p${fade}[v0]`];
  subs.forEach(({ line }, i) => {
    const start = (Math.round((line.start - scene.start) * FPS) - 0.5) / FPS;
    const end = (Math.round((line.end - scene.start) * FPS) - 0.5) / FPS;
    filters.push(`[v${i}][${i + 1}:v]overlay=0:${HEIGHT - BAND}:enable='between(t,${start},${end})'[v${i + 1}]`);
  });

  const tmp = output.replace(/\.mp4$/, `.${process.pid}.tmp.mp4`);
  try {
    await run('ffmpeg', [...args, '-filter_complex', filters.join(';'), '-map', `[v${subs.length}]`,
      '-frames:v', String(frames), '-an', '-c:v', 'libx264', '-threads', '3',
      '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', tmp]);
    renameSync(tmp, output);
  } finally {
    rmSync(tmp, { force: true });
  }
}

async function render(ep, out, storyFile, jobs, force, noPhone) {
  const timelinePath = path.join(out, 'timeline.json');
  const audio = path.join(out, 'audio.wav');
  if (!existsSync(audio) || !fresh(timelinePath, [storyFile, path.join(out, 'voices.json'), audio])) {
    throw new Error('timeline 단계를 먼저 실행한다.');
  }
  const timeline = readJson(timelinePath);
  const subsDir = path.join(out, 'subs');
  const scenesDir = path.join(out, 'scenes');
  mkdirSync(subsDir, { recursive: true });
  mkdirSync(scenesDir, { recursive: true });
  const subs = timeline.scenes.map(scene => scene.lines.map((line, i) => ({
    text: line.t, line, out: path.join(subsDir, `${scene.id}-${i + 1}.png`), color: line.who === 'narr' ? '#ffffff' : '#ffe9a8',
  })).filter(sub => sub.line.start < sub.line.end));
  const needed = subs.flat().filter(job => force || !fresh(job.out, [timelinePath, script('subs.py')]));
  if (needed.length) {
    const jobFile = path.join(out, `subs-jobs.${process.pid}.json`);
    save(jobFile, JSON.stringify(needed.map(({ text, out, color }) => ({ text, out, color }))));
    try {
      const { stderr } = await run('uv', ['run', '--with', 'pillow', 'python3', script('subs.py'), jobFile]);
      if (stderr.trim()) console.warn(stderr.trim());
    } finally {
      rmSync(jobFile, { force: true });
    }
  }

  const paths = timeline.scenes.map(scene => path.join(scenesDir, `${scene.id}.mp4`));
  let index = 0;
  let failure;
  await Promise.all(Array.from({ length: jobs }, async () => {
    while (!failure && index < paths.length) {
      const i = index++;
      const scene = timeline.scenes[i];
      const art = path.join(ep, 'art', `${scene.art}.png`);
      const artSource = existsSync(art) ? art : existsSync(path.dirname(art)) ? path.dirname(art) : ep;
      const inputs = [storyFile, timelinePath, artSource, ...subs[i].map(sub => sub.out)];
      if (!force && fresh(paths[i], inputs)) {
        console.log(`${scene.id} 기존 장면을 쓴다(0.0초).`);
        continue;
      }
      const started = performance.now();
      try {
        await makeScene(scene, i, art, subs[i], paths[i]);
        console.log(`${scene.id} 장면을 만들었다(${((performance.now() - started) / 1000).toFixed(1)}초).`);
      } catch (error) {
        console.error(`${scene.id} 장면을 만들지 못했다(${((performance.now() - started) / 1000).toFixed(1)}초).`);
        failure ??= error;
      }
    }
  }));
  if (failure) throw failure;

  const final = path.join(out, 'final.mp4');
  const phone = path.join(out, 'final-phone.mp4');
  const inputs = [audio, ...paths];
  if (!force && fresh(final, inputs) && (noPhone || fresh(phone, [final]))) {
    console.log('합친 영상은 기존 파일을 쓴다.');
    return;
  }
  const tmp = mkdtempSync(path.join(out, '.finish-'));
  try {
    await run('sh', [script('finish.sh'), path.join(tmp, 'final.mp4'), audio, ...paths], {
      env: { ...process.env, TMPDIR: tmp, PHONE: noPhone ? '0' : '1' },
    });
    if (!noPhone) renameSync(path.join(tmp, 'final-phone.mp4'), phone);
    renameSync(path.join(tmp, 'final.mp4'), final);
    console.log(`영상 ${timeline.total.toFixed(3)}초를 저장했다. ${path.relative(process.cwd(), final)}`);
    if (!noPhone) console.log(`휴대폰용 영상을 저장했다. ${path.relative(process.cwd(), phone)}`);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

async function main() {
  const { positionals: [dir, stage = 'all', ...extra], values } = parseArgs({
    options: {
      voices: { type: 'string' }, provider: { type: 'string' }, force: { type: 'boolean' },
      jobs: { type: 'string', default: '2' }, 'no-phone': { type: 'boolean' },
    },
    allowPositionals: true,
  });
  const jobs = Number(values.jobs);
  if (!dir || extra.length || !['voice', 'timeline', 'render', 'all'].includes(stage)
      || ![1, 2].includes(jobs) || (values.provider && values.provider !== 'say')) {
    throw new Error('쓰는 법 · node build.mjs <에피소드> [voice|timeline|render|all] [--voices <파일>] [--provider say] [--force] [--jobs 1|2] [--no-phone]');
  }
  const ep = path.resolve(dir);
  const out = path.join(ep, 'out');
  const storyFile = path.join(ep, 'story.json');
  const story = validateStory(readJson(storyFile));
  mkdirSync(out, { recursive: true });

  if (stage !== 'render') {
    const voiceDir = path.join(ep, 'voice');
    const setup = path.join(out, 'voices.json');
    const lines = speechLines(story).map(line => ({ ...line, t: clean(line.t) }));
    const remembered = stage === 'timeline' && fresh(setup, [storyFile]) ? readJson(setup) : undefined;
    const config = values.voices ? readJson(values.voices) : remembered ?? story.voices;
    const voices = Object.assign(Object.create(null), values.provider === 'say'
      ? Object.fromEntries(lines.map(({ who }) => [who, sayVoice(who)])) : config);
    const results = stage === 'timeline' ? await cachedVoice(lines, voices, voiceDir)
      : await makeVoice(lines, voices, voiceDir, out, values.force);
    const json = JSON.stringify(voices, null, 2) + '\n';
    if ((stage !== 'timeline' && values.force) || !fresh(setup, [storyFile]) || readFileSync(setup, 'utf8') !== json) {
      save(setup, json);
    }
    if (stage !== 'voice') await makeTimeline(story, ep, out, [storyFile, setup], results, values.force);
  }
  if (stage === 'render' || stage === 'all') {
    await render(ep, out, storyFile, jobs, values.force, values['no-phone']);
  }
}

main().catch(error => {
  console.error(error.stderr?.trim() || error.message);
  process.exitCode = 1;
});
