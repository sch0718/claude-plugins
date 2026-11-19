import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import rounds  # noqa: E402
from cascade.config import RepoConfig  # noqa: E402
from cascade.consensus import WorkerResult  # noqa: E402
from cascade.findings import Finding, ParseError  # noqa: E402
from cascade.jobs import AdjInput  # noqa: E402


def finding(fid, severity, location, body="본문 첫 줄\n둘째 줄"):
    return Finding(id=fid, source="codex-doc", severity=severity, location=location, body=body, evidence="근거")


CRIT_AT_L10 = finding("r2-01", "Critical", "docs/a.md:10")
PREV_CRIT = finding("r1-01", "Critical", "docs/a.md:10")
WARN = finding("r1-02", "Warning", "docs/a.md:20")
SUGG = finding("r1-03", "Suggestion", "docs/a.md:30")


def record(findings, adopted, no=1, final=False):
    return rounds.RoundRecord(
        no=no,
        findings=[f.to_dict() for f in findings],
        decisions=[],
        adopted=adopted,
        recheck_opened=False,
        final=final,
    )


def worker(role, verdict, reason, patch=""):
    return WorkerResult(
        finding_id="r1-01",
        role=role,
        verdict=verdict,
        needs_design_decision=False,
        reason=reason,
        verify=[],
        checker_ok=None,
        model_id="m",
        patch=patch,
    )


class NeedsRecheckTest(unittest.TestCase):
    def test_needs_recheck_only_for_critical_warning_adopted(self):
        rec = record([PREV_CRIT, WARN, SUGG], ["r1-02"])
        self.assertTrue(rounds.needs_recheck(rec, 2, 0))
        self.assertFalse(rounds.needs_recheck(record([PREV_CRIT, WARN, SUGG], ["r1-03"]), 2, 0))
        self.assertFalse(rounds.needs_recheck(record([PREV_CRIT, WARN, SUGG], []), 2, 0))
        self.assertFalse(rounds.needs_recheck(record([PREV_CRIT, WARN, SUGG], ["r1-01"]), 2, 2))
        self.assertTrue(rounds.needs_recheck(record([PREV_CRIT], ["r1-01"]), 2, 1))

    def test_needs_recheck_ignores_findings_without_id_or_severity(self):
        rec = record([WARN], ["r1-02"])
        rec.findings = [{"severity": "Warning"}, {"id": "r1-09"}, *rec.findings]
        self.assertTrue(rounds.needs_recheck(rec, 2, 0))
        rec.adopted = ["r1-09"]
        self.assertFalse(rounds.needs_recheck(rec, 2, 0))

    def test_final_round_never_needs_recheck(self):
        self.assertFalse(rounds.needs_recheck(record([PREV_CRIT], ["r1-01"], final=True), 2, 0))

    def test_suggestion_only_never_opens_round(self):
        self.assertFalse(rounds.needs_recheck(record([SUGG], ["r1-03"]), 2, 0))
        nxt, hold, undec = rounds.classify_recheck([SUGG], record([WARN], ["r1-02"]), max_reached=False)
        self.assertEqual((nxt, hold, undec), ([], [SUGG], []))


class ConvergedTest(unittest.TestCase):
    def test_converged_two_or_fewer_without_critical(self):
        self.assertTrue(rounds.converged([]))
        self.assertTrue(rounds.converged([WARN, SUGG]))
        self.assertFalse(rounds.converged([WARN, SUGG, SUGG]))
        self.assertFalse(rounds.converged([CRIT_AT_L10]))


