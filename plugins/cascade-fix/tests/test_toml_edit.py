import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import config, toml_edit  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = '# 머리 주석\n# 둘째 줄\n\n[profiles.mix]\nbase = "claude-only"\n\n[repos."/w/app"]\ntests = ["pytest {files}"]  # 주석\n'


class TomlEditTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_set_default_profile_inserts_before_first_table(self):
        out = toml_edit.set_default_profile(SAMPLE, "local")
        self.assertEqual(tomllib.loads(out)["default_profile"], "local")
        self.assertLess(out.index('default_profile = "local"'), out.index("[profiles.mix]"))
        self.assertIn("# 둘째 줄", out)
        self.assertIn("# 주석", out)

    def test_set_default_profile_stays_above_comment_block_of_first_table(self):
        text = '# 머리\n\n# 이 저장소는 로컬\n[repos."/x"]\nprofile = "local"\n'
        out = toml_edit.set_default_profile(text, "local")
        self.assertEqual(out, '# 머리\n\ndefault_profile = "local"\n\n# 이 저장소는 로컬\n[repos."/x"]\nprofile = "local"\n')

    def test_set_default_profile_keeps_multiline_string_ending_with_hash_line(self):
        text = 'repos."/x".tests = ["""echo ok\n# test script"""]\n[profiles.mix]\nbase = "claude-only"\n'
        out = toml_edit.set_default_profile(text, "local")
        self.assertEqual(out, 'repos."/x".tests = [\"\"\"echo ok\n# test script\"\"\"]\ndefault_profile = "local"\n\n[profiles.mix]\nbase = "claude-only"\n')

    def test_set_default_profile_replaces_existing_line(self):
        once = toml_edit.set_default_profile(SAMPLE, "local")
        twice = toml_edit.set_default_profile(once, "claude-only")
        self.assertEqual(twice.count("default_profile"), 1)
        self.assertEqual(tomllib.loads(twice)["default_profile"], "claude-only")

    def test_set_default_profile_keeps_trailing_comment(self):
        out = toml_edit.set_default_profile('default_profile = "a"  # 기본\n[repos]\n', "b")
        self.assertEqual(out, 'default_profile = "b"  # 기본\n[repos]\n')

    def test_set_default_profile_ignores_profile_line_inside_table(self):
        text = '[repos."/w/app"]\nprofile = "claude-only"\n'
        out = toml_edit.set_default_profile(text, "local")
        parsed = tomllib.loads(out)
        self.assertEqual(parsed["default_profile"], "local")
        self.assertEqual(parsed["repos"]["/w/app"]["profile"], "claude-only")
        self.assertLess(out.index("default_profile"), out.index("[repos"))

    def test_set_default_profile_on_comment_only_text(self):
        text = "# 머리\n# 둘째\n"
        out = toml_edit.set_default_profile(text, "local")
        self.assertTrue(out.startswith(text))
        self.assertEqual(tomllib.loads(out), {"default_profile": "local"})
        self.assertTrue(out.endswith('default_profile = "local"\n'))

    def test_set_default_profile_on_empty_text(self):
        self.assertEqual(toml_edit.set_default_profile("", "local"), 'default_profile = "local"\n')

    def test_set_repo_profile_adds_line_under_header(self):
        out = toml_edit.set_repo_profile(SAMPLE, Path("/w/app"), "claude-only")
        self.assertEqual(tomllib.loads(out)["repos"]["/w/app"], {"tests": ["pytest {files}"], "profile": "claude-only"})
        self.assertIn("# 주석", out)
        self.assertLess(out.index('[repos."/w/app"]'), out.index("profile = "))

    def test_set_repo_profile_replaces_existing_profile(self):
        text = '[repos."/w/app"]\ntests = ["pytest"]\nprofile = "local"  # 메모\n\n[repos."/w/other"]\nprofile = "keep"\n'
        out = toml_edit.set_repo_profile(text, Path("/w/app"), "claude-only")
        parsed = tomllib.loads(out)["repos"]
        self.assertEqual(parsed["/w/app"]["profile"], "claude-only")
        self.assertEqual(parsed["/w/other"]["profile"], "keep")
        self.assertIn('profile = "claude-only"  # 메모', out)
        self.assertEqual(out.count("profile ="), 2)

    def test_set_repo_profile_matches_tilde_key(self):
        home = self.tmp / "home"
        (home / "w" / "app").mkdir(parents=True)
        text = '[repos."~/w/app"]\ntests = ["pytest"]\n'
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            out = toml_edit.set_repo_profile(text, home / "w" / "app", "local")
        repos = tomllib.loads(out)["repos"]
        self.assertEqual(list(repos), ["~/w/app"])
        self.assertEqual(repos["~/w/app"], {"tests": ["pytest"], "profile": "local"})
        self.assertEqual(out.count("[repos."), 1)

    def test_set_repo_profile_matches_single_quoted_key(self):
        text = "[ repos.'/w/app' ]\ntests = [\"pytest\"]\n"
        out = toml_edit.set_repo_profile(text, Path("/w/app"), "local")
        self.assertEqual(tomllib.loads(out)["repos"]["/w/app"]["profile"], "local")
        self.assertEqual(out.count("[ repos."), 1)

    def test_set_repo_profile_creates_table_when_absent(self):
        out = toml_edit.set_repo_profile("# 머리\n", Path("/w/new"), "claude-only")
        self.assertEqual(tomllib.loads(out), {"repos": {"/w/new": {"profile": "claude-only"}}})
        self.assertTrue(out.startswith("# 머리\n"))
        self.assertIn('[repos."/w/new"]\nprofile = "claude-only"\n', out)

    def test_set_repo_profile_creates_table_on_empty_text_and_without_trailing_newline(self):
        self.assertEqual(toml_edit.set_repo_profile("", Path("/w/new"), "local"),
                         '[repos."/w/new"]\nprofile = "local"\n')
        out = toml_edit.set_repo_profile('[repos."/w/a"]\nprofile = "x"', Path("/w/new"), "local")
        self.assertEqual(tomllib.loads(out)["repos"]["/w/new"], {"profile": "local"})
        self.assertEqual(tomllib.loads(out)["repos"]["/w/a"], {"profile": "x"})

    def test_set_repo_profile_keeps_non_ascii_key(self):
        out = toml_edit.set_repo_profile("", Path("/w/앱"), "local")
        self.assertIn('[repos."/w/앱"]', out)
        again = toml_edit.set_repo_profile(out, Path("/w/앱"), "claude-only")
        self.assertEqual(again.count("[repos."), 1)
        self.assertEqual(tomllib.loads(again)["repos"]["/w/앱"]["profile"], "claude-only")

    def test_set_repo_profile_refuses_dotted_entry(self):
        text = 'repos."/w/app".tests = ["pytest"]\n'
        with self.assertRaises(toml_edit.EditError) as cm:
            toml_edit.set_repo_profile(text, Path("/w/app"), "claude-only")
        self.assertIn('profile = "claude-only"', str(cm.exception))

    def test_set_repo_profile_refuses_profile_line_inside_multiline_string(self):
        text = '[repos."/w/app"]\ntests = ["""python - <<X\nprofile = "production"\nX"""]\n'
        with self.assertRaises(toml_edit.EditError) as cm:
            toml_edit.set_repo_profile(text, Path("/w/app"), "claude-only")
        self.assertIn('profile = "claude-only"', str(cm.exception))

    def test_set_default_profile_refuses_line_inside_multiline_string(self):
        text = 'note = """\ndefault_profile = "x"\n"""\n'
        with self.assertRaises(toml_edit.EditError) as cm:
            toml_edit.set_default_profile(text, "local")
        self.assertIn('default_profile = "local"', str(cm.exception))

    def test_edit_error_is_config_error(self):
        self.assertTrue(issubclass(toml_edit.EditError, config.ConfigError))

    def test_locate_env_var_refuses(self):
        with self.assertRaises(toml_edit.EditError) as cm:
            toml_edit.locate({"CASCADE_REPOS_TOML": "/x/r.toml"}, self.tmp / "u.toml", self.tmp / "l.toml")
        self.assertEqual(str(cm.exception), "CASCADE_REPOS_TOML(/x/r.toml)이 잡혀 있어 기기 설정을 쓰지 않는다. 변수를 지우고 다시 하라")

    def test_locate_prefers_user_file(self):
        user, legacy = self.tmp / "u.toml", self.tmp / "l.toml"
        user.write_text("# 내 것\n", encoding="utf-8")
        legacy.write_text('[repos."/x"]\ntests = ["t"]\n', encoding="utf-8")
        self.assertEqual(toml_edit.locate({}, user, legacy), toml_edit.Target(user, None))

    def test_locate_copies_legacy_with_repos(self):
        user, legacy = self.tmp / "u.toml", self.tmp / "l.toml"
        legacy.write_text('[repos."/x"]\ntests = ["t"]\n', encoding="utf-8")
        self.assertEqual(toml_edit.locate({}, user, legacy), toml_edit.Target(user, legacy))

    def test_locate_ignores_legacy_without_repos(self):
        user = self.tmp / "u.toml"
        self.assertEqual(toml_edit.locate({}, user, SKILL_ROOT / "repos.toml"), toml_edit.Target(user, None))
        self.assertEqual(toml_edit.locate({}, user, self.tmp / "none.toml"), toml_edit.Target(user, None))

    def test_has_repos(self):
        path = self.tmp / "r.toml"
        self.assertFalse(config.has_repos(path))
        path.write_text("[repos]\n", encoding="utf-8")
        self.assertFalse(config.has_repos(path))
        path.write_text('[repos."/x"]\n', encoding="utf-8")
        self.assertTrue(config.has_repos(path))
        path.write_text("[repos\n", encoding="utf-8")
        self.assertTrue(config.has_repos(path))

    def test_base_text_new_file_uses_sample_header(self):
        target = toml_edit.Target(self.tmp / "u.toml", None)
        text = toml_edit.base_text(target, SKILL_ROOT / "repos.toml")
        sample = (SKILL_ROOT / "repos.toml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(sample.splitlines()[0]))
        self.assertTrue(all(not line.strip() or line.lstrip().startswith("#") for line in text.splitlines()))
        self.assertEqual(tomllib.loads(text), {})
        self.assertNotIn("\n[profiles.", text)
        self.assertNotIn("형식 견본이다", text)
        self.assertNotIn("예전처럼 이 파일에", text)
        self.assertLess(len(text), len(sample))

    def test_base_text_prefers_existing_then_legacy(self):
        user, legacy = self.tmp / "u.toml", self.tmp / "l.toml"
        legacy.write_text("# 예전\n", encoding="utf-8")
        self.assertEqual(toml_edit.base_text(toml_edit.Target(user, legacy), SKILL_ROOT / "repos.toml"), "# 예전\n")
        user.write_text("# 지금\n", encoding="utf-8")
        self.assertEqual(toml_edit.base_text(toml_edit.Target(user, legacy), SKILL_ROOT / "repos.toml"), "# 지금\n")
        self.assertEqual(toml_edit.base_text(toml_edit.Target(user, None), SKILL_ROOT / "repos.toml"), "# 지금\n")

    def test_diff(self):
        path = Path("/x/repos.toml")
        self.assertEqual(toml_edit.diff("a\n", "a\n", path), "")
        out = toml_edit.diff("a\n", "a\nb\n", path)
        self.assertIn("--- /x/repos.toml", out)
        self.assertIn("+++ /x/repos.toml", out)
        self.assertIn("+b", out)

    def test_write_checked_creates_parent_and_returns_none_without_original(self):
        path = self.tmp / "sub" / "dir" / "repos.toml"
        self.assertIsNone(toml_edit.write_checked(path, SAMPLE, "20261023-100000"))
        self.assertEqual(path.read_text(encoding="utf-8"), SAMPLE)
        self.assertEqual(toml_edit.backups(path), [])

    def test_write_checked_backs_up_and_prunes(self):
        path = self.tmp / "repos.toml"
        path.write_text("v0\n", encoding="utf-8")
        made = []
        for i in range(1, 7):
            made.append(toml_edit.write_checked(path, f"# v{i}\n", f"20261023-10000{i}"))
        found = toml_edit.backups(path)
        self.assertEqual(len(found), 5)
        self.assertEqual(found, list(reversed(made[1:])))
        self.assertFalse(made[0].exists())
        self.assertEqual(found[0].read_text(encoding="utf-8"), "# v5\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "# v6\n")

    def test_write_checked_same_stamp_keeps_both_backups(self):
        path = self.tmp / "repos.toml"
        path.write_text("v0\n", encoding="utf-8")
        a = toml_edit.write_checked(path, "# v1\n", "20261023-100000")
        b = toml_edit.write_checked(path, "# v2\n", "20261023-100000")
        self.assertNotEqual(a, b)
        self.assertEqual(toml_edit.backups(path), [b, a])
        self.assertEqual(b.read_text(encoding="utf-8"), "# v1\n")

    def test_write_checked_rolls_back_on_profile_error(self):
        path = self.tmp / "repos.toml"
        path.write_text(SAMPLE, encoding="utf-8")
        with self.assertRaises(toml_edit.EditError) as cm:
            toml_edit.write_checked(path, SAMPLE + "\n[profiles.claude-only]\n", "20261023-100000")
        self.assertIn("쓴 내용을 읽지 못해 되돌렸다", str(cm.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), SAMPLE)
        self.assertEqual(toml_edit.backups(path), [])

    def test_write_checked_rolls_back_on_broken_toml_and_removes_new_file(self):
        path = self.tmp / "repos.toml"
        with self.assertRaises(toml_edit.EditError):
            toml_edit.write_checked(path, "[repos\n", "20261023-100000")
        self.assertFalse(path.exists())

    def test_restore_latest_keeps_current_as_backup(self):
        path = self.tmp / "repos.toml"
        path.write_text("# v0\n", encoding="utf-8")
        toml_edit.write_checked(path, "# v1\n", "20261023-100001")
        used, kept = toml_edit.restore_latest(path, "20261023-100002")
        self.assertEqual(path.read_text(encoding="utf-8"), "# v0\n")
        self.assertEqual(used.read_text(encoding="utf-8"), "# v0\n")
        self.assertEqual(kept.read_text(encoding="utf-8"), "# v1\n")
        self.assertEqual(toml_edit.backups(path)[0], kept)

    def test_restore_latest_without_backup_refuses(self):
        path = self.tmp / "repos.toml"
        path.write_text("# v0\n", encoding="utf-8")
        with self.assertRaises(toml_edit.EditError):
            toml_edit.restore_latest(path, "20261023-100002")
        self.assertEqual(path.read_text(encoding="utf-8"), "# v0\n")

    def test_restore_latest_refuses_when_current_file_missing(self):
        path = self.tmp / "repos.toml"
        path.write_text("# v0\n", encoding="utf-8")
        toml_edit.write_checked(path, "# v1\n", "20261023-100001")
        path.unlink()
        with self.assertRaises(toml_edit.EditError):
            toml_edit.restore_latest(path, "20261023-100002")

    def test_backups_ignore_hand_made_files(self):
        path = self.tmp / "repos.toml"
        path.write_text("# v0\n", encoding="utf-8")
        mine = [self.tmp / f"repos.toml.bak-{name}" for name in ("mine", "zzz", "20261023-100000.orig", "20261023-100000-1")]
        for p in mine:
            p.write_text("# 손으로\n", encoding="utf-8")
        made = [toml_edit.write_checked(path, f"# v{i}\n", f"20261023-10000{i}") for i in range(1, 7)]
        self.assertEqual(toml_edit.backups(path), list(reversed(made[1:])))
        self.assertTrue(all(p.exists() for p in mine))
        used, _ = toml_edit.restore_latest(path, "20261023-100009")
        self.assertEqual(used, made[-1])
        self.assertEqual(path.read_text(encoding="utf-8"), "# v5\n")
        self.assertTrue(all(p.exists() for p in mine))

    def test_backups_empty_when_dir_missing(self):
        self.assertEqual(toml_edit.backups(self.tmp / "no" / "repos.toml"), [])


if __name__ == "__main__":
    unittest.main()
