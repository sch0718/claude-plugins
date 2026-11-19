# 측정 표본 형식과 실행 방법

설계 14절의 소급 재생·채점에 쓰는 표본 파일(`eval/sample.jsonl`)의 형식, 모으는 방법, 돌리는 순서를 적는다.

## 한 줄의 형식

한 줄이 지적 하나다. 줄마다 JSON 객체 하나, 빈 줄은 무시한다.

| 키 | 값 |
| --- | --- |
| `id` | 표본 항목 이름. 폴더 이름으로 쓰므로 영문·숫자·`.`·`_`·`-`만 쓴다 |
| `repo` | 지적을 처리한 저장소의 절대 경로 |
| `commit` | 지적이 나온 시점의 커밋. 그 커밋에서 다시 돌린다 |
| `source` | `codex-doc` · `codex-code` · `claude-doc` · `claude-code` · `pr-comment` |
| `doc` | 문서 모드면 문서 경로(저장소 안 절대·상대 경로), 아니면 `null` |
| `finding` | 정규화한 지적(`id`, `source`, `severity`, `location`, `body`, `evidence`, `thread_id`, `outdated`). `id`는 `^[A-Za-z0-9][A-Za-z0-9._-]*$` |
| `truth.verdict` | 그때의 Opus 판정 중 사람이 받아들인 것: `사실` · `아님` · `재현 불가` |
| `truth.accepted_patch` | 사람이 받아들인 수정의 diff. 「아님」이면 `null` |
| `baseline_opus_calls` | 지금 방식에서 Opus가 이 지적에 쓴 호출 수. 지적 1건 = 1 |

예(한 줄로 이어 쓴다. 보기 좋게 줄을 나눴다):

```json
{"id": "be-review-007", "repo": "/Users/me/work/sample-be", "commit": "3f2a9c1d5e7b8a4c6d0e1f2a3b4c5d6e7f8a9b0c",
 "source": "codex-code", "doc": null,
 "finding": {"id": "r1-01", "source": "codex-code", "severity": "Warning", "location": "src/rules/engine.py:88",
             "body": "빈 목록이면 첫 원소를 읽다가 예외가 난다", "evidence": "rules=[] 로 evaluate()를 부르면 IndexError",
             "thread_id": null, "outdated": false},
 "truth": {"verdict": "사실", "accepted_patch": "--- a/src/rules/engine.py\n+++ b/src/rules/engine.py\n@@ -86,3 +86,4 @@\n-    first = rules[0]\n+    if not rules:\n+        return []\n+    first = rules[0]\n"},
 "baseline_opus_calls": 1}
```

`finding.id`는 표본 안에서 겹쳐도 된다. 채점은 항목 `id`로 찾고 항목 안에서 `finding.id`의 마지막 판정을 본다.

## 실행 단위 표본(`run`)

문서 하나를 한 번 실행하면 지적 여러 개가 한 원장에 들어간다. 줄에 `run`을 적으면 `score.py`가 `replay.py`의 `<id>/run/` 대신 그 실행 폴더를 직접 읽는다. 판정은 지적마다 보고, 호출 수·사용량·비용은 같은 `run`을 한 번만 센다(지적별 폴더에 같은 실행을 연결하면 지적 수만큼 겹쳐 잡힌다).

```json
{"id": "plan-a-01", "run": "plan-a-2026-10-22", "finding": {"id": "r1-01"}, "truth": {"verdict": "사실", "accepted_patch": null}}
```

- `run`: `score.py --runs` 폴더 아래의 이름. 그 폴더에 `ledger.json`과 `round-*`가 있어야 한다(실행 폴더 `.cascade-fix/<실행 id>/`를 `cleanup` 전에 통째로 복사한 것). 구성별 폴더(`--runs`·`--baseline-runs`)에서 같은 `run` 이름으로 찾는다.
- 채점에 쓰는 키는 `id`·`run`·`finding.id`·`truth.verdict`다. `repo`·`commit`·`source`·`doc`은 `replay.py`가 읽는 키라 이 줄에는 필요 없다. `baseline_opus_calls`를 빼면 지적 1건당 1로 센다.
- `<runs>/<run>/ledger.json`이 없으면 그 지적은 「실행 기록 없는 항목」으로 세고 「맞히지 못함」이다.
- 실행이 끝나지 않았으면(회차마다 판정과 `apply` 이벤트가 있어야 끝난 것이다. `--round N`이면 N회차까지만 본다) 그 실행의 지적이 모두 「미완료 항목」이고 「맞히지 못함」이다. 다른 실행은 영향이 없다.

