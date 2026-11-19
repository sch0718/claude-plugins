import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import consensus  # noqa: E402
from cascade.consensus import Decision, WorkerResult  # noqa: E402
from cascade.verify import VerifyResult  # noqa: E402

PATCH_X = (
    "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
)
PATCH_Y = (
    "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 3\n"
)
PATCH_BIG = (
    "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1,2 @@\n-x = 1\n+x = 4\n+y = 5\n"
)


def worker(role="a", verdict=consensus.VERDICT_FACT, design=False, reason="근거", patch="", checker_ok=None):
    return WorkerResult(
        finding_id="f1",
        role=role,
        verdict=verdict,
        needs_design_decision=design,
        reason=reason,
        verify=[],
        checker_ok=checker_ok,
        model_id="m",
        patch=patch,
    )


def ok(cmd="pytest"):
    return VerifyResult(cmd=cmd, ok=True, tail="")


def fail(cmd="pytest"):
    return VerifyResult(cmd=cmd, ok=False, tail="실패")


def skipped(cmd='test -z "" || pytest'):
    return VerifyResult(cmd=cmd, ok=False, tail="", skipped=True)


class DecideTest(unittest.TestCase):
    def test_both_no(self):
        a = worker("a", consensus.VERDICT_NO, reason="이미 처리됨")
        b = worker("b", consensus.VERDICT_NO, reason="호출 경로 없음")
        d = consensus.decide(a, b, [ok()], [ok()], "", "")
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_NO)
        self.assertIsNone(d.patch_from)
        self.assertIn("a: 이미 처리됨", d.reasons)
        self.assertIn("b: 호출 경로 없음", d.reasons)

    def test_both_yes_same_patch_pass(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_FACT)
        self.assertEqual(d.patch_from, "a")

    def test_both_yes_diff_patch_pass_escalates(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_Y)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIsNone(d.patch_from)
        self.assertIn("패치가 다름", d.reasons)

    def test_one_pass_escalates_with_candidate(self):
        d = consensus.decide(worker("a"), worker("b"), [fail()], [ok()], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.patch_from, "b")
        self.assertIn("한쪽만 검증 통과", d.reasons)
        d = consensus.decide(worker("a"), worker("b"), [ok(), ok()], [ok(), fail()], PATCH_X, PATCH_X)
        self.assertEqual(d.patch_from, "a")

    def test_both_fail_escalates(self):
        d = consensus.decide(worker("a"), worker("b"), [fail()], [fail()], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIsNone(d.patch_from)
        self.assertIn("둘 다 검증 실패", d.reasons)

    def test_both_need_design_goes_human(self):
        a = worker("a", design=True, reason="저장 형식을 정해야 한다")
        b = worker("b", design=True, reason="JSON인지 TOML인지 문서에 없다")
        d = consensus.decide(a, b, [ok()], [ok()], PATCH_X, PATCH_Y)
        self.assertEqual(d.outcome, consensus.OUTCOME_DESIGN)
        self.assertIsNone(d.patch_from)
        self.assertEqual(d.reasons, ["a: 저장 형식을 정해야 한다", "b: JSON인지 TOML인지 문서에 없다"])

    def test_split_verdict_escalates(self):
        d = consensus.decide(
            worker("a", consensus.VERDICT_FACT), worker("b", consensus.VERDICT_NO), [ok()], [], PATCH_X, ""
        )
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIsNone(d.patch_from)
        self.assertTrue(any(consensus.VERDICT_FACT in r and consensus.VERDICT_NO in r for r in d.reasons))
        d = consensus.decide(
            worker("a", consensus.VERDICT_UNREPRODUCIBLE), worker("b", consensus.VERDICT_UNDECIDED), [], [], "", ""
        )
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)

    def test_one_needs_design_escalates(self):
        d = consensus.decide(worker("a", design=True), worker("b"), [ok()], [ok()], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIn("설계 결정 필요 여부 갈림", d.reasons)

    def test_both_unreproducible_escalates(self):
        a = worker("a", consensus.VERDICT_UNREPRODUCIBLE)
        b = worker("b", consensus.VERDICT_UNREPRODUCIBLE)
        d = consensus.decide(a, b, [], [], "", "")
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIn("둘 다 재현 불가", d.reasons)

    def test_both_undecided_is_human(self):
        a = worker("a", consensus.VERDICT_UNDECIDED)
        b = worker("b", consensus.VERDICT_UNDECIDED)
        d = consensus.decide(a, b, [], [], "", "")
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertEqual(d.reasons, ["둘 다 못 정함", "a: 근거", "b: 근거"])

    def test_both_undecided_keeps_call_failure_per_worker(self):
        a = worker("a", consensus.VERDICT_UNDECIDED, reason="호출 실패")
        b = worker("b", consensus.VERDICT_UNDECIDED, reason="요구사항이 불분명")
        d = consensus.decide(a, b, [], [], "", "")
        self.assertEqual(d.reasons, ["둘 다 못 정함", "a: 호출 실패", "b: 요구사항이 불분명"])

    def test_sensitive_path_never_adopted(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_X, sensitive=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIsNone(d.patch_from)
        self.assertIn("보안 경로", d.reasons)

    def test_sensitive_path_keeps_non_adopting_outcome_with_reason(self):
        a = worker("a", consensus.VERDICT_NO)
        b = worker("b", consensus.VERDICT_NO)
        d = consensus.decide(a, b, [], [], "", "", sensitive=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_NO)
        self.assertIn("보안 경로", d.reasons)
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_Y, sensitive=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIn("보안 경로", d.reasons)

    def test_cut_tests_never_adopted(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_X, tests_cut=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIsNone(d.patch_from)
        self.assertIn("기존 테스트 변경", d.reasons)
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_BIG, PATCH_X,
                             tests_cut=True, adopt_on_both_pass=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_Y, tests_cut=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIn("기존 테스트 변경", d.reasons)

    def test_adopt_on_both_pass_takes_smaller_patch(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_BIG, PATCH_X, adopt_on_both_pass=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_FACT)
        self.assertEqual(d.patch_from, "b")
        self.assertEqual(d.reasons, ["둘 다 검증 통과: 작은 패치 b 채택"])
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_BIG, adopt_on_both_pass=True)
        self.assertEqual(d.patch_from, "a")
        self.assertEqual(d.reasons, ["둘 다 검증 통과: 작은 패치 a 채택"])

    def test_adopt_on_both_pass_counts_removed_lines_that_look_like_headers(self):
        removes_comments = (
            "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,4 +1 @@\n"
            "--- 주석 1\n--- 주석 2\n--- 주석 3\n select 1;\n"
        )
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], removes_comments, PATCH_X, adopt_on_both_pass=True)
        self.assertEqual(d.patch_from, "b")

    def test_adopt_on_both_pass_same_size_takes_a(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_Y, adopt_on_both_pass=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_FACT)
        self.assertEqual(d.patch_from, "a")
        self.assertEqual(d.reasons, ["둘 다 검증 통과: 크기가 같아 a 채택"])

    def test_adopt_on_both_pass_needs_verifiers_and_both_patches(self):
        d = consensus.decide(worker("a"), worker("b"), [], [ok()], PATCH_X, PATCH_Y, adopt_on_both_pass=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.reasons, ["패치가 다름", "검증기 없음"])
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], "", PATCH_Y, adopt_on_both_pass=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.reasons, ["패치가 다름"])

    def test_adopt_on_both_pass_needs_tests_that_ran_on_both_sides(self):
        for va, vb in (([ok("lint"), skipped()], [ok("lint"), skipped()]), ([ok()], [ok("lint"), skipped()])):
            d = consensus.decide(worker("a"), worker("b"), va, vb, PATCH_X, PATCH_BIG, adopt_on_both_pass=True)
            self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
            self.assertEqual(d.reasons, ["패치가 다름", "테스트가 돌지 않음"])

    def test_same_patch_with_tests_not_run_is_still_agreed_with_note(self):
        d = consensus.decide(worker("a"), worker("b"), [skipped()], [skipped()], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_FACT)
        self.assertEqual(d.patch_from, "a")
        self.assertEqual(d.reasons, ["테스트가 돌지 않음"])

    def test_skipped_tests_are_not_failures(self):
        d = consensus.decide(worker("a"), worker("b"), [ok("lint"), skipped()], [fail()], PATCH_X, PATCH_Y)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.patch_from, "a")
        self.assertEqual(d.reasons, ["한쪽만 검증 통과", "테스트가 돌지 않음"])

    def test_adopt_on_both_pass_leaves_one_pass_to_adjudication(self):
        d = consensus.decide(worker("a"), worker("b"), [fail()], [ok()], PATCH_X, PATCH_BIG, adopt_on_both_pass=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.patch_from, "b")
        self.assertIn("한쪽만 검증 통과", d.reasons)

    def test_adopt_on_both_pass_sensitive_goes_human(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_BIG,
                             sensitive=True, adopt_on_both_pass=True)
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIsNone(d.patch_from)
        self.assertEqual(d.reasons, ["둘 다 검증 통과: 작은 패치 a 채택", "보안 경로"])

    def test_no_verifiers_adopts_with_note(self):
        d = consensus.decide(worker("a"), worker("b"), [], [], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_FACT)
        self.assertEqual(d.patch_from, "a")
        self.assertIn("검증기 없음", d.reasons)

    def test_unknown_verdict_raises(self):
        with self.assertRaises(ValueError):
            consensus.decide(worker("a", verdict="모름"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_X)
        with self.assertRaises(ValueError):
            consensus.decide_doc(worker("a"), worker("b", verdict=""), None, [])

    def test_one_side_empty_verify_adopts_with_note(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_FACT)
        self.assertIn("검증기 없음", d.reasons)
        d = consensus.decide(worker("a"), worker("b"), [], [fail()], PATCH_X, PATCH_X)
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.patch_from, "a")
        self.assertIn("검증기 없음", d.reasons)

    def test_decision_carries_finding_id(self):
        d = consensus.decide(worker("a"), worker("b"), [ok()], [ok()], PATCH_X, PATCH_X)
        self.assertEqual(d.finding_id, "f1")


