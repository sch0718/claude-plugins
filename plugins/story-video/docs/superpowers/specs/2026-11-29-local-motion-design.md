# story-video 0.6.0 설계: 로컬 그림·키프레임 움직임·세로 긴 영상

## 전제와 이유

- 사용자 1명, Apple Silicon 16GB Mac 한 대, 무료 운영. 디스크 여유 약 33GB.
- 필요한 것: 실사풍 그림을 이 기기의 로컬 모델로 만들고, 컷씬식(칸 분할·확대 펀치·시트) 대신 자연스럽게 움직이는 10분 세로 영상을 만든다.
- 이미 있는 것으로 안 되는 이유: 지금 그림은 Codex 클라우드(`art.mjs`)뿐이고, 움직임은 Ken Burns 한 장 또는 컷씬식뿐이다. 영상 모델은 없고 새로 받지 않는다.
- 로컬 모델(이미 있음): z-image-turbo q4(생성), flux2-klein-4b q4(편집). mflux 명령으로 부른다. 측정: z-image 640×1280·9단계가 단계당 약 24초, 한 장 약 4~5분(모델 읽기 포함).

## 범위

1. `art.mjs`에 로컬 엔진(`--engine local` 또는 `style.engine: "local"`).
2. 새 화면 방식 `style.mode: "motion"`: 장면 그림 + 편집 모델 키프레임, ffmpeg로 부드럽게 잇기, 아주 느린 카메라.
3. 10분 세로: 새 형식을 만들지 않고 `shorts`(1080×1920) 형식에서 길이 상한을 `check.mjs --max`로 바꿀 수 있게 한다. 길이 판정은 시간표가 있으면 시간표로 한다.
4. 문서와 0.6.0.

범위 밖: 영상 모델, 새 모델 내려받기, 인물 기준 그림을 편집 모델에 넣는 방식(아래 「인물 일관성」), 컷씬식 동작 변경.

## 1. 로컬 그림 엔진

- 엔진은 `--engine codex|local` > `style.engine` > `codex` 순으로 정한다. `style.engine`은 `style`이 객체일 때만 읽고 `codex`·`local`만 허용한다.
- `~/.config/story-video/art.env`(`STORY_VIDEO_ART_ENV`로 바꿈)에서 생성에 쓰는 `ZIMAGE_MODEL`과 키프레임 편집에 쓰는 `FLUX2_MODEL`의 값만 읽는다. 실행할 작업의 설정값이 비어 있으면 해당 키의 설정법을 알리고 멈춘다. 값이 있으면 실제로 존재하는 로컬 절대 경로로 확인한 뒤 그 경로를 `--model`에 넘긴다. 경로가 잘못됐으면 어느 키의 경로가 잘못됐는지 알리고 mflux를 실행하기 전에 멈춘다.
- 생성과 편집 모두 mflux를 오프라인으로 실행한다(자식 프로세스 환경에 `HF_HUB_OFFLINE=1`). 로컬 모델을 불러올 수 없으면 오류로 끝내며 모델 다운로드를 시도하지 않는다. mflux 명령은 PATH에서 찾고, 없으면 `~/.local/bin`에서 찾는다.
- 생성 크기는 형식의 그림 크기 비율 그대로 작게: wide 1152×768, shorts 768×1152. 만든 뒤 ffmpeg(lanczos)로 지금 그림 크기(1536×1024·1024×1536)로 키워 `art/<format>/<id>.png`에 둔다. 아래 단계(`build.mjs`)는 바뀌지 않는다.
- 명령: `mflux-generate-z-image-turbo --model <ZIMAGE_MODEL> --base-model z-image-turbo --prompt-file <요청문> --width --height --steps 9 --seed <seed> --output <임시>`. `--low-ram`은 art.mjs의 같은 이름 옵션으로만 넘긴다.
- 요청문(z-image용, 파일 저장 지시 없음): 화풍 → 장면 `art` → 등장인물 `이름: look` → 「모든 인물은 가상 인물, 실존 인물·연예인과 닮지 않게」 → 「글자·자막·로고·워터마크 없음」 → 쇼츠면 「중요한 것은 가운데·위쪽」. 실사 프리셋은 z-image에 맞게 사진 용어로 쓴 로컬 문구(`LOCAL_PRESETS.photoreal`: 35mm 영화 스틸, 자연스러운 피부 결, 얕은 심도, 필름 그레인, 차분한 색)를 쓰고, 다른 프리셋·문자열 화풍은 그대로 쓴다.
- seed: 장면의 `seed`(정수)가 있으면 그것, 없으면 장면 id의 해시. 같은 장면은 다시 그려도 같은 결과.
- 동시 실행: 로컬은 `--jobs`와 관계없이 한 장씩. mflux를 부르기 직전마다 다른 `mflux-generate` 프로세스가 돌고 있는지(`pgrep -f mflux-generate`) 보고, 있으면 그 장부터 멈춘다(16GB 스왑 방지). 잠금 파일은 두지 않는다(사용자 1명, 드문 경우).
- 로컬 엔진에서 `--anim`(컷씬 시트)은 쓰지 않는다. `--engine codex`로 그리라고 알리고 생성 전에 멈춘다. `--engine codex --anim`은 지금처럼 돈다.
- 계획 줄에 어림 시간을 함께 찍는다: 생성 장당 5분, 편집 장당 3분(어림, 실측 뒤 고침). 30장 경고는 codex 엔진에만 둔다.
- 요청문은 지금처럼 `art/<format>/prompts/<id>.txt`, 로그는 `logs/<id>.log`. `--dry-run`도 그대로 쓴다.

