import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import detect, profiles  # noqa: E402
from helpers import git, make_repo, run_cli, write_repos_toml  # noqa: E402
from test_cli_e2e import FIXTURES, CliTestBase  # noqa: E402

FAKE_GH = f"{sys.executable} {Path(__file__).resolve().parent / 'fake_gh.py'}"


class EstimateTest(unittest.TestCase):
    def test_code_and_doc_counts(self):
        e = detect.estimate(3, "code", "claude")
        self.assertEqual((e["stage1"], e["adjudicator_max"], e["opinion_max"], e["recheck"]), (6, 3, 3, 1))
        self.assertEqual((e["min"], e["max"]), (6, 13))
        e = detect.estimate(2, "doc", "codex")
        self.assertEqual((e["stage1"], e["checker_max"], e["opinion_max"], e["recheck"]), (6, 2, 0, 0))
        self.assertIn("점검자 최대 2", detect.estimate_line(2, "doc", "codex"))

    def test_estimate_line_without_roles_is_unchanged(self):
        self.assertEqual(detect.estimate_line(3, "code", "claude"),
                         "회차당 에이전트 6~13개(작업자 6, 판가름 최대 3, 의견 최대 3, 재검토 1)")

    def test_estimate_line_runner_totals(self):
        line = detect.estimate_line(3, "code", "codex", None, profiles.builtin("local")["roles"])
        self.assertTrue(line.endswith(" — Claude 최대 Haiku 1·Opus 1/지적, Codex 1/지적 + 의견·재검토"), line)

    def test_estimate_line_claude_only_names_every_model(self):
        line = detect.estimate_line(2, "code", "claude", None, profiles.builtin("claude-only")["roles"])
        self.assertTrue(line.endswith(" — Claude 최대 Sonnet 1·Haiku 1·Opus 1/지적 + 의견·재검토"), line)

    def test_estimate_line_doc_counts_writer_and_checker(self):
        roles = profiles.builtin("claude-only")["roles"]
        roles["doc.checker"] = {**roles["doc.checker"], "runner": "cursor", "model": "composer-2.5", "effort": ""}
        line = detect.estimate_line(2, "doc", "claude", None, roles)
        self.assertIn("Claude 최대 Sonnet 2·Haiku 1·Opus 1/지적", line)
        self.assertIn("Cursor 1/지적", line)


SECTIONED = "Critical\n없음.\n\nWarning\n1. {title}\n   위치: {loc}\n   근거: 확인했다\n   제안: 고친다\n\nSuggestion\n없음.\n"


class ClassifyInputTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def write(self, name, text, as_results=False):
        if as_results:
            text = json.dumps({"results": [{"id": "review:reviewer", "output": {"text": text, "model_id": "m"}}]})
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_claude_results_follow_finding_locations(self):
        doc = self.write("doc.json", SECTIONED.format(title="모호하다", loc="README.md:3"), as_results=True)
        self.assertEqual(detect.classify_input(doc), ("claude-doc", 1))
        code = self.write("code.json", SECTIONED.format(title="틀렸다", loc="app.py:1"), as_results=True)
        self.assertEqual(detect.classify_input(code), ("claude-code", 1))
        failed = self.write("failed.json", json.dumps({"results": [{"id": "review:reviewer", "output": None}]}))
        self.assertEqual(detect.classify_input(failed), ("claude-code", -1))

    def test_sectioned_text_with_code_location_is_code_mode(self):
        code = self.write("code.txt", SECTIONED.format(title="틀렸다", loc="app.py:1"))
        self.assertEqual(detect.classify_input(code), ("claude-code", 1))
        doc = self.write("doc.final", SECTIONED.format(title="모호하다", loc="plan.md:3"))
        self.assertEqual(detect.classify_input(doc), ("codex-doc", 1))

    def test_detect_names_cursor_review_output(self):
        doc = self.write("cursor-review.AbC123.final", SECTIONED.format(title="모호하다", loc="plan.md:3"))
        self.assertEqual(detect.classify_input(doc), ("cursor-doc", 1))
        code = self.write("cursor-review.XyZ789.final", SECTIONED.format(title="틀렸다", loc="app.py:1"))
        self.assertEqual(detect.classify_input(code), ("cursor-code", 1))


    def test_cf_review_folder_reads_the_actual_runner_from_review_meta(self):
        meta = self.tmp / "review-meta.json"
        for runner, loc, expected in (("cursor", "app.py:1", "cursor-code"), ("cursor", "plan.md:3", "cursor-doc"),
                                      ("claude", "app.py:1", "claude-code")):
            with self.subTest(runner=runner, loc=loc):
                meta.write_text(json.dumps({"actual": {"runner": runner}}), encoding="utf-8")
                result = self.write("review.out.final", SECTIONED.format(title="틀렸다", loc=loc))
                self.assertEqual(detect.classify_input(result)[0], expected)

class DetectCliTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"calc.py": "x = 1\n", "README.md": "# r\n", "tests/test_calc.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"})
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["python3 -m unittest {files}"]}})
        self.env = {"CASCADE_REPOS_TOML": str(self.toml), "CASCADE_REVIEWER": "auto"}

    def detect(self, *args):
        p = run_cli(["detect", "--json", "--no-gh", *args], cwd=self.repo, env=self.env)
        return p.returncode, json.loads(p.stdout)

    def test_clean_repo_has_nothing_to_do(self):
        code, r = self.detect()
        self.assertEqual(code, 6)
        self.assertIn("처리할 것이 없다", r["blockers"][0])

    def test_uncommitted_code_recommends_claude_review(self):
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        (self.repo / "new.py").write_text("y = 1\n", encoding="utf-8")
        code, r = self.detect()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(r["uncommitted"]), ["calc.py", "new.py"])
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["mode"], rec["review_args"]), ("claude-code", "code", "--uncommitted"))
        self.assertIn("review --kind code --uncommitted", rec["steps"][0])
        self.assertEqual(r["reviewer"], "claude")

    def test_branch_with_only_docs_recommends_doc_review(self):
        git(self.repo, "switch", "-q", "-c", "feature")
        (self.repo / "README.md").write_text("# 바꿈\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "doc")
        code, r = self.detect()
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["mode"]), ("claude-doc", "doc"))
        self.assertEqual(r["ahead"]["commits"], 1)
        self.assertIn("--doc README.md", rec["steps"][0])

    def test_doc_flag_recommends_doc_review_on_clean_repo(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs/spec.md").write_text("# 스펙\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "spec")
        code, r = self.detect("--doc", "docs/spec.md")
        self.assertEqual(code, 0)
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["mode"]), ("claude-doc", "doc"))
        self.assertIn("문서를 직접 지정했다", rec["reasons"][0])
        self.assertIn("review --kind doc --doc docs/spec.md", rec["steps"][0])
        self.assertEqual(r["approvals"], [])

    def test_doc_flag_wins_over_code_changes(self):
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        code, r = self.detect("--doc", "README.md")
        self.assertEqual(code, 0)
        self.assertEqual(r["recommendation"]["mode"], "doc")
        self.assertNotIn("candidates", r["config"])

    def test_missing_doc_flag_blocks(self):
        code, r = self.detect("--doc", "nope.md")
        self.assertEqual(code, 6)
        self.assertIn("문서가 없다: nope.md", r["blockers"][0])

    def test_mixed_changes_note_doc_only_run(self):
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        (self.repo / "README.md").write_text("# 바꿈\n", encoding="utf-8")
        _, r = self.detect()
        self.assertEqual(r["recommendation"]["mode"], "code")
        self.assertTrue(any("detect --doc README.md" in n for n in r["notes"]))

    def test_doc_flag_from_subdirectory_gives_runnable_review_command(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs/spec.md").write_text("# 스펙\n", encoding="utf-8")
        sub = self.repo / "docs"
        p = run_cli(["detect", "--json", "--no-gh", "--doc", "spec.md"], cwd=sub, env=self.env)
        step = json.loads(p.stdout)["recommendation"]["steps"][0]
        self.assertIn(f"--doc {self.repo / 'docs/spec.md'}", step)
        review = run_cli(shlex.split(step.split("cascade.cli ", 1)[1]), cwd=sub, env=self.env)
        self.assertEqual(review.returncode, 0, review.stdout + review.stderr)

    def test_mixed_changes_note_lists_every_doc(self):
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        for i in range(1, 7):
            (self.repo / f"docs/spec{i}.md").write_text(f"# {i}\n", encoding="utf-8")
        _, r = self.detect()
        note = next(n for n in r["notes"] if "detect --doc" in n)
        self.assertIn("문서도 6개", note)
        for i in range(1, 7):
            self.assertIn(f"--doc docs/spec{i}.md", note)

    def test_root_from_other_repo_is_kept_in_next_steps(self):
        caller = make_repo(self.tmp / "caller", {"x.py": "x = 1\n"})
        (self.repo / "README.md").write_text("# 바꿈\n", encoding="utf-8")
        p = run_cli(["detect", "--json", "--no-gh", "--root", str(self.repo)], cwd=caller, env=self.env)
        rec = json.loads(p.stdout)["recommendation"]
        self.assertIn(f"--doc {self.repo / 'README.md'}", rec["steps"][0])
        self.assertTrue(rec["steps"][0].endswith(f"--root {self.repo}"))

    def test_deleted_docs_only_blocks_doc_review(self):
        git(self.repo, "rm", "-q", "README.md")
        code, r = self.detect()
        self.assertEqual(code, 6)
        self.assertIn("모두 삭제", r["blockers"][0])
        (self.repo / "NOTES.md").write_text("# 새 문서\n", encoding="utf-8")
        code, r = self.detect()
        self.assertEqual(code, 0)
        self.assertNotIn("README.md", r["recommendation"]["review_args"])
        self.assertIn("--doc NOTES.md", r["recommendation"]["review_args"])

    def test_missing_config_offers_unittest_candidate(self):
        self.env["CASCADE_REPOS_TOML"] = str(self.tmp / "none.toml")
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        code, r = self.detect()
        self.assertEqual(code, 0)
        self.assertIn("discover -s tests", r["config"]["candidates"]["tests"][0])
        self.assertTrue(any("검증 명령 등록" in a for a in r["approvals"]))
        p = run_cli(["detect", "--no-gh"], cwd=self.repo, env=self.env)
        self.assertIn("  후보 tests: python3 -m unittest discover -s tests -p 'test_*.py'", p.stdout)

    def test_no_tests_blocks_code_mode(self):
        self.env["CASCADE_REPOS_TOML"] = str(self.tmp / "none.toml")
        git(self.repo, "rm", "-q", "tests/test_calc.py")
        git(self.repo, "commit", "-qm", "rm")
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        code, r = self.detect()
        self.assertEqual(code, 6)
        self.assertTrue(any("테스트 명령을 찾지 못했다" in b for b in r["blockers"]))

    def test_sensitive_path_is_noted(self):
        (self.repo / "auth.py").write_text("t = 1\n", encoding="utf-8")
        _, r = self.detect()
        self.assertTrue(any("auth.py" in n for n in r["notes"]))

    def test_codex_input_is_classified_and_estimated(self):
        code, r = self.detect("--input", str(FIXTURES / "codex-code-e2e.final"), "--reviewer", "codex")
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["estimate"]["stage1"]), ("codex-code", 4))
        self.assertIn("--reviewer codex", rec["steps"][0])
        self.assertTrue(any("→ OpenAI" in a for a in r["approvals"]))

    def test_detect_cursor_reviewer_asks_cursor_consent(self):
        code, r = self.detect("--input", str(FIXTURES / "codex-code-e2e.final"), "--reviewer", "cursor")
        self.assertEqual(r["reviewer"], "cursor")
        self.assertEqual(r["reviewer_label"], "Cursor")
        self.assertTrue(any("→ Cursor(팀 프라이버시 모드)" in a for a in r["approvals"]))
        self.assertFalse(any("OpenAI" in a for a in r["approvals"]))
        self.assertIn("--reviewer cursor", r["recommendation"]["steps"][0])

    def test_detect_cursor_result_file_recommends_cursor_source(self):
        raw = self.tmp / "cursor-review.AbC123.final"
        raw.write_text(SECTIONED.format(title="모호하다", loc="README.md:3"), encoding="utf-8")
        code, r = self.detect("--input", str(raw), "--reviewer", "cursor")
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["mode"]), ("cursor-doc", "doc"))
        self.assertIn("--source cursor-doc", rec["steps"][0])

    def test_external_doc_result_passes_doc_to_init(self):
        ext = self.tmp / "outside" / "external-plan.md"
        ext.parent.mkdir()
        ext.write_text("# 계획\n\n1단계\n", encoding="utf-8")
        text = SECTIONED.format(title="단계가 모호하다", loc=f"{ext}:3")
        result = self.tmp / "review.json"
        result.write_text(json.dumps({"results": [{"id": "review:reviewer", "output": {"text": text, "model_id": "m"}}]},
                                     ensure_ascii=False), encoding="utf-8")
        code, r = self.detect("--input", str(result))
        self.assertEqual(code, 0)
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["mode"]), ("claude-doc", "doc"))
        self.assertIn(f"--doc {ext}", rec["steps"][0])
        words = shlex.split(rec["steps"][0])
        init = words[words.index("init"):]
        p = run_cli(init, cwd=self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        docs = json.loads((run_dir / "ledger.json").read_text(encoding="utf-8"))["docs"]
        self.assertEqual(list(docs.values()), [str(ext)])

    def test_pr_with_unresolved_threads_recommends_pr_comment(self):
        env = {**self.env, "CASCADE_GH": FAKE_GH, "CASCADE_FAKE_GH_PR_URL": "https://github.com/o/r/pull/7",
               "CASCADE_FAKE_GH_FILE": str(FIXTURES / "pr-threads.json")}
        p = run_cli(["detect", "--json"], cwd=self.repo, env=env)
        r = json.loads(p.stdout)
        self.assertEqual((r["pr"]["number"], r["pr"]["repo"]), (7, "o/r"))
        self.assertGreater(r["pr"]["unresolved"], 0)
        rec = r["recommendation"]
        self.assertEqual(rec["source"], "pr-comment")
        self.assertIn("--input 7 --pr-repo o/r --me me", rec["steps"][0])

    def test_human_output(self):
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        p = run_cli(["detect", "--no-gh"], cwd=self.repo, env=self.env)
        self.assertIn("추천: --source claude-code", p.stdout)
        self.assertIn("규모: 첫 검토 1개 + 지적 1건당 에이전트 2~4개", p.stdout)
        self.assertIn("ok=예", p.stdout)


CODE_TESTS = {"tests/test_calc.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"}
CURSOR_FIRST = """
[profiles.cursor-first]
"code.review" = { runner = "cursor", model = "composer-9" }

[profiles.split]
"code.review" = { runner = "claude", model = "claude-sonnet-5-5", effort = "low" }
"""


class DetectProfileTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"calc.py": "x = 1\n", "README.md": "# r\n", **CODE_TESTS})
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["python3 -m unittest {files}"]}})
        self.toml.write_text(self.toml.read_text(encoding="utf-8") + CURSOR_FIRST, encoding="utf-8")
        self.rr = self.tmp / "run-review.sh"
        self.rr.write_text("#!/bin/sh\n", encoding="utf-8")
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")

    def call(self, name="local", *, env=None, available=None, run_review=None, cursor_ok=True, codex_model="", **kwargs):
        available = {"codex": True, "cursor": True} if available is None else available
        resolved = profiles.resolve(self.repo, self.toml, flag=name, cli=profiles.LegacyOptions(), env=env or {},
                                    available=available, codex_model=codex_model)
        return detect.detect(self.repo, repos_toml=self.toml, run_review=run_review or self.rr, cli="CF", gh=None,
                             profile=resolved, available=available, cursor_ok=cursor_ok, codex_model=codex_model,
                             profile_flag=name if name and not env else None, **kwargs)

    def test_detect_recommends_profile_first_review(self):
        result = self.call("local")
        steps = result["recommendation"]["steps"]
        self.assertEqual(steps[0], "CF review --kind code --uncommitted --profile local")
        self.assertFalse(any("run-review.sh" in s for s in steps), steps)
        self.assertEqual(result["recommendation"]["source"], "codex-code")
        self.assertIn("전송 동의: 첫 검토·A·의견·재검토 → OpenAI", result["approvals"])

    def test_cursor_first_review_is_also_a_cf_review_step(self):
        result = self.call("cursor-first")
        steps = result["recommendation"]["steps"]
        self.assertEqual(steps[0], "CF review --kind code --uncommitted --profile cursor-first")
        self.assertFalse(any("--model" in s for s in steps), steps)
        self.assertTrue(any("→ Cursor" in a for a in result["approvals"]))

    def test_doc_first_review_is_a_cf_review_step_with_the_docs(self):
        result = self.call("local", docs=[self.repo / "README.md"])
        steps = result["recommendation"]["steps"]
        self.assertEqual(result["recommendation"]["source"], "codex-doc")
        self.assertTrue(steps[0].startswith("CF review --kind doc --doc "), steps[0])
        self.assertTrue(steps[0].endswith("README.md --profile local"), steps[0])
        self.assertFalse(any("run-review.sh" in s for s in steps), steps)

    def test_claude_first_review_follows_the_explicit_profile(self):
        result = self.call("split")
        steps = result["recommendation"]["steps"]
        self.assertEqual(result["recommendation"]["source"], "claude-code")
        self.assertEqual(steps[0], "CF review --kind code --uncommitted --profile split")

    def test_default_profile_keeps_old_review_step(self):
        resolved = profiles.resolve(self.repo, self.toml, flag=None, cli=profiles.LegacyOptions(), env={},
                                    available={}, codex_model="")
        result = detect.detect(self.repo, repos_toml=self.toml, run_review=self.rr, cli="CF", gh=None, profile=resolved)
        self.assertEqual(result["recommendation"]["steps"][0], "CF review --kind code --uncommitted")

    def test_explicit_profile_from_environment_is_carried_to_init(self):
        result = self.call("local", env={"CASCADE_PROFILE": "local"})
        steps = result["recommendation"]["steps"]
        self.assertTrue(steps[0].endswith("review --kind code --uncommitted --profile local"), steps[0])

    def test_dry_run_hint_follows_the_original_source_not_the_external_first_review(self):
        hint = "--dry-run"
        external = self.call("local")
        self.assertEqual(external["recommendation"]["source"], "codex-code")
        self.assertFalse(any(hint in n for n in external["notes"]), external["notes"])
        claude = self.call("split")
        self.assertFalse(any(hint in n for n in claude["notes"]), claude["notes"])

    def test_unavailable_first_review_cli_falls_back_and_is_noted(self):
        result = self.call("local", available={"codex": False, "cursor": False})
        rec = result["recommendation"]
        self.assertEqual((rec["source"], rec["steps"][0]), ("claude-code", "CF review --kind code --uncommitted --profile local"))
        self.assertTrue(any("code.review codex → claude" in n for n in result["notes"]), result["notes"])
        self.assertFalse(any("OpenAI" in a for a in result["approvals"]))

    def test_missing_review_script_falls_back_to_the_cell_fallback(self):
        result = self.call("local", run_review=self.tmp / "none.sh")
        rec = result["recommendation"]
        self.assertEqual(rec["source"], "claude-code")
        self.assertTrue(any("code.review codex → claude — 검토 스크립트 없음" in n for n in result["notes"]), result["notes"])

    def test_input_file_excludes_first_review_cell_from_consent_and_roles(self):
        result = self.call("local", input_path=FIXTURES / "codex-code-e2e.final")
        self.assertIn("전송 동의: A·의견·재검토 → OpenAI", result["approvals"])
        self.assertNotIn("code.review", result["profile"]["roles"])
        self.assertNotIn("첫 검토", detect.render(result))
        self.assertIn("역할: A=Codex", detect.render(result))

    def test_first_review_is_listed_when_detect_recommends_one(self):
        result = self.call("local")
        self.assertIn("code.review", result["profile"]["roles"])
        self.assertIn("역할: 첫 검토=Codex, A=Codex", detect.render(result))

    def test_codex_model_is_filled_like_init(self):
        result = self.call("local", input_path=FIXTURES / "codex-code-e2e.final", codex_model="gpt-test")
        self.assertEqual(result["workers"]["a"]["model"], "gpt-test")
        self.assertEqual(result["profile"]["roles"]["code.a"]["model"], "gpt-test")
        self.assertEqual(result["profile"]["roles"]["code.opinion"]["model"], "")

    def test_profile_block_has_name_source_and_roles(self):
        result = self.call("local", input_path=FIXTURES / "codex-code-e2e.final")
        profile = result["profile"]
        self.assertEqual((profile["name"], profile["source"]), ("local", "--profile"))
        self.assertEqual(profile["roles"]["code.a"]["runner"], "codex")
        self.assertNotIn("doc.writer", profile["roles"])

    def test_render_prints_profile_override_and_role_lines(self):
        result = self.call("local", input_path=FIXTURES / "codex-code-e2e.final")
        text = detect.render(result)
        lines = text.splitlines()
        self.assertIn("프로필: local (--profile)", lines)
        self.assertTrue(any(x.startswith("역할: A=Codex") for x in lines), lines)
        self.assertLess(lines.index("프로필: local (--profile)"), next(i for i, x in enumerate(lines) if x.startswith("역할:")))


