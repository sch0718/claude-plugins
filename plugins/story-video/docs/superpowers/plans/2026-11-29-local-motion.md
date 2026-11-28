# story-video 0.6.0 구현계획: 로컬 그림·키프레임 움직임·세로 긴 영상

설계: `plugins/story-video/docs/superpowers/specs/2026-11-29-local-motion-design.md`(이하 「설계」). 구현자는 설계를 먼저 끝까지 읽는다. 이 계획은 파일·순서·이름·검증만 정한다.

## 전역 제약

- 작업 폴더: 저장소의 `plugins/story-video`. 모든 경로는 이 폴더 기준.
- Node 내장 모듈만 쓴다(새 npm 의존성 없음). 기존 스크립트 문체(ESM, 한국어 오류 문구 「…다.」체)를 따른다.
- 컷씬식(`style.mode: "cutscene"`)과 일반 모드의 동작·출력은 바꾸지 않는다. 기존 시험은 고치지 않고 통과해야 한다.
- **mflux를 실제로 실행하지 않는다**(다른 작업이 쓰는 중, 16GB). mflux 호출은 인자 만드는 순수 함수로 시험하고, 렌더 시험은 ffmpeg 합성 그림으로 한다.
- 이 저장소는 공개다. 회사·사람 이름, 사내 주소를 넣지 않는다. 개인 경로(`~/01.workspace/...`)도 코드·문서에 넣지 않는다(설정 파일 예시는 `/path/to/...`).
- `FPS = 30`(`lib.mjs`). 장면 프레임 수는 늘 `Math.round(scene.dur * FPS)`.
- 커밋은 작업 단위마다 한국어 메시지로. 커밋 전 저장소 훅(`tools/scan-private --staged`)이 돈다.

## 파일

| 파일 | 할 일 |
|---|---|
| `scripts/local-art.mjs` (새) | 엔진 결정, 로컬 요청문, seed, 설정 읽기, mflux 인자·실행, 키우기 |
| `scripts/keyframes.mjs` (새) | motion 모드 검증, keys 목록, 배치 계산, 카메라, 장면 렌더, 캐시 판단 |
| `scripts/art.mjs` | `--engine`·`--keys`·`--low-ram`, 로컬 생성·키프레임 생성 분기 |
| `scripts/build.mjs` | motion 모드 장면 렌더·캐시 분기 |
| `scripts/check.mjs` | 길이 판정 고침, motion 움직임 줄·누락 경고 |
| `scripts/cutscene.mjs` | `validateStyle`이 `engine`·`interp` 형식 검사 |
| `scripts/test/local-art.test.mjs` (새) | 순수 함수 시험 |
| `scripts/test/keyframes.test.mjs` (새) | 순수 함수 + ffmpeg 렌더 시험 |
| `scripts/test/check.test.mjs` (새) | 길이 판정 시험(`spawnSync`) |
| `examples/mini-motion/story.json` (새) | `examples/mini` 복사 + `style: {"mode":"motion","engine":"local","preset":"photoreal"}`, 장면 2개에 `keys` 예 |
| 문서 | `skills/story-visual`·`story-video`·`story-outline`·`story-script`·`story-finish/SKILL.md`, `README.md`, `CLAUDE.md`(플러그인), 버전 |

## 이름과 시그니처(작업 사이에서 공유)

`scripts/local-art.mjs`

```js
export const LOCAL_SIZES = { wide: [1152, 768], shorts: [768, 1152] };
export const LOCAL_PRESETS = { photoreal: '...' };            // 설계 1절의 사진 용어 문구, 가상 인물 문구 포함
export function artEngine(story, override)                   // → 'codex' | 'local', 잘못된 값이면 throw
export function localPrompt(story, scene, formatName)         // → string (설계 1절 순서)
export function editPrompt(change)                           // → string (설계 2절 편집 요청문, 영어)
export function sceneSeed(scene)                             // → 0 이상 정수. scene.seed(안전 정수 ≥0) 우선, 아니면 id 해시 % 2**31
export function readArtEnv(file)                             // → { ZIMAGE_MODEL?, FLUX2_MODEL? } (KEY=VALUE 줄, # 주석, 따옴표 벗김, ~ 펼침)
export function modelPath(env, key)                          // → 절대 경로. 값 없음/상대 경로/없는 경로면 키 이름을 넣어 throw
export function mfluxBin(name)                               // PATH → ~/.local/bin, 없으면 throw
export function mfluxEnv(base = process.env)                 // → { ...base, HF_HUB_OFFLINE: '1' }
export function zimageArgs({ model, promptFile, size, seed, output, lowRam })   // → string[]
export function flux2EditArgs({ model, image, promptFile, size, seed, output, lowRam }) // → string[]
export function mfluxBusy()                                  // → boolean (pgrep -f mflux-generate 종료 코드 0)
export async function runMflux(bin, args, log)               // spawn, stdout·stderr를 log에 덧붙임, 30분 제한, 실패면 throw
export function upscale(src, out, [w, h])                    // ffmpeg scale=w:h:flags=lanczos, 임시 파일 뒤 rename
```

