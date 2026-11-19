import json
import os
import sqlite3
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pytest

from doclookup import index
from doclookup.config import Config, load
from doclookup.index import build, ensure, open_index, signature, target_files

CONFIG = """\
match = ["fixture"]

[files]
markdown = ["**/*.md"]
text = ["tools/**/*.py"]
exclude = ["_workspace/**"]

[frontmatter]
columns = ["type", "status"]
"""

GUIDE = "---\ntype: Guide\nstatus: draft\nowner: 누구\n---\n# 개요\n본문\n## 세부\n내용\n"
NOTES = "머리말 글\n# 하나\n가\n# 둘\n나\n"
SCRIPT = "def main():\n    print('파트장')\n"


@dataclass
class Repo:
    root: Path
    cfg: Config
    out: Path

    def write(self, rel: str, content: str | bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repo"
    config = tmp_path / "configs" / "fixture.toml"
    config.parent.mkdir()
    config.write_text(CONFIG, encoding="utf-8")
    root.mkdir()
    repo = Repo(root.resolve(), load(config), tmp_path / "cache" / "index.sqlite")
    repo.write("docs/지침.md", GUIDE)
    repo.write("docs/notes.md", NOTES)
    repo.write("tools/make_summary.py", SCRIPT)
    repo.write("_workspace/draft.md", "# 초안\n")
    repo.write("tools/readme.txt", "글자\n")
    return repo


def rows(path: Path, sql: str, *args) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def meta(path: Path) -> dict[str, str]:
    return dict(rows(path, "SELECT key, value FROM meta"))


def test_target_files_applies_globs_excludes_and_sorts(fixture_repo):
    found = [p.relative_to(fixture_repo.root).as_posix() for p in target_files(fixture_repo.cfg, fixture_repo.root)]
    assert found == ["docs/notes.md", "docs/지침.md", "tools/make_summary.py"]


def test_double_star_matches_files_at_root(fixture_repo):
    fixture_repo.write("README.md", "# 읽기\n")
    found = [p.relative_to(fixture_repo.root).as_posix() for p in target_files(fixture_repo.cfg, fixture_repo.root)]
    assert "README.md" in found


def test_single_star_does_not_cross_folders(tmp_path, fixture_repo):
    config = tmp_path / "configs" / "narrow.toml"
    config.write_text('match = ["x"]\n[files]\nmarkdown = ["docs/*.md"]\n', encoding="utf-8")
    fixture_repo.write("docs/deep/inner.md", "# 안\n")
    found = [p.relative_to(fixture_repo.root).as_posix() for p in target_files(load(config), fixture_repo.root)]
    assert found == ["docs/notes.md", "docs/지침.md"]


def test_exclude_without_double_star_keeps_deeper_files(tmp_path, fixture_repo):
    config = tmp_path / "configs" / "shallow.toml"
    config.write_text('match = ["x"]\n[files]\nmarkdown = ["**/*.md"]\nexclude = ["docs/*"]\n', encoding="utf-8")
    fixture_repo.write("docs/deep/inner.md", "# 안\n")
    found = [p.relative_to(fixture_repo.root).as_posix() for p in target_files(load(config), fixture_repo.root)]
    assert found == ["_workspace/draft.md", "docs/deep/inner.md"]


def test_git_folder_is_not_scanned(fixture_repo):
    fixture_repo.write(".git/info/note.md", "# 깃\n")
    found = [p.relative_to(fixture_repo.root).as_posix() for p in target_files(fixture_repo.cfg, fixture_repo.root)]
    assert not any(f.startswith(".git/") for f in found)


def test_build_counts_sections_and_text_files(fixture_repo):
    stats = build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert stats.units == 7 and stats.files == 3 and stats.skipped == []
    kinds = rows(fixture_repo.out, "SELECT file, kind, title FROM units ORDER BY file, line")
    assert kinds == [
        ("docs/notes.md", "머리말", "머리말"),
        ("docs/notes.md", "절", "하나"),
        ("docs/notes.md", "절", "둘"),
        ("docs/지침.md", "프론트매터", "프론트매터"),
        ("docs/지침.md", "절", "개요"),
        ("docs/지침.md", "절", "세부"),
        ("tools/make_summary.py", "파일", None),
    ]


def test_text_file_is_one_unit(fixture_repo):
    fixture_repo.write("tools/crlf.py", b"a = 1\r\nb = 2\r\n")
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    got = rows(fixture_repo.out, "SELECT uid, kind, source, line, text_line, text FROM units WHERE file='tools/crlf.py'")
    assert got == [("tools/crlf.py", "파일", "text", 1, 1, "a = 1\nb = 2")]


def test_markdown_wins_when_file_matches_both_globs(tmp_path, fixture_repo):
    config = tmp_path / "configs" / "both.toml"
    config.write_text('match = ["x"]\n[files]\nmarkdown = ["docs/*.md"]\ntext = ["docs/**"]\n', encoding="utf-8")
    build(load(config), fixture_repo.root, fixture_repo.out)
    sources = {r[0] for r in rows(fixture_repo.out, "SELECT source FROM units WHERE file='docs/notes.md'")}
    assert sources == {"markdown"}


def test_frontmatter_columns_are_stored(fixture_repo):
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    got = rows(fixture_repo.out, "SELECT file, key, value FROM frontmatter ORDER BY key")
    assert got == [("docs/지침.md", "status", "draft"), ("docs/지침.md", "type", "Guide")]


def test_refs_and_ids_tables_exist_empty(fixture_repo):
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert rows(fixture_repo.out, "SELECT count(*) FROM refs") == [(0,)]
    assert rows(fixture_repo.out, "SELECT count(*) FROM ids") == [(0,)]
    columns = {name: [r[1] for r in rows(fixture_repo.out, f"PRAGMA table_info({name})")] for name in ("refs", "ids")}
    assert columns == {
        "refs": ["src_file", "src_line", "src_uid", "kind", "target", "scope", "raw", "broken"],
        "ids": ["name", "id", "scope", "file", "line", "uid"],
    }


def test_build_records_checkout_in_meta(fixture_repo):
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert meta(fixture_repo.out)["checkout"] == str(fixture_repo.root.resolve())


def test_build_records_signature_format_and_trigram(fixture_repo):
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    values = meta(fixture_repo.out)
    assert values["signature"] == signature(fixture_repo.cfg, fixture_repo.root, [])
    assert values["format"] == index.FORMAT
    assert values["trigram"] == ("1" if index.trigram_available() else "0")


def test_trigram_search_finds_units(fixture_repo):
    if not index.trigram_available():
        pytest.skip("이 SQLite에는 trigram이 없다")
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    got = rows(fixture_repo.out, "SELECT u.uid FROM bare_tri t JOIN units u ON u.rowid = t.rowid WHERE bare_tri MATCH ?",
               '"파트장"')
    assert got == [("tools/make_summary.py",)]


def test_bare_table_keeps_text_without_whitespace(fixture_repo):
    fixture_repo.write("docs/띄움.md", "# 띄운 제목\n정당성 검증\t을\n 한다\n")
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    got = rows(fixture_repo.out, "SELECT b.title, b.text FROM bare b JOIN units u ON u.rowid = b.rowid WHERE u.file=?",
               "docs/띄움.md")
    assert got == [("띄운제목", "정당성검증을한다")]


def test_build_without_trigram_skips_fts_table(monkeypatch, fixture_repo):
    monkeypatch.setattr(index, "trigram_available", lambda: False)
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    tables = {r[0] for r in rows(fixture_repo.out, "SELECT name FROM sqlite_master")}
    assert "bare_tri" not in tables and {"units", "bare"} <= tables
    assert meta(fixture_repo.out)["trigram"] == "0"


def test_ensure_rebuilds_only_when_signature_changes(fixture_repo):
    root, cfg, out = fixture_repo.root, fixture_repo.cfg, fixture_repo.out
    assert ensure(cfg, root, out) is True
    assert ensure(cfg, root, out) is False
    doc = root / "docs/notes.md"
    st = doc.stat()
    os.utime(doc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert ensure(cfg, root, out) is True
    assert ensure(cfg, root, out) is False


def test_ensure_rebuilds_when_file_is_added(fixture_repo):
    assert ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out) is True
    fixture_repo.write("docs/new.md", "# 새 문서\n")
    assert ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out) is True
    assert rows(fixture_repo.out, "SELECT uid FROM units WHERE file='docs/new.md'") == [("docs/new.md#새-문서",)]


