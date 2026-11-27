#!/usr/bin/env node
// 문장 → 음성 wav. 공급자 5개(say·edge·gemini·eleven·vstudio)를 한 함수로 부른다. Node 22 이상, 외부 라이브러리 없음(ffmpeg·ffprobe 필요, edge는 uvx).
//
// 명령으로:
//   node tts.mjs <lines.json> <voices.json> <출력 폴더> [--dry-run] [--force] [--env <키 파일>]
//     lines.json   [{ "who": "narr", "t": "문장", "direction": "천천히", "take": 1 }, ...]
//     voices.json  { "narr": { "provider": "eleven", "id": "<음성 ID>" }, ... }  (who가 없으면 narr를 쓴다)
//     결과         <출력 폴더>/<who>-<해시>.wav (48kHz 모노, 앞뒤 무음 자름) + durations.json
//     --dry-run    부르지 않고, 새로 받을 글자 수·요청 수와 무료 한도만 본다
//     --force      무료 한도를 넘어도 부른다
// 모듈로(프로젝트의 시간표 스크립트에서):
//   import { speak, loadEnv } from '<이 파일>';  loadEnv('~/.config/<프로젝트>/voice.env');
//   const { file, dur, dropped } = await speak(voices.lian, '문장', 출력폴더, 'lian', { direction, direction_en, take });
//   direction은 연기 지시, direction_en은 영어 지시, take는 다시 만드는 번호다(기본 0).
//
// 키: 환경 변수 GEMINI_API_KEY·ELEVENLABS_API_KEY, 없으면 --env 파일. 이 스크립트는 키를 출력하지 않는다.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { directionParams } from './lib.mjs';

// 무료 한도(2026-11-12 확인, Gemini는 모델마다 따로). 넘으면 --force 없이는 부르지 않는다
export const FREE = { eleven: { chars: 10000, per: '월' }, gemini: { requests: 10, per: '하루' } };

export function loadEnv(file) {
  file = file?.replace(/^~(?=\/)/, os.homedir());
  if (!file || !fs.existsSync(file)) return;
  for (const l of fs.readFileSync(file, 'utf8').split('\n')) {
    const m = l.match(/^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*"?([^"\n]*)"?\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim();
  }
}

const sh = (cmd, args) => execFileSync(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
const sleep = ms => new Promise(r => setTimeout(r, ms));
export const dur = f => +sh('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', f]).toString().trim();
// 말줄임표는 쉼표처럼 읽힌다(공급자마다 다르게 읽어서 맞춘다)
export const clean = t => t.replace(/…/g, ', ').replace(/\s+/g, ' ').trim();

function voiceDirection(voice, { direction, direction_en, take = 0 } = {}) {
  const instruction = direction_en || direction || '';
  const { whisper, pitch, speed, volume } = directionParams(`${direction ?? ''} ${direction_en ?? ''}`);
  const freeform = voice.provider === 'gemini' || (voice.provider === 'vstudio' && voice.instruct === 'freeform');
  const dropped = instruction.trim() && !freeform ? ['감정·말투'] : [];
  const changes = {};
  if (instruction.trim() && freeform) changes.style = [voice.style, instruction].filter(Boolean).join(', ');

  if (voice.provider === 'vstudio') {
    if (!freeform && (whisper || pitch)) {
      let tags = (voice.style ?? '').split(',').map(tag => tag.trim()).filter(Boolean);
      const original = tags.join(', ');
      if (pitch) {
        const isPitch = tag => /^(low|high) pitch$/i.test(tag);
        const existing = tags.filter(isPitch);
        if (existing.length !== 1 || existing[0].toLowerCase() !== `${pitch} pitch`) {
          tags = [...tags.filter(tag => !isPitch(tag)), `${pitch} pitch`];
        }
      }
      if (whisper && !tags.some(tag => tag.toLowerCase() === 'whisper')) tags.push('whisper');
      if (tags.join(', ') !== original) changes.style = tags.join(', ');
    }
    if (speed !== 1 || take > 0) changes.speed = (voice.speed || 1) * speed * 0.96 ** take;
    if (volume && !freeform) dropped.push(volume);
  } else if (voice.provider === 'edge') {
    if (speed !== 1) {
      const rate = (Number.parseFloat(voice.rate) || 0) + Math.round((speed - 1) * 100);
      changes.rate = `${rate >= 0 ? '+' : ''}${rate}%`;
    }
    if (whisper || volume) changes.volume = whisper || volume === 'soft' ? '-30%' : '+20%';
    if (whisper && volume === 'loud') dropped.push(volume);
    if (pitch && !voice.pitch) changes.pitch = pitch === 'high' ? '+8Hz' : '-8Hz';
    else if (pitch) dropped.push(pitch + ' pitch');
  } else if (voice.provider === 'say' || voice.provider === 'eleven') {
    if (speed !== 1) {
      if (voice.provider === 'say') changes.rate = Math.round((voice.rate || 175) * speed);
      else dropped.push('속도');
    }
    if (whisper) dropped.push('whisper');
    if (pitch) dropped.push(pitch + ' pitch');
    if (volume) dropped.push(volume);
  }
  for (const key of Object.keys(changes)) {
    if (key !== 'volume' && changes[key] === voice[key]) delete changes[key];
  }
  return { changes, dropped };
}

