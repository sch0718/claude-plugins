# 훅

`hooks.json`은 PreToolUse(Read) 훅 하나를 둔다. 설정한 범위의 큰 파일을 `offset`·`limit` 없이 Read하려 하면 Claude에게 조회 명령을 쓰라는 안내를 남긴다. Read는 막지 않는다.

- 켜고 끄기는 저장소 설정의 `[hint]`로 한다. 기본은 꺼짐(`enabled = false`)이고 기준 글자 수는 `min_chars`(기본 20000)다. 꺼져 있어도 훅 명령은 돌지만 아무것도 내보내지 않는다.
- 조건: 설정을 찾았고, `hint.enabled`가 참이고, 파일이 설정의 대상 범위(`[files]`의 glob에서 `exclude`를 뺀 것) 안이고, UTF-8로 읽은 글자 수가 `min_chars` 이상이고, Read에 `offset`·`limit`이 없을 때. 그 밖에는 아무것도 내보내지 않는다.
- 설정은 Read할 파일이 든 체크아웃 기준으로 찾는다(세션의 작업 폴더가 아니다).
- 안내 문구: `이 파일은 <n>자다. 먼저 doclookup search·section·impact로 필요한 범위를 찾고 Read의 offset·limit으로 읽는다.`
- 구현은 `src/doclookup/hint.py`의 `run`이다.

## 출력 형식

막지 않고 Claude에게 글을 보이려면 종료 코드 0으로 아래 JSON 하나를 표준 출력에 낸다.

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "이 파일은 <n>자다. …"}}
```

- `permissionDecision`은 넣지 않는다. 넣지 않으면 일반 권한 확인이 그대로 진행된다. `"allow"`는 권한 확인을 건너뛰고 `"deny"`는 막으므로 둘 다 쓰지 않는다.
- 종료 코드 2는 도구 호출을 막으므로 쓰지 않는다. 실행기가 Python을 못 찾거나 `hint` 명령이 실패해도 Read를 막지 않도록 `hooks.json`의 명령 끝에 `2>/dev/null || true`를 붙였다.
- 종료 코드 0의 JSON이 아닌 표준 출력은 PreToolUse에서 Claude에게 가지 않고 디버그 로그에만 남는다. 그래서 일반 글이 아니라 `additionalContext`를 쓴다.
- `systemMessage`는 사용자 화면에 보이는 경고라 쓰지 않는다.
- `timeout`은 초 단위이고 command 훅의 기본값은 600초다. 조회가 느려져도 Read가 오래 멈추지 않게 10초로 둔다.

## 확인한 출처 (2026-11-05)

- Claude Code 훅 문서 <https://code.claude.com/docs/en/hooks>
  - 「PreToolUse decision control」: `additionalContext`는 "String added to Claude's context alongside the tool result". 출력이 없으면 "the tool call continues through the normal permission flow". `"allow"`는 권한 확인을 건너뛴다.
  - 「JSON output」: `hookSpecificOutput`에는 이벤트 이름을 담은 `hookEventName`이 있어야 한다. JSON으로 제어할 때 의도된 종료 코드는 0이다. `systemMessage`는 사용자에게 보이는 경고다.
  - 「Exit codes」: 대부분의 이벤트에서 종료 코드 0의 표준 출력은 디버그 로그로만 간다(예외는 UserPromptSubmit, UserPromptExpansion, SessionStart, PostModelSwitch). 종료 코드 0인 훅의 표준 오류는 Claude가 보지 않는다.
  - 「Common fields」: `timeout`은 초 단위, command 훅 기본 600초. 공통 입력 필드 `cwd`는 훅이 불릴 때의 작업 폴더다.
- Claude Code 플러그인 문서 <https://code.claude.com/docs/en/plugins-reference> 「hooks」: `hooks/hooks.json`은 이벤트 표를 최상위 `"hooks"` 키로 감싼다. 예시 명령은 `"${CLAUDE_PLUGIN_ROOT}"/scripts/…` 꼴이다.
- Read 도구의 입력 필드 `file_path`·`offset`·`limit`은 훅 문서에 따로 적혀 있지 않다("The tool_input fields depend on the tool"). Read 도구 자체의 입력 스키마에서 확인했다.
