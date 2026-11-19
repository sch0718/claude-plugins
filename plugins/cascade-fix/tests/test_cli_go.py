import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli, cli_steps  # noqa: E402
from cascade.snapshot import commit_worktree  # noqa: E402
from helpers import git  # noqa: E402
from test_cli_e2e import CALC, FIXTURES, RECHECK_RESULT, CliTestBase, fix  # noqa: E402

INPUT = str(FIXTURES / "codex-code-e2e.final")


def workflow_output(path: Path, result: dict) -> Path:
    path.write_text(json.dumps({"summary": "x", "result": result}, ensure_ascii=False), encoding="utf-8")
    return path


class StartGoTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.env["CASCADE_REVIEWER"] = "claude"

    def start(self, *extra):
        p = self.cli("start", "--source", "codex-code", "--input", INPUT, *extra)
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        return p

    def go(self, *extra, ok=True):
        return self.cli("go", "--run-dir", str(self.run_dir), *extra, ok=ok)

    def edit_workers(self):
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, f"cascade-fix {name}")
        wt = self.wt("r1-02-a")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 a")

    def stage1_output(self):
        result = json.loads((FIXTURES / "workflow-stage1.json").read_text(encoding="utf-8"))
        return workflow_output(self.tmp / "stage1.out", result)

    def test_start_runs_init_and_jobs_then_stops_for_workflow(self):
        p = self.start()
        self.assertIn("멈춤: Workflow 결과가 필요하다", p.stdout)
        self.assertIn("stage1-args.json", p.stdout)
        self.assertIn("--workflow-output", p.stdout)
        self.assertTrue((self.run_dir / "round-1" / "stage1-args.json").is_file())
        self.assertEqual(p.stdout.count("다음:"), 1)
        args_line = next(line for line in p.stdout.splitlines() if line.startswith("    args: "))
        self.assertEqual(json.loads(args_line.removeprefix("    args: ")),
                         json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8")))
        self.assertIn(f"    scriptPath: {self.run_dir / 'run-jobs.js'}", p.stdout)
        self.assertEqual((self.run_dir / "run-jobs.js").read_bytes(), cli_steps.RUN_JOBS.read_bytes())

    def test_go_saves_workflow_result_and_judges_then_stops_for_adjudicator(self):
        self.start()
        self.edit_workers()
        p = self.go("--workflow-output", str(self.stage1_output()))
        self.assertIn("Workflow 결과를 저장했다", p.stdout)
        self.assertIn("Claude 토큰 미측정: --workflow 를 주지 않았다", p.stdout)
        self.assertIn("멈춤: Workflow 결과가 필요하다", p.stdout)
        self.assertIn("adj-args.json", p.stdout)
        saved = json.loads((self.run_dir / "round-1" / "stage1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["results"]), 4)
        outcomes = {d["finding_id"]: d["outcome"] for d in self.ledger().rounds[0].decisions}
        self.assertEqual(outcomes, {"r1-01": "합의-사실", "r1-02": "판가름"})

    def test_go_without_output_stops_and_changes_nothing(self):
        self.start()
        before = (self.run_dir / "ledger.json").read_text(encoding="utf-8")
        p = self.go()
        self.assertIn("멈춤: Workflow 결과가 필요하다", p.stdout)
        self.assertEqual((self.run_dir / "ledger.json").read_text(encoding="utf-8"), before)

    def test_go_refuses_output_that_does_not_match_waiting_jobs(self):
        self.start()
        wrong = workflow_output(self.tmp / "wrong.out", {"results": [{"id": "r1-09:a", "output": None}]})
        p = self.go("--workflow-output", str(wrong), ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertIn("기다리는 작업과 다르다", p.stdout)
        self.assertFalse((self.run_dir / "round-1" / "stage1.json").exists())

    def test_go_refuses_output_without_result(self):
        self.start()
        broken = self.tmp / "broken.out"
        broken.write_text("{}", encoding="utf-8")
        p = self.go("--workflow-output", str(broken), ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertIn("결과를 읽지 못했다", p.stdout)

    def test_full_flow_reaches_report_and_stops_before_cleanup(self):
        commits = git(self.repo, "rev-list", "--count", "HEAD")
        self.start()
        self.edit_workers()
        self.go("--workflow-output", str(self.stage1_output()))
        wt = self.wt("r1-02-adj")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 adjudicator")
        adj = workflow_output(self.tmp / "adj.out", json.loads((FIXTURES / "workflow-adj.json").read_text(encoding="utf-8")))
        p = self.go("--workflow-output", str(adj))
        self.assertIn("recheck-args.json", p.stdout)
        self.assertIn("멈춤: Workflow 결과가 필요하다", p.stdout)
        self.assertEqual(self.ledger().rounds[0].adopted, ["r1-01", "r1-02"])
        self.assertIn("return a * b", (self.repo / "calc.py").read_text(encoding="utf-8"))
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), commits)

        recheck = workflow_output(self.tmp / "recheck.out", {"results": [
            {"id": "round-1:recheck", "output": {"text": RECHECK_RESULT, "model_id": "claude-fable-5-1"}}]})
        p = self.go("--workflow-output", str(recheck))
        self.assertEqual(self.ledger().status, "종료")
        self.assertTrue((self.run_dir / "report.md").is_file())
        self.assertIn("멈춤: report까지 끝났다", p.stdout)
        self.assertIn("보류 후보", p.stdout)
        self.assertEqual(p.stdout.count("다음:"), 1)
        self.assertTrue((self.run_dir / "wt").exists())

        p = self.go()
        self.assertIn("멈춤: report까지 끝났다", p.stdout)
        self.assertEqual(len([e for e in self.ledger().rounds[0].events if e["step"] == "report"]), 1)

    def reach_recheck(self):
        self.start()
        self.edit_workers()
        self.go("--workflow-output", str(self.stage1_output()))
        wt = self.wt("r1-02-adj")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 adjudicator")
        adj = workflow_output(self.tmp / "adj.out", json.loads((FIXTURES / "workflow-adj.json").read_text(encoding="utf-8")))
        return self.go("--workflow-output", str(adj))

    def recheck_output(self, name, output):
        return workflow_output(self.tmp / name, {"results": [{"id": "round-1:recheck", "output": output}]})

    def test_go_replaces_unfinished_workflow_result_with_a_new_output(self):
        self.reach_recheck()
        broken = self.recheck_output("null.out", None)
        p = self.go("--workflow-output", str(broken), ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotEqual(self.ledger().status, "종료")
        good = self.recheck_output("good.out", {"text": RECHECK_RESULT, "model_id": "claude-fable-5-1"})
        p = self.go("--workflow-output", str(good))
        self.assertEqual(self.ledger().status, "종료")
        self.assertIn("멈춤: report까지 끝났다", p.stdout)

    def test_go_stops_on_model_id_warning_after_judge(self):
        self.start()
        self.edit_workers()
        result = json.loads((FIXTURES / "workflow-stage1.json").read_text(encoding="utf-8"))
        result["results"][1]["output"]["model_id"] = "wrong-model"
        p = self.go("--workflow-output", str(workflow_output(self.tmp / "wrong-model.out", result)))
        self.assertIn("경고:", p.stdout)
        self.assertIn("멈춤: 모델 ID 경고", p.stdout)
        self.assertNotIn("멈춤: Workflow 결과가 필요하다", p.stdout)

    def external_worker_stage(self):
        self.start()
        self.edit_workers()
        ledger = cli_steps.load_ledger(self.run_dir)
        cli_steps.mark(ledger.current_round(), "workers-stage1")
        cli_steps.save_ledger(self.run_dir, ledger)
        (self.run_dir / "round-1" / "stage1-cli-args.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")

    def test_go_replaces_stage1_result_when_judge_failed_in_an_external_worker_stage(self):
        self.external_worker_stage()
        (self.repo / "calc.py").write_text(CALC + "# 사용자 변경\n", encoding="utf-8")
        p = self.go("--workflow-output", str(self.stage1_output()), ok=False)
        self.assertEqual(p.returncode, 6)
        (self.repo / "calc.py").write_text(CALC, encoding="utf-8")
        fixed = json.loads((FIXTURES / "workflow-stage1.json").read_text(encoding="utf-8"))
        fixed["results"][0]["output"]["reason"] = "교체본"
        p = self.go("--workflow-output", str(workflow_output(self.tmp / "fixed.out", fixed)))
        saved = json.loads((self.run_dir / "round-1" / "stage1.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["results"][0]["output"]["reason"], "교체본")
        self.assertNotIn("쓰지 않았다", p.stdout)
        self.assertIn("adj-args.json", p.stdout)

    def test_next_steps_text_hides_workflow_line_once_external_stage_result_exists(self):
        self.external_worker_stage()
        rd = self.run_dir / "round-1"
        (rd / "stage1.json").write_text(json.dumps({"results": []}), encoding="utf-8")
        ledger = cli_steps.load_ledger(self.run_dir)
        steps = cli_steps.next_steps(ledger, self.run_dir)
        self.assertEqual(len(steps), 1)
        self.assertIn(" judge ", steps[0])
        self.assertTrue(any(a.kind == "workflow" for a in cli_steps.next_actions(ledger, self.run_dir)))

    def test_jobs_force_discards_old_stage1_result_so_go_waits_for_a_new_workflow(self):
        self.start()
        self.edit_workers()
        self.go("--workflow-output", str(self.stage1_output()))
        self.assertTrue((self.run_dir / "round-1" / "stage1.json").is_file())
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", "--force")
        self.assertFalse((self.run_dir / "round-1" / "stage1.json").exists())
        p = self.go()
        self.assertIn("멈춤: Workflow 결과가 필요하다", p.stdout)
        self.assertIn("stage1-args.json", p.stdout)
        self.assertNotIn("판정:", p.stdout)

    def test_go_stop_lists_only_the_workflow_still_waiting_in_a_mixed_runner_stage(self):
        self.external_worker_stage()
        rd = self.run_dir / "round-1"
        (rd / "stage1.json").write_text(json.dumps({"results": []}), encoding="utf-8")
        (rd / "stage1-fallback-args.json").write_text(json.dumps({"jobs": [{"id": "r1-01:b", "label": "r1-01 b", "model": "claude-haiku-5-5",
                                                                           "effort": "low", "kind": "worker", "promptPath": "p.md"}]}), encoding="utf-8")
        p = self.go()
        self.assertIn("멈춤: Workflow 결과가 필요하다", p.stdout)
        self.assertIn("stage1-fallback-args.json", p.stdout)
        self.assertNotIn("stage1-args.json", p.stdout.replace("stage1-fallback-args.json", ""))
        self.assertEqual(p.stdout.count("Workflow 실행:"), 1)

    def test_go_stops_before_recording_a_recheck_with_a_wrong_model_id(self):
        self.reach_recheck()
        wrong = self.recheck_output("wrong-model.out", {"text": RECHECK_RESULT, "model_id": "claude-opus-5-5"})
        p = self.go("--workflow-output", str(wrong))
        self.assertIn("멈춤: 모델 ID 경고", p.stdout)
        self.assertNotEqual(self.ledger().status, "종료")
        self.assertFalse(any(e["step"] == "recheck" for e in self.ledger().rounds[0].events))
        good = self.recheck_output("good-model.out", {"text": RECHECK_RESULT, "model_id": "claude-fable-5-1"})
        p = self.go("--workflow-output", str(good))
        self.assertEqual(self.ledger().status, "종료")
        self.assertIn("멈춤: report까지 끝났다", p.stdout)

    def test_go_retried_while_workers_runs_shows_only_the_remaining_workflow(self):
        self.start()
        (self.run_dir / "round-1" / "stage1-cli-args.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
        out = io.StringIO()
        with cli_steps.workers_lock(self.run_dir), contextlib.redirect_stdout(out):
            code = cli._advance_steps(self.run_dir, None, None, None, [])
        self.assertEqual(code, 0)
        self.assertIn("남은 Workflow만 띄운다", out.getvalue())
        self.assertEqual(out.getvalue().count("Workflow 실행:"), 1)
        self.assertNotIn("cascade.cli workers", out.getvalue())
        self.assertNotIn("동시에 띄운다", out.getvalue().split("다음:")[1])

    def test_go_and_workers_report_a_missing_run_dir_without_a_traceback(self):
        missing = str(self.tmp / "no-such-run")
        for args in (("go", "--run-dir", missing), ("workers", "--run-dir", missing, "--round", "1", "--stage", "stage1")):
            p = self.cli(*args, ok=False)
            self.assertEqual(p.returncode, 1, args)
            self.assertIn("오류: 원장이 없다", p.stdout)
            self.assertNotIn("Traceback", p.stderr)

    def test_go_recomputes_when_workers_finished_between_reading_state_and_checking_the_lock(self):
        self.start()
        (self.run_dir / "round-1" / "stage1-cli-args.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
        calls = []

        def finishing(run_dir):
            ledger = cli_steps.load_ledger(run_dir)
            cli_steps.mark(ledger.current_round(), "workers-stage1")
            cli_steps.save_ledger(run_dir, ledger)
            return False

        with mock.patch.object(cli_steps, "workers_running", finishing), \
                mock.patch.object(cli, "_run_step", lambda argv: (calls.append(argv) or (0, ""))), \
                contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli._advance_steps(self.run_dir, None, None, None, [])
        self.assertEqual(code, 0)
        self.assertEqual([c for c in calls if c[0] == "workers"], [])
        self.assertIn("멈춤: Workflow 결과가 필요하다", buffer.getvalue())

    def review_script(self, name, body):
        script = self.tmp / name
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_review_script_exit_77_without_a_log_stops_for_login(self):
        script = self.review_script("cursor-review.sh", "echo 'Cursor 로그인 필요: cursor-agent login' >&2\nexit 77\n")
        out = self.tmp / "opinion-r1-01.out"
        with mock.patch.object(cli_steps, "CURSOR_REVIEW", script):
            note, stop = cli._run_review_script(f"{script} --opinion p --repo r --out {out}")
        reason, code = stop
        self.assertEqual(code, 0)
        self.assertIn("cursor-agent login", reason)
        self.assertIn("로그인이 필요하다", reason)

    def test_review_script_failure_without_a_log_ignores_an_old_log_and_stops_with_an_error(self):
        script = self.review_script("codex-review.sh", "echo 'codex 실행 파일을 찾지 못했다' >&2\nexit 69\n")
        out = self.tmp / "opinion-r1-01.out"
        out.write_text("Authentication required\n", encoding="utf-8")
        Path(str(out) + ".final").write_text("옛 결과\n", encoding="utf-8")
        note, stop = cli._run_review_script(f"{script} --opinion p --repo r --out {out}")
        reason, code = stop
        self.assertEqual(code, 1)
        self.assertIn("codex 실행 파일을 찾지 못했다", reason)
        self.assertNotIn("로그인", reason)
        self.assertFalse(out.exists())
        self.assertFalse(Path(str(out) + ".final").exists())

    def test_review_script_failure_with_its_own_log_is_left_to_the_next_step(self):
        out = self.tmp / "recheck.out"
        script = self.review_script("codex-review.sh", f"echo 이번 로그 > {out}\nexit 1\n")
        note, stop = cli._run_review_script(f"{script} --recheck p --repo r --out {out}")
        self.assertIsNone(stop)
        self.assertIn("종료 코드 1", note)
        self.assertEqual(out.read_text(encoding="utf-8").strip(), "이번 로그")

    def test_missing_review_script_stops_with_an_error(self):
        note, stop = cli._run_review_script(f"/no/such/review-script --recheck p --repo r --out {self.tmp / 'x.out'}")
        self.assertEqual(stop[1], 1)
        self.assertIn("실행하지 못했다", stop[0])

    def go_with_review_script(self, script):
        self.start()
        out = self.tmp / "recheck.out"
        results = [[cli_steps.Action("shell", f"{script} --recheck p --repo r --out {out}"),
                    cli_steps.Action("cmd", "recheck", argv=("recheck", "--run-dir", str(self.run_dir)))],
                   [cli_steps.Action("note", "끝")]]
        calls = []
        with mock.patch.object(cli_steps, "next_actions", lambda ledger, run_dir: results.pop(0)), \
                mock.patch.object(cli, "_run_step", lambda argv: (calls.append(argv) or (0, ""))), \
                contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli._advance_steps(self.run_dir, None, None, None, [])
        return code, buffer.getvalue(), calls

    def test_go_stops_for_login_without_substituting_when_the_wrapper_exits_77(self):
        script = self.review_script("codex-review.sh", "echo 'Codex 로그인 필요: codex login' >&2\nexit 77\n")
        code, text, calls = self.go_with_review_script(script)
        self.assertEqual(code, 0)
        self.assertIn("멈춤: 로그인이 필요하다", text)
        self.assertIn("codex login", text)
        self.assertEqual(calls, [])

    def test_go_stops_with_an_error_when_the_wrapper_dies_without_a_log(self):
        script = self.review_script("codex-review.sh", "echo 'codex 실행 파일을 찾지 못했다' >&2\nexit 69\n")
        code, text, calls = self.go_with_review_script(script)
        self.assertEqual(code, 1)
        self.assertIn("오류:", text)
        self.assertIn("codex 실행 파일을 찾지 못했다", text)
        self.assertEqual(calls, [])

    def test_go_stops_when_another_go_runs_in_the_same_run(self):
        self.start()
        before = (self.run_dir / "ledger.json").read_text(encoding="utf-8")
        with cli_steps.go_lock(self.run_dir):
            p = self.go()
        self.assertIn("멈춤: go가 이미 돈다", p.stdout)
        self.assertEqual((self.run_dir / "ledger.json").read_text(encoding="utf-8"), before)

    def test_go_does_not_start_workers_while_one_is_running(self):
        self.start()
        rd = self.run_dir / "round-1"
        (rd / "stage1-cli-args.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
        calls = []
        with cli_steps.workers_lock(self.run_dir), mock.patch.object(cli, "_run_step", lambda argv: (calls.append(argv) or (0, ""))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli._advance_steps(self.run_dir, str(self.stage1_output()), None, None, [])
        self.assertEqual(code, 0)
        self.assertEqual([c for c in calls if c[0] == "workers"], [])
        self.assertIn("멈춤: workers가 돌고 있다", out.getvalue())
        self.assertNotIn("cascade.cli workers", out.getvalue())
        self.assertNotIn("Workflow 실행:", out.getvalue())
        self.assertIn("cascade.cli go --run-dir", out.getvalue())

    def test_workers_command_refuses_a_second_concurrent_run(self):
        self.start()
        with cli_steps.workers_lock(self.run_dir):
            p = self.cli("workers", "--run-dir", str(self.run_dir), "--round", "1", "--stage", "stage1", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("workers가 이미 돈다", p.stdout)

    def test_start_stops_for_scale_question_over_limit_without_severity(self):
        with mock.patch.object(cli, "START_JOBS_LIMIT", 2), \
                mock.patch.dict(os.environ, {**self.env, "CASCADE_REVIEWER": "claude", "CASCADE_WORKERS": "claude"}):
            cwd = os.getcwd()
            os.chdir(self.repo)
            self.addCleanup(os.chdir, cwd)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["start", "--source", "codex-code", "--input", INPUT])
        self.assertEqual(code, 0)
        self.assertIn("멈춤: 1단계 작업이 4개로 2개를 넘는다", out.getvalue())
        run_dir = Path(next(x for x in out.getvalue().splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertFalse((run_dir / "round-1" / "stage1-args.json").exists())
        self.assertIn(" go ", out.getvalue())


class GoStopTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.run_dir = Path(td.name)
        self.args_path = self.run_dir / "stage1-args.json"

    def stop(self, actions):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli._go_stop(self.run_dir, "Workflow 결과가 필요하다", actions)
        self.assertEqual(code, 0)
        return out.getvalue()

    def workflow(self):
        return cli_steps._workflow_action(self.args_path, self.run_dir / "stage1.json")

    def test_args_are_compact_json_on_the_line_after_the_workflow(self):
        self.args_path.write_text('{\n  "jobs": [{"label": "한글", "promptPath": "줄\\n바꿈.md"}]\n}', encoding="utf-8")
        action = self.workflow()
        text = self.stop([action])
        lines = text.splitlines()
        self.assertEqual(lines[lines.index(f"  {action.text}") + 1],
                         '    args: {"jobs":[{"label":"한글","promptPath":"줄\\n바꿈.md"}]}')
        self.assertEqual(text.count("Workflow 실행:"), 1)

    def test_args_over_8000_characters_are_omitted(self):
        for size, shown in ((7992, True), (7993, False)):
            with self.subTest(size=size):
                self.args_path.write_text(json.dumps({"x": "가" * size}, ensure_ascii=False), encoding="utf-8")
                text = self.stop([self.workflow()])
                self.assertEqual("    args: " in text, shown)

    def test_missing_or_invalid_args_are_omitted(self):
        self.assertNotIn("    args: ", self.stop([self.workflow()]))
        self.args_path.write_text("{", encoding="utf-8")
        self.assertNotIn("    args: ", self.stop([self.workflow()]))

    def test_script_is_copied_once_after_the_next_list_and_updated_if_different(self):
        action = self.workflow()
        text = self.stop([action, cli_steps.Action("note", "안내")])
        script = self.run_dir / "run-jobs.js"
        self.assertEqual(script.read_bytes(), cli_steps.RUN_JOBS.read_bytes())
        self.assertEqual(text.splitlines()[-1], f"    scriptPath: {script.resolve()}")
        self.assertGreater(text.index("    scriptPath: "), text.index("  안내"))
        os.utime(script, ns=(1_000_000_000, 1_000_000_000))
        self.stop([action])
        self.assertEqual(script.stat().st_mtime_ns, 1_000_000_000)
        script.write_text("옛 스크립트", encoding="utf-8")
        self.stop([action])
        self.assertEqual(script.read_bytes(), cli_steps.RUN_JOBS.read_bytes())

    def test_finished_workflow_has_no_args_or_script_path(self):
        self.args_path.write_text('{"jobs":[]}', encoding="utf-8")
        action = self.workflow()
        action.out_path.write_text("{}", encoding="utf-8")
        text = self.stop([action])
        self.assertNotIn("    args: ", text)
        self.assertNotIn("scriptPath:", text)
        self.assertFalse((self.run_dir / "run-jobs.js").exists())

    def test_script_copy_failure_omits_the_script_path_line(self):
        self.args_path.write_text('{"jobs":[]}', encoding="utf-8")
        with mock.patch.object(Path, "write_bytes", side_effect=PermissionError):
            text = self.stop([self.workflow()])
        self.assertIn("    args: ", text)
        self.assertNotIn("scriptPath:", text)

    def test_no_visible_workflow_has_no_args_or_script_path(self):
        self.args_path.write_text('{"jobs":[]}', encoding="utf-8")
        for actions in ([], [cli_steps.Action("note", "안내")],
                        [cli_steps.Action("workflow", "숨김", args_path=self.args_path, hidden=True)]):
            with self.subTest(actions=actions):
                text = self.stop(actions)
                self.assertNotIn("    args: ", text)
                self.assertNotIn("scriptPath:", text)
                self.assertFalse((self.run_dir / "run-jobs.js").exists())


class RunStepTest(unittest.TestCase):
    def test_model_warning_pattern_matches_only_model_id_warnings(self):
        text = "경고: r1-01:b 모델 ID가 x 이다(기대값 y)\n경고: 형식을 읽지 못한 항목 1건\n경고: 모델 ID가 비어 있다"
        self.assertEqual(len(cli._model_warnings(text)), 2)
        self.assertEqual(cli._model_warnings("참고: 모델 ID"), [])

    def test_trailing_next_block_is_dropped_and_code_is_kept(self):
        def fake_main(argv):
            print("판정 끝")
            print("다음:")
            print("  CF apply")
            return 3

        with mock.patch.object(cli, "main", fake_main):
            code, text = cli._run_step(["judge"])
        self.assertEqual((code, text), (3, "판정 끝"))

    def test_next_block_in_the_middle_is_kept(self):
        def fake_main(argv):
            print("다음:")
            print("  안내")
            print("끝")
            return 0

        with mock.patch.object(cli, "main", fake_main):
            _, text = cli._run_step(["x"])
        self.assertEqual(text, "다음:\n  안내\n끝")


if __name__ == "__main__":
    unittest.main()
