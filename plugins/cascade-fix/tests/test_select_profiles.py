import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = ROOT
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import select_profiles  # noqa: E402
import score  # noqa: E402

PRED = {"a": "사실", "b": "아님"}


def run(accuracy=0.9, false_fact=0, completion=1.0, fallback_ratio=0.0, regressions=0, claude_cost=1.0, to_human=2, predicted=None,
        acc_ok=True, regression_ok=True, **extra):
    data = {"accuracy": accuracy, "false_fact": false_fact, "completion": completion, "fallback_ratio": fallback_ratio,
            "regressions": regressions, "claude_cost": claude_cost, "to_human": to_human, "predicted": predicted or dict(PRED),
            "selection": {"checks": {"accuracy": {"ok": acc_ok}, "regression": {"ok": regression_ok}}}}
    data.update(extra)
    return data


def candidates(**overrides):
    cands = {
        "claude-only": [run(claude_cost=10.0, fallback_ratio=None), run(claude_cost=10.0, fallback_ratio=None)],
        "mix-codex-haiku": [run(claude_cost=5.0, to_human=3), run(claude_cost=6.0, to_human=3)],
        "external": [run(claude_cost=4.0, accuracy=0.6, acc_ok=False), run(claude_cost=4.0, accuracy=0.6, acc_ok=False)],
    }
    cands.update(overrides)
    return cands


