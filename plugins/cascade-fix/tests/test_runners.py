import itertools
import json
import os
import random
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import runners  # noqa: E402
from cascade.runners import (  # noqa: E402
    STATUS_AUTH,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_LIMIT,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    CliJob,
    CodexRunner,
    CursorRunner,
    _variant_rules,
    cache_env,
    classify_failure,
    cursor_home,
    extract_json_block,
    restore_tree,
    run_all,
    tree_of,
)
from fake_cli import CODEX_BODY, CURSOR_BODY, fenced, install, result_json  # noqa: E402
from helpers import cursor_read_denied, git, make_repo  # noqa: E402

AUTH_BEFORE = '{"tokens": {"refresh_token": "old"}, "last_refresh": "2026-10-10T00:00:00Z"}'
AUTH_AFTER = '{"tokens": {"refresh_token": "new"}, "last_refresh": "2026-10-18T00:00:00Z"}'


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.codex = str(install(bin_dir, "codex", CODEX_BODY))
        self.cursor = str(install(bin_dir, "cursor-agent", CURSOR_BODY))
        self.codex_dir = self.tmp / "codex-home"
        self.codex_dir.mkdir()
        (self.codex_dir / "auth.json").write_text(AUTH_BEFORE, encoding="utf-8")
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text("지적을 재현하라\n", encoding="utf-8")
        self.log = self.tmp / "log"
        patcher = mock.patch.dict(os.environ, {
            "FAKE_CLI_LOG": str(self.log),
            "FAKE_CODEX_RESULT": result_json(),
            "FAKE_CURSOR_RESULTS": json.dumps([fenced(result_json())]),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, **overrides):
        fields = dict(id="F1:a", role="a", kind="worker", runner="codex", model="gpt-test", effort="low",
                      prompt_path=self.prompt, worktree=self.wt, writable=True)
        fields.update(overrides)
        return CliJob(**fields)

    def cursor_job(self, **overrides):
        return self.job(runner="cursor", model="composer-2.5", **overrides)

    def log_args(self, name, n=1):
        return json.loads((self.log / f"{name}-{n}.args.json").read_text(encoding="utf-8"))

    def log_text(self, name, event, n=1):
        return (self.log / f"{name}-{n}.{event}").read_text(encoding="utf-8")

    def log_cursor_config(self, n=1):
        return json.loads(self.log_text("cursor-agent", "config.json", n))

    def calls(self, name):
        return len(list(self.log.glob(f"{name}-*.args.json")))


class CodexRunnerTest(RunnerTestBase):
    def run_codex(self, job=None, **kwargs):
        return CodexRunner(self.codex, codex_dir=self.codex_dir, **kwargs).run(job or self.job())

    def test_worker_command_is_sandboxed_with_schema(self):
        out = self.run_codex()
        args = self.log_args("codex")
        self.assertEqual(out.status, STATUS_OK)
        for part in ("exec", "-s", "workspace-write", "--ephemeral", "--json", "-C", str(self.wt), "--output-schema"):
            self.assertIn(part, args)
        self.assertEqual(args[args.index("-m") + 1], "gpt-test")
        self.assertIn('model_reasoning_effort="low"', args)
        self.assertEqual(args[-1], "-")
        for bad in ("--dangerously-bypass-approvals-and-sandbox", "danger-full-access"):
            self.assertNotIn(bad, args)
        self.assertEqual(out.output["model_id"], "codex:gpt-test")
        self.assertEqual(out.usage["output_tokens"], 7)
        self.assertEqual(out.usage["input_tokens"], 11)
        self.assertIn("지적을 재현하라", self.log_text("codex", "stdin"))

    def test_empty_model_omits_flag_and_reports_default_model_id(self):
        out = self.run_codex(self.job(model=""))
        self.assertEqual(out.status, STATUS_OK)
        self.assertNotIn("-m", self.log_args("codex"))
        self.assertEqual(out.output["model_id"], "codex:default")

    def test_readonly_role_uses_read_only_sandbox(self):
        out = self.run_codex(self.job(role="checker", kind="checker", writable=False))
        args = self.log_args("codex")
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        self.assertNotIn("workspace-write", args)
        self.assertEqual(out.status, STATUS_INVALID)

    def test_checker_result_is_validated_against_checker_schema(self):
        checker = json.dumps({"verdict": "사실", "needs_design_decision": False, "reason": "맞다",
                              "checker_ok": True, "model_id": "x"})
        with mock.patch.dict(os.environ, {"FAKE_CODEX_RESULT": checker}):
            out = self.run_codex(self.job(id="F1:checker", role="checker", kind="checker", writable=False))
        self.assertEqual(out.status, STATUS_OK)
        self.assertIs(out.output["checker_ok"], True)

    def test_home_copies_auth_and_disables_skills(self):
        self.run_codex()
        env = json.loads(self.log_text("codex", "env.json"))
        self.assertNotEqual(env["CODEX_HOME"], str(self.codex_dir))
        self.assertEqual(self.log_text("codex", "auth-is-link"), "False")
        config = self.log_text("codex", "config.toml")
        self.assertIn("include_instructions = false", config)
        self.assertIn("include_permissions_instructions = false", config)
        self.assertIn('model = "gpt-test"', config)
        self.assertIn('model_reasoning_effort = "low"', config)
        self.assertIn(f'[projects."{self.wt}"]', config)
        self.assertIn('trust_level = "trusted"', config)

    def test_missing_auth_is_auth_status(self):
        (self.codex_dir / "auth.json").unlink()
        out = self.run_codex()
        self.assertEqual(out.status, STATUS_AUTH)
        self.assertIn("codex login", out.detail)
        self.assertEqual(self.calls("codex"), 0)

    def test_refreshed_token_is_written_back(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_REFRESH": AUTH_AFTER}):
            out = self.run_codex()
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual((self.codex_dir / "auth.json").read_text(encoding="utf-8"), AUTH_AFTER)
        self.assertEqual(list(self.codex_dir.glob(".auth.json.*")), [])

    def test_unchanged_token_leaves_original_alone(self):
        before = (self.codex_dir / "auth.json").stat().st_mtime_ns
        self.run_codex()
        self.assertEqual((self.codex_dir / "auth.json").stat().st_mtime_ns, before)

    def test_auth_not_written_back_on_conflict(self):
        env = {"FAKE_CODEX_REFRESH": AUTH_AFTER, "FAKE_CODEX_TOUCH_ORIGINAL": '{"tokens": {"refresh_token": "other"}}',
               "FAKE_CODEX_ORIGINAL_AUTH": str(self.codex_dir / "auth.json")}
        with mock.patch.dict(os.environ, env):
            out = self.run_codex()
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual((self.codex_dir / "auth.json").read_text(encoding="utf-8"), env["FAKE_CODEX_TOUCH_ORIGINAL"])
        self.assertIn("auth.json 충돌", out.detail)

    def test_nonzero_exit_with_limit_text_is_limit(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_EXIT": "1", "FAKE_CODEX_STDERR": "You've hit your usage limit"}):
            out = self.run_codex()
        self.assertEqual(out.status, STATUS_LIMIT)
        self.assertIsNone(out.output)

    def test_agent_message_text_does_not_decide_failure_class(self):
        for message in ("cascade/api.py:401 에서 확인했다", "429 응답의 rate limit 재시도가 빠졌다", "Authentication required 문구를 찾았다"):
            events = [{"type": "item.completed", "item": {"type": "agent_message", "text": message}},
                      {"type": "turn.failed", "error": {"message": "stream disconnected before completion"}}]
            with self.subTest(message=message), mock.patch.dict(
                    os.environ, {"FAKE_CODEX_EXIT": "1", "FAKE_CODEX_EVENTS": json.dumps(events)}):
                self.assertEqual(self.run_codex().status, STATUS_ERROR)

    def test_error_events_decide_failure_class(self):
        cases = {STATUS_LIMIT: {"type": "error", "message": "You've hit your usage limit"},
                 STATUS_AUTH: {"type": "turn.failed", "error": {"message": "401 Unauthorized"}}}
        for status, event in cases.items():
            with self.subTest(status=status), mock.patch.dict(
                    os.environ, {"FAKE_CODEX_EXIT": "1", "FAKE_CODEX_EVENTS": json.dumps([event])}):
                self.assertEqual(self.run_codex().status, status)

    def test_login_path_in_stderr_is_not_auth_failure(self):
        stderr = "Error: failed to read /repo/app/login/views.py: permission denied"
        with mock.patch.dict(os.environ, {"FAKE_CODEX_EXIT": "1", "FAKE_CODEX_STDERR": stderr}):
            self.assertEqual(self.run_codex().status, STATUS_ERROR)
        self.assertEqual(classify_failure(stderr), STATUS_ERROR)
        self.assertEqual(classify_failure("Please run /login to continue"), STATUS_AUTH)

    def test_truncated_auth_copy_is_not_written_back(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_REFRESH": '{"tokens": {"refre'}):
            out = self.run_codex()
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual((self.codex_dir / "auth.json").read_text(encoding="utf-8"), AUTH_BEFORE)
        self.assertIn("JSON", out.detail)

    def test_unexpected_exception_becomes_error_outcome(self):
        out = self.run_codex(self.job(prompt_path=self.tmp / "missing.md"))
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("FileNotFoundError", out.detail)
        with mock.patch.object(CodexRunner, "_prepare_home", side_effect=OSError("auth.json 사라짐")):
            out = self.run_codex()
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("auth.json 사라짐", out.detail)

    def test_missing_result_file_is_error(self):
        env = dict(os.environ)
        env.pop("FAKE_CODEX_RESULT")
        with mock.patch.dict(os.environ, env, clear=True):
            out = self.run_codex()
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("결과 파일", out.detail)

    def test_invalid_result_is_invalid(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_RESULT": '{"verdict": "사실"}'}):
            out = self.run_codex()
        self.assertEqual(out.status, STATUS_INVALID)
        self.assertIsNone(out.output)
        self.assertEqual(self.calls("codex"), 1)

    def test_timeout_kills_process(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_SLEEP": "5"}):
            started = time.monotonic()
            out = self.run_codex(timeout=1)
        self.assertEqual(out.status, STATUS_TIMEOUT)
        self.assertLess(time.monotonic() - started, 3)

    def test_codex_ask_returns_text(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_RESULT": "OK\n"}):
            out = CodexRunner(self.codex, codex_dir=self.codex_dir).ask(self.wt, "시험 문장", "gpt-test", "low")
        args = self.log_args("codex")
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual(out.output, {"text": "OK"})
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        self.assertNotIn("--output-schema", args)
        self.assertEqual(args[args.index("-m") + 1], "gpt-test")
        self.assertIn("시험 문장", self.log_text("codex", "stdin"))
        env = json.loads(self.log_text("codex", "env.json"))
        self.assertNotEqual(env["CODEX_HOME"], str(self.codex_dir))

    def test_codex_ask_failures_are_classified(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_EXIT": "1", "FAKE_CODEX_STDERR": "usage limit reached"}):
            out = CodexRunner(self.codex, codex_dir=self.codex_dir).ask(self.wt, "시험 문장", "", "low")
        self.assertEqual(out.status, STATUS_LIMIT)
        (self.codex_dir / "auth.json").unlink()
        out = CodexRunner(self.codex, codex_dir=self.codex_dir).ask(self.wt, "시험 문장", "", "low")
        self.assertEqual(out.status, STATUS_AUTH)

    def test_cache_env_is_passed_outside_worktree(self):
        self.run_codex()
        env = json.loads(self.log_text("codex", "env.json"))
        for key in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache"):
            self.assertFalse(Path(env[key]).is_relative_to(self.wt))

    def test_temp_dirs_are_removed(self):
        self.run_codex()
        env = json.loads(self.log_text("codex", "env.json"))
        self.assertFalse(Path(env["CODEX_HOME"]).exists())
        self.assertFalse(Path(env["UV_CACHE_DIR"]).exists())

    def test_warmup_needed_follows_last_refresh(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        runner = CodexRunner(self.codex, codex_dir=self.codex_dir)

        def refreshed(at):
            (self.codex_dir / "auth.json").write_text(json.dumps({"last_refresh": at.isoformat().replace("+00:00", "Z")}))

        refreshed(now - timedelta(days=1))
        self.assertFalse(runner.warmup_needed(now))
        refreshed(now - timedelta(days=8))
        self.assertTrue(runner.warmup_needed(now))
        (self.codex_dir / "auth.json").write_text("{}")
        self.assertTrue(runner.warmup_needed(now))
        (self.codex_dir / "auth.json").unlink()
        self.assertTrue(runner.warmup_needed(now))


class CursorRunnerTest(RunnerTestBase):
    def run_cursor(self, job=None, **kwargs):
        return CursorRunner(self.cursor, **kwargs).run(job or self.cursor_job())

    def test_worker_uses_sandbox_and_isolated_home(self):
        out = self.run_cursor(self.cursor_job(writable=True))
        args = self.log_args("cursor-agent")
        self.assertEqual(out.status, STATUS_OK)
        for part in ("-p", "--trust", "--sandbox", "enabled", "--output-format", "json", "--model", "composer-2.5",
                     "--workspace", str(self.wt)):
            self.assertIn(part, args)
        self.assertEqual(args[args.index("--sandbox") + 1], "enabled")
        for bad in ("--force", "--yolo", "--approve-mcps", "--mode"):
            self.assertNotIn(bad, args)
        deny = self.log_cursor_config()["permissions"]["deny"]
        self.assertIn("Mcp(*:*)", deny)
        self.assertIn("WebFetch(*)", deny)
        self.assertNotIn("Shell(*)", deny)
        self.assertNotIn("Write(**)", deny)
        self.assertNotIn("approvalMode", self.log_cursor_config())
        stdin = self.log_text("cursor-agent", "stdin")
        self.assertIn("지적을 재현하라", stdin)
        self.assertIn("```json", stdin)
        self.assertIn('"verdict"', stdin)
        self.assertEqual(out.output["model_id"], "cursor:composer-2.5")
        self.assertEqual(out.usage["output_tokens"], 20)
        self.assertEqual(out.usage["input_tokens"], 100)
        env = json.loads(self.log_text("cursor-agent", "env.json"))
        self.assertNotEqual(env["HOME"], str(Path.home()))
        self.assertFalse(Path(env["HOME"]).exists())

    def test_readonly_role_goes_to_acp_without_shell_or_write(self):
        runner = CursorRunner(self.cursor)
        self.addCleanup(runner.close)
        out = runner.run(self.cursor_job(writable=False))
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual(self.calls("cursor-agent"), 0)
        deny = json.loads(self.log_text("cursor-acp", "config.json"))["permissions"]["deny"]
        self.assertIn("Write(**)", deny)
        self.assertIn("Shell(*)", deny)

    def test_invalid_json_is_retried_once_with_resume(self):
        responses = ["JSON을 못 냈다", fenced(result_json())]
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps(responses)}):
            out = self.run_cursor()
        self.assertEqual(out.status, STATUS_OK)
        self.assertEqual(self.calls("cursor-agent"), 2)
        self.assertNotIn("--resume", self.log_args("cursor-agent", 1))
        second = self.log_args("cursor-agent", 2)
        self.assertEqual(second[second.index("--resume") + 1], "s")
        self.assertIn("JSON 객체 하나", self.log_text("cursor-agent", "stdin", 2))
        self.assertEqual(out.usage["output_tokens"], 40)

    def test_still_invalid_after_retry_is_invalid(self):
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps(["없음", fenced('{"verdict": "사실"}')])}):
            out = self.run_cursor()
        self.assertEqual(out.status, STATUS_INVALID)
        self.assertIsNone(out.output)
        self.assertEqual(self.calls("cursor-agent"), 2)

    def test_non_json_stdout_is_error(self):
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([{"stdout": "segfault", "exit": 1}])}):
            out = self.run_cursor()
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIsNone(out.output)
        self.assertIn("segfault", out.detail)
        self.assertEqual(self.calls("cursor-agent"), 1)

    def test_empty_stdout_with_zero_exit_is_error(self):
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([{"stdout": ""}])}):
            self.assertEqual(self.run_cursor().status, STATUS_ERROR)

    def test_usage_limit_text_is_limit(self):
        entry = {"is_error": True, "result": "You've hit your usage limit", "exit": 1}
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([entry])}):
            out = self.run_cursor()
        self.assertEqual(out.status, STATUS_LIMIT)
        self.assertEqual(self.calls("cursor-agent"), 1)

    def test_agent_text_in_result_does_not_decide_failure_class(self):
        entry = {"result": "cascade/api.py:401 의 rate limit 재시도가 빠졌다", "exit": 1}
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([entry])}):
            self.assertEqual(self.run_cursor().status, STATUS_ERROR)
        entry = {"result": "401 Unauthorized 응답을 처리하지 않는다", "stderr": "crashed", "exit": 1}
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([entry])}):
            self.assertEqual(self.run_cursor().status, STATUS_ERROR)

    def test_stderr_decides_failure_class_with_envelope(self):
        entry = {"result": "", "stderr": "You've hit your usage limit", "exit": 1}
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([entry])}):
            self.assertEqual(self.run_cursor().status, STATUS_LIMIT)

    def test_unexpected_exception_becomes_error_outcome(self):
        out = self.run_cursor(self.cursor_job(prompt_path=self.tmp / "missing.md"))
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("FileNotFoundError", out.detail)

    def test_auth_failure_is_auth(self):
        entry = {"is_error": True, "result": "Authentication required", "exit": 1}
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([entry])}):
            self.assertEqual(self.run_cursor().status, STATUS_AUTH)

    def test_error_envelope_with_zero_exit_is_not_ok(self):
        entry = {"is_error": True, "result": "boom"}
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_RESULTS": json.dumps([entry])}):
            out = self.run_cursor()
        self.assertEqual(out.status, STATUS_ERROR)
        self.assertIn("boom", out.detail)

    def test_timeout_kills_process(self):
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_SLEEP": "5"}):
            started = time.monotonic()
            out = self.run_cursor(timeout=1)
        self.assertEqual(out.status, STATUS_TIMEOUT)
        self.assertLess(time.monotonic() - started, 3)

    def test_missing_executable_is_error(self):
        out = CursorRunner(str(self.tmp / "nope")).run(self.cursor_job())
        self.assertEqual(out.status, STATUS_ERROR)

    def test_cache_env_is_passed_outside_worktree(self):
        self.run_cursor()
        env = json.loads(self.log_text("cursor-agent", "env.json"))
        for key in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache"):
            self.assertFalse(Path(env[key]).is_relative_to(self.wt))

    def test_home_passes_real_tool_roots(self):
        real_home = self.tmp / "real-home"
        (real_home / ".pyenv").mkdir(parents=True)
        (real_home / ".local" / "share" / "uv" / "python").mkdir(parents=True)
        clean = {k: v for k, v in os.environ.items() if k not in ("PYENV_ROOT", "UV_PYTHON_INSTALL_DIR")}
        with mock.patch.dict(os.environ, {**clean, "HOME": str(real_home)}, clear=True):
            self.run_cursor()
        env = json.loads(self.log_text("cursor-agent", "env.json"))
        self.assertEqual(env["PYENV_ROOT"], str(real_home / ".pyenv"))
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], str(real_home / ".local" / "share" / "uv" / "python"))
        self.assertNotEqual(env["HOME"], str(real_home))

    def test_home_skips_missing_tool_roots(self):
        real_home = self.tmp / "bare-home"
        real_home.mkdir()
        clean = {k: v for k, v in os.environ.items() if k not in ("PYENV_ROOT", "UV_PYTHON_INSTALL_DIR")}
        with mock.patch.dict(os.environ, {**clean, "HOME": str(real_home)}, clear=True):
            self.run_cursor()
        env = json.loads(self.log_text("cursor-agent", "env.json"))
        self.assertIsNone(env["PYENV_ROOT"])
        self.assertIsNone(env["UV_PYTHON_INSTALL_DIR"])


class CursorHomeTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.base = Path(self._td.name)
        self.real_home = self.base / "real-home"
        self.real_home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.real_home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, profile):
        home = cursor_home(self.base, profile)
        return home, json.loads((home / ".cursor" / "cli-config.json").read_text(encoding="utf-8"))

    def test_review_profile_matches_review_script(self):
        home, config = self.config("review")
        self.assertTrue((home / "Library" / "Keychains").is_symlink())
        self.assertEqual(os.readlink(home / "Library" / "Keychains"), str(self.real_home / "Library" / "Keychains"))
        deny = config["permissions"]["deny"]
        for rule in ("Shell(*)", "Write(**)", "WebFetch(*)", "Mcp(*:*)"):
            self.assertIn(rule, deny)
        for name in (".netrc", ".npmrc", ".gitconfig"):
            self.assertIn(f"Read({self.real_home}/{name})", deny)
            self.assertNotIn(f"Read({self.real_home}/{name}/**)", deny)
        for name in (".ssh", ".codex", ".cursor", ".claude", ".aws", ".config", ".gnupg", ".docker", ".kube", "Library/Keychains"):
            self.assertIn(f"Read({self.real_home}/{name}/**)", deny)
            self.assertNotIn(f"Read({self.real_home}/{name})", deny)
        self.assertEqual(config["approvalMode"], "allowlist")
        self.assertIs(config["autoAcceptWebSearch"], False)
        self.assertEqual(config["version"], 1)
        self.assertEqual(config["permissions"]["allow"], [])

    def test_serena_dashboard_is_disabled(self):
        for profile in ("review", "worker", "readonly"):
            home, _ = self.config(profile)
            text = (home / ".serena" / "serena_config.yml").read_text(encoding="utf-8")
            self.assertIn("web_dashboard: false", text)
            self.assertIn("web_dashboard_open_on_launch: false", text)
            self.assertIn("projects: []", text)

    def test_worker_profile_allows_shell_and_write(self):
        _, config = self.config("worker")
        deny = config["permissions"]["deny"]
        self.assertNotIn("Shell(*)", deny)
        self.assertNotIn("Write(**)", deny)
        self.assertIn("Mcp(*:*)", deny)
        self.assertIn(f"Read({self.real_home}/.ssh/**)", deny)
        self.assertNotIn("approvalMode", config)

    def test_readonly_profile_adds_write_deny(self):
        _, worker = self.config("worker")
        _, readonly = self.config("readonly")
        self.assertEqual(set(readonly["permissions"]["deny"]) - set(worker["permissions"]["deny"]), {"Write(**)"})
        self.assertNotIn("Shell(*)", readonly["permissions"]["deny"])

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            cursor_home(self.base, "nope")

    def deny_of(self, read_root):
        home = cursor_home(self.base, "review", read_root=read_root)
        return json.loads((home / ".cursor" / "cli-config.json").read_text(encoding="utf-8"))["permissions"]["deny"]

    def test_read_root_denies_siblings_along_its_ancestors_in_any_case(self):
        top = self.base.resolve() / "tree"
        wt = top / "repo" / "run" / "wt"
        (wt / "inner").mkdir(parents=True)
        (top / "repo" / "src").mkdir()
        (top / "repo" / "notes.txt").write_text("x")
        (top / "repo" / "run" / "wt-other").mkdir()
        (top / "repo" / "run" / "w").mkdir()
        (top / "Elsewhere").mkdir()
        deny = self.deny_of(wt)
        for path in (top / "repo" / "src" / "a.py", top / "repo" / "SRC" / "a.py", top / "repo" / "notes.txt",
                     top / "repo" / "Notes.TXT", top / "repo" / "run" / "wt-other" / "x", top / "repo" / "run" / "w",
                     top / "repo" / "run" / "w" / "x", top / "elsewhere" / "x", top / "Elsewhere",
                     top / "Repo" / "run" / "wt" / "README.md", top / "repo" / "run" / "WT" / "README.md"):
            self.assertTrue(cursor_read_denied(deny, path), path)
        for path in (wt, wt / "inner" / "a.md", wt / "README.md", top / "repo" / "run", top / "repo", top):
            self.assertFalse(cursor_read_denied(deny, path), path)
        outside_root = next(p for p in Path("/").iterdir() if p.name != top.parts[1])
        self.assertTrue(cursor_read_denied(deny, outside_root / "x"))
        self.assertTrue(cursor_read_denied(deny, Path("/" + top.parts[1].upper()) / "x"))

    def test_read_root_through_symlink_keeps_both_forms(self):
        real = self.base.resolve() / "real"
        (real / "wt").mkdir(parents=True)
        (real / "secret").mkdir()
        link = self.base.resolve() / "link"
        link.symlink_to(real)
        deny = self.deny_of(link / "wt")
        for path in (real / "secret" / "a", link / "secret" / "a"):
            self.assertTrue(cursor_read_denied(deny, path), path)
        for path in (real / "wt" / "a", link / "wt" / "a"):
            self.assertFalse(cursor_read_denied(deny, path), path)

    def test_case_rules_do_not_block_other_kept_form(self):
        base = self.base.resolve()
        real = base / "real"
        (real / "wt").mkdir(parents=True)
        (base / "other").mkdir()
        link = base / "RealLink"
        link.symlink_to(real)
        deny = self.deny_of(link / "wt")
        for path in (link / "wt" / "README.md", real / "wt" / "README.md"):
            self.assertFalse(cursor_read_denied(deny, path), path)
        for path in (base / "other" / "x", base / "REAL" / "x", base / "Real" / "x", base / "realLINK" / "x",
                     base / "reALLink" / "x"):
            self.assertTrue(cursor_read_denied(deny, path), path)

    def test_unlistable_ancestor_fails_instead_of_leaving_gap(self):
        parent = self.base.resolve() / "xonly"
        (parent / "wt").mkdir(parents=True)
        os.chmod(parent, 0o311)
        self.addCleanup(os.chmod, parent, 0o755)
        with self.assertRaises(OSError):
            cursor_home(self.base, "review", read_root=parent / "wt")

    def test_shared_prefix_with_two_kept_forms_is_denied(self):
        base = self.base.resolve()
        (base / "really" / "wt").mkdir(parents=True)
        (base / "real").mkdir()
        (base / "RealLink").symlink_to(base / "really")
        deny = self.deny_of(base / "RealLink" / "wt")
        self.assertTrue(cursor_read_denied(deny, base / "real" / "secret.txt"))
        self.assertFalse(cursor_read_denied(deny, base / "really" / "wt" / "a"))
        self.assertFalse(cursor_read_denied(deny, base / "RealLink" / "wt" / "a"))

    def test_variant_rules_deny_every_case_variant_but_kept_names(self):
        rng = random.Random(7)

        def word():
            return "".join(rng.choice("aAbB1") for _ in range(rng.randint(1, 4)))

        def variants(w):
            return {"".join(p) for p in itertools.product(*[{c.lower(), c.upper()} for c in w])}

        for _ in range(400):
            names: list[str] = []
            while len(names) < rng.randint(2, 7):
                w = word()
                if all(w.lower() != n.lower() for n in names):
                    names.append(w)
            kept = set(names[:rng.randint(1, 2)])
            deny = set()
            for w in names:
                deny |= _variant_rules("/p/", w, kept)
            for w in names:
                for v in variants(w):
                    for path in (f"/p/{v}", f"/p/{v}/x"):
                        self.assertEqual(cursor_read_denied(deny, path), v not in kept, (names, kept, path))

    def test_variant_rules_deny_titlecase_letter_sibling(self):
        deny = _variant_rules("/p/", "ǅx", {"p"})
        for name in ("ǅx", "ǆx", "Ǆx"):
            self.assertTrue(cursor_read_denied(deny, f"/p/{name}"), name)

    def test_star_in_kept_or_sibling_name_fails(self):
        for kept, sibling in (("repo*", "secret"), ("repo", "odd*name")):
            with self.subTest(kept=kept, sibling=sibling):
                top = self.base.resolve() / f"star-{len(kept)}"
                (top / kept / "wt").mkdir(parents=True)
                (top / kept / sibling).mkdir()
                with self.assertRaises(OSError):
                    cursor_home(self.base, "review", read_root=top / kept / "wt")

    def test_rules_stay_few_in_crowded_folders(self):
        wt = self.base.resolve() / "crowd" / "wt"
        wt.mkdir(parents=True)
        for i in range(500):
            (wt.parent / f"tmp{i:04d}").write_text("x")
        deny = self.deny_of(wt)
        self.assertTrue(cursor_read_denied(deny, wt.parent / "tmp0042"))
        self.assertLess(sum(1 for rule in deny if rule.startswith(f"Read({wt.parent}/")), 20)

    def test_without_read_root_only_sensitive_paths_are_denied(self):
        _, config = self.config("review")
        reads = [rule for rule in config["permissions"]["deny"] if rule.startswith("Read(")]
        self.assertTrue(all(rule.startswith(f"Read({self.real_home}/") for rule in reads))