## 30건 모으는 방법

목표는 30건 이상이고 「사실」과 「아님」이 섞여야 한다. 「아님」은 일부러 넣는다. 일치율이 「사실」만 모은 표본에서는 과대하게 나온다.

- [ ] 출처 1: `sample-be`의 Codex 스킬 리뷰 세션. 세션 기록에서 Codex가 낸 지적과 그때 Opus가 내린 판정(사실·아님·재현 불가)을 뽑는다.
- [ ] 출처 2: `sample-docs`의 지침 검토 1~9차 agentmemory 기록. 지적별 판정이 적혀 있고 커밋이 남은 것만 쓴다.
- [ ] 커밋 해시는 지적이 나온 시점의 것이다. `git log`로 그 검토 직전 커밋을 확인한다. 해시를 확인하지 못한 지적은 넣지 않는다.
- [ ] `truth.verdict`는 그때 Opus 판정이 아니라 사람이 최종 받아들인 판정으로 적는다. 둘이 다르면 사람 쪽이다.
- [ ] 사실 항목의 `accepted_patch`는 실제로 반영된 커밋의 diff에서 가져온다.
- [ ] `baseline_opus_calls`는 지적 1건당 1로 센다. 같은 지적을 여러 번 다시 본 기록이 있으면 그 횟수를 적는다.
- [ ] 문서 모드(`codex-doc`) 항목은 넣지 않는다. 회차별 문서 상태가 git에 없어 소급이 안 된다. 문서는 전향(실제 검토를 dry-run으로 같이 돌림)으로만 잰다. 전향 항목은 `doc`에 그 시점 문서를 두고 같은 형식으로 쓸 수 있다.
- [ ] PR 코멘트(`pr-comment`)는 `judge-eval/review-labels.jsonl`의 사람이 남긴 14건을 입력으로 쓴다. 정답 수정이 없으므로 `truth.accepted_patch`는 `null`이고 수정은 사람이 따로 채점한다.
- [ ] 저장소별 검증 명령은 `repos.toml`에 적어 둔다. `replay.py`가 워크트리 경로에 맞춰 설정을 복사해 쓰므로 원래 저장소 경로로 적으면 된다.

## 돌리는 순서

모든 명령은 스킬 루트(`~/.claude/skills/cascade-fix`)에서 돌린다.

1. 소급 재생. 항목마다 그 커밋의 워크트리를 만들고 `init --dry-run`부터 `report`까지 돌려 원장을 모은다.

   ```
   python3 eval/replay.py --sample eval/sample.jsonl --out <결과 폴더> [--limit N] [--repos-toml F]
   ```

   모델을 실제로 부르는 단계는 스킬의 Workflow가 한다. 위 명령은 `jobs`까지 만들고 멈추며 `meta.json`에 `pending_run_dir`을 적는다. 항목마다 Workflow로 `stage1.json`(필요하면 `adj.json`)을 돌리고 `judge` → `adjudicate` → `apply` → `report`를 이어간 뒤 다음으로 모은다. Workflow를 돌린 뒤마다 `CF usage --run-dir <pending_run_dir> --round N --workflow <Workflow 실행 id>`를 돌린다. `score.py`의 Claude 비용과 `select_profiles.py`의 선정이 이 기록에 기대므로, 빠뜨리면 그 실행의 비용이 「미측정」이 되고 후보가 「비용 미측정으로 제외」된다.

   ```
   python3 eval/replay.py --sample eval/sample.jsonl --out <결과 폴더> --collect
   ```

   `--collect`는 항목이 끝났는지 먼저 본다(1회차 `apply` 완료 이벤트, 마지막 회차 `apply.json`, 남은 의견 단계 없음, `report.md`와 원장의 `report` 이벤트). 하나라도 빠지면 `meta.json`에 `incomplete`를 적고 워크트리를 지우지 않는다. 끝났으면 `cleanup`을 돌리고 `run_dir`을 `<결과 폴더>/<id>/run/`으로 복사하며 `ledger.json`을 `<결과 폴더>/<id>/`에 놓고 워크트리를 지운다.

   `--profile <이름>`은 init에 넘긴다. `--workers`·`--adopt-on-both-pass`도 명령줄에 줬을 때만 init에 넘긴다(프로필 칸을 덮으므로 프로필을 잴 때는 주지 않는다). init 출력의 `프로필:`·`덮음:`·`대체:`·`역할:`·`전송 동의:` 줄은 `<결과 폴더>/<id>/init.txt`에 남고, jobs 뒤 확정 배정은 `역할 확정:` 한 줄로 찍으며 `meta.json`의 `roles`에 남는다.

   외부 작업자(Codex·Cursor)를 재려면 `--workers external`을 준다. 항목마다 `jobs` 뒤에 `workers --stage stage1`을 스스로 돌려 CLI 작업을 끝내고, 대체 실행이나 Claude 몫으로 남은 stage1 작업이 있을 때만 위처럼 멈춘다. `--workers`를 주지 않으면 init의 기본(프로필 또는 `claude`)을 따른다. 같은 표본을 `--workers claude`와 `--workers external`로 각각 다른 `--out` 아래에 돌린다.

