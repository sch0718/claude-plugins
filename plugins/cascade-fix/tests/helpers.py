import os
import re
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent


def cursor_read_denied(deny, path):
    """Cursor처럼 Read(...) 패턴을 다듬어 `*`만 아무 문자열로 보고 요청 경로 전체와 대소문자 구분해 맞춘다."""
    patterns = [rule.strip()[len("Read("):-1].strip() for rule in deny if rule.strip().startswith("Read(")]
    return any(re.fullmatch(re.escape(p).replace(r"\*", ".*"), str(path)) for p in patterns)


def git(repo, *args):
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return p.stdout.strip()


def make_repo(tmp, files):
    repo = Path(tmp)
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "commit.gpgsign", "false")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def run_cli(args, cwd, env=None):
    full_env = dict(os.environ)
    full_env["CASCADE_REVIEWER"] = "codex"  # 기존 테스트는 Codex 경로를 본다. Claude 경로는 env로 덮어쓴다
    full_env["CASCADE_WORKERS"] = "claude"  # 테스트가 덮어쓰지 않는 한 작업자는 Claude다
    for name in ("CASCADE_CODEX", "CASCADE_CURSOR_AGENT"):  # 테스트가 덮어쓰지 않는 한 진짜 CLI를 찾지 못한다
        full_env[name] = str(SKILL_ROOT / "tests" / "no-such-cli")
    if env:
        full_env.update(env)
    full_env["PYTHONPATH"] = str(SKILL_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "cascade.cli", *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
    )


def _toml_value(value):
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_value(k)} = {_toml_value(v)}" for k, v in value.items()) + "}"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_repos_toml(tmp, mapping):
    lines = []
    for root, fields in mapping.items():
        lines.append(f"[repos.{_toml_value(root)}]")
        for key, value in fields.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    path = Path(tmp) / "repos.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
