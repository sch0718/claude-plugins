# offline-doc-bundle

마크다운 문서를 인터넷 없이 열리는 배포본(HTML 묶음, 목차, 발표용 PDF)으로 만드는 Claude Code 플러그인(마켓플레이스 `sch0718/claude-plugins`의 `plugins/offline-doc-bundle`, PolyForm Noncommercial). 스킬은 `skills/offline-doc-bundle/SKILL.md`, 뒷정리·검증 스크립트는 `skills/offline-doc-bundle/scripts/bundle.py`(표준 라이브러리만), 참고 문서는 `skills/offline-doc-bundle/references/`다. 마크다운→HTML·PDF 변환 자체는 이 플러그인이 하지 않는다.

- 자동 시험은 없다. `bundle.py`를 고쳤으면 임시 폴더에 샘플 HTML 몇 개(문서 간 `.md` 링크, 이미지 하나 포함)를 만들고 `link-fix`, `exclude`, `index`, `verify` 순으로 돌려 본다. `link-fix`를 두 번 돌려도 목차 바가 중복되지 않는지, `verify`가 일부러 깨 둔 링크를 잡는지 확인한다. 저장소 루트의 `python3 -m unittest discover -s tests`도 통과해야 한다.
- `SKILL.md`의 `description`은 스킬 목록에서 잘리지 않도록 짧게 유지한다.
- 릴리스할 때 `.claude-plugin/plugin.json`의 `version`과 저장소 루트 `.claude-plugin/marketplace.json`의 offline-doc-bundle 항목을 함께 고친다.
