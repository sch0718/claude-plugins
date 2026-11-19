import pytest

from doclookup.index import Index
from doclookup.query import broken_links, expand, find_id, refs_to, search, section


@pytest.fixture
def ix(open_fixture):
    return open_fixture("docs")


@pytest.fixture(params=["trigram", "like"])
def any_ix(request, ix):
    """trigram이 있는 색인과, 같은 색인을 trigram이 없는 것처럼 LIKE로만 찾는 경우."""
    return ix if request.param == "trigram" else Index(ix.conn, False, ix.cfg, ix.root)


@pytest.fixture
def catalog(open_fixture):
    return open_fixture("catalog")


def lines(hits):
    return [(h.file, h.line) for h in hits]


def test_expand_includes_group_and_spaceless_forms(ix):
    assert expand(ix.cfg, "파트 리더", False) == ["파트 리더", "파트리더", "파트장"]
    assert expand(ix.cfg, "파트장", True) == ["파트장"]
    assert expand(ix.cfg, "변경 책임자", True) == ["변경 책임자", "변경책임자"]


def test_two_char_term_uses_like(any_ix):
    hits = search(any_ix, ["겸직"])
    assert lines(hits) == [("docs/지침.md", 11)]
    assert hits[0].matched == "겸직" and hits[0].context == "파트 리더는 겸직할 수 없다."


def test_space_variant_found(any_ix):
    hits = search(any_ix, ["변경 책임자"])
    assert {h.matched for h in hits} >= {"변경 책임자", "변경책임자"}
    assert ("docs/지침.md", 9) in lines(hits) and ("docs/지침.md", 10) in lines(hits)


def test_alias_group_expands_and_exact_does_not(any_ix):
    assert any(h.matched == "파트 리더" for h in search(any_ix, ["파트장"]))
    assert not any(h.matched == "파트 리더" for h in search(any_ix, ["파트장"], exact=True))


def test_search_returns_unit_source(any_ix):
    found = {(h.file, h.source) for h in search(any_ix, ["파트장"])}
    assert found == {("docs/지침.md", "markdown"), ("tools/make_summary.py", "text")}


def test_spaceless_query_finds_spaced_text(any_ix):
    hits = search(any_ix, ["정당성검증"])
    assert lines(hits) == [("docs/역할.md", 8)] and hits[0].matched == "정당성 검증"


def test_spaced_query_finds_spaceless_text(any_ix):
    hits = search(any_ix, ["변경 담당자"])
    assert lines(hits) == [("docs/역할.md", 16), ("docs/역할.md", 17)] and {h.matched for h in hits} == {"변경담당자"}


def test_short_spaceless_query_finds_spaced_text(open_fixture, tmp_path):
    import shutil
    from pathlib import Path
    shutil.copytree(Path(__file__).parent / "fixtures" / "docs", tmp_path / "docs")
    (tmp_path / "docs" / "docs" / "띄움.md").write_text("# 띄움\n\n겸  직은 안 된다.\n정당성\n검증은 줄을 넘는다.\n",
                                                       encoding="utf-8")
    ix = open_fixture("docs")
    for probe in (ix, Index(ix.conn, False, ix.cfg, ix.root)):
        hits = [h for h in search(probe, ["겸직"]) if h.file == "docs/띄움.md"]
        assert lines(hits) == [("docs/띄움.md", 3)] and hits[0].matched == "겸  직"
        assert [h for h in search(probe, ["정당성검증"]) if h.file == "docs/띄움.md"] == []


def test_search_reports_line_numbers_in_text_files(any_ix):
    hits = [h for h in search(any_ix, ["파트장"]) if h.file == "tools/make_summary.py"]
    assert lines(hits) == [("tools/make_summary.py", 3)] and hits[0].uid == "tools/make_summary.py"


def test_search_finds_heading_line(any_ix):
    assert lines(search(any_ix, ["특수 문자"])) == [("docs/지침.md", 13)]


@pytest.mark.parametrize("query, line", [
    ('"따옴표', 15), ("별*표", 16), ("(괄호", 17), ("a-b", 18), ("100%", 19), ("a_b", 20),
])
def test_search_special_characters(any_ix, query, line):
    assert lines(search(any_ix, [query])) == [("docs/지침.md", line)]


def test_search_folds_case_and_normalizes_nfc(any_ix):
    import unicodedata
    assert lines(search(any_ix, ["A_B"])) == [("docs/지침.md", 20)]
    assert lines(search(any_ix, [unicodedata.normalize("NFD", "겸직")])) == [("docs/지침.md", 11)]


def test_search_any_terms_and_empty_term(any_ix):
    assert lines(search(any_ix, ["겸직", "  ", "a_b"])) == [("docs/지침.md", 11), ("docs/지침.md", 20)]
    assert search(any_ix, ["  "]) == []


def test_where_filters_frontmatter(any_ix):
    assert lines(search(any_ix, ["확정"])) == [("docs/안내.md", 3), ("docs/화면.md", 6)]
    assert lines(search(any_ix, ["확정"], where={"type": "Screen"})) == [("docs/화면.md", 6)]
    assert search(any_ix, ["겸직"], where={"tags": "변경"}) != []
    assert search(any_ix, ["겸직"], where={"tags": "변"}) == []


