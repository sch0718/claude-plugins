---
name: story-video
description: 이야기(창작, 인터넷 커뮤니티의 유명한 이야기, 요즘 화제인 이야기, 민담·도시전설·실제 사건)를 기승전결 영상(가로 5~10분 또는 세로 쇼츠 3분 이하)으로 끝까지 만든다. 수집 → 고르기 → 구성 → 대본 → 낭독 → 낭독 검증 → 화면 → 마무리·검토 여덟 단계를 차례로 부르는 순서표다(story-collect·story-select·story-outline·story-script·story-voice·story-voice-check·story-visual·story-finish). 긴 이야기는 여러 편으로 나눈다. "이 이야기로 영상 만들어줘", "썰 영상", "괴담 영상", "요즘 화제인 이야기 영상으로", "창작 이야기 영상", "이야기 쇼츠" 같은 요청에 쓴다. 게임·앱 소개 영상은 narrated-video를 쓴다.
---

# 이야기 영상 한 편 만들기 (순서표)

이 플러그인 폴더(`<플러그인>`)는 `${CLAUDE_PLUGIN_ROOT}`다. 값이 비어 있으면 이 스킬의 기본 폴더(Base directory)에서 두 단계 위(`../..`)가 플러그인 폴더다. 스크립트는 `<플러그인>/scripts/`에 있다.

필요한 것: Node.js 22 이상, ffmpeg·ffprobe, `uv`(자막 그림용 Pillow, 음성 인식용 faster-whisper), 그림을 그리려면 Codex CLI(`codex`, 이미지 생성이 되는 계정). 음성은 최종본에 VoiceStudio 앱(로컬), 임시판에 macOS `say`를 쓴다.

## 순서

각 단계 스킬은 따로도 쓸 수 있다. 스킬마다 「입력·출력」 표가 있다.

| # | 스킬 | 하는 일 | 명령·결과 | 맡는 모델 |
|---|---|---|---|---|
| 1 | `story-collect` | 이야기 후보 수집 | `collect/<날짜>-<주제>/index.md`, `01.md` … | 검색은 Sonnet 5.5 하위 에이전트 |
| 2 | `story-select` | 항목별 점수로 고르기 | `selection.md`, `<slug>/sources.md` | 이 세션, 고르기는 사용자 |
| 3 | `story-outline` | 기승전결·장면·편 나누기·핵심 사실 | `ep01/story.json`(뼈대, `facts`) | Opus 5.5 (이 세션) |
| 4 | `story-script` | 해설·대사·연기 지시 | `story.json`의 `lines` → `check.mjs ep01` | Opus 5.5 (이 세션) |
| 5 | `story-voice` | 배역 검사·낭독 | `cast.mjs ep01` → `build.mjs ep01 voice` → `voice/`, `out/timeline.json`, `out/voice-cast.md` | VoiceStudio(임시판은 say) |
| 6 | `story-voice-check` | 음성 인식으로 대조, 오독 다시 만들기, 목소리 일관성 검사 | `voicecheck.mjs ep01` → `out/voice-check.md` | 로컬 faster-whisper |
| 7 | `story-visual` | 그림 방식 정하기, 장면 그림, 움직임 영상 | `art.mjs ep01` → `contact.jpg` 확인, `build.mjs ep01 visual` | Codex `gpt-6.2-sol` 이미지 생성, ffmpeg |
| 8 | `story-finish` | 자막 입히기, 기계 확인, 원본과 비교 검토 | `build.mjs ep01 finish`, `check.mjs ep01 --frames …`, `review.md` | ffmpeg, 이 세션 |
| 9 | 올리기 | narrated-video 스킬의 「업로드」 절 | | |

- 4와 5 사이에 사용자에게 대본(장면별 문장·연기 지시)을 보여 주고 고칠 곳을 받는다. 대본이 굳으면 `cast.mjs ep01`로 후보와 배정을 확인한 뒤 음성을 만든다.
- 7의 그림 그리기는 대본이 굳으면 5·6과 따로 돌려도 된다(Codex와 VoiceStudio는 서로 다른 자원을 쓴다). 움직임 영상(`visual`)은 시간표가 있어야 한다.
- 시범으로 빨리 볼 때: `build.mjs ep01 all --provider say`(say 음성, 그림이 없으면 단색 자리 그림).
- 쇼츠도 만들 때: 같은 `story.json`으로 `art.mjs ep01 --format shorts`, `build.mjs ep01 visual --format shorts`, `finish --format shorts`. 음성·시간표는 같이 쓴다. 쇼츠는 3분 이하여야 하므로 구성 단계에서 편을 그 길이로 나눈다.

## 목소리 규칙 요약

- 해설은 `narr` 또는 `narr_<이름>`으로 1~2개를 쓴다. 모든 `who`를 배정하고 나이별 배역은 `base`를 적는다.
- 최종본은 한 VoiceStudio 엔진에서 배역별 복제 프로필과 정수 `seed`를 정해 쓴다. `say`는 임시판에 쓴다.
- 문장별 지시는 속도만 반영한다. 낭독 검증에서는 배정, 파일명, 같은 배역의 음높이를 확인한다.

## 긴 이야기를 쇼츠 시리즈로

