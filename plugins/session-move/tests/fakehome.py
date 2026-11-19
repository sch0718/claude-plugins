"""Build a fake HOME that looks like a Claude desktop app install, for tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

ACC_A = "aaaaaaaa-0000-4000-8000-000000000001"
ACC_B = "bbbbbbbb-0000-4000-8000-000000000002"
ORG = "oooooooo-0000-4000-8000-000000000003"
SCOPE_A = f"{ACC_A}/{ORG}"
SCOPE_B = f"{ACC_B}/{ORG}"

S_PARENT = "local_11111111-1111-4111-8111-111111111111"
S_CHILD = "local_22222222-2222-4222-8222-222222222222"
S_ARCHIVED = "local_33333333-3333-4333-8333-333333333333"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def app_dir(home: Path) -> Path:
    return home / "Library" / "Application Support" / "Claude"


def scope_dir(home: Path, scope: str) -> Path:
    return app_dir(home) / "claude-code-sessions" / scope


def encode(cwd: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


def session(home: Path, sid: str, cli: str, cwd: str, last: int, parent: str | None = None, **extra) -> dict:
    rec = {
        "sessionId": sid,
        "cliSessionId": cli,
        "cwd": cwd,
        "originCwd": cwd,
        "title": f"title {sid[-4:]}",
        "lastActivityAt": last,
        "isArchived": False,
        "bridgeSessionIds": ["bridge-1"],
        "remoteMcpServersConfig": [{"name": "x"}],
        "enabledMcpTools": {"x": True},
        "startedFromEnvironmentId": "env-1",
        "promptAppendSnapshot": {"a": 1},
        "toolSurfaceSnapshot": {"b": 2},
        "steeredByRemoteClient": True,
    }
    if parent:
        rec["spawnedFrom"] = {"sessionId": parent}
    rec.update(extra)
    return rec


def transcript(home: Path, cwd: str, cli: str, lines: list[dict] | None = None) -> Path:
    d = home / ".claude" / "projects" / encode(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cli}.jsonl"
    body = lines or [
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "ai-title", "title": "t"},
        {"type": "user", "cwd": cwd, "message": {"content": "hi"}},
    ]
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in body) + "\n", encoding="utf-8")
    return p


def make_fake_home(root: Path) -> Path:
    home = root / "home"
    work = home / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    os.makedirs(scope_dir(home, SCOPE_B), exist_ok=True)

    sa = scope_dir(home, SCOPE_A)
    write_json(sa / f"{S_PARENT}.json", session(home, S_PARENT, "cli-parent", str(repo), 2000))
    write_json(sa / f"{S_CHILD}.json", session(home, S_CHILD, "cli-child", str(repo), 1500, parent=S_PARENT))
    write_json(sa / f"{S_ARCHIVED}.json", session(home, S_ARCHIVED, "cli-arch", str(repo), 900, isArchived=True))
    write_json(sa / "archived-sessions.idx", {"archived": [S_ARCHIVED]})
    write_json(sa / "scheduled-tasks.json", {"scheduledTasks": [
        {"id": "daily-note", "displayName": "daily note", "cronExpression": "0 9 * * 1-5", "enabled": True,
         "filePath": str(home / ".claude/scheduled-tasks/daily-note/SKILL.md"), "cwd": str(repo),
         "lastRunAt": "2026-01-01T00:00:00.000Z", "lastScheduledFor": "2026-01-01T00:00:00.000Z",
         "missedRunScanFloor": "2026-01-01T00:00:00.000Z"},
        {"id": "weekly", "displayName": "weekly", "cronExpression": "0 10 * * 1", "enabled": False,
         "filePath": str(home / ".claude/scheduled-tasks/weekly/SKILL.md"), "cwd": str(repo)},
    ]})
    write_json(sa / "backlog" / "tasks.json", {"version": 1, "items": [
        {"id": "b1", "title": "idea one", "prompt": "do one", "cwd": str(repo), "filedAt": 100, "originSessionId": S_PARENT},
    ]})
    for cli in ("cli-parent", "cli-child", "cli-arch"):
        transcript(home, str(repo), cli)
    for tid in ("daily-note", "weekly"):
        p = home / ".claude" / "scheduled-tasks" / tid / "SKILL.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {tid}\nplain body\n", encoding="utf-8")

    write_json(app_dir(home) / "claude_desktop_config.json", {"preferences": {
        "epitaxyPrefs": {
            "dframe-group-scopes": {SCOPE_A: {
                "groups": [{"id": "g1", "name": "ideas"}, {"id": "g2", "name": "review"}],
                "order": {"g1": [f"code:{S_PARENT}"]},
                "assignments": {f"code:{S_CHILD}": "g1", f"code:{S_ARCHIVED}": "g2"},
            }},
            "dframe-code-sections": {SCOPE_A: {"sections": [
                {"id": "pinned", "kind": "pinned", "name": ""},
                {"id": "s1", "kind": "manual", "name": "ideas"},
                {"id": "sessions", "kind": "sessions", "name": ""},
            ]}},
            "starred-local-code-sessions": [S_PARENT, S_ARCHIVED],
        }
    }})
    write_json(app_dir(home) / "ccd-session-presets.json", {"byAccount": {
        ACC_A: {"presets": [{"id": "p1", "name": "repo preset", "target": {"kind": "local", "cwd": str(repo)}}]},
        ACC_B: {"presets": []},
    }})
    return home
