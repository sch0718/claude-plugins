import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import verify  # noqa: E402
from cascade.config import RepoConfig  # noqa: E402
from cascade.findings import Finding  # noqa: E402
from helpers import make_repo  # noqa: E402


def finding(body):
    return Finding(id="f1", source="pr", severity="Warning", location="a.py:1", body=body, evidence="")


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_is_test_path_by_name_and_dir(self):
        for path in ("test_a.py", "pkg/a_test.py", "src/a.test.ts", "src/a.spec.js", "tests/helpers.py",
                     "src/test/java/FooTest.java", "web/__tests__/a.js", "pkg/a_test.go"):
            self.assertTrue(verify.is_test_path(path), path)
        for path in ("a.py", "src/testing.py", "latest/a.py", "contest.py"):
            self.assertFalse(verify.is_test_path(path), path)

    def test_related_tests_by_dir_name_and_import(self):
        repo = make_repo(self.tmp / "repo", {
            "src/pkg/mod.py": "x = 1\n",
            "src/pkg/test_local.py": "pass\n",
            "src/pkg/other.py": "pass\n",
            "tests/test_mod.py": "pass\n",
            "tests/test_importer.py": "from pkg.mod import x\n",
            "tests/test_unrelated.py": "import os\n",
            "web/util.ts": "export const a = 1\n",
            "web/util.test.ts": "x\n",
            "web/page.spec.ts": "import { a } from './util'\n",
            "web/node_modules/dep/util.test.js": "x\n",
        })
        py = verify.related_tests(repo, ["src/pkg/mod.py"])
        self.assertEqual(py, ["src/pkg/test_local.py", "tests/test_importer.py", "tests/test_mod.py"])
        ts = verify.related_tests(repo, ["web/util.ts"])
        self.assertEqual(ts, ["web/page.spec.ts", "web/util.test.ts"])

    def test_run_verifiers_worker_stage_substitutes_files_and_reports_tail(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n", "test_a.py": "pass\n"})
        cfg = RepoConfig(
            tests=["python3 -c 'import sys; print(\"boom\"); sys.exit(1)' {files}"],
            build=["true"],
            lint=["true"],
            format=["true"],
        )
        r = verify.run_verifiers(repo, cfg, ["a.py"], "worker")
        self.assertEqual(len(r), 4)
        self.assertFalse(r[0].ok)
        self.assertIn("test_a.py", r[0].cmd)
        self.assertIn("boom", r[0].tail)
        self.assertTrue(all(x.ok for x in r[1:]))
        self.assertEqual(set(r[0].to_dict()), {"cmd", "ok", "tail"})

    def test_without_related_tests_runs_full_tests_once_instead(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(
            tests=['test -z "{files}" || false {files}', "false {files}"],
            full_tests=["echo full-ran"],
            lint=["true"],
        )
        r = verify.run_worker_verifiers(repo, cfg, ["a.py"])
        self.assertEqual([t for t, _ in r], ['test -z "{files}" || false {files}', "true"])
        self.assertEqual(r[0][1].cmd, verify.NO_TESTS_FULL_TAG + "echo full-ran")
        self.assertTrue(r[0][1].ok)
        self.assertFalse(r[0][1].skipped)
        self.assertIn("full-ran", r[0][1].tail)

    def test_without_related_tests_or_full_tests_skips_and_does_not_count_as_pass(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(tests=['touch ran; test -z "{files}" || true {files}'])
        r = verify.run_verifiers(repo, cfg, ["a.py"], "worker")
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0].skipped)
        self.assertFalse(r[0].ok)
        self.assertTrue(r[0].cmd.startswith(verify.NO_TESTS_SKIP_TAG))
        self.assertNotIn("{files}", r[0].cmd)
        self.assertFalse((repo / "ran").exists())
        self.assertIs(r[0].to_dict()["skipped"], True)

    def test_tests_command_without_files_placeholder_runs_as_is(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(tests=["echo suite"], full_tests=["echo full"])
        r = verify.run_verifiers(repo, cfg, ["a.py"], "worker")
        self.assertEqual([x.cmd for x in r], ["echo suite"])
        self.assertFalse(r[0].skipped)

    def test_final_stage_runs_only_full_tests(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(tests=["false"], full_tests=["true"], build=["false"], lint=["false"])
        r = verify.run_verifiers(repo, cfg, ["a.py"], "final")
        self.assertEqual([x.cmd for x in r], ["true"])
        self.assertTrue(r[0].ok)

    def test_timeout_counts_as_failure(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(full_tests=["sleep 5"])
        with mock.patch.object(verify, "TIMEOUT", 0.2):
            r = verify.run_verifiers(repo, cfg, [], "final")
        self.assertFalse(r[0].ok)
        self.assertEqual(r[0].tail, "시간 초과(600초)")

    def test_run_format_ignores_exit_codes(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(format=["false", "touch formatted.txt"])
        self.assertIsNone(verify.run_format(repo, cfg, ["a.py"]))
        self.assertTrue((repo / "formatted.txt").exists())

    def test_format_command_targets_only_given_files(self):
        self.assertEqual(verify.format_command("ruff format {files}", ["a.py", "b c.py"]), "ruff format a.py 'b c.py'")
        self.assertEqual(verify.format_command("prettier --write", ["x.ts"]), "prettier --write x.ts")
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(format=["touch formatted.txt"])
        verify.run_format(repo, cfg, ["gone.py"])
        self.assertFalse((repo / "formatted.txt").exists())

    def test_check_doc_refs_lists_missing_paths(self):
        repo = make_repo(self.tmp / "repo", {"app.py": "x=1\n", "docs/real.md": "r\n"})
        doc = repo / "docs" / "guide.md"
        doc.write_text(
            "코드는 app.py:12 를 본다.\n"
            "`ghost.py` 는 없다. `docs/real.md` 는 있다.\n"
            "또 missing/dir/x.ts:3 도 없다.\n"
            "https://example.com/a.py 와 `src/*.py`, `{x}/y.py`, `외부/copy/z.py` 는 무시.\n",
            encoding="utf-8",
        )
        self.assertEqual(verify.check_doc_refs(repo, doc), ["ghost.py", "missing/dir/x.ts"])

    def test_check_snippets_runs_configured_checker_per_language(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        doc = repo / "doc.md"
        doc.write_text(
            "```python\nx = 1\n```\n\n```python\nx = (\n```\n\n```js\nvar a;\n```\n\n```\nplain\n```\n",
            encoding="utf-8",
        )
        cfg = RepoConfig(doc_snippet_check={"python": "python3 -m py_compile {file}"})
        r = verify.check_snippets(repo, doc, cfg)
        self.assertEqual(len(r), 2)
        self.assertTrue(r[0].ok)
        self.assertFalse(r[1].ok)
        self.assertIn("0", r[0].cmd)
        self.assertIn("1", r[1].cmd)
        self.assertTrue((repo / ".cascade-fix" / "snippets" / "0.python").exists())

    def test_repro_cmds_from_confirmed_block_only(self):
        body = (
            "설명입니다.\n\n"
            "```sh\nignored --cmd\n```\n\n"
            "**확인한 내용**: 재현했다.\n"
            "```sh\n$ python3 -c 'print(1)'\n\nls -la\n```\n\n"
            "**근거**: 위\n```sh\nnot-this\n```\n"
        )
        self.assertEqual(verify.repro_cmds(finding(body)), ["python3 -c 'print(1)'"])
        self.assertEqual(verify.repro_cmds(finding("```sh\nfoo\n```\n")), [])

    def test_repro_cmds_spans_blank_line_until_evidence_or_reply(self):
        body = (
            "**확인한 내용**:\n\n"
            "```sh\n$ python3 -c 'print(1)'\n$ ls -la\n```\n\n"
            "**근거**: 위\n\n```sh\nnot-this\n```\n"
        )
        self.assertEqual(verify.repro_cmds(finding(body)), ["python3 -c 'print(1)'", "ls -la"])
        reply = "**확인한 내용**: 재현\n\n```sh\nfoo\n```\n\n답글(작성자):\n```sh\nnot-this\n```\n"
        self.assertEqual(verify.repro_cmds(finding(reply)), ["foo"])

    def test_repro_cmds_with_prompt_lines_skip_output_lines(self):
        body = "**확인한 내용**: 재현\n```\n$ pytest tests/test_a.py\nFAILED tests/test_a.py::t - boom\n1 failed\n```\n"
        self.assertEqual(verify.repro_cmds(finding(body)), ["pytest tests/test_a.py"])
        bare = "**확인한 내용**: 재현\n```sh\npytest tests/test_a.py\n```\n"
        self.assertEqual(verify.repro_cmds(finding(bare)), ["pytest tests/test_a.py"])

    def test_new_broken_refs_ignores_refs_already_broken_in_base(self):
        base = make_repo(self.tmp / "base", {"app.py": "x\n"})
        wt = make_repo(self.tmp / "wt", {"app.py": "x\n"})
        old = "`ghost.py` 는 예전부터 없다. `~/.claude/x.md`, `<경로>/a.py`, `$HOME/b.py`, `{root}/c.py`\n"
        (base / "plan.md").write_text(old, encoding="utf-8")
        (wt / "plan.md").write_text(old + "이제 `app.py` 를 고친다.\n", encoding="utf-8")
        self.assertEqual(verify.check_doc_refs(wt, wt / "plan.md"), ["ghost.py"])
        self.assertEqual(verify.new_broken_refs(base, base / "plan.md", wt, wt / "plan.md"), [])
        (wt / "plan.md").write_text(old + "`ghost2.py` 를 본다.\n", encoding="utf-8")
        self.assertEqual(verify.new_broken_refs(base, base / "plan.md", wt, wt / "plan.md"), ["ghost2.py"])
        self.assertEqual(verify.new_broken_refs(None, None, wt, wt / "plan.md"), ["ghost.py", "ghost2.py"])

    def test_related_tests_skips_untracked_ignored_dirs(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n", "test_a.py": "pass\n", ".gitignore": "venvx/\n"})
        (repo / ".venv" / "lib").mkdir(parents=True)
        (repo / ".venv" / "lib" / "test_a.py").write_text("import a\n")
        (repo / "venvx").mkdir()
        (repo / "venvx" / "test_a.py").write_text("import a\n")
        self.assertEqual(verify.related_tests(repo, ["a.py"]), ["test_a.py"])

    def test_related_tests_from_package_and_relative_imports(self):
        repo = make_repo(self.tmp / "repo", {
            "cascade/verify.py": "x = 1\n",
            "tests/test_pkg_style.py": "from cascade import verify\n",
            "tests/test_multi.py": "from cascade import config, verify\n",
            "tests/test_other.py": "from cascade import config\n",
            "cascade/test_rel_a.py": "from .verify import x\n",
            "pkg/mod.py": "x = 1\n",
            "pkg/test_rel_b.py": "from . import mod\n",
            "pkg/test_rel_c.py": "from .other import y\n",
        })
        self.assertEqual(
            verify.related_tests(repo, ["cascade/verify.py"]),
            ["cascade/test_rel_a.py", "tests/test_multi.py", "tests/test_pkg_style.py"],
        )
        self.assertEqual(verify.related_tests(repo, ["pkg/mod.py"]), ["pkg/test_rel_b.py", "pkg/test_rel_c.py"])

    def test_related_tests_includes_spec_named_file_under_tests_dir(self):
        repo = make_repo(self.tmp / "repo", {"web/util.ts": "x\n", "tests/util.spec.ts": "x\n"})
        self.assertEqual(verify.related_tests(repo, ["web/util.ts"]), ["tests/util.spec.ts"])

    def test_run_verifiers_quotes_related_test_paths(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n", "test_a b.py": "pass\n"})
        cfg = RepoConfig(tests=["python3 -c 'import sys; print(sys.argv[1:])' {files}"])
        r = verify.run_verifiers(repo, cfg, ["a.py"], "worker")
        self.assertTrue(r[0].ok)
        self.assertIn("['test_a b.py']", r[0].tail)

    def test_run_verifiers_survives_non_utf8_output(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n"})
        cfg = RepoConfig(full_tests=["python3 -c \"import sys; sys.stdout.buffer.write(b'\\xff\\xfe')\""])
        r = verify.run_verifiers(repo, cfg, [], "final")
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0].ok)

    def test_commands_do_not_inherit_stdin(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n"})
        cfg = RepoConfig(full_tests=["cat; echo done"])
        r = verify.run_verifiers(repo, cfg, [], "final")
        self.assertTrue(r[0].ok)
        self.assertIn("done", r[0].tail)

    def test_check_doc_refs_ignores_paths_outside_worktree(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n"})
        doc = repo / "doc.md"
        doc.write_text("`../outside.py` `/nonexistent/abs.py` `ghost.py` http://h/x.py:3\n", encoding="utf-8")
        self.assertEqual(verify.check_doc_refs(repo, doc), ["ghost.py"])

    def test_check_doc_refs_skips_new_names_and_finds_shortened_paths(self):
        repo = make_repo(self.tmp / "repo", {
            "20. Helm/replay-runner/values-local.yaml": "x: 1\n",
            "docs/real.md": "r\n",
        })
        doc = repo / "docs" / "design.md"
        doc.write_text(
            "라벨 `replay.example.com/description`, `replay.example.com/display-name` 을 붙인다.\n"
            "`values-local.yaml` 과 `replay-runner/values-local.yaml` 을 고친다.\n"
            "DAG 노드는 `execute-left/right`, `finalize-left/right` 이다.\n"
            "`docs/없는파일.md` 와 `docs/real.md`, `docs/없는폴더`, `ghost.yaml`, `nope/values-local.yaml` 을 본다.\n",
            encoding="utf-8",
        )
        self.assertEqual(
            verify.check_doc_refs(repo, doc),
            ["docs/없는파일.md", "docs/없는폴더", "ghost.yaml", "nope/values-local.yaml"],
        )

    def test_run_repro_requires_fail_before_and_pass_after(self):
        base = self.tmp / "base"
        fix = self.tmp / "fix"
        base.mkdir()
        fix.mkdir()
        (fix / "ok.flag").write_text("1")
        r = verify.run_repro(base, fix, ["test -f ok.flag", "true", "false"])
        self.assertEqual([x.ok for x in r], [True, False, False])
        self.assertIn("수정 전 rc=1 / 수정 후 rc=0", r[0].tail)
        self.assertIn("수정 전 rc=0 / 수정 후 rc=0", r[1].tail)

    def test_repro_cmds_skip_comment_lines(self):
        bare = "**확인한 내용**: 재현\n```sh\n# 재현 절차\npytest tests/test_a.py\n```\n"
        self.assertEqual(verify.repro_cmds(finding(bare)), ["pytest tests/test_a.py"])
        prompted = "**확인한 내용**: 재현\n```sh\n$ # 준비\n$ pytest tests/test_a.py\n# 출력\n```\n"
        self.assertEqual(verify.repro_cmds(finding(prompted)), ["pytest tests/test_a.py"])
        only = "**확인한 내용**: 재현\n```sh\n# 설명만 있다\n```\n"
        self.assertEqual(verify.repro_cmds(finding(only)), [])

    def test_related_tests_parenthesized_multiline_import(self):
        repo = make_repo(self.tmp / "repo", {
            "pkg/__init__.py": "",
            "pkg/mod.py": "x = 1\n",
            "tests/test_paren.py": "from pkg import (\n    other,\n    mod,\n)\n",
            "tests/test_paren_other.py": "from pkg import (\n    other,\n)\nmod = 1\n",
        })
        self.assertEqual(verify.related_tests(repo, ["pkg/mod.py"]), ["tests/test_paren.py"])

    def test_related_tests_ignores_stem_in_import_comment(self):
        repo = make_repo(self.tmp / "repo", {
            "pkg/mod.py": "x = 1\n",
            "tests/test_other.py": "from pkg import other  # mod 대신 other를 쓴다\n",
            "tests/test_paren_comment.py": "from pkg import (\n    other,  # mod 대신\n)\n",
        })
        self.assertEqual(verify.related_tests(repo, ["pkg/mod.py"]), [])

    def test_related_tests_reads_each_test_file_once(self):
        repo = make_repo(self.tmp / "repo", {
            "src/a.py": "x\n", "src/b.py": "x\n", "src/c.py": "x\n",
            "tests/test_x.py": "import os\n", "tests/test_y.py": "from src import b\n",
        })
        reads = []
        orig = Path.read_text

        def spy(path, *args, **kwargs):
            reads.append(path.name)
            return orig(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", spy):
            found = verify.related_tests(repo, ["src/a.py", "src/b.py", "src/c.py"])
        self.assertEqual(found, ["tests/test_y.py"])
        self.assertEqual(sorted(reads), ["test_x.py", "test_y.py"])

    def test_result_tail_keeps_last_tail_chars(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n"})
        r = verify.run_commands(repo, ["python3 -c \"print('A' * 3000 + 'END')\""])
        self.assertEqual(len(r[0].tail), verify.TAIL_CHARS)
        self.assertTrue(r[0].tail.endswith("END\n"))

    def test_run_repro_reports_timeout_as_failure(self):
        base = self.tmp / "base"
        fix = self.tmp / "fix"
        base.mkdir()
        fix.mkdir()
        with mock.patch.object(verify, "TIMEOUT", 0.2):
            r = verify.run_repro(base, fix, ["sleep 5"])
        self.assertFalse(r[0].ok)
        self.assertIn("수정 전 rc=시간 초과 / 수정 후 rc=시간 초과", r[0].tail)

    def test_check_doc_refs_checks_local_paths_starting_with_http(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n", "http/real.py": "x\n"})
        doc = repo / "doc.md"
        doc.write_text(
            "`httpx_client.py` 와 `http/server.py`, `http/real.py` 를 본다. https://h/a.py http://h/b.py\n",
            encoding="utf-8",
        )
        self.assertEqual(verify.check_doc_refs(repo, doc), ["http/server.py", "httpx_client.py"])


if __name__ == "__main__":
    unittest.main()