class DecideTest(unittest.TestCase):
    def test_picks_cheapest_passing(self):
        result = select_profiles.decide(candidates(), "claude-only")
        self.assertIn("선정: mix-codex-haiku", select_profiles.render(result))
        self.assertEqual(result["selected"], "mix-codex-haiku")
        self.assertNotIn("external", result["eligible"])

    def test_tie_within_10pct_prefers_fewer_to_human(self):
        cands = candidates(**{"a-cheap": [run(claude_cost=5.0, to_human=9)], "b-near": [run(claude_cost=5.4, to_human=1)],
                              "c-far": [run(claude_cost=5.6, to_human=0)]})
        result = select_profiles.decide(cands, "claude-only")
        self.assertEqual(result["selected"], "b-near")

    def test_tie_prefers_lower_cost_then_name(self):
        cands = {"claude-only": [run(claude_cost=9.0)], "b": [run(claude_cost=5.0, to_human=1)], "a": [run(claude_cost=5.0, to_human=1)],
                 "c": [run(claude_cost=5.2, to_human=1)]}
        self.assertEqual(select_profiles.decide(cands, "claude-only")["selected"], "a")

    def test_no_external_jobs_is_not_applicable(self):
        result = select_profiles.decide(candidates(), "claude-only")
        self.assertIn("claude-only", result["eligible"])
        self.assertIn("해당 없음", select_profiles.render(result))

    def test_unstable_flag_when_one_run_fails_accuracy(self):
        cands = candidates(**{"mix-codex-haiku": [run(claude_cost=5.0, accuracy=0.95), run(claude_cost=5.0, accuracy=0.87, acc_ok=False),
                                                  run(claude_cost=5.0, accuracy=0.95)]})
        result = select_profiles.decide(cands, "claude-only")
        self.assertTrue(result["candidates"]["mix-codex-haiku"]["unstable"])
        self.assertFalse(result["candidates"]["claude-only"]["unstable"])
        self.assertIn("불안정", select_profiles.render(result))
        self.assertIn("mix-codex-haiku", result["eligible"])

    def test_mean_below_limit_filters_out(self):
        cands = candidates(**{"mix-codex-haiku": [run(accuracy=0.8, acc_ok=False), run(accuracy=0.85, acc_ok=False)]})
        self.assertNotIn("mix-codex-haiku", select_profiles.decide(cands, "claude-only")["eligible"])

    def test_false_fact_mean_above_baseline_filters_out(self):
        cands = candidates(**{"mix-codex-haiku": [run(false_fact=1), run(false_fact=1)]})
        self.assertNotIn("mix-codex-haiku", select_profiles.decide(cands, "claude-only")["eligible"])

    def test_unfinished_mean_above_limit_filters_out(self):
        cands = candidates(**{"mix-codex-haiku": [run(completion=0.9), run(completion=0.9)]})
        self.assertNotIn("mix-codex-haiku", select_profiles.decide(cands, "claude-only")["eligible"])

    def test_fallback_mean_above_limit_filters_out(self):
        cands = candidates(**{"mix-codex-haiku": [run(fallback_ratio=0.2), run(fallback_ratio=0.1)]})
        self.assertNotIn("mix-codex-haiku", select_profiles.decide(cands, "claude-only")["eligible"])

    def test_one_regression_run_filters_out(self):
        cands = candidates(**{"mix-codex-haiku": [run(), run(regression_ok=False, regressions=1)]})
        self.assertNotIn("mix-codex-haiku", select_profiles.decide(cands, "claude-only")["eligible"])

    def test_regressions_count_filters_out_when_measured(self):
        cands = candidates(**{"mix-codex-haiku": [run(regressions=1, regression_measured=True, regression_ok=None)]})
        self.assertNotIn("mix-codex-haiku", select_profiles.decide(cands, "claude-only")["eligible"])

    def test_regression_never_measured_is_not_a_failure(self):
        cands = candidates(**{"mix-codex-haiku": [run(regression_ok=None, regression_measured=False)]})
        result = select_profiles.decide(cands, "claude-only")
        self.assertIn("mix-codex-haiku", result["eligible"])
        self.assertIn("미측정", select_profiles.render(result))

    def test_unmeasured_cost_never_wins(self):
        cands = candidates(**{"mix-codex-haiku": [run(claude_cost=0.5), run(claude_cost=None, cost_missing=["a"])]})
        result = select_profiles.decide(cands, "claude-only")
        self.assertIn("비용 미측정으로 제외", select_profiles.render(result))
        self.assertEqual(result["selected"], "claude-only")

    def test_no_selection_when_nothing_has_cost(self):
        cands = {"claude-only": [run(claude_cost=None)]}
        result = select_profiles.decide(cands, "claude-only")
        self.assertIsNone(result["selected"])
        self.assertIn("선정: 없음", select_profiles.render(result))

    def test_unmeasured_to_human_never_wins_tie_break(self):
        cands = {"claude-only": [run(claude_cost=9.0)], "a-none": [run(claude_cost=5.0, to_human=None)],
                 "b-some": [run(claude_cost=5.4, to_human=7)]}
        result = select_profiles.decide(cands, "claude-only")
        self.assertEqual(result["selected"], "b-some")
        only = select_profiles.decide({"claude-only": [run(claude_cost=9.0, to_human=None)]}, "claude-only")
        self.assertIn("사람 목록 미측정", select_profiles.render(only))

    def test_cost_tie_uses_rounding_tolerance(self):
        cands = {"claude-only": [run(claude_cost=9.0)], "a": [run(claude_cost=0.3, to_human=5)],
                 "b": [run(claude_cost=0.1 * 3 * 1.1 + 1e-12, to_human=1)]}
        self.assertEqual(select_profiles.decide(cands, "claude-only")["selected"], "b")

    def test_opus_calls_in_table_and_missing_is_unmeasured(self):
        cands = candidates(**{"mix-codex-haiku": [run(claude_cost=5.0, opus_calls=4), run(claude_cost=5.0, opus_calls=8)]})
        result = select_profiles.decide(cands, "claude-only")
        self.assertEqual(result["candidates"]["mix-codex-haiku"]["metrics"]["opus_calls"], {"mean": 6.0, "min": 4, "max": 8})
        text = select_profiles.render(result)
        self.assertIn("6 [4~8]", text)
        self.assertIn("| claude-only | ", text)
        self.assertIsNone(result["candidates"]["claude-only"]["metrics"]["opus_calls"])

    def test_runs_without_selection_are_tolerated(self):
        bare = run()
        bare["selection"] = None
        result = select_profiles.decide({"claude-only": [bare]}, "claude-only")
        self.assertEqual(result["selected"], "claude-only")

    def test_runs_without_selection_have_unmeasured_stability_not_stable(self):
        bare = run()
        bare["selection"] = None
        result = select_profiles.decide({"claude-only": [bare, run()], "other": [run(), run()]}, "claude-only")
        summary = result["candidates"]["claude-only"]
        self.assertIsNone(summary["unstable"])
        self.assertIn("정확도 안정성 미측정", summary["reasons"])
        text = select_profiles.render(result)
        self.assertIn("| claude-only (안정성 미측정) |", text)
        self.assertIn("정확도 안정성 미측정", text)
        self.assertNotIn("| other (", text)
        self.assertIs(result["candidates"]["other"]["unstable"], False)
        self.assertEqual(json.loads(json.dumps(result))["candidates"]["claude-only"]["unstable"], None)

    def test_one_failing_run_is_unstable_even_if_another_run_has_no_selection(self):
        bare = run()
        bare["selection"] = None
        result = select_profiles.decide({"claude-only": [bare, run(acc_ok=False, accuracy=0.5)]}, "claude-only")
        self.assertIs(result["candidates"]["claude-only"]["unstable"], True)

    def test_cost_mean_and_range_in_table(self):
        text = select_profiles.render(select_profiles.decide(candidates(), "claude-only"))
        self.assertIn(f"{score.money(5.5)} [{score.money(5.0)}~{score.money(6.0)}]", text)


