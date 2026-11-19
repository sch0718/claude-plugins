"""Build the manifest: what the source account has, without session bodies."""
from __future__ import annotations

import datetime as dt
import getpass
import socket

from session_move.company import CompanyRules
from session_move.jsonio import load_json, write_json
from session_move.paths import Paths
from session_move.scopes import archived_ids, is_archived, session_records

MANIFEST_VERSION = 1
ITEMS = ("sessions", "transcripts", "schedules", "backlog", "sidebar", "presets",
         "instructions", "skills", "plugins", "mcp", "memory", "repos")
SAME_DEVICE_ITEMS = {"sessions", "schedules", "backlog", "sidebar", "presets"}


def parse_items(only: str | None, skip: str | None, default: set[str]) -> set[str]:
    chosen = set(default)
    if only:
        chosen = {x.strip() for x in only.split(",") if x.strip()}
    if skip:
        chosen -= {x.strip() for x in skip.split(",") if x.strip()}
    unknown = chosen - set(ITEMS)
    if unknown:
        raise ValueError(f"모르는 항목: {', '.join(sorted(unknown))}")
    return chosen


def _groups(prefs: dict, scope: str, active: set[str]) -> list[dict]:
    gs = prefs.get("dframe-group-scopes", {}).get(scope, {})
    order = gs.get("order", {})
    assignments = gs.get("assignments", {})
    out = []
    for g in gs.get("groups", []):
        members = [k.removeprefix("code:") for k in order.get(g["id"], [])]
        members += [k.removeprefix("code:") for k, gid in assignments.items()
                    if gid == g["id"] and k.removeprefix("code:") not in members]
        out.append({"id": g["id"], "name": g.get("name", ""), "sessionIds": [m for m in members if m in active]})
    return out


def _sections(prefs: dict, scope: str) -> list[dict]:
    secs = prefs.get("dframe-code-sections", {}).get(scope, {}).get("sections", [])
    return [{"id": s["id"], "name": s.get("name", "")} for s in secs if s.get("kind") in ("manual", "custom")]


def build_manifest(paths: Paths, source: str, items: set[str], rules: CompanyRules | None = None) -> dict:
    sdir = paths.scope_dir(source)
    arch = archived_ids(sdir)
    sessions, archived = [], []
    for _, rec in session_records(sdir):
        cwd = rec.get("cwd", "")
        company = bool(rules and rules.is_company(cwd))
        if is_archived(rec, arch):
            archived.append({"sessionId": rec["sessionId"], "lastActivityAt": rec.get("lastActivityAt", 0),
                             "isCompany": company})
            continue
        sessions.append({
            "sessionId": rec["sessionId"],
            "title": rec.get("title", ""),
            "cwd": cwd,
            "cliSessionId": rec.get("cliSessionId"),
            "priorCliSessionIds": rec.get("priorCliSessionIds", []),
            "parentSessionId": (rec.get("spawnedFrom") or {}).get("sessionId"),
            "lastActivityAt": rec.get("lastActivityAt", 0),
            "isCompany": company,
        })
    sessions.sort(key=lambda s: s["lastActivityAt"], reverse=True)
    archived.sort(key=lambda s: s["sessionId"])
    active = {s["sessionId"] for s in sessions}
    account = source.split("/")[0]

    prefs = (load_json(paths.desktop_config, {}) or {}).get("preferences", {}).get("epitaxyPrefs", {})
    sidebar = "sidebar" in items
    presets = (load_json(paths.presets_file, {}) or {}).get("byAccount", {}).get(account, {}).get("presets", [])
    schedules = (load_json(sdir / "scheduled-tasks.json", {}) or {}).get("scheduledTasks", [])
    backlog = (load_json(sdir / "backlog" / "tasks.json", {}) or {}).get("items", [])

    return {
        "version": MANIFEST_VERSION,
        "createdAt": dt.datetime.now().isoformat(timespec="seconds"),
        "sourceScope": source,
        "sourceHome": str(paths.home),
        "sourceUser": getpass.getuser(),
        "sourceHost": socket.gethostname().split(".")[0],
        "items": sorted(items),
        "sessions": sessions if "sessions" in items else [],
        "archived": archived if "sessions" in items else [],
        "groups": _groups(prefs, source, active) if sidebar else [],
        "sections": _sections(prefs, source) if sidebar else [],
        "pinnedSessionIds": [s for s in prefs.get("starred-local-code-sessions", []) if s in active] if sidebar else [],
        "presets": presets if "presets" in items else [],
        "schedules": schedules if "schedules" in items else [],
        "backlog": backlog if "backlog" in items else [],
    }


def save_manifest(paths: Paths, manifest: dict) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = paths.state_dir / "manifests" / f"{stamp}.json"
    write_json(out, manifest)
    return str(out)
