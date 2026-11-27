import { readFileSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { NARR, selectVoices, speechLines, validateStory } from './lib.mjs';
import { loadEnv, vstudioBase } from './tts.mjs';

const AGE = /_(child|young|adult|old)$/;
const ALLOWED = new Set(['provider', 'model', 'id', 'seed', 'name', 'style', 'speed', 'lang', 'base']);
const nonempty = value => typeof value === 'string' && value.trim().length > 0;
const isVoice = voice => voice && typeof voice === 'object' && !Array.isArray(voice);
const hasVoice = (voices, who) => !!voices && Object.hasOwn(voices, who) && isVoice(voices[who]);
const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
const cell = value => String(value ?? '—').replaceAll('|', '\\|').replace(/\s+/gu, ' ').trim();

export function isDraft(voices, provider) {
  const entries = Object.values(voices ?? {});
  return provider === 'say' || (entries.length > 0 && entries.every(voice => voice?.provider === 'say'));
}

export function castLineErrors(voices, lines, baseVoices) {
  const errors = new Map();
  for (const { who } of lines) {
    if (!hasVoice(voices, who)) errors.set(who, `${who}: 배정표에 목소리가 없다.`);
  }
  for (const [who, voice] of Object.entries(voices ?? {})) {
    if (AGE.test(who) && isVoice(voice) && !nonempty(voice.base ?? baseVoices?.[who]?.base)) {
      errors.set(who, `${who}: 나이 배역에는 base가 필요하다.`);
    }
  }
  return errors;
}

export function validateCast(voices, lines, { profiles, draft = false, baseVoices } = {}) {
  const entries = Object.entries(voices ?? {});
  const roleErrors = castLineErrors(voices, lines, baseVoices);
  const errors = [...roleErrors.values()];
  const warnings = [];
  if (entries.filter(([who]) => NARR.test(who)).length > 2) errors.push('해설 목소리는 2개까지 배정할 수 있다.');
  if (draft) return { errors, warnings };

  const hasProfiles = Array.isArray(profiles);
  if (!hasProfiles) errors.push('프로필 목록 없음');
  const profileById = new Map((hasProfiles ? profiles : []).map(profile => [profile.id, profile]));
  const models = new Set();
  const owners = new Map();
  for (const [who, voice] of entries) {
    if (!isVoice(voice)) {
      if (!roleErrors.has(who)) errors.push(`${who}: 배정표 항목의 형식이 잘못됐다.`);
      continue;
    }
    if (voice.provider !== 'vstudio') errors.push(`${who}: 최종본의 provider는 vstudio여야 한다.`);
    if (nonempty(voice.model)) models.add(voice.model);
    else errors.push(`${who}: model을 명시해야 한다.`);
    if (!Number.isInteger(voice.seed)) errors.push(`${who}: seed는 정수여야 한다.`);
    const extra = Object.keys(voice).filter(key => !ALLOWED.has(key));
    if (extra.length) errors.push(`${who}: 허용하지 않는 칸 (${extra.join(', ')}).`);
    if (!nonempty(voice.id)) {
      errors.push(`${who}: 복제 프로필 id가 필요하다.`);
      continue;
    }
    const base = voice.base ?? baseVoices?.[who]?.base;
    const role = nonempty(base) ? base : who;
    const owner = owners.get(voice.id);
    if (!owner) owners.set(voice.id, { who, role });
    else if (owner.role !== role) errors.push(`${owner.who}·${who}: 서로 다른 배역에 프로필 ${voice.id}가 같다.`);
    if (hasProfiles && profileById.get(voice.id)?.kind !== 'clone') {
      errors.push(`${who}: 프로필 ${voice.id}가 없거나 clone이 아니다.`);
    }
  }
  if (models.size > 1) errors.push(`최종본에서 엔진을 섞을 수 없다 (${[...models].join(', ')}).`);
  return { errors, warnings };
}

export async function fetchProfiles() {
  const base = vstudioBase();
  let response;
  try {
    response = await fetch(`${base}/profiles`);
  } catch (cause) {
    throw new Error('VoiceStudio에 연결할 수 없다. VoiceStudio를 켜 주세요.', { cause });
  }
  if (!response.ok) throw new Error(`VoiceStudio 프로필 조회 실패 (HTTP ${response.status}).`);
  const profiles = await response.json();
  if (!Array.isArray(profiles)) throw new Error('VoiceStudio 프로필 목록 형식이 잘못됐다.');
  return profiles;
}

export async function assertCast(voices, lines, { draft = false, baseVoices } = {}) {
  if (draft) console.log('임시판 · say 목소리를 쓴다.');
  const profiles = draft ? null : await fetchProfiles();
  const { errors, warnings } = validateCast(voices, lines, { profiles, draft, baseVoices });
  for (const warning of warnings) console.warn(warning);
  if (errors.length) throw new Error(`목소리 배정 오류\n- ${errors.join('\n- ')}`);
  return profiles;
}

export function castReport(voices, lines, profiles, baseVoices) {
  const names = new Map((profiles ?? []).map(({ id, name }) => [id, name]));
  const counts = new Map();
  for (const { who } of lines) counts.set(who, (counts.get(who) ?? 0) + 1);
  return [
    '# 목소리 배정', '',
    '| 배역 | base | 엔진 | 프로필 id | 이름 | seed | style | 문장 수 |',
    '| --- | --- | --- | --- | --- | ---: | --- | ---: |',
    ...Object.entries(voices).map(([who, voice]) => `| ${[
      who, voice?.base ?? baseVoices?.[who]?.base, voice?.model ?? voice?.provider, voice?.id,
      names.get(voice?.id) ?? voice?.name,
      voice?.seed, voice?.style, counts.get(who) ?? 0,
    ].map(cell).join(' | ')} |`),
    '',
  ].join('\n');
}

const validPitch = pitch => Number.isFinite(pitch) && pitch > 0;

export function pitchAverages(samples) {
  const totals = new Map();
  for (const { who, pitch } of samples) {
    if (!validPitch(pitch)) continue;
    const group = totals.get(who) ?? { count: 0, sum: 0 };
    group.count++;
    group.sum += Math.log2(pitch);
    totals.set(who, group);
  }
  return new Map([...totals].map(([who, { count, sum }]) => [who, { count, pitch: 2 ** (sum / count) }]));
}

export function pitchOutliers(samples, limit = 3) {
  const averages = pitchAverages(samples);
  return samples.filter(({ who, pitch }) => {
    const mean = averages.get(who);
    if (!validPitch(pitch) || !mean || mean.count < 2) return false;
    return Math.abs(12 * Math.log2(pitch / mean.pitch)) >= limit - 1e-12;
  });
}

function profileTables(profiles, pool) {
  const clones = profiles.filter(profile => profile.kind === 'clone');
  const ids = new Set(clones.map(profile => profile.id));
  const rows = [
    '## 복제 프로필 후보', '',
    '| 프로필 id | 이름 | personality |',
    '| --- | --- | --- |',
    ...clones.map(({ id, name, personality }) => `| ${[id, name, personality?.slice(0, 80)].map(cell).join(' | ')} |`),
  ];
  if (pool) {
    if (!pool.roles || typeof pool.roles !== 'object') throw new Error('배역표 형식이 잘못됐다. { "roles": { "<역할>": [{ "id": "…" }] } } 꼴이어야 한다.');
    rows.push('', '## 배역표 후보', '',
      '| 배역 | 프로필 id | 이름 | 엔진 | 메모 | clone 확인 |',
      '| --- | --- | --- | --- | --- | --- |');
    for (const [role, candidates] of Object.entries(pool.roles)) {
      for (const { id, name, model, memo } of candidates) {
        rows.push(`| ${[role, id, name, model, memo, ids.has(id) ? '있음' : '없음'].map(cell).join(' | ')} |`);
      }
    }
  }
  return rows.join('\n');
}

async function main() {
  const { positionals: [ep, ...extra], values } = parseArgs({
    options: { pool: { type: 'string' } }, allowPositionals: true,
  });
  if (!ep || extra.length) throw new Error('쓰는 법 · node cast.mjs <에피소드> [--pool <배역표.json>]');
  loadEnv(process.env.STORY_VIDEO_VOICE_ENV ?? '~/.config/story-video/voice.env');
  const story = validateStory(readJson(path.join(ep, 'story.json')));
  const lines = speechLines(story);
  const voices = selectVoices(story, lines);
  if (isDraft(voices)) {
    await assertCast(voices, lines, { draft: true });
    console.log('목소리 배정 검사 통과');
    return;
  }
  const profiles = await fetchProfiles();
  const poolFile = values.pool ?? process.env.STORY_VIDEO_CAST_POOL;
  const pool = poolFile ? readJson(poolFile) : null;
  console.log(profileTables(profiles, pool));
  const { errors, warnings } = validateCast(voices, lines, { profiles });
  for (const warning of warnings) console.warn(warning);
  if (errors.length) throw new Error(`목소리 배정 오류\n- ${errors.join('\n- ')}`);
  console.log('목소리 배정 검사 통과');
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
