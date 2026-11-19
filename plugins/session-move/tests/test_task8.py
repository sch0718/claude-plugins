import contextlib
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import fakehome as fh

from session_move import cli
from session_move.environment import read_jsonc
from session_move.pathmap import PathMap, encode_project_dir, remap_schedule, remap_session, rewrite_jsonl

CO = "acme" + "-corp"          # company org (config)
TEAM = "blue" + "-squad"       # team name (denylist only)
S_CO = "local_44444444-4444-4444-8444-444444444444"
S_CO_CHILD = "local_55555555-5555-4555-8555-555555555555"
S_TEXT = "local_66666666-6666-4666-8666-666666666666"
S_CO_ARCH = "local_77777777-7777-4777-8777-777777777777"


def snapshot(*roots: Path) -> dict:
    out = {}
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if ".session-move" in p.parts:
                continue
            if p.is_symlink():
                out[str(p)] = "link:" + os.readlink(p)
            elif p.is_file():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def run_cli(args, env) -> tuple[int, str]:
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.main(args)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, buf.getvalue()


def members(bundle: Path) -> dict[str, bytes]:
    out = {}
    with tarfile.open(bundle, "r:gz") as tar:
        for m in tar.getmembers():
            out[m.name] = tar.extractfile(m).read() if m.isfile() else b"<" + m.type + b">"
    return out


class BundleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = fh.make_fake_home(self.tmp / "src")
        self.repo = self.home / "work" / "repo"
        self.sa = fh.scope_dir(self.home, fh.SCOPE_A)
        # company repo session + child, a personal-repo session that mentions the company, an archived company session
        corp = self.home / "work" / "corp-app"
        (corp / ".git").mkdir(parents=True)
        (corp / ".git" / "config").write_text(f'[remote "origin"]\n\turl = git@github.com:{CO}/corp-app.git\n')
        fh.write_json(self.sa / f"{S_CO}.json", fh.session(self.home, S_CO, "cli-co", str(corp), 3000,
                                                           postTurnSummary="fixed login", spawnSeed="go", prs=[{"n": 1}]))
        fh.write_json(self.sa / f"{S_CO_CHILD}.json", fh.session(self.home, S_CO_CHILD, "cli-co-child", str(corp), 2900, parent=S_CO))
        fh.write_json(self.sa / f"{S_TEXT}.json", fh.session(self.home, S_TEXT, "cli-text", str(self.repo), 2800,
                                                             scratchPromptRecents=[f"check {TEAM} notes"]))
        fh.write_json(self.sa / f"{S_CO_ARCH}.json", fh.session(self.home, S_CO_ARCH, "cli-co-arch", str(corp), 800, isArchived=True))
        idx = json.loads((self.sa / "archived-sessions.idx").read_text())
        idx["archived"].append(S_CO_ARCH)
        fh.write_json(self.sa / "archived-sessions.idx", idx)
        for cli_id, cwd in (("cli-co", corp), ("cli-co-child", corp), ("cli-text", self.repo), ("cli-co-arch", corp)):
            fh.transcript(self.home, str(cwd), cli_id)
        cfg = self.home / ".session-move" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(f'orgs = ["{CO}"]\n')
        deny = self.home / ".config" / "claude-plugins" / "denylist.txt"
        deny.parent.mkdir(parents=True)
        deny.write_text(f"{CO}\n{TEAM}\n")
        # environment
        claude = self.home / ".claude"
        (claude / "CLAUDE.md").write_text(f"# global\nwork at {CO}\n")
        fh.write_json(claude / "settings.json", {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                 "command": f"{self.home}/bin/notify.sh"}]}]}})
        (claude / "commands").mkdir()
        (claude / "commands" / "plain.md").write_text("plain command\n")
        (claude / "commands" / "team.md").write_text(f"ask {TEAM}\n")
        agents = self.home / ".agents" / "skills"
        for name, body in (("good", "generic skill\n"), ("teamy", f"for {TEAM}\n")):
            (agents / name).mkdir(parents=True)
            (agents / name / "SKILL.md").write_text(body)
        (claude / "skills").mkdir()
        (claude / "skills" / "good").symlink_to(agents / "good")
        big = claude / "skills" / "bigref"
        big.mkdir()
        (big / "SKILL.md").write_text("big\n")
        (big / "ref.md").write_text("x" * (2 * 1024 * 1024 - 30) + f" {CO}\n")
        binary = claude / "skills" / "binskill"
        binary.mkdir()
        (binary / "SKILL.md").write_text("bin\n")
        (binary / "blob.bin").write_bytes(b"\x00\xff\x00")
        (claude / "mcp.json").write_text(
            '{\n  // comment\n  "mcpServers": {"web": {"url": "https://example.com//x", '
            '"headers": {"Authorization": "Bearer abc", "X-Api-Key": "k"}, "env": {"TOKEN": "t"}}}\n  /* block */\n}\n')
        (claude / ".env").write_text("SECRET=1\n")
        am = self.home / ".agentmemory" / "data"
        am.mkdir(parents=True)
        (am / "db.bin").write_bytes(b"memory")
        # company scheduled task with generic body and company metadata
        sched = json.loads((self.sa / "scheduled-tasks.json").read_text())
        sched["scheduledTasks"] += [
            {"id": "corp-review", "displayName": "review", "cwd": str(corp), "enabled": True,
             "filePath": str(claude / "scheduled-tasks/corp-review/SKILL.md")},
            {"id": "named", "displayName": f"{TEAM} sync", "cwd": str(self.repo), "enabled": True,
             "filePath": str(claude / "scheduled-tasks/named/SKILL.md")},
            {"id": "perm", "displayName": "perm", "cwd": str(self.repo), "enabled": True,
             "approvedPermissions": [{"toolName": "Bash", "ruleContent": f"ls /{CO}"}],
             "filePath": str(claude / "scheduled-tasks/perm/SKILL.md")},
        ]
        fh.write_json(self.sa / "scheduled-tasks.json", sched)
        for tid in ("corp-review", "named", "perm"):
            p = claude / "scheduled-tasks" / tid / "SKILL.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("generic body\n")
        backlog = json.loads((self.sa / "backlog" / "tasks.json").read_text())
        backlog["items"] += [
            {"id": "b-linked", "title": "x", "prompt": "x", "cwd": str(self.repo), "filedAt": 1, "originSessionId": S_TEXT},
            {"id": "b-text", "title": "x", "prompt": f"ping {CO}", "cwd": str(self.repo), "filedAt": 1},
            {"id": "b-arch", "title": "x", "prompt": "x", "cwd": str(self.repo), "filedAt": 1, "originSessionId": S_CO_ARCH},
        ]
        fh.write_json(self.sa / "backlog" / "tasks.json", backlog)
        cfgp = fh.app_dir(self.home) / "claude_desktop_config.json"
        prefs = json.loads(cfgp.read_text())
        g = prefs["preferences"]["epitaxyPrefs"]["dframe-group-scopes"][fh.SCOPE_A]
        g["groups"].append({"id": "g3", "name": f"{TEAM} work"})
        g["assignments"][f"code:{S_CO}"] = "g1"
        prefs["preferences"]["epitaxyPrefs"]["dframe-code-sections"][fh.SCOPE_A]["sections"].append(
            {"id": "s2", "kind": "manual", "name": f"{CO} board"})
        prefs["preferences"]["epitaxyPrefs"]["starred-local-code-sessions"].append(S_CO)
        fh.write_json(cfgp, prefs)
        presets = json.loads((fh.app_dir(self.home) / "ccd-session-presets.json").read_text())
        presets["byAccount"][fh.ACC_A]["presets"].append({"id": "p2", "name": f"{CO} preset", "target": {"cwd": str(corp)}})
        fh.write_json(fh.app_dir(self.home) / "ccd-session-presets.json", presets)

        self.env = {"SESSION_MOVE_HOME": str(self.home), "SESSION_MOVE_FAKE_APP_RUNNING": "0",
                    "SESSION_MOVE_FAKE_AGENTMEMORY_RUNNING": "0"}
        self.bundle = self.tmp / "b.tar.gz"

    def export(self, *extra) -> tuple[int, str]:
        return run_cli(["export", "--source", fh.SCOPE_A, "--bundle", str(self.bundle), *extra], self.env)

    def other_home(self) -> Path:
        dst = self.tmp / "dst" / "home"
        os.makedirs(fh.scope_dir(dst, fh.SCOPE_B))
        return dst

    def apply_other(self, dst: Path, *extra) -> tuple[int, str]:
        env = dict(self.env, SESSION_MOVE_HOME=str(dst))
        return run_cli(["apply", "--bundle", str(self.bundle), "--map-path", f"{self.home}={dst}", *extra], env)


