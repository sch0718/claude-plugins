"""Plan and apply session files: add missing ones, update older ones, keep the target's account fields."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from session_move.jsonio import load_json, write_json

ACCOUNT_BOUND_FIELDS = (
    "bridgeSessionIds",
    "remoteMcpServersConfig",
    "enabledMcpTools",
    "startedFromEnvironmentId",
    "promptAppendSnapshot",
    "toolSurfaceSnapshot",
)


@dataclass(frozen=True)
class Action:
    kind: Literal["add", "update", "skip"]
    session_id: str
    reason: str
    archived: bool = False


def transcript_exists(projects_root: Path, cli_session_id: str | None) -> bool:
    return bool(cli_session_id) and any(projects_root.glob(f"*/{cli_session_id}.jsonl"))


def plan_sessions(manifest: dict, source_dir: Path, target_dir: Path, projects_root: Path | None,
                  only: set[str] | None = None) -> list[Action]:
    actions = []
    entries = [(s, False) for s in manifest["sessions"]] + [(s, True) for s in manifest.get("archived", [])]
    for s, archived in entries:
        sid = s["sessionId"]
        if only is not None and sid not in only:
            continue
        if not (source_dir / f"{sid}.json").exists():
            actions.append(Action("skip", sid, "원본 파일 없음", archived))
            continue
        dst = load_json(target_dir / f"{sid}.json")
        if dst is None:
            if archived:
                continue
            if projects_root is not None and not transcript_exists(projects_root, s.get("cliSessionId")):
                actions.append(Action("skip", sid, "대화 기록 파일 없음"))
                continue
            actions.append(Action("add", sid, "대상에 없음"))
        elif int(s.get("lastActivityAt") or 0) > int(dst.get("lastActivityAt") or 0):
            actions.append(Action("update", sid, "원본이 더 최근", archived))
        else:
            actions.append(Action("skip", sid, "대상이 더 최근이거나 같음", archived))
    return actions


def sanitize(rec: dict) -> dict:
    for key in ACCOUNT_BOUND_FIELDS:
        rec.pop(key, None)
    rec["steeredByRemoteClient"] = False
    return rec


def apply_sessions(actions: list[Action], source_dir: Path, target_dir: Path) -> None:
    idx_path = target_dir / "archived-sessions.idx"
    idx = load_json(idx_path, None)
    archived = list((idx or {}).get("archived", []))
    touched_idx = False
    for a in actions:
        if a.kind == "skip":
            continue
        src = sanitize(load_json(source_dir / f"{a.session_id}.json"))
        if a.kind == "update":
            dst = load_json(target_dir / f"{a.session_id}.json") or {}
            for key in ACCOUNT_BOUND_FIELDS:
                if key in dst:
                    src[key] = dst[key]
        write_json(target_dir / f"{a.session_id}.json", src)
        if a.kind == "update":
            if a.archived and a.session_id not in archived:
                archived.append(a.session_id)
                touched_idx = True
            elif not a.archived and a.session_id in archived:
                archived.remove(a.session_id)
                touched_idx = True
    if touched_idx:
        data = idx or {}
        data["archived"] = archived
        write_json(idx_path, data)
