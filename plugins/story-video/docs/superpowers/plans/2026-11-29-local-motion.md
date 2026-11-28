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
| `scripts/keyframes.mjs` (새) | motion 모드 검증, keys 목록, 배치 계산, 카메라, 장면 렌더, 렌더 입력 상태 수집 |
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
export async function runMflux(bin, args, log)               // mfluxBusy() 확인 후 env: mfluxEnv()로 spawn, stdout·stderr를 log에 덧붙임, 30분 제한, 실패면 throw
export function upscale(src, out, [w, h])                    // ffmpeg scale=w:h:flags=lanczos, 임시 파일 뒤 rename
```

- `zimageArgs` 결과: `['--model', model, '--base-model', 'z-image-turbo', '--prompt-file', promptFile, '--width', w, '--height', h, '--steps', '9', '--seed', seed, '--output', output, ...(lowRam ? ['--low-ram'] : [])]`(숫자는 문자열로).
- `flux2EditArgs` 결과: `['--model', model, '--base-model', 'flux2-klein-4b', '--image-paths', image, '--prompt-file', promptFile, '--width', w, '--height', h, '--seed', seed, '--output', output, ...(lowRam ? ['--low-ram'] : [])]`.
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
export async function motionArgs(item, tmp)                // ffmpeg 입력·필터 인자(출력·인코딩 제외). 시험은 이것으로 인코딩 전 rawvideo를 뽑는다
export async function renderMotion(item)                     // 장면 mp4를 item.output에 쓰고 manifest 반환
```

- `item`(build.mjs가 만든다): `{ scene, storyScene, index, ep, formatName, art, output, interp, first, last, next }`. `next`는 `{ art, dur, kb } | null`(마지막 장면이면 null). `art`는 `artPath(ep, format, scene.art)`.
- 그림 목록을 만드는 공통 함수를 추가해 `motionInputs`, `renderMotion`, `check.mjs`에서 사용한다. 실제 파일이 있는 기본 그림 `art`를 먼저 넣고, `sceneKeys(storyScene)`가 정한 번호의 `keyPath` 중 실제 파일이 있는 것만 번호순으로 잇는다. 렌더에서는 빠진 번호를 `console.warn`으로 알린다.
- `manifest`: `{ mode: 'motion', images, next: next?.art ?? null, nextKb, nextDur, interp, keys, first, last }`. `files`에는 `keyframes.mjs` 자체의 경로, `images`, `next?.art`(있으면)를 넣는다. 그림이 없어도 대체 렌더가 가능하므로 `motionInputs`는 `ready: true`를 반환한다.

## 작업 순서

### 작업 1: `local-art.mjs`와 시험

- `scripts/test/local-art.test.mjs`를 먼저 쓴다. 확인할 것:
  - `artEngine`: override > `style.engine` > `'codex'`, style이 문자열이면 style.engine 무시, `'gpu'` 같은 값은 throw.
  - `localPrompt`: 장면 art·인물 `이름: look`·「fictional」·「no text」 문구가 들어가고, shorts에만 구도 문구, `preset: 'photoreal'`이면 `LOCAL_PRESETS.photoreal`이 들어간다. 없는 인물 id면 throw(기존 art.mjs와 같은 문구).
  - `sceneSeed`: 같은 id → 같은 값, 다른 id → 대개 다른 값, `seed: 7` → 7.
  - `readArtEnv`·`modelPath`: 임시 파일과 임시 폴더로. 값 없음·상대 경로·없는 경로 각각 throw, 메시지에 키 이름.
  - `zimageArgs`·`flux2EditArgs`는 각각 `lowRam: true`일 때 끝에 `--low-ram`이 붙고, `lowRam: false`이거나 생략하면 빠지는지 전체 인자 배열을 비교한다. `mfluxEnv().HF_HUB_OFFLINE === '1'`.
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

