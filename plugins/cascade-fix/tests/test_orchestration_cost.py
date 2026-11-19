import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import orchestration_cost as oc  # noqa: E402
from cascade import usage  # noqa: E402

CF = 'PYTHONPATH="$S" python3 -m cascade.cli'


def assistant(ts, mid, output, tool=None, text=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": tool[0], "name": "Bash", "input": {"command": tool[1]}})
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5", "content": content,
                        "usage": {"input_tokens": 1, "output_tokens": output, "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}}


def result(ts, tool_id, text):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def read(ts, mid, output, tool):
    row = assistant(ts, mid, output, tool=tool)
    row["message"]["content"][0].update(name="Read", input={"file_path": tool[1]})
    return row


def human(ts, text):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def init_out(run_dir, n, profile="local"):
    return f"run_dir={run_dir}\nstatus=진행\n지적 {n}건: Warning {n}\n프로필: {profile} (기본)\n"


class OrchestrationCostTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def session(self, rows, name="s1"):
        path = self.tmp / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        return path

    def two_runs(self):
        return self.session([
            human("2026-10-25T00:00:00Z", "리뷰하고 고쳐줘"),
            assistant("2026-10-25T00:00:01Z", "a0", 1000, tool=("t0", f"{CF} detect")),
            result("2026-10-25T00:00:02Z", "t0", "프로필: local"),
            assistant("2026-10-25T00:01:00Z", "a1", 10, tool=("t1", f"{CF} init --source codex-code --input r.json")),
            result("2026-10-25T00:01:05Z", "t1", init_out("/r/.cascade-fix/cf-1", 2)),
            assistant("2026-10-25T00:02:00Z", "a2", 20, tool=("t2", f"{CF} judge --run-dir /r/.cascade-fix/cf-1 --round 1")),
            result("2026-10-25T00:02:05Z", "t2", "다음: apply"),
            human("2026-10-25T00:02:30Z", "<task-notification>워크플로 끝</task-notification>"),
            assistant("2026-10-25T00:03:00Z", "a3", 30, tool=("t3", f"{CF} report --run-dir /r/.cascade-fix/cf-1")),
            result("2026-10-25T00:03:05Z", "t3", "보고서"),
            assistant("2026-10-25T00:03:10Z", "a4", 40, text="정리했습니다"),
            human("2026-10-25T00:10:00Z", "다음 것도"),
            assistant("2026-10-25T00:10:01Z", "a5", 2000, text="네"),
            assistant("2026-10-25T00:11:00Z", "a6", 100, tool=("t6", f"{CF} init --source pr-comment --dry-run")),
            result("2026-10-25T00:11:05Z", "t6", init_out("/r/.cascade-fix/cf-2", 1, "local-sonnet-b")),
            assistant("2026-10-25T00:12:00Z", "a7", 200, tool=("t7", f"{CF} report --run-dir /r/.cascade-fix/cf-2")),
            result("2026-10-25T00:12:05Z", "t7", "보고서"),
            assistant("2026-10-25T00:12:10Z", "a8", 300, text="끝"),
        ])

    def test_finds_start_go_run_from_background_outputs(self):
        path = self.session([
            human("2026-10-28T00:00:00Z", "리뷰하고 고쳐줘"),
            assistant("2026-10-28T00:01:00Z", "a1", 10, tool=("t1", f"{CF} start --source codex-code --input r.json --dry-run")),
            result("2026-10-28T00:01:01Z", "t1", "Command running in background"),
            assistant("2026-10-28T00:02:00Z", "a2", 10, tool=("t2", "cat /tmp/tasks/b1.output")),
            result("2026-10-28T00:02:01Z", "t2", init_out("/r/.cascade-fix/cf-3", 3) + "멈춤: Workflow 결과가 필요하다\n"),
            assistant("2026-10-28T00:03:00Z", "a3", 10, tool=("t3", f"{CF} go --run-dir /r/.cascade-fix/cf-3 --workflow-output /tmp/o")),
            result("2026-10-28T00:03:01Z", "t3", "Command running in background"),
            assistant("2026-10-28T00:04:00Z", "a4", 10, tool=("t4", "cat /tmp/tasks/b2.output")),
            result("2026-10-28T00:04:01Z", "t4", "멈춤: report까지 끝났다. 보고 뒤 보류 후보 선택을 받고 cleanup 한다"),
            assistant("2026-10-28T00:04:10Z", "a5", 10, text="정리했습니다"),
            human("2026-10-28T00:10:00Z", "다음 것도"),
            assistant("2026-10-28T00:10:01Z", "a6", 5000, text="네"),
        ])
        [run] = oc.find_runs(path)
        self.assertEqual((run.run_dir, run.findings, run.dry_run), ("/r/.cascade-fix/cf-3", 3, True))
        self.assertEqual((run.start, run.end), ("2026-10-28T00:01:00Z", "2026-10-28T00:04:10Z"))

    def test_finds_start_go_run_from_background_read_outputs(self):
        for content_type in ("string", "list"):
            for separator in ("→", "\t"):
                with self.subTest(content_type=content_type, separator=separator):
                    init_text = "\n".join(f"{i:6}{separator}{line}" for i, line in enumerate(
                        init_out("/r/.cascade-fix/cf-3", 3, "local-sonnet-b").splitlines(), 1))
                    go_text = f"     1{separator}멈춤: report까지 끝났다. 보고 뒤 보류 후보 선택을 받고 cleanup 한다"
                    if content_type == "list":
                        init_text = [{"type": "text", "text": init_text}]
                        go_text = [{"type": "text", "text": go_text}]
                    path = self.session([
                        human("2026-10-28T00:00:00Z", "리뷰하고 고쳐줘"),
                        assistant("2026-10-28T00:01:00Z", "a1", 10, tool=("t1", f"{CF} start --source codex-code --input r.json --dry-run")),
                        result("2026-10-28T00:01:01Z", "t1", "Command running in background"),
                        read("2026-10-28T00:02:00Z", "a2", 10, ("t2", "/tmp/tasks/b1.output")),
                        result("2026-10-28T00:02:01Z", "t2", init_text),
                        assistant("2026-10-28T00:03:00Z", "a3", 10, tool=("t3", f"{CF} go --run-dir /r/.cascade-fix/cf-3 --workflow-output /tmp/o")),
                        result("2026-10-28T00:03:01Z", "t3", "Command running in background"),
                        read("2026-10-28T00:04:00Z", "a4", 10, ("t4", "/tmp/tasks/b2.output")),
                        result("2026-10-28T00:04:01Z", "t4", go_text),
                        assistant("2026-10-28T00:04:10Z", "a5", 10, text="정리했습니다"),
                        human("2026-10-28T00:10:00Z", "다음 것도"),
                        assistant("2026-10-28T00:10:01Z", "a6", 5000, text="네"),
                    ])
                    runs = oc.find_runs(path)
                    self.assertEqual(len(runs), 1)
                    [run] = runs
                    self.assertEqual((run.run_dir, run.findings, run.profile, run.dry_run),
                                     ("/r/.cascade-fix/cf-3", 3, "local-sonnet-b", True))
                    self.assertEqual((run.start, run.end), ("2026-10-28T00:01:00Z", "2026-10-28T00:04:10Z"))

    def test_ignores_indented_source_run_dir_in_read_output(self):
        init_text = "\n".join(f"{i:6}→{line}" for i, line in enumerate(
            init_out("/r/.cascade-fix/cf-3", 3, "local-sonnet-b").splitlines(), 1))
        path = self.session([
            human("2026-10-28T00:00:00Z", "리뷰하고 고쳐줘"),
            assistant("2026-10-28T00:01:00Z", "a1", 10, tool=("t1", f"{CF} start --source codex-code --input r.json --dry-run")),
            result("2026-10-28T00:01:01Z", "t1", "Command running in background"),
            read("2026-10-28T00:02:00Z", "a2", 10, ("t2", "/r/cascade/state.py")),
            result("2026-10-28T00:02:01Z", "t2", '   123→            run_dir=Path(data["run_dir"]),'),
            read("2026-10-28T00:03:00Z", "a3", 10, ("t3", "/tmp/tasks/b1.output")),
            result("2026-10-28T00:03:01Z", "t3", init_text),
            assistant("2026-10-28T00:04:00Z", "a4", 10, tool=("t4", f"{CF} go --run-dir /r/.cascade-fix/cf-3 --workflow-output /tmp/o")),
            result("2026-10-28T00:04:01Z", "t4", "멈춤: report까지 끝났다. 보고 뒤 cleanup 한다"),
            human("2026-10-28T00:10:00Z", "다음 것도"),
        ])
        [run] = oc.find_runs(path)
        self.assertEqual((run.run_dir, run.findings, run.profile, run.dry_run),
                         ("/r/.cascade-fix/cf-3", 3, "local-sonnet-b", True))
        self.assertEqual((run.start, run.end), ("2026-10-28T00:01:00Z", "2026-10-28T00:04:01Z"))

    def test_start_output_with_run_dir_and_report_marker_together_is_one_finished_run(self):
        path = self.session([
            human("2026-10-28T00:00:00Z", "리뷰하고 고쳐줘"),
            assistant("2026-10-28T00:01:00Z", "a1", 10, tool=("t1", f"{CF} start --source codex-code --input r.json")),
            result("2026-10-28T00:01:05Z", "t1", init_out("/r/.cascade-fix/cf-5", 1) + "멈춤: report까지 끝났다. 보고 뒤 cleanup 한다"),
            assistant("2026-10-28T00:01:10Z", "a2", 10, text="정리했습니다"),
            human("2026-10-28T00:10:00Z", "다음 것도"),
        ])
        [run] = oc.find_runs(path)
        self.assertEqual((run.run_dir, run.end), ("/r/.cascade-fix/cf-5", "2026-10-28T00:01:10Z"))

    def test_restart_after_scale_question_replaces_the_abandoned_run(self):
        path = self.session([
            human("2026-10-28T00:00:00Z", "리뷰하고 고쳐줘"),
            assistant("2026-10-28T00:01:00Z", "a1", 10, tool=("t1", f"{CF} start --source codex-code --input r.json")),
            result("2026-10-28T00:01:01Z", "t1", "Command running in background"),
            assistant("2026-10-28T00:02:00Z", "a2", 10, tool=("t2", "cat /tmp/tasks/b1.output")),
            result("2026-10-28T00:02:01Z", "t2", init_out("/r/.cascade-fix/cf-A", 12) + "멈춤: 1단계 작업이 24개로 10개를 넘는다\n"),
            human("2026-10-28T00:03:00Z", "Critical·Warning만"),
            assistant("2026-10-28T00:04:00Z", "a3", 10, tool=("t3", f"{CF} cleanup --run-dir /r/.cascade-fix/cf-A")),
            result("2026-10-28T00:04:01Z", "t3", "정리했다"),
            assistant("2026-10-28T00:05:00Z", "a4", 10, tool=("t4", f"{CF} start --source codex-code --input r.json --severity Critical,Warning")),
            result("2026-10-28T00:05:01Z", "t4", "Command running in background"),
            assistant("2026-10-28T00:06:00Z", "a5", 10, tool=("t5", "cat /tmp/tasks/b2.output")),
            result("2026-10-28T00:06:01Z", "t5", init_out("/r/.cascade-fix/cf-B", 2)),
            assistant("2026-10-28T00:07:00Z", "a6", 10, tool=("t6", f"{CF} go --run-dir /r/.cascade-fix/cf-B")),
            result("2026-10-28T00:07:01Z", "t6", "Command running in background"),
            assistant("2026-10-28T00:08:00Z", "a7", 10, tool=("t7", "cat /tmp/tasks/b3.output")),
            result("2026-10-28T00:08:01Z", "t7", "멈춤: report까지 끝났다. 보고 뒤 cleanup 한다"),
            assistant("2026-10-28T00:08:10Z", "a8", 10, text="정리했습니다"),
            human("2026-10-28T00:20:00Z", "다음 것도"),
        ])
        [run] = oc.find_runs(path)
        self.assertEqual((run.run_dir, run.findings), ("/r/.cascade-fix/cf-B", 2))
        self.assertEqual((run.start, run.end), ("2026-10-28T00:01:00Z", "2026-10-28T00:08:10Z"))

    def test_start_run_without_report_marker_is_dropped(self):
        path = self.session([
            human("2026-10-28T00:00:00Z", "리뷰하고 고쳐줘"),
            assistant("2026-10-28T00:01:00Z", "a1", 10, tool=("t1", f"{CF} start --source codex-code --input r.json")),
            result("2026-10-28T00:01:05Z", "t1", init_out("/r/.cascade-fix/cf-4", 1)),
            human("2026-10-28T00:10:00Z", "그만"),
        ])
        self.assertEqual(oc.find_runs(path), [])

    def test_finds_runs_and_windows(self):
        runs = oc.find_runs(self.two_runs())
        self.assertEqual([(r.run_dir, r.findings, r.profile, r.dry_run) for r in runs],
                         [("/r/.cascade-fix/cf-1", 2, "local", False), ("/r/.cascade-fix/cf-2", 1, "local-sonnet-b", True)])
        self.assertEqual((runs[0].start, runs[0].end), ("2026-10-25T00:01:00Z", "2026-10-25T00:03:10Z"))
        self.assertEqual(runs[1].end, "2026-10-25T00:12:10Z")

    def test_cost_counts_only_window_messages(self):
        path = self.two_runs()
        rows = oc.measure(path)
        want1 = sum(usage.cost("claude-opus-5-5", 1, out, 0, 0) for out in (10, 20, 30, 40))
        want2 = sum(usage.cost("claude-opus-5-5", 1, out, 0, 0) for out in (100, 200, 300))
        self.assertAlmostEqual(rows[0]["cost"], want1)
        self.assertAlmostEqual(rows[1]["cost"], want2)
        self.assertEqual((rows[0]["output"], rows[0]["requests"]), (100, 4))

    def test_forked_sessions_count_a_run_once(self):
        rows = oc.measure(self.two_runs())
        self.assertEqual(len(oc.unique_runs(rows + [dict(r, session="s2") for r in rows])), 2)

    def test_rerun_init_before_report_keeps_start(self):
        path = self.session([
            assistant("2026-10-25T00:01:00Z", "a1", 10, tool=("t1", f"{CF} init --source codex-code")),
            result("2026-10-25T00:01:05Z", "t1", "오류: 검증 명령 없음"),
            assistant("2026-10-25T00:02:00Z", "a2", 10, tool=("t2", f"{CF} init --source codex-code")),
            result("2026-10-25T00:02:05Z", "t2", init_out("/r/cf-3", 4)),
            assistant("2026-10-25T00:03:00Z", "a3", 10, tool=("t3", f"{CF} report --run-dir /r/cf-3")),
            result("2026-10-25T00:03:05Z", "t3", "보고서"),
        ])
        runs = oc.find_runs(path)
        self.assertEqual([(r.run_dir, r.findings, r.start, r.end) for r in runs],
                         [("/r/cf-3", 4, "2026-10-25T00:01:00Z", "2026-10-25T00:03:05Z")])

    def test_run_without_report_is_skipped(self):
        path = self.session([
            assistant("2026-10-25T00:01:00Z", "a1", 10, tool=("t1", f"{CF} init --source codex-code")),
            result("2026-10-25T00:01:05Z", "t1", init_out("/r/cf-4", 1)),
        ])
        self.assertEqual(oc.find_runs(path), [])

    def test_fit_fixed_and_per_finding(self):
        fit = oc.fit([{"findings": 1, "cost": 3.0}, {"findings": 3, "cost": 7.0}, {"findings": 2, "cost": 5.0}])
        self.assertAlmostEqual(fit["fixed"], 1.0)
        self.assertAlmostEqual(fit["per_finding"], 2.0)
        self.assertAlmostEqual(fit["one_finding_run"], 3.0)
        self.assertAlmostEqual(fit["mean_findings"], 2.0)
        self.assertAlmostEqual(fit["per_finding_at_mean"], 2.5)

    def test_fit_same_size_runs_has_no_slope(self):
        fit = oc.fit([{"findings": 2, "cost": 4.0}, {"findings": 2, "cost": 6.0}])
        self.assertIsNone(fit["per_finding"])
        self.assertAlmostEqual(fit["per_finding_at_mean"], 2.5)

    def test_main_writes_json(self):
        out = self.tmp / "out.json"
        self.assertEqual(oc.main(["--session", str(self.two_runs()), "--json", str(out)]), 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data["runs"]), 2)
        self.assertIn("fit", data)

    def test_project_removes_eight_of_twenty_four_requests(self):
        row = {"requests": 24, "cost": 1.55, "findings": 4, "cache_write": 100_000,
               "input": 100, "output": 1000, "cache_read": 200_000,
               "by_model": {"claude-sonnet-5-5": 0.15, "claude-opus-5-5": 1.40}}
        estimate = oc.project(row, 8)
        self.assertAlmostEqual(estimate["lower"], 0.25833333333333336)
        self.assertAlmostEqual(estimate["upper"], 0.325)
        self.assertEqual(row["requests"], 24)
        self.assertEqual(row["cost"], 1.55)
        self.assertEqual(oc.project(row, 0), {"lower": 0.3875, "upper": 0.3875})

    def test_project_matches_the_measurement_document_bounds(self):
        row = {"requests": 24, "cost": 1.55, "findings": 4, "cache_write": 106_250,
               "by_model": {"claude-opus-5-5": 1.55}}
        estimate = oc.project(row, 8)
        self.assertAlmostEqual(estimate["lower"], 0.2583, places=4)
        self.assertAlmostEqual(estimate["upper"], 0.3292, places=4)

    def test_project_without_cache_writes_has_the_same_bounds(self):
        row = {"requests": 24, "cost": 1.55, "findings": 4, "cache_write": 0, "by_model": {}}
        estimate = oc.project(row, 8)
        self.assertAlmostEqual(estimate["lower"], 0.25833333333333336)
        self.assertAlmostEqual(estimate["upper"], estimate["lower"])

    def test_project_unmeasured_values_and_invalid_request_counts(self):
        row = {"requests": 24, "cost": 1.55, "findings": 4, "cache_write": 100_000,
               "by_model": {"claude-opus-5-5": 1.55}}
        for field, value in (("cost", None), ("findings", 0), ("requests", 0)):
            with self.subTest(field=field):
                self.assertEqual(oc.project(dict(row, **{field: value}), 0), {"lower": None, "upper": None})
        for removed in (-1, 25):
            with self.subTest(removed=removed), self.assertRaises(ValueError):
                oc.project(row, removed)
        estimate = oc.project(dict(row, by_model={"unknown": 1.55}), 8)
        self.assertAlmostEqual(estimate["lower"], 0.25833333333333336)
        self.assertIsNone(estimate["upper"])

    def test_main_prints_projection_only_when_requested(self):
        session = str(self.two_runs())
        for options, count in (([], 0), (["--remove-requests", "1"], 2)):
            with self.subTest(options=options):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(oc.main(["--session", session, *options]), 0)
                lines = out.getvalue().splitlines()
                projections = [i for i, line in enumerate(lines) if line.startswith("추정:")]
                self.assertEqual(len(projections), count)
                for i in projections:
                    self.assertTrue(lines[i - 1].startswith("| s1 |"))
                    self.assertIn("~", lines[i])
                    self.assertIn("지적당", lines[i])


if __name__ == "__main__":
    unittest.main()
