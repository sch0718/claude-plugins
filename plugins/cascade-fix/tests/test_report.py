import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import profiles, report  # noqa: E402
from cascade.consensus import (  # noqa: E402
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_AGREED_NO,
    OUTCOME_CARRIED,
    OUTCOME_DESIGN,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
)
from cascade.rounds import Ledger, RoundRecord  # noqa: E402
from helpers import git, make_repo  # noqa: E402


def finding(fid, severity="Warning", location="src/a.py:10", body="첫 줄 본문\n둘째 줄", thread_id=None):
    return {
        "id": fid,
        "source": "review",
        "severity": severity,
        "location": location,
        "body": body,
        "evidence": "",
        "thread_id": thread_id,
        "outdated": False,
    }


def decision(fid, outcome, patch_from=None, reasons=None):
    return {"finding_id": fid, "outcome": outcome, "patch_from": patch_from, "reasons": reasons or []}


def worker(fid, role, reason="근거"):
    return {
        "finding_id": fid,
        "role": role,
        "verdict": "사실",
        "needs_design_decision": False,
        "reason": reason,
        "verify": [],
        "checker_ok": None,
        "model_id": "m",
        "patch": "",
    }


def make_ledger(findings, decisions, source="local", dry_run=False, opinions=None, events=None, recheck=False):
    adopted = [
        d["finding_id"]
        for d in decisions
        if d["outcome"] == OUTCOME_AGREED_FACT or (d["outcome"] == OUTCOME_ADJUDICATE and d["patch_from"])
    ]
    rnd = RoundRecord(
        no=1,
        findings=findings,
        decisions=decisions,
        adopted=adopted,
        recheck_opened=recheck,
        events=events or [],
    )
    return Ledger(
        run_id="run-1",
        source=source,
        mode="code",
        root="/tmp/x",
        snapshot={},
        dry_run=dry_run,
        rounds=[rnd],
        opinions=opinions or {},
    )


