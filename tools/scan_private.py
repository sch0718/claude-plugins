"""Block private or company identifiers from entering this repository.

The denylist lives outside the repository. Plain lines match after normalization
(case, '-', '_', '.', and spaces ignored); lines starting with 're:' are regular
expressions. Hits are reported by rule number only, never by the matched text.
Exit codes: 0 clean, 1 hit, 2 denylist missing or empty, 3 usage or git error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DENYLIST = Path.home() / ".config" / "claude-plugins" / "denylist.txt"
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
BUILTIN = [
    ("B1", re.compile(r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})(?![\d.])")),
    ("B2", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]
SKIP_DIRS = {".git", ".superpowers", "node_modules", ".venv", "__pycache__", ".pytest_cache"}
_NORMALIZE = re.compile(r"[-_.\s]+")


def normalize(s: str) -> str:
    return _NORMALIZE.sub("", unicodedata.normalize("NFKC", s).lower())


@dataclass(frozen=True)
class Rule:
    rid: str
    term: str | None = None
    regex: re.Pattern | None = None


@dataclass(frozen=True)
class Hit:
    where: str
    rid: str


def load_denylist(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    rules = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            rules.append(Rule(f"D{i}", regex=re.compile(line[3:])))
        else:
            for variant in dict.fromkeys((line, json.dumps(line)[1:-1])):
                term = normalize(variant)
                if term:
                    rules.append(Rule(f"D{i}", term=term))
    return rules


def _email_ok(addr: str) -> bool:
    low = addr.lower()
    if "noreply" in low.split("@")[0]:
        return True
    domain = low.rsplit("@", 1)[-1]
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def _is_url_userinfo(text: str, m: re.Match) -> bool:
    """SSH remotes (git@host:...) and credentials in URLs (https://user@host) are not email addresses."""
    if m.group(0).split("@", 1)[0].lower() == "git":
        return True
    head = re.split(r"[\s\"'`(<\[]", text[:m.start()])[-1]
    return "://" in head and "/" not in head.split("://", 1)[1]


def scan_text(text: str, rules: list[Rule]) -> list[str]:
    found = []
    norm = normalize(text)
    for r in rules:
        if r.term is not None and r.term in norm:
            found.append(r.rid)
        elif r.regex is not None and r.regex.search(text):
            found.append(r.rid)
    if BUILTIN[0][1].search(text):
        found.append(BUILTIN[0][0])
    if any(not _email_ok(m.group(0)) and not _is_url_userinfo(text, m)
           for m in BUILTIN[1][1].finditer(text)):
        found.append(BUILTIN[1][0])
    return found


def scan_lines(label: str, text: str, rules: list[Rule]) -> list[Hit]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for rid in scan_text(line, rules):
            hits.append(Hit(f"{label}:{n}", rid))
    return hits


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def scan_blob(label: str, data: bytes, rules: list[Rule]) -> list[Hit]:
    hits = [Hit(f"{label} (path)", rid) for rid in scan_text(label, rules)]
    if not _is_binary(data):
        hits += scan_lines(label, data.decode("utf-8", errors="replace"), rules)
    return hits


def _git(*args: str, cwd: Path | None = None) -> bytes:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed")
    return res.stdout


def _walk(paths: list[str]):
    for p in paths:
        p = Path(p)
        if p.is_file():
            yield p
            continue
        for dp, dns, fns in os.walk(p):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for d in dns:
                yield Path(dp) / d
            for f in fns:
                yield Path(dp) / f


def scan_paths(paths: list[str], rules: list[Rule]) -> list[Hit]:
    hits = []
    for p in _walk(paths):
        if p.is_dir() or p.is_symlink():
            target = os.readlink(p) if p.is_symlink() else ""
            hits += [Hit(f"{p} (path)", rid) for rid in scan_text(f"{p} {target}", rules)]
            continue
        hits += scan_blob(str(p), p.read_bytes(), rules)
    return hits


def scan_all(rules: list[Rule]) -> list[Hit]:
    root = Path(_git("rev-parse", "--show-toplevel").decode().strip())
    files = _git("ls-files", "-z", cwd=root).decode().split("\0")
    hits = []
    for f in filter(None, files):
        p = root / f
        data = os.readlink(p).encode() if p.is_symlink() else (p.read_bytes() if p.exists() else b"")
        hits += scan_blob(f, data, rules)
    return hits


def scan_staged(rules: list[Rule]) -> list[Hit]:
    names = _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").decode().split("\0")
    hits = []
    for f in filter(None, names):
        hits += scan_blob(f, _git("show", f":{f}"), rules)
    return hits


def _commit_hits(sha: str, rules: list[Rule]) -> list[Hit]:
    short = sha[:10]
    hits = []
    meta = _git("log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%B", sha).decode("utf-8", "replace")
    hits += scan_lines(f"commit {short} (message/ident)", meta, rules)
    parents = _git("rev-list", "--parents", "-n", "1", sha).decode().split()[1:]
    diff_args = ["show", "--format=", "--no-renames", "-U0", "--no-color", sha] if parents else [
        "show", "--format=", "--no-renames", "-U0", "--no-color", "--root", sha]
    diff = _git(*diff_args).decode("utf-8", "replace")
    for n, line in enumerate(diff.splitlines(), 1):
        if line.startswith("+++ ") or line.startswith("--- "):
            target = f"commit {short} (path)"
        elif line.startswith("+"):
            target = f"commit {short} (added line {n})"
            line = line[1:]
        elif line.startswith("diff --git"):
            target = f"commit {short} (path)"
        else:
            continue
        hits += [Hit(target, rid) for rid in scan_text(line, rules)]
    return hits


def scan_commits(shas: list[str], rules: list[Rule]) -> list[Hit]:
    hits = []
    for sha in shas:
        hits += _commit_hits(sha, rules)
    return hits


def scan_history(rules: list[Rule]) -> list[Hit]:
    shas = _git("rev-list", "--all").decode().split()
    hits = scan_commits(shas, rules)
    refs = _git("for-each-ref", "--format=%(refname)").decode()
    hits += scan_lines("refs", refs, rules)
    tags = _git("for-each-ref", "--format=%(refname)%0a%(contents)", "refs/tags").decode("utf-8", "replace")
    hits += scan_lines("tag messages", tags, rules)
    return hits


def scan_range(spec: str, rules: list[Rule]) -> list[Hit]:
    shas = _git("rev-list", spec).decode().split()
    if ".." in spec:
        start = spec.split("..", 1)[0]
        if start:
            start_sha = _git("rev-parse", start).decode().strip()
            if not _git("rev-list", "--parents", "-n", "1", start_sha).decode().split()[1:]:
                shas.append(start_sha)
    return scan_commits(shas, rules)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--history", action="store_true")
    mode.add_argument("--range")
    mode.add_argument("--stdin", action="store_true")
    mode.add_argument("--paths", nargs="+")
    ap.add_argument("--denylist", type=Path,
                    default=Path(os.environ.get("CLAUDE_PLUGINS_DENYLIST", DEFAULT_DENYLIST)))
    args = ap.parse_args(argv)

    rules = load_denylist(args.denylist)
    if not rules:
        print(f"금지어 목록이 없거나 비어 있습니다: {args.denylist}", file=sys.stderr)
        return 2
    try:
        if args.stdin:
            hits = scan_lines("stdin", sys.stdin.read(), rules)
        elif args.paths:
            hits = scan_paths(args.paths, rules)
        elif args.all:
            hits = scan_all(rules)
        elif args.staged:
            hits = scan_staged(rules)
        elif args.history:
            hits = scan_history(rules)
        else:
            hits = scan_range(args.range, rules)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 3
    for h in hits:
        print(f"{h.where}: 규칙 {h.rid}")
    if hits:
        print(f"비공개 정보 의심 {len(hits)}건. 위 위치를 고친 뒤 다시 실행하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
