import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from cascade import cli, cli_steps, profile, profiles
from cascade.rounds import Ledger, RoundRecord

SONNET = "claude-sonnet-5-5"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def usage_row(job, cell, *, cost=0.25, **extra):
    return {"job_id": job, "cell": cell, "model": SONNET,
            "input": 10, "output": 4, "cache_read": 25, "cache_write": 1_000,
            "cost": cost, **extra}


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "repos.toml"
        self.store = self.root / "profile"
        self.run_dir = self.root / "run"
        self.projects = self.root / "projects"
        self.env = self.enterContext(patch.dict(os.environ, {
            "CASCADE_PROFILE_DIR": str(self.store),
            "CASCADE_PROJECTS_ROOT": str(self.projects),
        }, clear=True))
        self.enterContext(patch.multiple(cli_steps, USER_REPOS_TOML=self.config,
                                        LEGACY_REPOS_TOML=self.root / "legacy.toml"))

    def command(self, *argv, code=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = cli.main(list(argv))
        self.assertEqual(result, code, out.getvalue() + err.getvalue())
        return out.getvalue(), err.getvalue()

    def report(self):
        return self.command("report", "--run-dir", str(self.run_dir))

    def ledger(self, **extra):
        rnd = RoundRecord(
            no=1, findings=[{"id": "r1-01", "body": "보관 제외 본문", "location": "/tmp/sample/secret.py:1"}],
            decisions=[], adopted=[], recheck_opened=False, started_at="2026-11-20T12:00:00+09:00",
            events=[{"step": "init", "at": "2026-11-20T12:00:00+09:00"},
                    {"step": "apply", "at": "2026-11-20T12:01:05+09:00"}],
        )
        record = {"name": "local", "source": "--profile", "roles": profiles.builtin("local")["roles"]}
        ledger = Ledger(run_id="run-1", root=str(self.root / "example"), source="local",
                        mode="code", snapshot={}, dry_run=False, rounds=[rnd], profile={**record, **extra})
        ledger.save(self.run_dir / "ledger.json")
        return ledger

    def test_default_off_and_environment_off_keep_report_output(self):
        self.ledger()
        before = self.report()
        self.config.write_text("profiling = true\n", encoding="utf-8")
        self.env["CASCADE_PROFILING"] = "0"
        self.assertEqual(self.report(), before)
        self.assertFalse(self.store.exists())

    def test_enabled_report_writes_usage_without_findings_and_keeps_run_identity(self):
        first = {"requested": {"runner": "codex", "model": "external-model"},
                 "actual": {"runner": "claude", "model": SONNET},
                 "attempts": [{"runner": "codex", "reason": "이용 한도"}],
                 "usage": {"workflows": {"wf_review": [usage_row("review:reviewer", "code.review")]}}}
        ledger = self.ledger(first_review=first)
        self.config.write_text("profiling = false\n", encoding="utf-8")
        self.env["CASCADE_PROFILING"] = "1"
        self.report()
        path = self.store / "run-1.json"
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertNotIn("보관 제외 본문", raw)
        self.assertNotIn("/tmp/sample/secret.py", raw)
        self.assertNotIn(ledger.root, raw)
        self.assertEqual(data["repo"], "example")
        manifest = json.loads((cli_steps.SKILL_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(data["cascade_fix_version"], manifest["version"])
        self.assertEqual(data["date"], "2026-11-20")
        self.assertEqual(data["workflows"], ["wf_review"])
        self.assertEqual(data["rounds"], [{"no": 1, "elapsed_seconds": 65}])
        review = data["cells"]["code.review"]
        self.assertEqual(review["calls"], 1)
        self.assertEqual(review["fallbacks"], {"이용 한도": 1})
        self.assertEqual(review["actual"][0]["cost"], 0.25)

        ledger.root = str(self.root / "other")
        first["attempts"][-1]["reason"] = "/tmp/sample/failure.log에서 오류"
        ledger.save(self.run_dir / "ledger.json")
        self.report()
        self.report()
        other, = self.store.glob("run-1-*.json")
        saved = other.read_text(encoding="utf-8")
        self.assertNotIn("/tmp/sample/failure.log", saved)
        self.assertEqual(json.loads(saved)["cells"]["code.review"]["fallbacks"], {"기타": 1})
        self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_write_failure_warns_and_report_succeeds(self):
        self.ledger()
        self.env["CASCADE_PROFILING"] = "1"
        self.store.write_text("", encoding="utf-8")
        out, err = self.report()
        self.assertIn("보고서", out)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("경고", err)
        self.assertTrue((self.run_dir / "report.md").is_file())

    def test_switches_preserve_config_and_status_shows_effective_value(self):
        original = '# 설정\n[repos."/tmp/example"]\nprofile = "local"\n'
        legacy = cli_steps.LEGACY_REPOS_TOML
        legacy.write_text("profiling = true\n\n" + original, encoding="utf-8")
        self.assertIn("켜짐", self.command("profile", "status")[0])
        self.command("profile", "off")
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf-8")),
                         {**tomllib.loads(original), "profiling": False})
        self.config.write_text(original, encoding="utf-8")
        self.assertIn("꺼짐", self.command("profile", "status")[0])
        self.command("profile", "on")
        self.assertIn("켜짐", self.command("profile", "status")[0])
        self.assertEqual(self.config.read_text(encoding="utf-8"), "profiling = true\n\n" + original)
        self.config.write_text(self.config.read_text(encoding="utf-8").replace(
            "profiling = true", "profiling = true  # 측정"), encoding="utf-8")
        self.env["CASCADE_PROFILING"] = "1"
        self.command("profile", "off")
        self.assertIn('profiling = false  # 측정', self.config.read_text(encoding="utf-8"))
        self.assertIn("켜짐", self.command("profile", "status")[0])
        del self.env["CASCADE_PROFILING"]
        self.assertIn("꺼짐", self.command("profile", "status")[0])

        override = self.root / "override.toml"
        override.write_text("profiling = true\n", encoding="utf-8")
        self.env["CASCADE_REPOS_TOML"] = str(override)
        self.assertIn("켜짐", self.command("profile", "status")[0])
        self.command("profile", "off", code=5)
        self.assertEqual(override.read_text(encoding="utf-8"), "profiling = true\n")

    def test_aggregation_uses_actual_runners_and_preserves_unknowns(self):
        ledger = self.ledger()
        ledger.opinions = {"r1-01": "첫 의견", "r1-02": "둘째 의견"}
        ledger.rounds[0].events += [
            {"step": "opinion:r1-01", "runner": "codex", "model": "external-model"},
            {"step": "opinion:r1-02", "runner": "claude", "model": SONNET,
             "fallback": {"reason": "이용 한도"}},
            {"step": "workers-stage1"},
        ]
        ledger.save(self.run_dir / "ledger.json")
        rd = self.run_dir / "round-1"
        write_json(rd / "stage1-cli-args.json", {"jobs": [
            {"id": "r1-01:a", "runner": "codex", "model": "external-model"},
            {"id": "r1-01:b", "runner": "claude", "model": SONNET},
        ]})
        write_json(rd / "stage1-cli.json", {
            "outcomes": [
                {"id": "r1-01:a", "runner": "cursor", "status": "ok", "usage": {"unrecorded": 1}},
                {"id": "r1-01:b", "runner": "claude", "status": "ok", "usage": {"input_tokens": 10}},
            ],
            "replaced": [{"id": "r1-01:a", "runner": "codex", "status": "limit",
                          "usage": {"input_tokens": 5, "output_tokens": 2}}],
            "fallback_runs": {"r1-01:a": {"runner": "cursor", "model": "cursor-model"}},
        })
        write_json(rd / "claude-usage.json", {"workflows": {
            "wf_opinion": [usage_row("r1-02:opinion", "code.opinion")],
            "cli-stage1-1": [usage_row("r1-01:b", "code.b", via="cli")],
        }})
        self.env["CASCADE_PROFILING"] = "1"
        self.report()
        run = json.loads((self.store / "run-1.json").read_text(encoding="utf-8"))
        self.assertEqual(run["workflows"], ["wf_opinion"])
        workers = run["cells"]["code.a"]
        self.assertEqual(workers["calls"], 1)
        self.assertEqual(workers["fallbacks"], {"한도": 1})
        actual = {item["runner"]: item for item in workers["actual"]}
        self.assertIsNone(actual["cursor"]["tokens"])
        self.assertEqual(actual["codex"]["tokens"], {"input": 5, "output": 2, "cache_read": None, "cache_write": None})
        self.assertEqual(run["cells"]["code.b"]["actual"][0]["calls"], 1)
        self.assertEqual(run["cells"]["code.b"]["actual"][0]["tokens"]["input"], 10)
        self.assertEqual(run["cells"]["code.opinion"]["calls"], 2)

        def summary(*args):
            return json.loads(self.command("profile", "report", "--json", *args)[0])

        data = summary("--since", "2026-11-20")
        groups = {(r["role"], r["model"]): r for r in data["groups"]}
        self.assertEqual(groups["code.opinion", SONNET]["cost"], 0.25)
        self.assertEqual(groups["code.opinion", "external-model"]["unknown_cost_calls"], 1)
        self.assertEqual(data["total"]["cost"], 0.5)
        self.assertEqual(data["total"]["fallbacks"], 2)
        self.assertGreater(data["model_mismatch_cells"], 0)
        self.assertAlmostEqual(data["cache_write_ratio"], 0.01)
        self.assertIn("단가 모름", self.command("profile", "report")[0])
        self.assertEqual({row["runner"]: row["calls"] for row in summary("--by", "runner")["groups"]},
                         {"claude": 2, "codex": 2, "cursor": 1})
        for by, keys in (("role", {"code.a", "code.b", "code.opinion"}),
                         ("model", {SONNET, "external-model", "cursor-model"}),
                         ("repo", {"example"}), ("week", {"2026-W47"})):
            with self.subTest(by=by):
                self.assertEqual({row[by] for row in summary("--by", by)["groups"]}, keys)
        self.assertEqual(summary("--since", "2026-11-21")["groups"], [])

    def test_cli_tokens_of_openai_models_are_priced(self):
        tokens = {"input": 1_000_000, "output": 0, "cache_read": 500_000, "cache_write": None}
        self.assertAlmostEqual(profile._openai_cost("gpt-6.2-sol", tokens), 1.0 + 0.05)
        self.assertIsNone(profile._openai_cost("gpt-6.2-sol", None))
        self.assertIsNone(profile._openai_cost(None, tokens))
        self.assertIsNone(profile._openai_cost("composer-2.5", tokens))

    def test_workflow_fallback_without_tokens_uses_its_own_model(self):
        ledger = self.ledger()
        ledger.rounds[0].events.append({"step": "workers-stage1"})
        ledger.save(self.run_dir / "ledger.json")
        rd = self.run_dir / "round-1"
        write_json(rd / "stage1-cli-args.json", {"jobs": [
            {"id": "r1-01:a", "runner": "codex", "model": "external-model"},
        ]})
        write_json(rd / "stage1-cli.json", {"outcomes": [
            {"id": "r1-01:a", "runner": "codex", "status": "limit"},
        ]})
        write_json(rd / "stage1-fallback-args.json", {"jobs": [{"id": "r1-01:a", "model": SONNET}]})
        write_json(rd / "stage1-fallback.json", {"results": [{"id": "r1-01:a", "output": {"model_id": SONNET}}]})
        self.config.write_text("profiling = true\n", encoding="utf-8")
        self.report()
        run = json.loads((self.store / "run-1.json").read_text(encoding="utf-8"))
        cell = run["cells"]["code.a"]
        self.assertEqual(cell["calls"], 1)
        self.assertEqual({(item["runner"], item["model"]) for item in cell["actual"]},
                         {("codex", "external-model"), ("claude", SONNET)})
        self.assertTrue(all(item["tokens"] is None and item["cost"] is None for item in cell["actual"]))

    def journal(self, workflow, labels, day):
        wd = self.projects / "project" / "session" / "subagents" / "workflows" / workflow
        wd.mkdir(parents=True)
        journal = wd / "journal.jsonl"
        journal.write_text("\n".join(json.dumps({"type": "started", "agentId": str(i), "label": label})
                                     for i, label in enumerate(labels)), encoding="utf-8")
        stamp = datetime.fromisoformat(day).timestamp()
        os.utime(journal, (stamp, stamp))
        for i, _ in enumerate(labels):
            write_json(wd / f"agent-{i}.meta.json", {"model": SONNET})
            write_json(wd / f"agent-{i}.jsonl", {"message": {"id": "m", "usage": {
                "input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }}})

    def test_import_matches_labels_and_excludes_logged_workflows(self):
        self.journal("wf_claimed", ["r1-01 a"], "2026-11-20")
        self.journal("wf_local", ["r1-01 a", "r1-01 writer", "r1-02 adjudicator", "doc review", "다른 작업"], "2026-11-20")
        self.journal("wf_old", ["code review"], "2026-11-19")
        self.journal("wf_bad", ["r1-01 a"], "2026-11-20")
        next(self.projects.rglob("wf_bad")).joinpath("journal.jsonl").write_text("{", encoding="utf-8")
        self.command("profile", "import-sessions", "--since", "2026-11-20")
        self.ledger()
        write_json(self.run_dir / "round-1" / "claude-usage.json", {"workflows": {
            "wf_claimed": [usage_row("r1-01:a", "code.a")],
        }})
        self.env["CASCADE_PROFILING"] = "1"
        self.report()
        summary = json.loads(self.command("profile", "report", "--json")[0])
        self.assertEqual(summary["total"]["calls"], 5)

        out, _ = self.command("profile", "import-sessions", "--since", "2026-11-20")
        self.assertIn("실패 1", out)
        rows = json.loads((self.store / "imported-claude.json").read_text(encoding="utf-8"))
        self.assertEqual({r["workflow_id"] for r in rows}, {"wf_local"})
        self.assertEqual({(r["mode"], r["role"]) for r in rows},
                         {("doc", "writer"), ("doc", "judge_a"), ("unknown", "adjudicator"), ("doc", "review")})
        summary = json.loads(self.command("profile", "report", "--by", "repo", "--json")[0])
        self.assertEqual({r["repo"]: r["calls"] for r in summary["groups"]}, {"(가져옴)": 4, "example": 1})


if __name__ == "__main__":
    unittest.main()
