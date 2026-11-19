import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import snapshot  # noqa: E402
from helpers import git, make_repo  # noqa: E402


def show(repo, spec):
    return subprocess.run(["git", "show", spec], cwd=repo, capture_output=True, text=True, check=True).stdout


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_snapshot_keeps_user_index(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        (repo / "a.py").write_text("x=2\n")
        (repo / "new.txt").write_text("n\n")
        ext = self.tmp / "outside" / "spec.md"
        ext.parent.mkdir()
        ext.write_text("# s\n")
        before = git(repo, "status", "--porcelain")
        head_before = git(repo, "rev-parse", "HEAD")
        snap = snapshot.create_snapshot(repo, "run1", [ext])
        self.assertEqual(git(repo, "status", "--porcelain"), before)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "")
        tree = git(repo, "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", snap.commit).splitlines()
        self.assertIn("new.txt", tree)
        self.assertIn("외부/spec.md", tree)
        self.assertEqual(show(repo, f"{snap.commit}:a.py"), "x=2\n")
        self.assertEqual(show(repo, f"{snap.commit}:외부/spec.md"), "# s\n")
        self.assertEqual(snap.branch, "cascade-fix/run1")
        self.assertEqual(git(repo, "rev-parse", snap.branch), snap.commit)
        self.assertEqual(git(repo, "rev-parse", f"{snap.commit}^"), head_before)
        self.assertEqual(snap.run_dir, repo / ".cascade-fix" / "run1")

    def test_snapshot_excludes_run_dir(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        stale = repo / ".cascade-fix" / "old" / "junk.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("j\n")
        snap = snapshot.create_snapshot(repo, "run1", [])
        self.assertTrue((snap.run_dir / "index.tmp").exists())
        tree = git(repo, "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", snap.commit).splitlines()
        self.assertEqual(tree, ["a.py"])
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_snapshot_without_commits_fails(self):
        repo = self.tmp / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        with self.assertRaisesRegex(RuntimeError, "커밋이 없는 저장소"):
            snapshot.create_snapshot(repo, "run1", [])

    def test_worktree_from_snapshot_and_commit(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        (repo / "a.py").write_text("x=2\n")
        before = git(repo, "status", "--porcelain")
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        self.assertEqual(wt, snap.run_dir / "wt" / "w1")
        self.assertEqual(snap.worktrees["w1"], wt)
        self.assertEqual((wt / "a.py").read_text(), "x=2\n")
        self.assertIsNone(snapshot.commit_worktree(wt, "변경 없음"))
        (wt / "a.py").write_text("x=3\n")
        sha = snapshot.commit_worktree(wt, "a.py 고침")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), sha)
        self.assertEqual(git(wt, "rev-parse", f"{sha}^"), snap.commit)
        self.assertEqual(git(wt, "log", "-1", "--format=%s"), "a.py 고침")
        self.assertEqual(git(wt, "status", "--porcelain"), "")
        base = snapshot.add_worktree(snap, "base")
        self.assertEqual((base / "a.py").read_text(), "x=2\n")
        self.assertEqual(git(repo, "status", "--porcelain"), before)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual((repo / "a.py").read_text(), "x=2\n")

    def test_commit_worktree_skips_hooks(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        markers = self.tmp / "markers"
        markers.mkdir()
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        names = ["pre-commit", "prepare-commit-msg", "commit-msg", "post-commit",
                 "post-checkout", "post-index-change", "reference-transaction"]
        for name in names:
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\ntouch '{markers}/{name}'\nexit 1\n")
            hook.chmod(0o755)
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "a.py").write_text("x=3\n")
        sha = snapshot.commit_worktree(wt, "훅 무시")
        snapshot.cleanup(snap, keep_run_dir=True)
        self.assertEqual(sorted(p.name for p in markers.iterdir()), [])
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(git(repo, "rev-parse", f"{sha}^"), snap.commit)

    def test_commit_worktree_outside_run_dir_skips_hooks_without_stray_dir(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        marker = self.tmp / "hook-ran"
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        wt = self.tmp / "elsewhere" / "wt"
        wt.parent.mkdir()
        git(repo, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(wt), "HEAD")
        (wt / "a.py").write_text("x=2\n")
        sha = snapshot.commit_worktree(wt, "바깥 워크트리")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertFalse(marker.exists())
        self.assertFalse((self.tmp / "no-hooks").exists())

    def test_cleanup_removes_branch_and_worktrees(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        snapshot.add_worktree(snap, "base")
        (snap.run_dir / "ledger.json").write_text("{}")
        snapshot.cleanup(snap, keep_run_dir=True)
        self.assertFalse(wt.exists())
        self.assertFalse((snap.run_dir / "wt").exists())
        self.assertFalse((snap.run_dir / "index.tmp").exists())
        self.assertTrue((snap.run_dir / "ledger.json").exists())
        self.assertEqual(git(repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertEqual(len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) - 1, 1)
        snap2 = snapshot.create_snapshot(repo, "run2", [])
        snapshot.add_worktree(snap2, "w1")
        snapshot.cleanup(snap2, keep_run_dir=False)
        self.assertFalse(snap2.run_dir.exists())
        self.assertEqual(git(repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_cleanup_removes_unregistered_worktree(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        ghost = snap.run_dir / "wt" / "ghost"
        git(repo, "worktree", "add", "--detach", str(ghost), snap.commit)
        self.assertNotIn("ghost", snap.worktrees)
        snapshot.cleanup(snap, keep_run_dir=False)
        listed = [l for l in git(repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]
        self.assertEqual(listed, [f"worktree {repo}"])
        self.assertEqual(list((repo / ".git" / "worktrees").glob("*")) if (repo / ".git" / "worktrees").exists() else [], [])
        self.assertFalse(snap.run_dir.exists())

    def test_cleanup_continues_after_worktree_failure(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        locked = snapshot.add_worktree(snap, "locked")
        other = snapshot.add_worktree(snap, "other")
        git(repo, "worktree", "lock", str(locked))
        with self.assertRaisesRegex(RuntimeError, "정리 실패"):
            snapshot.cleanup(snap, keep_run_dir=True)
        self.assertFalse(other.exists())
        self.assertEqual(git(repo, "branch", "--list", "cascade-fix/*"), "")
        git(repo, "worktree", "unlock", str(locked))
        git(repo, "worktree", "prune")

    def test_rejects_unsafe_run_id_and_worktree_name(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        for bad in ["../x", "a/b", "", ".hidden", "-x"]:
            with self.assertRaisesRegex(RuntimeError, "실행 ID"):
                snapshot.create_snapshot(repo, bad, [])
        snap = snapshot.create_snapshot(repo, "run1.a_b-c", [])
        with self.assertRaisesRegex(RuntimeError, "워크트리 이름"):
            snapshot.add_worktree(snap, "../x")
        self.assertEqual(git(repo, "branch", "--list", "cascade-fix/*").strip(), "cascade-fix/run1.a_b-c")

    def test_cleanup_refuses_run_dir_outside_root(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        victim = self.tmp / "victim"
        victim.mkdir()
        snap.run_dir = victim
        with self.assertRaisesRegex(RuntimeError, "run_dir"):
            snapshot.cleanup(snap, keep_run_dir=False)
        self.assertTrue(victim.exists())

    def test_snapshot_ignores_inherited_git_env(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        other = make_repo(self.tmp / "other", {"b.py": "y=1\n"})
        env = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
               "GIT_INDEX_FILE": str(other / ".git" / "index")}
        with mock.patch.dict(os.environ, env):
            snap = snapshot.create_snapshot(repo, "run1", [])
        tree = git(repo, "ls-tree", "-r", "--name-only", snap.commit).splitlines()
        self.assertEqual(tree, ["a.py"])
        self.assertEqual(git(other, "branch", "--list", "cascade-fix/*"), "")

    def test_ensure_excluded_adds_once(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snapshot.ensure_excluded(repo)
        snapshot.ensure_excluded(repo)
        lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(lines.count(".cascade-fix/"), 1)
        (repo / ".cascade-fix").mkdir()
        (repo / ".cascade-fix" / "x").write_text("x")
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_snapshot_roundtrip_dict(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        snapshot.add_worktree(snap, "base")
        data = snap.to_dict()
        self.assertEqual(data["root"], str(repo))
        self.assertEqual(data["worktrees"], {"base": str(snap.run_dir / "wt" / "base")})
        self.assertEqual(snapshot.Snapshot.from_dict(data), snap)


    def test_snapshot_tree_advances_branch_with_current_tree(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        ext = self.tmp / "outside" / "spec.md"
        ext.parent.mkdir()
        ext.write_text("# 1\n")
        snap = snapshot.create_snapshot(repo, "run1", [ext])
        (repo / "a.py").write_text("x=2\n")
        ext.write_text("# 2\n")
        before = git(repo, "status", "--porcelain")
        new = snapshot.snapshot_tree(snap, [ext])
        self.assertEqual(git(repo, "rev-parse", snap.branch), new)
        self.assertEqual(git(repo, "rev-parse", f"{new}^"), snap.commit)
        self.assertEqual(show(repo, f"{new}:a.py"), "x=2\n")
        self.assertEqual(show(repo, f"{new}:외부/spec.md"), "# 2\n")
        self.assertEqual(git(repo, "status", "--porcelain"), before)
        newer = snapshot.snapshot_tree(snap, [ext])
        self.assertEqual(git(repo, "rev-parse", f"{newer}^"), new)

    def test_add_worktree_from_given_commit(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        (repo / "a.py").write_text("x=2\n")
        new = snapshot.snapshot_tree(snap, [])
        old_wt = snapshot.add_worktree(snap, "old")
        new_wt = snapshot.add_worktree(snap, "new", new)
        self.assertEqual((old_wt / "a.py").read_text(), "x=1\n")
        self.assertEqual((new_wt / "a.py").read_text(), "x=2\n")

    def test_make_repo_ignores_global_gpgsign(self):
        global_cfg = self.tmp / "gitconfig"
        global_cfg.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_cfg), "GIT_CONFIG_NOSYSTEM": "1"}):
            repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        self.assertEqual(git(repo, "config", "--local", "commit.gpgsign"), "false")
        self.assertRegex(git(repo, "rev-parse", "HEAD"), r"^[0-9a-f]{40}$")

if __name__ == "__main__":
    unittest.main()
