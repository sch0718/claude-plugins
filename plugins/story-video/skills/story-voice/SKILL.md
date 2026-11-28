---
name: story-voice
description: story.json의 문장을 배역별 VoiceStudio 복제 목소리로 낭독해 문장별 음성을 voice/에 캐시하고, 연기 지시 direction에서 속도를 반영한다. 문장 시각표 out/timeline.json과 자막 out/subtitles.srt, 전체 음성 out/audio.wav, 배역 보고서 out/voice-cast.md를 만든다. "음성 만들어줘", "낭독해줘", "목소리 입혀줘", "목소리 바꿔서 다시", "연기 지시 반영해서 다시 녹음" 같은 요청이나 story-video 5단계에 쓴다.
---

# 5. 낭독 (음성)

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다. `<에피소드>`는 `story.json`이 있는 폴더다.

| | |
|---|---|
| 입력 | `story.json`(`lines`, `voices`) |
| 출력 | `voice/<who>-<해시>.wav`(문장마다), `out/timeline.json`, `out/subtitles.srt`, `out/audio.wav`, `out/voices.json`(쓴 배정표), `out/voice-cast.md`(배역·엔진·프로필·seed·style·문장 수) |
| 다음 | `story-voice-check`가 잘못 읽은 문장을 찾아 고친다 |

## 명령

```bash
node <플러그인>/scripts/cast.mjs <에피소드>                        # 배정 검사·복제 프로필 후보
node <플러그인>/scripts/build.mjs <에피소드> voice                  # 음성 → 시간표·자막·전체 음성
node <플러그인>/scripts/build.mjs <에피소드> voice --provider say   # 모든 목소리를 say로(빠른 시범)
node <플러그인>/scripts/build.mjs <에피소드> timeline               # 캐시된 음성으로 시간표만 다시
```

- 최종본 `voice`·`all`은 음성이 모두 캐시돼 있어도 시작할 때 VoiceStudio 프로필을 확인하므로 앱이 켜져 있어야 한다. 그림만 다시 만들 때는 `visual`·`finish`를 쓴다.
- 같은 목소리·같은 글·같은 지시면 다시 만들지 않는다(해시 캐시). 끊기면 같은 명령을 다시 돌린다. `--force`면 다시 만든다.
- `--voices <파일>`: `story.json`의 `voices` 대신 쓸 배정표.
- 끝에 공급자별로 「지시 일부 반영 못 함 N문장(무엇)」이 나온다. 보고에 옮긴다.
- 2분 넘을 명령(VoiceStudio로 문장 수십 개)은 Bash 백그라운드로 돌린다. 6분 영상(문장 108개)은 VoiceStudio로 약 40분 걸렸다(첫 모델 올리기 포함).

## 목소리 규칙

1. 해설은 `narr` 또는 `narr_<이름>`으로 1~2개를 배정한다. `recap`의 `who`를 생략하면 `narr`가 읽는다.
2. 모든 `who`마다 `voices`에 목소리를 배정한다. `_child`·`_young`·`_adult`·`_old`로 끝나는 배역은 `base`에 원래 배역 id를 적는다. 최종본에서 서로 다른 배역 또는 `base` 묶음은 각기 다른 복제 프로필을 쓴다.
3. 최종본은 같은 VoiceStudio 엔진의 복제 프로필만 쓴다. `model`·`id`·정수 `seed`를 명시한다. `VOICESTUDIO_URL`(기본 `http://127.0.0.1:3900`)에서 프로필을 조회해 없거나 복제 프로필이 아니면 멈춘다. `--provider say` 또는 전부 `say`인 배정표로는 배역 구조만 검사하고 임시판을 만든다.
4. 문장별 지시는 속도만 반영한다. 다시 만들 때도 같은 프로필과 `seed`를 쓰고 속도에 `0.96^take`를 곱한다.

## 목소리 배정 (`voices`)

1. `cast.mjs`로 복제 프로필 후보를 본다. 따로 모은 배역표가 있으면 `--pool <배역표.json>` 또는 `STORY_VIDEO_CAST_POOL`로 지정한다. 배역표의 `roles`에 배역별 후보 배열을 두고 각 후보에 `id`·`name`·`model`·`memo`를 적는다.
2. 해설 1~2개를 고르고 주요 배역마다 성별·나이대가 다른 복제 프로필을 골라 `voices`에 적는다. 복제 목소리는 견본의 말투를 그대로 따라 하므로, 주인공과 해설은 차분하고 호감 가는 견본을 고른다. 이름·`memo`가 외치는·과장된·코믹한·거친 견본은 그런 성격의 조연에만 쓴다.
3. 주요 배역마다 대사 두세 줄을 시험 낭독해 사용자에게 들려주고(하위 세션이면 파일 경로를 보고하고), 확인을 받은 뒤 프로필과 `seed`를 확정해 `voices`에 남긴다. 호들갑스럽거나 공격적으로 들린다는 말을 들으면 그 배역의 견본을 바꾼다.