1. 입력: 그림마다 `-loop 1 -framerate 30 -t <N/30+1> -i <그림>`. 모든 그림에 같은 `scale=2w:2h:force_original_aspect_ratio=increase,crop=2w:2h,setsar=1`을 적용한다(`cutscene.mjs`의 `kenBurns`와 같은 2배 크기). 기본 그림과 끝 디졸브에 쓸 다음 장면 그림은 같은 색 변환과 `format=yuv420p`로 정규화한다. 알파를 쓰는 키프레임 레이어의 형식은 `yuva420p`로 맞춘다. 그림이 하나도 없으면 `placeholder()`를 단색 영상 입력으로 쓰고, 키프레임 전환과 카메라를 건너뛰어 5단계를 적용한다.
2. `fade` 전환: `keyPlan(N, images.length)`의 `changes[k-1]`마다 그림 k에 `fade=in:st=<changes/30>:d=1.5:alpha=1`을 걸고 앞 결과 위에 `overlay=format=yuv420`으로 차례로 쌓는다. 전환 시작 전에는 레이어가 완전히 투명하다.
3. `mci` 전환: 전환마다 임시 폴더에 [앞, 뒤, 뒤] 세 장을 `-framerate 2/3`로 넣고 `minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:scd=none`, `-frames:v 45`로 2배 크기 PNG 시퀀스나 mp4를 만든다. 결과 프레임 수가 45가 아니거나 ffmpeg 오류면 경고하고 그 전환을 `fade`로 바꾼다. 맞으면 그 클립을 `setpts=PTS+<st>/TB`로 옮겨 `overlay=format=yuv420:eof_action=pass:enable='gte(t,<st>)'`로 얹고, 그 뒤 그림 k를 `overlay=format=yuv420:enable='gte(t,<st+1.5>)'`로 얹는다.
4. 카메라: 키프레임 유무에 관계없이 `yuv420p` 스트림에 `motionCamera(N, scene.kb, format)`을 적용하고 출력을 `format=yuv420p`로 고정한다. 첫 프레임의 카메라 입력은 기본 그림을 정규화한 프레임과 샘플 단위로 같아야 한다.
5. 장면 경계 처리(그림과 단색 자리 그림에 공통 적용):
   - 시작은 `first`면 첫 15프레임 검정 페이드인. 첫 장면이 아닌 단색 자리 그림은 `K = min(9, floor(N / 2))`가 2 이상이면 첫 `K`프레임 검정 페이드인. 그 밖의 장면은 들어오는 페이드 없이 시작한다.
   - `next?.art`가 있고 `M = tailFrames(N)`이 1 이상이면, 같은 그래프에서 다음 장면 그림에 1단계 정규화와 4단계 카메라 처리를 그대로 적용한다. 카메라 인자는 `Math.round(next.dur * FPS), next.kb, format`이고, `trim=end_frame=1`로 카메라 0번 프레임만 취한다. 이 프레임을 `yuv420p` 스트림으로 유지한다.
     - `M >= 2`이면 목표 프레임을 반복해 현재 스트림과 해상도·픽셀 형식·30fps·타임베이스·시작 시각을 맞춘 뒤, 알파용 `format=yuva420p,fade=in:start_frame=<N-M>:nb_frames=<M-1>:alpha=1`을 적용해 `overlay=format=yuv420`으로 합성한다. 마지막 프레임의 목표 가중치는 1이다. `M = 1`이면 현재 스트림의 처음 `N-1`프레임 뒤에 목표 프레임을 그대로 붙이고 30fps 타임스탬프로 맞춘다. `M = 0`이면 끝 디졸브를 생략한다.
   - `next`가 null(마지막 장면)이거나 `last`면 마지막 15프레임 검정 페이드아웃. `next`는 있는데 `next.art`가 없으면 `K = min(9, floor(N / 2))`가 2 이상일 때만 `sceneFade(N)`의 나가는 페이드와 같은 타이밍(`fade=out:s=<N-K>:n=<K-1>`)을 적용한다.
   - 시작 페이드인과 끝 디졸브가 겹치면 페이드인을 먼저 적용해 마지막 프레임의 목표 가중치 1을 보장한다.
