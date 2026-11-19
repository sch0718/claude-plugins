import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade.findings import parse_codex_doc  # noqa: E402
from helpers import make_repo  # noqa: E402

# CASCADE_RUN_REVIEW를 주면 그 스크립트를 시험한다(예: docs/codex-review-changes/run-review.sh)
SCRIPT = Path(os.environ.get("CASCADE_RUN_REVIEW")
              or Path.home() / ".claude" / "skills" / "codex-review" / "scripts" / "run-review.sh")

FAKE_CODEX = """#!/usr/bin/env bash
out_file=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out_file="$a"; fi
  prev="$a"
done
for a in "$@"; do printf '%s\\0' "$a"; done > "$FAKE_CODEX_OUT"
cp "$CODEX_HOME/config.toml" "$FAKE_CODEX_OUT.config"
last=""
for a in "$@"; do last="$a"; done
[ "$last" = "-" ] && cat > "$FAKE_CODEX_OUT.stdin"
[ -n "$out_file" ] && echo "fake result" > "$out_file"
echo "fake log"
exit 0
"""


@unittest.skipUnless(SCRIPT.is_file(), f"codex-review 스킬이 없다: {SCRIPT}")
class RunReviewModesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
        self.bin = base / "bin"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text(FAKE_CODEX, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.capture = base / "captured.txt"
        self.repo = make_repo(base / "repo", {"a.md": "문서\n"})
        self.repo = self.repo.resolve()
        self.prompt = base / "prompt.md"
        self.prompt.write_text("재검토 프롬프트 본문\n둘째 줄", encoding="utf-8")
        self.out = base / "out.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_CODEX_OUT"] = str(self.capture)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def captured_args(self):
        return self.capture.read_bytes().decode("utf-8").split("\0")[:-1]

    def stdin_text(self):
        return Path(f"{self.capture}.stdin").read_text(encoding="utf-8")

    def assert_exec_command(self, mode):
        proc = self.run_script(f"--{mode}", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.captured_args()
        self.assertEqual(args[0], "exec")
        self.assertIn("-s", args)
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        self.assertIn("--ephemeral", args)
        self.assertEqual(args[args.index("-C") + 1], str(self.repo))
        self.assertEqual(args[args.index("-o") + 1], f"{self.out}.final")
        self.assertEqual(args[-1], "-")
        self.assertEqual(self.stdin_text(), "재검토 프롬프트 본문\n둘째 줄")
        self.assertIn(f"mode={mode}", proc.stdout)
        self.assertIn("exit=0", proc.stdout)
        self.assertIn(f"full_log={self.out}", proc.stdout)
        self.assertIn(f"final={self.out}.final", proc.stdout)
        self.assertIn("mcp_calls=0", proc.stdout)
        self.assertIn("git_dirty=", proc.stdout)
        self.assertEqual((Path(f"{self.out}.final")).read_text(encoding="utf-8").strip(), "fake result")

    def test_run_review_recheck_mode_builds_exec_command(self):
        self.assert_exec_command("recheck")

    def test_run_review_opinion_mode_builds_exec_command(self):
        self.assert_exec_command("opinion")

    def test_codex_home_turns_off_skill_list(self):
        proc = self.run_script("--opinion", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = Path(f"{self.capture}.config").read_text(encoding="utf-8")
        self.assertIn("[skills]\ninclude_instructions = false\n", config)
        self.assertIn(f'[projects."{self.repo}"]\ntrust_level = "trusted"', config)

    def test_large_recheck_prompt_is_not_passed_as_argument(self):
        big = "diff 줄\n" * 300000
        self.prompt.write_text(big, encoding="utf-8")
        proc = self.run_script("--recheck", str(self.prompt), "--repo", str(self.repo), "--out", str(self.out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.captured_args()[-1], "-")
        self.assertEqual(self.stdin_text(), big)

    def test_recheck_root_defaults_to_prompt_file_repo(self):
        prompt = self.repo / "p.md"
        prompt.write_text("본문", encoding="utf-8")
        proc = self.run_script("--recheck", str(prompt), "--out", str(self.out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.captured_args()
        self.assertEqual(args[args.index("-C") + 1], str(self.repo))

    def test_recheck_missing_prompt_file_exits_3(self):
        proc = self.run_script("--recheck", str(self.prompt) + ".none", "--out", str(self.out))
        self.assertEqual(proc.returncode, 3)

    def test_recheck_empty_prompt_file_exits_3_without_calling_codex(self):
        empty = self.repo / "empty.md"
        empty.write_text("", encoding="utf-8")
        for mode in ("recheck", "opinion"):
            proc = self.run_script(f"--{mode}", str(empty), "--repo", str(self.repo), "--out", str(self.out))
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertIn("비어 있다", proc.stderr)
        self.assertFalse(self.capture.exists())

    def test_recheck_unreadable_prompt_file_exits_3_without_calling_codex(self):
        locked = self.repo / "locked.md"
        locked.write_text("본문", encoding="utf-8")
        locked.chmod(0)
        try:
            if os.access(locked, os.R_OK):
                self.skipTest("root 권한이라 chmod 000이 읽기를 막지 못한다")
            proc = self.run_script("--recheck", str(locked), "--repo", str(self.repo), "--out", str(self.out))
        finally:
            locked.chmod(0o644)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("읽을 수 없거나", proc.stderr)
        self.assertFalse(self.capture.exists())

    def test_recheck_missing_repo_exits_3_without_calling_codex(self):
        proc = self.run_script("--recheck", str(self.prompt), "--repo", str(self.repo) + "-none", "--out", str(self.out))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("저장소 디렉터리", proc.stderr)
        self.assertFalse(self.capture.exists())

    def test_doc_mode_still_works_with_fake_codex(self):
        doc = self.repo / "a.md"
        proc = self.run_script("--doc", str(doc), "--out", str(self.out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.captured_args()
        self.assertEqual(args[0], "exec")
        self.assertEqual(args[args.index("-C") + 1], str(self.repo))
        self.assertIn("설계 문서를 리뷰하라", args[-1])
        self.assertIn("mode=doc kind=spec", proc.stdout)

    def test_doc_output_format_example_matches_finding_parser(self):
        doc = self.repo / "a.md"
        for kind in ("spec", "plan"):
            proc = self.run_script("--doc", str(doc), "--kind", kind, "--out", str(self.out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            prompt = self.captured_args()[-1]
            self.assertIn("번호를 붙이고", prompt)
            example = prompt.split("예:\n", 1)[1].split("SUMMARY:", 1)[0]
            [finding] = parse_codex_doc(example, 1)
            self.assertEqual(finding.severity, "Critical")

    def test_usage_lists_new_modes(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 64)
        self.assertIn("--recheck", proc.stderr)
        self.assertIn("--opinion", proc.stderr)


if __name__ == "__main__":
    unittest.main()
