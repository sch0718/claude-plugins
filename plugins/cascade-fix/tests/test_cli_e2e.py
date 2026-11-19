import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli, cli_steps  # noqa: E402
from cascade.rounds import Ledger, RoundRecord  # noqa: E402
from cascade.snapshot import commit_worktree  # noqa: E402
from helpers import git, make_repo, run_cli, write_repos_toml  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CALC = """def add(a, b):
    return a - b


def ident(x):
    return x


def neg(x):
    return -x


def mul(a, b):
    return a + b
"""
TEST_CALC = """import unittest

from calc import ident


class IdentTest(unittest.TestCase):
    def test_ident(self):
        self.assertEqual(ident(3), 3)
"""
RECHECK_RESULT = """## 해소 여부
- r1-01: 해소됨 — add가 더한다
- r1-02: 해소됨 — mul이 곱한다

Critical
없음.

Warning
없음.

Suggestion
1. ident에 타입 힌트가 없다
   위치: calc.py:5
   근거: 다른 함수와 맞지 않는다
   제안: 힌트를 단다
"""


def fix(text, old, new):
    assert old in text, old
    return text.replace(old, new)


def worker_output(verdict, model, reason="근거"):
    return {"verdict": verdict, "needs_design_decision": False, "reason": reason, "verify": [], "model_id": model}


def section_of(text, heading):
    start = text.index(heading)
    nxt = text.find("\n## ", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


def results_json(path, items):
    path.write_text(json.dumps({"results": [{"id": i, "output": o} for i, o in items]}, ensure_ascii=False),
                    encoding="utf-8")
    return path


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {
            "calc.py": CALC,
            "test_calc.py": TEST_CALC,
            ".gitignore": "__pycache__/\n",
        })
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -m unittest discover -s ."],
            "format": [],
        }})
        self.env = {"CASCADE_REPOS_TOML": str(toml), "PYTHONDONTWRITEBYTECODE": "1"}

    def cli(self, *args, ok=True):
        p = run_cli(list(args), cwd=self.repo, env=self.env)
        if ok:
            self.assertEqual(p.returncode, 0, f"{args}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        return p

    def ledger(self):
        return Ledger.load(self.run_dir / "ledger.json")

    def wt(self, name):
        return Path(self.ledger().snapshot["worktrees"][name])

    def init(self, *extra):
        p = self.cli("init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"), *extra)
        line = next(x for x in p.stdout.splitlines() if x.startswith("run_dir="))
        self.run_dir = Path(line.split("=", 1)[1])
        return p

    def run_to_apply(self, *init_extra, before_apply=None):
        self.commits_before = git(self.repo, "rev-list", "--count", "HEAD")
        self.init(*init_extra)
        findings = json.loads((self.run_dir / "round-1" / "findings.json").read_text(encoding="utf-8"))
        self.assertEqual([f["id"] for f in findings], ["r1-01", "r1-02"])
        self.assertEqual([f["severity"] for f in findings], ["Warning", "Warning"])

        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        args = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [(j["id"], j["model"]) for j in args["jobs"]],
            [("r1-01:a", "claude-sonnet-5-5"), ("r1-01:b", "claude-haiku-5-5"),
             ("r1-02:a", "claude-sonnet-5-5"), ("r1-02:b", "claude-haiku-5-5")],
        )
        self.assertTrue(self.wt("base").is_dir())

        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            self.assertIsNotNone(commit_worktree(wt, f"cascade-fix {name}"))
        wt = self.wt("r1-02-a")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 a")

        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertIn("판가름", p.stdout)
        decisions = json.loads((self.run_dir / "round-1" / "decisions.json").read_text(encoding="utf-8"))
        outcomes = {d["finding_id"]: d["outcome"] for d in decisions}
        self.assertEqual(outcomes, {"r1-01": "합의-사실", "r1-02": "판가름"})
        adj_args = json.loads((self.run_dir / "round-1" / "adj-args.json").read_text(encoding="utf-8"))
        self.assertEqual([(j["id"], j["model"]) for j in adj_args["jobs"]], [("r1-02:adjudicator", "claude-opus-5-5")])
        self.assertTrue((self.run_dir / "round-1" / "patch-r1-01-a.diff").read_text(encoding="utf-8").strip())

        wt = self.wt("r1-02-adj")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 adjudicator")
        adj = self.run_dir / "round-1" / "adj.json"
        shutil.copyfile(FIXTURES / "workflow-adj.json", adj)
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        decisions = {d["finding_id"]: d for d in self.ledger().rounds[0].decisions}
        self.assertEqual(decisions["r1-02"]["outcome"], "판가름")
        self.assertEqual(decisions["r1-02"]["patch_from"], "adjudicator")
        self.assertTrue((self.run_dir / "round-1" / "patch-r1-02-adj.diff").is_file())

        if before_apply is not None:
            before_apply()
        return self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")


