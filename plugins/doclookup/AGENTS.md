# doclookup

저장소 문서를 제목 단위 절로 색인해 찾아 주는 Claude Code 플러그인(마켓플레이스 `sch0718/claude-plugins`의 `plugins/doclookup`, PolyForm Noncommercial). 색인·검색·명령은 `src/doclookup/`, 스킬은 `skills/doclookup/`, Read 전에 힌트를 주는 hook은 `hooks/hooks.json`이다.

- 시험: `uv run --with pytest pytest -q`. 끝내기 전에 돌린다.
- `skills/doclookup/SKILL.md`의 `description`과 `when_to_use`는 합쳐서 1,536자 안으로 둔다. 넘는 부분은 스킬 목록에서 잘려 모델이 보지 못한다(공식 문서).
- 릴리스할 때 `pyproject.toml`의 `version`, `.claude-plugin/plugin.json`, 저장소 루트 `.claude-plugin/marketplace.json`의 doclookup 항목, README의 업데이트 안내를 함께 고친다.
