import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import profiles  # noqa: E402


def write_toml(tmp, text):
    path = Path(tmp) / "repos.toml"
    path.write_text(text, encoding="utf-8")
    return path


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_builtins_match_design(self):
        d, co, lo = (profiles.builtin(n) for n in ("default", "claude-only", "local"))
        self.assertEqual(set(d["roles"]), set(profiles.ROLE_KEYS))
        self.assertEqual(d["roles"]["code.a"], {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low",
                                                "fallback": {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low"}})
        self.assertEqual(d["roles"]["code.opinion"]["runner"], "auto")
        self.assertEqual(d["roles"]["code.review"]["runner"], "auto")
        self.assertFalse(d["adopt_on_both_pass"])
        self.assertEqual(co["roles"]["code.opinion"], {"runner": "claude", "model": "claude-fable-5-2", "effort": "high",
                                                       "fallback": {"runner": "claude", "model": "claude-fable-5-2", "effort": "high"}})
        self.assertTrue(co["adopt_on_both_pass"])
        self.assertEqual(lo["roles"]["code.a"]["runner"], "codex")
        self.assertEqual(lo["roles"]["code.a"]["effort"], "low")
        self.assertEqual(lo["roles"]["code.b"]["model"], "claude-haiku-5-5")
        self.assertEqual(lo["roles"]["doc.judge_a"]["model"], "claude-sonnet-5-5")
        self.assertEqual(lo["roles"]["code.review"], {"runner": "codex", "model": "", "effort": "",
                                                      "fallback": {"runner": "claude", "model": "claude-fable-5-2", "effort": "high"}})
        for key in ("code.adjudicator", "doc.adjudicator"):
            self.assertEqual(lo["roles"][key]["model"], "claude-opus-5-5")

    def test_builtins_are_valid_and_independent_copies(self):
        self.assertEqual(profiles.ROLE_KEYS[0], "code.review")
        self.assertEqual(profiles.ROLE_KEYS[-1], "doc.recheck")
        self.assertEqual(len(profiles.ROLE_KEYS), 14)
        for name in profiles.BUILTIN_NAMES:
            profiles.validate(name, profiles.builtin(name))
        first = profiles.builtin("local")
        first["roles"]["code.a"]["fallback"]["model"] = "x"
        self.assertEqual(profiles.builtin("local")["roles"]["code.a"]["fallback"]["model"], "claude-sonnet-5-5")

    def test_local_external_cells_cover_review_and_code_a_only(self):
        lo = profiles.builtin("local")["roles"]
        external = {key for key, cell in lo.items() if cell["runner"] != "claude"}
        self.assertEqual(external, {"code.review", "code.a", "code.opinion", "code.recheck",
                                    "doc.review", "doc.opinion", "doc.recheck"})

    def test_allowed_runners(self):
        self.assertEqual(profiles.allowed_runners("code.adjudicator"), ("claude",))
        self.assertEqual(profiles.allowed_runners("code.a"), ("claude", "codex", "cursor"))
        self.assertEqual(profiles.allowed_runners("doc.recheck"), ("claude", "codex", "cursor", "auto"))

    def test_reset_cell_table(self):
        me = lambda cell: (cell["model"], cell["effort"])
        self.assertEqual(me(profiles.reset_cell("code.opinion", "codex")), ("", ""))
        self.assertEqual(me(profiles.reset_cell("code.a", "codex")), ("", "low"))
        self.assertEqual(me(profiles.reset_cell("code.b", "cursor", "composer-3")), ("composer-3", ""))
        self.assertEqual(me(profiles.reset_cell("code.opinion", "claude")), ("claude-fable-5-2", "high"))
        self.assertEqual(me(profiles.reset_cell("code.opinion", "auto")), ("claude-fable-5-2", "high"))

    def test_merge_runner_only_drops_old_model(self):
        base = profiles.builtin("claude-only")["roles"]["code.a"]
        merged = profiles.merge_cell("code.a", base, {"runner": "codex"})
        self.assertEqual((merged["runner"], merged["model"], merged["effort"]), ("codex", "", "low"))
        self.assertEqual(merged["fallback"], base["fallback"])

    def test_explicit_model_wins_over_reset(self):
        base = profiles.builtin("claude-only")["roles"]["code.opinion"]
        merged = profiles.merge_cell("code.opinion", base, {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low"})
        self.assertEqual((merged["model"], merged["effort"]), ("claude-sonnet-5-5", "low"))

    def test_merge_does_not_mutate_base(self):
        base = profiles.builtin("claude-only")["roles"]["code.a"]
        snapshot = copy.deepcopy(base)
        profiles.merge_cell("code.a", base, {"runner": "cursor", "fallback": {"runner": "codex"}})
        self.assertEqual(base, snapshot)

    def test_merge_without_runner_keeps_base_values(self):
        base = profiles.builtin("local")["roles"]["code.a"]
        merged = profiles.merge_cell("code.a", base, {"effort": "medium"})
        self.assertEqual((merged["runner"], merged["model"], merged["effort"]), ("codex", "", "medium"))

    def test_validate_rejects(self):
        cases = {
            "code.adjudicator": {"runner": "codex"},
            "code.a": {"runner": "auto"},
            "code.b": {"runner": "cursor", "model": "composer-2.5", "effort": "low"},
            "code.opinion": {"runner": "codex", "model": "gpt-6.1-sol"},
            "doc.checker": {"runner": "cursor", "model": "claude-sonnet-5-5"},
            "doc.writer": {"runner": "claude", "model": "gpt-6.1-sol"},
        }
        for key, override in cases.items():
            p = profiles.builtin("default")
            p["roles"][key] = profiles.merge_cell(key, p["roles"][key], override)
            with self.subTest(key=key), self.assertRaises(profiles.ProfileError):
                profiles.validate("x", p)

    def test_validate_rejects_more(self):
        def mutated(key, **fields):
            p = profiles.builtin("default")
            p["roles"][key].update(fields)
            return p

        def mutated_fallback(key, **fields):
            p = profiles.builtin("default")
            p["roles"][key]["fallback"].update(fields)
            return p

        cases = {
            "fallback auto": mutated_fallback("code.review", runner="auto"),
            "fallback judge codex": mutated_fallback("code.adjudicator", runner="codex", model="", effort=""),
            "fallback codex review with effort": mutated_fallback("code.recheck", runner="codex", model="", effort="high"),
            "bad effort": mutated("code.a", effort="turbo"),
            "claude empty model": mutated("code.a", model=""),
            "auto on cursor read role": mutated("doc.judge_a", runner="cursor", model="auto", effort=""),
            "gpt on cursor": mutated("code.b", runner="cursor", model="gpt-6.1-sol", effort=""),
            "codex worker empty effort": mutated("code.a", runner="codex", model="", effort=""),
            "codex worker fallback empty effort": mutated_fallback("code.b", runner="codex", model="", effort=""),
            "cursor worker empty model": mutated("doc.checker", runner="cursor", model="", effort=""),
            "cursor worker fallback empty model": mutated_fallback("code.a", runner="cursor", model="", effort=""),
            "adopt not bool": {**profiles.builtin("default"), "adopt_on_both_pass": "yes"},
        }
        for label, p in cases.items():
            with self.subTest(label), self.assertRaisesRegex(profiles.ProfileError, r"profiles\.x"):
                profiles.validate("x", p)

    def test_cursor_review_role_may_leave_model_empty(self):
        p = profiles.builtin("default")
        p["roles"]["code.opinion"] = {**p["roles"]["code.opinion"], "runner": "cursor", "model": "", "effort": ""}
        profiles.validate("x", p)

    def test_validate_message_names_profile_and_key(self):
        p = profiles.builtin("default")
        p["roles"]["code.adjudicator"]["runner"] = "codex"
        with self.assertRaisesRegex(profiles.ProfileError, r'profiles\.x 칸 "code\.adjudicator"'):
            profiles.validate("x", p)

    def test_load_merges_and_bases(self):
        path = write_toml(self.tmp, """
default_profile = "mine"
[profiles.local]
"code.b" = { runner = "cursor" }
[profiles.mine]
base = "local"
adopt_on_both_pass = false
"code.a" = { runner = "claude", model = "claude-haiku-5-5", effort = "low", fallback = { runner = "cursor", model = "composer-2.5" } }
""")
        found, data = profiles.load_profiles(path)
        self.assertEqual(found["local"]["roles"]["code.b"]["model"], "composer-2.5")
        self.assertEqual(found["mine"]["roles"]["code.b"]["runner"], "cursor")
        self.assertEqual(found["mine"]["roles"]["code.a"]["fallback"], {"runner": "cursor", "model": "composer-2.5", "effort": ""})
        self.assertFalse(found["mine"]["adopt_on_both_pass"])
        self.assertEqual(data["default_profile"], "mine")

    def test_load_without_file_returns_builtins(self):
        for path in (None, self.tmp / "missing.toml"):
            with self.subTest(path=path):
                found, data = profiles.load_profiles(path)
                self.assertEqual(tuple(found), profiles.BUILTIN_NAMES)
                self.assertEqual(data, {})

    def test_new_profile_starts_from_default(self):
        path = write_toml(self.tmp, '[profiles.mine]\n"code.b" = { runner = "cursor", model = "composer-2.5" }\n')
        found, _ = profiles.load_profiles(path)
        self.assertEqual(found["mine"]["roles"]["code.a"], profiles.builtin("default")["roles"]["code.a"])
        self.assertEqual(found["mine"]["roles"]["code.opinion"]["runner"], "auto")
        self.assertFalse(found["mine"]["adopt_on_both_pass"])

    def test_base_claude_only_is_allowed(self):
        path = write_toml(self.tmp, '[profiles.mine]\nbase = "claude-only"\n"code.b" = { runner = "cursor" }\n')
        found, _ = profiles.load_profiles(path)
        self.assertTrue(found["mine"]["adopt_on_both_pass"])
        self.assertEqual(found["mine"]["roles"]["code.b"]["model"], "composer-2.5")
        self.assertEqual(found["claude-only"], profiles.builtin("claude-only"))

    def test_claude_only_cannot_be_overridden(self):
        path = write_toml(self.tmp, '[profiles.claude-only]\n"code.a" = { runner = "codex" }\n')
        with self.assertRaisesRegex(profiles.ProfileError, "claude-only"):
            profiles.load_profiles(path)

    def test_base_errors(self):
        for text in ('[profiles.a]\nbase = "b"\n[profiles.b]\nbase = "a"\n', '[profiles.a]\nbase = "nope"\n',
                     '[profiles.a]\nbase = "a"\n', '[profiles.local]\nbase = "claude-only"\n'):
            with self.subTest(text=text), self.assertRaises(profiles.ProfileError):
                profiles.load_profiles(write_toml(self.tmp, text))

    def test_unknown_base_lists_known_names(self):
        path = write_toml(self.tmp, '[profiles.a]\nbase = "nope"\n')
        with self.assertRaisesRegex(profiles.ProfileError, r"profiles\.a.*default.*claude-only.*local"):
            profiles.load_profiles(path)

    def test_wrong_types_are_config_errors(self):
        for text in ('[profiles.a]\n"code.a" = "codex"\n', '[profiles.a]\n"code.a" = { runner = 1 }\n',
                     '[profiles.a]\n"code.z" = { runner = "claude" }\n', '[profiles.a]\n"code.a" = { color = "red" }\n'):
            with self.subTest(text=text), self.assertRaisesRegex(profiles.ProfileError, r"profiles\.a"):
                profiles.load_profiles(write_toml(self.tmp, text))

    def test_more_load_errors_name_profile(self):
        for text in ('[profiles.a]\nadopt_on_both_pass = "yes"\n',
                     '[profiles.a]\n"code.a" = { fallback = { runner = "auto" } }\n',
                     '[profiles.a]\n"code.a" = { fallback = { color = "red" } }\n',
                     '[profiles.a]\n"code.adjudicator" = { runner = "codex" }\n',
                     '[profiles.a]\n"code.b" = { runner = "cursor", effort = "low" }\n',
                     '[profiles.a]\ncode.a = { runner = "codex" }\n',
                     '[profiles.local]\n"code.review" = { model = "gpt-6.1-sol" }\n'):
            with self.subTest(text=text), self.assertRaisesRegex(profiles.ProfileError, r"profiles\.(a|local)"):
                profiles.load_profiles(write_toml(self.tmp, text))

    def test_auto_cells_must_hold_values_claude_can_use(self):
        for text in ('[profiles.a]\n"code.review" = { runner = "auto", model = "gpt-5" }\n',
                     '[profiles.a]\n"code.opinion" = { model = "composer-2.5" }\n',
                     '[profiles.a]\n"doc.recheck" = { runner = "auto", effort = "turbo" }\n'):
            with self.subTest(text=text), self.assertRaisesRegex(profiles.ProfileError, r"profiles\.a"):
                profiles.load_profiles(write_toml(self.tmp, text))
        found, _ = profiles.load_profiles(write_toml(
            self.tmp, '[profiles.a]\n"code.review" = { runner = "auto", model = "claude-sonnet-5-5", effort = "low" }\n'))
        self.assertEqual(found["a"]["roles"]["code.review"]["model"], "claude-sonnet-5-5")

    def test_explicit_cursor_fallback_model_is_kept(self):
        found, _ = profiles.load_profiles(write_toml(
            self.tmp, '[profiles.a]\n"code.a" = { runner = "codex", fallback = { runner = "cursor", model = "composer-9" } }\n'))
        self.assertEqual(found["a"]["roles"]["code.a"]["fallback"], {"runner": "cursor", "model": "composer-9", "effort": ""})

    def test_toml_syntax_error_is_profile_error(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.load_profiles(write_toml(self.tmp, "[profiles.a\n"))

    def test_profile_error_is_config_error(self):
        from cascade.config import ConfigError
        self.assertTrue(issubclass(profiles.ProfileError, ConfigError))


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.toml = write_toml(self.tmp, "")
        self.both = {"codex": True, "cursor": True}

    def resolve(self, toml_text=None, **kw):
        if toml_text is not None:
            write_toml(self.tmp, toml_text)
        args = {"flag": None, "cli": profiles.LegacyOptions(), "env": {}, "available": {}, "codex_model": ""}
        args.update(kw)
        return profiles.resolve(self.root, self.toml, **args)

    def test_select_order(self):
        known = ["default", "claude-only", "local", "mine"]
        sel = lambda **kw: profiles.select(**{"flag": None, "env": {}, "repo_profile": None, "default_profile": None, "known": known, **kw})
        self.assertEqual(sel(flag="local", env={"CASCADE_PROFILE": "mine"}), ("local", "--profile", True))
        self.assertEqual(sel(env={"CASCADE_PROFILE": "mine"}, repo_profile="local"), ("mine", "CASCADE_PROFILE", True))
        self.assertEqual(sel(repo_profile="local", default_profile="mine"), ("local", "repos.toml:repo", True))
        self.assertEqual(sel(default_profile="mine"), ("mine", "repos.toml:default_profile", True))
        self.assertEqual(sel(), ("default", "builtin", False))

    def test_empty_env_profile_is_not_chosen(self):
        known = ["default", "claude-only", "local"]
        self.assertEqual(profiles.select(flag=None, env={"CASCADE_PROFILE": ""}, repo_profile=None,
                                         default_profile=None, known=known), ("default", "builtin", False))

    def test_unknown_env_profile_lists_names(self):
        with self.assertRaisesRegex(profiles.ProfileError, "claude-only.*local"):
            profiles.select(flag=None, env={"CASCADE_PROFILE": "nope"}, repo_profile=None, default_profile=None,
                            known=["default", "claude-only", "local"])

    def test_unknown_flag_and_repo_profile_name_the_source(self):
        known = ["default", "claude-only", "local"]
        with self.assertRaisesRegex(profiles.ProfileError, r"--profile.*nope"):
            profiles.select(flag="nope", env={}, repo_profile=None, default_profile=None, known=known)
        with self.assertRaisesRegex(profiles.ProfileError, r"repos\.toml:repo.*nope"):
            profiles.select(flag=None, env={}, repo_profile="nope", default_profile=None, known=known)

    def test_workers_overrides(self):
        base = profiles.builtin("local")
        claude, notes = profiles.apply_overrides(base, profiles.LegacyOptions(workers="claude"), {}, "gpt-6.1-sol")
        self.assertEqual(claude["roles"]["code.a"], profiles.builtin("claude-only")["roles"]["code.a"])
        self.assertIn("code.a ← --workers claude", notes)
        self.assertEqual(base, profiles.builtin("local"))
        ext, _ = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(workers="external"),
                                          {"codex": True, "cursor": True}, "gpt-6.1-sol")
        self.assertEqual((ext["roles"]["code.a"]["runner"], ext["roles"]["doc.writer"]["runner"]), ("codex", "codex"))
        self.assertEqual({k: ext["roles"]["code.b"][k] for k in ("runner", "model", "effort")},
                         {"runner": "cursor", "model": "composer-2.5", "effort": ""})
        self.assertEqual(ext["roles"]["doc.checker"]["runner"], "cursor")
        self.assertEqual(ext["roles"]["doc.judge_a"]["runner"], "codex")
        self.assertEqual(ext["roles"]["code.a"]["effort"], "low")
        only_cursor, _ = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(workers="external"),
                                                  {"codex": False, "cursor": True}, "")
        self.assertEqual((only_cursor["roles"]["code.a"]["runner"], only_cursor["roles"]["code.b"]["runner"]), ("cursor", "claude"))
        only_codex, _ = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(workers="external"),
                                                 {"codex": True, "cursor": False}, "")
        self.assertEqual((only_codex["roles"]["code.a"]["runner"], only_codex["roles"]["code.b"]["runner"]), ("codex", "claude"))
        none, _ = profiles.apply_overrides(profiles.builtin("local"), profiles.LegacyOptions(workers="external"), {}, "")
        self.assertEqual(none["roles"]["code.a"], profiles.builtin("claude-only")["roles"]["code.a"])
        self.assertEqual(none["roles"]["code.b"], profiles.builtin("claude-only")["roles"]["code.b"])

    def test_cursor_model_overrides_only_cursor_cells(self):
        out, notes = profiles.apply_overrides(profiles.builtin("default"),
                                              profiles.LegacyOptions(workers="external", cursor_model="cursor-x"),
                                              {"codex": True, "cursor": True}, "")
        self.assertEqual(out["roles"]["code.b"]["model"], "cursor-x")
        self.assertEqual(out["roles"]["code.a"]["model"], "")
        self.assertIn("code.b ← --cursor-model cursor-x", notes)
        self.assertNotIn("code.a ← --cursor-model cursor-x", notes)

    def test_cursor_model_overrides_cursor_fallbacks_too(self):
        profile = profiles.builtin("local")
        profile["roles"]["code.a"]["fallback"] = {"runner": "cursor", "model": "composer-2.5", "effort": ""}
        out, notes = profiles.apply_overrides(profile, profiles.LegacyOptions(cursor_model="cursor-x"), {}, "")
        self.assertEqual(out["roles"]["code.a"]["fallback"], {"runner": "cursor", "model": "cursor-x", "effort": ""})
        self.assertEqual(out["roles"]["code.a"]["model"], "")
        self.assertIn("code.a ← --cursor-model cursor-x", notes)
        self.assertEqual(out["roles"]["code.b"], profile["roles"]["code.b"])

    def test_reviewer_overrides_reset_and_order(self):
        co = profiles.builtin("claude-only")
        out, _ = profiles.apply_overrides(co, profiles.LegacyOptions(reviewer="codex"), {}, "")
        self.assertEqual({k: out["roles"]["code.opinion"][k] for k in ("runner", "model", "effort")}, {"runner": "codex", "model": "", "effort": ""})
        self.assertEqual(out["roles"]["code.review"], co["roles"]["code.review"])
        self.assertEqual(out["roles"]["doc.recheck"]["runner"], "codex")
        out, _ = profiles.apply_overrides(profiles.builtin("local"),
                                          profiles.LegacyOptions(reviewer="claude", review_model="claude-sonnet-5-5"), {}, "")
        self.assertEqual(out["roles"]["doc.recheck"]["model"], "claude-sonnet-5-5")
        self.assertEqual(out["roles"]["doc.recheck"]["effort"], "high")

    def test_review_model_skips_non_claude_cells(self):
        out, _ = profiles.apply_overrides(profiles.builtin("local"),
                                          profiles.LegacyOptions(review_model="claude-sonnet-5-5", review_effort="low"), {}, "")
        self.assertEqual(out["roles"]["code.opinion"], profiles.builtin("local")["roles"]["code.opinion"])
        out, _ = profiles.apply_overrides(profiles.builtin("claude-only"),
                                          profiles.LegacyOptions(review_effort="low"), {}, "")
        self.assertEqual((out["roles"]["code.opinion"]["effort"], out["roles"]["code.recheck"]["effort"]), ("low", "low"))
        self.assertEqual(out["roles"]["code.review"]["effort"], "high")

    def test_adopt_on_both_pass_override(self):
        out, notes = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(adopt_on_both_pass=True), {}, "")
        self.assertTrue(out["adopt_on_both_pass"])
        self.assertTrue(any("adopt_on_both_pass" in n for n in notes))
        out, notes = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(), {}, "")
        self.assertFalse(out["adopt_on_both_pass"])
        self.assertEqual(notes, [])

    def test_overrides_are_validated(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.apply_overrides(profiles.builtin("default"),
                                     profiles.LegacyOptions(workers="external", cursor_model="claude-sonnet-5-5"),
                                     {"codex": False, "cursor": True}, "")

    def test_review_model_applies_to_auto_opinion_cell_of_default(self):
        out, notes = profiles.apply_overrides(profiles.builtin("default"),
                                              profiles.LegacyOptions(review_model="claude-opus-5-5", review_effort="low"), {}, "")
        for key in ("code.opinion", "code.recheck", "doc.opinion", "doc.recheck"):
            self.assertEqual((out["roles"][key]["runner"], out["roles"][key]["model"], out["roles"][key]["effort"]),
                             ("auto", "claude-opus-5-5", "low"))
        self.assertEqual(out["roles"]["code.review"], profiles.builtin("default")["roles"]["code.review"])
        self.assertIn("code.opinion ← --review-model claude-opus-5-5", notes)
        self.assertIn("doc.recheck ← --review-effort low", notes)

    def test_unchanged_cells_are_not_listed(self):
        _, notes = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(workers="claude"), {}, "")
        self.assertEqual(notes, [])
        _, notes = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(reviewer="auto"), {}, "")
        self.assertEqual(notes, [])
        _, notes = profiles.apply_overrides(profiles.builtin("default"), profiles.LegacyOptions(workers="external"),
                                            {"codex": True, "cursor": False}, "gpt-x")
        self.assertIn("code.a ← --workers external", notes)
        self.assertFalse(any(n.startswith(("code.b", "doc.judge_b", "doc.checker")) for n in notes))
        _, notes = profiles.apply_overrides(profiles.builtin("claude-only"), profiles.LegacyOptions(adopt_on_both_pass=True), {}, "")
        self.assertEqual(notes, [])

    def test_validation_error_names_the_legacy_option(self):
        with self.assertRaisesRegex(profiles.ProfileError, r"--cursor-model claude-sonnet-5-5"):
            profiles.apply_overrides(profiles.builtin("default"),
                                     profiles.LegacyOptions(workers="external", cursor_model="claude-sonnet-5-5"),
                                     {"codex": False, "cursor": True}, "")
        with self.assertRaisesRegex(profiles.ProfileError, r"--review-model gpt-x"):
            profiles.apply_overrides(profiles.builtin("claude-only"), profiles.LegacyOptions(review_model="gpt-x"), {}, "")
        with self.assertRaisesRegex(profiles.ProfileError, r"--reviewer gpt"):
            profiles.apply_overrides(profiles.builtin("claude-only"), profiles.LegacyOptions(reviewer="gpt"), {}, "")

    def test_unknown_workers_choice_names_the_option(self):
        with self.assertRaisesRegex(profiles.ProfileError, r"CASCADE_WORKERS=foo"):
            self.resolve(env={"CASCADE_WORKERS": "foo"})

    def test_env_derived_overrides_are_labelled_with_the_variable(self):
        r = self.resolve(env={"CASCADE_WORKERS": "external", "CASCADE_REVIEWER": "codex"}, available=self.both)
        self.assertIn("code.a ← CASCADE_WORKERS=external", r.overrides)
        self.assertIn("code.opinion ← CASCADE_REVIEWER=codex", r.overrides)
        self.assertFalse(any("--workers" in n or "--reviewer" in n for n in r.overrides))
        r = self.resolve(cli=profiles.LegacyOptions(workers="external"), env={"CASCADE_WORKERS": "claude"}, available=self.both)
        self.assertIn("code.a ← --workers external", r.overrides)

    def test_env_derived_override_error_names_the_variable(self):
        with self.assertRaisesRegex(profiles.ProfileError, r"CASCADE_REVIEWER=gpt"):
            self.resolve(env={"CASCADE_REVIEWER": "gpt"})

    def test_explain_overrides_only_touches_matching_cells(self):
        err = profiles.ProfileError("boom", cell="code.a")
        self.assertEqual(str(profiles.explain_overrides(err, ["code.b ← --x 1"])), "boom")
        self.assertIn("--workers external", str(profiles.explain_overrides(err, ["code.a ← --workers external"])))

    def test_resolve_env_rules(self):
        r = profiles.resolve(self.root, self.toml, flag=None, cli=profiles.LegacyOptions(),
                             env={"CASCADE_WORKERS": "external"}, available={"codex": True, "cursor": True}, codex_model="")
        self.assertEqual(r.roles["code.a"]["runner"], "codex")
        self.assertTrue(any("폐기 예정" in n for n in r.notices))
        r = profiles.resolve(self.root, self.toml, flag="claude-only", cli=profiles.LegacyOptions(),
                             env={"CASCADE_WORKERS": "external"}, available={"codex": True, "cursor": True}, codex_model="")
        self.assertEqual(r.roles["code.a"]["runner"], "claude")
        self.assertTrue(any("무시" in n for n in r.notices))

    def test_resolve_reads_env_reviewer_only_for_builtin_default(self):
        r = self.resolve(env={"CASCADE_REVIEWER": "codex"})
        self.assertEqual((r.name, r.source, r.explicit), ("default", "builtin", False))
        self.assertEqual(r.roles["code.opinion"]["runner"], "codex")
        self.assertEqual(r.roles["code.review"]["runner"], "auto")
        self.assertTrue(any("폐기 예정" in n for n in r.notices))
        r = self.resolve(flag="default", env={"CASCADE_REVIEWER": "codex"})
        self.assertEqual(r.roles["code.opinion"]["runner"], "auto")
        self.assertTrue(any("무시" in n for n in r.notices))

    def test_resolve_without_env_has_no_notices(self):
        r = self.resolve()
        self.assertEqual((r.name, r.source, r.explicit, r.notices, r.overrides), ("default", "builtin", False, [], []))
        self.assertEqual(r.roles, profiles.builtin("default")["roles"])
        self.assertFalse(r.adopt_on_both_pass)

    def test_cli_options_beat_env_and_work_on_explicit_profile(self):
        r = self.resolve(flag="local", cli=profiles.LegacyOptions(workers="claude"), env={"CASCADE_WORKERS": "external"},
                         available=self.both)
        self.assertEqual(r.roles["code.a"]["runner"], "claude")
        self.assertIn("code.a ← --workers claude", r.overrides)
        r = self.resolve(cli=profiles.LegacyOptions(workers="claude"), env={"CASCADE_WORKERS": "external"}, available=self.both)
        self.assertEqual(r.roles["code.a"]["runner"], "claude")
        self.assertFalse(any("폐기 예정" in n for n in r.notices))

    def test_resolve_reads_repo_profile_and_default_profile(self):
        toml = (f'default_profile = "mine"\n[profiles.mine]\nbase = "claude-only"\nadopt_on_both_pass = false\n'
                f'[profiles.other]\nbase = "local"\n[repos."{self.root}"]\ntests = ["x"]\nprofile = "other"\n')
        r = self.resolve(toml)
        self.assertEqual((r.name, r.source, r.explicit), ("other", "repos.toml:repo", True))
        self.assertEqual(r.roles["code.a"]["runner"], "codex")
        r = self.resolve(f'default_profile = "mine"\n[profiles.mine]\nbase = "claude-only"\nadopt_on_both_pass = false\n')
        self.assertEqual((r.name, r.source, r.explicit), ("mine", "repos.toml:default_profile", True))
        self.assertFalse(r.adopt_on_both_pass)
        r = self.resolve(toml, env={"CASCADE_PROFILE": "mine"})
        self.assertEqual((r.name, r.source), ("mine", "CASCADE_PROFILE"))

    def test_repo_profile_key_may_be_a_symlink_or_home_path(self):
        link = Path(self.tmp) / "link"
        link.symlink_to(self.root)
        toml = (f'default_profile = "local"\n[profiles.mine]\nbase = "claude-only"\n'
                f'[repos."{link}"]\ntests = ["x"]\nprofile = "mine"\n')
        self.assertEqual(self.resolve(toml).name, "mine")
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            toml = toml.replace(f'[repos."{link}"]', '[repos."~/link"]')
            self.assertEqual(self.resolve(toml).name, "mine")

    def test_resolve_unknown_names_are_profile_errors(self):
        with self.assertRaisesRegex(profiles.ProfileError, "nope"):
            self.resolve(flag="nope")
        with self.assertRaisesRegex(profiles.ProfileError, "repos.toml:default_profile"):
            self.resolve('default_profile = "ghost"\n')
        with self.assertRaisesRegex(profiles.ProfileError, "default_profile"):
            self.resolve("default_profile = 1\n")

    def test_resolve_adopt_flag(self):
        r = self.resolve(cli=profiles.LegacyOptions(adopt_on_both_pass=True))
        self.assertTrue(r.adopt_on_both_pass)
        r = self.resolve(flag="claude-only")
        self.assertTrue(r.adopt_on_both_pass)


if __name__ == "__main__":
    unittest.main()
