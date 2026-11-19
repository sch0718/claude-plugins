import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import fakehome as fh

from session_move import cli
from session_move.backlog import merge_backlog
from session_move.manifest import build_manifest, save_manifest, SAME_DEVICE_ITEMS
from session_move.paths import Paths
from session_move.schedules import merge_schedules
from session_move.sessions import ACCOUNT_BOUND_FIELDS, plan_sessions


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class ApplyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = fh.make_fake_home(self.tmp)
        self.paths = Paths(home=self.home)
        self.src = fh.scope_dir(self.home, fh.SCOPE_A)
        self.dst = fh.scope_dir(self.home, fh.SCOPE_B)
        self.env = {"SESSION_MOVE_HOME": str(self.home), "SESSION_MOVE_FAKE_APP_RUNNING": "0"}
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def manifest_file(self, items=SAME_DEVICE_ITEMS) -> str:
        return save_manifest(self.paths, build_manifest(self.paths, fh.SCOPE_A, set(items)))

    def apply(self, *extra) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.main(["apply", "--manifest", self.manifest_file(), "--to", fh.SCOPE_B, *extra])
        return code, buf.getvalue()

    def target(self, sid):
        return json.loads((self.dst / f"{sid}.json").read_text())

    def put_target(self, sid, **fields):
        rec = json.loads((self.src / f"{sid}.json").read_text())
        rec.update(fields)
        fh.write_json(self.dst / f"{sid}.json", rec)


class SessionsTest(ApplyCase):
    def test_add_when_missing(self):
        code, out = self.apply()
        self.assertEqual(code, 0, out)
        rec = self.target(fh.S_PARENT)
        for k in ACCOUNT_BOUND_FIELDS:
            self.assertNotIn(k, rec)
        self.assertFalse(rec["steeredByRemoteClient"])
        self.assertEqual(self.target(fh.S_CHILD)["spawnedFrom"]["sessionId"], fh.S_PARENT)

    def test_update_when_source_newer_keeps_target_account_fields(self):
        self.put_target(fh.S_PARENT, lastActivityAt=1000, title="old", bridgeSessionIds=["mine"],
                        enabledMcpTools={"mine": True})
        code, out = self.apply()
        rec = self.target(fh.S_PARENT)
        self.assertEqual(rec["title"], "title 1111")
        self.assertEqual(rec["lastActivityAt"], 2000)
        self.assertEqual(rec["bridgeSessionIds"], ["mine"])
        self.assertEqual(rec["enabledMcpTools"], {"mine": True})

    def test_skip_when_target_newer_or_equal(self):
        self.put_target(fh.S_PARENT, lastActivityAt=5000, title="newer")
        code, out = self.apply()
        self.assertEqual(code, 2)
        self.assertIn("대상이 더 최근", out)
        self.assertEqual(self.target(fh.S_PARENT)["title"], "newer")

    def test_archive_state_follows_newer_source(self):
        self.put_target(fh.S_ARCHIVED, lastActivityAt=100, isArchived=False)
        self.put_target(fh.S_PARENT, lastActivityAt=100, isArchived=True)
        fh.write_json(self.dst / "archived-sessions.idx", {"archived": [fh.S_PARENT]})
        self.apply()
        idx = json.loads((self.dst / "archived-sessions.idx").read_text())["archived"]
        self.assertIn(fh.S_ARCHIVED, idx)
        self.assertNotIn(fh.S_PARENT, idx)
        self.assertFalse(self.target(fh.S_PARENT).get("isArchived"))

    def test_skip_when_transcript_missing(self):
        for p in (self.home / ".claude" / "projects").rglob("cli-child.jsonl"):
            p.unlink()
        m = build_manifest(self.paths, fh.SCOPE_A, {"sessions"})
        actions = {a.session_id: a for a in plan_sessions(m, self.src, self.dst, self.paths.projects_root)}
        self.assertEqual(actions[fh.S_CHILD].kind, "skip")
        self.assertIn("대화 기록", actions[fh.S_CHILD].reason)
        self.assertEqual(actions[fh.S_PARENT].kind, "add")

    def test_add_when_only_prior_cli_transcript_exists(self):
        for p in (self.home / ".claude" / "projects").rglob("cli-child.jsonl"):
            p.unlink()
        rec = json.loads((self.src / f"{fh.S_CHILD}.json").read_text())
        rec["cliSessionId"] = None
        rec["priorCliSessionIds"] = ["cli-child-prior"]
        fh.write_json(self.src / f"{fh.S_CHILD}.json", rec)
        fh.transcript(self.home, rec["cwd"], "cli-child-prior")
        m = build_manifest(self.paths, fh.SCOPE_A, {"sessions"})
        actions = {a.session_id: a for a in plan_sessions(m, self.src, self.dst, self.paths.projects_root)}
        self.assertEqual(actions[fh.S_CHILD].kind, "add")