class DecideDocTest(unittest.TestCase):
    def judges(self, **kw):
        return worker("a", **kw), worker("b", **kw)

    def test_doc_agreed(self):
        a, b = self.judges()
        d = consensus.decide_doc(a, b, worker("checker", checker_ok=True), [])
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_FACT)
        self.assertEqual(d.patch_from, "writer")

    def test_doc_checker_no_escalates(self):
        a, b = self.judges()
        d = consensus.decide_doc(a, b, worker("checker", checker_ok=False), [])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIsNone(d.patch_from)
        self.assertIn("점검자 아니오", d.reasons)

    def test_doc_no_checker_escalates(self):
        a, b = self.judges()
        d = consensus.decide_doc(a, b, None, [])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIn("점검자 없음", d.reasons)

    def test_doc_checker_without_result_escalates(self):
        a, b = self.judges()
        d = consensus.decide_doc(a, b, worker("checker", checker_ok=None), [])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIn("점검자 결과 없음", d.reasons)

    def test_doc_missing_refs_escalates(self):
        a, b = self.judges()
        d = consensus.decide_doc(a, b, worker("checker", checker_ok=True), ["docs/x.md", "y.py"])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIn("참조 없음: docs/x.md, y.py", d.reasons)

    def test_doc_verdict_rules_apply(self):
        d = consensus.decide_doc(
            worker("a", consensus.VERDICT_NO), worker("b", consensus.VERDICT_NO), None, ["x"]
        )
        self.assertEqual(d.outcome, consensus.OUTCOME_AGREED_NO)
        a, b = self.judges(design=True)
        self.assertEqual(consensus.decide_doc(a, b, None, []).outcome, consensus.OUTCOME_DESIGN)
        d = consensus.decide_doc(worker("a", design=True), worker("b"), None, [])
        self.assertIn("설계 결정 필요 여부 갈림", d.reasons)
        d = consensus.decide_doc(worker("a"), worker("b", consensus.VERDICT_NO), None, [])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        a, b = self.judges(verdict=consensus.VERDICT_UNDECIDED)
        self.assertEqual(consensus.decide_doc(a, b, None, []).outcome, consensus.OUTCOME_UNDECIDABLE)


