---
name: story-visual
description: 이야기와 낭독 시간표에 맞춰 화면을 만든다. story.json에서 화풍과 톤을 정하고, Codex CLI나 로컬 모델로 장면 그림(가로 1536×1024, 세로 1024×1536)을 그려 자막 없는 video.mp4를 만든다. 일반 모드는 느린 카메라, 컷씬 모드는 컷·시트 애니메이션·화면 효과, motion 모드는 키프레임 전환과 느린 카메라를 쓴다. "장면 그림 그려줘", "그림체 정해줘", "이 장면만 다시 그려줘", "쇼츠용 세로 그림", "컷씬처럼 움직이게", "애니메이션 시트", "화면 만들어줘" 같은 요청이나 story-video 7단계에 쓴다.
---

# 7. 화면 (그림과 움직임)

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다. `<에피소드>`는 `story.json`이 있는 폴더다.

| | |
|---|---|
| 입력 | `story.json`(`scenes[].art`·`cast`·`kb`, `characters`, `format`), `out/timeline.json`(장면 길이) |
| 출력 | `story.json`의 `style`, `art/<format>/<장면 id>.png`·`contact.jpg`·`prompts/`·`logs/`, `out/<format>/scenes/<id>.mp4`, `out/<format>/video.mp4`(자막 없는 영상, 음성 포함). 컷씬식은 `art/<format>/anim/`, `out/<format>/fx/`·`audio-sfx.wav`도 |
| 다음 | `story-finish`가 자막을 입히고 검토한다 |

## 1) 그림 방식 정하기

사용자에게 이야기 분위기를 말하고 아래에서 두세 개를 권해 고르게 한다(하위 세션이면 brief의 것). 고른 값을 `story.json`의 `style`에 쓴다. 직접 쓰는 화풍은 기존 작가·작품·스튜디오 이름을 빼고 끝에 `original style`을 둔다.

| 화풍 | 어울리는 이야기 | `style` 예 |
|---|---|---|
| 수채 일러스트 | 감동, 일상, 옛이야기 | `Soft painterly digital illustration, muted palette, gentle film grain, cinematic wide composition, original style` |
| 웹툰풍 | 썰, 코믹, 반전 | 일반 `{"preset":"webtoon"}` / 컷씬 `{"mode":"cutscene","preset":"webtoon"}` |
| 실사풍 | 실제 사건, 다큐 | 일반 `{"preset":"photoreal"}` / 컷씬 `{"mode":"cutscene","preset":"photoreal"}` |
| 어두운 유화 | 괴담, 미스터리 | `Dark oil painting style, heavy brush strokes, deep shadows with a single warm light source, eerie quiet mood, original style` |
| 판화·민화풍 | 민담, 전설 | `Traditional woodblock print inspired illustration, textured paper, limited earthy palette, flat perspective, original style` |
| 동화 그림책 | 아이도 보는 이야기 | `Warm children's picture book illustration, soft gouache texture, rounded shapes, gentle pastel colors, original style` |

색·빛·시대는 객체의 `prompt`나 화풍 문자열 끝에 더한다(예시 `cold winter palette of frost blue and candle gold`). 같은 인물은 장면마다 같은 `look` 문장을 쓴다. 실제 사건도 실존 인물과 닮지 않은 얼굴로 그린다.

### 자연스러운 움직임 (`style.mode: "motion"`)

구성·대본 단계에서 시간표를 만들기 전에 화풍과 장면별 키프레임을 정한다. 예시는 `examples/mini-motion/story.json`에 있다.

```json
"style": {"mode": "motion", "engine": "local", "preset": "photoreal"}
```

