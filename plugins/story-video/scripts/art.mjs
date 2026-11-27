import { execFileSync, spawn } from 'node:child_process';
import { appendFileSync, closeSync, existsSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { FORMATS, storyFormat, validateStory } from './lib.mjs';

const run = (command, args) => execFileSync(command, args, { stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8' });

function prompt(story, scene, format) {
  const [w, h] = FORMATS[format].art;
  const portrait = format === 'shorts';
  const composition = portrait
    ? 'Compose for a portrait 9:16 crop. Keep the important people and objects near the center or upper center, inside the crop.\n\n' : '';
  const cast = (scene.cast ?? []).map(id => {
    const character = (story.characters ?? []).find(person => person.id === id);
    if (!character) throw new Error(`${scene.id}에 없는 인물 id가 있다(${id}).`);
    return `- ${character.name}: ${character.look}`;
  });
  const characters = cast.length
    ? `CHARACTERS (keep each one looking exactly like this in every scene):\n${cast.join('\n')}\n\n` : '';
  return `Generate ONE new image with your image generation tool and save it as ${scene.id}.png in the current directory (PNG, ${portrait ? 'portrait' : 'landscape'}, exactly ${w} x ${h}, opaque). Do not create or edit any other file.

This is one scene illustration for an original narrated short story video. Do not imitate any existing artist, comic, animation, game or film, and do not depict any real person. ABSOLUTELY NO text, letters, numbers, signs with writing, speech bubbles, captions, logos, signatures or watermarks anywhere in the image: subtitles are added later.

${composition}${characters}SCENE: ${scene.art}

STYLE: ${story.style}

If the image is not exactly ${w}x${h}, resize it before saving.
`;
}

// codex가 PATH에 없으면(셸 별칭만 있을 때) ChatGPT 앱에 든 CLI를 쓴다. CODEX_BIN으로 지정할 수도 있다
function codexBin() {
  if (process.env.CODEX_BIN) return process.env.CODEX_BIN;
  for (const dir of (process.env.PATH ?? '').split(path.delimiter)) {
    if (dir && existsSync(path.join(dir, 'codex'))) return 'codex';
  }
  const app = '/Applications/ChatGPT.app/Contents/Resources/codex-cli';
  for (const file of [`${app}/CodexCLI.app/Contents/MacOS/codex`, `${app}/bin/codex`]) if (existsSync(file)) return file;
  throw new Error('codex 명령을 찾지 못했다. CODEX_BIN에 경로를 준다.');
}

function generate(model, work, request, log) {
  return new Promise((resolve, reject) => {
    const fd = openSync(log, 'a');
    const child = spawn(codexBin(), ['exec', '-m', model, '-s', 'workspace-write',
      '--skip-git-repo-check', '--ephemeral', '-C', work, request],
    { cwd: work, detached: true, stdio: ['ignore', fd, fd] });
    closeSync(fd);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      if (child.pid) {
        try {
          process.kill(-child.pid, 'SIGKILL');
        } catch (error) {
          if (error.code !== 'ESRCH') child.kill('SIGKILL');
        }
      }
    }, 10 * 60 * 1000);
    child.on('error', reject);
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      if (timedOut) reject(new Error('10분 시간 제한을 넘었다.'));
      else if (code !== 0) reject(new Error(`Codex 종료 상태 ${signal ?? code}.`));
      else resolve();
    });
  });
}

function placeImage(work, id, out, [w, h]) {
  const expected = path.join(work, `${id}.png`);
  const other = readdirSync(work, { withFileTypes: true }).find(item => item.isFile() && /\.png$/i.test(item.name));
  const image = existsSync(expected) ? expected : other && path.join(work, other.name);
  if (!image) throw new Error('생성된 PNG가 없다.');
  const size = run('ffprobe', ['-v', 'error', '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', image]).trim();
  if (size === `${w}x${h}`) {
    renameSync(image, out);
  } else {
    const resized = path.join(work, '.resized.png');
    run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-i', image, '-vf',
      `scale=${w}:${h}:force_original_aspect_ratio=increase,crop=${w}:${h}`,
      '-frames:v', '1', '-pix_fmt', 'rgb24', '-update', '1', resized]);
    renameSync(resized, out);
  }
}

async function makeArt({ scene, request }, dir, { force, model, retries, size }) {
  const id = scene.id;
  const out = path.join(dir, `${id}.png`);
  if (!force && existsSync(out)) {
    console.log(`${id} 기존 그림을 쓴다.`);
    return true;
  }

  const work = path.join(dir, '.work', id);
  const log = path.join(dir, 'logs', `${id}.log`);
  writeFileSync(log, '');
  const start = performance.now();
  let failure;
  for (let attempt = 0; attempt <= retries; attempt++) {
    rmSync(work, { recursive: true, force: true });
    mkdirSync(work, { recursive: true });
    try {
      appendFileSync(log, `\n${attempt + 1}번째 시도\n`);
      await generate(model, work, request, log);
      placeImage(work, id, out, size);
      failure = undefined;
      break;
    } catch (error) {
      failure = error.stderr?.trim() || error.message;
      appendFileSync(log, `\n${failure}\n`);
    } finally {
      rmSync(work, { recursive: true, force: true });
    }
  }

  const status = failure ? `${id} 실패(${failure.split('\n')[0]})`
    : `${id} ok ${Math.round((performance.now() - start) / 1000)}초`;
  appendFileSync(log, `\n${status}\n`);
  console.log(status);
  return !failure;
}

