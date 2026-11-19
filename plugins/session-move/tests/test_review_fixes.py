import json
import os
import subprocess
import tarfile
import time
import unicodedata
import unittest
from pathlib import Path

import fakehome as fh
from test_task8 import CO, BundleCase, members, run_cli

from session_move.environment import Words

GH_TOKEN = "ghp_" + "A" * 36


def last_line(path: Path) -> dict:
    return json.loads(path.read_text().splitlines()[-1])


class ReviewFixesTest(BundleCase):
    def dst_scope(self, dst):
        return fh.scope_dir(dst, fh.SCOPE_B)

    def backup_from(self, out: str) -> str:
        return next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("백업: "))

    # Important 1
    def test_second_bundle_updates_transcript_of_updated_session(self):
        self.export("--only", "sessions,transcripts")
        dst = self.other_home()
        self.apply_other(dst)
        src_t = next((self.home / ".claude" / "projects").glob("*/cli-parent.jsonl"))
        with open(src_t, "a") as f:
            f.write(json.dumps({"type": "user", "cwd": str(self.repo), "message": {"content": "later"}}) + "\n")
        rec = json.loads((self.sa / f"{fh.S_PARENT}.json").read_text())
        rec["lastActivityAt"] = 9000
        rec["title"] = "newer"
        fh.write_json(self.sa / f"{fh.S_PARENT}.json", rec)
        self.export("--only", "sessions,transcripts")
        code, out = self.apply_other(dst)
        dst_t = next((dst / ".claude" / "projects").glob("*/cli-parent.jsonl"))
        self.assertEqual(last_line(dst_t)["message"]["content"], "later", out)
        self.assertEqual(json.loads((self.dst_scope(dst) / f"{fh.S_PARENT}.json").read_text())["title"], "newer")

    # Important 2
    def test_bundle_archive_state_follows_newer_source(self):
        self.export("--only", "sessions,transcripts")
        dst = self.other_home()
        self.apply_other(dst)
        rec = json.loads((self.sa / f"{fh.S_CHILD}.json").read_text())
        rec.update(isArchived=True, lastActivityAt=9500)
        fh.write_json(self.sa / f"{fh.S_CHILD}.json", rec)
        idx = json.loads((self.sa / "archived-sessions.idx").read_text())
        idx["archived"].append(fh.S_CHILD)
        fh.write_json(self.sa / "archived-sessions.idx", idx)
        self.export("--only", "sessions,transcripts")
        self.apply_other(dst)
        tidx = json.loads((self.dst_scope(dst) / "archived-sessions.idx").read_text())["archived"]
        self.assertIn(fh.S_CHILD, tidx)
        self.assertTrue(json.loads((self.dst_scope(dst) / f"{fh.S_CHILD}.json").read_text())["isArchived"])

    # Important 3
    def test_restore_refuses_when_app_running_or_newer_changes(self):
        self.export("--only", "sessions,transcripts")
        dst = self.other_home()
        code, out = self.apply_other(dst)
        backup = self.backup_from(out)
        env = dict(self.env, SESSION_MOVE_HOME=str(dst), SESSION_MOVE_FAKE_APP_RUNNING="1")
        self.assertEqual(run_cli(["restore", backup], env)[0], 3)
        env["SESSION_MOVE_FAKE_APP_RUNNING"] = "0"
        time.sleep(0.01)
        later = self.dst_scope(dst) / "local_99999999-9999-4999-8999-999999999999.json"
        later.write_text("{}")
        code, out = run_cli(["restore", backup], env)
        self.assertEqual(code, 1, out)
        self.assertTrue(later.exists())
        self.assertIn(later.name, out)
        code, out = run_cli(["restore", backup, "--force"], env)
        self.assertEqual(code, 0, out)
        self.assertFalse(later.exists())
        self.assertIn("되돌리기 전 상태 백업", out)

    # Important 4
    def test_tokens_in_session_files_and_repo_urls_not_bundled(self):
        rec = json.loads((self.sa / f"{fh.S_PARENT}.json").read_text())
        rec["scratchPromptRecents"] = [f"use {GH_TOKEN}"]
        fh.write_json(self.sa / f"{fh.S_PARENT}.json", rec)
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text(f'[remote "origin"]\n\turl = https://{GH_TOKEN}@github.com/me/repo.git\n')
        self.export("--only", "sessions,repos")
        m = members(self.bundle)
        blob = b"\n".join(m.values())
        self.assertNotIn(GH_TOKEN.encode(), blob)
        self.assertNotIn(b"remoteMcpServersConfig", blob)
        repos = json.loads(m["repos.json"])
        self.assertEqual(repos[0]["url"], "https://github.com/me/repo.git")

    # Important 5
    def test_agentmemory_with_company_text_excluded_even_without_company_sessions(self):
        for sid in (fh.S_PARENT, fh.S_CHILD):
            pass
        for p in list(self.sa.glob("local_*.json")):
            rec = json.loads(p.read_text())
            if rec["sessionId"] not in (fh.S_PARENT, fh.S_CHILD):
                p.unlink()
        (self.home / ".agentmemory" / "data" / "obs.jsonl").write_text(f'{{"note": "{CO} rollout"}}\n')
        self.export("--only", "memory")
        self.assertFalse(any(n.startswith("agentmemory/") for n in members(self.bundle)))
        (self.home / ".agentmemory" / "data" / "obs.jsonl").write_text('{"note": "personal"}\n')
        self.export("--only", "memory")
        self.assertTrue(any(n.startswith("agentmemory/") for n in members(self.bundle)))

    # Important 6
    def test_clone_recreates_worktree_on_its_branch(self):
        remote = self.tmp / "remote.git"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        main = self.home / "work" / "app"
        subprocess.run(["git", "clone", "-q", str(remote), str(main)], check=True, capture_output=True)
        (main / "f").write_text("x")
        subprocess.run(["git", "-C", str(main), "add", "."], check=True)
        subprocess.run(["git", "-C", str(main), "commit", "-q", "-m", "i"], check=True, env=env)
        subprocess.run(["git", "-C", str(main), "push", "-q", "origin", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(main), "branch", "feat/x"], check=True)
        subprocess.run(["git", "-C", str(main), "push", "-q", "origin", "feat/x"], check=True, capture_output=True)
        wt = self.home / "work" / "wt-x"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt), "feat/x"], check=True, capture_output=True)
        wt2 = self.home / "work" / "wt-gone"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "local/only", str(wt2)], check=True, capture_output=True)
        for sid, path, branch in (("local_aaaa0000-0000-4000-8000-00000000000a", wt, "feat/x"),
                                  ("local_aaaa0000-0000-4000-8000-00000000000b", wt2, "local/only")):
            fh.write_json(self.sa / f"{sid}.json", fh.session(self.home, sid, f"cli-{sid[-1]}", str(wt if path == wt else wt2), 2500,
                                                               originCwd=str(main), worktreePath=str(path), branch=branch))
            fh.transcript(self.home, str(path), f"cli-{sid[-1]}")
        self.export("--only", "sessions,repos")
        dst = self.other_home()
        code, out = self.apply_other(dst, "--clone")
        dwt = dst / "work" / "wt-x"
        head = subprocess.run(["git", "-C", str(dwt), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, "feat/x", out)
        self.assertTrue((dwt / ".git").is_file(), "worktree, not a separate clone")
        self.assertFalse((dst / "work" / "wt-gone").exists())
        self.assertIn("local/only", out)

    # Important 7
    def test_words_match_nfd_and_json_escaped_korean(self):
        term = "가나" + "다라"
        w = Words([term], [])
        self.assertTrue(w.hit(unicodedata.normalize("NFD", term)))
        self.assertTrue(w.hit(json.dumps({"x": term})))


if __name__ == "__main__":
    unittest.main()