2. 모델 없이 배관만 시험하려면 `--fake-results <폴더>`를 준다. 폴더 안에 `<id>/stage1.json`(필요하면 `adj.json`, `checker.json`)을 두면 `judge`·`adjudicate`·`apply`·`report`·`cleanup`까지 한 번에 돌고 항목마다 워크트리를 지운다. `--workers external`은 Codex·Cursor를 실제로 부르므로 `--fake-results`와 함께 쓸 수 없다(함께 주면 오류로 거절한다). init 뒤 확정 배정(`profile.roles`의 그 모드 작업자·점검자 칸)에 외부 runner나 외부 대체가 있으면 jobs를 부르기 전에 그 항목을 오류로 멈춘다(jobs의 샌드박스 점검이 Cursor 모델을 부를 수 있다). 외부 작업자 쪽 배관은 `--fake-results` 없이 가짜 CLI(`CASCADE_CODEX`·`CASCADE_CURSOR_AGENT`)로 시험한다.

3. 채택 규칙 반사실. 채택 규칙을 끄고 돌린 결과 폴더에서 규칙을 켠 값을 모델 호출 없이 다시 계산한다.

   ```
   python3 eval/adopt_recalc.py --runs <채택 규칙 끈 결과> --out <결과>/adopt-recalc
   ```

   항목마다 1회차 작업자 a·b 결과(`results.json`)와 검증(`verify.json`, 기준선 실패 명령 제외)으로 `decide`를 끄고 한 번, 켜고 한 번 돌린다. 보안 경로 → 빈 패치 → 손실 패치(`lossy.json`) 순서는 `judge`와 같다. 켠 결과가 바뀐 항목은 원장 결정을 새 결정으로 바꾸고 `adj-args.json`·`adj.json`·`results.json`의 `:adjudicator`·`claude-usage.json`의 판가름 행·원장의 `adjudicate` 이벤트를 지운다. 의견·재검토는 켠 값을 구할 수 없어 다시 계산하지 않으므로 `--out` 폴더는 `counterfactual.json`(`{"cost_scope": "round1-core"}`)을 두고 `score.py --cost-scope round1-core`로만 비용을 낸다. 오류·미완료 항목과 문서 모드 항목은 그대로 복사하고 `check.json`의 `skipped`에 적는다. `--out`을 `--runs` 안에 둬도 된다.

   `check.json`의 검증은 셋이다. 끈 결과의 판가름 분기가 원장(`adj-args.json` 유무)과 모두 같다 / 켠 결과가 바뀐 항목 수 = 빠진 판가름 수 / 뺀 토큰 = 그 항목들의 판가름 토큰 합. 옛 결과처럼 `lossy.json`이 없는데 켠 결과가 채택으로 바뀌는 항목은 「손실 여부 미확인」으로 실패한다. 하나라도 어긋나면 종료 코드 1과 어긋난 항목 목록을 낸다. 어긋난 항목(읽지 못한 항목 포함)은 `--out` 폴더의 그 항목 `meta.json`에 `error`를 적어 `score.py`가 오류 항목으로 센다(끈 결과를 켠 항목으로 채점하지 않는다).

