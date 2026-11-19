import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from doclookup.config import load
from doclookup.index import build
from doclookup.markdown import links, sections
from doclookup.model import Link
from doclookup.refs import bounded_id, extract, id_definitions, id_mentions, resolve_link

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class Catalog:
    root: Path
    cfg: object
    units: list
    links: list


@pytest.fixture
def catalog() -> Catalog:
    root = FIXTURES / "catalog"
    cfg = load(FIXTURES / "catalog.toml")
    units, found = [], []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        units += sections(rel, text, cfg.anchor)
        found += links(rel, text)
    return Catalog(root, cfg, units, found)


def one_link(text: str, file: str = "a.md") -> Link:
    return links(file, text)[0]


def test_absolute_and_relative_links_resolve():
    files = {"docs/a.md", "docs/sub/b.md", "c.md"}
    slugs = {"docs/a.md": set(), "docs/sub/b.md": {"절"}, "c.md": set()}
    assert resolve_link("docs/a.md", one_link("[x](/c.md)", "docs/a.md"), Path("."), {}, files, slugs) == ("c.md", False)
    assert resolve_link("docs/a.md", one_link("[x](sub/b.md#절)", "docs/a.md"), Path("."), {}, files, slugs) == (
        "docs/sub/b.md", False)
    assert resolve_link("docs/sub/b.md", one_link("[x](../a.md)", "docs/sub/b.md"), Path("."), {}, files, slugs) == (
        "docs/a.md", False)


def test_same_file_anchor_resolves():
    link = links("a.md", "[나1](#나1-절)")[0]
    target, broken = resolve_link("a.md", link, Path("."), {}, {"a.md"}, {"a.md": {"나1-절"}})
    assert target == "a.md" and broken is False


def test_missing_same_file_anchor_is_broken():
    link = links("a.md", "[나1](#나1-절)")[0]
    target, broken = resolve_link("a.md", link, Path("."), {}, {"a.md"}, {"a.md": set()})
    assert target == "a.md" and broken is True


def test_missing_anchor_is_broken():
    link = one_link("[x](b.md#없음)")
    assert resolve_link("a.md", link, Path("."), {}, {"a.md", "b.md"}, {"a.md": set(), "b.md": {"있음"}}) == (
        "b.md", True)


def test_missing_file_is_broken(tmp_path):
    assert resolve_link("a.md", one_link("[x](없음.md)"), tmp_path, {}, {"a.md"}, {"a.md": set()}) == ("없음.md", True)


def test_unindexed_file_on_disk_is_not_broken(tmp_path):
    (tmp_path / "그림.png").write_bytes(b"")
    assert resolve_link("a.md", one_link("[x](그림.png)"), tmp_path, {}, {"a.md"}, {}) == ("그림.png", False)


def test_anchor_of_text_file_is_not_checked(tmp_path):
    link = one_link("[x](tools/a.py#L3)")
    assert resolve_link("a.md", link, tmp_path, {}, {"a.md", "tools/a.py"}, {"a.md": set()}) == ("tools/a.py", False)


def test_anchor_compares_case_insensitively():
    link = one_link("[x](b.md#D-012)")
    assert resolve_link("a.md", link, Path("."), {}, {"b.md"}, {"b.md": {"d-012"}}) == ("b.md", False)


def test_roots_mapping_resolves_outside_checkout(tmp_path):
    outside = tmp_path / "main" / "20.원문"
    outside.mkdir(parents=True)
    (outside / "a.md").write_text("# a\n", encoding="utf-8")
    checkout = tmp_path / "wt"
    checkout.mkdir()
    link = one_link("[원문](/20.원문/a.md#a)", "docs/x.md")
    roots = {"20.원문/": outside}
    assert resolve_link("docs/x.md", link, checkout, roots, {"docs/x.md"}, {}) == ("20.원문/a.md", False)
    missing = one_link("[원문](/20.원문/b.md)", "docs/x.md")
    assert resolve_link("docs/x.md", missing, checkout, roots, {"docs/x.md"}, {}) == ("20.원문/b.md", True)


def test_same_id_two_scopes_defined_separately(catalog):
    defs = id_definitions(catalog.cfg, catalog.units)
    assert {(d.id, d.scope) for d in defs if d.id == "D-001"} == {("D-001", "시스템가"), ("D-001", "시스템나")}
    first = next(d for d in defs if d.id == "D-001" and d.scope == "시스템가")
    assert (first.name, first.file, first.line, first.uid) == (
        "결정", "product/시스템가/decision-log.md", 3, "product/시스템가/decision-log.md#d-001첫-결정")


def test_heading_definition_only_in_configured_files(catalog):
    defs = id_definitions(catalog.cfg, catalog.units)
    assert all(d.file.endswith("decision-log.md") for d in defs if d.name == "결정")


