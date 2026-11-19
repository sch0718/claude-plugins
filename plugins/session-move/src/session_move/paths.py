from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(home=Path(os.environ.get("SESSION_MOVE_HOME") or Path.home()))

    @property
    def app_dir(self) -> Path:
        return self.home / "Library" / "Application Support" / "Claude"

    @property
    def sessions_root(self) -> Path:
        return self.app_dir / "claude-code-sessions"

    @property
    def desktop_config(self) -> Path:
        return self.app_dir / "claude_desktop_config.json"

    @property
    def presets_file(self) -> Path:
        return self.app_dir / "ccd-session-presets.json"

    @property
    def claude_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def projects_root(self) -> Path:
        return self.claude_dir / "projects"

    @property
    def state_dir(self) -> Path:
        return self.home / ".session-move"

    @property
    def company_config(self) -> Path:
        return self.state_dir / "config.toml"

    def scope_dir(self, scope: str) -> Path:
        return self.sessions_root.joinpath(*scope.split("/"))
