import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "tools" / "scan_private.py"

TERM = "acme" + "-corp"
PRIVATE_IP = ".".join(["10", "1", "2", "3"])
PLAIN_EMAIL = "someone" + "@" + "mail" + ".test"


def run(args, denylist, cwd=None, stdin=None):
    cmd = [sys.executable, "-I", str(SCANNER), "--denylist", str(denylist), *args]
    return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True)


def git(repo, *args, env=None):
    base = {
        **os.environ,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    base.update(env or {})
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=base)


class ScanPrivateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.denylist = self.tmp / "denylist.txt"
        self.denylist.write_text(TERM + "\n", encoding="utf-8")

    def scan_text(self, text):
        return run(["--stdin"], self.denylist, stdin=text)

    def test_flags_content_hit(self):
        result = self.scan_text(f"see {TERM} repo\n")
        self.assertEqual(result.returncode, 1)

    def test_normalized_variants(self):
        for variant in ("acme_corp", "AcmeCorp", "acme corp", "ACME.CORP"):
            with self.subTest(variant=variant):
                self.assertEqual(self.scan_text(f"x {variant} y\n").returncode, 1)

    def test_clean_text_passes(self):
        self.assertEqual(self.scan_text("nothing to see here\n").returncode, 0)

    def test_flags_path_and_message(self):
        docs = self.tmp / "docs"
        docs.mkdir()
        (docs / f"{TERM}-notes.md").write_text("clean body\n", encoding="utf-8")
        result = run(["--paths", str(docs)], self.denylist)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.scan_text("fix: acme corp\n").returncode, 1)

    def test_builtin_private_ip_and_email(self):
        self.assertEqual(self.scan_text(f"host {PRIVATE_IP}\n").returncode, 1)
        self.assertEqual(self.scan_text(f"mail {PLAIN_EMAIL}\n").returncode, 1)
        self.assertEqual(self.scan_text("noreply@anthropic.com\n").returncode, 0)
        self.assertEqual(self.scan_text("1+x@users.noreply.github.com\n").returncode, 0)
        self.assertEqual(self.scan_text("user@example.com user@example.org\n").returncode, 0)

    def test_ssh_remote_and_url_userinfo_are_not_emails(self):
        self.assertEqual(self.scan_text("url = git@github.com:o/r.git\n").returncode, 0)
        self.assertEqual(self.scan_text("https://tok@github.com/o/r.git\n").returncode, 0)
        self.assertEqual(self.scan_text('"https://user:pw-x@github.com/O/R.git",\n').returncode, 0)
        self.assertEqual(self.scan_text(f"contact {PLAIN_EMAIL}\n").returncode, 1)

    def test_missing_denylist_fails(self):
        missing = self.tmp / "nope.txt"
        self.assertEqual(run(["--stdin"], missing, stdin="x\n").returncode, 2)
        empty = self.tmp / "empty.txt"
        empty.write_text("# only a comment\n\n", encoding="utf-8")
        self.assertEqual(run(["--stdin"], empty, stdin="x\n").returncode, 2)

    def test_output_hides_term(self):
        result = self.scan_text(f"see {TERM} repo\n")
        self.assertNotIn(TERM, result.stdout + result.stderr)
        self.assertNotIn("acme", (result.stdout + result.stderr).lower())

    def test_regex_rule(self):
        self.denylist.write_text("re:(?i)\\bzzq\\b\n", encoding="utf-8")
        self.assertEqual(self.scan_text("a ZZQ b\n").returncode, 1)
        self.assertEqual(self.scan_text("azzqb\n").returncode, 0)

    def test_binary_file_checked_by_name_only(self):
        d = self.tmp / "bin"
        d.mkdir()
        (d / "blob.dat").write_bytes(b"\x00\x01" + TERM.encode())
        self.assertEqual(run(["--paths", str(d)], self.denylist).returncode, 0)

    def test_history_includes_root_commit_and_author(self):
        cases = {
            "root_message": ({"msg": f"init {TERM}"}, {}),
            "author_email": ({"msg": "init"}, {"GIT_AUTHOR_EMAIL": PLAIN_EMAIL}),
            "deleted_file": ({"msg": "init", "file": f"{TERM}\n", "delete": True}, {}),
        }
        for name, (spec, env) in cases.items():
            with self.subTest(case=name):
                repo = Path(tempfile.mkdtemp())
                git(repo, "init", "-q", "-b", "main")
                (repo / "a.txt").write_text(spec.get("file", "clean\n"), encoding="utf-8")
                git(repo, "add", ".")
                git(repo, "commit", "-q", "-m", spec["msg"], env=env)
                if spec.get("delete"):
                    (repo / "a.txt").write_text("clean\n", encoding="utf-8")
                    git(repo, "commit", "-q", "-am", "clean")
                result = run(["--history"], self.denylist, cwd=repo)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_history_treats_added_lines_like_file_lines(self):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        (repo / "a.py").write_text("@functools.lru_cache(maxsize=None)\ndef f():\n    pass\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        self.assertEqual(run(["--all"], self.denylist, cwd=repo).returncode, 0)
        self.assertEqual(run(["--history"], self.denylist, cwd=repo).returncode, 0)

    def test_korean_term_in_nfd_and_json_escape(self):
        import json as _json
        import unicodedata
        term = "가나" + "다라"
        self.denylist.write_text(term + "\n", encoding="utf-8")
        self.assertEqual(self.scan_text(unicodedata.normalize("NFD", term) + "\n").returncode, 1)
        self.assertEqual(self.scan_text(_json.dumps(term) + "\n").returncode, 1)
        self.assertEqual(self.scan_text("ＡＣＭＥ\n").returncode, 0)

    def test_history_includes_annotated_tag_message(self):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("clean\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        git(repo, "tag", "-a", "v1", "-m", f"release for {TERM}")
        self.assertEqual(run(["--history"], self.denylist, cwd=repo).returncode, 1)

    def test_pre_push_hook_checks_remote_ref_names(self):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        hook = ROOT / ".githooks" / "pre-push"
        stdin = f"refs/heads/main abc refs/heads/{TERM}-x 000\n"
        env = {**os.environ, "CLAUDE_PLUGINS_DENYLIST": str(self.denylist)}
        (repo / "a.txt").write_text("clean\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        res = subprocess.run(["bash", str(hook), "origin", "url"], cwd=repo, input=stdin, text=True,
                             capture_output=True, env=env)
        self.assertNotEqual(res.returncode, 0)
        ok = subprocess.run(["bash", str(hook), "origin", "url"], cwd=repo, input="refs/heads/main abc refs/heads/main 000\n",
                            text=True, capture_output=True, env=env)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

    def test_range_includes_root_commit(self):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("clean\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", f"init {TERM}")
        root = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        (repo / "b.txt").write_text("clean\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "second")
        self.assertEqual(run(["--range", f"{root}..HEAD"], self.denylist, cwd=repo).returncode, 1)

    def test_staged_checks_index(self):
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text(f"{TERM}\n", encoding="utf-8")
        git(repo, "add", ".")
        self.assertEqual(run(["--staged"], self.denylist, cwd=repo).returncode, 1)

    def test_repo_itself_is_clean(self):
        real = Path(os.environ.get("CLAUDE_PLUGINS_DENYLIST", Path.home() / ".config/claude-plugins/denylist.txt"))
        if not real.exists():
            self.skipTest("real denylist not present on this machine")
        result = run(["--all"], real, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
