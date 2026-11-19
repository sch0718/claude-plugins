import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tests"))

import regress  # noqa: E402
import score  # noqa: E402
from test_eval import decision, make_cf_run, sample_line, sample_of, write_json  # noqa: E402

LOCAL = {"name": "local"}
OK = {"applied": ["r1-01"], "final_ok": True, "interference": False, "baseline_failed": False}
INTERFERENCE = {"applied": ["r1-01"], "final_ok": False, "interference": True, "baseline_failed": False}
BASELINE_FAILED = {"applied": ["r1-01"], "final_ok": False, "interference": False, "baseline_failed": True}


def round_of(no, apply):
    return {"no": no, "decisions": [decision("합의-사실", "a")], "apply": apply}


class RegressTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs = Path(self._tmp.name)

    def make(self, name, *rounds, profile=LOCAL, dry_run=False):
        make_cf_run(self.runs, name, list(rounds), profile=profile)
        if dry_run:
            ledger = json.loads((self.runs / name / "ledger.json").read_text(encoding="utf-8"))
            write_json(self.runs / name / "ledger.json", {**ledger, "dry_run": True})
        return self.runs / name

    def as_score_runs(self, *folders):
        sample = sample_of(*(sample_line(f.name, run=f.name) for f in folders))
        return self.runs, sample


