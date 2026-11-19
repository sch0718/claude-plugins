# claude-plugins

sch0718이 만든 Claude Code 플러그인을 한곳에 모은 비공개 마켓플레이스입니다.

## 설치

```
/plugin marketplace add sch0718/claude-plugins
```

필요한 플러그인만 골라 설치합니다.

| 플러그인 | 설치 | 하는 일 |
|---|---|---|
| [fanout](plugins/fanout) | `/plugin install fanout@sch0718-plugins` | 고칠 것들을 하위 세션에 나눠 맡기고 검토·PR·정리까지 진행 |

## 저장소 규칙

- 회사·팀·동료 이름, 회사 저장소 이름, 사내 주소는 파일·경로·커밋 메시지·브랜치 어디에도 넣지 않습니다. 커밋과 push 때 `tools/scan-private`가 검사합니다(아래 「처음 받았을 때」).
- 각 플러그인의 시험은 플러그인 폴더의 README를 따릅니다. 저장소 공통 시험은 `python3 -m unittest discover -s tests`(Python 3.11 이상)입니다.

## 처음 받았을 때

```
git config core.hooksPath .githooks
```

훅은 저장소 밖 `~/.config/claude-plugins/denylist.txt`의 금지어 목록을 씁니다. 목록이 없으면 커밋이 막힙니다. 한 줄에 하나씩 적고, 정규식은 `re:`로 시작합니다.
