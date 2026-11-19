import hashlib
import stat
from pathlib import Path

import pytest

from doclookup.paths import cache_root, checkout_root, index_path, normalize_remote, repo_keys


def test_normalize_remote_variants():
    for url in ["git@github.com:Org/Repo.git", "https://github.com/Org/Repo", "ssh://git@github.com/Org/Repo.git"]:
        assert normalize_remote(url) == "github.com/Org/Repo"


@pytest.mark.parametrize(
    "url",
    [
        "https://GitHub.com/Org/Repo.git/",
        "https://user:secret-token@github.com/Org/Repo.git",
        "ssh://git@github.com:22/Org/Repo",
        "  git@GITHUB.COM:Org/Repo.git\n",
    ],
)
def test_normalize_remote_drops_credentials_port_and_case_of_host(url):
    assert normalize_remote(url) == "github.com/Org/Repo"


def test_normalize_remote_keeps_nested_groups():
    assert normalize_remote("git@gitlab.example.com:Group/Sub/Repo.git") == "gitlab.example.com/Group/Sub/Repo"


def test_repo_keys_without_remote_uses_main_checkout_name(tmp_git_repo_without_remote):
    assert repo_keys(tmp_git_repo_without_remote) == [tmp_git_repo_without_remote.name]


def test_repo_keys_with_remote_puts_origin_first(repo_with_remote):
    assert repo_keys(repo_with_remote) == ["github.com/Org/Repo", repo_with_remote.name]


def test_repo_keys_in_worktree_uses_main_checkout_name(worktree_of_repo_without_remote):
    assert repo_keys(worktree_of_repo_without_remote) == ["mainrepo"]


def test_repo_keys_in_submodule_uses_checkout_name(submodule_checkout):
    assert repo_keys(submodule_checkout)[-1] == "docs"


def test_repo_keys_with_separate_git_dir_uses_checkout_name(separate_git_dir_checkout):
    assert repo_keys(separate_git_dir_checkout) == ["docs"]


def test_repo_keys_outside_git_uses_folder_name(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repo_keys(plain) == ["plain"]


def test_checkout_root_from_subfolder(tmp_git_repo_without_remote):
    sub = tmp_git_repo_without_remote / "a" / "b"
    sub.mkdir(parents=True)
    assert checkout_root(sub) == tmp_git_repo_without_remote


def test_checkout_root_outside_git_is_cwd(tmp_path):
    assert checkout_root(tmp_path) == tmp_path


def test_cache_root_per_platform(tmp_path):
    home = str(tmp_path)
    assert cache_root({"HOME": home}, "darwin") == tmp_path / "Library/Caches/doclookup"
    assert cache_root({"HOME": home, "XDG_CACHE_HOME": str(tmp_path / "xdg")}, "linux") == tmp_path / "xdg/doclookup"
    assert cache_root({"HOME": home}, "linux") == tmp_path / ".cache/doclookup"
    assert cache_root({"HOME": home, "XDG_CACHE_HOME": "relative"}, "linux") == tmp_path / ".cache/doclookup"


def test_index_path_differs_per_checkout_and_is_private(tmp_path):
    a = index_path(tmp_path / "a", {"HOME": str(tmp_path)}, "darwin")
    b = index_path(tmp_path / "b", {"HOME": str(tmp_path)}, "darwin")
    assert a != b and a.name == "index.sqlite"
    assert a.parent.parent == tmp_path / "Library/Caches/doclookup"
    assert stat.S_IMODE(a.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(a.parent.parent.stat().st_mode) == 0o700


def test_index_path_folder_name_and_stability(tmp_path):
    checkout = tmp_path / "repo"
    env = {"HOME": str(tmp_path)}
    first = index_path(checkout, env, "darwin")
    digest = hashlib.sha256(str(checkout.resolve()).encode("utf-8")).hexdigest()[:12]
    assert first.parent.name == f"repo-{digest}"
    assert index_path(checkout, env, "darwin") == first


def test_index_path_tightens_existing_folder(tmp_path):
    env = {"HOME": str(tmp_path)}
    folder = index_path(tmp_path / "repo", env, "darwin").parent
    folder.chmod(0o755)
    folder.parent.chmod(0o755)
    index_path(tmp_path / "repo", env, "darwin")
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(folder.parent.stat().st_mode) == 0o700


def test_index_path_does_not_touch_checkout(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    index_path(checkout, {"HOME": str(tmp_path / "home")}, "darwin")
    assert list(checkout.iterdir()) == []
