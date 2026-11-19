import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKET = ROOT / ".claude-plugin" / "marketplace.json"


class MarketplaceTest(unittest.TestCase):
    def setUp(self):
        self.market = json.loads(MARKET.read_text(encoding="utf-8"))
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_marketplace_identity(self):
        self.assertEqual(self.market["name"], "sch0718-plugins")
        self.assertEqual(self.market["owner"]["name"], "sch0718")
        self.assertIsInstance(self.market["plugins"], list)

    def test_readme_has_marketplace_add(self):
        self.assertIn("/plugin marketplace add sch0718/claude-plugins", self.readme)

    def test_each_entry_points_to_matching_plugin(self):
        names = [p["name"] for p in self.market["plugins"]]
        self.assertEqual(len(names), len(set(names)))
        for entry in self.market["plugins"]:
            with self.subTest(plugin=entry["name"]):
                self.assertEqual(entry["source"], f"./plugins/{entry['name']}")
                manifest = ROOT / "plugins" / entry["name"] / ".claude-plugin" / "plugin.json"
                plugin = json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(plugin["name"], entry["name"])
                self.assertEqual(plugin["version"], entry["version"])
                self.assertIn(f"/plugin install {entry['name']}@sch0718-plugins", self.readme)

    def test_every_plugin_folder_is_listed(self):
        listed = {p["name"] for p in self.market["plugins"]}
        folders = {p.name for p in (ROOT / "plugins").iterdir() if p.is_dir()} if (ROOT / "plugins").exists() else set()
        self.assertEqual(folders, listed)


if __name__ == "__main__":
    unittest.main()
