import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_SCRIPT = ROOT / "docs" / "codex-review-changes" / "run-review.sh"


class BundledReviewScriptsTest(unittest.TestCase):
    def test_every_codex_call_has_stdin_redirected(self):
        text = CODEX_SCRIPT.read_text(encoding="utf-8")
        calls = re.findall(r'"\$CODEX" (?:exec|review)(?:[^\n]*\\\n)*[^\n]*', text)
        self.assertEqual(len(calls), 3)
        for call in calls:
            with self.subTest(call=call.splitlines()[0]):
                self.assertRegex(call, r"(?<!\S)<\s*(/dev/null|\"\$prompt_file\")")


if __name__ == "__main__":
    unittest.main()
