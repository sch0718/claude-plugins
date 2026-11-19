import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import findings  # noqa: E402

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
FIX_DOC = (FIX / "codex-doc.final").read_text(encoding="utf-8")
FIX_DOC2 = (FIX / "codex-doc-2.final").read_text(encoding="utf-8")
FIX_CODE = (FIX / "codex-code.final").read_text(encoding="utf-8")
FAKE_GH = [sys.executable, str(HERE / "fake_gh.py")]


def pr_findings(file_name="pr-threads.json", **kwargs):
    old = os.environ.get("CASCADE_FAKE_GH_FILE")
    os.environ["CASCADE_FAKE_GH_FILE"] = str(FIX / file_name)
    try:
        return findings.fetch_pr_findings("o", "r", 7, me="sch0718", gh=FAKE_GH, **kwargs)
    finally:
        if old is None:
            del os.environ["CASCADE_FAKE_GH_FILE"]
        else:
            os.environ["CASCADE_FAKE_GH_FILE"] = old


class DocParserTest(unittest.TestCase):
    def test_doc_parser_sections_and_location(self):
        f = findings.parse_codex_doc(FIX_DOC, 1)
        self.assertEqual([x.severity for x in f], ["Warning", "Suggestion"])
        self.assertEqual(f[0].location, "docs/superpowers/plans/2026-10-16-viewer-ux-phase2.md:690")
        self.assertEqual([x.id for x in f], ["r1-01", "r1-02"])
        self.assertTrue(all(x.source == "codex-doc" for x in f))

    def test_doc_parser_evidence_and_body(self):
        f = findings.parse_codex_doc(FIX_DOC, 2)
        self.assertEqual(f[0].id, "r2-01")
        self.assertTrue(f[0].evidence.startswith("계획서 548행은"))
        self.assertNotIn("제안:", f[0].evidence)
        self.assertTrue(f[0].body.startswith("첫 화면에서 여러 저장소의 링크"))
        self.assertIn("제안", f[0].body)

    def test_doc_parser_multiple_locations_use_first(self):
        f = findings.parse_codex_doc(FIX_DOC, 1)
        self.assertEqual(f[1].location, "docs/superpowers/plans/2026-10-16-viewer-ux-phase2.md:549")
        self.assertIn("549, 602, 635", f[1].body)

    def test_doc_parser_handles_none_sections(self):
        f = findings.parse_codex_doc(FIX_DOC2, 1)
        self.assertEqual([x.severity for x in f], ["Suggestion"])
        self.assertEqual(f[0].location, "docs/superpowers/plans/2026-10-16-viewer-ux-phase2.md:690")
        self.assertTrue(f[0].evidence.startswith("설계서 114행은"))
        self.assertNotIn("Python 코드 블록", f[0].body)

    def test_doc_parser_all_none_is_empty(self):
        text = "Critical\n\n없음.\n\nWarning\n\n없음.\n\nSuggestion\n\n없음.\n"
        self.assertEqual(findings.parse_codex_doc(text, 1), [])

    def test_doc_parser_rejects_unknown_format(self):
        with self.assertRaises(findings.ParseError) as cm:
            findings.parse_codex_doc("아무 말", 1)
        self.assertEqual(cm.exception.line_no, 1)
        self.assertEqual(cm.exception.line, "아무 말")

    def test_doc_parser_accepts_unindented_field_lines(self):
        text = "Warning\n\n1. 들여쓰지 않은 항목\n위치: a.md:3\n근거: 모호하다\n\n2. 둘째\n   위치: b.md:1\n"
        f = findings.parse_codex_doc(text, 1)
        self.assertEqual([x.location for x in f], ["a.md:3", "b.md:1"])
        self.assertEqual(f[0].evidence, "모호하다")

    def test_doc_parser_rejects_item_without_location(self):
        text = "Warning\n\n1. 위치가 없는 항목\n   - 근거: 어딘가\n"
        with self.assertRaises(findings.ParseError) as cm:
            findings.parse_codex_doc(text, 1)
        self.assertEqual(cm.exception.line_no, 3)
        self.assertEqual(cm.exception.line, "1. 위치가 없는 항목")