class HelpersTest(unittest.TestCase):
    def test_execute_grace_sends_term_before_kill(self):
        with tempfile.TemporaryDirectory() as td:
            done = Path(td) / "cleaned"
            script = f"trap 'echo cleaned > \"{done}\"; exit 0' TERM\nsleep 30 &\nwait\n"
            run = runners._execute(["sh", "-c", script], "", Path(td), dict(os.environ), 1, grace=5)
            self.assertTrue(run.timed_out)
            self.assertEqual(done.read_text(encoding="utf-8").strip(), "cleaned")

    def test_classify_failure(self):
        self.assertEqual(classify_failure("You've hit your usage limit"), STATUS_LIMIT)
        self.assertEqual(classify_failure("429 rate limit exceeded"), STATUS_LIMIT)
        for text in ("Authentication required", "HTTP 401 Unauthorized", "refresh token was already used", "Not logged in"):
            self.assertEqual(classify_failure(text), STATUS_AUTH, text)
        self.assertEqual(classify_failure("segfault"), STATUS_ERROR)
        self.assertEqual(classify_failure("exit 4010 lines"), STATUS_ERROR)
        self.assertEqual(classify_failure(""), STATUS_ERROR)

    def test_extract_json_block_prefers_last_fenced_block(self):
        text = '앞\n```json\n{"a": 1}\n```\n중간 {"z": 0}\n```json\n{"a": 2, "n": {"b": 3}}\n```\n끝'
        self.assertEqual(extract_json_block(text), {"a": 2, "n": {"b": 3}})

    def test_extract_json_block_skips_unparsable_last_block(self):
        self.assertEqual(extract_json_block('```json\n{"a": 1}\n```\n```json\n{깨짐\n```'), {"a": 1})

    def test_extract_json_block_falls_back_to_last_object(self):
        self.assertEqual(extract_json_block('먼저 {"a": 1} 그리고 {"b": {"c": 2}} 끝'), {"b": {"c": 2}})
        self.assertIsNone(extract_json_block("JSON 없음"))
        self.assertIsNone(extract_json_block('```json\n[1, 2]\n```'))

    def test_cache_env_points_outside_worktree(self):
        with tempfile.TemporaryDirectory() as wt:
            env = cache_env(Path(tempfile.mkdtemp(prefix="cascade-cache-")))
            for key in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache"):
                self.assertFalse(Path(env[key]).is_relative_to(wt))

    def test_codex_model_reads_config_toml(self):
        with tempfile.TemporaryDirectory() as td:
            codex_dir = Path(td)
            self.assertEqual(runners.codex_model(codex_dir), "")
            (codex_dir / "config.toml").write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
            self.assertEqual(runners.codex_model(codex_dir), "")
            (codex_dir / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
            self.assertEqual(runners.codex_model(codex_dir), "gpt-test")
            (codex_dir / "config.toml").write_text("깨진 = = toml", encoding="utf-8")
            self.assertEqual(runners.codex_model(codex_dir), "")


class AvailabilityTest(RunnerTestBase):
    def setUp(self):
        super().setUp()
        for key in ("CASCADE_CODEX", "CASCADE_CURSOR_AGENT"):
            patcher = mock.patch.dict(os.environ)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(key, None)

    def test_env_overrides_executable_lookup(self):
        os.environ["CASCADE_CURSOR_AGENT"] = self.cursor
        self.assertEqual(runners.find_cursor_agent(), self.cursor)
        os.environ["CASCADE_CURSOR_AGENT"] = str(self.tmp / "nope")
        self.assertIsNone(runners.find_cursor_agent())
        os.environ["CASCADE_CODEX"] = self.codex
        self.assertEqual(runners.find_codex(), self.codex)
        os.environ["CASCADE_CODEX"] = str(self.tmp / "nope")
        self.assertIsNone(runners.find_codex())

    def test_relative_executable_paths_are_made_absolute(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        os.environ["CASCADE_CODEX"] = os.path.relpath(self.codex, self.tmp)
        os.environ["CASCADE_CURSOR_AGENT"] = os.path.relpath(self.cursor, self.tmp)
        self.assertEqual(runners.find_codex(), self.codex)
        self.assertEqual(runners.find_cursor_agent(), self.cursor)
        del os.environ["CASCADE_CODEX"], os.environ["CASCADE_CURSOR_AGENT"]
        with mock.patch.dict(os.environ, {"PATH": "bin", "HOME": str(self.tmp / "no-home")}):
            self.assertEqual(runners.find_codex(), self.codex)
            self.assertEqual(runners.find_cursor_agent(), self.cursor)

    def test_path_lookup_is_used_without_env(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "bin"), "HOME": str(self.tmp / "no-home")}):
            self.assertEqual(runners.find_cursor_agent(), self.cursor)
            self.assertEqual(runners.find_codex(), self.codex)

    def test_codex_available_needs_executable_and_auth(self):
        os.environ["CASCADE_CODEX"] = self.codex
        with mock.patch.object(Path, "home", return_value=self.tmp):
            self.assertFalse(runners.codex_available())
            (self.tmp / ".codex").mkdir()
            (self.tmp / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
            self.assertTrue(runners.codex_available())
        os.environ["CASCADE_CODEX"] = str(self.tmp / "nope")
        with mock.patch.object(Path, "home", return_value=self.tmp):
            self.assertFalse(runners.codex_available())

    def test_cursor_available_checks_login_in_isolated_home(self):
        os.environ["CASCADE_CURSOR_AGENT"] = self.cursor
        self.assertTrue(runners.cursor_available())
        self.assertEqual(self.calls("cursor-status"), 1)
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_STATUS": "Not logged in"}):
            self.assertFalse(runners.cursor_available())

    def test_cursor_available_without_executable_does_not_run_anything(self):
        os.environ["CASCADE_CURSOR_AGENT"] = str(self.tmp / "nope")
        self.assertFalse(runners.cursor_available())
        self.assertEqual(self.calls("cursor-status"), 0)


LIMIT_ENTRY = {"is_error": True, "result": "You've hit your usage limit", "exit": 1}
CHECKER_RESULT = json.dumps({"verdict": "사실", "needs_design_decision": False, "reason": "맞다",
                             "checker_ok": True, "model_id": "x"})


class RunAllTest(RunnerTestBase):
    def setUp(self):
        super().setUp()
        self.wt = make_repo(self.tmp / "repo", {"a.txt": "orig\n"})
        self.runners = {"codex": CodexRunner(self.codex, codex_dir=self.codex_dir), "cursor": CursorRunner(self.cursor)}

    def env(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cursor_jobs(self, count, **overrides):
        return [self.cursor_job(id=f"F{i}:a", **overrides) for i in range(count)]

    def started(self, name, n):
        return float(self.log_text(name, "start", n))

    def ended(self, name, n):
        return float(self.log_text(name, "end", n))

    def test_limit_stops_runner_and_skips_rest(self):
        self.env(FAKE_CURSOR_RESULTS=json.dumps([fenced(result_json()), LIMIT_ENTRY]))
        outcomes = run_all(self.cursor_jobs(4), self.runners, max_parallel=1)
        self.assertEqual([o.status for o in outcomes], [STATUS_OK, STATUS_LIMIT, STATUS_SKIPPED, STATUS_SKIPPED])
        self.assertEqual(self.calls("cursor-agent"), 2)
        self.assertIn("limit", outcomes[2].detail)
        self.assertIn("F1:a", outcomes[2].detail)
        self.assertIsNone(outcomes[2].output)

    def test_auth_failure_skips_rest(self):
        entry = {"is_error": True, "result": "Authentication required. Run cursor-agent login", "exit": 1}
        self.env(FAKE_CURSOR_RESULTS=json.dumps([entry]))
        outcomes = run_all(self.cursor_jobs(3), self.runners, max_parallel=1)
        self.assertEqual([o.status for o in outcomes], [STATUS_AUTH, STATUS_SKIPPED, STATUS_SKIPPED])
        self.assertIn("auth", outcomes[1].detail)
        self.assertEqual(self.calls("cursor-agent"), 1)

    def test_exhausted_runner_from_earlier_call_is_skipped_and_new_one_is_recorded(self):
        self.env(FAKE_CURSOR_RESULTS=json.dumps([LIMIT_ENTRY]))
        exhausted = {"codex": {"status": STATUS_LIMIT, "reason": "F0:a에서 limit: 앞선 호출"}}
        jobs = [self.job(id="C1:a"), self.cursor_job(id="K1:a")]
        outcomes = run_all(jobs, self.runners, max_parallel=1, exhausted=exhausted)
        self.assertEqual([o.status for o in outcomes], [STATUS_SKIPPED, STATUS_LIMIT])
        self.assertIn("F0:a에서 limit", outcomes[0].detail)
        self.assertEqual(self.calls("codex"), 0)
        self.assertEqual(set(exhausted), {"codex", "cursor"})
        self.assertEqual(exhausted["cursor"]["status"], STATUS_LIMIT)
        self.assertIn("K1:a에서 limit", exhausted["cursor"]["reason"])

    def test_running_jobs_finish_after_limit(self):
        self.env(FAKE_CURSOR_RESULTS=json.dumps([LIMIT_ENTRY, fenced(result_json())]), FAKE_CURSOR_SLEEP="0.5")
        outcomes = run_all(self.cursor_jobs(2), self.runners, max_parallel=2)
        self.assertEqual(sorted(o.status for o in outcomes), [STATUS_LIMIT, STATUS_OK])
        self.assertEqual(self.calls("cursor-agent"), 2)

    def test_other_runner_continues_after_limit(self):
        self.env(FAKE_CURSOR_RESULTS=json.dumps([LIMIT_ENTRY]))
        jobs = [self.cursor_job(id="K1:a"), self.job(id="C1:a"), self.cursor_job(id="K2:a"), self.job(id="C2:a")]
        outcomes = run_all(jobs, self.runners, max_parallel=1)
        self.assertEqual([o.status for o in outcomes], [STATUS_LIMIT, STATUS_OK, STATUS_SKIPPED, STATUS_OK])
        self.assertEqual(self.calls("cursor-agent"), 1)
        self.assertEqual(self.calls("codex"), 2)

    def test_readonly_job_changes_are_reverted(self):
        (self.wt / "a.txt").write_text("작성자 수정\n", encoding="utf-8")
        self.env(FAKE_CODEX_RESULT=CHECKER_RESULT, FAKE_MUTATE="b.txt=새 파일;a.txt=checker가 고침")
        job = self.job(id="F1:checker", role="checker", kind="checker", writable=False)
        [outcome] = run_all([job], self.runners)
        self.assertEqual((outcome.status, outcome.output), (STATUS_INVALID, None))
        self.assertIn("읽기 역할이 파일을 바꿔 되돌리고 결과를 버림", outcome.detail)
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"), "작성자 수정\n")
        self.assertFalse((self.wt / "b.txt").exists())

    def test_readonly_job_without_changes_keeps_detail_empty(self):
        job = self.job(id="F1:checker", role="checker", kind="checker", writable=False)
        self.env(FAKE_CODEX_RESULT=CHECKER_RESULT)
        [outcome] = run_all([job], self.runners)
        self.assertEqual((outcome.status, outcome.detail), (STATUS_OK, ""))

    def test_failed_writer_job_restores_pre_run_tree(self):
        (self.wt / "a.txt").write_text("실행 전 수정\n", encoding="utf-8")
        self.env(FAKE_MUTATE="a.txt=망가짐;new/c.txt=찌꺼기", FAKE_CODEX_EXIT="1")
        before = tree_of(self.wt)
        [outcome] = run_all([self.job()], self.runners)
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"), "실행 전 수정\n")
        self.assertFalse((self.wt / "new").exists())
        self.assertEqual(tree_of(self.wt), before)

    def head(self):
        return git(self.wt, "rev-parse", "HEAD")

    def test_failed_writer_job_that_committed_restores_head(self):
        before_head, before_tree = self.head(), tree_of(self.wt)
        self.env(FAKE_MUTATE="a.txt=커밋됨;new.txt=새 파일", FAKE_COMMIT="1", FAKE_CODEX_EXIT="1")
        [outcome] = run_all([self.job()], self.runners)
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertEqual(self.head(), before_head)
        self.assertEqual(tree_of(self.wt), before_tree)
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"), "orig\n")
        self.assertFalse((self.wt / "new.txt").exists())
        self.assertEqual(git(self.wt, "show", "HEAD:a.txt"), "orig")

    def test_failed_writer_job_restores_head_of_detached_worktree(self):
        git(self.wt, "checkout", "-q", "--detach")
        before_head = self.head()
        self.env(FAKE_MUTATE="a.txt=커밋됨", FAKE_COMMIT="1", FAKE_CODEX_EXIT="1")
        [outcome] = run_all([self.job()], self.runners)
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertEqual(self.head(), before_head)
        self.assertEqual(git(self.wt, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"), "orig\n")

    def test_successful_writer_job_that_committed_keeps_commit(self):
        before_head = self.head()
        self.env(FAKE_MUTATE="a.txt=커밋됨", FAKE_COMMIT="1")
        [outcome] = run_all([self.job()], self.runners)
        self.assertEqual(outcome.status, STATUS_OK)
        self.assertNotEqual(self.head(), before_head)
        self.assertEqual(git(self.wt, "show", "HEAD:a.txt"), "커밋됨")

    def test_readonly_job_that_committed_is_reverted_to_original_head(self):
        before_head, before_tree = self.head(), tree_of(self.wt)
        self.env(FAKE_CODEX_RESULT=CHECKER_RESULT, FAKE_MUTATE="a.txt=checker가 고침", FAKE_COMMIT="1")
        job = self.job(id="F1:checker", role="checker", kind="checker", writable=False)
        [outcome] = run_all([job], self.runners)
        self.assertEqual((outcome.status, outcome.output), (STATUS_INVALID, None))
        self.assertIn("읽기 역할이 파일을 바꿔 되돌리고 결과를 버림", outcome.detail)
        self.assertEqual(self.head(), before_head)
        self.assertEqual(tree_of(self.wt), before_tree)

    def test_readonly_job_with_empty_commit_is_reverted_to_original_head(self):
        before_head = self.head()
        self.env(FAKE_CODEX_RESULT=CHECKER_RESULT, FAKE_COMMIT="1")
        job = self.job(id="F1:checker", role="checker", kind="checker", writable=False)
        [outcome] = run_all([job], self.runners)
        self.assertEqual((outcome.status, outcome.output), (STATUS_INVALID, None))
        self.assertIn("읽기 역할이 파일을 바꿔 되돌리고 결과를 버림", outcome.detail)
        self.assertEqual(self.head(), before_head)

    def test_successful_writer_job_keeps_changes(self):
        self.env(FAKE_MUTATE="a.txt=고침;b.txt=새 파일")
        [outcome] = run_all([self.job()], self.runners)
        self.assertEqual(outcome.status, STATUS_OK)
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"), "고침")
        self.assertTrue((self.wt / "b.txt").exists())

    def test_restore_keeps_setup_symlink(self):
        target = self.tmp / "deps"
        target.mkdir()
        (self.wt / "node_modules").symlink_to(target)
        self.env(FAKE_MUTATE="a.txt=망가짐", FAKE_CODEX_EXIT="1")
        [outcome] = run_all([self.job()], self.runners)
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertTrue((self.wt / "node_modules").is_symlink())
        self.assertEqual((self.wt / "node_modules").resolve(), target)
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"), "orig\n")

    def test_codex_warmup_runs_first_job_alone(self):
        (self.codex_dir / "auth.json").write_text('{"last_refresh": "2026-10-10T00:00:00Z"}', encoding="utf-8")
        self.env(FAKE_CODEX_SLEEP="0.4")
        jobs = [self.job(id=f"F{i}:a") for i in range(3)]
        outcomes = run_all(jobs, self.runners, max_parallel=3, now=datetime(2026, 10, 18, tzinfo=timezone.utc))
        self.assertEqual([o.status for o in outcomes], [STATUS_OK] * 3)
        first_end = min(self.ended("codex", n) for n in (1, 2, 3))
        second_start = sorted(self.started("codex", n) for n in (1, 2, 3))[1]
        self.assertGreaterEqual(second_start, first_end)

    def test_fresh_codex_token_runs_jobs_concurrently(self):
        (self.codex_dir / "auth.json").write_text('{"last_refresh": "2026-10-17T00:00:00Z"}', encoding="utf-8")
        self.env(FAKE_CODEX_SLEEP="0.8")
        jobs = [self.job(id=f"F{i}:a") for i in range(2)]
        run_all(jobs, self.runners, max_parallel=2, now=datetime(2026, 10, 18, tzinfo=timezone.utc))
        self.assertLess(max(self.started("codex", n) for n in (1, 2)), min(self.ended("codex", n) for n in (1, 2)))

    def test_max_parallel_limits_concurrency(self):
        self.env(FAKE_CURSOR_SLEEP="0.5")
        run_all(self.cursor_jobs(3), self.runners, max_parallel=1)
        starts = sorted(self.started("cursor-agent", n) for n in (1, 2, 3))
        ends = sorted(self.ended("cursor-agent", n) for n in (1, 2, 3))
        self.assertGreaterEqual(starts[1], ends[0])
        self.assertGreaterEqual(starts[2], ends[1])

    def test_results_keep_input_order(self):
        self.env(FAKE_CURSOR_SLEEP="0.2")
        jobs = [self.cursor_job(id="K1:a"), self.job(id="C1:a"), self.cursor_job(id="K2:a"), self.job(id="C2:a"),
                self.cursor_job(id="K3:a")]
        outcomes = run_all(jobs, self.runners, max_parallel=2)
        self.assertEqual([o.id for o in outcomes], ["K1:a", "C1:a", "K2:a", "C2:a", "K3:a"])

    def test_runner_exception_becomes_error_and_others_continue(self):
        class Boom:
            def run(self, job):
                raise RuntimeError("터짐")

        jobs = [self.job(id="C1:a"), self.cursor_job(id="K1:a")]
        outcomes = run_all(jobs, {"codex": Boom(), "cursor": self.runners["cursor"]})
        self.assertEqual([o.status for o in outcomes], [STATUS_ERROR, STATUS_OK])
        self.assertIn("터짐", outcomes[0].detail)

    def test_unknown_runner_is_error(self):
        [outcome] = run_all([self.job()], {"cursor": self.runners["cursor"]})
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertEqual(self.calls("codex"), 0)

    def test_untrackable_worktree_is_error_without_running(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        [outcome] = run_all([self.job(worktree=plain)], self.runners)
        self.assertEqual(outcome.status, STATUS_ERROR)
        self.assertEqual(self.calls("codex"), 0)

    def test_empty_jobs(self):
        self.assertEqual(run_all([], self.runners), [])


class TreeTest(RunnerTestBase):
    def setUp(self):
        super().setUp()
        self.wt = make_repo(self.tmp / "repo", {"a.txt": "orig\n", "d/b.txt": "b\n"})

    def test_tree_of_ignores_hooks_and_matches_worktree_content(self):
        hook = self.wt / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        self.assertEqual(tree_of(self.wt), git(self.wt, "rev-parse", "HEAD^{tree}"))
        (self.wt / "c.txt").write_text("c\n", encoding="utf-8")
        self.assertNotEqual(tree_of(self.wt), git(self.wt, "rev-parse", "HEAD^{tree}"))

    def test_tree_of_is_stable_and_ignores_inherited_git_env(self):
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.tmp / "other-index"), "GIT_DIR": str(self.tmp / "nope")}):
            first = tree_of(self.wt)
        self.assertEqual(first, tree_of(self.wt))

    def test_restore_tree_resets_modified_new_and_deleted(self):
        (self.wt / "a.txt").write_text("수정\n", encoding="utf-8")
        before = tree_of(self.wt)
        (self.wt / "a.txt").write_text("또 수정\n", encoding="utf-8")
        (self.wt / "d" / "b.txt").unlink()
        (self.wt / "new" / "deep").mkdir(parents=True)
        (self.wt / "new" / "deep" / "x.txt").write_text("x\n", encoding="utf-8")
        restore_tree(self.wt, before)
        self.assertEqual((self.wt / "a.txt").read_text(encoding="utf-8"), "수정\n")
        self.assertEqual((self.wt / "d" / "b.txt").read_text(encoding="utf-8"), "b\n")
        self.assertFalse((self.wt / "new" / "deep" / "x.txt").exists())
        self.assertEqual(tree_of(self.wt), before)

    def test_restore_tree_removes_file_hidden_by_changed_gitignore_and_keeps_ignored(self):
        (self.wt / ".gitignore").write_text("cache/\n", encoding="utf-8")
        (self.wt / "cache").mkdir()
        (self.wt / "cache" / "keep.txt").write_text("keep\n", encoding="utf-8")
        before = tree_of(self.wt)
        (self.wt / ".gitignore").write_text("cache/\npartial.py\n", encoding="utf-8")
        (self.wt / "partial.py").write_text("실패한 작업의 찌꺼기\n", encoding="utf-8")
        restore_tree(self.wt, before)
        self.assertFalse((self.wt / "partial.py").exists())
        self.assertEqual((self.wt / ".gitignore").read_text(encoding="utf-8"), "cache/\n")
        self.assertEqual((self.wt / "cache" / "keep.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(tree_of(self.wt), before)

    def test_restore_tree_removes_nested_repo_created_after_snapshot(self):
        make_repo(self.wt / "kept", {"k.txt": "k\n"})
        before = tree_of(self.wt)
        make_repo(self.wt / "nested", {"n.txt": "n\n"})
        restore_tree(self.wt, before)
        self.assertFalse((self.wt / "nested").exists())
        self.assertEqual((self.wt / "kept" / "k.txt").read_text(encoding="utf-8"), "k\n")
        self.assertNotIn("nested", git(self.wt, "status", "--porcelain"))


if __name__ == "__main__":
    unittest.main()
