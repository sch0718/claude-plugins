import json
import tomllib
from pathlib import Path

from doclookup import __version__
from doclookup.cli import _parser

ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT.parent.parent / ".claude-plugin" / "marketplace.json"


def test_versions_match():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((MARKETPLACE).read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert market["name"] == "sch0718-plugins"
    [entry] = [p for p in market["plugins"] if p["name"] == "doclookup"]
    assert (plugin["name"], entry["name"], entry["source"]) == ("doclookup", "doclookup", "./plugins/doclookup")
    assert __version__ == plugin["version"] == entry["version"] == project["project"]["version"] == "0.1.5"


def test_readme_covers_every_command_and_install():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = _parser().get_default("command_names")
    for command in commands:
        assert f"| `{command}" in text, command
    assert "/plugin marketplace add sch0718/claude-plugins" in text
    assert "/plugin install doclookup@sch0718-plugins" in text
    assert "DOCLOOKUP_CONFIG_DIR" in text
    assert "DX" not in text
