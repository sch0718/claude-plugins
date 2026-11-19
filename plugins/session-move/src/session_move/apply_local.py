"""Same-device apply: copy sessions, scheduled tasks and backlog into another account's folder."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from session_move.app import app_is_running
from session_move.backlog import merge_backlog
from session_move.backup import backup, mark_applied
from session_move.jsonio import load_json, write_json
from session_move.manifest import SAME_DEVICE_ITEMS, parse_items
from session_move.paths import Paths
from session_move.scopes import list_scopes
from session_move.schedules import merge_schedules
from session_move.sessions import apply_sessions, plan_sessions


def latest_manifest(paths: Paths) -> Path | None:
    found = sorted((paths.state_dir / "manifests").glob("*.json"))
    return found[-1] if found else None


def resolve_target(paths: Paths, source: str, to: str | None) -> str:
    if to:
        return to
    others = [s.name for s in list_scopes(paths) if s.account != source.split("/")[0]]
    if len(others) != 1:
        raise ValueError("대상 계정 폴더를 하나로 정할 수 없습니다. --to <계정ID>/<조직ID>로 지정하세요.\n"
                         + "\n".join(f"  {s.name}" for s in list_scopes(paths)))
    return others[0]


def cmd_apply_local(args, paths: Paths) -> int:
    mpath = Path(args.manifest) if args.manifest else latest_manifest(paths)
    if not mpath or not mpath.exists():
        print("manifest가 없습니다. 먼저 export를 실행하세요.", file=sys.stderr)
        return 1
    manifest = load_json(mpath)
    source = manifest["sourceScope"]
    target = resolve_target(paths, source, args.to)
    sdir, tdir = paths.scope_dir(source), paths.scope_dir(target)
    if not tdir.is_dir():
        print(f"대상 계정 폴더가 없습니다: {target}\n새 계정으로 앱에 한 번 로그인해 Code 탭을 연 뒤 다시 실행하세요.",
              file=sys.stderr)
        return 1
    if os.path.realpath(sdir) == os.path.realpath(tdir):
        print("원본과 대상이 같습니다.", file=sys.stderr)
        return 1

    items = parse_items(args.only, args.skip, SAME_DEVICE_ITEMS) & set(manifest["items"])
    only = set(args.only_session) if args.only_session else None
    actions = plan_sessions(manifest, sdir, tdir, paths.projects_root, only) if "sessions" in items else []
    titles = {s["sessionId"]: s.get("title", "") for s in manifest["sessions"]}

    print(f"원본: {source}\n대상: {target}")
    label = {"add": "추가", "update": "갱신", "skip": "건너뜀"}
    for a in actions:
        mark = "  └ " if any(s["sessionId"] == a.session_id and s.get("parentSessionId") for s in manifest["sessions"]) else "  - "
        print(f"{mark}{label[a.kind]}: {titles.get(a.session_id, '(보관)')} ({a.session_id}) - {a.reason}")
    src_sched = load_json(sdir / "scheduled-tasks.json", {}) if "schedules" in items else None
    src_backlog = load_json(sdir / "backlog" / "tasks.json", {}) if "backlog" in items else None
    if src_sched is not None:
        print(f"예약 작업: 원본 {len(src_sched.get('scheduledTasks', []))}개 중 대상에 없는 것만 추가")
    if src_backlog is not None:
        print(f"backlog: 원본 {len(src_backlog.get('items', []))}개를 ID 기준으로 병합")
    if {"sidebar", "presets"} & items:
        print("그룹·섹션·핀·프리셋은 새 계정 세션에서 `/session-move` finish 단계로 만듭니다.")

    if args.dry_run:
        print("\n미리보기만 했습니다. 앱을 완전히 종료한 뒤 --dry-run 없이 실행하세요.")
        return 0
    if app_is_running():
        print("Claude 앱이 실행 중입니다. 앱을 완전히 종료(⌘Q)한 뒤 다시 실행하세요.", file=sys.stderr)
        return 3

    archive = backup([tdir], paths.state_dir)
    print(f"백업: {archive}")
    apply_sessions(actions, sdir, tdir)
    if src_sched is not None:
        merged, added = merge_schedules(src_sched, load_json(tdir / "scheduled-tasks.json", {}), keep_enabled=True)
        if added:
            write_json(tdir / "scheduled-tasks.json", merged)
        print(f"예약 작업 {len(added)}개 추가")
    if src_backlog is not None:
        merged, changed = merge_backlog(src_backlog, load_json(tdir / "backlog" / "tasks.json", {}))
        if changed:
            write_json(tdir / "backlog" / "tasks.json", merged)
        print(f"backlog {len(changed)}개 반영")
    mark_applied(archive)
    done = sum(1 for a in actions if a.kind != "skip")
    skipped = sum(1 for a in actions if a.kind == "skip")
    print(f"세션 {done}개 반영, {skipped}개 건너뜀. 같은 세션을 두 계정에서 동시에 이어가지 마세요.")
    return 2 if skipped else 0