class PathMapTest(unittest.TestCase):
    def test_encode_project_dir_underscore_and_nonascii(self):
        self.assertEqual(encode_project_dir("/Users/a_b/x.y z/한글"), "-Users-a-b-x-y-z---")

    def test_map_path_rewrites_project_dir_and_lines(self):
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "in.jsonl"
        src.write_text("\n".join(json.dumps(x) for x in [
            {"type": "queue-operation"},
            {"type": "user", "cwd": "/Users/alice/w/repo", "note": "/Users/alice/w/repo"},
        ]) + "\n")
        pm = PathMap.parse(["/Users/alice=/Users/bob"])
        dst = tmp / "out.jsonl"
        rewrite_jsonl(src, dst, pm)
        lines = [json.loads(x) for x in dst.read_text().splitlines()]
        self.assertEqual(lines[1]["cwd"], "/Users/bob/w/repo")
        self.assertEqual(lines[1]["note"], "/Users/alice/w/repo")
        self.assertEqual(pm.map_project_dir(encode_project_dir("/Users/alice/w/repo")), encode_project_dir("/Users/bob/w/repo"))
        self.assertEqual(pm.apply("/Users/alicexyz/q"), "/Users/alicexyz/q")

    def test_remap_session_and_schedule_fields(self):
        pm = PathMap.parse(["/Users/alice=/Users/bob"])
        rec = {"cwd": "/Users/alice/r", "originCwd": "/Users/alice/r", "worktreePath": "/Users/alice/wt",
               "gitAnchorsFolderRealpath": "/Users/alice/r", "gitAnchors": [{"path": "/Users/alice/r"}],
               "shellFolders": ["/Users/alice/r"]}
        out = json.dumps(remap_session(rec, pm))
        self.assertNotIn("/Users/alice", out)
        sch = remap_schedule({"cwd": "/Users/alice/r", "filePath": "/Users/alice/.claude/x/SKILL.md"}, pm)
        self.assertNotIn("/Users/alice", json.dumps(sch))

    def test_file_status_handles_utf8_split_across_chunks(self):
        from session_move.environment import CHUNK, Words
        p = Path(tempfile.mkdtemp()) / "t.jsonl"
        head = b"a" * (CHUNK - 1) + "한".encode()          # 3-byte char straddles the chunk boundary
        p.write_bytes(head + ("가" * 5000).encode() + f" {CO}\n".encode())
        self.assertEqual(Words([CO], []).file_status(p), "hit")

    def test_uncheckable_transcript_excludes_session(self):
        from session_move.environment import Words
        p = Path(tempfile.mkdtemp()) / "t.jsonl"
        p.write_bytes(b'{"a": "\xff\xfe broken"}\n' + f'{{"b": "{CO}"}}\n'.encode())
        self.assertEqual(Words([CO], []).file_status(p), "hit")

    def test_read_jsonc_mcp_with_comments(self):
        p = Path(tempfile.mkdtemp()) / "m.json"
        p.write_text('{\n // c\n "a": "https://x.example.com//y", /* b */ "b": 1\n}\n')
        self.assertEqual(read_jsonc(p), {"a": "https://x.example.com//y", "b": 1})


