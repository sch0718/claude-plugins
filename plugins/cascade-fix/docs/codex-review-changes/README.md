# codex-review 스킬 변경 기록

cascade-fix의 Codex 검토자는 `codex-review` 스킬의 `scripts/run-review.sh`를 부른다. 원래 `codex-review`에는 cascade-fix가 쓰는 모드가 없어서 스크립트를 고쳐 쓴다. 정본은 이 디렉터리의 플러그인 사본이고, `CF setup`이 개인 스킬(`~/.claude/skills/codex-review/`)과 비교해 승인을 받고 설치한다.

| 파일 | 내용 |
| --- | --- |
| [SKILL.md](SKILL.md) | `codex-review`의 SKILL.md 전체(2026-10-23). 재검토·의견 모드 설명을 포함한다. `CF setup`이 `~/.claude/skills/codex-review/SKILL.md`로 설치한다 |
| [run-review.sh](run-review.sh) | 지금 쓰는 `run-review.sh` 전체(2026-10-21). `CF setup`이 `~/.claude/skills/codex-review/scripts/run-review.sh`로 설치한다 |
| [SKILL.md.diff](SKILL.md.diff) | `codex-review`의 SKILL.md에 재검토·의견 모드 설명을 더한 diff(2026-10-17). 이력으로만 남긴다 |

예전에는 `run-review.sh`도 diff로 보관했다. 그 뒤 다른 변경(`--known` 등)이 쌓여 diff가 지금 스크립트와 맞지 않게 되어, 전체 파일로 바꿨다.

## 바꾼 것

### 재검토·의견 모드 (2026-10-17)

`--recheck <프롬프트 파일>`과 `--opinion <프롬프트 파일>`을 더했다.

- 프롬프트 파일을 stdin으로 `codex exec -s read-only --ephemeral -C ROOT -o out.final -`에 넘긴다. 인자로 넘기면 큰 diff가 인자 길이 상한(ARG_MAX)에 걸린다.
- 기존 `--uncommitted/--base/--commit/--doc` 모드는 바꾸지 않았다.

### 임시 CODEX_HOME에서 스킬 목록 끄기 (2026-10-18)

임시 CODEX_HOME의 `config.toml`에 다음을 더했다.

```toml
[skills]
include_instructions = false
```

- 임시 home에서도 `~/.agents/skills`의 스킬 목록이 Codex 프롬프트에 들어가고 있었다.
- 이 설정으로 프롬프트가 약 37KB에서 약 11KB(실측 11,359바이트)로 줄어 Codex 이용량을 덜 쓴다.
- `tests/test_run_review_recheck.py`의 `test_codex_home_turns_off_skill_list`가 확인한다. 이 테스트는 고치기 전 스크립트에서는 실패한다.

원래 CODEX_HOME(`~/.codex`)을 함께 쓰는 방식은 쓰지 않는다. `--ignore-user-config`를 줘도 전역 AGENTS.md와 플러그인 스킬 목록이 프롬프트에 들어가는 것을 확인했다. 그래서 임시 CODEX_HOME을 유지한다.

### 문서 모드 출력 형식을 파서에 맞추기 (2026-10-21)

`--doc` 프롬프트의 「출력 형식」에 번호 항목, 들여 쓴 `위치:`/`근거:`/`제안:` 줄, 없는 절의 「없음.」, 예시를 더했다. Cursor 리뷰 스크립트(`docs/cursor-review/run-review.sh`의 `output_format`)와 같은 문구다.

- 전에는 3단계 구분과 위치·근거·제안만 요구했다. Codex가 불릿으로 답하면 `parse_codex_doc`이 번호 항목과 `위치:`를 찾지 못해 지적이 0건으로 읽혔다.
- `tests/test_run_review_recheck.py`의 `test_doc_output_format_example_matches_finding_parser`가 확인한다. 이 테스트는 고치기 전 스크립트에서는 실패한다.

## 시험하기

보관한 스크립트로 cascade-fix의 `run-review.sh` 테스트를 돌릴 수 있다. 진짜 Codex 대신 가짜 `codex`를 쓰므로 Codex를 부르지 않는다.

```bash
CASCADE_RUN_REVIEW="$PWD/docs/codex-review-changes/run-review.sh" python3 -m unittest tests.test_run_review_recheck
```

## 남은 위험: refresh token

`run-review.sh`는 `auth.json`을 복사하지 않고, 원본 `~/.codex/auth.json`을 임시 CODEX_HOME에 심볼릭 링크로 연결한다. 모든 호출이 같은 원본을 본다.

- **확인하지 못한 것**: Codex가 토큰을 갱신할 때 어떻게 쓰는지 모른다. 링크를 따라 원본에 바로 쓰면 원본이 최신으로 남는다. 새 파일을 쓰고 이름을 바꾸는 방식이면 임시 home의 링크만 바뀐다. 그러면 원본에는 이미 쓴 refresh token이 남아 로그인이 풀릴 수 있다.
- **확실한 위험**: 호출 둘이 동시에 같은 refresh token으로 갱신하면 한쪽은 이미 쓴 토큰을 쓰게 된다.
- cascade-fix는 두 가지로 이 위험을 줄인다.
  - `RR`을 한 번에 하나씩 돌린다(SKILL.md 「규칙」).
  - 결과가 없거나 비면 실행 로그와 인증 실패 흔적을 보이고 멈춘다(`opinion`·`recheck`).