export function voiceHash(voice, text, opts) {
  const { changes } = voiceDirection(voice, opts);
  const take = opts?.take ?? 0;
  const extra = { ...changes, ...(take > 0 ? { take } : {}) };
  const key = JSON.stringify(voice) + '|' + clean(text);
  return crypto.createHash('md5').update(Object.keys(extra).length ? key + '|' + JSON.stringify(extra) : key)
    .digest('hex').slice(0, 12);
}

export const voiceFile = (voice, text, dir, who, opts) =>
  path.join(dir, `${who}-${voiceHash(voice, text, opts)}.wav`);

// 앞뒤 무음을 잘라 48kHz 모노 wav로(문장 사이 쉼은 시간표에서 준다). Edge는 끝에 약 0.9초 무음이 붙어 나온다
function norm(src, dst, rawPcm) {
  sh('ffmpeg', ['-v', 'error', '-y', ...(rawPcm ? ['-f', 's16le', '-ar', '24000', '-ac', '1'] : []), '-i', src, '-af',
    'silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse',
    '-ar', '48000', '-ac', '1', dst]);
}

async function http(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) { const e = new Error(`${res.status} ${(await res.text()).slice(0, 400)}`); e.status = res.status; throw e; }
  return res;
}
async function retry(fn, label) {
  for (let n = 0; ; n++) {
    try { return await fn(); } catch (e) {
      if (/per day|daily/i.test(e.message)) { e.daily = true; throw e; } // 하루 한도는 기다려도 소용없다
      if (n >= 5 || (e.status && e.status < 500 && e.status !== 429)) throw e;
      const wait = e.status === 429 ? 20000 * (n + 1) : 3000 * (n + 1);
      console.error(`  ${label}: ${e.status || e.message.slice(0, 60)} 다시 시도 (${wait / 1000}초 뒤)`);
      await sleep(wait);
    }
  }
}