class FlipRateTest(unittest.TestCase):
    def test_flip_rate(self):
        self.assertAlmostEqual(select_profiles.flip_rate([{"a": "사실", "b": "아님"}, {"a": "사실", "b": "사실"}]), 0.5)

    def test_flip_rate_single_run_is_zero(self):
        self.assertEqual(select_profiles.flip_rate([{"a": "사실"}]), 0.0)
        self.assertEqual(select_profiles.flip_rate([]), 0.0)

    def test_flip_rate_counts_missing_items_as_different(self):
        self.assertAlmostEqual(select_profiles.flip_rate([{"a": "사실"}, {"a": "사실", "b": "아님"}]), 0.5)

    def test_flip_rate_needs_all_runs_identical(self):
        runs = [{"a": "사실"}, {"a": "사실"}, {"a": "아님"}]
        self.assertEqual(select_profiles.flip_rate(runs), 1.0)


class CliTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = select_profiles.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_main_writes_markdown_and_json(self):
        base = [self.write("b1.json", run(claude_cost=10.0, fallback_ratio=None)), self.write("b2.json", run(claude_cost=10.0, fallback_ratio=None))]
        mix = [self.write("m1.json", run(claude_cost=5.0)), self.write("m2.json", run(claude_cost=5.0))]
        out_json = self.tmp / "out.json"
        code, out, _ = self.call(["--candidate", f"claude-only={','.join(base)}", "--candidate", f"mix={','.join(mix)}",
                                  "--baseline", "claude-only", "--json", str(out_json)])
        self.assertEqual(code, 0)
        self.assertIn("선정: mix", out)
        self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["selected"], "mix")

    def test_baseline_must_be_a_candidate(self):
        path = self.write("a.json", run())
        code, _, err = self.call(["--candidate", f"a={path}", "--baseline", "zzz"])
        self.assertEqual(code, 2)
        self.assertIn("zzz", err)

    def test_missing_json_is_a_korean_error(self):
        code, _, err = self.call(["--candidate", f"a={self.tmp / 'none.json'}", "--baseline", "a"])
        self.assertEqual(code, 1)
        self.assertIn("none.json", err)
        self.assertNotIn("Traceback", err)

    def test_bad_json_is_a_korean_error(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, _, err = self.call(["--candidate", f"a={bad}", "--baseline", "a"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_wrong_value_type_is_a_korean_error(self):
        path = self.write("wrong.json", run(predicted=["a", "b"]))
        code, _, err = self.call(["--candidate", f"a={path}", "--baseline", "a"])
        self.assertEqual(code, 1)
        self.assertIn("predicted", err)
        self.assertNotIn("Traceback", err)
        path = self.write("wrong2.json", run(accuracy="0.9"))
        code, _, err = self.call(["--candidate", f"a={path}", "--baseline", "a"])
        self.assertEqual(code, 1)
        self.assertIn("accuracy", err)

    def test_unwritable_json_output_is_a_korean_error(self):
        path = self.write("a.json", run())
        code, _, err = self.call(["--candidate", f"a={path}", "--baseline", "a", "--json", str(self.tmp / "no-dir" / "out.json")])
        self.assertEqual(code, 1)
        self.assertIn("out.json", err)
        self.assertNotIn("Traceback", err)

    def test_malformed_candidate_argument(self):
        code, _, err = self.call(["--candidate", "no-equals", "--baseline", "no-equals"])
        self.assertEqual(code, 2)


class ScriptsTest(unittest.TestCase):
    def test_select_profiles_does_not_shadow_stdlib(self):
        for script in ("replay.py", "score.py", "select_profiles.py", "adopt_recalc.py"):
            proc = subprocess.run([sys.executable, str(SKILL_ROOT / "eval" / script), "--help"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, script)


if __name__ == "__main__":
    unittest.main()
