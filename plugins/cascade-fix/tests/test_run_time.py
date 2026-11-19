import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import run_time  # noqa: E402


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class RunTimeTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.projects = self.tmp / "projects"
        wf = self.projects / "p" / "s" / "subagents" / "workflows" / "wf_1"
        write_jsonl(wf / "journal.jsonl", [{"type": "launched"},
                                          {"type": "started", "agentId": "a1", "label": "f1 b"},
                                          {"type": "started", "agentId": "a2", "label": "f1 adjudicator"}])
        write_jsonl(wf / "agent-a1.jsonl", [{"timestamp": "2026-10-26T01:00:00Z"}, {"type": "x"},
                                            {"timestamp": "2026-10-26T01:01:30Z"}])
        write_jsonl(wf / "agent-a2.jsonl", [{"timestamp": "2026-10-26T01:02:00Z"}, {"timestamp": "2026-10-26T01:05:00Z"}])

    def item(self, name, rows, events):
        d = self.tmp / "runs" / name
        write_json(d / "ledger.json", {"rounds": [{"no": 1, "events": events}]})
        write_json(d / "run" / "round-1" / "claude-usage.json", {"workflows": {"wf_1": rows}})
        return d

    def test_agent_seconds(self):
        wf = self.projects / "p" / "s" / "subagents" / "workflows" / "wf_1"
        self.assertEqual(run_time.agent_seconds(wf, "f1 b"), 90.0)
        self.assertIsNone(run_time.agent_seconds(wf, "없음"))

    def test_cascade_item_sums_claude_agents_and_cli_workers(self):
        d = self.item("i1", [{"label": "f1 b"}, {"label": "f1 adjudicator"}], [
            {"step": "init", "at": "2026-10-26T10:00:00+09:00"},
            {"step": "jobs", "at": "2026-10-26T10:00:10+09:00"},
            {"step": "workers-stage1", "at": "2026-10-26T10:01:20+09:00"},
            {"step": "judge", "at": "2026-10-26T10:09:00+09:00"},
        ])
        got = run_time.cascade_item(d, self.projects)
        self.assertEqual((got["claude_s"], got["cli_s"], got["missing"]), (270.0, 70.0, []))

    def test_cli_seconds_measures_each_stage_from_its_own_input(self):
        ledger = {"rounds": [{"events": [
            {"step": "jobs", "at": "2026-10-26T10:00:00+09:00"},
            {"step": "workers-stage1", "at": "2026-10-26T10:01:00+09:00"},
            {"step": "judge", "at": "2026-10-26T10:05:00+09:00"},
            {"step": "workers-adj", "at": "2026-10-26T10:06:00+09:00"},
            {"step": "workers-opinion", "at": "2026-10-26T10:09:00+09:00"},
        ]}]}
        self.assertEqual(run_time.cli_seconds(ledger), 120.0)

    def test_counterfactual_rows_drop_adjudicator_time(self):
        d = self.item("i2", [{"label": "f1 b"}], [])
        self.assertEqual(run_time.cascade_item(d, self.projects)["claude_s"], 90.0)

    def test_missing_workflow_is_reported(self):
        d = self.tmp / "runs" / "i3"
        write_json(d / "ledger.json", {"rounds": []})
        write_json(d / "run" / "round-1" / "claude-usage.json", {"workflows": {"wf_none": [{"label": "x"}]}})
        self.assertEqual(run_time.cascade_item(d, self.projects)["missing"], ["wf_none x"])

    def test_cascade_runs_mean_per_item(self):
        self.item("i1", [{"label": "f1 b"}], [{"step": "jobs", "at": "2026-10-26T10:00:00+09:00"},
                                              {"step": "workers-stage1", "at": "2026-10-26T10:00:30+09:00"}])
        self.item("i2", [{"label": "f1 adjudicator"}], [])
        got = run_time.cascade_runs(self.tmp / "runs", self.projects)
        self.assertEqual(got["items"], 2)
        self.assertAlmostEqual(got["minutes_per_item"], (90 + 30 + 180) / 2 / 60)

    def test_solo_uses_session_duration(self):
        for name, ms in (("a", 60_000), ("b", 120_000)):
            write_json(self.tmp / "solo" / name / "result.json", {"cli_duration_ms": ms, "duration_s": 999})
        got = run_time.solo_runs(self.tmp / "solo")
        self.assertEqual(got["items"], 2)
        self.assertAlmostEqual(got["minutes_per_item"], 1.5)

    def test_main_writes_json(self):
        self.item("i1", [{"label": "f1 b"}], [])
        out = self.tmp / "t.json"
        code = run_time.main(["--cascade", str(self.tmp / "runs"), "--projects-root", str(self.projects), "--json", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text())["items"], 1)


if __name__ == "__main__":
    unittest.main()
