import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import assignment, profiles  # noqa: E402
from cascade.rounds import Ledger  # noqa: E402

CLAUDE_ONLY = profiles.builtin("claude-only")["roles"]
CURSOR_FALLBACK = {"runner": "cursor", "model": "composer-2.5", "effort": ""}


def local_with_cursor_fallback():
    roles = profiles.builtin("local")["roles"]
    roles["code.a"]["fallback"] = dict(CURSOR_FALLBACK)
    return roles


class SubstituteUnavailableTest(unittest.TestCase):
    def test_local_without_codex(self):
        roles = profiles.builtin("local")["roles"]
        out, subs = assignment.substitute_unavailable(roles, {"codex": False, "cursor": True}, "미설치")
        self.assertEqual(out["code.a"], CLAUDE_ONLY["code.a"])
        self.assertIn({"role": "code.a", "from": "codex", "to": "claude", "reason": "미설치"}, subs)
        self.assertEqual(assignment.consent_line(out, "code"), "")
        self.assertEqual(roles, profiles.builtin("local")["roles"])

    def test_unavailable_runner_takes_its_fallback(self):
        roles = local_with_cursor_fallback()
        out, subs = assignment.substitute_unavailable(roles, {"codex": False, "cursor": True}, "미설치")
        self.assertEqual({k: out["code.a"][k] for k in ("runner", "model", "effort")}, CURSOR_FALLBACK)
        self.assertEqual(out["code.a"]["fallback"], CLAUDE_ONLY["code.a"]["fallback"])
        self.assertIn({"role": "code.a", "from": "codex", "to": "cursor", "reason": "미설치"}, subs)

    def test_unavailable_fallback_too_falls_to_builtin_claude_only(self):
        roles = local_with_cursor_fallback()
        out, subs = assignment.substitute_unavailable(roles, {"codex": False, "cursor": False}, "미설치")
        self.assertEqual(out["code.a"], CLAUDE_ONLY["code.a"])
        self.assertIn({"role": "code.a", "from": "codex", "to": "claude", "reason": "미설치"}, subs)

    def test_unavailable_fallback_of_usable_cell_is_dropped(self):
        roles = local_with_cursor_fallback()
        out, subs = assignment.substitute_unavailable(roles, {"codex": True, "cursor": False}, "미설치")
        self.assertEqual(out["code.a"]["runner"], "codex")
        self.assertEqual(out["code.a"]["fallback"], CLAUDE_ONLY["code.a"]["fallback"])
        self.assertIn({"role": "code.a(대체)", "from": "cursor", "to": "claude", "reason": "미설치"}, subs)

    def test_missing_review_script_substitutes_opinion_and_recheck_only(self):
        roles = profiles.builtin("local")["roles"]
        out, subs = assignment.substitute_unavailable(roles, {"codex": True, "cursor": True}, "미설치",
                                                      scripts={"codex": False, "cursor": True})
        for key in ("code.opinion", "code.recheck", "doc.opinion", "doc.recheck"):
            self.assertEqual(out[key], CLAUDE_ONLY[key], key)
            self.assertIn({"role": key, "from": "codex", "to": "claude", "reason": assignment.NO_SCRIPT_REASON}, subs)
        self.assertEqual((out["code.a"], out["code.review"]), (roles["code.a"], roles["code.review"]))
        self.assertEqual(len(subs), 4)

    def test_everything_available_changes_nothing(self):
        roles = profiles.builtin("local")["roles"]
        out, subs = assignment.substitute_unavailable(roles, {"codex": True, "cursor": True}, "미설치")
        self.assertEqual((out, subs), (roles, []))