- 장면의 `keys`에 자세, 시선, 손 위치처럼 조금씩 달라지는 동작을 영어로 0~3개 적는다. 눈 깜빡임처럼 빠른 동작은 그림을 겹칠 때 잔상이 생기기 쉬우므로 피한다.
- `keys`를 생략하면 자세와 시선을 조금 바꾸는 키프레임 한 장을 만든다. `"keys": []`를 쓰면 장면 그림 한 장에 느린 카메라만 적용한다.
- 키프레임은 `art/<format>/keys/<id>-<번호>.png`에 생긴다. 각 그림에 전환을 포함해 최소 4초를 배정하고, 장면 길이에 맞는 수만큼 앞에서부터 쓴다. 기본 전환은 1.5초 디졸브다. `style.interp: "mci"`를 쓰면 움직임 보간을 시도하고, 실패한 전환은 디졸브로 바꾼다.

### 게임 컷씬식

쇼츠에는 `style.mode: "cutscene"`을 권한다. 구성이나 대본 단계에서 `style` 객체와 아래 장면 설정을 써 둔다. 예시는 `<플러그인>/examples/mini-cutscene/story.json`이다.

- 움직임은 「보기 편한 정도」가 기준이다. 컷을 짧게 자주 바꾸거나 칸을 빨리 넘기면 정신없어 보인다(0.5.0에서 줄였다).
- 정지 그림은 문장 시작에 맞춰 3~7초 컷으로 나눈다. 긴 문장은 5초 안팎으로 고르게 나누고, 6초 미만 장면은 한 컷으로 둔다. 첫 컷의 `kb`에 이어 가벼운 클로즈업과 좌우 이동을 번갈아 쓴다.
- `!`로 끝나는 문장이 시작할 때 짧게 확대한다(`!?`·`?!` 포함). 장면마다 첫 번째 `!` 한 번만 확대한다. `fx`에 `shake`를 넣으면 장면 첫 페이드(0.3초)가 끝난 직후와 해당 문장 시작에 흔들림도 더한다. 그 밖의 화면 효과는 `fog`, `rain`, `snow`, `redmoon`, `dark`, `flash`다.
- `sfx: [{"t":0,"name":"thunder"}]`의 `t`는 장면 시작부터 지난 초다. 효과음은 `sfx/thunder.wav`에 둔다(`mp3`·`m4a`도 가능). 파일이 없으면 경고하고 건너뛴다.
- 여러 장면에 시트를 쓰려면 `style.anim: {"cols":2,"rows":2,"fps":1,"seq":[0,1,2,3,2,1]}`을 넣는다. 장면의 `anim` 객체가 기본값을 대신한다. `anim: false`는 그 장면의 시트를 끈다. 말하는 장면은 0.7~1.5fps(한 칸에 1초 안팎)로 두고, 문 열기·넘어지기처럼 짧은 동작만 3~4fps로 쓴다. 4fps 넘게 왕복하면 그림이 떨리듯 오가서 정신없다. 시트는 감정이나 동작이 바뀌는 장면(편의 절반 이하)에만 쓴다.
- 격자는 가로·세로 1~4칸, 재생 속도는 0.5~30fps다. `seq`는 왼쪽 위 0번부터 행 순서로 매긴 칸 번호다. `frames`에 칸 수를 쓰면 0번부터 재생한다. `loop: false`면 끝 칸에서 멈춘다. `motion`에 칸마다 달라질 동작을 적는다. 칸이 바뀔 때 앞뒤 프레임을 섞으며(느린 시트일수록 길게, 최대 7프레임), `blend: false`면 바로 바꾼다.

| 형식 | 권장 격자(가로×세로)와 칸 크기 |
|---|---|
| 쇼츠 | 2×2(512×768), 3×2(341×512), 3×3(341×512) |
| 가로 | 2×2(768×512), 2×3(512×341), 3×3(512×341) |

칸 비율이 기본 그림과 12% 넘게 다르거나 짧은 변이 340px 미만이면 경고한다. 쇼츠 2×3과 두 형식의 4×4가 여기에 해당한다. 1×1은 움직임이 없어 시트에 쓰지 않는다.