6. 시험(새 `scripts/test/keyframes.test.mjs`에 작성, ffmpeg 없으면 skip): 합성 그림 A(빨강 바탕)·키프레임 B(파랑 바탕)·다음 장면 C(초록 바탕에 대비가 있는 색과 무늬)로
   - N=240(8초) 장면: 출력 프레임 수 240, 0.5초 부근은 빨강 우세, 6초 부근은 파랑 우세, 마지막 프레임은 초록 우세(픽셀 1×1 평균, 기존 cutscene 시험의 `pixel` 방식).
   - `interp: 'mci'`: 같은 배경 위에서 물체 위치만 다른 두 합성 그림으로 렌더하면 프레임 수 240이고 `fade` 대체 경고가 나오지 않는다(경고는 `console.warn`을 시험에서 가로채 확인). 대체 경로는 시험용 가짜 ffmpeg를 두지 않고 구현 검토로 확인한다.
   - 다음 장면에도 키프레임을 하나 두고 전환이 배정될 길이로 `first: false`, `next: null`, `last: true`, `kb: 'out'`을 지정한다. 두 장면의 실제 렌더 그래프에서 장면 전환 처리까지 마친 자막 합성·인코딩 전 출력을 `yuv420p` rawvideo로 뽑는다. 앞 장면의 마지막 프레임과 다음 장면의 첫 프레임에서 모든 Y·U·V 샘플이 바이트 단위로 일치하는지 확인한다. 앞 장면의 `M = 1`인 경우도 같은 방법으로 확인한다.
   - 그림 A → 단색 자리 그림: A의 마지막 프레임과 자리 그림의 첫 프레임이 검정이고, 자리 그림의 페이드인 뒤에는 단색이 보인다.
   - 단색 자리 그림 → 그림 C: 자리 그림의 마지막 프레임과 페이드 없이 시작한 C의 첫 프레임이 같은 방법(rawvideo 바이트 비교)으로 일치한다.
- 커밋.

### 작업 4: `build.mjs`·`cutscene.mjs` 연결

- `cutscene.mjs` `validateStyle`: `engine`이 있으면 `'codex'|'local'`, `interp`가 있으면 `'fade'|'mci'`가 아니면 throw. 기존 시험 문구는 그대로 통과해야 한다.
- `build.mjs`:
  - `visual|finish|all`에서 `validateCutscene(story)` 옆에 `validateMotion(story)`.
  - `videoStages`의 items에 `next`(다음 시간표 장면의 `{ art: existsSync(artPath(...)) ? artPath(...) : null, dur, kb }`, 마지막이면 null), `first: index === 0`, `last: index === n-1`, `interp: story.style.interp ?? 'fade'`을 motion일 때만 더한다.
  - `makeScene`의 첫 인자를 `item`으로 받는다. 호출부는 `{ ...item, output: item.video }`를 넘긴다. motion이면 `interp`·`first`·`last`·`next`가 담긴 `item`을 그대로 `renderMotion(item)`에 넘겨 manifest를 반환한다. 기존 분기에 필요한 필드는 함수 본문에서 구조 분해한다.
  - `visualInputs`의 motion 분기는 `motionInputs(item)`에서 `{ ready, manifest, files }`를 받고 `{ ready: ready && sameText(item.inputsFile, JSON.stringify(manifest)), files: [...inputs, ...files] }`를 반환한다. 매니페스트 비교는 여기서 한 번 하고, `visual`과 `finish`는 기존 `sceneFresh`를 공유해 이 결과와 입력 파일 시각을 함께 확인한다.
  - 렌더 뒤 manifest 저장: 지금 `if (cutscene) save(...)`를 `if (cutscene || motion)`으로.
- `examples/mini-motion/story.json` 추가.
- 검증(작업 7에서 다시 한다): `node scripts/build.mjs examples/mini-motion all --provider say --no-phone --format shorts`가 그림 없이(단색) 끝까지 돈다.
- 캐시 검증: motion 예시의 임시 사본에서 연속 두 장면에 ffmpeg 합성 그림을 두고 `all`을 돌린다. 입력을 그대로 두고 `--force` 없이 같은 형식으로 `visual`을 다시 실행해 「기존 장면을 쓴다」는 로그를 확인한다. 다음 장면 그림만 같은 경로에서 장면 영상보다 최신 파일로 교체한 뒤 `visual` 없이 `finish`를 실행한다. 앞 장면이 오래됐다는 오류와 `visual`을 먼저 실행하라는 안내를 확인한다.
- 커밋.

### 작업 5: `art.mjs` 로컬 분기