class ConsentTest(unittest.TestCase):
    def test_consent_line_includes_fallback(self):
        line = assignment.consent_line(local_with_cursor_fallback(), "code")
        self.assertEqual(line, "전송 동의: 첫 검토·A·의견·재검토 → OpenAI / 대체 실행 때 A → Cursor(팀 프라이버시 모드)")

    def test_consent_line_empty_without_external(self):
        self.assertEqual(assignment.consent_line(CLAUDE_ONLY, "code"), "")
        self.assertEqual(assignment.consent_line(CLAUDE_ONLY, "doc"), "")

    def test_consent_line_is_per_mode(self):
        roles = profiles.builtin("local")["roles"]
        self.assertIn("A", assignment.consent_line(roles, "code"))
        self.assertEqual(assignment.consent_line(roles, "doc"), "전송 동의: 첫 검토·의견·재검토 → OpenAI")

    def test_consent_line_lists_both_recipients(self):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.a"] = profiles.merge_cell("code.a", roles["code.a"], {"runner": "codex"})
        roles["code.b"] = profiles.merge_cell("code.b", roles["code.b"], {"runner": "cursor"})
        self.assertEqual(assignment.consent_line(roles, "code"),
                         "전송 동의: A → OpenAI / B → Cursor(팀 프라이버시 모드)")

    def test_consent_line_fallback_same_recipient_is_listed(self):
        roles = profiles.builtin("local")["roles"]
        roles["code.a"]["fallback"] = {"runner": "codex", "model": "gpt-other", "effort": "low"}
        self.assertIn("대체 실행 때 A → OpenAI", assignment.consent_line(roles, "code"))

    def test_recipients(self):
        got = assignment.recipients(local_with_cursor_fallback(), "code")
        self.assertEqual(got, {"codex": ["code.review", "code.a", "code.opinion", "code.recheck"],
                               "cursor": ["code.a(대체)"]})
        self.assertEqual(assignment.recipients(CLAUDE_ONLY, "code"), {"codex": [], "cursor": []})

    def test_partial_consent(self):
        roles = local_with_cursor_fallback()
        out, dropped = assignment.apply_consent(roles, {"cursor"})
        self.assertEqual(out["code.a"], CLAUDE_ONLY["code.a"])
        self.assertEqual(out["code.opinion"], CLAUDE_ONLY["code.opinion"])
        self.assertIn({"role": "code.a", "from": "codex", "to": "claude", "reason": "전송 동의 없음"}, dropped)
        out, _ = assignment.apply_consent(roles, set())
        self.assertTrue(all(c["runner"] == "claude" and c["fallback"]["runner"] == "claude" for c in out.values()))

    def test_partial_consent_replaces_only_the_fallback(self):
        roles = local_with_cursor_fallback()
        out, dropped = assignment.apply_consent(roles, {"codex"})
        self.assertEqual(out["code.a"]["runner"], "codex")
        self.assertEqual(out["code.a"]["fallback"], CLAUDE_ONLY["code.a"]["fallback"])
        self.assertEqual(dropped, [{"role": "code.a(대체)", "from": "cursor", "to": "claude", "reason": "전송 동의 없음"}])

    def test_full_consent_changes_nothing(self):
        roles = local_with_cursor_fallback()
        out, dropped = assignment.apply_consent(roles, {"codex", "cursor"})
        self.assertEqual((out, dropped), (roles, []))

    def test_refuse_openai_with_user_profile_falls_to_builtin(self):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.a"] = profiles.merge_cell("code.a", roles["code.a"], {"runner": "codex"})
        out, _ = assignment.apply_consent(roles, set())
        self.assertEqual(out["code.a"]["runner"], "claude")


class ModeWorkersTest(unittest.TestCase):
    def test_mode_workers_and_legacy_fields(self):
        roles = profiles.builtin("local")["roles"]
        self.assertEqual(set(assignment.mode_workers(roles, "doc")), {"a", "b", "writer", "checker"})
        self.assertEqual(set(assignment.mode_workers(roles, "code")), {"a", "b"})
        self.assertEqual(assignment.mode_workers(roles, "code")["a"]["runner"], "codex")
        self.assertEqual(assignment.mode_workers(roles, "doc")["a"], roles["doc.judge_a"])
        self.assertEqual(assignment.mode_workers(roles, "doc")["checker"], roles["doc.checker"])
        rec = {"roles": roles}
        legacy = assignment.legacy_fields(rec | {"adopt_on_both_pass": True}, "code")
        self.assertEqual((legacy["reviewer"], legacy["workers_choice"]), ("codex", "external"))
        self.assertTrue(legacy["adopt_on_both_pass"])
        self.assertEqual((legacy["review_model"], legacy["review_effort"]), ("", ""))
        self.assertEqual(legacy["workers"]["a"]["runner"], "codex")

    def test_legacy_fields_claude_reviewer_and_default_workers(self):
        roles = profiles.builtin("claude-only")["roles"]
        legacy = assignment.legacy_fields({"roles": roles}, "doc")
        self.assertEqual(legacy["reviewer"], "claude")
        self.assertEqual((legacy["review_model"], legacy["review_effort"]), ("claude-fable-5-1", "high"))
        self.assertEqual((legacy["workers_choice"], legacy["workers"]), ("claude", {}))
        self.assertFalse(legacy["adopt_on_both_pass"])

    def test_legacy_fields_keeps_customised_claude_workers(self):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.b"] = profiles.merge_cell("code.b", roles["code.b"], {"model": "claude-sonnet-5-5"})
        legacy = assignment.legacy_fields({"roles": roles}, "code")
        self.assertEqual(legacy["workers_choice"], "claude")
        self.assertEqual(legacy["workers"]["b"]["model"], "claude-sonnet-5-5")

    def test_legacy_fields_external_requested_without_cli_keeps_dict(self):
        roles = profiles.builtin("default")["roles"]
        legacy = assignment.legacy_fields({"roles": roles}, "code", requested_workers="external")
        self.assertEqual(legacy["workers_choice"], "external")
        self.assertEqual({w["runner"] for w in legacy["workers"].values()}, {"claude"})


class ResolveAutoTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.script = Path(self._td.name) / "run-review.sh"
        self.script.write_text("#!/bin/sh\n", encoding="utf-8")
        self.missing = Path(self._td.name) / "none.sh"

    def test_claude_source_resolves_to_claude(self):
        out = assignment.resolve_auto(profiles.builtin("default")["roles"], "claude-code", self.script, True)
        self.assertEqual(out["code.opinion"], CLAUDE_ONLY["code.opinion"])
        self.assertFalse(any(c["runner"] == "auto" for c in out.values()))

    def test_first_review_auto_is_always_claude(self):
        out = assignment.resolve_auto(profiles.builtin("default")["roles"], "codex-code", self.script, True)
        self.assertEqual(out["code.review"], CLAUDE_ONLY["code.review"])
        self.assertEqual(out["doc.review"], CLAUDE_ONLY["doc.review"])

    def test_codex_script_resolves_review_cells_to_codex_with_reset(self):
        roles = profiles.builtin("default")["roles"]
        roles["code.opinion"] = {**roles["code.opinion"], "model": "claude-opus-5-5", "effort": "max"}
        out = assignment.resolve_auto(roles, "codex-code", self.script, False)
        for key in ("code.opinion", "code.recheck", "doc.opinion", "doc.recheck"):
            self.assertEqual({k: out[key][k] for k in ("runner", "model", "effort")},
                             {"runner": "codex", "model": "", "effort": ""})
            self.assertEqual(out[key]["fallback"], CLAUDE_ONLY[key]["fallback"])

    def test_cursor_resolves_with_cursor_model_and_no_effort(self):
        out = assignment.resolve_auto(profiles.builtin("default")["roles"], "codex-code", self.missing, True,
                                      cursor_model="composer-9")
        self.assertEqual({k: out["code.opinion"][k] for k in ("runner", "model", "effort")},
                         {"runner": "cursor", "model": "composer-9", "effort": ""})

    def test_nothing_available_resolves_to_claude(self):
        out = assignment.resolve_auto(profiles.builtin("default")["roles"], "codex-code", self.missing, False)
        self.assertEqual(out["code.opinion"], CLAUDE_ONLY["code.opinion"])

    def test_cursor_ok_may_be_callable(self):
        out = assignment.resolve_auto(profiles.builtin("default")["roles"], "codex-code", self.missing, lambda: True)
        self.assertEqual(out["code.opinion"]["runner"], "cursor")

    def test_claude_resolution_keeps_cell_model_and_validates(self):
        roles = profiles.builtin("default")["roles"]
        roles["code.opinion"] = {**roles["code.opinion"], "model": "claude-opus-5-5", "effort": "low"}
        out = assignment.resolve_auto(roles, "claude-code", self.missing, False)
        self.assertEqual((out["code.opinion"]["model"], out["code.opinion"]["effort"]), ("claude-opus-5-5", "low"))
        roles["code.opinion"] = {**roles["code.opinion"], "model": "composer-2.5"}
        with self.assertRaises(profiles.ProfileError) as ctx:
            assignment.resolve_auto(roles, "claude-code", self.missing, False)
        self.assertEqual(ctx.exception.cell, "code.opinion")

    def test_claude_resolution_fills_empty_model_and_effort_from_claude_only(self):
        roles = profiles.builtin("default")["roles"]
        for key in ("code.opinion", "code.review"):
            roles[key] = {**roles[key], "model": "", "effort": ""}
        out = assignment.resolve_auto(roles, "claude-code", self.missing, False)
        claude_only = profiles.builtin("claude-only")["roles"]
        for key in ("code.opinion", "code.review"):
            self.assertEqual((out[key]["runner"], out[key]["model"], out[key]["effort"]),
                             ("claude", claude_only[key]["model"], claude_only[key]["effort"]))

    def test_non_auto_cells_are_untouched(self):
        roles = profiles.builtin("local")["roles"]
        self.assertEqual(assignment.resolve_auto(roles, "claude-code", self.missing, False), roles)


