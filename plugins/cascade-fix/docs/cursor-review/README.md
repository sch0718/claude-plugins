# cursor-review 스킬

`cursor-review`는 Cursor CLI(`cursor-agent`)에 코드 변경이나 설계·계획 문서의 리뷰를 맡기는 개인 스킬이다. `codex-review`와 모드와 출력 계약이 같다. 정본은 이 디렉터리의 플러그인 사본이고, `CF setup`이 개인 스킬(`~/.claude/skills/cursor-review/`)과 비교해 승인을 받고 설치한다.

## 무엇을 보관하는가

| 파일 | 내용 |
| --- | --- |
| [SKILL.md](SKILL.md) | `cursor-review`의 SKILL.md 전체. `CF setup`이 `~/.claude/skills/cursor-review/SKILL.md`로 설치한다 |
| [run-review.sh](run-review.sh) | `cursor-review`의 `run-review.sh` 전체. `CF setup`이 `~/.claude/skills/cursor-review/scripts/run-review.sh`로 설치한다 |

`codex-review`와 다른 점은 셋이다.

- 전송처가 Cursor(팀 프라이버시 모드)다.
- 격리가 임시 HOME 방식이다. 호출마다 임시 HOME을 만들고 `Library/Keychains` 링크(로그인 유지)와 `.cursor/cli-config.json`(권한 거부 목록)만 둔다. 끝나면 지운다. Shell·Write·WebFetch·MCP와 홈의 민감한 경로 읽기를 거부한다.
- 코드 모드도 `--focus`·`--known`을 받는다. Cursor에는 `codex review`가 없어 스크립트가 diff를 만들어 프롬프트에 넣기 때문이다.

호출은 모두 `cursor-agent -p --trust --mode ask --output-format json --model <id> --workspace <저장소>`이고 프롬프트는 stdin으로 넘긴다. `--approve-mcps`·`--force`·`--yolo`는 넘기지 않는다.

종료 코드는 `0` 성공, `3` 대상 없음, `64` 사용법, `69` `cursor-agent` 없음, `75` 이용 한도 초과, `77` 로그인 필요다. 그 밖에는 CLI 종료 코드를 그대로 쓴다.

## 설치

`CF setup`이 설치한다. 손으로 하려면 저장소 사본을 개인 스킬 위치에 복사한다. 두 번째 명령은 차이가 없으면 아무것도 출력하지 않는다.

```bash
mkdir -p ~/.claude/skills/cursor-review/scripts && cp docs/cursor-review/SKILL.md ~/.claude/skills/cursor-review/ && cp docs/cursor-review/run-review.sh ~/.claude/skills/cursor-review/scripts/
diff docs/cursor-review/SKILL.md ~/.claude/skills/cursor-review/SKILL.md && diff docs/cursor-review/run-review.sh ~/.claude/skills/cursor-review/scripts/run-review.sh
```

## 시험하기

진짜 `cursor-agent` 대신 가짜 실행 파일을 쓰므로 Cursor를 부르지 않는다. 기본으로 이 저장소의 `docs/cursor-review/run-review.sh`를 시험하고, `CASCADE_CURSOR_REVIEW`로 다른 스크립트(예: 설치본)를 줄 수 있다.

```bash
python3 -m unittest tests.test_cursor_review -v
CASCADE_CURSOR_REVIEW=~/.claude/skills/cursor-review/scripts/run-review.sh python3 -m unittest tests.test_cursor_review
```

## 실측

2026-10-18에 진짜 `cursor-agent`(2026.10.16-e3cb207)로 확인했다.

- 임시 HOME 격리가 로그인을 유지한다. 문서 리뷰 한 번의 입력은 7,624토큰이다(격리 전 1.2만~1.4만).
- `--opinion`(ask 모드)에서 `~/.ssh/known_hosts`·`~/.gitconfig` Read는 거부되고 저장소 파일 Read는 된다. 셸·WebFetch·쓰기는 거부되고 MCP는 실리지 않는다.

## 확인하지 못한 것

- 이 저장소의 테스트는 진짜 `cursor-agent`를 부르지 않는다. 가짜 실행 파일은 인자·`$HOME`·설정 파일만 본다. 위 실측은 사람이 따로 한 것이다.
- 이용 한도·인증 실패 문구가 stdout 봉투, stderr, result 중 어디에 나오는지.