### 인물 일관성

flux2-klein 편집에 인물 기준 그림 여러 장을 넣는 방식은 이 기기에서 아직 한 번도 돌려 보지 못했다(다른 작업이 mflux를 쓰는 중). 그래서 이번에는 「같은 `look` 문장 + 장면별 고정 seed」로 단순하게 처리하고, 인수인계 메모에 수동 시험 명령을 남긴다. 시험에서 얼굴이 유지되면 다음 버전에서 넣는다.

## 2. 움직임 방식(`style.mode: "motion"`)

### 대본 설정

- `style.mode: "motion"`, 선택 `style.interp: "fade"|"mci"`(기본 `fade`).
- 장면 `keys`: 키프레임마다 달라질 작은 변화를 영어로 적은 문자열 배열, 0~3개. 없으면 기본 한 개(「몇 초 뒤의 같은 순간: 자세를 조금 바꾸고 고개와 시선을 살짝 돌린다, 같은 구도」). `[]`면 키프레임 없이 그림 한 장 + 카메라.
- 변화는 고개 돌리기·시선·손 위치·자세처럼 느린 것으로 쓴다. 눈 깜빡임처럼 빠른 동작은 디졸브로 이으면 유령처럼 겹쳐 보여 권하지 않는다.

### 키프레임 그림(`art.mjs --keys`)

- 생성·렌더·점검은 대본의 `scenes`에 있는 장면마다 같은 `keys` 목록을 사용한다. 생략하면 기본 변화 한 개를 쓰고, `[]`면 목록이 비어 있다. 목록 순서대로 1부터 번호를 매기며, 현재 목록에 없는 번호의 파일은 무시한다.
- 장면 그림이 먼저 있어야 한다. 키프레임 i(1부터)는 장면 그림을 입력으로 `mflux-generate-flux2-edit --model <FLUX2_MODEL> --base-model flux2-klein-4b --image-paths <장면 그림> --prompt-file … --width/--height(로컬 크기) --seed <장면 seed+i> --output`으로 만들고 키워서 `art/<format>/keys/<id>-<i>.png`에 둔다. 키프레임끼리 이어 만들지 않고 늘 장면 그림에서 만든다(어긋남이 쌓이지 않게).
- 편집 요청문: 「같은 사진을 유지: 같은 인물·얼굴·옷·배경·빛·카메라 구도. 아래 변화만 작고 자연스럽게: <keys[i]>. 글자 없음.」
- 로컬 엔진에서만 된다. codex 엔진이면 알리고 멈춘다. `--only`·`--force`·`--dry-run`을 따른다. motion 모드가 아니면 `--keys`는 오류.
- 재사용 판단: 키프레임을 제자리로 옮긴 뒤에만 쓴 요청문을 `art/<format>/keys/<id>-<i>.txt`에 남긴다(`--dry-run`용 요청문 `prompts/`와 따로 둔다). 키프레임 파일이 있고, 장면 그림보다 파일 시각이 나중이고, 남긴 요청문이 이번 요청문과 같을 때만 기존 것을 쓴다. 아니면 오래된 것으로 보고 다시 만든다(`--force`면 늘 다시 만든다). 새 키프레임은 임시 파일에 만들고 크기를 확인한 뒤 제자리로 옮긴다(`placeImage`와 같은 방식). 그래서 중간에 멈춘 뒤 같은 명령을 다시 돌리면 다 만든 키프레임은 그대로 쓰고 남은 것만 만든다.