4. 채점.

   ```
   python3 eval/score.py --runs <결과 폴더> --sample eval/sample.jsonl [--round N] [--pre-final] [--cost-scope all|round1-core] [--out score.md] [--json score.json]
   ```

   마크다운 표 하나와 정답별 표, 끝의 성공 기준 줄이 나온다. 표의 값은 설계 14절의 것이다.

   | 값 | 계산 |
   | --- | --- |
   | 정확도 | 판정이 `truth.verdict`와 같은 항목 ÷ **표본 전체 수**. 오류·미완료·누락 항목과 판정 없음은 「맞히지 못함」으로 센다(분모에서 빼지 않는다) |
   | 완료율 | 채점된 항목 ÷ 표본 전체 수. 오류·미완료·누락 항목이 빠진다 |
   | 판정 일치율 | 예측 판정이 `truth.verdict`와 같은 항목 ÷ 판정이 나온 항목. 합의-사실·판가름(패치 채택)은 사실, 합의-아님·판가름(아님)은 아님, 이유에 「재현 불가」가 있으면 재현 불가. 판단불가·설계결정·보류후보는 판정 없음이라 분모에서 뺀다 |
   | 채택 경로 분포 | 합의(합의-사실·합의-아님, 규칙 채택 제외) / 규칙 채택(합의-사실 중 이유에 「둘 다 검증 통과」가 있는 것. `--adopt-on-both-pass`를 끈 실행에서는 0) / 판가름 / Codex 검토(판단불가·설계결정·보류후보 중 Codex 의견이 원장에 붙은 것) / 사람(나머지) |
   | 패치 다르지만 둘 다 통과 | 판정 이유에 「패치가 다름」이나 「둘 다 검증 통과」가 있는 항목 ÷ 채점한 항목 |
   | Opus 호출 수 | 회차별 `results.json`의 `<id>:adjudicator` 개수 |
   | 기준 Opus 호출 수 | `baseline_opus_calls`의 합. 비율은 호출 수 ÷ 기준 |
   | 회귀 건수 | 회귀가 난 **실행** 수. `apply.json`의 전체 검사 이력(`final_history`) 중 한 번이라도 `final_ok`가 `false`이고 기준선도 실패한 것이 아니었던 실행이다(이력이 없는 옛 기록은 지금 필드로 같은 조건). 한 실행의 여러 회차가 회귀해도 1이다. 같은 판정 함수(`is_regression`)를 `regress.py`도 쓴다 |
   | 문서 점검자 정확도 | 문서 항목에서 점검자가 「아니오」라 한 것 중 판가름이 「사실」이라 한 비율. 없으면 `-` |
   | 아님→사실 채택 | 정답이 「아님」인데 예측이 「사실」인 항목 수 |
   | 사람 목록 | 판정이 판단불가·설계결정·보류후보인 항목 수 |
   | Claude 비용(추정) | 실행마다 `CF usage`가 저장한 작업별 비용(`claude-usage.json`, 첫 검토는 원장 `profile.first_review.usage`)의 합. 단가로 다시 계산하지 않는다. 비용을 구할 수 없는 실행(기록이 없거나 비용이 없는 작업이 있는 것)이 하나라도 섞이면 합계는 「미측정(<실행 id>)」이고 0으로 세지 않는다. 첫 검토는 원장 `profile.first_review`가 비어 있으면(`--findings-json`으로 시작했거나 review-meta 없는 `--input`) 해당 없음이라 비용도 「미측정」도 아니다. 비어 있지 않고 Claude가 한 첫 검토(`actual.runner`, 없으면 원장 `source`가 Claude)인데 사용량이 없을 때만 「미측정」이다. 실행 보고서도 같은 규칙이다 |
   | 오류 항목 수 | `replay.py`·`adopt_recalc.py`가 `meta.json`에 오류를 적은 항목, 원장·`meta.json`·`decisions-pre-final.json`을 읽지 못한 실행. 채점에서 빼고 읽지 못한 이유는 표준 오류로 낸다 |

   옵션과 출력은 다음과 같다.

   - `--round N`: N회차까지의 판정·호출·사용량·비용만 센다. 소급 재생 비교는 1회차만 보므로 `--round 1`을 준다.
   - `--pre-final`: 회차별 `decisions-pre-final.json`(`apply`가 전체 검사 전에 저장한 판정)으로 판정한다. 없는 회차는 원장 판정을 쓴다. 실제 실행만 거치는 전체 검사가 판정을 바꾼 몫을 빼고 dry-run과 비교할 때 쓴다.
   - `--cost-scope all|round1-core`(기본 `all`): `round1-core`는 1회차 작업자·점검자·판가름(`a`·`b`·`judge_a`·`judge_b`·`writer`·`checker`·`adjudicator`)과 그 대체 실행만 센다. 의견·재검토·첫 검토는 뺀다. `adopt_recalc.py`가 만든 폴더(`counterfactual.json`)는 `round1-core`로만 비용을 낸다(`all`이면 비용이 「미측정(반사실은 round1-core만)」).
   - `--json <파일>`: 지표를 JSON으로 쓴다. `--baseline-runs`를 줬으면 `selection`(선정 판정)도 들어간다. `select_profiles.py`의 입력이다. 파일을 쓰지 못하면(`--out` 포함) 이유를 내고 종료 코드 1이다.
   - 비용 줄과 표: 표 아래에 「칸별 Claude 사용량」 표(칸·호출·대체 실행·Claude 토큰(입력/출력/캐시 읽기)·Claude 비용(추정). 토큰을 남기지 않는 Cursor ACP 칸은 「기록 없음」, `CF usage` 기록이 빠진 칸은 「미측정」)와 「단가 기준:」 줄(저장된 기준 날짜와 출처)이 붙는다.
   - 정답별 표: 정답(`사실`·`아님`·`재현 불가`)마다 맞음·판정 있음·전체·사람 목록·아님→사실 채택.
   - `--baseline-runs`를 주면 끝에 「선정 조건」 줄이 붙는다. 형식은 `선정 조건: 통과|미달(<미달 조건>) — 정확도 …(…) · 「아님」 오채택 …(…) · 완주 …(…) · 대체 실행 …(…) · 회귀 … · Claude 비용 …`이다. 조건은 정확도(기준선보다 5%p 넘게 낮지 않다), 「아님」 오채택(기준선 이하), 완주(오류·미완료·누락이 표본의 5% 이하), 대체 실행(CLI 작업의 10% 이하), 회귀(0건)다. 회귀를 못 재면 `회귀 미측정`이고 조건에서 뺀다. 비용을 구할 수 없으면 `Claude 비용 미측정`이다.

   회귀는 전향(실제 적용) 실행에서만 측정된다. 소급 재생은 항상 dry-run이라 전체 테스트를 돌리지 않으므로 `apply.json`에 `final_ok`가 없고, 이때 표는 「측정 불가」, 판정 줄은 「회귀 미측정」으로 적는다. `--collect`는 끝나지 않은 항목(위 조건)을 수집하지 않고 `meta.json`에 `incomplete`를 적으며, 채점은 그런 항목을 오류 항목처럼 빼고 「미완료 항목 수」로 따로 센다. 항목에 오류가 하나라도 있으면 `replay.py`는 종료 코드 1을 돌려준다.

   `--workers external`로 돌린 결과에는 표에 행이 더해진다(CLI 작업이 있을 때만).

   | 값 | 계산 |
   | --- | --- |
   | 대체 실행 | 회차 이벤트 `workers-*`의 사유별 건수 합 ÷ 전체 CLI 작업. 사유는 한도·로그인·시간 초과·무효·오류·건너뜀 |
   | 무효 결과 | 재요청 뒤에도 무효인 CLI 작업 ÷ 전체 CLI 작업 |
   | Claude 호출 | Claude가 돈 작업 수를 모델 이름으로 센 값(Sonnet·Haiku·Opus). `stage1-args.json`·`checker-args.json`의 Claude 작업, 대체 실행(`*-fallback-args.json`), 판가름(`adj-args.json`)을 더한다. CLI가 맡은 작업은 세지 않는다 |
   | CLI 사용량 | CLI별 입력·출력·캐시 읽기 토큰의 합 |

   외부 작업자와 Claude 작업자를 같은 표본에서 나란히 비교하려면 `--baseline-runs`에 Claude 작업자로 돌린 결과 폴더를 준다.

   ```
   python3 eval/score.py --runs <external 결과> --baseline-runs <claude 결과> --sample eval/sample.jsonl [--out compare.md]
   ```

   표는 「Claude 작업자 | 외부 작업자」 두 열이고 끝에 「선정 조건」(위)과 「비교 판정」 두 줄이 붙는다. 통과 기준은 일치율 차이 5%p 이하, Opus 호출 수가 Claude 작업자 실행 이하, 대체 실행이 CLI 작업의 10% 이하, 회귀 0건이다. 못 미치는 항목은 `미달(일치율, Opus 호출, 대체 실행, 회귀)`처럼 적는다. 외부 실행에 CLI 작업이 없으면 대체 실행을 잴 수 없으므로 `미달(대체 실행 미측정)`이다. 소급 재생은 회귀를 못 재므로 「회귀 미측정」이라 적고 판정에서는 뺀다. 이때 단일 실행의 성공 기준 줄(95%·40%)은 나오지 않는다. 절차 전체는 비공개 측정 기록에 있다.

   성공 기준은 판정 일치 95% 이상, Opus 호출 40% 이하, 회귀 0건이다. 하나라도 못 미치면 값과 원인을 보고하고 구조를 되돌릴지 사용자가 정한다.

