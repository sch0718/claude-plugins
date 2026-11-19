import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli, cli_steps  # noqa: E402
from cascade.rounds import RoundRecord  # noqa: E402
from fake_cli import fenced, result_json  # noqa: E402
from test_cli_e2e import CALC, FIXTURES, fix, results_json, section_of, worker_output  # noqa: E402
from test_workers import ExternalWorkersBase  # noqa: E402

LIMIT_ENTRY = {"is_error": True, "result": "You've hit your usage limit", "exit": 1}
CODE_FIXES = {
    "r1-01-": {"calc.py": fix(CALC, "return a - b", "return a + b")},
    "r1-02-": {"calc.py": fix(CALC, "return a + b", "return a * b")},
}
PLAN = "# 계획\n\n1단계는 `calc.py`를 고친다.\n"
CHECKER_RESULT = json.dumps({"verdict": "사실", "needs_design_decision": False, "reason": "해소한다",
                             "checker_ok": True, "model_id": "x"}, ensure_ascii=False)


def role_sections(report):
    return section_of(report, "## 역할 배정") + section_of(report, "## 역할별 사용량")


class WorkersTestBase(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        reply = json.loads(self.env["FAKE_CURSOR_RESULTS"])[0]
        self.sandbox_reply = reply
        self.env.update({"FAKE_CODEX_RESULT": result_json(), "FAKE_MUTATE_WT": json.dumps(CODE_FIXES, ensure_ascii=False)})
        self.cursor_results([])

    def cursor_results(self, extra):
        self.env["FAKE_CURSOR_RESULTS"] = json.dumps(
            [self.sandbox_reply, {"result": fenced(result_json())}, *extra], ensure_ascii=False)

    def rd(self):
        return self.run_dir / "round-1"

    def read(self, name):
        return json.loads((self.rd() / name).read_text(encoding="utf-8"))

    def args(self, command, *extra, ok=True):
        return self.cli(command, "--run-dir", str(self.run_dir), "--round", "1", *extra, ok=ok)

    def start(self, *init_extra):
        self.use_fakes()
        self.init("--workers", "external", "--reviewer", "claude", *init_extra)
        self.args("jobs")

    def workers(self, stage="stage1", ok=True):
        return self.args("workers", "--stage", stage, ok=ok)

    def event(self, step):
        return next(e for e in self.ledger().rounds[0].events if e["step"] == step)


class WorkersCommandTest(WorkersTestBase):
    def test_external_run_reaches_report_without_claude_stage1(self):
        self.start()
        p = self.workers()
        self.assertIn("CLI 작업 4건: 성공 4, 대체 실행 0", p.stdout)
        self.assertNotIn("외부 재실행", p.stdout)
        cli = self.read("stage1-cli.json")
        self.assertEqual([o["status"] for o in cli["outcomes"]], ["ok"] * 4)
        self.assertEqual([r["id"] for r in cli["results"]], ["r1-01:a", "r1-01:b", "r1-02:a", "r1-02:b"])
        self.assertEqual(cli["results"][0]["output"]["model_id"], "codex:gpt-test")
        self.assertFalse((self.rd() / "stage1-fallback-args.json").exists())

        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        judge_line = next(line for line in status.splitlines() if " judge " in line)
        self.assertNotIn("--results", judge_line)

        p = self.args("judge")
        self.assertNotIn("경고", p.stdout)
        outcomes = {d["finding_id"]: d["outcome"] for d in self.read("decisions.json")}
        self.assertEqual(outcomes, {"r1-01": "합의-사실", "r1-02": "합의-사실"})
        self.assertEqual(self.read("stage1-args.json")["jobs"], [])

        self.args("apply")
        calc = (self.repo / "calc.py").read_text(encoding="utf-8")
        self.assertIn("return a + b\n\n\ndef ident", calc)
        self.assertIn("return a * b", calc)
        report = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        self.assertIn("## 역할 배정", report)
        self.assertIn("## 역할별 사용량", report)

    def test_empty_codex_model_reports_default_model_id_without_warning(self):
        (self.home / ".codex" / "config.toml").write_text("", encoding="utf-8")
        self.start()
        self.assertEqual(self.ledger().workers["a"]["model"], "")
        self.workers()
        self.assertEqual(self.read("stage1-cli.json")["results"][0]["output"]["model_id"], "codex:default")
        p = self.args("judge")
        self.assertNotIn("경고", p.stdout)
        self.assertEqual(self.read("results.json")["r1-01:a"]["model_id"], "codex:default")

    def test_failed_cli_job_goes_to_fallback_and_judge_merges(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-02-b")])
        self.start()
        p = self.workers()
        self.assertIn("CLI 작업 4건: 성공 3, 대체 실행 1(한도 1) → 외부 재실행 성공 0, Claude 대체 1", p.stdout)
        fallback = self.read("stage1-fallback-args.json")
        self.assertEqual(len(fallback["jobs"]), 1)
        job = fallback["jobs"][0]
        self.assertEqual((job["id"], job["model"], job["effort"], job["kind"]), ("r1-02:b", "claude-haiku-5-5", "low", "worker"))
        self.assertEqual(job["label"], "r1-02 b")
        self.assertIn("r1-02-b", Path(job["promptPath"]).read_text(encoding="utf-8"))
        self.assertNotIn("runner", job)
        cli = self.read("stage1-cli.json")
        self.assertEqual({o["id"]: o["status"] for o in cli["outcomes"]}["r1-02:b"], "limit")
        event = self.event("workers-stage1")
        self.assertEqual((event["counts"], event["fallback"]), ({"ok": 3, "limit": 1}, ["r1-02:b"]))
        self.assertEqual(event["usage"]["codex"], {"input": 22, "output": 14, "cache_read": 6})
        self.assertEqual(event["usage"]["cursor"], {"input": 200, "output": 40, "cache_read": 10})

        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        self.assertIn("stage1-fallback-args.json", status)
        self.assertIn("stage1-fallback.json", status)

        results_json(self.rd() / "stage1-fallback.json", [("r1-02:b", worker_output("사실", "claude-haiku-5-5"))])
        p = self.args("judge")
        self.assertNotIn("경고", p.stdout)
        merged = self.read("results.json")
        self.assertEqual(merged["r1-02:b"]["model_id"], "claude-haiku-5-5")
        self.assertEqual(merged["r1-02:a"]["model_id"], "codex:gpt-test")
        self.assertEqual(merged["r1-01:b"]["model_id"], "cursor:composer-2.5")

    def test_fallback_result_with_wrong_model_warns(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-02-b")])
        self.start()
        self.workers()
        results_json(self.rd() / "stage1-fallback.json", [("r1-02:b", worker_output("사실", "cursor:composer-2.5"))])
        p = self.args("judge")
        self.assertIn("r1-02:b 모델 ID가 cursor:composer-2.5 이다(기대값 claude-haiku-5-5)", p.stdout)

    def test_workers_force_is_refused_after_judge_and_keeps_results(self):
        self.start()
        self.workers()
        self.args("judge")
        ledger_before = (self.run_dir / "ledger.json").read_bytes()
        cli_before = (self.rd() / "stage1-cli.json").read_bytes()
        results_before = (self.rd() / "results.json").read_bytes()
        for extra in ((), ("--force",)):
            p = self.args("workers", "--stage", "stage1", *extra, ok=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("judge를 이미 했다", p.stdout)
            self.assertIn("jobs --force", p.stdout)
        self.assertEqual((self.run_dir / "ledger.json").read_bytes(), ledger_before)
        self.assertEqual((self.rd() / "stage1-cli.json").read_bytes(), cli_before)
        self.assertEqual((self.rd() / "results.json").read_bytes(), results_before)

    def test_workers_after_jobs_force_reopens_round_after_judge(self):
        self.start()
        self.workers()
        self.args("judge")
        self.args("jobs", "--force")
        self.workers()

    def test_jobs_force_after_judge_discards_old_judgement(self):
        self.start()
        self.workers()
        self.args("judge")
        (self.rd() / "adj-args.json").write_text('{"jobs": []}', encoding="utf-8")
        (self.rd() / "opinion-r1-01.md").write_text("의견 요청", encoding="utf-8")
        self.args("jobs", "--force")
        self.assertEqual([e["step"] for e in self.ledger().rounds[0].events], ["init", "jobs"])
        self.assertFalse((self.rd() / "adj-args.json").exists())
        self.assertFalse((self.rd() / "opinion-r1-01.md").exists())
        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        self.assertNotIn(" apply ", status)
        self.workers()
        self.args("judge")

    def test_jobs_force_after_apply_is_refused(self):
        self.start()
        self.workers()
        self.args("judge")
        for step in ("apply-patched", "apply"):
            ledger = self.ledger()
            ledger.rounds[0].events.append({"step": step, "at": "x"})
            ledger.save(self.run_dir / "ledger.json")
            p = self.args("jobs", "--force", ok=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("apply", p.stdout)

    def test_workers_command_is_refused_twice_without_force(self):
        self.start()
        self.workers()
        p = self.workers(ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--force", p.stdout)
        self.args("workers", "--stage", "stage1", "--force")

    def test_workers_force_reruns_on_input_tree(self):
        self.start()
        self.workers()
        wts = [Path(p) for name, p in self.ledger().snapshot["worktrees"].items() if name.startswith("r1-01-")]
        self.assertTrue(wts)
        self.assertTrue(any((wt / "calc.py").read_text(encoding="utf-8") != CALC for wt in wts))
        self.env["FAKE_MUTATE_WT"] = "{}"
        self.args("workers", "--stage", "stage1", "--force")
        for wt in wts:
            self.assertEqual((wt / "calc.py").read_text(encoding="utf-8"), CALC, wt)

    def test_jobs_force_restores_worktrees_before_reopening(self):
        self.start()
        self.workers()
        wts = [Path(p) for name, p in self.ledger().snapshot["worktrees"].items() if name.startswith("r1-01-")]
        self.assertTrue(any((wt / "calc.py").read_text(encoding="utf-8") != CALC for wt in wts))
        self.args("jobs", "--force")
        for wt in wts:
            self.assertEqual((wt / "calc.py").read_text(encoding="utf-8"), CALC, wt)
        self.env["FAKE_MUTATE_WT"] = "{}"
        self.workers()
        self.args("workers", "--stage", "stage1", "--force")
        for wt in wts:
            self.assertEqual((wt / "calc.py").read_text(encoding="utf-8"), CALC, wt)

    def test_workers_without_cli_jobs_is_error(self):
        self.init("--reviewer", "claude")
        self.args("jobs")
        p = self.workers(ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("CLI 작업이 없다", p.stdout)

    def test_missing_executable_sends_every_job_to_fallback(self):
        self.start()
        self.env["CASCADE_CODEX"] = str(self.tmp / "no-such-codex")
        p = self.workers()
        self.assertIn("CLI 작업 4건: 성공 2, 대체 실행 2(오류 2)", p.stdout)
        self.assertEqual([j["id"] for j in self.read("stage1-fallback-args.json")["jobs"]], ["r1-01:a", "r1-02:a"])
        self.assertEqual([j["model"] for j in self.read("stage1-fallback-args.json")["jobs"]], ["claude-sonnet-5-5"] * 2)

    def test_judge_refuses_before_workers_done(self):
        self.start()
        p = self.args("judge", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("workers 단계를 먼저", p.stdout)

    def test_judge_refuses_when_fallback_result_missing(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-02-b")])
        self.start()
        self.workers()
        p = self.args("judge", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("stage1-fallback.json", p.stdout)

    def test_judge_requires_results_when_claude_jobs_exist(self):
        self.use_fakes()
        del self.env["CASCADE_CURSOR_AGENT"]
        self.init("--workers", "external", "--reviewer", "claude")
        self.args("jobs")
        self.workers()
        p = self.args("judge", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--results", p.stdout)
        self.assertIn("Workflow", p.stdout)
        self.assertIn("stage1-args.json", p.stdout)

    def test_next_steps_run_workers_and_workflow_together(self):
        self.use_fakes()
        del self.env["CASCADE_CURSOR_AGENT"]
        self.init("--workers", "external", "--reviewer", "claude")
        p = self.args("jobs")
        lines = p.stdout.splitlines()
        workers_line = next(line for line in lines if "(백그라운드로)" in line)
        self.assertIn(" workers ", workers_line)
        self.assertIn("--stage stage1", workers_line)
        self.assertTrue(any("Workflow" in line and "stage1-args.json" in line for line in lines))
        self.assertTrue(any("동시에" in line for line in lines))
        self.assertFalse(any(" judge " in line for line in lines))

        self.workers()
        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        self.assertNotIn("(백그라운드로)", status)
        judge_line = next(line for line in status.splitlines() if " judge " in line)
        self.assertIn("--results", judge_line)

    def test_jobs_force_discards_stale_workers_results(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-02-b")])
        self.start()
        self.workers()
        results_json(self.rd() / "stage1-fallback.json", [("r1-02:b", worker_output("사실", "claude-haiku-5-5"))])
        self.args("jobs", "--force")
        for name in ("stage1-cli.json", "stage1-fallback-args.json", "stage1-fallback.json"):
            self.assertFalse((self.rd() / name).exists(), name)
        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        self.assertTrue(any("(백그라운드로)" in line and " workers " in line for line in status.splitlines()))
        self.assertNotIn("stage1-fallback-args.json", status)
        p = self.args("judge", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("workers 단계를 먼저", p.stdout)
        self.workers()

    def test_workers_requires_jobs_first(self):
        self.use_fakes()
        self.init("--workers", "external", "--reviewer", "claude")
        p = self.workers(ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("jobs 단계를 먼저", p.stdout)

    def test_next_steps_after_workers_include_pending_claude_workflow(self):
        self.use_fakes()
        del self.env["CASCADE_CURSOR_AGENT"]
        self.init("--workers", "external", "--reviewer", "claude")
        self.args("jobs")
        p = self.workers()
        lines = p.stdout.splitlines()
        workflow = next(i for i, line in enumerate(lines) if "Workflow" in line and "stage1-args.json" in line)
        judge = next(i for i, line in enumerate(lines) if " judge " in line)
        self.assertLess(workflow, judge)
        results_json(self.rd() / "stage1.json", [("r1-01:b", worker_output("사실", "claude-haiku-5-5"))])
        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        self.assertNotIn("Workflow", status)

    def test_jobs_force_discards_stale_claude_results_in_mixed_assignment(self):
        self.use_fakes()
        del self.env["CASCADE_CURSOR_AGENT"]
        self.init("--workers", "external", "--reviewer", "claude")
        self.args("jobs")
        self.workers()
        stage1 = results_json(self.rd() / "stage1.json", [("r1-01:b", worker_output("사실", "claude-haiku-5-5"))])
        checker = self.rd() / "checker.json"
        checker.write_text("{}", encoding="utf-8")
        self.args("jobs", "--force")
        self.assertFalse(stage1.exists())
        self.assertFalse(checker.exists())
        self.workers()
        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        lines = status.splitlines()
        workflow = next(i for i, line in enumerate(lines) if "Workflow" in line and "stage1-args.json" in line)
        judge = next(i for i, line in enumerate(lines) if " judge " in line)
        self.assertLess(workflow, judge)

    def test_next_steps_without_cli_jobs_are_unchanged(self):
        self.init("--reviewer", "claude")
        p = self.args("jobs")
        self.assertNotIn(" workers ", p.stdout)
        self.assertNotIn("동시에", p.stdout)
        self.assertIn("--results", p.stdout)


CODEX_LIMIT = {"FAKE_CODEX_EXIT": "1", "FAKE_CODEX_STDERR": "You've hit your usage limit"}
OK_CURSOR = [{"result": fenced(result_json())}]


class ProfileFallbackTest(WorkersTestBase):
    def add_profile(self, *lines):
        path = Path(self.env["CASCADE_REPOS_TOML"])
        path.write_text(path.read_text(encoding="utf-8") + "\n[profiles.mine]\nbase = \"claude-only\"\n" + "\n".join(lines) + "\n",
                        encoding="utf-8")

    def start_profile(self, cell, *init_extra, cursor=OK_CURSOR):
        self.add_profile(f'"code.a" = {cell}')
        self.use_fakes()
        self.env["FAKE_CURSOR_RESULTS"] = json.dumps(cursor, ensure_ascii=False)
        self.init("--profile", "mine", *init_extra)
        self.args("jobs")

    def calls(self, name):
        return len(list((self.tmp / "log").glob(f"{name}-[0-9]*.args.json")))

    def test_claude_only_stage1_args_unchanged(self):
        self.use_fakes()
        shapes = []
        for extra in (("--profile", "claude-only"), ("--workers", "claude", "--reviewer", "claude")):
            self.init(*extra)
            self.args("jobs")
            jobs = self.read("stage1-args.json")["jobs"]
            shapes.append([{k: v for k, v in j.items() if k != "promptPath"} for j in jobs])
        self.assertEqual(shapes[0], shapes[1])
        self.assertEqual([(j["id"], j["model"], j["effort"]) for j in shapes[0]][:2],
                         [("r1-01:a", "claude-sonnet-5-5", "low"), ("r1-01:b", "claude-haiku-5-5", "low")])

    def test_custom_claude_cell_model_reaches_stage1_args(self):
        self.add_profile('"code.a" = { model = "claude-haiku-5-5", effort = "medium" }')
        self.init("--profile", "mine")
        self.args("jobs")
        first = self.read("stage1-args.json")["jobs"][0]
        self.assertEqual((first["id"], first["model"], first["effort"]), ("r1-01:a", "claude-haiku-5-5", "medium"))
        self.assertNotIn("runner", first)

    def test_external_fallback_runs_in_same_workers_call(self):
        self.start_profile('{ runner = "codex", fallback = { runner = "cursor" } }')
        self.env.update(CODEX_LIMIT)
        p = self.workers()
        cli = self.read("stage1-cli.json")
        self.assertEqual([(o["id"], o["runner"], o["status"]) for o in cli["outcomes"]],
                         [("r1-01:a", "cursor", "ok"), ("r1-02:a", "cursor", "ok")])
        self.assertEqual(cli["outcomes"][0]["output"]["model_id"], "cursor:composer-2.5")
        self.assertFalse((self.rd() / "stage1-fallback-args.json").exists())
        event = self.event("workers-stage1")
        self.assertEqual(event["fallback_runs"], {"r1-01:a": "cursor", "r1-02:a": "cursor"})
        self.assertEqual((event["counts"], event["fallback"]), ({"limit": 1, "skipped": 1}, ["r1-01:a", "r1-02:a"]))
        self.assertEqual(event["exhausted"].keys(), {"codex"})
        self.assertEqual(self.calls("codex"), 1)
        self.assertEqual(self.calls("cursor-agent"), 2)
        self.assertIn("CLI 작업 2건: 성공 0, 대체 실행 2(한도 1, 건너뜀 1) → 외부 재실행 성공 2, Claude 대체 0", p.stdout)
        self.assertIn("외부 CLI로 다시 돌린 작업 2건: r1-01:a→cursor, r1-02:a→cursor", p.stdout)
        merged, expected = cli_steps.load_stage_results(self.rd(), "stage1", None)
        self.assertEqual(expected, {"r1-01:a": "cursor:composer-2.5", "r1-02:a": "cursor:composer-2.5"})
        self.assertEqual(len(merged["results"]), 2)
        section = self.section()
        self.assertIn("대체 실행 2건(한도 1, 건너뜀 1)", section)
        self.assertIn("| cursor | 200 | 40 | 10 |", section)

    def section(self):
        return role_sections(self.cli("report", "--run-dir", str(self.run_dir)).stdout)

    def test_fallback_outside_consent_goes_claude_only(self):
        self.start_profile('{ runner = "codex", fallback = { runner = "cursor" } }', "--consent", "codex")
        self.assertEqual(self.ledger().profile["consented"], ["codex"])
        self.env.update(CODEX_LIMIT)
        self.workers()
        job = self.read("stage1-fallback-args.json")["jobs"][0]
        self.assertEqual((job["id"], job["model"], job["effort"]), ("r1-01:a", "claude-sonnet-5-5", "low"))
        self.assertEqual(self.calls("cursor-agent"), 0)
        self.assertEqual(self.event("workers-stage1")["fallback_runs"], {})

    def test_runtime_guard_blocks_runner_missing_from_ledger_consent(self):
        self.start_profile('{ runner = "codex", fallback = { runner = "cursor" } }')
        path = self.run_dir / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["profile"]["consented"] = ["codex"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.env.update(CODEX_LIMIT)
        self.workers()
        self.assertEqual([j["model"] for j in self.read("stage1-fallback-args.json")["jobs"]], ["claude-sonnet-5-5"] * 2)
        self.assertEqual(self.calls("cursor-agent"), 0)

    def test_sandbox_failed_cli_is_not_the_runtime_fallback_of_another_cell(self):
        self.add_profile('"code.a" = { runner = "cursor", fallback = { runner = "codex" } }', '"code.b" = { runner = "codex" }')
        self.use_fakes()
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-0")])
        self.env["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.init("--profile", "mine")
        self.args("jobs")
        self.assertEqual([j["id"] for j in self.read("stage1-cli-args.json")["jobs"]], ["r1-01:a", "r1-02:a"])
        self.workers()
        self.assertEqual([(j["id"], j["model"]) for j in self.read("stage1-fallback-args.json")["jobs"]],
                         [("r1-01:a", "claude-sonnet-5-5"), ("r1-02:a", "claude-sonnet-5-5")])
        self.assertEqual(self.event("workers-stage1")["fallback_runs"], {})
        self.assertEqual(self.calls("codex"), 0)

    def test_old_ledger_without_profile_falls_back_to_claude(self):
        self.start()
        path = self.run_dir / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["profile"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.env.update(CODEX_LIMIT)
        self.workers()
        self.assertEqual([(j["id"], j["model"]) for j in self.read("stage1-fallback-args.json")["jobs"]],
                         [("r1-01:a", "claude-sonnet-5-5"), ("r1-02:a", "claude-sonnet-5-5")])
        self.assertEqual(self.event("workers-stage1")["fallback_runs"], {})

    def test_exhausted_fallback_runner_goes_claude_only(self):
        self.start_profile('{ runner = "codex", fallback = { runner = "codex", model = "gpt-other" } }')
        self.env.update(CODEX_LIMIT)
        self.workers()
        self.assertEqual([j["model"] for j in self.read("stage1-fallback-args.json")["jobs"]], ["claude-sonnet-5-5"] * 2)
        self.assertEqual(self.calls("codex"), 1)
        self.assertEqual(self.event("workers-stage1")["fallback_runs"], {})

    def test_failed_external_fallback_goes_claude_only(self):
        self.start_profile('{ runner = "codex", fallback = { runner = "cursor" } }', cursor=[dict(LIMIT_ENTRY)])
        self.env.update(CODEX_LIMIT)
        self.workers()
        cli = self.read("stage1-cli.json")
        self.assertEqual({o["runner"] for o in cli["outcomes"]}, {"cursor"})
        self.assertTrue(all(o["status"] in ("limit", "skipped") for o in cli["outcomes"]))
        self.assertEqual([j["model"] for j in self.read("stage1-fallback-args.json")["jobs"]], ["claude-sonnet-5-5"] * 2)
        self.assertEqual(self.event("workers-stage1")["exhausted"].keys(), {"codex", "cursor"})

    def test_claude_fallback_cell_model_is_the_fallback_job_and_expected_model(self):
        self.start_profile('{ runner = "codex", fallback = { model = "claude-haiku-5-5" } }')
        self.env.update(CODEX_LIMIT)
        self.workers()
        jobs = self.read("stage1-fallback-args.json")["jobs"]
        self.assertEqual([(j["model"], j["effort"]) for j in jobs], [("claude-haiku-5-5", "low")] * 2)
        results_json(self.rd() / "stage1-fallback.json", [(j["id"], worker_output("사실", "claude-haiku-5-5")) for j in jobs])
        _, expected = cli_steps.load_stage_results(self.rd(), "stage1", None)
        self.assertEqual(expected, {"r1-01:a": "claude-haiku-5-5", "r1-02:a": "claude-haiku-5-5"})


class DocWorkersTest(WorkersTestBase):
    def setUp(self):
        super().setUp()
        plan = self.repo / "plan.md"
        plan.write_text(PLAN, encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        self.doc_args = ["--source", "codex-doc", "--input", str(raw), "--doc", str(plan)]
        self.env["FAKE_MUTATE_WT"] = json.dumps(
            {"writer": {"plan.md": PLAN.replace("고친다", "고쳐 add가 더하게 한다")}}, ensure_ascii=False)
        self.cursor_results([
            {"match": "r1-01-b", "result": fenced(result_json())},
            {"match": "r1-01-writer", "result": fenced(CHECKER_RESULT)},
        ])

    def test_doc_mode_checker_stage_uses_cli(self):
        self.use_fakes()
        p = self.cli("init", *self.doc_args, "--workers", "external", "--reviewer", "claude")
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.args("jobs")
        stage1 = self.read("stage1-cli-args.json")["jobs"]
        self.assertEqual([(j["id"], j["runner"], j["writable"]) for j in stage1],
                         [("r1-01:a", "codex", False), ("r1-01:b", "cursor", False), ("r1-01:writer", "codex", True)])
        p = self.workers()
        self.assertIn("CLI 작업 3건: 성공 3, 대체 실행 0", p.stdout)

        p = self.args("judge")
        self.assertFalse((self.rd() / "decisions.json").exists())
        self.assertEqual(self.read("checker-args.json")["jobs"], [])
        checker = self.read("checker-cli-args.json")["jobs"]
        self.assertEqual([(j["id"], j["runner"], j["writable"]) for j in checker], [("r1-01:checker", "cursor", False)])
        self.assertIn("--stage checker", p.stdout)
        self.assertNotIn("Workflow", p.stdout)

        p = self.args("judge", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("workers 단계를 먼저", p.stdout)

        p = self.workers("checker")
        self.assertIn("CLI 작업 1건: 성공 1, 대체 실행 0", p.stdout)
        self.assertEqual(self.read("checker-cli.json")["results"][0]["output"]["model_id"], "cursor:composer-2.5")
        p = self.args("judge")
        self.assertNotIn("경고", p.stdout)
        decisions = self.ledger().rounds[0].decisions
        self.assertEqual([(d["outcome"], d["patch_from"]) for d in decisions], [("합의-사실", "writer")])
        self.args("apply")
        self.assertIn("add가 더하게", (self.repo / "plan.md").read_text(encoding="utf-8"))

    def test_report_counts_unrecorded_acp_usage(self):
        self.use_fakes()
        p = self.cli("init", *self.doc_args, "--workers", "external", "--reviewer", "claude")
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.args("jobs")
        self.workers()
        self.assertEqual(self.event("workers-stage1")["usage"]["cursor"],
                         {"input": 0, "output": 0, "cache_read": 0, "unrecorded": 1})
        report = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        section = role_sections(report)
        self.assertIn("| CLI | 입력 토큰 | 출력 토큰 | 캐시 읽기 토큰 | 기록 없음(건) |", section)
        self.assertIn("| cursor | 0 | 0 | 0 | 1 |", section)
        self.assertIn("| codex | 22 | 14 | 6 | 0 |", section)


    def start_doc(self, *workers):
        self.use_fakes()
        p = self.cli("init", *self.doc_args, *workers, "--reviewer", "claude")
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.args("jobs")

    def test_rerunning_stage1_discards_checker_results(self):
        self.start_doc("--workers", "external")
        self.workers()
        self.args("judge")
        self.workers("checker")
        self.assertTrue((self.rd() / "checker-cli.json").is_file())
        self.args("workers", "--stage", "stage1", "--force")
        self.assertFalse((self.rd() / "checker-cli.json").exists())
        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        self.assertNotIn("--stage checker", status)
        p = self.args("judge")
        self.assertFalse((self.rd() / "decisions.json").exists())
        self.assertIn("--stage checker", p.stdout)
        p = self.args("judge", ok=False)
        self.assertIn("workers 단계를 먼저", p.stdout)
        self.workers("checker")
        self.args("judge")
        self.assertEqual([d["outcome"] for d in self.ledger().rounds[0].decisions], ["합의-사실"])

    def cursor_calls(self):
        return len(list((self.tmp / "log").glob("cursor-agent-*.args.json")))

    def test_runner_exhausted_in_stage1_is_not_called_again_in_checker_stage(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-01-b")])
        self.start_doc("--workers", "external")
        p = self.workers()
        self.assertIn("대체 실행 1(한도 1)", p.stdout)
        results_json(self.rd() / "stage1-fallback.json", [("r1-01:b", worker_output("사실", "claude-haiku-5-5"))])
        self.args("judge")
        calls = self.cursor_calls()

        p = self.workers("checker")
        self.assertEqual(self.cursor_calls(), calls)
        self.assertIn("CLI 작업 1건: 성공 0, 대체 실행 1(건너뜀 1)", p.stdout)
        outcome = self.read("checker-cli.json")["outcomes"][0]
        self.assertEqual((outcome["id"], outcome["status"]), ("r1-01:checker", "skipped"))
        self.assertIn("r1-01:b에서 limit", outcome["detail"])
        self.assertEqual([j["id"] for j in self.read("checker-fallback-args.json")["jobs"]], ["r1-01:checker"])
        report = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        section = role_sections(report)
        self.assertIn("회차 1 stage1: CLI 작업 3건, 대체 실행 1건(한도 1)", section)
        self.assertIn("회차 1 checker: CLI 작업 1건, 대체 실행 1건(건너뜀 1)", section)

    def test_jobs_force_forgets_exhausted_runners(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-01-b")])
        self.start_doc("--workers", "external")
        self.workers()
        self.args("jobs", "--force")
        self.cursor_results([{"match": "r1-01-b", "result": fenced(result_json())}])
        p = self.workers()
        self.assertIn("CLI 작업 3건: 성공 3, 대체 실행 0", p.stdout)

    def test_checker_workers_force_is_refused_after_final_judge(self):
        self.start_doc("--workers", "external")
        self.workers()
        self.args("judge")
        self.workers("checker")
        self.args("judge")
        ledger_before = (self.run_dir / "ledger.json").read_bytes()
        cli_before = (self.rd() / "checker-cli.json").read_bytes()
        results_before = (self.rd() / "results.json").read_bytes()
        for extra in ((), ("--force",)):
            p = self.args("workers", "--stage", "checker", *extra, ok=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("judge를 이미 했다", p.stdout)
            self.assertIn("jobs --force", p.stdout)
        self.assertEqual((self.run_dir / "ledger.json").read_bytes(), ledger_before)
        self.assertEqual((self.rd() / "checker-cli.json").read_bytes(), cli_before)
        self.assertEqual((self.rd() / "results.json").read_bytes(), results_before)

    def test_stage1_workers_force_is_refused_after_final_judge_in_doc_mode(self):
        self.start_doc("--workers", "external")
        self.workers()
        self.args("judge")
        self.workers("checker")
        self.args("judge")
        p = self.args("workers", "--stage", "stage1", "--force", ok=False)
        self.assertIn("judge를 이미 했다", p.stdout)

    def test_jobs_force_discards_checker_state(self):
        self.start_doc("--workers", "external")
        self.workers()
        self.args("judge")
        self.workers("checker")
        self.args("jobs", "--force")
        self.assertFalse((self.rd() / "checker-cli.json").exists())
        status = self.cli("status", "--run-dir", str(self.run_dir)).stdout
        self.assertTrue(any("(백그라운드로)" in line and "--stage stage1" in line for line in status.splitlines()))

    def test_checker_workers_need_first_judge(self):
        self.start_doc("--workers", "external")
        self.workers()
        p = self.workers("checker", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("judge 단계를 먼저", p.stdout)

    def test_claude_doc_judge_without_checker_results_rebuilds_checker_jobs(self):
        self.start_doc("--workers", "claude")
        stage1 = results_json(self.rd() / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
            ("r1-01:writer", worker_output("사실", "claude-sonnet-5-5")),
        ])
        wt = Path(self.ledger().snapshot["worktrees"]["r1-01-writer"])
        (wt / "plan.md").write_text(PLAN.replace("고친다", "고쳐 add가 더하게 한다"), encoding="utf-8")
        for _ in range(2):
            p = self.args("judge", "--results", str(stage1))
            self.assertIn("점검자 작업 1건", p.stdout)
        self.assertFalse((self.rd() / "decisions.json").exists())


class ReportWorkersTest(WorkersTestBase):
    def section(self):
        report = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        return role_sections(report)

    def test_report_shows_fallback_and_usage(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-02-b")])
        self.start()
        self.workers()
        section = self.section()
        self.assertIn("A·writer=Codex(gpt-test), B·checker=Cursor(composer-2.5)", section)
        self.assertIn("대체 실행 1건(한도 1)", section)
        self.assertIn("| codex | 22 | 14 | 6 |", section)
        self.assertIn("| cursor | 200 | 40 | 10 |", section)
        self.assertNotIn("login", section)

    def test_report_points_to_login_when_auth_is_missing(self):
        self.start()
        (self.home / ".codex" / "auth.json").unlink()
        self.workers()
        section = self.section()
        self.assertIn("대체 실행 2건(로그인 1, 건너뜀 1)", section)
        self.assertIn("codex login", section)
        self.assertNotIn("cursor-agent login", section)

    def test_report_keeps_login_hint_after_forced_rerun_skips_exhausted_runner(self):
        self.start()
        (self.home / ".codex" / "auth.json").unlink()
        self.workers()
        self.args("workers", "--stage", "stage1", "--force")
        self.assertEqual({o["status"] for o in self.read("stage1-cli.json")["outcomes"] if o["runner"] == "codex"},
                         {"skipped"})
        self.assertIn("codex login", self.section())

    def test_report_drops_login_hint_after_jobs_force_and_successful_rerun(self):
        self.start()
        auth = self.home / ".codex" / "auth.json"
        auth.unlink()
        self.workers()
        auth.write_text("{}", encoding="utf-8")
        self.args("jobs", "--force")
        p = self.workers()
        self.assertIn("대체 실행 0", p.stdout)
        self.assertNotIn("codex login", self.section())

    def test_report_without_external_workers_has_no_cli_lines(self):
        self.init("--reviewer", "claude")
        report = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        self.assertNotIn("외부 작업자", report)
        self.assertIn("## 역할 배정", report)
        self.assertNotIn("CLI 작업", report)
        self.assertNotIn("배정: A·writer", report)


class RunCliJobsTest(unittest.TestCase):
    def test_closes_runners_even_when_run_all_fails(self):
        closed = []

        class Pooled:
            def close(self):
                closed.append(True)

        with mock.patch.object(cli, "_cli_runners", return_value={"cursor": Pooled(), "codex": object()}), \
                mock.patch.object(cli, "run_all", side_effect=RuntimeError("실패")):
            with self.assertRaises(RuntimeError):
                cli._run_cli_jobs([], {})
        self.assertEqual(closed, [True])


class StageResultsTest(unittest.TestCase):
    def test_merge_order_is_claude_then_cli_ok_then_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td)
            claude = results_json(rd / "stage1.json", [("f:a", worker_output("사실", "claude-sonnet-5-5")),
                                                       ("f:b", worker_output("아님", "claude-haiku-5-5"))])
            (rd / "stage1-cli-args.json").write_text(json.dumps({"jobs": [
                {"id": "f:a", "runner": "codex", "model": ""}, {"id": "g:a", "runner": "codex", "model": "gpt-x"},
                {"id": "g:b", "runner": "cursor", "model": "composer-2.5"}]}), encoding="utf-8")
            (rd / "stage1-cli.json").write_text(json.dumps({
                "results": [{"id": "f:a", "output": worker_output("사실", "codex:default")},
                            {"id": "g:a", "output": worker_output("사실", "codex:gpt-x")},
                            {"id": "g:b", "output": None}],
                "outcomes": [{"id": "f:a", "status": "ok"}, {"id": "g:a", "status": "ok"}, {"id": "g:b", "status": "limit"}],
            }), encoding="utf-8")
            results_json(rd / "stage1-fallback.json", [("g:b", worker_output("사실", "claude-haiku-5-5"))])
            merged, expected = cli_steps.load_stage_results(rd, "stage1", claude)
        by_id = {item["id"]: item["output"] for item in merged["results"]}
        self.assertEqual(sorted(by_id), ["f:a", "f:b", "g:a", "g:b"])
        self.assertEqual(by_id["f:a"]["model_id"], "codex:default")
        self.assertEqual(by_id["g:b"]["model_id"], "claude-haiku-5-5")
        self.assertEqual(expected, {"f:a": "codex:default", "g:a": "codex:gpt-x", "g:b": "claude-haiku-5-5"})

    def test_checker_stage_lists_claude_workflow_before_judge_when_result_missing(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td)
            for name in ("checker-args.json", "checker-cli-args.json"):
                (rd / name).write_text(json.dumps({"jobs": [{"id": "f:checker"}]}), encoding="utf-8")
            rnd = RoundRecord(no=1, findings=[], decisions=[], adopted=[], recheck_opened=False, events=[
                {"step": "jobs"}, {"step": "judge-checker"}, {"step": "workers-checker"}])
            steps = cli_steps._stage_steps(rnd, rd.parent, rd, "checker")
            self.assertTrue("checker-args.json" in steps[0] and "Workflow" in steps[0])
            self.assertIn(" judge ", steps[-1])
            (rd / "checker.json").write_text("{}", encoding="utf-8")
            self.assertEqual(len(cli_steps._stage_steps(rnd, rd.parent, rd, "checker")), 1)

    def test_without_any_file_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(cli_steps.load_stage_results(Path(td), "checker", None), ({"results": []}, {}))


if __name__ == "__main__":
    unittest.main()