def section(text, heading):
    start = text.index(heading)
    nxt = text.find("\n## ", start + 1) if heading.startswith("## ") else text.find("\n### ", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


def full_ledger():
    findings = [
        finding("F1", location="a.py:1", body="합의 사실"),
        finding("F2", location="b.py:2", body="판가름 채택"),
        finding("F3", location="c.py:3", body="합의 아님"),
        finding("F4", location="d.py:4", body="설계 필요"),
        finding("F5", location="e.py:5", body="판단 못 함"),
        finding("F6", location="f.py:6", body="보류 대상", severity="Suggestion"),
        finding("F7", location="g.py:7", body="판가름 아님"),
    ]
    decisions = [
        decision("F1", OUTCOME_AGREED_FACT, "a", ["둘 다 통과"]),
        decision("F2", OUTCOME_ADJUDICATE, "b", ["한쪽만 검증 통과", "판가름: 사실"]),
        decision("F3", OUTCOME_AGREED_NO, None, ["a: 코드에 없음", "b: 이미 처리됨"]),
        decision("F4", OUTCOME_DESIGN, None, ["API 형태를 정해야 함"]),
        decision("F5", OUTCOME_UNDECIDABLE, None, ["패치가 다름"]),
        decision("F6", OUTCOME_HOLD, None, ["제안 수준"]),
        decision("F7", OUTCOME_ADJUDICATE, None, ["패치가 다름", "판가름: 아님 — 이미 있음"]),
    ]
    return make_ledger(findings, decisions, opinions={"F5": "Codex 의견 1\nCodex 의견 2"}, events=[{"step": "opinion:F5"}])


def rerun_ledger(r2_decision, r3_decision, source="pr-comment", opinions=None, r3_events=None):
    f = finding("r1-03", thread_id="T3", body="같은 지적")
    r1 = RoundRecord(no=1, findings=[], decisions=[decision("r1-03", OUTCOME_HOLD, None, ["등급 선택"])],
                     adopted=[], recheck_opened=False, hold_findings=[f])
    r2 = RoundRecord(no=2, findings=[f], decisions=[r2_decision], adopted=[], recheck_opened=False, final=True)
    r3 = RoundRecord(no=3, findings=[f], decisions=[r3_decision], adopted=[], recheck_opened=False, final=True,
                     events=r3_events or [])
    return Ledger(run_id="run-1", source=source, mode="code", root="/tmp/x", snapshot={}, dry_run=False,
                  rounds=[r1, r2, r3], opinions=opinions or {})


def write_round_file(run_dir, no, name, payload):
    (run_dir / f"round-{no}").mkdir(exist_ok=True)
    (run_dir / f"round-{no}" / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class RenderTest(unittest.TestCase):
    def test_render_separates_consensus_and_adjudicated(self):
        text = report.render(full_ledger(), dry_run=False)
        table = section(text, "## 회차별 지적과 채택 경로")
        self.assertIn("| 회차 | 지적 | 합의 | 판가름 회부 | 판가름 채택 | Codex 검토 | 사람 |", table)
        self.assertIn("| 1 | 7 | 2 | 2 | 1 | 1 | 4 |", table)
        self.assertIn("합의-아님", table)
        commit = section(text, "### 커밋 메시지 초안")
        self.assertIn("- F2 b.py:2: 판가름 채택", commit)
        rebuttal = section(text, "### 반박")
        self.assertIn("**F3**", rebuttal)
        self.assertIn("**F7**", rebuttal)
        self.assertIn("판가름", rebuttal)

    def test_adjudication_sent_counts_findings_rewritten_after_adjudication(self):
        findings = [finding("R1"), finding("R2"), finding("R3")]
        decisions = [
            decision("R1", OUTCOME_UNDECIDABLE, None, ["판정이 갈림: a=사실, b=못 정함", "판가름 못 정함", "판가름: 원문 없음"]),
            decision("R2", OUTCOME_ADJUDICATE, None, ["판정이 갈림: a=아님, b=사실", "판가름: 아님", "판가름: 원문 그대로"]),
            decision("R3", OUTCOME_DESIGN, None, ["설계 결정 필요 여부 갈림", "판가름: 작성자가 정한다"]),
        ]
        ledger = make_ledger(findings, decisions, opinions={"R1": "의견"}, events=[{"step": "opinion:R1"}])
        text = report.render(ledger, dry_run=True)
        table = section(text, "## 회차별 지적과 채택 경로")
        self.assertIn("| 1 | 3 | 0 | 3 | 0 | 1 | 3 |", table)

    def test_table_counts_decision_path_even_after_apply_failed(self):
        findings = [finding("A1"), finding("A2"), finding("A3")]
        decisions = [
            decision("A1", OUTCOME_AGREED_FACT, "a"),
            {**decision("A2", OUTCOME_UNDECIDABLE, None, ["패치 적용 실패: error: patch failed"]),
             "adopted_as": OUTCOME_AGREED_FACT},
            {**decision("A3", OUTCOME_UNDECIDABLE, None, ["한쪽만 검증 통과", "판가름: 고친다", "패치 적용 실패: x"]),
             "adopted_as": OUTCOME_ADJUDICATE},
        ]
        text = report.render(make_ledger(findings, decisions), dry_run=False)
        table = section(text, "## 회차별 지적과 채택 경로")
        self.assertIn("| 1 | 3 | 2 | 1 | 1 | 0 | 2 |", table)
        self.assertIn("판정 때 값", table)

    def test_four_buckets_present_with_reasons(self):
        text = report.render(full_ledger(), dry_run=False)
        people = section(text, "## 사람이 볼 목록")
        for heading in ("### 반박", "### 설계 결정", "### 판단 불가", "### 보류 후보"):
            self.assertIn(heading, people)
        rebuttal = section(text, "### 반박")
        self.assertIn("a: 코드에 없음", rebuttal)
        self.assertIn("b: 이미 처리됨", rebuttal)
        self.assertIn("선택: [ ] 동의 [ ] 그래도 고친다", rebuttal)
        self.assertIn("- **F3** (Warning, c.py:3) — 합의 아님", rebuttal)
        design = section(text, "### 설계 결정")
        self.assertIn("**F4**", design)
        self.assertIn("API 형태를 정해야 함", design)
        undecidable = section(text, "### 판단 불가")
        self.assertIn("Codex 의견 1", undecidable)
        self.assertIn("round-1/patch-F5-a.diff", undecidable)
        self.assertIn("round-1/patch-F5-b.diff", undecidable)
        hold = section(text, "### 보류 후보")
        self.assertIn("선택: [ ] 지금 반영 [ ] 구현 때 테스트로 [ ] 보류", hold)

    def test_hold_choice_shown_and_codex_opinion_capped(self):
        ledger = full_ledger()
        ledger.hold_choices = {"F6": "보류"}
        ledger.opinions["F5"] = "\n".join(f"줄{i}" for i in range(30))
        text = report.render(ledger, dry_run=False)
        self.assertIn("선택됨: 보류", section(text, "### 보류 후보"))
        undecidable = section(text, "### 판단 불가")
        self.assertIn("줄19", undecidable)
        self.assertNotIn("줄20", undecidable)

    def test_results_json_supplies_worker_and_adjudicator_reasons(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {
                "F3:a": worker("F3", "a", "A 근거 상세"),
                "F3:b": worker("F3", "b", "B 근거 상세"),
                "F5:adjudicator": worker("F5", "adjudicator", "Opus가 못 정함"),
            }
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, dry_run=False, run_dir=run_dir)
        self.assertIn("a: A 근거 상세", section(text, "### 반박"))
        self.assertIn("b: B 근거 상세", section(text, "### 반박"))
        self.assertIn("Opus가 못 정함", section(text, "### 판단 불가"))

    def _design_report(self, writer_item):
        ledger = make_ledger([finding("D1", location="docs/spec.md:12", body="저장 형식을 정하지 않았다")],
                             [decision("D1", OUTCOME_DESIGN, None, ["a: 짧은 근거", "b: 짧은 근거"])])
        ledger.mode = "doc"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {
                "D1:a": worker("D1", "a", "A 상세: 형식이 없다"),
                "D1:b": worker("D1", "b", "B 상세: 12행에 형식이 없다"),
            }
            if writer_item is not None:
                results["D1:writer"] = writer_item
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            return section(report.render(ledger, dry_run=False, run_dir=run_dir), "### 설계 결정")

    def test_design_bucket_shows_judges_and_writer_options(self):
        options = "1) JSON — 표준 라이브러리 / 주석 없음\n2) TOML — 주석 가능 / 3.11 이상"
        design = self._design_report({**worker("D1", "writer", options), "needs_design_decision": True})
        self.assertIn("  - a: A 상세: 형식이 없다", design)
        self.assertIn("  - b: B 상세: 12행에 형식이 없다", design)
        self.assertNotIn("짧은 근거", design)
        self.assertIn("  - 작성자 선택지:\n    1) JSON — 표준 라이브러리 / 주석 없음\n    2) TOML", design)
        self.assertIn("선택: [ ] 선택지 ___ [ ] 직접 정함 [ ] 보류", design)
        self.assertNotIn("작성자 안 채택", design)
        self.assertNotIn("patch-D1-writer.diff", design)

    def test_design_bucket_offers_writer_patch_when_writer_fixed_it(self):
        item = {**worker("D1", "writer", "JSON으로 정하고 3행을 고쳤다"), "patch": "--- a/x\n+++ b/x\n"}
        design = self._design_report(item)
        self.assertIn("  - 작성자 수정안(채택 안 함):\n    JSON으로 정하고 3행을 고쳤다", design)
        self.assertIn("  - 패치: round-1/patch-D1-writer.diff", design)
        self.assertIn("선택: [ ] 작성자 안 채택(패치 직접 적용) [ ] 직접 정함 [ ] 보류", design)
        self.assertNotIn("작성자 선택지", design)

    def test_design_bucket_without_writer_options_or_patch(self):
        design = self._design_report({**worker("D1", "writer", "지적이 아니다"), "verdict": "아님"})
        self.assertIn("  - 작성자: 지적이 아니다", design)
        self.assertIn("선택: [ ] 직접 정함 [ ] 보류", design)
        self.assertIn("선택: [ ] 직접 정함 [ ] 보류", self._design_report(None))

    def test_adjudicator_reason_shown_once_when_already_in_reasons(self):
        ledger = full_ledger()
        ledger.rounds[0].decisions[4]["reasons"] = ["패치가 다름", "판가름 패치 검증 실패", "판가름: Opus가 고친 패치도 깨진다"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {"F5:adjudicator": worker("F5", "adjudicator", "Opus가 고친 패치도 깨진다\n")}
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, dry_run=False, run_dir=run_dir)
        undecidable = section(text, "### 판단 불가")
        self.assertEqual(undecidable.count("Opus가 고친 패치도 깨진다"), 1)
        self.assertIn("  - 판가름: Opus가 고친 패치도 깨진다", undecidable)
        self.assertNotIn("Opus: ", undecidable)

    def test_dry_run_banner(self):
        ledger = full_ledger()
        text = report.render(ledger, dry_run=True)
        body_lines = text.splitlines()
        self.assertTrue(body_lines[0].startswith("# cascade-fix 보고서 run-1"))
        first_body = next(line for line in body_lines[1:] if line.strip() and not line.startswith("출처:"))
        self.assertEqual(first_body, "**적용하지 않음(dry-run)**")
        self.assertNotIn("적용하지 않음(dry-run)", report.render(ledger, dry_run=False))

    def test_adopt_on_both_pass_note_counts_rule_adoptions(self):
        decisions = [
            decision("A1", OUTCOME_AGREED_FACT, "b", ["둘 다 검증 통과: 작은 패치 b 채택"]),
            decision("A2", OUTCOME_AGREED_FACT, "a"),
            decision("A3", OUTCOME_UNDECIDABLE, None, ["둘 다 검증 통과: 작은 패치 a 채택", "보안 경로"]),
        ]
        ledger = make_ledger([finding("A1"), finding("A2"), finding("A3")], decisions)
        self.assertNotIn("--adopt-on-both-pass", report.render(ledger, dry_run=False))
        ledger.adopt_on_both_pass = True
        table = section(report.render(ledger, dry_run=False), "## 회차별 지적과 채택 경로")
        self.assertIn("| 1 | 3 | 2 | 0 | 0 | 0 | 1 |", table)
        self.assertIn("이번 실행은 --adopt-on-both-pass 로 돌았다", table)
        self.assertIn("이 규칙으로 채택한 지적 1건", table)

    def test_header_line(self):
        text = report.render(full_ledger(), dry_run=False)
        self.assertIn("출처: local · 모드: code · 검토자: Codex · 상태: 진행", text)

    def test_commit_message_single_and_multiple(self):
        one = make_ledger(
            [finding("F1", body="x" * 80 + "\n나머지")],
            [decision("F1", OUTCOME_AGREED_FACT, "a")],
        )
        block = section(report.render(one, False), "### 커밋 메시지 초안")
        self.assertIn("fix: " + "x" * 60 + "\n", block)
        subject = next(line for line in block.splitlines() if line.startswith("fix: "))
        self.assertEqual(len(subject), len("fix: ") + 60)
        many = report.render(full_ledger(), False)
        block = section(many, "### 커밋 메시지 초안")
        self.assertIn("fix: 2건의 리뷰 지적 반영", block)
        self.assertIn("- F1 a.py:1: 합의 사실", block)

    def test_apply_result_section(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.assertNotIn("### 적용 결과", report.render(ledger, False, run_dir=run_dir))
            (run_dir / "round-1").mkdir()
            (run_dir / "round-1" / "apply.json").write_text(
                json.dumps({"applied": ["F1"], "failed": [["F2", "충돌 꼬리"]]}, ensure_ascii=False), encoding="utf-8"
            )
            text = report.render(ledger, False, run_dir=run_dir)
        apply_section = section(text, "### 적용 결과")
        self.assertIn("F1", apply_section)
        self.assertIn("F2", apply_section)
        self.assertIn("충돌 꼬리", apply_section)

    def test_carried_finding_not_listed_as_failure(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            (run_dir / "round-1" / "apply.json").write_text(json.dumps(
                {"applied": ["F1"], "failed": [["F2", "겹침 꼬리"], ["F5", "진짜 실패"]], "carried": ["F2"]},
                ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, False, run_dir=run_dir)
        apply_section = section(text, "### 적용 결과")
        self.assertIn("- 회차 1 다음 회차로 넘김(앞 패치와 겹침): F2", apply_section)
        self.assertNotIn("실패 **F2**", apply_section)
        self.assertNotIn("겹침 꼬리", apply_section)
        self.assertIn("실패 **F5**", apply_section)

    def test_pr_comment_replies_and_resolve(self):
        findings = [
            finding("F1", thread_id="T1", body="고칠 것"),
            finding("F3", thread_id="T3", body="아님 건"),
            finding("F4", thread_id="T4", body="설계"),
            finding("F5", thread_id=None, body="스레드 없음"),
        ]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["둘 다 통과"]),
            decision("F3", OUTCOME_AGREED_NO, None, ["a: 코드에 없음", "b: 이미 처리됨"]),
            decision("F4", OUTCOME_DESIGN, None, []),
            decision("F5", OUTCOME_AGREED_FACT, "a", []),
        ]
        ledger = make_ledger(findings, decisions, source="pr-comment")
        text = report.render(ledger, False)
        replies = section(text, "### PR 답글 초안")
        self.assertIn("thread T1:", replies)
        self.assertIn("반영했습니다.", replies)
        self.assertIn("thread T3:", replies)
        self.assertIn("확인 결과", replies)
        self.assertIn("코드에 없음", replies)
        self.assertIn("thread T4:", replies)
        self.assertIn("thread T4:\n검토 중입니다.\n", replies)
        self.assertNotIn("설계결정", replies)
        self.assertNotIn("F5", replies)
        resolve = section(text, "### resolve 대상")
        self.assertIn("T1", resolve)
        self.assertNotIn("T3", resolve)
        self.assertNotIn("T4", resolve)
        local = report.render(make_ledger(findings, decisions, source="local"), False)
        self.assertNotIn("### PR 답글 초안", local)
        self.assertNotIn("### resolve 대상", local)

    def _failed_apply_ledger(self):
        findings = [finding("F1", thread_id="T1", body="고칠 것 1"), finding("F2", thread_id="T2", body="고칠 것 2")]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["통과"]),
            decision("F2", OUTCOME_AGREED_FACT, "a", ["통과"]),
        ]
        return make_ledger(findings, decisions, source="pr-comment")

    def _write_apply(self, run_dir, payload):
        (run_dir / "round-1").mkdir(exist_ok=True)
        (run_dir / "round-1" / "apply.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_apply_failure_excluded_from_prepared_files(self):
        ledger = self._failed_apply_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self._write_apply(run_dir, {"applied": ["F1"], "failed": [["F2", "충돌 발생\n둘째 줄"]]})
            report.write_prepared(run_dir, ledger)
            message = (run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8")
            replies = json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8"))
            resolve = json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8"))
            text = report.render(ledger, False, run_dir=run_dir)
        self.assertEqual([line for line in message.splitlines() if line.startswith("- ")], ["- F1 src/a.py:10: 고칠 것 1"])
        self.assertTrue(message.startswith("fix: 고칠 것 1\n"))
        self.assertEqual(resolve, ["T1"])
        self.assertTrue(replies["T1"].startswith("반영했습니다."))
        self.assertEqual(replies["T2"], "적용 실패: 충돌 발생. 검토 중입니다.")
        self.assertIn("적용 실패: 충돌 발생. 검토 중입니다.", section(text, "### PR 답글 초안"))
        self.assertNotIn("T2", section(text, "### resolve 대상"))
        self.assertNotIn("- F2 ", section(text, "### 커밋 메시지 초안"))

    def test_malformed_apply_failed_entries_are_skipped(self):
        ledger = self._failed_apply_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self._write_apply(run_dir, {"applied": ["F1"], "failed": ["F2", ["F2"], 5, None]})
            text = report.render(ledger, False, run_dir=run_dir)
            report.write_prepared(run_dir, ledger)
            resolve = json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8"))
        self.assertIn("### 적용 결과", text)
        self.assertEqual(resolve, ["T1", "T2"])

    def test_empty_worker_reason_falls_back_to_decision_reasons(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {"F3:a": worker("F3", "a", ""), "F3:b": worker("F3", "b", "B 근거")}
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, False, run_dir=run_dir)
        rebuttal = section(text, "### 반박")
        self.assertIn("a: 코드에 없음", rebuttal)
        self.assertIn("b: 이미 처리됨", rebuttal)
        self.assertNotIn("B 근거", rebuttal)

    def test_claude_first_review_counted_in_calls(self):
        first = {"role": "code.review", "requested": {"runner": "claude", "model": "", "effort": ""},
                 "actual": {"runner": "claude", "model": SONNET, "effort": "high"}, "fallback_reason": None, "attempts": []}
        ledger = profile_ledger(profile_record("claude-only", first_review=first), source="claude-code")
        calls = section(report.render(ledger, False), "## 역할별 사용량")
        self.assertIn("| Claude 첫 검토 | 1 |", calls)
        self.assertIn("| Claude 의견 | 0 |", calls)

    def test_single_commit_subject_when_body_first_line_empty(self):
        ledger = make_ledger([finding("F1", body="")], [decision("F1", OUTCOME_AGREED_FACT, "a")])
        block = section(report.render(ledger, False), "### 커밋 메시지 초안")
        self.assertIn("fix: 리뷰 지적 F1 반영", block)

    def test_call_counts_and_elapsed(self):
        events = [
            {"step": "start", "at": "2026-10-17T10:00:00+00:00"},
            {"step": "end", "at": "2026-10-17T10:01:05+00:00"},
        ]
        ledger = make_ledger(
            [finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")],
            opinions={"F1": "의견"}, events=events, recheck=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {f"F1:{role}": worker("F1", role) for role in ("a", "b", "writer", "checker")}
            results["F2:a"] = worker("F2", "a")
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results), encoding="utf-8")
            text = report.render(ledger, False, run_dir=run_dir)
        calls = section(text, "## 역할별 사용량")
        self.assertIn("| code.a | 2 |", calls)
        self.assertIn("| code.b | 1 |", calls)
        self.assertIn("| code.adjudicator | 0 |", calls)
        self.assertIn("| Codex 의견 | 1 |", calls)
        self.assertNotIn("첫 검토", calls)
        self.assertIn("| Codex 재검토 | 1 |", calls)
        self.assertIn("| 1 | 1분 5초 |", calls)

    def test_elapsed_dash_when_fewer_than_two_events(self):
        text = report.render(full_ledger(), False)
        self.assertIn("| 1 | - |", section(text, "## 역할별 사용량"))

    def test_bucket_decisions_matches_render(self):
        ledger = full_ledger()
        buckets = report.bucket_decisions(ledger)
        self.assertEqual(set(buckets), {"반박", "설계 결정", "판단 불가", "보류 후보"})
        ids = {name: [f["id"] for f, _ in pairs] for name, pairs in buckets.items()}
        self.assertEqual(ids["반박"], ["F3", "F7"])
        self.assertEqual(ids["설계 결정"], ["F4"])
        self.assertEqual(ids["판단 불가"], ["F5"])
        self.assertEqual(ids["보류 후보"], ["F6"])
        text = report.render(ledger, False)
        for name, id_list in ids.items():
            body = section(text, f"### {name}")
            for fid in id_list:
                self.assertIn(f"**{fid}**", body)
        for f, d in buckets["반박"]:
            self.assertEqual(f["id"], d["finding_id"])


def profile_record(name="claude-only", **extra):
    record = {"name": name, "source": "--profile", "explicit": True, "roles": copy.deepcopy(profiles.builtin(name)["roles"]),
              "overrides": [], "substituted": [], "consented": [], "first_review": {}}
    return {**record, **extra}


def profile_ledger(record, mode="code", source="local", events=None, **kwargs):
    ledger = make_ledger([finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")], source=source, events=events,
                         **kwargs)
    ledger.mode = mode
    ledger.profile = record
    return ledger


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def job(job_id, runner="claude"):
    return {"id": job_id, "role": job_id.rsplit(":", 1)[-1], "runner": runner}


class RoleSectionTest(unittest.TestCase):
    def render(self, ledger, run_dir=None):
        return report.render(ledger, dry_run=True, run_dir=run_dir)

    def test_role_section_always_present(self):
        text = self.render(profile_ledger(profile_record()))
        self.assertIn("## 역할 배정", text)
        self.assertIn("프로필: claude-only (--profile)", text)
        self.assertIn("| 역할 | runner | 모델 | effort | fallback |", text)
        self.assertIn("| code.a | claude | claude-sonnet-5-5 | low |", text)

    def test_old_ledger_report_still_renders(self):
        ledger = make_ledger([finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")])
        text = self.render(ledger)
        self.assertIn("프로필: legacy", text)
        self.assertIn("| code.a | claude |", text)
        self.assertIn("## 역할별 사용량", text)

    def test_table_lists_only_the_ledger_mode_cells(self):
        code = section(self.render(profile_ledger(profile_record())), "## 역할 배정")
        self.assertIn("| code.review |", code)
        self.assertNotIn("| doc.", code)
        doc = section(self.render(profile_ledger(profile_record(), mode="doc", source="claude-doc")), "## 역할 배정")
        self.assertIn("| doc.writer | claude |", doc)
        self.assertNotIn("| code.", doc)

    def test_cell_table_shows_codex_cell_with_fallback(self):
        text = section(self.render(profile_ledger(profile_record("local"))), "## 역할 배정")
        self.assertIn("| code.a | codex | - | low | claude(claude-sonnet-5-5 low) |", text)

    def test_overrides_and_worker_assignment_are_shown(self):
        record = profile_record("local", overrides=["code.a ← --workers external"])
        ledger = profile_ledger(record)
        ledger.workers_choice = "external"
        ledger.workers = {"a": {"runner": "codex", "model": "gpt-test", "effort": "low"},
                          "b": {"runner": "cursor", "model": "composer-2.5", "effort": ""}}
        text = section(self.render(ledger), "## 역할 배정")
        self.assertIn("덮음: code.a ← --workers external", text)
        self.assertIn("배정: A·writer=Codex(gpt-test), B·checker=Cursor(composer-2.5)", text)

    def test_substitutions_are_filtered_to_the_ledger_mode(self):
        record = profile_record("local", substituted=[
            {"role": "code.a", "from": "codex", "to": "claude", "reason": "미설치 또는 로그인 필요"},
            {"role": "code.b(대체)", "from": "cursor", "to": "claude", "reason": "샌드박스 점검 실패"},
            {"role": "doc.writer", "from": "codex", "to": "claude", "reason": "미설치 또는 로그인 필요"}])
        text = section(self.render(profile_ledger(record)), "## 역할 배정")
        self.assertIn("code.a codex → claude — 미설치 또는 로그인 필요", text)
        self.assertIn("code.b(대체) cursor → claude — 샌드박스 점검 실패", text)
        self.assertNotIn("doc.writer codex", text)

    def test_runtime_substitutions_are_filtered_and_listed_apart(self):
        claude = profiles.builtin("claude-only")["roles"]
        record = profile_record("local", substituted=[
            {"role": "code.a", "from": "codex", "to": "claude", "reason": "미설치 또는 로그인 필요"}],
            runtime_substitutions=[
                {"role": "code.opinion", "from": "codex", "to": "claude", "cell": claude["code.opinion"],
                 "reason": "이용 한도", "round": 1},
                {"role": "doc.recheck", "from": "cursor", "to": "claude", "cell": claude["doc.recheck"],
                 "reason": "실행 오류", "round": 1}])
        text = section(self.render(profile_ledger(record)), "## 역할 배정")
        runtime = section(text, "### 실행 중 대체")
        self.assertIn("code.opinion codex → claude — 이용 한도 (회차 1)", runtime)
        self.assertNotIn("doc.recheck", text)
        setup = section(text, "### 배정 때 바꾼 칸")
        self.assertIn("code.a codex → claude", setup)
        self.assertNotIn("code.opinion", setup)

    def test_first_review_history_is_shown(self):
        first = {"role": "code.review", "requested": {"runner": "codex", "model": "", "effort": ""},
                 "actual": {"runner": "claude", "model": "claude-fable-5-1", "effort": "high"},
                 "fallback_reason": "한도", "attempts": [{"runner": "codex", "reason": "한도"}]}
        text = section(self.render(profile_ledger(profile_record("local", first_review=first), source="claude-code")),
                       "## 역할 배정")
        self.assertIn("첫 검토 Codex → Claude(한도)", text)

    def test_first_review_chain_names_every_attempt(self):
        first = {"role": "code.review", "requested": {"runner": "codex", "model": "", "effort": ""},
                 "actual": {"runner": "claude", "model": "claude-fable-5-1", "effort": "high"}, "fallback_reason": "오류",
                 "attempts": [{"runner": "codex", "reason": "한도"}, {"runner": "cursor", "reason": "오류"}]}
        text = self.render(profile_ledger(profile_record("local", first_review=first), source="claude-code"))
        self.assertIn("첫 검토 Codex → Cursor(한도) → Claude(오류)", text)

    def test_first_review_without_fallback_names_the_runner(self):
        first = {"role": "code.review", "requested": {"runner": "codex", "model": "", "effort": ""},
                 "actual": {"runner": "codex", "model": "", "effort": ""}, "fallback_reason": None, "attempts": []}
        text = self.render(profile_ledger(profile_record("local", first_review=first), source="codex-code"))
        self.assertIn("첫 검토 Codex — 대체 없음", text)

    def test_in_call_external_rerun_is_listed_apart_from_runtime_substitution(self):
        events = [{"step": "workers-stage1", "at": "t", "counts": {"limit": 1, "skipped": 1}, "fallback": ["F1:a", "F2:a"],
                   "fallback_runs": {"F1:a": "cursor"}, "usage": {}}]
        text = section(self.render(profile_ledger(profile_record("local"), events=events)), "## 역할 배정")
        runtime = section(text, "### 실행 중 대체")
        self.assertIn("회차 1 stage1: CLI 작업 2건, 대체 실행 2건(한도 1, 건너뜀 1)", runtime)
        self.assertIn("회차 1 stage1 외부 CLI로 다시 돌림: F1:a → cursor", runtime)

    def test_login_hint_survives_in_role_section(self):
        events = [{"step": "workers-stage1", "at": "t", "counts": {"auth": 1}, "fallback": ["F1:a"], "fallback_runs": {},
                   "usage": {}, "exhausted": {"codex": {"status": "auth", "detail": "x"}}}]
        text = self.render(profile_ledger(profile_record("local"), events=events))
        self.assertIn("codex login", section(text, "## 역할 배정"))
        self.assertNotIn("cursor-agent login", text)

    def test_opinion_failure_count_requires_failed_key_and_is_not_a_fallback(self):
        for failed in (None, [], ["F1:opinion"]):
            with self.subTest(failed=failed):
                event = {"step": "workers-opinion", "counts": {"error": 1}, "fallback": ["F1:opinion"]}
                if failed is not None:
                    event["failed"] = failed
                ledger = profile_ledger(profile_record(), events=[event])
                line = next(line for line in self.render(ledger).splitlines() if "회차 1 opinion:" in line)
                self.assertNotIn("대체 실행", line)
                if failed is None:
                    self.assertNotIn("의견 없음", line)
                else:
                    self.assertIn(f"의견 없음 {len(failed)}건", line)
                self.assertNotIn("code.opinion", report._worker_fallbacks(ledger, None)[0])


class RoleUsageTest(unittest.TestCase):
    def render(self, ledger, run_dir):
        return report.render(ledger, dry_run=True, run_dir=run_dir)

    def local_with_one_fallback(self, run_dir, mode="code"):
        record = profile_record("local")
        ledger = profile_ledger(record, mode=mode)
        ledger.workers_choice = "external"
        rd = run_dir / "round-1"
        write_json(rd / "stage1-args.json", {"jobs": [job("F1:b"), job("F2:b")]})
        write_json(rd / "stage1-cli-args.json", {"jobs": [job("F1:a", "codex"), job("F2:a", "codex")]})
        write_json(rd / "stage1-cli.json", {
            "results": [], "fallback_runs": {},
            "outcomes": [{"id": "F1:a", "runner": "codex", "status": "limit", "output": None,
                          "usage": {"input_tokens": 10, "output_tokens": 4}, "detail": ""},
                         {"id": "F2:a", "runner": "codex", "status": "ok", "output": {},
                          "usage": {"input_tokens": 12, "output_tokens": 6}, "detail": ""}],
            "replaced": []})
        ledger.rounds[0].events.append({"step": "workers-stage1", "at": "t", "counts": {"limit": 1, "ok": 1},
                                        "fallback": ["F1:a"], "fallback_runs": {},
                                        "usage": {"codex": {"input": 22, "output": 10, "cache_read": 0}}})
        return ledger

    def test_role_usage_counts_per_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.render(self.local_with_one_fallback(Path(tmp)), Path(tmp))
        usage = section(text, "## 역할별 사용량")
        self.assertIn("| 역할 | 호출 | 대체 실행(사유) | Claude 토큰(입력/출력/캐시 읽기) | Claude 비용(추정) | CLI 토큰 |", usage)
        self.assertIn("| code.a | 2 | 1(한도) |", usage)
        self.assertIn("| code.b | 2 | 0 |", usage)
        self.assertIn("| code.adjudicator | 0 | 0 |", usage)

    def test_cli_tokens_are_attributed_to_the_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.render(self.local_with_one_fallback(Path(tmp)), Path(tmp))
        usage = section(text, "## 역할별 사용량")
        self.assertIn("| code.a | 2 | 1(한도) | - | - | 입력 22 · 출력 10 |", usage)
        self.assertIn("| code.b | 2 | 0 | 미측정 | 미측정 | - |", usage)
        self.assertIn("| codex | 22 | 10 | 0 |", usage)

    def test_fallback_reasons_follow_the_first_failure_when_rerun_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.local_with_one_fallback(run_dir)
            cli = json.loads((run_dir / "round-1" / "stage1-cli.json").read_text(encoding="utf-8"))
            cli["outcomes"][0] = {"id": "F1:a", "runner": "cursor", "status": "ok", "output": {}, "usage": {}, "detail": ""}
            cli["replaced"] = [{"id": "F1:a", "runner": "codex", "status": "auth", "output": None, "usage": {}, "detail": ""}]
            cli["fallback_runs"] = {"F1:a": {"runner": "cursor", "model": "composer-2.5", "effort": ""}}
            write_json(run_dir / "round-1" / "stage1-cli.json", cli)
            usage = section(self.render(ledger, run_dir), "## 역할별 사용량")
        self.assertIn("| code.a | 2 | 1(로그인) |", usage)

    def test_doc_mode_counts_use_doc_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = profile_ledger(profile_record(), mode="doc", source="claude-doc")
            write_json(run_dir / "round-1" / "results.json",
                       {f"F1:{role}": worker("F1", role) for role in ("a", "b", "writer", "checker", "adjudicator")})
            usage = section(self.render(ledger, run_dir), "## 역할별 사용량")
        for key in ("doc.judge_a", "doc.judge_b", "doc.writer", "doc.checker", "doc.adjudicator"):
            self.assertIn(f"| {key} | 1 | 0 |", usage)
        self.assertNotIn("| code.", usage)

    def test_calls_are_counted_once_when_job_files_and_results_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = profile_ledger(profile_record())
            write_json(run_dir / "round-1" / "stage1-args.json", {"jobs": [job("F1:a"), job("F1:b")]})
            write_json(run_dir / "round-1" / "results.json", {"F1:a": worker("F1", "a"), "F1:b": worker("F1", "b")})
            write_json(run_dir / "round-1" / "adj-args.json", {"jobs": [job("F1:adjudicator")]})
            usage = section(self.render(ledger, run_dir), "## 역할별 사용량")
        self.assertIn("| code.a | 1 | 0 |", usage)
        self.assertIn("| code.adjudicator | 1 | 0 |", usage)

    def test_dash_without_run_dir(self):
        usage = section(report.render(profile_ledger(profile_record()), dry_run=True), "## 역할별 사용량")
        self.assertIn("| code.a | - | - |", usage)

    def test_first_review_row_is_named_by_the_first_review_runner_not_the_opinion_cell(self):
        first = {"role": "code.review", "requested": {"runner": "codex", "model": "", "effort": ""},
                 "actual": {"runner": "claude", "model": "claude-fable-5-1", "effort": "high"},
                 "fallback_reason": "한도", "attempts": [{"runner": "codex", "reason": "한도"}]}
        record = profile_record("local", first_review=first)
        usage = section(report.render(profile_ledger(record, source="claude-code", opinions={"F1": "의견"}), False),
                        "## 역할별 사용량")
        self.assertIn("| Claude 첫 검토 | 1 | 1(한도) |", usage)
        self.assertIn("| Codex 의견 | 1 |", usage)
        self.assertNotIn("Codex 첫 검토", usage)

    def test_first_review_row_names_an_external_first_review(self):
        first = {"role": "code.review", "requested": {"runner": "codex", "model": "", "effort": ""},
                 "actual": {"runner": "codex", "model": "", "effort": ""}, "fallback_reason": None, "attempts": []}
        record = profile_record("local", first_review=first)
        usage = section(report.render(profile_ledger(record, source="codex-code"), False), "## 역할별 사용량")
        self.assertIn("| Codex 첫 검토 | 1 | 0 |", usage)

    def test_review_rows_count_fallback_executions_with_reasons(self):
        events = [{"step": "opinion:F1", "at": "t", "runner": "codex", "model": ""},
                  {"step": "opinion:F2", "at": "t", "runner": "claude", "model": "claude-fable-5-1",
                   "fallback": {"from": "codex", "to": "claude", "reason": "이용 한도"}},
                  {"step": "recheck", "at": "t", "runner": "claude", "model": "claude-fable-5-1",
                   "fallback": {"from": "codex", "to": "claude", "reason": "실행 오류"}}]
        ledger = profile_ledger(profile_record("local"), events=events, opinions={"F1": "a", "F2": "b"}, recheck=True)
        usage = section(report.render(ledger, False), "## 역할별 사용량")
        self.assertIn("| Codex 의견 | 1 | 0 |", usage)
        self.assertIn("| Claude 의견 | 1 | 1(이용 한도) |", usage)
        self.assertIn("| Claude 재검토 | 1 | 1(실행 오류) |", usage)

    def test_legacy_ledger_usage_without_events_keeps_old_rows(self):
        usage = section(report.render(full_ledger(), False), "## 역할별 사용량")
        self.assertIn("| Codex 의견 | 1 | 0 | - |", usage)
        self.assertIn("| Codex 재검토 | 0 | 0 | - |", usage)
        self.assertIn("| 회차 | 소요 |", usage)


SONNET = "claude-sonnet-5-5"


def price_table_source():
    from cascade import usage as price_table
    return price_table.PRICE_SOURCE


def usage_row(job_id, cell, input=4, output=80, cache_read=2000, cost=0.0123, model=SONNET):
    return {"job_id": job_id, "label": f"{job_id} label", "cell": cell, "model": model, "input": input, "output": output,
            "cache_read": cache_read, "cache_write": 0, "cost": cost}


def usage_file(*rows, prices_as_of="2026-10-06", price_source=None):
    data = {"prices_as_of": prices_as_of, "workflows": {"wf_1": list(rows)}, "unmatched": {"wf_1": []}}
    if price_source is not None:
        data["price_source"] = price_source
    return data


class ClaudeUsageColumnsTest(unittest.TestCase):
    HEADER = "| 역할 | 호출 | 대체 실행(사유) | Claude 토큰(입력/출력/캐시 읽기) | Claude 비용(추정) | CLI 토큰 |"

    def render(self, ledger, run_dir):
        return section(report.render(ledger, dry_run=True, run_dir=run_dir), "## 역할별 사용량")

    def code_ledger(self, run_dir, usage=None, jobs=("F1:a", "F1:b")):
        ledger = profile_ledger(profile_record("claude-only"))
        write_json(run_dir / "round-1" / "stage1-args.json", {"jobs": [job(j) for j in jobs]})
        if usage is not None:
            write_json(run_dir / "round-1" / "claude-usage.json", usage)
        return ledger

    def test_usage_columns_from_claude_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a")))
            text = self.render(ledger, run_dir)
        self.assertIn(self.HEADER, text)
        self.assertRegex(text, r"\| code\.a \| 1 \| 0 \| 4/80/2000 \| \$0\.0123 \| - \|")

    def test_stored_cost_is_summed_not_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a", cost=0.5),
                                                          usage_row("F1:b", "code.b", cost=0.25)))
            text = self.render(ledger, run_dir)
        self.assertIn("| code.a | 1 | 0 | 4/80/2000 | $0.5000 | - |", text)
        self.assertIn("합계: Claude 토큰 입력 8 · 출력 160 · 캐시 읽기 4000, 추정 비용 $0.7500", text)

    def test_missing_usage_shows_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            text = self.render(self.code_ledger(run_dir), run_dir)
        self.assertIn("| code.a | 1 | 0 | 미측정 | 미측정 | - |", text)
        self.assertIn("합계: Claude 토큰·비용 미측정(미측정 2건)", text)

    def test_cell_without_claude_jobs_shows_dash_not_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:b", "code.b")), jobs=("F1:b",))
            text = self.render(ledger, run_dir)
        self.assertIn("| code.a | 0 | 0 | - | - | - |", text)
        self.assertIn("| code.adjudicator | 0 | 0 | - | - | - |", text)
        self.assertNotIn("미측정", text)

    def test_partly_measured_cell_names_the_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a")), jobs=("F1:a", "F2:a"))
            text = self.render(ledger, run_dir)
        self.assertIn("| code.a | 2 | 0 | 4/80/2000 외 미측정 1건 | $0.0123 외 미측정 1건 | - |", text)
        self.assertIn("(미측정 1건은 합계에서 빠졌다)", text)

    def test_same_job_id_in_a_resumed_round_without_usage_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a")), jobs=("F1:a",))
            ledger.rounds.append(RoundRecord(no=2, findings=[], decisions=[], adopted=[], recheck_opened=False))
            write_json(run_dir / "round-2" / "stage1-args.json", {"jobs": [job("F1:a")]})
            text = self.render(ledger, run_dir)
        self.assertIn("4/80/2000 외 미측정 1건", text)
        self.assertIn("(미측정 1건은 합계에서 빠졌다)", text)

    def test_row_without_a_price_keeps_tokens_and_marks_cost_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a", cost=None, model="claude-unknown-1")),
                                      jobs=("F1:a",))
            text = self.render(ledger, run_dir)
        self.assertIn("| code.a | 1 | 0 | 4/80/2000 | 미측정 | - |", text)
        self.assertIn("추정 비용 미측정", text)

    def test_total_line_and_price_basis(self):
        from cascade import usage as price_table
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            text = self.render(self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a"))), run_dir)
        self.assertIn("합계: Claude 토큰 입력 4 · 출력 80 · 캐시 읽기 2000, 추정 비용 $0.0123", text)
        self.assertIn(f"단가 기준: {price_table.PRICES_AS_OF} {price_table.PRICE_SOURCE}", text)
        self.assertLess(text.index("합계:"), text.index("단가 기준:"))

    def test_price_basis_shows_the_stored_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a"), prices_as_of="2026-09-01"))
            text = self.render(ledger, run_dir)
        self.assertIn("단가 기준: 2026-09-01 ", text)

    def test_old_result_folder_without_any_record_still_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = profile_ledger(profile_record("claude-only"))
            write_json(run_dir / "round-1" / "results.json", {"F1:a": worker("F1", "a")})
            text = self.render(ledger, run_dir)
        self.assertIn(self.HEADER, text)
        self.assertIn("| code.a | 1 | 0 | - | - | - |", text)
        self.assertIn("합계: Claude 작업 없음", text)

    def test_dry_report_without_run_dir_does_not_claim_there_is_no_claude_work(self):
        text = section(report.render(profile_ledger(profile_record("claude-only")), dry_run=True), "## 역할별 사용량")
        self.assertIn("| code.a | - | - | - | - | - |", text)
        self.assertIn("합계: 알 수 없다(run-dir 없음)", text)

    def test_unreadable_usage_file_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir)
            (run_dir / "round-1" / "claude-usage.json").write_text("{not json", encoding="utf-8")
            text = self.render(ledger, run_dir)
        self.assertIn("| code.a | 1 | 0 | 미측정 | 미측정 | - |", text)

    def test_usage_rows_from_every_round_are_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a", cost=0.1)))
            ledger.rounds.append(RoundRecord(no=2, findings=[], decisions=[], adopted=[], recheck_opened=False))
            write_json(run_dir / "round-2" / "stage1-args.json", {"jobs": [job("F9:a")]})
            write_json(run_dir / "round-2" / "claude-usage.json", usage_file(usage_row("F9:a", "code.a", cost=0.2)))
            text = self.render(ledger, run_dir)
        self.assertIn("| code.a | 2 | 0 | 8/160/4000 | $0.3000 | - |", text)

    def test_acp_cell_shows_cli_tokens_as_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            roles = copy.deepcopy(profiles.builtin("claude-only")["roles"])
            roles["doc.checker"] = {"runner": "cursor", "model": "composer-2.5", "effort": "", "fallback": None}
            ledger = profile_ledger(profile_record("claude-only", roles=roles), mode="doc", source="claude-doc")
            ledger.workers_choice = "external"
            write_json(run_dir / "round-1" / "checker-cli-args.json", {"jobs": [job("F1:checker", "cursor")]})
            write_json(run_dir / "round-1" / "checker-cli.json", {
                "results": [], "fallback_runs": {}, "replaced": [],
                "outcomes": [{"id": "F1:checker", "runner": "cursor", "status": "ok", "output": {},
                              "usage": {"unrecorded": 1}, "detail": ""}]})
            ledger.rounds[0].events.append({"step": "workers-checker", "at": "t", "counts": {"ok": 1}, "fallback": [],
                                            "fallback_runs": {}, "usage": {"cursor": {"unrecorded": 1}}})
            text = self.render(ledger, run_dir)
        self.assertIn("| doc.checker | 1 | 0 | - | - | 기록 없음 |", text)
        self.assertNotIn("입력 0", text)

    def test_first_review_usage_is_shown_on_the_first_review_row(self):
        first = {"role": "code.review", "requested": {"runner": "claude", "model": "", "effort": ""},
                 "actual": {"runner": "claude", "model": SONNET, "effort": "high"}, "fallback_reason": None, "attempts": [],
                 "usage": usage_file(usage_row("review:reviewer", "code.review", input=3, output=20, cache_read=500, cost=0.4))}
        record = profile_record("claude-only", first_review=first)
        text = self.render(profile_ledger(record, source="claude-code"), None)
        self.assertIn("| Claude 첫 검토 | 1 | 0 | 3/20/500 | $0.4000 | - |", text)
        self.assertIn("합계: Claude 토큰 입력 3 · 출력 20 · 캐시 읽기 500, 추정 비용 $0.4000", text)

    def test_claude_first_review_without_usage_is_unmeasured(self):
        first = {"role": "code.review", "requested": {"runner": "claude", "model": "", "effort": ""},
                 "actual": {"runner": "claude", "model": SONNET, "effort": "high"}, "fallback_reason": None, "attempts": []}
        text = self.render(profile_ledger(profile_record("claude-only", first_review=first), source="claude-code"), None)
        self.assertIn("| Claude 첫 검토 | 1 | 0 | 미측정 | 미측정 | - |", text)

    def test_first_review_record_without_runner_is_claude_for_a_claude_source_and_unmeasured_without_usage(self):
        first = {"role": "code.review", "requested": {"runner": "claude", "model": "", "effort": ""}, "attempts": []}
        text = self.render(profile_ledger(profile_record("claude-only", first_review=first), source="claude-code"), None)
        self.assertIn("| Claude 첫 검토 | 1 | 0 | 미측정 | 미측정 | - |", text)

    def test_no_first_review_record_has_no_first_review_row_even_for_a_claude_source(self):
        usage = usage_file(usage_row("F1:a", "code.a", input=3, output=20, cache_read=500, cost=0.4))
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_json(run_dir / "round-1" / "claude-usage.json", usage)
            write_json(run_dir / "round-1" / "stage1-args.json", {"jobs": [job("F1:a")]})
            text = self.render(profile_ledger(profile_record("claude-only", first_review={}), source="claude-code"), run_dir)
        self.assertNotIn("첫 검토", text)
        self.assertNotIn("합계에서 빠졌다", text)

    def test_external_first_review_has_no_claude_columns(self):
        first = {"role": "code.review", "requested": {"runner": "codex", "model": "", "effort": ""},
                 "actual": {"runner": "codex", "model": "", "effort": ""}, "fallback_reason": None, "attempts": []}
        text = self.render(profile_ledger(profile_record("local", first_review=first), source="codex-code"), None)
        self.assertIn("| Codex 첫 검토 | 1 | 0 | - | - | - |", text)

    def test_claude_opinion_row_uses_the_opinion_cell(self):
        events = [{"step": "opinion:F1", "at": "t", "runner": "claude", "model": SONNET}]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = profile_ledger(profile_record("claude-only"), events=events, opinions={"F1": "의견"})
            write_json(run_dir / "round-1" / "opinion-args.json", {"jobs": [job("F1:opinion")]})
            write_json(run_dir / "round-1" / "claude-usage.json",
                       usage_file(usage_row("F1:opinion", "code.opinion", cost=0.05)))
            text = self.render(ledger, run_dir)
        self.assertIn("| Claude 의견 | 1 | 0 | 4/80/2000 | $0.0500 | - |", text)
        self.assertIn("| Claude 재검토 | 0 | 0 | - | - | - |", text)

    def mixed_opinion_ledger(self, run_dir, with_usage):
        events = [{"step": "opinion:F1", "at": "t", "runner": "codex", "model": ""},
                  {"step": "opinion:F2", "at": "t", "runner": "claude", "model": SONNET,
                   "fallback": {"from": "codex", "to": "claude", "reason": "이용 한도"}}]
        ledger = profile_ledger(profile_record("local"), events=events, opinions={"F1": "a", "F2": "b"})
        write_json(run_dir / "round-1" / "opinion-args.json", {"jobs": [job("F2:opinion")]})
        if with_usage:
            write_json(run_dir / "round-1" / "claude-usage.json",
                       usage_file(usage_row("F2:opinion", "code.opinion", cost=0.05)))
        return ledger

    def test_codex_opinion_row_does_not_show_the_claude_cell_usage_or_double_the_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            text = self.render(self.mixed_opinion_ledger(run_dir, True), run_dir)
        self.assertIn("| Codex 의견 | 1 | 0 | - | - | - |", text)
        self.assertIn("| Claude 의견 | 1 | 1(이용 한도) | 4/80/2000 | $0.0500 | - |", text)
        self.assertIn("합계: Claude 토큰 입력 4 · 출력 80 · 캐시 읽기 2000, 추정 비용 $0.0500", text)
        self.assertNotIn("$0.1000", text)

    def test_codex_opinion_row_does_not_double_the_unmeasured_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            text = self.render(self.mixed_opinion_ledger(run_dir, False), run_dir)
        self.assertIn("| Codex 의견 | 1 | 0 | - | - | - |", text)
        self.assertIn("| Claude 의견 | 1 | 1(이용 한도) | 미측정 | 미측정 | - |", text)
        self.assertIn("합계: Claude 토큰·비용 미측정(미측정 1건)", text)

    def test_codex_recheck_row_does_not_show_the_claude_cell_usage_or_double_the_total(self):
        events = [{"step": "recheck", "at": "t", "runner": "codex", "model": ""}]
        ledger = profile_ledger(profile_record("local"), events=events, recheck=True)
        ledger.rounds.append(RoundRecord(no=2, findings=[], decisions=[], adopted=[], recheck_opened=True,
                                         events=[{"step": "recheck", "at": "t", "runner": "claude", "model": SONNET}]))
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_json(run_dir / "round-2" / "recheck-args.json", {"jobs": [job("round-2:recheck")]})
            write_json(run_dir / "round-2" / "claude-usage.json",
                       usage_file(usage_row("round-2:recheck", "code.recheck", cost=0.07)))
            text = self.render(ledger, run_dir)
        self.assertIn("| Codex 재검토 | 1 | 0 | - | - | - |", text)
        self.assertIn("| Claude 재검토 | 1 | 0 | 4/80/2000 | $0.0700 | - |", text)
        self.assertIn("추정 비용 $0.0700", text)
        self.assertNotIn("$0.1400", text)

    def test_tiny_cost_prints_as_less_than_a_unit_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a", cost=0.00002),
                                                          usage_row("F1:b", "code.b", cost=0.0)))
            text = self.render(ledger, run_dir)
        self.assertIn("| code.a | 1 | 0 | 4/80/2000 | <$0.0001 | - |", text)
        self.assertIn("| code.b | 1 | 0 | 4/80/2000 | $0.0000 | - |", text)
        self.assertIn("추정 비용 <$0.0001", text)

    def test_price_basis_uses_the_stored_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a"), prices_as_of="2026-09-01",
                                                          price_source="옛 단가 출처"))
            text = self.render(ledger, run_dir)
        self.assertIn("단가 기준: 2026-09-01 옛 단가 출처", text)
        self.assertNotIn(price_table_source(), text)

    def test_superseded_usage_is_listed_apart_and_not_in_the_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            ledger = self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a", cost=0.5)))
            write_json(run_dir / "round-1" / "claude-usage.superseded-1.json", usage_file(usage_row("F1:a", "code.a", cost=0.2)))
            write_json(run_dir / "round-1" / "claude-usage.superseded-2.json", usage_file(usage_row("F1:a", "code.a", cost=0.3)))
            text = self.render(ledger, run_dir)
        self.assertIn("다시 연 회차의 이전 Claude 비용: $0.5000(별도, 합계에 넣지 않음)", text)
        self.assertIn("| code.a | 1 | 0 | 4/80/2000 | $0.5000 | - |", text)
        self.assertIn("추정 비용 $0.5000", text)

    def test_no_superseded_line_without_superseded_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            text = self.render(self.code_ledger(run_dir, usage_file(usage_row("F1:a", "code.a"))), run_dir)
        self.assertNotIn("다시 연 회차", text)

    def test_claude_opinion_without_usage_is_unmeasured(self):
        events = [{"step": "opinion:F1", "at": "t", "runner": "claude", "model": SONNET}]
        ledger = profile_ledger(profile_record("claude-only"), events=events, opinions={"F1": "의견"})
        text = self.render(ledger, None)
        self.assertIn("| Claude 의견 | 1 | 0 | 미측정 | 미측정 | - |", text)


