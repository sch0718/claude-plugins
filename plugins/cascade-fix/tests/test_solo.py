import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import select_profiles  # noqa: E402
import solo  # noqa: E402
from cascade import jobs, usage  # noqa: E402
from helpers import git, make_repo, write_repos_toml  # noqa: E402

CALC = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return None\n"
TEST_CALC = """import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 1), 2)
"""
FINDING = {"id": "r1-01", "source": "codex-code", "severity": "Warning", "location": "calc.py:2",
           "body": "add가 더하지 않고 뺀다", "evidence": "add(2, 3)이 -1을 돌려준다", "thread_id": None, "outdated": False}
TRUTH_MARK = "정답-패치-표식-7f3a"

FAKE_CLAUDE = r'''#!{python}
import json, os, sys, time, uuid
from pathlib import Path

spec = json.loads(Path(os.environ["FAKE_SOLO_SPEC"]).read_text(encoding="utf-8"))
log = Path(os.environ["FAKE_SOLO_LOG"])
log.mkdir(parents=True, exist_ok=True)
n = len(list(log.glob("call-*.json"))) + 1
prompt = sys.stdin.read()
(log / f"call-{n}.json").write_text(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                                               "claudecode": os.environ.get("CLAUDECODE")}), encoding="utf-8")
time.sleep(spec.get("sleep", 0))
for name, content in spec.get("write", {}).items():
    path = Path(os.getcwd()) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
if "stdout" in spec:
    print(spec["stdout"])
    sys.exit(spec.get("exit", 0))
session = str(uuid.uuid4())
project = Path(os.environ["FAKE_SOLO_PROJECTS"]) / "-proj"
project.mkdir(parents=True, exist_ok=True)
rows = [{"type": "assistant", "timestamp": "2026-10-26T01:00:00Z",
         "message": {"id": f"m{i}", "model": "claude-opus-5-5",
                     "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
                               "cache_creation_input_tokens": 0}}} for i in range(spec.get("messages", 2))]
(project / f"{session}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
out = {"type": "result", "subtype": "success", "is_error": spec.get("is_error", False), "session_id": session,
       "result": spec.get("result", "끝"), "total_cost_usd": 0.5, "duration_ms": 1234, "num_turns": 3}
if "structured" in spec:
    out["structured_output"] = spec["structured"]
print(json.dumps(out, ensure_ascii=False))
'''


def answer(verdict="사실", design=False, verify=None):
    return {"verdict": verdict, "needs_design_decision": design, "reason": "재현함",
            "verify": verify or [], "model_id": "claude-opus-5-5"}


class SoloTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"calc.py": CALC, "test_calc.py": TEST_CALC})
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": [f"{sys.executable} -m unittest -q test_calc"],
        }})
        self.projects = self.tmp / "projects"
        self.log = self.tmp / "log"
        self.fake = self.tmp / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE.replace("{python}", sys.executable), encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.out = self.tmp / "out"

    def item(self, item_id="calc-1", verdict="사실"):
        return {"id": item_id, "repo": str(self.repo), "commit": self.commit, "source": "codex-code", "doc": None,
                "finding": dict(FINDING), "truth": {"verdict": verdict, "accepted_patch": TRUTH_MARK},
                "baseline_opus_calls": 1}

    def spec(self, **spec):
        path = self.tmp / "spec.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        os.environ["FAKE_SOLO_SPEC"] = str(path)
        os.environ["FAKE_SOLO_LOG"] = str(self.log)
        os.environ["FAKE_SOLO_PROJECTS"] = str(self.projects)
        self.addCleanup(os.environ.pop, "FAKE_SOLO_SPEC", None)
        self.addCleanup(os.environ.pop, "FAKE_SOLO_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_SOLO_PROJECTS", None)

    def options(self, **kw):
        return solo.Options(**{"claude": str(self.fake), "projects_root": self.projects, "timeout": 30, **kw})

    def run_one(self, item=None, **kw):
        item = item or self.item()
        solo.run_item(item, self.out, self.toml, self.options(**kw))
        return json.loads((self.out / item["id"] / "result.json").read_text(encoding="utf-8"))

    def calls(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.log.glob("call-*.json"))]