5. 후보 고르기. 구성(프로필)마다 같은 표본을 여러 번 돌려 `score.py --json`으로 실행마다 지표를 저장해 둔다. 기준선 구성의 결과를 `--baseline-runs`로 주고 채점한 JSON이면 실행마다의 `selection`도 들어 있다.

   ```
   python3 eval/select_profiles.py --candidate <이름>=<JSON>[,<JSON>…] [--candidate …] --baseline <기준선 이름> [--json 선정.json]
   ```

   - `--candidate`는 후보 이름과 그 후보의 실행별 `score.py --json` 파일을 쉼표로 이어 준다. 여러 번 줄 수 있고 `--baseline`은 그중 하나여야 한다.
   - 출력은 `기준선:` 줄, 후보별 평균 `[최소~최대]` 표(정확도·「아님」 오채택·완주·대체 실행·회귀 실행·Opus 호출·Claude 비용·사람 목록·판정 흔들림), 후보마다의 「거름 사유」, 끝의 `선정:` 줄이다.
   - 조건은 `score.py`의 「선정 조건」과 같고 평균으로 본다. 실행 중 한 번이라도 정확도 조건을 못 넘으면 후보 이름에 「(불안정)」이 붙는다. 실행 중 하나라도 `selection`이 없어(`--baseline-runs` 없이 채점) 정확도 조건을 알 수 없고 불안정한 실행도 없으면 안정하다고 하지 않고 「(안정성 미측정)」이 붙는다(JSON의 `unstable`은 `null`). Claude 비용이 한 실행이라도 미측정이면 그 후보는 「비용 미측정으로 제외」되어 0원으로 이기지 않는다.
   - 조건을 통과한 후보 중 Claude 비용 평균이 가장 적은 것을 고른다. 비용 차이가 10% 이내인 후보끼리는 사람 목록 평균이 적은 쪽이다. 고를 후보가 없으면 `선정: 없음`이다.
   - 점수 JSON을 읽지 못하면 종료 코드 1이다.

