import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { assertCast, castLineErrors, isDraft, pitchAverages, pitchOutliers, validateCast } from './cast.mjs';
import { readable, selectVoices, speechLines, speechScore, storyScenes, validateStory } from './lib.mjs';
import { estimate, loadEnv, speak, voiceFile } from './tts.mjs';

const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
const json = value => JSON.stringify(value, null, 2) + '\n';
const script = name => path.join(import.meta.dirname, name);
const score = speechScore;
const fileMismatch = '현재 설정에서 예상한 음성 파일과 다름';
const pitchMismatch = '목소리 흔들림';

function samplesFor(story, timeline, ep) {
  const scenes = storyScenes(story);
  const mismatch = () => new Error('대본과 시간표의 문장 순서가 다르거나 음성 경로가 없다. voice 단계를 먼저 실행한다.');
  if (scenes.length !== timeline.scenes?.length) throw mismatch();
  return scenes.flatMap((scene, i) => {
    const saved = timeline.scenes[i];
    if (scene.id !== saved.id || scene.lines.length !== saved.lines?.length) throw mismatch();
    return scene.lines.map((line, j) => {
      const { who, file } = saved.lines[j];
      if (who !== line.who || !file) throw mismatch();
      return { scene: scene.id, index: j + 1, line, file: path.resolve(ep, file), reference: line.say ?? line.t };
    });
  });
}

function transcribe(files, model, work) {
  if (!files.length) return [];
  const input = path.join(work, 'input.json');
  const output = path.join(work, 'transcripts.json');
  const pitchFile = path.join(work, 'pitch.json');
  writeFileSync(input, json(files));
  execFileSync('uv', ['run', '--with', 'faster-whisper', 'python', script('asr.py'), input, output, '--model', model, '--pitch', pitchFile],
    { cwd: work, stdio: 'inherit' });
  const texts = readJson(output);
  const pitches = readJson(pitchFile);
  if (!Array.isArray(texts) || texts.length !== files.length || texts.some(text => typeof text !== 'string')) {
    throw new Error('받아 적은 문장 수나 형식이 맞지 않는다.');
  }
  if (!Array.isArray(pitches) || pitches.length !== files.length
      || pitches.some(pitch => pitch !== null && (!Number.isFinite(pitch) || pitch <= 0))) {
    throw new Error('음높이 측정 수나 형식이 맞지 않는다.');
  }
  return texts.map((recognized, i) => ({ recognized, pitch: pitches[i] }));
}

async function retrySamples(failed, voices, voiceDir, model, work) {
  const candidates = [];
  for (const sample of failed) {
    const say = readable(sample.reference);
    const { who } = sample.line;
    for (const take of [1, 2]) {
      const voice = voices[who];
      console.log(`${sample.scene} ${sample.index}번 문장 · 다시 만들기 ${take}`);
      try {
        const { file } = await speak(voice, say, voiceDir, who, { ...sample.line, take });
        candidates.push({ sample, say, take, file });
      } catch (error) {
        // 공급자가 꺼졌거나 한도에 걸려도 검사 결과는 남긴다
        console.error(`  다시 만들기 실패: ${error.message.slice(0, 200)}`);
        sample.retries.push({ take, say, error: error.message.slice(0, 200), score: -1, pitch: null, chosen: false });
      }
    }
  }
  if (!candidates.length) return;
  const measurements = transcribe(candidates.map(candidate => candidate.file), model, work);
  candidates.forEach(({ sample, say, take }, i) => {
    const { recognized, pitch } = measurements[i];
    sample.retries.push({ take, say, recognized, score: score(sample.reference, recognized), pitch, chosen: false });
  });
}

