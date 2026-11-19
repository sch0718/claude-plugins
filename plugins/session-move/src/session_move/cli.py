"""session-move: Claude 데스크톱 앱 Code 탭 세션을 다른 계정이나 다른 기기로 옮긴다."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from session_move.company import CompanyRules
from session_move.jsonio import load_json
from session_move.manifest import SAME_DEVICE_ITEMS, build_manifest, parse_items, save_manifest
from session_move.paths import Paths
from session_move.scopes import list_scopes


def _fmt_time(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "-"


def cmd_scopes(args, paths: Paths) -> int:
    scopes = list_scopes(paths)
    if not scopes:
        print("계정 폴더가 없습니다.")
        return 1
    for s in sorted(scopes, key=lambda s: s.last_activity, reverse=True):
        print(f"{s.name}  활성 {s.active} · 보관 {s.archived} · 최근 {_fmt_time(s.last_activity)}")
    return 0


def default_source(paths: Paths) -> str | None:
    scopes = [s for s in list_scopes(paths) if s.active]
    if not scopes:
        return None
    return max(scopes, key=lambda s: s.last_activity).name


def cmd_export(args, paths: Paths) -> int:
    if getattr(args, "bundle", None):
        from session_move.bundle import cmd_export_bundle
        return cmd_export_bundle(args, paths)
    source = args.source or default_source(paths)
    if not source or not paths.scope_dir(source).is_dir():
        print(f"원본 계정 폴더가 없습니다: {source}", file=sys.stderr)
        return 1
    items = parse_items(args.only, args.skip, SAME_DEVICE_ITEMS)
    rules = CompanyRules.load(paths.company_config)
    manifest = build_manifest(paths, source, items, rules)
    out = save_manifest(paths, manifest)
    children = sum(1 for s in manifest["sessions"] if s["parentSessionId"])
    print(f"원본: {source}")
    print(f"세션 {len(manifest['sessions'])}개(하위 세션 {children}개), 보관 {len(manifest['archived'])}개, "
          f"그룹 {len(manifest['groups'])}개, 예약 작업 {len(manifest['schedules'])}개, backlog {len(manifest['backlog'])}개")
    print(f"manifest: {out}")
    return 0


def inspect_manifest(manifest: dict, paths: Paths, target: str | None) -> list[str]:
    lines = [
        f"원본: {manifest['sourceScope']} ({manifest['sourceHost']}, {manifest['createdAt']})",
        f"항목: {', '.join(manifest['items'])}",
        f"세션 {len(manifest['sessions'])}개(회사 판정 {sum(1 for s in manifest['sessions'] if s['isCompany'])}개), "
        f"보관 {len(manifest['archived'])}개(회사 판정 {sum(1 for s in manifest['archived'] if s.get('isCompany'))}개)",
        f"그룹 {len(manifest['groups'])} · 섹션 {len(manifest['sections'])} · 핀 {len(manifest['pinnedSessionIds'])} · "
        f"프리셋 {len(manifest['presets'])} · 예약 작업 {len(manifest['schedules'])} · backlog {len(manifest['backlog'])}",
    ]
    if target:
        tdir = paths.scope_dir(target)
        existing = {p.stem for p in tdir.glob("local_*.json")} if tdir.is_dir() else set()
        clash = sum(1 for s in manifest["sessions"] if s["sessionId"] in existing)
        lines.append(f"대상 {target}: 같은 ID 세션 {clash}개(더 새로운 쪽만 갱신)")
    return lines


def cmd_inspect(args, paths: Paths) -> int:
    if getattr(args, "bundle", None):
        from session_move.bundle import cmd_inspect_bundle
        return cmd_inspect_bundle(args, paths)
    if not args.manifest:
        print("--manifest 나 --bundle 을 주세요.", file=sys.stderr)
        return 1
    manifest = load_json(Path(args.manifest))
    for line in inspect_manifest(manifest, paths, args.to):
        print(line)
    return 0


def cmd_apply(args, paths: Paths) -> int:
    if getattr(args, "bundle", None):
        from session_move.bundle import cmd_apply_bundle
        return cmd_apply_bundle(args, paths)
    from session_move.apply_local import cmd_apply_local
    return cmd_apply_local(args, paths)


def cmd_restore(args, paths: Paths) -> int:
    from session_move.app import app_is_running
    from session_move.backup import backup, changed_since, entries, restore
    archive = Path(args.archive)
    if app_is_running():
        print("Claude 앱이 실행 중입니다. 앱을 완전히 종료(⌘Q)한 뒤 다시 실행하세요.", file=sys.stderr)
        return 3
    changed = changed_since(archive)
    if changed and not args.force:
        print("적용한 뒤에 바뀐 파일이 있습니다. 되돌리면 이 내용이 사라집니다(그래도 하려면 --force):", file=sys.stderr)
        for p in changed[:50]:
            print(f"  {p}", file=sys.stderr)
        return 1
    targets, _ = entries(archive)
    print(f"되돌리기 전 상태 백업: {backup(targets, paths.state_dir)}")
    restore(archive)
    print(f"되돌렸습니다: {archive}")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="session-move", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scopes", help="계정·조직 폴더와 세션 수")

    e = sub.add_parser("export", help="manifest 또는 다른 기기용 번들 만들기")
    e.add_argument("--source", help="원본 <계정ID>/<조직ID> (생략하면 가장 최근에 쓴 폴더)")
    e.add_argument("--bundle", help="다른 기기용 번들 파일(tar.gz) 경로")
    e.add_argument("--only")
    e.add_argument("--skip")
    e.add_argument("--include-company", action="store_true")
    e.add_argument("--no-company-check", action="store_true")

    i = sub.add_parser("inspect", help="manifest·번들에 담긴 것과 충돌 보기")
    i.add_argument("--manifest")
    i.add_argument("--bundle")
    i.add_argument("--to")

    a = sub.add_parser("apply", help="대상 계정·기기에 적용(앱을 종료한 뒤)")
    a.add_argument("--manifest")
    a.add_argument("--bundle")
    a.add_argument("--to")
    a.add_argument("--only")
    a.add_argument("--skip")
    a.add_argument("--only-session", nargs="+")
    a.add_argument("--map-path", action="append", default=[])
    a.add_argument("--clone", action="store_true")
    a.add_argument("--dry-run", action="store_true")

    r = sub.add_parser("restore", help="적용 전 백업으로 되돌리기(앱을 종료한 뒤)")
    r.add_argument("archive")
    r.add_argument("--force", action="store_true", help="적용 뒤 바뀐 파일이 있어도 되돌림")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = Paths.from_env()
    handlers = {"scopes": cmd_scopes, "export": cmd_export, "inspect": cmd_inspect,
                "apply": cmd_apply, "restore": cmd_restore}
    try:
        return handlers[args.cmd](args, paths)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