6. 첫 검토 세 결과 합치기·채점. 같은 입력을 Claude·Codex·Cursor가 각각 첫 검토한 결과를 묶음으로 합친다. 묶음 정답은 합친 목록으로 실제 실행한 결과와 사람의 반영·철회로 정한다.

   - **검토자마다 결과를 자기 폴더에 둔다.** `merge`는 `--claude`·`--codex`·`--cursor`가 가리키는 결과 파일 **옆의** `review-meta.json`을 그 검토자의 첫 검토 이력으로 붙이고(`merged.json`의 `reviewers.<runner>.meta`), Claude는 같은 폴더의 `claude-usage.json`을, Cursor는 결과 파일 이름에서 `.final`을 뺀 원본 로그(`review.out.final`이면 `review.out`)에서 토큰을 읽는다. 결과를 한 폴더에 모으면 그 폴더의 `review-meta.json` 하나가 모두의 이력으로 붙는다. 보통 `CF review`가 만든 각자의 `review_dir`을 그대로 쓴다. Claude 첫 검토는 Workflow 뒤에 `CF usage --review-dir <review_dir> --workflow <Workflow 실행 id>`를 돌려 `claude-usage.json`을 만들어 둔다.
   - 묶음 후보를 만든다. 코드 모드는 `--doc` 없이, 문서 모드는 검토한 문서를 `--doc`으로 준다. 결과는 하나 이상 주면 된다.

     ```
     python3 eval/first_review.py merge [--claude <review.json>] [--codex <.final>] [--cursor <.final>] [--doc <문서>…] --out <폴더>
     ```

     `<폴더>/merged.json`(지적과 묶음 후보)과 `<폴더>/review.md`(확정 방법과 묶음 표)가 생긴다. 위치가 같거나 같은 파일 5줄 안이면 묶음 후보로 제안할 뿐 자동으로 확정하지 않는다.
   - 사람이 `merged.json`의 `groups`를 고친다(같은 지적이면 `members`를 한 묶음으로 옮기고, 다르면 나눈다. 모든 지적은 한 묶음에만 둔다). 다 고쳤으면 `"confirmed"`를 `true`로 바꾸고 확정한다.

     ```
     python3 eval/first_review.py merge --confirm <폴더>/merged.json --out <폴더>
     ```

     `findings-input.json`(묶음마다 가장 높은 등급의 지적)과 `found-by.json`(묶음을 찾은 검토자)이 생기고, 이어 돌릴 `cascade.cli init --source claude-code|claude-doc --findings-json …` 명령이 출력된다. `--confirm`은 `--claude`·`--codex`·`--cursor`·`--doc`과 함께 쓰지 않는다.
   - **합친 입력의 실제 실행 보고서에는 첫 검토 이력이 없다**(`--findings-json`으로 시작하므로). 첫 검토 비용은 아래 `score`로 본다.
   - 채점. 정답 파일(jsonl)은 줄마다 `{"group": "g01", "truth": "사실|아님|재현 불가"}`이고 모든 묶음에 줄이 있어야 한다.

     ```
     python3 eval/first_review.py score --merged <확정한 merged.json> --truth <정답.jsonl> [--usage-claude <claude-usage.json>] [--usage-cli <Cursor 원본 로그>…] [--json 결과.json]
     ```

     검토자별 지적 수·정밀도·재현율·단독 사실(그 검토자만 찾은 「사실」 묶음 수), 등급별 정밀도·재현율, 첫 검토 비용(입력·출력·캐시 읽기·캐시 쓰기 토큰과 저장된 비용의 합, 다시 계산하지 않는다)이 나온다. Codex는 토큰을 남기지 않아 「기록 없음」, Cursor는 로그에 usage가 없으면 「기록 없음」이고 `--usage-cli` 로그 중 일부에만 있으면 「부분 기록(일부 로그에 usage 없음)」, Claude는 사용량 파일이 없으면 「기록 없음」, 사용량 행이 없으면 「미측정(형식 확인 필요: …)」, 비용이 없는 작업이 있으면 비용 칸이 「미측정: 비용이 없는 작업이 있다」이다. `--usage-*`를 주지 않으면 `merged.json`에 담긴 값을 쓴다. 확정하지 않았거나 묶음·정답이 어긋나면 종료 코드 1과 이유 한 줄이다.

