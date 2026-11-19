import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import detect, jobs, workers  # noqa: E402
from cascade.cli_steps import model_warnings  # noqa: E402
from cascade.consensus import WorkerResult  # noqa: E402
from cascade.rounds import Ledger  # noqa: E402
from fake_cli import CODEX_BODY, CURSOR_BODY, fenced, install  # noqa: E402
from helpers import run_cli  # noqa: E402
from test_cli_e2e import CALC, FIXTURES, CliTestBase  # noqa: E402

BOTH = {"codex": True, "cursor": True}
CLAUDE_SONNET = {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low"}
CLAUDE_HAIKU = {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low"}


def assigned(available=BOTH, codex="gpt-x", cursor="composer-2.5"):
    return workers.assign("external", available, codex, cursor)


class AssignTest(unittest.TestCase):
    def test_assign_both_available(self):
        w = assigned()
        self.assertEqual((w["a"]["runner"], w["writer"]["runner"]), ("codex", "codex"))
        self.assertEqual((w["b"]["runner"], w["checker"]["runner"], w["b"]["model"]), ("cursor", "cursor", "composer-2.5"))
        self.assertEqual((w["a"]["model"], w["a"]["effort"]), ("gpt-x", "low"))
        self.assertEqual(set(w), set(workers.WORKER_ROLES))

    def test_assign_only_codex_keeps_haiku_seat(self):
        w = assigned({"codex": True, "cursor": False})
        self.assertEqual((w["a"]["runner"], w["writer"]["runner"]), ("codex", "codex"))
        self.assertEqual((w["b"], w["checker"]), (CLAUDE_HAIKU, CLAUDE_HAIKU))

    def test_assign_only_cursor_takes_sonnet_seat(self):
        w = assigned({"codex": False, "cursor": True}, codex="")
        self.assertEqual((w["a"]["runner"], w["a"]["model"], w["writer"]["runner"]), ("cursor", "composer-2.5", "cursor"))
        self.assertEqual(w["b"], CLAUDE_HAIKU)

    def test_assign_nothing_available_is_roles(self):
        w = assigned({"codex": False, "cursor": False})
        self.assertEqual((w["a"], w["writer"]), (CLAUDE_SONNET, CLAUDE_SONNET))
        self.assertEqual((w["b"], w["checker"]), (CLAUDE_HAIKU, CLAUDE_HAIKU))

    def test_assign_claude_choice_is_roles(self):
        w = workers.assign("claude", BOTH, "gpt-x", "composer-2.5")
        for role in workers.WORKER_ROLES:
            model, effort = jobs.ROLES[role]
            self.assertEqual(w[role], {"runner": "claude", "model": model, "effort": effort})

    def test_expected_model_id(self):
        w = assigned()
        self.assertEqual(workers.expected_model_id(w["a"]), "codex:gpt-x")
        self.assertEqual(workers.expected_model_id(w["b"]), "cursor:composer-2.5")
        self.assertEqual(workers.expected_model_id(CLAUDE_HAIKU), "claude-haiku-5-5")
        self.assertEqual(workers.expected_model_id(assigned(codex="")["a"]), "codex:default")

    def test_describe(self):
        self.assertEqual(workers.describe(assigned()), "A·writer=Codex(gpt-x), B·checker=Cursor(composer-2.5)")
        self.assertEqual(workers.describe(assigned({"codex": True, "cursor": False})),
                         "A·writer=Codex(gpt-x), B·checker=Claude(claude-haiku-5-5)")

    def test_consent_line_names_only_used_clis(self):
        self.assertIn("OpenAI", workers.consent_line(assigned()))
        self.assertIn("Cursor(팀 프라이버시 모드)", workers.consent_line(assigned()))
        only_codex = workers.consent_line(assigned({"codex": True, "cursor": False}))
        self.assertIn("OpenAI", only_codex)
        self.assertNotIn("Cursor", only_codex)
        self.assertEqual(workers.consent_line(assigned({"codex": False, "cursor": False})), "")


class ModelWarningsTest(unittest.TestCase):
    def result(self, role, model_id):
        return WorkerResult(finding_id="r1-01", role=role, verdict="사실", needs_design_decision=False,
                            reason="근거", verify=[], checker_ok=None, model_id=model_id)

    def test_model_warnings_use_expected_map(self):
        results = {"r1-01:a": self.result("a", "codex:gpt-x"), "r1-01:b": self.result("b", "claude-haiku-5-5")}
        self.assertEqual(model_warnings(results, {"r1-01:a": "codex:gpt-x"}), [])
        warnings = model_warnings({"r1-01:a": self.result("a", "codex:other")}, {"r1-01:a": "codex:gpt-x"})
        self.assertEqual(len(warnings), 1)
        self.assertIn("codex:gpt-x", warnings[0])

    def test_unlisted_ids_fall_back_to_roles(self):
        warnings = model_warnings({"r1-01:b": self.result("b", "other-model")}, {"r1-01:a": "codex:gpt-x"})
        self.assertEqual(len(warnings), 1)
        self.assertIn("claude-haiku-5-5", warnings[0])
        self.assertEqual(model_warnings({"r1-01:b": self.result("b", "claude-haiku-5-5")}), [])


class EstimateTest(unittest.TestCase):
    def test_estimate_line_splits_by_runner(self):
        line = detect.estimate_line(2, "code", "claude", assigned())
        self.assertIn("작업자 4(Claude 0·Codex 2·Cursor 2)", line)
        doc = detect.estimate_line(1, "doc", "claude", assigned({"codex": True, "cursor": False}))
        self.assertIn("작업자 3(Claude 1·Codex 2·Cursor 0)", doc)

    def test_estimate_line_without_workers_is_unchanged(self):
        self.assertIn("작업자 4,", detect.estimate_line(2, "code", "claude"))
        self.assertIn("작업자 4,", detect.estimate_line(2, "code", "claude", {}))


class ExternalWorkersBase(CliTestBase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
        (self.home / ".codex" / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.codex = install(bin_dir, "codex", CODEX_BODY)
        self.cursor = install(bin_dir, "cursor-agent", CURSOR_BODY)
        reply = fenced(json.dumps({"results": [{"cmd": "true", "exit": 0}, {"cmd": "python3 -m unittest", "exit": 0}]}))
        self.env.update({"HOME": str(self.home), "FAKE_CLI_LOG": str(self.tmp / "log"),
                         "CASCADE_SANDBOX_CHECK": str(self.tmp / "sandbox-check.json"),
                         "FAKE_CURSOR_RESULTS": json.dumps([{"result": reply}])})

    def use_fakes(self):
        self.env.update({"CASCADE_CODEX": str(self.codex), "CASCADE_CURSOR_AGENT": str(self.cursor)})


class InitWorkersTest(ExternalWorkersBase):
    def test_init_external_records_assignment_and_prints_it(self):
        self.use_fakes()
        p = self.init("--workers", "external", "--reviewer", "claude")
        ledger = self.ledger()
        self.assertEqual(ledger.workers_choice, "external")
        self.assertEqual({k: ledger.workers["a"][k] for k in ("runner", "model", "effort")},
                         {"runner": "codex", "model": "gpt-test", "effort": "low"})
        self.assertEqual(ledger.workers["b"]["runner"], "cursor")
        self.assertIn("작업자: A·writer=Codex(gpt-test), B·checker=Cursor(composer-2.5)", p.stdout)
        self.assertIn("작업자 4(Claude 0·Codex 2·Cursor 2)", p.stdout)
        self.assertIn("전송 동의", p.stdout)
        self.assertIn("OpenAI", p.stdout)

    def test_init_external_without_any_cli_falls_back_with_notice(self):
        p = self.init("--workers", "external", "--reviewer", "claude")
        ledger = self.ledger()
        self.assertEqual({v["runner"] for v in ledger.workers.values()}, {"claude"})
        self.assertIn("쓸 수 있는 외부 CLI가 없다", p.stdout)
        self.assertNotIn("전송 동의", p.stdout)

    def test_init_default_leaves_workers_empty(self):
        self.use_fakes()
        p = self.init("--reviewer", "claude")
        ledger = self.ledger()
        self.assertEqual((ledger.workers_choice, ledger.workers), ("claude", {}))
        self.assertNotIn("작업자:", p.stdout)

    def test_init_workers_from_environment(self):
        self.use_fakes()
        self.env["CASCADE_WORKERS"] = "external"
        self.init("--reviewer", "claude")
        self.assertEqual(self.ledger().workers_choice, "external")

    def test_init_cursor_model_option(self):
        self.use_fakes()
        self.init("--workers", "external", "--cursor-model", "composer-9", "--reviewer", "claude")
        self.assertEqual(self.ledger().workers["b"]["model"], "composer-9")

    def test_jobs_split_claude_and_cli_files(self):
        self.use_fakes()
        self.init("--workers", "external", "--reviewer", "claude")
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        rd = self.run_dir / "round-1"
        claude = json.loads((rd / "stage1-args.json").read_text(encoding="utf-8"))
        cli = json.loads((rd / "stage1-cli-args.json").read_text(encoding="utf-8"))
        self.assertEqual(claude["jobs"], [])
        self.assertEqual([(j["id"], j["runner"], j["writable"]) for j in cli["jobs"]],
                         [("r1-01:a", "codex", True), ("r1-01:b", "cursor", True),
                          ("r1-02:a", "codex", True), ("r1-02:b", "cursor", True)])
        self.assertIn("CLI 작업 4건", p.stdout)
        self.assertIn(str(rd / "stage1-cli-args.json"), p.stdout)

    def test_jobs_without_cli_writes_no_cli_file(self):
        self.init("--reviewer", "claude")
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertFalse((self.run_dir / "round-1" / "stage1-cli-args.json").exists())
        self.assertNotIn("CLI 작업", p.stdout)

    def detect(self, *extra):
        p = run_cli(["detect", "--json", "--no-gh", "--input", str(FIXTURES / "codex-code-e2e.final"),
                     "--reviewer", "claude", *extra], cwd=self.repo, env=self.env)
        return json.loads(p.stdout)

    def test_detect_external_adds_consent_estimate_and_flag(self):
        self.use_fakes()
        r = self.detect("--workers", "external")
        self.assertEqual(r["workers"]["a"]["runner"], "codex")
        self.assertTrue(any(a.startswith("전송 동의") and "OpenAI" in a and "Cursor" in a for a in r["approvals"]))
        self.assertEqual(r["recommendation"]["estimate"]["stage1_by_runner"], {"claude": 0, "codex": 2, "cursor": 2})
        self.assertIn("--workers external", r["recommendation"]["steps"][0])

    def test_detect_external_without_cli_notes_fallback(self):
        r = self.detect("--workers", "external")
        self.assertFalse(any("전송 동의" in a for a in r["approvals"]))
        self.assertTrue(any("쓸 수 있는 외부 CLI가 없다" in n for n in r["notes"]))

    def test_detect_default_is_unchanged(self):
        self.use_fakes()
        r = self.detect()
        self.assertEqual(r["workers"], {})
        self.assertNotIn("--workers", r["recommendation"]["steps"][0])
        self.assertFalse(any("전송 동의" in a for a in r["approvals"]))

    def test_detect_claude_keeps_flag_when_env_selects_external(self):
        self.use_fakes()
        self.env["CASCADE_WORKERS"] = "external"
        r = self.detect("--workers", "claude")
        self.assertFalse(any("작업자 전송 동의" in a for a in r["approvals"]))
        self.assertIn("--workers claude", r["recommendation"]["steps"][0])

    def test_first_review_path_carries_workers_to_review_and_init(self):
        self.use_fakes()
        (self.repo / "calc.py").write_text(CALC + "\n# 바꿈\n", encoding="utf-8")
        p = run_cli(["detect", "--json", "--no-gh", "--reviewer", "claude", "--workers", "external",
                     "--cursor-model", "composer-test"], cwd=self.repo, env=self.env)
        review = json.loads(p.stdout)["recommendation"]["steps"][0]
        self.assertIn("review --kind code --uncommitted", review)
        self.assertIn("--workers external --cursor-model composer-test", review)
        p = self.cli("review", "--kind", "code", "--uncommitted", "--workers", "external", "--cursor-model", "composer-test")
        init = next(line for line in p.stdout.splitlines() if " init " in line)
        self.assertIn("--workers external --cursor-model composer-test", init)


class LedgerCompatTest(unittest.TestCase):
    def test_old_ledger_without_workers_loads_with_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.json"
            path.write_text(json.dumps({"run_id": "x", "source": "codex-code", "mode": "code", "root": "/r",
                                        "snapshot": {}, "dry_run": False}), encoding="utf-8")
            ledger = Ledger.load(path)
        self.assertEqual((ledger.workers_choice, ledger.workers), ("claude", {}))


if __name__ == "__main__":
    unittest.main()
