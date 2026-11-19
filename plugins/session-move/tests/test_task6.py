import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fakehome as fh

from session_move.company import CompanyRules
from session_move.manifest import build_manifest
from session_move.paths import Paths
from session_move.scopes import list_scopes

PLUGIN = Path(__file__).resolve().parents[1]


class FakeHomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = fh.make_fake_home(self.tmp)
        self.paths = Paths(home=self.home)


class ScopesTest(FakeHomeCase):
    def test_list_scopes_counts(self):
        scopes = {f"{s.account}/{s.org}": s for s in list_scopes(self.paths)}
        a, b = scopes[fh.SCOPE_A], scopes[fh.SCOPE_B]
        self.assertEqual((a.active, a.archived, a.last_activity), (2, 1, 2000))
        self.assertEqual((b.active, b.archived), (0, 0))


class ManifestTest(FakeHomeCase):
    def build(self, items=None):
        return build_manifest(self.paths, fh.SCOPE_A, items or {"sessions", "schedules", "backlog", "sidebar", "presets"})

    def test_manifest_has_archived_ids_only(self):
        m = self.build()
        self.assertEqual([s["sessionId"] for s in m["sessions"]], [fh.S_PARENT, fh.S_CHILD])
        self.assertEqual(m["archived"], [{"sessionId": fh.S_ARCHIVED, "lastActivityAt": 900, "isCompany": False}])
        child = next(s for s in m["sessions"] if s["sessionId"] == fh.S_CHILD)
        self.assertEqual(child["parentSessionId"], fh.S_PARENT)
        self.assertNotIn("postTurnSummary", json.dumps(m))

    def test_manifest_groups_sections_pins_presets(self):
        m = self.build()
        groups = {g["name"]: g["sessionIds"] for g in m["groups"]}
        self.assertEqual(groups, {"ideas": [fh.S_PARENT, fh.S_CHILD], "review": []})
        self.assertEqual([s["name"] for s in m["sections"]], ["ideas"])
        self.assertEqual(m["pinnedSessionIds"], [fh.S_PARENT])
        self.assertEqual([p["name"] for p in m["presets"]], ["repo preset"])
        self.assertEqual([s["id"] for s in m["schedules"]], ["daily-note", "weekly"])
        self.assertEqual([b["id"] for b in m["backlog"]], ["b1"])
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["sourceScope"], fh.SCOPE_A)
        self.assertEqual(m["sourceHome"], str(self.home))

    def test_manifest_respects_items(self):
        m = self.build({"sessions"})
        self.assertEqual(m["schedules"], [])
        self.assertEqual(m["groups"], [])


class CompanyRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def cfg(self, text):
        p = self.tmp / "config.toml"
        p.write_text(text, encoding="utf-8")
        return CompanyRules.load(p)

    def repo(self, name, url, layout="plain"):
        root = self.tmp / name
        root.mkdir()
        config = f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n'
        if layout == "plain":
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text(config)
        elif layout == "worktree":
            common = self.tmp / f"{name}-common"
            wt = common / "worktrees" / "w1"
            wt.mkdir(parents=True)
            (common / "config").write_text(config)
            (wt / "commondir").write_text("../..\n")
            (root / ".git").write_text(f"gitdir: {wt}\n")
        else:
            gd = self.tmp / f"{name}-modules"
            gd.mkdir()
            (gd / "config").write_text(config)
            (root / ".git").write_text(f"gitdir: ../{gd.name}\n")
        sub = root / "src"
        sub.mkdir()
        return str(sub)

    def test_company_rules_match_remote_and_prefix(self):
        rules = self.cfg('orgs = ["example-org"]\nrepos = ["secret-app"]\npath_prefixes = ["/opt/corp"]\n')
        self.assertTrue(rules.is_company(self.repo("a", "git@github.com:example-org/x.git")))
        self.assertFalse(rules.is_company(self.repo("b", "https://github.com/someone/x.git")))
        self.assertTrue(rules.is_company("/opt/corp/tool"))
        self.assertTrue(rules.is_company(str(self.tmp / "secret-app" / "docs")))
        self.assertFalse(rules.is_company("/opt/corporate"))

    def test_missing_or_empty_config_is_none(self):
        self.assertIsNone(CompanyRules.load(self.tmp / "nope.toml"))
        self.assertIsNone(self.cfg(""))
        self.assertIsNone(self.cfg('orgs = []\nrepos = []\npath_prefixes = []\n'))

    def test_company_rules_read_git_config_without_git(self):
        rules = self.cfg('orgs = ["example-org"]\n')
        cwds = [self.repo(n, f"https://github.com/example-org/{n}.git", layout=n)
                for n in ("plain", "worktree", "submodule")]
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.tmp)
        try:
            for cwd in cwds:
                with self.subTest(cwd=cwd):
                    self.assertTrue(rules.is_company(cwd))
        finally:
            os.environ["PATH"] = old


class LauncherTest(unittest.TestCase):
    def test_launcher_resolves_symlink_and_python(self):
        tmp = Path(tempfile.mkdtemp())
        link = tmp / "sm"
        link.symlink_to(PLUGIN / "bin" / "session-move")
        ok = subprocess.run([str(link), "scopes", "--help"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        old_py = fake_bin / "python3"
        old_py.write_text("#!/bin/sh\nexit 1\n")
        old_py.chmod(0o755)
        env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp)}
        res = subprocess.run(["/bin/bash", str(link), "scopes"], capture_output=True, text=True, env=env)
        if shutil.which("python3", path="/usr/bin") and subprocess.run(
                ["/usr/bin/python3", "-c", "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)"]).returncode == 0:
            self.skipTest("system python3 is already 3.11+")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Python 3.11", res.stderr)


if __name__ == "__main__":
    unittest.main()
