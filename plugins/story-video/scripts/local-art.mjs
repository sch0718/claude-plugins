import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { accessSync, closeSync, constants, existsSync, mkdtempSync, openSync, readFileSync, renameSync, rmSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { styleText } from './cutscene.mjs';

export const LOCAL_SIZES = { wide: [1152, 768], shorts: [768, 1152] };
export const LOCAL_PRESETS = {
  photoreal: '35mm cinematic film still, natural skin texture, shallow depth of field, fine film grain, muted colors',
};

export function artEngine(story, override) {
  const style = story.style;
  const configured = style && typeof style === 'object' && Object.hasOwn(style, 'engine')
    ? style.engine : 'codex';
  const engine = override ?? configured;
  if (!['codex', 'local'].includes(engine)) throw new Error('그림 엔진은 codex 또는 local을 쓴다.');
  return engine;
}

export function localPrompt(story, scene, formatName) {
  const style = story.style?.preset === 'photoreal'
    ? [LOCAL_PRESETS.photoreal, story.style.prompt].filter(Boolean).join(', ')
    : styleText(story);
  const cast = (scene.cast ?? []).map(id => {
    const character = (story.characters ?? []).find(person => person.id === id);
    if (!character) throw new Error(`${scene.id}에 없는 인물 id가 있다(${id}).`);
    return `${character.name}: ${character.look}`;
  });
  const composition = formatName === 'shorts' ? 'Keep important people and objects near the center or upper center.' : '';
  return [style, scene.art, ...cast,
    'All people are fictional and must not resemble real people or celebrities.',
    'No text, captions, logos or watermarks.', composition].filter(Boolean).join('\n') + '\n';
}

export function editPrompt(change) {
  return `Keep the same photo: same people, faces, clothes, background, lighting and camera framing. Make only this small, natural change: ${String(change).replace(/[.\s]+$/, '')}. No text.\n`;
}

export function sceneSeed(scene) {
  if (Number.isSafeInteger(scene.seed) && scene.seed >= 0) return scene.seed;
  return createHash('sha256').update(scene.id).digest().readUInt32BE() % 2 ** 31;
}

const expandHome = value => value.startsWith('~/') ? path.join(os.homedir(), value.slice(2)) : value;
const artEnvFile = () => process.env.STORY_VIDEO_ART_ENV ?? '~/.config/story-video/art.env';

export function readArtEnv(file = artEnvFile()) {
  let source;
  try {
    source = readFileSync(expandHome(file), 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return {};
    throw error;
  }
  const env = {};
  for (const line of source.split(/\r?\n/)) {
    const match = line.match(/^\s*(ZIMAGE_MODEL|FLUX2_MODEL)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^"'#]*?))\s*(?:#.*)?$/);
    if (match) {
      const [, key, doubleQuoted, singleQuoted, unquoted] = match;
      env[key] = expandHome(doubleQuoted ?? singleQuoted ?? unquoted);
    }
  }
  return env;
}

export function modelPath(env, key) {
  const value = env[key];
  if (!value) throw new Error(`그림 설정 파일(${artEnvFile()})에 ${key}=/path/to/model을 설정한다.`);
  if (!path.isAbsolute(value) || !existsSync(value)) {
    throw new Error(`그림 설정 파일의 ${key}에 실제로 존재하는 로컬 모델의 절대 경로를 쓴다.`);
  }
  return value;
}

export function mfluxBin(name) {
  const dirs = [...(process.env.PATH ?? '').split(path.delimiter).filter(Boolean), path.join(os.homedir(), '.local/bin')];
  for (const dir of dirs) {
    const file = path.resolve(dir, name);
    try {
      accessSync(file, constants.X_OK);
      if (statSync(file).isFile()) return file;
    } catch (error) {
      if (!['ENOENT', 'EACCES', 'ENOTDIR'].includes(error.code)) throw error;
    }
  }
  throw new Error(`${name} 명령을 PATH와 ~/.local/bin에서 찾지 못했다.`);
}

export const mfluxEnv = (base = process.env) => ({ ...base, HF_HUB_OFFLINE: '1' });

export function zimageArgs({ model, promptFile, size: [w, h], seed, output, lowRam }) {
  return ['--model', model, '--base-model', 'z-image-turbo', '--prompt-file', promptFile,
    '--width', String(w), '--height', String(h), '--steps', '9', '--seed', String(seed), '--output', output,
    ...(lowRam ? ['--low-ram'] : [])];
}

export function flux2EditArgs({ model, image, promptFile, size: [w, h], seed, output, lowRam }) {
  return ['--model', model, '--base-model', 'flux2-klein-4b', '--image-paths', image, '--prompt-file', promptFile,
    '--width', String(w), '--height', String(h), '--seed', String(seed), '--output', output,
    ...(lowRam ? ['--low-ram'] : [])];
}

export function mfluxBusy() {
  return spawnSync('pgrep', ['-f', 'mflux-generate'], { stdio: 'ignore' }).status === 0;
}

export async function runMflux(bin, args, log) {
  if (mfluxBusy()) throw Object.assign(new Error('다른 mflux 작업이 돌고 있다.'), { code: 'MFLUX_BUSY' });
  await new Promise((resolve, reject) => {
    const fd = openSync(log, 'a');
    let child;
    try {
      child = spawn(bin, args, { env: mfluxEnv(), detached: true, stdio: ['ignore', fd, fd] });
    } finally {
      closeSync(fd);
    }
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch (error) {
        if (error.code !== 'ESRCH') child.kill('SIGKILL');
      }
    }, 30 * 60 * 1000);
    child.on('error', reject);
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      if (timedOut) reject(new Error('mflux가 30분 시간 제한을 넘었다.'));
      else if (code !== 0) reject(new Error(`mflux 종료 상태 ${signal ?? code}.`));
      else resolve();
    });
  });
}

export function upscale(src, out, [w, h]) {
  const dir = mkdtempSync(path.join(path.dirname(out), '.resize-'));
  try {
    const resized = path.join(dir, 'image.png');
    execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-i', src, '-vf', `scale=${w}:${h}:flags=lanczos`,
      '-frames:v', '1', '-pix_fmt', 'rgb24', '-update', '1', resized], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    renameSync(resized, out);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
