---
name: session-move
description: Claude 데스크톱 앱 Code 탭의 세션·하위 세션 연결·예약 작업·backlog·사이드바 그룹과 핀을 같은 기기의 다른 계정으로 옮기거나, 번들로 묶어 다른 기기로 옮긴다. 「다른 계정 세션 가져와」, 「계정 바꿔서 이어가」, 「사용량 한도 때문에 다른 계정으로 옮겨」, 「다른 기기로 세션 옮겨」, 「세션 이전」 같은 요청에 쓴다. 맥락만 이어가려는 요청(「어디까지 했지」)은 handoff 스킬로 넘긴다.
---

# session-move

명령 안의 `<스킬 폴더>`는 이 스킬을 불러올 때 알려 주는 기본 폴더(Base directory)다. CLI는 `<스킬 폴더>/../../bin/session-move`다(아래에서는 `SM`으로 줄인다).

## 지킬 것

- 앱 설정 파일(`claude_desktop_config.json`, `ccd-session-presets.json`)은 고치지 않는다. 그룹·섹션·핀은 앱 도구로 만들고, 프리셋은 목록만 보여 준다.
- 앱 파일을 쓰는 `apply`·`restore`는 앱을 ⌘Q로 끈 뒤 **macOS 터미널 앱**에서 사용자가 실행한다. 앱 안의 Claude는 `--dry-run`까지만 실행한다(앱을 끄면 이 세션도 꺼진다).
- 같은 세션을 두 계정·두 기기에서 동시에 이어가지 말라고 알린다.
- 다른 기기용 번들은 회사 데이터를 기본으로 뺀다. `--include-company`는 사용자가 명시할 때만 붙인다. 번들 전송(AirDrop·USB)은 사용자가 한다.

## 1. 상황 판단

1. `get_session` 도구에 `"self"`를 넘겨 지금 세션의 계정 경로를 보고, `SM scopes`로 계정 폴더와 세션 수·최근 활동을 본다.
2. 같은 기기 계정 전환인지, 다른 기기로 옮기는지 사용자 말로 정한다. 모르면 묻는다.

## 2. 같은 기기

1. `SM export --source <이전 계정ID>/<조직ID>`(생략하면 가장 최근에 쓴 폴더). 출력된 manifest 경로를 기억한다. 새 계정에서 시작했어도 원본은 읽기만 하므로 괜찮다.
2. `SM inspect --manifest <경로> --to <새 계정ID>/<조직ID>`와 `SM apply --manifest <경로> --to <새 계정ID>/<조직ID> --dry-run` 결과를 보여 준다.
3. 새 계정에 Code 탭을 한 번 연 적이 있어야 대상 폴더가 있다. 없으면 그것부터 안내한다.
4. 터미널 앱에서 실행할 명령을 한 줄씩 준다.
   - 시험: `SM apply --manifest <경로> --to <대상> --only sessions --only-session <세션ID>` → 앱을 열어 그 세션이 보이고 이어지는지 확인
   - 전체: `SM apply --manifest <경로> --to <대상>`
   - 출력의 `백업:` 경로를 적어 두게 한다. 되돌리기는 앱을 끄고 `SM restore <백업>`.
5. 앱을 다시 연 뒤 새 계정 세션에서 4절(finish)을 한다.

## 3. 다른 기기

1. 원본 기기: 회사 목록 설정(`~/.session-move/config.toml`의 `orgs`·`repos`·`path_prefixes`)이 있는지 본다. 없으면 만들게 안내한다(없으면 내보내기가 멈춘다).
2. agentmemory를 옮기려면 서버를 멈추게 한다. `SM export --bundle <파일>.tar.gz [--only …|--skip …]` 출력의 「제외」 개수와 경고를 그대로 보여 준다. 항목: sessions, transcripts, schedules, backlog, sidebar, presets, instructions, skills, plugins, mcp, memory, repos.
3. 대상 기기: 이 플러그인을 설치하고(`/plugin marketplace add sch0718/claude-plugins` → `/plugin install session-move@sch0718-plugins`), Code 탭을 한 번 연 뒤 `SM inspect --bundle <파일>`로 내용과 제안된 `--map-path`를 보여 준다.
4. 터미널 앱에서: `SM apply --bundle <파일> --map-path <원본 HOME>=<지금 HOME> [--clone] --dry-run` → `--dry-run` 없이 실행. 백업 경로를 적어 두게 한다.
5. 앱을 연 뒤 4절(finish)을 하고, 출력의 플러그인 설치 명령과 「MCP 정의를 … 에 두었습니다」 안내를 정리해 준다. 번들 파일은 지우게 한다.

## 4. finish (새 계정·새 기기의 세션에서)

manifest(같은 기기) 또는 번들 안 `manifest.json`(다른 기기, `SM inspect`로 확인)을 기준으로 한다. 그룹·섹션·핀 말고는 바꾸지 않는다.

1. `list_groups`·`list_sections`로 지금 있는 것을 본다.
2. manifest의 `groups` 순서대로 같은 이름이 있으면 그것을 쓰고, 없으면 `create_group`으로 만든다. `sections`도 같은 방식으로 `create_section`으로 따로 만든다.
3. `list_sessions`(보관 제외, limit 200)로 지금 보이는 세션만 골라 `move_sessions`로 넣는다. 한 번에 100개를 넘기지 않는다.
4. `pinnedSessionIds` 중 고정되지 않은 것을 `set_pinned`로 고정한다.
5. 예약 작업이 목록에 보이는지 확인한다. 다른 기기에서는 꺼진 상태로 들어오므로 켤 것을 사용자에게 고르게 한다(원본 기기와 두 번 돌지 않게).
6. 프리셋 목록을 보여 주고 앱에서 만들게 한다. 커넥터, Remote Control, PR Auto-fix 감시는 새 계정에서 다시 켜야 한다고 알린다.
7. 그룹별로 manifest 세션 수와 실제로 넣은 수를 표로 보여 주고, manifest에는 있는데 보이지 않는 세션은 제목과 ID를 따로 알린다.
