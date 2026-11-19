import io
import json
import os
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import make_repo
from doclookup import hint

CONFIG = """\
match = ["docs-repo"]

[files]
markdown = ["**/*.md"]
text = ["tools/**/*.py"]
exclude = ["_workspace/**"]

[hint]
enabled = {enabled}
min_chars = 100
"""


@dataclass
class Setup:
    root: Path
    env: dict[str, str]
    bundled: Path
    config: Path

    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def configure(self, enabled: bool) -> None:
        self.config.write_text(CONFIG.format(enabled="true" if enabled else "false"), encoding="utf-8")

    def run(self, file_path: str | Path, cwd: Path | None = None, **tool_input) -> tuple[str, int]:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "cwd": str(cwd or self.root),
            "tool_input": {"file_path": str(file_path), **tool_input},
        }
        return hint.run(json.dumps(payload), cwd or self.root, self.env, self.bundled)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    root = make_repo(tmp_path / "docs-repo")
    configs = tmp_path / "configs"
    configs.mkdir()
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    s = Setup(root, {"DOCLOOKUP_CONFIG_DIR": str(configs)}, bundled, configs / "docs.toml")
    s.configure(enabled=True)
    return s


def guidance(out: str) -> str:
    data = json.loads(out)
    specific = data["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in specific
    assert set(data) == {"hookSpecificOutput"}
    return specific["additionalContext"]


def test_hint_silent_when_disabled(setup):
    big = setup.write("docs/큰.md", "가" * 500)
    setup.configure(enabled=False)
    assert setup.run(big) == ("", 0)


def test_hint_silent_by_default_when_hint_table_missing(setup):
    big = setup.write("docs/큰.md", "가" * 50000)
    setup.config.write_text('match = ["docs-repo"]\n\n[files]\nmarkdown = ["**/*.md"]\n', encoding="utf-8")
    assert setup.run(big) == ("", 0)


def test_hint_silent_for_small_or_out_of_scope_file(setup, tmp_path):
    small = setup.write("docs/작은.md", "가" * 99)
    excluded = setup.write("_workspace/초안.md", "가" * 500)
    other_type = setup.write("docs/notes.txt", "가" * 500)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("가" * 500, encoding="utf-8")
    for path in (small, excluded, other_type, outside):
        assert setup.run(path) == ("", 0), path


def test_hint_emits_guidance_for_big_file(setup):
    big = setup.write("docs/큰.md", "가" * 120)
    out, code = setup.run(big)
    assert code == 0
    assert guidance(out) == (
        "이 파일은 120자다. 먼저 doclookup search·section·impact로 필요한 범위를 찾고 Read의 offset·limit으로 읽는다."
    )
    assert "doclookup search" in guidance(out)


def test_hint_emits_for_text_target(setup):
    script = setup.write("tools/make.py", "x = 1\n" * 30)
    out, code = setup.run(script)
    assert code == 0 and "180자" in guidance(out)


def test_hint_counts_characters_not_bytes(setup):
    just_below = setup.write("docs/아래.md", "가" * 99)
    at_limit = setup.write("docs/기준.md", "가" * 100)
    assert setup.run(just_below) == ("", 0)
    out, code = setup.run(at_limit)
    assert code == 0 and "100자" in guidance(out)


def test_hint_silent_when_read_already_limited(setup):
    big = setup.write("docs/큰.md", "가" * 500)
    assert setup.run(big, offset=10) == ("", 0)
    assert setup.run(big, limit=50) == ("", 0)


def test_hint_uses_checkout_of_file_not_cwd(setup, tmp_path):
    big = setup.write("docs/큰.md", "가" * 500)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out, code = setup.run(big, cwd=elsewhere)
    assert code == 0 and "500자" in guidance(out)


def test_hint_resolves_relative_path_against_cwd(setup):
    setup.write("docs/큰.md", "가" * 500)
    out, code = setup.run("docs/큰.md", cwd=setup.root)
    assert code == 0 and "500자" in guidance(out)


def test_hint_matches_nfd_file_name_given_as_nfc(setup):
    nfd_name = unicodedata.normalize("NFD", "지침.md")
    setup.write(f"docs/{nfd_name}", "가" * 500)
    nfc_path = setup.root / "docs" / unicodedata.normalize("NFC", "지침.md")
    if not nfc_path.is_file():
        pytest.skip("이 파일 시스템은 NFC·NFD 이름을 같은 파일로 열지 않는다")
    out, code = setup.run(nfc_path)
    assert code == 0 and "500자" in guidance(out)


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[]",
        '{"tool_input": "x"}',
        '{"tool_input": {}}',
        '{"tool_input": {"file_path": 3}}',
        '{"tool_input": {"file_path": ""}}',
        '{"tool_input": {"file_path": "/없는/파일.md"}}',
    ],
)
def test_hint_never_fails_on_garbage_stdin(setup, stdin):
    assert hint.run(stdin, setup.root, setup.env, setup.bundled) == ("", 0)


def test_hint_silent_for_non_utf8_file(setup):
    bad = setup.write("docs/깨짐.md", b"\xff\xfe" * 300)
    assert setup.run(bad) == ("", 0)


def test_hint_silent_when_config_missing_or_broken(setup):
    big = setup.write("docs/큰.md", "가" * 500)
    setup.config.write_text("match = [", encoding="utf-8")
    assert setup.run(big) == ("", 0)
    setup.config.unlink()
    assert setup.run(big) == ("", 0)
    assert hint.run("{}", setup.root, {"DOCLOOKUP_CONFIG_DIR": "/없는/폴더"}, setup.bundled) == ("", 0)