- 옵션 추가: `engine`(string), `keys`(boolean), `'low-ram'`(boolean). 쓰는 법 문구에 더한다.
- `const engine = artEngine(story, values.engine)`.
  - `engine === 'local' && values.anim` → 「로컬 엔진에서는 --anim을 쓰지 않는다. --engine codex로 그린다.」 throw.
  - `values.keys`: motion 아니면 throw, local 아니면 throw, `--anim`과 같이 쓰면 throw. 옵션 조합을 확인한 뒤 `validateMotion(story)`를 호출한다. `--dry-run`에서도 키프레임 수를 세거나 요청문을 쓰기 전에 검증한다.
- 로컬 장면 그림 생성과 키프레임 편집은 `--jobs` 값과 관계없이 한 장씩 차례로 `await runMflux(bin, args, log)`로 실행한다. 재시도를 포함해 호출마다 `runMflux`에서 자식 프로세스를 띄우기 직전에 `mfluxBusy()`를 확인한다. 다른 작업이 없으면 자식 프로세스에 `mfluxEnv()`를 넘겨 오프라인으로 실행한다. 다른 작업이 있으면 mflux를 띄우지 않고, `art.mjs`에서 「다른 mflux 작업이 돌고 있어 <id>부터 멈춘다.」를 출력한 뒤 현재 장면부터 남은 작업을 실패로 두고 재시도 없이 끝낸다.
- 로컬 장면 그림: 요청문은 `localPrompt`로 만들어 `prompts/<id>.txt`에 쓴다. `--dry-run`이면 여기서 끝. 아니면 `ZIMAGE_MODEL`을 `modelPath`로 확인(없으면 생성 전에 멈춤). `runMflux(mfluxBin('mflux-generate-z-image-turbo'), zimageArgs({... size: LOCAL_SIZES[format], seed: sceneSeed(scene), output: <work>/raw.png }), log)` → `upscale(raw, out, FORMATS[format].art)`. 이미 있는 그림은 `--force` 없으면 건너뛴다. 기존 `makeArt`의 재시도·로그·상태 줄 형식을 따른다(재시도는 `--retries`, 기본 2).
- 키프레임(`--keys`): 장면마다 `sceneKeys(scene)`의 i번째(1부터) 변화로 `editPrompt(change)`를 만든다. `--dry-run`이면 `prompts/keys/<id>-<i>.txt`에만 쓴다. 장면 그림이 없으면 그 장면은 「기본 그림을 먼저 그린다」로 실패. `!force && keyFresh(...)`면 건너뛴다. 아니면 요청문을 작업 폴더에 쓰고 `runMflux(mfluxBin('mflux-generate-flux2-edit'), flux2EditArgs({ model: modelPath(env,'FLUX2_MODEL'), image: <장면 그림>, size: LOCAL_SIZES[format], seed: sceneSeed(scene) + i, output: <work>/raw.png, ... }), log)` → `upscale` → 제자리로 옮긴 **뒤에** `keyRecord`에 요청문을 쓴다.
- 계획 줄: 로컬이면 `그림 계획 · <format> · 로컬 · 장면 그림 X장 · 키프레임 Y장 · 어림 Z분(생성 5분·편집 3분 기준)`. 30장 경고는 codex에만.
- 로컬 장면 그림을 만든 뒤에도 `contactSheet`를 부른다. 키프레임만 만들 때는 부르지 않는다.
- 시험(`local-art.test.mjs`에 더함, `spawnSync('node', ['scripts/art.mjs', ...])`, 임시 에피소드): local+`--anim` 거부, codex+`--keys` 거부, motion 아닌 대본+`--keys` 거부, `--engine local --dry-run`이 `prompts/s01.txt`를 쓰고 mflux를 부르지 않음, `--keys --dry-run`이 `prompts/keys/s01-1.txt`를 씀. 별도의 임시 motion·local 에피소드에 `keys` 네 개를 넣으면 `--keys --dry-run`이 0이 아닌 코드로 종료하고 키프레임 요청문을 하나도 쓰지 않음. 이 시험은 모델 설정 없이 돌아야 하므로 dry-run은 설정을 읽지 않는다.
- 커밋.

### 작업 6: `check.mjs`

