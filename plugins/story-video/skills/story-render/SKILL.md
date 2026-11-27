---
name: story-render
description: story.json과 장면 그림으로 문장별 음성(say·edge·VoiceStudio 등, 캐시), 시간표 timeline.json, 자막 .srt와 화면에 구운 한글 자막, 장면마다 느린 확대·이동(Ken Burns)과 짧은 전환을 만들어 final.mp4(1920×1080, 30fps)와 휴대폰용 축소본으로 합성하고, 길이·프레임을 뽑아 확인한다. "음성 입혀줘", "자막 넣어서 영상으로", "렌더해줘", "목소리 바꿔서 다시", "영상 길이 확인" 같은 요청이나 story-video 4단계에 쓴다.
---

# 음성·자막·합성

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다. `<에피소드>`는 `story.json`이 있는 폴더다.

## 명령

```bash
node <플러그인>/scripts/build.mjs <에피소드> voice      # 문장 → 음성(voice/에 캐시)
node <플러그인>/scripts/build.mjs <에피소드> timeline   # out/timeline.json, out/subtitles.srt, out/audio.wav
node <플러그인>/scripts/build.mjs <에피소드> render     # 자막 PNG → 장면 영상 → out/final.mp4, out/final-phone.mp4
node <플러그인>/scripts/build.mjs <에피소드> all        # 위 셋을 차례로(기본)
```

- 다시 돌리면 끝난 것은 건너뛴다. 그 단계를 다시 하려면 `--force`.
- `--provider say`: 모든 목소리를 macOS `say`(Yuna)로 바꾼다. 대본·화면을 빠르게 볼 때 쓴다(캐시가 따로라 나중에 진짜 목소리로 다시 돌려도 된다).
- `--voices <파일>`: `story.json`의 `voices` 대신 쓸 배정표. `--no-phone`: 휴대폰용을 건너뛴다. `--jobs 2`: 장면 렌더 동시 수.
- 그림(`art/<id>.png`)이 없는 장면은 단색 자리 그림으로 렌더한다. 대본·자막만 먼저 볼 때 쓸 수 있다.
- 6분 영상 렌더는 M2에서 10분 안팎이다. 2분 넘을 명령(VoiceStudio 음성, 렌더)은 Bash 백그라운드로 돌린다.

## 목소리 배정 (`voices`)

`who` → 공급자 설정. 없는 `who`는 `narr`를 쓴다. 형식은 플러그인의 `scripts/tts.mjs` 머리 주석과 같다.

```json
{
  "narr": { "provider": "vstudio", "style": "male", "seed": 7 },
  "jun":  { "provider": "edge", "id": "ko-KR-InJoonNeural", "rate": "+4%" },
  "test": { "provider": "say", "id": "Yuna", "rate": 170 }
}
```

| 공급자 | 비용 | 사용권·주의 |
|---|---|---|
| `say` | 무료, 로컬 | 품질이 낮다. 시범·대본 확인용 |
| `vstudio` (VoiceStudio 앱) | 무료, 로컬. 앱이 켜져 있어야 한다 | 기본 엔진 가중치 CC-BY-NC(비상업). 공개·수익 영상이면 다른 엔진·공급자. `style`은 `female`·`male`·`whisper`·`high pitch` 같은 태그만, 인물마다 `seed`를 다르게 주고 바꾸지 않는다 |
| `edge` (`uvx edge-tts`) | 무료, 키 없음 | 비공식 경로라 상업 사용은 불분명, 예고 없이 막힐 수 있다 |
| `gemini`·`eleven` | 무료 한도 작음, 키 필요(환경 변수 `GEMINI_API_KEY`·`ELEVENLABS_API_KEY`) | `tts.mjs`가 무료 한도를 넘으면 멈춘다. 쓰기 전에 사용자에게 묻는다 |

VoiceStudio로 몰아 만들 때는 `story-video` 스킬의 「VoiceStudio 규칙 요약」을 따른다: 한 번에 하나, 생성 중 무거운 작업 금지, 엔진 10GB 넘으면 엔진만 재시작, 끊기면 같은 명령으로 이어 만들기, 오래 걸리면 `caffeinate -i`. `build.mjs`는 VoiceStudio 문장을 하나씩 순서대로 보낸다.

## 화면 구성

- 장면 = 그림 한 장. 장면 길이 = 앞 쉼(lead 0.6초) + 문장 음성 + 문장 사이 0.35초 + 뒤 쉼(tail 0.8초).
- 그림은 장면 동안 천천히 확대(`in`)·축소(`out`)·옆 이동(`left`·`right`)한다. 장면 앞뒤 0.3초는 검은색으로 페이드한다.
- 자막은 문장 음성이 나오는 동안 화면 아래에 흰 글씨(해설)·노란 글씨(대사)로 나온다. 이 Mac의 ffmpeg에는 자막 필터가 없어서, Pillow로 투명 PNG를 그려 `overlay`로 겹친다. 같은 시각의 `.srt`도 따로 나온다(유튜브 자막 트랙용).

## 확인

```bash
node <플러그인>/scripts/check.mjs <에피소드> --frames 3,95,250
```

- 길이·해상도·fps·음성 트랙, timeline 총길이와 차이가 출력된다. 차이가 0.5초를 넘으면 문제다.
- `--frames`의 시각마다 `out/frames/t<초>.png`와 그 시각에 보여야 할 자막이 나온다. PNG를 Read로 열어 본다: 한글이 깨지지 않았는지, 자막이 아래 두 줄 이내인지, 그 자막이 그 장면 그림 위에 있는지. 시각은 장면 가운데·문장 가운데로 고른다(`out/timeline.json`의 `start`·`end`).
- `.srt` 시각이 음성과 맞는지는 `out/subtitles.srt`의 몇 줄을 `out/timeline.json`의 같은 문장 `start`·`end`와 비교한다.
- **음성은 사람이 들어 보기 전에는 확인되지 않은 것이다.** 보고에 「음성은 들어 보지 못함」을 적고, 사용자에게 `final-phone.mp4`를 들어 봐 달라고 한다.

## 결과물

`out/final.mp4`(1920×1080, 30fps, AAC), `out/final-phone.mp4`(960×540), `out/subtitles.srt`, `out/timeline.json`. 중간 파일 `out/scenes/`·`out/subs/`·`out/audio.wav`는 다시 렌더할 때 쓰인다. 디스크가 빠듯하면 `final.mp4`를 확인한 뒤 지워도 된다.
