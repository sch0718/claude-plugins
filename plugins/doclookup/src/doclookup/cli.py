import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

from doclookup import __version__, ext, hint, index, query
from doclookup.config import ConfigError, find_config, load
from doclookup.index import Index
from doclookup.paths import cache_root, checkout_root, index_path
from doclookup.refs import UNKNOWN_SCOPE

BUNDLED = Path(__file__).resolve().parents[2] / "repos"


def _snippet(text: str, needle: str, width: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    needle = re.sub(r"\s+", " ", needle).strip()
    pos = flat.casefold().find(needle.casefold()) if needle else -1
    if pos < 0:
        return flat[: width * 2] + ("…" if len(flat) > width * 2 else "")
    start, end = max(0, pos - width), pos + len(needle) + width
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


SUMMARY_TOP = 5


def _summary(counts: Counter, every: bool, unit: str) -> str:
    """건수 많은 순(같으면 이름 순)으로 SUMMARY_TOP개까지 보이고 나머지는 「외 n개 <unit> m건」으로 줄인다."""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    shown = ordered if every else ordered[:SUMMARY_TOP]
    rest = ordered[len(shown):]
    text = ", ".join(f"{key} {n}" for key, n in shown)
    return text + (f" 외 {len(rest)}개 {unit} {sum(n for _, n in rest)}건" if rest else "")


def _more(shown: int, total: int) -> None:
    if total > shown:
        print(f"… {total - shown}건 더 (--limit)")


def _target(args):
    root = Path(args.root).resolve() if args.root else checkout_root(Path.cwd()).resolve()
    cfg = load(Path(args.config)) if args.config else find_config(root, os.environ, BUNDLED)
    return root, cfg


def _open(args) -> Index:
    root, cfg = _target(args)
    path = index_path(root, os.environ, sys.platform)
    rebuilt = index.ensure(cfg, root, path)
    ix = index.open_index(cfg, root, path)
    if rebuilt:
        row = ix.conn.execute("SELECT value FROM meta WHERE key='skipped'").fetchone()
        for item in json.loads(row["value"]) if row else []:
            print(f"건너뜀: {item}", file=sys.stderr)
    if not ix.trigram:
        print(f"알림: SQLite {sqlite3.sqlite_version}에 trigram이 없어 LIKE로 찾는다")
    return ix


def _where(args) -> dict[str, str]:
    where = {}
    for item in args.where or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"--where는 키=값 꼴이어야 한다: {item}")
        where[key.strip()] = value.strip()
    if args.type:
        where["type"] = args.type
    return where


def run_search(ix: Index, args) -> int:
    terms = [*args.terms, *(t for t in (args.any or "").split(",") if t.strip())]
    hits = query.ranked(ix.cfg, query.search(ix, terms, in_glob=args.in_glob, where=_where(args), exact=args.exact))
    counts = Counter(hit.file for hit in hits)
    print(f"{len(hits)}건" + (": " + _summary(counts, args.files, "파일") if hits else ""))
    asked = {unicodedata.normalize("NFC", t).strip().casefold() for t in terms}
    for hit in hits[: args.limit]:
        mark = "" if hit.matched.casefold() in asked else f" 〔표기: {hit.matched}〕"
        print(f"{hit.file}:{hit.line}  {_snippet(hit.context, hit.matched, args.width)}{mark}")
    _more(min(args.limit, len(hits)), len(hits))
    return 0 if hits else 1


def run_section(ix: Index, args) -> int:
    found = query.section(ix, args.doc, args.key)
    if not found:
        print(f"절을 찾지 못했다: {args.doc} {args.key}")
        return 1
    if len(found) > 1 and not args.all:
        print(f"절 {len(found)}개가 걸린다. 첫 절만 보인다(--all로 모두):")
        for unit in found:
            print(f"  #{unit.slug}  {unit.file}:{unit.line}")
        found = found[:1]
    for unit in found:
        print(f"[{unit.uid}] {unit.file}:{unit.line}  #{unit.slug}\n{unit.path}\n{unit.text}")
    return 0