function adopt(story, samples, ep, storyFile, voices, work) {
  const changed = [];
  const voiceDir = path.join(ep, 'voice');
  for (const sample of samples) {
    const best = sample.retries.reduce((top, retry) => retry.score > top.score ? retry : top, sample.original);
    if (best === sample.original) continue;

    let line = sample.line;
    if (sample.scene === 'recap') {
      line = typeof story.recap === 'string' ? { t: story.recap } : story.recap;
      story.recap = line;
    }
    if (best.say !== sample.reference) line.say = best.say;
    line.take = best.take;
    changed.push({ sample, best });
  }
  if (!changed.length) return;
  if (Object.keys(estimate(speechLines(story), voices, voiceDir)).length) {
    throw new Error('선택한 목소리로 만든 음성이 빠져 있다. 같은 옵션으로 voice 단계를 먼저 실행한다.');
  }

  const config = path.join(work, 'voices.json');
  writeFileSync(config, json(voices));
  const tmp = `${storyFile}.${process.pid}.tmp`;
  try {
    writeFileSync(tmp, json(story));
    renameSync(tmp, storyFile);
  } finally {
    rmSync(tmp, { force: true });
  }
  execFileSync(process.execPath, [script('build.mjs'), ep, 'timeline', '--voices', config], { stdio: 'inherit' });
  for (const { sample, best } of changed) {
    best.chosen = true;
    Object.assign(sample, {
      recognized: best.recognized, score: best.score, pitch: best.pitch,
      issues: sample.issues.filter(issue => issue !== fileMismatch),
    });
  }
  console.log(`문장 ${changed.length}개의 음성을 바꿨다.`);
}

function writeReport(out, samples, min, castErrors) {
  const lines = samples.map(({ scene, index, line, reference, original, recognized, score, pitch, issues, retries }) => ({
    scene, index, who: line.who, reference, recognized, score, pitch, issues,
    verdict: score >= min ? '통과' : '사람 확인', original, retries,
  }));
  for (const line of pitchOutliers(lines)) line.issues.push(pitchMismatch);
  const needsReview = lines.filter(line => line.score < min || line.issues.length);
  const passed = lines.filter(line => line.score >= min).length;
  const average = lines.length ? lines.reduce((sum, line) => sum + line.score, 0) / lines.length : 0;
  const cell = text => text.replaceAll('|', '\\|').replace(/\s+/gu, ' ').trim();
  const retryNotes = retries => retries.map(retry =>
    `${retry.take}차 ${retry.error ? '실패' : retry.score.toFixed(3)}${retry.chosen ? ' (채택)' : ''}`).join(', ');
  const counts = new Map();
  for (const { who } of lines) counts.set(who, (counts.get(who) ?? 0) + 1);
  const pitches = pitchAverages(lines);
  const hz = pitch => pitch == null ? '—' : pitch.toFixed(1);
  const inconsistent = lines.filter(line => line.issues.length);
  const md = [
    '# 낭독 검증', '',
    `문장 ${lines.length}개 · 통과 ${passed}개 · 평균 점수 ${average.toFixed(3)} · 통과 기준 ${min}`, '',
    '## 사람 확인', '',
    ...castErrors.map(error => `- ${error}`),
    ...(castErrors.length ? [''] : []),
    '| 장면·문장 | 기준 글 | 인식 글 | 점수 | 다시 만든 기록 | 사유 |',
    '| --- | --- | --- | ---: | --- | --- |',
    ...needsReview.map(line => `| ${line.scene} ${line.index} | ${cell(line.reference)} | ${cell(line.recognized)} | ${line.score.toFixed(3)} | ${retryNotes(line.retries)} | ${cell(line.issues.join(', '))} |`),
    '',
    '## 목소리 일관성', '',
    '| 배역 | 문장 수 | 평균 음높이 (Hz) |',
    '| --- | ---: | ---: |',
    ...[...counts].map(([who, count]) => `| ${cell(who)} | ${count} | ${hz(pitches.get(who)?.pitch)} |`),
    '',
    '| 장면·문장 | 배역 | 음높이 (Hz) | 사유 |',
    '| --- | --- | ---: | --- |',
    ...inconsistent.map(line => `| ${line.scene} ${line.index} | ${cell(line.who)} | ${hz(line.pitch)} | ${cell(line.issues.join(', '))} |`),
    '',
  ];
  mkdirSync(out, { recursive: true });
  writeFileSync(path.join(out, 'voice-check.json'), json({ min, total: lines.length, passed, average, castErrors, lines }));
  writeFileSync(path.join(out, 'voice-check.md'), md.join('\n'));
  console.log(`낭독 검증 ${passed}/${lines.length}문장 통과 · 사람 확인 ${needsReview.length}문장 · ${out}`);
}