class AfterAdjudicationTest(unittest.TestCase):
    def escalated(self, patch_from=None):
        return Decision(
            finding_id="f1", outcome=consensus.OUTCOME_ADJUDICATE, patch_from=patch_from, reasons=["패치가 다름"]
        )

    def test_adjudicator_undecided_goes_human(self):
        adj = worker("adjudicator", consensus.VERDICT_UNDECIDED, reason="모름")
        d = consensus.after_adjudication(self.escalated(), adj, [])
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIn("판가름 못 정함", d.reasons)
        self.assertIn("패치가 다름", d.reasons)
        self.assertIn("판가름: 모름", d.reasons)

    def test_adjudicator_patch_fail_goes_human(self):
        adj = worker("adjudicator", patch=PATCH_X)
        d = consensus.after_adjudication(self.escalated(), adj, [ok(), fail()])
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIn("판가름 패치 검증 실패", d.reasons)
        self.assertIsNone(d.patch_from)

    def test_adjudicator_patch_with_tests_not_run_is_not_a_failure(self):
        adj = worker("adjudicator", patch=PATCH_X, reason="a가 맞음")
        d = consensus.after_adjudication(self.escalated(), adj, [ok("lint"), skipped()])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.patch_from, "adjudicator")

    def test_adjudicator_adopts_own_patch(self):
        adj = worker("adjudicator", patch=PATCH_X, reason="a가 맞음")
        d = consensus.after_adjudication(self.escalated(), adj, [ok()])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.patch_from, "adjudicator")
        self.assertEqual(d.reasons[-1], "판가름: a가 맞음")

    def test_after_adjudication_keeps_candidate_when_no_patch(self):
        adj = worker("adjudicator", patch="")
        d = consensus.after_adjudication(self.escalated("b"), adj, [ok()])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertEqual(d.patch_from, "b")

    def test_adjudicator_no_is_rebuttal(self):
        adj = worker("adjudicator", consensus.VERDICT_NO, reason="문제 아님")
        d = consensus.after_adjudication(self.escalated("a"), adj, [])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertIsNone(d.patch_from)
        self.assertIn("판가름: 아님", d.reasons)
        self.assertIn("판가름: 문제 아님", d.reasons)

    def test_after_adjudication_never_adopts_sensitive(self):
        sensitive = Decision(
            finding_id="f1", outcome=consensus.OUTCOME_ADJUDICATE, patch_from=None, reasons=["패치가 다름", "보안 경로"]
        )
        d = consensus.after_adjudication(sensitive, worker("adjudicator", patch=PATCH_X), [ok()])
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIsNone(d.patch_from)
        self.assertIn("보안 경로: 자동 채택 안 함", d.reasons)
        candidate = Decision(
            finding_id="f1", outcome=consensus.OUTCOME_ADJUDICATE, patch_from="b", reasons=["한쪽만 검증 통과", "보안 경로"]
        )
        d = consensus.after_adjudication(candidate, worker("adjudicator", patch=""), [ok()])
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIsNone(d.patch_from)
        for verdict, design, outcome in (
            (consensus.VERDICT_NO, False, consensus.OUTCOME_ADJUDICATE),
            (consensus.VERDICT_FACT, True, consensus.OUTCOME_DESIGN),
            (consensus.VERDICT_UNDECIDED, False, consensus.OUTCOME_UNDECIDABLE),
        ):
            d = consensus.after_adjudication(candidate, worker("adjudicator", verdict, design), [])
            self.assertEqual(d.outcome, outcome)
            self.assertNotIn("보안 경로: 자동 채택 안 함", d.reasons)

    def test_after_adjudication_never_adopts_cut_tests(self):
        cut = Decision(
            finding_id="f1", outcome=consensus.OUTCOME_ADJUDICATE, patch_from="b", reasons=["한쪽만 검증 통과", "기존 테스트 변경"]
        )
        d = consensus.after_adjudication(cut, worker("adjudicator", patch=PATCH_X), [ok()])
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)
        self.assertIsNone(d.patch_from)
        self.assertIn("기존 테스트 변경: 자동 채택 안 함", d.reasons)
        d = consensus.after_adjudication(cut, worker("adjudicator", consensus.VERDICT_NO), [])
        self.assertEqual(d.outcome, consensus.OUTCOME_ADJUDICATE)
        self.assertNotIn("기존 테스트 변경: 자동 채택 안 함", d.reasons)

    def test_adjudicator_unknown_verdict_raises(self):
        with self.assertRaises(ValueError):
            consensus.after_adjudication(self.escalated(), worker("adjudicator", verdict="??"), [ok()])

    def test_adjudicator_needs_design(self):
        adj = worker("adjudicator", design=True)
        d = consensus.after_adjudication(self.escalated(), adj, [])
        self.assertEqual(d.outcome, consensus.OUTCOME_DESIGN)
        self.assertIsNone(d.patch_from)

    def test_adjudicator_unreproducible_goes_human(self):
        adj = worker("adjudicator", consensus.VERDICT_UNREPRODUCIBLE)
        d = consensus.after_adjudication(self.escalated(), adj, [])
        self.assertEqual(d.outcome, consensus.OUTCOME_UNDECIDABLE)

    def test_only_for_adjudicate_outcome(self):
        d = Decision(finding_id="f1", outcome=consensus.OUTCOME_AGREED_NO, patch_from=None, reasons=[])
        with self.assertRaises(ValueError):
            consensus.after_adjudication(d, worker("adjudicator"), [])

    def test_does_not_mutate_input(self):
        original = self.escalated()
        consensus.after_adjudication(original, worker("adjudicator", patch=PATCH_X), [ok()])
        self.assertEqual(original.reasons, ["패치가 다름"])


class RoundtripTest(unittest.TestCase):
    def test_roundtrip_dicts(self):
        w = WorkerResult(
            finding_id="f1", role="checker", verdict=consensus.VERDICT_FACT, needs_design_decision=False,
            reason="r", verify=[{"cmd": "pytest", "ok": True}], checker_ok=True, model_id="m", patch=PATCH_X,
        )
        self.assertEqual(WorkerResult.from_dict(w.to_dict()), w)
        d = Decision(finding_id="f1", outcome=consensus.OUTCOME_ADJUDICATE, patch_from="a", reasons=["x"])
        self.assertEqual(Decision.from_dict(d.to_dict()), d)

    def test_worker_from_dict_defaults_patch(self):
        data = worker().to_dict()
        del data["patch"]
        self.assertEqual(WorkerResult.from_dict(data).patch, "")


if __name__ == "__main__":
    unittest.main()