class OverridesLineTest(unittest.TestCase):
    def test_groups_cells_by_option(self):
        notes = ["code.a ← --workers external", "doc.writer ← --workers external",
                 "code.opinion ← CASCADE_REVIEWER=codex", "adopt_on_both_pass ← --adopt-on-both-pass"]
        self.assertEqual(assignment.overrides_line(notes),
                         "덮음: code.a·doc.writer ← --workers external / code.opinion ← CASCADE_REVIEWER=codex"
                         " / adopt_on_both_pass ← --adopt-on-both-pass")
        self.assertEqual(assignment.overrides_line([]), "")


class PinnedCellsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.toml = self.tmp / "repos.toml"
        self.toml.write_text('[profiles.mine]\nbase = "claude-only"\n"code.b" = { runner = "cursor" }\n', encoding="utf-8")
        self.none = self.tmp / "none.sh"

    def resolve(self, flag="mine", **cli):
        return profiles.resolve(self.tmp, self.toml, flag=flag, cli=profiles.LegacyOptions(**cli), env={},
                                available={"codex": True, "cursor": True}, codex_model="")

    def decide(self, resolved, available):
        return assignment.decide(resolved.roles, resolved.pinned, "codex-code", self.none, False, available)

    def test_model_only_option_does_not_disable_availability_check(self):
        for opts in ({}, {"cursor_model": "composer-9"}, {"review_model": "claude-opus-5-5"},
                     {"review_effort": "low"}, {"adopt_on_both_pass": True}):
            resolved = self.resolve(**opts)
            self.assertEqual(resolved.pinned, [], opts)
            self.assertEqual(assignment.runners_to_check(resolved.roles, resolved.pinned), {"cursor"}, opts)
            roles, subs = self.decide(resolved, {"cursor": False})
            self.assertEqual(roles["code.b"]["runner"], "claude", opts)
            self.assertEqual([r["role"] for r in subs], ["code.b"], opts)
            self.assertEqual(assignment.consent_line(roles, "code"), "", opts)

    def test_runner_options_pin_their_cells_even_when_unchanged(self):
        reviewer = self.resolve(flag="local", reviewer="codex")
        self.assertEqual(reviewer.overrides, [])
        self.assertEqual(reviewer.pinned, ["code.opinion", "code.recheck", "doc.opinion", "doc.recheck"])
        workers = self.resolve(flag="local", workers="claude")
        self.assertEqual(workers.pinned, ["code.a", "code.b", "doc.judge_a", "doc.judge_b", "doc.writer", "doc.checker"])
        self.assertEqual(self.resolve().pinned, [])

    def test_local_and_default_treat_reviewer_codex_the_same(self):
        for flag in ("local", "default"):
            resolved = self.resolve(flag=flag, reviewer="codex")
            roles, subs = self.decide(resolved, {"codex": False, "cursor": False})
            self.assertEqual((roles["code.opinion"]["runner"], roles["code.recheck"]["runner"]), ("codex", "codex"), flag)
            self.assertNotIn("code.opinion", [r["role"] for r in subs], flag)
            self.assertIn("의견·재검토 → OpenAI", assignment.consent_line(roles, "code"), flag)
        local, subs = self.decide(self.resolve(flag="local", reviewer="codex"), {"codex": False, "cursor": False})
        self.assertEqual(local["code.a"], CLAUDE_ONLY["code.a"])
        self.assertIn("code.a", [r["role"] for r in subs])


    def test_missing_script_leaves_pinned_review_cells(self):
        scripts = {"codex": False, "cursor": False}
        both = {"codex": True, "cursor": True}
        pinned = self.resolve(flag="local", reviewer="codex")
        roles, subs = assignment.decide(pinned.roles, pinned.pinned, "codex-code", self.none, False, both, scripts=scripts)
        self.assertEqual((roles["code.opinion"]["runner"], subs), ("codex", []))
        free = self.resolve(flag="local")
        roles, subs = assignment.decide(free.roles, free.pinned, "codex-code", self.none, False, both, scripts=scripts)
        self.assertEqual(roles["code.opinion"], CLAUDE_ONLY["code.opinion"])
        self.assertEqual({r["reason"] for r in subs}, {assignment.NO_SCRIPT_REASON})


