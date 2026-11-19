import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

import compare  # noqa: E402


def score(correct, cost, false_fact=0, to_human=0, total=10, codex=0, predicted=None):
    return {"total": total, "correct": correct, "accuracy": correct / total, "false_fact": false_fact,
            "to_human": to_human, "claude_cost": cost, "completion": 1.0, "scored": total,
            "usage": {"codex": {"input": codex, "output": 1, "cache_read": 0}} if codex else {},
            "predicted": predicted or {}}


class CompareTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return name

    def spec(self):
        return {
            "baseline": "단일 Claude",
            "total_items": 10,
            "orchestration": {"per_item": 0.5, "basis": "실제 사용 9회",
                              "alternatives": {"진행 비용 0": 0.0, "지적 1건 실행": 1.0}},
            "configs": [
                {"name": "단일 Claude", "group": "solo",
                 "runs": [{"score": self.write("s1.json", score(5, 10.0, false_fact=1, predicted={"a": "사실"})), "minutes": 1.0},
                          {"score": self.write("s2.json", score(7, 14.0, predicted={"a": "아님"})), "minutes": 2.0}]},
                {"name": "local", "group": "external",
                 "runs": [{"score": self.write("l1.json", score(6, 2.0, to_human=3, codex=1000)),
                           "extra": {"from": self.write("l1-all.json", {**score(6, None), "cells": {
                               "code.opinion": {"cost": 1.0}, "code.a": {"cost": None}, "code.b": {"cost": 9.0}}}),
                               "cells": ["code.opinion", "code.a", "code.recheck"]}}]},
            ],
        }

    def rows(self):
        path = self.tmp / "spec.json"
        path.write_text(json.dumps(self.spec(), ensure_ascii=False), encoding="utf-8")
        return compare.summarize(compare.load_spec(path))

    def test_summary_values(self):
        solo, local = self.rows()
        self.assertEqual(solo["runs"], 2)
        self.assertAlmostEqual(solo["accuracy"]["mean"], 0.6)
        self.assertEqual((solo["accuracy"]["min"], solo["accuracy"]["max"]), (0.5, 0.7))
        self.assertAlmostEqual(solo["work_item"]["mean"], 1.2)
        self.assertEqual(solo["orchestration_item"], 0.0)
        self.assertAlmostEqual(solo["total_item"]["mean"], 1.2)
        self.assertAlmostEqual(solo["ratio"], 1.0)
        self.assertAlmostEqual(solo["flip_rate"], 1.0)
        self.assertAlmostEqual(solo["minutes"]["mean"], 1.5)
        self.assertIsNone(local["minutes"])
        self.assertAlmostEqual(local["work_item"]["mean"], 0.3)
        self.assertAlmostEqual(local["total_item"]["mean"], 0.8)
        self.assertAlmostEqual(local["ratio"], 0.8 / 1.2)
        self.assertEqual(local["codex_input"], 1000)
        self.assertIsNone(local["flip_rate"])

    def test_markdown_table(self):
        text = compare.markdown(self.rows(), self.spec())
        self.assertIn("| 단일 Claude |", text)
        self.assertIn("60.0% [50.0~70.0]", text)
        self.assertIn("$0.30 + $0.50 = $0.80", text)
        self.assertIn("67%", text)
        self.assertIn("실제 사용 9회", text)

    def test_markdown_shows_minutes(self):
        text = compare.markdown(self.rows(), self.spec())
        self.assertIn("1.5분 [1.0~2.0]", text)

    def test_minutes_from_runs_that_have_them(self):
        spec = self.spec()
        del spec["configs"][0]["runs"][1]["minutes"]
        path = self.tmp / "spec2.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        rows = compare.summarize(compare.load_spec(path))
        self.assertEqual((rows[0]["minutes"]["mean"], rows[0]["minutes_runs"]), (1.0, 1))
        self.assertIn("1.0분(1회)", compare.markdown(rows, spec))

    def test_sensitivity_table(self):
        text = compare.sensitivity(self.rows(), self.spec())
        self.assertIn("| 구성 | 진행 비용 0 | 평균 규모(기본) | 지적 1건 실행 |", text)
        self.assertIn("| local | $0.30 (25%) | $0.80 (67%) | $1.30 (108%) |", text)
        self.assertIn("| 단일 Claude | $1.20 (100%) | $1.20 (100%) | $1.20 (100%) |", text)

    def test_svg_is_valid_and_labelled(self):
        rows = self.rows()
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                text = compare.svg(rows, theme)
                root = ET.fromstring(text)
                self.assertTrue(root.tag.endswith("svg"))
                self.assertIn("<title", text)
                self.assertIn("<desc", text)
                for name in ("단일 Claude", "local"):
                    self.assertIn(name, text)
                self.assertIn("정확도", text)
                self.assertIn("$", text)
                self.assertNotIn("nan", text.lower())
        self.assertNotEqual(compare.svg(rows, "light"), compare.svg(rows, "dark"))

    def test_svg_shapes_differ_per_config(self):
        text = compare.svg(self.rows(), "light")
        self.assertIn('data-shape="circle"', text)
        self.assertIn('data-shape="square"', text)

    def test_svg_draws_work_only_marker_for_cascade(self):
        text = compare.svg(self.rows(), "light")
        self.assertEqual(text.count('data-kind="work-only"'), 1)
        self.assertIn("진행 비용 제외", text)

    def test_main_writes_outputs(self):
        path = self.tmp / "spec.json"
        path.write_text(json.dumps(self.spec(), ensure_ascii=False), encoding="utf-8")
        md, light, dark = self.tmp / "t.md", self.tmp / "l.svg", self.tmp / "d.svg"
        code = compare.main(["--spec", str(path), "--md", str(md), "--svg-light", str(light), "--svg-dark", str(dark)])
        self.assertEqual(code, 0)
        self.assertTrue(md.read_text(encoding="utf-8").startswith("|"))
        ET.fromstring(dark.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