- `zimageArgs` 결과: `['--model', model, '--base-model', 'z-image-turbo', '--prompt-file', promptFile, '--width', w, '--height', h, '--steps', '9', '--seed', seed, '--output', output, ...(lowRam ? ['--low-ram'] : [])]`(숫자는 문자열로).
- `flux2EditArgs` 결과: `['--model', model, '--base-model', 'flux2-klein-4b', '--image-paths', image, '--prompt-file', promptFile, '--width', w, '--height', h, '--seed', seed, '--output', output, ...lowRam]`.
- 설정 파일 기본 경로: `process.env.STORY_VIDEO_ART_ENV ?? '~/.config/story-video/art.env'`.

`scripts/keyframes.mjs`

```js
export const DEFAULT_KEY = 'the same moment a few seconds later: the people shift their posture slightly and turn their heads and gaze a little; same camera framing';
export const MIN_HOLD = 4;            // 초, 전환 포함 배정 구간
export const TRANSITION = 1.5;        // 초 (45프레임)
export const motionMode = story => story?.style?.mode === 'motion';
export function validateMotion(story)                        // motion이면 style.interp(fade|mci|없음), scenes[].keys(없음 또는 문자열 0~3개, 빈 문자열 금지) 검사. story 반환
export function sceneKeys(storyScene)                        // storyScene 없으면 [] (recap), keys 없으면 [DEFAULT_KEY], 있으면 그대로
export const keyPath = (ep, formatName, id, i) => path.join(ep, 'art', formatName, 'keys', `${id}-${i}.png`);
export const keyRecord = (ep, formatName, id, i) => path.join(ep, 'art', formatName, 'keys', `${id}-${i}.txt`);
export function keyFresh({ key, record, base, prompt })      // 파일 있음 && mtime(key) >= mtime(base) && record 내용 === prompt
export function keyPlan(frames, count)                       // → { used, changes } used = min(count, max(1, floor(frames / (MIN_HOLD*FPS)))), changes = k=1..used-1의 Math.round(frames*k/used)
export const tailFrames = frames => Math.min(FPS, Math.floor(frames / 2));
export function motionCamera(frames, kb, { w, h })           // zoompan 필터 문자열(d=1, 비디오 입력용). in 1→1.05, out 1.05→1, left/right 1.05 고정 옆 이동, 그 밖은 in
export function motionInputs(item)                           // → { ready, manifest, files } (panelInputs와 같은 모양)
export async function renderMotion(item)                     // 장면 mp4를 item.output에 쓰고 manifest 반환
```

- `item`(build.mjs가 만든다): `{ scene, storyScene, index, ep, formatName, art, output, interp, first, last, next }`. `next`는 `{ art, dur, kb } | null`(마지막 장면이면 null). `art`는 `artPath(ep, format, scene.art)`.
- 그림 목록: `[art(있으면), ...sceneKeys(storyScene).map((_, i) => keyPath(..., storyScene.id, i + 1)).filter(existsSync)]`. 없는 키프레임은 `console.warn`.
- `manifest`: `{ mode: 'motion', images, next: next?.art ?? null, nextKb, nextDur, interp, keys, first, last }`. `files`: `[script('keyframes.mjs'), ...images, next.art(있으면)]`. `ready`는 `inputsFile` 내용이 manifest JSON과 같을 때(build.mjs의 `sameText` 방식).

## 작업 순서

### 작업 1: `local-art.mjs`와 시험

- `scripts/test/local-art.test.mjs`를 먼저 쓴다. 확인할 것:
  - `artEngine`: override > `style.engine` > `'codex'`, style이 문자열이면 style.engine 무시, `'gpu'` 같은 값은 throw.
  - `localPrompt`: 장면 art·인물 `이름: look`·「fictional」·「no text」 문구가 들어가고, shorts에만 구도 문구, `preset: 'photoreal'`이면 `LOCAL_PRESETS.photoreal`이 들어간다. 없는 인물 id면 throw(기존 art.mjs와 같은 문구).
  - `sceneSeed`: 같은 id → 같은 값, 다른 id → 대개 다른 값, `seed: 7` → 7.
  - `readArtEnv`·`modelPath`: 임시 파일과 임시 폴더로. 값 없음·상대 경로·없는 경로 각각 throw, 메시지에 키 이름.
  - `zimageArgs`·`flux2EditArgs` 전체 배열 일치, `lowRam` 붙음. `mfluxEnv().HF_HUB_OFFLINE === '1'`.
