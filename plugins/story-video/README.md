# story-video

창작 이야기, 인터넷 커뮤니티에서 유명한 이야기, 요즘 많이 회자되는 이야기를 가로 5~10분 영상, 3분 이하 쇼츠, 10분 세로 영상으로 만드는 개인용 Claude Code 플러그인이에요. 긴 이야기는 여러 편으로 나눠요.

- 결과물은 `final.mp4`(가로 1920×1080 또는 세로 1080×1920, 30fps, 한글 자막을 구워 넣음), 휴대폰용 `final-phone.mp4`, 자막 파일 `subtitles.srt`, 출처 목록 `sources.md`, 원본과 비교한 검토 `review.md`예요.
- 그림은 Codex CLI나 로컬 모델로 만들어요. 최종본은 VoiceStudio의 배역별 복제 목소리, 임시판은 macOS `say`로 읽고, 낭독은 로컬 faster-whisper로 확인해요.
- 업로드는 하지 않아요. 올릴 때는 narrated-video 스킬의 업로드 절을 써요.

## 설치

필요한 것: Claude Code, Node.js 22 이상, ffmpeg·ffprobe, [uv](https://docs.astral.sh/uv/)(자막을 그리는 Pillow, 음성 인식 faster-whisper용), macOS(`say`, `sips`, 한글 글꼴 AppleSDGothicNeo). 그림을 그리려면 Codex CLI나 mflux와 로컬 모델이, VoiceStudio 음성을 쓰려면 VoiceStudio 앱이 필요해요.

Claude Code에서:

```
/plugin marketplace add sch0718/claude-plugins
/plugin install story-video@sch0718-plugins
```

## 사용

"이 이야기로 영상 만들어 줘", "요즘 화제인 썰 찾아서 쇼츠로", "창작 괴담 영상 하나"처럼 요청하면 스킬 `story-video`가 아래 여덟 단계를 차례로 불러요. 단계 스킬은 따로도 쓸 수 있어요.

| 단계 | 스킬 | 하는 일 |
|---|---|---|
| 1 | `story-collect` | 웹 검색으로 후보를 모으고 후보마다 원문 요지·출처·핵심 사실을 파일로 남겨요 |
| 2 | `story-select` | 첫 3초 훅·감정 몰입·댓글 반응·화제성·관심도·영상화 적합도 점수와 위험 감점으로 골라요 |
| 3 | `story-outline` | 기승전결 비트, 장면 목록, 편 나누기, 핵심 사실 `facts`를 `story.json`에 써요 |
| 4 | `story-script` | 해설·대사와 문장마다 연기 지시(`direction`)를 써요 |
| 5 | `story-voice` | 문장을 낭독하고 시간표·자막 시각을 만들어요 |
| 6 | `story-voice-check` | 음성 인식으로 받아 적어 대본과 대조하고, 잘못 읽은 문장을 다시 만들어요 |
| 7 | `story-visual` | 그림 방식을 정하고 장면 그림을 그려 움직임 영상을 만들어요 |
| 8 | `story-finish` | 자막을 입히고 원본과 비교해 검토해요 |

명령으로 직접 돌릴 수도 있어요(`<플러그인>`은 설치된 플러그인 폴더, `<에피소드>`는 `story.json`이 있는 폴더예요).

```bash
node <플러그인>/scripts/check.mjs <에피소드>                  # 장면·문장 수, 어림 길이, 빠진 것
node <플러그인>/scripts/build.mjs <에피소드> voice            # 낭독 → 시간표·자막 시각
node <플러그인>/scripts/voicecheck.mjs <에피소드>             # 낭독 검증
node <플러그인>/scripts/art.mjs <에피소드>                    # 장면 그림
node <플러그인>/scripts/build.mjs <에피소드> visual           # 자막 없는 움직임 영상
node <플러그인>/scripts/build.mjs <에피소드> finish           # 자막 입힌 완성본
node <플러그인>/scripts/check.mjs <에피소드> --frames 3,60    # 길이 확인, 프레임 PNG
```

쇼츠는 `--format shorts`를 붙여요(그림은 `art/shorts/`에 따로, 음성은 같이 써요). 예시 `examples/mini/story.json`(4장면, 약 35초)은 그림이 없어도 단색 자리 그림으로 렌더돼요.

```bash
node scripts/build.mjs examples/mini all --provider say
node scripts/build.mjs examples/mini all --provider say --format shorts
```

## 그림 방식: 게임 컷씬식

`story.json`의 `style`을 `{"mode": "cutscene", "prompt": "…"}`로 쓰면 장면마다 패널 그림 한 장에 움직임과 효과를 얹어요. 쇼츠에 권해요.

- 컷: 한 장면을 문장 경계에 맞춰 3~7초 컷으로 나누고, 같은 그림을 확대·클로즈업·좌우 이동으로 천천히 번갈아 보여 줘요. 장면마다 처음 `!`로 끝나는 문장에서 한 번 짧게 확대(줌 펀치)해요.
- `fx`: 안개(`fog`)·비(`rain`)·눈(`snow`)·붉은 색조(`redmoon`)·어둠(`dark`)·번쩍임(`flash`)·흔들림(`shake`)
- `sfx`: 장면 시작부터 몇 초에 낼 효과음(`sfx/<이름>.wav`). 파일이 없으면 건너뛰어요.
- `anim`: Codex가 2×2 같은 격자 시트로 그리고(`art.mjs --anim`), 칸을 `fps`대로 넘기며 칸 사이를 살짝 섞어요. `style.anim`에 적으면 모든 장면의 기본값이 되고, `anim: false`로 장면마다 끌 수 있어요. 시트가 없으면 컷과 카메라 움직임으로 대신해요. 그림은 편당 장면+시트 30장까지 권해요.
- 쇼츠 자막은 크게, 유튜브 화면 단추를 피해 위 12%·아래 25%·오른쪽 13% 밖에 둬요.

화풍 프리셋 `{"preset": "photoreal"}`(한국 드라마 스틸 느낌의 실사풍)과 `{"preset": "webtoon"}`(한국 웹툰 컬러)은 일반·컷씬 두 모드에서 다 써요(컷씬이면 `"mode": "cutscene"`을 함께 적어요). 인물은 모두 지어낸 얼굴로 그려요.

예시 `examples/mini-cutscene/story.json`(3장면, 약 23초)도 그림 없이 렌더돼요.

```bash
node scripts/build.mjs examples/mini-cutscene all --provider say --format shorts
```

긴 이야기를 일반 쇼츠로 만들 때는 편당 2분 30초~3분으로 나누고, 한 편씩 끝까지 만들어요(`story-outline`).

## 0.6.0에서 바뀐 것

- `art.mjs --engine local`로 디스크에 있는 모델을 써서 장면 그림을 그릴 수 있어요. 모델 설정은 [화면 스킬의 로컬 그림 엔진](skills/story-visual/SKILL.md)에 있어요.
- `style.mode: "motion"`은 장면 그림과 키프레임을 부드럽게 잇고 카메라를 천천히 움직여요. [모션 대본 예시](examples/mini-motion/story.json)처럼 설정하고, 장면 그림을 그린 뒤 `art.mjs ep --keys`로 키프레임을 만들어요.
- 10분 세로는 `"format": "shorts"`와 `"pace": "normal"`을 쓰고, `check.mjs ep --min 540 --max 660`으로 점검해요. 시간표가 있으면 그 길이를 기준으로 삼아요.

## 0.5.0에서 바뀐 것

- 고르기 단계에서 순위표(어디서·언제 찾은 소재인지, 두세 문장 요약, 신선함·반전 포인트, 점수)를 먼저 보여 주고, 사용자가 고를 때까지 구성·대본으로 넘어가지 않아요.
- 점수에 「신선함」을 더했어요(최대 35점). 뻔한 전개는 모으지 않고, 특정 인기 글이 없는 「유형만」 후보는 화제성을 2점까지로 묶어요.
- 화면이 덜 정신없게: 컷 3~7초, 줌 펀치는 장면당 한 번, 시트는 0.7~1.5fps 권장, 느린 시트는 칸 사이를 더 길게 섞어요. 정지 경고는 4초부터예요.
- 주인공·해설은 차분하고 호감 가는 복제 견본을 고르고, 시험 낭독을 사용자에게 들려준 뒤 확정해요. 대사의 `!`는 장면당 하나 정도로 줄여요.
- 자막 줄을 고르게 나눠 끝말 한두 글자만 다음 줄로 넘어가지 않게 했어요. 쇼츠 길이 어림을 실측(글자당 0.15초)에 맞췄어요.

## 0.4.0에서 바뀐 것

- 이야기 고르기: 장르(연애·막장·웃긴 썰·반전 등)를 지정할 수 있고, 점수에 첫 3초 훅·감정 몰입·댓글 반응을 넣었어요. 커뮤니티 썰은 줄거리 틀만 빌려 새로 짓고(각색), 설명란에 각색 고지 문장을 넣어요.
- 빠르기: `story.json`의 `pace`(`fast`·`normal`)로 문장 사이 쉼과 말 빠르기를 정해요. 없으면 쇼츠는 `fast`(쉼을 줄이고 1.25배)예요. 이어 만드는 0.3.0 시리즈는 `"pace": "normal"`을 적어야 음성 캐시와 빠르기가 그대로예요. 쇼츠 대본은 160~175초, 18~26장면으로 빠르게 전개해요.
- 화면: 게임 컷씬식에 장면 안 컷 분할·줌 펀치·흔들림, 여러 장면 기본 시트(`style.anim`)와 칸 사이 섞기를 더했어요. 실사풍·웹툰풍 프리셋(`style.preset`)을 두 모드에서 써요. `check.mjs`가 장면당 컷 수와 2초 넘는 정지 구간을 알려 줘요.

## 0.3.0에서 바뀐 것

- 게임 컷씬식 화면(위 절), 쇼츠 시리즈 편 나누기.
- 해설 배역은 `narr`·`narr_<이름>` 최대 2개, 대본의 모든 `who`에 목소리를 배정해야 해요(없는 배역을 해설 목소리로 대신 읽지 않아요). 나이별 배역(`_child`·`_young`·`_adult`·`_old`)은 `base`를 적어요.
- 최종본은 VoiceStudio 복제 프로필만, 한 엔진(model), 배역끼리 프로필을 나눠 쓰지 않아요. `--provider say`는 임시판이에요. 배역 후보 표는 `node scripts/cast.mjs <에피소드>`.
- 낭독 검증에 「목소리 일관성」(배역별 평균 음높이, 3반음 이탈)이 더해졌어요.

## 지키는 것

- 수집한 이야기는 줄거리·사실관계만 가져와 새로 써요. 실존 일반인의 실명·신상은 빼거나 바꾸고, 실화라고 단정할 수 없으면 「전해지는 이야기」로 밝혀요.
- 저작권이 살아 있는 소설·웹툰·영화 같은 창작물의 줄거리는 다루지 않아요.
- 그림에는 글자·말풍선·워터마크를 넣지 않고, 실존 인물이나 기존 작품의 화풍을 흉내 내지 않아요.
- VoiceStudio 기본 엔진의 가중치는 비상업 라이선스(CC-BY-NC)예요. 공개하거나 수익을 내는 영상이면 상업 사용이 되는 엔진을 골라 주세요. 목소리 복제는 주인 허락이 있는 목소리만 써요.

## 라이선스

[PolyForm Noncommercial 1.0.0](LICENSE)