def _scope_order(scope: str) -> tuple[int, str]:
    return (2 if scope == UNKNOWN_SCOPE else 0 if scope == "" else 1, scope)


def run_id(ix: Index, args) -> int:
    groups = query.find_id(ix, args.ident, args.scope)
    if not groups:
        print(f"ID 정의를 찾지 못했다: {args.ident}")
        return 1
    for scope in sorted(groups, key=_scope_order):
        if scope:
            print(f"[{scope}]")
        for d in groups[scope]:
            print(f"{d.file}:{d.line}  {query.line_text(ix, d.uid, d.line).strip()}")
    return 0


def run_refs(ix: Index, args) -> int:
    groups = query.refs_to(ix, args.target, args.scope)
    total = sum(len(refs) for refs in groups.values())
    scopes = sorted(groups, key=_scope_order)
    summary = ", ".join(f"{s or '링크·인용'} {len(groups[s])}" for s in scopes)
    print(f"{total}건" + (f": {summary}" if total else ""))
    shown = 0
    for scope in scopes:
        if shown >= args.limit:
            break
        if scope:
            print(f"[{scope}]")
        for ref in groups[scope]:
            if shown >= args.limit:
                break
            line = query.line_text(ix, ref.src_uid, ref.src_line) or ref.raw
            print(f"{ref.src_file}:{ref.src_line}  {_snippet(line, ref.raw, args.width)}")
            shown += 1
    _more(shown, total)
    return 0 if total else 1


def run_links(ix: Index, args) -> int:
    broken = query.broken_links(ix)
    print(f"{len(broken)}건")
    for ref in broken[: args.limit]:
        print(f"{ref.src_file}:{ref.src_line}  {ref.raw} → {ref.target}")
    _more(min(args.limit, len(broken)), len(broken))
    return 0 if broken else 1


def _mark(asked: str, matched: str) -> str:
    return "" if matched.casefold() == unicodedata.normalize("NFC", asked).strip().casefold() else f" 〔표기: {matched}〕"


def run_impact(ix: Index, args) -> int:
    found = query.impact(ix, args.target, args.scope)
    term = query.target_kind(ix, args.target) == "term"
    print(f"마크다운 {len(found.markdown)}개 파일 / 텍스트 {len(found.text)}개 파일")
    for label, groups in (("마크다운", found.markdown), ("텍스트", found.text)):
        if not groups:
            continue
        print(f"[{label}]")
        for file, hits in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
            print(f"{file} ({len(hits)}건)")
            for hit in hits[: args.per_file]:
                mark = _mark(args.target, hit.matched) if term else ""
                print(f"  {hit.file}:{hit.line}  {_snippet(hit.context, hit.matched, args.width)}{mark}")
            if len(hits) > args.per_file:
                print(f"  … {len(hits) - args.per_file}건 더")
    return 0 if found.markdown or found.text else 1


def run_variants(ix: Index, args) -> int:
    found = query.variants(ix, args.term)
    print(f"후보 {len(found)}개 (사람이 확인한 뒤에만 설정 [[aliases]]에 옮긴다)")
    for c in found[: args.limit]:
        print(f"후보  {c.form}  {c.count}건  〔{c.rule}〕")
    _more(min(args.limit, len(found)), len(found))
    return 0 if found else 1


def run_roles(ix: Index, args) -> int:
    if not ix.cfg.roles.suspect:
        raise ConfigError(f"{ix.cfg.path}: roles.suspect가 설정에 없다")
    hits = query.roles(ix)
    counts = Counter(hit.matched for hit in hits)
    print(f"{len(hits)}건" + (": " + _summary(counts, args.names, "이름") if hits else ""))
    for hit in hits[: args.limit]:
        print(f"{hit.file}:{hit.line}  {_snippet(hit.context, hit.matched, args.width)} 〔역할: {hit.matched}〕")
    _more(min(args.limit, len(hits)), len(hits))
    return 0 if hits else 1