- 시간표 읽기를 범위 판정 앞으로 옮긴다. `const length = timeline?.total ?? counts.seconds`, 문구 `시간표 길이`(시간표 있음)/`어림 길이`(없음). 범위 경고는 지금처럼 `warn`만.
- 쇼츠 상한: `if (format === 'shorts' && total > max)`, 문구 `쇼츠 길이 …초가 상한 ${max}초를 넘었다.`
- 대본을 읽는 기존 `try` 블록에서 `validateStory` 다음에 `validateMotion(story)`를 호출한다. 검증에 실패하면 기존 대본 오류 처리로 종료 코드 1을 반환한다.
- motion이면 대본 `scenes`마다 `sceneKeys`의 번호별 `keyPath`가 없을 때 `warn(\`${id} 키프레임 ${i}번이 없다.\`)`을 호출한다.
- motion 모드에서 시간표에 장면이 있으면 각 장면의 `artPath(ep, formatName, scene.art)`과 장면 ID에 대응하는 대본 장면을 렌더와 같은 공통 함수에 넘겨 그림 목록 `images`를 얻는다. 장면 내부 전환 수는 `keyPlan(Math.round(scene.dur * FPS), images.length).changes.length`로 센다. `recap`에는 대본 장면을 넘기지 않아 전환이 0회다. 시간표 장면 전체의 전환 평균을 `x.x`, 전환이 0회인 장면 수를 `y`로 넣어 `움직임 · 장면당 키프레임 전환 평균 x.x회 · 키프레임 없는 장면 y개`를 찍는다. motion 모드에서는 `checkMotion`(컷씬 정지 경고)을 생략한다.
- `scripts/test/check.test.mjs`: 임시 에피소드(`story.json` 쇼츠 1장면, 짧은 문장, `out/timeline.json`을 손으로 씀, `total: 200`)로
  - 기본 실행 → 종료 코드 1, 출력에 `시간표 길이`와 `상한 180초`.
  - 같은 시간표로 `--max 660` 실행 → 종료 코드 0, 상한 문구 없음.
- 커밋.

### 작업 7: 문서·버전·끝 검증

- 설계 4절대로 문서를 고친다. `skills/story-visual/SKILL.md`에 「로컬 그림(`--engine local`)」·「자연스러운 움직임(`style.mode: "motion"`)」 절을 쓴다.
  - `story.json`의 모션용 설정 예시는 `"style": {"mode": "motion", "engine": "local", "preset": "photoreal"}`이다. 시간표를 만들기 전에 구성·대본 단계에서 `style`과 장면별 `keys` 설정을 확정한다.
  - `~/.config/story-video/art.env` 예시에 장면 그림 생성용 `ZIMAGE_MODEL=/path/to/z-image-turbo-q4`와 키프레임 편집용 `FLUX2_MODEL=/path/to/flux2-klein-4b`를 모두 싣는다. 각 값에는 실제로 존재하는 로컬 모델의 절대 경로를 쓴다.
  - `node scripts/art.mjs ep --engine local`로 장면 그림을 먼저 만들고, `node scripts/art.mjs ep --keys`로 키프레임을 만든다. 이 `--keys` 호출은 대본의 `style.engine`에서 `local`을 읽는다.
  - 최신 시간표가 준비되면 `node scripts/build.mjs ep visual`로 렌더한다. 시간표를 만든 뒤 화면 설정을 수정했으면 `node scripts/build.mjs ep timeline --voices ep/out/voices.json`으로 시간표를 갱신한 뒤 `visual`을 돌린다. `say`로 낭독한 편은 `timeline`에 `--provider say`도 붙인다.
  - `keys` 쓰는 법(느린 변화, 깜빡임 금지), 시간 어림(10분 80장 약 5.5시간), 다른 mflux 작업과 동시에 돌리지 않는다는 안내를 넣는다. frontmatter description에도 로컬·motion 한 구절을 넣는다.
