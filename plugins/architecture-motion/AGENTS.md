# architecture-motion

시스템 구성 변화·요청 흐름·장애 전파를 브라우저에서 재생되는 모션 그래픽(HTML+SVG)으로 만드는 Claude Code 플러그인(비공개 마켓플레이스 `sch0718/claude-plugins`의 `plugins/architecture-motion`, PolyForm Noncommercial). 스킬·엔진·예시·점검 스크립트는 `skills/architecture-motion/`에 있다.

- 시험 자동화는 없다. 끝내기 전에 예시로 점검 스크립트를 돌린다: `(cd skills/architecture-motion/scripts && { [ -d node_modules ] || npm i --no-audit --no-fund; })`, 그다음 `node skills/architecture-motion/scripts/check.mjs skills/architecture-motion/examples/sample-cluster --out <캡처 폴더>`. 저장소 루트의 `python3 -m unittest discover -s tests`도 통과해야 한다.
- `node_modules`와 캡처 폴더는 커밋하지 않는다.
- `skills/architecture-motion/SKILL.md`의 `description`은 스킬 목록에서 잘리지 않게 길이를 지킨다. 결과물은 HTML 묶음뿐이고 MP4·GIF 변환은 넣지 않는다(사용자 결정).
- 릴리스할 때 `.claude-plugin/plugin.json`의 `version`과 저장소 루트 `.claude-plugin/marketplace.json`의 architecture-motion 항목을 함께 고친다.