_CACHE_DIR = re.compile(r".+-[0-9a-f]{12}")


def _checkout_of(index: Path) -> str | None:
    try:
        conn = sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='checkout'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def run_gc(args) -> int:
    """캐시 폴더 아래 `<이름>-<해시 12자>` 폴더만 본다. 체크아웃이 없거나 색인을 days일 넘게 안 쓴 폴더를 지운다."""
    root = cache_root(os.environ, sys.platform)
    limit = time.time() - args.days * 86400
    removed = 0
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        if not folder.is_dir() or folder.is_symlink() or not _CACHE_DIR.fullmatch(folder.name):
            continue
        index = folder / "index.sqlite"
        checkout = _checkout_of(index) if index.is_file() else None
        stamp = (index if index.is_file() else folder).stat().st_mtime
        if checkout is None or not Path(checkout).is_dir():
            reason = "체크아웃 없음"
        elif stamp < limit:
            reason = f"{args.days}일 넘게 안 씀"
        else:
            continue
        shutil.rmtree(folder)
        removed += 1
        print(f"지움: {folder} ({reason})")
    print(f"지운 폴더 {removed}개")
    return 0


def run_hint() -> int:
    """PreToolUse(Read) 훅. Read를 막지 않도록 어떤 예외에도 0으로 끝낸다."""
    try:
        out, code = hint.run(sys.stdin.read(), Path.cwd(), os.environ, BUNDLED)
        if out:
            print(out)
        return code
    except Exception:
        return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="체크아웃 대신 쓸 폴더")
    common.add_argument("--config", default=argparse.SUPPRESS, help="설정 찾기 대신 쓸 설정 파일")
    listing = argparse.ArgumentParser(add_help=False)
    listing.add_argument("--limit", type=int, default=20, help="보여 줄 결과 수")
    listing.add_argument("--width", type=int, default=50, help="찾은 낱말 앞뒤로 보여 줄 글자 수")

    parser = argparse.ArgumentParser(prog="doclookup", description="문서를 절 단위로 색인하고 파일:줄로 찾는다.")
    parser.add_argument("--version", action="version", version=f"doclookup {__version__}")
    parser.add_argument("--root", help="체크아웃 대신 쓸 폴더")
    parser.add_argument("--config", help="설정 찾기 대신 쓸 설정 파일")
    commands = parser.add_subparsers(dest="command", metavar="명령")

    sub = commands.add_parser("search", parents=[common, listing], help="낱말과 그 표기 묶음이 쓰인 곳")
    sub.add_argument("terms", nargs="+", metavar="낱말",
                     help="띄어 쓴 여러 낱말은 어느 하나라도 들어간 줄을 찾는다(--any와 같다). 띄어쓰기가 든 구절은 따옴표로 묶는다. 낱말은 옵션 앞에 모아서 준다")
    sub.add_argument("--any", help="함께 찾을 낱말(쉼표로 구분)")
    sub.add_argument("--in", dest="in_glob", help="이 경로(폴더 또는 glob) 안에서만 찾는다")
    sub.add_argument("--type", help="프론트매터 type이 이 값인 파일만")
    sub.add_argument("--where", action="append", help="프론트매터 조건 키=값(여러 번 줄 수 있음)")
    sub.add_argument("--exact", action="store_true", help="표기 묶음을 펼치지 않는다")
    sub.add_argument("--files", action="store_true", help=f"첫 줄에 건수 많은 {SUMMARY_TOP}개 파일만이 아니라 모든 파일을 보인다")
    sub.set_defaults(handler=run_search)

    sub = commands.add_parser("section", parents=[common], help="절 하나")
    sub.add_argument("doc", help="파일 경로 또는 그 일부")
    sub.add_argument("key", help="앵커 앞부분 또는 제목 일부")
    sub.add_argument("--all", action="store_true", help="걸린 절을 모두 보인다")
    sub.set_defaults(handler=run_section)

    sub = commands.add_parser("id", parents=[common], help="ID가 정의된 곳")
    sub.add_argument("ident", metavar="ID")
    sub.add_argument("--scope", help="이 범위와 범위 불명만 보인다")
    sub.set_defaults(handler=run_id)

    sub = commands.add_parser("refs", parents=[common, listing], help="대상(ID, 파일, 파일#앵커)을 가리키는 곳")
    sub.add_argument("target")
    sub.add_argument("--scope", help="이 범위와 범위 불명만 보인다")
    sub.set_defaults(handler=run_refs)

    sub = commands.add_parser("links", parents=[common, listing], help="깨진 링크·앵커")
    sub.set_defaults(handler=run_links)

    sub = commands.add_parser("impact", parents=[common, listing], help="바꾸면 같이 고칠 곳(낱말, ID, 파일#앵커)")
    sub.add_argument("target")
    sub.add_argument("--scope", help="이 범위와 범위 불명만 보인다")
    sub.add_argument("--per-file", type=int, default=5, help="파일마다 보여 줄 줄 수")
    sub.set_defaults(handler=run_impact)

    sub = commands.add_parser("variants", parents=[common, listing], help="묶음에 없는 비슷한 표기 후보")
    sub.add_argument("term")
    sub.set_defaults(handler=run_variants)

    sub = commands.add_parser("roles", parents=[common, listing], help="정의되지 않은 역할 이름")
    sub.add_argument("--names", action="store_true", help=f"첫 줄에 건수 많은 {SUMMARY_TOP}개 이름만이 아니라 모든 이름을 보인다")
    sub.set_defaults(handler=run_roles)

    sub = commands.add_parser("gc", help="오래되었거나 체크아웃이 없는 색인을 지운다")
    sub.add_argument("--days", type=int, default=30, help="이 날수보다 오래 안 쓴 색인을 지운다")
    sub.set_defaults(handler=None, gc=True)

    sub = commands.add_parser("hint", help="PreToolUse(Read) 훅: 표준 입력의 JSON을 보고 큰 파일이면 안내한다")
    sub.set_defaults(handler=None, hint=True)
    parser.set_defaults(command_names=set(commands.choices))
    return parser


