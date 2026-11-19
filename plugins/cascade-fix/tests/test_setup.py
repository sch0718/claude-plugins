import copy
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import profiles, recommend, setup  # noqa: E402
from cascade.toml_edit import Target  # noqa: E402
from fake_cli import CODEX_BODY, CURSOR_BODY, install  # noqa: E402

TABLE = recommend.load_table()
CLAUDE_ONLY = profiles.builtin("claude-only")["roles"]
LOCAL = profiles.builtin("local")["roles"]
CODEX_ONLY_ENV = {"codex": "로그인됨", "cursor": "없음"}


def table_v2(**recommends):
    table = copy.deepcopy(TABLE)
    table["version"] = 2
    for env, name in recommends.items():
        table["env"][env]["recommend"] = name
    return table


def with_cursor(roles, key, fallback_only=False):
    roles = copy.deepcopy(roles)
    cell = roles[key]
    target = cell["fallback"] if fallback_only else cell
    target.update(runner="cursor", model="composer-2.5", effort="")
    return roles


class SetupTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()


class CheckPathsTest(unittest.TestCase):
    def test_check_paths_local(self):
        self.assertEqual(setup.check_paths(LOCAL), [("codex", "review"), ("codex", "worker")])

    def test_check_paths_claude_only_is_empty(self):
        self.assertEqual(setup.check_paths(CLAUDE_ONLY), [])

    def test_check_paths_includes_external_fallback(self):
        roles = with_cursor(CLAUDE_ONLY, "code.a", fallback_only=True)
        self.assertEqual(setup.check_paths(roles), [("cursor", "worker")])

    def test_check_paths_are_sorted_without_duplicates(self):
        roles = with_cursor(LOCAL, "code.b")
        self.assertEqual(setup.check_paths(roles), [("codex", "review"), ("codex", "worker"), ("cursor", "worker")])

    def test_acp_models_lists_cursor_read_role_models(self):
        self.assertEqual(setup.acp_models(LOCAL), [])
        roles = with_cursor(CLAUDE_ONLY, "doc.judge_b")
        roles["doc.judge_b"]["model"] = "model-x"
        roles = with_cursor(roles, "doc.checker", fallback_only=True)
        self.assertEqual(setup.acp_models(roles), ["composer-2.5", "model-x"])

    def test_acp_models_only_for_cursor_read_roles(self):
        self.assertEqual(setup.acp_models(with_cursor(CLAUDE_ONLY, "code.a")), [])
        self.assertEqual(setup.acp_models(with_cursor(CLAUDE_ONLY, "doc.judge_b")), ["composer-2.5"])
        self.assertEqual(setup.acp_models(with_cursor(CLAUDE_ONLY, "doc.checker", fallback_only=True)), ["composer-2.5"])


class CheckCellsTest(unittest.TestCase):
    def test_same_runner_and_path_with_different_models_are_checked_separately(self):
        roles = copy.deepcopy(LOCAL)
        roles["code.review"].update(model="gpt-a", effort="high")
        roles["code.opinion"].update(model="gpt-b", effort="high")
        models = {model for runner, path, model, _ in setup.check_cells(roles) if (runner, path) == ("codex", "review")}
        self.assertTrue({"gpt-a", "gpt-b"} <= models)

    def test_same_cell_used_by_many_roles_is_checked_once(self):
        cells = setup.check_cells(with_cursor(CLAUDE_ONLY, "code.a"))
        self.assertEqual(len(cells), len(set(cells)))


