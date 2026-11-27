---
name: story-video
description: 이야기(창작, 인터넷 커뮤니티의 유명한 이야기, 요즘 화제인 이야기, 민담·도시전설·실제 사건)를 5~10분짜리 기승전결 영상으로 끝까지 만든다. 수집 → 대본 → 장면 그림 → 음성·자막 → 합성 → 검수 순서를 잡고 단계마다 story-scout·story-script·story-art·story-render 스킬을 부른다. 긴 이야기는 여러 편으로 나눈다. "이 이야기로 영상 만들어줘", "썰 영상", "괴담 영상", "요즘 화제인 이야기 영상으로", "창작 이야기 영상", "자막·음성 있는 짧은 이야기 영상" 같은 요청에 쓴다. 게임·앱 소개 영상은 narrated-video를 쓴다.
---

# 이야기 영상 한 편 만들기 (총괄)

이 플러그인 폴더(`<플러그인>`)는 `${CLAUDE_PLUGIN_ROOT}`다. 값이 비어 있으면 이 스킬의 기본 폴더(Base directory)에서 두 단계 위(`../..`)가 플러그인 폴더다. 스크립트는 `<플러그인>/scripts/`에 있다.

필요한 것: Node.js 22 이상, ffmpeg·ffprobe, `uv`(자막 그림용 Pillow), 그림을 그리려면 Codex CLI(`codex`, 이미지 생성 도구가 켜진 계정). 음성은 macOS `say`(시범), VoiceStudio 앱(로컬), Edge TTS 등.

## 작업 폴더 규약

```
<작업 폴더>/<slug>/
├── sources.md          수집 출처(영상 설명에 쓴다)
├── candidates.md       수집 후보 표(수집했을 때)
├── ep01/
│   ├── story.json      대본·장면·화풍·인물·목소리 (story-script)
│   ├── art/            장면 그림 <장면 id>.png, prompts/, logs/, contact.jpg (story-art)
│   ├── voice/          문장 음성 캐시 (story-render)
│   └── out/            timeline.json, subtitles.srt, final.mp4, final-phone.mp4, frames/ (story-render)
└── ep02/ ...
```

작업 폴더는 git 저장소 밖이나 `.gitignore`된 곳에 둔다. 그림·음성·영상은 커밋하지 않는다.

## 순서

| # | 단계 | 스킬 | 명령·결과 | 맡는 모델 |
|---|---|---|---|---|
| 1 | 이야기 고르기 | `story-scout` | `candidates.md`, `sources.md` (창작이면 로그라인만) | 수집은 Sonnet 5.5 하위 에이전트(웹 검색), 고르기는 사용자 |
| 2 | 대본 | `story-script` | `ep01/story.json` → `node <플러그인>/scripts/check.mjs ep01` | Opus 5.5 (이 세션) |
| 3 | 장면 그림 | `story-art` | `node <플러그인>/scripts/art.mjs ep01` → `art/contact.jpg` 확인 | Codex `gpt-6.2-sol` 이미지 생성 |
| 4 | 음성·자막·합성 | `story-render` | `node <플러그인>/scripts/build.mjs ep01 all` → `out/final.mp4` | 로컬 VoiceStudio(또는 say·edge), ffmpeg |
| 5 | 검수 | 이 스킬 「검수」 | `check.mjs ep01 --frames …` + 프레임 Read | 상위 세션(하위 세션이면 결과를 넘긴다) |
| 6 | 올리기 | narrated-video 스킬의 「업로드」 절 | | |

2·3 사이에 사용자에게 대본(장면별 비트·문장)을 보여 주고 고칠 곳을 받는다. 그림·음성은 대본이 굳은 뒤에 만든다(다시 만들면 시간이 든다).

## 하위 세션에서 돌릴 때 (brief에 넣는다)

