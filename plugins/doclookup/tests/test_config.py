from pathlib import Path

import pytest

from doclookup.config import AliasGroup, ConfigError, Extension, Hint, IdKind, Roles, Search, find_config, load

FULL = """\
match = ["github.com/example-org/docs-catalog"]
anchor = "github"

[files]
markdown = ["**/*.md"]
text = ["tools/**/*.py"]
exclude = ["_workspace/**", "_html/**", "node_modules/**"]

[frontmatter]
columns = ["type", "status", "source_version", "screen_id", "flow_id"]

[[ids]]
name = "결정"
pattern = 'D-\\d{3}'
defined_by = "heading"
scope = 'product/([^/]+)/'
in = ["product/*/decision-log.md"]

[[ids]]
name = "화면"
pattern = 'SCR-[A-Z]+'
defined_by = "frontmatter:screen_id"

[[aliases]]
canonical = "파트장"
variants = ["파트 리더", "파트리더"]

[roles]
names = ["변경관리자", "변경수행자"]
suspect = '(?:[가-힣]+ ?)?(?:승인자|검증자|책임자|담당자)'

[variants]
suffix_pairs = [["장", " 리더"]]

[hint]
enabled = true
min_chars = 5000

[search]
priority = ["docs/규정/**", "docs/점검/01.*"]
"""

MINIMAL = 'match = ["{key}"]\n'


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reads_all_sections(tmp_path):
    cfg = load(write(tmp_path / "full.toml", FULL))
    assert cfg.path == tmp_path / "full.toml"
    assert cfg.match == ["github.com/example-org/docs-catalog"]
    assert cfg.anchor == "github"
    assert cfg.markdown == ["**/*.md"]
    assert cfg.text == ["tools/**/*.py"]
    assert cfg.exclude == ["_workspace/**", "_html/**", "node_modules/**"]
    assert cfg.frontmatter_columns == ["type", "status", "source_version", "screen_id", "flow_id"]
    assert cfg.ids == [
        IdKind(name="결정", pattern=r"D-\d{3}", defined_by="heading", scope="product/([^/]+)/", in_=["product/*/decision-log.md"]),
        IdKind(name="화면", pattern="SCR-[A-Z]+", defined_by="frontmatter:screen_id", scope=None, in_=[]),
    ]
    assert cfg.aliases == [AliasGroup(canonical="파트장", variants=["파트 리더", "파트리더"])]
    assert cfg.roles == Roles(names=["변경관리자", "변경수행자"], suspect="(?:[가-힣]+ ?)?(?:승인자|검증자|책임자|담당자)")
    assert cfg.suffix_pairs == [("장", " 리더")]
    assert cfg.hint == Hint(enabled=True, min_chars=5000)
    assert cfg.search == Search(priority=["docs/규정/**", "docs/점검/01.*"])
    assert cfg.extensions == []


def test_load_defaults(tmp_path):
    cfg = load(write(tmp_path / "min.toml", MINIMAL.format(key="repo")))
    assert cfg.match == ["repo"]
    assert cfg.anchor == "gitlab"
    assert cfg.markdown == [] and cfg.text == [] and cfg.exclude == []
    assert cfg.frontmatter_columns == []
    assert cfg.ids == [] and cfg.aliases == [] and cfg.extensions == []
    assert cfg.roles == Roles(names=[], suspect=None)
    assert cfg.suffix_pairs == [("장", " 리더"), ("장", "리더")]
    assert cfg.hint == Hint(enabled=False, min_chars=20000)
    assert cfg.search == Search(priority=[])


def test_load_reads_extensions_array(tmp_path):
    ext_toml = write(
        tmp_path / "ext.toml",
        'match = ["r"]\n[[extensions]]\nname = "myext"\ncommand = ["uv", "run", "python", "-m", "x"]\ncommands = ["find"]\n',
    )
    cfg = load(ext_toml)
    assert cfg.extensions == [Extension(name="myext", command=["uv", "run", "python", "-m", "x"], commands=["find"])]


def test_load_extension_commands_default_empty(tmp_path):
    cfg = load(write(tmp_path / "e.toml", 'match = ["r"]\n[[extensions]]\nname = "x"\ncommand = ["x"]\n'))
    assert cfg.extensions == [Extension(name="x", command=["x"], commands=[])]


def test_load_rejects_bad_regex(tmp_path):
    bad_toml = write(tmp_path / "bad.toml", 'match = ["r"]\n[[ids]]\nname = "x"\npattern = "("\ndefined_by = "heading"\n')
    with pytest.raises(ConfigError, match="pattern"):
        load(bad_toml)


