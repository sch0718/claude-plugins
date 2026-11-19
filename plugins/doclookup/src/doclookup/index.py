import functools
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from dataclasses import astuple, dataclass, field, fields
from pathlib import Path

from doclookup.config import Config
from doclookup.markdown import frontmatter_values, links, sections, split_frontmatter
from doclookup.model import Link, Unit
from doclookup.paths import glob_regex as _glob
from doclookup.paths import matches_glob as _matches
from doclookup import ext as extensions
from doclookup.ext import ExtData
from doclookup.refs import extract, link_path, located_path

FORMAT = "4"
REPLACE_RETRIES = 3
REPLACE_DELAY = 0.1

_UNIT_COLUMNS = [f.name for f in fields(Unit)]
SCHEMA = f"""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE units(
    uid TEXT PRIMARY KEY, doc TEXT, kind TEXT, number TEXT, title TEXT, path TEXT, parent TEXT,
    file TEXT, line INTEGER, text TEXT, slug TEXT, source TEXT, text_line INTEGER
);
CREATE INDEX units_file ON units(file);
CREATE INDEX units_parent ON units(parent);
CREATE TABLE frontmatter(file TEXT, key TEXT, value TEXT);
CREATE INDEX frontmatter_key ON frontmatter(key, value);
CREATE TABLE refs(
    src_file TEXT, src_line INTEGER, src_uid TEXT, kind TEXT, target TEXT, scope TEXT, raw TEXT, broken INTEGER
);
CREATE INDEX refs_target ON refs(target);
CREATE INDEX refs_src ON refs(src_file);
CREATE TABLE ids(name TEXT, id TEXT, scope TEXT, file TEXT, line INTEGER, uid TEXT);
CREATE INDEX ids_id ON ids(id);
CREATE TABLE bare(rowid INTEGER PRIMARY KEY, title TEXT, text TEXT);
"""
_TRIGRAM_TABLE = "CREATE VIRTUAL TABLE bare_tri USING fts5(title, text, content='bare', content_rowid='rowid', tokenize='trigram');"
_SPACE = re.compile(r"\s+")


def bare(text: str | None) -> str | None:
    """공백을 모두 뺀 글. 띄어쓰기만 다른 표기를 한 색인으로 찾으려고 `bare` 표에 넣는다."""
    return None if text is None else _SPACE.sub("", text)


@dataclass
class BuildStats:
    units: int
    files: int
    skipped: list[str] = field(default_factory=list)


@dataclass
class Index:
    conn: sqlite3.Connection
    trigram: bool
    cfg: Config
    root: Path


@functools.lru_cache(maxsize=None)
def trigram_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return True


def _pruned(rel_dir: str, excludes: list[str]) -> bool:
    return any(p.rstrip("/").endswith("/**") and _glob(p.rstrip("/")[:-3]).fullmatch(rel_dir) for p in excludes)


def _classified(cfg: Config, root: Path) -> list[tuple[Path, str, str]]:
    """(실제 경로, NFC 상대 경로, "markdown"|"text")를 상대 경로 순으로. 두 glob에 다 맞으면 마크다운이다."""
    found = []
    for current, dirs, names in os.walk(root):
        base = Path(current)
        rel_base = unicodedata.normalize("NFC", base.relative_to(root).as_posix())
        prefix = "" if rel_base == "." else rel_base + "/"
        dirs[:] = sorted(d for d in dirs if d != ".git" and not _pruned(prefix + unicodedata.normalize("NFC", d), cfg.exclude))
        for name in names:
            rel = prefix + unicodedata.normalize("NFC", name)
            if _matches(rel, cfg.exclude):
                continue
            kind = "markdown" if _matches(rel, cfg.markdown) else "text" if _matches(rel, cfg.text) else None
            if kind and (base / name).is_file():
                found.append((base / name, rel, kind))
    return sorted(found, key=lambda item: item[1])


def target_files(cfg: Config, root: Path) -> list[Path]:
    return [path for path, _, _ in _classified(cfg, root)]


def _stamp(label: str, path: Path) -> list:
    try:
        st = path.stat()
    except OSError:
        return [label, None, None, None]
    return [label, st.st_mtime_ns, st.st_size, os.access(path, os.R_OK)]