```json
{
  "narr": { "provider": "vstudio", "model": "omnivoice", "id": "<해설 복제 프로필 id>", "seed": 7 },
  "jun":  { "provider": "vstudio", "model": "omnivoice", "id": "<준 복제 프로필 id>", "seed": 21 }
}
```

선택 칸은 `name`·`style`·`speed`·`lang`·`base`다. 복제 목소리의 `style`은 비워 두기를 권한다(`low pitch` 같은 태그가 복제 음색을 바꿀 수 있다). `VOICESTUDIO_URL`과 `STORY_VIDEO_CAST_POOL`은 음성 환경 파일(`STORY_VIDEO_VOICE_ENV`로 지정)에도 적을 수 있다.

| 공급자 | 비용 | 사용권·주의 |
|---|---|---|
| `vstudio` (VoiceStudio 앱) | 무료, 로컬. 앱이 켜져 있어야 한다 | 최종본은 복제 프로필을 쓴다. OmniVoice 가중치는 CC-BY-NC(비상업)다. |
| `say` | 무료, 로컬 | 임시판으로 시범·대본을 확인할 때 쓴다. |

`examples/mini/story.json`에 적힌 복제 프로필 id는 작성자 환경의 VoiceStudio에만 있다. 다른 환경에서 예시를 렌더할 때는 `--provider say`를 쓴다.

## 연기 지시가 반영되는 정도

`lines[].direction`·`direction_en`에서 속도를 뽑는다. 실제로 반영한 값과 `take`가 캐시 해시에 들어간다. 반영 못 한 지시는 명령 끝에 표시한다.

| 공급자 | 문장형 지시(감정·상황·말투) | 속도 | 속삭임 | 높낮이 | 크기 |
|---|---|---|---|---|---|
| `vstudio` 복제 | 반영 못 함 | `speed` | 반영 못 함 | 반영 못 함 | 반영 못 함 |
| `say` 임시판 | 반영 못 함 | `rate` | 반영 못 함 | 반영 못 함 | 반영 못 함 |

## VoiceStudio로 몰아 만들 때

`story-video` 스킬의 「VoiceStudio 규칙 요약」을 따른다: 한 번에 하나, 생성 중에는 음성 인식·렌더 같은 무거운 작업을 같이 돌리지 않음, 엔진이 10GB를 넘으면 엔진만 재시작, 끊기면 같은 명령으로 이어 만들기, 오래 걸리면 `caffeinate -i`. `build.mjs`는 VoiceStudio 문장을 하나씩 순서대로 보낸다.

## 시간표

`story.json`의 `pace`로 기본 쉼과 말 빠르기를 정한다. 생략하면 대본의 `format`이 `shorts`일 때 `fast`, 그 외에는 `normal`이다.

| `pace` | 문장 사이 | 장면 앞 `lead` | 장면 뒤 `tail` | 말 빠르기 | 글자당 어림 |
|---|---|---|---|---|---|
| `normal` | 0.35초 | 0.6초 | 0.8초 | 1.0 | 0.133초 |
| `fast` | 0.12초 | 0.2초 | 0.3초 | 1.25 | 0.15초 |

장면에 `lead`·`tail`을 직접 쓰면 그 값을 쓴다. 말 빠르기는 음성 합성에 적용한다. `voices.<who>.speed`가 있으면 그 값이 우선이다.

장면 길이는 앞 쉼, 합성된 WAV의 실측 길이, 문장 사이 쉼, 뒤 쉼을 더해 정한다. 시각은 1/30초 단위로 맞춘다. `out/timeline.json`의 문장마다 `start`·`end`·`file`이 있고, `out/subtitles.srt`는 같은 시각이다(유튜브 자막 트랙용). 음성은 화면 크기와 상관없이 하나를 쓴다.

기존 쇼츠에서 `pace`를 생략하면 `fast`가 된다. 기존 속도와 쉼을 유지하려면 `"pace": "normal"`을 쓴다. 새 기본값을 쓰거나 `pace`를 바꿀 때는 `voice`를 먼저 돌려 배정표와 시간표를 갱신한다. 별도 배정표를 썼으면 `--voices <원본 배정표>`, 임시판이면 `--provider say`를 붙인다.
