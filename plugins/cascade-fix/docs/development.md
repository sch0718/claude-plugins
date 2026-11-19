# cascade-fix 개발

[README](../README.md) · [상세 레퍼런스](reference.md) · [SKILL.md](../SKILL.md)

## 테스트

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

- 테스트는 1704개입니다. `codex-review` 스킬이 없으면 그 스크립트를 쓰는 테스트는 건너뜁니다. 보관한 스크립트로 돌리려면 `CASCADE_RUN_REVIEW="$PWD/docs/codex-review-changes/run-review.sh"`를 앞에 붙입니다.

## 구조와 측정

- 디렉터리: `cascade/`(CLI와 단계 로직. `recommendations.toml`은 `CF setup`이 읽는 환경별 프로필 추천 표이고, 측정 결과로 갱신하는 절차는 `cascade/recommendations.toml` 머리 주석에 있습니다), `prompts/`(작업자·판정자·판가름·검토자 프롬프트), `scripts/run-jobs.js`(Workflow 스크립트), `eval/`(측정 도구), `docs/`(설계 검토 기록, 고친 `codex-review` 스크립트, `cursor-review` 사본, 외부 작업자 측정 계획)
- 측정: `eval/replay.py`가 과거 지적 표본을 그 커밋에서 다시 돌리고, `eval/score.py`가 정확도·판정 일치율·채택 경로·Opus 호출 절감·Claude 비용을 채점합니다. 그 위에 `eval/adopt_recalc.py`(채택 규칙 반사실), `eval/select_profiles.py`(반복한 후보에서 고르기), `eval/first_review.py`(첫 검토 세 결과 합치기·검토자별 채점), `eval/regress.py`(실제 실행의 프로필별 회귀 세기)가 있습니다. 단일 Claude와의 가성비 비교에는 `eval/solo.py`(지적마다 `claude -p` 세션 하나가 판정·수정·검증하는 기준선과 그 채점), `eval/orchestration_cost.py`(실제 사용 세션 기록에서 메인 세션 진행 비용 추정), `eval/run_time.py`(항목당 처리 시간), `eval/compare.py`(점수 파일들 → 요약 표·민감도 표·산점도 SVG)를 씁니다. 절차와 결과는 가성비 측정(비공개 측정 기록)에 있습니다. 표본 형식과 순서는 [eval/sample-format.md](../eval/sample-format.md)에 있습니다. `--workers external`과 Claude 작업자를 같은 표본에서 비교하는 절차는 측정 계획(비공개 측정 기록)에 있습니다.
- 출력 스키마를 바꾸면 `cascade/jobs.py`의 `SCHEMAS`와 `scripts/run-jobs.js`의 `SCHEMAS` 블록을 같이 고칩니다(테스트가 두 쪽이 같은지 확인합니다).

## 배포

- 배포할 때는 `.claude-plugin/plugin.json`과 `.claude-plugin/marketplace.json`의 `version`을 함께 올립니다(테스트가 둘이 같은지 확인합니다). 버전이 같으면 설치한 사람에게 업데이트로 보이지 않습니다.
- 매니페스트 검사: `claude plugin validate .`