def test_ensure_rebuilds_broken_index_file(fixture_repo):
    fixture_repo.out.parent.mkdir(parents=True)
    fixture_repo.out.write_bytes(b"not a database")
    assert ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out) is True
    assert meta(fixture_repo.out)["format"] == index.FORMAT


def test_ensure_rebuilds_index_of_other_format(monkeypatch, fixture_repo):
    assert ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out) is True
    monkeypatch.setattr(index, "FORMAT", index.FORMAT + "-next")
    assert ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out) is True


def test_signature_changes_when_config_changes(fixture_repo):
    before = signature(fixture_repo.cfg, fixture_repo.root, [])
    assert signature(fixture_repo.cfg, fixture_repo.root, []) == before
    fixture_repo.cfg.path.write_text(CONFIG + "\n# 주석 한 줄\n", encoding="utf-8")
    assert signature(fixture_repo.cfg, fixture_repo.root, []) != before


def test_signature_includes_extra_files(tmp_path, fixture_repo):
    extra = tmp_path / "outside.txt"
    extra.write_text("가", encoding="utf-8")
    before = signature(fixture_repo.cfg, fixture_repo.root, [extra])
    assert before != signature(fixture_repo.cfg, fixture_repo.root, [])
    extra.write_text("가나", encoding="utf-8")
    assert signature(fixture_repo.cfg, fixture_repo.root, [extra]) != before


