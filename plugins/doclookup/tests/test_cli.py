import shutil
from pathlib import Path

import pytest

from doclookup import index
from doclookup.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "docs"
    shutil.copytree(FIXTURES / "docs", root)
    return ["--root", str(root), "--config", str(FIXTURES / "docs.toml")]


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "catalog"
    shutil.copytree(FIXTURES / "catalog", root)
    return ["--root", str(root), "--config", str(FIXTURES / "catalog.toml")]


def make(tmp_path, monkeypatch, files: dict[str, str], extra: str = "") -> list[str]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "r"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    config = tmp_path / "r.toml"
    config.write_text('match = ["r"]\n[files]\nmarkdown = ["**/*.md"]\ntext = ["**/*.py"]\n' + extra, encoding="utf-8")
    return ["--root", str(root), "--config", str(config)]


def where(out: str) -> list[str]:
    return [line.split("  ")[0] for line in out.splitlines()[1:] if not line.startswith("…")]


def run(capsys, argv):
    code = main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_exit_codes(repo, capsys, tmp_path):
    assert run(capsys, [*repo, "search", "없는낱말"])[0] == 1
    assert run(capsys, [*repo, "search", "겸직"])[0] == 0
    code, _, err = run(capsys, ["--config", str(tmp_path / "없음.toml"), "search", "x"])
    assert code == 2 and "설정 파일을 읽을 수 없다" in err


def test_cli_missing_config_for_checkout_is_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOCLOOKUP_CONFIG_DIR", str(tmp_path))
    (tmp_path / "아무거나").mkdir()
    code, _, err = run(capsys, ["--root", str(tmp_path / "아무거나"), "search", "x"])
    assert code == 2 and "설정이 없다" in err


def test_cli_search_output(repo, capsys):
    code, out, _ = run(capsys, [*repo, "search", "파트장"])
    assert code == 0
    first, *rest = out.splitlines()
    assert first == "3건: docs/지침.md 2, tools/make_summary.py 1"
    assert "docs/지침.md:10  변경책임자가 없으면 파트장이 맡는다." in rest
    assert "docs/지침.md:11  파트 리더는 겸직할 수 없다. 〔표기: 파트 리더〕" in rest


ORDER_FILES = {"a/도구.py": "x = '승인'\n", "b/적음.md": "# 적음\n승인\n", "c/많음.md": "# 많음\n승인\n승인\n승인\n"}


def test_cli_search_orders_markdown_before_text_then_file_and_line(tmp_path, monkeypatch, capsys):
    _, out, _ = run(capsys, [*make(tmp_path, monkeypatch, ORDER_FILES), "search", "승인"])
    assert out.splitlines()[0] == "5건: c/많음.md 3, a/도구.py 1, b/적음.md 1"
    assert where(out) == ["b/적음.md:2", "c/많음.md:2", "c/많음.md:3", "c/많음.md:4", "a/도구.py:1"]


def test_cli_search_priority_globs_come_first_in_written_order(tmp_path, monkeypatch, capsys):
    argv = make(tmp_path, monkeypatch, ORDER_FILES, '[search]\npriority = ["c/**", "a/*.py"]\n')
    _, out, _ = run(capsys, [*argv, "search", "승인"])
    assert where(out) == ["c/많음.md:2", "c/많음.md:3", "c/많음.md:4", "a/도구.py:1", "b/적음.md:2"]


SEVEN = {f"d{i}.md": "# 제목\n" + "승인\n" * (8 - i) for i in range(1, 8)}


def test_cli_search_header_shows_top_five_files(tmp_path, monkeypatch, capsys):
    argv = make(tmp_path, monkeypatch, SEVEN)
    _, out, _ = run(capsys, [*argv, "search", "승인", "--limit", "1"])
    assert out.splitlines()[0] == "28건: d1.md 7, d2.md 6, d3.md 5, d4.md 4, d5.md 3 외 2개 파일 3건"
    _, out, _ = run(capsys, [*argv, "search", "승인", "--limit", "1", "--files"])
    assert out.splitlines()[0] == "28건: d1.md 7, d2.md 6, d3.md 5, d4.md 4, d5.md 3, d6.md 2, d7.md 1"


def test_cli_roles_header_shows_top_five_names(tmp_path, monkeypatch, capsys):
    names = ["가책임자", "나책임자", "다책임자", "라책임자", "마책임자", "바책임자"]
    body = "# 역할\n" + "".join(f"{n}\n" * (7 - i) for i, n in enumerate(names))
    argv = make(tmp_path, monkeypatch, {"역할.md": body}, "[roles]\nsuspect = '[가-힣]책임자'\n")
    _, out, _ = run(capsys, [*argv, "roles", "--limit", "1"])
    assert out.splitlines()[0] == "27건: 가책임자 7, 나책임자 6, 다책임자 5, 라책임자 4, 마책임자 3 외 1개 이름 2건"
    _, out, _ = run(capsys, [*argv, "roles", "--limit", "1", "--names"])
    assert out.splitlines()[0].endswith("마책임자 3, 바책임자 2")