@pytest.mark.parametrize(
    "body, key",
    [
        ('match = ["r"]\n[[ids]]\nname = "x"\npattern = "x"\ndefined_by = "heading"\nscope = "("\n', "scope"),
        ('match = ["r"]\n[roles]\nsuspect = "["\n', "suspect"),
        ('match = ["r"]\n[[ids]]\nname = "x"\npattern = "x"\ndefined_by = "body"\n', "defined_by"),
        ('match = ["r"]\n[[ids]]\nname = "x"\npattern = "x"\ndefined_by = "frontmatter:"\n', "defined_by"),
        ('match = ["r"]\n[[ids]]\nname = "x"\ndefined_by = "heading"\n', "pattern"),
        ('match = ["r"]\nanchor = "bitbucket"\n', "anchor"),
        ("anchor = \"gitlab\"\n", "match"),
        ('match = []\n', "match"),
        ('match = "r"\n', "match"),
        ('match = ["r"]\n[files]\nmarkdown = "**/*.md"\n', "markdown"),
        ('match = ["r"]\n[[aliases]]\ncanonical = "a"\n', "variants"),
        ('match = ["r"]\n[[extensions]]\nname = "x"\n', "command"),
        ('match = ["r"]\n[[extensions]]\nname = "x"\ncommand = []\n', "command"),
        ('match = ["r"]\n[hint]\nenabled = "yes"\n', "enabled"),
        ('match = ["r"]\n[hint]\nmin_chars = -1\n', "min_chars"),
        ('match = ["r"]\n[variants]\nsuffix_pairs = [["장"]]\n', "suffix_pairs"),
        ('match = ["r"]\n[file]\nmarkdown = ["**/*.md"]\n', "file"),
        ('match = ["r"]\n[files]\nmarkdwon = ["**/*.md"]\n', "markdwon"),
        ('match = ["r"]\n[search]\npriority = "docs/**"\n', "priority"),
        ('match = ["r"]\n[search]\npriority = [""]\n', "priority"),
        ('match = ["r"]\n[search]\npriorty = ["docs/**"]\n', "priorty"),
    ],
)
def test_load_rejects_invalid_values(tmp_path, body, key):
    with pytest.raises(ConfigError, match=key):
        load(write(tmp_path / "bad.toml", body))


def test_load_error_names_file(tmp_path):
    path = write(tmp_path / "broken.toml", "match = [\n")
    with pytest.raises(ConfigError, match="broken.toml"):
        load(path)


def test_load_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="없음.toml"):
        load(tmp_path / "없음.toml")


def test_find_config_prefers_env_dir(tmp_path, repo_with_remote):
    env_dir = tmp_path / "env"
    bundled_dir = tmp_path / "bundled"
    write(env_dir / "r.toml", MINIMAL.format(key="github.com/Org/Repo"))
    write(bundled_dir / "r.toml", MINIMAL.format(key="github.com/Org/Repo"))
    cfg = find_config(repo_with_remote, {"DOCLOOKUP_CONFIG_DIR": str(env_dir)}, bundled_dir)
    assert cfg.path.parent == env_dir


def test_find_config_falls_back_to_bundled(tmp_path, repo_with_remote):
    env_dir = tmp_path / "env"
    bundled_dir = tmp_path / "bundled"
    write(env_dir / "other.toml", MINIMAL.format(key="github.com/Other/Repo"))
    write(bundled_dir / "r.toml", MINIMAL.format(key="github.com/Org/Repo"))
    cfg = find_config(repo_with_remote, {"DOCLOOKUP_CONFIG_DIR": str(env_dir)}, bundled_dir)
    assert cfg.path == bundled_dir / "r.toml"


@pytest.mark.parametrize("has_env_config", [True, False])
def test_find_config_without_bundled_dir(tmp_path, repo_with_remote, has_env_config):
    bundled_dir = tmp_path / "bundled"
    assert not bundled_dir.exists()
    env = {}
    if has_env_config:
        config_path = write(tmp_path / "env" / "r.toml", MINIMAL.format(key="github.com/Org/Repo"))
        env["DOCLOOKUP_CONFIG_DIR"] = str(config_path.parent)
        assert find_config(repo_with_remote, env, bundled_dir).path == config_path
    else:
        with pytest.raises(ConfigError, match="github.com/Org/Repo"):
            find_config(repo_with_remote, env, bundled_dir)


def test_find_config_matches_main_checkout_name(tmp_path, worktree_of_repo_without_remote):
    bundled_dir = tmp_path / "bundled"
    write(bundled_dir / "mainrepo.toml", MINIMAL.format(key="mainrepo"))
    assert find_config(worktree_of_repo_without_remote, {}, bundled_dir).path == bundled_dir / "mainrepo.toml"


def test_find_config_missing_lists_keys(tmp_path, repo_with_remote):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ConfigError, match="github.com/Org/Repo") as err:
        find_config(repo_with_remote, {}, empty_dir)
    message = str(err.value)
    assert repo_with_remote.name in message
    assert str(empty_dir) in message
    assert f"~/.config/doclookup/repos/{repo_with_remote.name}.toml" in message
    assert "DOCLOOKUP_CONFIG_DIR" in message
    assert 'match = ["github.com/Org/Repo"]' in message


def test_find_config_env_dir_must_exist(tmp_path, repo_with_remote):
    with pytest.raises(ConfigError, match="DOCLOOKUP_CONFIG_DIR"):
        find_config(repo_with_remote, {"DOCLOOKUP_CONFIG_DIR": str(tmp_path / "nope")}, tmp_path)


def test_find_config_reports_broken_file(tmp_path, repo_with_remote):
    bundled_dir = tmp_path / "bundled"
    write(bundled_dir / "broken.toml", "match = [\n")
    with pytest.raises(ConfigError, match="broken.toml"):
        find_config(repo_with_remote, {}, bundled_dir)
