import { execFile, execFileSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync,
  statSync, symlinkSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs, promisify } from 'node:util';
import { FORMATS, FPS, storyFormat, validateStory } from './lib.mjs';

const run = promisify(execFile);
const script = name => path.join(import.meta.dirname, name);
const mtime = file => statSync(file, { throwIfNoEntry: false })?.mtimeMs ?? -Infinity;
const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
const sameJson = (file, data) => {
  try {
    return JSON.stringify(readJson(file)) === JSON.stringify(data);
  } catch (error) {
    if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
    return false;
  }
};
const tones = { redmoon: '0x741b2c@0.25', dark: '0x080817@0.46' };
const weather = ['fog', 'rain', 'snow'];
const effects = [...weather, ...Object.keys(tones), 'flash'];
const texturesFor = fx => [...new Set(fx.filter(name => weather.includes(name)))];

export const cutsceneMode = story => story?.style?.mode === 'cutscene';
export const styleText = ({ style }) => style && typeof style === 'object' ? style.prompt ?? '' : style;

export function validateCutscene(story) {
  if (!cutsceneMode(story)) return story;
  if (story.style.prompt !== undefined && typeof story.style.prompt !== 'string') {
    throw new Error('style.prompt는 문자열이어야 한다.');
  }
  const integer = (value, low, high) => Number.isInteger(value) && value >= low && value <= high;
  for (const { id, fx, sfx, anim } of story.scenes) {
    if (fx !== undefined && (!Array.isArray(fx) || fx.some(name => !effects.includes(name)))) {
      throw new Error(`${id}의 fx에는 ${effects.join(', ')}만 쓴다.`);
    }
    if (sfx !== undefined && (!Array.isArray(sfx) || sfx.some(event =>
      !Number.isFinite(event?.t) || event.t < 0 || typeof event.name !== 'string' || !/^[\w-]+$/.test(event.name)))) {
      throw new Error(`${id}의 sfx에는 0 이상의 t와 영문·숫자·밑줄·붙임표로 된 name을 쓴다.`);
    }
    if (anim === undefined) continue;
    if (!integer(anim?.cols, 1, 4) || !integer(anim.rows, 1, 4)
        || !Number.isFinite(anim.fps) || anim.fps < 0.5 || anim.fps > 30
        || (anim.loop !== undefined && typeof anim.loop !== 'boolean')
        || (anim.motion !== undefined && typeof anim.motion !== 'string')) {
      throw new Error(`${id}의 anim은 cols·rows 1~4, fps 0.5~30, loop 참·거짓, motion 문자열을 쓴다.`);
    }
    const count = anim.cols * anim.rows;
    if (anim.seq !== undefined
      ? !Array.isArray(anim.seq) || !anim.seq.length || anim.seq.some(cell => !integer(cell, 0, count - 1))
      : !integer(anim.frames, 1, count)) {
      throw new Error(`${id}의 anim은 seq에 0~${count - 1}번 칸을 하나 이상 넣거나 frames에 1~${count}를 쓴다.`);
    }
  }
  return story;
}

export const animSequence = anim => anim.seq ?? Array.from({ length: anim.frames }, (_, i) => i);

function cellAt(anim, seq, step) {
  return seq[anim.loop === false ? Math.min(step, seq.length - 1) : step % seq.length];
}

export const animCell = (anim, seconds) => cellAt(anim, animSequence(anim), Math.floor(seconds * anim.fps));

export function frameOrder(anim, frames, fps) {
  const seq = animSequence(anim);
  return Array.from({ length: frames }, (_, i) => cellAt(anim, seq, Math.floor(i * anim.fps / fps)));
}

export function cellRects(w, h, cols, rows) {
  const x = Array.from({ length: cols + 1 }, (_, i) => Math.floor(i * w / cols));
  const y = Array.from({ length: rows + 1 }, (_, i) => Math.floor(i * h / rows));
  return Array.from({ length: cols * rows }, (_, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    return { x: x[col], y: y[row], w: x[col + 1] - x[col], h: y[row + 1] - y[row] };
  });
}

export function sheetSize([w, h], cols, rows) {
  const ratio = w * cols / (h * rows);
  return [[1024, 1024], [1536, 1024], [1024, 1536]].reduce((best, size) =>
    Math.abs(size[0] / size[1] - ratio) < Math.abs(best[0] / best[1] - ratio) ? size : best);
}

