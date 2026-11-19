import os
import shutil
import time
from pathlib import Path

import pytest

from doclookup import cli
from doclookup.cli import main
from doclookup.index import build
from doclookup.config import load
from doclookup.query import impact, roles, variants

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ix(open_fixture):
    return open_fixture("docs")


@pytest.fixture
def catalog(open_fixture):
    return open_fixture("catalog")


def lines(hits_by_file):
    return {(file, h.line) for file, hits in hits_by_file.items() for h in hits}


def test_impact_term_includes_aliases_and_text_files(ix):
    imp = impact(ix, "파트장", None)
    assert "docs/지침.md" in imp.markdown and "tools/make_summary.py" in imp.text
    assert [h.line for h in imp.markdown["docs/지침.md"]] == [10, 11]
    assert [h.line for h in imp.text["tools/make_summary.py"]] == [3]


def test_impact_term_adds_links_to_section_with_that_title(catalog):
    imp = impact(catalog, "배경", None)
    assert ("product/시스템가/D-001-배경.md", 1) in lines(imp.markdown)
    imp = impact(catalog, "D-002/둘째-결정", None)
    assert ("product/시스템가/D-001-배경.md", 3) in lines(imp.markdown)


def test_impact_id_includes_links_and_mentions(catalog):
    imp = impact(catalog, "D-001", None)
    found = lines(imp.markdown)
    assert {("product/시스템나/decision-log.md", 5), ("log.md", 3), ("log.md", 4),
            ("product/시스템가/decision-log.md", 9)} <= found
    assert {("product/시스템가/decision-log.md", 3), ("product/시스템나/decision-log.md", 3)} <= found
    assert imp.text == {}


def test_impact_id_with_scope(catalog):
    found = lines(impact(catalog, "D-001", "시스템나").markdown)
    assert ("product/시스템나/decision-log.md", 3) in found and ("log.md", 3) in found
    assert ("product/시스템가/decision-log.md", 3) not in found


def test_impact_file_anchor_lists_linking_lines(catalog):
    imp = impact(catalog, "product/시스템가/decision-log.md#d-002둘째-결정", None)
    assert lines(imp.markdown) == {("product/시스템가/D-001-배경.md", 3)}
    imp = impact(catalog, "/product/시스템가/decision-log.md", None)
    assert lines(imp.markdown) == {("log.md", 4), ("product/시스템가/D-001-배경.md", 3),
                                   ("product/시스템가/D-001-배경.md", 4), ("product/시스템나/decision-log.md", 5)}


def test_impact_nothing(ix):
    imp = impact(ix, "없는낱말", None)
    assert imp.markdown == {} and imp.text == {}


@pytest.fixture
def big(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "big"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "많음.md").write_text("".join(f"파트장 줄 {n}\n" for n in range(300)), encoding="utf-8")
    (root / "docs" / "적음.md").write_text("파트 리더 한 줄\n", encoding="utf-8")
    config = tmp_path / "big.toml"
    config.write_text('match = ["big"]\n[files]\nmarkdown = ["**/*.md"]\n'
                      '[[aliases]]\ncanonical = "파트장"\nvariants = ["파트 리더"]\n', encoding="utf-8")
    return ["--root", str(root), "--config", str(config)]