- 구현 후 `node --test scripts/test/local-art.test.mjs` 통과 → 커밋.

### 작업 2: `keyframes.mjs`의 순수 함수와 시험

- `scripts/test/keyframes.test.mjs`에 먼저:
  - `validateMotion`: keys 4개·빈 문자열·숫자 throw, interp `'blur'` throw, motion이 아니면 아무것도 검사 안 함.
  - `sceneKeys(undefined)` → `[]`, `{}` → `[DEFAULT_KEY]`, `{keys: []}` → `[]`.
  - `keyPlan(90, 2)` → `{used:1, changes:[]}`(3초), `keyPlan(240, 2)` → `{used:2, changes:[120]}`, `keyPlan(600, 4)` → `{used:4, changes:[150,300,450]}`, `keyPlan(600, 1)` → used 1. 이웃 changes 간격 ≥ 120.
  - `tailFrames(1)`=0, `tailFrames(3)`=1, `tailFrames(300)`=30.
  - `keyFresh`: 키프레임이 장면 그림보다 오래됨 → false, record 다름 → false, 다 맞음 → true(임시 파일·`utimesSync`).
  - `motionCamera`: `in`·`out`·`left`·`right`·없음마다 문자열에 `zoompan`과 `d=1` 포함, `out`의 0번 프레임 zoom이 1.05(식에 `on`=0 넣어 계산할 필요는 없고 `1.05-0.05*` 꼴인지).
- 커밋.

### 작업 3: `renderMotion`

필터 구성(한 번의 ffmpeg 호출, 30fps, `-frames:v N`, `videoEncoding(14)`):

1. 입력: 그림마다 `-loop 1 -framerate 30 -t <N/30+1> -i <그림>`. 모두 `scale=2w:2h:force_original_aspect_ratio=increase,crop=2w:2h,setsar=1,format=rgba`(cutscene `kenBurns`와 같은 2배 크기).
2. `fade` 전환: `keyPlan(N, images.length)`의 `changes[k-1]`마다 그림 k에 `fade=in:st=<changes/30>:d=1.5:alpha=1`을 걸고 앞 결과 위에 `overlay` 차례로 쌓는다(되돌아가지 않으므로 이것으로 충분하다).
3. `mci` 전환: 전환마다 임시 폴더에 [앞, 뒤, 뒤] 세 장을 `-framerate 2/3`로 넣고 `minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:scd=none`, `-frames:v 45`로 2배 크기 PNG 시퀀스나 mp4를 만든다. 결과 프레임 수가 45가 아니거나 ffmpeg 오류면 경고하고 그 전환을 `fade`로 바꾼다. 맞으면 그 클립을 `setpts=PTS+<st>/TB`로 옮겨 `overlay=eof_action=pass`로 얹고, 그 뒤 그림 k를 `enable='gte(t,<st+1.5>)'`로 얹는다.
4. 카메라: 합친 스트림에 `motionCamera(N, scene.kb, format)` → `format=yuv420p` 전 단계.
5. 끝 처리:
   - `next?.art`가 있으면: 먼저 별도 ffmpeg로 다음 장면 그림에 `motionCamera(round(next.dur*30), next.kb, format)`을 적용한 첫 프레임을 임시 `target.png`로 만든다. 본 그래프에서 `target.png`를 루프 입력으로 넣고 `M = tailFrames(N)`이 1 이상이면 `fade=in:start_frame=<N-M>:nb_frames=<max(1,M-1)>:alpha=1`로 위에 얹는다(마지막 프레임 가중치 1).
   - `next`가 null(마지막 장면)이거나 `last`면 마지막 15프레임 검정 페이드아웃. `next`는 있는데 `next.art`가 없으면 `sceneFade(N)`의 끝 부분과 같은 길이로 검정 페이드아웃.
   - `first`면 첫 15프레임 검정 페이드인. 자기 그림이 없으면(단색 자리 그림) `placeholder()`를 쓰고 `sceneFade(N)`을 그대로 쓴다(카메라 없음).