7. 회귀(전향 측정). 실제 실행(dry-run 아님)의 실행 폴더를 프로필별로 모아 회귀를 센다.

   ```
   python3 eval/regress.py --runs <.cascade-fix/<실행 id>>… [--reverts <되돌림.jsonl>] [--stop-runs N] [--stop-patches N] [--json 결과.json]
   ```

   - 프로필은 원장의 `profile.name`이고 없으면 `legacy`다. dry-run 실행은 「dry-run 제외」로 따로 세고 회귀에 넣지 않는다.
   - 회귀 = 전체 검사 회귀가 있었던 실행(`score.py`의 `is_regression`과 같은 판정) ∪ 되돌림이 있었던 실행(합집합의 크기)이다. 기준선도 실패한 전체 검사는 회귀가 아니다.
   - 되돌림 파일은 채택 패치를 되돌리거나 다시 고친 것을 줄마다 `{"run": "<실행 폴더 이름>", "finding": "<지적 id>", "reason": "<사유>"}`로 적는다. `run`이 `--runs`에 없으면 경고하고 「프로필 불명」으로 센다.
   - `--runs`는 시간 순서로 준다. 프로필마다 실행 `--stop-runs`건(기본 10) 또는 채택 패치 `--stop-patches`건(기본 50) 중 먼저 닿는 쪽을 「종료 조건 진행」 줄이 알린다.
   - 출력은 프로필 · 실행 · dry-run 제외 · 채택 패치 · 간섭 회귀 실행 · 회귀 회차 · 기준선도 실패 · 되돌림 · 회귀 표다. 읽지 못한 실행 폴더나 형식이 틀린 되돌림 줄이 있으면 `오류:`를 stderr에 내고 종료 코드 1이다.