class CellModelTest(unittest.TestCase):
    def test_codex_worker_cell(self):
        roles = copy.deepcopy(LOCAL)
        roles["code.a"].update(model="gpt-test", effort="high")
        self.assertEqual(setup.cell_model(roles, "codex", "worker"), ("gpt-test", "high"))

    def test_empty_codex_model_reads_config(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".codex").mkdir()
            (Path(home) / ".codex" / "config.toml").write_text('model = "gpt-from-config"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": home}):
                model, effort = setup.cell_model(LOCAL, "codex", "worker")
        self.assertEqual((model, effort), ("gpt-from-config", profiles.ROLES["a"][1]))

    def test_cursor_cell_and_fallback(self):
        roles = with_cursor(CLAUDE_ONLY, "code.b")
        self.assertEqual(setup.cell_model(roles, "cursor", "worker"), ("composer-2.5", ""))
        roles = with_cursor(CLAUDE_ONLY, "doc.writer", fallback_only=True)
        self.assertEqual(setup.cell_model(roles, "cursor", "worker"), ("composer-2.5", ""))

    def test_review_path_ignores_worker_cells(self):
        roles = with_cursor(CLAUDE_ONLY, "code.a")
        roles["code.opinion"].update(runner="cursor", model="composer-9", effort="")
        self.assertEqual(setup.cell_model(roles, "cursor", "review"), ("composer-9", ""))


class AvailabilityTest(unittest.TestCase):
    def test_absent_needs_login_logged_in(self):
        with mock.patch.object(setup, "find_codex", return_value=None), \
                mock.patch.object(setup, "find_cursor_agent", return_value=None):
            self.assertEqual(setup.availability(), {"codex": "없음", "cursor": "없음"})
        with mock.patch.object(setup, "find_codex", return_value="/x/codex"), \
                mock.patch.object(setup, "find_cursor_agent", return_value="/x/cursor-agent"), \
                mock.patch.object(setup, "codex_available", return_value=False), \
                mock.patch.object(setup, "cursor_available", return_value=True):
            self.assertEqual(setup.availability(), {"codex": "로그인 필요", "cursor": "로그인됨"})
        with mock.patch.object(setup, "find_codex", return_value="/x/codex"), \
                mock.patch.object(setup, "find_cursor_agent", return_value=None), \
                mock.patch.object(setup, "codex_available", return_value=True):
            self.assertEqual(setup.availability(), {"codex": "로그인됨", "cursor": "없음"})


class InspectTest(SetupTestBase):
    def setUp(self):
        super().setUp()
        self.user = self.tmp / "user" / "repos.toml"
        self.user.parent.mkdir()
        self.legacy = self.tmp / "legacy.toml"

    def inspect(self, **overrides):
        args = dict(table=TABLE, availability={"codex": "로그인됨", "cursor": "없음"}, codex_model="gpt-x",
                    target=Target(self.user, None), target_error=None, scripts={}, script_dirs={}, record=None)
        args.update(overrides)
        return setup.inspect(**args)

    def test_inspect_env_excludes_needs_login(self):
        r = self.inspect(availability={"codex": "로그인 필요", "cursor": "로그인됨"})
        self.assertEqual(r["env"], "cursor")
        self.assertEqual(r["availability"], {"codex": "로그인 필요", "cursor": "로그인됨"})

    def test_inspect_env_none_when_nothing_usable(self):
        self.assertEqual(self.inspect(availability={"codex": "없음", "cursor": "로그인 필요"})["env"], "none")

    def test_inspect_reports_table_options_and_passthrough(self):
        record = {"schema": 1}
        scripts = {"codex-review": ("같음", [])}
        dirs = {"codex-review": "/h/.claude/skills/codex-review"}
        r = self.inspect(availability={"codex": "로그인됨", "cursor": "로그인됨"}, record=record, scripts=scripts,
                         script_dirs=dirs)
        self.assertEqual(r["env"], "codex+cursor")
        self.assertEqual(r["table_version"], TABLE["version"])
        self.assertEqual(r["codex_model"], "gpt-x")
        self.assertIs(r["record"], record)
        self.assertEqual(r["scripts"], scripts)
        self.assertEqual(r["script_dirs"], dirs)
        self.assertEqual(set(r["options"]), {"save-claude", "fast"})
        self.assertEqual(r["options"]["fast"], recommend.options(TABLE, "codex+cursor", "fast"))
        self.assertTrue(r["options"]["save-claude"][0]["recommended"])

    def test_inspect_reports_default_profile_and_overrides(self):
        self.user.write_text(
            'default_profile = "local"\n\n[profiles.local]\n"code.a" = { effort = "low" }\n\n[profiles.mine]\nbase = "local"\n',
            encoding="utf-8")
        repos = self.inspect()["repos_toml"]
        self.assertEqual(repos["path"], str(self.user))
        self.assertIsNone(repos["copy_from"])
        self.assertIsNone(repos["error"])
        self.assertEqual(repos["default_profile"], "local")
        self.assertEqual(repos["overridden_builtins"], ["local"])

    def test_inspect_missing_file_has_no_values(self):
        repos = self.inspect()["repos_toml"]
        self.assertEqual(repos, {"path": str(self.user), "copy_from": None, "error": None,
                                 "default_profile": None, "overridden_builtins": []})

    def test_inspect_reads_legacy_when_copying(self):
        self.legacy.write_text('default_profile = "claude-only"\n\n[repos."/x"]\ntests = ["t"]\n', encoding="utf-8")
        repos = self.inspect(target=Target(self.user, self.legacy))["repos_toml"]
        self.assertEqual(repos["path"], str(self.user))
        self.assertEqual(repos["copy_from"], str(self.legacy))
        self.assertEqual(repos["default_profile"], "claude-only")

    def test_inspect_reports_unreadable_toml(self):
        self.user.write_text("default_profile = \n", encoding="utf-8")
        repos = self.inspect()["repos_toml"]
        self.assertIn("repos.toml", repos["error"])
        self.assertIsNone(repos["default_profile"])

    def test_inspect_reports_target_error(self):
        repos = self.inspect(target=None, target_error="CASCADE_REPOS_TOML이 잡혀 있다")["repos_toml"]
        self.assertEqual(repos["error"], "CASCADE_REPOS_TOML이 잡혀 있다")
        self.assertIsNone(repos["path"])
        self.assertIsNone(repos["default_profile"])


class NoticesTest(unittest.TestCase):
    def notices(self, **overrides):
        args = dict(record=None, table=TABLE, default_profile="claude-only", roles=CLAUDE_ONLY, scripts={})
        args.update(overrides)
        return setup.notices(**args)

    def test_notice_before_setup(self):
        self.assertEqual(
            setup.notices(record=None, table=TABLE, default_profile=None, roles=CLAUDE_ONLY, scripts={}),
            ["이 기기 설정 전이다. 「cascade-fix 설정해줘」로 쓸 수 있는 도구에 맞는 프로필을 고른다"])

    def test_no_before_setup_notice_when_default_profile_hand_written(self):
        self.assertEqual(self.notices(default_profile="local", roles=LOCAL), [])

    def test_table_change_notice_only_when_recommendation_changed(self):
        rec = {"table_version": 1, "env": "codex", "priority": "save-claude", "recommended": "claude-only",
               "profile": "claude-only"}
        self.assertEqual(self.notices(record=rec, table=table_v2(codex="claude-only")), [])
        self.assertEqual(
            self.notices(record=rec, table=table_v2(codex="local")),
            ["추천 표가 바뀌었다(v1→v2): 이 기기 추천은 local. 「cascade-fix 설정해줘」로 바꿀 수 있다"])

    def test_no_table_notice_when_version_not_newer(self):
        rec = {"table_version": 2, "env": "codex", "priority": "save-claude", "recommended": "claude-only",
               "profile": "claude-only"}
        self.assertEqual(self.notices(record=rec, table=table_v2(codex="local")), [])

    def test_no_table_notice_when_user_already_chose_new_recommendation(self):
        rec = {"table_version": 1, "env": "codex", "priority": "save-claude", "recommended": "claude-only",
               "profile": "local"}
        self.assertEqual(self.notices(record=rec, table=table_v2(codex="local"), roles=LOCAL), [])

    def test_table_notice_follows_recorded_priority(self):
        rec = {"table_version": 1, "env": "codex", "priority": "fast", "recommended": "claude-only",
               "profile": "claude-only"}
        self.assertEqual(self.notices(record=rec, table=table_v2(codex="local")), [])

    def test_unreadable_record_values_are_ignored(self):
        rec = {"table_version": "x", "env": "mars", "priority": 1}
        self.assertEqual(self.notices(record=rec, table=table_v2(codex="local")), [])

    def test_script_notice_only_when_profile_uses_review_script(self):
        scripts = {"codex-review": ("다름", ["scripts/run-review.sh: 다름(+1/-0줄)"]), "cursor-review": ("같음", [])}
        self.assertEqual(
            self.notices(default_profile="local", roles=LOCAL, scripts=scripts),
            ["codex-review 스크립트가 플러그인 판과 다르다. 「cascade-fix 설정해줘」로 바꿀 수 있다"])
        self.assertEqual(self.notices(roles=CLAUDE_ONLY, scripts=scripts), [])

    def test_script_notice_for_missing_and_for_cursor(self):
        roles = copy.deepcopy(LOCAL)
        roles["code.recheck"].update(runner="cursor", model="composer-2.5", effort="")
        scripts = {"codex-review": ("없음", []), "cursor-review": ("다름", [])}
        self.assertEqual(
            self.notices(default_profile="local", roles=roles, scripts=scripts),
            ["codex-review 스크립트가 플러그인 판과 다르다. 「cascade-fix 설정해줘」로 바꿀 수 있다",
             "cursor-review 스크립트가 플러그인 판과 다르다. 「cascade-fix 설정해줘」로 바꿀 수 있다"])

    def test_script_notice_ignores_worker_only_use(self):
        scripts = {"cursor-review": ("다름", [])}
        roles = with_cursor(CLAUDE_ONLY, "code.a")
        self.assertEqual(self.notices(roles=roles, scripts=scripts), [])


class AcpResultTest(unittest.TestCase):
    def test_acp_result(self):
        self.assertEqual(setup.acp_result("read\t막힘\n합계: 6건, 막힘 6건, 누출 0건\n", 0), "누출 0건")
        self.assertEqual(setup.acp_result("read\t누출\n합계: 6건, 막힘 4건, 누출 2건\n", 1), "누출 있음")
        self.assertEqual(setup.acp_result("Cursor ACP 격리 점검 오류: x\n", 1), "확인 못 함")
        self.assertEqual(setup.acp_result("", 0), "확인 못 함")


class RecordTest(SetupTestBase):
    def test_read_record_missing_or_broken_is_none(self):
        path = self.tmp / "setup.json"
        self.assertIsNone(setup.read_record(path))
        path.write_text("{", encoding="utf-8")
        self.assertIsNone(setup.read_record(path))
        path.write_text("[1]", encoding="utf-8")
        self.assertIsNone(setup.read_record(path))

    def test_write_record_adds_schema_and_roundtrips(self):
        path = self.tmp / "nested" / "setup.json"
        setup.write_record(path, {"profile": "claude-only", "note": "한글"})
        self.assertEqual(setup.read_record(path), {"schema": 1, "profile": "claude-only", "note": "한글"})
        self.assertIn("한글", path.read_text(encoding="utf-8"))


def write_script(path: Path, log: Path, code: int) -> Path:
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}.args"\ncp "$2" "{log}.prompt"\nexit {code}\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class RunCheckTest(SetupTestBase):
    def review(self, runner="codex", code=0, model=""):
        script = write_script(self.tmp / "run-review.sh", self.tmp / "log", code)
        return setup.run_check(runner, "review", model, "", {runner: script}, timeout=20)

    def test_run_check_review_path_with_fake_script(self):
        self.assertEqual(self.review(), {"status": "성공", "detail": ""})
        args = (self.tmp / "log.args").read_text(encoding="utf-8").splitlines()
        self.assertEqual(args[0], "--opinion")
        self.assertEqual(args[2], "--repo")
        self.assertEqual(args[4], "--out")
        self.assertNotIn("--model", args)
        self.assertEqual(args[1], str(Path(args[3]) / "prompt.md"))
        self.assertEqual((self.tmp / "log.prompt").read_text(encoding="utf-8"), setup.SMOKE_PROMPT)
        self.assertFalse(Path(args[3]).exists())

    def test_run_check_review_repo_is_git_work_tree(self):
        script = self.tmp / "run-review.sh"
        log = self.tmp / "git.log"
        script.write_text(f'#!/bin/sh\ngit -C "$4" rev-parse --is-inside-work-tree > "{log}" 2>&1\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        result = setup.run_check("codex", "review", "", "", {"codex": script}, timeout=20)
        self.assertEqual(log.read_text(encoding="utf-8").strip(), "true")
        self.assertEqual(result, {"status": "성공", "detail": ""})

    def test_run_check_cursor_review_passes_model(self):
        self.assertEqual(self.review("cursor", model="composer-2.5")["status"], "성공")
        args = (self.tmp / "log.args").read_text(encoding="utf-8").splitlines()
        self.assertEqual(args[args.index("--model") + 1], "composer-2.5")

    def test_run_check_review_exit_codes(self):
        self.assertEqual(self.review(code=77)["status"], "로그인 필요")
        self.assertEqual(self.review(code=75)["status"], "한도")
        failed = self.review(code=3)
        self.assertEqual(failed["status"], "실패")
        self.assertIn("3", failed["detail"])

    def test_run_check_review_unknown_exit_uses_log_tail(self):
        script = self.tmp / "run-review.sh"
        script.write_text("#!/bin/sh\necho 'You have hit your usage limit' >&2\nexit 1\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        result = setup.run_check("codex", "review", "", "", {"codex": script}, timeout=20)
        self.assertEqual(result["status"], "한도")

    def test_run_check_review_reads_out_log(self):
        script = self.tmp / "run-review.sh"
        script.write_text("#!/bin/sh\necho 'ERROR: You have hit your usage limit' > \"$6\"\n"
                          "echo exit=1\necho \"full_log=$6\"\nexit 1\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        result = setup.run_check("codex", "review", "", "", {"codex": script}, timeout=20)
        self.assertEqual(result["status"], "한도")
        self.assertIn("usage limit", result["detail"])

    def test_run_check_review_script_missing(self):
        missing = self.tmp / "none.sh"
        result = setup.run_check("codex", "review", "", "", {"codex": missing})
        self.assertEqual(result, {"status": "실패", "detail": f"스크립트 없음: {missing}"})
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            result = setup.run_check("cursor", "review", "", "", {})
        self.assertEqual(result, {"status": "실패", "detail": f"스크립트 없음: {self.tmp}/.claude/skills/cursor-review/scripts/run-review.sh"})

    def test_run_check_review_timeout_kills_grandchild(self):
        pid_file = self.tmp / "child.pid"
        script = self.tmp / "run-review.sh"
        script.write_text(f"#!/bin/sh\nsh -c 'echo $$ > \"{pid_file}\"; exec sleep 20' &\nwait\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        def cleanup():
            if pid_file.is_file():
                try:
                    os.kill(int(pid_file.read_text()), 9)
                except (ProcessLookupError, ValueError):
                    pass

        self.addCleanup(cleanup)
        result = setup.run_check("codex", "review", "", "", {"codex": script}, timeout=2)
        self.assertEqual(result["status"], "실패")
        self.assertIn("시간 초과", result["detail"])
        pid = int(pid_file.read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class RunCheckWorkerTest(SetupTestBase):
    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.codex = install(bin_dir, "codex", CODEX_BODY)
        self.cursor = install(bin_dir, "cursor-agent", CURSOR_BODY)
        home = self.tmp / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "auth.json").write_text('{"last_refresh": "2026-10-20T00:00:00Z"}', encoding="utf-8")
        self.log = self.tmp / "log"
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(home), "FAKE_CLI_LOG": str(self.log), "FAKE_CODEX_RESULT": "OK",
            "FAKE_CURSOR_RESULTS": json.dumps(["OK"]),
            "CASCADE_CODEX": str(self.codex), "CASCADE_CURSOR_AGENT": str(self.cursor),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, name):
        return json.loads((self.log / f"{name}-1.args.json").read_text(encoding="utf-8"))

    def test_run_check_worker_codex_uses_fake_cli(self):
        result = setup.run_check("codex", "worker", "gpt-test", "low", {}, timeout=20)
        self.assertEqual(result, {"status": "성공", "detail": ""})
        args = self.args("codex")
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        self.assertEqual(args[args.index("-m") + 1], "gpt-test")
        self.assertIn(setup.SMOKE_PROMPT, (self.log / "codex-1.stdin").read_text(encoding="utf-8"))
        self.assertNotIn(str(Path.cwd()), args)

    def test_run_check_worker_cursor_uses_fake_cli(self):
        result = setup.run_check("cursor", "worker", "composer-2.5", "", {}, timeout=20)
        self.assertEqual(result, {"status": "성공", "detail": ""})
        args = self.args("cursor-agent")
        self.assertEqual(args[args.index("--model") + 1], "composer-2.5")

    def test_run_check_worker_failure_is_classified(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_EXIT": "1", "FAKE_CODEX_STDERR": "usage limit reached"}):
            self.assertEqual(setup.run_check("codex", "worker", "", "low", {}, timeout=20)["status"], "한도")
        (Path(os.environ["HOME"]) / ".codex" / "auth.json").unlink()
        result = setup.run_check("codex", "worker", "", "low", {}, timeout=20)
        self.assertEqual(result["status"], "로그인 필요")

    def test_run_check_worker_without_executable_fails(self):
        with mock.patch.dict(os.environ, {"CASCADE_CODEX": str(self.tmp / "none"), "CASCADE_CURSOR_AGENT": str(self.tmp / "none")}):
            self.assertEqual(setup.run_check("codex", "worker", "", "low", {}),
                             {"status": "실패", "detail": "codex 실행 파일 없음"})
            self.assertEqual(setup.run_check("cursor", "worker", "composer-2.5", "", {}),
                             {"status": "실패", "detail": "cursor 실행 파일 없음"})


if __name__ == "__main__":
    unittest.main()