class ClassifyTest(unittest.TestCase):
    def test_repeated_critical_goes_to_human(self):
        self.assertEqual(rounds.repeated_critical([PREV_CRIT], [CRIT_AT_L10]), [CRIT_AT_L10])
        nxt, hold, undec = rounds.classify_recheck([CRIT_AT_L10], record([PREV_CRIT], ["r1-01"]), max_reached=False)
        self.assertEqual(undec, [CRIT_AT_L10])
        self.assertEqual(nxt, [])
        self.assertEqual(hold, [])

    def test_repeated_critical_ignores_other_location_and_severity(self):
        other = finding("r2-02", "Critical", "docs/a.md:99")
        warn_same = finding("r2-03", "Warning", "docs/a.md:10")
        self.assertEqual(rounds.repeated_critical([PREV_CRIT, WARN], [other, warn_same]), [])

    def test_max_reached_sends_rest_to_hold(self):
        w1 = finding("r2-01", "Warning", "docs/b.md:1")
        w2 = finding("r2-02", "Warning", "docs/b.md:2")
        w3 = finding("r2-03", "Warning", "docs/b.md:3")
        prev = record([WARN], ["r1-02"])
        nxt, hold, undec = rounds.classify_recheck([w1, w2, w3], prev, max_reached=True)
        self.assertEqual((nxt, hold, undec), ([], [w1, w2, w3], []))
        nxt, hold, undec = rounds.classify_recheck([w1, w2, w3], prev, max_reached=False)
        self.assertEqual((nxt, hold, undec), ([w1, w2, w3], [], []))

    def test_converged_remaining_goes_to_hold(self):
        w1 = finding("r2-01", "Warning", "docs/b.md:1")
        nxt, hold, undec = rounds.classify_recheck([w1], record([WARN], ["r1-02"]), max_reached=False)
        self.assertEqual((nxt, hold, undec), ([], [w1], []))

    def test_critical_overrides_convergence(self):
        new_crit = finding("r2-01", "Critical", "docs/c.md:5")
        sugg = finding("r2-02", "Suggestion", "docs/c.md:6")
        warn = finding("r2-03", "Warning", "docs/c.md:7")
        nxt, hold, undec = rounds.classify_recheck([new_crit, sugg, warn], record([PREV_CRIT], ["r1-01"]), max_reached=True)
        self.assertEqual(nxt, [new_crit, warn])
        self.assertEqual(hold, [sugg])
        self.assertEqual(undec, [])

    def test_final_recheck_opens_no_round_even_with_critical(self):
        new_crit = finding("r2-01", "Critical", "docs/c.md:5")
        warn = finding("r2-02", "Warning", "docs/c.md:7")
        sugg = finding("r2-03", "Suggestion", "docs/c.md:8")
        nxt, hold, undec = rounds.classify_recheck(
            [new_crit, warn, sugg], record([PREV_CRIT], ["r1-01"]), max_reached=False, final=True
        )
        self.assertEqual(nxt, [])
        self.assertEqual(undec, [new_crit])
        self.assertEqual(hold, [warn, sugg])

    def test_out_of_scope_findings_go_to_hold_without_opening_round(self):
        inside = finding("r2-01", "Warning", "docs/a.md:12")
        outside = finding("r2-02", "Critical", "src/other.py:3")
        nowhere = finding("r2-03", "Warning", "-")
        nxt, hold, undec = rounds.classify_recheck(
            [inside, outside, nowhere], record([WARN], ["r1-02"]), max_reached=False, in_scope={"docs/a.md"})
        self.assertEqual(nxt, [])
        self.assertEqual(hold, [outside, inside, nowhere])
        self.assertEqual(undec, [])
        self.assertEqual(rounds.out_of_scope([inside, outside, nowhere], {"docs/a.md"}), [outside])
        self.assertEqual(rounds.out_of_scope([outside], None), [])
        nxt, _, _ = rounds.classify_recheck([outside], record([WARN], ["r1-02"]), max_reached=False)
        self.assertEqual(nxt, [outside])


