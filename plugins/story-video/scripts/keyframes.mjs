import { execFile } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { placeholder, videoEncoding } from './cutscene.mjs';
import { FORMATS, FPS } from './lib.mjs';

const run = promisify(execFile);
const ffmpegOptions = ['-nostdin', '-v', 'error', '-y', '-filter_complex_threads', '2'];

export const DEFAULT_KEY = 'the same moment a few seconds later: the people shift their posture slightly and turn their heads and gaze a little; same camera framing';
export const MIN_HOLD = 4;
export const TRANSITION = 1.5;
export const motionMode = story => story?.style?.mode === 'motion';

export function validateMotion(story) {
  if (!motionMode(story)) return story;
  const { interp } = story.style;
  if (interp !== undefined && !['fade', 'mci'].includes(interp)) {
    throw new Error('style.interp에는 fade 또는 mci를 쓴다.');
  }
  for (const { id, keys } of story.scenes) {
    if (keys !== undefined && (!Array.isArray(keys) || keys.length > 3
        || keys.some(key => typeof key !== 'string' || !key.trim()))) {
      throw new Error(`${id}의 keys에는 비어 있지 않은 문자열을 0~3개 넣는다.`);
    }
  }
  return story;
}

export function sceneKeys(storyScene) {
  return storyScene ? storyScene.keys ?? [DEFAULT_KEY] : [];
}

export const keyPath = (ep, formatName, id, i) => path.join(ep, 'art', formatName, 'keys', `${id}-${i}.png`);
export const keyRecord = (ep, formatName, id, i) => path.join(ep, 'art', formatName, 'keys', `${id}-${i}.txt`);