class PromptTest(SoloTestBase):
    def prompt(self):
        wt = self.tmp / "wt"
        return solo.build_prompt(self.item(), wt, solo.load_cfg(self.repo, self.toml))

    def test_keeps_design_criteria_from_worker_prompt(self):
        template = (ROOT / "prompts" / "worker-code.md").read_text(encoding="utf-8")
        criteria = template.split("## 설계 결정 기준", 1)[1].split("## 작업 위치", 1)[0]
        self.assertIn("## 설계 결정 기준" + criteria, self.prompt())

    def test_drops_two_worker_framing(self):
        text = self.prompt()
        self.assertNotIn("두 작업자", text)
        self.assertNotIn("작업자 {role}", text)
        self.assertNotIn("{", text.split("## 출력", 1)[0].replace("{files}", ""))

    def test_has_finding_worktree_and_verify_commands(self):
        text = self.prompt()
        self.assertIn(FINDING["body"], text)
        self.assertIn(str(self.tmp / "wt"), text)
        self.assertIn(f"{sys.executable} -m unittest -q test_calc", text)
        self.assertIn("커밋하지 않는다", text)

    def test_does_not_leak_truth(self):
        self.assertNotIn(TRUTH_MARK, self.prompt())

    def test_missing_anchor_raises(self):
        with self.assertRaises(ValueError):
            solo.solo_template("# 다른 제목\n\n본문\n")


class VerdictTest(unittest.TestCase):
    def test_mapping(self):
        cases = [
            (answer("사실"), "diff", solo.VERDICT_FACT),
            (answer("사실", design=True), "diff", solo.VERDICT_NONE),
            (answer("사실"), "", solo.VERDICT_NONE),
            (answer("아님"), "", solo.VERDICT_NO),
            (answer("재현 불가"), "", solo.VERDICT_UNREPRODUCIBLE),
            (answer("못 정함"), "", solo.VERDICT_NONE),
            (None, "diff", solo.VERDICT_NONE),
        ]
        for output, patch, want in cases:
            with self.subTest(output=output, patch=patch):
                self.assertEqual(solo.map_verdict(output, patch), want)


class ParseTest(unittest.TestCase):
    def envelope(self, **kw):
        return json.dumps({"type": "result", "is_error": False, "session_id": "s", "result": "", **kw}, ensure_ascii=False)

    def test_structured_output(self):
        status, output, meta = solo.parse_output(self.envelope(structured_output=answer()))
        self.assertEqual((status, output["verdict"], meta["session_id"]), (solo.STATUS_OK, "사실", "s"))

    def test_falls_back_to_json_block_in_result(self):
        text = "설명\n```json\n" + json.dumps(answer("아님"), ensure_ascii=False) + "\n```"
        status, output, _ = solo.parse_output(self.envelope(result=text))
        self.assertEqual((status, output["verdict"]), (solo.STATUS_OK, "아님"))

    def test_error_envelope(self):
        status, output, _ = solo.parse_output(self.envelope(is_error=True, result="한도"))
        self.assertEqual((status, output), (solo.STATUS_ERROR, None))

    def test_not_json(self):
        status, output, _ = solo.parse_output("오류")
        self.assertEqual((status, output), (solo.STATUS_ERROR, None))

    def test_schema_mismatch_is_invalid(self):
        status, output, _ = solo.parse_output(self.envelope(structured_output={"verdict": "아마"}))
        self.assertEqual((status, output), (solo.STATUS_INVALID, None))