class PreparedTest(unittest.TestCase):
    def test_prepared_files_written_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo", {"a.txt": "x\n"})
            before_log = git(repo, "log", "--oneline")
            run_dir = repo / ".cascade-run"
            report.write_prepared(run_dir, full_ledger())
            self.assertEqual(git(repo, "log", "--oneline"), before_log)
            staged = git(repo, "diff", "--cached", "--name-only")
            self.assertEqual(staged, "")
            message = (run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8")
            self.assertTrue(message.startswith("fix: 2건의 리뷰 지적 반영\n\n- F1 a.py:1: 합의 사실"))
            self.assertNotIn("```", message)
            self.assertEqual(json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8")), {})
            self.assertEqual(json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8")), [])

    def test_prepared_pr_comment_files(self):
        findings = [finding("F1", thread_id="T1"), finding("F3", thread_id="T3")]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["통과"]),
            decision("F3", OUTCOME_AGREED_NO, None, ["a: 없음", "b: 없음"]),
        ]
        ledger = make_ledger(findings, decisions, source="pr-comment")
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report.write_prepared(run_dir, ledger)
            replies = json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8"))
            resolve = json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8"))
        self.assertEqual(set(replies), {"T1", "T3"})
        self.assertTrue(replies["T1"].startswith("반영했습니다."))
        self.assertTrue(replies["T3"].startswith("확인 결과"))
        self.assertEqual(resolve, ["T1"])

    def test_dry_run_pr_comment_writes_no_replies_or_resolve(self):
        findings = [finding("F1", thread_id="T1"), finding("F3", thread_id="T3")]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["통과"]),
            decision("F3", OUTCOME_AGREED_NO, None, ["a: 없음", "b: 없음"]),
        ]
        ledger = make_ledger(findings, decisions, source="pr-comment", dry_run=True)
        text = report.render(ledger, True)
        self.assertNotIn("반영했습니다", text)
        self.assertIn("적용 안 됨(dry-run)", section(text, "### PR 답글 초안"))
        self.assertIn("적용 안 됨(dry-run)", section(text, "### resolve 대상"))
        self.assertNotIn("- T1", section(text, "### resolve 대상"))
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report.write_prepared(run_dir, ledger)
            self.assertEqual(json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8")), {})
            self.assertEqual(json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8")), [])


class ReplyDraftTest(unittest.TestCase):
    def _replies(self, findings, decisions, results=None, apply=None):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            if results is not None:
                write_round_file(run_dir, 1, "results.json", results)
            if apply is not None:
                write_round_file(run_dir, 1, "apply.json", apply)
            report.write_prepared(run_dir, make_ledger(findings, decisions, source="pr-comment"))
            prepared = run_dir / "prepared"
            return (json.loads((prepared / "replies.json").read_text(encoding="utf-8")),
                    json.loads((prepared / "resolve.json").read_text(encoding="utf-8")))

    def test_replies_hide_internal_reasons(self):
        findings = [finding(f"F{i}", thread_id=f"T{i}", body=f"지적 {i}\n둘째 줄") for i in range(1, 8)]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["검증기 없음"]),
            decision("F2", OUTCOME_ADJUDICATE, "adjudicator", ["패치가 다름", "판가름: b가 맞다"]),
            decision("F3", OUTCOME_ADJUDICATE, None, ["판정이 갈림: a=사실, b=아님", "판가름: 아님", "판가름: 이미 있다"]),
            decision("F4", OUTCOME_UNDECIDABLE, None, ["보안 경로"]),
            decision("F5", OUTCOME_AGREED_NO, None, ["a: 짧은 a", "b: 짧은 b"]),
            decision("F6", OUTCOME_AGREED_NO, None, ["a: 코드에 없음", "b: 코드에 없음"]),
            decision("F7", OUTCOME_ADJUDICATE, None, ["판정이 갈림: a=사실, b=아님", "판가름: 아님"]),
        ]
        results = {
            "F3:a": worker("F3", "a", "고쳐야 한다"),
            "F3:b": worker("F3", "b", "이미 있다"),
            "F5:a": worker("F5", "a", "A 근거 상세"),
            "F5:b": worker("F5", "b", "B 근거 상세"),
        }
        replies, _ = self._replies(findings, decisions, results)
        self.assertEqual(replies, {
            "T1": "반영했습니다. 지적 1",
            "T2": "반영했습니다. 지적 2",
            "T3": "확인 결과 고치지 않았습니다. 이미 있다",
            "T4": "검토 중입니다.",
            "T5": "확인 결과 고치지 않았습니다. A 근거 상세 / B 근거 상세",
            "T6": "확인 결과 고치지 않았습니다. 코드에 없음",
            "T7": "확인 결과 고치지 않았습니다.",
        })

    def test_rebuttal_reply_prefers_adjudicator_result(self):
        findings = [finding("F1", thread_id="T1")]
        decisions = [decision("F1", OUTCOME_ADJUDICATE, None, ["패치가 다름", "판가름: 아님", "판가름: 짧은 판가름"])]
        results = {"F1:adjudicator": worker("F1", "adjudicator", "판가름 상세 근거\n")}
        replies, _ = self._replies(findings, decisions, results)
        self.assertEqual(replies["T1"], "확인 결과 고치지 않았습니다. 판가름 상세 근거")

    def test_one_thread_with_two_findings_merges_replies(self):
        findings = [finding("F1", thread_id="T1", body="고칠 것"), finding("F2", thread_id="T1", body="아닌 것")]
        decisions = [decision("F1", OUTCOME_AGREED_FACT, "a", []),
                     decision("F2", OUTCOME_AGREED_NO, None, ["a: 코드에 없음", "b: 코드에 없음"])]
        replies, resolve = self._replies(findings, decisions)
        self.assertEqual(replies, {"T1": "> 고칠 것\n반영했습니다.\n\n> 아닌 것\n확인 결과 고치지 않았습니다. 코드에 없음"})
        self.assertEqual(resolve, [])

    def test_thread_resolved_only_when_every_finding_applied(self):
        findings = [finding("F1", thread_id="T1", body="고칠 것 1"), finding("F2", thread_id="T1", body="고칠 것 2"),
                    finding("F3", thread_id="T2", body="고칠 것 3"), finding("F4", thread_id="T2", body="고칠 것 4")]
        decisions = [decision(fid, OUTCOME_AGREED_FACT, "a", []) for fid in ("F1", "F2", "F3", "F4")]
        replies, resolve = self._replies(findings, decisions, apply={"applied": ["F1", "F2", "F3"],
                                                                     "failed": [["F4", "충돌"]]})
        self.assertEqual(resolve, ["T1"])
        self.assertEqual(replies["T2"], "> 고칠 것 3\n반영했습니다.\n\n> 고칠 것 4\n적용 실패: 충돌. 검토 중입니다.")