def test_table_first_cell_definition(catalog):
    defs = [d for d in id_definitions(catalog.cfg, catalog.units) if d.name == "기능 ID"]
    assert [(d.id, d.file, d.line, d.scope) for d in defs] == [
        ("SCR-ABC-03-09", "product/시스템가/요구사항.md", 5, ""),
        ("SCR-ABC-03-10", "product/시스템가/요구사항.md", 6, ""),
    ]


def test_frontmatter_definition(catalog):
    defs = [d for d in id_definitions(catalog.cfg, catalog.units) if d.name == "화면 ID"]
    assert [(d.id, d.file, d.line) for d in defs] == [("SCR-ABC", "product/시스템가/screens/조회.md", 3)]


def mentions(catalog, ident):
    return [r for r in id_mentions(catalog.cfg, catalog.units, catalog.links) if r.target == ident]


def test_link_target_scope_wins_for_cross_system_mention(catalog):
    found = [r for r in mentions(catalog, "D-001") if r.src_file == "product/시스템나/decision-log.md" and r.src_line == 5]
    assert len(found) == 1
    assert (found[0].kind, found[0].scope, found[0].raw) == ("id", "시스템가", "D-001")
    assert found[0].src_uid == "product/시스템나/decision-log.md#d-001나-첫-결정"


def test_mention_scope_from_file_path(catalog):
    found = [r for r in mentions(catalog, "D-001") if r.src_file == "product/시스템가/decision-log.md" and r.src_line == 9]
    assert [r.scope for r in found] == ["시스템가"]


def test_mention_in_log_md_is_scope_unknown(catalog):
    found = [r for r in mentions(catalog, "D-001") if r.src_file == "log.md"]
    assert [(r.src_line, r.scope) for r in found] == [(3, "범위 불명")]


def test_id_inside_link_target_not_counted(catalog):
    lines = [r.src_line for r in mentions(catalog, "D-001") if r.src_file == "product/시스템나/decision-log.md"]
    assert 6 not in lines


def test_id_without_scope_has_empty_scope_and_needs_boundary(catalog):
    screen = [r for r in id_mentions(catalog.cfg, catalog.units, catalog.links) if r.target == "SCR-ABC"]
    assert all(r.scope == "" for r in screen)
    assert not any(r.src_file.endswith("요구사항.md") for r in screen)
    assert any(r.target == "SCR-ABC-03-09" and r.src_file.endswith("조회.md")
               for r in id_mentions(catalog.cfg, catalog.units, catalog.links))


def test_extract_links_and_skips_definition_lines(catalog):
    refs, defs = extract(catalog.cfg, catalog.root, catalog.units, catalog.links, {})
    assert not any(r.kind == "id" and (r.src_file, r.src_line, r.target) == ("product/시스템가/decision-log.md", 3, "D-001")
                   for r in refs)
    link_refs = {(r.src_file, r.src_line): r for r in refs if r.kind == "link"}
    back = link_refs[("product/시스템가/D-001-배경.md", 3)]
    assert (back.target, back.broken, back.raw) == (
        "product/시스템가/decision-log.md#d-002둘째-결정", False, "[돌아가기](decision-log.md#d-002둘째-결정)")
    assert back.src_uid == "product/시스템가/D-001-배경.md#배경"
    assert link_refs[("product/시스템가/D-001-배경.md", 4)].broken is True
    assert link_refs[("product/시스템가/D-001-배경.md", 5)].target == "product/없음.md"
    assert link_refs[("product/시스템가/D-001-배경.md", 5)].broken is True
    assert link_refs[("log.md", 4)].broken is False
    assert len(defs) == 6


def test_build_fills_refs_and_ids(tmp_path):
    root = tmp_path / "catalog"
    shutil.copytree(FIXTURES / "catalog", root)
    cfg = load(FIXTURES / "catalog.toml")
    out = tmp_path / "index.sqlite"
    build(cfg, root, out)
    conn = sqlite3.connect(out)
    try:
        ids = conn.execute("SELECT name, id, scope, file, line, uid FROM ids WHERE id='D-001' ORDER BY scope").fetchall()
        broken = conn.execute("SELECT src_file, src_line FROM refs WHERE kind='link' AND broken=1 ORDER BY src_line").fetchall()
        unknown = conn.execute("SELECT src_file FROM refs WHERE kind='id' AND scope='범위 불명'").fetchall()
    finally:
        conn.close()
    assert [row[2] for row in ids] == ["시스템가", "시스템나"]
    assert broken == [("product/시스템가/D-001-배경.md", 4), ("product/시스템가/D-001-배경.md", 5)]
    assert unknown == [("log.md",)]


@pytest.mark.parametrize("pattern", ["(?i)D-[0-9]{3}", "(?i)(?s)D-[0-9]{3}", "(?is)D-[0-9]{3}"])
def test_bounded_id_keeps_leading_global_flags(pattern):
    assert bounded_id(pattern).findall("xd-123 d-456 D-789") == ["d-456", "D-789"]