def signature(cfg: Config, root: Path, extra_files: list[Path]) -> str:
    digest = hashlib.sha256()
    config = cfg.path.read_bytes()
    stamps = [_stamp(rel, path) for path, rel, _ in _classified(cfg, root)]
    stamps += [_stamp(str(path), path) for path in extra_files]
    head = [FORMAT, trigram_available(), len(config)]
    digest.update(json.dumps(head).encode("utf-8") + b"\n" + config + b"\n")
    digest.update(json.dumps(stamps, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def _decode(path: Path) -> tuple[str | None, str | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, "읽을 수 없다"
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "UTF-8이 아니다"


def _text_unit(rel: str, text: str) -> Unit:
    return Unit(rel, rel, "파일", None, None, "파일", None, rel, 1, "\n".join(text.splitlines()), None, "text", 1)


def _units(cfg: Config, root: Path, supplied: set[str]) -> tuple[list[Unit], list[Link], list[tuple[str, str, str]], int,
                                                                  list[str]]:
    """확장이 단위를 공급하는 파일(`supplied`)은 절로 나누지 않는다."""
    units, found, front, files, skipped = [], [], [], 0, []
    for path, rel, kind in _classified(cfg, root):
        if rel in supplied:
            continue
        text, problem = _decode(path)
        if problem:
            skipped.append(f"{rel}: {problem}")
            continue
        files += 1
        if kind == "text":
            units.append(_text_unit(rel, text))
            continue
        units.extend(sections(rel, text, cfg.anchor))
        found.extend(links(rel, text))
        block, _, _ = split_frontmatter(text)
        if block is not None and cfg.frontmatter_columns:
            front.extend((rel, key, value) for key, value in frontmatter_values(block, cfg.frontmatter_columns).items())
    return units, found, front, files, skipped


def _sidecars(path: Path) -> list[Path]:
    return [path, *(path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm"))]


def _dep_state(paths: list[str]) -> list:
    return [[path, Path(path).exists()] for path in paths]


def build(cfg: Config, root: Path, out: Path, ext_data: ExtData | None = None, sig: str | None = None) -> BuildStats:
    """서명은 파일을 읽기 전에 잡는다. 읽는 동안 바뀐 파일은 다음 ensure에서 서명이 달라 다시 색인된다.
    링크 대상의 존재 여부(색인 밖 파일 포함)는 meta `deps`에 실제 경로로 남겨 ensure가 다시 확인한다."""
    data = ext_data or ExtData()
    sig = sig or signature(cfg, root, data.files)
    trigram = trigram_available()
    supplied = {u.file for u in data.units}
    units, found, front, files, skipped = _units(cfg, root, supplied)
    units += data.units
    refs, ids = extract(cfg, root, units, found, data.roots, data.cites)
    targets = {str(located_path(link_path(link.file, link), root, data.roots)) for link in found}
    deps = _dep_state(sorted(targets))

    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in _sidecars(out):
        path.unlink(missing_ok=True)
    os.close(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.chmod(out, 0o600)
    conn = sqlite3.connect(out)
    try:
        conn.executescript(SCHEMA + (_TRIGRAM_TABLE if trigram else ""))
        placeholders = ",".join("?" * len(_UNIT_COLUMNS))
        conn.executemany(f"INSERT INTO units({','.join(_UNIT_COLUMNS)}) VALUES ({placeholders})",
                         [astuple(u) for u in units])
        conn.executemany("INSERT INTO frontmatter VALUES (?,?,?)", front)
        conn.executemany("INSERT INTO refs VALUES (?,?,?,?,?,?,?,?)", [astuple(r) for r in refs])
        conn.executemany("INSERT INTO ids VALUES (?,?,?,?,?,?)", [astuple(d) for d in ids])
        conn.create_function("bare", 1, bare, deterministic=True)
        conn.execute("INSERT INTO bare(rowid, title, text) SELECT rowid, bare(title), bare(text) FROM units")
        if trigram:
            conn.execute("INSERT INTO bare_tri(bare_tri) VALUES('rebuild')")
        conn.executemany("INSERT INTO meta VALUES (?,?)", [
            ("checkout", str(root.resolve())),
            ("signature", sig),
            ("format", FORMAT),
            ("trigram", "1" if trigram else "0"),
            ("skipped", json.dumps(skipped, ensure_ascii=False)),
            ("deps", json.dumps(deps, ensure_ascii=False)),
        ])
        conn.commit()
    finally:
        conn.close()
    return BuildStats(len(units), files, skipped)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _stored_value(path: Path, key: str) -> str | None:
    try:
        conn = _connect_read_only(path)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _stored_signature(path: Path) -> str | None:
    return _stored_value(path, "signature")


def _deps_current(path: Path) -> bool:
    raw = _stored_value(path, "deps")
    if raw is None:
        return False
    stored = json.loads(raw)
    return _dep_state([p for p, _ in stored]) == stored


def _replace(src: Path, dst: Path) -> None:
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == REPLACE_RETRIES:
                raise
            time.sleep(REPLACE_DELAY)


def ensure(cfg: Config, root: Path, path: Path, ext=None) -> bool:
    """색인이 없거나 서명이 다르면 같은 폴더의 임시 파일에 새로 만들어 바꿔 끼운다. 다시 만들었으면 True.
    설정에 확장이 있으면 서명에 확장의 `signature-files`를 더하고, 다시 만들 때만 단위·경로 대응·인용을 받는다.
    `ext`는 그 일을 맡을 모듈(기본 doclookup.ext)이다. 실패하면 ext.ExtError."""
    provider = ext or extensions
    extra = provider.signature_files(cfg, root) if cfg.extensions else []
    sig = signature(cfg, root, extra)
    if path.is_file() and _stored_signature(path) == sig and _deps_current(path):
        return False
    data = provider.collect(cfg, root) if cfg.extensions else None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".index-", suffix=".sqlite", dir=path.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        build(cfg, root, tmp, data, sig)
        _replace(tmp, path)
    finally:
        for leftover in _sidecars(tmp):
            leftover.unlink(missing_ok=True)
    return True


def open_index(cfg: Config, root: Path, path: Path) -> Index:
    """읽기 전용으로 연다. 최신으로 맞추는 일은 부르는 쪽이 먼저 ensure로 한다."""
    if not path.is_file():
        raise FileNotFoundError(f"색인이 없다: {path}")
    conn = _connect_read_only(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='trigram'").fetchone()
    except sqlite3.Error:
        conn.close()
        raise
    return Index(conn, bool(row) and row["value"] == "1", cfg, root)