class RerunRoundsTest(unittest.TestCase):
    def test_resolve_follows_final_decision(self):
        ledger = rerun_ledger(decision("r1-03", OUTCOME_AGREED_FACT, "a", []),
                              decision("r1-03", OUTCOME_UNDECIDABLE, None, ["둘 다 검증 실패"]))
        text = report.render(ledger, False)
        self.assertIn("thread T3:\n검토 중입니다.\n", section(text, "### PR 답글 초안"))
        self.assertNotIn("- T3", section(text, "### resolve 대상"))
        readopted = rerun_ledger(decision("r1-03", OUTCOME_UNDECIDABLE, None, ["둘 다 검증 실패"]),
                                 decision("r1-03", OUTCOME_AGREED_FACT, "a", []))
        self.assertIn("- T3", section(report.render(readopted, False), "### resolve 대상"))

    def test_carried_finding_replaces_its_original_in_the_thread(self):
        original = finding("r1-02", thread_id="T2", body="겹친 지적")
        carried = {**finding("r2-01", thread_id="T2", body="겹친 지적"), "carried_from": "r1-02"}
        r1 = RoundRecord(no=1, findings=[original], decisions=[decision("r1-02", OUTCOME_CARRIED, None, ["겹침"])],
                         adopted=[], recheck_opened=False)
        r2 = RoundRecord(no=2, findings=[carried], decisions=[decision("r2-01", OUTCOME_AGREED_FACT, "a", [])],
                         adopted=["r2-01"], recheck_opened=False, final=True)
        ledger = Ledger(run_id="run-1", source="pr-comment", mode="code", root="/tmp/x", snapshot={}, dry_run=False,
                        rounds=[r1, r2])
        text = report.render(ledger, False)
        self.assertIn("thread T2:\n반영했습니다. 겹친 지적\n", section(text, "### PR 답글 초안"))
        self.assertIn("- T2", section(text, "### resolve 대상"))

    def test_reviewer_column_counts_only_round_with_opinion_event(self):
        ledger = rerun_ledger(decision("r1-03", OUTCOME_AGREED_FACT, "a", []),
                              decision("r1-03", OUTCOME_UNDECIDABLE, None, ["판가름 못 정함"]),
                              source="codex-code", opinions={"r1-03": "회차 3 의견"},
                              r3_events=[{"step": "opinion:r1-03", "at": "2026-10-17T10:00:00+00:00"}])
        table = section(report.render(ledger, False), "## 회차별 지적과 채택 경로")
        self.assertIn("| 2 | 1 | 1 | 0 | 0 | 0 | 0 |", table)
        self.assertIn("| 3 | 1 | 0 | 1 | 0 | 1 | 1 |", table)

    def test_apply_failure_in_later_round_keeps_earlier_applied_commit(self):
        ledger = rerun_ledger(decision("r1-03", OUTCOME_AGREED_FACT, "a", []), decision("r1-03", OUTCOME_AGREED_FACT, "a", []))
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_round_file(run_dir, 2, "apply.json", {"applied": ["r1-03"], "failed": []})
            write_round_file(run_dir, 3, "apply.json", {"applied": [], "failed": [["r1-03", "patch failed"]]})
            report.write_prepared(run_dir, ledger)
            prepared = run_dir / "prepared"
            message = (prepared / "commit-message.txt").read_text(encoding="utf-8")
            replies = json.loads((prepared / "replies.json").read_text(encoding="utf-8"))
            resolve = json.loads((prepared / "resolve.json").read_text(encoding="utf-8"))
        self.assertEqual(message, "fix: 같은 지적\n\n- r1-03 src/a.py:10: 같은 지적\n")
        self.assertEqual(replies, {"T3": "적용 실패: patch failed. 검토 중입니다."})
        self.assertEqual(resolve, [])


