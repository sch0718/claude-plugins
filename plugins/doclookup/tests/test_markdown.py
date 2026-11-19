import pytest

from doclookup.markdown import frontmatter_values, links, sections, slug, split_frontmatter

FENCED = "# 개요\n본문\n```\n# 가짜\n```\n## 진짜\n내용\n"


def test_heading_inside_code_fence_is_not_a_section():
    secs = sections("a.md", FENCED, "gitlab")
    assert [u.title for u in secs] == ["개요", "진짜"]
    assert "# 가짜" in secs[0].text


def test_tilde_fence_needs_matching_marker_to_close():
    secs = sections("a.md", "~~~~\n```\n# 가짜\n~~~~\n# 진짜\n", "gitlab")
    assert [u.title for u in secs if u.kind == "절"] == ["진짜"]


@pytest.mark.parametrize("style", ["gitlab", "github"])
def test_duplicate_heading_slugs(style):
    secs = sections("a.md", "# 개요\n가\n# 개요\n나\n", style)
    assert [u.slug for u in secs] == ["개요", "개요-1"]
    assert [u.uid for u in secs] == ["a.md#개요", "a.md#개요-1"]


def test_github_suffix_skips_existing_slug():
    secs = sections("a.md", "# a\n# a\n# a-1\n", "github")
    assert [u.slug for u in secs] == ["a", "a-1", "a-1-1"]


def test_gitlab_suffix_may_repeat_and_uid_gets_tilde():
    secs = sections("a.md", "# a\n# a\n# a-1\n", "gitlab")
    assert [u.slug for u in secs] == ["a", "a-1", "a-1"]
    assert [u.uid for u in secs] == ["a.md#a", "a.md#a-1", "a.md#a-1~1"]


def test_slug_styles_differ_on_double_space():
    assert slug("A  B", "gitlab") == "a-b" and slug("A  B", "github") == "a--b"


def test_slug_styles_differ_on_spaced_hyphen():
    assert slug("가 - 나", "gitlab") == "가-나" and slug("가 - 나", "github") == "가---나"


def test_slug_keeps_hangul_and_drops_punctuation():
    assert slug("D-012/no-request-intake", "github") == "d-012no-request-intake"


@pytest.mark.parametrize("style", ["gitlab", "github"])
def test_slug_drops_emoji_and_parentheses(style):
    assert slug("🚀 배포 (운영)", style) == "-배포-운영"
    assert slug("제6조(변경 관리)", style) == "제6조변경-관리"


@pytest.mark.parametrize("style", ["gitlab", "github"])
def test_slug_uses_link_text_in_heading(style):
    assert slug("[지침](a.md) 개요", style) == "지침-개요"


def test_slug_rejects_unknown_style():
    with pytest.raises(ValueError):
        slug("a", "bitbucket")


def test_split_frontmatter_returns_block_and_body_start():
    block, start, body = split_frontmatter("---\ntype: Screen\n---\n# 제목\n")
    assert block == "type: Screen" and start == 4 and body == "# 제목"


@pytest.mark.parametrize("text", ["# 제목\n---\n", "---\ntype: Screen\n# 닫히지 않음\n", ""])
def test_split_frontmatter_without_block(text):
    assert split_frontmatter(text) == (None, 1, text)


def test_split_frontmatter_ignores_bom():
    block, start, _ = split_frontmatter("\ufeff---\na: b\n---\n")
    assert block == "a: b" and start == 4


def test_frontmatter_is_its_own_unit_and_columns_parse():
    fm = frontmatter_values('type: Screen\ntags: [a, b]\nsource_version: "2.18"\n', ["type", "tags", "source_version"])
    assert fm == {"type": "Screen", "tags": "a, b", "source_version": "2.18"}
    secs = sections("s.md", "---\ntype: Screen\ntags: [a, b]\n---\n# 화면\n본문\n", "github")
    assert secs[0].kind == "프론트매터" and secs[0].line == 1
    assert secs[0].uid == "s.md#프론트매터" and secs[0].text_line == 2
    assert secs[0].text == "type: Screen\ntags: [a, b]"
    assert (secs[1].title, secs[1].line, secs[1].text_line) == ("화면", 5, 6)


def test_frontmatter_values_only_top_level_and_requested_columns():
    block = "title: 'a: b'\nnested:\n  type: Inner\ntype: Outer\nempty:\nlist: [\"x, y\", z]\nother: 1\n"
    assert frontmatter_values(block, ["title", "type", "empty", "list"]) == {
        "title": "a: b", "type": "Outer", "list": "x, y, z"}


def test_preamble_is_its_own_kind():
    secs = sections("a.md", "\n머리 글\n# 제목\n", "gitlab")
    assert (secs[0].kind, secs[0].uid, secs[0].title, secs[0].text_line) == ("머리말", "a.md#머리말", "머리말", 2)
    assert secs[1].kind == "절"


def test_empty_preamble_is_skipped():
    assert [u.kind for u in sections("a.md", "\n\n# 제목\n", "gitlab")] == ["절"]