def _extension_command(parser: argparse.ArgumentParser, argv: list[str]) -> int | None:
    """첫 명령이 기본 명령이 아니면 설정의 `Extension.commands`에서 찾아 확장의 `run`으로 넘긴다. 없으면 None."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--root")
    pre.add_argument("--config")
    known, rest = pre.parse_known_args(argv)
    first = next((i for i, a in enumerate(rest) if not a.startswith("-")), None)
    builtin = parser.get_default("command_names")
    if first is None or rest[first] in builtin or any(a in ("-h", "--help", "--version") for a in rest[:first]):
        return None
    root, cfg = _target(known)
    spec = next((e for e in cfg.extensions if rest[first] in e.commands), None)
    if spec is None:
        return None
    return ext.run(spec, rest[first], rest[:first] + rest[first + 1:], root)


def main(argv: list[str]) -> int:
    parser = _parser()
    try:
        passed = _extension_command(parser, argv)
    except (ConfigError, ext.ExtError) as err:
        print(f"doclookup: {err}", file=sys.stderr)
        return 2
    if passed is not None:
        return passed
    args = parser.parse_args(argv)
    if getattr(args, "gc", False):
        return run_gc(args)
    if getattr(args, "hint", False):
        return run_hint()
    if not getattr(args, "handler", None):
        parser.print_usage(sys.stderr)
        return 2
    try:
        ix = _open(args)
    except (ConfigError, ext.ExtError) as err:
        print(f"doclookup: {err}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as err:
        print(f"doclookup: 색인을 만들거나 열 수 없다: {err}", file=sys.stderr)
        return 2
    try:
        return args.handler(ix, args)
    except ConfigError as err:
        print(f"doclookup: {err}", file=sys.stderr)
        return 2
    finally:
        ix.conn.close()