def test_signature_tolerates_missing_extra_file(tmp_path, fixture_repo):
    missing = tmp_path / "gone.txt"
    assert signature(fixture_repo.cfg, fixture_repo.root, [missing]) != signature(fixture_repo.cfg, fixture_repo.root, [])


def test_signature_ignores_files_outside_globs(fixture_repo):
    before = signature(fixture_repo.cfg, fixture_repo.root, [])
    fixture_repo.write("_workspace/other.md", "# 제외\n")
    fixture_repo.write("tools/notes.txt", "안 봄\n")
    assert signature(fixture_repo.cfg, fixture_repo.root, []) == before


def test_deleted_file_disappears_after_rebuild(fixture_repo):
    root, cfg, out = fixture_repo.root, fixture_repo.cfg, fixture_repo.out
    ensure(cfg, root, out)
    assert rows(out, "SELECT count(*) FROM units WHERE file='docs/지침.md'") == [(3,)]
    (root / "docs/지침.md").unlink()
    assert ensure(cfg, root, out) is True
    assert rows(out, "SELECT count(*) FROM units WHERE file='docs/지침.md'") == [(0,)]
    assert rows(out, "SELECT count(*) FROM frontmatter WHERE file='docs/지침.md'") == [(0,)]


def test_renamed_file_keeps_only_new_name(fixture_repo):
    root, cfg, out = fixture_repo.root, fixture_repo.cfg, fixture_repo.out
    ensure(cfg, root, out)
    (root / "docs/notes.md").rename(root / "docs/memo.md")
    assert ensure(cfg, root, out) is True
    files = {r[0] for r in rows(out, "SELECT DISTINCT file FROM units")}
    assert "docs/notes.md" not in files and "docs/memo.md" in files


def test_deleted_file_disappears_from_search_index(fixture_repo):
    if not index.trigram_available():
        pytest.skip("이 SQLite에는 trigram이 없다")
    root, cfg, out = fixture_repo.root, fixture_repo.cfg, fixture_repo.out
    ensure(cfg, root, out)
    (root / "tools/make_summary.py").unlink()
    ensure(cfg, root, out)
    assert rows(out, "SELECT rowid FROM bare_tri WHERE bare_tri MATCH ?", '"파트장"') == []


def test_build_tolerates_bom_crlf_and_non_utf8(fixture_repo):
    fixture_repo.write("docs/bom.md", "﻿# 제목\r\n본문\r\n\r\n## 둘째\r\n끝\r\n".encode("utf-8"))
    fixture_repo.write("docs/bom-front.md", "﻿---\r\ntype: Screen\r\n---\r\n# 화면\r\n".encode("utf-8"))
    fixture_repo.write("docs/legacy.md", "# 옛 문서\n한글\n".encode("cp949"))
    stats = build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert stats.skipped == ["docs/legacy.md: UTF-8이 아니다"]
    assert stats.files == 5
    got = rows(fixture_repo.out, "SELECT kind, title, slug, line, text_line, text FROM units WHERE file='docs/bom.md' ORDER BY line")
    assert got == [("절", "제목", "제목", 1, 2, "본문"), ("절", "둘째", "둘째", 4, 5, "끝")]
    assert rows(fixture_repo.out, "SELECT key, value FROM frontmatter WHERE file='docs/bom-front.md'") == [("type", "Screen")]
    assert rows(fixture_repo.out, "SELECT line FROM units WHERE uid='docs/bom-front.md#화면'") == [(4,)]
    assert rows(fixture_repo.out, "SELECT count(*) FROM units WHERE file='docs/legacy.md'") == [(0,)]
    assert json.loads(meta(fixture_repo.out)["skipped"]) == ["docs/legacy.md: UTF-8이 아니다"]


def test_unreadable_file_is_skipped(fixture_repo):
    path = fixture_repo.write("docs/secret.md", "# 비밀\n")
    path.chmod(0)
    try:
        stats = build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    finally:
        path.chmod(0o600)
    assert stats.skipped == ["docs/secret.md: 읽을 수 없다"]


def test_restored_read_permission_triggers_rebuild(fixture_repo):
    path = fixture_repo.write("docs/secret.md", "# 비밀\n")
    path.chmod(0)
    try:
        assert index.ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    finally:
        path.chmod(0o600)
    assert index.ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert rows(fixture_repo.out, "SELECT count(*) FROM units WHERE file='docs/secret.md'") == [(1,)]