class RolesLineTest(unittest.TestCase):
    def test_code_and_doc_lines(self):
        roles = profiles.builtin("local")["roles"]
        self.assertEqual(
            assignment.roles_line(roles, "code"),
            "역할: 첫 검토=Codex, A=Codex(low), B=Claude(Haiku 5.5 low), 판가름=Claude(Opus 5.5 high),"
            " 의견=Codex, 재검토=Codex")
        doc = assignment.roles_line(roles, "doc")
        self.assertTrue(doc.startswith("역할: 첫 검토=Codex, 판정자 a=Claude(Sonnet 5.5 low), 판정자 b=Claude(Haiku 5.5 low), "
                                       "작성자=Claude(Sonnet 5.5 low), 점검자=Claude(Haiku 5.5 low), 판가름="))
        self.assertTrue(doc.endswith("의견=Codex, 재검토=Codex"))

    def test_cursor_label_has_model(self):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.b"] = profiles.merge_cell("code.b", roles["code.b"], {"runner": "cursor"})
        self.assertIn("B=Cursor(composer-2.5)", assignment.roles_line(roles, "code"))


class FillCodexModelTest(unittest.TestCase):
    def test_only_worker_cells_get_the_codex_model(self):
        roles = local_with_cursor_fallback()
        roles["code.b"]["fallback"] = {"runner": "codex", "model": "", "effort": "low"}
        out = assignment.fill_codex_model(roles, "gpt-test")
        self.assertEqual(out["code.a"]["model"], "gpt-test")
        self.assertEqual(out["code.b"]["fallback"]["model"], "gpt-test")
        self.assertEqual(out["code.opinion"]["model"], "")
        self.assertEqual(roles["code.a"]["model"], "")
        self.assertEqual(assignment.fill_codex_model(roles, ""), roles)