### 장면 영상(`build.mjs visual`)

장면 그림 뒤에 현재 `keys` 목록에서 번호가 정해진 키프레임 파일을 번호순으로 잇는다. 해당 번호의 파일이 없으면 경고하고 건너뛴다. 그림이 하나도 없으면 지금처럼 단색 자리 그림을 쓴다.

- 시간표의 `recap`은 첫 장면의 기본 그림에 느린 카메라를 적용한다. 키프레임 생성과 누락 경고는 대본의 `scenes`에 있는 장면에만 적용한다.
- 키프레임 배치는 그림 목록 앞에서부터 `n = min(그림 목록 길이, max(1, floor(장면 길이 / 4초)))`장을 쓴다. 바뀌는 시각은 장면을 n등분한 경계다. 4초는 전환(1.5초)과 장면 끝 디졸브를 포함한 배정 구간의 최소 길이이며, 한 그림만 보이는 시간은 이보다 짧을 수 있다. 4초보다 짧은 장면에는 그림 한 장을 쓴다. 같은 장면 안에서 앞 그림으로 되돌아가지 않는다(왔다 갔다 금지).
- 전환: 경계에서 시작해 1.5초. `fade`는 선형 디졸브, `mci`는 ffmpeg `minterpolate`(mci·aobmc·bidir)로 두 그림 사이를 움직임 보간한다.
  - `mci` 입력: 두 정지 그림을 그대로 넣으면 출력이 0프레임이다(ffmpeg 9.0.2 확인). 그래서 [앞 그림, 뒤 그림, 뒤 그림 복제] 세 장을 `-framerate 2/3`(1.5초 간격)으로 넣고 `minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:scd=none`, `-frames:v 45`(1.5초 × 30fps)로 전환 구간만 자른다. `scd=none`이 없으면 그림 차이를 장면 바뀜으로 보고 중간에서 끊어 버린다.
  - 출력 프레임 수가 45가 아니거나 ffmpeg가 실패하면 그 전환만 `fade`로 바꾸고 경고한다. 실제 편집 그림에서의 보간 품질과 시간은 아직 재 보지 않았으므로 기본값은 `fade`로 둔다.