def test_cli_search_snippet_finds_match_with_tab_or_spaces(tmp_path, monkeypatch, capsys):
    argv = make(tmp_path, monkeypatch, {"긴.md": "# 긴\n" + "가" * 100 + " 정당성\t검증 " + "나" * 100 + "\n"})
    _, out, _ = run(capsys, [*argv, "search", "정당성검증", "--width", "5"])
    assert out.splitlines()[1] == "긴.md:2  …가가가가 정당성 검증 나나나나… 〔표기: 정당성\t검증〕"


def test_cli_search_limit_and_width(repo, capsys):
    code, out, _ = run(capsys, [*repo, "search", "파트장", "--limit", "1", "--width", "2"])
    assert out.splitlines()[1:] == ["docs/지침.md:10  …면 파트장이 …", "… 2건 더 (--limit)"]


def test_cli_search_type_and_in(repo, capsys):
    assert run(capsys, [*repo, "search", "확정", "--type", "Screen"])[1].splitlines()[0] == "1건: docs/화면.md 1"
    assert run(capsys, [*repo, "search", "파트장", "--in", "tools"])[1].splitlines()[0] == "1건: tools/make_summary.py 1"
    assert run(capsys, [*repo, "search", "겸직", "--any", "a_b"])[1].splitlines()[0] == "2건: docs/지침.md 2"


@pytest.mark.parametrize("terms, any_terms", [
    (["겸직", "a_b"], "a_b"),
    (["겸직", "a_b", "확정"], "a_b,확정"),
    (["겸직", "a_b", "--any", "확정"], "a_b,확정"),
])
def test_cli_search_multiple_terms_match_any(repo, capsys, terms, any_terms):
    expected = run(capsys, [*repo, "search", "겸직", "--any", any_terms])
    assert expected[0] == 0
    assert run(capsys, [*repo, "search", *terms]) == expected


def test_cli_search_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["search", "-h"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "낱말 [낱말 ...]" in out
    assert "여러 낱말" in out and "어느 하나라도" in out and "--any" in out
    assert "따옴표" in out and "낱말은 옵션 앞에" in out


def test_cli_search_multiple_terms_before_filter(repo, capsys):
    expected = run(capsys, [*repo, "search", "겸직", "--any", "a_b", "--in", "docs"])
    assert expected[0] == 0
    assert run(capsys, [*repo, "search", "겸직", "a_b", "--in", "docs"]) == expected


def test_cli_search_rejects_terms_after_filter(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        main([*repo, "search", "겸직", "--in", "docs", "a_b"])
    assert exc.value.code == 2
    assert "unrecognized arguments: a_b" in capsys.readouterr().err


def test_cli_trigram_notice(repo, capsys, monkeypatch):
    monkeypatch.setattr(index, "trigram_available", lambda: False)
    code, out, _ = run(capsys, [*repo, "search", "변경 책임자"])
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("알림: SQLite ") and lines[0].endswith("에 trigram이 없어 LIKE로 찾는다")
    assert sum(line.startswith("알림:") for line in lines) == 1
    assert lines[1].startswith("3건:")


def test_cli_reports_skipped_after_rebuild(repo, capsys, tmp_path):
    (tmp_path / "docs" / "docs" / "옛.md").write_bytes("# 옛\n겸직\n".encode("cp949"))
    _, _, err = run(capsys, [*repo, "search", "겸직"])
    assert "건너뜀: docs/옛.md: UTF-8이 아니다" in err
    _, _, err = run(capsys, [*repo, "search", "겸직"])
    assert "건너뜀" not in err


def test_cli_section(repo, capsys):
    code, out, _ = run(capsys, [*repo, "section", "지침", "특수"])
    assert code == 0
    assert out.splitlines()[:3] == ["[docs/지침.md#특수-문자] docs/지침.md:13  #특수-문자", "지침 > 특수 문자", '"따옴표 안 글']
    assert run(capsys, [*repo, "section", "지침", "없는절"])[0] == 1


def test_cli_id(catalog, capsys):
    code, out, _ = run(capsys, [*catalog, "id", "D-001"])
    assert code == 0
    assert out.splitlines() == [
        "[시스템가]",
        "product/시스템가/decision-log.md:3  D-001/첫-결정",
        "[시스템나]",
        "product/시스템나/decision-log.md:3  D-001/나-첫-결정",
    ]
    assert run(capsys, [*catalog, "id", "D-001", "--scope", "시스템나"])[1].splitlines()[0] == "[시스템나]"
    assert run(capsys, [*catalog, "id", "D-999"])[0] == 1


def test_cli_refs(catalog, capsys):
    code, out, _ = run(capsys, [*catalog, "refs", "D-001"])
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("4건")
    assert "[범위 불명]" in lines and "log.md:3  - D-001 정리" in lines
    assert lines.index("[시스템가]") < lines.index("[범위 불명]")


def test_cli_links(catalog, capsys):
    code, out, _ = run(capsys, [*catalog, "links"])
    assert code == 0
    assert out.splitlines() == [
        "2건",
        "product/시스템가/D-001-배경.md:4  [없는 절](decision-log.md#없는-절) → product/시스템가/decision-log.md#없는-절",
        "product/시스템가/D-001-배경.md:5  [없는 파일](../없음.md) → product/없음.md",
    ]


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0 and capsys.readouterr().out.startswith("doclookup ")