async function main() {
  const { positionals: [dir, ...extra], values } = parseArgs({
    options: {
      min: { type: 'string', default: '0.85' }, 'no-fix': { type: 'boolean' },
      model: { type: 'string', default: 'large-v3-turbo' }, out: { type: 'string' },
      provider: { type: 'string' }, voices: { type: 'string' },
    },
    allowPositionals: true,
  });
  const min = Number(values.min);
  if (!dir || extra.length || !values.min.trim() || !Number.isFinite(min) || min < 0 || min > 1
      || (values.provider !== undefined && values.provider !== 'say')) {
    throw new Error('쓰는 법 · node voicecheck.mjs <에피소드> [--min 0.85] [--no-fix] [--model large-v3-turbo] [--out <폴더>] [--provider say] [--voices <파일>]');
  }
  const ep = path.resolve(dir);
  const out = path.join(ep, 'out');
  const timelineFile = path.join(out, 'timeline.json');
  if (!existsSync(timelineFile)) throw new Error('시간표가 없다. voice 단계를 먼저 실행한다.');
  const storyFile = path.join(ep, 'story.json');
  const story = validateStory(readJson(storyFile));
  const lines = speechLines(story);
  const setup = path.join(out, 'voices.json');
  const config = values.voices ? readJson(values.voices) : existsSync(setup) ? readJson(setup) : undefined;
  const voices = selectVoices(story, lines, { provider: values.provider, voices: config });
  const baseVoices = story.voices;
  const { errors: castErrors } = validateCast(voices, lines, { draft: true, baseVoices });
  const roleErrors = castLineErrors(voices, lines, baseVoices);
  const voiceDir = path.join(ep, 'voice');
  const samples = samplesFor(story, readJson(timelineFile), ep);
  for (const sample of samples) {
    const { line, file } = sample;
    const { who } = line;
    sample.issues = [];
    if (roleErrors.has(who)) sample.issues.push(roleErrors.get(who));
    else if (path.basename(file) !== path.basename(voiceFile(voices[who], line.say ?? line.t, voiceDir, who, line))) {
      sample.issues.push(fileMismatch);
    }
  }
  const work = mkdtempSync(path.join(os.tmpdir(), 'story-voicecheck-'));
  try {
    const measurements = transcribe(samples.map(sample => sample.file), values.model, work);
    samples.forEach((sample, i) => {
      const { recognized, pitch } = measurements[i];
      const value = score(sample.reference, recognized);
      const original = { recognized, score: value, pitch };
      Object.assign(sample, original, { original, retries: [] });
    });

    const failed = samples.filter(sample => sample.score < min);
    let repairError;
    if (!values['no-fix'] && failed.length) {
      try {
        if (castErrors.length) throw new Error(`목소리 배정 오류\n- ${castErrors.join('\n- ')}`);
        loadEnv(process.env.STORY_VIDEO_VOICE_ENV ?? '~/.config/story-video/voice.env');
        await assertCast(voices, lines, { draft: isDraft(voices, values.provider), baseVoices });
        await retrySamples(failed, voices, voiceDir, values.model, work);
        adopt(story, failed, ep, storyFile, voices, work);
      } catch (error) {
        repairError = error;
      }
    }
    writeReport(values.out ? path.resolve(values.out) : out, samples, min, castErrors);
    if (repairError) throw repairError;
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

main().catch(error => {
  console.error(error.stderr?.trim() || error.message);
  process.exitCode = 1;
});
