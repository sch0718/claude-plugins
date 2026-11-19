import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tests"))

import first_review  # noqa: E402
from test_cli_e2e import CliTestBase  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
CODEX_DOC_FINAL = FIXTURES / "codex-doc.final"
CLAUDE_BODY = FIXTURES / "codex-doc-2.final"
CURSOR_FINAL = FIXTURES / "cursor-doc.final"
CURSOR_LOG = FIXTURES / "cursor-review.log"

CLAUDE_TEXT = """Critical
1. 변경이 설계와 어긋난다
   위치: plan.md:10
   근거: 설계서 3절과 다르다
Warning
1. 두 번째 문제
   위치: plan.md:40
   근거: 근거가 없다
"""
CODEX_TEXT = """Warning
1. 같은 문제를 다르게 적었다
   위치: plan.md:20
   근거: 코덱스의 근거
2. 코덱스만 찾았다
   위치: plan.md:80
   근거: 코덱스만의 근거
"""
CURSOR_TEXT = """Warning
1. 코덱스와 같은 문제다
   위치: plan.md:22
   근거: 커서의 근거
Suggestion
1. 커서만 찾았다
   위치: plan.md:90
   근거: 커서만의 근거
"""
TRUTH = {"g01": "아님", "g02": "사실", "g03": "사실", "g04": "아님", "g05": "재현 불가"}


def confirmed_of(merged: dict) -> dict:
    return {**merged, "confirmed": True}


def usage_data(*costs, wf="wf_1"):
    rows = [{"job_id": "review:reviewer", "label": "검토", "cell": "doc.review", "model": "claude-opus-5-5", "input": 100,
             "output": 20, "cache_read": 5, "cache_write": 1, "cost": cost} for cost in costs]
    return {"prices_as_of": "2026-10-06", "price_source": "자료", "workflows": {wf: rows}, "unmatched": {wf: []}}


class MergeTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def wrapped_claude(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "review.json"
        text = CLAUDE_BODY.read_text(encoding="utf-8")
        path.write_text(json.dumps({"results": [{"id": "review:reviewer", "output": {"text": text, "model_id": "claude-opus-5-5"}}]}),
                        encoding="utf-8")
        return path

    def test_merge_reads_real_review_json(self):
        merged = first_review.merge_files(claude=self.wrapped_claude(self.tmp / "c"), codex=CODEX_DOC_FINAL, cursor=CURSOR_FINAL,
                                          mode="doc")
        self.assertTrue(merged["findings"])
        self.assertEqual({f["runner"] for f in merged["findings"]}, {"claude", "codex", "cursor"})

    def test_merge_records_found_by_and_proposes_groups(self):
        merged = first_review.merge(CLAUDE_TEXT, CODEX_TEXT, CURSOR_TEXT, mode="doc")
        self.assertTrue(any(set(g["found_by"]) == {"codex", "cursor"} for g in merged["groups"]))
        group = next(g for g in merged["groups"] if g["found_by"] == ["codex", "cursor"])
        self.assertEqual(group["members"], [{"runner": "codex", "id": "r1-01"}, {"runner": "cursor", "id": "r1-01"}])
        self.assertTrue(group["proposed"])
        self.assertEqual([g["id"] for g in merged["groups"]], ["g01", "g02", "g03", "g04", "g05"])
        self.assertIs(merged["confirmed"], False)

    def test_same_reviewer_findings_are_not_proposed_together(self):
        text = "Warning\n1. 하나\n   위치: a.md:3\n   근거: x\n2. 둘\n   위치: a.md:4\n   근거: y\n"
        merged = first_review.merge(text, None, None, mode="doc")
        self.assertEqual(len(merged["groups"]), 2)

    def test_distance_over_five_lines_is_not_proposed(self):
        far = "Warning\n1. 멀다\n   위치: plan.md:26\n   근거: x\n"
        merged = first_review.merge(None, CODEX_TEXT, far, mode="doc")
        self.assertEqual(len(merged["groups"]), 3)
        near = "Warning\n1. 가깝다\n   위치: plan.md:25\n   근거: x\n"
        self.assertEqual(len(first_review.merge(None, CODEX_TEXT, near, mode="doc")["groups"]), 2)

    def test_location_without_line_groups_only_on_identical_text(self):
        one = "Warning\n1. 하나\n   위치: 전체 구성\n   근거: x\n"
        same = "Warning\n1. 같은 곳\n   위치: 전체 구성\n   근거: y\n"
        other = "Warning\n1. 다른 곳\n   위치: 다른 구성\n   근거: y\n"
        self.assertEqual(len(first_review.merge(None, one, same, mode="doc")["groups"]), 1)
        self.assertEqual(len(first_review.merge(None, one, other, mode="doc")["groups"]), 2)

    def test_absolute_location_under_root_groups_with_relative_one(self):
        claude = "Warning\n1. 상대 경로\n   위치: cascade/report.py:610\n   근거: x\n"
        codex = "Warning\n1. 절대 경로\n   위치: /private/tmp/wt/T1/cascade/report.py:612\n   근거: y\n"
        self.assertEqual(len(first_review.merge(claude, codex, None, mode="doc")["groups"]), 2)
        merged = first_review.merge(claude, codex, None, mode="doc", root="/private/tmp/wt/T1/")
        self.assertEqual(len(merged["groups"]), 1)
        self.assertEqual({f["location"] for f in merged["findings"]}, {"cascade/report.py:610", "cascade/report.py:612"})

    def test_location_outside_root_is_kept(self):
        codex = "Warning\n1. 밖\n   위치: /other/T1x/a.py:3\n   근거: y\n2. 접두만 같음\n   위치: /private/tmp/wt/T1x/a.py:4\n   근거: y\n"
        merged = first_review.merge(None, codex, None, mode="doc", root="/private/tmp/wt/T1")
        self.assertEqual([f["location"] for f in merged["findings"]], ["/other/T1x/a.py:3", "/private/tmp/wt/T1x/a.py:4"])

    def test_relative_location_is_kept_even_when_root_is_relative(self):
        claude = "Warning\n1. 상대\n   위치: project/a.py:10\n   근거: x\n"
        merged = first_review.merge(claude, None, None, mode="doc", root="project")
        self.assertEqual([f["location"] for f in merged["findings"]], ["project/a.py:10"])

    def test_code_mode_reads_codex_code_format(self):
        codex = (FIXTURES / "codex-code.final").read_text(encoding="utf-8")
        merged = first_review.merge(None, codex, None, mode="code")
        self.assertEqual([f["severity"] for f in merged["findings"]], ["Warning"])
        self.assertEqual(merged["findings"][0]["location"], "shared/example-common/src/example_common/git_manager.py:157")

    def test_reviewers_record_which_were_given(self):
        merged = first_review.merge(None, CODEX_TEXT, None, mode="doc")
        self.assertEqual({r: v["given"] for r, v in merged["reviewers"].items()}, {"claude": False, "codex": True, "cursor": False})

    def test_no_reviewer_is_an_error(self):
        with self.assertRaises(first_review.FirstReviewError):
            first_review.merge(None, None, None, mode="doc")

    def test_unparseable_reviewer_text_names_the_reviewer(self):
        with self.assertRaises(first_review.FirstReviewError) as ctx:
            first_review.merge(None, "형식이 아닌 글", None, mode="doc")
        self.assertIn("Codex", str(ctx.exception))

    def test_empty_claude_result_is_an_error(self):
        path = self.tmp / "review.json"
        path.write_text(json.dumps({"results": []}), encoding="utf-8")
        with self.assertRaises(first_review.FirstReviewError) as ctx:
            first_review.merge_files(claude=path, codex=None, cursor=None, mode="doc")
        self.assertIn("Claude", str(ctx.exception))

    def test_merge_carries_reviewer_meta_and_usage(self):
        review_dir = self.tmp / "claude-review"
        claude = self.wrapped_claude(review_dir)
        (review_dir / "review-meta.json").write_text(json.dumps({"role": "doc.review", "actual": {"runner": "claude", "model": "claude-opus-5-5"},
                                                                  "args": {"secret": "x"}}), encoding="utf-8")
        (review_dir / "claude-usage.json").write_text(json.dumps(usage_data(0.5)), encoding="utf-8")
        merged = first_review.merge_files(claude=claude, codex=CODEX_DOC_FINAL, cursor=CURSOR_FINAL, mode="doc")
        self.assertIn("usage", merged["reviewers"]["claude"])
        self.assertIn("meta", merged["reviewers"]["claude"])
        self.assertEqual(merged["reviewers"]["claude"]["meta"], {"role": "doc.review", "actual": {"runner": "claude", "model": "claude-opus-5-5"}})
        self.assertEqual(merged["reviewers"]["claude"]["usage"]["workflows"]["wf_1"][0]["cost"], 0.5)
        self.assertIsNone(merged["reviewers"]["codex"]["usage"])
        self.assertIsNone(merged["reviewers"]["codex"]["meta"])

    def test_cursor_tokens_come_from_raw_log_next_to_final(self):
        review_dir = self.tmp / "cursor-review"
        review_dir.mkdir()
        (review_dir / "review.out.final").write_text(CURSOR_FINAL.read_text(encoding="utf-8"), encoding="utf-8")
        (review_dir / "review.out").write_text(CURSOR_LOG.read_text(encoding="utf-8"), encoding="utf-8")
        merged = first_review.merge_files(claude=None, codex=None, cursor=review_dir / "review.out.final", mode="doc")
        self.assertEqual(merged["reviewers"]["cursor"]["usage"], {"input": 1234, "output": 567, "cache_read": 89, "cache_write": 10})

    def test_cursor_tokens_without_raw_log_are_none(self):
        merged = first_review.merge_files(claude=None, codex=None, cursor=CURSOR_FINAL, mode="doc")
        self.assertIsNone(merged["reviewers"]["cursor"]["usage"])


class CursorUsageTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def log(self, text: str) -> Path:
        path = self.tmp / "review.out"
        path.write_text(text, encoding="utf-8")
        return path

    def test_cursor_tokens_from_log_json(self):
        self.assertEqual(first_review.cursor_usage(CURSOR_LOG)["input"], 1234)

    def test_whole_file_json(self):
        usage = {"inputTokens": 7, "outputTokens": 1, "cacheReadTokens": 0, "cacheWriteTokens": 3}
        path = self.log(json.dumps({"type": "result", "usage": usage}, indent=2))
        self.assertEqual(first_review.cursor_usage(path), {"input": 7, "output": 1, "cache_read": 0, "cache_write": 3})

    def test_missing_usage_key_is_none_not_zero(self):
        self.assertIsNone(first_review.cursor_usage(self.log(json.dumps({"type": "result", "usage": {"inputTokens": 7, "outputTokens": 1}}))))
        self.assertIsNone(first_review.cursor_usage(self.log(json.dumps({"type": "result", "usage": {}}))))

    def test_no_envelope_is_none_not_zero(self):
        self.assertIsNone(first_review.cursor_usage(self.log("오류만 있는 로그\n")))
        self.assertIsNone(first_review.cursor_usage(self.log('{"type":"system"}\n')))

    def test_envelope_without_usage_is_none(self):
        self.assertIsNone(first_review.cursor_usage(self.log('{"type":"result","result":"x"}\n')))

    def test_missing_file_is_none(self):
        self.assertIsNone(first_review.cursor_usage(self.tmp / "none"))


class ConfirmTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.merged = first_review.merge(CLAUDE_TEXT, CODEX_TEXT, CURSOR_TEXT, mode="doc")
        self.out = self.tmp / "out"

    def test_confirm_writes_findings_input(self):
        first_review.confirm(confirmed_of(self.merged), self.out)
        self.assertTrue((self.out / "findings-input.json").is_file())
        findings = json.loads((self.out / "findings-input.json").read_text(encoding="utf-8"))
        self.assertEqual([f["id"] for f in findings], ["g01", "g02", "g03", "g04", "g05"])
        merged_group = next(f for f in findings if f["id"] == "g03")
        self.assertEqual(merged_group["severity"], "Warning")
        self.assertEqual(merged_group["location"], "plan.md:20")

    def test_group_takes_highest_severity_of_members(self):
        codex = "Suggestion\n1. 낮다\n   위치: plan.md:20\n   근거: x\n"
        cursor = "Critical\n1. 높다\n   위치: plan.md:21\n   근거: y\n"
        merged = first_review.merge(None, codex, cursor, mode="doc")
        first_review.confirm(confirmed_of(merged), self.out)
        finding = json.loads((self.out / "findings-input.json").read_text(encoding="utf-8"))[0]
        self.assertEqual((finding["severity"], finding["location"]), ("Critical", "plan.md:21"))

    def test_found_by_map_keeps_original_runner_and_id(self):
        first_review.confirm(confirmed_of(self.merged), self.out)
        found_by = json.loads((self.out / "found-by.json").read_text(encoding="utf-8"))
        self.assertEqual(found_by["g03"], [{"runner": "codex", "id": "r1-01"}, {"runner": "cursor", "id": "r1-01"}])
        self.assertEqual(found_by["g01"], [{"runner": "claude", "id": "r1-01"}])

    def test_confirmed_input_is_accepted_by_init(self):
        (self.repo / "plan.md").write_text("# 계획\n" * 100, encoding="utf-8")
        first_review.confirm(confirmed_of(self.merged), self.out)
        p = self.cli("init", "--source", "claude-doc", "--findings-json", str(self.out / "findings-input.json"), "--doc",
                     str(self.repo / "plan.md"), "--dry-run")
        self.assertIn("지적 5건", p.stdout)

    def test_unconfirmed_input_is_rejected(self):
        with self.assertRaises(first_review.FirstReviewError) as ctx:
            first_review.confirm(self.merged, self.out)
        self.assertIn("confirmed", str(ctx.exception))
        self.assertFalse((self.out / "findings-input.json").exists())

    def test_user_can_regroup(self):
        data = confirmed_of(self.merged)
        data["groups"] = [{"id": "g01", "members": [{"runner": "claude", "id": "r1-02"}, {"runner": "codex", "id": "r1-02"}]},
                          {"id": "g02", "members": [{"runner": "claude", "id": "r1-01"}]},
                          {"id": "g03", "members": [{"runner": "codex", "id": "r1-01"}]},
                          {"id": "g04", "members": [{"runner": "cursor", "id": "r1-01"}]},
                          {"id": "g05", "members": [{"runner": "cursor", "id": "r1-02"}]}]
        first_review.confirm(data, self.out)
        found_by = json.loads((self.out / "found-by.json").read_text(encoding="utf-8"))
        self.assertEqual([m["runner"] for m in found_by["g01"]], ["claude", "codex"])

    def test_invalid_groups_are_rejected(self):
        cases = {
            "미배정": lambda d: d["groups"].pop(),
            "중복 배정": lambda d: d["groups"][0]["members"].append({"runner": "codex", "id": "r1-02"}),
            "없는 지적": lambda d: d["groups"][0]["members"].append({"runner": "codex", "id": "r9-99"}),
            "id 없음": lambda d: d["groups"][0].pop("id"),
            "id 겹침": lambda d: d["groups"][1].update(id="g01"),
            "빈 묶음": lambda d: d["groups"][0].update(members=[]),
        }
        for word, mutate in cases.items():
            with self.subTest(word):
                data = confirmed_of(json.loads(json.dumps(self.merged)))
                mutate(data)
                with self.assertRaises(first_review.FirstReviewError) as ctx:
                    first_review.confirm(data, self.out)
                self.assertIn(word.split()[0], str(ctx.exception))


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.merged = confirmed_of(first_review.merge(CLAUDE_TEXT, CODEX_TEXT, CURSOR_TEXT, mode="doc"))

    def test_precision_recall(self):
        s = first_review.score(self.merged, TRUTH)
        self.assertEqual(s["codex"]["precision"], (1, 2))
        self.assertEqual(s["cursor"]["recall"], (1, 2))
        self.assertEqual(s["claude"]["precision"], (1, 2))
        self.assertEqual(s["claude"]["recall"], (1, 2))

    def test_by_severity_uses_reviewer_grade_for_precision_and_group_grade_for_recall(self):
        s = first_review.score(self.merged, TRUTH)
        self.assertEqual(s["claude"]["by_severity"]["Critical"]["precision"], (0, 1))
        self.assertEqual(s["claude"]["by_severity"]["Warning"]["precision"], (1, 1))
        self.assertEqual(s["codex"]["by_severity"]["Warning"]["precision"], (1, 2))
        self.assertEqual(s["codex"]["by_severity"]["Warning"]["recall"], (1, 2))
        self.assertEqual(s["codex"]["by_severity"]["Suggestion"]["recall"], (0, 0))

    def test_unique_counts_fact_groups_only_that_reviewer_found(self):
        s = first_review.score(self.merged, TRUTH)
        self.assertEqual({r: v["unique"] for r, v in s.items()}, {"claude": 1, "codex": 0, "cursor": 0})
        s = first_review.score(self.merged, {**TRUTH, "g04": "사실"})
        self.assertEqual(s["codex"]["unique"], 1)

    def test_reviewer_not_given_is_left_out(self):
        merged = confirmed_of(first_review.merge(None, CODEX_TEXT, CURSOR_TEXT, mode="doc"))
        s = first_review.score(merged, {"g01": "사실", "g02": "아님", "g03": "사실"})
        self.assertEqual(sorted(s), ["codex", "cursor"])

    def test_missing_truth_line_is_an_error_listing_group_ids(self):
        with self.assertRaises(first_review.FirstReviewError) as ctx:
            first_review.score(self.merged, {"g03": "사실"})
        self.assertIn("g01, g02, g04, g05", str(ctx.exception))
        self.assertNotIn("g03", str(ctx.exception))
        with self.assertRaises(first_review.FirstReviewError) as ctx:
            first_review.score(self.merged, {})
        self.assertIn("g01, g02, g03, g04, g05", str(ctx.exception))

    def test_precision_denominator_is_the_reviewers_finding_count(self):
        truth = {**TRUTH, "g04": "사실"}
        s = first_review.score(self.merged, truth)
        self.assertEqual(s["codex"]["findings"], 2)
        self.assertEqual(s["codex"]["precision"], (2, 2))
        self.assertEqual(s["cursor"]["precision"], (1, 2))
        self.assertEqual(s["cursor"]["recall"], (1, 3))

    def test_structurally_broken_merged_is_a_korean_error(self):
        broken = (["그냥 목록"], {**self.merged, "groups": "문자열"}, {**self.merged, "findings": [1]},
                  {**self.merged, "groups": [{"id": "g01", "members": ["codex r1-01"]}]}, {**self.merged, "groups": ["g01"]},
                  {**self.merged, "findings": [{"runner": "gemini", "id": "x", "severity": "Warning", "location": "a:1", "body": "b"}]})
        for merged in broken:
            with self.subTest(merged=str(merged)[:40]):
                with self.assertRaises(first_review.FirstReviewError) as ctx:
                    first_review.score(merged, TRUTH)
                self.assertIn("merged.json", str(ctx.exception))

    def test_unknown_group_or_truth_is_rejected(self):
        for truth in ({"g99": "사실"}, {"g01": "맞음"}):
            with self.assertRaises(first_review.FirstReviewError):
                first_review.score(self.merged, truth)

    def test_unconfirmed_merged_is_rejected(self):
        with self.assertRaises(first_review.FirstReviewError):
            first_review.score({**self.merged, "confirmed": False}, TRUTH)

    def test_claude_cost_sums_stored_cost(self):
        merged = {**self.merged, "reviewers": {**self.merged["reviewers"]}}
        merged["reviewers"]["claude"] = {**merged["reviewers"]["claude"], "usage": usage_data(0.5, 0.25)}
        cost = first_review.score(merged, TRUTH)["claude"]["cost"]
        self.assertAlmostEqual(cost["cost"], 0.75)
        self.assertEqual(cost["tokens"], {"input": 200, "output": 40, "cache_read": 10, "cache_write": 2})

    def test_usage_claude_overrides_merged_record(self):
        s = first_review.score(self.merged, TRUTH, usage_claude=usage_data(2.0))
        self.assertAlmostEqual(s["claude"]["cost"]["cost"], 2.0)

    def test_unmeasured_stored_cost_is_not_zero(self):
        s = first_review.score(self.merged, TRUTH, usage_claude=usage_data(0.5, None))
        self.assertIsNone(s["claude"]["cost"]["cost"])
        self.assertIn("미측정", s["claude"]["cost"]["note"])

    def test_unreadable_usage_shape_is_unmeasured_with_reason(self):
        s = first_review.score(self.merged, TRUTH, usage_claude={"workflows": {}})
        self.assertIn("형식 확인 필요", s["claude"]["cost"]["note"])

    def test_missing_records_say_no_record(self):
        s = first_review.score(self.merged, TRUTH)
        for runner in ("claude", "codex", "cursor"):
            self.assertEqual(s[runner]["cost"], {"tokens": None, "cost": None, "note": "기록 없음"})

    def test_cursor_tokens_from_merged_or_usage_cli(self):
        tokens = {"input": 5, "output": 1, "cache_read": 0, "cache_write": 0}
        merged = {**self.merged, "reviewers": {**self.merged["reviewers"], "cursor": {**self.merged["reviewers"]["cursor"], "usage": tokens}}}
        s = first_review.score(merged, TRUTH)
        self.assertEqual(s["cursor"]["cost"]["tokens"], tokens)
        self.assertIsNone(s["cursor"]["cost"]["cost"])
        over = first_review.score(self.merged, TRUTH, usage_cli={"input": 9, "output": 2, "cache_read": 0, "cache_write": 0})
        self.assertEqual(over["cursor"]["cost"]["tokens"]["input"], 9)
        self.assertEqual(over["codex"]["cost"]["note"], "기록 없음")

    def test_render_shows_ratios_and_no_record(self):
        text = first_review.render(first_review.score(self.merged, TRUTH))
        self.assertIn("1/2", text)
        self.assertIn("기록 없음", text)
        self.assertIn("| 단독 사실 |", text)


class MainTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = first_review.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_merge_does_not_overwrite_confirmed_merged_json(self):
        out = self.tmp / "out"
        codex = self.write("codex.final", CODEX_TEXT)
        self.assertEqual(self.call(["merge", "--codex", codex, "--doc", "plan.md", "--out", out])[0], 0)
        merged = json.loads((out / "merged.json").read_text(encoding="utf-8"))
        merged["confirmed"] = True
        (out / "merged.json").write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        code, _, err = self.call(["merge", "--codex", codex, "--doc", "plan.md", "--out", out])
        self.assertEqual(code, 1)
        self.assertIn("덮어쓰지 않는다", err)
        self.assertTrue(json.loads((out / "merged.json").read_text(encoding="utf-8"))["confirmed"])

    def test_merge_warns_about_unparsed_codex_code_items(self):
        out = self.tmp / "out"
        codex = self.write("codex.final", "- [P2] 위치가 빠진 항목\n본문\n")
        code, _, err = self.call(["merge", "--codex", codex, "--out", out])
        self.assertEqual(code, 0)
        self.assertIn("형식을 읽지 못한 Codex 항목 1건", err)

    def test_merge_confirm_score_round_trip(self):
        out = self.tmp / "out"
        code, stdout, _ = self.call(["merge", "--codex", self.write("codex.final", CODEX_TEXT), "--cursor",
                                     self.write("cursor.final", CURSOR_TEXT), "--doc", "plan.md", "--out", out])
        self.assertEqual(code, 0)
        self.assertTrue((out / "merged.json").is_file())
        review_md = (out / "review.md").read_text(encoding="utf-8")
        self.assertIn("g01", review_md)
        self.assertIn("plan.md:20", review_md)
        self.assertIn("--confirm", stdout)
        merged = json.loads((out / "merged.json").read_text(encoding="utf-8"))
        self.assertEqual(merged["docs"], ["plan.md"])
        self.assertEqual(self.call(["merge", "--confirm", out / "merged.json", "--out", out])[0], 1)
        merged["confirmed"] = True
        edited = self.write("edited.json", json.dumps(merged, ensure_ascii=False))
        code, stdout, _ = self.call(["merge", "--confirm", edited, "--out", out])
        self.assertEqual(code, 0)
        self.assertIn("--findings-json", stdout)
        self.assertTrue((out / "findings-input.json").is_file())
        truth = self.write("truth.jsonl", "\n".join(json.dumps({"group": g, "truth": t}, ensure_ascii=False)
                                                    for g, t in {"g01": "사실", "g02": "아님", "g03": "사실"}.items()))
        code, stdout, _ = self.call(["score", "--merged", edited, "--truth", truth, "--json", self.tmp / "score.json"])
        self.assertEqual(code, 0)
        self.assertIn("Codex", stdout)
        saved = json.loads((self.tmp / "score.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["codex"]["precision"], [1, 2])

    def test_merge_without_any_reviewer_fails(self):
        code, _, err = self.call(["merge", "--out", self.tmp / "out"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("Traceback", err)

    def test_unreadable_input_is_a_korean_error_without_traceback(self):
        code, _, err = self.call(["merge", "--codex", self.tmp / "none.final", "--out", self.tmp / "out"])
        self.assertEqual(code, 1)
        self.assertIn("읽지 못했다", err)
        self.assertNotIn("Traceback", err)

    def test_bad_truth_line_is_reported(self):
        merged = self.write("m.json", json.dumps(confirmed_of(first_review.merge(None, CODEX_TEXT, None, mode="doc")), ensure_ascii=False))
        truth = self.write("t.jsonl", '{"group": "g01", "truth": "맞음"}\n')
        code, _, err = self.call(["score", "--merged", merged, "--truth", truth])
        self.assertEqual(code, 1)
        self.assertIn("맞음", err)
        truth = self.write("t2.jsonl", '{"group": "g01", "truth": "사실"}\n그냥 글\n')
        code, _, err = self.call(["score", "--merged", merged, "--truth", truth])
        self.assertEqual(code, 1)
        self.assertIn("2번째 줄", err)

    def test_empty_truth_file_fails_listing_missing_groups(self):
        merged = self.write("m.json", json.dumps(confirmed_of(first_review.merge(None, CODEX_TEXT, None, mode="doc")), ensure_ascii=False))
        code, _, err = self.call(["score", "--merged", merged, "--truth", self.write("t.jsonl", "")])
        self.assertEqual(code, 1)
        self.assertIn("g01, g02", err)

    def test_duplicate_truth_group_is_an_error(self):
        merged = self.write("m.json", json.dumps(confirmed_of(first_review.merge(None, CODEX_TEXT, None, mode="doc")), ensure_ascii=False))
        truth = self.write("t.jsonl", '{"group": "g01", "truth": "사실"}\n{"group": "g01", "truth": "아님"}\n{"group": "g02", "truth": "사실"}\n')
        code, _, err = self.call(["score", "--merged", merged, "--truth", truth])
        self.assertEqual(code, 1)
        self.assertIn("2번째 줄의 묶음 g01", err)

    def test_non_string_truth_group_is_an_error(self):
        merged = self.write("m.json", json.dumps(confirmed_of(first_review.merge(None, CODEX_TEXT, None, mode="doc")), ensure_ascii=False))
        truth = self.write("t.jsonl", '{"group": ["g01"], "truth": "사실"}\n')
        code, _, err = self.call(["score", "--merged", merged, "--truth", truth])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("1번째 줄을 읽지 못했다", err)

    def test_broken_merged_file_exits_one_without_traceback(self):
        merged = self.write("m.json", json.dumps(["목록"]))
        code, _, err = self.call(["score", "--merged", merged, "--truth", self.write("t.jsonl", '{"group": "g01", "truth": "사실"}\n')])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("merged.json", err)

    def test_partial_cursor_logs_are_not_summed(self):
        merged = self.write("m.json", json.dumps(confirmed_of(first_review.merge(None, None, CURSOR_TEXT, mode="doc")), ensure_ascii=False))
        truth = self.write("t.jsonl", '{"group": "g01", "truth": "사실"}\n{"group": "g02", "truth": "사실"}\n')
        empty = self.write("empty.log", "usage 없음\n")
        code, stdout, err = self.call(["score", "--merged", merged, "--truth", truth, "--usage-cli", CURSOR_LOG, empty])
        self.assertEqual(code, 0)
        self.assertIn("부분 기록", stdout)
        self.assertNotIn("1234", stdout)
        self.assertIn(str(empty), err)
        code, stdout, _ = self.call(["score", "--merged", merged, "--truth", truth, "--usage-cli", empty])
        self.assertIn("기록 없음", stdout)

    def test_score_reads_usage_files(self):
        merged = self.write("m.json", json.dumps(confirmed_of(first_review.merge(CLAUDE_TEXT, None, CURSOR_TEXT, mode="doc")), ensure_ascii=False))
        truth = self.write("t.jsonl", "".join(json.dumps({"group": f"g0{n}", "truth": "사실"}) + "\n" for n in range(1, 5)))
        usage = self.write("u.json", json.dumps(usage_data(1.5)))
        code, stdout, _ = self.call(["score", "--merged", merged, "--truth", truth, "--usage-claude", usage, "--usage-cli", CURSOR_LOG])
        self.assertEqual(code, 0)
        self.assertIn("$1.5000", stdout)
        self.assertIn("1234", stdout)


if __name__ == "__main__":
    unittest.main()
