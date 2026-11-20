# fanout

대화에서 정리한 「고칠 것들」을 하위 세션에 나눠 맡기고 PR 병합·정리까지 끝내는 Claude Code 플러그인(마켓플레이스 `sch0718/claude-plugins`의 `plugins/fanout`, PolyForm Noncommercial). 스킬 본문은 `skills/fanout/SKILL.md` 하나이고 실행 코드와 시험은 없다. Claude 데스크톱 앱 Code 탭 전용이다.

- 시험은 없다. 끝내기 전에 `skills/fanout/SKILL.md`의 `description`과 `when_to_use`가 합쳐서 1,536자 안인지 확인한다. 넘는 부분은 스킬 목록에서 잘려 모델이 보지 못한다(공식 문서). 저장소 루트에서 `python3 -m unittest discover -s tests`도 돌린다.
- 동작을 바꾸면 `README.md`와 `SKILL.md`의 설명을 함께 고친다.
- 릴리스할 때 `.claude-plugin/plugin.json`의 `version`, 저장소 루트 `.claude-plugin/marketplace.json`의 fanout 항목, README를 함께 고친다.