// ── 공급자 ──
const P = {
  say(v, text, out) {
    sh('say', ['-v', v.id, ...(v.rate ? ['-r', String(v.rate)] : []), '-o', out + '.aiff', text]);
    return out + '.aiff';
  },
  async edge(v, text, out, { volume }) {
    await retry(async () => sh('uvx', ['edge-tts', '--voice', v.id, ...(v.rate ? [`--rate=${v.rate}`] : []),
      ...(v.pitch ? [`--pitch=${v.pitch}`] : []), ...(volume ? [`--volume=${volume}`] : []),
      '--text', text, '--write-media', out + '.mp3']), 'Edge');
    return out + '.mp3';
  },
  async eleven(v, text, out) {
    const model = v.model || 'eleven_v4';
    const H = { 'xi-api-key': process.env.ELEVENLABS_API_KEY, 'content-type': 'application/json' };
    const lang = model.includes('multilingual') ? undefined : (v.lang || 'ko');
    // eleven_v4는 Text to Dialogue로만 된다
    const res = await retry(() => model === 'eleven_v4'
      ? http('https://api.elevenlabs.io/v1/text-to-dialogue?output_format=mp3_44100_128', { method: 'POST', headers: H,
        body: JSON.stringify({ model_id: model, language_code: lang, inputs: [{ text, voice_id: v.id }] }) })
      : http(`https://api.elevenlabs.io/v1/text-to-speech/${v.id}?output_format=mp3_44100_128`, { method: 'POST', headers: H,
        body: JSON.stringify({ model_id: model, language_code: lang, text }) }), 'ElevenLabs');
    fs.writeFileSync(out + '.mp3', Buffer.from(await res.arrayBuffer()));
    return out + '.mp3';
  },
  async gemini(v, text, out) {
    const model = v.model || 'gemini-3.8-flash-tts';
    const H = { 'x-goog-api-key': process.env.GEMINI_API_KEY, 'content-type': 'application/json' };
    const base = 'https://generativelanguage.googleapis.com/v1beta';
    // 3.x 정식 모델은 Interactions(말투 지시를 speech_metadata로 따로 준다). 3.1·2.5 프리뷰는 그걸 안 받아 generateContent로 보낸다
    const old = /3\.1-flash-tts-preview|2\.5-flash-preview-tts/.test(model);
    const body = old ? {
      contents: [{ parts: [{ text: v.style ? `${v.style}\nRead the following line exactly as written:\n${text}` : text }] }],
      generationConfig: { responseModalities: ['AUDIO'], speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: v.id } } } },
    } : {
      model,
      input: [{ type: 'user_input', content: [{ type: 'text', text, ...(v.style ? { annotations: [{ type: 'speech_metadata', style: v.style }] } : {}) }] }],
      response_format: { type: 'audio' },
      generation_config: { speech_config: [{ voice: v.id }] },
    };
    const j = await (await retry(() => http(old ? `${base}/models/${model}:generateContent` : `${base}/interactions`,
      { method: 'POST', headers: H, body: JSON.stringify(body) }), 'Gemini')).json();
    const a = findAudio(j);
    if (!a) throw new Error('Gemini 응답에 오디오 없음: ' + JSON.stringify(j).slice(0, 300));
    const b = Buffer.from(a, 'base64'), isWav = b.subarray(0, 4).toString() === 'RIFF';
    fs.writeFileSync(out + (isWav ? '.src.wav' : '.pcm'), b);
    return { file: out + (isWav ? '.src.wav' : '.pcm'), rawPcm: !isWav };
  },
  // VoiceStudio 앱(로컬, OpenAI 호환 API). 앱이 켜져 있어야 한다. 첫 요청은 모델을 올리느라 몇 분 걸린다
  async vstudio(v, text, out) {
    const base = (v.url || process.env.VOICESTUDIO_URL || 'http://127.0.0.1:3900').replace(/\/$/, '');
    const body = { model: v.model || 'tts-1', voice: v.id || 'alloy', input: text, language: v.lang || 'ko',
      seed: v.seed ?? 42, response_format: 'wav', ...(v.style ? { instruct: v.style } : {}), ...(v.speed ? { speed: v.speed } : {}) };
    const res = await retry(() => http(`${base}/v1/audio/speech`, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body) }).catch(e => {
      if (e.cause?.code === 'ECONNREFUSED') { const x = new Error(`VoiceStudio에 연결 안 됨(${base}). 앱을 켜 주세요`); x.status = 400; throw x; }
      throw e;
    }), 'VoiceStudio');
    fs.writeFileSync(out + '.src.wav', Buffer.from(await res.arrayBuffer()));
    return out + '.src.wav';
  },
};
function findAudio(o) {
  if (!o || typeof o !== 'object') return null;
  if (typeof o.data === 'string' && o.data.length > 1000) return o.data;
  for (const x of Object.values(o)) { const r = findAudio(x); if (r) return r; }
  return null;
}