- `skills/story-video/SKILL.md`: 순서표에서 모션 화면 설정을 구성·대본 단계에서 시간표 생성 전에 확정한다고 적는다. 10분 세로(`format: "shorts"`, `"pace": "normal"`, `check.mjs --min 540 --max 660`)를 설명하고 description의 「세로 쇼츠 3분 이하」 옆에 「세로 10분」을 더한다. 「쇼츠도 만들 때」의 3분 분할 지시와 「긴 이야기를 쇼츠 시리즈로」의 절차는 일반 쇼츠에만 적용한다고 밝힌다.
- `skills/story-outline/SKILL.md`의 「길이와 편」에 10분 세로는 540~660초 한 편으로 짠다고 적는다. 「비트와 장면」에서 「쇼츠 빠른 전개」를 적용하는 범위는 일반 쇼츠로 한정한다.
- `skills/story-script/SKILL.md`의 「길이 맞추기」에 10분 세로는 `check.mjs --min 540 --max 660`으로 점검한다고 적는다.
- `skills/story-finish/SKILL.md`의 「기계 확인」에 일반 쇼츠 상한은 180초이고 10분 세로는 `check.mjs --format shorts --max 660`으로 확인한다고 적는다. `review.md` 양식의 「길이」에는 일반 쇼츠 3분 이하와 10분 세로 540~660초를 각각 판정 기준으로 적는다.
- `README.md` 「0.6.0에서 바뀐 것」 절(0.5.0 절 위), `CLAUDE.md`(플러그인)에 `keyframes.mjs`·`local-art.mjs` 위치와 `examples/mini-motion`.
- 버전 0.6.0: `.claude-plugin/plugin.json`, 저장소 루트 `.claude-plugin/marketplace.json`(story-video 항목, description에 「로컬 실사 그림·키프레임 움직임」 구절).
- 끝 검증(모두 통과해야 한다):
  - `cd plugins/story-video && node --test scripts/test/*.test.mjs`
  - 저장소 루트 `python3 -m unittest discover -s tests`
  - `examples/mini`와 `examples/mini-cutscene`을 각각 `node scripts/build.mjs <예시> all --provider say --no-phone --format <형식>`으로 `wide`와 `shorts` 두 크기에서 렌더한다. 각 결과를 `node scripts/check.mjs <예시> --format <형식> --frames 2,10,20`으로 점검하고 뽑은 PNG를 본다(`AGENTS.md`).
  - 작업 4의 그림 없는 `examples/mini-motion` 검증을 다시 실행한다. 합성 그림 렌더는 별도 임시 에피소드에서 다음 순서로 확인한다.
    1. `examples/mini/story.json`을 임시 에피소드로 복사하고 `style`을 `{"mode":"motion","engine":"local","interp":"fade"}`로 바꾼다. `s01`에는 키프레임 설명 하나를, 나머지 장면에는 `keys: []`를 넣는다. `s01`의 `lead`를 8초로 두어 전환에 필요한 장면 길이를 확보한다. ffmpeg로 빨강·초록·파랑 PNG를 만들어 각각 `art/shorts/s01.png`, `art/shorts/s02.png`, `art/shorts/keys/s01-1.png`에 둔다. `s03`부터는 그림을 비워 둔다.
    2. `node scripts/build.mjs <임시 에피소드> all --provider say --no-phone --format shorts`로 렌더한다. `ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames -of csv=p=0 <영상>`으로 `out/shorts/scenes/<id>.mp4`의 프레임 수가 `out/timeline.json`의 각 `Math.round(scene.dur * FPS)`와 일치하는지 확인한다. `out/shorts/video.mp4`와 `out/shorts/final.mp4`는 각각 그 합계 및 `Math.round(total * FPS)`와 일치해야 한다. 두 합본에서 `ffprobe -show_frames`로 각 장면 첫 프레임의 표시 시각을 `scene.start`와 프레임 단위로 대조한다.
    3. 자막 합성 전의 `out/shorts/scenes/` 클립에서 작업 3의 픽셀 비교를 적용한다. `s01`은 첫 페이드인과 전환 사이에 빨강이 우세해야 한다. `N = Math.round(scene.dur * FPS)`일 때 `Math.round(N / 2)`에서 시작하는 45프레임 전환 중간에는 빨강·파랑이 섞이고, 전환 뒤 장면 끝 디졸브 전에는 파랑이 우세해야 한다. `s01` 마지막 프레임과 `s02` 첫 프레임은 초록이 우세하고 평균 RGB 차이가 채널별 8 이하여야 한다. `s02`의 끝은 검정으로 어두워지고 `s03`은 검정에서 단색으로 밝아지는지 `sceneFade`의 구간 전후에서 확인한다.
    4. `node scripts/check.mjs <임시 에피소드> --format shorts --frames 2,10,20`으로 완성본을 점검한다.
- 커밋.

## 범위 밖(하지 않는다)

인물 기준 그림 편집, mflux 실제 실행, 잠금 파일, 새 화면 형식, 컷씬식 변경.
