# cascade-fix 상세 레퍼런스

[README](../README.md)에서 다루지 않는 세부 동작을 모았습니다. 단계별 실행 절차는 [SKILL.md](../SKILL.md)에 있습니다.

- [무엇을 고칠 수 있나](#무엇을-고칠-수-있나)
- [요구 사항](#요구-사항)
- [처음 한 번: 이 기기 설정](#처음-한-번-이-기기-설정)
- [처음 한 번: 저장소 검증 명령 등록](#처음-한-번-저장소-검증-명령-등록)
- [사용법](#사용법)
- [프로필](#프로필)
- [비용](#비용)
- [결과물](#결과물)
- [CLI 레퍼런스](#cli-레퍼런스)

## 무엇을 고칠 수 있나

| 입력 | `--source` | 설명 |
| --- | --- | --- |
| Claude 리뷰 | `claude-code` · `claude-doc` | 커밋하지 않은 변경, 브랜치 diff, 특정 커밋, 설계·계획 문서를 Claude가 먼저 검토하고 그 지적을 고칩니다. Codex 없이 돕니다. 설계 단계에서는 스펙·계획서 초안을 구현 전에 검토하는 데 씁니다([설계 단계에서 쓰기](#설계-단계에서-쓰기)). |
| PR 리뷰 코멘트 | `pr-comment` | 해결 안 된 리뷰 스레드를 지적으로 읽고 고칩니다. 답글 초안과 resolve 대상도 만듭니다. 재검토 회차는 열지 않습니다. `gh` CLI가 필요합니다. |
| Codex 리뷰 결과 | `codex-code` · `codex-doc` | `codex-review` 스킬의 `run-review.sh` 결과 파일(`*.final`)을 받아 고칩니다. |
| Cursor 리뷰 결과 | `cursor-code` · `cursor-doc` | `cursor-review` 스킬의 `run-review.sh` 결과 파일(`*.final`)을 받아 고칩니다. 형식은 `codex-doc`과 같은 Critical/Warning/Suggestion입니다. |

`*-code`와 `pr-comment`는 코드 모드라 저장소 검증 명령(`tests`)이 등록되어 있어야 합니다. `*-doc`은 문서 모드라 검증 명령 없이 돕니다. 코드 모드는 지적마다 작업자 둘이, 문서 모드는 작성자 하나와 판정자 둘이 붙습니다.

적합하지 않은 일도 있습니다.
- 문서를 처음부터 새로 쓰는 일(초안 작성). 초안이 나온 뒤의 검토-수정은 맡길 수 있습니다.
- 여러 파일에 걸친 리팩터링
- 테스트가 없는 저장소의 코드 수정
- 스펙이 모호해 「맞다」의 기준이 없는 지적

코드 모드에서 보안·인증·삭제 경로(저장소 설정 `sensitive_paths`, 기본 `**/auth*`, `**/security*`, `**/*delete*`, `**/migrations/**`)와 일치하는 파일을 고친 지적은 채택하지 않고 사람 목록으로 보냅니다. 패치가 기존 테스트 파일(`test_*.py`·`*_test.py`·`*.test.*`·`*.spec.*`·`*_test.go`, `tests/`·`test/`·`__tests__/` 아래 파일)에서 줄을 지우거나 고치거나 파일을 지워도 같은 사유로 「기존 테스트 변경」을 붙여 사람 목록으로 보냅니다. 테스트를 약하게 만들어 검증을 통과하는 수정을 막기 위해서입니다. 새 테스트를 더하기만 한 패치는 그대로 채택합니다. 문서 모드에는 이 검사가 없습니다.

코드 모드에서 작업자 둘이 같은 판정(사실)을 내고 둘 다 검증을 통과했지만 패치가 다르면, 기본은 Opus 판가름으로 보냅니다. `init --adopt-on-both-pass`를 주면 판가름 없이 바뀐 줄이 적은 패치를 채택합니다(같으면 작업자 A). 검증 명령이 하나도 돌지 않았거나, 관련 테스트가 없어 테스트를 실제로 돌리지 못했거나, 한쪽 패치가 비었으면 이 옵션을 줘도 판가름으로 갑니다. 보안 경로와 기존 테스트 변경은 이때도 채택하지 않고 사람 목록으로 보냅니다. 효과를 아직 측정하지 않아 내장 `default`에서는 꺼져 있습니다. 내장 `claude-only`와 `local`은 켠 채로 시작합니다.

## 요구 사항

- Claude Code. 여러 에이전트를 띄우는 **Workflow 도구**를 쓸 수 있어야 합니다.
- Python 3.11 이상. 외부 패키지는 필요 없습니다.
- git
- (PR 모드) 로그인된 `gh` CLI
- (선택) Codex를 검토자로 쓰려면 `codex-review` 스킬과 `codex` CLI. 이 스킬은 `run-review.sh`의 `--opinion`·`--recheck` 모드를 씁니다. 원래 `codex-review`에는 없는 모드라 [docs/codex-review-changes/](codex-review-changes/README.md)의 플러그인 사본이 필요하고, [이 기기 설정](#처음-한-번-이-기기-설정)이 그 사본을 `~/.claude/skills/codex-review/`에 설치합니다. 이 스크립트는 임시 CODEX_HOME에서 스킬 목록을 꺼 Codex 프롬프트도 줄입니다. 없으면 그 칸은 fallback(기본 Claude)으로 바뀝니다.
- (선택) Cursor를 검토자나 작업자로 쓰려면 `cursor-review` 스킬(`~/.claude/skills/cursor-review/`, 플러그인 사본은 [docs/cursor-review/](cursor-review/README.md))과 로그인된 `cursor-agent` CLI(`cursor-agent login`). 이 스킬도 setup이 플러그인 사본을 설치합니다. Codex를 작업자로 쓰려면 `codex` CLI와 로그인이 필요합니다.

## 처음 한 번: 이 기기 설정

Claude Code에서 「cascade-fix 설정해줘」(또는 「이 기기에 맞게 cascade-fix 설정」)라고 말하면 시작합니다. 설정하지 않아도 cascade-fix는 돌지만, 설정하기 전에 실행하면 `detect`가 `참고: 이 기기 설정 전이다…` 줄로 알리고 계획 확인에서 「먼저 설정」을 고를 수 있습니다.

흐름은 다음과 같습니다. 판단은 모두 `CF setup`이 하고, 스킬은 묻는 일만 맡습니다.

1. **감지**: 쓸 수 있는 도구(`codex`·`cursor-agent`와 로그인 여부), Codex 모델, 검토 스크립트(`codex-review`·`cursor-review`)가 플러그인 사본과 같은지, 현재 `default_profile`을 읽습니다. 파일을 쓰지 않고 코드도 보내지 않습니다.
2. **질문**: 우선순위(「Claude 한도 아끼기」·「빨리 끝내기」), 프로필, 다른 스크립트를 바꿀지, 외부 도구 동작 확인을 할지 묻습니다.
3. **미리 보기와 쓰기**: 바뀔 내용을 diff로 보여 주고 확인받은 뒤 씁니다.
4. **동작 확인**: 고른 프로필이 외부 도구를 쓰고 승인했을 때만, 그 도구에 시험 문장 하나를 보냅니다. 코드·문서는 보내지 않습니다. 실패하면 그 도구를 뺀 선택지로 다시 고릅니다.

쓰는 것은 다음뿐입니다.

| 파일 | 내용 |
| --- | --- |
| `~/.claude/cascade-fix/repos.toml` | 최상위 `default_profile = "<이름>"` 한 줄(주석·다른 내용은 그대로). 기존 파일은 `repos.toml.bak-<날짜-시각>`로 백업하고 최근 5개만 남깁니다. 이 파일이 없고 예전 위치(스킬 폴더의 `repos.toml`)에 항목이 있으면 그 파일을 복사해 쓰며 예전 파일은 고치지 않습니다. |
| `~/.claude/cascade-fix/setup.json` | 설정 기록(환경, 우선순위, 추천, 고른 프로필, 시험 결과, 백업 경로, 표 버전). 추천 표가 바뀌었는지 알리는 데 쓰입니다. |
| `~/.claude/skills/codex-review/`·`cursor-review/` | 승인했을 때만. 원래 파일은 `<파일>.bak-<날짜-시각>`(`YYYYMMDD-HHMMSS`)으로 남기고 플러그인 사본(`SKILL.md`와 `scripts/run-review.sh`)으로 바꿉니다. |

- **어떤 프로필을 고를까**: 측정 전에는 모든 환경에서 `claude-only`를 권합니다. Codex가 있으면 `local`이 「잠정·미측정」 대안으로 보입니다. 추천은 `cascade/recommendations.toml`이 정하고, 「빨리 끝내기」를 고르면 늘 `claude-only`입니다(지난 측정에서 약 30분, 외부를 섞은 구성은 1.5~3시간).
- **저장소별 예외**: setup은 기기 기본만 정합니다. 외부 도구를 쓰는 프로필에서 어떤 저장소는 코드를 밖으로 보내지 않으려면, 그 저장소의 첫 실행 전송 동의 질문에서 「이 저장소는 항상 Claude만」을 고릅니다(`CF setup --repo-profile claude-only --root <저장소>`가 그 저장소 항목에 `profile = "claude-only"`를 씁니다). 풀려면 repos.toml에서 그 줄을 지웁니다.
- **다시 하기**: 같은 말로 다시 하면 지난 기록과 현재 값이 함께 보입니다. 추천 표가 바뀌어 이 기기 추천이 달라지거나 고른 프로필이 쓰는 검토 스크립트가 플러그인 사본과 다르면, 실행 때 `detect`가 `참고:` 줄로 알립니다.
- **되돌리기**: 「cascade-fix 설정 되돌려줘」라고 말하면 `CF setup --restore`가 가장 최근 백업과의 diff를 보여 주고, 확인받아 repos.toml을 그 백업으로 되돌립니다(되돌리기 전 파일도 새 백업으로 남습니다). 스크립트 백업은 되돌리기 대상이 아니라 `.bak-<날짜-시각>` 파일을 직접 되돌립니다(경로는 감지 출력의 `지난 설정:` 아래 `백업:` 줄에 보입니다). `--restore`는 repos.toml만 되돌리므로 `setup.json`의 `profile`이 실제 값과 달라질 수 있습니다. 다시 설정하면 기록이 갱신됩니다.
- **삭제**: `claude plugin uninstall cascade-fix@sch0718-plugins`로 지웁니다. 플러그인을 지워도 `~/.claude/cascade-fix/`(`repos.toml`·`setup.json`·백업)와 설치한 `codex-review`·`cursor-review`는 남습니다. 다 지우려면 직접 지웁니다.

## 처음 한 번: 저장소 검증 명령 등록

코드를 고치는 모드는 저장소마다 테스트 명령이 등록되어 있어야 합니다. 테스트 없이 합의만으로는 틀린 수정을 걸러낼 수 없기 때문입니다.

**등록은 직접 하지 않아도 됩니다.** 스킬이 `package.json`·`pyproject.toml`·`Makefile`·`tests/`를 보고 후보를 찾아 확인을 받은 뒤 대신 적어 줍니다.

직접 적으려면 `~/.claude/cascade-fix/repos.toml`에 씁니다. 이 파일은 플러그인을 업데이트해도 남습니다.

```toml
[repos."/Users/me/work/my-app"]
tests = ["pytest {files}"]          # 바뀐 파일과 관련된 테스트만 돌리는 명령
full_tests = ["pytest"]             # 적용 뒤 한 번 돌리는 전체 테스트
lint = ["ruff check ."]
format = ["ruff format {files}"]    # 작업자가 바꾼 파일에만 돈다
# build / typecheck / setup / sensitive_paths / doc_snippet_check 도 쓸 수 있다
```

바뀐 파일과 관련된 테스트 파일이 없으면 `{files}`가 든 `tests` 명령 대신 `full_tests`를 돌립니다. `full_tests`도 없으면 그 명령을 「실행 안 함」으로 남기고 검증 통과로 세지 않습니다(같은 패치 합의는 그대로 채택하고 사유에 「테스트가 돌지 않음」을 남깁니다).

전체 형식은 [repos.toml](../repos.toml)의 머리 주석에 있습니다. 다른 파일을 쓰려면 환경 변수 `CASCADE_REPOS_TOML`로 지정합니다.

## 사용법

### 말로 맡기기 (기본)

Claude Code에 평소처럼 말하면 상황에 맞춰 알아서 진행합니다.

```
이번 변경 리뷰하고 고쳐줘
PR 코멘트 반영해줘
docs/plan.md 리뷰 지적 고쳐줘
스펙 초안(docs/superpowers/specs/…-design.md) 검토하고 고쳐줘
Codex 검토 결과(review.final) 반영해줘
```

진행 순서는 이렇습니다.

1. **상황 판정**: `detect`가 저장소를 읽기만 하고 무엇을 처리할지 추천합니다. 판단 순서는 다음과 같습니다.
   - 결과 파일이 있는가
   - 검토할 문서를 지정했는가(`--doc`)
   - PR에 해결 안 된 리뷰 스레드가 있는가
   - 커밋하지 않은 변경이 있는가
   - 기준 브랜치보다 앞선 커밋이 있는가

   바뀐 파일이 모두 문서면 문서 모드로 갑니다. 코드와 문서가 섞였으면 코드 모드로 가고, 문서만 따로 볼 명령을 함께 알려 줍니다. 테스트가 없거나 처리할 것이 없으면 여기서 멈춥니다.
2. **계획 확인 한 번**: 입력·모드·프로필(역할 배정)·검토자와 예상 에이전트 수, 전송 동의 항목을 보여 줍니다. 「진행 / Critical·Warning만 / dry-run」 중에서 고릅니다. 검증 명령 등록이 필요하면 이때 함께 확인합니다.
3. **끝까지 진행**: 리뷰, 작업자 2명, 검증, 판가름, 적용, 재검토를 차례로 돕니다. 단계마다 다시 묻지 않습니다. Workflow를 돌릴 때마다 `CF usage`로 Claude 토큰을 기록합니다([역할별 사용량](#보고서의-역할-절)).
   - 재검토는 실행당 최대 2회입니다(`--max-rechecks`). 재검토의 Suggestion은 보류 후보로 갑니다. Critical이 있거나 Critical·Warning이 3건 이상 남으면 다음 회차를 열고, Critical 없이 2건 이하면 수렴으로 보고 보류 후보로 넘깁니다. 앞 회차와 같은 위치에서 Critical이 다시 나오면 판단 불가로 보냅니다. 마지막 재검토에서 나온 지적은 목록으로만 갑니다.
   - 앞 패치와 같은 파일을 고쳐 적용하지 못한 지적은 다음 회차에서 바뀐 코드로 다시 고칩니다.
4. **보고**: 회차별 표, 역할 배정(「역할 배정」·「역할별 사용량」: 칸별 호출 수와 Claude 토큰·추정 비용), 사람이 볼 목록 네 칸을 보여 줍니다.
   - 반박
   - 설계 결정: 판정자 근거와, 문서 모드면 작성자가 낸 선택지·장단점(작성자가 안 하나를 골라 고쳤으면 그 수정안과 패치 경로)
   - 판단 불가
   - 보류 후보

   커밋 메시지 초안 등 「준비됨」 파일도 함께 알려 줍니다.

중간에 멈추는 경우는 다음뿐입니다.
- 검증 설정이 없을 때
- 리뷰 결과를 해석하지 못할 때
- 작업 중 사용자 저장소가 바뀌었을 때
- PR 재현 명령의 실행 승인이 필요할 때
- 모델 ID가 기대와 다를 때

### 설계 단계에서 쓰기

초안을 쓰는 일은 맡기지 않고, 초안이 나온 뒤 구현에 들어가기 전에 검토-수정을 맡깁니다.

- superpowers를 쓴다면 brainstorming이 스펙을 쓰고 자체 검토를 마친 뒤, 또 writing-plans가 계획서를 쓰고 자체 검토를 마친 뒤에 붙입니다. 이 스킬의 보고를 그다음 사용자 검토 때 함께 봅니다.
- 스펙은 쓰자마자 커밋되는 일이 많아 변경으로 잡히지 않습니다. 「docs/…/spec.md 검토하고 고쳐줘」처럼 문서를 말하면 `detect --doc`으로 판정합니다. 검증 명령 등록은 필요 없습니다.
- 구현 직전의 계획서는 지적이 구체적이라 자동 채택이 많습니다. 초기 스펙은 「정하지 않은 결정」 지적이 많아 「설계 결정」 칸으로 모입니다. 이것이 정상 결과입니다.
- 「설계 결정」 칸에는 선택지와 장단점이 붙습니다. 결정은 사람이 합니다. 고른 대로 문서를 고친 뒤 다시 검토합니다.
- 작성자가 선택지 대신 안 하나를 골라 고쳤으면 「작성자 수정안(채택 안 함)」과 패치 경로가 붙습니다. 그 안을 쓰기로 하면 Claude에게 맡기세요. 패치를 적용하고 다시 검토합니다.

### 직접 부르기

- 플러그인으로 설치했으면 `/cascade-fix:cascade-fix`, 개인 스킬이면 `/cascade-fix`로 부릅니다.
- 옵션을 함께 말하면("Critical만, dry-run으로, 묻지 말고 진행") 계획 확인을 건너뜁니다.

## 프로필

역할 14칸(코드 6칸·문서 8칸)마다 누가 맡을지(Claude·Codex·Cursor), 모델, effort, 대체 실행 대상(fallback)을 프로필 하나로 고릅니다. 프로필을 고르지 않으면 내장 `default`가 돌고, v1.2.0과 같은 배정입니다. 의견·재검토에서 한도·오류가 나면 멈추지 않고 Claude로 대체하는 점만 v1.2.0과 다릅니다([검토 역할](#검토-역할의견재검토)).

### 역할 칸

| 역할 | 코드 모드 칸 | 문서 모드 칸 | 받는 runner |
| --- | --- | --- | --- |
| 첫 검토 | `code.review` | `doc.review` | claude · codex · cursor · auto |
| 작업자 A / 판정자 a | `code.a` | `doc.judge_a` | claude · codex · cursor |
| 작업자 B / 판정자 b | `code.b` | `doc.judge_b` | claude · codex · cursor |
| 작성자 | - | `doc.writer` | claude · codex · cursor |
| 점검자 | - | `doc.checker` | claude · codex · cursor |
| 판가름 | `code.adjudicator` | `doc.adjudicator` | claude |
| 판단 불가 의견 | `code.opinion` | `doc.opinion` | claude · codex · cursor · auto |
| 수정 재검토 | `code.recheck` | `doc.recheck` | claude · codex · cursor · auto |

칸마다 네 값을 둡니다.

| 값 | 뜻 |
| --- | --- |
| `runner` | `claude`·`codex`·`cursor`. 검토 칸(`review`·`opinion`·`recheck`)은 `auto`도 받습니다. `auto`는 init이 정합니다(아래) |
| `model` | claude면 `claude-`로 시작하는 모델 ID. cursor면 Cursor 모델 ID(`claude-`·`gpt-`로 시작하면 오류). codex면 비우거나 모델 ID. 비우면 `~/.codex/config.toml`의 `model`을 씁니다. auto는 Claude로 풀릴 수 있어 비우거나 `claude-`로 시작해야 합니다. 비운 auto 칸이 Claude로 풀리면 `claude-only`의 같은 칸 모델·effort를 씁니다 |
| `effort` | claude·codex만 씁니다(`low`·`medium`·`high`·`xhigh`·`max`). cursor 칸에 적으면 오류입니다 |
| `fallback` | 대체 실행 대상 `{ runner, model, effort }` 하나. 생략하면 내장 `claude-only`의 같은 칸 값입니다. `auto`는 받지 않습니다 |

- `auto`는 init이 정합니다(`--reviewer auto` 규칙). Claude 리뷰로 시작했으면 Claude, 아니면 `codex-review`가 있으면 Codex, 아니면 로그인된 `cursor-review`면 Cursor, 모두 아니면 Claude입니다. 첫 검토 칸의 `auto`는 Claude입니다.
- codex 검토 칸(`review`·`opinion`·`recheck`)은 `model`·`effort`를 비워야 합니다. `run-review.sh`에 그 옵션이 없기 때문입니다. cursor 검토 칸은 `CR --model`로 모델을 넘깁니다. 반대로 codex 작업자 칸은 `effort`를, cursor 작업자 칸은 `model`을 비울 수 없습니다(실행하면 늘 실패해 fallback으로 넘어가므로 종료 코드 5로 막습니다).
- 판가름 칸은 `claude`만 받습니다.
- 프로필 단위 값으로 `adopt_on_both_pass`(참/거짓)가 있습니다. 명령줄에 `--adopt-on-both-pass`를 주면 참이 됩니다.
- 칸의 `runner`만 바꾸면 모델·effort는 새 runner에 맞게 다시 정합니다. claude는 `claude-only`의 같은 칸 값, codex는 모델을 비우고 effort는 작업자 칸이면 `low`·검토 칸이면 비움, cursor는 `--cursor-model`(기본 `composer-2.5`)과 effort 비움입니다. 같은 칸에 `model`·`effort`를 함께 주면 그 값이 먼저입니다.

### 내장 프로필

| 칸 | `default` | `claude-only` | `local` |
| --- | --- | --- | --- |
| 첫 검토(`*.review`) | auto(= Claude Fable 5.2 high) | Claude Fable 5.2 high | Codex |
| 작업자 6칸(`code.a`·`code.b`·`doc.judge_a`·`doc.judge_b`·`doc.writer`·`doc.checker`) | Sonnet 5.5 low(a·judge_a·writer) / Haiku 5.5 low(b·judge_b·checker) | 같음 | `code.a`만 Codex(`~/.codex/config.toml` 모델, low). 나머지는 `default`와 같음 |
| 판가름 | Opus 5.5 high | 같음 | 같음 |
| 의견·재검토 | auto(Claude면 Fable 5.2 high) | Claude Fable 5.2 high | Codex |
| fallback | `claude-only`의 같은 칸 | - | `claude-only`의 같은 칸 |
| `adopt_on_both_pass` | 꺼짐 | 켜짐 | 켜짐 |

- `default`는 프로필을 고르지 않았을 때 도는 값입니다. 옛 옵션·환경 변수만 쓰면 v1.2.0과 같은 배정·출력·작업 파일이 나옵니다.
- `claude-only`는 외부 전송이 없습니다. 전송 동의를 거절하거나 fallback을 생략했을 때, 마지막 대체 대상, `--workers claude`가 모두 이 값으로 돌아옵니다.
- `local`은 측정 전 잠정값입니다. 쓸 수 없는 CLI의 칸은 init이 fallback으로 바꿉니다.

### repos.toml로 덮거나 더하기

`~/.claude/cascade-fix/repos.toml`(또는 `CASCADE_REPOS_TOML`)에 적습니다.

```toml
default_profile = "local"            # 최상위. 저장소 항목에 profile이 없을 때 쓴다

[profiles.local]                     # 내장 local을 칸 단위로 덮는다
adopt_on_both_pass = true
"code.a" = { runner = "codex", effort = "low" }
"code.b" = { runner = "claude", model = "claude-haiku-5-5", effort = "low" }

[profiles.mix-sonnet-cursor]         # 새로 만든다
base = "claude-only"
"code.b" = { runner = "cursor", model = "composer-2.5" }

[profiles.codex-a-cursor-fallback]
base = "claude-only"
"code.a" = { runner = "codex", fallback = { runner = "cursor", model = "composer-2.5" } }

[repos."/Users/me/work/regulations"]
profile = "claude-only"              # 사내규정 원문이 든 저장소는 Claude만
tests = ["pytest {files}"]
```

- 칸 키에는 점이 있어 따옴표로 감쌉니다. 인라인 표는 반드시 한 줄에 적습니다. TOML이 여러 줄 인라인 표를 받지 않아 파일을 읽지 못합니다. `fallback`도 인라인 표 안의 인라인 표로 적습니다.
- 내장 이름과 같은 `[profiles.<이름>]`은 그 프로필을 칸 단위로 덮습니다. 적지 않은 칸은 내장값입니다. 이 표에는 `base`를 쓸 수 없습니다(오류).
- 새 이름은 `base = "<다른 프로필>"`로 시작점을 고릅니다. `base`가 없으면 `default`에서 시작합니다. `base`가 순환하거나 없는 이름이면 오류입니다.
- **`claude-only`는 덮을 수 없습니다.** `[profiles.claude-only]`를 적으면 init이 종료 코드 5로 멈춥니다. 「외부 전송 없이 Claude로」가 늘 지켜지게 하려는 것입니다. Claude 모델·effort를 바꾸고 싶으면 `base = "claude-only"`로 새 이름을 만듭니다(그 프로필에서는 외부 runner도 고를 수 있으므로 안전 경로는 내장 `claude-only`입니다).
- 설정 오류(모르는 프로필·칸 키·값 키, 받지 않는 runner, runner와 모델 불일치, cursor의 effort, codex 검토 칸의 model·effort, 값 타입 오류)는 트레이스백 없이 어느 프로필·칸·키인지 한 줄로 알리고 종료 코드 5로 멈춥니다. 모르는 프로필 이름이면 알려진 이름 목록을 함께 보입니다.
- 견본은 [repos.toml](../repos.toml)에 있습니다.

### 프로필 고르기

다음 순서로 고릅니다.

1. `init`·`detect`·`review`의 `--profile <이름>`
2. 환경 변수 `CASCADE_PROFILE`
3. repos.toml 저장소 항목의 `profile`
4. repos.toml 최상위 `default_profile`
5. 내장 `default`

`init`과 `detect`는 고른 프로필과 출처를 `프로필: <이름> (<출처>)`로 찍습니다. 출처는 `--profile`·`CASCADE_PROFILE`·`repos.toml:repo`·`repos.toml:default_profile`·`builtin`입니다. `init`은 이어서 폐기 예정 알림(`참고: …`), 옛 옵션이 덮은 칸(`덮음: …`), 바꾼 칸(`대체: …`), 이 모드의 칸별 배정(`역할: …`), `전송 동의: …`를 찍습니다. `detect`는 `프로필:`·`덮음:`·`역할:`을 찍고, 바꾼 칸은 `참고: 대체: …`로, 전송 동의는 `확인 필요: 전송 동의: …`로 보입니다. `detect`는 폐기 예정 알림을 찍지 않습니다. 원장에는 `profile`(이름·출처·14칸 확정값·덮은 칸·바꾼 칸·동의 목록)이 남습니다.

### 옛 옵션·환경 변수와의 관계

프로필로 배정을 만든 뒤, **명령줄에 준** 옵션만 해당 칸을 덮습니다.

| 옛 옵션 | 덮는 칸 | 동작 |
| --- | --- | --- |
| `--workers claude` | 작업자 6칸 | 모두 `claude-only` 값 |
| `--workers external` | 작업자 6칸 | 쓸 수 있는 CLI를 보고 A 자리·B 자리에 넣습니다(아래 표) |
| `--cursor-model M` | runner가 cursor인 모든 칸 | 모델을 M으로 |
| `--reviewer X` | `<모드>.opinion`·`<모드>.recheck` | runner를 X로(auto 포함). 첫 검토 칸은 덮지 않습니다 |
| `--review-model M` · `--review-effort E` | `opinion`·`recheck` 중 claude(또는 auto) 칸 | 모델·effort를 M·E로 |
| `--adopt-on-both-pass` | `adopt_on_both_pass` | 참으로 |

`--workers external`의 자리 배정입니다. A 자리는 `code.a`·`doc.judge_a`·`doc.writer`, B 자리는 `code.b`·`doc.judge_b`·`doc.checker`입니다.

| 쓸 수 있는 CLI | A 자리 | B 자리 |
| --- | --- | --- |
| Codex와 Cursor | Codex(`~/.codex/config.toml` 모델, low) | Cursor(`--cursor-model`, 기본 `composer-2.5`) |
| Codex만 | Codex | Claude Haiku(`claude-only` 값) |
| Cursor만 | Cursor | Claude Haiku |
| 둘 다 없음 | Claude Sonnet(init이 알림) | Claude Haiku |

- runner를 정하는 옵션(`--workers`·`--reviewer`와 두 환경 변수)이 겨냥한 칸은 **고정**됩니다. 쓸 수 없는 CLI라고 init이 fallback으로 바꾸지 않습니다(v1.2.0과 같습니다). 모델만 바꾸는 옵션(`--cursor-model`·`--review-model`·`--review-effort`)은 고정하지 않습니다. 덮은 값은 검사를 다시 거치므로, 금지 모델을 넣으면 어느 옵션 탓인지 알리고 종료 코드 5로 멈춥니다.
- `CASCADE_WORKERS`·`CASCADE_REVIEWER`는 **프로필을 명시적으로 고르지 않았을 때만**(내장 `default`로 떨어졌을 때만) 읽습니다. 읽으면 `init`이 `참고: 폐기 예정: CASCADE_PROFILE 또는 repos.toml profile을 쓴다`를 찍습니다(`detect`는 찍지 않습니다). 프로필을 명시했으면(1~4단계) 두 변수를 무시하고 무시했다고 한 줄 알립니다. 값이 잘못됐으면 종료 코드 5로 멈춥니다(v1.2.0은 1).
- 옛 옵션은 덮어쓰기 수단으로 계속 쓸 수 있습니다. 없앨지는 다음 메이저에서 정합니다.

### 전송 동의

외부 runner(Codex는 OpenAI, Cursor는 팀 프라이버시 모드)를 쓰는 칸은 코드·문서를 밖으로 보냅니다. `init`과 `detect`가 `전송 동의:` 줄로 수신처별 칸을 보입니다.

```
전송 동의: A·의견·재검토 → OpenAI / 대체 실행 때 A → Cursor(팀 프라이버시 모드)
```

- 칸의 fallback이 외부 runner면 그 수신처도 같은 줄에 「대체 실행 때 …」로 따로 적고 동의를 받습니다. 주 runner와 수신처가 같아도 적습니다.
- 동의는 수신처 단위이고 저장소마다 한 번 받습니다. 사내규정 원문이 든 저장소는 동의하지 않으면 됩니다.
- `init`과 `CF review`의 `--consent <codex,cursor|none>`이 동의한 수신처를 정합니다. 생략하면 이 프로필이 쓰는 수신처 전부에 동의한 것으로 봅니다. 스킬은 먼저 사용자에게 묻고 일부만(또는 아무것도) 동의했을 때 이 옵션으로 넘깁니다. `detect`는 읽기만 하므로 `--consent`가 없습니다.
- 동의하지 않은 수신처를 쓰는 칸과 fallback은 내장 `claude-only` 값으로 바뀌고 init이 `대체: … — 전송 동의 없음`으로 알립니다. 모두 거절하면(`--consent none`) 그 실행은 Claude만 씁니다.
- 원장의 `profile.consented`는 동의한 수신처 중 실제로 쓰는 것만 담습니다. 실행기는 이 목록에 없는 runner로는 대체 실행하지 않고 `claude-only` 값으로 돕니다.
- `CF review`는 받은 `--consent`를 「다음:」의 init 줄에 그대로 넘깁니다.

### 첫 검토

`CF review --profile …`가 `<모드>.review` 칸을 따릅니다(`--profile`을 생략하면 같은 순서로 고릅니다). `detect`는 첫 검토를 늘 `CF review …`로 권합니다.

- claude 칸: `review-args.json`을 만들고 Workflow를 안내합니다. 모델·effort는 칸 값이고 `--review-model`·`--review-effort`는 이 칸만 덮습니다. Workflow가 끝나면 **`init`보다 먼저** `CF usage --review-dir <review_dir> --workflow <Workflow 실행 id>`를 돌립니다. `init`이 그때 `<review_dir>/claude-usage.json`을 읽어 원장 `profile.first_review.usage`에 넣으므로, `init` 뒤에 돌리면 보고서에 반영되지 않습니다.
- codex·cursor 칸: Claude 작업을 만들지 않고 `RR`·`CR` 명령과 그 결과로 이어 갈 init 줄(`--source codex-*`·`cursor-*`, `--input <review_dir>/review.out.final`)을 찍습니다. 코드 모드의 `RR`은 `--repo`를 무시하고 현재 디렉터리 저장소를 보므로 명령이 `cd <저장소 루트> && RR …`로 나옵니다. 이 칸에 `--review-model`·`--review-effort`를 주면 종료 코드 5로 거절합니다.
- 프로필을 명시했으면 「다음:」의 init 줄에 `--profile <이름>`을 붙이고, 아니면(내장 `default`) 옛 옵션을 그대로 넘깁니다.
- 외부 첫 검토가 한도·오류로 실패하면 `CF review --review-dir <처음 폴더> --fallback-from <실패한 runner> --reason <사유>`로 fallback 칸 작업을 같은 폴더에 만듭니다. fallback 칸을 따라 codex → cursor → claude로 이어 갈 수 있고, 동의 밖이거나 이미 실패한 runner로는 가지 않습니다. 이력은 `<review_dir>/review-meta.json`에 남고 init이 읽어 보고서에 보입니다.
- 로그인 만료는 대체하지 않고 멈춥니다(`codex login`·`cursor-agent login`).

### 검토 역할(의견·재검토)

판가름 뒤 「의견」과 적용 뒤 「재검토」는 `<모드>.opinion`·`<모드>.recheck` 칸이 따로 맡습니다.

| runner | 동작 |
| --- | --- |
| `claude` | Claude 에이전트가 맡습니다. 외부 전송이 없습니다. 기본 모델은 `claude-fable-5-2`(effort `high`)입니다. 판가름을 맡는 Opus와 다른 모델을 써서 같은 판단을 되풀이하지 않게 하려는 것입니다 |
| `codex` | `run-review.sh --opinion/--recheck`로 OpenAI에 보냅니다. 호출은 한 번에 하나씩 돌립니다(동시에 토큰을 갱신하면 로그인이 풀릴 수 있습니다) |
| `cursor` | `cursor-review`의 `run-review.sh --opinion/--recheck`로 Cursor(팀 프라이버시 모드)에 보냅니다. 읽기 전용이고 모델은 기본 `composer-2.5`입니다. 호출은 한 번에 하나씩 돌립니다 |

- 한도(로그의 usage limit)·시간 초과·실행 오류가 나면 그 칸의 fallback으로 자동으로 다시 돕니다. **프로필을 고르지 않은 기본 동작에서도 같습니다.** v1.2.0은 한도에서 멈췄습니다.
- 로그인 만료(로그의 401·refresh token)는 대체하지 않고 멈춥니다. CF는 `run-review.sh`의 종료 코드(75·77)를 보지 못하고 실행 로그 끝 20줄로 원인을 가릅니다. 로그에 인증 흔적 없이 77로 끝났으면 실행 오류로 보고 fallback으로 갑니다. 터미널에서 `codex login`(Cursor는 `cursor-agent login`)을 한 뒤 다시 돌립니다.
- 실행 중 대체는 init이 정한 배정과 따로 `profile.runtime_substitutions`에 남고, 다음 회차에도 이어집니다. 보고서는 의견마다 실제로 맡은 runner를 붙입니다.
- `cursor-review`의 `run-review.sh`는 환경 변수 `CASCADE_CURSOR_REVIEW`, 이 스킬 옆의 `cursor-review/`, `~/.claude/skills/cursor-review/` 순으로 찾습니다.

### 작업자

작업자 칸(`code.a`·`code.b`·`doc.judge_a`·`doc.judge_b`·`doc.writer`·`doc.checker`)을 Codex·Cursor CLI에 맡길 수 있습니다. 측정 계획의 기준을 통과하기 전까지는 내장 `default`가 Claude 작업자이고 외부는 프로필·옵션으로만 켭니다.

- Cursor 모델은 `--cursor-model`로 바꿉니다(기본 `composer-2.5`, 호출당 약 $0.01). Auto·grok-4.7-high는 호출당 약 85배 비싸 쓰지 않습니다. Claude·GPT 계열 Cursor 모델도 쓰지 않습니다.
- 작업자 CLI는 워크트리 전체를 읽습니다. 코드·문서가 OpenAI(Codex)와 Cursor(팀 프라이버시 모드)로 나가므로 [전송 동의](#전송-동의)를 저장소마다 한 번 받습니다.
- 코드 모드에서 저장소마다 처음 한 번, 검증 명령을 각 CLI의 샌드박스 안에서 돌려 봅니다. 결과는 `~/.claude/cascade-fix/sandbox-check.json`에 남습니다. 샌드박스 안에서만 실패하는 CLI는 그 저장소에서 쓰지 않고 그 칸을 fallback으로 바꿉니다(원장 `profile.substituted`에 남습니다). 샌드박스 밖에서도 실패하는 명령은 샌드박스 탓으로 보지 않습니다. 저장소 명령 전에 사소한 명령을 먼저 돌려 점검 호출 자체(사용법 오류 등)가 실패하면 결과를 저장하지 않고, 그 실행에서는 Claude로 돌고 다음에 다시 점검합니다. 점검이 base 워크트리에 남긴 변경은 되돌립니다. 점검은 주 runner만 보고, fallback 전용 CLI는 미리 점검하지 않습니다(실행 중 실패하면 `claude-only` 값으로 돕니다).
- CLI 작업은 `workers` 명령이 한 번에 돌리고 Claude 작업의 Workflow와 동시에 진행합니다. 메인 세션이 읽는 출력은 요약 몇 줄입니다.
- Cursor 쓰기 역할(코드 작업자, 문서 작성자)은 작업마다 `cursor-agent -p --sandbox enabled`로, 읽기 역할(문서 판정자, 점검자)은 `cursor-agent acp` 상주 서버(작업 폴더마다 하나, 최대 4개, 작업마다 새 세션)로 돕니다. 읽기 역할은 셸·쓰기·Web·MCP가 막혀 Read·Grep·Glob만 쓰고, 작업 폴더 밖은 서버를 띄울 때 작업 폴더의 각 상위 폴더에 있던 다른 항목을 Read 거부 규칙으로 막아 읽지 못합니다(ACP는 Cursor의 읽기 경계 설정 `sandbox.readBoundary`를 쓰지 않습니다, 2026-10-23 실측). 규칙은 남길 경로 이름과 처음 갈리는 글자로 만들어 대소문자를 바꾼 경로도 막습니다. 서버를 띄운 뒤 지금 있던 항목과 다른 글자에서 갈리는 새 항목, 유니코드 정규화만 다른 경로, 작업 폴더 안에서 밖을 가리키는 심볼릭 링크는 막지 못합니다. 목록을 읽을 수 없는 상위 폴더가 있거나 경로·형제 이름에 `*`가 있으면(Cursor 규칙에서 `*`는 늘 와일드카드) 규칙을 바르게 만들 수 없으므로 그 작업은 오류로 끝나 fallback으로 돕니다. 2026-10-21 실측에서 `-p`는 시작 단계(팀 플러그인 MCP 설치) 탓에 건당 75~185초였고, ACP 읽기 작업 8건은 동시 4로 26초였습니다. ACP에서는 셸이 샌드박스 없이 돌아 쓰기 역할은 옮기지 않았습니다.
- 한도 초과·로그인 풀림·시간 초과(작업당 900초)·실행 오류·무효 결과가 나면 그 작업만 **그 칸의 fallback**으로 다시 돕니다(칸마다 따로). fallback이 외부 runner면 같은 `CF workers` 호출 안에서 다시 돌립니다. 동의 밖 수신처나, 그 회차에 한도·로그인 실패가 났거나 샌드박스 점검에 실패한 CLI로는 가지 않고 `claude-only` 값으로 돕니다.
- 보고서의 「역할 배정」 절에 대체 실행 내역이, 「역할별 사용량」 표에 칸별 호출 수·대체 실행 사유별 건수·Claude 토큰과 추정 비용·CLI 토큰이 남습니다. ACP 작업은 토큰 사용량이 없어 「기록 없음」으로 셉니다(0으로 세지 않습니다).
- Cursor ACP 격리 점검: 스킬 디렉터리에서 `python3 -m cascade.cursor_acp_check`(누출 0건이면 종료 코드 0)와 `python3 -m cascade.cursor_acp_check --control`(시험이 누출을 잡으면 0). 2026-10-23에 Read·셸·쓰기·검색·홈 아래 읽기·대소문자 변형 경로·Web·MCP 8건이 모두 막혔고 대조는 성공했습니다. 카나리아는 모두 작업 폴더 밖에 두고, 검색은 카나리아 앞부분만 패턴으로 카나리아 폴더 안에서만 해 사용자 파일이 걸리지 않습니다. Cursor CLI가 바뀌면 다시 돌립니다.
- 2026-10-18에 진짜 `codex`·`cursor-agent`로 실측했습니다. Cursor 임시 HOME 격리는 로그인을 유지했고 MCP·Web·셸·쓰기와 홈 민감 경로 읽기를 막았습니다. 외부 작업자 전체 흐름이 Claude 작업자 호출 없이 돌았습니다. 사전 점검은 `codex sandbox -P :workspace`로 하며, 워크트리 안 쓰기만 되고 저장소 루트·홈 쓰기와 네트워크가 막혀 `exec -s workspace-write`와 같았습니다(codex-cli 0.163.0-alpha.10.2).
- 아직 실측하지 않은 것: 이용 한도·인증 실패 문구가 나오는 위치, 작업자 프로필에서 Shell로 민감 경로를 읽는 우회, Codex 동시 실행 때 토큰 갱신 경합, 표본 품질·비용 비교.
- 작업자 프로필은 Shell을 허용하므로 홈 민감 경로의 Read 거부는 Shell(`cat`)로 우회될 수 있습니다. 민감 경로 보호는 cursor-review(검토자, Shell 거부)에서만 실질적입니다.
- 통과 기준(판정 일치율, Opus 호출 수, 대체 실행 비율, 회귀)과 측정 절차는 비공개 측정 기록에 있습니다. 프로필 구성별 측정은 비공개 측정 기록에 있습니다.

### Claude 작업을 `claude -p`로

`--claude-via cli`(`init`·`start`, 또는 판정·적용 전의 `jobs --force --claude-via …`)면 1단계 작업자·점검자(코드 `a`·`b`, 문서 `judge_a`·`judge_b`·`writer`·`checker`)와 판가름(`adjudicator`), Claude 칸일 때의 의견(`opinion`)과 재검토(`recheck`)를 Workflow 대신 `CF workers`가 호출마다 `claude -p`로 돌립니다. `stage1-args.json`은 비고 Claude 작업도 `stage1-cli-args.json`(`runner: claude`)에 들어가므로 `go`가 메인 세션 없이 `workers`를 돌려 판정까지 이어 갑니다. 기본은 `workflow`이고 그 경로는 그대로 남습니다.

- 판가름은 `judge`가 `adj-cli-args.json`(판가름 워크트리, 쓰기)을 만들고 `workers --stage adj`가 `adj-cli.json`에 씁니다. `adjudicate`는 `--results` 없이 이 파일(과 `adj-fallback.json`)을 읽습니다. 실패한 판가름은 Claude 대체 실행(`adj-fallback-args.json`) Workflow로 남습니다. 의견은 `adjudicate`가 `opinion-cli-args.json`(base 워크트리, 읽기)을 만들고 `workers --stage opinion`이 `opinion-cli.json`에 쓰며 `opinion --results`가 그대로 읽습니다. base 워크트리가 없으면 사용자 저장소를 읽게 되므로 Workflow 경로로 둡니다. 의견은 한 base 워크트리를 공유해 읽기 역할 검사가 겹치지 않도록 하나씩 돌립니다. 실패한 의견은 대체 실행 없이 「호출 실패」 문구로 적고 넘어갑니다.
- **재검토(Claude 칸일 때)는 사용자 저장소를 읽지 않고 임시 워크트리를 읽습니다.** `apply`가 적용 뒤 상태(`result_commit`)의 복사를 `wt/recheck-r<N>`에 열고 `recheck-cli-args.json`(읽기, 그 워크트리)을 만듭니다. `workers --stage recheck`가 `_run_isolated`로 돌려 `recheck-cli.json`에 쓰고, `recheck --result recheck-cli.json`이 읽은 뒤 워크트리를 치웁니다. `git add -A`와 되돌림이 이 복사에만 일어나므로 사용자 저장소의 작업 트리·인덱스·스테이징은 바뀌지 않습니다(`--dry-run`은 이미 실행 소유 워크트리인 `result-r<N>`을 읽습니다). 호출이 실패(한도·로그인·무효·시간 초과)하면 같은 워크트리를 읽는 `recheck-args.json` Workflow가 대체 실행으로 남습니다. 사용자 저장소 밖 문서(`--doc`로 준 외부 문서)가 있는 실행은 복사에 그 문서가 없어 Workflow 경로로 둡니다. 복사에는 `.gitignore`로 가려진 파일(`node_modules`·`.env` 등)이 없어 재검토가 그 내용을 볼 수 없습니다.
- **첫 검토(`review`)와 재검토 칸이 Codex·Cursor일 때는 이 경로와 상관없습니다.** 첫 검토는 이 옵션과 상관없이 Workflow입니다. 재검토가 Claude 칸인 프로필(`claude-only` 등)도 재검토가 성공하면 `go`가 Workflow 없이 `report`까지 가고, 재검토 칸이 Codex·Cursor이면 이전처럼 스크립트로 돕니다. Claude 1단계 작업이 실패했을 때의 대체 실행(`*-fallback-args.json`)은 그대로 Workflow입니다.
- 도구: 쓰기 역할은 `acceptEdits`로 `Bash·Read·Edit·Write·Grep·Glob`, 읽기 역할은 `default`로 `Bash·Read·Grep·Glob`만 열고 `Edit·Write·NotebookEdit`을 막습니다(읽기 역할이 Bash로 파일을 바꾸면 기존 검사가 되돌리고 결과를 버립니다). 모든 호출에서 `WebFetch·WebSearch·Agent·Task`와 사용자 MCP·설정(`--setting-sources project --strict-mcp-config`)을 끕니다. 같은 목록을 `--tools`로도 주어 모델에 보이는 도구 정의를 줄입니다(권한 규칙 `--allowedTools`·`--disallowedTools`와 별개라 둘 다 둡니다). Haiku 5.5 low 7회 호출(2026-10-30)에서 `--json-schema` 출력이 정상이었고, 읽기 역할은 `Edit`을 쓰지 못했으며 Bash 쓰기는 `READONLY_CHANGED`로 되돌렸고, 쓰기 역할은 파일을 고쳤습니다. 콜드 호출의 캐시 쓰기는 약 30.4k → 8.9k 토큰이었습니다. 웜 호출의 효과, Sonnet 5.5, 실제 점검 프롬프트는 재 보지 않았습니다. 쓰기 역할에서 `NotebookEdit`은 이제 모델에 보이지 않습니다(`.ipynb` 수정은 확인하지 않음). 작업 폴더 밖 읽기·쓰기를 막지 못합니다(2026-10-30 실측, 아래). Workflow 서브에이전트가 같은 수준인지는 재 보지 않았습니다. Cursor 읽기 역할에는 위의 작업 폴더 밖 읽기 차단이 있지만 이 경로에는 없습니다. 기본값은 `workflow`로 두며, 끝단 비용 절감을 확인하기 전에는 바꾸지 않습니다.
- 사용량: 호출마다 `claude -p` JSON 봉투의 `usage`·`total_cost_usd`를 `round-N/claude-usage.json`의 `workflows.cli-<stage>-<n>`에 작업별로 씁니다. 다시 돌려도 앞 시도의 행이 남고 실패한 호출의 비용도 들어갑니다. 봉투가 없던 시도는 토큰·비용이 비어 있는 행으로 남아 보고서가 두 칸 모두 「미측정」으로 셉니다. CLI 호출이 실패해 Workflow로 대체 실행한 작업은 호출 횟수만큼 기록이 있어야 측정으로 세므로, 대체 실행분은 `CF usage`로 기록하기 전까지 「외 미측정 N건」으로 드러납니다. 실패한 Claude CLI의 로그인 안내는 `claude /login`입니다. 이 경로는 `CF usage`가 필요 없습니다.
- 모델 ID: 결과의 `model_id`는 요청 모델 그대로(`claude-sonnet-5-5`)입니다. 봉투의 `modelUsage`가 요청 모델을 하나도 담지 않으면 그 결과를 채택하지 않고 `무효`로 대체 실행합니다.
- 실측(2026-10-30, `claude -p` 2.1.311, Haiku 5.5 low, 소규모 호출):
  - 확인한 것: JSON 봉투의 `usage`·`total_cost_usd`를 읽을 수 있고, `modelUsage` 키는 모델 ID 그대로(`claude-haiku-5-5`)라 모델 확인이 맞게 동작합니다. 빈 `CLAUDE_CONFIG_DIR`에서는 `Not logged in · Please run /login`에 종료 코드 1이 나와 `auth`로 분류됩니다. 읽기 역할이 `Bash`로 `printf > note.txt`를 해 워크트리에 쓸 수 있었고, 실제 경로(`_run_isolated`)는 이를 되돌리고 `invalid`로 버렸습니다.
  - 확인한 한계: 읽기 역할이 워크트리 밖 파일을 읽고 내용을 그대로 돌려줬습니다. 워크트리 밖 읽기·쓰기는 막히지 않고, 변경 탐지는 워크트리 안만 봅니다.
  - 비용(`--tools` 적용 전): 호출 1건이 약 $0.006이고, 빈 호출도 캐시 쓰기 약 3만 토큰이 들었습니다. 소규모 단건 호출에서 관찰한 값이며, 여러 호출을 이어 돌릴 때도 같은지는 확인하지 못했습니다.
  - 아직 실측하지 않은 것: 이용 한도 초과 문구(일으키지 않았습니다), 여러 호출을 돌렸을 때의 전체 비용 절감, 호출을 겹쳐 돌릴 때의 동작.
  - 시험해 본 보강: `--settings`로 `sandbox.enabled`와 `autoAllowBashIfSandboxed`를 켜면 Bash의 홈 아래 워크트리 밖 쓰기는 막혔지만(1회 관찰, `operation not permitted`), 밖 읽기는 그대로 됐습니다. `/private/tmp` 아래 쓰기는 막히지 않았습니다(1회 관찰). 연결된 워크트리(linked worktree)의 git 메타데이터나 검증 명령이 작업 폴더 밖에 쓰는 경우에 영향이 있는지는 보지 못해 적용하지 않았습니다.

### 보고서의 역할 절

- 「역할 배정」: 프로필 이름·출처, 이 모드의 칸별 runner·모델·effort·fallback 표, 덮은 칸, 배정 때 바꾼 칸(init·jobs), 첫 검토 이력, 실행 중 대체(칸·사유·회차)와 로그인이 풀린 CLI의 안내. 항상 들어갑니다.
- 「역할별 사용량」: 칸마다 호출 수, 대체 실행 수(사유별), Claude 토큰, Claude 비용(추정), CLI 토큰. 첫 검토·의견·재검토는 실제로 맡은 runner별로 나뉩니다.

  ```
  | 역할 | 호출 | 대체 실행(사유) | Claude 토큰(입력/출력/캐시 읽기) | Claude 비용(추정) | CLI 토큰 |
  ```

  - Claude 토큰·비용 열은 `CF usage`가 Workflow 기록(`~/.claude/projects/<프로젝트>/<세션>/subagents/workflows/wf_*/`)에서 읽어 `round-N/claude-usage.json`에 적은 값입니다. 스킬이 Workflow를 돌릴 때마다 이 명령을 부릅니다.
  - 비용은 **추정**입니다. `CF usage`가 기록할 때 모델별 Anthropic 공개 단가(입력·출력·캐시 읽기·캐시 쓰기)로 작업마다 계산해 저장한 값을 더하고, 보고서가 다시 계산하지 않습니다. 표 아래 「단가 기준:」 줄이 저장된 단가 기준 날짜와 출처를 보입니다.
  - 「미측정」은 그 칸에 Claude 작업이 있는데 `CF usage` 기록이 없는 경우입니다. Workflow 기록 폴더를 읽지 못해도(문서화되지 않은 내부 형식입니다) 실행은 멈추지 않고 `Claude 토큰 미측정: <이유>`로 끝나며 그 칸이 「미측정」이 됩니다. 일부 작업만 기록됐으면 `… 외 미측정 N건`으로 나뉩니다.
  - 「기록 없음」은 CLI 열의 값입니다. Cursor ACP 읽기 역할은 응답에 토큰을 남기지 않아 「기록 없음」으로 적고 0으로 세지 않습니다. 「미측정」(Claude 쪽 기록이 빠짐)과 다릅니다.
  - 표 아래에 합계 줄(`합계: Claude 토큰 입력 … · 출력 … · 캐시 읽기 …, 추정 비용 …`)이 붙습니다. 합계에서 빠진 미측정 작업 수는 줄 끝에 적힙니다.
  - `jobs --force`로 회차를 다시 열면 이전 `claude-usage.json`은 `claude-usage.superseded-<k>.json`으로 옮겨지고, 보고서에 「다시 연 회차의 이전 Claude 비용: …(별도, 합계에 넣지 않음)」 줄이 붙습니다.
  - 옛 실행(`CF usage` 이전)의 결과는 그대로 읽히고 그 열만 「미측정」입니다.
- v1.2.0의 「외부 작업자」·「호출 수와 시간」 절은 이 둘로 대체됐습니다.

## 비용

에이전트 수는 지적 수에 비례합니다. 지적 1건마다 아래만큼 붙습니다.

| 모드 | 작업자 | 추가로 붙을 수 있는 것 |
| --- | --- | --- |
| 코드 | 2 | 판가름 최대 1 |
| 문서 | 3 | 점검자·판가름 최대 1씩 |

판단 불가 1건마다 의견 1이 더 붙고, 재검토는 회차마다 1입니다(Claude·Codex·Cursor 중 칸이 정한 runner). 외부 runner를 쓰면 호출이 Claude 대신 Codex·Cursor로 나뉘고, `init`의 「규모:」 줄이 이를 보입니다. 프로필을 명시했으면 줄 끝에 runner별 합계가 붙습니다(예: `… — Claude 최대 Haiku 1·Opus 1/지적, Codex 1/지적 + 의견·재검토`). 대체 실행은 그만큼 fallback 호출로 돌아옵니다.

- `detect`와 `init`이 예상 개수를 먼저 보여 줍니다.
- 돈 뒤의 실제 사용량과 Claude 추정 비용은 보고서 「역할별 사용량」에 칸별로 나옵니다([보고서의 역할 절](#보고서의-역할-절)).
- 지적이 많으면 `--severity Critical,Warning`으로 먼저 돌립니다. 빠진 등급은 보류 후보로 남고, 나중에 「지금 반영」으로 이어서 처리할 수 있습니다.

## 프로파일링

`CF profile on`과 `CF profile off`는 이 기기의 `repos.toml` 최상위에 `profiling = true` 또는 `false`를 씁니다. 기본값은 꺼짐입니다. `CASCADE_PROFILING=1` 또는 `0`을 주면 설정 파일보다 우선합니다. `CASCADE_REPOS_TOML`로 설정 파일을 지정한 동안에는 `on`과 `off`가 파일 쓰기를 거부합니다. `CF profile status`는 실행 폴더 없이 쓸 수 있고 적용값과 출처를 보여 줍니다.

켜면 `CF report`를 만들 때 `~/.claude/cascade-fix/profile/<run_id>.json`에 사용량을 보관합니다. 저장소 이름, 역할별 요청·실제 모델, 호출 수, 대체 사유별 건수, 토큰·비용과 회차 소요 시간을 남깁니다. 지적 본문·코드·전체 경로는 보관 대상에서 제외하며, 정해진 분류에 없는 대체 사유는 「기타」로 셉니다. 같은 실행을 다시 보고하면 기록을 갱신합니다. 다른 저장소의 실행 ID가 겹치면 파일명에 저장소 경로의 해시 8자리를 붙입니다. 저장에 실패하면 경고를 남기고 보고서는 정상적으로 생성합니다. `CASCADE_PROFILE_DIR`로 기록 폴더를 바꿀 수 있습니다.

- `CF profile report [--since YYYY-MM-DD] [--by role|model|runner|repo|week] [--json]`은 기본으로 역할과 실제 모델을 함께 묶습니다. 날짜 범위에 시작일을 포함하며, 주는 ISO 주차를 씁니다. 대체 시도는 실제 러너에 각각 집계하고 대체 건수는 한 번만 셉니다. 비용 합계와 호출당 비용에는 기록된 비용만 쓰고 「단가 모름」 건수를 따로 표시합니다. 캐시 쓰기 비용 비율은 5분 TTL 단가를 적용한 추정값입니다.
- `CF profile import-sessions [--since YYYY-MM-DD]`는 `~/.claude/projects` 아래의 Workflow 저널에서 cascade-fix 역할과 일치하는 작업을 가져옵니다. 날짜는 저널 수정일을 기준으로 합니다. 저장된 실행에 속한 Workflow는 제외하고, 가져온 작업은 `(가져옴)` 저장소로 집계합니다. 다시 실행하면 `imported-claude.json`을 새로 씁니다. `CASCADE_PROJECTS_ROOT`로 저널을 찾을 폴더를 바꿀 수 있습니다.

## 결과물

모든 상태는 대상 저장소의 `.cascade-fix/<run_id>/`에 남습니다. git 추적에서는 자동으로 빠집니다.

- `ledger.json`: 원장. 중단돼도 `status` 명령으로 이어 갈 수 있습니다.
- `report.md`: 회차별 표, 사람이 볼 목록, 「역할 배정」, 「역할별 사용량」
- `prepared/commit-message.txt`: 커밋 메시지 초안
- `prepared/replies.json`: PR 답글 초안 (PR 모드)
- `prepared/resolve.json`: resolve 대상 (PR 모드)
- `round-N/patch-*.diff`: 작업자·판가름자 패치
- `round-N/claude-usage.json`: `CF usage`가 쓴 작업별 Claude 토큰과 추정 비용, 단가 기준 날짜·출처. 첫 검토는 `<review_dir>/claude-usage.json`에 쓰이고 `init`이 원장에 넣습니다. 회차를 다시 열면 이전 것이 `claude-usage.superseded-<k>.json`으로 남습니다.
- `round-N/decisions-pre-final.json`: `apply`가 전체 테스트를 돌리기 전의 판정. 측정 도구(`eval/score.py --pre-final`)가 읽습니다.
- `round-N/apply.json`의 `final_history`: 전체 테스트 결과 이력(`apply --force`로 다시 돌려도 앞 결과가 남습니다). 측정 도구가 회귀를 셀 때 씁니다.
- `round-N/lossy.json`(코드 모드): 작업자 패치에 UTF-8로 읽히지 않아 바꿔 읽은 바이트가 있었는지. 측정 도구(`eval/adopt_recalc.py`)가 읽습니다.

채택한 패치는 작업 트리에 적용되고 커밋되지는 않습니다. `--dry-run`이면 작업 트리를 건드리지 않고 임시 워크트리에만 적용합니다.

- 적용 뒤 전체 테스트가 실패하면 그 회차의 채택 지적은 판단 불가로 바뀌지만, **이미 적용한 패치는 작업 트리에 그대로 남습니다.** 기준선에서도 실패하는지 다시 돌려 보고서의 「간섭 의심」 절에 「전체 테스트 실패(기준선도 실패)」와 「전체 테스트 실패(간섭 의심)」로 나눠 적습니다. 되돌릴지는 사람이 정합니다(`git diff`로 확인하고 `git restore <파일>` 등으로 되돌립니다).
- 보류 후보의 「지금 반영」은 보고 단계에서 `cleanup` 전에만 고를 수 있습니다(`resume`은 정리한 실행을 받지 않습니다). 정리한 뒤에는 새로 실행합니다.
- 끝나면 스킬이 `cleanup`으로 임시 브랜치와 워크트리를 지웁니다. 원장과 보고서는 남습니다.

## CLI 레퍼런스

스킬이 내부에서 부르는 명령입니다. 직접 쓸 일은 거의 없습니다. `<스킬 경로>`는 플러그인이면 `~/.claude/plugins/cache/sch0718-plugins/cascade-fix/<버전>`, 개인 스킬이면 `~/.claude/skills/cascade-fix`입니다.

```bash
PYTHONPATH=<스킬 경로> python3 -m cascade.cli <명령> [옵션]
```

| 명령 | 하는 일 |
| --- | --- |
| `detect [--input F] [--doc F…] [--pr N] [--no-gh] [--profile P] [--reviewer R] [--workers W] [--cursor-model M] [--json]` | 상황을 판정하고 추천·프로필·규모·전송 동의·멈춤 사유를 보여 줍니다. 읽기만 하므로 `--consent`가 없습니다. 첫 검토는 늘 `CF review …`로 권합니다. `--doc`은 커밋한 문서도 검토 대상으로 지정합니다. `--no-gh`면 PR을 조회하지 않습니다. |
| `review --kind code\|doc (--base B \| --uncommitted \| --commit SHA \| --doc F…) [--focus 관점] [--profile P] [--review-model M] [--review-effort E] [--consent C] [--workers W] [--cursor-model M]` | `<모드>.review` 칸을 따라 첫 검토를 준비합니다. claude 칸이면 Workflow 입력을, codex·cursor 칸이면 `RR`·`CR` 명령을 출력합니다. 외부 칸에는 `--review-model`·`--review-effort`를 줄 수 없습니다. `--workers`·`--cursor-model`은 출력하는 init 줄에 붙습니다. |
| `review --review-dir D --fallback-from codex\|cursor [--reason 사유]` | 외부 첫 검토가 한도·오류로 실패했을 때 처음 폴더 `D`의 이력을 고치고 fallback 칸 작업을 만듭니다. 실패한 runner는 마지막으로 시도한 runner와 같아야 합니다. |
| `init --source S --input X [--doc F…] [--severity …] [--dry-run] [--adopt-on-both-pass] [--max-rechecks N] [--profile P] [--consent C] [--reviewer R] [--workers W] [--cursor-model M] [--review-model M] [--review-effort E] [--claude-via workflow\|cli]` | 실행을 시작하고 지적을 읽습니다. `--claude-via cli`는 1단계 Claude 작업을 Workflow 대신 `CF workers`가 `claude -p`로 돌리게 원장에 기록합니다(기본 `workflow`, [Claude 작업을 `claude -p`로](#claude-작업을-claude--p로)). 프로필을 골라 역할을 배정하고 `프로필:`·`역할:`·`전송 동의:` 줄을 찍습니다. `--consent`는 전송에 동의한 수신처(`codex`·`cursor` 쉼표 구분, 또는 `none`)입니다. PR이면 `--input`에 PR 번호를 주고 `--pr-repo owner/name`·`--me <내 로그인>`을 붙일 수 있습니다. `--input` 대신 `--findings-json F`로 정규화한 지적 목록(`id`·`severity`·`location`·`body`)을 바로 줄 수도 있습니다. |
| `start` (`init`과 같은 옵션) | `init` 뒤 `jobs`까지 이어서 합니다. 1단계 작업이 10개를 넘고 `--severity`가 없으면 `init`에서 멈추고 규모를 사용자에게 묻도록 안내합니다. |
| `go --run-dir D [--workflow-output F] [--workflow W] [--allow-repro F]` | 원장이 가리키는 다음 단계부터 멈출 곳까지 단계 명령(`jobs`·`judge`·`adjudicate`·`opinion`·`apply`·`recheck`·`report`)을 이어 부릅니다. `--workflow-output`은 Workflow 완료 알림의 output-file로, 기다리는 Workflow 결과를 `round-N/`에 저장합니다(기다리는 작업과 다르면 종료 코드 5). `--workflow`를 주면 `usage`도 합니다. Workflow가 필요할 때, `workers`와 Workflow를 동시에 띄워야 할 때, `workers`가 이미 돌 때, 모델 ID 경고가 났을 때, PR 재현 명령 승인이 필요할 때, `report`까지 끝났을 때(`cleanup`은 하지 않음) `멈춤: <이유>`와 `다음:`을 출력하고 멈춥니다. 단계 명령이 0이 아닌 종료 코드로 끝나면 그 코드로 끝납니다. |
| `jobs [--claude-via workflow\|cli]` / `judge` / `adjudicate` / `opinion` / `apply` / `recheck` | 회차 단계(`--run-dir`·`--round`). `jobs --force --claude-via …`는 판정·적용 전에 Claude 작업의 실행 경로를 바꿔 작업 파일을 다시 만듭니다. |
| `workers --stage stage1\|checker\|adj\|opinion` | CLI 작업(Codex·Cursor, `--claude-via cli`면 Claude)을 돌리고 결과를 `stage1-cli.json`(점검자는 `checker-cli.json`, 판가름은 `adj-cli.json`, 의견은 `opinion-cli.json`)에 씁니다. `adj`·`opinion`은 `--claude-via cli`의 Claude 작업만 있습니다. 의견은 대체 실행이 없고, 이 단계의 한도·인증 상태는 같은 단계 기록에서만 물려받습니다. 실패한 작업은 그 칸의 fallback으로 같은 호출에서 다시 돌리고(외부 fallback), Claude로 대체할 작업이 있으면 `*-fallback-args.json`을 만듭니다. 한 번에 하나만 돌립니다(실행 폴더의 `workers.lock`으로 막고, 돌고 있으면 오류로 끝납니다). |
| `resume --choices F` | 보류 후보마다 「지금 반영 / 구현 때 테스트로 / 보류」를 받습니다. 「지금 반영」이 있으면 회차를 엽니다. |
| `usage (--run-dir D --round N \| --review-dir D) --workflow W` | Workflow 기록을 읽어 작업별 Claude 토큰과 추정 비용을 `round-N/claude-usage.json`(첫 검토는 `<review_dir>/claude-usage.json`)에 씁니다. `W`는 Workflow 실행 id 또는 그 기록 폴더 경로입니다. 같은 Workflow를 다시 넣으면 그 몫만 바뀌고 두 번 더해지지 않습니다. 이름표가 작업과 맞지 않는 에이전트는 어느 칸에도 넣지 않고 경고합니다. 기록 폴더를 읽지 못하면 `Claude 토큰 미측정: <이유>`를 출력하고 종료 코드 0으로 끝납니다. 첫 검토는 `init`보다 먼저 돌립니다. |
| `report` / `status` / `cleanup` | 보고서, 다음 할 일, 정리 |
| `setup` | 이 기기 설정. 다음 다섯 동작 중 하나를 고르며 모두 `--json`을 받습니다. 아래 표를 보세요. |

`setup`의 동작입니다.

| 동작 | 하는 일 |
| --- | --- |
| `setup --detect` | 환경(`none`·`codex`·`cursor`·`codex+cursor`), repos.toml 경로, 현재 `default_profile`, 추천(`save-claude`·`fast`), 검토 스크립트의 플러그인 사본 일치 여부, 지난 설정을 보입니다. 읽기만 합니다. |
| `setup --check --profile P` | 프로필 `P`가 쓰는 외부 도구에 경로마다(작업자·검토) 시험 문장 하나를 보냅니다. 한 가지라도 성공이 아니면 종료 코드 6입니다. Cursor 읽기 역할을 쓰면 ACP 격리 점검도 돌립니다. 결과를 `setup-check.json`에 쓰고, `setup.json`의 프로필이 같으면 거기에도 합칩니다. `--dry-run`을 받지 않습니다. |
| `setup --apply (--profile P \| --keep) --priority save-claude\|fast [--replace-scripts codex,cursor] [--dry-run]` | repos.toml의 `default_profile`(`--keep`이면 건드리지 않음)·승인한 스크립트·`setup.json`을 씁니다. `--dry-run`은 diff만 보입니다. |
| `setup --repo-profile P [--root R] [--dry-run]` | 저장소 `R`(기본: 현재 디렉터리의 git 루트) 항목의 `profile`을 `P`로 정합니다. 항목이 인라인 표라 찾지 못하면 쓰지 않고 넣을 줄을 보입니다. |
| `setup --restore [--dry-run]` | repos.toml을 가장 최근 백업으로 되돌립니다. |

- repos.toml을 쓰는 동작(`--apply`·`--repo-profile`·`--restore`)은 `CASCADE_REPOS_TOML`이 잡혀 있으면 종료 코드 5로 멈춥니다(측정용 임시 파일을 기기 설정으로 바꾸지 않으려는 것입니다). `--apply --replace-scripts`는 `CASCADE_RUN_REVIEW`·`CASCADE_CURSOR_REVIEW`가 잡혀 있어도 5로 멈춥니다. 쓴 파일은 `tomllib`로 다시 읽고 프로필을 검사하며, 실패하면 백업으로 되돌립니다.

- `detect`·`review`·`init`은 `--root`로 대상 저장소 루트를 받습니다. 없으면 현재 디렉터리의 저장소입니다. 나머지 명령은 `init`이 출력한 `--run-dir`로 실행을 찾습니다.
- 회차 단계는 이미 한 단계를 다시 하지 않습니다. 다시 하려면 `--force`를 붙입니다.
- 각 명령은 끝에 「다음:」으로 다음에 할 명령을 출력합니다. 단계 상세는 [SKILL.md](../SKILL.md)를 보세요.

종료 코드:

| 코드 | 뜻 |
| --- | --- |
| 4 | 저장소 검증 설정이 없습니다. 후보 명령을 함께 출력합니다. |
| 5 | 리뷰 결과·설정 파일을 해석하지 못했습니다. 프로필 설정 오류(모르는 프로필 이름, 칸 키·값 오류, `[profiles.claude-only]`)와 잘못된 `CASCADE_*` 값도 여기입니다. |
| 6 | 사람이 봐야 합니다. `detect`가 멈출 사유를 냈거나, 작업 중 사용자 저장소(작업 트리·HEAD·현재 브랜치·stash)가 바뀌었습니다. 브랜치 목록만 바뀌었으면 경고만 하고 계속합니다. |

환경 변수:

| 변수 | 뜻 |
| --- | --- |
| `CASCADE_REPOS_TOML` | 검증 설정 파일 경로 |
| `CASCADE_PROFILE` | 역할 프로필 이름(내장 `default`·`claude-only`·`local` 또는 repos.toml에 정의한 이름) |
| `CASCADE_REVIEWER` | (폐기 예정) 기본 검토자(`auto`/`claude`/`codex`/`cursor`). 프로필을 명시하지 않았을 때만 읽습니다 |
| `CASCADE_WORKERS` | (폐기 예정) 기본 작업자(`claude`/`external`). 프로필을 명시하지 않았을 때만 읽습니다 |
| `CASCADE_RUN_REVIEW` | `codex-review`의 `run-review.sh` 경로 |
| `CASCADE_CURSOR_REVIEW` | `cursor-review`의 `run-review.sh` 경로 |
| `CASCADE_SANDBOX_CHECK` | 샌드박스 사전 점검 결과 파일 경로(기본 `~/.claude/cascade-fix/sandbox-check.json`) |
| `CASCADE_CODEX` · `CASCADE_CURSOR_AGENT` | `codex`·`cursor-agent` 실행 파일 경로(작업자 실행기와 로그인 확인이 PATH 대신 씁니다) |
| `CASCADE_GH` | `gh` 대신 쓸 명령 |