def test_hint_swallows_unexpected_errors(setup, monkeypatch):
    big = setup.write("docs/큰.md", "가" * 500)

    def boom(*args, **kwargs):
        raise RuntimeError("뜻밖의 오류")

    monkeypatch.setattr(hint, "target_files", boom)
    assert setup.run(big) == ("", 0)


ROOT = Path(__file__).resolve().parent.parent


def test_hooks_json_runs_launcher_on_read_without_blocking():
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = data["hooks"]["PreToolUse"]
    assert [e["matcher"] for e in entries] == ["Read"]
    [command] = entries[0]["hooks"]
    assert command["type"] == "command"
    assert command["command"] == '"${CLAUDE_PLUGIN_ROOT}"/skills/doclookup/bin/doclookup hint 2>/dev/null || true'
    assert 0 < command["timeout"] <= 30
    assert (ROOT / "skills" / "doclookup" / "bin" / "doclookup").is_file()
    assert set(data["hooks"]) == {"PreToolUse"}


def test_hook_command_never_exits_nonzero(tmp_path):
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT), "PATH": "/usr/bin:/bin"}
    for stdin in ("not json", json.dumps({"tool_input": {"file_path": str(tmp_path / "없음.md")}})):
        result = subprocess.run(["sh", "-c", command], input=stdin, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_skill_md_has_frontmatter_and_all_commands():
    text = (ROOT / "skills" / "doclookup" / "SKILL.md").read_text(encoding="utf-8")
    head, _, body = text.removeprefix("---\n").partition("\n---\n")
    assert "name: doclookup" in head.splitlines()
    assert any(line.startswith("description: ") for line in head.splitlines())
    for command in ("search", "section", "id", "refs", "impact", "variants", "roles", "links"):
        assert f"| `{command}" in body, command
    assert "bin/doclookup" in body
    assert "DX" not in text


def hook_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """실행기가 uv 없이 PATH의 python3(지금 시험을 돌리는 Python)로 doclookup을 부르게 한다."""
    fakebin = tmp_path / "fakebin"
    if not fakebin.is_dir():
        fakebin.mkdir()
        python3 = fakebin / "python3"
        python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        python3.chmod(0o755)
    return {"HOME": os.environ["HOME"], "PATH": f"{fakebin}:/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(ROOT), **extra}


def run_hook(setup: Setup, tmp_path: Path, file_path: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    data = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "cwd": str(cwd or setup.root),
        "tool_input": {"file_path": str(file_path)},
    }
    return subprocess.run(["sh", "-c", command], input=json.dumps(payload, ensure_ascii=False),
                          env=hook_env(tmp_path, **setup.env), cwd=cwd or setup.root,
                          capture_output=True, text=True, encoding="utf-8", timeout=30)


def test_hook_command_emits_guidance_for_big_file(setup, tmp_path):
    big = setup.write("docs/큰.md", "가" * 120)
    result = run_hook(setup, tmp_path, big)
    assert (result.returncode, result.stderr) == (0, "")
    assert guidance(result.stdout) == hint.MESSAGE.format(n=120)


def test_hook_command_resolves_relative_path_against_hook_cwd(setup, tmp_path):
    setup.write("docs/큰.md", "가" * 500)
    result = run_hook(setup, tmp_path, "docs/큰.md")
    assert result.returncode == 0 and "500자" in guidance(result.stdout)


def test_hook_command_silent_when_disabled_or_small(setup, tmp_path):
    small = setup.write("docs/작은.md", "가" * 99)
    big = setup.write("docs/큰.md", "가" * 500)
    assert (run_hook(setup, tmp_path, small).returncode, run_hook(setup, tmp_path, small).stdout) == (0, "")
    setup.configure(enabled=False)
    result = run_hook(setup, tmp_path, big)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("stdin", ["", "not json", '{"tool_input": {"file_path": "/없는/파일.md"}}'])
def test_launcher_hint_exits_zero_without_fallback(tmp_path, stdin):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    env = hook_env(tmp_path, DOCLOOKUP_CONFIG_DIR=str(tmp_path / "없는-폴더"))
    result = subprocess.run([str(ROOT / "skills" / "doclookup" / "bin" / "doclookup"), "hint"], input=stdin,
                            env=env, cwd=outside, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_cli_hint_prints_guidance_and_returns_code(setup, monkeypatch, capsys):
    from doclookup import cli

    big = setup.write("docs/큰.md", "가" * 120)
    payload = {"cwd": str(setup.root), "tool_input": {"file_path": str(big)}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(cli, "BUNDLED", setup.bundled)
    monkeypatch.setenv("DOCLOOKUP_CONFIG_DIR", setup.env["DOCLOOKUP_CONFIG_DIR"])
    monkeypatch.chdir(setup.root)
    assert cli.main(["hint"]) == 0
    out, err = capsys.readouterr()
    assert err == "" and out.endswith("\n") and guidance(out) == hint.MESSAGE.format(n=120)


def test_cli_hint_silent_when_nothing_to_say(setup, monkeypatch, capsys):
    from doclookup import cli

    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    monkeypatch.chdir(setup.root)
    assert cli.main(["hint"]) == 0
    assert capsys.readouterr() == ("", "")