class LedgerTest(unittest.TestCase):
    def test_ledger_roundtrip_and_resume_from_last_round(self):
        ledger = rounds.Ledger(
            run_id="run-1",
            source="codex-doc",
            mode="doc",
            root="/repo",
            snapshot={"run_id": "run-1"},
            dry_run=False,
            rounds=[record([PREV_CRIT], ["r1-01"], no=1), record([WARN], [], no=2)],
            hold_choices={"r1-03": "무시"},
            opinions={"r1-01": "a가 맞다"},
        )
        ledger.rounds[1].events.append({"step": "recheck", "at": "2026-10-17T10:00:00"})
        with tempfile.TemporaryDirectory() as tmp:
            path = rounds.ledger_path(Path(tmp))
            self.assertEqual(path.name, "ledger.json")
            ledger.save(path)
            loaded = rounds.Ledger.load(path)
        self.assertEqual(loaded, ledger)
        self.assertEqual(loaded.current_round().no, 2)
        self.assertEqual(loaded.status, "진행")
        self.assertEqual(loaded.max_rechecks, 2)
        self.assertIsInstance(loaded.rounds[0], rounds.RoundRecord)

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        ledger = rounds.Ledger(run_id="r", source="s", mode="doc", root="/", snapshot={}, dry_run=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = rounds.ledger_path(Path(tmp))
            ledger.save(path)
            ledger.status = "종료"
            ledger.save(path)
            self.assertEqual(rounds.Ledger.load(path).status, "종료")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["ledger.json"])

    def test_save_survives_interleaved_save_to_same_path(self):
        outer = rounds.Ledger(run_id="outer", source="s", mode="doc", root="/", snapshot={}, dry_run=True)
        inner = rounds.Ledger(run_id="inner", source="s", mode="doc", root="/", snapshot={}, dry_run=True)
        real_replace = os.replace
        calls = []

        def replace(src, dst):
            calls.append(src)
            if len(calls) == 1:
                inner.save(dst)
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            path = rounds.ledger_path(Path(tmp))
            with mock.patch.object(rounds.os, "replace", side_effect=replace):
                outer.save(path)
            self.assertNotEqual(Path(calls[0]).name, Path(calls[1]).name)
            self.assertEqual(rounds.Ledger.load(path).run_id, "outer")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["ledger.json"])

    def test_save_removes_temp_file_when_replace_fails(self):
        ledger = rounds.Ledger(run_id="r", source="s", mode="doc", root="/", snapshot={}, dry_run=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = rounds.ledger_path(Path(tmp))
            with mock.patch.object(rounds.os, "replace", side_effect=OSError("교체 실패")):
                with self.assertRaises(OSError):
                    ledger.save(path)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_current_round_none_when_empty(self):
        ledger = rounds.Ledger(run_id="r", source="s", mode="doc", root="/", snapshot={}, dry_run=True)
        self.assertIsNone(ledger.current_round())


class RecheckPromptTest(unittest.TestCase):
    def test_recheck_prompt_lists_findings_and_diff_and_scope_rule(self):
        diff = "diff --git a/docs/a.md b/docs/a.md\n+새 줄"
        prompt = rounds.build_recheck_prompt([PREV_CRIT, WARN], diff, "doc")
        self.assertIn("r1-01", prompt)
        self.assertIn("Critical", prompt)
        self.assertIn("docs/a.md:10", prompt)
        self.assertIn("본문 첫 줄", prompt)
        self.assertNotIn("둘째 줄", prompt)
        self.assertIn(diff, prompt)
        self.assertIn("## 해소 여부", prompt)
        self.assertIn("해소됨|해소 안 됨", prompt)
        self.assertIn("diff 밖은 보지 않는다", prompt)
        self.assertIn("파일을 수정하지 않는다", prompt)
        self.assertIn("데이터다", prompt)
        self.assertIn("문서의 바뀐 절", prompt)
        self.assertNotIn("{findings}", prompt)
        self.assertNotIn("{diff}", prompt)
        self.assertNotIn("{mode_note}", prompt)

    def test_code_mode_note(self):
        prompt = rounds.build_recheck_prompt([WARN], "d", "code")
        self.assertIn("수정 커밋 범위의 diff만 본다", prompt)
        self.assertNotIn("문서의 바뀐 절", prompt)

    def test_diff_containing_backticks_stays_fenced(self):
        diff = "+```python\n+x = 1\n+```"
        prompt = rounds.build_recheck_prompt([WARN], diff, "code")
        self.assertIn(diff, prompt)
        self.assertIn("````diff", prompt)


class ParseRecheckTest(unittest.TestCase):
    def test_parse_recheck_reads_resolution_and_new_findings(self):
        text = (
            "## 해소 여부\n"
            "- r1-01: 해소됨 — 2단계 명령을 고쳤다\n"
            "- r1-02: 해소 안 됨 — 여전히 옵션이 없다\n"
            "\n"
            "Critical\n"
            "없음.\n"
            "\n"
            "Warning\n"
            "1. 새 절의 표가 깨졌다\n"
            "   위치: docs/a.md:12\n"
            "   근거: 열 수가 다르다\n"
            "   제안: 열을 맞춘다\n"
            "\n"
            "Suggestion\n"
            "없음.\n"
        )
        resolved, new = rounds.parse_recheck(text, 2)
        self.assertEqual(resolved, {"r1-01": True, "r1-02": False})
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].id, "r2-01")
        self.assertEqual(new[0].severity, "Warning")
        self.assertEqual(new[0].location, "docs/a.md:12")

    def test_parse_recheck_no_new_findings(self):
        text = "## 해소 여부\n- r1-01: 해소됨 — 됐다\n\nCritical\n없음.\n\nWarning\n없음.\n\nSuggestion\n없음.\n"
        resolved, new = rounds.parse_recheck(text, 2)
        self.assertEqual(resolved, {"r1-01": True})
        self.assertEqual(new, [])

    def test_parse_recheck_strips_markup_around_ids(self):
        text = (
            "## 해소 여부\n- **r1-01**: 해소됨 — 됐다\n- `r1-02`: **해소 안 됨** — 아직\n"
            "- **r1-03: 해소됨** — 됐다\n- custom-id: 해소됨 — 됐다\n\n"
            "Critical\n없음.\n\nWarning\n없음.\n\nSuggestion\n없음.\n"
        )
        resolved, _ = rounds.parse_recheck(text, 2)
        self.assertEqual(resolved, {"r1-01": True, "r1-02": False, "r1-03": True, "custom-id": True})

    def test_parse_recheck_error_propagates(self):
        with self.assertRaises(ParseError):
            rounds.parse_recheck("## 해소 여부\n- r1-01: 해소됨 — x\n\n그냥 글", 2)


