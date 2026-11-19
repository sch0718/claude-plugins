"""Company-word checks over text, JSON and files, plus JSON-with-comments reading."""
from __future__ import annotations

import codecs
import json
import os
import re
import unicodedata
from pathlib import Path

from session_move.company import CompanyRules

CHUNK = 64 * 1024
OVERLAP = 256
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"}
_NORM = re.compile(r"[-_.\s]+")


def _norm(s: str) -> str:
    return _NORM.sub("", unicodedata.normalize("NFKC", s).lower())


def denylist_path(home: Path) -> Path:
    return Path(os.environ.get("SESSION_MOVE_DENYLIST") or home / ".config" / "claude-plugins" / "denylist.txt")


class Words:
    """Company words: company config orgs/repos plus every denylist entry, case/-/_/space insensitive."""

    def __init__(self, terms: list[str], regexes: list[re.Pattern]):
        variants = [v for x in terms for v in (x, json.dumps(x)[1:-1])]
        self.terms = [t for t in dict.fromkeys(_norm(x) for x in variants) if t]
        self.regexes = regexes

    @classmethod
    def load(cls, rules: CompanyRules | None, denylist: Path) -> tuple["Words", bool]:
        terms = list(rules.terms()) if rules else []
        regexes = []
        has_denylist = denylist.is_file()
        if has_denylist:
            for line in denylist.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("re:"):
                    regexes.append(re.compile(line[3:]))
                else:
                    terms.append(line)
        return cls(terms, regexes), has_denylist

    def hit(self, text: str) -> bool:
        n = _norm(text)
        return any(t in n for t in self.terms) or any(r.search(text) for r in self.regexes)

    def hit_json(self, value) -> bool:
        return self.hit(json.dumps(value, ensure_ascii=False))

    def file_status(self, path: Path) -> str:
        """'hit', 'binary' or 'clean'. Streams the whole file; multi-byte characters may straddle chunks."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        tail = ""
        with open(path, "rb") as f:
            first = True
            while True:
                raw = f.read(CHUNK)
                if first and b"\x00" in raw[:8192]:
                    return "binary"
                first = False
                text = decoder.decode(raw, final=not raw)
                if self.hit(tail + text):
                    return "hit"
                if not raw:
                    return "clean"
                tail = text[-OVERLAP:]


def iter_files(root: Path):
    for dp, dns, fns in os.walk(root, followlinks=False):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for name in fns:
            yield Path(dp) / name


def unit_status(root: Path, words: Words | None) -> str:
    """Status of a skill folder or a single file: 'hit', 'binary' or 'clean'."""
    target = root.resolve() if root.is_symlink() else root
    files = [target] if target.is_file() else list(iter_files(target))
    if words is None:
        return "clean"
    if words.hit(str(root.name)):
        return "hit"
    for p in files:
        status = words.file_status(p)
        if status != "clean":
            return status
    return "clean"


def _strip_comments(text: str) -> str:
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_jsonc(path: Path):
    return json.loads(_strip_comments(path.read_text(encoding="utf-8")))
