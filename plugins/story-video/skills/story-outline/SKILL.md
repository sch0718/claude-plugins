---
name: story-outline
description: 고른 이야기(수집 후보 파일)나 창작 로그라인으로 영상의 뼈대를 짠다. 기승전결 비트, 장면 목록(장면마다 그림 요청과 등장인물), 편 나누기, 원본에서 틀리면 안 되는 핵심 사실 목록 facts를 story.json에 쓴다. 문장(해설·대사)은 story-script가 쓴다. "구성 짜줘", "기승전결로 나눠줘", "장면 나눠줘", "편 나눠줘", "창작 이야기 하나 만들어줘" 같은 요청이나 story-video 3단계에 쓴다.
---

# 3. 스토리 구성

명령 안의 `<플러그인>`은 `${CLAUDE_PLUGIN_ROOT}`(비어 있으면 이 스킬 기본 폴더의 `../..`)다.

| | |
|---|---|
| 입력 | 고른 후보 파일(`collect/…/<번호>.md`)과 `selection.md`, 또는 창작 로그라인 |
| 출력 | `<작업 폴더>/<slug>/ep01/story.json`: `title`·`slug`·`episode`·`episodes`·`format`·`characters`·`facts`·`scenes[]`(`id`·`beat`·`cast`·`kb`·`art`, `lines`는 빈 배열) |
| 다음 | `story-script`가 `lines`를 채운다. `style`·`voices`는 `story-visual`·`story-voice`가 채운다 |

## 순서

1. **화면 크기.** 사용자에게 `wide`(가로 1920×1080, 5~10분, 기본)인지 `shorts`(세로 1080×1920)인지 묻고 `format`에 쓴다. 둘 다 만들 수도 있다: 대본과 음성은 하나로 쓰고 그림만 크기별로 따로 그린다(`story-visual`).
2. **길이와 편.** wide는 편당 5~10분(읽는 글 2,200~4,500자), **shorts는 편당 3분 이하**(유튜브는 3분이 넘으면 쇼츠로 분류하지 않는다, 대략 1,100자 이하)로 맞춘다. 넘으면 편을 나눈다. 각 편도 기승전결을 갖고, 편의 「결」은 다음 편으로 끌고 가는 질문이나 반전으로 끝낸다. 편마다 `ep01/`, `ep02/` 폴더와 `story.json`을 따로 두고 `characters`·`style`·`voices`는 똑같이 복사한다. 2편부터 `recap`에 「지난 이야기」 한두 문장(15초 이내)을 쓴다.
3. **핵심 사실 `facts`.** 후보 파일의 「핵심 사실」과 요지에서 영상이 지켜야 할 사실을 한 줄씩 뽑는다(날짜·장소·숫자·누가 무엇을 했는지·어떻게 끝났는지). 지어낸 이야기에서도 바꾸면 안 되는 설정을 적는다. `story-finish`가 완성 영상을 이 목록과 대조한다. 각색으로 바꾸는 것은 `"changed": "무엇을 왜"`로 같이 적는다.
4. **비트와 장면.** 기(인물·상황·궁금증), 승(일이 커짐), 전(뒤집힘·위기), 결(풀림·여운). 비트마다 장면 2~5개. 한 장면은 그림 한 장, 20~60초(shorts는 8~20초). 첫 장면에서 15초(shorts는 3초) 안에 궁금증을 건다: 결말의 한 장면을 먼저 보여 주거나, 이상한 사실 하나를 던지거나, 질문으로 시작한다.
5. **장면마다** `beat`, `cast`(그림에 나오는 인물 id), `kb`(`in`·`out`·`left`·`right`), `art`(영어 그림 요청: 장소·시간·날씨·인물 동작·분위기)를 쓴다. 글자가 생기기 쉬운 물건(간판, 책 표지, 화면)은 피하거나 「without any writing」을 붙인다. 장면 내용 메모가 필요하면 `note`(한국어)를 둔다.
6. **인물** `characters[]`: `id`, `name`, `look`(영어, 나이대·머리 모양과 색·옷 색·안경 같은 소품까지 구체적으로. 모든 장면 그림 요청에 같은 문장이 들어간다).
7. 사용자에게 비트·장면 목록·facts를 보여 주고 고친다.

## 창작 이야기

수집 없이 시작한다. 로그라인 한 줄(누가, 무엇을 원하는데, 무엇이 막는가)과 분위기·길이를 받는다. 없으면 세 개를 제안해 고르게 한다. `facts`에는 지켜야 할 설정을 적고, `sources.md`에는 「창작 이야기」 한 줄만 쓴다. 기존 작품을 떠올리게 하는 제목·인물 이름·설정은 피한다.

## 예

```json
{
  "title": "마지막 버스의 우산", "slug": "last-bus-umbrella",
  "episode": 1, "episodes": 1, "format": "wide",
  "characters": [{ "id": "jun", "name": "준", "look": "a tired young man in his late twenties, short black hair, round glasses, navy raincoat" }],
  "facts": [
    "자정 넘은 비 오는 밤, 준은 우산이 없다",
    { "fact": "우산은 10년 동안 주인이 찾아가지 않았다" },
    { "fact": "이름표의 글씨는 준의 아버지 글씨다", "changed": "원래 이야기는 어머니. 대사를 줄이려고 바꿈" }
  ],
  "scenes": [
    { "id": "s01", "beat": "기", "cast": ["jun"], "kb": "in",
      "art": "A nearly empty city bus stop at midnight in heavy rain. The young man stands alone ...",
      "lines": [] }
  ]
}
```

완성 예시는 `<플러그인>/examples/mini/story.json`이다.
