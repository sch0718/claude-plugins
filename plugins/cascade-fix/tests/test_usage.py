import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import usage  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflow-run"


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class UsageTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def make_run(self, usage_row, model="claude-sonnet-5-5"):
        run = self.tmp / "run"
        run.mkdir()
        write_jsonl(run / "journal.jsonl", [{"type": "started", "agentId": "a1", "label": "x"}])
        (run / "agent-a1.meta.json").write_text(json.dumps({"model": model}), encoding="utf-8")
        write_jsonl(run / "agent-a1.jsonl", [{"message": {"id": "m1", "usage": usage_row}}])
        return run

    def test_reads_usage_per_label(self):
        got = {u.label: u for u in usage.read_workflow(FIXTURE)}
        a = got["r1-01 a"]
        self.assertEqual((a.label, a.model, a.input, a.output, a.cache_read, a.cache_write),
                         ("r1-01 a", "claude-sonnet-5-5", 4, 80, 2000, 150))
        self.assertEqual(got["r1-01 b"].model, "claude-haiku-5-5")

    def test_duplicate_message_counted_once(self):
        self.assertEqual({u.label: u.output for u in usage.read_workflow(FIXTURE)}["r1-01 a"], 80)

    def test_cost_sums_per_message(self):
        a = {u.label: u for u in usage.read_workflow(FIXTURE)}["r1-01 a"]
        want = (usage.cost("claude-sonnet-5-5", 2, 30, 1000, 100)
                + usage.cost("claude-sonnet-5-5", 2, 50, 1000, 50))
        self.assertAlmostEqual(a.cost, want)

    def test_cost_is_none_when_model_unknown(self):
        run = self.make_run({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}, model="gpt-6.1-sol")
        self.assertIsNone(usage.read_workflow(run)[0].cost)

    def test_missing_files_raise_format_error(self):
        with self.assertRaises(usage.UsageFormatError):
            usage.read_workflow(self.tmp)

    def test_missing_usage_key_raises_format_error(self):
        run = self.make_run({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0})
        with self.assertRaises(usage.UsageFormatError) as cm:
            usage.read_workflow(run)
        self.assertIn("cache_creation_input_tokens", str(cm.exception))

    def test_missing_agent_file_raises_format_error(self):
        run = self.make_run({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0})
        (run / "agent-a1.jsonl").unlink()
        with self.assertRaises(usage.UsageFormatError):
            usage.read_workflow(run)

    def test_find_by_run_id(self):
        root = self.tmp / "projects"
        d = root / "p" / "s" / "subagents" / "workflows" / "wf_x"
        d.mkdir(parents=True)
        self.assertEqual(usage.find_workflow_dir("wf_x", root), d)
        with self.assertRaises(usage.UsageFormatError):
            usage.find_workflow_dir("wf_none", root)

    def test_find_by_run_id_ambiguous_raises(self):
        root = self.tmp / "projects"
        for s in ("s1", "s2"):
            (root / "p" / s / "subagents" / "workflows" / "wf_x").mkdir(parents=True)
        with self.assertRaises(usage.UsageFormatError):
            usage.find_workflow_dir("wf_x", root)

    def test_find_by_directory(self):
        self.assertEqual(usage.find_workflow_dir(str(FIXTURE), self.tmp / "none"), FIXTURE)

    def test_cost(self):
        self.assertAlmostEqual(usage.cost("claude-opus-5-5", 1_000_000, 1_000_000, 0, 0), 24.0)
        self.assertIsNone(usage.cost("gpt-6.1-sol", 1, 1, 0, 0))

    def test_cost_cache_prices(self):
        self.assertAlmostEqual(usage.cost("claude-opus-5-5", 0, 0, 1_000_000, 1_000_000), 0.20 + 5.0)
        self.assertAlmostEqual(usage.cost("claude-fable-5-1", 0, 0, 1_000_000, 0), 0.25)

    def test_cost_fable_5_2_and_sonnet_cache_read(self):
        self.assertAlmostEqual(usage.cost("claude-fable-5-2", 1_000_000, 1_000_000, 1_000_000, 1_000_000),
                               8.0 + 40.0 + 0.20 + 10.0)
        self.assertAlmostEqual(usage.cost("claude-sonnet-5-5", 0, 0, 1_000_000, 0), 0.10)

    def test_openai_cost_subtracts_cached_input(self):
        self.assertAlmostEqual(usage.openai_cost("gpt-6.2-sol", 1_000_000, 1_000_000, 900_000), 0.2 + 0.09 + 10.0)
        self.assertAlmostEqual(usage.openai_cost("gpt-6.1-sol", 1_000_000, 0, 0), 2.0)
        self.assertIsNone(usage.openai_cost("composer-2.5", 1, 1, 0))

    def test_haiku_tier_is_per_request(self):
        two = 2 * usage.cost("claude-haiku-5-5", 60_000, 0, 0, 0)
        one = usage.cost("claude-haiku-5-5", 120_000, 0, 0, 0)
        self.assertAlmostEqual(two, 120_000 * 0.10 / 1e6)
        self.assertAlmostEqual(one, 120_000 * 0.50 / 1e6)

    def test_haiku_tier_counts_cache_in_prompt_size(self):
        low = usage.cost("claude-haiku-5-5", 1, 0, 99_000, 0)
        high = usage.cost("claude-haiku-5-5", 1, 0, 100_000, 0)
        self.assertAlmostEqual(low, 1 * 0.10 / 1e6 + 99_000 * 0.01 / 1e6)
        self.assertAlmostEqual(high, 1 * 0.50 / 1e6 + 100_000 * 0.05 / 1e6)

    def test_one_hour_cache_write_costs_double_the_input_price(self):
        self.assertAlmostEqual(usage.cost("claude-sonnet-5-5", 0, 0, 0, 0, cache_write_1h=1_000_000), 4.0)
        self.assertAlmostEqual(usage.cost("claude-sonnet-5-5", 0, 0, 0, 1_000_000), 2.5)

    def test_cache_creation_breakdown_prices_each_ttl(self):
        row = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 300_000,
               "cache_creation": {"ephemeral_5m_input_tokens": 100_000, "ephemeral_1h_input_tokens": 200_000}}
        got = usage.read_workflow(self.make_run(row))[0]
        self.assertEqual(got.cache_write, 300_000)
        self.assertAlmostEqual(got.cost, 100_000 * 2.5 / 1e6 + 200_000 * 4.0 / 1e6)

    def test_cache_creation_without_breakdown_is_all_five_minute(self):
        row = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 300_000}
        self.assertAlmostEqual(usage.read_workflow(self.make_run(row))[0].cost, 300_000 * 2.5 / 1e6)

    def session_row(self, message_id, model, ts, output, cache_write_1h=0):
        return {"type": "assistant", "timestamp": ts,
                "message": {"id": message_id, "model": model,
                            "usage": {"input_tokens": 1, "output_tokens": output, "cache_read_input_tokens": 100,
                                      "cache_creation_input_tokens": 10 + cache_write_1h,
                                      "cache_creation": {"ephemeral_5m_input_tokens": 10,
                                                         "ephemeral_1h_input_tokens": cache_write_1h}}}}

    def make_session(self):
        main = self.tmp / "s1.jsonl"
        write_jsonl(main, [
            {"type": "user", "timestamp": "2026-10-26T01:00:00.000Z", "message": {"role": "user", "content": "x"}},
            self.session_row("m1", "claude-opus-5-5", "2026-10-26T01:00:01.000Z", 5),
            self.session_row("m1", "claude-opus-5-5", "2026-10-26T01:00:02.000Z", 7),
            self.session_row("m2", "claude-haiku-5-5", "2026-10-26T01:10:00.000Z", 3),
            self.session_row("m3", "claude-opus-5-5", "2026-10-26T02:00:00.000Z", 11, cache_write_1h=1000),
        ])
        sub = self.tmp / "s1" / "subagents"
        (sub / "workflows" / "wf_x").mkdir(parents=True)
        write_jsonl(sub / "agent-a1.jsonl", [self.session_row("s1", "claude-sonnet-5-5", "2026-10-26T01:05:00.000Z", 13)])
        write_jsonl(sub / "workflows" / "wf_x" / "journal.jsonl", [{"type": "started", "agentId": "a2", "label": "y"}])
        write_jsonl(sub / "workflows" / "wf_x" / "agent-a2.jsonl",
                    [self.session_row("s2", "claude-haiku-5-5", "2026-10-26T01:06:00.000Z", 17)])
        return main

    def test_session_counts_each_message_once_per_model(self):
        got = {(u.label, u.model): u for u in usage.read_session(self.make_session())}
        self.assertEqual(set(got), {("main", "claude-opus-5-5"), ("main", "claude-haiku-5-5")})
        opus = got[("main", "claude-opus-5-5")]
        self.assertEqual((opus.input, opus.output, opus.cache_read, opus.cache_write), (2, 18, 200, 1020))
        self.assertEqual(opus.messages, 2)
        want = (usage.cost("claude-opus-5-5", 1, 7, 100, 10)
                + usage.cost("claude-opus-5-5", 1, 11, 100, 10, cache_write_1h=1000))
        self.assertAlmostEqual(opus.cost, want)

    def test_session_subagents_only_when_asked(self):
        main = self.make_session()
        self.assertNotIn("subagent", {u.label for u in usage.read_session(main)})
        got = {(u.label, u.model): u.output for u in usage.read_session(main, subagents=True)}
        self.assertEqual(got[("subagent", "claude-sonnet-5-5")], 13)
        self.assertEqual(got[("subagent", "claude-haiku-5-5")], 17)

    def test_session_window_keeps_messages_between_since_and_until(self):
        got = usage.read_session(self.make_session(), since="2026-10-26T01:00:02Z", until="2026-10-26T01:10:00Z")
        self.assertEqual({(u.model, u.output) for u in got}, {("claude-opus-5-5", 7), ("claude-haiku-5-5", 3)})

    def test_session_window_applies_to_subagents(self):
        got = usage.read_session(self.make_session(), subagents=True, until="2026-10-26T01:05:30Z")
        self.assertEqual({(u.label, u.model) for u in got}, {("main", "claude-opus-5-5"), ("subagent", "claude-sonnet-5-5")})

    def test_session_skips_zero_usage_messages(self):
        path = self.tmp / "s.jsonl"
        zero = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        write_jsonl(path, [{"timestamp": "2026-10-26T01:00:00Z", "message": {"id": "z", "model": "<synthetic>", "usage": zero}}])
        self.assertEqual(usage.read_session(path), [])

    def test_session_without_timestamp_in_window_raises(self):
        path = self.tmp / "s.jsonl"
        row = self.session_row("m1", "claude-opus-5-5", "x", 1)
        del row["timestamp"]
        write_jsonl(path, [row])
        with self.assertRaises(usage.UsageFormatError):
            usage.read_session(path, since="2026-10-26T00:00:00Z")

    def test_session_missing_file_raises(self):
        with self.assertRaises(usage.UsageFormatError):
            usage.read_session(self.tmp / "none.jsonl")

    def test_unconfirmed_price_key_gives_none(self):
        saved = usage.PRICES["claude-sonnet-5-5"].pop("cache_write")
        self.addCleanup(usage.PRICES["claude-sonnet-5-5"].__setitem__, "cache_write", saved)
        self.assertIsNone(usage.cost("claude-sonnet-5-5", 1, 1, 0, 1))
        self.assertIsNotNone(usage.cost("claude-sonnet-5-5", 1, 1, 0, 0))


if __name__ == "__main__":
    unittest.main()