class CodeParserTest(unittest.TestCase):
    def test_code_parser_maps_priority_and_dedups(self):
        f = findings.parse_codex_code(FIX_CODE, 1)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "Warning")
        self.assertEqual(f[0].source, "codex-code")
        self.assertEqual(f[0].id, "r1-01")
        self.assertEqual(f[0].location, "shared/example-common/src/example_common/git_manager.py:157")
        self.assertTrue(f[0].body.startswith("fetch 시간 초과 시 하위 전송 프로세스도 종료하세요"))
        self.assertTrue(f[0].evidence.startswith("비기본 브랜치를 검색하다"))
        self.assertFalse(f[0].evidence.startswith("Warning:"))

    def _item(self, prio, body):
        return f"Review comment:\n\n- [{prio}] 제목 — a/b.py:3-5\n  {body}\n"

    def test_code_parser_label_overrides_priority(self):
        f = findings.parse_codex_code(self._item("P3", "Critical: 데이터가 사라집니다."), 1)
        self.assertEqual(f[0].severity, "Critical")
        self.assertEqual(f[0].evidence, "데이터가 사라집니다.")
        self.assertEqual(f[0].location, "a/b.py:3")

    def test_code_parser_priority_map(self):
        for prio, sev in (("P0", "Critical"), ("P1", "Critical"), ("P2", "Warning"), ("P3", "Suggestion"), ("P9", "Warning")):
            f = findings.parse_codex_code(self._item(prio, "본문입니다."), 1)
            self.assertEqual(f[0].severity, sev, prio)
        self.assertEqual(findings.SEVERITY_MAP["P1"], "Critical")

    def test_code_parser_multiple_items_numbered_in_order(self):
        text = self._item("P1", "첫째.") + "- [P2] 둘째 — c.py:9-9\n  Suggestion: 둘째 본문.\n"
        f = findings.parse_codex_code(text, 3)
        self.assertEqual([x.id for x in f], ["r3-01", "r3-02"])
        self.assertEqual([x.severity for x in f], ["Critical", "Suggestion"])

    def test_code_parser_location_with_spaces(self):
        f = findings.parse_codex_code("- [P2] 제목 — 다른 말 — some dir/a b.py:3-4\n  본문.\n", 1)
        self.assertEqual([(x.body.splitlines()[0], x.location) for x in f], [("제목 — 다른 말", "some dir/a b.py:3")])

    def test_unparsed_code_items_lists_item_lines_the_parser_skips(self):
        text = "- [P1] 첫째 — a.py:1\n  본문.\n- [P2] 위치 없는 항목\n  본문.\n"
        self.assertEqual(len(findings.parse_codex_code(text, 1)), 1)
        self.assertEqual(findings.unparsed_code_items(text), ["- [P2] 위치 없는 항목"])

    def test_code_parser_no_items_is_empty(self):
        self.assertEqual(findings.parse_codex_code("문제를 찾지 못했습니다.", 1), [])