- 카메라: 키프레임을 이은 뒤 장면 전체에 느린 카메라 한 번. `kb`를 따르되 폭을 줄인다: `in` 1.00→1.05, `out` 1.05→1.00, `left`·`right`는 1.05 고정에서 끝까지 옆 이동. 확대 펀치·흔들림·칸 분할 없음.
- 장면 길이와 전환: 시간표가 정한 장면 길이를 30fps로 환산한 `N = Math.round(scene.dur * FPS)` 프레임을 장면마다 출력한다(`scripts/build.mjs`의 `makeScene`과 같은 계산). 전환은 이 프레임 안에서 처리하며 `joinVideo`는 시간표의 장면 경계에서 클립을 잇는다.
  - 첫 장면은 0.5초 검은 화면에서 밝아지고, 마지막 장면은 0.5초 어두워진다.
  - 다음 장면 그림이 있으면 현재 장면의 끝 `M = min(30, floor(N / 2))` 프레임을 다음 장면 그림에 그 장면의 카메라 0번을 적용한 목표 프레임으로 선형 디졸브한다. 구간 안에서 0부터 센 프레임 `j`의 목표 가중치 `w`는 `j / (M - 1)`이고 현재 프레임의 가중치는 `1 - w`다. `M = 1`이면 목표 가중치 1, `M = 0`이면 디졸브를 생략한다. 다음 장면은 그 목표 프레임에서 페이드 없이 시작한다. 합성 전 두 입력의 해상도·픽셀 형식·프레임률·타임베이스를 맞춘다.
  - 다음 장면 그림이 없으면 `scripts/cutscene.mjs`의 `sceneFade` 기본값에 맞춰 현재 장면 끝에서 검정으로 페이드아웃하고, 다음 장면의 단색 자리 그림은 검정에서 페이드인한다. 두 길이는 각각 해당 장면의 `min(9, floor(N / 2))` 프레임이며, 2프레임 미만이면 그쪽 페이드를 생략한다.
- 다시 만들기 판단: 컷씬식처럼 장면별 `inputs.json`(mode, 쓴 그림 경로 목록, 다음 장면 그림, interp, keys)을 남기고, 내용이나 파일 시각(그림·`keyframes.mjs` 포함)이 바뀌면 다시 만든다. `visual`과 `finish`는 같은 `sceneFresh`로 이 판단을 공유한다(오래됐으면 `finish`는 `visual`을 먼저 돌리라고 멈춘다).
- 되돌리기: `style.mode`를 되돌린 뒤 `build.mjs <에피소드> timeline --voices <에피소드>/out/voices.json` → `visual` → `finish`.
- 자막 배치는 일반 모드(`captionLayout(format, false)`)를 쓴다.

### 점검(`check.mjs`)

- motion 모드에서는 현재 `keys` 목록이 지정한 번호의 누락 파일을 경고한다. 시간표가 있으면 각 장면에 렌더와 같은 그림 목록과 4초 배치 규칙을 적용해 「움직임 · 장면당 키프레임 전환 평균 x회 · 키프레임 없는 장면 y개」를 찍는다. 배치에 들어간 장면 내부 키프레임 전환을 세고, 전환이 0회인 장면을 y에 넣는다. 시간표의 `recap`은 전환 0회로 집계한다. 카메라가 늘 천천히 움직이므로 정지 경고는 생략한다.

## 3. 10분 세로와 길이 점검 고침

- 결함: `check.mjs`의 범위 경고(`어림 길이가 … 벗어났다`)는 시간표가 있어도 글자 수 어림을 쓴다. 고침: 읽을 수 있는 시간표(`out/timeline.json`의 `total`)가 있으면 그 값으로 범위를 판정하고 「시간표 길이」라고 적는다. 없을 때만 어림. 범위 경고는 종료 코드를 바꾸지 않는다. 시간표가 대본보다 오래됐는지는 따로 보지 않는다(`build.mjs`가 오래된 시간표로는 영상을 만들지 않고, 영상과 0.5초 넘게 다르면 이미 오류다).
- 쇼츠 상한(오류 종료)은 지금처럼 시간표·영상 중 긴 값(둘 다 없으면 어림)을 쓰되, 고정값 180 대신 `--max`(기본 180)와 비교한다. 같은 길이는 통과한다. 10분 세로는 `format: "shorts"`, `"pace": "normal"`, `check.mjs <에피소드> --min 540 --max 660`으로 점검한다. 새 형식은 만들지 않는다(세로 분기 `format === 'shorts'`가 여러 파일에 퍼져 있어 형식 추가가 더 크다).
- 10분 세로를 고르면 구성·대본·마무리도 이 값을 따른다: `story-outline`은 편을 180초로 나누지 않고 540~660초 한 편으로 짜며 쇼츠 빠른 전개 규칙을 쓰지 않는다, `story-script`는 `check.mjs --min 540 --max 660`으로 점검한다, `story-finish`는 `check.mjs --format shorts --max 660`으로 확인한다.