시간표를 만든 뒤 `story.json`을 고쳤으면 아래 순서로 확인한다. `say`로 낭독한 편은 `timeline`에 `--provider say`도 붙인다.

```bash
node <플러그인>/scripts/cutscene.mjs <에피소드> --check
node <플러그인>/scripts/build.mjs <에피소드> timeline --voices <에피소드>/out/voices.json
node <플러그인>/scripts/build.mjs <에피소드> visual
```

## 2) 장면 그림

```bash
node <플러그인>/scripts/art.mjs <에피소드> --dry-run             # 요청문만 art/<format>/prompts/에 쓴다
node <플러그인>/scripts/art.mjs <에피소드>                       # 없는 그림을 모두 그린다(동시에 3개)
node <플러그인>/scripts/art.mjs <에피소드> --only s03,s07 --force   # 그 장면만 다시
node <플러그인>/scripts/art.mjs <에피소드> --format shorts       # 쇼츠용 세로 그림(art/shorts/)
node <플러그인>/scripts/art.mjs <에피소드> --sheet-only          # 모아보기만 다시
```

- Codex 엔진은 장면마다 빈 작업 폴더에서 `codex exec -m gpt-6.2-sol`을 부르고, 나온 그림을 크기에 맞춰 `art/<format>/<id>.png`로 옮긴다. `codex`가 PATH에 없으면 `CODEX_BIN`에 경로를 준다.
- 한 편·한 크기에 장면 그림과 시트를 합쳐 30장까지 권장한다. 장면 그림은 한 장에 약 80초, 2×2 시트는 약 130초 걸린다(2026-11 측정). 장면 10장+시트 10장을 동시 3장으로 그리면 약 12~13분, 30장이면 15~20분이다(재시도 제외). 30장을 넘기기 전에 사용자에게 장수를 알린다. 장면이 10개를 넘으면 Bash 백그라운드로 돌린다.
- 요청문에는 그림 한 장만 저장하라는 지시, 글자·말풍선·로고·기존 작품 모방·실존 인물과 닮은 얼굴 금지, `cast` 인물의 `look`, 장면의 `art`, `style`을 넣는다. 쇼츠는 중요한 것을 화면 가운데나 위쪽에 둔다(아래쪽은 자막과 쇼츠 화면 단추가 가린다).
- 같은 대본으로 wide와 shorts를 다 만들면 그림만 크기별로 따로 그린다. 음성·시간표는 같이 쓴다.
- 0.1.0 작업 폴더의 `art/<id>.png`는 wide 그림으로 그대로 쓰인다.

### 로컬 그림 엔진 (`--engine local`)

mflux와 로컬 모델을 준비하고 `~/.config/story-video/art.env`에 실제로 존재하는 모델의 절대 경로를 쓴다. 설정 파일을 따로 쓰려면 `STORY_VIDEO_ART_ENV`에 경로를 준다. 각 작업에는 해당 모델의 경로가 필요하다.

```dotenv
ZIMAGE_MODEL=/path/to/z-image-turbo-q4
FLUX2_MODEL=/path/to/flux2-klein-4b
```

플러그인 폴더에서 장면 그림을 먼저 그린다. `--keys`는 대본의 `style.engine`에서 `local`을 읽고, 장면 그림을 바탕으로 키프레임을 만든다.

```bash
node scripts/art.mjs ep --engine local
node scripts/art.mjs ep --keys
node scripts/build.mjs ep visual
```

