# story-video

창작 이야기, 인터넷 커뮤니티에서 유명한 이야기, 요즘 많이 회자되는 이야기를 5~10분짜리 영상으로 만드는 개인용 Claude Code 플러그인이에요. 이야기 수집부터 기승전결 대본, 장면 그림, 음성·자막, 합성까지 한 편을 끝내요. 긴 이야기는 여러 편으로 나눠요.

- 결과물은 `final.mp4`(1920×1080, 30fps, 한글 자막을 구워 넣음), 휴대폰용 `final-phone.mp4`, 자막 파일 `subtitles.srt`, 출처 목록 `sources.md`예요.
- 그림은 Codex CLI의 이미지 생성으로, 음성은 로컬 VoiceStudio 앱이나 macOS `say`·Edge TTS로 만들어서 따로 드는 비용이 없어요.
- 업로드는 하지 않아요. 올릴 때는 narrated-video 스킬의 업로드 절을 써요.

## 설치

필요한 것: Claude Code, Node.js 22 이상, ffmpeg·ffprobe, [uv](https://docs.astral.sh/uv/)(자막을 그리는 Pillow용), macOS(`say`, `sips`, 한글 글꼴 AppleSDGothicNeo). 그림을 그리려면 Codex CLI가, VoiceStudio 음성을 쓰려면 VoiceStudio 앱이 필요해요.

Claude Code에서:

```
/plugin marketplace add sch0718/claude-plugins
/plugin install story-video@sch0718-plugins
```

## 사용

"이 이야기로 영상 만들어 줘", "요즘 화제인 썰 찾아서 영상으로", "창작 괴담 영상 하나"처럼 요청하면 스킬 `story-video`가 순서를 잡아요.

| 단계 | 스킬 | 하는 일 |
|---|---|---|
| 1 | `story-scout` | 웹 검색으로 후보를 모아 표로 정리하고 출처 목록을 남겨요. 창작 이야기면 로그라인에서 시작해요 |
| 2 | `story-script` | 기승전결로 나누고 길이를 맞춰 `story.json`을 써요. 10분이 넘으면 편을 나눠요 |
| 3 | `story-art` | 장면마다 그림을 한 장씩 그리고 모아보기로 확인해요 |
| 4 | `story-render` | 음성 → 시간표 → 자막 → 장면 영상 → 합성 |

명령으로 직접 돌릴 수도 있어요(`<플러그인>`은 설치된 플러그인 폴더예요).

```bash
node <플러그인>/scripts/check.mjs <에피소드 폴더>                 # 장면·문장 수, 어림 길이, 빠진 것
node <플러그인>/scripts/art.mjs <에피소드 폴더>                   # 장면 그림 (Codex)
node <플러그인>/scripts/build.mjs <에피소드 폴더> all             # 음성·시간표·자막·영상
node <플러그인>/scripts/check.mjs <에피소드 폴더> --frames 3,60   # 길이 확인, 프레임 PNG
```

예시 `examples/mini/story.json`(4장면, 약 30초)은 그림이 없어도 단색 자리 그림으로 렌더돼요.

```bash
node scripts/build.mjs examples/mini all --provider say
```

## 지키는 것

- 수집한 이야기는 줄거리·사실관계만 가져와 새로 써요. 실존 일반인의 실명·신상은 빼거나 바꾸고, 실화라고 단정할 수 없으면 「전해지는 이야기」로 밝혀요.
- 저작권이 살아 있는 소설·웹툰·영화 같은 창작물의 줄거리는 다루지 않아요.
- 그림에는 글자·말풍선·워터마크를 넣지 않고, 실존 인물이나 기존 작품의 화풍을 흉내 내지 않아요.
- VoiceStudio 기본 엔진의 가중치는 비상업 라이선스(CC-BY-NC)예요. 공개하거나 수익을 내는 영상이면 다른 엔진이나 공급자를 골라 주세요.

## 라이선스

[PolyForm Noncommercial 1.0.0](LICENSE)
