import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade.runners import (  # noqa: E402
    STATUS_AUTH,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_LIMIT,
    STATUS_OK,
    STATUS_TIMEOUT,
    UNRECORDED,
    ClaudeRunner,
    CliJob,
    find_claude,
    _check_output,
)
from fake_cli import CLAUDE_BODY, install, result_json  # noqa: E402

MODEL = "claude-sonnet-5-5"


class ClaudeRunnerTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.claude = str(install(bin_dir, "claude", CLAUDE_BODY))
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text("지적을 재현하라\n", encoding="utf-8")
        self.log = self.tmp / "log"
        patcher = mock.patch.dict(os.environ, {"FAKE_CLI_LOG": str(self.log), "FAKE_CLAUDE_RESULT": result_json(),
                                               "CLAUDECODE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, **overrides):
        fields = dict(id="F1:a", role="a", kind="worker", runner="claude", model=MODEL, effort="low",
                      prompt_path=self.prompt, worktree=self.wt, writable=True)
        fields.update(overrides)
        return CliJob(**fields)

    def run_job(self, job=None, **kwargs):
        return ClaudeRunner(self.claude, **kwargs).run(job or self.job())

    def args(self):
        return json.loads((self.log / "claude-1.args.json").read_text(encoding="utf-8"))

    def option(self, name):
        args = self.args()
        return args[args.index(name) + 1]

    def test_success_uses_plain_model_id_and_records_envelope_usage(self):
        out = self.run_job()
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual(out.output["model_id"], MODEL)
        self.assertEqual(out.usage, {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 5,
                                     "cache_write_tokens": 1, "cost_usd": 0.25})
        self.assertEqual((self.log / "claude-1.stdin").read_text(encoding="utf-8"), "지적을 재현하라\n")
        self.assertEqual((self.log / "claude-1.cwd").read_text(encoding="utf-8"), str(self.wt))

    def test_command_pins_model_effort_schema_and_drops_claudecode(self):
        self.run_job()
        args = self.args()
        self.assertEqual(args[:1], ["-p"])
        self.assertEqual((self.option("--model"), self.option("--effort")), (MODEL, "low"))
        self.assertIn("verdict", json.loads(self.option("--json-schema"))["properties"])
        for part in ("--strict-mcp-config", "--setting-sources"):
            self.assertIn(part, args)
        self.assertIsNone(json.loads((self.log / "claude-1.env.json").read_text(encoding="utf-8"))["CLAUDECODE"])

    def test_writable_job_opens_edit_tools_and_read_only_job_denies_them(self):
        self.run_job()
        self.assertEqual(self.option("--permission-mode"), "acceptEdits")
        self.assertIn("Edit", self.option("--allowedTools").split(","))
        self.assertNotIn("Edit", self.option("--disallowedTools").split(","))
        for tool in ("Agent", "Task", "WebFetch", "WebSearch"):
            self.assertIn(tool, self.option("--disallowedTools").split(","))
        self.run_job(self.job(writable=False))
        args = json.loads((self.log / "claude-2.args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--permission-mode") + 1], "default")
        self.assertNotIn("Edit", args[args.index("--allowedTools") + 1].split(","))
        self.assertTrue({"Edit", "Write", "NotebookEdit"} <= set(args[args.index("--disallowedTools") + 1].split(",")))

    def test_tools_flag_shrinks_tool_definitions_to_the_role(self):
        self.run_job()
        self.assertEqual(self.option("--tools"), "Bash,Read,Grep,Glob,Edit,Write")
        self.run_job(self.job(writable=False))
        args = json.loads((self.log / "claude-2.args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--tools") + 1], "Bash,Read,Grep,Glob")

    def test_result_text_json_block_is_used_without_structured_output(self):
        del os.environ["FAKE_CLAUDE_RESULT"]
        os.environ["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"result": f"설명\n```json\n{result_json()}\n```\n"})
        self.assertEqual(self.run_job().status, STATUS_OK)

    def test_invalid_output_is_invalid_but_keeps_usage(self):
        os.environ["FAKE_CLAUDE_RESULT"] = json.dumps({"verdict": "엉뚱함"})
        out = self.run_job()
        self.assertEqual(out.status, STATUS_INVALID)
        self.assertEqual(out.usage["cost_usd"], 0.25)

    def test_review_kind_validates_text_and_uses_requested_model(self):
        job = self.job(id="F1:opinion", role="opinion", kind="review", writable=False)
        self.assertEqual(_check_output(job, {"text": "review", "model_id": "reported"}),
                         {"text": "review", "model_id": MODEL})
        for output in ({}, {"text": ""}, {"text": "  "}, {"text": 1}, {"text": "review", "extra": True}):
            with self.subTest(output=output):
                self.assertIsNone(_check_output(job, output))
        os.environ["FAKE_CLAUDE_RESULT"] = json.dumps({"text": "review", "model_id": "reported"})
        out = self.run_job(job)
        self.assertEqual((out.status, out.output), (STATUS_OK, {"text": "review", "model_id": MODEL}))
        self.assertEqual(set(json.loads(self.option("--json-schema"))["properties"]), {"text", "model_id"})

    def test_model_mismatch_is_not_adopted(self):
        os.environ["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"modelUsage": {"claude-haiku-5-5": {}}})
        out = self.run_job()
        self.assertEqual((out.status, out.output), (STATUS_INVALID, None))
        self.assertIn("claude-haiku-5-5", out.detail)

    def test_extra_internal_model_next_to_requested_one_is_accepted(self):
        os.environ["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"modelUsage": {MODEL: {}, "claude-haiku-5-5": {}}})
        self.assertEqual(self.run_job().status, STATUS_OK)

    def test_missing_model_usage_is_accepted(self):
        os.environ["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"modelUsage": None})
        self.assertEqual(self.run_job().status, STATUS_OK)

    def test_limit_and_auth_failures_are_classified(self):
        for text, status in (("Claude AI usage limit reached", STATUS_LIMIT), ("Invalid API key · Please run /login", STATUS_AUTH),
                             ("뭔가 터졌다", STATUS_ERROR)):
            with self.subTest(text=text):
                os.environ["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": text})
                self.assertEqual(self.run_job().status, status)

    def test_errors_array_of_error_envelope_is_classified(self):
        os.environ["FAKE_CLAUDE_ENVELOPE"] = json.dumps(
            {"is_error": True, "subtype": "error_during_execution", "result": None, "errors": ["Rate limit exceeded"]})
        out = self.run_job()
        self.assertEqual(out.status, STATUS_LIMIT)
        self.assertIn("Rate limit", out.detail)

    def test_non_json_stdout_is_error_with_unrecorded_usage(self):
        os.environ["FAKE_CLAUDE_STDOUT"] = "not json"
        os.environ["FAKE_CLAUDE_EXIT"] = "1"
        out = self.run_job()
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertEqual(out.usage, {UNRECORDED: 1})

    def test_not_logged_in_stderr_without_envelope_is_auth(self):
        os.environ.update({"FAKE_CLAUDE_STDOUT": "\n", "FAKE_CLAUDE_STDERR": "Not logged in · Please run /login",
                           "FAKE_CLAUDE_EXIT": "1"})
        self.assertEqual(self.run_job().status, STATUS_AUTH)

    def test_login_path_in_stderr_without_envelope_is_error(self):
        os.environ.update({"FAKE_CLAUDE_STDOUT": "\n", "FAKE_CLAUDE_STDERR": "cannot read /repo/app/login/views.py",
                           "FAKE_CLAUDE_EXIT": "1"})
        self.assertEqual(self.run_job().status, STATUS_ERROR)

    def test_read_only_checker_and_doc_judge_jobs_run_with_read_tools(self):
        checker = json.dumps({"verdict": "사실", "needs_design_decision": False, "reason": "맞다", "checker_ok": True,
                              "model_id": "x"})
        os.environ["FAKE_CLAUDE_RESULT"] = checker
        out = self.run_job(self.job(id="F1:checker", role="checker", kind="checker", writable=False))
        self.assertEqual(out.status, STATUS_OK)
        self.assertIs(out.output["checker_ok"], True)
        os.environ["FAKE_CLAUDE_RESULT"] = result_json()
        out = self.run_job(self.job(id="F1:judge_a", role="judge_a", writable=False))
        self.assertEqual(out.status, STATUS_OK)
        for n in (1, 2):
            args = json.loads((self.log / f"claude-{n}.args.json").read_text(encoding="utf-8"))
            self.assertEqual(args[args.index("--permission-mode") + 1], "default")
            self.assertNotIn("Write", args[args.index("--allowedTools") + 1].split(","))

    def test_timeout_is_reported(self):
        os.environ["FAKE_CLAUDE_SLEEP"] = "5"
        self.assertEqual(self.run_job(timeout=1).status, STATUS_TIMEOUT)

    def test_missing_executable_is_error(self):
        out = ClaudeRunner(str(self.tmp / "nope")).run(self.job())
        self.assertEqual(out.status, STATUS_ERROR)

    def test_find_claude_prefers_env_variable(self):
        with mock.patch.dict(os.environ, {"CASCADE_CLAUDE": self.claude}):
            self.assertEqual(find_claude(), self.claude)
        with mock.patch.dict(os.environ, {"CASCADE_CLAUDE": str(self.tmp / "nope")}):
            self.assertIsNone(find_claude())


if __name__ == "__main__":
    unittest.main()
