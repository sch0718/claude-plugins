import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import runners  # noqa: E402
from cascade.findings import parse_codex_doc  # noqa: E402
from helpers import git, make_repo  # noqa: E402

SCRIPT = Path(os.environ.get("CASCADE_CURSOR_REVIEW")
              or Path(__file__).resolve().parent.parent / "docs" / "cursor-review" / "run-review.sh")

DEFAULT_JSON = ('{"type":"result","is_error":false,"result":"본문\\nSUMMARY: critical=1 warning=2 suggestion=0",'
                '"session_id":"s","usage":{"inputTokens":10,"outputTokens":2,"cacheReadTokens":0,"cacheWriteTokens":0}}')

FAKE_CURSOR = """#!/usr/bin/env bash
if [ "$1" = "status" ]; then
  echo "${FAKE_CURSOR_STATUS-✓ Logged in as t}"
  exit 0
fi
for a in "$@"; do printf '%s\\0' "$a"; done > "$FAKE_CURSOR_OUT"
printf '%s' "$HOME" > "$FAKE_CURSOR_OUT.home"
cp "$HOME/.cursor/cli-config.json" "$FAKE_CURSOR_OUT.config"
readlink "$HOME/Library/Keychains" > "$FAKE_CURSOR_OUT.keychains"
ls -A "$HOME" > "$FAKE_CURSOR_OUT.homelist"
cat > "$FAKE_CURSOR_OUT.stdin"
printf '%s\\n' "$FAKE_CURSOR_JSON"
exit "${FAKE_CURSOR_EXIT:-0}"
"""


class CursorReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        fake = self.bin / "cursor-agent"
        fake.write_text(FAKE_CURSOR, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.capture = base / "captured.txt"
        self.repo = make_repo(base / "repo", {"a.md": "문서\n"}).resolve()
        self.prompt = base / "prompt.md"
        self.prompt.write_text("재검토 프롬프트 본문\n둘째 줄", encoding="utf-8")
        self.out = base / "out.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, cwd=None, env=None, path=None):
        full_env = dict(os.environ)
        full_env["HOME"] = str(self.home)
        full_env["PATH"] = path if path is not None else f"{self.bin}:{full_env['PATH']}"
        full_env["FAKE_CURSOR_OUT"] = str(self.capture)
        full_env["FAKE_CURSOR_JSON"] = DEFAULT_JSON
        full_env.pop("CURSOR_AGENT", None)
        full_env.pop("CASCADE_CURSOR_AGENT", None)
        if env:
            full_env.update(env)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=full_env, cwd=cwd)

    def captured_args(self):
        return self.capture.read_bytes().decode("utf-8").split("\0")[:-1]

    def captured_stdin(self):
        return Path(f"{self.capture}.stdin").read_text(encoding="utf-8")

    def captured_home(self):
        return Path(f"{self.capture}.home").read_text(encoding="utf-8")

    def captured_config(self):
        return Path(f"{self.capture}.config").read_text(encoding="utf-8")

    def final_text(self):
        return Path(f"{self.out}.final").read_text(encoding="utf-8")

    def test_doc_mode_writes_final_and_summary(self):
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("exit=0", out.stdout)
        self.assertIn("mode=doc kind=spec", out.stdout)
        self.assertIn(f"full_log={self.out}", out.stdout)
        self.assertIn(f"final={self.out}.final", out.stdout)
        self.assertIn("summary=critical=1 warning=2 suggestion=0", out.stdout)
        self.assertEqual(self.final_text().rstrip("\n"), "본문\nSUMMARY: critical=1 warning=2 suggestion=0")
        self.assertIn("usage=input=10 output=2 cache_read=0 cache_write=0", out.stdout)
        self.assertIn("git_dirty=0", out.stdout)

    def test_call_uses_ask_mode_stdin_and_no_forbidden_flags(self):
        self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out))
        args = self.captured_args()
        for flag in ("-p", "--trust", "--output-format", "json", "--mode", "ask", "--model", "composer-2.5", "--workspace"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--workspace") + 1], str(self.repo))
        for bad in ("--force", "--yolo", "--approve-mcps", "disabled"):
            self.assertNotIn(bad, args)
        self.assertIn("다음 설계 문서를 리뷰하라", self.captured_stdin())

    def test_model_option_overrides_default(self):
        self.run_script("--doc", str(self.repo / "a.md"), "--model", "other-model", "--out", str(self.out))
        args = self.captured_args()
        self.assertEqual(args[args.index("--model") + 1], "other-model")

    def test_isolated_home_links_only_keychains_and_denies_tools(self):
        self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out))
        home = Path(self.captured_home())
        self.assertNotEqual(home, self.home)
        self.assertFalse(home.exists())
        self.assertEqual(Path(f"{self.capture}.keychains").read_text(encoding="utf-8").strip(),
                         f"{self.home}/Library/Keychains")
        self.assertEqual(sorted(Path(f"{self.capture}.homelist").read_text(encoding="utf-8").split()),
                         [".cursor", "Library", "prompt.txt"])
        cfg = json.loads(self.captured_config())
        self.assertEqual(cfg["approvalMode"], "allowlist")
        self.assertEqual(cfg["permissions"]["allow"], [])
        self.assertFalse(cfg["autoAcceptWebSearch"])
        for rule in ("Mcp(*:*)", "WebFetch(*)", "Shell(*)", "Write(**)", f"Read({self.home}/.ssh/**)",
                     f"Read({self.home}/.docker/**)", f"Read({self.home}/.kube/**)",
                     f"Read({self.home}/Library/Keychains/**)", f"Read({self.home}/.netrc)",
                     f"Read({self.home}/.npmrc)", f"Read({self.home}/.gitconfig)"):
            self.assertIn(rule, cfg["permissions"]["deny"])

    def test_config_matches_runners_review_profile(self):
        self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out))
        with tempfile.TemporaryDirectory() as base, mock.patch.object(Path, "home", return_value=self.home):
            home = runners.cursor_home(Path(base), "review")
            expected = json.loads((home / ".cursor" / "cli-config.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.captured_config()), expected)

    def test_uncommitted_puts_diff_and_untracked_file_in_prompt(self):
        (self.repo / "a.md").write_text("바뀐 문서\n", encoding="utf-8")
        (self.repo / "new.py").write_text("print('새 파일')\n", encoding="utf-8")
        out = self.run_script("--uncommitted", "--out", str(self.out), cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("mode=uncommitted", out.stdout)
        stdin = self.captured_stdin()
        self.assertIn("다음 코드 변경을 리뷰하라", stdin)
        self.assertIn("+바뀐 문서", stdin)
        self.assertIn("print('새 파일')", stdin)
        self.assertIn("SUMMARY: critical=", stdin)
        self.assertIn("summary=critical=1 warning=2 suggestion=0", out.stdout)

    def test_base_mode_puts_branch_diff_in_prompt(self):
        git(self.repo, "checkout", "-q", "-b", "topic")
        (self.repo / "b.py").write_text("print('브랜치')\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "topic")
        out = self.run_script("--base", "main", "--out", str(self.out), cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("+print('브랜치')", self.captured_stdin())

    def test_base_mode_without_changes_exits_3(self):
        out = self.run_script("--base", "main", "--out", str(self.out), cwd=self.repo)
        self.assertEqual(out.returncode, 3)
        self.assertFalse(self.capture.exists())

    def test_uncommitted_without_changes_exits_3(self):
        out = self.run_script("--uncommitted", "--out", str(self.out), cwd=self.repo)
        self.assertEqual(out.returncode, 3)
        self.assertFalse(self.capture.exists())

    def test_commit_mode_puts_show_in_prompt(self):
        (self.repo / "c.py").write_text("print('커밋')\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c")
        sha = git(self.repo, "rev-parse", "HEAD")
        out = self.run_script("--commit", sha, "--out", str(self.out), cwd=self.repo)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("+print('커밋')", self.captured_stdin())

    def test_commit_mode_unknown_sha_exits_3(self):
        out = self.run_script("--commit", "0" * 40, "--out", str(self.out), cwd=self.repo)
        self.assertEqual(out.returncode, 3)

    def test_code_mode_accepts_focus_and_known(self):
        known = self.repo.parent / "known.md"
        known.write_text("- a.md:1 — 오래된 지적: 아님\n", encoding="utf-8")
        (self.repo / "a.md").write_text("바뀐 문서\n", encoding="utf-8")
        self.run_script("--uncommitted", "--focus", "동시성 문제", "--known", str(known),
                        "--out", str(self.out), cwd=self.repo)
        stdin = self.captured_stdin()
        self.assertIn("추가 관점: 동시성 문제", stdin)
        self.assertIn("- a.md:1 — 오래된 지적: 아님", stdin)

    def test_opinion_passes_prompt_file_verbatim(self):
        for mode in ("opinion", "recheck"):
            out = self.run_script(f"--{mode}", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(self.captured_stdin(), "재검토 프롬프트 본문\n둘째 줄")
            self.assertIn(f"mode={mode}", out.stdout)
            self.assertNotIn("summary=", out.stdout)
            args = self.captured_args()
            self.assertEqual(args[args.index("--workspace") + 1], str(self.repo))

    def test_large_prompt_is_passed_through_stdin(self):
        big = "diff 줄\n" * 300000
        self.prompt.write_text(big, encoding="utf-8")
        out = self.run_script("--recheck", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.captured_stdin(), big)

    def test_empty_prompt_file_exits_3_without_calling_cursor(self):
        empty = self.repo / "empty.md"
        empty.write_text("", encoding="utf-8")
        out = self.run_script("--opinion", str(empty), "--out", str(self.out))
        self.assertEqual(out.returncode, 3)
        self.assertFalse(self.capture.exists())

    def test_not_logged_in_exits_77_without_main_call(self):
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"FAKE_CURSOR_STATUS": "Not logged in"})
        self.assertEqual(out.returncode, 77)
        self.assertFalse(self.capture.exists())

    def test_usage_limit_exits_75(self):
        body = '{"type":"result","is_error":true,"result":"You\'ve hit your usage limit","session_id":"s"}'
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"FAKE_CURSOR_JSON": body, "FAKE_CURSOR_EXIT": "1"})
        self.assertEqual(out.returncode, 75)
        self.assertIn("exit=75", out.stdout)
        self.assertEqual(self.final_text(), "")
        self.assertIn("usage limit", self.out.read_text(encoding="utf-8"))

    def test_authentication_required_exits_77(self):
        body = '{"type":"result","is_error":true,"result":"Authentication required","session_id":"s"}'
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"FAKE_CURSOR_JSON": body, "FAKE_CURSOR_EXIT": "1"})
        self.assertEqual(out.returncode, 77)
        self.assertEqual(self.final_text(), "")
        self.assertIn("Authentication required", self.out.read_text(encoding="utf-8"))

    def test_plain_usage_limit_output_exits_75_with_empty_final(self):
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"FAKE_CURSOR_JSON": "You've hit your usage limit", "FAKE_CURSOR_EXIT": "1"})
        self.assertEqual(out.returncode, 75)
        self.assertEqual(self.final_text(), "")
        self.assertIn("usage limit", self.out.read_text(encoding="utf-8"))

    def test_plain_authentication_output_exits_77_with_empty_final(self):
        out = self.run_script("--opinion", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out),
                              env={"FAKE_CURSOR_JSON": "Not logged in", "FAKE_CURSOR_EXIT": "1"})
        self.assertEqual(out.returncode, 77)
        self.assertEqual(self.final_text(), "")
        self.assertIn("Not logged in", self.out.read_text(encoding="utf-8"))

    def test_other_error_envelope_keeps_final_empty(self):
        body = '{"type":"result","is_error":true,"result":"internal failure","session_id":"s"}'
        out = self.run_script("--recheck", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out),
                              env={"FAKE_CURSOR_JSON": body, "FAKE_CURSOR_EXIT": "1"})
        self.assertEqual(out.returncode, 1)
        self.assertEqual(self.final_text(), "")
        self.assertIn("internal failure", self.out.read_text(encoding="utf-8"))

    def test_review_text_mentioning_usage_limit_is_not_an_error(self):
        body = ('{"type":"result","is_error":false,"result":"usage limit 처리 누락\\nSUMMARY: critical=0 warning=1 suggestion=0",'
                '"usage":{"inputTokens":1,"outputTokens":1,"cacheReadTokens":0,"cacheWriteTokens":0}}')
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out), env={"FAKE_CURSOR_JSON": body})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("summary=critical=0 warning=1 suggestion=0", out.stdout)

    def test_cli_failure_without_json_keeps_cli_exit_code(self):
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"FAKE_CURSOR_JSON": "boom", "FAKE_CURSOR_EXIT": "7"})
        self.assertEqual(out.returncode, 7)
        self.assertIn("exit=7", out.stdout)
        self.assertTrue(Path(f"{self.out}.final").exists())

    def test_missing_cursor_agent_exits_69(self):
        path = ":".join(d for d in os.environ["PATH"].split(":") if d and not (Path(d) / "cursor-agent").exists())
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out), path=path)
        self.assertEqual(out.returncode, 69)

    def test_cursor_agent_env_overrides_path_lookup(self):
        other = self.bin / "other-agent"
        other.write_text(FAKE_CURSOR, encoding="utf-8")
        other.chmod(other.stat().st_mode | stat.S_IXUSR)
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"CURSOR_AGENT": str(other)}, path="/usr/bin:/bin")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_cascade_cursor_agent_alone_is_found_without_path_or_local_bin(self):
        other = self.bin / "other-agent"
        other.write_text(FAKE_CURSOR, encoding="utf-8")
        other.chmod(other.stat().st_mode | stat.S_IXUSR)
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"CASCADE_CURSOR_AGENT": str(other)}, path="/usr/bin:/bin")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.capture.exists())

    def test_cascade_cursor_agent_that_is_not_executable_exits_69_even_if_path_has_agent(self):
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"CASCADE_CURSOR_AGENT": str(self.bin / "no-such-agent")})
        self.assertEqual(out.returncode, 69)
        self.assertFalse(self.capture.exists())

    def test_cascade_cursor_agent_wins_over_cursor_agent(self):
        winner = self.bin / "winner-agent"
        winner.write_text(FAKE_CURSOR, encoding="utf-8")
        winner.chmod(winner.stat().st_mode | stat.S_IXUSR)
        loser = self.bin / "loser-agent"
        loser.write_text("#!/usr/bin/env bash\nexit 9\n", encoding="utf-8")
        loser.chmod(loser.stat().st_mode | stat.S_IXUSR)
        out = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out),
                              env={"CASCADE_CURSOR_AGENT": str(winner), "CURSOR_AGENT": str(loser)})
        self.assertEqual(out.returncode, 0, out.stderr)

    def assert_rerun_removes_previous_final(self, expected, doc=None, env=None):
        first = self.run_script("--doc", str(self.repo / "a.md"), "--out", str(self.out))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(self.final_text(), "")
        again = self.run_script("--doc", doc or str(self.repo / "a.md"), "--out", str(self.out), env=env)
        self.assertEqual(again.returncode, expected, again.stderr)
        final = Path(f"{self.out}.final")
        self.assertTrue(not final.exists() or final.read_text(encoding="utf-8") == "")

    def test_rerun_after_login_expired_removes_previous_final(self):
        self.assert_rerun_removes_previous_final(77, env={"FAKE_CURSOR_STATUS": "Not logged in"})

    def test_rerun_with_missing_agent_removes_previous_final(self):
        self.assert_rerun_removes_previous_final(69, env={"CASCADE_CURSOR_AGENT": str(self.bin / "no-such-agent")})

    def test_rerun_with_missing_doc_removes_previous_final(self):
        self.assert_rerun_removes_previous_final(3, doc=str(self.repo / "none.md"))

    def test_rerun_with_usage_limit_removes_previous_final(self):
        body = '{"type":"result","is_error":true,"result":"You\'ve hit your usage limit","session_id":"s"}'
        self.assert_rerun_removes_previous_final(75, env={"FAKE_CURSOR_JSON": body, "FAKE_CURSOR_EXIT": "1"})

    def test_relative_agent_path_survives_cd_to_repo(self):
        caller = self.repo.parent
        args = ("--opinion", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out))
        out = self.run_script(*args, cwd=caller, env={"CASCADE_CURSOR_AGENT": "bin/cursor-agent"})
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.run_script(*args, cwd=caller, path=f"bin:{os.environ['PATH']}")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_success_envelope_with_nonzero_exit_keeps_final_empty(self):
        out = self.run_script("--opinion", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out),
                              env={"FAKE_CURSOR_EXIT": "7"})
        self.assertEqual(out.returncode, 7)
        self.assertIn("exit=7", out.stdout)
        self.assertEqual(self.final_text(), "")

    def test_output_format_example_matches_finding_parser(self):
        (self.repo / "a.md").write_text("문서 바꿈\n", encoding="utf-8")
        for args in (("--doc", str(self.repo / "a.md")), ("--uncommitted",)):
            out = self.run_script(*args, "--out", str(self.out), cwd=self.repo)
            self.assertEqual(out.returncode, 0, out.stderr)
            stdin = self.captured_stdin()
            self.assertIn("번호를 붙이고", stdin)
            example = stdin.split("예:\n", 1)[1].split("SUMMARY:", 1)[0]
            [finding] = parse_codex_doc(example, 1)
            self.assertEqual(finding.severity, "Critical")

    def test_relative_out_is_written_and_removed_from_caller_dir(self):
        caller = self.repo.parent
        args = ("--opinion", str(self.prompt), "--repo", str(self.repo), "--out", "out.log")
        first = self.run_script(*args, cwd=caller)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(f"full_log={caller / 'out.log'}\n", first.stdout)
        self.assertIn(f"final={caller / 'out.log.final'}\n", first.stdout)
        self.assertNotEqual((caller / "out.log.final").read_text(encoding="utf-8"), "")
        self.assertFalse((self.repo / "out.log.final").exists())
        unrelated = self.repo / "out.log.final"
        unrelated.write_text("저장소에 있던 다른 파일\n", encoding="utf-8")
        again = self.run_script(*args, cwd=caller, env={"FAKE_CURSOR_STATUS": "Not logged in"})
        self.assertEqual(again.returncode, 77, again.stderr)
        self.assertFalse((caller / "out.log.final").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "저장소에 있던 다른 파일\n")

    def test_out_in_missing_directory_exits_64_without_calling_cursor(self):
        for out_path in ("no-such-dir/out.log", str(self.repo.parent / "no-such-dir" / "out.log")):
            out = self.run_script("--doc", str(self.repo / "a.md"), "--out", out_path, cwd=self.repo.parent)
            self.assertEqual(out.returncode, 64, out.stderr)
            self.assertIn("no-such-dir", out.stderr)
            self.assertFalse(self.capture.exists())

    def test_known_list_is_included_for_doc(self):
        known = self.repo.parent / "known.md"
        known.write_text("- a.md:1 — 이미 기각한 지적: 아님\n", encoding="utf-8")
        self.run_script("--doc", str(self.repo / "a.md"), "--known", str(known), "--out", str(self.out))
        stdin = self.captured_stdin()
        self.assertIn("- a.md:1 — 이미 기각한 지적: 아님", stdin)
        self.assertIn("기각 목록 시작", stdin)

    def test_missing_doc_exits_3(self):
        out = self.run_script("--doc", str(self.repo / "none.md"), "--out", str(self.out))
        self.assertEqual(out.returncode, 3)

    def test_usage_lists_modes(self):
        out = self.run_script()
        self.assertEqual(out.returncode, 64)
        for opt in ("--uncommitted", "--base", "--commit", "--doc", "--recheck", "--opinion", "--model"):
            self.assertIn(opt, out.stderr)


if __name__ == "__main__":
    unittest.main()