## 4. 문서와 버전

- `skills/story-visual/SKILL.md`: 로컬 엔진·motion 모드 절, 명령, 시간 어림.
- `skills/story-video/SKILL.md`: 10분 세로 만드는 법(형식·pace·check 옵션), 로컬 그림은 다른 mflux 작업과 동시에 돌리지 않는다.
- `skills/story-outline`·`story-script`·`story-finish/SKILL.md`: 10분 세로일 때의 길이와 `check.mjs` 옵션(3절).
- `README.md` 「0.6.0에서 바뀐 것」, `CLAUDE.md`(플러그인)에 motion 모듈 위치.
- 버전 0.6.0: `plugins/story-video/.claude-plugin/plugin.json`, 루트 `.claude-plugin/marketplace.json`.

## 구성 요소

- 새 파일 `scripts/keyframes.mjs`: `motionMode`, `validateMotion`, `sceneKeys`, `keyPlan`(그림 배치·전환 프레임 계산, 순수 함수), `motionCamera`, `motionInputs`, `renderMotion`, `keyPath`, `keyFresh`(키프레임 재사용 판단).
- 새 파일 `scripts/local-art.mjs`: `artEngine`, `localPrompt`, `editPrompt`, `sceneSeed`, `localSize`, `mfluxBusy`, `runZImage`, `runFlux2Edit`(명령 인자 만드는 부분은 순수 함수로 떼어 시험).
- 고침: `art.mjs`(엔진 분기, `--keys`, `--low-ram`, `--engine`), `build.mjs`(motion 분기), `check.mjs`(길이 판정, motion 움직임 줄), `cutscene.mjs`의 `validateStyle`(engine·interp 형식).

## 시험

- `scripts/test/keyframes.test.mjs`: `keyPlan`(4초 최소 머묾, 되돌아가지 않음, 짧은 장면은 그림 1장, 끝 디졸브 길이), `validateMotion`(keys 0~3 문자열, interp 값), 카메라 식의 0번 프레임이 다음 장면 목표와 같은 식인지, `keyFresh`(장면 그림이 더 새것이거나 요청문이 바뀌면 다시 만듦).
- `scripts/test/local-art.test.mjs`: 엔진 결정 순서, 요청문에 금지 문구·인물·쇼츠 구도가 들어가는지, seed가 고정인지, mflux 인자.
- `check.mjs` 길이 판정(작은 임시 에피소드로 `spawnSync`): 시간표가 있으면 시간표 값으로 범위 경고, 쇼츠 시간표 200초면 기본은 종료 코드 1이고 `--max 660`이면 0.
- `art.mjs` 옵션 조합: 로컬+`--anim` 거부, codex+`--keys` 거부, motion이 아닐 때 `--keys` 거부.
- 렌더 확인: `examples/mini`를 motion 모드로 복사한 임시 에피소드에 ffmpeg 합성 그림(장면 그림 + 키프레임 1장)을 두고 그림이 있는 경계와 없는 경계를 만든다. `build.mjs … all --provider say --no-phone --format shorts`로 렌더하고 장면별 프레임 수와 합계가 시간표와 일치하는지 확인한다. 자막 합성과 인코딩 전 경계 프레임을 비교해 현재 장면의 마지막 프레임이 다음 장면의 카메라 0번 프레임과 같은지 보고, 다음 그림이 없는 경계에서는 검정 페이드아웃과 자리 그림 페이드인을 확인한다. 실제 mflux 생성 시험은 다른 mflux 작업이 끝난 뒤에 한다.

## 10분 1편 그림 수·시간 어림

- 장면 평균 15초면 약 40장면. 장면 그림 40장 + 키프레임 기본 1장씩 40장 = 80장.
- 생성 약 5분 × 40 + 편집 약 3분(실측 전 어림) × 40 ≈ 320분, 약 5.5시간. 키프레임 2장이면 약 7.5시간. 그래서 기본은 장면당 키프레임 1장이다.