class ReportMinorTest(unittest.TestCase):
    def test_commit_bullet_without_body_has_no_trailing_space(self):
        ledger = make_ledger([finding("F1", body=""), finding("F2", body="둘째")],
                             [decision("F1", OUTCOME_AGREED_FACT, "a"), decision("F2", OUTCOME_AGREED_FACT, "a")])
        bullets = [line for line in report._commit_message(ledger, {}).splitlines() if line.startswith("- ")]
        self.assertEqual(bullets, ["- F1 src/a.py:10", "- F2 src/a.py:10: 둘째"])

    def _undecidable(self, mode, adj_patch):
        ledger = make_ledger([finding("r1-01", location="plan.md:3")],
                             [decision("r1-01", OUTCOME_UNDECIDABLE, None, ["점검자 아니오", "판가름 패치 검증 실패"])])
        ledger.mode = mode
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            if adj_patch:
                (run_dir / "round-1" / "patch-r1-01-adj.diff").write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
            return section(report.render(ledger, False, run_dir=run_dir), "### 판단 불가")

    def test_undecidable_patch_paths_follow_mode_and_adjudicator_patch(self):
        doc = self._undecidable("doc", adj_patch=False)
        self.assertIn("  - 패치 작성자: round-1/patch-r1-01-writer.diff", doc)
        self.assertNotIn("patch-r1-01-a.diff", doc)
        self.assertNotIn("patch-r1-01-b.diff", doc)
        self.assertNotIn("patch-r1-01-adj.diff", doc)
        self.assertIn("  - 패치 판가름: round-1/patch-r1-01-adj.diff", self._undecidable("doc", adj_patch=True))
        code = self._undecidable("code", adj_patch=True)
        self.assertIn("  - 패치 a: round-1/patch-r1-01-a.diff", code)
        self.assertIn("  - 패치 b: round-1/patch-r1-01-b.diff", code)
        self.assertIn("  - 패치 판가름: round-1/patch-r1-01-adj.diff", code)
        self.assertNotIn("writer.diff", code)
        self.assertNotIn("adj.diff", self._undecidable("code", adj_patch=False))


