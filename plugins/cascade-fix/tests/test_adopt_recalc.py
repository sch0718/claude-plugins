import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import adopt_recalc  # noqa: E402
import score  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "adopt-recalc" / "both-pass"
SAMPLE = {"x": {"id": "x", "finding": {"id": "r1-01"}, "truth": {"verdict": "사실"}, "baseline_opus_calls": 1}}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class AdoptRecalcTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.runs = self.tmp / "runs"
        shutil.copytree(FIXTURE, self.runs)
        self.out = self.tmp / "out"

    def round_dir(self, base=None):
        return (base or self.runs) / "x" / "run" / "round-1"

    def recalc(self, runs=None):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = adopt_recalc.main(["--runs", str(runs or self.runs), "--out", str(self.out)])
        return code, buf.getvalue()

    def test_recalc_flips_both_pass_item_and_removes_opus_usage(self):
        off = score.compute(self.runs, SAMPLE, cost_scope="round1-core")
        self.assertEqual(self.recalc()[0], 0)
        on = score.compute(self.out, SAMPLE, cost_scope="round1-core")
        self.assertEqual(off["opus_calls"] - on["opus_calls"], 1)
        self.assertLess(on["claude_cost"], off["claude_cost"])
        self.assertEqual(on["cost_missing"], [])
        check = read(self.out / "check.json")
        self.assertTrue(check["ok"])
        self.assertEqual(check["flipped"], 1)
        self.assertEqual(check["cost_scope"], "round1-core")
        ledger = read(self.out / "x" / "ledger.json")
        self.assertIn("둘 다 검증 통과", " ".join(ledger["rounds"][0]["decisions"][0]["reasons"]))
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["patch_from"], "a")
        self.assertNotIn("adjudicate", [e["step"] for e in ledger["rounds"][0]["events"]])
        self.assertEqual(read(self.out / "counterfactual.json"), {"cost_scope": "round1-core"})
        row = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((row["off"]["outcome"], row["on"]["outcome"]), ("판가름", "합의-사실"))

    def test_recalc_removes_every_adjudication_trace_and_leaves_the_source_alone(self):
        before = (self.round_dir() / "results.json").read_text(encoding="utf-8")
        self.recalc()
        rd = self.round_dir(self.out)
        self.assertFalse((rd / "adj-args.json").exists())
        self.assertFalse((rd / "adj.json").exists())
        self.assertNotIn("r1-01:adjudicator", read(rd / "results.json"))
        rows = read(rd / "claude-usage.json")["workflows"]["wf_1"]
        self.assertEqual([r["job_id"] for r in rows], ["r1-01:a", "r1-01:b"])
        self.assertEqual(read(rd / "decisions.json")[0]["outcome"], "합의-사실")
        self.assertEqual((self.round_dir() / "results.json").read_text(encoding="utf-8"), before)
        self.assertTrue((self.round_dir() / "adj-args.json").is_file())

    def test_recalc_handles_cli_adjudication_files(self):
        rd = self.round_dir()
        jobs = read(rd / "adj-args.json")["jobs"]
        write(rd / "adj-args.json", {"jobs": []})
        write(rd / "adj-cli-args.json", {"jobs": jobs})
        write(rd / "adj-cli.json", {"results": [{"id": j["id"], "output": {}} for j in jobs],
                                    "outcomes": [{"id": j["id"], "status": "ok"} for j in jobs],
                                    "fallback_runs": {}, "replaced": []})
        (rd / "adj.json").unlink()
        ledger_path = self.runs / "x" / "ledger.json"
        ledger = read(ledger_path)
        ledger["rounds"][0]["events"].append({"step": "workers-adj", "at": "2026-10-29T00:00:00+09:00"})
        write(ledger_path, ledger)
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertTrue(read(self.out / "check.json")["ok"])
        steps = [e["step"] for e in read(self.out / "x" / "ledger.json")["rounds"][0]["events"]]
        self.assertNotIn("workers-adj", steps)
        out_rd = self.round_dir(self.out)
        for name in ("adj-args.json", "adj-cli-args.json", "adj-cli.json"):
            with self.subTest(name=name):
                self.assertFalse((out_rd / name).exists())
        self.assertTrue((rd / "adj-cli-args.json").is_file())

    def test_recalc_recomputes_remaining_adjudication_event_when_only_some_are_dropped(self):
        rd = self.round_dir()
        ledger_path = self.runs / "x" / "ledger.json"
        ledger = read(ledger_path)
        rnd = ledger["rounds"][0]
        rnd["findings"].append({**rnd["findings"][0], "id": "r1-02"})
        rnd["decisions"].append({**rnd["decisions"][0], "finding_id": "r1-02"})
        results = read(rd / "results.json")
        results["r1-02:a"] = {**results["r1-01:a"], "finding_id": "r1-02"}
        results["r1-02:b"] = {**results["r1-01:b"], "finding_id": "r1-02", "verdict": "아님"}
        results["r1-02:adjudicator"] = {**results["r1-01:adjudicator"], "finding_id": "r1-02"}
        write(rd / "results.json", results)
        verify = read(rd / "verify.json")
        verify["r1-02"] = verify["r1-01"]
        write(rd / "verify.json", verify)
        write(rd / "lossy.json", {**read(rd / "lossy.json"), "r1-02": {"a": False, "b": False}})
        write(rd / "decisions.json", rnd["decisions"])
        jobs = [{**read(rd / "adj-args.json")["jobs"][0], "id": f"{fid}:adjudicator"} for fid in ("r1-01", "r1-02")]
        usage = {"r1-01:adjudicator": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 1000},
                 "r1-02:adjudicator": {"input_tokens": 200, "output_tokens": 20, "cached_input_tokens": 2000}}
        outcomes = [{"id": job["id"], "runner": "codex", "status": "limit" if job["id"].startswith("r1-01") else "ok",
                     "output": {}, "usage": usage[job["id"]], "detail": ""} for job in jobs]
        write(rd / "adj-args.json", {"jobs": []})
        write(rd / "adj-cli-args.json", {"jobs": jobs})
        write(rd / "adj-cli.json", {"results": [{"id": j["id"], "output": {}} for j in jobs], "outcomes": outcomes,
                                    "fallback_runs": {"r1-01:adjudicator": {"runner": "cursor", "model": "m", "effort": "high"}},
                                    "replaced": []})
        (rd / "adj.json").unlink()
        rnd["events"].append({
            "step": "workers-adj", "at": "2026-10-29T00:00:00+09:00", "counts": {"limit": 1, "ok": 1},
            "usage": {"codex": {"input": 300, "output": 30, "cache_read": 3000}},
            "fallback": ["r1-01:adjudicator"], "fallback_runs": {"r1-01:adjudicator": "cursor"}})
        write(ledger_path, ledger)
        write(self.runs / "x" / "run" / "ledger.json", ledger)
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertTrue(read(self.out / "check.json")["ok"])
        for path in (self.out / "x" / "ledger.json", self.out / "x" / "run" / "ledger.json"):
            with self.subTest(path=path.parent.name):
                event = next(e for e in read(path)["rounds"][0]["events"] if e["step"] == "workers-adj")
                self.assertEqual(event["counts"], {"ok": 1})
                self.assertEqual(event["usage"], {"codex": {"input": 200, "output": 20, "cache_read": 2000}})
                self.assertEqual(event["fallback"], [])
                self.assertEqual(event["fallback_runs"], {})

    def test_recalc_applies_lossy_like_judge(self):
        write(self.round_dir() / "lossy.json", {"r1-01": {"a": True, "b": False}})
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        row = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["on"]["outcome"], "판단불가")
        self.assertIn("UTF-8 아닌 패치", row["on"]["reasons"])
        self.assertTrue(read(self.out / "check.json")["ok"])

    def test_recalc_fails_without_lossy_record(self):
        (self.round_dir() / "lossy.json").unlink()
        code, text = self.recalc()
        self.assertEqual(code, 1)
        check = read(self.out / "check.json")
        self.assertFalse(check["ok"])
        self.assertIn("손실 여부 미확인", json.dumps(check["mismatches"], ensure_ascii=False))
        self.assertIn("x", text)

    def test_recalc_detects_branch_mismatch(self):
        (self.round_dir() / "adj-args.json").unlink()
        code, _ = self.recalc()
        self.assertEqual(code, 1)
        mismatch = read(self.out / "check.json")["mismatches"][0]
        self.assertEqual((mismatch["item"], mismatch["check"]), ("x", "branch"))

    def test_recalc_detects_token_mismatch(self):
        usage = read(self.round_dir() / "claude-usage.json")
        usage["workflows"]["wf_1"].append({**usage["workflows"]["wf_1"][2], "job_id": "r1-01:adjudicator-retry"})
        write(self.round_dir() / "claude-usage.json", usage)
        code, _ = self.recalc()
        self.assertEqual(code, 1)
        mismatch = read(self.out / "check.json")["mismatches"][0]
        self.assertEqual((mismatch["item"], mismatch["check"]), ("x", "tokens"))

    def test_recalc_copies_error_and_incomplete_items_unchanged_and_lists_them(self):
        (self.runs / "bad").mkdir()
        write(self.runs / "bad" / "meta.json", {"error": "boom"})
        (self.runs / "slow").mkdir()
        write(self.runs / "slow" / "meta.json", {"incomplete": True, "pending_run_dir": "/p"})
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertEqual(read(self.out / "bad" / "meta.json"), {"error": "boom"})
        self.assertTrue((self.out / "slow" / "meta.json").is_file())
        skipped = {row["item"]: row["reason"] for row in read(self.out / "check.json")["skipped"]}
        self.assertEqual(set(skipped), {"bad", "slow"})

    def test_recalc_keeps_items_the_rule_does_not_change(self):
        shutil.copytree(self.runs / "x", self.runs / "same")
        rd = self.round_dir(self.runs).parent.parent.parent / "same" / "run" / "round-1"
        results = read(rd / "results.json")
        results["r1-01:b"]["patch"] = results["r1-01:a"]["patch"]
        write(rd / "results.json", results)
        for name in ("adj-args.json", "adj.json"):
            (rd / name).unlink()
        ledger_path = self.runs / "same" / "ledger.json"
        ledger = read(ledger_path)
        ledger["rounds"][0]["decisions"][0].update(outcome="합의-사실", patch_from="a", reasons=[])
        write(ledger_path, ledger)
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertEqual(read(self.out / "check.json")["flipped"], 1)
        self.assertEqual(read(self.out / "same" / "ledger.json"), ledger)
        rows = [json.loads(line) for line in (self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({row["item"]: row["flipped"] for row in rows}, {"x": True, "same": False})

    def write_repos_toml(self, *lines):
        (self.runs / "x" / "repos.toml").write_text(
            '[repos."/nonexistent/wt"]\n' + "\n".join(lines) + "\n", encoding="utf-8")

    def on_row(self):
        return json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])

    def test_recalc_applies_security_path_like_judge(self):
        self.write_repos_toml('tests = ["python3 -m unittest {files}"]', 'sensitive_paths = ["calc.py"]')
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        row = self.on_row()
        self.assertEqual(row["on"]["outcome"], "판단불가")
        self.assertIn("보안 경로", row["on"]["reasons"])
        self.assertIsNone(row["on"]["patch_from"])
        self.assertTrue(read(self.out / "check.json")["ok"])

    def test_recalc_excludes_baseline_failed_commands_like_judge(self):
        verify = read(self.round_dir() / "verify.json")
        failed = {"cmd": "npm run lint", "ok": False, "tail": "lint 실패"}
        verify["r1-01"]["b"].append({**failed, "cmd": "[기준선 실패·판정 제외] " + failed["cmd"]})
        write(self.round_dir() / "verify.json", verify)
        self.assertEqual(self.recalc()[0], 0)
        self.assertEqual(self.on_row()["on"]["outcome"], "합의-사실")

        shutil.rmtree(self.out)
        verify["r1-01"]["b"][-1] = failed
        write(self.round_dir() / "verify.json", verify)
        self.assertEqual(self.recalc()[0], 0)
        row = self.on_row()
        self.assertEqual((row["on"]["outcome"], row["flipped"]), ("판가름", False))
        self.assertIn("한쪽만 검증 통과", row["on"]["reasons"])

    def test_recalc_requires_repo_config_with_tests(self):
        (self.runs / "x" / "repos.toml").unlink()
        self.assertEqual(self.recalc()[0], 1)
        self.assertEqual(read(self.out / "check.json")["mismatches"][0]["check"], "config")
        shutil.rmtree(self.out)
        self.write_repos_toml('format = []')
        self.assertEqual(self.recalc()[0], 1)
        self.assertIn("tests", read(self.out / "check.json")["mismatches"][0]["reason"])

    def test_failed_item_after_a_flipped_item_adds_no_tokens(self):
        self.assertEqual(self.recalc()[0], 0)
        alone = read(self.out / "check.json")["removed_tokens"]
        shutil.rmtree(self.out)
        shutil.copytree(self.runs / "x", self.runs / "y")
        (self.runs / "y" / "ledger.json").write_text("{깨짐", encoding="utf-8")
        self.assertEqual(self.recalc()[0], 1)
        self.assertEqual(read(self.out / "check.json")["removed_tokens"], alone)

    def test_recalc_counts_unreadable_items_as_errors_and_continues(self):
        shutil.copytree(self.runs / "x", self.runs / "ok")
        (self.runs / "x" / "ledger.json").write_text("{깨짐", encoding="utf-8")
        shutil.copytree(self.runs / "ok", self.runs / "nokey")
        results = read(self.runs / "nokey" / "run" / "round-1" / "results.json")
        del results["r1-01:b"]
        write(self.runs / "nokey" / "run" / "round-1" / "results.json", results)
        shutil.copytree(self.runs / "ok", self.runs / "noroot")
        ledger = read(self.runs / "noroot" / "ledger.json")
        del ledger["root"]
        write(self.runs / "noroot" / "ledger.json", ledger)
        code, text = self.recalc()
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", text)
        check = read(self.out / "check.json")
        self.assertEqual({m["item"] for m in check["mismatches"]}, {"x", "nokey", "noroot"})
        self.assertTrue(all(m["check"] == "read" for m in check["mismatches"]))
        self.assertEqual(check["flipped"], 1)
        self.assertEqual(read(self.out / "ok" / "ledger.json")["rounds"][0]["decisions"][0]["patch_from"], "a")

    def failing_cases(self):
        def no_tests():
            self.write_repos_toml("tests = []")

        def no_lossy():
            (self.round_dir() / "lossy.json").unlink()

        def no_branch():
            (self.round_dir() / "adj-args.json").unlink()

        def broken_ledger():
            (self.runs / "x" / "ledger.json").write_text("{깨짐", encoding="utf-8")

        def already_adopting():
            for name in ("ledger.json", "run/ledger.json"):
                ledger = read(self.runs / "x" / name)
                ledger["adopt_on_both_pass"] = True
                write(self.runs / "x" / name, ledger)

        return {"tests 없음": no_tests, "손실 미확인": no_lossy, "분기 불일치": no_branch, "원장 깨짐": broken_ledger,
                "이미 채택": already_adopting}

    def test_failed_item_is_written_as_an_error_item_so_score_does_not_count_it_as_on(self):
        for name, break_item in self.failing_cases().items():
            with self.subTest(name):
                shutil.rmtree(self.tmp / "runs", ignore_errors=True)
                shutil.rmtree(self.out, ignore_errors=True)
                shutil.copytree(FIXTURE, self.runs)
                shutil.copytree(self.runs / "x", self.runs / "ok")
                break_item()
                code, _ = self.recalc()
                self.assertEqual(code, 1)
                self.assertFalse(read(self.out / "check.json")["ok"])
                self.assertIn("error", read(self.out / "x" / "meta.json"))
                self.assertNotIn("error", read(self.out / "ok" / "meta.json"))
                both = {"x": SAMPLE["x"], "ok": {**SAMPLE["x"], "id": "ok"}}
                on = score.compute(self.out, both, cost_scope="round1-core")
                ok_only = score.compute(self.out, {"ok": both["ok"]}, cost_scope="round1-core")
                self.assertEqual((on["errors"], on["items"], on["correct"], on["total"]), (1, 1, 1, 2))
                self.assertEqual(on["opus_calls"], ok_only["opus_calls"])
                self.assertEqual(on["claude_cost"], ok_only["claude_cost"])

    def test_error_reason_names_the_failed_checks_in_korean(self):
        self.write_repos_toml("tests = []")
        self.recalc()
        reason = read(self.out / "x" / "meta.json")["error"]
        self.assertIn("채택 규칙 재계산 실패", reason)
        self.assertIn("tests", reason)

    def test_recalc_refuses_a_ledger_that_already_adopts_on_both_pass(self):
        for name in ("ledger.json", "run/ledger.json"):
            ledger = read(self.runs / "x" / name)
            ledger["adopt_on_both_pass"] = True
            write(self.runs / "x" / name, ledger)
        code, text = self.recalc()
        self.assertEqual(code, 1)
        mismatch = read(self.out / "check.json")["mismatches"][0]
        self.assertEqual(mismatch["check"], "adopt")
        self.assertIn("이미 참", mismatch["reason"])
        self.assertIn("이미 참", text)

    def test_recalc_can_write_inside_the_runs_folder(self):
        self.out = self.runs / "adopt-recalc"
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertFalse((self.out / "adopt-recalc").exists())
        self.assertTrue((self.out / "x" / "ledger.json").is_file())

    def test_recalc_refuses_a_non_empty_out_folder(self):
        self.out.mkdir()
        (self.out / "old.txt").write_text("x", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            self.recalc()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
