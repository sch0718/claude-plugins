import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import skill_scripts  # noqa: E402


class SkillScriptsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_bundled_files_exist(self):
        for name, files in skill_scripts.SKILLS.items():
            for src in files.values():
                self.assertTrue(src.is_file(), src)

    def test_runner_skills(self):
        self.assertEqual(skill_scripts.RUNNER_SKILLS, {"codex": "codex-review", "cursor": "cursor-review"})
        self.assertEqual(set(skill_scripts.RUNNER_SKILLS.values()), set(skill_scripts.SKILLS))

    def test_codex_skill_md_documents_opinion_and_recheck(self):
        text = skill_scripts.SKILLS["codex-review"]["SKILL.md"].read_text(encoding="utf-8")
        self.assertIn("--opinion", text)
        self.assertIn("--recheck", text)

    def test_compare_missing_then_install_then_same(self):
        target = self.tmp / "codex-review"
        self.assertEqual(skill_scripts.compare("codex-review", target)[0], skill_scripts.MISSING)
        self.assertEqual(skill_scripts.install("codex-review", target, "20261023"), [])
        self.assertEqual(skill_scripts.compare("codex-review", target)[0], skill_scripts.SAME)
        self.assertTrue(os.access(target / "scripts" / "run-review.sh", os.X_OK))

    def test_compare_missing_lists_each_file(self):
        status, lines = skill_scripts.compare("cursor-review", self.tmp / "cursor-review")
        self.assertEqual(status, skill_scripts.MISSING)
        self.assertEqual(lines, ["SKILL.md: 없음", "scripts/run-review.sh: 없음"])

    def test_install_backs_up_changed_file(self):
        target = self.tmp / "cursor-review"
        skill_scripts.install("cursor-review", target, "20261023")
        script = target / "scripts" / "run-review.sh"
        script.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
        backups = skill_scripts.install("cursor-review", target, "20261024")
        self.assertEqual(backups, [target / "scripts" / "run-review.sh.bak-20261024"])
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "#!/bin/sh\necho local\n")
        self.assertEqual(script.read_bytes(), skill_scripts.SKILLS["cursor-review"]["scripts/run-review.sh"].read_bytes())
        self.assertEqual(skill_scripts.compare("cursor-review", target)[0], skill_scripts.SAME)

    def test_install_leaves_unchanged_file_without_backup(self):
        target = self.tmp / "cursor-review"
        skill_scripts.install("cursor-review", target, "20261023")
        self.assertEqual(skill_scripts.install("cursor-review", target, "20261024"), [])
        self.assertEqual(list(target.rglob("*.bak-*")), [])

    def test_install_makes_script_executable_even_when_content_same(self):
        target = self.tmp / "cursor-review"
        skill_scripts.install("cursor-review", target, "20261023")
        script = target / "scripts" / "run-review.sh"
        script.chmod(0o644)
        skill_scripts.install("cursor-review", target, "20261024")
        self.assertTrue(os.access(script, os.X_OK))

    def test_compare_script_without_exec_bit_is_different(self):
        target = self.tmp / "codex-review"
        skill_scripts.install("codex-review", target, "20261023")
        (target / "scripts" / "run-review.sh").chmod(0o644)
        self.assertEqual(skill_scripts.compare("codex-review", target),
                         (skill_scripts.DIFFERENT, ["scripts/run-review.sh: 실행 권한 없음"]))

    def test_compare_reports_line_counts(self):
        target = self.tmp / "codex-review"
        skill_scripts.install("codex-review", target, "20261023")
        script = target / "scripts" / "run-review.sh"
        script.write_bytes(script.read_bytes() + b"# extra\n")
        status, lines = skill_scripts.compare("codex-review", target)
        self.assertEqual(status, skill_scripts.DIFFERENT)
        self.assertEqual(lines, ["scripts/run-review.sh: 다름(+1/-0줄)"])

    def test_compare_counts_removed_lines(self):
        target = self.tmp / "codex-review"
        skill_scripts.install("codex-review", target, "20261023")
        skill = target / "SKILL.md"
        skill.write_text("대체 본문\n", encoding="utf-8")
        status, lines = skill_scripts.compare("codex-review", target)
        self.assertEqual(status, skill_scripts.DIFFERENT)
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^SKILL\.md: 다름\(\+1/-\d+줄\)$")

    def test_compare_partly_missing_is_different(self):
        target = self.tmp / "cursor-review"
        skill_scripts.install("cursor-review", target, "20261023")
        (target / "SKILL.md").unlink()
        status, lines = skill_scripts.compare("cursor-review", target)
        self.assertEqual(status, skill_scripts.DIFFERENT)
        self.assertEqual(lines, ["SKILL.md: 없음"])

    def test_skill_dir_from_script_path(self):
        script = Path("/home/u/.claude/skills/codex-review/scripts/run-review.sh")
        self.assertEqual(skill_scripts.skill_dir(script), Path("/home/u/.claude/skills/codex-review"))


if __name__ == "__main__":
    unittest.main()
