import { existsSync } from 'node:fs';
import path from 'node:path';

export const FPS = 30;
export const FORMATS = {
  wide: { w: 1920, h: 1080, art: [1536, 1024], phone: [960, 540],
    subs: { size: 52, minSize: 44, maxChars: 28, maxWidth: 1600, preferredLines: 2, maxLines: 3, bottom: 1020 } },
  shorts: { w: 1080, h: 1920, art: [1024, 1536], phone: [540, 960],
    subs: { size: 64, minSize: 64, maxChars: 14, maxWidth: 900, preferredLines: 3, maxLines: 3, bottom: 1440 } },
};
export const PACES = {
  normal: { gap: 0.35, lead: 0.6, tail: 0.8, speed: 1, secPerChar: 0.133 },
  fast: { gap: 0.12, lead: 0.2, tail: 0.3, speed: 1.25, secPerChar: 0.112 },
};
const chars = text => Array.from(text.replace(/\s/gu, '')).length;
export const NARR = /^narr(_[\w-]+)?$/;

export const estimateSeconds = (text, pace = 'normal') => chars(text) * PACES[pace].secPerChar;

export function storyFormat(story, override) {
  const format = override ?? (story.format === undefined ? 'wide' : story.format);
  if (typeof format !== 'string' || !Object.hasOwn(FORMATS, format)) {
    throw new Error('format은 wide 또는 shorts여야 한다.');
  }
  return format;
}

export function storyPace(story) {
  const pace = story.pace === undefined ? (story.format === 'shorts' ? 'fast' : 'normal') : story.pace;
  if (typeof pace !== 'string' || !Object.hasOwn(PACES, pace)) {
    throw new Error('pace는 normal 또는 fast여야 한다.');
  }
  return pace;
}

export function artPath(ep, format, id) {
  const root = path.join(ep, 'art');
  const dir = path.join(root, format);
  const legacy = path.join(root, `${id}.png`);
  if (format === 'wide' && !existsSync(dir) && existsSync(legacy)) return legacy;
  return path.join(dir, `${id}.png`);
}

export function storyScenes(story) {
  const recap = typeof story.recap === 'string' ? { t: story.recap } : story.recap;
  if (!recap?.t.trim()) return story.scenes;
  return [
    { id: 'recap', art: story.scenes[0].id, lines: [{ ...recap, who: recap.who ?? 'narr' }] },
    ...story.scenes,
  ];
}

export function speechLines(story) {
  return storyScenes(story).flatMap(scene => scene.lines.map(line => ({ ...line, t: line.say ?? line.t })));
}

const validLine = line => typeof line?.t === 'string'
  && (line.say == null || typeof line.say === 'string')
  && (line.direction === undefined || typeof line.direction === 'string')
  && (line.direction_en === undefined || typeof line.direction_en === 'string')
  && (line.take === undefined || (Number.isSafeInteger(line.take) && line.take >= 0));

export function validateStory(story) {
  if (!Array.isArray(story?.scenes) || !story.scenes.length) throw new Error('장면을 한 개 이상 넣어야 한다.');
  storyFormat(story);
  const { lead, tail } = PACES[storyPace(story)];
  if (story.recap != null && typeof story.recap !== 'string' && !validLine(story.recap)) {
    throw new Error('지난 이야기의 문장 형식이 잘못됐다.');
  }
  if (typeof story.recap === 'object' && story.recap?.who !== undefined
      && (typeof story.recap.who !== 'string' || !NARR.test(story.recap.who))) {
    throw new Error('지난 이야기의 who는 해설 키(narr 또는 narr_<이름>)여야 한다.');
  }
  const ids = new Set(['recap']);
  const validId = id => typeof id === 'string' && /^[\w-]+$/.test(id);
  for (const scene of story.scenes) {
    if (!validId(scene?.id) || ids.has(scene.id.toLowerCase())) throw new Error('장면 id가 잘못됐거나 겹친다.');
    ids.add(scene.id.toLowerCase());
    if ([scene.lead ?? lead, scene.tail ?? tail].some(value => !Number.isFinite(value) || value < 0)) {
      throw new Error(`${scene.id}의 쉼 길이는 0 이상의 숫자여야 한다.`);
    }
    if (!Array.isArray(scene.lines) || scene.lines.some(line => !validId(line?.who) || !validLine(line))) {
      throw new Error(`${scene.id}의 문장 형식이 잘못됐다.`);
    }
  }
  return story;
}

