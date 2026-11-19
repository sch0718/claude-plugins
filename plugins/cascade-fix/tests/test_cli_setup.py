import contextlib
import io
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli  # noqa: E402
from cascade import cli_steps as s  # noqa: E402
from cascade import skill_scripts  # noqa: E402
from fake_cli import CODEX_BODY, install  # noqa: E402
from helpers import make_repo, run_cli  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SETUP_PENDING = "참고: 이 기기 설정 전이다."
ACP_PROFILE = '[profiles.acp]\nbase = "claude-only"\n"doc.judge_a" = { runner = "cursor", model = "composer-2.6" }\n'
CODEX_SCRIPT = skill_scripts.SKILLS["codex-review"]["scripts/run-review.sh"]


class CliSetupBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.home / ".claude" / "cascade-fix"
        self.repos = self.state / "repos.toml"
        self.env = {"HOME": str(self.home), "CASCADE_REPOS_TOML": "", "CASCADE_RUN_REVIEW": "", "CASCADE_CURSOR_REVIEW": "",
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_setup(self, *args, cwd=None, env=None):
        return run_cli(["setup", *args], cwd or self.tmp, env={**self.env, **(env or {})})

    def apply(self, name="claude-only", *extra, priority="save-claude"):
        p = self.run_setup("--apply", "--profile", name, "--priority", priority, *extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def record(self):
        return json.loads((self.state / "setup.json").read_text(encoding="utf-8"))

    def make_repo(self):
        return make_repo(self.tmp / "repo", {"calc.py": "def add(a, b):\n    return a + b\n"})

    def run_main(self, *argv, legacy=None, extra=()):
        """cli.main을 같은 프로세스에서 돌린다. 진짜 CLI·~/.codex·~/.claude에 닿지 않게 모두 임시 폴더로 돌린다."""
        missing = str(self.tmp / "no-such-cli")
        env = {"HOME": str(self.home), "CASCADE_REPOS_TOML": "", "CASCADE_CODEX": missing, "CASCADE_CURSOR_AGENT": missing,
               "CASCADE_RUN_REVIEW": "", "CASCADE_CURSOR_REVIEW": ""}
        skills = self.home / ".claude" / "skills"
        patches = [mock.patch.dict("os.environ", env), mock.patch.object(s, "USER_REPOS_TOML", self.repos),
                   mock.patch.object(s, "RUN_REVIEW", skills / "codex-review" / "scripts" / "run-review.sh"),
                   mock.patch.object(s, "CURSOR_REVIEW", skills / "cursor-review" / "scripts" / "run-review.sh"),
                   mock.patch.object(cli.setup, "availability", return_value={"codex": "없음", "cursor": "없음"}),
                   mock.patch.object(cli, "codex_model", return_value="")]
        if legacy is not None:
            patches.append(mock.patch.object(s, "LEGACY_REPOS_TOML", legacy))
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in [*patches, *extra, contextlib.redirect_stdout(out)]:
                stack.enter_context(patch)
            code = cli.main(list(argv))
        return code, out.getvalue()


class SetupDetectTest(CliSetupBase):
    def test_detect_json_on_fresh_machine(self):
        p = self.run_setup("--detect", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["env"], "none")
        self.assertEqual(out["options"]["save-claude"][0]["profile"], "claude-only")
        self.assertIsNone(out["repos_toml"]["default_profile"])
        self.assertEqual(out["repos_toml"]["path"], str(self.repos))
        self.assertIsNone(out["record"])

    def test_detect_text_lines_on_fresh_machine(self):
        p = self.run_setup("--detect")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        self.assertIn("환경: none (codex 없음, cursor 없음)", lines)
        self.assertIn(f"repos.toml: {self.repos}", lines)
        self.assertIn("현재 default_profile: 없음", lines)
        self.assertIn("추천(Claude 한도 아끼기): claude-only [측정됨]", lines)
        self.assertIn("추천(빨리 끝내기): claude-only", lines)
        self.assertTrue(any(line.startswith("스크립트 codex-review: 없음") for line in lines), p.stdout)
        self.assertFalse(any(line.startswith("지난 설정:") for line in lines))
        self.assertFalse(self.state.exists())

    def test_detect_shows_alternatives_and_last_record(self):
        self.state.mkdir(parents=True)
        (self.state / "setup.json").write_text(json.dumps(
            {"schema": 1, "setup_at": "2026-10-23T10:00:00+09:00", "profile": "claude-only", "table_version": 1}), encoding="utf-8")
        self.repos.write_text('default_profile = "local"\n', encoding="utf-8")
        p = self.run_setup("--detect")
        self.assertIn("현재 default_profile: local", p.stdout)
        self.assertIn("지난 설정: 2026-10-23T10:00:00+09:00 claude-only (표 v1)", p.stdout)

    def test_detect_shows_script_locations_and_last_backups(self):
        skills = self.home / ".claude" / "skills"
        backups = [str(self.state / "repos.toml.bak-20261023-100000"),
                   str(skills / "codex-review" / "scripts" / "run-review.sh.bak-20261023-100000")]
        self.state.mkdir(parents=True)
        (self.state / "setup.json").write_text(json.dumps(
            {"schema": 1, "setup_at": "2026-10-23T10:00:00+09:00", "profile": "claude-only", "table_version": 1,
             "backups": backups}), encoding="utf-8")
        lines = self.run_setup("--detect").stdout.splitlines()
        for name in ("codex-review", "cursor-review"):
            self.assertTrue(any(line.startswith(f"스크립트 {name}: 없음 ({skills / name}) — ") for line in lines), lines)
        last = lines.index("지난 설정: 2026-10-23T10:00:00+09:00 claude-only (표 v1)")
        self.assertEqual(lines[last + 1:last + 3], [f"  백업: {b}" for b in backups])
        out = json.loads(self.run_setup("--detect", "--json").stdout)
        self.assertEqual(out["script_dirs"], {name: str(skills / name) for name in ("codex-review", "cursor-review")})
        self.assertEqual(out["record"]["backups"], backups)

    def test_detect_does_not_write_anything(self):
        self.run_setup("--detect", "--json")
        self.assertEqual(list(self.home.rglob("*")), [])


class SetupApplyTest(CliSetupBase):
    def test_apply_dry_run_writes_nothing(self):
        p = self.run_setup("--apply", "--profile", "claude-only", "--priority", "save-claude", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('+default_profile = "claude-only"', p.stdout)
        self.assertIn("다음:", p.stdout)
        self.assertNotIn("--dry-run", p.stdout.split("다음:", 1)[1])
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_apply_creates_file_from_sample_and_record(self):
        self.apply()
        data = tomllib.loads(self.repos.read_text(encoding="utf-8"))
        self.assertEqual(data["default_profile"], "claude-only")
        self.assertNotIn("형식 견본이다", self.repos.read_text(encoding="utf-8"))
        rec = self.record()
        self.assertEqual((rec["schema"], rec["table_version"], rec["recommended"], rec["profile"]),
                         (1, 1, "claude-only", "claude-only"))
        self.assertEqual((rec["env"], rec["priority"], rec["kept_existing"]), ("none", "save-claude", False))
        self.assertEqual(rec["backups"], [])

    def test_apply_on_fresh_machine_does_not_offer_restore(self):
        p = self.apply()
        self.assertIn("백업: 없음", p.stdout)
        self.assertNotIn("--restore", p.stdout)
        self.assertIn("setup --detect", p.stdout.split("다음:", 1)[1])

    def test_apply_existing_file_is_edited_in_place_with_backup(self):
        self.state.mkdir(parents=True)
        self.repos.write_text('# 내 메모\ndefault_profile = "default"  # 손으로\n', encoding="utf-8")
        p = self.apply("claude-only")
        self.assertIn("setup --restore --dry-run", p.stdout.split("다음:", 1)[1])
        self.assertEqual(self.repos.read_text(encoding="utf-8"), '# 내 메모\ndefault_profile = "claude-only"  # 손으로\n')
        backups = sorted(self.state.glob("repos.toml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), '# 내 메모\ndefault_profile = "default"  # 손으로\n')
        self.assertEqual(self.record()["backups"], [str(backups[0])])

    def test_apply_refuses_with_env_var(self):
        other = self.tmp / "other.toml"
        other.write_text('default_profile = "default"\n', encoding="utf-8")
        p = self.run_setup("--apply", "--profile", "claude-only", "--priority", "save-claude",
                           env={"CASCADE_REPOS_TOML": str(other)})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("오류:", p.stdout)
        self.assertIn("CASCADE_REPOS_TOML", p.stdout)
        self.assertEqual(other.read_text(encoding="utf-8"), 'default_profile = "default"\n')
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_apply_keep_records_only(self):
        self.state.mkdir(parents=True)
        self.repos.write_text('default_profile = "local"\n', encoding="utf-8")
        p = self.run_setup("--apply", "--keep", "--priority", "save-claude")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.repos.read_text(encoding="utf-8"), 'default_profile = "local"\n')
        self.assertEqual(list(self.state.glob("repos.toml.bak-*")), [])
        rec = self.record()
        self.assertEqual((rec["profile"], rec["kept_existing"], rec["recommended"]), ("local", True, "claude-only"))

    def test_apply_keep_without_default_profile_records_null(self):
        p = self.run_setup("--apply", "--keep", "--priority", "fast")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.repos.exists())
        rec = self.record()
        self.assertIsNone(rec["profile"])
        self.assertTrue(rec["kept_existing"])
        self.assertEqual(rec["priority"], "fast")

    def test_apply_unknown_profile_exit_5(self):
        p = self.run_setup("--apply", "--profile", "no-such", "--priority", "save-claude")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("오류:", p.stdout)
        self.assertIn("no-such", p.stdout)
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_apply_accepts_user_defined_profile(self):
        self.state.mkdir(parents=True)
        self.repos.write_text('[profiles.mine]\nbase = "claude-only"\n', encoding="utf-8")
        self.apply("mine")
        self.assertEqual(tomllib.loads(self.repos.read_text(encoding="utf-8"))["default_profile"], "mine")

    def test_apply_needs_profile_or_keep_and_priority(self):
        for args in (("--apply", "--priority", "fast"), ("--apply", "--profile", "claude-only"),
                     ("--apply", "--keep", "--profile", "claude-only", "--priority", "fast")):
            p = self.run_setup(*args)
            self.assertEqual(p.returncode, 1, (args, p.stdout, p.stderr))
            self.assertIn("오류:", p.stdout)
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_actions_are_mutually_exclusive_and_required(self):
        self.assertEqual(self.run_setup().returncode, 2)
        self.assertEqual(self.run_setup("--detect", "--restore").returncode, 2)

    def test_apply_picks_up_matching_check_and_removes_it(self):
        self.state.mkdir(parents=True)
        check = {"profile": "local", "checked_at": "2026-10-23T09:00:00+09:00",
                 "results": {"codex-worker": {"status": "성공", "detail": ""}, "codex-review": {"status": "실패", "detail": "x"}}}
        (self.state / "setup-check.json").write_text(json.dumps(check), encoding="utf-8")
        self.apply("local")
        self.assertEqual(self.record()["checks"], {"codex-worker": "성공", "codex-review": "실패"})
        self.assertFalse((self.state / "setup-check.json").exists())

    def test_apply_leaves_check_of_another_profile(self):
        self.state.mkdir(parents=True)
        check = {"profile": "local", "checked_at": "x", "results": {"codex-worker": {"status": "성공", "detail": ""}}}
        (self.state / "setup-check.json").write_text(json.dumps(check), encoding="utf-8")
        self.apply("claude-only")
        self.assertEqual(self.record()["checks"], {})
        self.assertTrue((self.state / "setup-check.json").exists())


class SetupScriptsTest(CliSetupBase):
    def setUp(self):
        super().setUp()
        self.skill = self.home / ".claude" / "skills" / "codex-review"
        self.script = self.skill / "scripts" / "run-review.sh"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("#!/bin/sh\necho old\n", encoding="utf-8")

    def test_detect_reports_script_difference(self):
        p = self.run_setup("--detect")
        line = next(x for x in p.stdout.splitlines() if x.startswith("스크립트 codex-review:"))
        self.assertIn("다름", line)
        self.assertIn("scripts/run-review.sh: 다름(+", line)

    def test_apply_dry_run_summarizes_script_replacement_and_changes_nothing(self):
        p = self.run_setup("--apply", "--profile", "claude-only", "--priority", "save-claude",
                           "--replace-scripts", "codex", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("스크립트 교체 codex-review", p.stdout)
        self.assertEqual(self.script.read_text(encoding="utf-8"), "#!/bin/sh\necho old\n")
        self.assertFalse(self.repos.exists())

    def test_apply_replaces_script_with_backup_and_records_it(self):
        self.apply("claude-only", "--replace-scripts", "codex")
        self.assertEqual(self.script.read_bytes(), CODEX_SCRIPT.read_bytes())
        self.assertEqual(len(list(self.script.parent.glob("run-review.sh.bak-*"))), 1)
        self.assertTrue((self.skill / "SKILL.md").is_file())
        rec = self.record()
        self.assertEqual(rec["scripts"]["codex-review"], "교체함")
        self.assertEqual(rec["scripts"]["cursor-review"], "없음")
        self.assertEqual(len(rec["backups"]), 1)

    def test_apply_without_replace_keeps_script(self):
        self.apply("claude-only")
        self.assertEqual(self.script.read_text(encoding="utf-8"), "#!/bin/sh\necho old\n")
        self.assertEqual(self.record()["scripts"]["codex-review"], "다름")

    def test_replace_scripts_refuses_when_script_env_redirects(self):
        elsewhere = self.tmp / "elsewhere" / "scripts" / "run-review.sh"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text("#!/bin/sh\n", encoding="utf-8")
        for name in ("CASCADE_RUN_REVIEW", "CASCADE_CURSOR_REVIEW"):
            for extra in (("--dry-run",), ()):
                p = self.run_setup("--apply", "--profile", "claude-only", "--priority", "save-claude",
                                   "--replace-scripts", "codex", *extra, env={name: str(elsewhere)})
                self.assertEqual(p.returncode, 5, (name, extra, p.stdout + p.stderr))
                self.assertIn(f"오류: {name}", p.stdout)
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "#!/bin/sh\n")
        self.assertEqual(self.script.read_text(encoding="utf-8"), "#!/bin/sh\necho old\n")
        self.assertFalse(self.repos.exists())

    def test_apply_without_replace_scripts_ignores_script_env(self):
        p = self.run_setup("--apply", "--profile", "claude-only", "--priority", "save-claude",
                           env={"CASCADE_RUN_REVIEW": str(self.tmp / "elsewhere.sh")})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_install_failure_reports_what_was_written_and_how_to_undo(self):
        self.state.mkdir(parents=True)
        self.repos.write_text('default_profile = "default"\n', encoding="utf-8")
        with mock.patch.object(cli.skill_scripts, "install", side_effect=OSError("권한 없음")):
            code, out = self.run_main("setup", "--apply", "--profile", "claude-only", "--priority", "save-claude",
                                      "--replace-scripts", "codex")
        self.assertEqual(code, 1, out)
        self.assertIn("스크립트 설치에 실패했다: 권한 없음", out)
        self.assertIn(str(self.repos), out)
        self.assertIn("repos.toml.bak-", out)
        self.assertIn("setup --restore", out)
        self.assertEqual(tomllib.loads(self.repos.read_text(encoding="utf-8"))["default_profile"], "claude-only")
        self.assertFalse((self.state / "setup.json").exists())

    def test_install_failure_on_fresh_machine_does_not_offer_restore(self):
        with mock.patch.object(cli.skill_scripts, "install", side_effect=OSError("권한 없음")):
            code, out = self.run_main("setup", "--apply", "--profile", "claude-only", "--priority", "save-claude",
                                      "--replace-scripts", "codex")
        self.assertEqual(code, 1, out)
        self.assertIn(f"repos.toml {self.repos}를 새로 만들었다(백업 없음)", out)
        self.assertNotIn("--restore", out)

    def test_replace_scripts_rejects_unknown_runner(self):
        p = self.run_setup("--apply", "--profile", "claude-only", "--priority", "save-claude", "--replace-scripts", "claude")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("오류:", p.stdout)
        self.assertFalse(self.repos.exists())


class SetupLegacyCopyTest(CliSetupBase):
    def test_dry_run_shows_copy_of_legacy_file_and_apply_copies_it(self):
        legacy = self.tmp / "legacy.toml"
        legacy.write_text('# 예전\n[repos."/work/x"]\ntests = ["pytest"]\n', encoding="utf-8")
        argv = ["setup", "--apply", "--profile", "claude-only", "--priority", "save-claude"]
        code, text = self.run_main(*argv, "--dry-run", legacy=legacy)
        self.assertEqual(code, 0, text)
        self.assertIn(str(legacy), text)
        self.assertIn("복사", text)
        self.assertIn('+[repos."/work/x"]', text)
        self.assertIn('+default_profile = "claude-only"', text)
        self.assertFalse(self.repos.exists())
        code, text = self.run_main(*argv, legacy=legacy)
        self.assertEqual(code, 0, text)
        data = tomllib.loads(self.repos.read_text(encoding="utf-8"))
        self.assertEqual((data["default_profile"], data["repos"]["/work/x"]["tests"]), ("claude-only", ["pytest"]))
        self.assertEqual(legacy.read_text(encoding="utf-8"), '# 예전\n[repos."/work/x"]\ntests = ["pytest"]\n')


class SetupRepoProfileTest(CliSetupBase):
    def test_repo_profile_dry_run_writes_nothing(self):
        repo = self.make_repo()
        p = self.run_setup("--repo-profile", "claude-only", "--root", str(repo), "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('+profile = "claude-only"', p.stdout)
        self.assertIn("다음:", p.stdout)
        self.assertFalse(self.repos.exists())

    def test_repo_profile_then_init_still_needs_tests(self):
        repo = self.make_repo()
        p = self.run_setup("--repo-profile", "claude-only", "--root", str(repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = tomllib.loads(self.repos.read_text(encoding="utf-8"))
        self.assertEqual(data["repos"][str(repo)], {"profile": "claude-only"})
        init = run_cli(["init", "--source", "claude-code", "--input", str(FIXTURES / "codex-code-e2e.final")], repo,
                       env=self.env)
        self.assertEqual(init.returncode, 4, init.stdout + init.stderr)
        self.assertIn("오류:", init.stdout)
        self.assertIn("tests", init.stdout)

    def test_repo_profile_uses_resolved_root_for_existing_entry(self):
        repo = self.make_repo()
        self.state.mkdir(parents=True)
        self.repos.write_text(f'[repos."{repo}"]\ntests = ["pytest"]\n', encoding="utf-8")
        link = self.tmp / "link"
        link.symlink_to(repo)
        p = self.run_setup("--repo-profile", "claude-only", "--root", str(link))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = tomllib.loads(self.repos.read_text(encoding="utf-8"))
        self.assertEqual(data["repos"], {str(repo): {"profile": "claude-only", "tests": ["pytest"]}})

    def test_repo_profile_defaults_to_git_root_of_cwd(self):
        repo = self.make_repo()
        p = self.run_setup("--repo-profile", "claude-only", cwd=repo)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(str(repo.resolve()), tomllib.loads(self.repos.read_text(encoding="utf-8"))["repos"])

    def test_repo_profile_twice_says_nothing_to_change(self):
        repo = self.make_repo()
        self.run_setup("--repo-profile", "claude-only", "--root", str(repo))
        before = self.repos.read_text(encoding="utf-8")
        p = self.run_setup("--repo-profile", "claude-only", "--root", str(repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("바꿀 내용이 없다", p.stdout)
        self.assertNotIn("썼다", p.stdout)
        self.assertEqual(self.repos.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.state.glob("repos.toml.bak-*")), [])

    def test_repo_profile_unknown_name_exit_5(self):
        repo = self.make_repo()
        p = self.run_setup("--repo-profile", "no-such", "--root", str(repo))
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertFalse(self.repos.exists())

    def test_repo_profile_empty_name_exit_5_without_traceback(self):
        repo = self.make_repo()
        p = self.run_setup("--repo-profile", "", "--root", str(repo))
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertFalse(self.repos.exists())

    def test_repo_profile_refuses_with_env_var(self):
        repo = self.make_repo()
        p = self.run_setup("--repo-profile", "claude-only", "--root", str(repo),
                           env={"CASCADE_REPOS_TOML": str(self.tmp / "other.toml")})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertFalse((self.tmp / "other.toml").exists())


class SetupRestoreTest(CliSetupBase):
    def test_restore_round_trip(self):
        self.state.mkdir(parents=True)
        original = '# 처음\ndefault_profile = "default"\n'
        self.repos.write_text(original, encoding="utf-8")
        self.apply("claude-only")
        self.assertNotEqual(self.repos.read_text(encoding="utf-8"), original)
        dry = self.run_setup("--restore", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn('+default_profile = "default"', dry.stdout)
        self.assertIn("다음:", dry.stdout)
        self.assertNotEqual(self.repos.read_text(encoding="utf-8"), original)
        done = self.run_setup("--restore")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.repos.read_text(encoding="utf-8"), original)
        self.assertEqual(len(list(self.state.glob("repos.toml.bak-*"))), 2)

    def test_restore_without_backup_exit_5(self):
        self.state.mkdir(parents=True)
        self.repos.write_text('default_profile = "default"\n', encoding="utf-8")
        for extra in ((), ("--dry-run",)):
            p = self.run_setup("--restore", *extra)
            self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
            self.assertIn("되돌릴 백업이 없다", p.stdout)


class SetupCheckTest(CliSetupBase):
    def test_check_writes_state_and_fails_without_cli(self):
        p = self.run_setup("--check", "--profile", "local")
        self.assertEqual(p.returncode, 6, p.stdout + p.stderr)
        check = json.loads((self.state / "setup-check.json").read_text(encoding="utf-8"))
        self.assertEqual(check["profile"], "local")
        self.assertTrue(check["checked_at"])
        self.assertEqual(sorted(check["results"]), ["codex-review", "codex-worker"])
        self.assertEqual(check["results"]["codex-worker"]["status"], "실패")
        self.assertIn("시험 codex-worker: 실패", p.stdout)

    def test_check_json_output(self):
        p = self.run_setup("--check", "--profile", "local", "--json")
        out = json.loads(p.stdout)
        self.assertEqual(out["profile"], "local")
        self.assertEqual(out["results"]["codex-review"]["status"], "실패")

    def test_check_claude_only_has_nothing_to_test(self):
        p = self.run_setup("--check", "--profile", "claude-only")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        check = json.loads((self.state / "setup-check.json").read_text(encoding="utf-8"))
        self.assertEqual(check["results"], {})
        self.assertIn("다음:", p.stdout)

    def test_check_unknown_profile_exit_5(self):
        p = self.run_setup("--check", "--profile", "no-such")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)

    def test_check_rejects_dry_run(self):
        p = self.run_setup("--check", "--profile", "local", "--dry-run")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("--dry-run", p.stdout)
        self.assertFalse((self.state / "setup-check.json").exists())

    def test_check_after_apply_merges_results_into_record(self):
        self.apply("local")
        self.assertEqual(self.record()["checks"], {})
        p = self.run_setup("--check", "--profile", "local")
        self.assertEqual(p.returncode, 6, p.stdout + p.stderr)
        self.assertEqual(self.record()["checks"], {"codex-worker": "실패", "codex-review": "실패"})
        self.assertEqual(self.record()["profile"], "local")
        self.assertFalse((self.state / "setup-check.json").exists())

    def test_check_merged_into_record_removes_stale_check(self):
        self.apply("local")
        stale = {"profile": "local", "checked_at": "x", "results": {"codex-worker": {"status": "성공", "detail": ""}}}
        (self.state / "setup-check.json").write_text(json.dumps(stale), encoding="utf-8")
        self.run_setup("--check", "--profile", "local")
        self.assertFalse((self.state / "setup-check.json").exists())
        self.apply("local")
        self.assertEqual(self.record()["checks"], {})

    def test_check_without_matching_record_keeps_check_file(self):
        self.apply("claude-only")
        self.run_setup("--check", "--profile", "local")
        self.assertEqual(json.loads((self.state / "setup-check.json").read_text(encoding="utf-8"))["profile"], "local")

    def test_check_after_apply_ignores_record_of_another_profile(self):
        self.apply("claude-only")
        self.run_setup("--check", "--profile", "local")
        self.assertEqual(self.record()["checks"], {})

    def test_check_requires_profile(self):
        p = self.run_setup("--check")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def install_review_script(self, skill):
        script = self.home / ".claude" / "skills" / skill / "scripts" / "run-review.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)

    def check_default(self, codex, cursor):
        for skill in ("codex-review", "cursor-review"):
            self.install_review_script(skill)
        code, out = self.run_main("setup", "--check", "--profile", "default", "--json",
                                  extra=[mock.patch.object(cli, "codex_available", return_value=codex),
                                         mock.patch.object(cli, "cursor_available", return_value=cursor)])
        return code, json.loads(out)["results"]

    def test_check_default_resolves_auto_review_cells_to_codex(self):
        self.assertEqual(self.check_default(codex=True, cursor=True), (0, {"codex-review": {"status": "성공", "detail": ""}}))

    def test_check_default_resolves_auto_review_cells_to_cursor_without_codex(self):
        self.assertEqual(self.check_default(codex=False, cursor=True), (0, {"cursor-review": {"status": "성공", "detail": ""}}))

    def test_check_default_resolves_auto_review_cells_to_claude_without_usable_cli(self):
        self.assertEqual(self.check_default(codex=False, cursor=False), (0, {}))

    def test_check_runs_acp_check_for_cursor_read_roles(self):
        self.state.mkdir(parents=True)
        self.repos.write_text(ACP_PROFILE, encoding="utf-8")
        done = mock.Mock(returncode=0, stdout="합계: 6건, 막힘 6건, 누출 0건\n", stderr="", timed_out=False, missing=False)
        with mock.patch.object(cli, "_execute", return_value=done) as run:
            code, out = self.run_main("setup", "--check", "--profile", "acp",
                                      extra=[mock.patch.object(cli.setup, "run_check", return_value={"status": "성공", "detail": ""})])
        self.assertEqual(code, 0, out)
        self.assertEqual(run.call_args.args[0][1:], ["-m", "cascade.cursor_acp_check", "--model", "composer-2.6"])
        check = json.loads((self.state / "setup-check.json").read_text(encoding="utf-8"))
        self.assertEqual(check["results"]["cursor_acp"]["status"], "누출 0건")

    def test_check_acp_leak_fails_with_exit_6(self):
        self.state.mkdir(parents=True)
        self.repos.write_text(ACP_PROFILE, encoding="utf-8")
        done = mock.Mock(returncode=1, stdout="합계: 6건, 막힘 4건, 누출 2건\n", stderr="", timed_out=False, missing=False)
        with mock.patch.object(cli, "_execute", return_value=done):
            code, out = self.run_main("setup", "--check", "--profile", "acp",
                                      extra=[mock.patch.object(cli.setup, "run_check", return_value={"status": "성공", "detail": ""})])
        self.assertEqual(code, 6, out)
        self.assertIn("시험 cursor_acp: 누출 있음", out)


class DetectSetupNoticeTest(CliSetupBase):
    SCRIPT_NOTICE = "참고: codex-review 스크립트가 플러그인 판과 다르다. 「cascade-fix 설정해줘」로 바꿀 수 있다"

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        self.result = FIXTURES / "codex-code-e2e.final"

    def detect(self, *extra, env=None):
        p = run_cli(["detect", "--root", str(self.repo), "--no-gh", *extra], self.tmp, env={**self.env, **(env or {})})
        self.assertIn(p.returncode, (0, 6), p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("저장소:", p.stdout)
        self.assertIn("프로필:", p.stdout)
        return p

    def test_detect_prints_before_setup_notice(self):
        p = self.detect()
        self.assertIn(SETUP_PENDING, p.stdout)
        notes = json.loads(run_cli(["detect", "--root", str(self.repo), "--no-gh", "--json"], self.tmp, env=self.env).stdout)["notes"]
        self.assertTrue(any(n.startswith("이 기기 설정 전이다") for n in notes), notes)

    def test_detect_no_notice_after_apply(self):
        self.apply()
        p = self.detect()
        self.assertNotIn(SETUP_PENDING, p.stdout)
        self.assertNotIn("추천 표가 바뀌었다", p.stdout)

    def test_detect_no_notice_with_hand_written_default_profile(self):
        self.state.mkdir(parents=True)
        self.repos.write_text('default_profile = "claude-only"\n', encoding="utf-8")
        p = self.detect()
        self.assertNotIn(SETUP_PENDING, p.stdout)
        self.assertIn("프로필: claude-only (repos.toml:default_profile)", p.stdout)

    def test_detect_prints_table_changed_notice(self):
        self.state.mkdir(parents=True)
        (self.state / "setup.json").write_text(json.dumps(
            {"schema": 1, "table_version": 0, "env": "none", "priority": "save-claude", "recommended": "local",
             "profile": "default"}), encoding="utf-8")
        self.repos.write_text('default_profile = "default"\n', encoding="utf-8")
        p = self.detect()
        self.assertIn("참고: 추천 표가 바뀌었다(v0→v1): 이 기기 추천은 claude-only", p.stdout)

    def codex_env(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        codex = install(bin_dir, "codex", CODEX_BODY)
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
        return {"CASCADE_CODEX": str(codex)}

    def test_detect_script_notice_for_explicit_codex_profile_when_script_is_missing(self):
        env = self.codex_env()
        local = self.detect("--profile", "local", "--input", str(self.result), env=env)
        self.assertIn(self.SCRIPT_NOTICE, local.stdout)
        notes = json.loads(run_cli(["detect", "--root", str(self.repo), "--no-gh", "--json", "--profile", "local",
                                    "--input", str(self.result)], self.tmp, env={**self.env, **env}).stdout)["notes"]
        self.assertIn(self.SCRIPT_NOTICE.removeprefix("참고: "), notes)

    def test_detect_no_script_notice_when_codex_cli_is_missing(self):
        local = self.detect("--profile", "local", "--input", str(self.result))
        self.assertIn("대체: code.opinion codex → claude — 미설치 또는 로그인 필요", local.stdout)
        self.assertNotIn("스크립트가 플러그인 판과 다르다", local.stdout)
        self.state.mkdir(parents=True)
        self.repos.write_text('default_profile = "local"\n', encoding="utf-8")
        default = self.detect("--input", str(self.result))
        self.assertIn("프로필: local (repos.toml:default_profile)", default.stdout)
        self.assertNotIn("스크립트가 플러그인 판과 다르다", default.stdout)

    def test_detect_setup_notes_survive_os_error(self):
        code, out = self.run_main("detect", "--root", str(self.repo), "--no-gh", "--input", str(self.result),
                                  extra=[mock.patch.object(cli, "_script_states", side_effect=PermissionError(13, "권한 없음"))])
        self.assertIn(code, (0, 6), out)
        self.assertIn("프로필:", out)
        self.assertIn("참고: 설정 알림을 건너뛴다:", out)

    def test_detect_no_script_notice_for_default_and_claude_only_without_script(self):
        for name in ("default", "claude-only"):
            p = self.detect("--profile", name, "--input", str(self.result))
            self.assertNotIn("스크립트가 플러그인 판과 다르다", p.stdout, name)
        p = self.detect("--input", str(self.result), env={"CASCADE_REVIEWER": "", "CASCADE_WORKERS": ""})
        self.assertNotIn("스크립트가 플러그인 판과 다르다", p.stdout)

    def test_detect_script_notice_uses_roles_after_auto_resolution(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        codex = install(bin_dir, "codex", CODEX_BODY)
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
        script = self.home / ".claude" / "skills" / "codex-review" / "scripts" / "run-review.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        env = {"CASCADE_CODEX": str(codex), "CASCADE_WORKERS": "", "CASCADE_REVIEWER": ""}
        default = self.detect("--profile", "default", "--input", str(self.result), env=env)
        self.assertIn(self.SCRIPT_NOTICE, default.stdout)
        local = self.detect("--profile", "local", "--input", str(self.result), env=env)
        self.assertIn(self.SCRIPT_NOTICE, local.stdout)
        claude = self.detect("--profile", "claude-only", "--input", str(self.result), env=env)
        self.assertNotIn("스크립트가 플러그인 판과 다르다", claude.stdout)
        script.write_bytes(CODEX_SCRIPT.read_bytes())
        script.chmod(0o755)
        (script.parent.parent / "SKILL.md").write_bytes(skill_scripts.SKILLS["codex-review"]["SKILL.md"].read_bytes())
        same = self.detect("--profile", "local", "--input", str(self.result), env=env)
        self.assertNotIn("스크립트가 플러그인 판과 다르다", same.stdout)


if __name__ == "__main__":
    unittest.main()
