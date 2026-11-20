# session-move

Claude 데스크톱 앱 Code 탭의 세션·예약 작업·backlog·사이드바를 같은 기기의 다른 계정이나 다른 기기로 옮기는 Claude Code 플러그인(비공개 마켓플레이스 `sch0718/claude-plugins`의 `plugins/session-move`, PolyForm Noncommercial). 명령은 `bin/session-move`, 코드는 `src/`, 스킬은 `skills/`에 있다(Python 3.11+, macOS).

- 시험: `cd plugins/session-move && python3 -m unittest discover -s tests`. 끝내기 전에 돌린다.
- 스킬 `SKILL.md`의 `description`과 `when_to_use`는 합쳐서 1,536자 안으로 둔다. 넘는 부분은 스킬 목록에서 잘려 모델이 보지 못한다(공식 문서).
- 릴리스할 때 `.claude-plugin/plugin.json`의 `version`, 저장소 루트 `.claude-plugin/marketplace.json`의 session-move 항목, README를 함께 고친다.