class ComputeTest(RegressTestBase):
    def test_counts_interference_and_reverts_not_baseline_failures(self):
        run_ok = self.make("ok", round_of(1, OK))
        run_interference = self.make("interference", round_of(1, INTERFERENCE))
        run_baseline_failed = self.make("baseline", round_of(1, BASELINE_FAILED))
        run_dry = self.make("dry", round_of(1, INTERFERENCE), dry_run=True)
        revert_row = {"run": "ok", "finding": "r1-01", "reason": "버그"}
        r = regress.compute([run_ok, run_interference, run_baseline_failed, run_dry], reverts=[revert_row])
        self.assertEqual((r["local"]["regressions"], r["local"]["dry_run_excluded"]), (2, 1))
        self.assertEqual((r["local"]["runs"], r["local"]["baseline_failed"], r["local"]["reverts"]), (3, 1, 1))

    def test_matches_score_regression_count(self):
        run_interference = self.make("interference", round_of(1, INTERFERENCE))
        run_baseline_failed = self.make("baseline", round_of(1, BASELINE_FAILED))
        folders = [run_interference, run_baseline_failed]
        self.assertEqual(regress.compute(folders)["local"]["interference"],
                         score.compute(*self.as_score_runs(*folders))["regressions"])

    def test_applied_patches_survive_final_failure(self):
        failed = {"applied": ["r1-01", "r1-02"], "final_ok": False, "interference": True, "baseline_failed": False}
        run = self.make("failed", {"no": 1, "decisions": [], "apply": failed})
        self.assertEqual(regress.compute([run])["local"]["applied"], 2)

    def test_multi_round_regression_counts_once_per_run(self):
        run = self.make("two", round_of(1, INTERFERENCE), round_of(2, INTERFERENCE))
        r = regress.compute([run])
        self.assertEqual((r["local"]["interference"], r["local"]["regression_rounds"]), (1, 2))
        self.assertEqual(r["local"]["applied"], 2)
        self.assertEqual(score.compute(*self.as_score_runs(run))["regressions"], 1)

    def test_interference_and_revert_on_same_run_count_once(self):
        run = self.make("interference", round_of(1, INTERFERENCE))
        r = regress.compute([run], reverts=[{"run": "interference", "finding": "r1-01", "reason": "버그"}])
        self.assertEqual(r["local"]["regressions"], 1)

    def test_force_rerun_keeps_regression_through_final_history(self):
        apply = {"applied": ["r1-01"], "final_ok": None, "interference": False, "baseline_failed": False,
                 "final_history": [{"final_ok": False, "baseline_failed": False, "interference": True},
                                   {"final_ok": None, "baseline_failed": False, "interference": False}]}
        r = regress.compute([self.make("forced", round_of(1, apply))])
        self.assertEqual((r["local"]["interference"], r["local"]["regression_rounds"]), (1, 1))

    def test_profile_comes_from_ledger_and_legacy_when_absent(self):
        a = self.make("a", round_of(1, OK), profile={"name": "mix"})
        b = self.make("b", round_of(1, INTERFERENCE), profile=None)
        ledger = json.loads((b / "ledger.json").read_text(encoding="utf-8"))
        del ledger["profile"]
        write_json(b / "ledger.json", ledger)
        r = regress.compute([a, b])
        self.assertEqual((r["mix"]["runs"], r["legacy"]["runs"], r["legacy"]["regressions"]), (1, 1, 1))

    def test_run_id_is_folder_name_and_revert_matches_it(self):
        run = self.make("run-x", round_of(1, OK))
        r = regress.compute([run], reverts=[{"run": "run-x", "finding": "r1-01", "reason": "버그"}])
        self.assertEqual(r["local"]["regressions"], 1)

    def test_run_id_of_current_folder_is_its_resolved_name(self):
        run = self.make("run-dot", round_of(1, OK))
        cwd = Path.cwd()
        try:
            os.chdir(run)
            r = regress.compute(["."], reverts=[{"run": "run-dot", "finding": "r1-01", "reason": "버그"}])
        finally:
            os.chdir(cwd)
        self.assertEqual(r["local"]["regressions"], 1)
        self.assertEqual(r.warnings, [])

    def test_null_final_history_entry_is_an_error_item(self):
        run = self.make("odd", round_of(1, {**OK, "final_history": [None]}))
        r = regress.compute([run])
        [error] = r.errors
        self.assertIn("final_history", error)
        self.assertNotIn("local", r)

    def test_render_notes_say_reverts_count_baseline_failed_runs(self):
        text = regress.render(regress.compute([self.make("a", round_of(1, BASELINE_FAILED))]))
        self.assertIn("되돌림은 그런 실행도 센다", text)
        self.assertIn("위 표는 --runs의 실행을 모두 센다", text)

    def test_revert_for_unknown_run_goes_to_unknown_profile_bucket(self):
        run = self.make("ok", round_of(1, OK))
        r = regress.compute([run], reverts=[{"run": "elsewhere", "finding": "r1-01", "reason": "버그"}])
        self.assertEqual(r["local"]["regressions"], 0)
        self.assertEqual(r[regress.UNKNOWN_PROFILE]["regressions"], 1)
        self.assertTrue(any("elsewhere" in w for w in r.warnings))
        self.assertIn(regress.UNKNOWN_PROFILE, regress.render(r))

    def test_revert_for_dry_run_is_a_warning_only(self):
        run = self.make("dry", round_of(1, OK), dry_run=True)
        r = regress.compute([run], reverts=[{"run": "dry", "finding": "r1-01", "reason": "버그"}])
        self.assertEqual((r["local"]["regressions"], r["local"]["reverts"]), (0, 0))
        self.assertNotIn(regress.UNKNOWN_PROFILE, r)
        self.assertTrue(any("dry" in w and "dry-run 실행이라 세지 않는다" in w for w in r.warnings))

    def test_revert_for_unreadable_run_says_so_and_counts_as_unknown(self):
        bad = self.runs / "bad"
        bad.mkdir()
        r = regress.compute([bad], reverts=[{"run": "bad", "finding": "r1-01", "reason": "버그"}])
        self.assertEqual(r[regress.UNKNOWN_PROFILE]["regressions"], 1)
        [warning] = r.warnings
        self.assertIn("읽지 못한 실행", warning)
        self.assertNotIn("--runs에 없는", warning)

    def test_profile_named_with_underscore_is_listed_and_does_not_collide(self):
        run = self.make("x", round_of(1, INTERFERENCE), profile={"name": "_errors"})
        r = regress.compute([run, self.runs / "missing"])
        self.assertEqual(r["_errors"]["regressions"], 1)
        self.assertEqual(len(r.errors), 1)
        self.assertIn("| _errors |", regress.render(r))
        self.assertEqual(r.to_dict()["profiles"]["_errors"]["runs"], 1)

    def test_run_without_apply_json_counts_zero(self):
        run = self.make("plain", {"no": 1, "decisions": []})
        r = regress.compute([run])
        self.assertEqual((r["local"]["runs"], r["local"]["applied"], r["local"]["regressions"]), (1, 0, 0))

    def test_broken_ledger_is_an_error_item_and_others_continue(self):
        good = self.make("good", round_of(1, INTERFERENCE))
        bad = self.runs / "bad"
        bad.mkdir()
        (bad / "ledger.json").write_text("{깨짐", encoding="utf-8")
        missing = self.runs / "missing"
        r = regress.compute([bad, missing, good])
        self.assertEqual(r["local"]["regressions"], 1)
        self.assertEqual(len(r.errors), 2)
        self.assertTrue(all("ledger.json" in e for e in r.errors))

    def test_broken_apply_json_is_an_error_item_with_run_id(self):
        run = self.make("odd", round_of(1, OK))
        (run / "round-1" / "apply.json").write_text("{깨짐", encoding="utf-8")
        r = regress.compute([run])
        [error] = r.errors
        self.assertIn("odd", error)
        self.assertIn("round-1/apply.json", error)
        self.assertNotIn("local", r)

    def test_malformed_apply_applied_is_an_error_item(self):
        run = self.make("odd", round_of(1, {"applied": "r1-01", "final_ok": True}))
        r = regress.compute([run])
        self.assertEqual(len(r.errors), 1)
        self.assertNotIn("local", r)

    def test_duplicate_run_folder_name_is_an_error_item(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        make_cf_run(Path(other.name), "ok", [round_of(1, OK)], profile=LOCAL)
        first = self.make("ok", round_of(1, OK))
        r = regress.compute([first, Path(other.name) / "ok"])
        self.assertEqual(r["local"]["runs"], 1)
        self.assertEqual(len(r.errors), 1)

    def test_runs_accepts_preloaded_dicts(self):
        loaded = {"id": "pre", "ledger": {"profile": LOCAL}, "applies": [INTERFERENCE]}
        r = regress.compute([loaded])
        self.assertEqual((r["local"]["runs"], r["local"]["regressions"], r["local"]["applied"]), (1, 1, 1))


class StopConditionTest(RegressTestBase):
    def many(self, count, applied_each=1):
        apply = {**OK, "applied": [f"r1-{i:02d}" for i in range(applied_each)]}
        return [self.make(f"run-{i:02d}", round_of(1, apply)) for i in range(count)]

    def test_stop_condition_progress(self):
        r = regress.compute(self.many(3))
        text = regress.render(r)
        self.assertIn("채택 패치 50건 중", text)
        self.assertIn("실행 10건 중", text)
        self.assertEqual(r["local"]["stop"]["reached"], None)

    def test_runs_limit_reached_first(self):
        r = regress.compute(self.many(10))
        self.assertEqual(r["local"]["stop"]["reached"], "runs")
        self.assertIn("실행 10건에 먼저 닿음", regress.render(r))

    def test_patches_limit_reached_first(self):
        r = regress.compute(self.many(5, applied_each=10))
        self.assertEqual(r["local"]["stop"]["reached"], "patches")
        self.assertIn("채택 패치 50건에 먼저 닿음", regress.render(r))

    def test_custom_limits(self):
        r = regress.compute(self.many(2), stop_runs=2, stop_patches=5)
        self.assertEqual(r["local"]["stop"]["reached"], "runs")
        self.assertIn("채택 패치 5건 중", regress.render(r))

    def test_dry_runs_do_not_advance_progress(self):
        runs = [self.make(f"d{i}", round_of(1, OK), dry_run=True) for i in range(12)]
        r = regress.compute(runs)
        self.assertEqual(r["local"]["runs"], 0)
        self.assertEqual(r["local"]["stop"]["reached"], None)


class RenderAndMainTest(RegressTestBase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = regress.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_render_is_markdown_table_per_profile(self):
        r = regress.compute([self.make("a", round_of(1, INTERFERENCE)), self.make("b", round_of(1, OK), profile={"name": "mix"})])
        text = regress.render(r)
        self.assertIn("| 프로필 |", text)
        self.assertIn("| local |", text)
        self.assertIn("| mix |", text)

    def test_main_prints_table_and_writes_json(self):
        run = self.make("a", round_of(1, INTERFERENCE))
        reverts = self.runs / "reverts.jsonl"
        reverts.write_text(json.dumps({"run": "a", "finding": "r1-01", "reason": "버그"}, ensure_ascii=False) + "\n", encoding="utf-8")
        out_json = self.runs / "out.json"
        code, out, _ = self.run_main("--runs", str(run), "--reverts", str(reverts), "--json", str(out_json))
        self.assertEqual(code, 0)
        self.assertIn("| local |", out)
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(data["profiles"]["local"]["regressions"], 1)
        self.assertEqual((data["errors"], data["warnings"], data["stop"]), ([], [], {"runs": 10, "patches": 50}))

    def test_main_reports_broken_run_in_korean_and_exits_one(self):
        good = self.make("good", round_of(1, OK))
        bad = self.runs / "bad"
        bad.mkdir()
        code, out, err = self.run_main("--runs", str(bad), str(good))
        self.assertEqual(code, 1)
        self.assertIn("| local |", out)
        self.assertIn("ledger.json", err)
        self.assertNotIn("Traceback", err)

    def test_main_warns_about_unknown_revert_run(self):
        good = self.make("good", round_of(1, OK))
        reverts = self.runs / "reverts.jsonl"
        reverts.write_text(json.dumps({"run": "nowhere", "finding": "r1-01", "reason": "버그"}) + "\n", encoding="utf-8")
        code, out, err = self.run_main("--runs", str(good), "--reverts", str(reverts))
        self.assertEqual(code, 0)
        self.assertIn("nowhere", err)
        self.assertIn(regress.UNKNOWN_PROFILE, out)

    def test_main_bad_revert_line_is_a_korean_error(self):
        good = self.make("good", round_of(1, OK))
        reverts = self.runs / "reverts.jsonl"
        reverts.write_text('{"run": "good"}\n깨짐\n[1]\n', encoding="utf-8")
        code, out, err = self.run_main("--runs", str(good), "--reverts", str(reverts))
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn(":2", err)
        self.assertIn(":3", err)

    def test_main_unreadable_reverts_file_exits_one(self):
        good = self.make("good", round_of(1, OK))
        code, _, err = self.run_main("--runs", str(good), "--reverts", str(self.runs / "none.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("none.jsonl", err)
        self.assertNotIn("Traceback", err)

    def test_main_passes_stop_options(self):
        runs = [self.make(f"r{i}", round_of(1, OK)) for i in range(2)]
        code, out, _ = self.run_main("--runs", *map(str, runs), "--stop-runs", "2", "--stop-patches", "7")
        self.assertEqual(code, 0)
        self.assertIn("실행 2건에 먼저 닿음", out)
        self.assertIn("채택 패치 7건 중", out)


if __name__ == "__main__":
    unittest.main()
