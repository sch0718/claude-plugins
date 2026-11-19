import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade.runners import (  # noqa: E402
    READONLY_CHANGED,
    STATUS_AUTH,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_LIMIT,
    STATUS_OK,
    STATUS_TIMEOUT,
    CursorRunner,
    Outcome,
    run_all,
)
from cascade.workers import usage_totals  # noqa: E402
from fake_cli import fenced, result_json  # noqa: E402
from helpers import cursor_read_denied, make_repo  # noqa: E402
from test_runners import RunnerTestBase  # noqa: E402

LIMIT_ENTRY = {"is_error": True, "result": "You've hit your usage limit"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class CursorAcpTest(RunnerTestBase):
    def runner(self, **kwargs):
        runner = CursorRunner(self.cursor, **kwargs)
        self.addCleanup(runner.close)
        return runner

    def readonly_job(self, **overrides):
        return self.job(**{"runner": "cursor", "model": "composer-2.5", "role": "b", "writable": False, **overrides})

    def env(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def results(self, *entries):
        self.env(FAKE_CURSOR_RESULTS=json.dumps(list(entries), ensure_ascii=False))

    def prompt_record(self, m=1):
        return json.loads((self.log / f"acp-prompt-{m}.json").read_text(encoding="utf-8"))

    def server_file(self, name, n=1):
        return (self.log / f"cursor-acp-{n}.{name}").read_text(encoding="utf-8")

    def test_readonly_job_uses_acp_server_with_review_profile(self):
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_OK, out.detail)
        self.assertEqual(self.calls("cursor-agent"), 0)
        self.assertEqual(self.calls("cursor-acp"), 1)
        self.assertEqual(self.log_args("cursor-acp"), ["acp"])
        config = json.loads(self.server_file("config.json"))
        for rule in ("Shell(*)", "Write(**)", "WebFetch(*)", "Mcp(*:*)"):
            self.assertIn(rule, config["permissions"]["deny"])
        self.assertEqual(config["approvalMode"], "allowlist")
        initialize = json.loads(self.server_file("initialize.json"))
        caps = initialize["clientCapabilities"]
        self.assertEqual(caps["fs"], {"readTextFile": False, "writeTextFile": False})
        self.assertFalse(caps["terminal"])
        self.assertTrue(caps["_meta"]["parameterizedModelPicker"])
        record = self.prompt_record()
        self.assertEqual(record["cwd"], str(self.wt))
        self.assertEqual(record["mcpServers"], [])
        self.assertEqual(record["values"], {"model": "composer-2.5", "fast": "false", "mode": "ask"})
        self.assertIn("지적을 재현하라", record["prompt"])
        self.assertIn("```json", record["prompt"])
        self.assertEqual(out.output["model_id"], "cursor:composer-2.5")
        self.assertEqual(out.usage, {"unrecorded": 1})
        env = json.loads(self.server_file("env.json"))
        self.assertNotEqual(env["HOME"], str(Path.home()))
        self.assertFalse(Path(env["UV_CACHE_DIR"]).is_relative_to(self.wt))

    def test_writable_job_still_uses_print_mode(self):
        out = self.runner().run(self.cursor_job(writable=True))
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual(self.calls("cursor-agent"), 1)
        self.assertEqual(self.calls("cursor-acp"), 0)
        self.assertIn("-p", self.log_args("cursor-agent"))

    def test_server_is_reused_with_new_session_per_job_in_same_worktree(self):
        runner = self.runner()
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        self.assertEqual(self.calls("cursor-acp"), 1)
        first, second = self.prompt_record(1), self.prompt_record(2)
        self.assertEqual(first["pid"], second["pid"])
        self.assertNotEqual(first["sessionId"], second["sessionId"])

    def test_profile_denies_reads_outside_worktree(self):
        (self.tmp / "outside.txt").write_text("x")
        self.assertEqual(self.runner().run(self.readonly_job()).status, STATUS_OK)
        deny = json.loads(self.server_file("config.json"))["permissions"]["deny"]
        self.assertTrue(cursor_read_denied(deny, self.tmp / "outside.txt"))
        self.assertTrue(cursor_read_denied(deny, self.tmp / "bin" / "cursor-agent"))
        self.assertFalse(cursor_read_denied(deny, self.wt / "a.txt"))

    def test_other_worktree_gets_its_own_server(self):
        runner = self.runner()
        other = self.tmp / "other-wt"
        other.mkdir()
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        self.assertEqual(runner.run(self.readonly_job(worktree=other)).status, STATUS_OK)
        self.assertEqual(self.calls("cursor-acp"), 2)
        self.assertNotEqual(self.prompt_record(1)["pid"], self.prompt_record(2)["pid"])
        self.assertEqual(self.prompt_record(2)["cwd"], str(other))
        deny = json.loads(self.server_file("config.json", 2))["permissions"]["deny"]
        self.assertTrue(cursor_read_denied(deny, self.wt / "a.txt"))
        self.assertFalse(cursor_read_denied(deny, other / "a.txt"))

    def test_full_pool_replaces_idle_server_of_other_worktree(self):
        runner = self.runner(max_servers=1)
        other = self.tmp / "other-wt"
        other.mkdir()
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        old_home = Path(json.loads(self.server_file("env.json"))["HOME"])
        self.assertEqual(runner.run(self.readonly_job(worktree=other)).status, STATUS_OK)
        self.assertEqual(self.calls("cursor-acp"), 2)
        self.assertFalse(old_home.exists())
        self.assertFalse(_alive(int(self.server_file("pid"))))

    def test_server_is_replaced_after_session_limit_and_old_home_removed(self):
        runner = self.runner(sessions_per_server=1)
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        old_home = Path(json.loads(self.server_file("env.json"))["HOME"])
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        self.assertEqual(self.calls("cursor-acp"), 2)
        self.assertFalse(old_home.exists())
        self.assertFalse(_alive(int(self.server_file("pid"))))

    def test_concurrent_jobs_are_bounded_by_pool_size(self):
        self.wt = make_repo(self.tmp / "repo", {"a.txt": "orig\n"})
        self.env(FAKE_CURSOR_SLEEP="0.3")
        jobs = [self.readonly_job(id=f"F{i}:b") for i in range(4)]
        outcomes = run_all(jobs, {"cursor": self.runner(max_servers=2)}, max_parallel=4)
        self.assertEqual([o.status for o in outcomes], [STATUS_OK] * 4)
        self.assertEqual(self.calls("cursor-acp"), 2)

    def test_invalid_answer_is_retried_in_same_session(self):
        self.results("JSON을 못 냈다", fenced(result_json()))
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_OK)
        first, second = self.prompt_record(1), self.prompt_record(2)
        self.assertEqual(first["sessionId"], second["sessionId"])
        self.assertIn("JSON 객체 하나", second["prompt"])
        self.assertEqual(self.calls("cursor-acp"), 1)

    def test_still_invalid_after_retry_is_invalid(self):
        self.results("없음", fenced('{"verdict": "사실"}'))
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_INVALID)
        self.assertIsNone(out.output)
        self.assertEqual(out.usage, {"unrecorded": 1})

    def test_tool_preamble_is_dropped_and_permission_is_rejected(self):
        self.env(FAKE_ACP_TOOL="1", FAKE_ACP_CLIENT_REQ="1")
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_OK, out.detail)
        record = self.prompt_record()
        self.assertEqual(record["permission_response"]["result"],
                         {"outcome": {"outcome": "selected", "optionId": "reject-once"}})
        self.assertIn("error", record["client_response"])

    def test_permission_without_reject_option_is_cancelled(self):
        self.env(FAKE_ACP_TOOL="1", FAKE_ACP_PERMISSION_OPTIONS=json.dumps([{"optionId": "ok", "kind": "allow_once"}]))
        self.runner().run(self.readonly_job())
        self.assertEqual(self.prompt_record()["permission_response"]["result"], {"outcome": {"outcome": "cancelled"}})

    def test_limit_error_is_limit_and_next_job_gets_new_server(self):
        self.results(LIMIT_ENTRY, fenced(result_json()))
        runner = self.runner()
        out = runner.run(self.readonly_job())
        self.assertEqual(out.status, STATUS_LIMIT)
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        self.assertEqual(self.calls("cursor-acp"), 2)

    def test_auth_error_is_auth(self):
        self.results({"is_error": True, "result": "Authentication required"})
        self.assertEqual(self.runner().run(self.readonly_job()).status, STATUS_AUTH)

    def test_stderr_decides_failure_class(self):
        self.results({"is_error": True, "result": "boom", "stderr": "You've hit your usage limit\n"})
        self.assertEqual(self.runner().run(self.readonly_job()).status, STATUS_LIMIT)

    def test_answer_text_does_not_decide_failure_class(self):
        self.results("cascade/api.py:401 의 rate limit 재시도가 빠졌다", "Unauthorized 401 처리가 없다")
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_INVALID)

    def test_prompt_is_removed_from_detail(self):
        self.results({"is_error": True, "result": "boom", "echo_prompt": True})
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("boom", out.detail)
        self.assertNotIn("지적을 재현하라", out.detail)

    def test_server_exit_is_error_and_next_job_recovers(self):
        self.results({"stdout": "segfault"}, fenced(result_json()))
        runner = self.runner()
        out = runner.run(self.readonly_job())
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("segfault", out.detail)
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        self.assertEqual(self.calls("cursor-acp"), 2)

    def test_timeout_kills_server(self):
        self.env(FAKE_CURSOR_SLEEP="5")
        started = time.monotonic()
        out = self.runner(timeout=1).run(self.readonly_job())
        self.assertEqual(out.status, STATUS_TIMEOUT)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(_alive(int(self.server_file("pid"))))

    def test_unexpected_stop_reason_is_error(self):
        self.results({"result": fenced(result_json()), "stop": "max_tokens"})
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("max_tokens", out.detail)

    def test_config_mismatch_never_sends_prompt(self):
        self.env(FAKE_ACP_IGNORE_CONFIG="fast")
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("fast", out.detail)
        self.assertEqual(self.calls("acp-prompt"), 0)

    def test_unknown_model_never_sends_prompt(self):
        out = self.runner().run(self.readonly_job(model="composer-9"))
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("composer-9", out.detail)
        self.assertEqual(self.calls("acp-prompt"), 0)

    def test_auto_model_is_rejected_without_starting_server(self):
        out = self.runner().run(self.readonly_job(model="auto-smart"))
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("Auto", out.detail)
        self.assertEqual(self.calls("cursor-acp"), 0)

    def test_initialize_error_is_classified(self):
        self.env(FAKE_ACP_INIT_ERROR="Authentication required")
        out = self.runner().run(self.readonly_job())
        self.assertEqual(out.status, STATUS_AUTH)
        self.assertEqual(self.calls("acp-prompt"), 0)

    def test_unlistable_worktree_ancestor_is_error_without_starting_server(self):
        parent = self.tmp / "xonly"
        wt = parent / "wt"
        wt.mkdir(parents=True)
        os.chmod(parent, 0o311)
        self.addCleanup(os.chmod, parent, 0o755)
        out = self.runner().run(self.readonly_job(worktree=wt))
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("프로필", out.detail)
        self.assertEqual(self.calls("cursor-acp"), 0)

    def test_missing_executable_is_error(self):
        runner = CursorRunner(str(self.tmp / "nope"))
        self.addCleanup(runner.close)
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_ERROR)

    def test_close_stops_process_group_and_removes_home(self):
        self.env(FAKE_ACP_LINGER="1")
        runner = self.runner()
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_OK)
        home = Path(json.loads(self.server_file("env.json"))["HOME"])
        linger = int((home / "linger.pid").read_text())
        runner.close()
        self.assertFalse(_alive(int(self.server_file("pid"))))
        self.assertFalse(_alive(linger))
        self.assertFalse(home.parent.exists())

    def test_closed_runner_reports_error(self):
        runner = self.runner()
        runner.close()
        self.assertEqual(runner.run(self.readonly_job()).status, STATUS_ERROR)

    def test_readonly_change_is_restored_and_discarded(self):
        self.wt = make_repo(self.tmp / "repo", {"a.txt": "orig\n"})
        self.env(FAKE_MUTATE="a.txt=changed")
        [out] = run_all([self.readonly_job()], {"cursor": self.runner()})
        self.assertEqual(out.status, STATUS_INVALID)
        self.assertIn(READONLY_CHANGED, out.detail)
        self.assertEqual((self.wt / "a.txt").read_text(), "orig\n")


class UsageTotalsTest(unittest.TestCase):
    def test_unrecorded_jobs_are_counted_per_runner(self):
        outcomes = [
            Outcome("a", "cursor", STATUS_OK, None, {"unrecorded": 1}, ""),
            Outcome("b", "cursor", STATUS_OK, None, {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 5}, ""),
            Outcome("c", "codex", STATUS_OK, None, {"input_tokens": 1}, ""),
        ]
        totals = usage_totals(outcomes)
        self.assertEqual(totals["cursor"], {"input": 100, "output": 20, "cache_read": 5, "unrecorded": 1})
        self.assertEqual(totals["codex"], {"input": 1, "output": 0, "cache_read": 0})


if __name__ == "__main__":
    unittest.main()