class MergeTest(unittest.TestCase):
    def test_merge_schedules_adds_missing_keeps_run_history(self):
        src = {"scheduledTasks": [{"id": "a", "enabled": True, "lastRunAt": "x", "lastScheduledFor": "y",
                                   "missedRunScanFloor": "z"}, {"id": "b", "enabled": True}]}
        dst = {"scheduledTasks": [{"id": "b", "enabled": False}], "other": 1}
        merged, added = merge_schedules(src, dst, keep_enabled=True)
        self.assertEqual(added, ["a"])
        a = next(t for t in merged["scheduledTasks"] if t["id"] == "a")
        self.assertEqual((a["lastRunAt"], a["lastScheduledFor"], a["missedRunScanFloor"]), ("x", "y", "z"))
        self.assertTrue(a["enabled"])
        self.assertFalse(next(t for t in merged["scheduledTasks"] if t["id"] == "b")["enabled"])
        self.assertEqual(merged["other"], 1)
        merged2, _ = merge_schedules(src, dst, keep_enabled=False)
        self.assertFalse(next(t for t in merged2["scheduledTasks"] if t["id"] == "a")["enabled"])

    def test_merge_backlog_by_id_newer_wins(self):
        src = {"items": [{"id": "x", "title": "new", "filedAt": 20}, {"id": "y", "title": "y", "suggestedAt": 5},
                         {"id": "z", "title": "src"}]}
        dst = {"version": 1, "items": [{"id": "x", "title": "old", "filedAt": 10}, {"id": "z", "title": "dst"}]}
        merged, changed = merge_backlog(src, dst)
        by = {i["id"]: i for i in merged["items"]}
        self.assertEqual(by["x"]["title"], "new")
        self.assertEqual(by["y"]["title"], "y")
        self.assertEqual(by["z"]["title"], "dst")
        self.assertEqual(sorted(changed), ["x", "y"])
        self.assertEqual(merged["version"], 1)


class ApplyGuardsTest(ApplyCase):
    def test_apply_refuses_when_app_running(self):
        before = tree_hash(self.dst)
        os.environ["SESSION_MOVE_FAKE_APP_RUNNING"] = "1"
        code, out = self.apply()
        self.assertEqual(code, 3)
        self.assertEqual(tree_hash(self.dst), before)

    def test_apply_refuses_target_equal_to_source(self):
        before_src, before_dst = tree_hash(self.src), tree_hash(self.dst)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.main(["apply", "--manifest", self.manifest_file(), "--to", fh.SCOPE_A])
        self.assertEqual(code, 1)
        alias = self.home / "Library" / "Application Support" / "Claude" / "claude-code-sessions" / "alias"
        alias.mkdir()
        (alias / fh.ORG).symlink_to(self.src)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code2 = cli.main(["apply", "--manifest", self.manifest_file(), "--to", f"alias/{fh.ORG}"])
        self.assertEqual(code2, 1)
        self.assertEqual((tree_hash(self.src), tree_hash(self.dst)), (before_src, before_dst))

    def test_apply_backs_up_then_restore_roundtrip(self):
        fh.write_json(self.dst / "scheduled-tasks.json", {"scheduledTasks": []})
        before = tree_hash(self.dst)
        code, out = self.apply()
        self.assertEqual(code, 0, out)
        self.assertNotEqual(tree_hash(self.dst), before)
        backup = next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("백업: "))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["restore", backup]), 0)
        self.assertEqual(tree_hash(self.dst), before)

    def test_dry_run_writes_nothing(self):
        before = tree_hash(self.dst)
        os.environ["SESSION_MOVE_FAKE_APP_RUNNING"] = "1"
        code, out = self.apply("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(tree_hash(self.dst), before)
        self.assertIn("추가", out)

    def test_source_untouched(self):
        before = tree_hash(self.src)
        self.apply()
        self.assertEqual(tree_hash(self.src), before)

    def test_only_session_trial_leaves_other_items(self):
        fh.write_json(self.dst / "scheduled-tasks.json", {"scheduledTasks": []})
        fh.write_json(self.dst / "backlog" / "tasks.json", {"version": 1, "items": []})
        sched = (self.dst / "scheduled-tasks.json").read_bytes()
        backlog = (self.dst / "backlog" / "tasks.json").read_bytes()
        code, out = self.apply("--only", "sessions", "--only-session", fh.S_PARENT)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.dst / f"{fh.S_PARENT}.json").exists())
        self.assertFalse((self.dst / f"{fh.S_CHILD}.json").exists())
        self.assertEqual((self.dst / "scheduled-tasks.json").read_bytes(), sched)
        self.assertEqual((self.dst / "backlog" / "tasks.json").read_bytes(), backlog)

    def test_schedules_and_backlog_applied_same_device(self):
        code, out = self.apply()
        tasks = json.loads((self.dst / "scheduled-tasks.json").read_text())["scheduledTasks"]
        self.assertEqual({t["id"]: t["enabled"] for t in tasks}, {"daily-note": True, "weekly": False})
        items = json.loads((self.dst / "backlog" / "tasks.json").read_text())["items"]
        self.assertEqual([i["id"] for i in items], ["b1"])


if __name__ == "__main__":
    unittest.main()
