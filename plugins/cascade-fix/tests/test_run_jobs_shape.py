import json
import re
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from cascade import jobs  # noqa: E402

SCHEMAS_BLOCK = re.compile(r"const SCHEMAS = (\{.*?\n\})\n// SCHEMAS 끝", re.S)
STRING_OR_COMMENT = re.compile(r"""('(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)|//[^\n]*|/\*.*?\*/""", re.S)


def strip_comments(src):
    return STRING_OR_COMMENT.sub(lambda m: m.group(1) or "", src)


class RunJobsShapeTest(unittest.TestCase):
    def setUp(self):
        self.src = (SKILL / "scripts/run-jobs.js").read_text()

    def test_script_has_meta_and_uses_args_jobs(self):
        code = strip_comments(self.src)
        self.assertIn("export const meta", code)
        self.assertIn("const jobs = args?.jobs ?? []", code)
        self.assertNotIn("Date.now", code)

    def test_script_has_no_nondeterministic_calls(self):
        code = strip_comments(self.src)
        self.assertNotIn("Math.random", code)
        self.assertNotIn("new Date", code)

    def test_strip_comments_drops_forbidden_words_only_in_comments(self):
        src = "// Date.now는 쓰지 않는다\n/* new Date\n   Math.random */\nconst x = 1 // new Date 금지\n"
        code = strip_comments(src)
        for word in ("Date.now", "new Date", "Math.random"):
            self.assertNotIn(word, code)
        self.assertIn("const x = 1", code)

    def test_strip_comments_keeps_forbidden_words_in_code(self):
        src = "// 주석\nconst t = Date.now()\nconst u = 'http://x' + new Date() /* 끝 */ + Math.random()\n"
        code = strip_comments(src)
        for word in ("Date.now", "new Date", "Math.random", "'http://x'"):
            self.assertIn(word, code)

    def test_script_schemas_match_python_by_kind(self):
        m = SCHEMAS_BLOCK.search(self.src)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1)), jobs.SCHEMAS)
        self.assertEqual(set(jobs.SCHEMAS), {"worker", "checker", "adjudicator", "review"})

    def test_script_reads_prompt_from_file_path(self):
        self.assertIn("SCHEMAS[j.kind]", self.src)
        self.assertIn("다음 파일을 Read로 읽고 그 지시를 따른다: ${j.promptPath}", self.src)
        self.assertNotIn("j.prompt,", self.src)
        self.assertNotIn("j.schema", self.src)


if __name__ == "__main__":
    unittest.main()
