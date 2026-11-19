import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import assignment, cli, cli_steps, usage  # noqa: E402
from helpers import run_cli  # noqa: E402
from test_cli_e2e import CliTestBase  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
USAGE_ROW = {"input_tokens": 3, "output_tokens": 20, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 40}


def write_workflow(path, agents):
    """agents: (label, model) 목록. Task 1이 읽는 형식의 합성 Workflow 기록 폴더를 만든다."""
    path.mkdir(parents=True)
    journal = [{"type": "launched"}]
    for n, (label, model) in enumerate(agents, 1):
        agent_id = f"a{n}"
        journal.append({"type": "started", "key": f"k{n}", "agentId": agent_id, "label": label, "phase": "Jobs"})
        (path / f"agent-{agent_id}.meta.json").write_text(json.dumps({"model": model}), encoding="utf-8")
        row = {"message": {"id": f"msg_{n}", "usage": USAGE_ROW}}
        (path / f"agent-{agent_id}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (path / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in journal) + "\n", encoding="utf-8")
    return path


class UsageCliBase(CliTestBase):
    def setUp(self):
        super().setUp()
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.rd = self.run_dir / "round-1"
        jobs = json.loads((self.rd / "stage1-args.json").read_text(encoding="utf-8"))["jobs"]
        self.labels = {j["id"]: (j["label"], j["model"]) for j in jobs}

    def workflow(self, name="wf_1", extra=()):
        agents = [self.labels["r1-01:a"], self.labels["r1-01:b"], *extra]
        return write_workflow(self.tmp / name, agents)

    def usage(self, wf, *extra, ok=True):
        return self.cli("usage", "--run-dir", str(self.run_dir), "--round", "1", "--workflow", str(wf), *extra, ok=ok)

    def recorded(self):
        return json.loads((self.rd / "claude-usage.json").read_text(encoding="utf-8"))


class UsageRecordTest(UsageCliBase):
    def test_usage_records_per_job_and_cell(self):
        wf = self.workflow()
        self.usage(wf)
        data = self.recorded()
        rows = {r["job_id"]: r for r in data["workflows"][wf.name]}
        self.assertEqual(data["prices_as_of"], usage.PRICES_AS_OF)
        self.assertEqual(data["price_source"], usage.PRICE_SOURCE)
        self.assertEqual(rows["r1-01:a"]["cell"], "code.a")
        self.assertEqual(rows["r1-01:a"]["model"], SONNET)
        self.assertEqual(rows["r1-01:b"]["cell"], "code.b")
        self.assertEqual(rows["r1-01:b"]["model"], HAIKU)
        self.assertEqual((rows["r1-01:a"]["input"], rows["r1-01:a"]["output"], rows["r1-01:a"]["cache_read"],
                          rows["r1-01:a"]["cache_write"]), (3, 20, 500, 40))
        self.assertAlmostEqual(rows["r1-01:a"]["cost"], usage.cost(SONNET, 3, 20, 500, 40))
        self.assertEqual(rows["r1-01:a"]["label"], self.labels["r1-01:a"][0])
        self.assertEqual(data["unmatched"], {wf.name: []})

    def test_same_workflow_twice_is_idempotent(self):
        wf = self.workflow()
        for _ in range(2):
            self.usage(wf)
        data = self.recorded()
        self.assertEqual(len(data["workflows"]), 1)
        self.assertEqual(len(data["workflows"][wf.name]), 2)

    def test_another_workflow_is_kept_beside_the_first(self):
        first, second = self.workflow("wf_1"), self.workflow("wf_2")
        self.usage(first)
        self.usage(second)
        self.assertEqual(sorted(self.recorded()["workflows"]), ["wf_1", "wf_2"])

    def test_rerun_replaces_the_workflow_share(self):
        wf = self.workflow()
        self.usage(wf)
        (wf / "agent-a1.jsonl").write_text(
            json.dumps({"message": {"id": "msg_9", "usage": {**USAGE_ROW, "output_tokens": 999}}}) + "\n", encoding="utf-8")
        self.usage(wf)
        rows = {r["job_id"]: r for r in self.recorded()["workflows"][wf.name]}
        self.assertEqual(rows["r1-01:a"]["output"], 999)

    def test_unmatched_label_is_reported(self):
        wf = self.workflow(extra=[("zz unknown", SONNET)])
        proc = self.usage(wf)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("일치하지 않음 1", proc.stdout)
        data = self.recorded()
        self.assertEqual(data["unmatched"][wf.name], ["zz unknown"])
        self.assertEqual(sorted(r["job_id"] for r in data["workflows"][wf.name]), ["r1-01:a", "r1-01:b"])

    def test_event_is_appended_to_the_round(self):
        wf = self.workflow(extra=[("zz unknown", SONNET)])
        self.usage(wf)
        event = [e for e in self.ledger().rounds[0].events if e["step"] == "usage"][-1]
        self.assertEqual((event["workflow"], event["jobs"], event["unmatched"], event["error"]), (wf.name, 2, 1, None))

    def test_unreadable_workflow_does_not_fail(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        proc = self.usage(empty)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Claude 토큰 미측정", proc.stdout)
        event = [e for e in self.ledger().rounds[0].events if e["step"] == "usage"][-1]
        self.assertTrue(event["error"])
        self.assertFalse((self.rd / "claude-usage.json").exists())

    def test_unknown_workflow_id_does_not_fail(self):
        proc = self.cli("usage", "--run-dir", str(self.run_dir), "--round", "1", "--workflow", "wf_no_such_run_id")
        self.assertIn("Claude 토큰 미측정", proc.stdout)

    def test_unreadable_rerun_keeps_the_earlier_record(self):
        wf = self.workflow()
        self.usage(wf)
        (wf / "journal.jsonl").write_text("", encoding="utf-8")
        self.usage(wf)
        self.assertEqual(len(self.recorded()["workflows"][wf.name]), 2)

    def test_invalid_json_record_is_replaced_with_a_warning(self):
        wf = self.workflow()
        (self.rd / "claude-usage.json").write_text("{not json", encoding="utf-8")
        proc = self.usage(wf)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("경고: 기존 claude-usage.json이 JSON 객체가 아니어서 덮어쓴다", proc.stdout)
        self.assertEqual(len(self.recorded()["workflows"][wf.name]), 2)

    def test_valid_record_is_replaced_without_that_warning(self):
        wf = self.workflow()
        self.usage(wf)
        self.assertNotIn("덮어쓴다", self.usage(wf).stdout)

    def test_usage_event_does_not_make_the_report_look_stale(self):
        ledger = self.ledger()
        ledger.status = "종료"
        ledger.save(self.run_dir / "ledger.json")
        self.cli("report", "--run-dir", str(self.run_dir))
        before = cli_steps.next_steps(self.ledger(), self.run_dir)
        self.usage(self.workflow())
        self.assertEqual(cli_steps.next_steps(self.ledger(), self.run_dir), before)
        self.assertNotIn("report --run-dir", " ".join(before))

    def test_report_shows_the_recorded_usage(self):
        before = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        self.assertIn("| code.a | 2 | 0 | 미측정 | 미측정 | - |", before)
        self.usage(self.workflow())
        after = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        cost = usage.cost(SONNET, 3, 20, 500, 40)
        self.assertIn(f"| code.a | 2 | 0 | 3/20/500 외 미측정 1건 | ${cost:.4f} 외 미측정 1건 |", after)
        self.assertIn("단가 기준: " + usage.PRICES_AS_OF, after)

    def jobs_force(self):
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", "--force")

    def test_reopening_the_round_moves_the_record_aside(self):
        self.usage(self.workflow())
        first = self.recorded()
        self.jobs_force()
        self.assertFalse((self.rd / "claude-usage.json").exists())
        self.assertEqual(json.loads((self.rd / "claude-usage.superseded-1.json").read_text(encoding="utf-8")), first)
        self.usage(self.workflow("wf_2"))
        self.jobs_force()
        self.assertFalse((self.rd / "claude-usage.json").exists())
        self.assertTrue((self.rd / "claude-usage.superseded-1.json").is_file())
        self.assertEqual(sorted(json.loads((self.rd / "claude-usage.superseded-2.json").read_text(encoding="utf-8"))["workflows"]),
                         ["wf_2"])

    def test_reopening_without_a_record_leaves_no_superseded_file(self):
        self.jobs_force()
        self.assertEqual(list(self.rd.glob("claude-usage*")), [])

    def test_report_shows_superseded_cost_apart_from_the_current_total(self):
        self.usage(self.workflow())
        self.jobs_force()
        text = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        cost = usage.cost(SONNET, 3, 20, 500, 40) + usage.cost(HAIKU, 3, 20, 500, 40)
        self.assertIn(f"다시 연 회차의 이전 Claude 비용: ${cost:.4f}(별도, 합계에 넣지 않음)", text)
        self.assertIn("합계: Claude 토큰·비용 미측정", text)

    def test_opinion_and_recheck_jobs_map_to_cells(self):
        (self.rd / "opinion-args.json").write_text(json.dumps(
            {"jobs": [{"id": "r1-01:opinion", "label": "r1-01 opinion"}]}), encoding="utf-8")
        (self.rd / "recheck-args.json").write_text(json.dumps(
            {"jobs": [{"id": "round-1:recheck", "label": "round-1 recheck"}]}), encoding="utf-8")
        wf = write_workflow(self.tmp / "wf_review", [("r1-01 opinion", SONNET), ("round-1 recheck", SONNET)])
        self.usage(wf)
        rows = {r["job_id"]: r["cell"] for r in self.recorded()["workflows"][wf.name]}
        self.assertEqual(rows, {"r1-01:opinion": "code.opinion", "round-1:recheck": "code.recheck"})

    def test_usage_keeps_a_ledger_event_saved_while_it_reads_the_workflow(self):
        wf = self.workflow()
        real = cli._usage_record

        def workers_save_in_between(*args, **kwargs):
            ledger = cli_steps.load_ledger(self.run_dir)
            cli_steps.mark(cli_steps.get_round(ledger, 1), "workers-stage1", counts={"ok": 1})
            cli_steps.save_ledger(self.run_dir, ledger)
            return real(*args, **kwargs)

        with mock.patch.object(cli, "_usage_record", workers_save_in_between), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["usage", "--run-dir", str(self.run_dir), "--round", "1", "--workflow", str(wf)])
        steps = [e["step"] for e in json.loads((self.run_dir / "ledger.json").read_text(encoding="utf-8"))
                 ["rounds"][0]["events"]]
        self.assertIn("workers-stage1", steps)
        self.assertIn("usage", steps)

    def test_run_dir_needs_round(self):
        p = self.cli("usage", "--run-dir", str(self.run_dir), "--workflow", str(self.workflow()), ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertNotIn("Traceback", p.stderr)

    def test_needs_exactly_one_of_run_dir_and_review_dir(self):
        wf = str(self.workflow())
        both = self.cli("usage", "--run-dir", str(self.run_dir), "--round", "1", "--review-dir", str(self.tmp),
                        "--workflow", wf, ok=False)
        neither = self.cli("usage", "--workflow", wf, ok=False)
        self.assertEqual((both.returncode, neither.returncode), (5, 5))


class JobCellKeyTest(unittest.TestCase):
    def test_review_roles_map_to_cells(self):
        for mode in ("code", "doc"):
            self.assertEqual(assignment.job_cell_key(mode, "opinion"), f"{mode}.opinion")
            self.assertEqual(assignment.job_cell_key(mode, "recheck"), f"{mode}.recheck")
            self.assertEqual(assignment.job_cell_key(mode, "adjudicator"), f"{mode}.adjudicator")
        self.assertEqual(assignment.job_cell_key("code", "a"), "code.a")
        self.assertIsNone(assignment.job_cell_key("code", "writer"))
        self.assertIsNone(assignment.job_cell_key("code", "stranger"))


class FirstReviewUsageTest(CliTestBase):
    def review_dir(self):
        path = self.tmp / "review-1"
        path.mkdir()
        (path / "review-args.json").write_text(json.dumps(
            {"jobs": [{"id": "review:reviewer", "label": "code review", "model": SONNET}]}), encoding="utf-8")
        (path / "review-meta.json").write_text(json.dumps(
            {"role": "code.review", "actual": {"runner": "claude", "model": SONNET, "effort": "high"}}), encoding="utf-8")
        shutil.copyfile(FIXTURES / "codex-code-e2e.final", path / "review.out.final")
        return path

    def test_review_dir_usage_uses_the_meta_role_as_cell(self):
        review_dir = self.review_dir()
        wf = write_workflow(self.tmp / "wf_review", [("code review", SONNET)])
        p = self.cli("usage", "--review-dir", str(review_dir), "--workflow", str(wf))
        self.assertIn("Claude 토큰", p.stdout)
        data = json.loads((review_dir / "claude-usage.json").read_text(encoding="utf-8"))
        [row] = data["workflows"][wf.name]
        self.assertEqual((row["job_id"], row["cell"], row["model"]), ("review:reviewer", "code.review", SONNET))
        self.assertEqual(data["unmatched"], {wf.name: []})

    def test_review_dir_is_idempotent_and_unreadable_does_not_fail(self):
        review_dir = self.review_dir()
        wf = write_workflow(self.tmp / "wf_review", [("code review", SONNET)])
        for _ in range(2):
            self.cli("usage", "--review-dir", str(review_dir), "--workflow", str(wf))
        data = json.loads((review_dir / "claude-usage.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["workflows"]), 1)
        empty = self.tmp / "empty"
        empty.mkdir()
        p = self.cli("usage", "--review-dir", str(review_dir), "--workflow", str(empty))
        self.assertIn("Claude 토큰 미측정", p.stdout)

    def init_from(self, review_dir):
        p = self.cli("init", "--source", "codex-code", "--input", str(review_dir / "review.out.final"))
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        return json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))["profile"]["first_review"]

    def test_init_stores_the_review_usage(self):
        review_dir = self.review_dir()
        wf = write_workflow(self.tmp / "wf_review", [("code review", SONNET)])
        self.cli("usage", "--review-dir", str(review_dir), "--workflow", str(wf))
        first_review = self.init_from(review_dir)
        self.assertEqual(first_review["usage"], json.loads((review_dir / "claude-usage.json").read_text(encoding="utf-8")))
        self.assertEqual(first_review["role"], "code.review")

    def test_init_without_review_usage_has_no_usage_key(self):
        self.assertNotIn("usage", self.init_from(self.review_dir()))

    def test_usage_after_init_warns_that_the_report_will_not_get_it(self):
        review_dir = self.review_dir()
        wf = write_workflow(self.tmp / "wf_review", [("code review", SONNET)])
        self.init_from(review_dir)
        p = self.cli("usage", "--review-dir", str(review_dir), "--workflow", str(wf))
        self.assertIn("이미 init에 쓰였다", p.stdout)

    def test_usage_before_init_has_no_warning(self):
        review_dir = self.review_dir()
        wf = write_workflow(self.tmp / "wf_review", [("code review", SONNET)])
        p = self.cli("usage", "--review-dir", str(review_dir), "--workflow", str(wf))
        self.assertNotIn("이미 init에 쓰였다", p.stdout)


class WorkflowStepTest(unittest.TestCase):
    def test_workflow_step_mentions_usage(self):
        self.assertIn("CF usage", cli_steps.workflow_step(Path("a.json"), Path("b.json")))

    def test_round_step_names_run_dir_and_round(self):
        step = cli_steps.workflow_step(Path("/x/run dir/round-3/stage1-args.json"), Path("/x/run dir/round-3/stage1.json"))
        self.assertIn("CF usage --run-dir '/x/run dir' --round 3 --workflow <Workflow 실행 id>", step)

    def test_first_review_step_names_review_dir(self):
        step = cli_steps.workflow_step(Path("/x/review-1/review-args.json"), Path("/x/review-1/review.json"))
        self.assertIn("CF usage --review-dir /x/review-1 --workflow <Workflow 실행 id>", step)


if __name__ == "__main__":
    unittest.main()
