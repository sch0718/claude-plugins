import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import sandbox_check, workers  # noqa: E402
from cascade.config import RepoConfig  # noqa: E402
from cascade.runners import tree_of  # noqa: E402
from cascade.snapshot import commit_worktree  # noqa: E402
from fake_cli import CODEX_BODY, CURSOR_BODY, fenced, install  # noqa: E402
from helpers import git, make_repo, run_cli  # noqa: E402
from test_cli_e2e import FIXTURES, CliTestBase  # noqa: E402

BOTH = {"codex": True, "cursor": True}
CLAUDE_HAIKU = {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low"}
CURSOR_FALLBACK = {"runner": "cursor", "model": "composer-2.5", "effort": ""}


def cursor_reply(cmds, exit_code=0, probe_exit=0):
    results = [{"cmd": "true", "exit": probe_exit}, *({"cmd": c, "exit": exit_code} for c in cmds)]
    return fenced(json.dumps({"results": results}))


class SandboxCheckBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.codex = install(bin_dir, "codex", CODEX_BODY)
        self.cursor = install(bin_dir, "cursor-agent", CURSOR_BODY)
        self.root = self.tmp / "repo"
        self.wt = make_repo(self.tmp / "wt", {"calc.py": "x = 1\n"}).resolve()
        self.log = self.tmp / "log"
        self.check = self.tmp / "sandbox-check.json"
        patcher = mock.patch.dict(os.environ, {
            "CASCADE_CODEX": str(self.codex), "CASCADE_CURSOR_AGENT": str(self.cursor),
            "CASCADE_SANDBOX_CHECK": str(self.check), "FAKE_CLI_LOG": str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.workers = workers.assign("external", BOTH, "gpt-x", "composer-2.5")

    def ensure(self, cfg, baseline=(), initial=None):
        return sandbox_check.ensure(self.root, cfg, self.wt, initial or self.workers, list(baseline))

    def calls(self, name):
        return len(list(self.log.glob(f"{name}-*.args.json"))) if self.log.is_dir() else 0

    def codex_commands(self):
        calls = [json.loads(p.read_text(encoding="utf-8"))[-1] for p in sorted(self.log.glob("codex-sandbox-*.args.json"),
                 key=lambda p: int(p.name.split("-")[2].split(".")[0]))]
        return calls[1:]

    def set_cursor(self, *entries):
        os.environ["FAKE_CURSOR_RESULTS"] = json.dumps(list(entries))
        self.addCleanup(os.environ.pop, "FAKE_CURSOR_RESULTS", None)


class CommandsTest(unittest.TestCase):
    def test_commands_lists_templates_in_order_without_duplicates(self):
        cfg = RepoConfig(tests=["pytest {files}"], build=["make build"], typecheck=["mypy ."], lint=["ruff check .", "make build"],
                         full_tests=["pytest"], format=["ruff format {files}"])
        self.assertEqual(sandbox_check.commands(cfg), ["pytest {files}", "make build", "mypy .", "ruff check ."])

    def test_cache_key_depends_on_root_runner_and_commands(self):
        key = sandbox_check.cache_key(Path("/r"), "codex", ["a", "b"])
        self.assertTrue(key.startswith("/r|codex|"))
        self.assertNotEqual(key, sandbox_check.cache_key(Path("/r"), "cursor", ["a", "b"]))
        self.assertNotEqual(key, sandbox_check.cache_key(Path("/r"), "codex", ["a"]))
        self.assertNotEqual(key, sandbox_check.cache_key(Path("/x"), "codex", ["a", "b"]))

    def test_check_file_default_and_override(self):
        with mock.patch.dict(os.environ, {"CASCADE_SANDBOX_CHECK": "/tmp/x/check.json"}):
            self.assertEqual(sandbox_check.check_file(), Path("/tmp/x/check.json"))
        env = {k: v for k, v in os.environ.items() if k != "CASCADE_SANDBOX_CHECK"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(sandbox_check.check_file(), Path.home() / ".claude" / "cascade-fix" / "sandbox-check.json")


class EnsureTest(SandboxCheckBase):
    def test_failing_command_in_codex_sandbox_demotes_codex(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        self.set_cursor({"result": cursor_reply(["true"])})
        updated, lines = self.ensure(RepoConfig(tests=["true"]))
        self.assertEqual((updated["a"]["runner"], updated["writer"]["runner"]), ("claude", "claude"))
        self.assertEqual(updated["b"]["runner"], "cursor")
        self.assertEqual(len(lines), 1)
        self.assertIn("codex 샌드박스에서 검증 명령이 실패해 이 저장소에서는 Claude 작업자를 쓴다: true", lines[0])

    def cell_workers(self, fallback, runner="codex"):
        return {"a": {"runner": runner, "model": "gpt-x" if runner == "codex" else "composer-2.5", "effort": "low",
                      "fallback": fallback},
                "b": {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low", "fallback": CLAUDE_HAIKU}}

    def test_sandbox_failure_uses_cell_fallback(self):
        workers = self.cell_workers(CLAUDE_HAIKU)
        out, notices = sandbox_check.ensure(self.root, RepoConfig(tests=["true"]), self.wt, workers, [],
                                            checks={"codex": lambda wt, cmds: {c: 1 for c in cmds}})
        self.assertEqual((out["a"]["runner"], out["a"]["model"]), ("claude", "claude-haiku-5-5"))
        self.assertEqual(out["b"], workers["b"])
        self.assertIn("codex 샌드박스에서 검증 명령이 실패해 이 저장소에서는 Claude 작업자를 쓴다", notices[0])

    def test_sandbox_failure_moves_to_consented_external_fallback_and_checks_it(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        self.set_cursor({"result": cursor_reply(["true"])})
        workers = self.cell_workers(CURSOR_FALLBACK)
        out, notices = sandbox_check.ensure(self.root, RepoConfig(tests=["true"]), self.wt, workers, [],
                                            consented={"codex", "cursor"})
        self.assertEqual((out["a"]["runner"], out["a"]["model"], out["a"]["effort"]), ("cursor", "composer-2.5", ""))
        self.assertEqual(out["a"]["fallback"]["runner"], "claude")
        self.assertIn("대체 작업자(cursor)", notices[0])
        self.assertEqual(self.calls("cursor-agent"), 1)

    def test_failing_external_fallback_goes_claude_only(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        self.set_cursor({"result": cursor_reply(["true"], 1)})
        out, notices = sandbox_check.ensure(self.root, RepoConfig(tests=["true"]), self.wt,
                                            self.cell_workers(CURSOR_FALLBACK), [], consented={"codex", "cursor"})
        self.assertEqual((out["a"]["runner"], out["a"]["model"]), ("claude", "claude-sonnet-5-5"))
        self.assertEqual(len(notices), 2)
        self.assertIn("cursor 샌드박스에서 검증 명령이 실패해", notices[1])

    def test_fallback_outside_consent_goes_claude_only_without_checking_it(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        out, notices = sandbox_check.ensure(self.root, RepoConfig(tests=["true"]), self.wt,
                                            self.cell_workers(CURSOR_FALLBACK), [], consented={"codex"})
        self.assertEqual((out["a"]["runner"], out["a"]["model"]), ("claude", "claude-sonnet-5-5"))
        self.assertEqual(self.calls("cursor-agent"), 0)

    def test_failed_runner_is_removed_from_other_cells_fallback(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        self.set_cursor({"result": cursor_reply(["true"])})
        workers = self.cell_workers({"runner": "codex", "model": "gpt-x", "effort": "low"}, runner="cursor")
        workers["b"] = {"runner": "codex", "model": "gpt-x", "effort": "low", "fallback": CLAUDE_HAIKU}
        out, _ = sandbox_check.ensure(self.root, RepoConfig(tests=["true"]), self.wt, workers, [],
                                      consented={"codex", "cursor"})
        self.assertEqual(out["b"]["runner"], "claude")
        self.assertEqual((out["a"]["runner"], out["a"]["fallback"]["runner"], out["a"]["fallback"]["model"]),
                         ("cursor", "claude", "claude-sonnet-5-5"))

    def test_tests_command_failing_inside_and_outside_sandbox_keeps_assignment(self):
        self.set_cursor({"result": cursor_reply(["false"], 1)})
        updated, lines = self.ensure(RepoConfig(tests=["false"]))
        self.assertEqual((updated, lines), (self.workers, []))
        data = json.loads(self.check.read_text(encoding="utf-8"))
        self.assertEqual({entry["ok"] for entry in data.values()}, {True})

    def test_outside_rerun_is_shared_between_runners(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        counter = self.tmp / "outside.log"
        cmd = f"echo x >> {counter}"
        self.set_cursor({"result": cursor_reply([cmd], 1)})
        updated, lines = self.ensure(RepoConfig(tests=[cmd]))
        self.assertEqual(len(lines), 2)
        self.assertEqual({spec["runner"] for spec in updated.values()}, {"claude"})
        self.assertEqual(counter.read_text(encoding="utf-8"), "x\n")

    def test_outside_rerun_starts_from_restored_tree(self):
        cmd = "test ! -e lock"
        self.set_cursor({"result": cursor_reply([cmd], 1)})
        os.environ["FAKE_MUTATE"] = "lock=x"
        self.addCleanup(os.environ.pop, "FAKE_MUTATE", None)
        initial = workers.assign("external", {"cursor": True}, "gpt-x", "composer-2.5")
        updated, lines = self.ensure(RepoConfig(tests=[cmd]), initial=initial)
        self.assertEqual({spec["runner"] for spec in updated.values()}, {"claude"})
        self.assertEqual(len(lines), 1)
        self.assertIn("cursor 샌드박스에서 검증 명령이 실패해", lines[0])
        self.assertFalse((self.wt / "lock").exists())

    def test_failing_command_in_cursor_reply_demotes_cursor(self):
        self.set_cursor({"result": cursor_reply(["true"], 1)})
        updated, lines = self.ensure(RepoConfig(tests=["true"]))
        self.assertEqual(updated["a"]["runner"], "codex")
        self.assertEqual((updated["b"]["runner"], updated["checker"]["runner"]), ("claude", "claude"))
        self.assertIn("cursor 샌드박스에서 검증 명령이 실패해", lines[0])

    def test_all_commands_pass_keeps_assignment(self):
        self.set_cursor({"result": cursor_reply(["true", "echo ok"])})
        updated, lines = self.ensure(RepoConfig(tests=["true"], lint=["echo ok"]))
        self.assertEqual((updated, lines), (self.workers, []))

    def test_codex_runs_each_command_in_sandbox_with_isolated_home(self):
        self.set_cursor({"result": cursor_reply(["echo {files}"])})
        self.ensure(RepoConfig(tests=["echo {files}"]))
        args = json.loads((self.log / "codex-sandbox-2.args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[:1], ["sandbox"])
        self.assertEqual(args[args.index("-C") + 1], str(self.wt))
        self.assertEqual(args[args.index("-P") + 1], ":workspace")
        self.assertNotIn("sandbox_mode", " ".join(args))
        self.assertEqual(args[args.index("--") + 1:], ["sh", "-c", "echo"])
        for forbidden in ("--dangerously-bypass-approvals-and-sandbox", "danger-full-access"):
            self.assertNotIn(forbidden, " ".join(args))
        home = json.loads((self.log / "codex-sandbox-2.env.json").read_text(encoding="utf-8"))["CODEX_HOME"]
        self.assertNotEqual(home, str(Path.home() / ".codex"))
        self.assertFalse(Path(home).exists())

    def test_cursor_prompt_lists_commands_and_forbids_edits(self):
        self.set_cursor({"result": cursor_reply(["echo hi"])})
        self.ensure(RepoConfig(tests=["echo hi"]))
        stdin = (self.log / "cursor-agent-1.stdin").read_text(encoding="utf-8")
        self.assertIn("echo hi", stdin)
        self.assertIn('"results"', stdin)
        args = json.loads((self.log / "cursor-agent-1.args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--workspace") + 1], str(self.wt))
        self.assertEqual(args[args.index("--model") + 1], "composer-2.5")
        for forbidden in ("--force", "--yolo", "--approve-mcps"):
            self.assertNotIn(forbidden, args)

    def test_baseline_failures_are_ignored(self):
        self.set_cursor({"result": cursor_reply(["true"])})
        updated, lines = self.ensure(RepoConfig(tests=["true"], lint=["false"]), baseline=["false"])
        self.assertEqual((updated, lines), (self.workers, []))
        self.assertEqual(self.codex_commands(), ["true"])

    def test_only_baseline_failing_commands_means_nothing_to_check(self):
        updated, lines = self.ensure(RepoConfig(lint=["false"]), baseline=["false"])
        self.assertEqual((updated, lines), (self.workers, []))
        self.assertEqual((self.calls("codex-sandbox"), self.calls("cursor-agent")), (0, 0))

    def test_cached_result_skips_rerun(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        self.set_cursor({"result": cursor_reply(["true", "echo hi"])})
        cfg = RepoConfig(tests=["true"], lint=["echo hi"])
        first, first_lines = self.ensure(cfg)
        counts = (self.calls("codex-sandbox"), self.calls("cursor-agent"))
        self.assertEqual(counts, (3, 1))
        second, second_lines = self.ensure(cfg)
        self.assertEqual((self.calls("codex-sandbox"), self.calls("cursor-agent")), counts)
        self.assertEqual((second, second_lines), (first, first_lines))
        self.assertEqual(first["a"]["runner"], "claude")

    def test_result_file_records_outcome_time_and_failed_commands(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        self.set_cursor({"result": cursor_reply(["true", "echo hi"])})
        self.ensure(RepoConfig(tests=["true"], lint=["echo hi"]))
        data = json.loads(self.check.read_text(encoding="utf-8"))
        codex = data[sandbox_check.cache_key(self.root, "codex", ["true", "echo hi"])]
        self.assertEqual((codex["ok"], codex["failed"]), (False, ["true", "echo hi"]))
        self.assertTrue(codex["at"])
        cursor = data[sandbox_check.cache_key(self.root, "cursor", ["true", "echo hi"])]
        self.assertEqual((cursor["ok"], cursor["failed"]), (True, []))

    def test_changed_commands_rerun(self):
        self.set_cursor({"result": cursor_reply(["true", "echo hi"])})
        self.ensure(RepoConfig(tests=["true"]))
        before = self.calls("codex-sandbox")
        self.ensure(RepoConfig(tests=["true"], lint=["echo hi"]))
        self.assertEqual(self.calls("codex-sandbox"), before + 3)

    def test_same_commands_in_other_repo_rerun(self):
        self.set_cursor({"result": cursor_reply(["true"])})
        cfg = RepoConfig(tests=["true"])
        self.ensure(cfg)
        sandbox_check.ensure(self.tmp / "other", cfg, self.wt, self.workers, [])
        self.assertEqual(self.calls("codex-sandbox"), 4)

    def test_no_commands_or_no_external_workers_passes_without_calls(self):
        for cfg, initial in ((RepoConfig(), self.workers), (RepoConfig(tests=["false"]), workers.assign("claude", BOTH, "", ""))):
            updated, lines = self.ensure(cfg, initial=initial)
            self.assertEqual((updated, lines), (initial, []))
        self.assertEqual((self.calls("codex-sandbox"), self.calls("cursor-agent")), (0, 0))
        self.assertFalse(self.check.exists())

    def test_cursor_reply_without_results_demotes_but_is_not_cached(self):
        self.set_cursor({"result": "명령을 못 돌렸다"})
        updated, lines = self.ensure(RepoConfig(tests=["true"]))
        self.assertEqual(updated["b"]["runner"], "claude")
        self.assertIn("cursor 샌드박스 점검 명령 자체가 실패했다", lines[0])
        data = json.loads(self.check.read_text(encoding="utf-8"))
        self.assertEqual([k for k in data if "|cursor|" in k], [])
        self.assertEqual(len([k for k in data if "|codex|" in k]), 1)

    def test_missing_codex_executable_demotes_without_caching(self):
        self.set_cursor({"result": cursor_reply(["true"])})
        with mock.patch.dict(os.environ, {"CASCADE_CODEX": str(self.tmp / "no-such")}):
            updated, lines = self.ensure(RepoConfig(tests=["true"]))
        self.assertEqual(updated["a"]["runner"], "claude")
        self.assertIn("codex 샌드박스에서", lines[0])
        data = json.loads(self.check.read_text(encoding="utf-8"))
        self.assertEqual([k for k in data if "|codex|" in k], [])

    def test_corrupt_check_file_is_ignored(self):
        self.check.write_text("not json", encoding="utf-8")
        self.set_cursor({"result": cursor_reply(["true"])})
        updated, lines = self.ensure(RepoConfig(tests=["true"]))
        self.assertEqual((updated, lines), (self.workers, []))
        self.assertIsInstance(json.loads(self.check.read_text(encoding="utf-8")), dict)


    def test_codex_probe_failure_is_not_cached_and_names_the_cause(self):
        os.environ["FAKE_CODEX_SANDBOX_PROBE_EXIT"] = "2"
        os.environ["FAKE_CODEX_SANDBOX_PROBE_STDERR"] = "error: unrecognized option '-C'"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_PROBE_EXIT", None)
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_PROBE_STDERR", None)
        self.set_cursor({"result": cursor_reply(["true", "echo hi"])})
        updated, lines = self.ensure(RepoConfig(tests=["true"], lint=["echo hi"]))
        self.assertEqual((updated["a"]["runner"], updated["writer"]["runner"]), ("claude", "claude"))
        self.assertEqual(self.codex_commands(), [])
        self.assertEqual(self.calls("codex-sandbox"), 1)
        self.assertIn("codex 샌드박스 점검 명령 자체가 실패했다", lines[0])
        self.assertIn("종료 코드 2", lines[0])
        self.assertIn("unrecognized option", lines[0])
        self.assertNotIn("검증 명령이 실패해", lines[0])
        data = json.loads(self.check.read_text(encoding="utf-8"))
        self.assertEqual([k for k in data if "|codex|" in k], [])

    def test_codex_probe_failure_is_retried_on_next_run(self):
        os.environ["FAKE_CODEX_SANDBOX_PROBE_EXIT"] = "2"
        self.set_cursor({"result": cursor_reply(["true"])})
        cfg = RepoConfig(tests=["true"])
        self.ensure(cfg)
        del os.environ["FAKE_CODEX_SANDBOX_PROBE_EXIT"]
        updated, lines = self.ensure(cfg)
        self.assertEqual((updated, lines), (self.workers, []))

    def test_cursor_probe_not_reported_zero_is_not_cached(self):
        self.set_cursor({"result": cursor_reply(["true"], probe_exit=1)})
        updated, lines = self.ensure(RepoConfig(tests=["true"]))
        self.assertEqual(updated["b"]["runner"], "claude")
        self.assertIn("cursor 샌드박스 점검 명령 자체가 실패했다", lines[0])
        self.assertIn("true", lines[0])
        data = json.loads(self.check.read_text(encoding="utf-8"))
        self.assertEqual([k for k in data if "|cursor|" in k], [])

    def test_cursor_prompt_puts_probe_command_first(self):
        self.set_cursor({"result": cursor_reply(["echo hi"])})
        self.ensure(RepoConfig(tests=["echo hi"]))
        stdin = (self.log / "cursor-agent-1.stdin").read_text(encoding="utf-8")
        self.assertLess(stdin.index('"true"'), stdin.index("echo hi"))

    def test_base_worktree_is_restored_after_checks(self):
        os.environ["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_CODEX_SANDBOX_EXIT", None)
        before = tree_of(self.wt)
        cmd = "echo junk > outside.txt; rm calc.py; false"
        self.set_cursor({"result": cursor_reply([cmd], 1)})
        self.ensure(RepoConfig(tests=[cmd]))
        self.assertEqual(tree_of(self.wt), before)
        self.assertFalse((self.wt / "outside.txt").exists())
        self.assertTrue((self.wt / "calc.py").is_file())

    def test_base_worktree_is_restored_when_cursor_edits_files(self):
        self.set_cursor({"result": cursor_reply(["true"])})
        os.environ["FAKE_MUTATE"] = "junk.txt=x;calc.py=y"
        self.addCleanup(os.environ.pop, "FAKE_MUTATE", None)
        before = tree_of(self.wt)
        self.ensure(RepoConfig(tests=["true"]))
        self.assertEqual(tree_of(self.wt), before)
        self.assertEqual((self.wt / "calc.py").read_text(encoding="utf-8"), "x = 1\n")
        self.assertFalse((self.wt / "junk.txt").exists())


class JobsSandboxCheckTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.codex = install(bin_dir, "codex", CODEX_BODY)
        self.cursor = install(bin_dir, "cursor-agent", CURSOR_BODY)
        self.log = self.tmp / "log"
        self.env.update({
            "HOME": str(self.home), "FAKE_CLI_LOG": str(self.log),
            "CASCADE_CODEX": str(self.codex), "CASCADE_CURSOR_AGENT": str(self.cursor),
            "CASCADE_SANDBOX_CHECK": str(self.tmp / "sandbox-check.json"),
            "FAKE_CURSOR_RESULTS": json.dumps([{"result": cursor_reply(["python3 -m unittest"])}])})

    def stage1_cli_jobs(self):
        path = self.run_dir / "round-1" / "stage1-cli-args.json"
        return [j["id"] for j in json.loads(path.read_text(encoding="utf-8"))["jobs"]] if path.is_file() else []

    def test_jobs_runs_check_before_writing_cli_args(self):
        self.env["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.init("--workers", "external", "--reviewer", "claude")
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        ledger = self.ledger()
        self.assertEqual(ledger.workers["a"]["runner"], "claude")
        self.assertEqual(ledger.workers["b"]["runner"], "cursor")
        self.assertEqual(self.stage1_cli_jobs(), ["r1-01:b", "r1-02:b"])
        self.assertIn("codex 샌드박스에서 검증 명령이 실패해 이 저장소에서는 Claude 작업자를 쓴다: python3 -m unittest {files}", p.stdout)
        claude = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in claude["jobs"]], ["r1-01:a", "r1-02:a"])

    def add_profile(self, *lines):
        path = Path(self.env["CASCADE_REPOS_TOML"])
        path.write_text(path.read_text(encoding="utf-8") + "\n[profiles.mine]\nbase = \"claude-only\"\n" + "\n".join(lines) + "\n",
                        encoding="utf-8")

    def test_jobs_sandbox_failure_updates_profile_roles_and_substituted(self):
        self.add_profile('"code.a" = { runner = "codex", fallback = { model = "claude-haiku-5-5" } }')
        self.env["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.init("--profile", "mine")
        self.assertEqual(self.ledger().profile["consented"], ["codex"])
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        profile = self.ledger().profile
        self.assertEqual((profile["roles"]["code.a"]["runner"], profile["roles"]["code.a"]["model"]), ("claude", "claude-haiku-5-5"))
        self.assertEqual(profile["substituted"],
                         [{"role": "code.a", "from": "codex", "to": "claude", "reason": "샌드박스 점검 실패"}])
        self.assertEqual(profile["consented"], [])
        self.assertEqual(self.ledger().workers["a"]["model"], "claude-haiku-5-5")
        self.assertEqual(self.stage1_cli_jobs(), [])
        claude = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual([(j["id"], j["model"]) for j in claude["jobs"]][0], ("r1-01:a", "claude-haiku-5-5"))
        self.assertIn("codex 샌드박스에서 검증 명령이 실패해", p.stdout)

    def test_jobs_sandbox_failure_moves_to_external_fallback_in_profile(self):
        self.add_profile('"code.a" = { runner = "codex", fallback = { runner = "cursor" } }')
        self.env["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.init("--profile", "mine")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        profile = self.ledger().profile
        self.assertEqual(profile["roles"]["code.a"]["runner"], "cursor")
        self.assertEqual(profile["roles"]["code.a"]["fallback"]["runner"], "claude")
        self.assertEqual(profile["consented"], ["cursor"])
        self.assertEqual(self.stage1_cli_jobs(), ["r1-01:a", "r1-02:a"])

    def test_jobs_sandbox_failure_replaces_other_cells_fallback_and_records_it(self):
        self.add_profile('"code.a" = { runner = "cursor", fallback = { runner = "codex" } }', '"code.b" = { runner = "codex" }')
        self.env["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.init("--profile", "mine")
        self.assertEqual(self.ledger().profile["consented"], ["codex", "cursor"])
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        profile = self.ledger().profile
        self.assertEqual((profile["roles"]["code.b"]["runner"], profile["roles"]["code.a"]["runner"]), ("claude", "cursor"))
        self.assertEqual((profile["roles"]["code.a"]["fallback"]["runner"], profile["roles"]["code.a"]["fallback"]["model"]),
                         ("claude", "claude-sonnet-5-5"))
        self.assertEqual(profile["substituted"], [
            {"role": "code.a(대체)", "from": "codex", "to": "claude", "reason": "샌드박스 점검 실패"},
            {"role": "code.b", "from": "codex", "to": "claude", "reason": "샌드박스 점검 실패"}])
        self.assertEqual(profile["consented"], ["cursor"])
        self.assertEqual(self.ledger().workers["a"]["fallback"]["runner"], "claude")

    def test_jobs_sandbox_failure_on_old_ledger_materializes_profile(self):
        self.init("--workers", "external", "--reviewer", "claude")
        path = self.run_dir / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["profile"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.env["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        profile = self.ledger().profile
        self.assertEqual(profile["name"], "legacy")
        self.assertEqual(profile["roles"]["code.a"]["runner"], "claude")
        self.assertEqual(profile["roles"]["code.b"]["runner"], "cursor")
        self.assertEqual([r["role"] for r in profile["substituted"]], ["code.a"])
        self.assertEqual(self.stage1_cli_jobs(), ["r1-01:b", "r1-02:b"])

    def test_jobs_keeps_assignment_when_sandbox_passes(self):
        self.init("--workers", "external", "--reviewer", "claude")
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertEqual(self.ledger().workers["a"]["runner"], "codex")
        self.assertEqual(self.stage1_cli_jobs(), ["r1-01:a", "r1-01:b", "r1-02:a", "r1-02:b"])
        self.assertNotIn("샌드박스에서", p.stdout)

    def test_jobs_force_removes_stale_cli_args_when_all_demoted(self):
        self.init("--workers", "external", "--reviewer", "claude")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertTrue(self.stage1_cli_jobs())
        self.env["FAKE_CODEX_SANDBOX_EXIT"] = "1"
        self.env["FAKE_CURSOR_RESULTS"] = json.dumps([{"result": cursor_reply(["python3 -m unittest"], 1)}])
        self.env["CASCADE_SANDBOX_CHECK"] = str(self.tmp / "other-check.json")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", "--force")
        self.assertEqual(self.stage1_cli_jobs(), [])
        self.assertFalse((self.run_dir / "round-1" / "stage1-cli-args.json").exists())
        self.assertEqual({v["runner"] for v in self.ledger().workers.values()}, {"claude"})

    def test_jobs_keeps_assignment_when_target_tests_fail_at_baseline(self):
        (self.repo / "test_calc.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.fail('baseline')\n",
            encoding="utf-8")
        git(self.repo, "commit", "-qam", "failing baseline test")
        self.init("--workers", "external", "--reviewer", "claude")
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertEqual((self.ledger().workers["a"]["runner"], self.ledger().workers["b"]["runner"]), ("codex", "cursor"))
        self.assertEqual(self.stage1_cli_jobs(), ["r1-01:a", "r1-01:b", "r1-02:a", "r1-02:b"])
        self.assertNotIn("샌드박스에서", p.stdout)

    def test_doc_mode_does_not_run_sandbox_check(self):
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        (self.repo / "plan.md").write_text("# 계획\n\n1단계는 `calc.py`를 고친다.\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), "--doc", str(self.repo / "plan.md"),
                     "--workers", "external", "--reviewer", "claude")
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertFalse(list(self.log.glob("codex-sandbox-*")) if self.log.is_dir() else False)
        self.assertEqual(self.ledger().workers["a"]["runner"], "codex")


class JudgeStaleFileTest(CliTestBase):
    def test_judge_removes_stale_checker_cli_args_without_cli_checker_jobs(self):
        (self.repo / "plan.md").write_text("# 계획\n\n1단계는 `calc.py`를 고친다.\n", encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), "--doc", str(self.repo / "plan.md"))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        wt = self.wt("r1-01-writer")
        (wt / "plan.md").write_text("# 계획\n\n1단계는 `calc.py`를 고쳐 add가 더하게 한다.\n", encoding="utf-8")
        commit_worktree(wt, "writer")
        out = {"verdict": "사실", "needs_design_decision": False, "reason": "모호하다", "verify": []}
        stage1 = self.run_dir / "round-1" / "stage1.json"
        stage1.write_text(json.dumps({"results": [
            {"id": "r1-01:a", "output": {**out, "model_id": "claude-sonnet-5-5"}},
            {"id": "r1-01:b", "output": {**out, "model_id": "claude-haiku-5-5"}},
            {"id": "r1-01:writer", "output": {**out, "model_id": "claude-sonnet-5-5"}},
        ]}, ensure_ascii=False), encoding="utf-8")
        stale = self.run_dir / "round-1" / "checker-cli-args.json"
        stale.write_text('{"jobs": [{"id": "stale"}]}', encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertTrue((self.run_dir / "round-1" / "checker-args.json").is_file())
        self.assertFalse(stale.exists())


class HelpersDefaultsTest(CliTestBase):
    def test_run_cli_defaults_workers_to_claude(self):
        with mock.patch.dict(os.environ, {"CASCADE_WORKERS": "external"}):
            self.init("--reviewer", "claude")
        self.assertEqual((self.ledger().workers_choice, self.ledger().workers), ("claude", {}))


if __name__ == "__main__":
    unittest.main()