class BaselineAndTimingTest(unittest.TestCase):
    def test_baseline_section_lists_excluded_commands(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            (run_dir / "round-1" / "baseline.json").write_text(json.dumps({"failed": ["ruff check ."]}), encoding="utf-8")
            (run_dir / "round-1" / "apply.json").write_text(json.dumps({"applied": [], "failed": [], "baseline_failed": True}),
                                                            encoding="utf-8")
            text = report.render(ledger, False, run_dir)
        body = section(text, "## 기준선 실패")
        self.assertIn("`ruff check .`", body)
        self.assertIn("기준선(수정 전)에서도 실패했다", body)
        self.assertNotIn("## 기준선 실패", report.render(ledger, False))

    def test_report_events_do_not_count_in_elapsed(self):
        events = [{"step": "init", "at": "2026-10-17T10:00:00+09:00"}, {"step": "apply", "at": "2026-10-17T10:00:30+09:00"},
                  {"step": "report", "at": "2026-10-17T12:00:00+09:00", "round": 1}]
        ledger = make_ledger([finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")], events=events)
        self.assertIn("| 1 | 30초 |", report.render(ledger, False))

    def test_usage_events_do_not_count_in_elapsed(self):
        events = [{"step": "init", "at": "2026-10-17T10:00:00+09:00"}, {"step": "apply", "at": "2026-10-17T10:00:30+09:00"},
                  {"step": "usage", "at": "2026-10-17T12:00:00+09:00", "workflow": "wf_1", "jobs": 2, "unmatched": 0,
                   "error": None}]
        ledger = make_ledger([finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")], events=events)
        self.assertIn("| 1 | 30초 |", report.render(ledger, False))


if __name__ == "__main__":
    unittest.main()