function contactSheet(story, format, dir) {
  const images = story.scenes.map(scene => path.join(dir, `${scene.id}.png`)).filter(existsSync);
  if (!images.length) {
    console.log('모아볼 그림이 없다.');
    return;
  }
  const portrait = format === 'shorts';
  const columns = portrait ? 4 : 3;
  const width = portrait ? 256 : 512;
  const height = portrait ? 384 : 341;
  const filters = images.map((_, i) => `[${i}:v]scale=${width}:${height},format=yuvj444p,setsar=1[v${i}]`);
  let input = '[v0]';
  if (images.length > 1) {
    const positions = images.map((_, i) => `${i % columns * width}_${Math.floor(i / columns) * height}`);
    const labels = images.map((_, i) => `[v${i}]`).join('');
    filters.push(`${labels}xstack=inputs=${images.length}:layout=${positions.join('|')}:fill=black[grid]`);
    input = '[grid]';
  }
  filters.push(`${input}pad=${columns * width}:${Math.ceil(images.length / columns) * height}[sheet]`);
  const file = path.join(dir, 'contact.jpg');
  run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-filter_complex_threads', '2',
    ...images.flatMap(image => ['-i', image]), '-filter_complex', filters.join(';'), '-map', '[sheet]',
    '-frames:v', '1', '-q:v', '3', '-pix_fmt', 'yuvj444p', '-update', '1', file]);
  console.log(`그림 모아보기 · ${path.relative(process.cwd(), file)}`);
}

async function main() {
  const { positionals: [episode, ...extra], values } = parseArgs({
    options: {
      only: { type: 'string' }, jobs: { type: 'string', default: '3' }, retries: { type: 'string', default: '2' },
      force: { type: 'boolean' }, model: { type: 'string', default: 'gpt-6.2-sol' },
      'dry-run': { type: 'boolean' }, 'sheet-only': { type: 'boolean' }, format: { type: 'string' },
    },
    allowPositionals: true,
  });
  const jobs = Number(values.jobs);
  const retries = Number(values.retries);
  if (!episode || extra.length || ![1, 2, 3].includes(jobs) || !Number.isSafeInteger(retries) || retries < 0
      || (values['dry-run'] && values['sheet-only'])) {
    throw new Error('쓰는 법 · node art.mjs <에피소드> [--format wide|shorts] [--only s01,s03] [--jobs 1|2|3] [--retries 2] [--force] [--model 이름] [--dry-run|--sheet-only]');
  }
  const ep = path.resolve(episode);
  const story = validateStory(JSON.parse(readFileSync(path.join(ep, 'story.json'), 'utf8')));
  const format = storyFormat(story, values.format);
  const dir = path.join(ep, 'art', format);
  // 0.1.0 작업 폴더(art/<id>.png)의 그림은 처음 art/wide/를 만들 때 그리로 옮긴다
  const legacy = story.scenes.map(scene => path.join(ep, 'art', `${scene.id}.png`)).filter(file => existsSync(file));
  if (format === 'wide' && !existsSync(dir) && legacy.length) {
    mkdirSync(dir, { recursive: true });
    for (const file of legacy) renameSync(file, path.join(dir, path.basename(file)));
    console.log(`0.1.0 그림 ${legacy.length}장을 art/wide/로 옮겼다.`);
  }
  if (values['sheet-only']) {
    contactSheet(story, format, dir);
    return;
  }

  const only = values.only?.split(',').map(id => id.trim());
  if (only?.some(id => !story.scenes.some(scene => scene.id === id))) throw new Error('--only에 없는 장면 id가 있다.');
  const items = story.scenes.filter(scene => !only || only.includes(scene.id))
    .map(scene => ({ scene, request: prompt(story, scene, format) }));
  const prompts = path.join(dir, 'prompts');
  mkdirSync(prompts, { recursive: true });
  for (const { scene, request } of items) writeFileSync(path.join(prompts, `${scene.id}.txt`), request);
  console.log(`그림 요청문 ${items.length}개를 저장했다. ${path.relative(process.cwd(), prompts)}`);
  if (values['dry-run']) return;

  mkdirSync(path.join(dir, 'logs'), { recursive: true });
  const failed = [];
  let next = 0;
  await Promise.all(Array.from({ length: jobs }, async () => {
    while (next < items.length) {
      const item = items[next++];
      if (!await makeArt(item, dir, { ...values, retries, size: FORMATS[format].art })) failed.push(item.scene.id);
    }
  }));
  if (failed.length) {
    console.error(`그림 생성 실패 · ${failed.sort().join(', ')}`);
    process.exitCode = 1;
  }
  contactSheet(story, format, dir);
}

main().catch(error => {
  console.error(error.stderr?.trim() || error.message);
  process.exitCode = 1;
});