def test_impact_limits_lines_per_file(big, capsys):
    assert main([*big, "impact", "파트장"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "마크다운 2개 파일 / 텍스트 0개 파일"
    assert out[1] == "[마크다운]"
    assert out[2] == "docs/많음.md (300건)"
    assert out[3:8] == [f"  docs/많음.md:{n}  파트장 줄 {n - 1}" for n in range(1, 6)]
    assert out[8] == "  … 295건 더"
    assert out[9:11] == ["docs/적음.md (1건)", "  docs/적음.md:1  파트 리더 한 줄 〔표기: 파트 리더〕"]


def test_impact_per_file_option(big, capsys):
    main([*big, "impact", "파트장", "--per-file", "2"])
    assert "  … 298건 더" in capsys.readouterr().out.splitlines()


def test_cli_impact_none_is_1(big, capsys):
    assert main([*big, "impact", "없는낱말"]) == 1


def test_variants_marks_candidates(ix):
    found = variants(ix, "파트장")
    assert all(c.rule for c in found)
    assert "파트 리더" not in {c.form for c in found} and "파트리더" not in {c.form for c in found}


def test_variants_suffix_pair_without_alias(open_fixture, tmp_path):
    shutil.copytree(FIXTURES / "docs", tmp_path / "docs")
    (tmp_path / "docs" / "docs" / "유닛.md").write_text("# 유닛\n\n유닛장과 유닛 리더, 유닛리더.\n", encoding="utf-8")
    found = {c.form: c for c in variants(open_fixture("docs"), "유닛장")}
    assert found["유닛 리더"].rule == "접미사 짝" and found["유닛 리더"].count == 1
    assert found["유닛리더"].rule == "접미사 짝"


def test_variants_same_stem(ix):
    found = {c.form: c for c in variants(ix, "변경수행자")}
    assert found["변경담당자"].rule == "같은 줄기" and found["변경담당자"].count == 2
    assert "변경수행자" not in found


def test_variants_spacing(open_fixture, tmp_path):
    shutil.copytree(FIXTURES / "docs", tmp_path / "docs")
    (tmp_path / "docs" / "docs" / "띄어쓰기.md").write_text("# 띄어\n\n변경 수행자가 있다.\n", encoding="utf-8")
    found = {c.form: c for c in variants(open_fixture("docs"), "변경수행자")}
    assert found["변경 수행자"].rule == "띄어쓰기"


def test_cli_variants_marks_candidate(ix, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code = main(["--root", str(ix.root), "--config", str(FIXTURES / "docs.toml"), "variants", "변경수행자"])
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and out[0].startswith("후보 ")
    assert "후보  변경담당자  2건  〔같은 줄기〕" in out


def test_roles_flags_undefined(ix):
    found = {h.matched for h in roles(ix)}
    assert found >= {"검증자", "승인 책임자"} and "변경관리자" not in found


def test_roles_matches_bare_and_spaced_names(ix):
    found = {(h.file, h.line): h.matched for h in roles(ix)}
    assert found[("docs/역할.md", 5)] == "검증자"
    assert found[("docs/역할.md", 6)] == "승인 책임자"
    assert found[("docs/역할.md", 7)] == "승인권자"
    assert found[("docs/역할.md", 8)] == "정당성 검증자"
    assert found[("docs/역할.md", 9)] == "변경 검증자"


def test_roles_excludes_defined_names(ix):
    found = {h.matched for h in roles(ix)}
    assert not found & {"변경 책임자", "변경책임자", "시스템 책임자"}
    assert ("docs/지침.md", 9) not in {(h.file, h.line) for h in roles(ix)}


def test_cli_roles(ix, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code = main(["--root", str(ix.root), "--config", str(FIXTURES / "docs.toml"), "roles"])
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and out[0].startswith("5건: ")
    assert "docs/역할.md:6  승인 책임자가 결재한다. 〔역할: 승인 책임자〕" in out


def test_cli_roles_without_suspect_is_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    shutil.copytree(FIXTURES / "docs", tmp_path / "docs")
    assert main(["--root", str(tmp_path / "docs"), "--config", str(FIXTURES / "catalog.toml"), "roles"]) == 2
    assert "roles.suspect" in capsys.readouterr().err


def _cache(tmp_path, name, checkout: str | None, age_days: float) -> Path:
    folder = tmp_path / "home" / "Library" / "Caches" / "doclookup" / f"{name}-0123456789ab"
    folder.mkdir(parents=True)
    index = folder / "index.sqlite"
    import sqlite3
    conn = sqlite3.connect(index)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    if checkout is not None:
        conn.execute("INSERT INTO meta VALUES ('checkout', ?)", (checkout,))
    conn.commit()
    conn.close()
    old = time.time() - age_days * 86400
    os.utime(index, (old, old))
    return folder


def test_gc_removes_orphans(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    alive = tmp_path / "alive"
    alive.mkdir()
    keep = _cache(tmp_path, "keep", str(alive), 1)
    gone = _cache(tmp_path, "gone", str(tmp_path / "없음"), 1)
    old = _cache(tmp_path, "old", str(alive), 40)
    other = tmp_path / "home" / "Library" / "Caches" / "doclookup" / "다른것"
    other.mkdir()
    assert main(["gc", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert keep.exists() and other.exists() and not gone.exists() and not old.exists()
    assert f"지움: {gone} (체크아웃 없음)" in out and f"지움: {old} (30일 넘게 안 씀)" in out
    assert "지운 폴더 2개" in out


def test_roles_skips_match_inside_defined_name(ix):
    found = {(h.file, h.line) for h in roles(ix)}
    assert ("docs/역할.md", 18) not in found and ("docs/역할.md", 19) not in found