// 문장 하나 → { file, dur, dropped }. 같은 목소리·문장·지시면 다시 받지 않는다
export async function speak(voice, text, dir, who = 'x', opts) {
  if (!voice || !P[voice.provider]) throw new Error(`알 수 없는 공급자: ${JSON.stringify(voice)}`);
  const { changes, dropped } = voiceDirection(voice, opts);
  const wav = voiceFile(voice, text, dir, who, opts);
  let duration;
  if (!fs.existsSync(wav)) {
    fs.mkdirSync(dir, { recursive: true });
    const work = fs.mkdtempSync(path.join(dir, '.tts-'));
    try {
      const r = await P[voice.provider]({ ...voice, ...changes }, clean(text), path.join(work, 'source'), changes);
      const tmp = path.join(work, 'normalized.wav');
      norm(r.file || r, tmp, r.rawPcm);
      duration = dur(tmp);
      if (!Number.isFinite(duration) || duration <= 0) throw new Error(`${who}의 음성 길이가 0이거나 잘못됐다.`);
      fs.renameSync(tmp, wav);
    } finally {
      fs.rmSync(work, { recursive: true, force: true });
    }
  }
  return { file: wav, dur: duration ?? dur(wav), ...(dropped.length ? { dropped } : {}) };
}

// 아직 안 받은 문장의 글자 수·요청 수(공급자별)
export function estimate(lines, voices, dir) {
  const need = {};
  const pending = new Set();
  for (const l of lines) {
    const v = voices[l.who] || voices.narr;
    if (!v) throw new Error(`배정표에 ${l.who}도 narr도 없음`);
    const text = l.say ?? l.t;
    const file = voiceFile(v, text, dir, l.who, l);
    if (pending.has(file) || fs.existsSync(file)) continue;
    pending.add(file);
    const n = (need[v.provider] ||= { requests: 0, chars: 0 });
    n.requests++; n.chars += clean(text).length;
  }
  return need;
}
export function overFree(need) {
  const msg = [];
  if (need.eleven?.chars > FREE.eleven.chars) msg.push(`ElevenLabs ${need.eleven.chars}자 > 무료 ${FREE.eleven.per} ${FREE.eleven.chars}크레딧`);
  if (need.gemini?.requests > FREE.gemini.requests) msg.push(`Gemini ${need.gemini.requests}번 > 무료 ${FREE.gemini.per} ${FREE.gemini.requests}번`);
  return msg;
}

// ── 명령으로 실행 ──
if (process.argv[1] && fs.realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const flag = f => args.includes(f);
  const envAt = args.indexOf('--env');
  if (envAt >= 0) loadEnv(args.splice(envAt, 2)[1]);
  const [linesF, voicesF, dir] = args.filter(a => !a.startsWith('--'));
  if (!dir) { console.error('쓰는 법: node tts.mjs <lines.json> <voices.json> <출력 폴더> [--dry-run] [--force] [--env <키 파일>]'); process.exit(2); }
  const lines = JSON.parse(fs.readFileSync(linesF, 'utf8'));
  const voices = JSON.parse(fs.readFileSync(voicesF, 'utf8'));
  const need = estimate(lines, voices, dir);
  console.log(`문장 ${lines.length}개. 새로 받을 것:`, Object.keys(need).length ? Object.entries(need).map(([p, n]) => `${p} ${n.requests}번 ${n.chars}자`).join(', ') : '없음(전부 캐시)');
  const over = overFree(need);
  for (const m of over) console.log('  한도 넘음: ' + m);
  if (flag('--dry-run')) process.exit(0);
  if (over.length && !flag('--force')) { console.log('무료 한도를 넘어서 멈춥니다. 그래도 부르려면 --force'); process.exit(1); }
  const out = [];
  for (const l of lines) {
    try { out.push({ ...l, ...(await speak(voices[l.who] || voices.narr, l.say ?? l.t, dir, l.who, l)) }); }
    catch (e) { console.error(`멈춤(${l.who}: ${l.t.slice(0, 20)}…): ${e.daily ? '하루 한도. ' : ''}${e.message.slice(0, 200)}`); process.exitCode = 1; break; }
  }
  fs.writeFileSync(path.join(dir, 'durations.json'), JSON.stringify(out, null, 1));
  console.log(`${out.length}/${lines.length}문장, 합 ${out.reduce((s, x) => s + x.dur, 0).toFixed(1)}초 → ${path.join(dir, 'durations.json')}`);
}