- 엔진은 `--engine`, `style.engine`, 기본값 `codex` 순으로 고른다. 로컬 모델은 오프라인에서 한 장씩 실행한다. 다른 mflux 작업이 끝난 뒤 시작한다. 저메모리 옵션이 필요하면 `--low-ram`을 붙인다.
- `--dry-run`으로 모델 없이 요청문을 확인할 수 있다. `--only`로 장면을 고르고, `--force`로 기존 그림을 다시 만든다. 컷씬 시트는 `--engine codex --anim`으로 그린다.
- 장면 그림은 한 장에 약 5분, 키프레임은 한 장에 약 3분으로 잡는다. 장면 40개와 키프레임 40개를 쓰는 10분 영상은 약 5시간 반이 걸릴 수 있다.
- 화면 설정을 바꾼 뒤에는 `node scripts/build.mjs ep timeline --voices ep/out/voices.json`으로 시간표를 갱신하고 `visual`을 실행한다. `say`로 낭독한 편은 `timeline`에 `--provider say`를 붙인다.

### 확인

1. `art/<format>/contact.jpg`를 Read로 본다.
2. 장면마다: 글자·말풍선이 섞였는지, 인물 수·옷·머리가 `look`과 맞는지, 장면 내용이 대본과 맞는지, 화풍이 한 편 안에서 튀지 않는지.
3. 걸린 장면은 `art` 문장을 고치고(「the sign is blank」, 「only one person」) `--only <id> --force`로 다시 그린다. 세 번 넘게 안 되면 구도를 바꾼다(글자가 잘 생기는 간판·책·화면을 뺀다).
4. 그림은 커밋하지 않는다. Codex는 원본을 `~/.codex/generated_images`에 따로 보관한다. 몇백 장이면 몇 GB가 되니 디스크가 빠듯하면 사용자에게 알리고 지울지 묻는다(직접 지우지 않는다).

### 애니메이션 시트

기본 그림을 먼저 그린 뒤 `node <플러그인>/scripts/art.mjs <에피소드> --anim --only s02`를 실행한다. `--only`를 빼면 시트 설정이 있는 장면을 모두 그린다. 파일은 `art/<format>/anim/s02.png`, 잘린 칸은 그 옆 `s02/`에 생긴다. 첫 칸이 기본 그림과 맞는지, 칸끼리 구도·인물이 이어지는지, 테두리나 여백이 없는지 본다.

## 3) 움직임 영상 (자막 없음)

```bash
node <플러그인>/scripts/build.mjs <에피소드> visual [--format shorts] [--jobs 2]
```

- 일반 모드는 시간표의 장면 길이에 맞춰 그림 한 장을 천천히 확대(`in`)·축소(`out`)·옆 이동(`left`·`right`)한다. 장면 앞뒤에는 0.3초 페이드가 들어간다.
- 장면 영상은 화질을 높게(중간본) 만들어 두고, 자막은 `story-finish`에서 한 번만 다시 인코딩하며 얹는다. `video.mp4`는 장면 영상을 다시 인코딩하지 않고 이어 붙여 음성을 얹은 미리보기다.
- 그림이 없는 장면은 단색 자리 그림으로 만든다(대본·시간만 먼저 볼 때).
- 6분 영상에 M2에서 몇 분 걸린다. 2분 넘을 것 같으면 Bash 백그라운드로 돌린다.
- 컷씬 모드는 시트 칸을 `fps`대로 넘기며 살짝 확대한다. 시트 파일이 없으면 기본 그림에 컷 분할과 카메라 움직임을 쓴다. `flash`는 장면 첫 0.3초를 흰색에서 시작한다. 효과음은 `audio-sfx.wav`로 섞어 `video.mp4`·`final.mp4`에 넣는다.
- 일반·컷씬 모드의 `check.mjs`는 장면당 평균 컷 수와 정지 구간을 보여 준다. 컷씬은 4초 넘게 컷·칸·카메라가 거의 안 움직인 구간을 정지로 보고, 쇼츠면 경고한다. 장면당 컷 평균이 2를 크게 넘으면 오히려 정신없지 않은지 프레임으로 본다.
- motion 모드는 장면당 키프레임 전환 평균과 전환이 없는 장면 수를 보여 준다. 필요한 키프레임 파일이 빠지면 경고한다.
