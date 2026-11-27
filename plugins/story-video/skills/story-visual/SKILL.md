---
name: story-visual
description: 이야기와 낭독 시간표에 맞춰 화면을 만든다. 그림 방식(일러스트·웹툰풍·실사풍 등)과 톤을 프리셋에서 정해 story.json의 style로 쓰고, Codex CLI 이미지 생성으로 장면마다 그림 한 장(가로 1536×1024, 쇼츠는 세로 1024×1536)을 그려 모아보기로 확인한 뒤, 장면마다 느린 확대·이동(Ken Burns)과 페이드를 넣은 자막 없는 영상 video.mp4를 만든다. "장면 그림 그려줘", "그림체 정해줘", "이 장면만 다시 그려줘", "쇼츠용 세로 그림", "화면 만들어줘" 같은 요청이나 story-video 7단계에 쓴다.
---

# 7. 화면 (그림과 움직임)

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다. `<에피소드>`는 `story.json`이 있는 폴더다.

| | |
|---|---|
| 입력 | `story.json`(`scenes[].art`·`cast`·`kb`, `characters`, `format`), `out/timeline.json`(장면 길이) |
| 출력 | `story.json`의 `style`, `art/<format>/<장면 id>.png`·`contact.jpg`·`prompts/`·`logs/`, `out/<format>/scenes/<id>.mp4`, `out/<format>/video.mp4`(자막 없는 영상, 음성 포함) |
| 다음 | `story-finish`가 자막을 입히고 검토한다 |

## 1) 그림 방식 정하기

사용자에게 이야기 분위기를 말하고 아래 프리셋 중 두세 개를 권해 고르게 한다(하위 세션이면 brief의 것). 고른 문장을 `story.json`의 `style`에 쓴다. 문장은 고쳐 써도 되지만 기존 작가·작품·스튜디오 이름은 넣지 않고 끝에 `original style`을 둔다.

| 프리셋 | 어울리는 이야기 | `style` 예 |
|---|---|---|
| 수채 일러스트 | 감동, 일상, 옛이야기 | `Soft painterly digital illustration, muted palette, gentle film grain, cinematic wide composition, original style` |
| 웹툰풍 | 썰, 코믹, 반전 | `Clean Korean webtoon-like digital art, bold clean line art, flat cel shading, bright saturated colors, expressive faces, original style` |
| 실사풍 | 실제 사건, 다큐 | `Photorealistic cinematic still, natural lighting, shallow depth of field, 35mm film look, muted realistic colors, original style` |
| 어두운 유화 | 괴담, 미스터리 | `Dark oil painting style, heavy brush strokes, deep shadows with a single warm light source, eerie quiet mood, original style` |
| 판화·민화풍 | 민담, 전설 | `Traditional woodblock print inspired illustration, textured paper, limited earthy palette, flat perspective, original style` |
| 동화 그림책 | 아이도 보는 이야기 | `Warm children's picture book illustration, soft gouache texture, rounded shapes, gentle pastel colors, original style` |

톤은 `style` 뒤에 색·빛·시대를 더해 맞춘다(예: `cold winter palette of frost blue and candle gold`, `1990s Seoul`). 실존 인물은 그리지 않는다. 실제 사건이라도 인물은 지어낸 얼굴로 그린다.

## 2) 장면 그림

```bash
node <플러그인>/scripts/art.mjs <에피소드> --dry-run             # 요청문만 art/<format>/prompts/에 쓴다
node <플러그인>/scripts/art.mjs <에피소드>                       # 없는 그림을 모두 그린다(동시에 3개)
node <플러그인>/scripts/art.mjs <에피소드> --only s03,s07 --force   # 그 장면만 다시
node <플러그인>/scripts/art.mjs <에피소드> --format shorts       # 쇼츠용 세로 그림(art/shorts/)
node <플러그인>/scripts/art.mjs <에피소드> --sheet-only          # 모아보기만 다시
```

- 장면마다 빈 작업 폴더에서 `codex exec -m gpt-6.2-sol`을 부르고, 나온 그림을 크기에 맞춰 `art/<format>/<id>.png`로 옮긴다. `codex`가 PATH에 없으면 `CODEX_BIN`에 경로를 준다.
- 한 장에 40초~2분. 장면이 10개를 넘으면 Bash 백그라운드로 돌린다. 30장 넘게 그리기 전에 사용자에게 장수를 알린다(Codex 사용량).
- 요청문에는 매번 같은 틀이 들어간다: 「그림 한 장만 저장」, 금지(글자·숫자·간판 글씨·말풍선·자막·로고·서명·워터마크, 기존 작품 모방, 실존 인물), 그 장면 `cast` 인물의 `look`, 장면의 `art`, `style`. 쇼츠는 세로 구도와 「중요한 것은 화면 가운데~위쪽」이 더해진다(아래쪽은 자막과 쇼츠 화면 단추가 가린다).
- 같은 대본으로 wide와 shorts를 다 만들면 그림만 크기별로 따로 그린다. 음성·시간표는 같이 쓴다.
- 0.1.0 작업 폴더의 `art/<id>.png`는 wide 그림으로 그대로 쓰인다.

### 확인

1. `art/<format>/contact.jpg`를 Read로 본다.
2. 장면마다: 글자·말풍선이 섞였는지, 인물 수·옷·머리가 `look`과 맞는지, 장면 내용이 대본과 맞는지, 화풍이 한 편 안에서 튀지 않는지.
3. 걸린 장면은 `art` 문장을 고치고(「the sign is blank」, 「only one person」) `--only <id> --force`로 다시 그린다. 세 번 넘게 안 되면 구도를 바꾼다(글자가 잘 생기는 간판·책·화면을 뺀다).
4. 그림은 커밋하지 않는다. Codex는 원본을 `~/.codex/generated_images`에 따로 보관한다. 몇백 장이면 몇 GB가 되니 디스크가 빠듯하면 사용자에게 알리고 지울지 묻는다(직접 지우지 않는다).

## 3) 움직임 영상 (자막 없음)

```bash
node <플러그인>/scripts/build.mjs <에피소드> visual [--format shorts] [--jobs 2]
```

- 장면 = 그림 한 장, 길이는 `out/timeline.json`의 장면 길이. 그림이 장면 동안 천천히 확대(`in`)·축소(`out`)·옆 이동(`left`·`right`)하고, 앞뒤 0.3초는 검은색으로 페이드한다.
- 장면 영상은 화질을 높게(중간본) 만들어 두고, 자막은 `story-finish`에서 한 번만 다시 인코딩하며 얹는다. `video.mp4`는 장면 영상을 다시 인코딩하지 않고 이어 붙여 음성을 얹은 미리보기다.
- 그림이 없는 장면은 단색 자리 그림으로 만든다(대본·시간만 먼저 볼 때).
- 6분 영상에 M2에서 몇 분 걸린다. 2분 넘을 것 같으면 Bash 백그라운드로 돌린다.
