import difflib
import os
import shutil
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent

SKILLS = {
    "codex-review": {
        "SKILL.md": SKILL_ROOT / "docs" / "codex-review-changes" / "SKILL.md",
        "scripts/run-review.sh": SKILL_ROOT / "docs" / "codex-review-changes" / "run-review.sh",
    },
    "cursor-review": {
        "SKILL.md": SKILL_ROOT / "docs" / "cursor-review" / "SKILL.md",
        "scripts/run-review.sh": SKILL_ROOT / "docs" / "cursor-review" / "run-review.sh",
    },
}
RUNNER_SKILLS = {"codex": "codex-review", "cursor": "cursor-review"}
SAME, DIFFERENT, MISSING = "같음", "다름", "없음"


def skill_dir(script: Path) -> Path:
    return script.parent.parent


def _line_counts(source: Path, installed: Path) -> tuple[int, int]:
    old = source.read_text(encoding="utf-8", errors="replace").splitlines()
    new = installed.read_text(encoding="utf-8", errors="replace").splitlines()
    added = removed = 0
    for line in difflib.ndiff(old, new):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


def compare(name: str, target: Path) -> tuple[str, list[str]]:
    states = []
    lines = []
    for rel, source in SKILLS[name].items():
        installed = target / rel
        if not installed.is_file():
            states.append(MISSING)
            lines.append(f"{rel}: {MISSING}")
        elif installed.read_bytes() == source.read_bytes():
            if installed.suffix == ".sh" and not os.access(installed, os.X_OK):
                states.append(DIFFERENT)
                lines.append(f"{rel}: 실행 권한 없음")
            else:
                states.append(SAME)
        else:
            added, removed = _line_counts(source, installed)
            states.append(DIFFERENT)
            lines.append(f"{rel}: {DIFFERENT}(+{added}/-{removed}줄)")
    if all(state == SAME for state in states):
        return SAME, []
    if all(state == MISSING for state in states):
        return MISSING, lines
    return DIFFERENT, lines


def install(name: str, target: Path, stamp: str) -> list[Path]:
    backups = []
    for rel, source in SKILLS[name].items():
        dest = target / rel
        content = source.read_bytes()
        if dest.is_file() and dest.read_bytes() != content:
            backup = dest.with_name(f"{dest.name}.bak-{stamp}")
            shutil.copy2(dest, backup)
            backups.append(backup)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        if dest.suffix == ".sh":
            dest.chmod(0o755)
    return backups
