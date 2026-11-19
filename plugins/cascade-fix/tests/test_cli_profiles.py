import json
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli_steps, profiles, review  # noqa: E402
from cascade.report import render  # noqa: E402
from cascade.rounds import Ledger, RoundRecord  # noqa: E402
from cascade.snapshot import commit_worktree  # noqa: E402
from helpers import make_repo, run_cli, write_repos_toml  # noqa: E402
from test_claude_reviewer import CLAUDE_REVIEW, review_results  # noqa: E402
from test_cli_e2e import CALC, RECHECK_RESULT, fix  # noqa: E402
from test_workers import ExternalWorkersBase  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NO_LEGACY_ENV = {"CASCADE_WORKERS": "", "CASCADE_REVIEWER": ""}
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
OLD_STAGE1_JOBS = [
    {"id": "r1-01:a", "model": SONNET, "effort": "low"}, {"id": "r1-01:b", "model": HAIKU, "effort": "low"},
    {"id": "r1-02:a", "model": SONNET, "effort": "low"}, {"id": "r1-02:b", "model": HAIKU, "effort": "low"},
]


class CliProfilesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": "import unittest\n",
            ".gitignore": "__pycache__/\n",
        })
        toml = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["python3 -m unittest {files}"], "format": []}})
        self.env = {"CASCADE_REPOS_TOML": str(toml), "PYTHONDONTWRITEBYTECODE": "1", **NO_LEGACY_ENV}
        self.result = FIXTURES / "codex-code-e2e.final"

    def init(self, *extra, env=None):
        return run_cli(["init", "--source", "codex-code", "--input", str(self.result), *extra], self.repo,
                       env={**self.env, **(env or {})})

    def run_dir(self, p):
        line = next(x for x in p.stdout.splitlines() if x.startswith("run_dir="))
        return Path(line.split("=", 1)[1])

    def ledger(self, p):
        return json.loads((self.run_dir(p) / "ledger.json").read_text(encoding="utf-8"))

    def stage1_jobs(self, p):
        run_dir = self.run_dir(p)
        jobs = run_cli(["jobs", "--run-dir", str(run_dir), "--round", "1"], self.repo, env=self.env)
        self.assertEqual(jobs.returncode, 0, jobs.stdout + jobs.stderr)
        data = json.loads((run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        return [{k: j[k] for k in ("id", "model", "effort")} for j in data["jobs"]], data["jobs"]

    def old_behaviour(self, p, *, reviewer, review_model, review_effort):
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("폐기 예정", p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("지적 2건", p.stdout)
        self.assertIn("프로필: default (builtin)", p.stdout)
        self.assertNotIn("덮음:", p.stdout)
        self.assertNotIn("대체:", p.stdout)
        ledger = self.ledger(p)
        self.assertEqual((ledger["workers_choice"], ledger["workers"], ledger["adopt_on_both_pass"]), ("claude", {}, False))
        self.assertEqual((ledger["reviewer"], ledger["review_model"], ledger["review_effort"]),
                         (reviewer, review_model, review_effort))
        self.assertEqual((ledger["profile"]["name"], ledger["profile"]["source"], ledger["profile"]["explicit"]),
                         ("default", "builtin", False))
        self.assertEqual((ledger["profile"]["overrides"], ledger["profile"]["substituted"]), ([], []))
        slim, full = self.stage1_jobs(p)
        self.assertEqual(slim, OLD_STAGE1_JOBS)
        self.assertEqual([j["kind"] for j in full], ["worker"] * 4)
        self.assertEqual(sorted(f.name for f in (self.run_dir(p) / "round-1").iterdir() if f.name.startswith("stage1")),
                         ["stage1-args.json"])
        self.assertEqual(sorted(f.name for f in (self.run_dir(p) / "round-1" / "prompts").iterdir()),
                         ["r1-01-a.md", "r1-01-b.md", "r1-02-a.md", "r1-02-b.md"])

    def test_init_without_profile_is_unchanged(self):
        script = self.tmp / "run-review.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        p = self.init(env={"CASCADE_RUN_REVIEW": str(script)})
        self.old_behaviour(p, reviewer="codex", review_model="", review_effort="")
        self.assertNotIn("첫 검토", p.stdout)
        self.assertIn("역할: A=Claude(Sonnet 5.5 low)", p.stdout)
        self.assertIn("의견=Codex, 재검토=Codex", p.stdout)
        self.assertIn("전송 동의: 의견·재검토 → OpenAI", p.stdout)

    def test_init_without_profile_and_without_review_cli_is_claude(self):
        p = self.init(env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.old_behaviour(p, reviewer="claude", review_model="claude-fable-5-1", review_effort="high")
        self.assertIn("의견=Claude(Fable 5.1 high), 재검토=Claude(Fable 5.1 high)", p.stdout)
        self.assertNotIn("전송 동의", p.stdout)
        self.assertIn("의견·재검토: Claude 단독(claude-fable-5-1, high) — 외부 전송 없음", p.stdout)

    def test_init_prints_profile_lines_before_scale_line(self):
        p = self.init(env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        lines = p.stdout.splitlines()
        profile_at = next(i for i, x in enumerate(lines) if x.startswith("프로필:"))
        roles_at = next(i for i, x in enumerate(lines) if x.startswith("역할:"))
        scale_at = next(i for i, x in enumerate(lines) if x.startswith("규모:"))
        self.assertLess(profile_at, roles_at)
        self.assertLess(roles_at, scale_at)

    def test_init_review_model_applies_to_auto_cell_and_is_dropped_for_codex(self):
        script = self.tmp / "run-review.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        p = self.init("--review-model", "claude-opus-5-5", env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.ledger(p)["review_model"], "claude-opus-5-5")
        self.assertIn("덮음: code.opinion·code.recheck·doc.opinion·doc.recheck ← --review-model claude-opus-5-5", p.stdout)
        p = self.init("--review-model", "claude-opus-5-5", env={"CASCADE_RUN_REVIEW": str(script)})
        ledger = self.ledger(p)
        self.assertEqual((ledger["reviewer"], ledger["review_model"], ledger["review_effort"]), ("codex", "", ""))
        self.assertEqual(ledger["profile"]["roles"]["code.opinion"]["model"], "")

    def test_init_bad_review_model_for_claude_exit5_names_option(self):
        p = self.init("--review-model", "composer-2.5", env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("--review-model composer-2.5", p.stdout)
        p = self.init("--reviewer", "claude", "--review-model", "composer-2.5")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("--review-model composer-2.5", p.stdout)

    def test_init_explicit_profile_ignores_invalid_workers_env(self):
        p = self.init("--profile", "claude-only", env={"CASCADE_WORKERS": "bogus"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("CASCADE_WORKERS는 프로필을 명시적으로 골랐으므로 무시한다", p.stdout)
        self.assertIn("프로필: claude-only (--profile)", p.stdout)
        self.assertEqual(self.ledger(p)["profile"]["explicit"], True)

    def test_init_invalid_workers_env_without_profile_exit5(self):
        p = self.init(env={"CASCADE_WORKERS": "bogus"})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("CASCADE_WORKERS=bogus", p.stdout)

    def test_init_env_overrides_are_labelled_and_deprecated(self):
        p = self.init(env={"CASCADE_REVIEWER": "claude", "CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.assertIn("참고: 폐기 예정", p.stdout)
        self.assertIn("덮음: code.opinion·code.recheck·doc.opinion·doc.recheck ← CASCADE_REVIEWER=claude", p.stdout)
        self.assertNotIn("← --reviewer", p.stdout)

    def test_init_consent_option_is_validated(self):
        p = self.init("--consent", "openai")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("openai", p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        p = self.init("--consent", "none,codex")
        self.assertNotEqual(p.returncode, 0)

    def test_init_unknown_profile_exit5(self):
        p = self.init("--profile", "nope")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("nope", p.stdout)

    def test_init_unknown_env_profile_exit5(self):
        p = self.init(env={"CASCADE_PROFILE": "nope"})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("claude-only", p.stdout)

    def test_init_known_profile_is_accepted(self):
        p = self.init("--profile", "claude-only")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_init_override_that_breaks_validation_exit5(self):
        fake = self.tmp / "cursor-agent"
        fake.write_text("#!/bin/sh\necho Logged in\n", encoding="utf-8")
        fake.chmod(0o755)
        p = self.init("--workers", "external", "--cursor-model", "claude-sonnet-5-5",
                      env={"CASCADE_CURSOR_AGENT": str(fake)})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("cursor", p.stdout)

    def test_detect_and_review_accept_profile(self):
        p = run_cli(["detect", "--profile", "default", "--no-gh", "--json", "--input", str(self.result)], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["profile"]["name"], "default")
        p = run_cli(["review", "--profile", "default", "--kind", "code", "--uncommitted"], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_detect_unknown_profile_exit5(self):
        p = run_cli(["detect", "--profile", "nope", "--no-gh", "--json", "--input", str(self.result)], self.repo, env=self.env)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("nope", p.stdout)
        p = run_cli(["detect", "--no-gh", "--json", "--input", str(self.result)], self.repo,
                    env={**self.env, "CASCADE_PROFILE": "nope"})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)


class LocalProfileTest(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        rr = self.tmp / "run-review.sh"
        rr.write_text("#!/bin/sh\n", encoding="utf-8")
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(rr),
                         "CASCADE_CURSOR_REVIEW": str(self.tmp / "none-cursor.sh")})
        self.result = FIXTURES / "codex-code-e2e.final"

    def init_cli(self, *extra, ok=True):
        return self.cli("init", "--source", "codex-code", "--input", str(self.result), *extra, ok=ok)

    def ledger_json(self, p):
        line = next(x for x in p.stdout.splitlines() if x.startswith("run_dir="))
        return json.loads((Path(line.split("=", 1)[1]) / "ledger.json").read_text(encoding="utf-8"))

    def test_init_local_records_profile(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local")
        ledger = self.ledger_json(p)
        self.assertEqual(ledger["profile"]["name"], "local")
        self.assertEqual(ledger["profile"]["roles"]["code.a"]["runner"], "codex")
        self.assertEqual(ledger["profile"]["roles"]["code.a"]["model"], "gpt-test")
        self.assertEqual(ledger["workers"]["a"]["runner"], "codex")
        self.assertEqual(ledger["workers"]["a"]["model"], "gpt-test")
        self.assertEqual(ledger["workers"]["b"]["runner"], "claude")
        self.assertEqual(ledger["reviewer"], "codex")
        self.assertEqual(ledger["workers_choice"], "external")
        self.assertTrue(ledger["adopt_on_both_pass"])
        self.assertEqual(ledger["profile"]["consented"], ["codex"])
        self.assertEqual((ledger["profile"]["substituted"], ledger["profile"]["overrides"]), ([], []))
        self.assertEqual(ledger["profile"]["first_review"], {})
        self.assertEqual(len(ledger["profile"]["roles"]), 14)
        self.assertIn("프로필: local (--profile)", p.stdout)
        self.assertIn("역할: A=Codex(gpt-test low)", p.stdout)
        self.assertNotIn("첫 검토", p.stdout)
        self.assertIn("전송 동의: A·의견·재검토 → OpenAI", p.stdout)
        self.assertIn("한쪽 채택", p.stdout)

    def test_init_local_jobs_use_the_profile_workers(self):
        self.use_fakes()
        self.init_cli("--profile", "local")
        run_dir = next(iter((self.repo / ".cascade-fix").glob("cf-*")))
        self.cli("jobs", "--run-dir", str(run_dir), "--round", "1")
        cli = json.loads((run_dir / "round-1" / "stage1-cli-args.json").read_text(encoding="utf-8"))
        self.assertEqual([(j["id"], j["runner"]) for j in cli["jobs"]], [("r1-01:a", "codex"), ("r1-02:a", "codex")])
        claude = json.loads((run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual([(j["id"], j["model"]) for j in claude["jobs"]], [("r1-01:b", HAIKU), ("r1-02:b", HAIKU)])

    def test_init_consent_none_runs_claude_only(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local", "--consent", "none")
        ledger = self.ledger_json(p)
        self.assertTrue(all(c["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        self.assertTrue(all(c["fallback"]["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        self.assertEqual(ledger["profile"]["consented"], [])
        self.assertEqual((ledger["reviewer"], ledger["workers_choice"], ledger["workers"]), ("claude", "claude", {}))
        self.assertIn("대체: code.a codex → claude — 전송 동의 없음", p.stdout)
        self.assertNotIn("전송 동의:", p.stdout)
        self.assertIn("역할: A=Claude(Sonnet 5.5 low)", p.stdout)
        self.assertNotIn("첫 검토", p.stdout)

    def test_init_partial_consent_keeps_only_consented_recipients(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local", "--consent", "cursor")
        ledger = self.ledger_json(p)
        self.assertEqual(ledger["profile"]["consented"], [])
        self.assertTrue(all(c["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        p = self.init_cli("--profile", "local", "--consent", "codex,cursor")
        self.assertEqual(self.ledger_json(p)["profile"]["consented"], ["codex"])
        self.assertEqual(self.ledger_json(p)["profile"]["roles"]["code.a"]["runner"], "codex")

    def test_init_local_without_codex_substitutes_with_notice(self):
        p = self.init_cli("--profile", "local")
        ledger = self.ledger_json(p)
        self.assertTrue(all(c["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        self.assertIn({"role": "code.a", "from": "codex", "to": "claude", "reason": "미설치 또는 로그인 필요"},
                      ledger["profile"]["substituted"])
        self.assertIn("대체: code.a codex → claude — 미설치 또는 로그인 필요", p.stdout)
        self.assertNotIn("전송 동의", p.stdout)
        self.assertEqual((ledger["reviewer"], ledger["workers"], ledger["workers_choice"]), ("claude", {}, "claude"))
        self.assertEqual(ledger["profile"]["consented"], [])

    def test_init_local_override_is_listed_and_recorded(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local", "--reviewer", "claude")
        ledger = self.ledger_json(p)
        self.assertIn("덮음: code.opinion·code.recheck·doc.opinion·doc.recheck ← --reviewer claude", p.stdout)
        self.assertIn("code.opinion ← --reviewer claude", ledger["profile"]["overrides"])
        self.assertEqual((ledger["reviewer"], ledger["review_model"]), ("claude", "claude-fable-5-1"))

    def test_init_explicit_override_is_not_substituted(self):
        p = self.init_cli("--profile", "claude-only", "--reviewer", "codex")
        ledger = self.ledger_json(p)
        self.assertEqual((ledger["reviewer"], ledger["profile"]["substituted"]), ("codex", []))

    def test_init_workers_external_without_cli_keeps_old_ledger_shape(self):
        p = self.init_cli("--workers", "external", "--reviewer", "claude")
        ledger = self.ledger_json(p)
        self.assertEqual(ledger["workers_choice"], "external")
        self.assertEqual({v["runner"] for v in ledger["workers"].values()}, {"claude"})
        self.assertIn("쓸 수 있는 외부 CLI가 없다", p.stdout)

    def test_init_workers_external_does_not_probe_twice(self):
        self.use_fakes()
        self.init_cli("--workers", "external", "--reviewer", "claude")
        status_calls = sorted((self.tmp / "log").glob("cursor-status-*.args.json"))
        self.assertEqual(len(status_calls), 1)

    def test_ignored_env_workers_does_not_probe_external_clis(self):
        self.use_fakes()
        self.env["CASCADE_WORKERS"] = "external"
        for extra in (("--profile", "claude-only"), ("--workers", "claude")):
            self.init_cli(*extra, "--reviewer", "claude")
        self.assertEqual(sorted((self.tmp / "log").glob("cursor-status-*.args.json")), [])

    def test_detect_local_asks_one_consent_line(self):
        self.use_fakes()
        p = self.cli("detect", "--json", "--no-gh", "--profile", "local", "--input", str(self.result))
        r = json.loads(p.stdout)
        self.assertIn("전송 동의: A·의견·재검토 → OpenAI", r["approvals"])
        self.assertEqual((r["reviewer"], r["workers"]["a"]["runner"]), ("codex", "codex"))
        self.assertIn("--profile local", r["recommendation"]["steps"][0])
        self.assertEqual(r["profile"]["name"], "local")

    def test_detect_local_without_codex_has_no_openai_line(self):
        p = self.cli("detect", "--json", "--no-gh", "--profile", "local", "--input", str(self.result))
        r = json.loads(p.stdout)
        self.assertFalse(any("OpenAI" in a for a in r["approvals"]))
        self.assertTrue(any("code.a" in n and "codex → claude" in n for n in r["notes"]))
        self.assertEqual(r["workers"], {})


class DetectFirstReviewCliTest(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.rr = self.tmp / "run-review.sh"
        self.rr.write_text("#!/bin/sh\n", encoding="utf-8")
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.rr)})
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    def detect(self, *extra):
        p = run_cli(["detect", "--no-gh", "--json", *extra], self.repo, env=self.env)
        return p.returncode, json.loads(p.stdout)

    def test_local_profile_recommends_cf_review_not_a_direct_rr(self):
        code, r = self.detect("--profile", "local")
        self.assertEqual(code, 0, r)
        steps = r["recommendation"]["steps"]
        self.assertTrue(steps[0].endswith("review --kind code --uncommitted --profile local"), steps[0])
        self.assertFalse(any("run-review.sh" in s for s in steps), steps)
        self.assertIn("전송 동의: 첫 검토·A·의견·재검토 → OpenAI", r["approvals"])

    def test_detect_review_step_prints_rr_and_init_through_cf_review(self):
        code, r = self.detect("--profile", "local")
        words = shlex.split(r["recommendation"]["steps"][0])
        p = run_cli(words[words.index("review"):], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("run-review.sh --uncommitted", p.stdout)
        self.assertIn("--source codex-code", p.stdout)

    def test_input_file_shows_codex_model_for_workers_and_roles(self):
        code, r = self.detect("--profile", "local", "--input", str(FIXTURES / "codex-code-e2e.final"))
        self.assertEqual(code, 0, r)
        self.assertEqual(r["workers"]["a"]["model"], "gpt-test")
        self.assertEqual(r["profile"]["roles"]["code.a"]["model"], "gpt-test")
        self.assertNotIn("code.review", r["profile"]["roles"])
        self.assertIn("전송 동의: A·의견·재검토 → OpenAI", r["approvals"])

    def test_human_output_prints_profile_and_role_lines(self):
        p = run_cli(["detect", "--no-gh", "--profile", "local", "--input", str(FIXTURES / "codex-code-e2e.final")],
                    self.repo, env=self.env)
        self.assertIn("프로필: local (--profile)", p.stdout)
        self.assertIn("역할: A=Codex(gpt-test low), B=Claude(Haiku 5.5 low)", p.stdout)
        self.assertNotIn("첫 검토=", p.stdout)

    def test_environment_profile_is_named_in_the_next_init_line(self):
        self.env["CASCADE_PROFILE"] = "local"
        code, r = self.detect("--input", str(FIXTURES / "codex-code-e2e.final"))
        self.assertTrue(r["recommendation"]["steps"][0].endswith("--profile local"), r["recommendation"]["steps"])
        self.assertEqual(r["profile"]["source"], "CASCADE_PROFILE")

    def test_explicit_profile_ignores_env_workers_in_next_lines(self):
        self.env["CASCADE_WORKERS"] = "external"
        code, r = self.detect("--profile", "local")
        self.assertEqual(code, 0, r)
        self.assertNotIn("--workers", r["recommendation"]["steps"][0])
        words = shlex.split(r["recommendation"]["steps"][0])
        p = run_cli(words[words.index("review"):], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("--workers", p.stdout)

    def test_given_options_reach_the_next_init_line_even_when_they_are_defaults(self):
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8")
                        + '\n[profiles.cursor-a]\n"code.a" = { runner = "cursor", model = "composer-9" }\n'
                        + '"code.opinion" = { runner = "codex" }\n', encoding="utf-8")
        code, r = self.detect("--profile", "cursor-a", "--workers", "claude", "--cursor-model", "composer-2.5",
                              "--reviewer", "auto", "--input", str(FIXTURES / "codex-code-e2e.final"))
        self.assertEqual(code, 0, r)
        step = r["recommendation"]["steps"][0]
        self.assertIn("--reviewer auto --workers claude --cursor-model composer-2.5", step)
        words = shlex.split(step)
        p = run_cli(words[words.index("init"):], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("A=Claude", p.stdout)

    def test_missing_review_script_substitutes_opinion_like_init(self):
        self.env["CASCADE_RUN_REVIEW"] = str(self.tmp / "no-such-run-review.sh")
        code, r = self.detect("--profile", "local", "--input", str(FIXTURES / "codex-code-e2e.final"))
        self.assertEqual(code, 0, r)
        roles = r["profile"]["roles"]
        self.assertEqual((roles["code.opinion"]["runner"], roles["code.recheck"]["runner"]), ("claude", "claude"))
        words = shlex.split(r["recommendation"]["steps"][0])
        p = run_cli(words[words.index("init"):], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("의견=Claude", p.stdout)

    def test_cursor_model_alone_reaches_the_next_init_line(self):
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8")
                        + '\n[profiles.cursor-a]\n"code.a" = { runner = "cursor", model = "composer-9" }\n', encoding="utf-8")
        code, r = self.detect("--profile", "cursor-a", "--cursor-model", "composer-test",
                              "--input", str(FIXTURES / "codex-code-e2e.final"))
        self.assertEqual(code, 0, r)
        self.assertEqual(r["profile"]["roles"]["code.a"]["model"], "composer-test")
        step = r["recommendation"]["steps"][0]
        self.assertIn("--cursor-model composer-test", step)
        self.assertNotIn("--workers", step)
        words = shlex.split(step)
        p = run_cli(words[words.index("init"):], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("A=Cursor(composer-test)", p.stdout)

    def test_detect_init_step_runs(self):
        code, r = self.detect("--profile", "local", "--input", str(FIXTURES / "codex-code-e2e.final"))
        words = shlex.split(r["recommendation"]["steps"][0])
        p = run_cli(words[words.index("init"):], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("A=Codex(gpt-test low)", p.stdout)


class InitFirstReviewHistoryTest(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.rr = self.tmp / "run-review.sh"
        self.rr.write_text("#!/bin/sh\n", encoding="utf-8")
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.rr)})

    def init_with_meta(self, first_review, *extra):
        src = self.tmp / "rev"
        src.mkdir()
        result = src / "review.out.final"
        result.write_text((FIXTURES / "codex-code-e2e.final").read_text(encoding="utf-8"), encoding="utf-8")
        (src / "review-meta.json").write_text(json.dumps({"role": "code.review", **first_review}), encoding="utf-8")
        p = run_cli(["init", "--source", "codex-code", "--input", str(result), "--profile", "local", *extra],
                    self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def test_completed_first_review_is_history_not_assignment(self):
        p = self.init_with_meta({"requested": {"runner": "codex", "model": "", "effort": ""},
                                 "actual": {"runner": "claude", "model": "claude-fable-5-1", "effort": "high"},
                                 "fallback_reason": "한도", "attempts": [{"runner": "codex", "reason": "한도"}]})
        self.assertIn("역할: 첫 검토(완료)=Codex → Claude(한도), A=Codex(gpt-test low)", p.stdout)
        self.assertIn("전송 동의: A·의견·재검토 → OpenAI", p.stdout)
        self.assertNotIn("첫 검토·", p.stdout)

    def test_completed_first_review_without_fallback_names_the_runner(self):
        p = self.init_with_meta({"requested": {"runner": "codex", "model": "", "effort": ""},
                                 "actual": {"runner": "codex", "model": "", "effort": ""},
                                 "fallback_reason": None, "attempts": []})
        self.assertIn("역할: 첫 검토(완료)=Codex, A=Codex(gpt-test low)", p.stdout)

    def test_first_review_fallback_is_not_listed_in_the_consent_line(self):
        p = run_cli(["init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"),
                     "--profile", "local", "--consent", "codex"], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("첫 검토", p.stdout)


class ScaleLineTest(ExternalWorkersBase):
    def scale(self, *extra, env=None):
        p = run_cli(["init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"), *extra],
                    self.repo, env={**self.env, **NO_LEGACY_ENV, **(env or {})})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return next(x for x in p.stdout.splitlines() if x.startswith("규모:"))

    def test_explicit_profile_scale_line_ends_with_runner_totals(self):
        self.use_fakes()
        line = self.scale("--profile", "local")
        self.assertTrue(line.endswith(" — Claude 최대 Haiku 1·Opus 1/지적, Codex 1/지적 + 의견·재검토"), line)

    def test_claude_only_profile_scale_line_names_models(self):
        line = self.scale("--profile", "claude-only")
        self.assertIn(" — Claude 최대 Sonnet 1·Haiku 1·Opus 1/지적 + 의견·재검토", line)

    def test_default_profile_scale_line_is_unchanged(self):
        line = self.scale()
        self.assertNotIn(" — ", line)
        self.assertNotIn("/지적", line)


REVIEW_PROFILES = """
[profiles.split]
"code.opinion" = { runner = "claude" }
"code.recheck" = { runner = "codex" }

[profiles.codex-opinion]
"code.opinion" = { runner = "codex" }

[profiles.codex-recheck]
"code.opinion" = { runner = "claude" }
"code.recheck" = { runner = "codex" }

[profiles.cursor-opinion]
"code.opinion" = { runner = "cursor", model = "composer-9" }

[profiles.codex-then-cursor]
"code.opinion" = { runner = "codex", fallback = { runner = "cursor", model = "composer-9" } }

[profiles.sonnet-review]
"code.opinion" = { runner = "claude", model = "claude-sonnet-5-5", effort = "low" }
"code.recheck" = { runner = "claude", model = "claude-haiku-5-5", effort = "low" }
"""
FABLE = "claude-fable-5-1"
SONNET_ID = "claude-sonnet-5-5"
LIMIT_LOG = "You've hit your usage limit\n"
ERROR_LOG = "codex 시작\nERROR: stream disconnected\n"
AUTH_LOG = "codex 시작\nERROR: unexpected status 401 Unauthorized: refresh token was already used\n"


class ReviewRolesBase(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8") + REVIEW_PROFILES, encoding="utf-8")
        self.use_fakes()
        self.rr = self.tmp / "run-review.sh"
        self.cr = self.tmp / "cursor-run-review.sh"
        for script in (self.rr, self.cr):
            script.write_text("#!/bin/sh\n", encoding="utf-8")
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.rr), "CASCADE_CURSOR_REVIEW": str(self.cr)})
        self.rd = None

    def next_steps(self, p):
        lines = p.stdout.splitlines()
        return [x.strip() for x in lines[lines.index("다음:") + 1:]]

    def to_adjudicate(self, *init_extra):
        self.init(*init_extra)
        self.rd = self.run_dir / "round-1"
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.rd / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        adj = self.rd / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        return self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))

    def opinion_workflow(self, text="판가름자와 같은 의견이다", model=FABLE, force=False):
        results = review_results(self.rd / "opinion.json", "r1-02:opinion", text, model)
        return self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(results),
                        *(["--force"] if force else []))

    def recheck_workflow(self, model=FABLE):
        text = RECHECK_RESULT.replace("- r1-02: 해소됨 — mul이 곱한다\n", "")
        results = review_results(self.rd / "recheck.json", "round-1:recheck", text, model)
        return self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(results))

    def failed_opinion(self, log_text):
        out = self.rd / "opinion-r1-02.out"
        out.write_text(log_text, encoding="utf-8")
        final = Path(f"{out}.final")
        final.write_text("", encoding="utf-8")
        return self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(final),
                        ok=False)

    def failed_recheck(self, log_text):
        out = self.rd / "recheck.out"
        out.write_text(log_text, encoding="utf-8")
        final = Path(f"{out}.final")
        final.write_text("", encoding="utf-8")
        return self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(final), ok=False)

    def cell(self, key):
        return self.ledger().profile["roles"][key]

    def current(self, role):
        return cli_steps.review_cell(self.ledger(), role)

    def substituted(self):
        return [{"role": r["role"], "from": r["from"], "to": r["to"], "reason": r["reason"]}
                for r in self.ledger().profile.get("runtime_substitutions", [])]

    def external_opinion(self, fid, text):
        out = self.rd / f"opinion-{fid}.out"
        out.write_text("codex 시작\n", encoding="utf-8")
        final = Path(f"{out}.final")
        final.write_text(text, encoding="utf-8")
        return self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", fid, "--result", str(final))


class ReviewRolesSplitTest(ReviewRolesBase):
    def test_opinion_and_recheck_runners_split(self):
        p = self.to_adjudicate("--profile", "split")
        steps_after_adjudicate = self.next_steps(p)
        self.assertIn("Workflow 실행", steps_after_adjudicate[0])
        self.assertIn("opinion-args.json", steps_after_adjudicate[0])
        [job] = json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["id"], job["model"], job["effort"]), ("r1-02:opinion", FABLE, "high"))
        self.opinion_workflow()
        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        steps_after_apply = self.next_steps(p)
        self.assertIn("--recheck", steps_after_apply[0])
        self.assertIn(shlex.quote(str(self.rr)), steps_after_apply[0])
        self.assertNotIn("Workflow", steps_after_apply[0])
        self.assertFalse((self.rd / "recheck-args.json").exists())
        self.assertEqual(self.ledger().reviewer, "claude")

    def test_cursor_review_command_passes_the_cell_model(self):
        p = self.to_adjudicate("--profile", "cursor-opinion")
        [line] = [x for x in self.next_steps(p) if " --opinion " in x]
        self.assertTrue(line.startswith(shlex.quote(str(self.cr)) + " --opinion"), line)
        self.assertIn("--model composer-9", line)

    def test_codex_review_command_has_no_model(self):
        p = self.to_adjudicate("--profile", "codex-opinion")
        [line] = [x for x in self.next_steps(p) if " --opinion " in x]
        self.assertTrue(line.startswith(shlex.quote(str(self.rr)) + " --opinion"), line)
        self.assertNotIn("--model", line)

    def test_opinion_model_warning_uses_cell(self):
        self.to_adjudicate("--profile", "sonnet-review")
        [job] = json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["model"], job["effort"]), (SONNET_ID, "low"))
        p = self.opinion_workflow(model=SONNET_ID)
        self.assertNotIn("경고", p.stdout)
        p = self.opinion_workflow(model=FABLE, force=True)
        self.assertIn("경고", p.stdout)
        self.assertIn(SONNET_ID, p.stdout)

    def test_recheck_model_warning_uses_cell(self):
        self.to_adjudicate("--profile", "sonnet-review")
        self.opinion_workflow(model=SONNET_ID)
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        [job] = json.loads((self.rd / "recheck-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["model"], job["effort"]), (HAIKU, "low"))
        p = self.recheck_workflow(model=FABLE)
        self.assertIn("경고", p.stdout)
        self.assertIn(HAIKU, p.stdout)

    def test_recheck_matching_model_has_no_warning(self):
        self.to_adjudicate("--profile", "sonnet-review")
        self.opinion_workflow(model=SONNET_ID)
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.recheck_workflow(model=HAIKU)
        self.assertNotIn("경고", p.stdout)


class ReviewRolesFallbackTest(ReviewRolesBase):
    def test_opinion_limit_falls_back_to_claude(self):
        self.to_adjudicate("--profile", "codex-opinion")
        p = self.failed_opinion(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [job] = json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["id"], job["model"], job["effort"]), ("r1-02:opinion", FABLE, "high"))
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "claude", "reason": "이용 한도"}])
        self.assertEqual((self.current("opinion")["runner"], self.current("opinion")["model"]), ("claude", FABLE))
        self.assertEqual(self.cell("code.opinion")["runner"], "codex")
        self.assertEqual(self.ledger().profile["substituted"], [])
        self.assertEqual(self.ledger().reviewer, "codex")
        self.assertNotIn("r1-02", self.ledger().opinions)
        self.assertIn("대체", p.stdout)
        self.assertIn("Workflow 실행", self.next_steps(p)[0])
        self.assertIn("opinion-args.json", self.next_steps(p)[0])
        p = self.opinion_workflow()
        self.assertEqual(self.ledger().opinions["r1-02"], "판가름자와 같은 의견이다")
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("검토자: Claude", p.stdout)
        self.assertIn("Claude 의견:", p.stdout)
        self.assertNotIn("Codex", p.stdout.split("## 사람이 볼 목록")[0])

    def test_opinion_error_falls_back_with_error_reason(self):
        self.to_adjudicate("--profile", "codex-opinion")
        p = self.failed_opinion(ERROR_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "claude", "reason": "실행 오류"}])
        self.assertTrue((self.rd / "opinion-args.json").is_file())

    def test_opinion_fallback_writes_every_pending_opinion_including_the_failed_one(self):
        self.to_adjudicate("--profile", "codex-opinion")
        (self.rd / "opinion-r1-01.md").write_text("판정 의견 요청\n", encoding="utf-8")
        self.failed_opinion(LIMIT_LOG)
        ids = [j["id"] for j in json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]]
        self.assertEqual(ids, ["r1-01:opinion", "r1-02:opinion"])

    def test_opinion_fallback_keeps_attached_opinions_and_labels_each_by_its_runner(self):
        self.to_adjudicate("--profile", "codex-opinion")
        (self.rd / "opinion-r1-01.md").write_text("판정 의견 요청\n", encoding="utf-8")
        path = self.run_dir / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for d in data["rounds"][0]["decisions"]:
            if d["finding_id"] == "r1-01":
                d.update(outcome="판단불가", patch_from=None, reasons=["테스트"])
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        p = self.external_opinion("r1-01", "Codex가 본 의견이다")
        self.assertIn("r1-02", self.next_steps(p)[0])
        self.failed_opinion(LIMIT_LOG)
        ids = [j["id"] for j in json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]]
        self.assertEqual(ids, ["r1-02:opinion"])
        ledger = self.ledger()
        self.assertEqual(ledger.opinions["r1-01"], "Codex가 본 의견이다")
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["runner"], "codex")
        self.assertEqual(len(ledger.profile["runtime_substitutions"]), 1)
        self.opinion_workflow(text="Claude가 본 의견이다")
        events = {e["step"]: e for e in self.ledger().rounds[0].events if e["step"].startswith("opinion:")}
        self.assertEqual((events["opinion:r1-01"]["runner"], "fallback" in events["opinion:r1-01"]), ("codex", False))
        self.assertEqual((events["opinion:r1-02"]["runner"], events["opinion:r1-02"]["model"]), ("claude", FABLE))
        self.assertEqual(events["opinion:r1-02"]["fallback"], {"from": "codex", "to": "claude", "reason": "이용 한도"})
        report = render(self.ledger(), False, self.run_dir)
        self.assertIn("검토자: Codex·Claude", report)
        lines = report.splitlines()
        codex_at = lines.index("  - Codex 의견:")
        claude_at = lines.index("  - Claude 의견:")
        self.assertIn("Codex가 본 의견이다", lines[codex_at + 1])
        self.assertIn("Claude가 본 의견이다", lines[claude_at + 1])
        self.assertLess(codex_at, claude_at)
        self.assertIn("| Codex 의견 | 1 |", report)
        self.assertIn("| Claude 의견 | 1 |", report)

    def test_opinion_auth_still_stops(self):
        self.to_adjudicate("--profile", "codex-opinion")
        p = self.failed_opinion(AUTH_LOG)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("codex login", p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [])
        self.assertEqual(self.cell("code.opinion")["runner"], "codex")
        self.assertFalse((self.rd / "opinion-args.json").exists())

    def test_opinion_without_log_still_stops(self):
        self.to_adjudicate("--profile", "codex-opinion")
        out = self.rd / "opinion-r1-02.out"
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02",
                     "--result", f"{out}.final", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("실행 로그도 없다", p.stdout)
        self.assertEqual(self.substituted(), [])

    def test_opinion_fallback_to_cursor_prints_cr_with_model(self):
        self.to_adjudicate("--profile", "codex-then-cursor")
        p = self.failed_opinion(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "cursor", "reason": "이용 한도"}])
        [line] = [x for x in self.next_steps(p) if " --opinion " in x]
        self.assertTrue(line.startswith(shlex.quote(str(self.cr)) + " --opinion"), line)
        self.assertIn("--model composer-9", line)
        self.assertFalse((self.rd / "opinion-args.json").exists())
        self.assertEqual(self.current("opinion")["runner"], "cursor")
        self.assertEqual(self.current("opinion")["fallback"]["runner"], "claude")
        self.assertEqual(self.cell("code.opinion")["runner"], "codex")

    def test_opinion_fallback_outside_consent_is_claude_only(self):
        self.to_adjudicate("--profile", "codex-then-cursor", "--consent", "codex")
        self.assertEqual(self.cell("code.opinion")["fallback"]["runner"], "claude")
        self.failed_opinion(LIMIT_LOG)
        self.assertEqual(self.substituted()[-1]["to"], "claude")
        self.assertTrue((self.rd / "opinion-args.json").is_file())

    def test_recheck_limit_falls_back_to_claude(self):
        self.to_adjudicate("--profile", "codex-recheck")
        self.opinion_workflow()
        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("--recheck", self.next_steps(p)[0])
        p = self.failed_recheck(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [job] = json.loads((self.rd / "recheck-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["id"], job["model"], job["effort"]), ("round-1:recheck", FABLE, "high"))
        self.assertEqual(self.substituted(), [{"role": "code.recheck", "from": "codex", "to": "claude", "reason": "이용 한도"}])
        self.assertEqual(self.ledger().reviewer, "claude")
        self.assertIn("Workflow 실행", self.next_steps(p)[0])
        self.assertIn("recheck-args.json", self.next_steps(p)[0])
        self.assertFalse(any(e["step"] == "recheck" for e in self.ledger().rounds[0].events))
        text = RECHECK_RESULT.replace("- r1-02: 해소됨 — mul이 곱한다\n", "")
        ok = review_results(self.rd / "recheck.json", "round-1:recheck", text)
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(ok))
        self.assertIn("status=종료", p.stdout)
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("| Claude 재검토 | 1 |", p.stdout)
        events = [e for e in self.ledger().rounds[0].events if e["step"] == "recheck"]
        self.assertEqual((events[0]["runner"], events[0]["model"]), ("claude", FABLE))
        self.assertEqual(events[0]["fallback"], {"from": "codex", "to": "claude", "reason": "이용 한도"})
        self.assertEqual(self.cell("code.recheck")["runner"], "codex")

    def test_recheck_auth_still_stops(self):
        self.to_adjudicate("--profile", "codex-recheck")
        self.opinion_workflow()
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.failed_recheck(AUTH_LOG)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("codex login", p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [])

    def test_legacy_ledger_without_profile_still_falls_back(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        path = self.run_dir / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(profile={}, reviewer="codex", review_model="", review_effort="")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.rd = self.run_dir / "round-1"
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.rd / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        adj = self.rd / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        p = self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        self.assertIn("--opinion", p.stdout)
        p = self.failed_opinion(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ledger = self.ledger()
        self.assertEqual((ledger.profile["name"], ledger.profile["source"]), ("legacy", "legacy"))
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["runner"], "codex")
        self.assertEqual(ledger.profile["roles"]["code.recheck"]["runner"], "codex")
        self.assertEqual(self.current("opinion")["runner"], "claude")
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "claude", "reason": "이용 한도"}])
        self.assertIn("Workflow 실행", self.next_steps(p)[0])


FABLE_CELL = {"runner": "claude", "model": "claude-fable-5-1", "effort": "high"}
FIRST_REVIEW_PROFILES = """
[profiles.split]
"code.review" = { runner = "claude", model = "claude-sonnet-5-5", effort = "low" }

[profiles.cursor-first]
"code.review" = { runner = "cursor", model = "composer-9" }

[profiles.codex-then-cursor-first]
"code.review" = { runner = "codex", fallback = { runner = "cursor", model = "composer-9" } }
"""
OPUS = "claude-opus-5-5"


class FirstReviewTest(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8") + FIRST_REVIEW_PROFILES, encoding="utf-8")
        self.use_fakes()
        self.rr, self.cr = self.tmp / "run-review.sh", self.tmp / "cursor-run-review.sh"
        for script in (self.rr, self.cr):
            script.write_text("#!/bin/sh\n", encoding="utf-8")
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.rr), "CASCADE_CURSOR_REVIEW": str(self.cr)})

    def review(self, *extra, kind="code", scope=("--base", "main"), env=None):
        return run_cli(["review", "--kind", kind, *scope, *extra], self.repo, env={**self.env, **(env or {})})

    def review_dir(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        line = next(x for x in p.stdout.splitlines() if x.startswith("review_dir="))
        return Path(line.split("=", 1)[1])

    def meta(self, review_dir):
        return json.loads((review_dir / "review-meta.json").read_text(encoding="utf-8"))

    def next_lines(self, p):
        lines = p.stdout.splitlines()
        return [x.strip() for x in lines[lines.index("다음:") + 1:]]

    def init_line(self, p):
        return next(x for x in self.next_lines(p) if " init --source " in x)

    def test_review_with_local_prints_rr(self):
        p = self.review("--profile", "local")
        review_dir = self.review_dir(p)
        self.assertFalse((review_dir / "review-args.json").exists())
        self.assertIn("run-review.sh --base main", p.stdout)
        self.assertIn(f"--repo {self.repo}", p.stdout)
        self.assertIn(f"--out {review_dir / 'review.out'}", p.stdout)
        self.assertNotIn("--model", p.stdout)
        self.assertIn("--source codex-code", p.stdout)
        self.assertIn(f"--input {review_dir / 'review.out.final'}", p.stdout)
        self.assertIn("--profile local", p.stdout)
        self.assertIn("검토자: Codex", p.stdout)
        steps = self.next_lines(p)
        self.assertTrue(steps[0].startswith(f"cd {self.repo} && {self.rr} --base main"), steps)
        self.assertEqual(self.meta(review_dir), {
            "role": "code.review", "requested": {"runner": "codex", "model": "", "effort": "", "fallback": FABLE_CELL},
            "actual": {"runner": "codex", "model": "", "effort": ""}, "fallback_reason": None, "attempts": [],
            "consented": ["codex"],
            "args": {"kind": "code", "base": "main", "uncommitted": False, "commit": None, "docs": [], "focus": "",
                     "root": str(self.repo)},
            "profile": "local", "explicit": True})

    def test_review_doc_with_local_prints_rr_with_docs(self):
        (self.repo / "plan.md").write_text("# 계획\n", encoding="utf-8")
        p = self.review("--profile", "local", "--focus", "순서", kind="doc", scope=("--doc", str(self.repo / "plan.md")))
        review_dir = self.review_dir(p)
        command, init = self.next_lines(p)[:2]
        self.assertTrue(command.startswith(f"{self.rr} --doc {self.repo / 'plan.md'}"), command)
        self.assertIn("--focus", command)
        self.assertIn("--source codex-doc", init)
        self.assertIn(f"--doc {self.repo / 'plan.md'}", init)
        self.assertEqual(self.meta(review_dir)["role"], "doc.review")
        self.assertEqual(self.meta(review_dir)["args"]["docs"], [str(self.repo / "plan.md")])

    def test_codex_code_review_warns_that_focus_is_dropped(self):
        p = self.review("--profile", "local", "--focus", "보안")
        self.assertIn("경고: Codex 코드 검토는 --focus를 받지 못한다", p.stdout)
        self.assertNotIn("--focus", self.next_lines(p)[0])
        p = self.review("--profile", "cursor-first", "--focus", "보안")
        self.assertNotIn("경고:", p.stdout)
        self.assertIn("--focus", self.next_lines(p)[0])

    def test_review_cursor_cell_prints_cr_with_model(self):
        p = self.review("--profile", "cursor-first", "--uncommitted", scope=())
        command = self.next_lines(p)[0]
        self.assertTrue(command.startswith(str(self.cr) + " --uncommitted"), command)
        self.assertIn("--model composer-9", command)
        self.assertIn("--source cursor-code", p.stdout)
        self.assertIn("검토자: Cursor", p.stdout)

    def test_cursor_model_overrides_the_cursor_first_review_and_its_fallback(self):
        p = self.review("--profile", "cursor-first", "--cursor-model", "composer-2.5", "--uncommitted", scope=())
        self.assertIn("--model composer-2.5", self.next_lines(p)[0])
        self.assertIn("--cursor-model composer-2.5", self.init_line(p))
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first", "--cursor-model", "composer-2.5"))
        p = run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex"], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("--model composer-2.5", self.next_lines(p)[0])

    def test_review_model_rejected_for_external_cell(self):
        for option in (["--review-model", "claude-sonnet-5-5"], ["--review-effort", "low"]):
            with self.subTest(option=option):
                p = self.review("--profile", "local", *option)
                self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertIn("code.review", p.stdout)
                self.assertIn(option[0], p.stdout)

    def test_review_model_for_claude_cell_is_validated(self):
        p = self.review("--profile", "split", "--review-model", "composer-2.5")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("--review-model composer-2.5", p.stdout)

    def test_explicit_profile_does_not_propagate_review_model(self):
        p = self.review("--profile", "split")
        review_dir = self.review_dir(p)
        [job] = json.loads((review_dir / "review-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["model"], job["effort"]), (SONNET, "low"))
        init_line = self.init_line(p)
        self.assertNotIn("--review-model", init_line)
        self.assertNotIn("--review-effort", init_line)
        self.assertIn("--profile split", init_line)
        self.assertIn("--source claude-code", init_line)
        self.assertIn(f"--input {review_dir / 'review.json'}", init_line)
        self.assertEqual(self.meta(review_dir)["actual"], {"runner": "claude", "model": SONNET, "effort": "low"})

    def test_explicit_profile_review_option_overrides_cell_but_is_not_propagated(self):
        p = self.review("--profile", "split", "--review-model", OPUS, "--review-effort", "high")
        review_dir = self.review_dir(p)
        [job] = json.loads((review_dir / "review-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["model"], job["effort"]), (OPUS, "high"))
        self.assertNotIn("--review-model", self.init_line(p))
        self.assertNotIn("--review-effort", self.init_line(p))
        self.assertEqual(self.meta(review_dir)["requested"],
                         {"runner": "claude", "model": OPUS, "effort": "high", "fallback": FABLE_CELL})

    def test_env_profile_is_explicit_too(self):
        p = self.review("--review-model", OPUS, env={"CASCADE_PROFILE": "split"})
        init_line = self.init_line(p)
        self.assertIn("--profile split", init_line)
        self.assertNotIn("--review-model", init_line)
        self.assertTrue(self.meta(self.review_dir(p))["explicit"])

    def test_init_from_claude_first_review_keeps_the_opinion_cell(self):
        p = self.review("--profile", "split")
        review_dir = self.review_dir(p)
        review_results(review_dir / "review.json", "review:reviewer", CLAUDE_REVIEW, SONNET)
        q = run_cli(["init", "--source", "claude-code", "--input", str(review_dir / "review.json"), "--profile", "split"],
                    self.repo, env=self.env)
        self.assertEqual(q.returncode, 0, q.stdout + q.stderr)
        self.assertNotIn("경고", q.stdout)
        run_dir = Path(next(x for x in q.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual((ledger["reviewer"], ledger["review_model"]), ("claude", FABLE))
        self.assertEqual(ledger["profile"]["roles"]["code.review"]["model"], SONNET)
        self.assertEqual(ledger["profile"]["first_review"]["actual"]["model"], SONNET)

    def test_default_profile_keeps_old_propagation(self):
        p = self.review("--review-model", SONNET, "--review-effort", "low")
        init_line = self.init_line(p)
        self.assertIn(f"--review-model {SONNET}", init_line)
        self.assertIn("--review-effort low", init_line)
        self.assertNotIn("--profile", init_line)
        meta = self.meta(self.review_dir(p))
        self.assertEqual((meta["profile"], meta["explicit"]), ("default", False))
        self.assertEqual(meta["requested"], {"runner": "claude", "model": SONNET, "effort": "low", "fallback": FABLE_CELL})

    def test_missing_script_substitutes_the_fallback_and_says_so(self):
        p = self.review("--profile", "local", env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        review_dir = self.review_dir(p)
        self.assertIn("대체: code.review codex → claude", p.stdout)
        self.assertTrue((review_dir / "review-args.json").is_file())
        self.assertIn("--source claude-code", self.init_line(p))
        meta = self.meta(review_dir)
        self.assertEqual((meta["requested"]["runner"], meta["actual"]["runner"]), ("codex", "claude"))
        self.assertEqual(meta["attempts"], [{"runner": "codex", "reason": meta["fallback_reason"]}])
        self.assertTrue(meta["fallback_reason"])

    def test_unavailable_cli_substitutes_the_fallback(self):
        p = self.review("--profile", "local", env={"CASCADE_CODEX": str(self.tmp / "no-such-cli")})
        self.assertIn("대체: code.review codex → claude — 미설치 또는 로그인 필요", p.stdout)
        self.assertEqual(self.meta(self.review_dir(p))["actual"]["runner"], "claude")

    def test_fallback_from_updates_same_dir(self):
        first = self.review_dir(self.review("--profile", "local"))
        other = self.review_dir(self.review("--profile", "local"))
        self.assertNotEqual(first, other)
        p = run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex", "--reason", "한도"], self.repo,
                    env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        meta = self.meta(first)
        self.assertEqual((meta["requested"]["runner"], meta["actual"]["runner"], meta["fallback_reason"]),
                         ("codex", "claude", "한도"))
        self.assertEqual(meta["attempts"], [{"runner": "codex", "reason": "한도"}])
        self.assertEqual(meta["actual"]["model"], FABLE)
        self.assertTrue((first / "review-args.json").exists())
        self.assertFalse((other / "review-args.json").exists())
        self.assertEqual(self.meta(other)["actual"]["runner"], "codex")
        self.assertEqual(self.meta(other)["attempts"], [])
        [job] = json.loads((first / "review-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["model"], job["effort"]), (FABLE, "high"))
        self.assertIn("diff main...HEAD", Path(job["promptPath"]).read_text(encoding="utf-8"))
        init_line = self.init_line(p)
        self.assertIn("--source claude-code", init_line)
        self.assertIn(f"--input {first / 'review.json'}", init_line)
        self.assertIn("--profile local", init_line)
        self.assertIn(f"--root {self.repo}", init_line)
        self.assertIn("한도", p.stdout)

    def test_fallback_from_keeps_the_workers_given_to_the_first_review(self):
        first = self.review_dir(self.review("--profile", "local", "--workers", "external", "--cursor-model", "composer-test"))
        p = run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex"], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("--profile local --workers external --cursor-model composer-test", self.init_line(p))

    def test_review_keeps_a_given_default_workers_choice(self):
        p = self.review("--profile", "local", "--workers", "claude")
        self.assertIn("--profile local --workers claude", self.init_line(p))

    def test_fallback_from_reuses_the_original_scope_not_the_cwd(self):
        first = self.review_dir(self.review("--profile", "local", "--commit", "HEAD", scope=()))
        p = run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex", "--reason", "한도"], self.tmp,
                    env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [job] = json.loads((first / "review-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertIn(str(self.repo), Path(job["promptPath"]).read_text(encoding="utf-8"))
        self.assertIn(f"--root {self.repo}", self.init_line(p))

    def test_fallback_to_a_cursor_cell_prints_cr(self):
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first"))
        p = run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex", "--reason", "오류"], self.repo,
                    env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((first / "review-args.json").exists())
        command = self.next_lines(p)[0]
        self.assertTrue(command.startswith(str(self.cr) + " --base main"), command)
        self.assertIn("--model composer-9", command)
        self.assertIn(f"--out {first / 'review.out'}", command)
        self.assertIn("--source cursor-code", self.init_line(p))
        meta = self.meta(first)
        self.assertEqual((meta["requested"]["runner"], meta["actual"]["runner"], meta["actual"]["model"]),
                         ("codex", "cursor", "composer-9"))
        self.assertEqual(meta["attempts"], [{"runner": "codex", "reason": "오류"}])

    def test_fallback_to_a_cursor_cell_without_its_script_ends_at_claude(self):
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first"))
        p = run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex", "--reason", "오류"], self.repo,
                    env={**self.env, "CASCADE_CURSOR_REVIEW": str(self.tmp / "none.sh")})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        meta = self.meta(first)
        self.assertEqual(meta["actual"]["runner"], "claude")
        self.assertEqual([a["runner"] for a in meta["attempts"]], ["codex", "cursor"])
        self.assertTrue((first / "review-args.json").is_file())

    def test_fallback_from_errors(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        first = self.review_dir(self.review("--profile", "local"))
        cases = (
            ["--fallback-from", "codex"],
            ["--review-dir", str(empty), "--fallback-from", "codex"],
            ["--review-dir", str(self.tmp / "missing"), "--fallback-from", "codex"],
            ["--review-dir", str(first), "--fallback-from", "cursor"],
            ["--review-dir", str(first), "--fallback-from", "claude"],
            ["--review-dir", str(first)],
        )
        for extra in cases:
            with self.subTest(extra=extra):
                p = run_cli(["review", *extra, "--reason", "x"], self.repo, env=self.env)
                self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(self.meta(first)["attempts"], [])

    def fallback(self, review_dir, runner, reason="한도", cwd=None):
        return run_cli(["review", "--review-dir", str(review_dir), "--fallback-from", runner, "--reason", reason],
                       cwd or self.repo, env=self.env)

    def test_partial_consent_sends_the_first_review_fallback_to_claude(self):
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first", "--consent", "codex"))
        meta = self.meta(first)
        self.assertEqual(meta["consented"], ["codex"])
        self.assertEqual(meta["requested"]["fallback"]["runner"], "claude")
        p = self.fallback(first, "codex")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.meta(first)["actual"]["runner"], "claude")
        self.assertTrue((first / "review-args.json").is_file())
        self.assertNotIn(str(self.cr), p.stdout)

    def test_consent_none_makes_the_first_review_claude(self):
        p = self.review("--profile", "local", "--consent", "none")
        first = self.review_dir(p)
        self.assertIn("대체: code.review codex → claude — 전송 동의 없음", p.stdout)
        self.assertTrue((first / "review-args.json").is_file())
        self.assertNotIn(str(self.rr), p.stdout)
        meta = self.meta(first)
        self.assertEqual((meta["requested"]["runner"], meta["actual"]["runner"], meta["fallback_reason"], meta["consented"]),
                         ("codex", "claude", "전송 동의 없음", []))
        self.assertEqual(meta["attempts"], [{"runner": "codex", "reason": "전송 동의 없음"}])

    def test_consent_without_first_review_recipient_keeps_claude_cell(self):
        p = self.review("--profile", "split", "--consent", "none")
        meta = self.meta(self.review_dir(p))
        self.assertEqual((meta["actual"]["model"], meta["attempts"], meta["consented"]), (SONNET, [], []))

    def test_consent_covering_the_fallback_allows_it(self):
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first", "--consent", "codex,cursor"))
        self.assertEqual(self.meta(first)["consented"], ["codex", "cursor"])
        self.assertEqual(self.fallback(first, "codex").returncode, 0)
        self.assertEqual(self.meta(first)["actual"]["runner"], "cursor")

    def test_missing_cli_does_not_reach_an_unconsented_fallback(self):
        p = self.review("--profile", "codex-then-cursor-first", "--consent", "codex",
                        env={"CASCADE_CODEX": str(self.tmp / "no-such-cli")})
        meta = self.meta(self.review_dir(p))
        self.assertEqual(meta["actual"]["runner"], "claude")
        self.assertEqual([a["runner"] for a in meta["attempts"]], ["codex"])

    def test_bad_consent_value_is_rejected(self):
        p = self.review("--profile", "local", "--consent", "openai")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("openai", p.stdout)

    def test_review_next_init_line_carries_consent_option(self):
        for option, text in (("codex", "--consent codex"), ("codex,cursor", "--consent codex,cursor"),
                             ("none", "--consent none")):
            p = self.review("--profile", "local", "--consent", option)
            self.assertIn(f" {text}", self.init_line(p), (option, p.stdout))

    def test_review_next_init_line_has_no_consent_option_when_not_given(self):
        p = self.review("--profile", "local")
        self.assertNotIn("--consent", self.init_line(p))
        first = self.review_dir(p)
        self.assertNotIn("consent", self.meta(first))

    def test_consent_is_carried_on_the_claude_first_review_init_line_too(self):
        p = self.review("--profile", "split", "--consent", "none")
        self.assertIn(" --profile split --consent none", self.init_line(p))

    def test_fallback_init_line_keeps_the_consent_given_to_the_first_review(self):
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first", "--consent", "codex"))
        p = self.fallback(first, "codex")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(" --consent codex", self.init_line(p))
        self.assertEqual(self.meta(first)["consent"], ["codex"])

    def test_consent_option_on_review_reaches_init_unchanged(self):
        p = self.review("--profile", "local", "--consent", "none")
        line = shlex.split(self.init_line(p))
        self.assertEqual(line[line.index("--consent") + 1], "none")

    def test_codex_code_command_enters_the_root_even_with_a_space(self):
        spaced = make_repo(self.tmp / "my repo", {"x.py": "x = 1\n"})
        (self.tmp / "t2").mkdir()
        toml = write_repos_toml(self.tmp / "t2", {str(spaced): {"tests": [], "format": []}})
        toml.write_text(toml.read_text(encoding="utf-8") + FIRST_REVIEW_PROFILES, encoding="utf-8")
        p = run_cli(["review", "--kind", "code", "--base", "main", "--profile", "local", "--root", str(spaced)], self.tmp,
                    env={**self.env, "CASCADE_REPOS_TOML": str(toml)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.next_lines(p)[0].startswith(f"cd {shlex.quote(str(spaced))} && {self.rr} --base main"),
                        self.next_lines(p)[0])
        self.assertIn(f"--root {shlex.quote(str(spaced))}", self.init_line(p))

    def test_fallback_chain_codex_cursor_claude_and_no_duplicates(self):
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first"))
        self.assertEqual(self.fallback(first, "codex", "한도").returncode, 0)
        self.assertEqual(self.meta(first)["actual"]["runner"], "cursor")
        again = self.fallback(first, "codex", "한도")
        self.assertEqual(again.returncode, 5, again.stdout + again.stderr)
        self.assertEqual(len(self.meta(first)["attempts"]), 1)
        p = self.fallback(first, "cursor", "오류")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        meta = self.meta(first)
        self.assertEqual((meta["requested"]["runner"], meta["actual"]["runner"], meta["fallback_reason"]),
                         ("codex", "claude", "오류"))
        self.assertEqual(meta["attempts"], [{"runner": "codex", "reason": "한도"}, {"runner": "cursor", "reason": "오류"}])
        self.assertTrue((first / "review-args.json").is_file())
        self.assertEqual(self.fallback(first, "cursor").returncode, 5)
        self.assertEqual(self.fallback(first, "claude").returncode, 5)

    def test_failed_fallback_leaves_meta_untouched(self):
        first = self.review_dir(self.review("--profile", "local"))
        before = (first / "review-meta.json").read_text(encoding="utf-8")
        p = run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex", "--reason", "한도",
                     "--review-model", "composer-2.5"], self.repo, env=self.env)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertEqual((first / "review-meta.json").read_text(encoding="utf-8"), before)
        self.assertFalse((first / "review-args.json").exists())

    def test_fallback_uses_the_recorded_fallback_not_the_current_repos_toml(self):
        first = self.review_dir(self.review("--profile", "codex-then-cursor-first"))
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8").replace('runner = "cursor", model = "composer-9"',
                                                                 'runner = "claude", model = "claude-haiku-5-5"'),
                        encoding="utf-8")
        p = self.fallback(first, "codex")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.meta(first)["actual"]["runner"], self.meta(first)["actual"]["model"]), ("cursor", "composer-9"))
        toml.write_text("[repos]\n", encoding="utf-8")
        self.assertEqual(self.fallback(first, "cursor").returncode, 0)
        self.assertEqual(self.meta(first)["actual"]["runner"], "claude")

    def test_code_review_with_doc_option_keeps_doc_in_the_init_line(self):
        (self.repo / "plan.md").write_text("# 계획\n", encoding="utf-8")
        p = self.review("--doc", str(self.repo / "plan.md"))
        self.assertIn(f"--doc {self.repo / 'plan.md'}", self.init_line(p))
        p = self.review("--profile", "local", "--doc", str(self.repo / "plan.md"))
        self.assertIn(f"--doc {self.repo / 'plan.md'}", self.init_line(p))
        self.assertNotIn("--doc", self.next_lines(p)[0])

    def test_review_without_kind_is_still_a_usage_error(self):
        p = run_cli(["review", "--base", "main"], self.repo, env=self.env)
        self.assertEqual(p.returncode, 2)

    def test_init_carries_first_review_meta(self):
        first = self.review_dir(self.review("--profile", "local"))
        run_cli(["review", "--review-dir", str(first), "--fallback-from", "codex", "--reason", "한도"], self.repo,
                env=self.env)
        review_results(first / "review.json", "review:reviewer", CLAUDE_REVIEW, FABLE)
        p = run_cli(["init", "--source", "claude-code", "--input", str(first / "review.json"), "--profile", "local"],
                    self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["profile"]["first_review"], {
            "role": "code.review", "requested": {"runner": "codex", "model": "", "effort": ""},
            "actual": {"runner": "claude", "model": FABLE, "effort": "high"}, "fallback_reason": "한도",
            "attempts": [{"runner": "codex", "reason": "한도"}]})

    def test_init_carries_the_meta_of_an_external_first_review_without_fallback(self):
        first = self.review_dir(self.review("--profile", "local"))
        shutil.copyfile(FIXTURES / "codex-code-e2e.final", first / "review.out.final")
        p = run_cli(["init", "--source", "codex-code", "--input", str(first / "review.out.final"), "--profile", "local"],
                    self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        first_review = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))["profile"]["first_review"]
        self.assertEqual((first_review["requested"]["runner"], first_review["actual"]["runner"],
                          first_review["fallback_reason"], first_review["attempts"]), ("codex", "codex", None, []))

    def test_init_without_meta_keeps_first_review_empty(self):
        p = run_cli(["init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final")], self.repo,
                    env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertEqual(json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))["profile"]["first_review"], {})

    def test_init_ignores_a_meta_of_the_other_mode(self):
        first = self.review_dir(self.review("--profile", "local"))
        meta = self.meta(first)
        meta["role"] = "doc.review"
        (first / "review-meta.json").write_text(json.dumps(meta), encoding="utf-8")
        shutil.copyfile(FIXTURES / "codex-code-e2e.final", first / "review.out.final")
        p = run_cli(["init", "--source", "codex-code", "--input", str(first / "review.out.final")], self.repo, env=self.env)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertEqual(json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))["profile"]["first_review"], {})


class FirstReviewCommandTest(unittest.TestCase):
    def test_first_review_command_per_runner_and_scope(self):
        args = {"kind": "code", "base": "main", "uncommitted": False, "commit": None, "docs": [], "focus": "", "root": "/r p"}
        codex = {"runner": "codex", "model": "", "effort": ""}
        cursor = {"runner": "cursor", "model": "composer-9", "effort": ""}
        command, result = review.first_review_command(codex, args, Path("/d"))
        self.assertEqual(command, f"cd '/r p' && {shlex.quote(str(cli_steps.RUN_REVIEW))} --base main --repo '/r p' "
                                  "--out /d/review.out")
        self.assertEqual(result, "/d/review.out.final")
        command, _ = review.first_review_command(cursor, {**args, "base": None, "commit": "abc123"}, Path("/d"))
        self.assertEqual(command, f"{shlex.quote(str(cli_steps.CURSOR_REVIEW))} --commit abc123 --repo '/r p' "
                                  "--out /d/review.out --model composer-9")
        command, _ = review.first_review_command(codex, {**args, "base": None, "uncommitted": True, "focus": "보안"},
                                                 Path("/d"))
        self.assertTrue(command.startswith("cd '/r p' && "), command)
        self.assertNotIn("--focus", command)
        docs = {**args, "kind": "doc", "base": None, "docs": ["/a.md", "/b c.md"]}
        command, _ = review.first_review_command(codex, docs, Path("/d"))
        self.assertIn(" --doc /a.md --doc '/b c.md' --repo ", command)
        self.assertNotIn("cd ", command)


class ReviewHelpersTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def ledger(self, reviewer="codex", profile=None):
        return Ledger(run_id="x", source="codex-code", mode="code", root="/r", snapshot={}, dry_run=False,
                      reviewer=reviewer, profile=profile or {})

    def profile(self, fallback=None, consented=("codex", "cursor")):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.opinion"] = {**profiles.reset_cell("code.opinion", "codex"),
                                 "fallback": fallback or roles["code.opinion"]["fallback"]}
        return {"name": "t", "source": "--profile", "explicit": True, "roles": roles, "overrides": [], "substituted": [],
                "consented": list(consented), "first_review": {}}

    def result(self, log_text=None, final=None):
        out = self.tmp / "r.out"
        final_path = Path(f"{out}.final")
        if log_text is not None:
            out.write_text(log_text, encoding="utf-8")
        if final is not None:
            final_path.write_text(final, encoding="utf-8")
        return final_path

    def test_external_failure_classification(self):
        self.assertIsNone(cli_steps.external_failure(self.result()))
        self.assertIsNone(cli_steps.external_failure(self.result(final="")))
        self.assertIsNone(cli_steps.external_failure(self.result(LIMIT_LOG, final="의견")))
        self.assertEqual(cli_steps.external_failure(self.result(LIMIT_LOG, final="")), "limit")
        self.assertEqual(cli_steps.external_failure(self.result(ERROR_LOG, final="\n")), "error")
        self.assertEqual(cli_steps.external_failure(self.result(AUTH_LOG, final="")), "auth")
        self.assertEqual(cli_steps.external_failure(self.result("Authentication required\n" + LIMIT_LOG)), "auth")
        self.assertIsNone(cli_steps.external_failure(self.tmp / "plain.txt"))

    def test_external_failure_reads_only_the_log_tail(self):
        prompt = "\n".join(["판정 의견 요청", "calc.py:401 add가 틀리다", "@@ -401,3 +401,3 @@", "unauthorized 접근을 막는다",
                            "refresh token을 다시 쓴다", "usage limit 문구를 다룬다"] + ["본문"] * 30)
        self.assertEqual(cli_steps.external_failure(self.result(prompt + "\nYou've hit your usage limit\n", final="")), "limit")
        self.assertEqual(cli_steps.external_failure(self.result(prompt + "\nERROR: stream disconnected\n", final="")), "error")
        self.assertEqual(cli_steps.external_failure(
            self.result(prompt + "\nERROR: unexpected status 401 Unauthorized\n", final="")), "auth")
        noisy_tail = "\n".join(["ERROR: stream disconnected"] + ["진행"] * 25)
        self.assertEqual(cli_steps.external_failure(self.result("401 Unauthorized\n" + noisy_tail, final="")), "error")

    def test_review_cell_reads_the_mode_cell_and_materializes_legacy(self):
        ledger = self.ledger(reviewer="cursor")
        self.assertEqual(cli_steps.review_cell(ledger, "opinion")["runner"], "cursor")
        self.assertEqual(cli_steps.review_cell(ledger, "recheck")["runner"], "cursor")
        self.assertEqual(ledger.profile, {})
        ledger = self.ledger(profile=self.profile())
        self.assertEqual(cli_steps.review_cell(ledger, "opinion")["runner"], "codex")
        self.assertEqual(cli_steps.review_cell(ledger, "recheck")["runner"], "claude")
        self.assertTrue(cli_steps.is_claude(ledger, "recheck"))
        self.assertFalse(cli_steps.is_claude(ledger, "opinion"))
        self.assertEqual(cli_steps.review_script(ledger, "opinion"), cli_steps.RUN_REVIEW)
        ledger.profile["roles"]["code.opinion"]["runner"] = "cursor"
        self.assertEqual(cli_steps.review_script(ledger, "opinion"), cli_steps.CURSOR_REVIEW)

    def test_substitute_review_records_runtime_substitution_and_keeps_assignment(self):
        ledger = self.ledger(profile=self.profile())
        new = cli_steps.substitute_review(ledger, "opinion", "이용 한도", 2)
        self.assertEqual((new["runner"], new["model"], new["effort"]), ("claude", FABLE, "high"))
        self.assertEqual(ledger.profile["runtime_substitutions"],
                         [{"role": "code.opinion", "from": "codex", "to": "claude", "cell": new, "reason": "이용 한도",
                           "round": 2}])
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["runner"], "codex")
        self.assertEqual(ledger.profile["substituted"], [])
        self.assertEqual(cli_steps.review_cell(ledger, "opinion"), new)
        self.assertEqual(cli_steps.review_cell(ledger, "recheck")["runner"], "claude")
        self.assertEqual(cli_steps.review_event_extra(ledger, "opinion"),
                         {"runner": "claude", "model": FABLE,
                          "fallback": {"from": "codex", "to": "claude", "reason": "이용 한도"}})
        self.assertEqual((ledger.reviewer, ledger.review_model), ("codex", ""))
        self.assertEqual(ledger.profile["consented"], ["codex", "cursor"])

    def test_substitute_review_materializes_a_legacy_ledger(self):
        ledger = self.ledger(reviewer="codex")
        new = cli_steps.substitute_review(ledger, "recheck", "실행 오류")
        self.assertEqual(new["runner"], "claude")
        self.assertEqual(ledger.profile["name"], "legacy")
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["runner"], "codex")
        self.assertEqual(ledger.profile["roles"]["code.recheck"]["runner"], "codex")
        self.assertEqual(cli_steps.review_cell(ledger, "recheck")["runner"], "claude")
        self.assertEqual([(r["role"], r["from"], r["to"], r["reason"]) for r in ledger.profile["runtime_substitutions"]],
                         [("code.recheck", "codex", "claude", "실행 오류")])
        self.assertEqual(ledger.profile["substituted"], [])
        self.assertEqual(ledger.reviewer, "codex")

    def test_report_labels_opinions_without_recorded_runner_by_the_current_cell(self):
        ledger = self.ledger(reviewer="cursor")
        ledger.opinions["r1-01"] = "의견"
        ledger.rounds = [RoundRecord(no=1, findings=[], decisions=[], adopted=[], recheck_opened=True)]
        report = render(ledger, False)
        self.assertIn("검토자: Cursor", report)
        self.assertIn("| Cursor 의견 | 1 |", report)
        self.assertIn("| Cursor 재검토 | 1 |", report)

    def test_latest_runtime_substitution_wins(self):
        cursor = {"runner": "cursor", "model": "composer-9", "effort": ""}
        ledger = self.ledger(profile=self.profile(fallback=cursor))
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "a")["runner"], "cursor")
        self.assertEqual(cli_steps.review_cell(ledger, "opinion")["runner"], "cursor")
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "b")["runner"], "claude")
        self.assertEqual(cli_steps.review_cell(ledger, "opinion")["runner"], "claude")
        self.assertEqual([r["from"] for r in ledger.profile["runtime_substitutions"]], ["codex", "cursor"])

    def test_substitute_review_respects_consent_and_exhaustion(self):
        cursor = {"runner": "cursor", "model": "composer-9", "effort": ""}
        ledger = self.ledger(profile=self.profile(fallback=cursor))
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "x")["runner"], "cursor")
        self.assertEqual(cli_steps.review_cell(ledger, "opinion")["fallback"]["runner"], "claude")
        ledger = self.ledger(profile=self.profile(fallback=cursor, consented=("codex",)))
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "x")["runner"], "claude")
        same = {"runner": "codex", "model": "", "effort": ""}
        ledger = self.ledger(profile=self.profile(fallback=same))
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "x")["runner"], "claude")


