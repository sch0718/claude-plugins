import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import patches, snapshot  # noqa: E402
from cascade.config import RepoConfig  # noqa: E402
from helpers import git, make_repo  # noqa: E402

BASE = "a=0\nb=0\nx=0\nd=0\n"

P_LINE3_A = (
    "diff --git a/a.py b/a.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1,4 +1,4 @@\n"
    " a=0\n"
    " b=0\n"
    "-x=0\n"
    "+x=A\n"
    " d=0\n"
)
P_LINE3_B = P_LINE3_A.replace("x=A", "x=B").replace("2222222", "3333333")


class PatchesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"a.py": BASE})

    def test_same_ignores_index_lines_and_trailing_ws(self):
        other = P_LINE3_A.replace("1111111..2222222", "aaaaaaa..bbbbbbb").replace("+x=A\n", "+x=A  \n")
        self.assertNotEqual(P_LINE3_A, other)
        self.assertTrue(patches.same(P_LINE3_A, other))

    def test_different_hunks_not_same(self):
        self.assertFalse(patches.same(P_LINE3_A, P_LINE3_B))

    def test_normalize_strips_hunk_function_context(self):
        with_ctx = P_LINE3_A.replace("@@ -1,4 +1,4 @@\n", "@@ -1,4 +1,4 @@ def foo():\n")
        norm = patches.normalize(with_ctx)
        self.assertIn("@@ -1,4 +1,4 @@\n", norm)
        self.assertNotIn("def foo", norm)
        self.assertNotIn("index ", norm)
        self.assertTrue(patches.same(with_ctx, P_LINE3_A))
        self.assertTrue(norm.endswith("\n") and not norm.endswith("\n\n"))

    def test_changed_files_lists_added_and_deleted(self):
        diff = (
            P_LINE3_A
            + "diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..e69de29\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+n\n"
            + "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\nindex e69de29..0000000\n"
            "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-o\n"
            + P_LINE3_A
        )
        self.assertEqual(patches.changed_files(diff), ["a.py", "new.txt", "old.txt"])

    def test_apply_conflict_goes_to_undecidable(self):
        rep = patches.apply_all(self.repo, [("r1-01", P_LINE3_A), ("r1-02", P_LINE3_B)])
        self.assertEqual(rep.applied, ["r1-01"])
        self.assertEqual(rep.failed[0][0], "r1-02")
        self.assertTrue(rep.failed[0][1])
        self.assertIn("x=A", (self.repo / "a.py").read_text())
        self.assertEqual(git(self.repo, "diff", "--cached"), "")

    def test_apply_all_does_not_touch_index(self):
        rep = patches.apply_all(self.repo, [("r1-01", P_LINE3_A), ("r1-00", "")])
        self.assertEqual(rep.applied, ["r1-01", "r1-00"])
        self.assertEqual(git(self.repo, "diff", "--cached"), "")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "M a.py")

    def test_apply_report_roundtrip_dict(self):
        rep = patches.ApplyReport(applied=["r1-01"], failed=[("r1-02", "err")])
        self.assertEqual(rep.to_dict(), {"applied": ["r1-01"], "failed": [["r1-02", "err"]]})

    def test_cut_test_files_lists_removed_lines_and_deleted_tests(self):
        diff = (
            "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
            "@@ -1,2 +1,2 @@\n def test_a():\n-    assert f() == 1\n+    pass\n"
            "diff --git a/b.test.js b/b.test.js\ndeleted file mode 100644\n--- a/b.test.js\n+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-test()\n"
            "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        )
        self.assertEqual(patches.cut_test_files(diff), ["tests/test_a.py", "b.test.js"])

    def test_cut_test_files_allows_added_tests(self):
        diff = (
            "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
            "@@ -1,2 +1,4 @@\n def test_a():\n     assert f() == 1\n+def test_b():\n+    assert g() == 2\n"
            "diff --git a/test_new.py b/test_new.py\nnew file mode 100644\n--- /dev/null\n+++ b/test_new.py\n"
            "@@ -0,0 +1 @@\n+import unittest\n"
        )
        self.assertEqual(patches.cut_test_files(diff), [])

    def test_cut_test_files_flags_binary_change_to_existing_test(self):
        diff = (
            "diff --git a/test_a.py b/test_a.py\nindex 1234567..89abcde 100644\n"
            "GIT binary patch\nliteral 4\nLcmZ?d00001\n\n"
            "diff --git a/test_new.py b/test_new.py\nnew file mode 100644\nindex 0000000..1234567\n"
            "GIT binary patch\nliteral 4\nLcmZ?d00001\n\n"
            "diff --git a/test_mode.py b/test_mode.py\nold mode 100644\nnew mode 100755\n"
        )
        self.assertEqual(patches.cut_test_files(diff), ["test_a.py"])

    def test_cut_test_files_counts_removed_line_that_looks_like_header(self):
        diff = (
            "diff --git a/test_a.py b/test_a.py\n--- a/test_a.py\n+++ b/test_a.py\n@@ -1 +1 @@\n"
            "--- comment\n+# comment\n"
        )
        self.assertEqual(patches.cut_test_files(diff), ["test_a.py"])

    def test_changed_files_ignores_hunk_body_lookalikes(self):
        diff = (
            "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n"
            "--- a/fake.py\n+++ b/fake.py\n"
        )
        self.assertEqual(patches.changed_files(diff), ["a.py"])

    def test_changed_files_covers_mode_only_and_binary(self):
        diff = (
            "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
            "diff --git a/img.bin b/img.bin\nnew file mode 100644\nindex 0000000..1234567\n"
            "GIT binary patch\nliteral 0\nHcmV?d00001\n\n"
        )
        self.assertEqual(patches.changed_files(diff), ["run.sh", "img.bin"])

    def test_extract_ignores_user_diff_config(self):
        git(self.repo, "config", "diff.noprefix", "true")
        git(self.repo, "config", "diff.renames", "copies")
        snap = snapshot.create_snapshot(self.repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "a.py").write_text("a=0\nb=0\nx=1\nd=0\n")
        diff = patches.extract(wt, snap.commit)
        self.assertIn("+++ b/a.py", diff)
        self.assertEqual(patches.changed_files(diff), ["a.py"])

    def test_extract_ignores_user_diff_context_zero(self):
        body = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(6))
        repo = make_repo(self.tmp / "ctx", {"m.py": body})
        git(repo, "config", "diff.context", "0")
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        new = body.replace("def f2():\n    return 2\n\n", "") + "def g():\n    return 9\n\n"
        (wt / "m.py").write_text(new)
        rep = patches.apply_all(repo, [("r1-01", patches.extract(wt, snap.commit))])
        self.assertEqual(rep.failed, [])
        self.assertEqual((repo / "m.py").read_text(), new)

    def test_changed_files_reads_quoted_paths(self):
        diff = (
            'diff --git "a/auth/q\\"x.py" "b/auth/q\\"x.py"\nnew file mode 100644\n'
            'diff --git "a/t\\tb.py" "b/t\\tb.py"\nnew file mode 100644\n'
            'diff --git "a/\\355\\225\\234.py" "b/\\355\\225\\234.py"\nnew file mode 100644\n'
        )
        self.assertEqual(patches.changed_files(diff), ['auth/q"x.py', "t\tb.py", "한.py"])

    def test_extract_quoted_path_is_listed_and_applies(self):
        snap = snapshot.create_snapshot(self.repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "auth").mkdir()
        (wt / "auth" / 'q"x.py').write_text("k=1\n")
        diff = patches.extract(wt, snap.commit)
        self.assertEqual(patches.changed_files(diff), ['auth/q"x.py'])
        rep = patches.apply_all(self.repo, [("r1-01", diff)])
        self.assertEqual(rep.failed, [])
        self.assertEqual((self.repo / "auth" / 'q"x.py').read_text(), "k=1\n")

    def test_extract_covers_rename_mode_and_binary_and_applies(self):
        snap = snapshot.create_snapshot(self.repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "a.py").rename(wt / "b.py")
        (wt / "tool.sh").write_text("echo\n")
        (wt / "tool.sh").chmod(0o755)
        (wt / "img.bin").write_bytes(bytes(range(256)) * 4)
        diff = patches.extract(wt, snap.commit)
        self.assertEqual(sorted(patches.changed_files(diff)), ["a.py", "b.py", "img.bin", "tool.sh"])
        rep = patches.apply_all(self.repo, [("r1-01", diff)])
        self.assertEqual(rep.failed, [])
        self.assertFalse((self.repo / "a.py").exists())
        self.assertEqual((self.repo / "img.bin").read_bytes(), bytes(range(256)) * 4)
        self.assertTrue((self.repo / "tool.sh").stat().st_mode & 0o100)

    def test_crlf_patch_roundtrips_through_extract_and_apply(self):
        repo = make_repo(self.tmp / "crlf", {"c.txt": "a\r\nb\r\nc\r\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "c.txt").write_bytes(b"a\r\nB\r\nc\r\n")
        diff = patches.extract(wt, snap.commit)
        self.assertIn("+B\r\n", diff)
        rep = patches.apply_all(repo, [("r1-01", diff)])
        self.assertEqual(rep.failed, [])
        self.assertEqual((repo / "c.txt").read_bytes(), b"a\r\nB\r\nc\r\n")

    def test_same_sees_crlf_difference(self):
        crlf = P_LINE3_A.replace("+x=A\n", "+x=A\r\n")
        self.assertFalse(patches.same(P_LINE3_A, crlf))

    def test_extract_runs_format_then_diffs_against_snapshot(self):
        snap = snapshot.create_snapshot(self.repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "a.py").write_text("a=0\nb=0\nx=1\nd=0\n")
        (wt / "fmt_me.py").write_text("y=2\n")
        snapshot.commit_worktree(wt, "worker change")
        cfg = RepoConfig(format=['python3 -c "import pathlib; pathlib.Path(\'fmt_me.py\').write_text(\'y = 2\\n\')"'])
        diff = patches.extract(wt, snap.commit, cfg)
        self.assertIn("+x=1", diff)
        self.assertIn("+y = 2", diff)
        self.assertNotIn("+y=2", diff)
        self.assertEqual(patches.changed_files(diff), ["a.py", "fmt_me.py"])
        without = patches.extract(wt, snap.commit)
        self.assertIn("+y = 2", without)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


    def test_extract_formats_only_worker_files_and_leaves_unformatted_neighbours(self):
        repo = make_repo(self.tmp / "fmt", {"a.py": BASE, "three.py": "z=0\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        fmt = (
            "python3 -c \"import sys, pathlib; "
            "[pathlib.Path(p).write_text(pathlib.Path(p).read_text().replace('=', ' = ')) "
            "for p in (sys.argv[1:] or [str(q) for q in pathlib.Path('.').glob('*.py')])]\""
        )
        cfg = RepoConfig(format=[fmt])
        diffs = []
        for name, line in (("w1", "x=1"), ("w2", "d=1")):
            wt = snapshot.add_worktree(snap, name)
            text = (wt / "a.py").read_text().replace("x=0" if name == "w1" else "d=0", line)
            (wt / "a.py").write_text(text)
            (wt / "new.py").write_text("n=1\n") if name == "w2" else None
            diff = patches.extract(wt, snap.commit, cfg)
            self.assertEqual((wt / "three.py").read_text(), "z=0\n")
            self.assertNotIn("three.py", diff)
            diffs.append(diff)
        self.assertEqual(patches.changed_files(diffs[0]), ["a.py"])
        self.assertEqual(sorted(patches.changed_files(diffs[1])), ["a.py", "new.py"])
        self.assertIn("+n = 1", diffs[1])
        rep = patches.apply_all(repo, [("f1", diffs[0])])
        self.assertEqual(rep.failed, [])
        self.assertEqual((repo / "three.py").read_text(), "z=0\n")

    def test_extract_without_changes_is_empty(self):
        snap = snapshot.create_snapshot(self.repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        self.assertEqual(patches.extract(wt, snap.commit, RepoConfig(format=["touch junk.py"])), "")
        self.assertFalse((wt / "junk.py").exists())

    def _external_patch(self, old, new):
        return (
            "diff --git a/외부/plan.md b/외부/plan.md\n"
            "--- a/외부/plan.md\n"
            "+++ b/외부/plan.md\n"
            "@@ -1,2 +1,2 @@\n"
            " # 계획\n"
            f"-{old}\n"
            f"+{new}\n"
        )

    def test_apply_all_writes_external_section_to_original_file(self):
        doc = self.tmp / "outside" / "plan.md"
        doc.parent.mkdir()
        doc.write_text("# 계획\n1단계\n", encoding="utf-8")
        report = patches.apply_all(self.repo, [("r1-01", P_LINE3_A + self._external_patch("1단계", "1단계: 고친다"))],
                                   {"외부/plan.md": doc})
        self.assertEqual(report.applied, ["r1-01"])
        self.assertEqual(doc.read_text(encoding="utf-8"), "# 계획\n1단계: 고친다\n")
        self.assertIn("x=A", (self.repo / "a.py").read_text())
        self.assertFalse((self.repo / "외부").exists())

    def test_apply_all_external_failure_changes_nothing(self):
        doc = self.tmp / "outside" / "plan.md"
        doc.parent.mkdir()
        doc.write_text("# 계획\n다른 줄\n", encoding="utf-8")
        report = patches.apply_all(self.repo, [("r1-01", P_LINE3_A + self._external_patch("1단계", "x"))],
                                   {"외부/plan.md": doc})
        self.assertEqual([fid for fid, _ in report.failed], ["r1-01"])
        self.assertEqual(doc.read_text(encoding="utf-8"), "# 계획\n다른 줄\n")
        self.assertEqual((self.repo / "a.py").read_text(), BASE)

if __name__ == "__main__":
    unittest.main()