def test_decomposed_file_name_is_stored_composed(fixture_repo):
    name = unicodedata.normalize("NFD", "한글.md")
    (fixture_repo.root / "docs" / name).write_text("# 가\n", encoding="utf-8")
    if unicodedata.normalize("NFD", "한글.md") not in os.listdir(fixture_repo.root / "docs"):
        pytest.skip("이 파일 시스템은 이름을 NFD로 두지 않는다")
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    files = {r[0] for r in rows(fixture_repo.out, "SELECT DISTINCT file FROM units")}
    assert "docs/한글.md" in files


def test_replace_retries_once_on_permission_error(monkeypatch, fixture_repo):
    real_replace, calls, sleeps = os.replace, [], []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("다른 프로세스가 쓰는 중")
        real_replace(src, dst)

    monkeypatch.setattr(index.os, "replace", flaky)
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    assert ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out) is True
    assert len(calls) == 2 and sleeps == [0.1]
    assert meta(fixture_repo.out)["checkout"] == str(fixture_repo.root)
    assert sorted(p.name for p in fixture_repo.out.parent.iterdir()) == ["index.sqlite"]


def test_replace_gives_up_after_three_retries(monkeypatch, fixture_repo):
    calls, sleeps = [], []

    def always_fails(src, dst):
        calls.append(dst)
        raise PermissionError("다른 프로세스가 쓰는 중")

    monkeypatch.setattr(index.os, "replace", always_fails)
    monkeypatch.setattr(index.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert len(calls) == 4 and sleeps == [0.1, 0.1, 0.1]
    assert list(fixture_repo.out.parent.iterdir()) == []


def test_failed_build_leaves_no_temporary_file(monkeypatch, fixture_repo):
    def broken(*args, **kwargs):
        raise RuntimeError("만들다 실패")

    monkeypatch.setattr(index, "_units", broken)
    with pytest.raises(RuntimeError):
        ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert list(fixture_repo.out.parent.iterdir()) == []


def test_index_file_is_private(fixture_repo):
    ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert stat.S_IMODE(fixture_repo.out.stat().st_mode) == 0o600
    assert stat.S_IMODE(fixture_repo.out.parent.stat().st_mode) == 0o700


def test_build_replaces_existing_output(fixture_repo):
    build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    (fixture_repo.root / "docs/notes.md").unlink()
    stats = build(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert stats.files == 2
    assert stat.S_IMODE(fixture_repo.out.stat().st_mode) == 0o600


def test_open_index_reads_trigram_flag_and_rows(fixture_repo):
    ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    ix = open_index(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    try:
        assert ix.trigram is index.trigram_available()
        assert ix.cfg is fixture_repo.cfg and ix.root == fixture_repo.root
        row = ix.conn.execute("SELECT uid, file FROM units WHERE kind='파일'").fetchone()
        assert row["uid"] == "tools/make_summary.py" and row["file"] == "tools/make_summary.py"
    finally:
        ix.conn.close()


def test_open_index_is_read_only(fixture_repo):
    ensure(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    ix = open_index(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ix.conn.execute("DELETE FROM units")
    finally:
        ix.conn.close()


def test_open_index_without_file_raises(fixture_repo):
    with pytest.raises(FileNotFoundError):
        open_index(fixture_repo.cfg, fixture_repo.root, fixture_repo.out)
    assert not fixture_repo.out.exists()


def test_open_index_handles_special_characters_in_path(tmp_path, fixture_repo):
    out = tmp_path / "캐시 #1 ?%" / "index.sqlite"
    assert ensure(fixture_repo.cfg, fixture_repo.root, out) is True
    assert ensure(fixture_repo.cfg, fixture_repo.root, out) is False
    ix = open_index(fixture_repo.cfg, fixture_repo.root, out)
    try:
        assert ix.conn.execute("SELECT count(*) FROM units").fetchone()[0] == 7
    finally:
        ix.conn.close()


@pytest.mark.parametrize(("link", "name"), [("![](img.png)", "img.png"), ("[그림](img.png#top)", "img.png"),
                                          ("![](a%23b.png)", "a#b.png")])
def test_ensure_rebuilds_when_unindexed_link_target_changes(fixture_repo, link, name):
    repo = fixture_repo
    repo.write("docs/pic.md", f"# 그림\n\n{link}\n")
    ensure(repo.cfg, repo.root, repo.out)
    assert ensure(repo.cfg, repo.root, repo.out) is False
    image = repo.write(f"docs/{name}", b"x")
    assert ensure(repo.cfg, repo.root, repo.out) is True
    broken = rows(repo.out, "SELECT broken FROM refs WHERE src_file='docs/pic.md' AND kind='link'")
    assert broken == [(0,)]
    image.unlink()
    assert ensure(repo.cfg, repo.root, repo.out) is True
