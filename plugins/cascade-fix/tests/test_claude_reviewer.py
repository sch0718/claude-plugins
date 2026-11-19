import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import review  # noqa: E402
from cascade.snapshot import commit_worktree  # noqa: E402
from helpers import git, make_repo, run_cli  # noqa: E402
from test_cli_e2e import CALC, FIXTURES, RECHECK_RESULT, CliTestBase, fix  # noqa: E402

MODEL = review.DEFAULT_MODEL
CLAUDE_REVIEW = """Critical
없음.

Warning
1. add가 빼기를 한다
   위치: calc.py:2
   근거: add(2, 3)이 -1이다
   제안: a + b로 고친다

Suggestion
없음.
"""


def review_results(path, job_id, text, model=MODEL):
    output = None if text is None else {"text": text, "model_id": model}
    path.write_text(json.dumps({"results": [{"id": job_id, "output": output}]}, ensure_ascii=False), encoding="utf-8")
    return path


class ReviewModuleTest(unittest.TestCase):
    def test_resolve_auto(self):
        missing = Path("/nonexistent/run-review.sh")
        self.assertEqual(review.resolve("auto", "codex-code", missing), "claude")
        self.assertEqual(review.resolve("auto", "claude-doc", Path(__file__)), "claude")
        self.assertEqual(review.resolve("auto", "codex-code", Path(__file__)), "codex")
        self.assertEqual(review.resolve("codex", "claude-code", missing), "codex")

    def test_load_text_reads_plain_and_workflow_json(self):
        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "a.final"
            plain.write_text("본문", encoding="utf-8")
            self.assertEqual(review.load_text(plain), ("본문", None))
            js = review_results(Path(td) / "a.json", "x:opinion", "의견", "m")
            self.assertEqual(review.load_text(js, "x:opinion"), ("의견", "m"))
            self.assertEqual(review.load_text(js, "y:opinion"), ("", None))
            failed = review_results(Path(td) / "b.json", "x:opinion", None)
            self.assertEqual(review.load_text(failed), ("", None))

    def test_claude_prompt_marks_repo_read_only(self):
        text = review.claude_prompt("질문", "/repo")
        self.assertIn("`/repo`", text)
        self.assertIn("`text`", text)
        self.assertIn("model_id", text)


class ClaudeReviewCommandTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.env["CASCADE_REVIEWER"] = "auto"

    def review_dir(self, p):
        line = next(x for x in p.stdout.splitlines() if x.startswith("review_dir="))
        return Path(line.split("=", 1)[1])

    def test_review_code_writes_workflow_args(self):
        git(self.repo, "switch", "-q", "-c", "feature")
        p = self.cli("review", "--kind", "code", "--base", "main")
        out = self.review_dir(p)
        args = json.loads((out / "review-args.json").read_text(encoding="utf-8"))
        [job] = args["jobs"]
        self.assertEqual((job["id"], job["kind"], job["model"], job["effort"]),
                         (review.REVIEW_JOB, "review", MODEL, review.DEFAULT_EFFORT))
        prompt = Path(job["promptPath"]).read_text(encoding="utf-8")
        self.assertIn("diff main...HEAD", prompt)
        self.assertIn(str(self.repo), prompt)
        self.assertIn("init --source claude-code", p.stdout)
        self.assertIn(".cascade-fix/", git(self.repo, "status", "--porcelain", "--ignored"))

    def test_review_writes_meta_and_keeps_old_init_line_for_default_profile(self):
        p = self.cli("review", "--kind", "code", "--base", "main", "--review-model", "claude-sonnet-5-5")
        out = self.review_dir(p)
        meta = json.loads((out / "review-meta.json").read_text(encoding="utf-8"))
        cell = {"runner": "claude", "model": "claude-sonnet-5-5", "effort": review.DEFAULT_EFFORT}
        fallback = {"runner": "claude", "model": review.DEFAULT_MODEL, "effort": review.DEFAULT_EFFORT}
        self.assertEqual((meta["role"], meta["requested"], meta["actual"], meta["fallback_reason"], meta["attempts"]),
                         ("code.review", {**cell, "fallback": fallback}, cell, None, []))
        self.assertEqual(meta["consented"], [])
        self.assertEqual((meta["profile"], meta["explicit"]), ("default", False))
        self.assertEqual(meta["args"]["base"], "main")
        init_line = next(x for x in p.stdout.splitlines() if " init --source " in x)
        self.assertIn("--review-model claude-sonnet-5-5", init_line)
        self.assertNotIn("--profile", init_line)
        self.assertNotIn("--review-effort", init_line)

    def test_review_dir_without_fallback_is_rejected(self):
        p = self.cli("review", "--review-dir", str(self.tmp), ok=False)
        self.assertEqual(p.returncode, 5)

    def test_review_rejects_a_model_the_claude_cell_cannot_take(self):
        p = self.cli("review", "--kind", "code", "--base", "main", "--review-model", "composer-2.5", ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertIn("--review-model composer-2.5", p.stdout)

    def test_review_with_root_keeps_target_in_init(self):
        caller = make_repo(self.tmp / "caller", {"x.py": "x = 1\n"})
        (self.repo / "calc.py").write_text(CALC + "\n", encoding="utf-8")
        p = run_cli(["review", "--kind", "code", "--uncommitted", "--root", str(self.repo)], cwd=caller, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        init_line = next(x for x in p.stdout.splitlines() if " init --source " in x)
        self.assertIn(f"--root {self.repo}", init_line)
        result = review_results(self.review_dir(p) / "review.json", review.REVIEW_JOB, CLAUDE_REVIEW)
        p = run_cli(["init", "--source", "claude-code", "--input", str(result), "--root", str(self.repo)],
                    cwd=caller, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertEqual(Path(self.ledger().root), self.repo)

    def test_review_needs_exactly_one_range(self):
        p = self.cli("review", "--kind", "code", ok=False)
        self.assertNotEqual(p.returncode, 0)
        p = self.cli("review", "--kind", "doc", ok=False)
        self.assertIn("--doc", p.stdout)

    def test_review_doc_lists_docs_and_init_parses_claude_doc(self):
        (self.repo / "plan.md").write_text("# 계획\n\n1단계\n", encoding="utf-8")
        p = self.cli("review", "--kind", "doc", "--doc", str(self.repo / "plan.md"), "--review-model", "claude-opus-5-5")
        out = self.review_dir(p)
        job = json.loads((out / "review-args.json").read_text(encoding="utf-8"))["jobs"][0]
        self.assertEqual(job["model"], "claude-opus-5-5")
        self.assertIn("`plan.md`", Path(job["promptPath"]).read_text(encoding="utf-8"))
        self.assertIn("--review-model claude-opus-5-5", p.stdout)
        text = "Critical\n없음.\n\nWarning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 없다\n\nSuggestion\n없음.\n"
        result = review_results(out / "review.json", review.REVIEW_JOB, text, "claude-opus-5-5")
        p = self.cli("init", "--source", "claude-doc", "--input", str(result), "--doc", str(self.repo / "plan.md"),
                     "--review-model", "claude-opus-5-5")
        self.assertNotIn("경고", p.stdout)
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = self.ledger()
        self.assertEqual((ledger.mode, ledger.reviewer, ledger.review_model), ("doc", "claude", "claude-opus-5-5"))
        self.assertEqual(ledger.rounds[0].findings[0]["source"], "claude-doc")

    def test_init_claude_code_parses_review_json(self):
        result = review_results(self.tmp / "review.json", review.REVIEW_JOB, CLAUDE_REVIEW, "claude-sonnet-5-5")
        p = self.cli("init", "--source", "claude-code", "--input", str(result))
        self.assertIn("모델 ID가 claude-sonnet-5-5", p.stdout)
        self.assertIn("Claude 단독", p.stdout)
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = self.ledger()
        self.assertEqual((ledger.source, ledger.mode, ledger.reviewer), ("claude-code", "code", "claude"))
        [f] = ledger.rounds[0].findings
        self.assertEqual((f["id"], f["severity"], f["location"]), ("r1-01", "Warning", "calc.py:2"))

    def test_init_claude_code_failed_workflow_stops(self):
        result = review_results(self.tmp / "review.json", review.REVIEW_JOB, None)
        p = self.cli("init", "--source", "claude-code", "--input", str(result), ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertIn("비어 있다", p.stdout)

    def test_bad_reviewer_env_is_rejected(self):
        self.env["CASCADE_REVIEWER"] = "gpt"
        p = self.cli("init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"), ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("gpt", p.stdout)


class ClaudeReviewerFlowTest(CliTestBase):
    """판가름 뒤 의견과 재검토를 Codex 없이 Claude Workflow로 돈다."""

    def setUp(self):
        super().setUp()
        self.env["CASCADE_REVIEWER"] = "claude"

    def test_opinion_and_recheck_use_claude_workflow(self):
        p = self.init()
        self.assertIn("Claude 단독", p.stdout)
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        rd = self.run_dir / "round-1"
        adj = rd / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        p = self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        self.assertIn("opinion-args.json", p.stdout)
        self.assertNotIn("run-review.sh", p.stdout)

        [job] = json.loads((rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["id"], job["kind"], job["model"]), ("r1-02:opinion", "review", MODEL))
        prompt = Path(job["promptPath"]).read_text(encoding="utf-8")
        self.assertIn("판정 의견 요청", prompt)
        self.assertIn(str(self.wt("base")), prompt)

        results = review_results(rd / "opinion.json", "r1-02:opinion", "판가름자와 같은 의견이다")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(results))
        self.assertIn("Claude 의견 1건", p.stdout)
        self.assertEqual(self.ledger().opinions["r1-02"], "판가름자와 같은 의견이다")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(results), ok=False)
        self.assertNotEqual(p.returncode, 0)

        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("recheck-args.json", p.stdout)
        [job] = json.loads((rd / "recheck-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual(job["id"], "round-1:recheck")
        self.assertIn("수정 재검토", Path(job["promptPath"]).read_text(encoding="utf-8"))

        failed = review_results(rd / "recheck.json", "round-1:recheck", None)
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(failed), ok=False)
        self.assertIn("비어 있다", p.stdout)
        text = RECHECK_RESULT.replace("- r1-02: 해소됨 — mul이 곱한다\n", "")
        review_results(rd / "recheck.json", "round-1:recheck", text)
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(rd / "recheck.json"))
        self.assertIn("status=종료", p.stdout)

        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("검토자: Claude", p.stdout)
        self.assertIn("Claude 의견:", p.stdout)
        self.assertIn("| Claude 재검토 | 1 |", p.stdout)

    def test_failed_opinion_is_noted_and_flow_continues(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        rd = self.run_dir / "round-1"
        adj = rd / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        results = review_results(rd / "opinion.json", "r1-02:opinion", None)
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(results))
        self.assertIn("호출 실패", p.stdout)
        self.assertEqual(self.ledger().opinions["r1-02"], review.CALL_FAILED_NOTE)
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("apply", p.stdout)


if __name__ == "__main__":
    unittest.main()
