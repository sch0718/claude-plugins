---
name: story-art
description: story.json의 화풍·인물 묘사를 모든 요청문에 고정해, Codex CLI(codex exec)의 이미지 생성으로 장면마다 가로 1536×1024 그림을 한 장씩 그리고 모아보기(contact sheet)로 확인한다. 그림 속 글자·말풍선 금지, 실존 인물·기존 화풍 모방 금지. "장면 그림 그려줘", "이야기 그림 만들어줘", "이 장면만 다시 그려줘", "그림 모아보기" 같은 요청이나 story-video 3단계에 쓴다.
---

# 장면 그림 (Codex 이미지 생성)

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다. `<에피소드>`는 `story.json`이 있는 폴더다.

## 명령

```bash
node <플러그인>/scripts/art.mjs <에피소드> --dry-run          # 요청문만 art/prompts/<id>.txt에 쓴다
node <플러그인>/scripts/art.mjs <에피소드>                    # 없는 그림을 모두 그린다(동시에 3개)
node <플러그인>/scripts/art.mjs <에피소드> --only s03,s07 --force   # 그 장면만 다시
node <플러그인>/scripts/art.mjs <에피소드> --sheet-only       # 모아보기만 다시
```

- 장면마다 빈 작업 폴더에서 `codex exec -m gpt-6.2-sol -s workspace-write --ephemeral`를 부르고, 나온 PNG를 1536×1024로 맞춰 `art/<id>.png`로 옮긴다. 로그는 `art/logs/<id>.log`.
- 이미 있는 그림은 건너뛴다. 실패하면 `--retries`(기본 2)만큼 다시 그리고, 끝내 실패한 장면은 목록으로 알린다.
- 한 장에 40초~2분 걸린다. 장면이 10개를 넘으면 Bash 백그라운드로 돌린다.
- 그림 수가 많으면 Codex 사용량이 든다. 30장 넘게 그리기 전에 사용자에게 장수를 알린다.

## 요청문에 고정되는 것

`art.mjs`가 장면마다 같은 틀로 쓴다. 바꿀 것은 `story.json`에서 바꾼다.

- 맨 앞: 「그림 한 장을 `<id>.png`로 저장, 다른 파일은 만들지 않음」.
- 금지: 글자·숫자·간판 글씨·말풍선·자막·로고·서명·워터마크, 기존 작가·만화·애니메이션·게임·영화 모방, 실존 인물.
- `CHARACTERS`: 그 장면 `cast` 인물의 `name: look`을 매번 똑같이 넣는다. 인물 모습을 장면마다 맞추는 장치는 이것뿐이니 `look`을 구체적으로 쓴다(나이대, 머리 모양·색, 옷 색, 안경 같은 소품).
- `SCENE`: 장면의 `art`. `STYLE`: `story.json`의 `style`.

## 확인

1. 다 그리면 `art/contact.jpg`(가로 3칸 모아보기)를 Read로 본다.
2. 장면마다 본다: 글자·말풍선이 섞였는지, 인물 수·옷·머리가 `look`과 맞는지, 장면 내용이 대본과 맞는지, 화풍이 한 편 안에서 튀지 않는지.
3. 걸린 장면은 `art` 문장을 고치고(예: 「the sign is blank」, 「only one person」) `--only <id> --force`로 다시 그린다. 같은 장면을 세 번 넘게 다시 그려도 안 되면 장면 구도를 바꾼다(글자가 잘 생기는 간판·책·화면을 빼는 식).
4. 그림 파일은 커밋하지 않는다.

## 디스크

Codex는 그린 원본을 따로 보관한다(`~/.codex/generated_images`). 몇백 장을 그리면 몇 GB가 쌓이니, 디스크가 빠듯하면 사용자에게 알리고 지울지 묻는다. 직접 지우지 않는다.
