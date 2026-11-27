---
name: story-voice
description: story.json의 문장을 공급자(VoiceStudio 로컬, macOS say, Edge TTS 등)로 낭독해 문장별 음성을 voice/에 캐시하고, 연기 지시 direction을 공급자가 받는 만큼 넘기며, 문장 시각표 out/timeline.json과 자막 out/subtitles.srt, 전체 음성 out/audio.wav를 만든다. "음성 만들어줘", "낭독해줘", "목소리 입혀줘", "목소리 바꿔서 다시", "연기 지시 반영해서 다시 녹음" 같은 요청이나 story-video 5단계에 쓴다.
---

# 5. 낭독 (음성)

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다. `<에피소드>`는 `story.json`이 있는 폴더다.

| | |
|---|---|
| 입력 | `story.json`(`lines`, `voices`) |
| 출력 | `voice/<who>-<해시>.wav`(문장마다), `out/timeline.json`, `out/subtitles.srt`, `out/audio.wav`, `out/voices.json`(쓴 배정표) |
| 다음 | `story-voice-check`가 잘못 읽은 문장을 찾아 고친다 |

## 명령

```bash
node <플러그인>/scripts/build.mjs <에피소드> voice                  # 음성 → 시간표·자막·전체 음성
node <플러그인>/scripts/build.mjs <에피소드> voice --provider say   # 모든 목소리를 say로(빠른 시범)
node <플러그인>/scripts/build.mjs <에피소드> timeline               # 캐시된 음성으로 시간표만 다시
```

- 같은 목소리·같은 글·같은 지시면 다시 만들지 않는다(해시 캐시). 끊기면 같은 명령을 다시 돌린다. `--force`면 다시 만든다.
- `--voices <파일>`: `story.json`의 `voices` 대신 쓸 배정표.
- 끝에 공급자별로 「지시 일부 반영 못 함 N문장(무엇)」이 나온다. 보고에 옮긴다.
- 2분 넘을 명령(VoiceStudio로 문장 수십 개)은 Bash 백그라운드로 돌린다. 6분 영상(문장 108개)은 VoiceStudio로 약 40분 걸렸다(첫 모델 올리기 포함).

## 목소리 배정 (`voices`)

`who` → 공급자 설정. 없는 `who`는 `narr`를 쓴다.

```json
{
  "narr": { "provider": "vstudio", "style": "female", "seed": 7 },
  "jun":  { "provider": "vstudio", "style": "male, high pitch", "seed": 21 },
  "test": { "provider": "say", "id": "Yuna", "rate": 170 }
}
```

| 공급자 | 비용 | 사용권·주의 |
|---|---|---|
| `vstudio` (VoiceStudio 앱) | 무료, 로컬. 앱이 켜져 있어야 한다 | 기본 엔진 OmniVoice 가중치는 CC-BY-NC(비상업). 공개·수익 영상이면 다른 엔진·공급자. `style`은 태그만(`female`·`male`·`whisper`·`low pitch`·`high pitch` 등). 인물마다 `seed`를 다르게 주고 바꾸지 않는다(seed가 목소리를 정한다) |
| `say` | 무료, 로컬 | 품질이 낮다. 시범·대본 확인용 |
| `edge` (`uvx edge-tts`) | 무료, 키 없음 | 비공식 경로라 상업 사용은 불분명, 예고 없이 막힐 수 있다 |
| `gemini` | 무료 한도 작음(하루 10번), 키 필요 | 무료 한도를 넘으면 멈춘다. 쓰기 전에 사용자에게 묻는다 |
| `eleven` | 유료(무료 한도 작음) | 무료 운영 전제라 쓰지 않는다 |

## 연기 지시가 반영되는 정도

`lines[].direction`(없으면 무시)에서 속도·속삭임·높낮이·크기를 뽑아 공급자에 넘기고, 문장형 지시는 받는 공급자에만 넘긴다. 실제로 보낸 값이 캐시 해시에 들어간다(지시를 고쳐도 보내는 값이 같으면 다시 만들지 않는다).

| 공급자 | 문장형 지시(감정·상황·말투) | 속도 | 속삭임 | 높낮이 | 크기 |
|---|---|---|---|---|---|
| `vstudio` OmniVoice(기본) | ✗ | ✓ `speed` | ✓ `whisper` 태그 | ✓ `low/high pitch` 태그 | ✗ |
| `vstudio` + `"instruct": "freeform"` | ✓ `direction_en`을 그대로 | ✓ | 엔진에 따라 | 엔진에 따라 | 엔진에 따라 |
| `gemini` | ✓ `direction_en`을 style로 | 지시문으로 | 지시문으로 | 지시문으로 | 지시문으로 |
| `edge` | ✗ | ✓ `rate` | 소리만 작게 | ✓ `pitch` | ✓ `volume` |
| `say` | ✗ | ✓ `rate` | ✗ | ✗ | ✗ |
| `eleven` | ✗ | ✗ | ✗ | ✗ | ✗ |

문장형 지시를 로컬에서 쓰려면 VoiceStudio 앱에서 문장형 지시(freeform instruct)를 받는 한국어 엔진을 켜야 한다. 이 Mac에 설치돼 있는 `mlx-audio`의 Qwen3-TTS VoiceDesign 모델(약 1.4GB 내려받기)이나 CosyVoice 3(약 6GB)이 그런 엔진이다. 사용자가 앱의 Model Catalogue에서 내려받고 엔진을 고른 뒤, 목소리 설정에 `"instruct": "freeform"`을 넣는다. 엔진마다 목소리를 고정하는 방법(seed, 목소리 프로필)이 다르니 한 장면으로 먼저 들어 보고 정한다. 내려받기·엔진 바꾸기는 사용자가 정한다.

## VoiceStudio로 몰아 만들 때

`story-video` 스킬의 「VoiceStudio 규칙 요약」을 따른다: 한 번에 하나, 생성 중에는 음성 인식·렌더 같은 무거운 작업을 같이 돌리지 않음, 엔진이 10GB를 넘으면 엔진만 재시작, 끊기면 같은 명령으로 이어 만들기, 오래 걸리면 `caffeinate -i`. `build.mjs`는 VoiceStudio 문장을 하나씩 순서대로 보낸다.

## 시간표

장면 길이 = 앞 쉼(lead 0.6초) + 문장 음성 + 문장 사이 0.35초 + 뒤 쉼(tail 0.8초). 시각은 1/30초 단위로 맞춘다. `out/timeline.json`의 문장마다 `start`·`end`·`file`이 있고, `out/subtitles.srt`는 같은 시각이다(유튜브 자막 트랙용). 음성은 화면 크기와 상관없이 하나를 쓴다.