export const SAFE_AREA = { shorts: { left: 54, top: 230, right: 940, bottom: 1440 } };

export function captionLayout(formatName, cutscene) {
  const { w, h, subs } = FORMATS[formatName];
  if (formatName === 'shorts' && cutscene) {
    return { w, h, ...subs, size: 76, minSize: 64, maxChars: 12, maxWidth: 800,
      preferredLines: 2, bottom: 1420, cx: 497, boxAlpha: 160 };
  }
  return { w, h, ...subs };
}

export function captionBox(layout, lines) {
  const w = layout.maxWidth + 56;
  const h = lines * (layout.size + 12) + 4;
  return { x: (layout.cx ?? layout.w / 2) - w / 2, y: layout.bottom - h, w, h };
}

export function insideSafe(box, formatName) {
  const bounds = SAFE_AREA[formatName] ?? { left: 0, top: 0, right: FORMATS[formatName].w, bottom: FORMATS[formatName].h };
  return box.x >= bounds.left && box.y >= bounds.top
    && box.x + box.w <= bounds.right && box.y + box.h <= bounds.bottom;
}

export function kenBurns(frames, kb, { w, h }) {
  const progress = `on/${Math.max(1, frames - 1)}`;
  const pan = kb === 'left' || kb === 'right';
  const zoom = pan ? '1.1' : kb === 'out' ? `1.12-0.12*${progress}` : `1+0.12*${progress}`;
  const x = kb === 'right' ? `(iw-iw/zoom)*${progress}`
    : kb === 'left' ? `(iw-iw/zoom)*(1-${progress})` : '(iw-iw/zoom)/2';
  return `scale=${w * 2}:${h * 2}:force_original_aspect_ratio=increase,crop=${w * 2}:${h * 2},setsar=1,`
    + `zoompan=z='${zoom}':x='${x}':y='(ih-ih/zoom)/2':d=${frames}:s=${w}x${h}:fps=${FPS}`;
}

export function placeholder(scene, index, { w, h }) {
  const colors = [0x242e40, 0x302638, 0x24372f, 0x3b2c24, 0x242940, 0x393321];
  const color = (colors[index % colors.length] + 0x010101 * (Math.floor(index / colors.length) % 16)).toString(16);
  console.warn(`${scene.id} 그림이 없어 단색을 쓴다.`);
  return ['-f', 'lavfi', '-i', `color=c=0x${color}:s=${w}x${h}:r=${FPS}`];
}

export function sceneFade(frames, flash = false) {
  const n = Math.min(9, Math.floor(frames / 2));
  const start = n > 1 || (flash && n) ? `,fade=in:s=0:n=${n}${flash ? ':color=white' : ''}` : '';
  return start + (n > 1 ? `,fade=out:s=${frames - n}:n=${n - 1}` : '');
}

export function fxFilter(fx, { w, h, frames }) {
  const textures = texturesFor(fx);
  const positions = {
    fog: `x='-mod(t*${w / 18},${w})':y=0`,
    rain: `x=0:y='mod(t*${h / 0.8},${h})-${h}'`,
    snow: `x='-32+32*sin(t*1.4)':y='mod(t*${h / 10},${h})-${h}'`,
  };
  const filters = [];
  let source = '[base]';
  for (const name of fx) {
    if (name === 'flash') continue;
    const label = `[fx${filters.length}]`;
    if (Object.hasOwn(tones, name)) {
      // 4:2:0에서는 색차 한 점에 반투명 색이 여러 번 섞인다.
      filters.push(`${source}format=yuv444p,drawbox=c=${tones[name]}:t=fill${label}`);
    } else if (weather.includes(name)) {
      filters.push(`${source}[${textures.indexOf(name) + 1}:v]overlay=${positions[name]}${label}`);
    } else {
      throw new Error(`모르는 화면 효과다(${name}).`);
    }
    source = label;
  }
  filters.push(`${source}format=yuv420p${sceneFade(frames, fx.includes('flash'))}[panel]`);
  return { filter: filters.join(';'), textures };
}

export function sfxEvents(timeline, story) {
  if (!cutsceneMode(story)) return [];
  const scenes = new Map(story.scenes.map(scene => [scene.id, scene]));
  return timeline.scenes.flatMap(({ id, start }) =>
    (scenes.get(id)?.sfx ?? []).map(({ t, name }) => ({ t: start + t, name })));
}