6. 시험(`keyframes.test.mjs`, ffmpeg 없으면 skip): 합성 그림 A(빨강 바탕)·키프레임 B(파랑 바탕)·다음 장면 C(초록)로
   - N=240(8초) 장면: 출력 프레임 수 240, 0.5초 부근은 빨강 우세, 6초 부근은 파랑 우세, 마지막 프레임은 초록 우세(픽셀 1×1 평균, 기존 cutscene 시험의 `pixel` 방식).
   - 같은 장면을 `interp: 'mci'`로 렌더해도 프레임 수 240.
   - 다음 장면 C를 `first: false`, `next: null`, `last: true`로 렌더한 첫 프레임과 앞 장면 마지막 프레임의 평균 색 차이 ≤ 8(채널별).
- 커밋.

### 작업 4: `build.mjs`·`cutscene.mjs` 연결

- `cutscene.mjs` `validateStyle`: `engine`이 있으면 `'codex'|'local'`, `interp`가 있으면 `'fade'|'mci'`가 아니면 throw. 기존 시험 문구는 그대로 통과해야 한다.
- `build.mjs`:
  - `visual|finish|all`에서 `validateCutscene(story)` 옆에 `validateMotion(story)`.
  - `videoStages`의 items에 `next`(다음 시간표 장면의 `{ art: existsSync(artPath(...)) ? artPath(...) : null, dur, kb }`, 마지막이면 null), `first: index === 0`, `last: index === n-1`, `interp: story.style.interp ?? 'fade'`을 motion일 때만 더한다.
  - `makeScene`: motion이면 `renderMotion(item)`을 부르고 manifest를 돌려준다.
  - `visualInputs`: motion이면 `motionInputs(item)`으로 `{ ready && sameText(inputsFile, JSON.stringify(manifest)), files: [...inputs, script('keyframes.mjs'), ...files] }`.
  - 렌더 뒤 manifest 저장: 지금 `if (cutscene) save(...)`를 `if (cutscene || motion)`으로.
- `examples/mini-motion/story.json` 추가.
- 검증(작업 7에서 다시 한다): `node scripts/build.mjs examples/mini-motion all --provider say --no-phone --format shorts`가 그림 없이(단색) 끝까지 돈다.
- 커밋.

### 작업 5: `art.mjs` 로컬 분기

- 옵션 추가: `engine`(string), `keys`(boolean), `'low-ram'`(boolean). 쓰는 법 문구에 더한다.
- `const engine = artEngine(story, values.engine)`.
  - `engine === 'local' && values.anim` → 「로컬 엔진에서는 --anim을 쓰지 않는다. --engine codex로 그린다.」 throw.
  - `values.keys`: motion 아니면 throw, local 아니면 throw, `--anim`과 같이 쓰면 throw.
- 로컬 장면 그림: 요청문은 `localPrompt`로 만들어 `prompts/<id>.txt`에 쓴다. `--dry-run`이면 여기서 끝. 아니면 `ZIMAGE_MODEL`을 `modelPath`로 확인(없으면 생성 전에 멈춤). 장면마다 차례로(동시 실행 없음): `mfluxBusy()`면 「다른 mflux 작업이 돌고 있어 <id>부터 멈춘다.」 출력 후 남은 장면을 실패로 두고 끝낸다. `runMflux(mfluxBin('mflux-generate-z-image-turbo'), zimageArgs({... size: LOCAL_SIZES[format], seed: sceneSeed(scene), output: <work>/raw.png }), log)` → `upscale(raw, out, FORMATS[format].art)`. 이미 있는 그림은 `--force` 없으면 건너뛴다. 기존 `makeArt`의 재시도·로그·상태 줄 형식을 따른다(재시도는 `--retries`, 기본 2).
- 키프레임(`--keys`): 장면마다 `sceneKeys(scene)`의 i번째(1부터) 변화로 `editPrompt(change)`를 만든다. `--dry-run`이면 `prompts/keys/<id>-<i>.txt`에만 쓴다. 장면 그림이 없으면 그 장면은 「기본 그림을 먼저 그린다」로 실패. `!force && keyFresh(...)`면 건너뛴다. 아니면 요청문을 작업 폴더에 쓰고 `flux2EditArgs({ model: modelPath(env,'FLUX2_MODEL'), image: <장면 그림>, size: LOCAL_SIZES[format], seed: sceneSeed(scene) + i, ... })` → `upscale` → 제자리로 옮긴 **뒤에** `keyRecord`에 요청문을 쓴다.
- 계획 줄: 로컬이면 `그림 계획 · <format> · 로컬 · 장면 그림 X장 · 키프레임 Y장 · 어림 Z분(생성 5분·편집 3분 기준)`. 30장 경고는 codex에만.
- 로컬 장면 그림을 만든 뒤에도 `contactSheet`를 부른다. 키프레임만 만들 때는 부르지 않는다.
- 시험(`local-art.test.mjs`에 더함, `spawnSync('node', ['scripts/art.mjs', ...])`, 임시 에피소드): local+`--anim` 거부, codex+`--keys` 거부, motion 아닌 대본+`--keys` 거부, `--engine local --dry-run`이 `prompts/s01.txt`를 쓰고 mflux를 부르지 않음, `--keys --dry-run`이 `prompts/keys/s01-1.txt`를 씀. 이 시험은 모델 설정 없이 돌아야 하므로 dry-run은 설정을 읽지 않는다.
- 커밋.