class CliEndToEndTest(CliTestBase):
    def test_full_run_applies_both_fixes_and_ends_after_recheck(self):
        self.run_to_apply()
        calc = (self.repo / "calc.py").read_text(encoding="utf-8")
        self.assertIn("return a + b\n\n\ndef ident", calc)
        self.assertIn("return a * b", calc)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), self.commits_before)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual(apply["applied"], ["r1-01", "r1-02"])
        self.assertTrue(apply["final_ok"])
        self.assertEqual(self.ledger().rounds[0].adopted, ["r1-01", "r1-02"])
        self.assertEqual(set(self.ledger().snapshot["worktrees"]), {"base"})
        self.assertFalse((self.run_dir / "wt" / "r1-01-a").exists())
        self.assertFalse((self.run_dir / "wt" / "r1-02-adj").exists())
        self.assertTrue((self.run_dir / "wt" / "base").is_dir())
        self.assertEqual((self.run_dir / "round-1" / "recheck-repo.txt").read_text(encoding="utf-8").strip(), str(self.repo))

        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("| 1 | 2 | 1 | 1 | 1 | 0 | 0 |", p.stdout)
        self.assertNotIn("적용하지 않음(dry-run)", p.stdout)
        self.assertTrue((self.run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8").startswith("fix: 2건"))

        prompt = self.run_dir / "round-1" / "recheck-prompt.md"
        self.assertTrue(prompt.is_file())
        self.assertIn("return a * b", prompt.read_text(encoding="utf-8"))
        self.assertTrue(self.ledger().rounds[0].recheck_opened)

        result = self.tmp / "recheck.final"
        result.write_text(RECHECK_RESULT, encoding="utf-8")
        self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result))
        ledger = self.ledger()
        self.assertEqual(ledger.status, "종료")
        self.assertEqual(len(ledger.rounds), 1)
        hold = [d for d in ledger.rounds[0].decisions if d["outcome"] == "보류후보"]
        self.assertEqual(len(hold), 1)
        self.assertEqual(len(ledger.rounds[0].findings), 2)
        held = next(f for f in ledger.rounds[0].hold_findings if f["id"] == hold[0]["finding_id"])
        self.assertEqual(held["severity"], "Suggestion")

        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("report --run-dir", p.stdout)
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("ident에 타입 힌트가 없다", p.stdout)
        self.assertIn("| 1 | 2 | 1 | 1 | 1 | 0 | 1 |", p.stdout)
        self.assertIn("Suggestion, calc.py:5", p.stdout)
        reports = [e for e in self.ledger().rounds[0].events if e["step"] == "report"]
        self.assertEqual([e["round"] for e in reports], [1, 1])
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertNotIn("report --run-dir", p.stdout)
        self.assertIn("cleanup", p.stdout)

        self.cli("cleanup", "--run-dir", str(self.run_dir))
        self.assertEqual(git(self.repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertTrue((self.run_dir / "ledger.json").is_file())
        self.assertFalse((self.run_dir / "wt").exists())

    def _stage1_with_disagreement(self):
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, f"cascade-fix {name}")
        wt = self.wt("r1-02-a")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 a")
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))

    def test_jobs_force_resets_adjudicator_worktree(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self._stage1_with_disagreement()
        adj_wt = self.wt("r1-02-adj")
        start = git(adj_wt, "rev-parse", "HEAD")
        (adj_wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(adj_wt, "cascade-fix r1-02 adjudicator")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", "--force")
        self._stage1_with_disagreement()
        self.assertEqual(git(adj_wt, "rev-parse", "HEAD"), start)
        self.assertEqual((adj_wt / "calc.py").read_text(encoding="utf-8"), CALC)

    def test_dry_run_leaves_working_tree_and_shows_banner(self):
        self.run_to_apply("--dry-run")
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), CALC)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.ledger().rounds[0].adopted, ["r1-01", "r1-02"])
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("**적용하지 않음(dry-run)**", p.stdout)
        self.assertIn("| 1 | 2 | 1 | 1 | 1 | 0 | 0 |", p.stdout)

    def test_completed_step_is_refused_without_force(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--force", p.stdout + p.stderr)

    def test_status_names_next_command(self):
        self.init()
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("jobs --run-dir", p.stdout)
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("stage1-args.json", p.stdout)
        self.assertIn("judge", p.stdout)

    def test_apply_failure_moves_finding_to_undecidable(self):
        self.run_to_apply_with_conflict()
        decisions = {d["finding_id"]: d for d in self.ledger().rounds[0].decisions}
        self.assertEqual(decisions["r1-01"]["outcome"], "판단불가")
        self.assertTrue(any(r.startswith("패치 적용 실패: ") for r in decisions["r1-01"]["reasons"]))
        self.assertNotIn("r1-01", self.ledger().rounds[0].adopted)
        self.assertEqual(decisions["r1-01"]["adopted_as"], "합의-사실")
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("| 1 | 2 | 1 | 1 | 0 | 1 | 2 |", p.stdout)

    def test_pre_final_decisions_keep_apply_failures(self):
        self.run_to_apply_with_conflict()
        pre = {d["finding_id"]: d for d in json.loads(
            (self.run_dir / "round-1" / "decisions-pre-final.json").read_text(encoding="utf-8"))}
        self.assertEqual(pre["r1-01"]["outcome"], "판단불가")
        self.assertTrue(any(r.startswith("패치 적용 실패: ") for r in pre["r1-01"]["reasons"]))

    def test_broken_full_suite_marks_interference_and_keeps_tree(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -c \"import sys; sys.exit(3 if 'a * b' in open('calc.py').read() else 0)\""],
        }})
        self.env["CASCADE_REPOS_TOML"] = str(toml)
        self.run_to_apply()
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual(apply["applied"], ["r1-01", "r1-02"])
        self.assertIs(apply["final_ok"], False)
        self.assertIs(apply["interference"], True)
        ledger = self.ledger()
        for d in ledger.rounds[0].decisions:
            self.assertEqual(d["outcome"], "판단불가")
            self.assertIn("전체 테스트 실패(간섭 의심)", d["reasons"])
        self.assertEqual(ledger.rounds[0].adopted, [])
        self.assertEqual(ledger.status, "종료")
        self.assertIn("return a * b", (self.repo / "calc.py").read_text(encoding="utf-8"))
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("## 간섭 의심", p.stdout)
        self.assertIn("| 1 | 2 | 1 | 1 | 1 | 0 | 2 |", p.stdout)
        self.assertIn("되돌리지 않았다", p.stdout)
        self.assertEqual((self.run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8"), "")


    def test_second_round_starts_from_applied_fixes(self):
        self.run_to_apply()
        result = self.tmp / "recheck.final"
        result.write_text(
            "## 해소 여부\n- r1-01: 해소됨 — 됐다\n- r1-02: 해소됨 — 됐다\n\n"
            "Critical\n1. mul이 정수를 돌려주지 않는다\n   위치: calc.py:14\n   근거: 실수가 나온다\n   제안: int로 감싼다\n\n"
            "Warning\n없음.\n\nSuggestion\n없음.\n",
            encoding="utf-8",
        )
        self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result))
        ledger = self.ledger()
        self.assertEqual((len(ledger.rounds), ledger.status), (2, "진행"))
        self.assertTrue(ledger.rounds[0].result_commit)
        self.assertEqual(ledger.rounds[1].base_commit, ledger.rounds[0].result_commit)

        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "2")
        for name in ("r2-01-a", "r2-01-b"):
            wt = self.wt(name)
            text = (wt / "calc.py").read_text(encoding="utf-8")
            self.assertIn("return a + b\n\n\ndef ident", text)
            (wt / "calc.py").write_text(fix(text, "return a * b", "return int(a * b)"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = results_json(self.run_dir / "round-2" / "stage1.json", [
            ("r2-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r2-01:b", worker_output("사실", "claude-haiku-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "2", "--results", str(stage1))
        self.assertEqual([d["outcome"] for d in self.ledger().rounds[1].decisions], ["합의-사실"])
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "2")
        apply = json.loads((self.run_dir / "round-2" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual((apply["applied"], apply["failed"]), (["r2-01"], []))
        calc = (self.repo / "calc.py").read_text(encoding="utf-8")
        self.assertIn("return int(a * b)", calc)
        self.assertIn("return a + b\n\n\ndef ident", calc)
        prompt = (self.run_dir / "round-2" / "recheck-prompt.md").read_text(encoding="utf-8")
        self.assertIn("+    return int(a * b)", prompt)
        self.assertNotIn("return a - b", prompt)

    def run_to_apply_with_overlap(self, *init_extra):
        self.init(*init_extra)
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        for name in ("r1-02-a", "r1-02-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "def add(a, b):", "def add(a: int, b: int):"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            (f"{fid}:{role}", worker_output("사실", model))
            for fid in ("r1-01", "r1-02") for role, model in (("a", "claude-sonnet-5-5"), ("b", "claude-haiku-5-5"))
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertEqual([d["outcome"] for d in self.ledger().rounds[0].decisions], ["합의-사실", "합의-사실"])
        return self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")

    def test_overlapping_patch_is_carried_to_next_round_and_fixed_on_new_base(self):
        self.run_to_apply_with_overlap()
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual((apply["applied"], [f for f, _ in apply["failed"]], apply["carried"]), (["r1-01"], ["r1-02"], ["r1-02"]))
        d = {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}
        self.assertEqual((d["r1-02"]["outcome"], d["r1-02"]["adopted_as"]), ("다음 회차", "합의-사실"))
        self.assertTrue(d["r1-02"]["reasons"][-1].startswith("패치 적용 실패: "))
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("| 1 | 2 | 2 | 0 | 0 | 0 | 0 |", p.stdout)
        self.assertIn("- 회차 1 다음 회차로 넘김(앞 패치와 겹침): r1-02", p.stdout)
        self.assertNotIn("실패 **r1-02**", p.stdout)
        self.assertNotIn("**r1-02**", section_of(p.stdout, "## 사람이 볼 목록"))

        result = self.tmp / "recheck.final"
        result.write_text("## 해소 여부\n- r1-01: 해소됨 — 됐다\n\nCritical\n없음.\n\nWarning\n없음.\n\nSuggestion\n없음.\n",
                          encoding="utf-8")
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result))
        self.assertIn("적용 못 해 넘긴 1건", p.stdout)
        ledger = self.ledger()
        self.assertEqual((len(ledger.rounds), ledger.status), (2, "진행"))
        self.assertEqual(ledger.rounds[1].base_commit, ledger.rounds[0].result_commit)
        self.assertEqual([(f["id"], f["carried_from"]) for f in ledger.rounds[1].findings], [("r2-01", "r1-02")])

        self.assertEqual(ledger.rounds[1].findings[0]["carried_after"], ["r1-01 calc.py:2 — add가 빼기를 한다"])

        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "2")
        args = json.loads((self.run_dir / "round-2" / "stage1-args.json").read_text(encoding="utf-8"))
        prompt = Path(args["jobs"][0]["promptPath"]).read_text(encoding="utf-8")
        self.assertIn("앞 회차의 r1-02", prompt)
        self.assertIn("- r1-01 calc.py:2 — add가 빼기를 한다", prompt)
        for name in ("r2-01-a", "r2-01-b"):
            wt = self.wt(name)
            text = (wt / "calc.py").read_text(encoding="utf-8")
            self.assertIn("return a + b", text)
            (wt / "calc.py").write_text(fix(text, "def add(a, b):", "def add(a: int, b: int):"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = results_json(self.run_dir / "round-2" / "stage1.json", [
            ("r2-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r2-01:b", worker_output("사실", "claude-haiku-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "2", "--results", str(stage1))
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "2")
        apply = json.loads((self.run_dir / "round-2" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual((apply["applied"], apply["failed"]), (["r2-01"], []))
        self.assertIn("def add(a: int, b: int):\n    return a + b", (self.repo / "calc.py").read_text(encoding="utf-8"))

    def test_carried_finding_opens_next_round_even_without_recheck(self):
        p = self.run_to_apply_with_overlap("--max-rechecks", "0")
        self.assertIn("회차 2을 연다", p.stdout)
        ledger = self.ledger()
        self.assertFalse(ledger.rounds[0].recheck_opened)
        self.assertEqual((len(ledger.rounds), ledger.status), (2, "진행"))
        self.assertEqual([f["carried_from"] for f in ledger.rounds[1].findings], ["r1-02"])
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("jobs --run-dir", p.stdout)

    def test_carried_finding_becomes_undecidable_when_full_suite_breaks(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -c \"import sys; sys.exit(3 if 'a + b' in open('calc.py').read() else 0)\""],
        }})
        self.env["CASCADE_REPOS_TOML"] = str(toml)
        self.run_to_apply_with_overlap()
        ledger = self.ledger()
        d = {x["finding_id"]: x for x in ledger.rounds[0].decisions}
        self.assertEqual((d["r1-01"]["outcome"], d["r1-02"]["outcome"]), ("판단불가", "판단불가"))
        self.assertEqual(json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))["carried"], [])
        self.assertEqual((len(ledger.rounds), ledger.status), (1, "종료"))

    def test_both_fact_without_patch_goes_to_opus_then_undecidable(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
            ("r1-02:a", worker_output("아님", "claude-sonnet-5-5")),
            ("r1-02:b", worker_output("아님", "")),
        ])
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertIn("r1-02:b 모델 ID가 비어 있다", p.stdout)
        d = {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}
        verify = json.loads((self.run_dir / "round-1" / "verify.json").read_text(encoding="utf-8"))
        self.assertEqual(verify["r1-02"], {"a": [], "b": []})
        self.assertTrue(verify["r1-01"]["a"])
        self.assertEqual((d["r1-01"]["outcome"], d["r1-01"]["patch_from"]), ("판가름", None))
        self.assertIn("패치 없음", d["r1-01"]["reasons"])
        adj_args = json.loads((self.run_dir / "round-1" / "adj-args.json").read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in adj_args["jobs"]], ["r1-01:adjudicator"])
        adj = results_json(self.run_dir / "round-1" / "adj.json", [
            ("r1-01:adjudicator", worker_output("사실", "claude-opus-5-5")),
        ])
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        d = {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}
        self.assertEqual(d["r1-01"]["outcome"], "판단불가")
        self.assertEqual(d["r1-01"]["reasons"][-1], "패치 없음")
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertEqual(self.ledger().rounds[0].adopted, [])
        self.assertEqual(self.ledger().status, "종료")

    def test_adopt_on_both_pass_takes_smaller_patch_without_opus(self):
        p = self.init("--adopt-on-both-pass")
        self.assertIn("둘 다 검증을 통과하면 작은 패치를 채택한다", p.stdout)
        self.assertTrue(self.ledger().adopt_on_both_pass)
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        (self.wt("r1-01-a") / "calc.py").write_text(
            fix(CALC, "return a - b", "total = a + b\n    return total"), encoding="utf-8")
        (self.wt("r1-01-b") / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
            ("r1-02:a", worker_output("아님", "claude-sonnet-5-5")),
            ("r1-02:b", worker_output("아님", "claude-haiku-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        d = {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}
        self.assertEqual((d["r1-01"]["outcome"], d["r1-01"]["patch_from"]), ("합의-사실", "b"))
        self.assertEqual(d["r1-01"]["reasons"], ["둘 다 검증 통과: 작은 패치 b 채택"])
        self.assertFalse((self.run_dir / "round-1" / "adj-args.json").exists())
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("return a + b\n\n\ndef ident", (self.repo / "calc.py").read_text(encoding="utf-8"))
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("| 1 | 2 | 2 | 0 | 0 | 0 | 0 |", p.stdout)
        self.assertIn("이번 실행은 --adopt-on-both-pass 로 돌았다", p.stdout)
        self.assertIn("이 규칙으로 채택한 지적 1건", p.stdout)

    def _judge_r1_01_with_test_change(self, test_calc):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            (wt / "test_calc.py").write_text(test_calc, encoding="utf-8")
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
            ("r1-02:a", worker_output("아님", "claude-sonnet-5-5")),
            ("r1-02:b", worker_output("아님", "claude-haiku-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        return {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}["r1-01"]

    def test_patch_that_cuts_existing_test_is_not_adopted(self):
        d = self._judge_r1_01_with_test_change(TEST_CALC.replace("self.assertEqual(ident(3), 3)", "pass"))
        self.assertEqual((d["outcome"], d["patch_from"]), ("판단불가", None))
        self.assertIn("기존 테스트 변경", d["reasons"])

    def test_patch_that_only_adds_tests_is_adopted(self):
        d = self._judge_r1_01_with_test_change(
            TEST_CALC + "\n    def test_ident_zero(self):\n        self.assertEqual(ident(0), 0)\n")
        self.assertEqual((d["outcome"], d["patch_from"]), ("합의-사실", "a"))
        self.assertNotIn("기존 테스트 변경", d["reasons"])

    def _recheck(self, text):
        self.run_to_apply()
        result = self.tmp / "recheck.final"
        result.write_text(text, encoding="utf-8")
        return self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result))

    def test_recheck_without_resolution_section_warns_once(self):
        p = self._recheck(RECHECK_RESULT.split("Critical", 1)[1].join(["Critical", ""]))
        self.assertIn("절이 없다", p.stdout)
        self.assertNotIn("해소 여부가 없는 지적", p.stdout)

    def test_recheck_with_unreadable_resolution_lines_warns(self):
        p = self._recheck(RECHECK_RESULT.replace("r1-01: 해소됨", "r1-01 해소됨").replace("r1-02: 해소됨", "r1-02 해소됨"))
        self.assertIn("경고: 해소 여부가 없는 지적은 해소 안 됨으로 본다: r1-01, r1-02", p.stdout)

    def test_non_utf8_patch_is_not_adopted(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            (wt / "notes.txt").write_bytes(b"na\xefve\n")
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        d = {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}
        self.assertEqual((d["r1-01"]["outcome"], d["r1-01"]["patch_from"]), ("판단불가", None))
        self.assertEqual(d["r1-01"]["reasons"][-1], "UTF-8 아닌 패치")

    def test_utf8_replacement_character_in_the_file_is_not_lossy(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            (wt / "notes.txt").write_text("대체 문자 \ufffd\n", encoding="utf-8")
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        d = {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}
        self.assertEqual(d["r1-01"]["outcome"], "합의-사실")
        self.assertNotIn("UTF-8 아닌 패치", d["r1-01"]["reasons"])

    def test_missing_full_tests_falls_back_to_tests_with_warning(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["python3 -m unittest {files}"]}})
        self.env["CASCADE_REPOS_TOML"] = str(toml)
        p = self.run_to_apply()
        self.assertIn("full_tests가 없어", p.stdout)
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertIs(apply["final_ok"], True)


    def run_to_apply_with_conflict(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        adj = self.run_dir / "round-1" / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        self.assertTrue((self.run_dir / "round-1" / "opinion-r1-02.md").is_file())
        opinion = self.tmp / "opinion.final"
        opinion.write_text("Opus와 같은 의견이다", encoding="utf-8")
        self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(opinion))
        self.assertEqual(self.ledger().opinions["r1-02"], "Opus와 같은 의견이다")
        (self.repo / "calc.py").write_text(fix(CALC, "return a - b", "return b - a"), encoding="utf-8")
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")



AUTH_LOG = "codex 시작\nERROR: unexpected status 401 Unauthorized: refresh token was already used\n"


class CliCodexResultTest(CliTestBase):
    def run_to_opinion(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        adj = self.run_dir / "round-1" / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        return self.run_dir / "round-1" / "opinion-r1-02.out"

    def opinion(self, final):
        return self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02",
                        "--result", str(final), ok=False)

    def test_missing_opinion_final_points_to_log_and_auth_failure(self):
        log = self.run_to_opinion()
        log.write_text(AUTH_LOG, encoding="utf-8")
        p = self.opinion(Path(f"{log}.final"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(str(log), p.stdout)
        self.assertIn("401 Unauthorized", p.stdout)
        self.assertIn("인증 실패", p.stdout)
        self.assertIn("codex login", p.stdout)
        self.assertNotIn("r1-02", self.ledger().opinions)

    def test_empty_opinion_final_shows_log_tail_without_auth_claim(self):
        log = self.run_to_opinion()
        log.write_text("codex 시작\nERROR: stream disconnected\n", encoding="utf-8")
        final = Path(f"{log}.final")
        final.write_text("\n", encoding="utf-8")
        with self.assertRaises(cli_steps.CliError) as ctx:
            cli._external_result_text(final, "의견", "codex")
        message = str(ctx.exception)
        self.assertIn("비어 있다", message)
        self.assertIn("stream disconnected", message)
        self.assertNotIn("인증 실패 흔적", message)
        self.assertIn("codex login", message)

    def test_empty_opinion_final_after_run_error_falls_back_to_claude(self):
        log = self.run_to_opinion()
        log.write_text("codex 시작\nERROR: stream disconnected\n", encoding="utf-8")
        final = Path(f"{log}.final")
        final.write_text("\n", encoding="utf-8")
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(final))
        self.assertIn("대체: code.opinion codex → claude — 실행 오류", p.stdout)
        self.assertNotIn("r1-02", self.ledger().opinions)
        self.assertTrue((self.run_dir / "round-1" / "opinion-args.json").is_file())

    def test_missing_log_is_reported(self):
        log = self.run_to_opinion()
        p = self.opinion(Path(f"{log}.final"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("실행 로그도 없다", p.stdout)

    def test_missing_recheck_final_points_to_log_not_workflow(self):
        self.run_to_apply()
        log = self.run_dir / "round-1" / "recheck.out"
        log.write_text(AUTH_LOG, encoding="utf-8")
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", f"{log}.final", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(str(log), p.stdout)
        self.assertIn("인증 실패", p.stdout)
        self.assertNotIn("Workflow", p.stdout)
        self.assertFalse(self.ledger().rounds[0].events[-1].get("step") == "recheck")

    def test_empty_recheck_final_points_to_log_not_workflow(self):
        self.run_to_apply()
        log = self.run_dir / "round-1" / "recheck.out"
        log.write_text(AUTH_LOG, encoding="utf-8")
        final = Path(f"{log}.final")
        final.write_text("", encoding="utf-8")
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(final), ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("비어 있다", p.stdout)
        self.assertIn(str(log), p.stdout)
        self.assertNotIn("Workflow", p.stdout)

    def test_empty_recheck_json_still_points_to_workflow(self):
        self.run_to_apply()
        result = self.run_dir / "round-1" / "recheck.json"
        result.write_text(json.dumps({"results": []}), encoding="utf-8")
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result), ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Workflow", p.stdout)


class CliDocModeTest(CliTestBase):
    PLAN = "# 계획\n\n1단계는 `calc.py`를 고친다.\n"

    def test_doc_mode_runs_checker_loop_and_applies_writer_patch(self):
        plan = "# 계획\n\n1단계는 `calc.py`를 고친다.\n\n예전 이름 `ghost.py`와 `~/.claude/x.md`는 지금 없다.\n"
        (self.repo / "plan.md").write_text(plan, encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), "--doc", str(self.repo / "plan.md"))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertEqual(self.ledger().docs, {"plan.md": str(self.repo / "plan.md")})
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        args = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in args["jobs"]], ["r1-01:a", "r1-01:b", "r1-01:writer"])

        wt = self.wt("r1-01-writer")
        (wt / "plan.md").write_text(plan.replace("고친다", "고쳐 add가 더하게 한다"), encoding="utf-8")
        commit_worktree(wt, "writer")
        out = {"verdict": "사실", "needs_design_decision": False, "reason": "모호하다", "verify": []}
        stage1 = self.run_dir / "round-1" / "stage1.json"
        stage1.write_text(json.dumps({"results": [
            {"id": "r1-01:a", "output": {**out, "model_id": "claude-sonnet-5-5"}},
            {"id": "r1-01:b", "output": {**out, "model_id": "claude-haiku-5-5"}},
            {"id": "r1-01:writer", "output": {**out, "model_id": "claude-sonnet-5-5"}},
        ]}, ensure_ascii=False), encoding="utf-8")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        checker_args = json.loads((self.run_dir / "round-1" / "checker-args.json").read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in checker_args["jobs"]], ["r1-01:checker"])
        self.assertIn("checker.json", p.stdout)
        self.assertFalse((self.run_dir / "round-1" / "decisions.json").exists())

        checker = self.run_dir / "round-1" / "checker.json"
        checker.write_text(json.dumps({"results": [{"id": "r1-01:checker", "output": {
            "verdict": "사실", "needs_design_decision": False, "reason": "해소한다", "checker_ok": True,
            "model_id": "claude-haiku-5-5"}}]}, ensure_ascii=False), encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1),
                 "--checker-results", str(checker))
        decisions = self.ledger().rounds[0].decisions
        self.assertEqual([(d["outcome"], d["patch_from"]) for d in decisions], [("합의-사실", "writer")])
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("add가 더하게", (self.repo / "plan.md").read_text(encoding="utf-8"))
        self.assertTrue((self.run_dir / "round-1" / "recheck-prompt.md").is_file())

    def _judge_doc_writer_line(self, line):
        (self.repo / "helm").mkdir()
        (self.repo / "helm" / "values-local.yaml").write_text("x: 1\n", encoding="utf-8")
        (self.repo / "plan.md").write_text(self.PLAN, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "plan")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), "--doc", str(self.repo / "plan.md"))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        wt = self.wt("r1-01-writer")
        (wt / "plan.md").write_text(self.PLAN + line, encoding="utf-8")
        commit_worktree(wt, "writer")
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
            ("r1-01:writer", worker_output("사실", "claude-sonnet-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))

    def test_doc_new_names_and_shortened_paths_reach_checker(self):
        self._judge_doc_writer_line(
            "\n노드 `execute-left/right`, `finalize-left/right`에 라벨 `replay.example.com/description`을 단다."
            " `values-local.yaml`도 고친다.\n"
        )
        checker_args = json.loads((self.run_dir / "round-1" / "checker-args.json").read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in checker_args["jobs"]], ["r1-01:checker"])
        self.assertEqual(self.ledger().rounds[0].decisions, [])

    def test_doc_missing_file_path_still_escalates(self):
        self._judge_doc_writer_line("\n자세한 내용은 `docs/없는파일.md`에 둔다.\n")
        self.assertFalse((self.run_dir / "round-1" / "checker-args.json").exists())
        decisions = self.ledger().rounds[0].decisions
        self.assertEqual([d["outcome"] for d in decisions], ["판가름"])
        self.assertIn("참조 없음: docs/없는파일.md", decisions[0]["reasons"])

    def test_doc_mode_writer_patch_touching_non_doc_file_is_not_adopted(self):
        (self.repo / "plan.md").write_text(self.PLAN, encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), "--doc", str(self.repo / "plan.md"))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        wt = self.wt("r1-01-writer")
        (wt / "plan.md").write_text(self.PLAN.replace("고친다", "고쳐 add가 더하게 한다"), encoding="utf-8")
        (wt / "auth.py").write_text("TOKEN = 1\n", encoding="utf-8")
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
            ("r1-01:writer", worker_output("사실", "claude-sonnet-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        checker = results_json(self.run_dir / "round-1" / "checker.json", [
            ("r1-01:checker", {**worker_output("사실", "claude-haiku-5-5"), "checker_ok": True}),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1),
                 "--checker-results", str(checker))
        d = self.ledger().rounds[0].decisions[0]
        self.assertEqual((d["outcome"], d["patch_from"]), ("판단불가", None))
        self.assertEqual(d["reasons"][-1], "문서 밖 파일 수정: auth.py")
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertFalse((self.repo / "auth.py").exists())

    def _run_doc_to_apply(self, doc, location):
        doc.write_text(self.PLAN, encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text(f"Warning\n1. 단계가 모호하다\n   위치: {location}:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), "--doc", str(doc))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertEqual(self.ledger().docs, {"외부/plan.md": str(doc.resolve())})
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        wt = self.wt("r1-01-writer")
        target = wt / "외부/plan.md"
        args = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        writer = next(j for j in args["jobs"] if j["id"] == "r1-01:writer")
        prompt = Path(writer["promptPath"]).read_text(encoding="utf-8")
        self.assertIn(f"- 위치: {target}:3", prompt)
        self.assertIn(f"대상 문서: `{target}`", prompt)
        self.assertIn("원본 경로(사용자 저장소의 파일이나 저장소 밖 원본)는 열지 않는다", prompt)
        self.assertEqual(self.ledger().rounds[0].findings[0]["location"], f"{location}:3")
        target.write_text(target.read_text(encoding="utf-8").replace("고친다", "고쳐 add가 더하게 한다"), encoding="utf-8")
        commit_worktree(wt, "writer")
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
            ("r1-01:writer", worker_output("사실", "claude-sonnet-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        checker = self.run_dir / "round-1" / "checker.json"
        checker.write_text(json.dumps({"results": [{"id": "r1-01:checker", "output": {
            "verdict": "사실", "needs_design_decision": False, "reason": "해소한다", "checker_ok": True,
            "model_id": "claude-haiku-5-5"}}]}, ensure_ascii=False), encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1),
                 "--checker-results", str(checker))
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual((apply["applied"], apply["failed"]), (["r1-01"], []))
        self.assertIn("add가 더하게", doc.read_text(encoding="utf-8"))
        self.assertFalse((self.repo / "외부").exists())
        prompt = (self.run_dir / "round-1" / "recheck-prompt.md").read_text(encoding="utf-8")
        self.assertIn("add가 더하게", prompt)

    def test_doc_outside_repo_is_fixed_in_place(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self._run_doc_to_apply(outside / "plan.md", "plan.md")

    def test_doc_outside_repo_with_absolute_location(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self._run_doc_to_apply(outside / "plan.md", str(outside / "plan.md"))

    def test_gitignored_doc_in_repo_is_fixed_in_place(self):
        (self.repo / ".gitignore").write_text("__pycache__/\nnotes/\n", encoding="utf-8")
        (self.repo / "notes").mkdir()
        self._run_doc_to_apply(self.repo / "notes" / "plan.md", "notes/plan.md")


class CliPrCommentTest(CliTestBase):
    def test_pr_comment_init_uses_gh_from_env(self):
        env = {**self.env, "CASCADE_GH": f"{sys.executable} {Path(__file__).resolve().parent / 'fake_gh.py'}",
               "CASCADE_FAKE_GH_FILE": str(FIXTURES / "pr-threads.json")}
        p = run_cli(["init", "--source", "pr-comment", "--input", "7", "--pr-repo", "o/r", "--me", "sch0718"],
                    cwd=self.repo, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = Ledger.load(run_dir / "ledger.json")
        self.assertEqual(ledger.source, "pr-comment")
        self.assertEqual(ledger.mode, "code")
        self.assertEqual([f["location"].split(":")[0] for f in ledger.rounds[0].findings], ["src/b.py", "src/d.py"])
        self.assertTrue(all(f["thread_id"] for f in ledger.rounds[0].findings))

    def test_pr_comment_init_warns_when_local_head_is_not_pr_head(self):
        env = {**self.env, "CASCADE_GH": f"{sys.executable} {Path(__file__).resolve().parent / 'fake_gh.py'}",
               "CASCADE_FAKE_GH_FILE": str(FIXTURES / "pr-threads.json"), "CASCADE_FAKE_GH_HEAD": "f" * 40}
        p = run_cli(["init", "--source", "pr-comment", "--input", "7", "--pr-repo", "o/r", "--me", "sch0718"],
                    cwd=self.repo, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PR #7의 head(ffffffffffff)와 다르다", p.stdout)
        env["CASCADE_FAKE_GH_HEAD"] = git(self.repo, "rev-parse", "HEAD")
        p = run_cli(["init", "--source", "pr-comment", "--input", "7", "--pr-repo", "o/r", "--me", "sch0718"],
                    cwd=self.repo, env=env)
        self.assertNotIn("head", p.stdout)

    def test_repro_commands_need_explicit_approval(self):
        body = ("add가 뺀다\n\n**확인한 내용**: 재현\n```\n$ touch REPRO_RAN\nFAILED test_calc.py\n```\n")
        items = self.tmp / "findings.json"
        items.write_text(json.dumps([{"id": "r1-01", "severity": "Warning", "location": "calc.py:2", "body": body,
                                      "thread_id": "T1"}], ensure_ascii=False), encoding="utf-8")
        p = self.cli("init", "--source", "pr-comment", "--findings-json", str(items))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        listed = self.run_dir / "round-1" / "repro-cmds.json"
        self.assertEqual(json.loads(listed.read_text(encoding="utf-8")), {"r1-01": ["touch REPRO_RAN"]})
        self.assertIn("r1-01: touch REPRO_RAN", p.stdout)
        self.assertNotIn("FAILED", listed.read_text(encoding="utf-8"))
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("--allow-repro", p.stdout)
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("사실", "claude-haiku-5-5")),
        ])
        base = self.wt("base")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertIn("재현 검증을 건너뛴다", p.stdout)
        self.assertFalse((base / "REPRO_RAN").exists())
        self.assertFalse((self.repo / "REPRO_RAN").exists())
        wrong = self.tmp / "wrong.json"
        wrong.write_text(json.dumps({"r1-01": ["rm -rf ~"]}), encoding="utf-8")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1), "--force",
                     "--allow-repro", str(wrong), ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("repro-cmds.json 와 다르다", p.stdout)
        approved = self.tmp / "approved.json"
        approved.write_text(listed.read_text(encoding="utf-8"), encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1), "--force",
                 "--allow-repro", str(approved))
        self.assertTrue((base / "REPRO_RAN").exists())
        self.assertFalse((self.repo / "REPRO_RAN").exists())
        self.assertEqual(self.ledger().rounds[0].approved_repro, {"r1-01": ["touch REPRO_RAN"]})
        self.assertEqual(self.ledger().rounds[0].decisions[0]["outcome"], "판가름")
        listed.write_text(json.dumps({"r1-01": ["touch TAMPERED"]}), encoding="utf-8")
        wt = self.wt("r1-01-adj")
        (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
        commit_worktree(wt, "adjudicator")
        (base / "REPRO_RAN").unlink()
        adj = results_json(self.run_dir / "round-1" / "adj.json", [
            ("r1-01:adjudicator", worker_output("사실", "claude-opus-5-5")),
        ])
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        self.assertTrue((base / "REPRO_RAN").exists())
        for where in (base, wt, self.repo):
            self.assertFalse((where / "TAMPERED").exists(), where)

    def test_pr_comment_without_config_exits_4(self):
        p = run_cli(["init", "--source", "pr-comment", "--input", "7", "--pr-repo", "o/r"],
                    cwd=self.repo, env={"CASCADE_REPOS_TOML": str(self.tmp / "none.toml")})
        self.assertEqual(p.returncode, 4)


class CliInitTest(CliTestBase):
    def test_zero_findings_warns_and_ends(self):
        raw = self.tmp / "empty.final"
        raw.write_text("검토 결과\n- [P2] 위치가 빠진 항목\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-code", "--input", str(raw))
        self.assertIn("지적 0건 — 원문을 확인하라", p.stdout)
        self.assertIn("검토 결과", p.stdout)
        self.assertIn("경고: 형식을 읽지 못한 항목 1건: - [P2] 위치가 빠진 항목", p.stdout)
        self.assertIn("status=종료", p.stdout)
        self.assertFalse((self.repo / ".cascade-fix").exists())

    def test_partly_unparsed_code_items_warn(self):
        raw = self.tmp / "code.final"
        raw.write_text("- [P2] add가 뺀다 — calc.py:2\n  본문.\n- [P2] 위치가 빠진 항목\n  본문.\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-code", "--input", str(raw))
        self.assertIn("경고: 형식을 읽지 못한 항목 1건: - [P2] 위치가 빠진 항목", p.stdout)
        self.assertIn("run_dir=", p.stdout)

    def test_doc_parse_error_exits_5_with_line(self):
        raw = self.tmp / "doc.final"
        raw.write_text("Critical\n1. 위치 없는 항목\n   근거: 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertIn("줄 2", p.stdout + p.stderr)

    def test_broken_toml_exits_5(self):
        bad = self.tmp / "bad.toml"
        bad.write_text("[repos\n", encoding="utf-8")
        p = run_cli(["init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final")],
                    cwd=self.repo, env={"CASCADE_REPOS_TOML": str(bad)})
        self.assertEqual(p.returncode, 5)

    def test_codex_doc_runs_without_repo_config(self):
        doc = self.tmp / "plan.md"
        doc.write_text("# 계획\n\n1단계는 `calc.py`를 고친다.\n", encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: 외부/plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = run_cli(["init", "--source", "codex-doc", "--input", str(raw), "--doc", str(doc)],
                    cwd=self.repo, env={"CASCADE_REPOS_TOML": str(self.tmp / "none.toml")})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = Ledger.load(run_dir / "ledger.json")
        self.assertEqual(ledger.mode, "doc")
        self.assertEqual(ledger.docs, {"외부/plan.md": str(doc)})
        self.assertIn("외부/plan.md", git(self.repo, "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", ledger.snapshot["commit"]))


class CliSafetyTest(CliTestBase):
    def test_judge_stops_when_user_tree_changes_after_jobs(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        args = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        prompt = Path(args["jobs"][0]["promptPath"]).read_text(encoding="utf-8")
        self.assertIn(f"- 위치: {self.wt('r1-01-a')}/calc.py:", prompt)
        self.assertEqual(set(args["jobs"][0]), {"id", "label", "model", "effort", "kind", "promptPath"})
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        (self.repo / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1), ok=False)
        self.assertEqual(p.returncode, 6, p.stdout)
        self.assertIn("작업 트리", p.stdout)
        self.assertIn("git diff --stat", p.stdout)
        self.assertIn("calc.py", p.stdout)
        self.assertFalse(cli_steps.step_done(self.ledger().rounds[0], "judge"))

        (self.repo / "calc.py").write_text(CALC, encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))

    def _jobs_then_stage1(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        return stage1

    def test_judge_warns_and_continues_when_only_branches_change(self):
        git(self.repo, "branch", "other-session")
        stage1 = self._jobs_then_stage1()
        git(self.repo, "branch", "-D", "other-session")
        git(self.repo, "branch", "fetched")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertIn("경고", p.stdout)
        self.assertIn("- refs/heads/other-session", p.stdout)
        self.assertIn("+ refs/heads/fetched", p.stdout)
        self.assertTrue(cli_steps.step_done(self.ledger().rounds[0], "judge"))

    def test_judge_stops_when_user_checkout_switches_branch(self):
        stage1 = self._jobs_then_stage1()
        git(self.repo, "switch", "-q", "-c", "side")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1), ok=False)
        self.assertEqual(p.returncode, 6, p.stdout)
        self.assertIn("refs/heads/main → refs/heads/side", p.stdout)
        self.assertFalse(cli_steps.step_done(self.ledger().rounds[0], "judge"))

    def test_judge_stops_when_worker_worktree_attaches_to_branch(self):
        stage1 = self._jobs_then_stage1()
        git(self.wt("r1-01-a"), "switch", "-q", "-c", "worker-branch")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1), ok=False)
        self.assertEqual(p.returncode, 6, p.stdout)
        self.assertIn("r1-01-a refs/heads/worker-branch", p.stdout)
        self.assertFalse(cli_steps.step_done(self.ledger().rounds[0], "judge"))

    def test_baseline_failures_are_excluded_and_setup_runs_in_worktrees(self):
        lint = "python3 -c 'import sys; sys.exit(1)'"
        setup = ("python3 -c \"import sys, pathlib; pathlib.Path('__pycache__').mkdir(exist_ok=True); "
                 "pathlib.Path('__pycache__/root.txt').write_text(sys.argv[1])\" {root}")
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -m unittest discover -s ."],
            "lint": [lint],
            "setup": [setup, "ln -s {root} upstream"],
        }})
        self.env["CASCADE_REPOS_TOML"] = str(toml)
        self.run_to_apply()
        baseline = json.loads((self.run_dir / "round-1" / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["failed"], [lint])
        verify = json.loads((self.run_dir / "round-1" / "verify.json").read_text(encoding="utf-8"))
        self.assertTrue(any(r["cmd"].startswith("[기준선 실패·판정 제외] ") for r in verify["r1-01"]["a"]))
        self.assertEqual((self.wt("base") / "__pycache__" / "root.txt").read_text(encoding="utf-8"), str(self.repo))
        patch = (self.run_dir / "round-1" / "patch-r1-01-a.diff").read_text(encoding="utf-8")
        self.assertNotIn("__pycache__", patch)
        self.assertNotIn("upstream", patch)
        self.assertTrue((self.wt("base") / "upstream").is_symlink())
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual((apply["applied"], apply["failed"]), (["r1-01", "r1-02"], []))
        self.assertFalse((self.repo / "upstream").exists())
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("## 기준선 실패", p.stdout)
        self.assertIn(lint, p.stdout)
        self.assertNotIn("python3 -m unittest {files}", baseline["failed"])

    def test_full_suite_failing_after_fix_is_undecidable_even_if_baseline_failed(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -c 'import sys; sys.exit(3)'"],
        }})
        self.env["CASCADE_REPOS_TOML"] = str(toml)
        p = self.run_to_apply()
        self.assertIn("전체 테스트 실패(기준선도 실패)", p.stdout)
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertIs(apply["final_ok"], False)
        self.assertIs(apply["baseline_failed"], True)
        self.assertEqual(self.ledger().rounds[0].adopted, [])
        for d in self.ledger().rounds[0].decisions:
            self.assertEqual(d["outcome"], "판단불가")
            self.assertIn("전체 테스트 실패(기준선도 실패)", d["reasons"])
        self.assertIn("return a * b", (self.repo / "calc.py").read_text(encoding="utf-8"))
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("기준선(수정 전)에서도 전체 테스트가 실패했다", p.stdout)

    def test_tests_failing_at_baseline_still_judge_adjudicator_patch(self):
        test_calc = TEST_CALC.replace("from calc import ident", "from calc import add, ident") + (
            "\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        (self.repo / "test_calc.py").write_text(test_calc, encoding="utf-8")
        git(self.repo, "commit", "-qam", "add 시험")
        items = self.tmp / "findings.json"
        items.write_text(json.dumps([{"id": "r1-01", "severity": "Warning", "location": "calc.py:14",
                                      "body": "mul이 더한다"}], ensure_ascii=False), encoding="utf-8")
        p = self.cli("init", "--source", "codex-code", "--findings-json", str(items))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertEqual(json.loads((self.run_dir / "round-1" / "baseline.json").read_text(encoding="utf-8"))["failed"], [])
        stage1 = results_json(self.run_dir / "round-1" / "stage1.json", [
            ("r1-01:a", worker_output("사실", "claude-sonnet-5-5")),
            ("r1-01:b", worker_output("아님", "claude-haiku-5-5")),
        ])
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertEqual(self.ledger().rounds[0].decisions[0]["outcome"], "판가름")
        wt = self.wt("r1-01-adj")
        broken = fix(fix(CALC, "return a + b", "return a * b"), "    return x\n", "    return x + 1\n")
        (wt / "calc.py").write_text(broken, encoding="utf-8")
        commit_worktree(wt, "adjudicator")
        adj = results_json(self.run_dir / "round-1" / "adj.json", [
            ("r1-01:adjudicator", worker_output("사실", "claude-opus-5-5")),
        ])
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        d = self.ledger().rounds[0].decisions[0]
        self.assertEqual(d["outcome"], "판단불가")
        self.assertIn("판가름 패치 검증 실패", d["reasons"])
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), CALC)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_apply_rerun_after_patching_does_not_reapply(self):
        self.run_to_apply()
        ledger = self.ledger()
        rnd = ledger.rounds[0]
        rnd.events = [e for e in rnd.events if e["step"] != "apply"]
        rnd.recheck_opened = False
        ledger.status = "진행"
        ledger.save(self.run_dir / "ledger.json")
        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("적용을 건너뛰고", p.stdout)
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual((apply["applied"], apply["failed"]), (["r1-01", "r1-02"], []))
        self.assertTrue(apply["final_ok"])
        self.assertEqual(self.ledger().rounds[0].adopted, ["r1-01", "r1-02"])
        self.assertEqual(sum(1 for e in self.ledger().rounds[0].events if e["step"] == "apply-patched"), 1)

    def test_apply_rerun_after_snapshot_failure_keeps_patched_record(self):
        state = {}

        def break_snapshot_branch():
            state["branch"] = self.ledger().snapshot["branch"]
            state["commit"] = git(self.repo, "rev-parse", state["branch"])
            git(self.repo, "branch", "-D", state["branch"])

        cli = self.cli
        self.cli = lambda *args, ok=True: cli(*args, ok=ok and args[0] != "apply")
        p = self.run_to_apply(before_apply=break_snapshot_branch)
        self.cli = cli
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("return a * b", (self.repo / "calc.py").read_text(encoding="utf-8"))
        self.assertTrue(any(e["step"] == "apply-patched" for e in self.ledger().rounds[0].events))
        git(self.repo, "branch", state["branch"], state["commit"])
        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("적용을 건너뛰고", p.stdout)
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual((apply["applied"], apply["failed"]), (["r1-01", "r1-02"], []))
        rnd = self.ledger().rounds[0]
        self.assertEqual(rnd.adopted, ["r1-01", "r1-02"])
        self.assertTrue(rnd.result_commit)
        self.assertNotEqual(rnd.result_commit, state["commit"])

    def test_recheck_diff_excludes_test_artifacts_and_other_user_edits(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -m unittest discover -s . && python3 -c \"open('artifact.txt', 'w').write('x')\""],
        }})
        self.env["CASCADE_REPOS_TOML"] = str(toml)

        def user_edit():
            (self.repo / "test_calc.py").write_text(TEST_CALC + "# 사용자 메모\n", encoding="utf-8")

        self.run_to_apply(before_apply=user_edit)
        self.assertTrue((self.repo / "artifact.txt").exists())
        result = self.ledger().rounds[0].result_commit
        tree = git(self.repo, "ls-tree", "-r", "--name-only", result)
        self.assertNotIn("artifact.txt", tree)
        prompt = (self.run_dir / "round-1" / "recheck-prompt.md").read_text(encoding="utf-8")
        self.assertIn("return a * b", prompt)
        self.assertNotIn("artifact.txt", prompt)
        self.assertNotIn("사용자 메모", prompt)
        self.assertEqual(json.loads((self.run_dir / "round-1" / "recheck-scope.json").read_text(encoding="utf-8")), ["calc.py"])

    def test_dry_run_rounds_build_on_result_worktree(self):
        self.run_to_apply("--dry-run")
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), CALC)
        rnd = self.ledger().rounds[0]
        self.assertTrue(rnd.result_commit)
        self.assertNotEqual(rnd.result_commit, rnd.base_commit)
        result_wt = Path((self.run_dir / "round-1" / "recheck-repo.txt").read_text(encoding="utf-8").strip())
        self.assertEqual(result_wt, self.wt("result-r1"))
        self.assertIn("return a * b", (result_wt / "calc.py").read_text(encoding="utf-8"))
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn(f"--repo {result_wt}", p.stdout)
        self.assertIn("return a * b", (self.run_dir / "round-1" / "recheck-prompt.md").read_text(encoding="utf-8"))
        result = self.tmp / "recheck.final"
        result.write_text(
            "## 해소 여부\n- r1-01: 해소됨 — 됐다\n- r1-02: 해소됨 — 됐다\n\n"
            "Critical\n1. mul이 정수를 돌려주지 않는다\n   위치: calc.py:14\n   근거: 실수\n   제안: int\n\n"
            "Warning\n없음.\n\nSuggestion\n없음.\n",
            encoding="utf-8",
        )
        self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result))
        self.assertEqual(self.ledger().rounds[1].base_commit, rnd.result_commit)
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "2")
        text = (self.wt("r2-01-a") / "calc.py").read_text(encoding="utf-8")
        self.assertIn("return a + b\n\n\ndef ident", text)
        self.assertIn("return a * b", text)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_out_of_scope_recheck_finding_is_held(self):
        self.run_to_apply()
        result = self.tmp / "recheck.final"
        result.write_text(
            "## 해소 여부\n- **r1-01**: 해소됨 — 됐다\n- `r1-02`: 해소됨 — 됐다\n\n"
            "Critical\n1. 다른 모듈이 깨져 있다\n   위치: other.py:3\n   근거: 본 적 있다\n   제안: 고친다\n\n"
            "Warning\n없음.\n\nSuggestion\n없음.\n",
            encoding="utf-8",
        )
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result))
        self.assertIn("해소 2건", p.stdout)
        ledger = self.ledger()
        self.assertEqual((len(ledger.rounds), ledger.status), (1, "종료"))
        held = [d for d in ledger.rounds[0].decisions if d["outcome"] == "보류후보"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["reasons"], ["재검토 범위 밖"])


