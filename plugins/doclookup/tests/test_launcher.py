import os
import subprocess
import sys
from pathlib import Path

import pytest

from doclookup import __version__

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "skills" / "doclookup" / "bin" / "doclookup"

FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
case "$FAKE_UV_MODE" in
  fail) exit 1 ;;
  exit7) case " $* " in *" -c "*) ;; *) exit 7 ;; esac ;;
esac
while [ $# -gt 0 ] && [ "$1" != python ]; do shift; done
shift
exec "{python}" "$@"
"""

FAKE_PYTHON3 = """#!/bin/sh
printf '%s\\n' "$*" >> "$PY_LOG"
{body}
"""


def make_bin(tmp_path: Path, uv_mode: str | None, python3: str | None) -> tuple[Path, dict]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    if uv_mode is not None:
        uv = fakebin / "uv"
        uv.write_text(FAKE_UV.replace("{python}", sys.executable), encoding="utf-8")
        uv.chmod(0o755)
    if python3 is not None:
        body = f'exec "{sys.executable}" "$@"' if python3 == "ok" else "exit 1"
        py = fakebin / "python3"
        py.write_text(FAKE_PYTHON3.replace("{body}", body), encoding="utf-8")
        py.chmod(0o755)
    env = {
        "HOME": os.environ["HOME"],
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "UV_LOG": str(tmp_path / "uv.log"),
        "PY_LOG": str(tmp_path / "py.log"),
        "FAKE_UV_MODE": uv_mode or "",
    }
    return fakebin, env


def run(args: list[str], env: dict, launcher: Path = LAUNCHER) -> subprocess.CompletedProcess:
    return subprocess.run([str(launcher), *args], env=env, capture_output=True, text=True, timeout=30)


def log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_launcher_runs_version():
    out = subprocess.run([LAUNCHER, "--version"], capture_output=True, text=True, check=True).stdout
    assert out.strip() == f"doclookup {__version__}"


def test_launcher_passes_no_python_downloads_to_every_uv_call(tmp_path):
    _, env = make_bin(tmp_path, "ok", None)
    result = run(["--version"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"doclookup {__version__}"
    calls = log_lines(tmp_path / "uv.log")
    assert len(calls) == 2
    assert all("--no-python-downloads" in call and "--no-project" in call for call in calls)
    assert calls[-1].endswith("python -P -m doclookup --version")


def test_launcher_falls_back_to_python3_when_uv_check_fails(tmp_path):
    _, env = make_bin(tmp_path, "fail", "ok")
    result = run(["--version"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"doclookup {__version__}"
    assert len(log_lines(tmp_path / "uv.log")) == 1
    assert log_lines(tmp_path / "py.log")[-1] == "-P -m doclookup --version"


def test_launcher_uses_python3_without_uv(tmp_path):
    _, env = make_bin(tmp_path, None, "ok")
    result = run(["--version"], env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"doclookup {__version__}"


@pytest.mark.parametrize("uv_mode", ["fail", None])
def test_launcher_without_suitable_python_exits_2(tmp_path, uv_mode):
    _, env = make_bin(tmp_path, uv_mode, "old")
    result = run(["--version"], env)
    assert result.returncode == 2
    assert "doclookup: Python 3.11 이상이 필요하다" in result.stderr
    assert result.stdout == ""


def test_launcher_passes_exit_code_without_retry(tmp_path):
    _, env = make_bin(tmp_path, "exit7", "ok")
    result = run(["search", "x"], env)
    assert result.returncode == 7
    assert len(log_lines(tmp_path / "uv.log")) == 2
    assert log_lines(tmp_path / "py.log") == []


def test_launcher_resolves_symlink(tmp_path):
    _, env = make_bin(tmp_path, "ok", None)
    link = tmp_path / "elsewhere" / "doclookup"
    link.parent.mkdir()
    link.symlink_to(LAUNCHER)
    result = run(["--version"], env, launcher=link)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"doclookup {__version__}"


def test_launcher_ignores_doclookup_module_in_working_directory(tmp_path):
    shadow = tmp_path / "work"
    shadow.mkdir()
    (shadow / "doclookup.py").write_text('print("SHADOW")\n', encoding="utf-8")
    result = subprocess.run([str(LAUNCHER), "--version"], cwd=shadow, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"doclookup {__version__}"
