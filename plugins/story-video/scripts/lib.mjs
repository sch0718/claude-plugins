export const FPS = 30;
const GAP = 0.35;
const lead = scene => scene.lead ?? 0.6;
const tail = scene => scene.tail ?? 0.8;
const chars = text => Array.from(text.replace(/\s/gu, '')).length;

export const estimateSeconds = text => chars(text) * 0.133;

export function storyScenes(story) {
  if (!story.recap?.trim()) return story.scenes;
  return [
    { id: 'recap', art: story.scenes[0].id, lines: [{ who: 'narr', t: story.recap }] },
    ...story.scenes,
  ];
}

export function speechLines(story) {
  return storyScenes(story).flatMap(scene => scene.lines.map(line => ({ who: line.who, t: line.say ?? line.t })));
}

export function validateStory(story) {
  if (!Array.isArray(story?.scenes) || !story.scenes.length) throw new Error('장면을 한 개 이상 넣어야 한다.');
  if (story.recap != null && typeof story.recap !== 'string') throw new Error('지난 이야기는 글이어야 한다.');
  const ids = new Set(['recap']);
  const validId = id => typeof id === 'string' && /^[\w-]+$/.test(id);
  for (const scene of story.scenes) {
    if (!validId(scene?.id) || ids.has(scene.id.toLowerCase())) throw new Error('장면 id가 잘못됐거나 겹친다.');
    ids.add(scene.id.toLowerCase());
    if ([lead(scene), tail(scene)].some(value => !Number.isFinite(value) || value < 0)) {
      throw new Error(`${scene.id}의 쉼 길이는 0 이상의 숫자여야 한다.`);
    }
    if (!Array.isArray(scene.lines) || scene.lines.some(line => !validId(line?.who)
        || typeof line.t !== 'string' || typeof (line.say ?? line.t) !== 'string')) {
      throw new Error(`${scene.id}의 문장 형식이 잘못됐다.`);
    }
  }
  return story;
}

export function estimateStory(story) {
  const scenes = storyScenes(story);
  const lines = speechLines(story);
  const count = lines.reduce((sum, line) => sum + chars(line.t), 0);
  const pauses = scenes.reduce((sum, scene) =>
    sum + lead(scene) + tail(scene) + GAP * Math.max(0, scene.lines.length - 1), 0);
  return { chars: count, seconds: count * 0.133 + pauses, scenes: scenes.length, lines: lines.length };
}

export function buildTimeline(story, durs, { files } = {}) {
  let seconds = 0;
  let endFrame = 0;
  // 반 프레임의 부동소수점 오차를 보정한다.
  const frame = () => Math.round(seconds * FPS + 1e-9);
  const scenes = storyScenes(story).map((scene, i) => {
    const startFrame = endFrame;
    seconds += lead(scene);
    const lines = scene.lines.map(({ who, t }, j) => {
      if (j > 0) seconds += GAP;
      const start = frame() / FPS;
      const dur = durs[i]?.[j];
      if (!Number.isFinite(dur) || dur < 0) throw new Error(`${scene.id}의 ${j + 1}번 음성 길이가 잘못됐다.`);
      seconds += dur;
      return { who, t, start, end: frame() / FPS, file: files?.[i]?.[j] ?? null };
    });
    seconds += tail(scene);
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