ADJUDICATOR_PROFILES = """
[profiles.sonnet-adj]
base = "claude-only"
"code.adjudicator" = { model = "claude-sonnet-5-5", effort = "low" }
"""


class AdjudicatorCellTest(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8") + ADJUDICATOR_PROFILES, encoding="utf-8")
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.tmp / "none.sh"),
                         "CASCADE_CURSOR_REVIEW": str(self.tmp / "none-cursor.sh")})

    def to_judge(self, *init_extra):
        p = self.init(*init_extra)
        self.rd = self.run_dir / "round-1"
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.rd / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        return p

    def adj_jobs(self):
        return json.loads((self.rd / "adj-args.json").read_text(encoding="utf-8"))["jobs"]

    def adjudicate(self, model_id):
        data = json.loads((FIXTURES / "workflow-adj.json").read_text(encoding="utf-8"))
        data["results"][0]["output"]["model_id"] = model_id
        adj = self.rd / "adj.json"
        adj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))

    def test_adjudicator_cell_model_and_effort_drive_the_job(self):
        p = self.to_judge("--profile", "sonnet-adj")
        self.assertIn("판가름=Claude(Sonnet 5.5 low)", p.stdout)
        self.assertEqual([(j["id"], j["model"], j["effort"]) for j in self.adj_jobs()],
                         [("r1-02:adjudicator", SONNET, "low")])
        p = self.adjudicate(SONNET)
        self.assertNotIn("r1-02:adjudicator 모델 ID", p.stdout)

    def test_adjudicate_model_warning_expects_the_cell_model(self):
        self.to_judge("--profile", "sonnet-adj")
        p = self.adjudicate("claude-opus-5-5")
        self.assertIn("r1-02:adjudicator 모델 ID가 claude-opus-5-5 이다(기대값 claude-sonnet-5-5)", p.stdout)

    def test_default_profile_adjudicator_job_is_unchanged(self):
        self.to_judge()
        [job] = self.adj_jobs()
        self.assertEqual(sorted(job), ["effort", "id", "kind", "label", "model", "promptPath"])
        self.assertEqual((job["id"], job["model"], job["effort"], job["kind"]),
                         ("r1-02:adjudicator", "claude-opus-5-5", "high", "adjudicator"))
        p = self.adjudicate("claude-opus-5-5")
        self.assertNotIn("모델 ID", p.stdout)