export function selectVoices(story, lines, { provider, voices } = {}) {
  const selected = provider === 'say' ? Object.fromEntries(lines.map(({ who }) => {
    let hash = 0;
    for (const char of who) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
    return [who, { provider: 'say', id: 'Yuna', rate: who === 'narr' ? 170 : 150 + hash % 41 }];
  })) : voices ?? story.voices;
  const result = Object.assign(Object.create(null), selected);
  const { speed } = PACES[storyPace(story)];
  if (speed === 1) return result;
  for (const [who, voice] of Object.entries(result)) {
    if (voice?.speed !== undefined) continue;
    if (voice?.provider === 'vstudio') result[who] = { ...voice, speed };
    if (voice?.provider === 'say') result[who] = { ...voice, rate: Math.round((voice.rate ?? 175) * speed), speed };
  }
  return result;
}

export function directionParams(text = '') {
  const whisper = /속삭|\bwhisper\w*\b/iu.test(text);
  const pitch = /높은\s*목소리|높게|\bhigh[- ]pitch/iu.test(text) ? 'high'
    : /낮은\s*목소리|낮게|\blow[- ]pitch|\bdeep\b/iu.test(text) ? 'low' : null;
  const speed = /아주\s*(천천히|느리)|\bvery\s+slow/iu.test(text) ? 0.85
    : /천천히|느리|\bslow\w*\b/iu.test(text) ? 0.92
      : /빠르|다급|급하|\b(?:fast|rushed|urgent)\w*\b/iu.test(text) ? 1.1 : 1;
  const volume = /외치|소리치|크게|\b(?:shout|loud)\w*\b/iu.test(text) ? 'loud'
    : /조용|작게|\b(?:quiet|soft)\w*\b/iu.test(text) ? 'soft' : null;
  return { whisper, pitch, speed, volume };
}

export function readable(text) {
  const digits = '영일이삼사오육칠팔구';
  const units = ['천', '백', '십', ''];
  return text.replace(/[0-9]+/g, raw => {
    const number = Number(raw);
    if (number > 9999) return raw;
    if (number === 0) return digits[0];
    return [...String(number).padStart(4, '0')].map((digit, i) => {
      if (digit === '0') return '';
      return (digit === '1' && units[i] ? '' : digits[digit]) + units[i];
    }).join('');
  });
}

export const normalizeSpeech = text => readable(text.normalize('NFC')).toLowerCase().replace(/[\p{P}\s]/gu, '');

// 고유어 수 읽기(1~99 + 살·시·명 같은 단위). 음성 인식은 「열아홉 살」을 「19살」로 적기도 한다
export function nativeReadable(text) {
  const ones = ['', '한', '두', '세', '네', '다섯', '여섯', '일곱', '여덟', '아홉'];
  const tens = ['', '열', '스물', '서른', '마흔', '쉰', '예순', '일흔', '여든', '아흔'];
  return text.replace(/([0-9]{1,2})(\s*)(살|시|명|개|번|마리|사람|잔|달|가지|권|채|대|척|송이|그루|군데|시간)/gu,
    (raw, number, space, unit) => {
      const n = Number(number);
      if (n < 1) return raw;
      return (n === 20 ? '스무' : tens[Math.floor(n / 10)] + ones[n % 10]) + space + unit;
    });
}