class ExportTest(BundleCase):
    def test_bundle_export_refuses_without_company_config(self):
        (self.home / ".session-move" / "config.toml").unlink()
        code, out = self.export()
        self.assertEqual(code, 1)
        self.assertFalse(self.bundle.exists())
        code, out = self.export("--no-company-check")
        self.assertEqual(code, 0, out)
        self.assertIn("회사 판정 없이", out.splitlines()[0])

    def test_bundle_export_refuses_with_empty_company_config(self):
        cfg = self.home / ".session-move" / "config.toml"
        for text in ("", 'orgs = []\nrepos = []\npath_prefixes = []\n'):
            cfg.write_text(text)
            code, _ = self.export()
            self.assertEqual(code, 1)
            self.assertFalse(self.bundle.exists())

    def test_company_sessions_excluded_entirely_by_default(self):
        code, out = self.export("--skip", "memory")
        self.assertEqual(code, 0, out)
        m = members(self.bundle)
        blob = b"\n".join(list(map(str.encode, m)) + [v for k, v in m.items() if k != "claude/CLAUDE.md"])
        for needle in (S_CO, S_CO_CHILD, S_TEXT, "corp-app", "fixed login", CO, TEAM):
            self.assertNotIn(needle.encode(), blob, needle)
        self.assertIn(f"sessions/{fh.S_PARENT}.json", m)
        self.assertNotIn(S_CO.encode(), m["manifest.json"])
        code, _ = run_cli(["export", "--source", fh.SCOPE_A, "--bundle", str(self.tmp / "all.tar.gz"),
                           "--skip", "memory", "--include-company"], self.env)
        allm = members(self.tmp / "all.tar.gz")
        self.assertIn(f"sessions/{S_CO}.json", allm)
        self.assertIn(f"sessions/{S_TEXT}.json", allm)
        self.assertTrue(set(m) < set(allm))

    def test_personal_repo_session_with_company_text_excluded(self):
        code, out = self.export("--skip", "memory")
        self.assertNotIn(f"sessions/{S_TEXT}.json", members(self.bundle))
        self.assertIn("회사 내용 세션 1개 제외", out)

    def test_archived_company_sessions_excluded(self):
        self.export("--skip", "memory")
        m = members(self.bundle)
        self.assertNotIn(S_CO_ARCH.encode(), m["archived-ids.json"])
        self.assertIn(fh.S_ARCHIVED.encode(), m["archived-ids.json"])

    def test_company_agentmemory_excluded_by_default(self):
        self.export()
        self.assertFalse(any(n.startswith("agentmemory/") for n in members(self.bundle)))
        self.export("--only", "memory")
        self.assertFalse(any(n.startswith("agentmemory/") for n in members(self.bundle)))
        for sid in (S_CO, S_CO_CHILD, S_TEXT):
            (self.sa / f"{sid}.json").unlink()
        self.export("--only", "memory")
        self.assertFalse(any(n.startswith("agentmemory/") for n in members(self.bundle)), "archived company only")
        self.export("--only", "memory", "--include-company")
        self.assertTrue(any(n.startswith("agentmemory/") for n in members(self.bundle)))

    def test_agentmemory_requires_stopped_server(self):
        self.env["SESSION_MOVE_FAKE_AGENTMEMORY_RUNNING"] = "1"
        code, out = self.export("--only", "memory", "--include-company")
        self.assertFalse(any(n.startswith("agentmemory/") for n in members(self.bundle)))
        self.assertIn("agentmemory", out)

    def test_company_skills_commands_schedules_excluded(self):
        self.export("--skip", "memory")
        m = members(self.bundle)
        self.assertIn("agents-skills/good/SKILL.md", m)
        self.assertFalse(any(n.startswith("agents-skills/teamy") for n in m))
        self.assertIn("claude/commands/plain.md", m)
        self.assertNotIn("claude/commands/team.md", m)
        self.assertIn("claude/CLAUDE.md", m)

    def test_denylist_only_term_excludes_skill_command_schedule(self):
        self.export("--skip", "memory")
        m = members(self.bundle)
        sched = json.loads(m["scheduled-tasks.json"])["scheduledTasks"]
        self.assertNotIn("named", [t["id"] for t in sched])
        self.assertNotIn("scheduled-tasks/named/SKILL.md", m)

    def test_schedule_with_company_cwd_but_generic_body_excluded(self):
        self.export("--skip", "memory")
        m = members(self.bundle)
        ids = [t["id"] for t in json.loads(m["scheduled-tasks.json"])["scheduledTasks"]]
        self.assertNotIn("corp-review", ids)
        self.assertNotIn("scheduled-tasks/corp-review/SKILL.md", m)
        self.assertNotIn(b"corp-review", m["manifest.json"])
        self.assertIn("daily-note", ids)

    def test_metadata_names_only_company_excluded(self):
        self.export("--skip", "memory")
        man = json.loads(members(self.bundle)["manifest.json"])
        self.assertNotIn(f"{TEAM} work", [g["name"] for g in man["groups"]])
        self.assertNotIn(f"{CO} board", [s["name"] for s in man["sections"]])
        self.assertEqual([p["name"] for p in man["presets"]], ["repo preset"])
        self.assertNotIn("perm", [t["id"] for t in man["schedules"]])
        self.assertNotIn(S_CO, man["pinnedSessionIds"])

    def test_backlog_linked_to_excluded_session_or_company_text_excluded(self):
        self.export("--skip", "memory")
        m = members(self.bundle)
        ids = [i["id"] for i in json.loads(m["backlog/tasks.json"])["items"]]
        self.assertEqual(ids, ["b1"])

    def test_large_and_binary_files_checked_or_excluded(self):
        self.export("--skip", "memory")
        m = members(self.bundle)
        self.assertFalse(any(n.startswith("claude/skills/bigref") for n in m))
        self.assertFalse(any(n.startswith("claude/skills/binskill") for n in m))
        self.export("--skip", "memory", "--include-company")
        self.assertTrue(any(n.startswith("claude/skills/binskill") for n in members(self.bundle)))

    def test_company_warning_for_instructions(self):
        code, out = self.export("--skip", "memory")
        self.assertIn("CLAUDE.md", out)
        self.assertIn("회사 내용 포함", out)

    def test_secret_files_and_values_excluded(self):
        self.export("--skip", "memory")
        m = members(self.bundle)
        self.assertFalse(any(n.endswith(".env") for n in m))
        mcp = json.loads(m["mcp.json"])["mcpServers"]["web"]
        self.assertEqual(mcp["env"], {"TOKEN": ""})
        self.assertEqual(mcp["headers"], {"Authorization": "", "X-Api-Key": ""})
        self.assertEqual(mcp["url"], "https://example.com//x")


