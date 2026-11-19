from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from session_move.jsonio import load_json
from session_move.paths import Paths


@dataclass(frozen=True)
class Scope:
    account: str
    org: str
    path: Path
    active: int
    archived: int
    last_activity: int

    @property
    def name(self) -> str:
        return f"{self.account}/{self.org}"


def archived_ids(scope_dir: Path) -> set[str]:
    data = load_json(scope_dir / "archived-sessions.idx", {}) or {}
    return set(data.get("archived", []))


def session_records(scope_dir: Path):
    for p in sorted(scope_dir.glob("local_*.json")):
        yield p, load_json(p)


def is_archived(rec: dict, archived: set[str]) -> bool:
    return bool(rec.get("isArchived")) or rec.get("sessionId") in archived


def list_scopes(paths: Paths) -> list[Scope]:
    out = []
    if not paths.sessions_root.is_dir():
        return out
    for acc in sorted(p for p in paths.sessions_root.iterdir() if p.is_dir()):
        for org in sorted(p for p in acc.iterdir() if p.is_dir()):
            arch = archived_ids(org)
            active = archived = last = 0
            for _, rec in session_records(org):
                if is_archived(rec, arch):
                    archived += 1
                else:
                    active += 1
                    last = max(last, int(rec.get("lastActivityAt") or 0))
            out.append(Scope(acc.name, org.name, org, active, archived, last))
    return out