class LedgerProfileTest(unittest.TestCase):
    def ledger(self, **kw):
        return Ledger(run_id="r", source="codex-code", mode="code", root="/x", snapshot={}, dry_run=True, **kw)

    def test_old_ledger_without_profile(self):
        ledger = self.ledger(reviewer="claude", review_model="claude-fable-5-1", review_effort="high")
        rec = assignment.ledger_profile(ledger)
        self.assertEqual(rec["name"], "legacy")
        self.assertEqual(rec["roles"]["code.opinion"]["model"], "claude-fable-5-1")
        self.assertEqual(rec["roles"]["code.a"]["model"], "claude-sonnet-5-5")
        profiles.validate("legacy", {"adopt_on_both_pass": False, "roles": rec["roles"]})

    def test_old_ledger_with_codex_reviewer_and_external_workers(self):
        workers = {"a": {"runner": "codex", "model": "gpt-x", "effort": "low"},
                   "b": {"runner": "cursor", "model": "composer-2.5", "effort": "low"}}
        ledger = self.ledger(reviewer="codex", workers_choice="external", workers=workers, adopt_on_both_pass=True)
        rec = assignment.ledger_profile(ledger)
        roles = rec["roles"]
        self.assertEqual((roles["code.opinion"]["runner"], roles["code.recheck"]["runner"]), ("codex", "codex"))
        self.assertEqual((roles["code.opinion"]["model"], roles["code.opinion"]["effort"]), ("", ""))
        self.assertEqual((roles["code.a"]["runner"], roles["code.a"]["model"]), ("codex", "gpt-x"))
        self.assertEqual(roles["code.b"]["runner"], "cursor")
        self.assertEqual(roles["code.a"]["fallback"], CLAUDE_ONLY["code.a"]["fallback"])
        self.assertEqual(rec["consented"], ["codex", "cursor"])

    def test_old_doc_ledger_maps_worker_seats(self):
        workers = {r: {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low"} for r in ("a", "b", "writer", "checker")}
        ledger = Ledger(run_id="r", source="codex-doc", mode="doc", root="/x", snapshot={}, dry_run=True, workers=workers)
        rec = assignment.ledger_profile(ledger)
        self.assertEqual(rec["roles"]["doc.writer"]["model"], "claude-haiku-5-5")
        self.assertEqual(rec["roles"]["doc.judge_a"]["model"], "claude-haiku-5-5")

    def test_ledger_with_profile_is_returned_as_is(self):
        record = {"name": "local", "roles": {}}
        self.assertIs(assignment.ledger_profile(self.ledger(profile=record)), record)

    def test_ledger_profile_field_roundtrips(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.json"
            self.ledger(profile={"name": "local"}).save(path)
            self.assertEqual(Ledger.load(path).profile, {"name": "local"})
            self.assertEqual(self.ledger().profile, {})


class FallbackTargetTest(unittest.TestCase):
    CODEX_THEN_CURSOR = {"runner": "codex", "model": "gpt-x", "effort": "low", "fallback": dict(CURSOR_FALLBACK)}
    CODEX_THEN_CODEX = {"runner": "codex", "model": "gpt-x", "effort": "low",
                        "fallback": {"runner": "codex", "model": "gpt-y", "effort": "low"}}
    CODEX_THEN_HAIKU = {"runner": "codex", "model": "gpt-x", "effort": "low",
                        "fallback": {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low"}}
    CLAUDE_A = {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low"}

    def target(self, cell, consented=("codex", "cursor"), exhausted=(), role="a"):
        return assignment.fallback_target(cell, set(consented), set(exhausted), role)

    def test_consented_external_fallback_is_kept(self):
        out = self.target(self.CODEX_THEN_CURSOR)
        self.assertEqual({k: out[k] for k in ("runner", "model", "effort")}, CURSOR_FALLBACK)
        self.assertEqual(out["fallback"], self.CLAUDE_A)

    def test_fallback_outside_consent_goes_claude_only(self):
        self.assertEqual(self.target(self.CODEX_THEN_CURSOR, consented={"codex"}),
                         {**self.CLAUDE_A, "fallback": self.CLAUDE_A})

    def test_exhausted_fallback_runner_goes_claude_only(self):
        self.assertEqual(self.target(self.CODEX_THEN_CURSOR, exhausted={"cursor"})["runner"], "claude")
        out = self.target(self.CODEX_THEN_CODEX, exhausted={"codex"}, role="b")
        self.assertEqual((out["runner"], out["model"]), ("claude", "claude-haiku-5-5"))
        self.assertEqual(self.target(self.CODEX_THEN_CODEX)["model"], "gpt-y")

    def test_claude_fallback_is_used_even_when_runners_are_exhausted(self):
        out = self.target(self.CODEX_THEN_HAIKU, consented=(), exhausted={"codex", "cursor"})
        self.assertEqual((out["runner"], out["model"]), ("claude", "claude-haiku-5-5"))

    def test_cell_without_fallback_is_claude_only(self):
        legacy = {"runner": "codex", "model": "", "effort": "low"}
        self.assertEqual(self.target(legacy, role="writer")["model"], "claude-sonnet-5-5")

    def test_claude_fallback_picks_claude_cell_or_claude_only(self):
        self.assertEqual(assignment.claude_fallback(self.CODEX_THEN_HAIKU, "a")["model"], "claude-haiku-5-5")
        self.assertEqual(assignment.claude_fallback(self.CODEX_THEN_CURSOR, "b")["model"], "claude-haiku-5-5")
        self.assertEqual(assignment.claude_fallback({}, "a")["model"], "claude-sonnet-5-5")


class JobWorkersTest(unittest.TestCase):
    def ledger(self, roles, **kw):
        record = {"name": "x", "source": "builtin", "explicit": True, "roles": roles, "overrides": [], "substituted": [],
                  "consented": ["codex"], "first_review": {}}
        return Ledger(run_id="r", source="codex-code", mode="code", root="/x", snapshot={}, dry_run=True, profile=record, **kw)

    def test_all_default_claude_cells_give_none(self):
        self.assertIsNone(assignment.job_workers(self.ledger(profiles.builtin("claude-only")["roles"])))

    def test_custom_claude_cell_keeps_workers(self):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.a"] = profiles.merge_cell("code.a", roles["code.a"], {"model": "claude-haiku-5-5", "effort": "medium"})
        out = assignment.job_workers(self.ledger(roles))
        self.assertEqual((out["a"]["model"], out["a"]["effort"], set(out)), ("claude-haiku-5-5", "medium", {"a", "b"}))

    def test_external_cell_keeps_workers(self):
        out = assignment.job_workers(self.ledger(profiles.builtin("local")["roles"]))
        self.assertEqual((out["a"]["runner"], out["b"]["runner"]), ("codex", "claude"))

    def test_old_ledger_without_profile_uses_its_workers(self):
        workers = {"a": {"runner": "codex", "model": "gpt-x", "effort": "low"},
                   "b": {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low"}}
        ledger = Ledger(run_id="r", source="codex-code", mode="code", root="/x", snapshot={}, dry_run=True, workers=workers)
        self.assertEqual(assignment.job_workers(ledger)["a"]["model"], "gpt-x")
        self.assertIsNone(assignment.job_workers(Ledger(run_id="r", source="codex-code", mode="code", root="/x",
                                                        snapshot={}, dry_run=True)))


class SandboxSubstitutionTest(unittest.TestCase):
    def ledger(self, **kw):
        return Ledger(run_id="r", source="codex-code", mode="code", root="/x", snapshot={}, dry_run=True, **kw)

    def test_records_changed_cells_and_trims_consent(self):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.a"] = profiles.merge_cell("code.a", roles["code.a"], {"runner": "codex", "fallback": {"runner": "cursor"}})
        record = {"name": "mine", "source": "builtin", "explicit": True, "roles": roles, "overrides": [], "substituted": [],
                  "consented": ["codex", "cursor"], "first_review": {}}
        ledger = self.ledger(profile=record)
        before = assignment.mode_workers(roles, "code")
        after = {**before, "a": assignment.fallback_target(before["a"], {"codex", "cursor"}, {"codex"}, "a")}
        assignment.apply_sandbox_substitution(ledger, before, after, "샌드박스 점검 실패")
        self.assertEqual(ledger.profile["roles"]["code.a"]["runner"], "cursor")
        self.assertEqual(ledger.profile["substituted"],
                         [{"role": "code.a", "from": "codex", "to": "cursor", "reason": "샌드박스 점검 실패"}])
        self.assertEqual(ledger.workers["a"]["runner"], "cursor")
        self.assertEqual(ledger.profile["consented"], ["cursor"])
        self.assertEqual(record["roles"]["code.a"]["runner"], "codex")

    def test_old_ledger_is_materialized_first(self):
        workers = {"a": {"runner": "codex", "model": "gpt-x", "effort": "low"},
                   "b": {"runner": "claude", "model": "claude-haiku-5-5", "effort": "low"}}
        ledger = self.ledger(workers=workers)
        before = assignment.job_workers(ledger)
        after = {**before, "a": assignment.fallback_target(before["a"], set(), set(), "a")}
        assignment.apply_sandbox_substitution(ledger, before, after, "샌드박스 점검 실패")
        self.assertEqual(ledger.profile["name"], "legacy")
        self.assertEqual(ledger.profile["roles"]["code.a"]["runner"], "claude")
        self.assertEqual(ledger.profile["substituted"][0]["from"], "codex")
        self.assertEqual(len(ledger.profile["roles"]), 14)


class FirstReviewRunnerTest(unittest.TestCase):
    def test_empty_first_review_is_not_applicable_whatever_the_source(self):
        for source in ("claude-code", "claude-doc", "codex-code", "local"):
            self.assertIsNone(assignment.first_review_runner({}, source))
            self.assertIsNone(assignment.first_review_runner(None, source))

    def test_actual_runner_wins_over_source(self):
        self.assertEqual(assignment.first_review_runner({"actual": {"runner": "codex"}}, "claude-code"), "codex")
        self.assertEqual(assignment.first_review_runner({"actual": {"runner": "claude"}}, "codex-code"), "claude")

    def test_claude_source_stands_in_when_the_record_has_no_runner(self):
        self.assertEqual(assignment.first_review_runner({"role": "code.review"}, "claude-code"), "claude")
        self.assertIsNone(assignment.first_review_runner({"role": "code.review"}, "codex-code"))


if __name__ == "__main__":
    unittest.main()