class RunItemTest(SoloTestBase):
    def test_fact_with_passing_patch(self):
        self.spec(write={"calc.py": FIXED}, structured=answer("사실"))
        r = self.run_one()
        self.assertEqual((r["status"], r["verdict"], r["verify_state"]), (solo.STATUS_OK, solo.VERDICT_FACT, solo.VERIFY_PASS))
        patch = (self.out / "calc-1" / "patch.diff").read_text(encoding="utf-8")
        self.assertIn("+    return a + b", patch)
        self.assertFalse((self.out / "calc-1" / "wt").exists())
        self.assertNotIn("calc-1", git(self.repo, "worktree", "list"))

    def test_command_line(self):
        self.spec(structured=answer("아님"))
        self.run_one()
        call = self.calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[0], "-p")
        for flag, value in (("--model", "claude-opus-5-5"), ("--effort", "high"), ("--output-format", "json"),
                            ("--setting-sources", "project"), ("--permission-mode", "acceptEdits"),
                            ("--allowedTools", "Bash")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), jobs.WORKER_SCHEMA)
        self.assertEqual(Path(call["cwd"]).resolve(), (self.out / "calc-1" / "wt").resolve())
        self.assertIn(FINDING["body"], call["prompt"])
        self.assertIsNone(call["claudecode"])

    def test_usage_from_session_record(self):
        self.spec(structured=answer("아님"), messages=3)
        r = self.run_one()
        want = 3 * usage.cost("claude-opus-5-5", 10, 100, 1000, 0)
        self.assertAlmostEqual(r["claude_cost"], want)
        self.assertEqual(r["cli_cost_usd"], 0.5)
        self.assertEqual(r["usage"][0]["model"], "claude-opus-5-5")
        self.assertTrue(list((self.out / "calc-1" / "session").glob("*.jsonl")))

    def test_failing_patch_still_fact_but_marked(self):
        self.spec(write={"calc.py": BROKEN}, structured=answer("사실"))
        r = self.run_one()
        self.assertEqual((r["verdict"], r["verify_state"]), (solo.VERDICT_FACT, solo.VERIFY_FAIL))

    def test_baseline_failing_lint_not_counted(self):
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": [f"{sys.executable} -m unittest -q test_calc"], "lint": ["false"],
        }})
        self.spec(write={"calc.py": FIXED}, structured=answer("사실"))
        r = self.run_one()
        self.assertEqual(r["verify_state"], solo.VERIFY_PASS)
        self.assertIn("false", " ".join(v["cmd"] for v in r["verify"]))

    def test_setup_paths_excluded_new_files_kept(self):
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {
            "setup": ["mkdir -p .venv && touch .venv/marker"],
            "tests": [f"{sys.executable} -m unittest -q test_calc"],
        }})
        self.spec(write={"calc.py": FIXED, "notes/new.txt": "x\n"}, structured=answer("사실"))
        self.run_one()
        patch = (self.out / "calc-1" / "patch.diff").read_text(encoding="utf-8")
        self.assertIn("notes/new.txt", patch)
        self.assertNotIn(".venv", patch)

    def test_timeout(self):
        self.spec(sleep=5, structured=answer("아님"))
        r = self.run_one(timeout=1)
        self.assertEqual((r["status"], r["verdict"]), (solo.STATUS_TIMEOUT, solo.VERDICT_NONE))
        self.assertFalse((self.out / "calc-1" / "wt").exists())

    def test_invalid_output(self):
        self.spec(stdout="not json")
        r = self.run_one()
        self.assertEqual(r["status"], solo.STATUS_ERROR)

    def test_skips_done_and_reruns_failed_only_when_asked(self):
        self.spec(stdout="not json")
        self.run_one()
        self.spec(structured=answer("아님"))
        self.assertEqual(self.run_one()["status"], solo.STATUS_ERROR)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.run_one(rerun_failed=True)["status"], solo.STATUS_OK)
        self.assertEqual(self.run_one(rerun_failed=True)["status"], solo.STATUS_OK)
        self.assertEqual(len(self.calls()), 2)

    def test_main_runs_items_in_parallel(self):
        sample = self.tmp / "sample.jsonl"
        sample.write_text("\n".join(json.dumps(self.item(f"calc-{i}"), ensure_ascii=False) for i in range(3)) + "\n",
                          encoding="utf-8")
        self.spec(structured=answer("아님"))
        code = solo.main(["run", "--sample", str(sample), "--out", str(self.out), "--repos-toml", str(self.toml),
                          "--claude", str(self.fake), "--projects-root", str(self.projects), "--parallel", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.out.iterdir() if (p / "result.json").is_file()),
                         ["calc-0", "calc-1", "calc-2"])


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)

    def result(self, item_id, status, verdict, cost=1.0, verify_state=solo.VERIFY_PASS, duration=10.0):
        d = self.runs / item_id
        d.mkdir()
        (d / "result.json").write_text(json.dumps({
            "id": item_id, "status": status, "verdict": verdict, "claude_cost": cost, "verify_state": verify_state,
            "duration_s": duration, "usage": [{"model": "claude-opus-5-5", "cost": cost}],
        }, ensure_ascii=False), encoding="utf-8")

    def sample(self):
        truths = {"a": "사실", "b": "아님", "c": "아님", "d": "사실", "e": "재현 불가", "f": "사실"}
        return {k: {"id": k, "truth": {"verdict": v}} for k, v in truths.items()}

    def test_metrics(self):
        self.result("a", solo.STATUS_OK, solo.VERDICT_FACT)
        self.result("b", solo.STATUS_OK, solo.VERDICT_FACT, verify_state=solo.VERIFY_FAIL)
        self.result("c", solo.STATUS_OK, solo.VERDICT_NONE)
        self.result("d", solo.STATUS_TIMEOUT, solo.VERDICT_NONE, cost=None)
        self.result("e", solo.STATUS_OK, solo.VERDICT_UNREPRODUCIBLE)
        m = solo.score(self.runs, self.sample())
        self.assertEqual((m["total"], m["correct"], m["false_fact"], m["to_human"]), (6, 2, 1, 1))
        self.assertEqual((m["errors"], m["missing"], m["scored"]), (1, 1, 4))
        self.assertAlmostEqual(m["accuracy"], 2 / 6)
        self.assertAlmostEqual(m["completion"], 4 / 6)
        self.assertEqual(m["verify_failed_fact"], 1)
        self.assertEqual(m["opus_calls"], 5)
        self.assertEqual(m["by_truth"]["아님"], {"total": 2, "correct": 0, "judged": 1, "to_human": 1, "false_fact": 1})
        self.assertIsNone(m["claude_cost"])
        self.assertEqual(m["predicted"]["b"], solo.VERDICT_FACT)
        self.assertNotIn("d", m["predicted"])

    def test_cost_sum_and_select_profiles_keys(self):
        for k in ("a", "b"):
            self.result(k, solo.STATUS_OK, solo.VERDICT_FACT, cost=2.5)
        m = solo.score(self.runs, {k: v for k, v in self.sample().items() if k in ("a", "b")})
        self.assertAlmostEqual(m["claude_cost"], 5.0)
        self.assertAlmostEqual(m["duration_s"], 20.0)
        self.assertTrue(set(select_profiles.METRICS) <= set(m))
        self.assertIsNone(m["fallback_ratio"])

    def test_score_cli_writes_json(self):
        self.result("a", solo.STATUS_OK, solo.VERDICT_FACT)
        sample = self.runs / "sample.jsonl"
        sample.write_text(json.dumps({"id": "a", "truth": {"verdict": "사실"}}) + "\n", encoding="utf-8")
        out = self.runs / "score.json"
        self.assertEqual(solo.main(["score", "--runs", str(self.runs), "--sample", str(sample), "--json", str(out)]), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["correct"], 1)


if __name__ == "__main__":
    unittest.main()