class SeverityTest(CliTestBase):
    def test_severity_defers_other_findings_to_hold(self):
        raw = self.tmp / "doc.final"
        raw.write_text(
            "Critical\n1. add가 뺀다\n   위치: calc.py:2\n   근거: a\n\n"
            "Warning\n없음.\n\n"
            "Suggestion\n1. 이름\n   위치: calc.py:5\n   근거: b\n",
            encoding="utf-8")
        p = self.cli("init", "--source", "claude-code", "--input", str(raw), "--severity", "critical,Warning")
        self.assertIn("보류 후보로 둔다", p.stdout)
        self.assertIn("규모: 회차당 에이전트 2~", p.stdout)
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        rnd = self.ledger().rounds[0]
        self.assertEqual([f["id"] for f in rnd.findings], ["r1-01"])
        self.assertEqual([f["id"] for f in rnd.hold_findings], ["r1-02"])
        self.assertEqual([(d["finding_id"], d["outcome"]) for d in rnd.decisions], [("r1-02", "보류후보")])

        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        stage1 = self.run_dir / "round-1" / "stage1.json"
        stage1.write_text(json.dumps({"results": []}), encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        outcomes = {d["finding_id"]: d["outcome"] for d in self.ledger().rounds[0].decisions}
        self.assertEqual(outcomes["r1-02"], "보류후보")
        self.assertIn("r1-01", outcomes)

    def test_bad_or_empty_severity_stops(self):
        p = self.cli("init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"),
                     "--severity", "Major", ok=False)
        self.assertIn("--severity", p.stdout)
        p = self.cli("init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"),
                     "--severity", "Critical", ok=False)
        self.assertIn("맞는 지적이 없다", p.stdout)


if __name__ == "__main__":
    unittest.main()
