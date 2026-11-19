import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import recommend  # noqa: E402
from cascade.config import ConfigError  # noqa: E402


def _row(recommend_name="claude-only", status="provisional", alternatives=()):
    return {"recommend": recommend_name, "status": status, "alternatives": list(alternatives)}


def _toml_row(env, row):
    alternatives = ", ".join(
        f'{{ profile = "{a["profile"]}", status = "{a["status"]}", note = "{a["note"]}" }}'
        for a in row["alternatives"]
    )
    return (
        f'[env."{env}"]\nrecommend = "{row["recommend"]}"\nstatus = "{row["status"]}"\n'
        f"alternatives = [{alternatives}]\n"
    )


class RecommendTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def table_with(self, **rows):
        base = {env: _row() for env in recommend.ENV_KEYS}
        base["none"]["status"] = "measured"
        for env, row in rows.items():
            base[env] = {**_row(), **row}
        text = 'version = 1\nupdated = "2026-10-23"\nsource = ""\n\n' + "\n".join(
            _toml_row(env, row) for env, row in base.items()
        )
        path = self.tmp / "recommendations.toml"
        path.write_text(text, encoding="utf-8")
        return recommend.load_table(path)

    def test_shipped_table_is_valid_and_pre_measurement(self):
        t = recommend.load_table()
        self.assertEqual(t["version"], 1)
        self.assertEqual(set(t["env"]), set(recommend.ENV_KEYS))
        for env in recommend.ENV_KEYS:
            self.assertEqual(t["env"][env]["recommend"], "claude-only")
        self.assertEqual(t["env"]["none"]["status"], "measured")
        self.assertEqual([a["profile"] for a in t["env"]["codex"]["alternatives"]], ["local"])
        self.assertEqual([a["profile"] for a in t["env"]["codex+cursor"]["alternatives"]], ["local"])
        self.assertEqual(t["env"]["cursor"]["alternatives"], [])

    def test_env_key(self):
        self.assertEqual(recommend.env_key(False, False), "none")
        self.assertEqual(recommend.env_key(True, False), "codex")
        self.assertEqual(recommend.env_key(True, True), "codex+cursor")
        self.assertEqual(recommend.env_key(False, True), "cursor")

    def test_env_runners(self):
        self.assertEqual(recommend.env_runners("none"), set())
        self.assertEqual(recommend.env_runners("codex"), {"codex"})
        self.assertEqual(recommend.env_runners("cursor"), {"cursor"})
        self.assertEqual(recommend.env_runners("codex+cursor"), {"codex", "cursor"})

    def test_fast_is_always_claude_only(self):
        t = self.table_with(codex={"recommend": "local", "status": "measured"})
        self.assertEqual(recommend.recommended(t, "codex", "fast"), "claude-only")
        self.assertEqual(recommend.recommended(t, "codex", "save-claude"), "local")
        self.assertEqual([o["profile"] for o in recommend.options(t, "codex", "fast")], ["claude-only", "local"])

    def test_options_put_recommendation_first_and_dedupe(self):
        t = self.table_with(codex={
            "recommend": "local", "status": "measured",
            "alternatives": [
                {"profile": "local", "status": "provisional", "note": "중복"},
                {"profile": "claude-only", "status": "measured", "note": "기준"},
            ],
        })
        opts = recommend.options(t, "codex", "save-claude")
        self.assertEqual([o["profile"] for o in opts], ["local", "claude-only"])
        self.assertEqual(opts[0], {"profile": "local", "status": "measured", "note": "", "recommended": True})
        self.assertEqual(opts[1]["note"], "기준")
        self.assertNotIn("recommended", opts[1])

    def test_options_of_shipped_table(self):
        t = recommend.load_table()
        opts = recommend.options(t, "codex", "save-claude")
        self.assertEqual([o["profile"] for o in opts], ["claude-only", "local"])
        self.assertTrue(opts[0]["recommended"])
        self.assertEqual(opts[1]["note"], "측정 전 잠정값")
        self.assertEqual([o["profile"] for o in recommend.options(t, "cursor", "save-claude")], ["claude-only"])

    def test_rejects_unknown_profile(self):
        with self.assertRaises(ConfigError):
            self.table_with(codex={"recommend": "my-custom"})
        with self.assertRaises(ConfigError):
            self.table_with(codex={"alternatives": [{"profile": "my-custom", "status": "provisional", "note": ""}]})

    def test_rejects_missing_env_key(self):
        path = self.tmp / "recommendations.toml"
        path.write_text(
            'version = 1\nupdated = "2026-10-23"\nsource = ""\n\n' + _toml_row("none", _row(status="measured")),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            recommend.load_table(path)

    def test_rejects_profile_needing_absent_runner(self):
        with self.assertRaises(ConfigError):
            self.table_with(cursor={"recommend": "local"})
        with self.assertRaises(ConfigError):
            self.table_with(cursor={"alternatives": [{"profile": "local", "status": "provisional", "note": ""}]})
        with self.assertRaises(ConfigError):
            self.table_with(none={"recommend": "local"})

    def test_rejects_unknown_status(self):
        with self.assertRaises(ConfigError):
            self.table_with(codex={"status": "guess"})
        with self.assertRaises(ConfigError):
            self.table_with(codex={"alternatives": [{"profile": "local", "status": "guess", "note": ""}]})

    def test_rejects_malformed_file(self):
        path = self.tmp / "recommendations.toml"
        path.write_text("version = [", encoding="utf-8")
        with self.assertRaises(ConfigError):
            recommend.load_table(path)
        path.write_text('version = "1"\n', encoding="utf-8")
        with self.assertRaises(ConfigError):
            recommend.load_table(path)


if __name__ == "__main__":
    unittest.main()