def test_section_fields_and_path():
    secs = sections("d/a.md", "# 상\n\n\n  첫 줄\n둘째\n## 하\n### 끝\n## 옆\n", "gitlab")
    top, low, end, side = secs
    assert (top.doc, top.file, top.source, top.number, top.parent) == ("d/a.md", "d/a.md", "markdown", None, None)
    assert (top.line, top.text_line, top.text) == (1, 4, "첫 줄\n둘째")
    assert low.path == "상 > 하" and end.path == "상 > 하 > 끝" and side.path == "상 > 옆"
    assert end.text_line is None and end.text == ""


def test_sections_handles_crlf_line_numbers():
    secs = sections("a.md", "# 가\r\n본문\r\n# 나\r\n", "gitlab")
    assert [(u.title, u.line, u.text_line) for u in secs] == [("가", 1, 2), ("나", 3, None)]


def test_links_kinds():
    ls = links("a.md", '[x](/p/b.md#c) [y](../d.md) [나1](#나1-절)\n[정의]: e.md\n`[z](f.md)`\n[w](https://x)\n')
    assert [(l.target, l.anchor) for l in ls] == [("/p/b.md", "c"), ("../d.md", None), ("", "나1-절"), ("e.md", None)]


def test_link_positions_and_raw():
    line = "앞 [D-001](/x/decision-log.md#d-001-결정) 뒤"
    (link,) = links("a.md", "첫 줄\n" + line + "\n")
    assert (link.file, link.line, link.text, link.raw) == ("a.md", 2, "D-001", "[D-001](/x/decision-log.md#d-001-결정)")
    assert line[link.text_start:link.text_end] == "D-001"


def test_definition_position():
    (link,) = links("a.md", "  [이름]: <b c.md#가> \"제목\"\n")
    assert (link.text, link.target, link.anchor) == ("이름", "b c.md", "가")
    assert "  [이름]: <b c.md#가> \"제목\""[link.text_start:link.text_end] == "이름"


def test_links_skip_code_fence_and_other_schemes():
    text = "```\n[a](a.md)\n```\n[m](mailto:x@y) [h](http://x) [f](ftp://x)\n[^1]: 각주\n"
    assert links("a.md", text) == []


def test_link_title_image_nested_text_and_encoded_anchor():
    text = '[a](a.md "제목") ![그림](img/b.png) [[c]](c(1).md) [d](d.md#%EA%B0%80)\n'
    assert [(l.text, l.target, l.anchor) for l in links("a.md", text)] == [
        ("a", "a.md", None), ("그림", "img/b.png", None), ("[c]", "c(1).md", None), ("d", "d.md", "가")]


def test_inline_code_with_double_backticks_is_skipped():
    assert [l.target for l in links("a.md", "``[a](a.md) ` `` [b](b.md)\n")] == ["b.md"]


def test_escaped_opening_bracket_is_not_a_link():
    assert links("a.md", "\\[example](not-a-link.md)\n") == []
    assert [l.target for l in links("a.md", "\\\\[real](b.md) 와 \\[fake](c.md)\n")] == ["b.md"]


def test_escaped_bracket_inside_destination_keeps_link():
    assert [l.target for l in links("a.md", "[a](dir\\[b/file.md)\n")] == ["dir\\[b/file.md"]
    assert [l.target for l in links("a.md", "[a](<dir\\[b/file.md>)\n")] == ["dir\\[b/file.md"]


@pytest.mark.parametrize("style", ["gitlab", "github"])
def test_slug_drops_link_destination_with_escaped_parenthesis(style):
    assert slug(r"[x](a\(b)", style) == "x"
    assert slug(r"[x](a\)b)", style) == "x"


@pytest.mark.parametrize(("line", "title"), [("# C#", "C#"), ("# C# #", "C#"), ("# a ##", "a"), ("# a #b", "a #b"), ("# #", "")])
def test_closing_hashes_need_leading_whitespace(line, title):
    assert sections("a.md", line + "\n", "gitlab")[0].title == title


def test_inline_code_span_across_lines_is_skipped():
    text = "x ``\n[a](a.md)\n`` [b](b.md)\n\n`[c](c.md)\n\n[d](d.md)`\n"
    assert [(link.line, link.target) for link in links("f.md", text)] == [(3, "b.md"), (5, "c.md"), (7, "d.md")]


def test_escaped_bracket_inside_label_keeps_link():
    ls = links("a.md", "[a \\[b](c.md) [e \\] f](g.md)\n")
    assert [(l.text, l.target) for l in ls] == [("a \\[b", "c.md"), ("e \\] f", "g.md")]
    assert [l.target for l in links("a.md", "\\[a [b](c.md)\n")] == ["c.md"]


@pytest.mark.parametrize("text", [
    "# 제목 `\n[a](a.md)\n# 다음 `\n",
    "- 항목 `\n- [a](a.md)\n- 항목 `\n",
    "| `가 |\n| [a](a.md) |\n| 나` |\n",
    "문단 `\n***\n[a](a.md) `\n",
])
def test_inline_code_span_stops_at_block_boundaries(text):
    assert [l.target for l in links("f.md", text)] == ["a.md"]
