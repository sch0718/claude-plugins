import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay  # noqa: E402
import score  # noqa: E402
from cascade.consensus import REASON_BOTH_PASS, REASON_SENSITIVE  # noqa: E402
from cascade.report import _money as money  # noqa: E402
from cascade.usage import PRICE_SOURCE, PRICES_AS_OF  # noqa: E402
from fake_cli import fenced, result_json  # noqa: E402
from helpers import git, make_repo, run_cli, write_repos_toml  # noqa: E402
from test_cli_workers import LIMIT_ENTRY, WorkersTestBase  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CALC = "def add(a, b):\n    return a - b\n"
TEST_CALC = """import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 0), 1)
"""
FINDING = {"id": "r1-01", "source": "codex-code", "severity": "Warning", "location": "calc.py:2",
           "body": "add가 더하지 않고 뺀다", "evidence": "add(2, 3)이 -1을 돌려준다", "thread_id": None, "outdated": False}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def worker(verdict, role, checker_ok=None):
    return {"finding_id": "r1-01", "role": role, "verdict": verdict, "needs_design_decision": False,
            "reason": "근거", "verify": [], "checker_ok": checker_ok, "model_id": "m", "patch": ""}


def make_run(runs, item_id, decisions, results=None, mode="code", opinions=None, apply=None, error=None,
             events=None, files=None, profile=None, source=None):
    base = Path(runs) / item_id
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / "meta.json", {"error": error} if error else {"truth": {}, "baseline_opus_calls": 1})
    if error:
        return
    write_json(base / "ledger.json", {"mode": mode, "source": source or ("codex-doc" if mode == "doc" else "codex-code"),
                                      "opinions": opinions or {}, "profile": profile or {},
                                      "rounds": [{"no": 1, "decisions": decisions, "events": events or []}]})
    rnd = base / "run" / "round-1"
    write_json(rnd / "results.json", results or {})
    for name, data in (files or {}).items():
        write_json(rnd / name, data)
    if apply is not None:
        write_json(rnd / "apply.json", apply)


def decision(outcome, patch_from=None, reasons=None, fid="r1-01"):
    return {"finding_id": fid, "outcome": outcome, "patch_from": patch_from, "reasons": reasons or []}


APPLY_DONE = [{"step": "apply"}]


def make_cf_run(runs, name, rounds, mode="code", profile=None):
    base = Path(runs) / name
    write_json(base / "ledger.json", {"mode": mode, "source": "codex-code", "opinions": {}, "profile": profile or {}, "rounds": [
        {"no": r["no"], "decisions": r.get("decisions", []), "events": r.get("events", APPLY_DONE)} for r in rounds]})
    for r in rounds:
        rd = base / f"round-{r['no']}"
        write_json(rd / "results.json", r.get("results", {}))
        if "apply" in r:
            write_json(rd / "apply.json", r["apply"])
        if "pre_final" in r:
            write_json(rd / "decisions-pre-final.json", r["pre_final"])
        for fname, data in r.get("files", {}).items():
            write_json(rd / fname, data)


def sample_line(item_id, truth="사실", run=None, fid="r1-01"):
    line = {"id": item_id, "finding": {"id": fid}, "truth": {"verdict": truth}, "baseline_opus_calls": 1}
    if run:
        line["run"] = run
    return line


def sample_of(*lines):
    return {line["id"]: line for line in lines}


def claude_job(job_id, model):
    return {"id": job_id, "label": job_id, "model": model, "effort": "low", "kind": "worker", "promptPath": "p.md"}


def jobs_file(*jobs):
    return {"jobs": [claude_job(*j) for j in jobs]}


def workers_event(stage, counts, usage=None, fallback=None):
    return {"step": f"workers-{stage}", "counts": counts, "usage": usage or {}, "fallback": fallback or []}


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)
        self.sample = FIXTURES / "sample.jsonl"

    def three_runs(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")], apply={"final_ok": True})
        make_run(self.runs, "s2",
                 [decision("판가름", "adjudicator", ["패치가 다름", "판가름: 곱셈이 맞다"])],
                 {"r1-01:adjudicator": worker("사실", "adjudicator")})
        make_run(self.runs, "s3", [decision("판단불가", None, ["판가름 못 정함"])],
                 {"r1-01:adjudicator": worker("못 정함", "adjudicator")})

    def test_score_computes_agreement_and_opus_ratio(self):
        self.three_runs()
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["agree"], m["judged"]), (2, 2))
        self.assertEqual((m["opus_calls"], m["baseline_calls"]), (2, 3))
        self.assertAlmostEqual(m["opus_ratio"], 2 / 3)
        self.assertEqual(m["paths"], {"합의": 1, "규칙 채택": 0, "판가름": 1, "Codex 검토": 0, "사람": 1})
        self.assertEqual(m["patch_differs"], 1)
        self.assertEqual(m["items"], 3)
        self.assertEqual(m["regressions"], 0)
        text = score.render(m)
        self.assertIn("2/2", text)
        self.assertIn("66.7%", text)
        self.assertIn("| 패치 다르지만 둘 다 통과 | 1/3 (33.3%) |", text)

    def test_score_prints_pass_fail_line(self):
        self.three_runs()
        out = self.runs / "score.md"
        p = subprocess.run([sys.executable, str(ROOT / "eval" / "score.py"), "--runs", str(self.runs),
                            "--sample", str(self.sample), "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        line = "성공 기준: 판정 일치 100.0% (목표 95%) 통과 · Opus 호출 66.7% (목표 40% 이하) 미달 · 회귀 0건 (목표 0) 통과"
        self.assertIn(line, p.stdout)
        self.assertIn(line, out.read_text(encoding="utf-8"))

    def test_dry_run_only_runs_report_regression_as_unmeasured(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")], apply={"dry_run": True, "applied": []})
        make_run(self.runs, "s3", [decision("합의-아님")], apply={"dry_run": True, "applied": []})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertFalse(m["regression_measured"])
        segment = score.verdict_line(m).split(" · ")[-1]
        self.assertIn("회귀 미측정", segment)
        self.assertNotIn("통과", segment)
        self.assertNotIn("미달", segment)
        self.assertIn("| 회귀 건수 | 측정 불가(dry-run은 전체 테스트를 돌리지 않음) |", score.render(m))

    def test_incomplete_items_are_excluded_and_counted(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")])
        make_run(self.runs, "s2", [decision("합의-사실", "a")])
        write_json(self.runs / "s2" / "meta.json", {"incomplete": True})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["incomplete"], m["items"], m["baseline_calls"]), (1, 1, 1))
        self.assertIn("| 미완료 항목 수 | 1 |", score.render(m))

    def test_fail_line_when_regression_and_low_agreement(self):
        make_run(self.runs, "s1", [decision("합의-아님")], apply={"final_ok": False})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["agree"], m["judged"]), (0, 1))
        self.assertEqual(m["regressions"], 1)
        line = score.verdict_line(m)
        self.assertIn("판정 일치 0.0% (목표 95%) 미달", line)
        self.assertIn("회귀 1건 (목표 0) 미달", line)
        self.assertIn("Opus 호출 0.0% (목표 40% 이하) 통과", line)

    def test_error_items_are_counted_and_skipped(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")])
        make_run(self.runs, "s2", [], error="boom")
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["errors"], m["items"], m["baseline_calls"]), (1, 1, 1))

    def test_adjudicated_no_and_unreproducible_and_codex_paths(self):
        make_run(self.runs, "s3", [decision("판가름", None, ["판가름: 아님", "판가름: 쓰임이 없다"])])
        make_run(self.runs, "s2", [decision("판단불가", None, ["판가름 재현 불가"])], opinions={"r1-01": "의견"})
        make_run(self.runs, "s1", [decision("설계결정")])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["predicted"], {"s1": "판정 없음", "s2": "재현 불가", "s3": "아님"})
        self.assertEqual((m["agree"], m["judged"]), (1, 2))
        self.assertEqual(m["paths"], {"합의": 0, "규칙 채택": 0, "판가름": 1, "Codex 검토": 1, "사람": 1})

    def test_adopt_on_both_pass_counts_rule_path_and_patch_differs(self):
        make_run(self.runs, "s1", [decision("합의-사실", "b", [f"{REASON_BOTH_PASS}: 작은 패치 b 채택"])])
        make_run(self.runs, "s2", [decision("판단불가", None, [f"{REASON_BOTH_PASS}: 크기가 같아 a 채택", REASON_SENSITIVE])])
        make_run(self.runs, "s3", [decision("합의-아님")])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["paths"], {"합의": 1, "규칙 채택": 1, "판가름": 0, "Codex 검토": 0, "사람": 1})
        self.assertEqual(m["patch_differs"], 2)
        self.assertEqual(m["predicted"], {"s1": "사실", "s2": "판정 없음", "s3": "아님"})
        text = score.render(m)
        self.assertIn("| 채택 경로 분포 | 합의 1, 규칙 채택 1, 판가름 0, Codex 검토 0, 사람 1 |", text)
        self.assertIn("| 패치 다르지만 둘 다 통과 | 2/3 (66.7%) |", text)

    def test_doc_checker_accuracy(self):
        make_run(self.runs, "s1", [decision("판가름", "writer", ["점검자 아니오", "판가름: 고친다"])],
                 {"r1-01:checker": worker("사실", "checker", checker_ok=False),
                  "r1-01:adjudicator": worker("사실", "adjudicator")}, mode="doc")
        make_run(self.runs, "s2", [decision("판가름", None, ["점검자 아니오", "판가름: 아님"])],
                 {"r1-01:checker": worker("사실", "checker", checker_ok=False),
                  "r1-01:adjudicator": worker("아님", "adjudicator")}, mode="doc")
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["checker"], (1, 2))
        self.assertIn("| 문서 점검자 정확도 | 1/2 (50.0%) |", score.render(m))

    def test_checker_accuracy_dash_without_doc_items(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertIn("| 문서 점검자 정확도 | - |", score.render(m))

    def test_score_counts_claude_calls_and_fallbacks(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")],
                 events=[workers_event("stage1", {"ok": 1, "limit": 1}, {"codex": {"input": 11, "output": 7, "cache_read": 3}},
                                       ["r1-01:b"])],
                 files={"stage1-args.json": jobs_file(),
                        "stage1-cli-args.json": {"jobs": [{"id": "r1-01:a"}, {"id": "r1-01:b"}]},
                        "stage1-fallback-args.json": jobs_file(("r1-01:b", "claude-haiku-5-5")),
                        "adj-args.json": jobs_file(("r1-01:adjudicator", "claude-opus-5-5"))})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["claude_calls"], {"sonnet": 0, "haiku": 1, "opus": 1})
        self.assertEqual(m["fallback"], {"한도": 1})
        self.assertEqual((m["cli_jobs"], m["invalid"]), (2, 0))
        self.assertEqual(m["usage"], {"codex": {"input": 11, "output": 7, "cache_read": 3}})

    def test_score_counts_claude_worker_jobs_by_model_name(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")],
                 files={"stage1-args.json": jobs_file(("r1-01:a", "claude-sonnet-5-5"), ("r1-01:b", "claude-haiku-5-5")),
                        "checker-args.json": jobs_file(("r1-01:checker", "claude-haiku-5-5")),
                        "adj-args.json": jobs_file(("r1-01:adjudicator", "claude-opus-5-5"))})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["claude_calls"], {"sonnet": 1, "haiku": 2, "opus": 1})
        self.assertEqual((m["fallback"], m["cli_jobs"], m["invalid"], m["usage"]), ({}, 0, 0, {}))

    def test_score_uses_last_workers_event_per_stage_and_sums_invalid_and_usage(self):
        usage = {"cursor": {"input": 100, "output": 20, "cache_read": 5}}
        make_run(self.runs, "s1", [decision("합의-사실", "a")], events=[
            workers_event("stage1", {"ok": 1, "error": 1}, usage, ["r1-01:b"]),
            workers_event("stage1", {"ok": 1, "invalid": 1}, usage, ["r1-01:b"]),
            workers_event("checker", {"ok": 1, "auth": 1, "skipped": 1}, usage, ["r1-01:b", "r1-01:c"])])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["cli_jobs"], m["invalid"]), (5, 1))
        self.assertEqual(m["fallback"], {"무효": 1, "로그인": 1, "건너뜀": 1})
        self.assertEqual(m["usage"], {"cursor": {"input": 200, "output": 40, "cache_read": 10}})

    def test_render_without_workers_keeps_table_unchanged(self):
        self.three_runs()
        text = score.render(score.compute(self.runs, score.load_sample(self.sample)))
        self.assertNotIn("대체 실행", text)
        self.assertNotIn("Claude 호출", text)

    def test_opinion_failures_are_not_fallback_executions(self):
        usage = {"claude": {"input": 100, "output": 20, "cache_read": 5}}
        event = workers_event("opinion", {"auth": 1, "skipped": 1, "invalid": 1}, usage)
        event["failed"] = ["r1-01:opinion", "r1-02:opinion", "r1-03:opinion"]
        ledger = {"rounds": [{"no": 1, "events": [
            workers_event("adj", {"error": 1}, fallback=["r1-01:adjudicator"]), event]}]}
        self.assertEqual(score.workers_stats(ledger), (1, {"오류": 1}, 0, usage))
        self.assertEqual(score.fallback_job_ids(ledger), ["r1-01:adjudicator"])

    def test_opinion_invalid_results_are_excluded_from_invalid_ratio(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")], events=[
            workers_event("stage1", {"ok": 2}), workers_event("opinion", {"invalid": 3})])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["cli_jobs"], m["invalid"]), (2, 0))
        self.assertIn("| 무효 결과 | 0/2 (0.0%) |", score.render(m))
        self.assertIn("| 무효 결과 | 0/2 (0.0%) | 0/2 (0.0%) |", score.render_compare(m, m))

    def test_opinion_jobs_do_not_change_fallback_ratio_or_selection(self):
        for adj in ([], [workers_event("adj", {"ok": 2})]):
            with self.subTest(adj=adj):
                events = [workers_event("stage1", {"ok": 3, "error": 1}, fallback=["r1-01:b"]), *adj]
                make_run(self.runs, "s1", [decision("합의-사실", "a")], events=events)
                sample = sample_of(sample_line("s1"))
                before = score.compute(self.runs, sample)
                events.append(workers_event("opinion", {"ok": 4}))
                make_run(self.runs, "s1", [decision("합의-사실", "a")], events=events)
                after = score.compute(self.runs, sample)
                self.assertEqual(before["cli_jobs"], 4 + 2 * bool(adj))
                self.assertEqual(after["cli_jobs"], before["cli_jobs"])
                self.assertEqual(score.fallback_ratio(after), score.fallback_ratio(before))
                self.assertEqual(score.fallback_check(after), score.fallback_check(before))
                self.assertFalse(score.fallback_check(after)["ok"])
                self.assertIn("대체 실행", score.compare_causes(after, before))

    def test_render_with_cli_jobs_adds_workers_rows(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")],
                 events=[workers_event("stage1", {"ok": 3, "invalid": 1}, {"codex": {"input": 1, "output": 2, "cache_read": 3}})],
                 files={"adj-args.json": jobs_file(("r1-01:adjudicator", "claude-opus-5-5"))})
        text = score.render(score.compute(self.runs, score.load_sample(self.sample)))
        self.assertIn("| 대체 실행 | 1/4 (25.0%) — 무효 1 |", text)
        self.assertIn("| 무효 결과 | 1/4 (25.0%) |", text)
        self.assertIn("| Claude 호출 | Sonnet 0 · Haiku 0 · Opus 1 |", text)
        self.assertIn("| CLI 사용량(codex) | 입력 1 · 출력 2 · 캐시 읽기 3 |", text)


class ScoreAccuracyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)

    def mixed_runs(self):
        make_run(self.runs, "m1", [decision("합의-사실", "a")])
        make_run(self.runs, "m2", [decision("합의-사실", "a")])
        make_run(self.runs, "m3", [decision("합의-사실", "a")])
        make_run(self.runs, "m4", [decision("설계결정")])
        make_run(self.runs, "m5", [decision("판단불가", None, ["판가름 재현 불가"])])
        return sample_of(sample_line("m1"), sample_line("m2"), sample_line("m3", "아님"), sample_line("m4", "아님"),
                         sample_line("m5", "재현 불가"))

    def test_fixed_denominator_counts_errors_wrong(self):
        lines = [sample_line(f"e{i}") for i in range(36)] + [sample_line("ok")]
        for i in range(36):
            make_run(self.runs, f"e{i}", [], error="boom")
        make_run(self.runs, "ok", [decision("합의-사실", "a")])
        m = score.compute(self.runs, sample_of(*lines))
        self.assertAlmostEqual(m["accuracy"], 1 / 37)
        self.assertAlmostEqual(m["completion"], 1 / 37)
        self.assertEqual(m["agreement"], 1.0)
        self.assertEqual((m["total"], m["correct"]), (37, 1))

    def test_accuracy_none_without_sample(self):
        m = score.compute(self.runs, {})
        self.assertEqual((m["accuracy"], m["completion"]), (None, None))

    def test_run_unit_sample_counts_calls_once(self):
        results = {"r1-01:adjudicator": worker("사실", "adjudicator"), "r1-02:adjudicator": worker("사실", "adjudicator")}
        make_cf_run(self.runs, "doc1", [{"no": 1, "decisions": [decision("합의-사실", "a"), decision("합의-사실", "a", fid="r1-02")],
                                         "results": results, "apply": {"final_ok": False},
                                         "files": {"adj-args.json": jobs_file(("r1-01:adjudicator", "claude-opus-5-5"))}}])
        sample = sample_of(sample_line("a", run="doc1"), sample_line("b", run="doc1", fid="r1-02"))
        m = score.compute(self.runs, sample)
        self.assertEqual((m["opus_calls"], m["baseline_calls"], m["items"]), (2, 2, 2))
        self.assertEqual(m["claude_calls"]["opus"], 1)
        self.assertEqual(m["regressions"], 1)
        self.assertEqual(m["predicted"], {"a": "사실", "b": "사실"})

    def test_run_resolved_under_each_runs_dir(self):
        cfg_a, cfg_b = self.runs / "a", self.runs / "b"
        make_cf_run(cfg_a, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")]}])
        make_cf_run(cfg_b, "r", [{"no": 1, "decisions": [decision("합의-아님")]}])
        sample = sample_of(sample_line("x", run="r"))
        a, b = score.compute(cfg_a, sample), score.compute(cfg_b, sample)
        self.assertNotEqual(a["predicted"], b["predicted"])

    def test_run_without_ledger_is_missing(self):
        m = score.compute(self.runs, sample_of(sample_line("x", run="nope")))
        self.assertEqual((m["missing"], m["items"], m["accuracy"], m["completion"]), (1, 0, 0.0, 0.0))

    def test_round_limits_decision_and_completion(self):
        make_cf_run(self.runs, "r", [
            {"no": 1, "decisions": [decision("합의-사실", "a")], "apply": {"final_ok": True}},
            {"no": 2, "decisions": [], "events": [], "apply": {"final_ok": False}}])
        sample = sample_of(sample_line("x", run="r"))
        full = score.compute(self.runs, sample)
        self.assertEqual((full["incomplete"], full["items"]), (1, 0))
        m = score.compute(self.runs, sample, round_no=1)
        self.assertEqual((m["incomplete"], m["regressions"]), (0, 0))
        self.assertEqual(m["predicted"]["x"], "사실")

    def test_run_ended_before_round_n_is_not_incomplete(self):
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")]}])
        m = score.compute(self.runs, sample_of(sample_line("x", run="r")), round_no=3)
        self.assertEqual((m["incomplete"], m["items"]), (0, 1))

    def test_round_limits_role_counts(self):
        adjudicator = {"r1-01:adjudicator": worker("사실", "adjudicator")}
        make_cf_run(self.runs, "r", [
            {"no": 1, "decisions": [decision("합의-사실", "a")], "results": adjudicator},
            {"no": 2, "decisions": [decision("합의-사실", "a")], "results": adjudicator}])
        sample = sample_of(sample_line("x", run="r"))
        self.assertEqual(score.compute(self.runs, sample)["opus_calls"], 2)
        self.assertEqual(score.compute(self.runs, sample, round_no=1)["opus_calls"], 1)

    def test_round_limits_legacy_item(self):
        base = self.runs / "s1"
        make_run(self.runs, "s1", [decision("합의-사실", "a")], apply={"final_ok": True})
        write_json(base / "run" / "round-2" / "apply.json", {"final_ok": False})
        write_json(base / "ledger.json", {"mode": "code", "opinions": {}, "rounds": [
            {"no": 1, "decisions": [decision("합의-사실", "a")], "events": []},
            {"no": 2, "decisions": [decision("합의-아님")], "events": []}]})
        sample = sample_of(sample_line("s1"))
        self.assertEqual(score.compute(self.runs, sample)["predicted"]["s1"], "아님")
        m = score.compute(self.runs, sample, round_no=1)
        self.assertEqual((m["predicted"]["s1"], m["regressions"]), ("사실", 0))

    def test_apply_patched_only_is_incomplete(self):
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")], "events": [{"step": "apply-patched"}]}])
        m = score.compute(self.runs, sample_of(sample_line("x", run="r")), round_no=1)
        self.assertEqual((m["incomplete"], m["accuracy"]), (1, 0.0))

    def test_incomplete_run_counts_all_its_findings_wrong(self):
        two = [decision("합의-사실", "a"), decision("합의-사실", "a", fid="r1-02")]
        make_cf_run(self.runs, "A", [{"no": 1, "decisions": two}])
        make_cf_run(self.runs, "B", [{"no": 1, "decisions": two, "events": [{"step": "apply-patched"}]}])
        sample = sample_of(sample_line("a1", run="A"), sample_line("a2", run="A", fid="r1-02"),
                           sample_line("b1", run="B"), sample_line("b2", run="B", fid="r1-02"))
        m = score.compute(self.runs, sample)
        self.assertEqual((m["incomplete"], m["accuracy"]), (2, 2 / 4))
        self.assertEqual(m["completion"], 2 / 4)

    def test_pre_final_uses_decisions_before_final_suite(self):
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("판단불가", None, ["전체 검사 실패"])],
                                      "pre_final": [decision("합의-사실", "a")], "apply": {"final_ok": False}}])
        sample = sample_of(sample_line("x", run="r"))
        self.assertEqual(score.compute(self.runs, sample)["predicted"]["x"], "판정 없음")
        self.assertEqual(score.compute(self.runs, sample, pre_final=True)["predicted"]["x"], "사실")

    def test_broken_pre_final_file_counts_the_run_as_an_error_and_names_it(self):
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")], "pre_final": [decision("합의-사실", "a")]}])
        make_cf_run(self.runs, "ok", [{"no": 1, "decisions": [decision("합의-사실", "a")]}])
        (self.runs / "r" / "round-1" / "decisions-pre-final.json").write_text("{깨짐", encoding="utf-8")
        sample = sample_of(sample_line("x", run="r"), sample_line("y", run="ok"))
        m = score.compute(self.runs, sample, pre_final=True)
        self.assertEqual((m["errors"], m["items"], m["scored"], m["correct"]), (1, 1, 1, 1))
        self.assertEqual(m["accuracy"], 1 / 2)
        self.assertEqual(len(m["read_errors"]), 1)
        self.assertIn("r", m["read_errors"][0])
        self.assertIn("decisions-pre-final.json", m["read_errors"][0])
        self.assertEqual(score.compute(self.runs, sample)["errors"], 0)

    def test_pre_final_file_that_is_not_a_list_counts_the_run_as_an_error(self):
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")], "pre_final": {"a": 1}}])
        m = score.compute(self.runs, sample_of(sample_line("x", run="r")), pre_final=True)
        self.assertEqual((m["errors"], m["items"]), (1, 0))

    def test_broken_ledger_counts_the_run_as_an_error(self):
        make_run(self.runs, "x", [decision("합의-사실", "a")])
        (self.runs / "x" / "ledger.json").write_text("{깨짐", encoding="utf-8")
        m = score.compute(self.runs, sample_of(sample_line("x")))
        self.assertEqual((m["errors"], m["items"], m["correct"]), (1, 0, 0))

    def test_pre_final_leaves_rounds_without_file_unchanged(self):
        make_cf_run(self.runs, "r", [
            {"no": 1, "decisions": [decision("합의-아님")], "pre_final": [decision("합의-사실", "a")]},
            {"no": 2, "decisions": [decision("합의-사실", "a")]}])
        m = score.compute(self.runs, sample_of(sample_line("x", run="r")), pre_final=True)
        self.assertEqual(m["predicted"]["x"], "사실")
        m = score.compute(self.runs, sample_of(sample_line("x", run="r")), round_no=1, pre_final=True)
        self.assertEqual(m["predicted"]["x"], "사실")

    def test_baseline_failure_is_not_regression(self):
        self.assertFalse(score.is_regression({"final_ok": False, "baseline_failed": True}))
        self.assertTrue(score.is_regression({"final_ok": False, "baseline_failed": False}))
        self.assertTrue(score.is_regression({"final_ok": False}))
        self.assertFalse(score.is_regression({"final_ok": None}))
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")],
                                      "apply": {"final_ok": False, "baseline_failed": True}}])
        make_cf_run(self.runs, "q", [{"no": 1, "decisions": [decision("합의-사실", "a")],
                                      "apply": {"final_ok": False, "baseline_failed": False}}])
        self.assertEqual(score.compute(self.runs, sample_of(sample_line("x", run="r")))["regressions"], 0)
        self.assertEqual(score.compute(self.runs, sample_of(sample_line("x", run="q")))["regressions"], 1)

    def test_regression_uses_final_history(self):
        past = {"final_ok": False, "baseline_failed": False}
        redo = {"final_ok": None, "baseline_failed": False}
        self.assertTrue(score.is_regression({"final_ok": None, "final_history": [past, redo]}))
        self.assertFalse(score.is_regression({"final_ok": None, "final_history": [redo, {"final_ok": True}]}))
        self.assertFalse(score.is_regression({"final_ok": False, "baseline_failed": False,
                                              "final_history": [{"final_ok": False, "baseline_failed": True}]}))

    def test_force_rerun_with_history_is_measured_regression(self):
        apply = {"final_ok": None, "baseline_failed": False,
                 "final_history": [{"final_ok": False, "baseline_failed": False}, {"final_ok": None, "baseline_failed": False}]}
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")], "apply": apply}])
        m = score.compute(self.runs, sample_of(sample_line("x", run="r")))
        self.assertTrue(m["regression_measured"])
        self.assertEqual(m["regressions"], 1)
        text = score.render(m)
        self.assertIn("| 회귀 건수 | 1 |", text)
        self.assertNotIn("미측정", text)
        self.assertNotIn("측정 불가", text)

    def test_regressions_count_runs_not_rounds_and_legacy_items(self):
        bad = {"final_ok": False, "baseline_failed": False}
        make_cf_run(self.runs, "r", [{"no": 1, "decisions": [decision("합의-사실", "a")], "apply": bad},
                                     {"no": 2, "decisions": [decision("합의-사실", "a")], "apply": bad}])
        m = score.compute(self.runs, sample_of(sample_line("x", run="r"), sample_line("y", run="r")))
        self.assertEqual(m["regressions"], 1)

    def test_by_truth_table(self):
        m = score.compute(self.runs, self.mixed_runs())
        self.assertEqual(m["by_truth"]["아님"], {"total": 2, "correct": 0, "judged": 1, "to_human": 1, "false_fact": 1})
        self.assertEqual(m["by_truth"]["사실"]["total"], 2)
        self.assertEqual(m["by_truth"]["재현 불가"], {"total": 1, "correct": 1, "judged": 1, "to_human": 1, "false_fact": None})
        self.assertIn("| 아님 | 0 | 1 | 2 | 1 | 1 |", score.render(m))

    def test_false_fact_and_to_human(self):
        m = score.compute(self.runs, self.mixed_runs())
        self.assertEqual((m["false_fact"], m["to_human"]), (1, 2))

    def test_render_compare_has_by_truth_table(self):
        m = score.compute(self.runs, self.mixed_runs())
        text = score.render_compare(m, m)
        self.assertIn("| 아님 |", text)
        self.assertIn("정답별", text)

    def test_cli_passes_round_and_pre_final(self):
        make_cf_run(self.runs, "r", [
            {"no": 1, "decisions": [decision("판단불가", None, ["x"])], "pre_final": [decision("합의-사실", "a")]},
            {"no": 2, "decisions": [], "events": []}])
        sample = self.runs / "sample.jsonl"
        sample.write_text(json.dumps(sample_line("x", run="r"), ensure_ascii=False) + "\n", encoding="utf-8")
        p = subprocess.run([sys.executable, str(ROOT / "eval" / "score.py"), "--runs", str(self.runs), "--sample", str(sample),
                            "--round", "1", "--pre-final"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("| 미완료 항목 수 | 0 |", p.stdout)
        self.assertIn("| 정확도 | 1/1 (100.0%)", p.stdout)


SONNET, HAIKU, OPUS, FABLE = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-fable-5-2"
COUNTED_ITEM = "x"


def usage_row(job_id, cell, cost=0.5, model=SONNET, input=4, output=80, cache_read=2000, cache_write=0):
    return {"job_id": job_id, "label": f"{job_id} label", "cell": cell, "model": model, "input": input, "output": output,
            "cache_read": cache_read, "cache_write": cache_write, "cost": cost}


def usage_data(*rows, prices_as_of="2026-10-06"):
    return {"prices_as_of": prices_as_of, "price_source": "자료", "workflows": {"wf_1": list(rows)}, "unmatched": {"wf_1": []}}


def cf_round(no, usage=None, jobs=("r1-01:a",), extra=None, events=None):
    files = {"stage1-args.json": jobs_file(*[(j, SONNET) for j in jobs]), **(extra or {})}
    if usage is not None:
        files["claude-usage.json"] = usage
    round_ = {"no": no, "decisions": [decision("합의-사실", "a")], "files": files}
    return round_ if events is None else {**round_, "events": events}


class ScoreCostTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)
        self.sample = sample_of(sample_line(COUNTED_ITEM))

    def add(self, usage=None, jobs=("r1-01:a",), item_id=COUNTED_ITEM, **kwargs):
        files = {"stage1-args.json": jobs_file(*[(j, SONNET) for j in jobs])}
        if usage is not None:
            files["claude-usage.json"] = usage
        make_run(self.runs, item_id, [decision("합의-사실", "a")], files=files, **kwargs)

    def compute(self, **kwargs):
        return score.compute(self.runs, self.sample, **kwargs)

    def two_round_run(self, profile=None):
        make_cf_run(self.runs, "r", [
            cf_round(1, usage_data(usage_row("r1-01:a", "code.a", 0.5), usage_row("r1-01:opinion", "code.opinion", 0.25)),
                     extra={"opinion-args.json": jobs_file(("r1-01:opinion", OPUS))}),
            cf_round(2, usage_data(usage_row("r1-01:a", "code.a", 1.0)))], profile=profile)
        self.sample = sample_of(sample_line(COUNTED_ITEM, run="r"))

    def test_price_basis_in_outputs(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")))
        m = self.compute()
        self.assertIn("prices_as_of", m)
        self.assertEqual(m["prices_as_of"], "2026-10-06")
        self.assertEqual(m["price_source"], PRICE_SOURCE)
        text = score.render(m)
        self.assertIn(f"단가 기준: 2026-10-06 {PRICE_SOURCE}", text)

    def test_price_basis_falls_back_to_the_table_without_usage_files(self):
        self.add()
        self.assertEqual(self.compute()["prices_as_of"], PRICES_AS_OF)

    def test_mixed_price_dates_are_listed(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a"), prices_as_of="2026-10-06"), item_id="s1")
        self.add(usage_data(usage_row("r1-01:a", "code.a"), prices_as_of="2026-11-01"), item_id="s2")
        m = score.compute(self.runs, sample_of(sample_line("s1"), sample_line("s2")))
        self.assertEqual(m["prices_as_of"], ["2026-10-06", "2026-11-01"])
        self.assertIn("2026-10-06", score.render(m))
        self.assertIn("2026-11-01", score.render(m))

    def test_stored_cost_is_summed_per_cell_and_total(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a", 0.5), usage_row("r1-01:b", "code.b", 0.25, HAIKU)),
                 jobs=("r1-01:a", "r1-01:b"))
        m = self.compute()
        self.assertAlmostEqual(m["claude_cost"], 0.75)
        self.assertEqual(m["cost_missing"], [])
        self.assertEqual(m["cells"]["code.a"], {"calls": 1, "fallback": 0, "cost": 0.5,
                                                "tokens": {"input": 4, "output": 80, "cache_read": 2000, "cache_write": 0}})
        self.assertEqual(m["cells"]["code.b"]["cost"], 0.25)

    def test_cost_scope_round1_core_excludes_opinion_and_round2(self):
        self.two_round_run()
        everything, core = self.compute(), self.compute(cost_scope="round1-core")
        self.assertAlmostEqual(everything["claude_cost"], 1.75)
        self.assertAlmostEqual(core["claude_cost"], 0.5)
        self.assertLess(core["claude_cost"], everything["claude_cost"])
        self.assertNotIn("code.opinion", core["cells"])
        self.assertIn("code.opinion", everything["cells"])
        self.assertEqual(core["cost_scope"], "round1-core")

    def test_round_limit_also_limits_cost_rounds(self):
        self.two_round_run()
        self.assertAlmostEqual(self.compute(round_no=1)["claude_cost"], 0.75)

    def test_unknown_cost_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            self.compute(cost_scope="round1")

    def test_all_scope_adds_first_review_cost_and_core_does_not(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a", 0.5)), profile={"first_review": {
            "actual": {"runner": "claude"}, "usage": usage_data(usage_row("review", "code.review", 2.0, FABLE))}})
        everything, core = self.compute(), self.compute(cost_scope="round1-core")
        self.assertAlmostEqual(everything["claude_cost"], 2.5)
        self.assertEqual(everything["cells"]["code.review"]["calls"], 1)
        self.assertAlmostEqual(everything["cells"]["code.review"]["cost"], 2.0)
        self.assertAlmostEqual(core["claude_cost"], 0.5)
        self.assertNotIn("code.review", core["cells"])

    def test_first_review_without_usage_is_missing_in_all_scope_only(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")), profile={"first_review": {"actual": {"runner": "claude"}}})
        everything = self.compute()
        self.assertIsNone(everything["claude_cost"])
        self.assertEqual(everything["cost_missing"], ["x: 첫 검토"])
        self.assertIsNotNone(self.compute(cost_scope="round1-core")["claude_cost"])

    def test_replay_without_first_review_is_not_missing(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")), profile={"first_review": {}})
        m = self.compute()
        self.assertAlmostEqual(m["claude_cost"], 0.5)
        self.assertEqual(m["cost_missing"], [])

    def test_claude_source_without_first_review_record_is_not_missing(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")), profile={"first_review": {}}, source="claude-code")
        m = self.compute()
        self.assertAlmostEqual(m["claude_cost"], 0.5)
        self.assertEqual(m["cost_missing"], [])
        self.assertNotIn("code.review", m["cells"])

    def test_first_review_record_without_runner_is_claude_for_a_claude_source(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")), profile={"first_review": {"role": "code.review"}},
                 source="claude-code")
        m = self.compute()
        self.assertIsNone(m["claude_cost"])
        self.assertEqual(m["cost_missing"], ["x: 첫 검토"])

    def test_external_first_review_is_not_missing(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")), profile={"first_review": {"actual": {"runner": "cursor"}}})
        self.assertAlmostEqual(self.compute()["claude_cost"], 0.5)

    def test_counterfactual_folder_refuses_all_scope_cost(self):
        self.two_round_run()
        write_json(self.runs / "counterfactual.json", {"cost_scope": "round1-core"})
        everything = self.compute()
        self.assertIsNone(everything["claude_cost"])
        self.assertEqual(everything["cost_missing"], ["반사실은 round1-core만"])
        self.assertAlmostEqual(self.compute(cost_scope="round1-core")["claude_cost"], 0.5)

    def test_counterfactual_folder_scoped_to_all_allows_all(self):
        self.two_round_run()
        write_json(self.runs / "counterfactual.json", {"cost_scope": "all"})
        self.assertAlmostEqual(self.compute()["claude_cost"], 1.75)

    def test_missing_usage_makes_cost_unmeasured(self):
        self.add()
        m = self.compute()
        self.assertIsNone(m["claude_cost"])
        self.assertEqual(m["cost_missing"], ["x"])

    def test_partly_recorded_usage_makes_cost_unmeasured(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")), jobs=("r1-01:a", "r1-01:b"))
        m = self.compute()
        self.assertIsNone(m["claude_cost"])
        self.assertEqual(m["cost_missing"], ["x"])

    def test_partly_recorded_cell_is_unmeasured_not_partial(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a")), jobs=("r1-01:a", "r1-02:a"))
        m = self.compute()
        self.assertEqual(m["cells"]["code.a"], {"calls": 2, "fallback": 0, "tokens": None, "cost": None})
        self.assertEqual(m["cells_unmeasured"], ["code.a"])
        self.assertIn("| code.a | 2 | 0 | 미측정 | 미측정 |", score.render(m))

    def test_gap_cell_seen_only_in_fallback_args_does_not_crash(self):
        make_run(self.runs, COUNTED_ITEM, [decision("합의-사실", "a")],
                 files={"stage1-fallback-args.json": jobs_file(("r1-01:a", SONNET))})
        m = self.compute()
        self.assertIsNone(m["claude_cost"])
        self.assertEqual(m["cells_unmeasured"], ["code.a"])
        self.assertIsNone(m["cells"]["code.a"]["tokens"])
        self.assertIsNone(m["cells"]["code.a"]["cost"])
        self.assertIn("code.a", score.render(m))

    def test_fully_recorded_cell_beside_a_gap_keeps_its_values(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a", 0.5)), jobs=("r1-01:a", "r1-01:b"))
        cells = self.compute()["cells"]
        self.assertEqual(cells["code.a"]["cost"], 0.5)
        self.assertIsNone(cells["code.b"]["cost"])

    def test_claude_cell_without_usage_says_unmeasured_and_acp_cell_says_no_record(self):
        self.add(jobs=("r1-01:b",), results={"r1-01:a": worker("사실", "a")})
        text = score.render(self.compute())
        self.assertIn("| code.b | 1 | 0 | 미측정 | 미측정 |", text)
        self.assertIn("| code.a | 1 | 0 | 기록 없음 | 기록 없음 |", text)

    def test_counterfactual_refusal_marks_claude_cells_unmeasured(self):
        self.two_round_run()
        write_json(self.runs / "counterfactual.json", {"cost_scope": "round1-core"})
        m = self.compute()
        self.assertIn("code.a", m["cells_unmeasured"])
        self.assertIsNone(m["cells"]["code.a"]["tokens"])

    def test_fallback_uses_only_the_last_event_per_stage(self):
        self.add(events=[workers_event("stage1", {"ok": 1, "limit": 1}, fallback=["r1-01:b"]),
                         workers_event("stage1", {"ok": 2}, fallback=[])])
        self.assertEqual(self.compute()["cells"].get("code.b", {}).get("fallback", 0), 0)

    def test_null_cost_makes_cost_unmeasured(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a", None)))
        m = self.compute()
        self.assertIsNone(m["claude_cost"])
        self.assertEqual(m["cost_missing"], ["x"])
        self.assertIsNone(m["cells"]["code.a"]["cost"])

    def test_unreadable_usage_file_is_missing(self):
        self.add()
        (self.runs / "x" / "run" / "round-1" / "claude-usage.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.compute()["cost_missing"], ["x"])

    def test_run_without_claude_jobs_costs_zero(self):
        make_run(self.runs, "x", [decision("합의-사실", "a")])
        m = self.compute()
        self.assertEqual(m["claude_cost"], 0.0)
        self.assertEqual(m["cost_missing"], [])

    def test_no_scored_run_is_unmeasured(self):
        m = score.compute(self.runs, {})
        self.assertIsNone(m["claude_cost"])
        self.assertTrue(m["cost_missing"])

    def test_incomplete_run_is_left_out_of_cost(self):
        make_cf_run(self.runs, "done", [cf_round(1, usage_data(usage_row("r1-01:a", "code.a", 0.5)))])
        make_cf_run(self.runs, "stopped", [cf_round(1, usage_data(usage_row("r1-01:a", "code.a", 9.0)), events=[])])
        m = score.compute(self.runs, sample_of(sample_line("x", run="done"), sample_line("y", run="stopped")))
        self.assertEqual(m["incomplete"], 1)
        self.assertAlmostEqual(m["claude_cost"], 0.5)
        self.assertEqual(m["cost_missing"], [])

    def test_run_unit_sample_counts_a_run_cost_once(self):
        make_cf_run(self.runs, "r", [cf_round(1, usage_data(usage_row("r1-01:a", "code.a", 0.5)))])
        m = score.compute(self.runs, sample_of(sample_line("x", run="r"), sample_line("y", run="r")))
        self.assertAlmostEqual(m["claude_cost"], 0.5)

    def test_acp_cell_has_no_tokens_and_is_not_zero(self):
        self.add(usage_data(usage_row("r1-01:b", "code.b", 0.25, HAIKU)), jobs=("r1-01:b",),
                 results={"r1-01:a": worker("사실", "a"), "r1-01:b": worker("사실", "b")})
        m = self.compute()
        self.assertEqual(m["cells"]["code.a"], {"calls": 1, "fallback": 0, "tokens": None, "cost": None})
        self.assertAlmostEqual(m["claude_cost"], 0.25)
        self.assertIn("| code.a | 1 | 0 | 기록 없음 | 기록 없음 |", score.render(m))

    def test_cell_fallbacks_come_from_workers_events(self):
        self.add(usage_data(usage_row("r1-01:b", "code.b", 0.25, HAIKU)), jobs=("r1-01:b",),
                 events=[workers_event("stage1", {"ok": 1, "limit": 1}, fallback=["r1-01:b"])],
                 results={"r1-01:a": worker("사실", "a")})
        m = self.compute()
        self.assertEqual((m["cells"]["code.b"]["fallback"], m["cells"]["code.a"]["fallback"]), (1, 0))

    def test_fallback_ratio(self):
        self.add(events=[workers_event("stage1", {"ok": 9, "limit": 1})])
        self.assertAlmostEqual(self.compute()["fallback_ratio"], 0.1)
        self.assertIsNone(score.compute(self.runs, {})["fallback_ratio"])

    def test_render_shows_cost_row_and_cells_table(self):
        self.two_round_run()
        text = score.render(self.compute())
        self.assertIn("| Claude 비용(추정) | $1.7500 — 범위 all |", text)
        self.assertIn("| code.a | 2 | 0 | 8/160/4000 | $1.5000 |", text)
        text = score.render(self.compute(cost_scope="round1-core"))
        self.assertIn("| Claude 비용(추정) | $0.5000 — 범위 round1-core |", text)

    def test_render_names_unmeasured_cost_items(self):
        self.add()
        self.assertIn("| Claude 비용(추정) | 미측정(x) — 범위 all |", score.render(self.compute()))

    def test_tiny_cost_uses_report_format(self):
        self.add(usage_data(usage_row("r1-01:a", "code.a", 0.00001)))
        self.assertIn("<$0.0001", score.render(self.compute()))

    def test_usage_cost_flows_to_report_and_score(self):
        from cascade import cli, report
        from test_report import profile_ledger, profile_record
        run_dir = self.runs / "flow"
        ledger = profile_ledger(profile_record("claude-only"), events=[{"step": "apply"}])
        ledger.save(run_dir / "ledger.json")
        rd = run_dir / "round-1"
        jobs = [claude_job("F1:a", HAIKU), claude_job("F1:b", HAIKU)]
        write_json(rd / "stage1-args.json", {"jobs": [{**j, "label": f"{j['id']} 이름표"} for j in jobs]})
        workflow = write_workflow(self.runs / "wf_1", {"F1:a 이름표": [60_000, 60_000], "F1:b 이름표": [120_000]})
        labels = {f"{j['id']} 이름표": j["id"] for j in jobs}
        result = cli._usage_record(str(workflow), labels, lambda job_id: f"code.{job_id[-1]}", rd / "claude-usage.json")
        self.assertEqual((result["jobs"], result["error"]), (2, None))
        stored = {r["job_id"]: r["cost"] for r in json.loads((rd / "claude-usage.json").read_text(encoding="utf-8"))
                  ["workflows"]["wf_1"]}
        self.assertNotEqual(stored["F1:a"], stored["F1:b"])
        shown = report.render(ledger, dry_run=True, run_dir=run_dir)
        self.assertIn(f"추정 비용 {money(sum(stored.values()))}", shown)
        self.assertIn(money(stored["F1:a"]), shown)
        self.assertIn(money(stored["F1:b"]), shown)
        m = score.compute(self.runs, sample_of(sample_line("x", run="flow")))
        self.assertAlmostEqual(m["claude_cost"], sum(stored.values()))
        self.assertAlmostEqual(m["cells"]["code.a"]["cost"], stored["F1:a"])
        self.assertIn(money(m["claude_cost"]), score.render(m))


def write_workflow(path, agents):
    """agents: 이름표 → 요청마다의 입력 토큰 수. 작업마다 요청(메시지)이 여럿일 수 있는 합성 Workflow 기록 폴더."""
    path.mkdir(parents=True)
    journal = [{"type": "launched"}]
    for n, (label, inputs) in enumerate(agents.items(), 1):
        journal.append({"type": "started", "key": f"k{n}", "agentId": f"a{n}", "label": label, "phase": "Jobs"})
        (path / f"agent-a{n}.meta.json").write_text(json.dumps({"model": HAIKU}), encoding="utf-8")
        rows = [{"message": {"id": f"msg_{n}_{i}", "usage": {"input_tokens": tokens, "output_tokens": 500,
                                                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
                for i, tokens in enumerate(inputs)]
        (path / f"agent-a{n}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (path / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in journal) + "\n", encoding="utf-8")
    return path


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)

    def metrics(self, accuracy=0.9, false_fact=0, total=20, unfinished=0, fallback=0, cli_jobs=10, regressions=None,
                cost=0.5, missing=()):
        m = score.compute(self.runs, {})
        m.update(accuracy=accuracy, false_fact=false_fact, total=total, errors=unfinished, fallback={"한도": fallback} if fallback else {},
                 cli_jobs=cli_jobs, claude_cost=cost, cost_missing=list(missing), agreement=accuracy)
        if regressions is not None:
            m.update(regression_measured=True, regressions=regressions)
        return m

    def checks(self, cand, base=None):
        return score.selection(cand, base or self.metrics())["checks"]

    def test_selection_passes_new_but_fails_old_opus(self):
        cand, base = self.runs / "cand", self.runs / "base"
        for name in ("s1", "s2"):
            make_run(cand, name, [decision("합의-사실", "a")], results={"r1-01:adjudicator": worker("사실", "adjudicator")},
                     events=[workers_event("stage1", {"ok": 10})])
        make_run(base, "s1", [decision("합의-사실", "a")], results={"r1-01:adjudicator": worker("사실", "adjudicator")})
        make_run(base, "s2", [decision("합의-사실", "a")])
        sample = sample_of(sample_line("s1"), sample_line("s2"))
        text = score.render_compare(score.compute(cand, sample), score.compute(base, sample))
        self.assertIn("비교 판정: 미달(Opus 호출)", text)
        self.assertIn("선정 조건: 통과", text)
        self.assertTrue(text.splitlines()[-1].startswith("비교 판정:"))

    def test_accuracy_floor_is_five_points_below_baseline_inclusive(self):
        self.assertTrue(self.checks(self.metrics(0.85))["accuracy"]["ok"])
        self.assertFalse(self.checks(self.metrics(0.84))["accuracy"]["ok"])
        self.assertFalse(self.checks(self.metrics(None))["accuracy"]["ok"])

    def test_false_fact_must_not_exceed_baseline(self):
        base = self.metrics(false_fact=1)
        self.assertTrue(self.checks(self.metrics(false_fact=1), base)["false_fact"]["ok"])
        self.assertFalse(self.checks(self.metrics(false_fact=2), base)["false_fact"]["ok"])

    def test_completion_allows_five_percent_unfinished_absolute(self):
        self.assertTrue(self.checks(self.metrics(unfinished=1))["completion"]["ok"])
        self.assertFalse(self.checks(self.metrics(unfinished=2))["completion"]["ok"])
        self.assertTrue(self.checks(self.metrics(unfinished=1), self.metrics(unfinished=5))["completion"]["ok"])

    def test_completion_counts_incomplete_and_missing(self):
        m = self.metrics()
        m.update(incomplete=1, missing=1)
        self.assertFalse(score.selection(m, self.metrics())["checks"]["completion"]["ok"])

    def test_fallback_allows_ten_percent_and_no_external_jobs(self):
        self.assertTrue(self.checks(self.metrics(fallback=1))["fallback"]["ok"])
        self.assertFalse(self.checks(self.metrics(fallback=2))["fallback"]["ok"])
        self.assertTrue(self.checks(self.metrics(cli_jobs=0))["fallback"]["ok"])

    def test_regression_zero_when_measured_and_unmeasured_is_no_failure(self):
        self.assertTrue(self.checks(self.metrics(regressions=0))["regression"]["ok"])
        self.assertFalse(self.checks(self.metrics(regressions=1))["regression"]["ok"])
        unmeasured = score.selection(self.metrics(), self.metrics())
        self.assertIsNone(unmeasured["checks"]["regression"]["ok"])
        self.assertTrue(unmeasured["passed"])
        self.assertIn("회귀 미측정", score.selection_line(unmeasured))

    def test_line_names_failed_conditions_values_and_cost(self):
        sel = score.selection(self.metrics(0.80, false_fact=2, fallback=3, regressions=1), self.metrics(0.90, false_fact=1))
        line = score.selection_line(sel)
        self.assertTrue(line.startswith("선정 조건: 미달(정확도, 「아님」 오채택, 대체 실행, 회귀)"), line)
        self.assertTrue(line.endswith("Claude 비용 $0.5000"), line)
        self.assertFalse(sel["passed"])

    def test_line_says_when_there_are_no_external_jobs(self):
        line = score.selection_line(score.selection(self.metrics(cli_jobs=0), self.metrics()))
        self.assertIn("대체 실행 통과(외부 작업 없음)", line)

    def test_line_ends_with_unmeasured_cost(self):
        line = score.selection_line(score.selection(self.metrics(cost=None, missing=["x"]), self.metrics()))
        self.assertTrue(line.startswith("선정 조건: 통과"), line)
        self.assertTrue(line.endswith("Claude 비용 미측정"), line)

    def test_selection_carries_cost_for_select_profiles(self):
        sel = score.selection(self.metrics(cost=None, missing=["x"]), self.metrics())
        self.assertEqual((sel["claude_cost"], sel["cost_missing"]), (None, ["x"]))


class ScoreJsonTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)
        self.sample = self.runs / "sample.jsonl"
        self.sample.write_text(json.dumps(sample_line("x")) + "\n", encoding="utf-8")
        make_run(self.runs, "x", [decision("합의-사실", "a")], files={
            "stage1-args.json": jobs_file(("r1-01:a", SONNET)), "claude-usage.json": usage_data(usage_row("r1-01:a", "code.a"))})
        self.out = self.runs / "out.json"

    def run_main(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = score.main(["--runs", str(self.runs), "--sample", str(self.sample), "--json", str(self.out), *extra])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_json_output_round_trips(self):
        self.run_main()
        data = json.loads(self.out.read_text(encoding="utf-8"))
        for key in ("accuracy", "false_fact", "completion", "fallback_ratio", "regressions", "regression_measured",
                    "claude_cost", "cost_missing", "to_human", "predicted", "selection", "cells", "prices_as_of", "price_source"):
            self.assertIn(key, data)
        self.assertAlmostEqual(data["claude_cost"], 0.5)
        self.assertIsNone(data["selection"])

    def test_json_has_selection_with_baseline(self):
        out = self.run_main("--baseline-runs", str(self.runs))
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertTrue(data["selection"]["passed"])
        self.assertIn("선정 조건: 통과", out)

    def test_cost_scope_option(self):
        self.run_main("--cost-scope", "round1-core")
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["cost_scope"], "round1-core")

    def test_json_write_failure_is_a_korean_message_and_exit_one(self):
        self.out = self.runs / "missing-dir" / "out.json"
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = score.main(["--runs", str(self.runs), "--sample", str(self.sample), "--json", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("쓰지 못했다", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_main_prints_unreadable_runs_to_stderr_and_still_exits_zero(self):
        (self.runs / "x" / "ledger.json").write_text("{깨짐", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = score.main(["--runs", str(self.runs), "--sample", str(self.sample), "--json", str(self.out)])
        self.assertEqual(code, 0)
        self.assertIn("x", err.getvalue())
        self.assertIn("읽지 못했다", err.getvalue())
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["errors"], 1)

    def test_unknown_cost_scope_is_rejected_by_the_parser(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            score.main(["--runs", str(self.runs), "--sample", str(self.sample), "--cost-scope", "bogus"])


class CompareTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)

    def metrics(self, agreement, opus, fallback=None, cli_jobs=10, regressions=None):
        m = score.compute(self.runs, {})
        m.update(agreement=agreement, opus_calls=opus, fallback=fallback or {}, cli_jobs=cli_jobs)
        if regressions is not None:
            m.update(regression_measured=True, regressions=regressions)
        return m

    def verdict(self, ext, base):
        return score.compare_verdict_line(ext, base)

    def test_compare_verdict_line_passes_within_criteria(self):
        line = self.verdict(self.metrics(0.86, 3, {"한도": 1}), self.metrics(0.90, 3))
        self.assertIn("통과", line)
        self.assertNotIn("미달", line)
        self.assertIn("일치율 차이 4.0%p", line)
        self.assertIn("대체 실행 1/10", line)
        self.assertIn("회귀 미측정", line)

    def test_compare_verdict_line_fails_on_agreement(self):
        line = self.verdict(self.metrics(0.84, 3, {"한도": 1}), self.metrics(0.90, 3))
        self.assertIn("미달(일치율)", line)

    def test_compare_agreement_boundary_is_inclusive(self):
        self.assertIn("통과", self.verdict(self.metrics(0.85, 3), self.metrics(0.90, 3)))

    def test_compare_fails_on_opus_fallback_and_regression_with_all_causes(self):
        line = self.verdict(self.metrics(0.90, 4, {"오류": 2}, regressions=1), self.metrics(0.90, 3))
        self.assertIn("미달(Opus 호출, 대체 실행, 회귀)", line)

    def test_compare_fallback_boundary_is_ten_percent(self):
        self.assertIn("통과", self.verdict(self.metrics(0.90, 3, {"한도": 1}), self.metrics(0.90, 3)))
        self.assertIn("미달(대체 실행)", self.verdict(self.metrics(0.90, 3, {"한도": 2}), self.metrics(0.90, 3)))

    def test_compare_without_cli_jobs_is_not_a_pass(self):
        line = self.verdict(self.metrics(0.90, 3, cli_jobs=0), self.metrics(0.90, 3))
        self.assertIn("미달(대체 실행 미측정)", line)

    def test_compare_without_agreement_is_not_a_pass(self):
        self.assertIn("미달(일치율)", self.verdict(self.metrics(None, 3), self.metrics(0.90, 3)))

    def test_compare_table_puts_both_runs_side_by_side(self):
        ext, base = self.metrics(0.86, 3, {"한도": 1}), self.metrics(0.90, 3, cli_jobs=0)
        ext["claude_calls"], base["claude_calls"] = {"sonnet": 0, "haiku": 1, "opus": 3}, {"sonnet": 4, "haiku": 4, "opus": 3}
        text = score.render_compare(ext, base)
        self.assertIn("| 값 | Claude 작업자 | 외부 작업자 |", text)
        self.assertIn("| Claude 호출 | Sonnet 4 · Haiku 4 · Opus 3 | Sonnet 0 · Haiku 1 · Opus 3 |", text)
        self.assertIn("| Opus 호출 수 | 3 | 3 |", text)
        self.assertIn("| 대체 실행 | - | 1/10 (10.0%) — 한도 1 |", text)
        self.assertIn("통과", text.splitlines()[-1])

    def test_compare_cli_prints_table_and_verdict(self):
        sample = FIXTURES / "sample.jsonl"
        base, ext = self.runs / "claude", self.runs / "external"
        make_run(base, "s1", [decision("합의-사실", "a")])
        make_run(ext, "s1", [decision("합의-사실", "a")],
                 events=[workers_event("stage1", {"ok": 10})])
        out = self.runs / "cmp.md"
        p = subprocess.run([sys.executable, str(ROOT / "eval" / "score.py"), "--runs", str(ext), "--baseline-runs", str(base),
                            "--sample", str(sample), "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("| 값 | Claude 작업자 | 외부 작업자 |", p.stdout)
        self.assertIn("비교 판정: 통과", p.stdout)
        self.assertNotIn("성공 기준:", p.stdout)
        self.assertIn("비교 판정: 통과", out.read_text(encoding="utf-8"))


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"calc.py": CALC, "test_calc.py": TEST_CALC, ".gitignore": "__pycache__/\n"})
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -m unittest discover -s ."],
            "format": [],
        }})
        self.out = self.tmp / "out"

    def item(self, item_id, commit=None, truth="아님"):
        return {"id": item_id, "repo": str(self.repo), "commit": commit or self.head, "source": "codex-code",
                "doc": None, "finding": FINDING, "truth": {"verdict": truth, "accepted_patch": None},
                "baseline_opus_calls": 1}

    def write_sample(self, items):
        path = self.tmp / "sample.jsonl"
        path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
        return path

    def write_fake(self, item_id):
        fake = self.tmp / "fake" / item_id
        write_json(fake / "stage1.json", {"results": [
            {"id": f"r1-01:{r}", "output": {"verdict": "아님", "needs_design_decision": False, "reason": "쓰임이 없다",
                                            "verify": [], "model_id": m}}
            for r, m in (("a", "claude-sonnet-5-5"), ("b", "claude-haiku-5-5"))]})
        return self.tmp / "fake"

    def run_replay(self, sample, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return replay.main(["--sample", str(sample), "--out", str(self.out), "--repos-toml", str(self.toml), *extra])

    def test_replay_uses_fake_results_and_writes_ledgers(self):
        sample = self.write_sample([self.item("item-01")])
        fake = self.write_fake("item-01")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake)), 0)
        base = self.out / "item-01"
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertTrue(ledger["dry_run"])
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["outcome"], "합의-아님")
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["truth"], {"verdict": "아님", "accepted_patch": None})
        self.assertEqual(meta["baseline_opus_calls"], 1)
        self.assertNotIn("error", meta)
        self.assertTrue((base / "run" / "round-1" / "results.json").is_file())
        self.assertTrue((base / "run" / "report.md").is_file())
        self.assertFalse((base / "wt").exists())
        listed = git(self.repo, "worktree", "list", "--porcelain")
        self.assertEqual(listed.count("worktree "), 1)
        self.assertEqual(git(self.repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        m = score.compute(self.out, score.load_sample(sample))
        self.assertEqual((m["agree"], m["judged"]), (1, 1))

    def test_replay_fake_skips_adjudicate_without_adj_args(self):
        sample = self.write_sample([self.item("item-01")])
        fake = self.write_fake("item-01")
        write_json(fake / "item-01" / "adj.json", {"results": [
            {"id": "r1-01:adjudicator", "output": {"verdict": "사실", "needs_design_decision": False, "reason": "x",
                                                   "verify": [], "model_id": "claude-opus-5-5"}}]})
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake)), 0)
        base = self.out / "item-01"
        self.assertNotIn("error", json.loads((base / "meta.json").read_text(encoding="utf-8")))
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["outcome"], "합의-아님")
        self.assertFalse((base / "run" / "round-1" / "adj.json").exists())

    def test_replay_passes_adopt_on_both_pass_to_init(self):
        sample = self.write_sample([self.item("item-01")])
        fake = self.write_fake("item-01")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake), "--adopt-on-both-pass"), 0)
        ledger = json.loads((self.out / "item-01" / "ledger.json").read_text(encoding="utf-8"))
        self.assertIs(ledger["adopt_on_both_pass"], True)

    def test_replay_doc_mode_uses_fake_results_and_writes_ledgers(self):
        (self.repo / "plan.md").write_text("# 계획\n\n1단계는 `calc.py`를 고친다.\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "plan")
        item = {"id": "doc-01", "repo": str(self.repo), "commit": git(self.repo, "rev-parse", "HEAD"),
                "source": "codex-doc", "doc": "plan.md",
                "finding": {**FINDING, "source": "codex-doc", "location": "plan.md:3", "body": "단계가 모호하다"},
                "truth": {"verdict": "아님", "accepted_patch": None}, "baseline_opus_calls": 1}
        sample = self.write_sample([item])
        fake = self.tmp / "fake"
        write_json(fake / "doc-01" / "stage1.json", {"results": [
            {"id": f"r1-01:{r}", "output": {"verdict": "아님", "needs_design_decision": False, "reason": "이미 구체적이다",
                                            "verify": [], "model_id": m}}
            for r, m in (("a", "claude-sonnet-5-5"), ("b", "claude-haiku-5-5"), ("writer", "claude-sonnet-5-5"))]})
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake)), 0)
        base = self.out / "doc-01"
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertNotIn("error", meta)
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual((ledger["mode"], ledger["source"]), ("doc", "codex-doc"))
        self.assertEqual(list(ledger["docs"]), ["plan.md"])
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["outcome"], "합의-아님")
        self.assertTrue((base / "run" / "round-1" / "results.json").is_file())
        self.assertTrue((base / "run" / "report.md").is_file())
        self.assertFalse((base / "wt").exists())
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(git(self.repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        m = score.compute(self.out, score.load_sample(sample))
        self.assertEqual((m["agree"], m["judged"]), (1, 1))

    def test_replay_worktree_add_skips_user_hooks(self):
        marker = self.tmp / "hook-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        sample = self.write_sample([self.item("item-01")])
        self.assertEqual(self.run_replay(sample, "--fake-results", str(self.write_fake("item-01"))), 0)
        self.assertFalse(marker.exists())

    def test_replay_records_error_and_continues(self):
        sample = self.write_sample([self.item("bad", commit="deadbeef" * 5), self.item("item-02")])
        fake = self.write_fake("item-02")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake)), 1)
        meta = json.loads((self.out / "bad" / "meta.json").read_text(encoding="utf-8"))
        self.assertIn("error", meta)
        self.assertTrue((self.out / "item-02" / "ledger.json").is_file())
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)
        m = score.compute(self.out, score.load_sample(sample))
        self.assertEqual((m["errors"], m["items"]), (1, 1))

    def test_replay_limit(self):
        sample = self.write_sample([self.item("item-01"), self.item("item-02")])
        fake = self.write_fake("item-01")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake), "--limit", "1"), 0)
        self.assertTrue((self.out / "item-01").is_dir())
        self.assertFalse((self.out / "item-02").exists())

    def start_pending(self, item_id="item-01"):
        sample = self.write_sample([self.item(item_id)])
        self.assertEqual(self.run_replay(sample), 0)
        base = self.out / item_id
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        return sample, base, Path(meta["pending_run_dir"])

    def run_steps(self, base, run_dir, *commands):
        env = replay.base_env(base / "repos.toml")
        for command in commands:
            extra = {"judge": ["--round", "1", "--results", str(run_dir / "round-1" / "stage1.json")],
                     "apply": ["--round", "1"], "report": []}[command]
            replay.cli(base / "wt", env, command, "--run-dir", str(run_dir), *extra)

    def finish_steps(self, base, run_dir, *commands):
        shutil.copyfile(self.write_fake(base.name) / base.name / "stage1.json", run_dir / "round-1" / "stage1.json")
        self.run_steps(base, run_dir, *commands)

    def collect(self, item_id="item-01"):
        with contextlib.redirect_stdout(io.StringIO()):
            return replay.collect_item(self.item(item_id), self.out, self.toml)

    def test_replay_without_fake_results_stops_after_jobs_and_collect_finishes(self):
        sample, base, run_dir = self.start_pending()
        self.assertTrue((run_dir / "round-1" / "stage1-args.json").is_file())
        self.assertTrue((base / "wt").is_dir())

        self.assertEqual(self.run_replay(sample, "--collect"), 0)
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertIs(meta["incomplete"], True)
        self.assertIn("pending_run_dir", meta)
        self.assertFalse((base / "ledger.json").exists())
        self.assertTrue((base / "wt").is_dir())
        m = score.compute(self.out, score.load_sample(sample))
        self.assertEqual((m["incomplete"], m["items"]), (1, 0))

        self.finish_steps(base, run_dir, "judge", "apply", "report")
        self.assertEqual(self.run_replay(sample, "--collect"), 0)
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertNotIn("pending_run_dir", meta)
        self.assertNotIn("incomplete", meta)
        self.assertTrue((base / "ledger.json").is_file())
        self.assertFalse((base / "wt").exists())
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_collect_requires_report(self):
        _, base, run_dir = self.start_pending()
        self.finish_steps(base, run_dir, "judge", "apply")
        self.assertTrue((run_dir / "round-1" / "apply.json").is_file())
        self.assertFalse(self.collect())
        self.assertTrue((base / "wt").exists())
        self.assertIs(json.loads((base / "meta.json").read_text(encoding="utf-8"))["incomplete"], True)
        self.assertFalse((base / "ledger.json").exists())

    def test_collect_requires_apply_event_and_no_pending_opinion(self):
        _, base, run_dir = self.start_pending()
        self.finish_steps(base, run_dir, "judge", "apply", "report")
        ledger_path = run_dir / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        events = ledger["rounds"][0]["events"]
        ledger["rounds"][0]["events"] = [e for e in events if e["step"] != "apply"]
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(self.collect())
        ledger["rounds"][0]["events"] = events
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
        opinion = run_dir / "round-1" / "opinion-r1-01.md"
        opinion.write_text("의견 프롬프트", encoding="utf-8")
        self.assertFalse(self.collect())
        self.assertTrue((base / "wt").exists())
        opinion.unlink()
        self.assertTrue(self.collect())
        self.assertFalse((base / "wt").exists())

    def test_replay_passes_only_explicit_options(self):
        item, wt = self.item("item-01"), self.tmp / "wt"
        args = replay.init_args(item, wt, profile="m-claude-only", workers=None, adopt_on_both_pass=False)
        self.assertEqual(args[args.index("--profile") + 1], "m-claude-only")
        self.assertNotIn("--workers", args)
        self.assertNotIn("--adopt-on-both-pass", args)
        self.assertIn("--dry-run", args)
        bare = replay.init_args(item, wt, profile=None, workers=None, adopt_on_both_pass=False)
        self.assertEqual([a for a in bare if a in ("--profile", "--workers", "--adopt-on-both-pass")], [])
        full = replay.init_args(item, wt, profile="p", workers="external", adopt_on_both_pass=True)
        self.assertEqual(full[full.index("--workers") + 1], "external")
        self.assertIn("--adopt-on-both-pass", full)

    def test_replay_records_init_lines_and_confirmed_roles(self):
        sample = self.write_sample([self.item("item-01")])
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = replay.main(["--sample", str(sample), "--out", str(self.out), "--repos-toml", str(self.toml),
                                "--fake-results", str(self.write_fake("item-01")), "--profile", "claude-only"])
        self.assertEqual(code, 0)
        base = self.out / "item-01"
        init_text = (base / "init.txt").read_text(encoding="utf-8")
        self.assertIn("프로필: claude-only (--profile)", init_text)
        self.assertIn("역할:", init_text)
        self.assertNotIn("덮음:", init_text)
        self.assertNotIn("run_dir=", init_text)
        self.assertIn("item-01: 프로필: claude-only (--profile)", buf.getvalue())
        self.assertIn("item-01: 역할 확정: code.a=claude(claude-sonnet-5-5, low), code.b=claude(claude-haiku-5-5, low)", buf.getvalue())
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual({k: meta["roles"][k]["runner"] for k in meta["roles"]},
                         {"code.a": "claude", "code.b": "claude", "code.adjudicator": "claude"})
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["profile"]["name"], "claude-only")

    def test_doc_mode_confirmed_roles_use_the_profile_cell_keys(self):
        keys = ["doc.judge_a", "doc.judge_b", "doc.writer", "doc.checker", "doc.adjudicator"]
        cell = {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low", "fallback": None}
        ledger = {"mode": "doc", "profile": {"roles": {key: dict(cell) for key in keys}}}
        self.assertEqual(sorted(replay.confirmed_roles(ledger)), sorted(keys))

    def test_worktree_toml_matches_key_written_through_symlink(self):
        link = self.tmp / "link"
        link.symlink_to(self.tmp)
        (self.tmp / "cfg").mkdir()
        toml = write_repos_toml(self.tmp / "cfg", {str(link / "repo"): {"tests": ["make test"]}})
        dest = self.tmp / "wt.toml"
        replay.write_worktree_toml(toml, self.repo, self.tmp / "wt", dest)
        self.assertIn('tests = ["make test"]', dest.read_text(encoding="utf-8"))

    def test_worktree_toml_keeps_profile_definitions_the_repo_selects(self):
        src = self.tmp / "src.toml"
        src.write_text(f'[profiles.mine]\nbase = "claude-only"\nadopt_on_both_pass = false\n'
                       f'"code.a" = {{ runner = "codex", model = "", effort = "low", '
                       f'fallback = {{ runner = "claude", model = "claude-sonnet-5-5", effort = "low" }} }}\n'
                       f'[repos."{self.repo}"]\ntests = ["make test"]\nprofile = "mine"\n', encoding="utf-8")
        dest, wt = self.tmp / "wt.toml", self.tmp / "wt"
        replay.write_worktree_toml(src, self.repo, wt, dest)
        written = tomllib.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(written["profiles"], tomllib.loads(src.read_text(encoding="utf-8"))["profiles"])
        self.assertEqual(written["repos"][str(wt)]["profile"], "mine")

    def test_worktree_toml_keeps_default_profile(self):
        src = self.tmp / "src.toml"
        src.write_text(f'default_profile = "claude-only"\n[repos."{self.repo}"]\ntests = ["make test"]\n', encoding="utf-8")
        dest = self.tmp / "wt.toml"
        replay.write_worktree_toml(src, self.repo, self.tmp / "wt", dest)
        self.assertEqual(tomllib.loads(dest.read_text(encoding="utf-8"))["default_profile"], "claude-only")

    def test_replay_rejects_reused_worktree_at_other_commit(self):
        (self.repo / "calc.py").write_text(CALC + "\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "second")
        git(self.repo, "worktree", "add", "--detach", str(self.out / "item-01" / "wt"), self.head)
        sample = self.write_sample([self.item("item-01", commit=git(self.repo, "rev-parse", "HEAD"))])
        self.assertEqual(self.run_replay(sample, "--fake-results", str(self.write_fake("item-01"))), 1)
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        self.assertIn("HEAD", meta["error"])
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)


class ReplayExternalTest(WorkersTestBase):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.env["FAKE_CODEX_RESULT"] = result_json(verdict="아님")
        self.env["FAKE_CURSOR_RESULTS"] = json.dumps([self.sandbox_reply, {"result": fenced(result_json(verdict="아님"))}],
                                                     ensure_ascii=False)
        self.out = self.tmp / "out"
        self.fake = self.tmp / "fake"

    def sample(self):
        item = {"id": "item-01", "repo": str(self.repo), "commit": git(self.repo, "rev-parse", "HEAD"),
                "source": "codex-code", "doc": None, "finding": FINDING,
                "truth": {"verdict": "아님", "accepted_patch": None}, "baseline_opus_calls": 1}
        path = self.tmp / "sample.jsonl"
        path.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def replay(self, *extra):
        argv = ["--sample", str(self.sample()), "--out", str(self.out), "--repos-toml", self.env["CASCADE_REPOS_TOML"], *extra]
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()) as buf:
            code = replay.main(argv)
        return code, buf.getvalue()

    def pending(self):
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        return Path(meta["pending_run_dir"])

    def finish_by_cli(self):
        base = self.out / "item-01"
        env = {**self.env, "CASCADE_REPOS_TOML": str(base / "repos.toml")}
        run_dir = str(self.pending())
        for command in ("judge", "apply", "report"):
            extra = ["--round", "1"] if command != "report" else []
            p = run_cli([command, "--run-dir", run_dir, *extra], cwd=base / "wt", env=env)
            self.assertEqual(p.returncode, 0, f"{command}\n{p.stdout}\n{p.stderr}")

    def test_replay_external_runs_workers_with_fake_cli_then_collects(self):
        code, _ = self.replay("--workers", "external")
        self.assertEqual(code, 0)
        run_dir = self.pending()
        self.assertTrue((run_dir / "round-1" / "stage1-cli.json").is_file())
        self.assertFalse((run_dir / "round-1" / "stage1.json").exists())
        self.finish_by_cli()
        code, _ = self.replay("--collect")
        self.assertEqual(code, 0)
        base = self.out / "item-01"
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["workers_choice"], "external")
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["outcome"], "합의-아님")
        self.assertTrue((base / "run" / "report.md").is_file())
        m = score.compute(self.out, score.load_sample(self.sample()))
        self.assertEqual((m["agree"], m["judged"], m["cli_jobs"], m["fallback"]), (1, 1, 2, {}))
        self.assertEqual(m["claude_calls"], {"sonnet": 0, "haiku": 0, "opus": 0})
        self.assertEqual(set(m["usage"]), {"codex", "cursor"})

    def test_replay_external_uses_fallback_results_for_failed_cli_job(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-01-b")])
        code, _ = self.replay("--workers", "external")
        self.assertEqual(code, 0)
        write_json(self.pending() / "round-1" / "stage1-fallback.json", {"results": [
            {"id": "r1-01:b", "output": {"verdict": "아님", "needs_design_decision": False, "reason": "쓰임이 없다",
                                         "verify": [], "model_id": "claude-haiku-5-5"}}]})
        self.finish_by_cli()
        code, _ = self.replay("--collect")
        self.assertEqual(code, 0)
        m = score.compute(self.out, score.load_sample(self.sample()))
        self.assertEqual((m["agree"], m["judged"]), (1, 1))
        self.assertEqual((m["fallback"], m["claude_calls"]["haiku"]), ({"한도": 1}, 1))

    def test_replay_rejects_fake_results_with_external_workers_before_any_cli_call(self):
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                replay.main(["--sample", str(self.sample()), "--out", str(self.out), "--workers", "external",
                             "--fake-results", str(self.fake)])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("external", err.getvalue())
        self.assertIn("모델을 부르지 않", err.getvalue())
        self.assertFalse((self.out / "item-01").exists())

    def test_fake_results_refuse_external_profile(self):
        code, text = self.replay("--fake-results", str(self.fake), "--profile", "local")
        self.assertEqual(code, 1)
        self.assertIn("오류", text)
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        self.assertIn("code.a=codex", meta["error"])
        self.assertEqual(list((self.tmp / "log").glob("*.args.json")), [])
        self.assertFalse((self.out / "item-01" / "run" / "round-1" / "stage1-args.json").exists())
        self.assertFalse((self.out / "item-01" / "wt").exists())

    def test_replay_external_without_fake_results_stops_after_workers(self):
        code, text = self.replay("--workers", "external")
        self.assertEqual(code, 0)
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        run_dir = Path(meta["pending_run_dir"])
        self.assertTrue((run_dir / "round-1" / "stage1-cli.json").is_file())
        self.assertFalse((run_dir / "round-1" / "decisions.json").exists())
        self.assertIn("workers", text)

    def test_replay_claude_workers_is_default(self):
        code, _ = self.replay()
        self.assertEqual(code, 0)
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        ledger = json.loads((Path(meta["pending_run_dir"]) / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["workers_choice"], "claude")
        self.assertFalse((Path(meta["pending_run_dir"]) / "round-1" / "stage1-cli.json").exists())


class EvalScriptsTest(unittest.TestCase):
    def test_eval_scripts_still_import_stdlib(self):
        for script in ("replay.py", "score.py", "select_profiles.py", "adopt_recalc.py", "first_review.py", "regress.py"):
            proc = subprocess.run([sys.executable, str(ROOT / "eval" / script), "--help"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{script}: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