BAD_AUTO_PROFILE = """
[profiles.bad-auto]
"code.review" = { runner = "auto", model = "gpt-5" }
"""


class FirstReviewProfileErrorTest(ExternalWorkersBase):
    def test_cf_review_with_bad_auto_cell_exits_5_without_traceback(self):
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8") + BAD_AUTO_PROFILE, encoding="utf-8")
        self.env.update(NO_LEGACY_ENV)
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        p = self.cli("review", "--kind", "code", "--uncommitted", "--profile", "bad-auto", ok=False)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("code.review", p.stdout)

    def test_first_review_cell_wraps_profile_error_as_exit_5(self):
        from cascade import cli
        roles = profiles.builtin("default")["roles"]
        roles["code.review"] = {**roles["code.review"], "model": "gpt-5"}
        resolved = profiles.Resolved("bad-auto", "--profile", True, False, roles, [], [])
        with self.assertRaises(cli_steps.CliError) as ctx:
            cli._first_review_cell(resolved, "code")
        self.assertEqual(ctx.exception.code, 5)
        self.assertIn("code.review", ctx.exception.message)


class InitReviewScriptTest(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.tmp / "none.sh"),
                         "CASCADE_CURSOR_REVIEW": str(self.tmp / "none-cursor.sh")})

    def test_local_without_rr_script_substitutes_opinion_and_recheck(self):
        p = self.init("--profile", "local")
        profile = self.ledger().profile
        for key in ("code.opinion", "code.recheck", "doc.opinion", "doc.recheck"):
            self.assertEqual(profile["roles"][key]["runner"], "claude", key)
            self.assertIn({"role": key, "from": "codex", "to": "claude", "reason": "검토 스크립트 없음"},
                          profile["substituted"])
        self.assertEqual(profile["roles"]["code.a"]["runner"], "codex")
        self.assertEqual(self.ledger().reviewer, "claude")
        self.assertIn("대체: code.opinion codex → claude — 검토 스크립트 없음", p.stdout)
        self.assertIn("대체: code.recheck codex → claude — 검토 스크립트 없음", p.stdout)
        self.assertIn("의견=Claude(Fable 5.1 high), 재검토=Claude(Fable 5.1 high)", p.stdout)
        self.assertIn("전송 동의: A → OpenAI", p.stdout)
        self.assertNotIn("스킬이 없다", p.stdout)

    def test_cursor_fallback_without_cr_script_is_skipped(self):
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8") + (
            '\n[profiles.codex-then-cursor]\n'
            '"code.opinion" = { runner = "codex", fallback = { runner = "cursor", model = "composer-9" } }\n'),
            encoding="utf-8")
        rr = self.tmp / "run-review.sh"
        rr.write_text("#!/bin/sh\n", encoding="utf-8")
        self.env["CASCADE_RUN_REVIEW"] = str(rr)
        self.init("--profile", "codex-then-cursor")
        profile = self.ledger().profile
        self.assertEqual(profile["roles"]["code.opinion"]["runner"], "codex")
        self.assertEqual(profile["roles"]["code.opinion"]["fallback"]["runner"], "claude")
        self.assertIn({"role": "code.opinion(대체)", "from": "cursor", "to": "claude", "reason": "검토 스크립트 없음"},
                      profile["substituted"])
        self.env["CASCADE_RUN_REVIEW"] = str(self.tmp / "none.sh")
        self.init("--profile", "codex-then-cursor")
        self.assertEqual(self.ledger().profile["roles"]["code.opinion"]["runner"], "claude")

    def test_init_does_not_print_substitution_of_the_finished_first_review(self):
        self.env.pop("CASCADE_CODEX")
        p = self.init("--profile", "local")
        self.assertIn("code.review", [r["role"] for r in self.ledger().profile["substituted"]])
        self.assertNotIn("대체: code.review", p.stdout)
        self.assertIn("대체: code.a codex → claude", p.stdout)

    def test_reviewer_option_keeps_the_missing_script_warning(self):
        p = self.init("--reviewer", "codex")
        ledger = self.ledger()
        self.assertEqual(ledger.reviewer, "codex")
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["runner"], "codex")
        self.assertEqual(ledger.profile["substituted"], [])
        self.assertIn("경고: codex-review 스킬이 없다", p.stdout)
        self.env["CASCADE_REVIEWER"] = "codex"
        p = self.init()
        self.assertEqual(self.ledger().reviewer, "codex")
        self.assertIn("경고: codex-review 스킬이 없다", p.stdout)


if __name__ == "__main__":
    unittest.main()