class ApplyBundleTest(BundleCase):
    def test_bundle_roundtrip_same_layout(self):
        self.export("--skip", "memory")
        dst = self.other_home()
        code, out = self.apply_other(dst)
        self.assertIn(code, (0, 2), out)
        tdir = fh.scope_dir(dst, fh.SCOPE_B)
        self.assertTrue((tdir / f"{fh.S_PARENT}.json").exists())
        rec = json.loads((tdir / f"{fh.S_PARENT}.json").read_text())
        self.assertEqual(rec["cwd"], str(dst / "work" / "repo"))
        folder = dst / ".claude" / "projects" / encode_project_dir(str(dst / "work" / "repo"))
        line = [json.loads(x) for x in (folder / "cli-parent.jsonl").read_text().splitlines()][-1]
        self.assertEqual(line["cwd"], str(dst / "work" / "repo"))

    def test_schedules_disabled_on_other_device(self):
        self.export("--skip", "memory")
        dst = self.other_home()
        self.apply_other(dst)
        tasks = json.loads((fh.scope_dir(dst, fh.SCOPE_B) / "scheduled-tasks.json").read_text())["scheduledTasks"]
        self.assertTrue(tasks)
        self.assertTrue(all(t["enabled"] is False for t in tasks))
        self.assertNotIn(str(self.home), json.dumps(tasks))

    def test_existing_config_not_overwritten(self):
        self.export("--skip", "memory")
        dst = self.other_home()
        (dst / ".claude").mkdir(parents=True)
        (dst / ".claude" / "CLAUDE.md").write_text("mine\n")
        self.apply_other(dst)
        self.assertEqual((dst / ".claude" / "CLAUDE.md").read_text(), "mine\n")
        self.assertTrue(list((dst / ".claude").glob("CLAUDE.md.from-*")))

    def test_skill_symlinks_remapped_to_new_home(self):
        self.export("--skip", "memory")
        dst = self.other_home()
        self.apply_other(dst)
        link = dst / ".claude" / "skills" / "good"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(dst / ".agents" / "skills" / "good"))
        self.assertEqual((link / "SKILL.md").read_text(), "generic skill\n")

    def test_backlog_bundle_preserves_body_and_merges(self):
        self.export("--skip", "memory")
        dst = self.other_home()
        fh.write_json(fh.scope_dir(dst, fh.SCOPE_B) / "backlog" / "tasks.json",
                      {"version": 1, "items": [{"id": "b1", "title": "older", "filedAt": 50}]})
        self.apply_other(dst)
        items = json.loads((fh.scope_dir(dst, fh.SCOPE_B) / "backlog" / "tasks.json").read_text())["items"]
        b1 = next(i for i in items if i["id"] == "b1")
        self.assertEqual((b1["title"], b1["prompt"], b1["cwd"]), ("idea one", "do one", str(dst / "work" / "repo")))

    def test_agentmemory_refuses_when_target_has_data(self):
        self.export("--only", "memory", "--include-company")
        dst = self.other_home()
        data = dst / ".agentmemory" / "data"
        data.mkdir(parents=True)
        (data / "mine.bin").write_bytes(b"x")
        code, out = self.apply_other(dst)
        self.assertEqual(sorted(p.name for p in data.iterdir()), ["mine.bin"])
        self.assertIn("agentmemory", out)

    def test_bundle_apply_restore_roundtrip_outside_account(self):
        self.export("--include-company")
        dst = self.other_home()
        (dst / ".claude" / "projects" / "keep").mkdir(parents=True)
        (dst / ".claude" / "projects" / "keep" / "k.jsonl").write_text("{}\n")
        (dst / ".claude" / "CLAUDE.md").write_text("mine\n")
        (dst / ".agentmemory" / "data").mkdir(parents=True)
        (dst / ".agentmemory" / "data" / "m.bin").write_bytes(b"m")
        before = snapshot(dst)
        code, out = self.apply_other(dst)
        self.assertNotEqual(snapshot(dst), before)
        backup = next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("백업: "))
        run_cli(["restore", backup], dict(self.env, SESSION_MOVE_HOME=str(dst)))
        self.assertEqual(snapshot(dst), before)

    def test_bundle_apply_restore_removes_new_agentmemory(self):
        self.export("--only", "memory", "--include-company")
        dst = self.other_home()
        before = snapshot(dst)
        code, out = self.apply_other(dst)
        self.assertTrue((dst / ".agentmemory" / "data" / "db.bin").exists(), out)
        backup = next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("백업: "))
        run_cli(["restore", backup], dict(self.env, SESSION_MOVE_HOME=str(dst)))
        self.assertEqual(snapshot(dst), before)
        self.assertFalse((dst / ".agentmemory").exists())

    def test_repos_clone_skips_missing_branch(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        seed = self.tmp / "seed"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "f").write_text("x")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "i"], check=True, env=env)
        subprocess.run(["git", "-C", str(seed), "push", "-q", str(remote), "main"], check=True)
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {remote}\n')
        rec = json.loads((self.sa / f"{fh.S_PARENT}.json").read_text())
        rec["branch"] = "feature/missing"
        fh.write_json(self.sa / f"{fh.S_PARENT}.json", rec)
        self.export("--only", "repos")
        dst = self.other_home()
        code, out = self.apply_other(dst, "--clone")
        self.assertTrue((dst / "work" / "repo" / "f").exists(), out)
        self.assertIn("feature/missing", out)
        self.assertIn("원격에 없음", out)


if __name__ == "__main__":
    unittest.main()