export function keyFresh({ key, record, base, prompt }) {
  try {
    return statSync(key).mtimeMs >= statSync(base).mtimeMs && readFileSync(record, 'utf8') === prompt;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

export function keyPlan(frames, count) {
  const used = Math.min(count, Math.max(1, Math.floor(frames / (MIN_HOLD * FPS))));
  const changes = Array.from({ length: Math.max(0, used - 1) }, (_, i) => Math.round(frames * (i + 1) / used));
  return { used, changes };
}

export const tailFrames = frames => Math.min(FPS, Math.floor(frames / 2));

export function motionCamera(frames, kb, { w, h }) {
  const phase = `on/${Math.max(1, frames - 1)}`;
  const travel = 'iw-iw/zoom';
  let zoom = `1+0.05*${phase}`;
  let x = `(${travel})/2`;
  if (kb === 'out') zoom = `1.05-0.05*${phase}`;
  if (kb === 'left' || kb === 'right') {
    zoom = '1.05';
    x = `(${travel})*(${kb === 'left' ? `1-${phase}` : phase})`;
  }
  return `zoompan=z='${zoom}':x='${x}':y='(ih-ih/zoom)/2':d=1:s=${w}x${h}:fps=${FPS}`;
}

export function motionImages({ storyScene, ep, formatName, art }, warn = () => {}) {
  const images = art && existsSync(art) ? [art] : [];
  sceneKeys(storyScene).forEach((_, index) => {
    const number = index + 1;
    const key = keyPath(ep, formatName, storyScene.id, number);
    if (!existsSync(key)) warn(`${storyScene.id} 키프레임 ${number}번이 없다.`);
    else if (art && existsSync(art) && statSync(key).mtimeMs < statSync(art).mtimeMs) {
      warn(`${storyScene.id} 키프레임 ${number}번이 장면 그림보다 오래됐다(--keys로 다시 만든다).`);
    } else images.push(key);
  });
  return images;
}

export function motionInputs(item) {
  const { scene, storyScene, next, interp, first, last } = item;
  const allImages = motionImages(item);
  const { used } = keyPlan(Math.round(scene.dur * FPS), allImages.length);
  const images = allImages.slice(0, used);
  const manifest = { mode: 'motion', images, next: next?.art ?? null, nextKb: next?.kb,
    nextDur: next?.dur, interp, keys: sceneKeys(storyScene), first, last };
  return {
    ready: true, manifest,
    files: [fileURLToPath(import.meta.url), ...images, ...(next?.art ? [next.art] : [])],
  };
}

const normalized = ({ w, h }, k = 2) => `scale=${k * w}:${k * h}:force_original_aspect_ratio=increase,crop=${k * w}:${k * h},setsar=1,format=yuv420p`;
const transitionFrames = TRANSITION * FPS;

async function interpolate(before, after, format, file) {
  // 2배 크기에서 보간하면 전환 하나에 몇 분·수 GB가 든다. 최종 크기에서 보간하고 합성용으로 2배로 키운다.
  const normalize = normalized(format, 1);
  const filters = [
    `[0:v]${normalize}[before]`,
    `[1:v]${normalize},tpad=stop_mode=clone:stop=1[after]`,
    `[before][after]concat=n=2:v=1:a=0,settb=3/2,setpts=N,minterpolate=fps=${FPS}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:scd=none,scale=${2 * format.w}:${2 * format.h},setsar=1[mci]`,
  ];
  // minterpolate가 첫 전환을 보간하려면 뒤 그림을 한 장 더 입력해야 한다.
  await run('ffmpeg', [...ffmpegOptions, '-framerate', '2/3', '-i', before, '-framerate', '2/3', '-i', after,
    '-filter_complex', filters.join(';'), '-map', '[mci]', '-frames:v', String(transitionFrames),
    '-c:v', 'ffv1', '-threads', '2', file]);
  const { stdout } = await run('ffprobe', ['-v', 'error', '-count_frames', '-select_streams', 'v:0',
    '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', file]);
  if (Number(stdout.trim()) !== transitionFrames) throw new Error('보간 프레임 수가 전환 길이와 다르다.');
}

export async function motionArgs(item, tmp) {
  const { scene, index, formatName, interp, first, last, next } = item;
  const format = FORMATS[formatName];
  const frames = Math.round(scene.dur * FPS);
  if (frames < 1) throw new Error(`${scene.id}의 길이가 한 프레임보다 짧다.`);
  const images = motionImages(item, console.warn);
  const { used, changes } = keyPlan(frames, images.length);
  const inputs = [];
  const filters = [];
  let inputCount = 0;
  const add = args => {
    inputs.push(...args);
    return `[${inputCount++}:v]`;
  };
  const still = (file, duration = frames / FPS + 1) => add([
    '-loop', '1', '-framerate', String(FPS), '-t', String(duration), '-threads', '1', '-i', file,
  ]);
  const camera = (n, kb) => `${motionCamera(n, kb, format)},format=yuv420p,setsar=1`;
  const normalize = normalized(format);
  let source;

  if (!used) {
    source = add(placeholder(scene, index, format));
  } else {
    filters.push(`${still(images[0])}${normalize}[base]`);
    source = '[base]';
    for (let i = 1; i < used; i++) {
      const change = changes[i - 1];
      const layer = `[key${i}]`;
      const mixed = `[mix${i}]`;
      const image = still(images[i]);
      let clip;
      if (interp === 'mci') {
        const file = path.join(tmp, `mci-${i}.nut`);
        try {
          await interpolate(images[i - 1], images[i], format, file);
          clip = add(['-i', file]);
        } catch {
          console.warn(`${scene.id} 키프레임 전환 ${i}번을 보간하지 못해 fade를 쓴다.`);
        }
      }
      if (clip) {
        filters.push(`${clip}settb=1/${FPS},setpts=N+${change}[mci${i}]`);
        filters.push(`${source}[mci${i}]overlay=format=yuv420:eof_action=pass:enable='gte(n,${change})'[moving${i}]`);
        filters.push(`${image}${normalize}${layer}`);
        filters.push(`[moving${i}]${layer}overlay=format=yuv420:enable='gte(n,${change + transitionFrames})'${mixed}`);
      } else {
        filters.push(`${image}${normalize},format=yuva420p,fade=in:s=${change}:n=${transitionFrames}:alpha=1${layer}`);
        filters.push(`${source}${layer}overlay=format=yuv420${mixed}`);
      }
      source = mixed;
    }
    filters.push(`${source}${camera(frames, scene.kb)}[camera]`);
    source = '[camera]';
  }

  const blackFrames = Math.min(9, Math.floor(frames / 2));
  if (first || (!used && blackFrames >= 2)) {
    filters.push(`${source}fade=in:s=0:n=${first ? 15 : blackFrames}[fadein]`);
    source = '[fadein]';
  }
  const tail = tailFrames(frames);
  if (last || !next) {
    const start = Math.max(0, frames - 15);
    filters.push(`${source}fade=out:s=${start}:n=${Math.max(1, frames - start - 1)}[fadeout]`);
    source = '[fadeout]';
  } else if (next.art && tail > 0) {
    const nextFrames = Math.round(next.dur * FPS);
    filters.push(`${still(next.art, 1 / FPS)}${normalize},${camera(nextFrames, next.kb)},trim=end_frame=1[target]`);
    if (tail === 1) {
      filters.push(`${source}trim=end_frame=${frames - 1}[main]`);
      filters.push(`[main][target]concat=n=2:v=1:a=0,settb=1/${FPS},setpts=N[dissolve]`);
    } else {
      filters.push(`[target]loop=loop=-1:size=1:start=0,settb=1/${FPS},setpts=N,format=yuva420p,fade=in:s=${frames - tail}:n=${tail - 1}:alpha=1[tail]`);
      filters.push(`${source}[tail]overlay=format=yuv420[dissolve]`);
    }
    source = '[dissolve]';
  } else if (!next.art && blackFrames >= 2) {
    filters.push(`${source}fade=out:s=${frames - blackFrames}:n=${blackFrames - 1}[fadeout]`);
    source = '[fadeout]';
  }
  filters.push(`${source}format=yuv420p,setsar=1[motion]`);
  return [...ffmpegOptions, ...inputs, '-filter_complex', filters.join(';'), '-map', '[motion]', '-frames:v', String(frames)];
}

export async function renderMotion(item) {
  mkdirSync(path.dirname(item.output), { recursive: true });
  const tmp = mkdtempSync(path.join(path.dirname(item.output), '.motion-'));
  try {
    const { manifest } = motionInputs(item);
    const video = path.join(tmp, 'motion.mp4');
    const args = await motionArgs(item, tmp);
    await run('ffmpeg', [...args, ...videoEncoding(14), video]);
    renameSync(video, item.output);
    return manifest;
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}