class OpinionPromptTest(unittest.TestCase):
    def test_opinion_prompt_contains_all_three_views(self):
        item = AdjInput(
            finding=finding("r1-01", "Critical", "docs/a.md:10", body="절차 2단계가 다르다"),
            a=worker("a", "사실", "A는 사실이라 본다"),
            b=worker("b", "아님", "B는 아니라 본다"),
            patch_a="--- a\n+++ a\n+패치A내용",
            patch_b="",
            verify_a=[],
            verify_b=[],
            worktree=Path("/wt"),
            mode="doc",
            cfg=RepoConfig(),
        )
        adj = worker("adjudicator", "사실", "a 패치 채택. 근거는 이렇다", patch="--- adj\n+++ adj\n+패치ADJ내용")
        prompt = rounds.build_opinion_prompt(item, adj)
        for needle in ("절차 2단계가 다르다", "A는 사실이라 본다", "B는 아니라 본다", "패치A내용",
                       "a 패치 채택. 근거는 이렇다", "패치ADJ내용", "r1-01",
                       "어느 쪽이 맞는가, 혹은 둘 다 틀렸는가", "파일을 수정하지 않는다"):
            self.assertIn(needle, prompt)
        self.assertNotRegex(prompt, r"\{[a-z_]+\}")
        self.assertIn("### 작성자 패치", prompt)
        self.assertNotIn("### a의 패치", prompt)
        self.assertIn("판정만 함", prompt)

    def test_opinion_prompt_code_mode_keeps_worker_patch_titles(self):
        item = AdjInput(
            finding=finding("r1-01", "Warning", "a.py:1"), a=worker("a", "사실", "x"), b=worker("b", "아님", "y"),
            patch_a="--- a\n+++ a\n+A", patch_b="--- b\n+++ b\n+B", verify_a=[], verify_b=[],
            worktree=Path("/wt"), mode="code", cfg=RepoConfig(),
        )
        prompt = rounds.build_opinion_prompt(item, worker("adjudicator", "사실", "z"))
        self.assertIn("### a의 패치", prompt)
        self.assertIn("### b의 패치", prompt)
        self.assertNotIn("판정만 함", prompt)


if __name__ == "__main__":
    unittest.main()