const gridData = (sheet, cols, rows) => ({ cols, rows, mtime: mtime(sheet) });
export const cellFiles = (dir, cols, rows) => Array.from({ length: cols * rows }, (_, i) =>
  path.join(dir, `${String(i + 1).padStart(2, '0')}.png`));
const gridReady = (dir, grid) => sameJson(path.join(dir, 'grid.json'), grid)
  && cellFiles(dir, grid.cols, grid.rows).every(existsSync);
const ffmpeg = args => run('ffmpeg', ['-nostdin', '-v', 'error', '-y', ...args]);

export async function cutSheet(sheet, cols, rows, outDir) {
  const grid = gridData(sheet, cols, rows);
  const files = cellFiles(outDir, cols, rows);
  if (gridReady(outDir, grid)) return files;
  const { stdout } = await run('ffprobe', ['-v', 'error', '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', sheet]);
  const [w, h] = stdout.trim().split('x').map(Number);
  if (!(w >= cols && h >= rows)) throw new Error('시트가 격자보다 작거나 크기를 읽을 수 없다.');
  mkdirSync(path.dirname(outDir), { recursive: true });
  const tmp = mkdtempSync(outDir + '-');
  try {
    // 칸 크기가 달라지면 ffmpeg가 프레임 도중 필터를 다시 시작한다.
    const cw = Math.floor(w / cols);
    const ch = Math.floor(h / rows);
    const crops = cellRects(w, h, cols, rows).map(({ x, y, w, h }, i) => {
      const scale = w === cw && h === ch ? '' : `,scale=${cw}:${ch}`;
      return `[c${i}]crop=${w}:${h}:${x}:${y}${scale}[p${i}]`;
    });
    const split = crops.length === 1 ? '[c0]' : `,split=${crops.length}${crops.map((_, i) => `[c${i}]`).join('')}`;
    await ffmpeg(['-filter_complex_threads', '2', '-i', sheet,
      '-filter_complex', `[0:v]format=rgb24${split};${crops.join(';')}`,
      ...cellFiles(tmp, cols, rows).flatMap((file, i) =>
        ['-map', `[p${i}]`, '-threads', '1', '-frames:v', '1', '-update', '1', file])]);
    writeFileSync(path.join(tmp, 'grid.json'), JSON.stringify(grid));
    rmSync(outDir, { recursive: true, force: true });
    renameSync(tmp, outDir);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
  return files;
}

export const animDir = (ep, formatName, id) => path.join(ep, 'art', formatName, 'anim', id);
const textureFile = (ep, formatName, name) => path.join(ep, 'out', formatName, 'fx', `${name}.png`);
const textureReady = file => mtime(file) >= mtime(script('fx.py'));

export function panelInputs({ scene, storyScene, ep, formatName, art }) {
  const anim = storyScene?.anim;
  const dir = anim && animDir(ep, formatName, storyScene.id);
  const sheet = dir && existsSync(dir + '.png') ? dir + '.png' : null;
  const grid = sheet ? gridData(sheet, anim.cols, anim.rows) : null;
  const cells = sheet ? cellFiles(dir, anim.cols, anim.rows) : [];
  const used = sheet ? [...new Set(frameOrder(anim, Math.round(scene.dur * FPS), FPS))].map(cell => cells[cell]) : [];
  const fx = texturesFor(storyScene?.fx ?? []).map(name => textureFile(ep, formatName, name));
  const still = !sheet && existsSync(art) ? art : null;
  return {
    manifest: { mode: sheet ? 'anim' : still ? 'still' : 'color', sheet, art: still, cells: used, fx },
    files: [script('cutscene.mjs'), script('fx.py'), ...[sheet, still, ...used, ...fx].filter(Boolean)],
    ready: (!grid || gridReady(dir, grid)) && fx.every(textureReady),
  };
}

function makeTexture(ep, formatName, name, { w, h }) {
  const file = textureFile(ep, formatName, name);
  if (!textureReady(file)) {
    mkdirSync(path.dirname(file), { recursive: true });
    execFileSync('uv', ['run', '--with', 'pillow', 'python', script('fx.py'), name, String(w), String(h), file]);
  }
  return file;
}

export const videoEncoding = crf => ['-an', '-c:v', 'libx264', '-threads', '3', '-preset', 'veryfast', '-crf', String(crf),
  '-pix_fmt', 'yuv420p'];

export async function renderPanel({ scene, storyScene, index, ep, formatName, art, output }) {
  const format = FORMATS[formatName];
  const { w, h } = format;
  const frames = Math.round(scene.dur * FPS);
  if (frames < 1) throw new Error(`${scene.id}의 길이가 한 프레임보다 짧다.`);
  const { manifest } = panelInputs({ scene, storyScene, ep, formatName, art });
  mkdirSync(path.dirname(output), { recursive: true });
  const tmp = mkdtempSync(path.join(path.dirname(output), '.panel-'));
  try {
    let input, base;
    if (manifest.sheet) {
      const { anim } = storyScene;
      const dir = animDir(ep, formatName, storyScene.id);
      const cells = await cutSheet(manifest.sheet, anim.cols, anim.rows, dir);
      frameOrder(anim, frames, FPS).forEach((cell, i) => {
        symlinkSync(path.resolve(cells[cell]), path.join(tmp, `f${String(i + 1).padStart(5, '0')}.png`));
      });
      input = ['-framerate', String(FPS), '-i', path.join(tmp, 'f%05d.png')];
      base = `scale=${w}:${h}:force_original_aspect_ratio=increase,crop=${w}:${h},setsar=1`;
    } else if (manifest.art) {
      input = ['-i', art];
      base = kenBurns(frames, scene.kb, format);
    } else {
      input = placeholder(scene, index, format);
      base = 'null';
    }
    const { filter, textures } = fxFilter(storyScene?.fx ?? [], { ...format, frames, fps: FPS });
    for (const name of textures) {
      input.push('-loop', '1', '-framerate', String(FPS), '-i', makeTexture(ep, formatName, name, format));
    }
    const video = path.join(tmp, 'panel.mp4');
    await ffmpeg(['-filter_complex_threads', '2', ...input, '-filter_complex', `[0:v]${base}[base];${filter}`,
      '-map', '[panel]', '-frames:v', String(frames), ...videoEncoding(14), video]);
    renameSync(video, output);
    return manifest;
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

export async function mixSfx({ timeline, story, ep, audio, outDir }) {
  if (!cutsceneMode(story)) return audio;
  const output = path.join(outDir, 'audio-sfx.wav');
  const manifest = path.join(outDir, 'sfx.json');
  const missing = new Set();
  const inputs = sfxEvents(timeline, story).flatMap(({ t, name }) => {
    const file = ['wav', 'mp3', 'm4a'].map(ext => path.join(ep, 'sfx', `${name}.${ext}`))
      .find(file => statSync(file, { throwIfNoEntry: false })?.isFile());
    if (file) return [{ t, file, mtime: mtime(file) }];
    missing.add(name);
    return [];
  });
  for (const name of missing) console.warn(`효과음 ${name} 파일이 없어 생략한다.`);
  if (mtime(output) >= mtime(audio) && existsSync(output) && sameJson(manifest, inputs)) return output;

  mkdirSync(outDir, { recursive: true });
  const tmp = mkdtempSync(path.join(outDir, '.sfx-'));
  try {
    const wav = path.join(tmp, 'audio.wav');
    if (inputs.length) {
      const delays = inputs.map(({ t }, i) => `[${i + 1}:a]adelay=${Math.round(t * 1000)}:all=1[a${i + 1}]`);
      const mix = `[0:a]${inputs.map((_, i) => `[a${i + 1}]`).join('')}amix=inputs=${inputs.length + 1}:normalize=0:duration=first`;
      await ffmpeg(['-i', audio, ...inputs.flatMap(({ file }) => ['-i', file]), '-filter_complex',
        [...delays, mix].join(';'), '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', wav]);
    } else {
      copyFileSync(audio, wav);
    }
    const json = path.join(tmp, 'sfx.json');
    writeFileSync(json, JSON.stringify(inputs));
    renameSync(wav, output);
    renameSync(json, manifest);
    return output;
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}

function main() {
  const { positionals: [ep, ...extra], values } = parseArgs({
    options: { format: { type: 'string' }, check: { type: 'boolean' } },
    allowPositionals: true,
  });
  if (!ep || extra.length || !values.check) {
    throw new Error('쓰는 법 · node cutscene.mjs <에피소드> [--format wide|shorts] --check');
  }
  const story = validateCutscene(validateStory(readJson(path.join(ep, 'story.json'))));
  storyFormat(story, values.format);
  console.log('화면 설정을 확인했다.');
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