def test_in_glob_limits_files(any_ix):
    assert {h.file for h in search(any_ix, ["파트장"], in_glob="tools")} == {"tools/make_summary.py"}
    assert {h.file for h in search(any_ix, ["파트장"], in_glob="docs/*.md")} == {"docs/지침.md"}


def test_section_by_anchor_prefix(ix):
    found = section(ix, "지침", "특수")
    assert [(u.uid, u.line, u.title) for u in found] == [("docs/지침.md#특수-문자", 13, "특수 문자")]
    assert [u.uid for u in section(ix, "docs/지침.md", "#역")] == ["docs/지침.md#역할"]
    assert section(ix, "없는문서", "역할") == []


def test_section_by_title_part(ix):
    assert [u.uid for u in section(ix, "화면", "조회")] == ["docs/화면.md#조회-화면"]


def test_find_id_groups_by_scope(catalog):
    found = find_id(catalog, "D-001", None)
    assert set(found) == {"시스템가", "시스템나"}
    assert [(d.file, d.line) for d in found["시스템가"]] == [("product/시스템가/decision-log.md", 3)]
    assert set(find_id(catalog, "D-001", "시스템나")) == {"시스템나"}
    assert find_id(catalog, "D-999", None) == {}


def test_refs_to_id_groups_by_scope_and_unknown(catalog):
    found = refs_to(catalog, "D-001", None)
    assert set(found) == {"시스템가", "범위 불명"}
    assert ("log.md", 3) in [(r.src_file, r.src_line) for r in found["범위 불명"]]
    lines_ga = [(r.src_file, r.src_line) for r in found["시스템가"]]
    assert ("product/시스템나/decision-log.md", 5) in lines_ga
    assert ("log.md", 4) in lines_ga
    assert len(lines_ga) == len(set(lines_ga))


def test_refs_to_id_with_scope_keeps_unknown(catalog):
    assert set(refs_to(catalog, "D-001", "시스템나")) == {"범위 불명"}


def test_refs_to_file_and_anchor(catalog):
    by_file = refs_to(catalog, "product/시스템가/decision-log.md", None)
    assert sorted((r.src_file, r.src_line) for r in by_file[""]) == [
        ("log.md", 4), ("product/시스템가/D-001-배경.md", 3), ("product/시스템가/D-001-배경.md", 4),
        ("product/시스템나/decision-log.md", 5)]
    by_anchor = refs_to(catalog, "/product/시스템가/decision-log.md#d-002둘째-결정", None)
    assert [(r.src_file, r.src_line) for r in by_anchor[""]] == [("product/시스템가/D-001-배경.md", 3)]


def test_broken_links(catalog):
    assert [(r.src_file, r.src_line) for r in broken_links(catalog)] == [
        ("product/시스템가/D-001-배경.md", 4), ("product/시스템가/D-001-배경.md", 5)]


def test_first_maps_casefolded_offsets_to_original():
    from doclookup.query import _first
    assert _first("ß x", ["x"]) == "x"
    assert _first("Straße Xy", ["xy"]) == "Xy"
    assert _first("STRASSE", ["straße"]) == "STRASSE"


def test_first_allows_spaces_between_query_letters():
    from doclookup.query import _first
    assert _first("정당성 검증자", ["정당성검증"]) == "정당성 검증"
    assert _first("변경책임자", ["변경 책임자"]) == "변경책임자"
    assert _first("가 나\t다 라", ["가나다"]) == "가 나\t다"
    assert _first("STRA SSE", ["straße"]) == "STRA SSE"


def test_first_with_empty_form_returns_empty():
    from doclookup.query import _first
    assert _first("abc", [""]) == "" and _first("", [""]) == ""


def test_refs_to_id_counts_a_line_once(open_fixture, tmp_path):
    import shutil
    from pathlib import Path
    FIXTURES = Path(__file__).parent / "fixtures"
    shutil.copytree(FIXTURES / "catalog", tmp_path / "catalog")
    (tmp_path / "catalog" / "log.md").write_text("# 작업 기록\n\n- D-001 그리고 D-001\n", encoding="utf-8")
    found = refs_to(open_fixture("catalog"), "D-001", None)
    assert [(r.src_file, r.src_line) for r in found["범위 불명"]] == [("log.md", 3)]


def test_refs_to_anchor_ignores_case(open_fixture, tmp_path):
    import shutil
    from pathlib import Path
    FIXTURES = Path(__file__).parent / "fixtures"
    shutil.copytree(FIXTURES / "catalog", tmp_path / "catalog")
    (tmp_path / "catalog" / "log.md").write_text(
        "# 작업 기록\n\n[돌아가기](product/시스템가/decision-log.md#D-002둘째-결정)\n", encoding="utf-8")
    ix = open_fixture("catalog")
    by_anchor = refs_to(ix, "product/시스템가/decision-log.md#d-002둘째-결정", None)
    assert ("log.md", 3) in [(r.src_file, r.src_line) for r in by_anchor[""]]
    assert ("log.md", 3) in [(r.src_file, r.src_line) for r in refs_to(ix, "D-002", None)["시스템가"]]