- 인앱 브라우저·Chrome 연동·computer-use처럼 권한 창이 뜨는 도구는 쓰지 않는다. 지켜보는 사람이 없어 권한 창에서 끝없이 멈춘다. 화면 확인은 `check.mjs --frames`로 PNG를 뽑아 Read로 보고, 그 밖의 화면 확인은 상위 세션에 넘긴다.
- 2분 넘는 명령(그림 그리기, VoiceStudio 음성, 렌더)은 Bash 백그라운드로 돌린다.
- VoiceStudio 규칙(아래)을 brief에 옮겨 적는다.
- 수집 단계의 웹 검색은 검색 도구(WebSearch·web-fetch 에이전트)만 쓴다.

## VoiceStudio 규칙 요약 (`http://127.0.0.1:3900`)

- 음성 생성은 한 번에 하나만. 다른 세션이 쓰고 있으면(`pgrep -f omnivoice_subprocess`로 CPU·메모리 확인) 끝난 뒤에 하거나 사용자에게 묻는다. 생성 중에는 무거운 작업(음성 인식 검사, 전체 시험, 렌더)을 같이 돌리지 않는다.
- 엔진 메모리가 생성할수록 쌓인다(시간당 2~5GB). 30분쯤마다 `top -l 1 -pid <pid> -stats mem`으로 보고, 10GB를 넘으면 엔진 프로세스만 `kill`한다. 감독 프로세스가 몇 초 안에 다시 띄운다. 15초 뒤 API 200을 확인하고, 안 뜨면 사용자에게 앱을 껐다 켜 달라고 한다.
- `build.mjs voice`는 만든 문장을 캐시해 두고 이어 만든다. 끊기면 같은 명령을 다시 돌린다. 몇십 분 넘게 걸리면 `caffeinate -i`로 감싸고 로그를 파일로 남긴다.
- 어림: 혼자 돌 때 문장당 7~11초, 첫 요청은 모델을 올리느라 약 5분, 메모리가 차면 40초 이상.
- **기본 엔진(OmniVoice) 가중치는 CC-BY-NC(비상업)다.** 개인·비공개 영상에만 쓴다. 공개하거나 수익을 내는 영상이면 앱 Model Catalogue에서 상업 사용이 되는 엔진을 고르거나 다른 공급자를 쓴다. 목소리 복제는 주인 허락이 있는 목소리만.

## 검수 체크리스트

- [ ] 길이 5~10분(`check.mjs`의 어림과 `final.mp4` 실제 길이). 여러 편이면 편마다.
- [ ] 기승전결 비트가 다 있고, 첫 15초 안에 궁금증을 거는 도입이 있다. 여러 편이면 끝이 다음 편으로 끌고 가고, 2편부터 짧은 「지난 이야기」가 있다.
- [ ] 자막과 음성이 맞는다: `out/subtitles.srt` 시각이 `out/timeline.json`의 문장 시각과 같고, `final.mp4` 길이와 timeline 총길이 차가 0.5초 이내.
- [ ] 프레임 3장 이상을 뽑아 Read로 본다: 한글이 깨지지 않고, 자막이 화면 아래 두 줄 이내이고, 그림이 그 장면 내용과 맞는다.
- [ ] 그림 속에 글자·말풍선·워터마크가 없다(`art/contact.jpg`로 한 번에 확인). 인물 모습이 장면마다 크게 달라지지 않는다.
- [ ] 출처: 수집한 이야기면 `sources.md`가 있고 영상 설명에 넣을 출처 목록이 준비됐다. 실화라고 단정할 수 없는 이야기는 대본에 「전해지는 이야기」로 밝혔다.
- [ ] 음성은 사람이 들어 보기 전에는 확인된 것이 아니다. 보고에 「음성은 들어 보지 못함」이라고 적는다.

## 보고에 넣을 것

결과물 경로(`final.mp4`, `final-phone.mp4`, `subtitles.srt`, `sources.md`), 길이, 장면·문장 수, 쓴 음성 공급자와 라이선스 조건, 직접 확인한 것과 확인하지 못한 것.
