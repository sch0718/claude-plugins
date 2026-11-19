import os
import shutil
import subprocess
from pathlib import Path

import pytest

from doclookup.config import load
from doclookup.index import ensure, open_index

FIXTURES = Path(__file__).parent / "fixtures"

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], env=GIT_ENV, capture_output=True, text=True, check=True
    ).stdout


def make_repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path.resolve()


@pytest.fixture
def tmp_git_repo_without_remote(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "noremote")


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "withremote", "git@github.com:Org/Repo.git")


@pytest.fixture
def worktree_of_repo_without_remote(tmp_path: Path) -> Path:
    main = make_repo(tmp_path / "mainrepo")
    (main / "a.md").write_text("# a\n", encoding="utf-8")
    git(main, "add", "a.md")
    git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt-elsewhere"
    git(main, "worktree", "add", "-q", str(wt))
    return wt.resolve()


@pytest.fixture
def submodule_checkout(tmp_path: Path) -> Path:
    sub = make_repo(tmp_path / "sub")
    git(sub, "commit", "-q", "--allow-empty", "-m", "init")
    parent = make_repo(tmp_path / "parent")
    git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "docs")
    return parent / "docs"


@pytest.fixture
def separate_git_dir_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "docs"
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(tmp_path / "meta"), str(checkout)],
        env=GIT_ENV, capture_output=True, check=True,
    )
    return checkout.resolve()


@pytest.fixture
def open_fixture(tmp_path: Path):
    """tests/fixtures/<이름> 묶음을 tmp_path에 복사해 색인하고 연다. 설정은 tests/fixtures/<이름>.toml."""
    opened = []

    def make(name: str):
        root = tmp_path / name
        if not root.exists():
            shutil.copytree(FIXTURES / name, root)
        cfg = load(FIXTURES / f"{name}.toml")
        out = tmp_path / "cache" / f"{name}.sqlite"
        ensure(cfg, root.resolve(), out)
        ix = open_index(cfg, root.resolve(), out)
        opened.append(ix)
        return ix

    yield make
    for ix in opened:
        ix.conn.close()
