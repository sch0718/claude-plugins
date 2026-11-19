import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_cli_e2e import CliTestBase  # noqa: E402

FINDING = {"id": "x-01", "source": "codex-code", "severity": "Critical", "location": "calc.py:2",
           "body": "add가 뺀다", "evidence": "add(2, 3)이 -1"}


class FindingsJsonTest(CliTestBase):
    def write(self, data):
        path = self.tmp / "findings.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_init_loads_findings_from_json_without_input(self):
        p = self.cli("init", "--source", "codex-code", "--findings-json", str(self.write([FINDING])), "--dry-run")
        self.assertIn("지적 1건: Critical 1", p.stdout)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        saved = json.loads((run_dir / "round-1" / "findings.json").read_text(encoding="utf-8"))
        self.assertEqual([f["id"] for f in saved], ["x-01"])
        self.assertEqual(saved[0]["body"], "add가 뺀다")

    def test_init_requires_input_or_findings_json(self):
        p = self.cli("init", "--source", "codex-code", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--input", p.stdout + p.stderr)

    def test_init_rejects_malformed_findings_json(self):
        bad = {k: v for k, v in FINDING.items() if k != "body"}
        p = self.cli("init", "--source", "codex-code", "--findings-json", str(self.write([bad])), ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertIn("body", p.stdout)

    def test_init_rejects_bad_or_duplicate_ids(self):
        for findings, word in (([{**FINDING, "id": "../x"}], "id"),
                               ([FINDING, {**FINDING, "location": "calc.py:5"}], "겹친다")):
            p = self.cli("init", "--source", "codex-code", "--findings-json", str(self.write(findings)), ok=False)
            self.assertEqual(p.returncode, 5)
            self.assertIn(word, p.stdout)


if __name__ == "__main__":
    unittest.main()
