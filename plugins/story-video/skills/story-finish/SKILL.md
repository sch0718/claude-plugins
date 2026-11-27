---
name: story-finish
description: 자막 없는 장면 영상에 한글 자막을 한 번에 입혀 final.mp4와 휴대폰용 축소본을 만들고, 길이·해상도·프레임을 기계로 확인한 뒤, 원본 이야기(수집본·facts·대본)와 비교해 빠진 핵심 사실, 바뀐 사실, 기승전결, 자막·음성·그림 일치, 길이를 검토해 review.md로 남긴다. "자막 입혀줘", "마무리해줘", "완성본 만들어줘", "영상 검토해줘", "원본이랑 비교해줘" 같은 요청이나 story-video 8단계에 쓴다.
---

# 8. 마무리 (자막과 검토)

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다. `<에피소드>`는 `story.json`이 있는 폴더다.

| | |
|---|---|
| 입력 | `out/<format>/scenes/`(자막 없는 장면 영상), `out/timeline.json`, `out/audio.wav`, `story.json`(`facts`), 고른 후보 파일, `out/voice-check.json` |
| 출력 | `out/<format>/final.mp4`, `out/<format>/final-phone.mp4`, `out/<format>/subs/`, `out/<format>/frames/`, `<에피소드>/review.md` |

## 1) 자막 입히기

```bash
node <플러그인>/scripts/build.mjs <에피소드> finish [--format shorts] [--no-phone]
```

- 자막은 문장 음성이 나오는 동안 나온다. 해설은 흰 글씨, 대사는 노란 글씨. wide는 화면 아래 두 줄 이내, shorts는 화면 아래 4분의 1(쇼츠 화면 단추 자리)보다 위에 세 줄 이내.
- 이 Mac의 ffmpeg에는 자막 필터가 없어서 Pillow로 투명 PNG를 그려 `overlay`로 얹는다. 장면마다 한 번만 다시 인코딩한다.
- 결과: `final.mp4`(wide 1920×1080, shorts 1080×1920, 30fps, AAC), `final-phone.mp4`(절반 크기). 유튜브 자막 트랙은 `out/subtitles.srt`.

## 2) 기계 확인

```bash
node <플러그인>/scripts/check.mjs <에피소드> [--format shorts] --frames 3,95,250
```

- 길이·해상도·fps·음성 트랙, timeline 총길이와의 차(0.5초 넘으면 문제), shorts면 3분 이하인지 나온다.
- `--frames`의 시각마다 `out/<format>/frames/t<초>.png`와 그 시각에 보여야 할 자막이 나온다. 시각은 장면 가운데·문장 가운데로 고른다(`out/timeline.json`의 `start`·`end`). PNG를 Read로 열어 한글이 깨지지 않는지, 자막이 정해진 줄 수 안인지, 자막이 그 장면 그림 위에 있는지, 쇼츠면 자막·인물 얼굴이 화면 아래 4분의 1에 걸리지 않는지 본다.

## 3) 원본과 비교 검토 (`review.md`)

`story.json`의 `facts`, 고른 후보 파일(`collect/…/<번호>.md`), 대본(`lines`)을 나란히 놓고 본다. 하위 세션이면 이 세션이 쓰고, 상위 세션은 그것을 검수한다.

```markdown
# <제목> ep01 검토 (<날짜>, <format>)

## 결과
통과 | 고칠 것 있음

## 핵심 사실
| # | 사실 | 대본에 있나 | 맞게 전하나 | 비고 |
|---|---|---|---|---|
- 빠진 사실, 바뀐 사실(`changed`로 밝히지 않은 것), 원본에 없는데 사실처럼 단정한 것

## 구성
- 기승전결 비트가 다 있나, 첫 15초(쇼츠 3초) 안에 궁금증을 거나, 여러 편이면 끝이 다음 편으로 끄나

## 자막·음성·그림
- check.mjs 결과(길이, 해상도, 차이)
- 낭독 검증: 통과 n/N, 사람 확인 목록(`out/voice-check.md`)
- 본 프레임과 그때 자막, 그림이 장면과 맞는지
- 그림 속 글자·말풍선, 인물 모습 일관성(`contact.jpg`)

## 길이
- wide 5~10분 / shorts 3분 이하, 실제 길이

## 출처·권리
- `sources.md`, 실화가 불확실하면 「전해지는 이야기」로 밝혔나, 실존 일반인 신상이 없나

## 고칠 것
- 단계(대본·낭독·그림), 장면 id, 무엇을
```

고칠 것이 있으면 그 단계로 돌아가 고치고 다시 이 단계를 돈다. 대본을 고치면 그 문장만 음성이 다시 만들어진다(캐시).

## 끝내기 전에

- **음성은 사람이 들어 보기 전에는 확인되지 않은 것이다.** 보고와 `review.md`에 「음성은 들어 보지 못함」을 적고, 사용자에게 `final-phone.mp4`를 들어 봐 달라고 한다.
- 디스크가 빠듯하면 `final.mp4`를 확인한 뒤 중간 파일(`out/<format>/scenes/`, `subs/`)을 지워도 된다고 알린다(다시 렌더할 때 다시 만들어진다).
- 업로드는 narrated-video 스킬의 업로드 절을 쓴다.
