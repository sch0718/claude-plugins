import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli, cli_steps, runners  # noqa: E402
from cascade.review import CURSOR_SOURCES, REVIEWER_CHOICES, resolve  # noqa: E402
from test_cli_e2e import AUTH_LOG, FIXTURES, CliCodexResultTest, CliTestBase  # noqa: E402

DOC_FIXTURE = FIXTURES / "codex-doc.final"


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.rr = Path(__file__)
        self.missing = Path("/nonexistent/run-review.sh")

    def test_choices_and_sources(self):
        self.assertEqual(REVIEWER_CHOICES, ("auto", "codex", "cursor", "claude"))
        self.assertEqual(CURSOR_SOURCES, ("cursor-code", "cursor-doc"))

    def test_auto_prefers_codex_then_cursor_then_claude(self):
        self.assertEqual(resolve("auto", "codex-code", self.rr, cursor_ok=True), "codex")
        self.assertEqual(resolve("auto", "codex-code", self.missing, cursor_ok=True), "cursor")
        self.assertEqual(resolve("auto", "codex-code", self.missing, cursor_ok=False), "claude")
        self.assertEqual(resolve("auto", "claude-code", self.rr, cursor_ok=True), "claude")

    def test_cursor_availability_is_checked_only_when_needed(self):
        calls = []

        def probe():
            calls.append(1)
            return True

        self.assertEqual(resolve("cursor", "codex-code", self.missing, probe), "cursor")
        self.assertEqual(resolve("auto", "claude-code", self.missing, probe), "claude")
        self.assertEqual(resolve("auto", "codex-code", self.rr, probe), "codex")
        self.assertEqual(calls, [])
        self.assertEqual(resolve("auto", "codex-code", self.missing, probe), "cursor")
        self.assertEqual(calls, [1])

    def test_explicit_choice_is_kept(self):
        self.assertEqual(resolve("cursor", "claude-doc", self.missing), "cursor")


class CursorAgentLookupTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        saved = {k: os.environ.get(k) for k in ("CASCADE_CURSOR_AGENT", "CASCADE_CURSOR_REVIEW")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])

    def executable(self, name, body=""):
        path = self.tmp / name
        path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_env_overrides_agent_lookup(self):
        script = self.executable("run-review.sh")
        os.environ["CASCADE_CURSOR_REVIEW"] = str(script)
        self.assertEqual(cli_steps.find_cursor_review(), script)
        agent = self.executable("cursor-agent")
        os.environ["CASCADE_CURSOR_AGENT"] = str(agent)
        self.assertEqual(runners.find_cursor_agent(), str(agent))
        os.environ["CASCADE_CURSOR_AGENT"] = str(self.tmp / "nope")
        self.assertIsNone(runners.find_cursor_agent())

    def test_available_needs_script_and_agent(self):
        agent = self.executable("cursor-agent", 'echo "Logged in as tester"\n')
        script = self.executable("run-review.sh")
        os.environ["CASCADE_CURSOR_AGENT"] = str(agent)
        original = cli_steps.CURSOR_REVIEW
        self.addCleanup(setattr, cli_steps, "CURSOR_REVIEW", original)
        cli_steps.CURSOR_REVIEW = script
        self.assertTrue(cli_steps.cursor_reviewer_available())
        cli_steps.CURSOR_REVIEW = self.tmp / "missing.sh"
        self.assertFalse(cli_steps.cursor_reviewer_available())
        cli_steps.CURSOR_REVIEW = script
        os.environ["CASCADE_CURSOR_AGENT"] = str(self.tmp / "nope")
        self.assertFalse(cli_steps.cursor_reviewer_available())


class CursorInputTest(CliTestBase):
    def test_init_cursor_doc_source_parses_sections(self):
        (self.repo / "plan.md").write_text("# 계획\n\n내용\n", encoding="utf-8")
        counts = {}
        for source in ("codex-doc", "cursor-doc"):
            p = self.cli("init", "--source", source, "--input", str(DOC_FIXTURE), "--doc", str(self.repo / "plan.md"))
            counts[source] = next(x for x in p.stdout.splitlines() if x.startswith("지적 "))
            self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertEqual(counts["codex-doc"], counts["cursor-doc"])
        ledger = self.ledger()
        self.assertEqual((ledger.source, ledger.mode), ("cursor-doc", "doc"))
        findings = json.loads((self.run_dir / "round-1" / "findings.json").read_text(encoding="utf-8"))
        self.assertTrue(findings)
        self.assertEqual({f["source"] for f in findings}, {"cursor-doc"})

    def test_init_cursor_code_source_is_code_mode(self):
        raw = self.tmp / "cursor-review.abc.final"
        raw.write_text("Critical\n없음.\n\nWarning\n1. add가 빼기를 한다\n   위치: calc.py:2\n   근거: add(2, 3)이 -1이다\n"
                       "   제안: a + b로 고친다\n\nSuggestion\n없음.\n", encoding="utf-8")
        p = self.cli("init", "--source", "cursor-code", "--input", str(raw))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = self.ledger()
        self.assertEqual((ledger.source, ledger.mode), ("cursor-code", "code"))


class CursorReviewerTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.script = self.tmp / "cursor-run-review.sh"
        self.script.write_text("#!/bin/sh\n", encoding="utf-8")
        self.script.chmod(0o755)
        self.env.update({"CASCADE_CURSOR_REVIEW": str(self.script), "CASCADE_REVIEWER": "cursor"})

    def run_to_opinion(self):
        return CliCodexResultTest.run_to_opinion(self)

    def test_init_announces_cursor_reviewer(self):
        p = self.init()
        self.assertIn(f"의견·재검토: Cursor({self.script})", p.stdout)

    def test_cursor_reviewer_next_step_uses_cursor_script(self):
        log = self.run_to_opinion()
        self.assertEqual(self.ledger().reviewer, "cursor")
        status = self.cli("status", "--run-dir", str(self.run_dir))
        line = next(x.strip() for x in status.stdout.splitlines() if " --opinion " in x)
        self.assertTrue(line.startswith(shlex.quote(str(self.script)) + " --opinion"), line)
        self.assertIn(str(log), line)

    def test_missing_cursor_result_mentions_cursor_login(self):
        log = self.run_to_opinion()
        final = Path(f"{log}.final")
        final.write_text("", encoding="utf-8")
        log.write_text("Authentication required\n", encoding="utf-8")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(final), ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("cursor-agent login", p.stdout)
        self.assertNotIn("codex login", p.stdout)
        self.assertIn("Cursor", p.stdout)
        self.assertIn("인증 실패 흔적", p.stdout)

    def test_removed_final_after_login_expired_rerun_stops_opinion(self):
        log = self.run_to_opinion()
        final = Path(f"{log}.final")
        final.write_text("이전 회차 의견\n", encoding="utf-8")
        final.unlink()
        log.write_text("Not logged in\n", encoding="utf-8")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(final), ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("결과 파일이 없다", p.stdout)
        self.assertIn("cursor-agent login", p.stdout)
        self.assertNotIn("r1-02", self.ledger().opinions)

    def test_cursor_usage_limit_is_pointed_out(self):
        log = self.run_to_opinion()
        final = Path(f"{log}.final")
        log.write_text("You've hit your usage limit\n", encoding="utf-8")
        with self.assertRaises(cli_steps.CliError) as ctx:
            cli._external_result_text(final, "의견", "cursor")
        self.assertIn("이용 한도", str(ctx.exception))
        self.assertNotIn("인증 실패 흔적", str(ctx.exception))

    def test_cursor_usage_limit_falls_back_to_the_cell_fallback(self):
        log = self.run_to_opinion()
        final = Path(f"{log}.final")
        log.write_text("You've hit your usage limit\n", encoding="utf-8")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(final))
        self.assertIn("대체: code.opinion cursor → claude — 이용 한도", p.stdout)
        self.assertNotIn("인증 실패 흔적", p.stdout)
        self.assertEqual(self.ledger().reviewer, "cursor")
        self.assertEqual([r["to"] for r in self.ledger().profile["runtime_substitutions"]], ["claude"])
        self.assertTrue((self.run_dir / "round-1" / "opinion-args.json").is_file())

    def test_empty_final_after_usage_limit_stops_with_limit_guidance(self):
        log = self.run_to_opinion()
        final = Path(f"{log}.final")
        final.write_text("", encoding="utf-8")
        log.write_text('{"type":"result","is_error":true,"result":"You\'ve hit your usage limit"}\n', encoding="utf-8")
        with self.assertRaises(cli_steps.CliError) as ctx:
            cli._external_result_text(final, "의견", "cursor")
        message = str(ctx.exception)
        self.assertIn("비어 있다", message)
        self.assertIn("이용 한도", message)
        self.assertIn("cursor-agent login", message)

    def test_empty_final_after_usage_limit_falls_back_without_attaching_an_opinion(self):
        log = self.run_to_opinion()
        final = Path(f"{log}.final")
        final.write_text("", encoding="utf-8")
        log.write_text('{"type":"result","is_error":true,"result":"You\'ve hit your usage limit"}\n', encoding="utf-8")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(final))
        self.assertIn("대체: code.opinion cursor → claude — 이용 한도", p.stdout)
        self.assertNotIn("r1-02", self.ledger().opinions)

    def test_empty_final_after_login_failure_stops_recheck_with_login_guidance(self):
        final = self.tmp / "recheck.out.final"
        final.write_text("", encoding="utf-8")
        final.with_suffix("").write_text("Authentication required\n", encoding="utf-8")
        with self.assertRaises(cli_steps.CliError) as ctx:
            cli._external_result_text(final, "재검토", "cursor")
        message = str(ctx.exception)
        self.assertIn("재검토 결과 파일이 비어 있다", message)
        self.assertIn("cursor-agent login", message)
        self.assertIn("인증 실패 흔적", message)

    def test_codex_reviewer_message_is_unchanged(self):
        self.env["CASCADE_REVIEWER"] = "codex"
        log = self.run_to_opinion()
        log.write_text(AUTH_LOG, encoding="utf-8")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02",
                     "--result", f"{log}.final", ok=False)
        self.assertIn("Codex 의견 결과 파일이 없다", p.stdout)
        self.assertIn("codex login", p.stdout)
        self.assertNotIn("cursor-agent login", p.stdout)


if __name__ == "__main__":
    unittest.main()