### 작업 6: `check.mjs`

- 시간표 읽기를 범위 판정 앞으로 옮긴다. `const length = timeline?.total ?? counts.seconds`, 문구 `시간표 길이`(시간표 있음)/`어림 길이`(없음). 범위 경고는 지금처럼 `warn`만.
- 쇼츠 상한: `if (format === 'shorts' && total > max)`, 문구 `쇼츠 길이 …초가 상한 ${max}초를 넘었다.`
- motion이면: 대본 `scenes`마다 `sceneKeys`의 번호별 `keyPath`가 없으면 `warn(\`${id} 키프레임 ${i}번이 없다.\`)`. 시간표가 있으면 장면마다 그림 수(장면 그림 1 + 있는 키프레임 수, recap은 1)로 `keyPlan`을 돌려 `움직임 · 장면당 키프레임 전환 평균 x.x회 · 키프레임 없는 장면 y개`를 찍고 `checkMotion`(컷씬 정지 경고)은 부르지 않는다.
- `scripts/test/check.test.mjs`: 임시 에피소드(`story.json` 쇼츠 1장면, 짧은 문장, `out/timeline.json`을 손으로 씀, `total: 200`)로
  - 기본 실행 → 종료 코드 1, 출력에 `시간표 길이`와 `상한 180초`.
  - `--max 660 --min 0` → 상한 문구 없음(그림·음성 없음 경고 때문에 종료 코드는 볼 필요 없고, 상한 문구가 없는지만 본다).
- 커밋.

### 작업 7: 문서·버전·끝 검증

- 설계 4절대로 문서를 고친다. `story-visual/SKILL.md`에 「로컬 그림(`--engine local`)」·「자연스러운 움직임(`style.mode: "motion"`)」 절: 설정 파일 예시(`ZIMAGE_MODEL=/path/to/z-image-turbo-q4`), 명령(`art.mjs ep --engine local`, `art.mjs ep --keys`, `build.mjs ep visual`), keys 쓰는 법(느린 변화, 깜빡임 금지), 시간 어림(10분 80장 약 5.5시간), 다른 mflux 작업과 동시에 돌리지 않음. frontmatter description에도 로컬·motion 한 구절.
- `story-video/SKILL.md`: 10분 세로(`format: "shorts"`, `"pace": "normal"`, `check.mjs --min 540 --max 660`), description의 「세로 쇼츠 3분 이하」 옆에 「세로 10분」.
- `story-outline`·`story-script`·`story-finish/SKILL.md`: 설계 3절의 한 줄씩.
- `README.md` 「0.6.0에서 바뀐 것」 절(0.5.0 절 위), `CLAUDE.md`(플러그인)에 `keyframes.mjs`·`local-art.mjs` 위치와 `examples/mini-motion`.
- 버전 0.6.0: `.claude-plugin/plugin.json`, 저장소 루트 `.claude-plugin/marketplace.json`(story-video 항목, description에 「로컬 실사 그림·키프레임 움직임」 구절).
- 끝 검증(모두 통과해야 한다):
  - `cd plugins/story-video && node --test scripts/test/*.test.mjs`
  - 저장소 루트 `python3 -m unittest discover -s tests`
  - `node scripts/build.mjs examples/mini all --provider say --no-phone`와 `--format shorts`, `node scripts/check.mjs examples/mini --frames 2,10,20` (기존 동작 그대로)
  - `node scripts/build.mjs examples/mini-motion all --provider say --no-phone --format shorts`, `node scripts/check.mjs examples/mini-motion --format shorts --frames 2,10,20`
- 커밋.

## 범위 밖(하지 않는다)

인물 기준 그림 편집, mflux 실제 실행, 잠금 파일, 새 화면 형식, 컷씬식 변경.
