# claude-plugins 작업 지침

- 이 저장소는 공개 개인 플러그인 모음이다. 회사·팀·동료 이름, 회사 저장소 이름, 사내 주소·IP·호스트를 파일 내용·파일 이름·커밋 메시지·브랜치·태그·PR·이슈 어디에도 넣지 않는다.
- 커밋 전에 `tools/scan-private --staged`, push 전에 `tools/scan-private --history`가 훅으로 돈다(`git config core.hooksPath .githooks`). 금지어 목록은 저장소 밖 `~/.config/claude-plugins/denylist.txt`에 있고, 그 내용을 이 저장소나 대화에 옮겨 적지 않는다.
- PR을 만들 때는 제목과 본문을 먼저 `tools/scan-private --stdin`으로 검사한다.
- 커밋 작성자는 GitHub noreply 주소를 쓴다(저장소 로컬 `git config user.email`).
- 플러그인을 더하면 `.claude-plugin/marketplace.json` 항목과 README 표의 설치 명령을 함께 더한다. `tests/test_marketplace.py`가 둘을 맞춰 본다.
- 예전 개별 저장소(cascade-fix, fanout, doclookup)는 이 저장소로 옮겨 왔다. 예전 저장소는 고치지 않는다.