// 인식 글은 숫자를 한자어·고유어 어느 쪽으로도 읽을 수 있으니 둘 중 더 맞는 쪽 점수를 쓴다
export const speechScore = (reference, recognized) => Math.max(
  similarity(normalizeSpeech(reference), normalizeSpeech(recognized)),
  similarity(normalizeSpeech(reference), normalizeSpeech(nativeReadable(recognized))),
);

export function similarity(a, b) {
  const left = Array.from(a);
  const right = Array.from(b);
  if (!left.length && !right.length) return 1;
  let prev = Array.from({ length: right.length + 1 }, (_, i) => i);
  for (const [i, char] of left.entries()) {
    const row = [i + 1];
    for (const [j, other] of right.entries()) {
      row.push(Math.min(prev[j + 1] + 1, row[j] + 1, prev[j] + Number(char !== other)));
    }
    prev = row;
  }
  return 1 - prev[right.length] / Math.max(left.length, right.length);
}

export function estimateStory(story) {
  const { gap, lead, tail, secPerChar } = PACES[storyPace(story)];
  const scenes = storyScenes(story);
  const lines = speechLines(story);
  const count = lines.reduce((sum, line) => sum + chars(line.t), 0);
  const pauses = scenes.reduce((sum, scene) =>
    sum + (scene.lead ?? lead) + (scene.tail ?? tail) + gap * Math.max(0, scene.lines.length - 1), 0);
  return { chars: count, seconds: count * secPerChar + pauses, scenes: scenes.length, lines: lines.length };
}

export function buildTimeline(story, durs, { files } = {}) {
  const { gap, lead, tail } = PACES[storyPace(story)];
  let seconds = 0;
  let endFrame = 0;
  // 반 프레임의 부동소수점 오차를 보정한다.
  const frame = () => Math.round(seconds * FPS + 1e-9);
  const scenes = storyScenes(story).map((scene, i) => {
    const startFrame = endFrame;
    seconds += scene.lead ?? lead;
    const lines = scene.lines.map(({ who, t }, j) => {
      if (j > 0) seconds += gap;
      const start = frame() / FPS;
      const dur = durs[i]?.[j];
      if (!Number.isFinite(dur) || dur < 0) throw new Error(`${scene.id}의 ${j + 1}번 음성 길이가 잘못됐다.`);
      seconds += dur;
      return { who, t, start, end: frame() / FPS, file: files?.[i]?.[j] ?? null };
    });
    seconds += scene.tail ?? tail;
    endFrame = frame();
    return {
      id: scene.id, start: startFrame / FPS, end: endFrame / FPS, dur: (endFrame - startFrame) / FPS,
      kb: scene.kb ?? 'in', art: scene.id === 'recap' ? scene.art : scene.id, lines,
    };
  });
  return { fps: FPS, total: endFrame / FPS, scenes };
}

function srtTime(seconds) {
  const ms = Math.round(seconds * 1000);
  const pad = (n, len = 2) => String(n).padStart(len, '0');
  return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)},${pad(ms % 1000, 3)}`;
}

export function toSrt(timeline) {
  return timeline.scenes.flatMap(scene => scene.lines).map(({ start, end, t }, i) =>
    `${i + 1}\n${srtTime(start)} --> ${srtTime(end)}\n${t}\n`).join('\n');
}

export function wrapLines(text, maxChars = 28) {
  if (!Number.isInteger(maxChars) || maxChars < 1) throw new Error('줄 너비는 양의 정수여야 한다.');
  const lines = [];
  let line = '';
  for (const word of text.trim().split(/\s+/u).filter(Boolean)) {
    const letters = Array.from(word);
    if (line && Array.from(line).length + 1 + letters.length > maxChars) {
      lines.push(line);
      line = '';
    }
    while (letters.length > maxChars) lines.push(letters.splice(0, maxChars).join(''));
    const rest = letters.join('');
    line = line ? `${line} ${rest}` : rest;
  }
  if (line) lines.push(line);
  return lines;
}