class PrFindingsTest(unittest.TestCase):
    def test_pr_findings_select_unresolved_first_comment_not_mine(self):
        f = pr_findings()
        self.assertEqual([x.thread_id for x in f], ["T2", "T4"])
        self.assertTrue(f[1].outdated)
        self.assertFalse(f[0].outdated)
        self.assertIn("답글(", f[1].body)
        self.assertIn("답글(@sch0718): 다음 PR에서 하겠습니다.", f[1].body)
        self.assertNotIn("초안 답글", f[1].body)
        self.assertTrue(all(x.source == "pr-comment" for x in f))
        self.assertEqual([x.id for x in f], ["r1-01", "r1-02"])

    def test_pr_severity_from_first_line_default_warning(self):
        f = pr_findings()
        self.assertEqual([x.severity for x in f], ["Warning", "Suggestion"])
        self.assertEqual(findings.pr_severity("그냥 의견입니다"), "Warning")
        self.assertEqual(findings.pr_severity("**Critical** 문제\nWarning 아님"), "Critical")

    def test_pr_severity_needs_a_leading_label(self):
        self.assertEqual(findings.pr_severity("🔴 critical: 데이터가 사라집니다"), "Critical")
        self.assertEqual(findings.pr_severity("This is not critical, just a nit"), "Warning")
        self.assertEqual(findings.pr_severity("Noncritical: rename"), "Warning")

    def test_pr_location_and_evidence(self):
        f = pr_findings()
        self.assertEqual(f[0].location, "src/b.py:42")
        self.assertEqual(f[1].location, "src/d.py:20")
        self.assertEqual(f[0].evidence, "b.py:42 의 except 가 로그 없이 pass 합니다.")
        self.assertEqual(f[1].evidence, "")

    def test_pr_pending_first_comment_skips_thread(self):
        data = json.loads((FIX / "pr-threads.json").read_text(encoding="utf-8"))
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        threads[1]["comments"]["nodes"][0]["pullRequestReview"]["state"] = "PENDING"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            old = os.environ.get("CASCADE_FAKE_GH_FILE")
            os.environ["CASCADE_FAKE_GH_FILE"] = str(path)
            try:
                f = findings.fetch_pr_findings("o", "r", 7, me="sch0718", gh=FAKE_GH)
            finally:
                if old is None:
                    del os.environ["CASCADE_FAKE_GH_FILE"]
                else:
                    os.environ["CASCADE_FAKE_GH_FILE"] = old
        self.assertEqual([x.thread_id for x in f], ["T4"])

    def test_pr_warns_when_threads_or_comments_have_more_pages(self):
        data = json.loads((FIX / "pr-threads.json").read_text(encoding="utf-8"))
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads["pageInfo"] = {"hasNextPage": True}
        threads["nodes"][1]["comments"]["pageInfo"] = {"hasNextPage": True}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            old = os.environ.get("CASCADE_FAKE_GH_FILE")
            os.environ["CASCADE_FAKE_GH_FILE"] = str(path)
            warnings = []
            try:
                f = findings.fetch_pr_findings("o", "r", 7, me="sch0718", gh=FAKE_GH, warnings=warnings)
            finally:
                if old is None:
                    del os.environ["CASCADE_FAKE_GH_FILE"]
                else:
                    os.environ["CASCADE_FAKE_GH_FILE"] = old
        self.assertEqual([x.thread_id for x in f], ["T2", "T4"])
        self.assertEqual(len(warnings), 2)
        self.assertIn("100", warnings[0])
        self.assertIn(threads["nodes"][1]["id"], warnings[1])
        self.assertEqual(findings.PR_QUERY.count("pageInfo { hasNextPage }"), 2)

    def test_pr_gh_timeout_raises(self):
        old = findings.GH_TIMEOUT
        findings.GH_TIMEOUT = 1
        try:
            with self.assertRaises(RuntimeError) as cm:
                findings.fetch_pr_findings("o", "r", 7, me="x", gh=[sys.executable, "-c", "import time; time.sleep(30)"])
        finally:
            findings.GH_TIMEOUT = old
        self.assertIn("시간 초과", str(cm.exception))

    def test_pr_gh_failure_raises(self):
        with self.assertRaises(RuntimeError):
            findings.fetch_pr_findings("o", "r", 7, me="x", gh=[sys.executable, "-c", "import sys; sys.exit(1)"])


class FindingJsonTest(unittest.TestCase):
    def test_round_trip(self):
        f = findings.Finding(id="r1-01", source="codex-doc", severity="Warning", location="a.md:1",
                             body="본문", evidence="근거", thread_id=None, outdated=False)
        self.assertEqual(findings.Finding.from_dict(json.loads(json.dumps(f.to_dict()))), f)
        self.assertEqual(set(f.to_dict()), {"id", "source", "severity", "location", "body", "evidence", "thread_id", "outdated", "carried_from", "carried_after"})

    def test_write_and_read_findings(self):
        items = findings.parse_codex_doc(FIX_DOC, 1) + pr_findings()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "findings.json"
            findings.write_findings(path, items)
            self.assertEqual(findings.read_findings(path), items)
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), list)


if __name__ == "__main__":
    unittest.main()