class CliResumeTest(unittest.TestCase):
    def test_resume_opens_final_round_with_chosen_findings(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            held = [
                {"id": "r2-01", "source": "codex-code", "severity": "Suggestion", "location": "calc.py:5",
                 "body": "힌트", "evidence": "", "thread_id": None, "outdated": False},
                {"id": "r2-02", "source": "codex-code", "severity": "Warning", "location": "calc.py:9",
                 "body": "부호", "evidence": "", "thread_id": None, "outdated": False},
            ]
            rnd = RoundRecord(
                no=1, findings=held,
                decisions=[{"finding_id": f["id"], "outcome": "보류후보", "patch_from": None, "reasons": []} for f in held],
                adopted=[], recheck_opened=True,
            )
            Ledger(run_id="cf-x", source="codex-code", mode="code", root=td, snapshot={}, dry_run=False,
                   rounds=[rnd], status="종료").save(run_dir / "ledger.json")
            choices = Path(td) / "choices.json"
            choices.write_text(json.dumps({"r2-01": "보류", "r2-02": "지금 반영"}, ensure_ascii=False), encoding="utf-8")
            p = run_cli(["resume", "--run-dir", str(run_dir), "--choices", str(choices)], cwd=td)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            ledger = Ledger.load(run_dir / "ledger.json")
            self.assertEqual(ledger.hold_choices, {"r2-01": "보류", "r2-02": "지금 반영"})
            self.assertEqual(ledger.status, "진행")
            new = ledger.rounds[-1]
            self.assertEqual((new.no, new.final), (2, True))
            self.assertEqual([f["id"] for f in new.findings], ["r2-02"])
            written = json.loads((run_dir / "round-2" / "findings.json").read_text(encoding="utf-8"))
            self.assertEqual([f["id"] for f in written], ["r2-02"])

            bad = Path(td) / "bad.json"
            bad.write_text(json.dumps({"r9-01": "지금 반영"}, ensure_ascii=False), encoding="utf-8")
            p = run_cli(["resume", "--run-dir", str(run_dir), "--choices", str(bad)], cwd=td)
            self.assertNotEqual(p.returncode, 0)


class CliStepsTest(unittest.TestCase):
    def test_gh_call_that_hangs_is_a_failed_call(self):
        from cascade import cli
        old = cli.GH_TIMEOUT
        cli.GH_TIMEOUT = 1
        try:
            p = cli._run_gh([sys.executable, "-c", "import time; time.sleep(30)"])
        finally:
            cli.GH_TIMEOUT = old
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def _ledger(self, severity):
        done = [RoundRecord(no=n, findings=[], decisions=[], adopted=[], recheck_opened=True) for n in (1, 2)]
        rnd = RoundRecord(no=3, findings=[{"id": "r3-01", "severity": severity}], decisions=[],
                          adopted=["r3-01"], recheck_opened=False)
        return Ledger(run_id="cf-x", source="codex-code", mode="code", root="/x", snapshot={}, dry_run=False,
                      max_rechecks=2, rounds=[*done, rnd]), rnd

    def test_adopted_critical_opens_recheck_past_limit(self):
        ledger, rnd = self._ledger("Critical")
        self.assertTrue(cli_steps.recheck_needed(ledger, rnd))

    def test_adopted_warning_past_limit_does_not_recheck(self):
        ledger, rnd = self._ledger("Warning")
        self.assertFalse(cli_steps.recheck_needed(ledger, rnd))

    def test_carried_ids_only_for_failures_that_overlap_an_applied_patch(self):
        def diff(path):
            return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-x\n+y\n"
        patches = {"r1-01": diff("a.py"), "r1-02": diff("a.py"), "r1-03": diff("b.py"), "r1-04": diff("a.py")}
        findings = [{"id": "r1-01"}, {"id": "r1-02"}, {"id": "r1-03"}, {"id": "r1-04", "carried_from": "r0-01"}]
        rnd = RoundRecord(no=1, findings=findings, decisions=[], adopted=[], recheck_opened=False)
        failed = ["r1-02", "r1-03", "r1-04"]
        self.assertEqual(cli_steps.carried_ids(rnd, ["r1-01"], failed, patches), {"r1-02"})
        self.assertEqual(cli_steps.carried_ids(rnd, [], failed, patches), set())
        rnd.final = True
        self.assertEqual(cli_steps.carried_ids(rnd, ["r1-01"], failed, patches), set())

    def test_map_location_to_worktree_keys(self):
        docs = {"외부/x.md": "/w/other/docs/plans/x.md", "docs/a.md": "/repo/docs/a.md"}
        root = Path("/repo")
        self.assertEqual(cli_steps.map_location("docs/plans/x.md:690", root, docs), "외부/x.md:690")
        self.assertEqual(cli_steps.map_location("/w/other/docs/plans/x.md:5", root, docs), "외부/x.md:5")
        self.assertEqual(cli_steps.map_location("docs/a.md:3", root, docs), "docs/a.md:3")
        self.assertEqual(cli_steps.map_location("/repo/src/b.py:7", root, {}), "src/b.py:7")
        self.assertEqual(cli_steps.map_location("src/b.py:7", root, docs), "src/b.py:7")
        self.assertEqual(cli_steps.map_location("-", root, docs), "-")

    def test_next_step_paths_are_shell_quoted(self):
        self.assertIn("--run-dir '/a b/run'", cli_steps._cmd("report", Path("/a b/run")))


class FinalSuiteRecordTest(CliTestBase):
    """전체 검사 직전 판정(decisions-pre-final.json)과 전체 검사 이력(apply.json의 final_history)."""

    def failing_full_suite(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -c \"import sys; sys.exit(3 if 'a * b' in open('calc.py').read() else 0)\""],
        }})
        self.env["CASCADE_REPOS_TOML"] = str(toml)

    def read(self, name):
        return json.loads((self.run_dir / "round-1" / name).read_text(encoding="utf-8"))

    def apply_force(self):
        return self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1", "--force")

    def test_pre_final_decisions_written_before_final_suite(self):
        self.failing_full_suite()
        self.run_to_apply()
        pre = {d["finding_id"]: d for d in self.read("decisions-pre-final.json")}
        self.assertEqual(pre["r1-01"]["outcome"], "합의-사실")
        self.assertEqual((pre["r1-02"]["outcome"], pre["r1-02"]["patch_from"]), ("판가름", "adjudicator"))
        self.assertEqual({d["finding_id"]: d["outcome"] for d in self.read("decisions.json")},
                         {"r1-01": "판단불가", "r1-02": "판단불가"})

    def test_pre_final_survives_apply_rerun(self):
        self.failing_full_suite()
        self.run_to_apply()
        before = (self.run_dir / "round-1" / "decisions-pre-final.json").read_text(encoding="utf-8")
        self.apply_force()
        self.assertEqual((self.run_dir / "round-1" / "decisions-pre-final.json").read_text(encoding="utf-8"), before)
        self.assertEqual(self.read("decisions-pre-final.json")[0]["outcome"], "합의-사실")

    def test_pre_final_survives_resume_after_patching(self):
        self.failing_full_suite()
        self.run_to_apply()
        before = (self.run_dir / "round-1" / "decisions-pre-final.json").read_text(encoding="utf-8")
        ledger = self.ledger()
        ledger.rounds[0].events = [e for e in ledger.rounds[0].events if e["step"] != "apply"]
        ledger.status = "진행"
        ledger.save(self.run_dir / "ledger.json")
        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("적용을 건너뛰고", p.stdout)
        self.assertEqual((self.run_dir / "round-1" / "decisions-pre-final.json").read_text(encoding="utf-8"), before)

    def test_dry_run_writes_neither_pre_final_nor_history(self):
        self.run_to_apply("--dry-run")
        self.assertFalse((self.run_dir / "round-1" / "decisions-pre-final.json").exists())
        self.assertNotIn("final_history", self.read("apply.json"))

    def test_final_history_records_a_passing_suite(self):
        self.run_to_apply()
        apply = self.read("apply.json")
        [entry] = apply["final_history"]
        self.assertEqual((entry["final_ok"], entry["baseline_failed"], entry["interference"], entry["applied"]),
                         (True, False, False, ["r1-01", "r1-02"]))
        self.assertTrue(entry["at"])
        self.assertIs(apply["final_ok"], True)

    def test_final_history_keeps_failure_after_force_rerun(self):
        self.failing_full_suite()
        self.run_to_apply()
        self.apply_force()
        apply = self.read("apply.json")
        hist = apply["final_history"]
        self.assertEqual([h["final_ok"] for h in hist], [False, None])
        self.assertEqual((hist[0]["interference"], hist[0]["baseline_failed"], hist[0]["applied"]),
                         (True, False, ["r1-01", "r1-02"]))
        self.assertEqual((hist[1]["interference"], hist[1]["applied"]), (False, []))
        self.assertIsNone(apply["final_ok"])
        self.assertIs(apply["interference"], True)

    def drop_history(self):
        path = self.run_dir / "round-1" / "apply.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["final_history"]
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_old_apply_json_without_history_is_seeded_before_a_forced_rerun(self):
        self.failing_full_suite()
        self.run_to_apply()
        self.drop_history()
        self.apply_force()
        hist = self.read("apply.json")["final_history"]
        self.assertEqual([h["final_ok"] for h in hist], [False, None])
        self.assertEqual((hist[0]["interference"], hist[0]["baseline_failed"], hist[0]["applied"]),
                         (True, False, ["r1-01", "r1-02"]))

    def test_rerun_after_a_final_failure_does_not_write_pre_final_from_changed_decisions(self):
        self.failing_full_suite()
        self.run_to_apply()
        self.drop_history()
        (self.run_dir / "round-1" / "decisions-pre-final.json").unlink()
        self.apply_force()
        self.assertFalse((self.run_dir / "round-1" / "decisions-pre-final.json").exists())

    def test_second_forced_rerun_of_an_old_failed_record_does_not_write_pre_final(self):
        self.failing_full_suite()
        self.run_to_apply()
        self.drop_history()
        (self.run_dir / "round-1" / "decisions-pre-final.json").unlink()
        self.apply_force()
        apply = self.read("apply.json")
        self.assertIsNone(apply["final_ok"])
        self.assertIs(apply["interference"], True)
        self.apply_force()
        self.assertFalse((self.run_dir / "round-1" / "decisions-pre-final.json").exists())

    def test_old_passing_apply_json_is_seeded_and_still_gets_pre_final(self):
        self.run_to_apply()
        self.drop_history()
        (self.run_dir / "round-1" / "decisions-pre-final.json").unlink()
        self.apply_force()
        hist = self.read("apply.json")["final_history"]
        self.assertEqual([h["final_ok"] for h in hist], [True, True])
        self.assertEqual(hist[0]["applied"], ["r1-01", "r1-02"])
        self.assertEqual(self.read("decisions-pre-final.json")[0]["outcome"], "합의-사실")

    def test_final_history_records_baseline_failure(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"], "full_tests": ["python3 -c 'import sys; sys.exit(3)'"]}})
        self.env["CASCADE_REPOS_TOML"] = str(toml)
        self.run_to_apply()
        [entry] = self.read("apply.json")["final_history"]
        self.assertEqual((entry["final_ok"], entry["baseline_failed"]), (False, True))


class LossyRecordTest(CliTestBase):
    def judge_with_notes(self, bad_roles):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for role in ("a", "b"):
            wt = self.wt(f"r1-01-{role}")
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            (wt / "notes.txt").write_bytes(b"na\xefve\n" if role in bad_roles else "naive\n".encode())
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        return json.loads((self.run_dir / "round-1" / "lossy.json").read_text(encoding="utf-8"))

    def test_judge_records_lossy_per_worker(self):
        lossy = self.judge_with_notes({"a"})
        self.assertEqual(lossy["r1-01"], {"a": True, "b": False})

    def test_judge_records_lossy_for_both_workers_even_when_neither_is_adopted(self):
        lossy = self.judge_with_notes({"a", "b"})
        self.assertEqual(lossy["r1-01"], {"a": True, "b": True})
        d = {x["finding_id"]: x for x in self.ledger().rounds[0].decisions}
        self.assertEqual(d["r1-01"]["reasons"][-1], "UTF-8 아닌 패치")

    def test_every_judged_finding_has_an_entry(self):
        lossy = self.judge_with_notes(set())
        self.assertEqual(lossy, {"r1-01": {"a": False, "b": False}, "r1-02": {"a": False, "b": False}})


if __name__ == "__main__":
    unittest.main()