`story-outline`에서 편 수와 편별 기승전결을 정하고 `ep01/`부터 `epNN/`까지 폴더를 둔다. 한 편의 대본부터 마무리 검토까지 끝낸 뒤 다음 편으로 넘어간다. 여러 편을 동시에 돌리지 않는다. 게임 컷씬식의 화면 설정은 구성·대본 단계에서 시간표를 만들기 전에 쓴다.

## 작업 폴더 규약

```
<작업 폴더>/
├── collect/<날짜>-<주제>/   index.md, 01.md …, selection.md        (1·2)
└── <slug>/
    ├── sources.md                                                    (2)
    └── ep01/
        ├── story.json      뼈대·facts(3) → lines·direction(4) → 그림 방식(7, 컷씬 설정은 3·4)
        ├── voice/          문장 음성 캐시                              (5·6)
        ├── sfx/            효과음(게임 컷씬식, 있으면)                  (7)
        ├── art/<format>/   장면 그림, contact.jpg, prompts/, logs/     (7)
        ├── out/            timeline.json, audio.wav, subtitles.srt, voices.json, voice-cast.md, voice-check.json·.md (5·6)
        │   └── <format>/   scenes/, video.mp4 (7)  subs/, final.mp4, final-phone.mp4, frames/ (8)
        └── review.md                                                 (8)
```

`<format>`은 `wide`(1920×1080) 또는 `shorts`(1080×1920). 작업 폴더는 git 저장소 밖이나 `.gitignore`된 곳에 둔다. 그림·음성·영상은 커밋하지 않는다.

## 0.1.0 작업 폴더를 0.2.0으로

0.1.0은 `art/<id>.png`, `out/final.mp4`를 썼다. 그림은 그대로 wide 그림으로 쓰인다. 지시가 없는 문장은 음성 캐시 해시가 같아서 다시 만들지 않는다.

1. `story.json`에 `"format": "wide"`와 `facts`를 더하고(`story-outline` 3), 문장마다 `direction`·`direction_en`을 쓴다(`story-script`).
2. 배정표를 복제 프로필 형식으로 맞춘 뒤 `build.mjs ep01 voice`를 실행한다. 새 배정이나 속도로 해시가 달라진 문장은 다시 만들어진다.
3. `voicecheck.mjs ep01` → `build.mjs ep01 visual` → `finish` → `check.mjs` → `review.md`.
4. 예전 `out/final.mp4`·`out/scenes/`·`out/subs/`는 새 결과(`out/wide/`)를 확인한 뒤 지워도 된다.

## 하위 세션에서 돌릴 때 (brief에 넣는다)

- 인앱 브라우저·Chrome 연동·computer-use처럼 권한 창이 뜨는 도구는 쓰지 않는다. 지켜보는 사람이 없어 권한 창에서 끝없이 멈춘다. 화면 확인은 `check.mjs --frames`로 PNG를 뽑아 Read로 보고, 그 밖의 화면 확인은 상위 세션에 넘긴다.
- 2분 넘는 명령(그림 그리기, VoiceStudio 음성, 음성 인식, 렌더)은 Bash 백그라운드로 돌린다.
- VoiceStudio 규칙(아래)을 brief에 옮겨 적는다.
- 수집 단계의 웹 검색은 검색 도구(WebSearch·web-fetch 에이전트)만 쓴다.

## VoiceStudio 규칙 요약 (`http://127.0.0.1:3900`)

- 음성 생성은 한 번에 하나만. 다른 세션이 쓰고 있으면(`pgrep -f omnivoice_subprocess`로 CPU·메모리 확인) 끝난 뒤에 하거나 사용자에게 묻는다. **생성 중에는 음성 인식(`voicecheck.mjs`), 전체 시험, 렌더를 같이 돌리지 않는다.**
- 엔진 메모리가 생성할수록 쌓인다(시간당 2~5GB). 30분쯤마다 `top -l 1 -pid <pid> -stats mem`으로 보고, 10GB를 넘으면 엔진 프로세스만 `kill`한다. 감독 프로세스가 몇 초 안에 다시 띄운다. 15초 뒤 API 200을 확인하고, 안 뜨면 사용자에게 앱을 껐다 켜 달라고 한다.
- `build.mjs voice`는 만든 문장을 캐시해 두고 이어 만든다. 끊기면 같은 명령을 다시 돌린다. 몇십 분 넘게 걸리면 `caffeinate -i`로 감싸고 로그를 파일로 남긴다.
- 어림: 문장당 7~11초, 첫 요청은 모델을 올리느라 약 5분, 메모리가 차면 40초 이상. 6분 영상(108문장)에 약 40분.
- **기본 엔진(OmniVoice) 가중치는 CC-BY-NC(비상업)다.** 개인·비공개 영상에 쓴다. 상업적으로 쓸 영상이면 VoiceStudio Model Catalogue에서 상업 사용이 가능한 엔진을 고른다. 목소리 복제는 주인 허락이 있는 목소리만 쓴다.

## 보고에 넣을 것

결과물 경로(`final.mp4`, `final-phone.mp4`, `subtitles.srt`, `sources.md`, `review.md`), 화면 크기, 길이, 장면·문장 수, 쓴 음성 공급자와 라이선스 조건, 연기 지시 반영 정도(`voice` 단계 끝의 「반영 못 함」 줄), 낭독 검증 결과(통과 수, 사람 확인 목록), 직접 확인한 것과 확인하지 못한 것. **음성은 사람이 들어 보기 전에는 확인되지 않은 것이다.**
