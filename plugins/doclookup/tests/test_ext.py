import json
import sqlite3
import sys
from pathlib import Path

import pytest

from doclookup import ext
from doclookup.cli import main
from doclookup.config import Extension, load
from doclookup.index import ensure, open_index
from doclookup.query import impact, refs_to, search

FAKE = Path(__file__).parent / "fixtures" / "fake_ext.py"


@pytest.fixture
def fake_ext_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    outside = tmp_path / "main" / "20.원문"
    outside.mkdir(parents=True)
    (outside / "외부.md").write_text("# 외부\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_EXT_OUTSIDE", str(outside))
    monkeypatch.setenv("FAKE_EXT_LOG", str(tmp_path / "calls.log"))
    root = tmp_path / "wt"
    (root / "docs").mkdir(parents=True)
    (root / "20.원문").mkdir()
    (root / "20.원문" / "가짜규정.md").write_text("# 가짜규정\n\n## 제7조\n변경은 파트장이 승인한다.\n", encoding="utf-8")
    (root / "docs" / "지침.md").write_text(
        "# 지침\n\n가짜규정 제7조에 따른다.\n없는규정 제1조도 본다.\n[외부](/20.원문/외부.md) [규정](/20.원문/가짜규정.md)\n",
        encoding="utf-8")
    config = tmp_path / "fake.toml"
    command = json.dumps([sys.executable, str(FAKE)], ensure_ascii=False)
    config.write_text(f'match = ["wt"]\n[files]\nmarkdown = ["**/*.md"]\n\n[[extensions]]\nname = "fake"\n'
                      f'command = {command}\ncommands = ["find", "quotes"]\n', encoding="utf-8")
    return root.resolve(), config, tmp_path


def args(repo):
    root, config, _ = repo
    return ["--root", str(root), "--config", str(config)]


def calls(repo) -> list[str]:
    log = repo[2] / "calls.log"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


def opened(repo):
    root, config, tmp = repo
    cfg = load(config)
    out = tmp / "cache" / "index.sqlite"
    ensure(cfg, root, out)
    return open_index(cfg, root, out)


def test_call_returns_json_and_wraps_failures(fake_ext_repo, monkeypatch):
    root = fake_ext_repo[0]
    spec = Extension(name="fake", command=[sys.executable, str(FAKE)])
    assert ext.call(spec, "roots", [], root, 5)["roots"]
    with pytest.raises(ext.ExtError, match="fake bad-json"):
        ext.call(spec, "bad-json", [], root, 5)
    monkeypatch.setenv("FAKE_EXT_FAIL", "units")
    with pytest.raises(ext.ExtError, match="fake units"):
        ext.call(spec, "units", [], root, 5)
    with pytest.raises(ext.ExtError, match="없는명령"):
        ext.call(Extension(name="없는명령", command=["/없는/실행파일"]), "units", [], root, 5)


def test_ext_units_are_indexed_and_their_files_not_split(fake_ext_repo):
    ix = opened(fake_ext_repo)
    try:
        rows = ix.conn.execute("SELECT uid, source, kind FROM units WHERE file='20.원문/가짜규정.md' ORDER BY line").fetchall()
    finally:
        ix.conn.close()
    assert [tuple(r) for r in rows] == [("가짜규정#제7조", "ext:fake", "조"), ("가짜규정#제7조/1항", "ext:fake", "항")]


def test_roots_resolve_link_outside_checkout(fake_ext_repo):
    ix = opened(fake_ext_repo)
    try:
        broken = {r.target: r.broken for refs in refs_to(ix, "20.원문/외부.md", None).values() for r in refs}
        inside = [r.broken for refs in refs_to(ix, "20.원문/가짜규정.md", None).values() for r in refs]
    finally:
        ix.conn.close()
    assert broken == {"20.원문/외부.md": False} and inside == [False]


def test_cites_feed_impact_of_ext_uid(fake_ext_repo):
    ix = opened(fake_ext_repo)
    try:
        imp = impact(ix, "가짜규정#제7조", None)
        missing = ix.conn.execute("SELECT target, broken, src_uid FROM refs WHERE kind='cite' AND src_line=4").fetchone()
    finally:
        ix.conn.close()
    assert "docs/지침.md" in imp.markdown and [h.line for h in imp.markdown["docs/지침.md"]] == [3]
    assert tuple(missing) == ("대상 없음", 1, "docs/지침.md#지침")


def test_search_finds_ext_unit(fake_ext_repo, capsys):
    assert main([*args(fake_ext_repo), "search", "승인한다"]) == 0
    assert "20.원문/가짜규정.md:3  변경은 파트장이 승인한다." in capsys.readouterr().out


def test_search_shows_ext_units_before_markdown(fake_ext_repo, capsys):
    (fake_ext_repo[0] / "10.메모.md").write_text("# 메모\n파트장\n파트장\n파트장\n", encoding="utf-8")
    assert main([*args(fake_ext_repo), "search", "파트장"]) == 0
    shown = [line.split("  ")[0] for line in capsys.readouterr().out.splitlines()[1:]]
    assert shown == ["20.원문/가짜규정.md:3", "10.메모.md:2", "10.메모.md:3", "10.메모.md:4"]


def test_ext_command_passthrough_exit_code(fake_ext_repo, capfd):
    assert main([*args(fake_ext_repo), "find", "없음"]) == 1
    assert "찾지 못했다: 없음" in capfd.readouterr().out
    assert main(["find", "제7조", *args(fake_ext_repo)]) == 0
    assert "가짜규정 제7조" in capfd.readouterr().out
    assert calls(fake_ext_repo) == ["run", "run"]


def test_quotes_passes_args_and_exit_code(fake_ext_repo, capfd, monkeypatch):
    monkeypatch.delenv("FAKE_EXT_QUOTES_EXIT", raising=False)
    argv = [*args(fake_ext_repo), "quotes", "docs/지침.md", "--all", "--json"]
    assert main(argv) == 0
    assert capfd.readouterr().out == "quotes: docs/지침.md --all --json\n"
    assert main([*args(fake_ext_repo), "quotes"]) == 0
    assert capfd.readouterr().out == "quotes: \n"
    for code in (1, 2):
        monkeypatch.setenv("FAKE_EXT_QUOTES_EXIT", str(code))
        assert main(argv) == code
        assert capfd.readouterr().out == "quotes: docs/지침.md --all --json\n"
    assert calls(fake_ext_repo) == ["run"] * 4


def test_quotes_runs_in_repo_root(fake_ext_repo, capfd, monkeypatch):
    monkeypatch.delenv("FAKE_EXT_QUOTES_EXIT", raising=False)
    monkeypatch.setenv("FAKE_EXT_QUOTES_CWD", "1")
    monkeypatch.chdir(fake_ext_repo[2])
    assert main([*args(fake_ext_repo), "quotes", "docs/지침.md"]) == 0
    assert capfd.readouterr().out == f"quotes: docs/지침.md\ncwd: {fake_ext_repo[0].resolve()}\n"


def test_ext_timeout_is_exit_2(fake_ext_repo, monkeypatch, capsys):
    monkeypatch.setitem(ext.TIMEOUTS, "signature-files", 0.2)
    monkeypatch.setenv("FAKE_EXT_SLEEP", "1")
    assert main([*args(fake_ext_repo), "search", "x"]) == 2
    err = capsys.readouterr().err
    assert "fake" in err and "signature-files" in err and "시간" in err


def test_ext_failure_is_exit_2(fake_ext_repo, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_EXT_FAIL", "units")
    assert main([*args(fake_ext_repo), "search", "x"]) == 2
    assert "fake units" in capsys.readouterr().err


def test_plain_query_does_not_call_units_again(fake_ext_repo, capsys):
    main([*args(fake_ext_repo), "search", "승인한다"])
    main([*args(fake_ext_repo), "search", "승인한다"])
    log = calls(fake_ext_repo)
    assert log.count("units") == 1 and log.count("roots") == 1 and log.count("cites") == 1
    assert log.count("signature-files") >= 2


def test_signature_files_change_triggers_rebuild(fake_ext_repo, capsys):
    main([*args(fake_ext_repo), "search", "승인한다"])
    outside = fake_ext_repo[2] / "main" / "20.원문" / "외부.md"
    outside.write_text("# 외부\n바뀜\n", encoding="utf-8")
    main([*args(fake_ext_repo), "search", "승인한다"])
    assert calls(fake_ext_repo).count("units") == 2


def test_unknown_command_is_usage_error(fake_ext_repo):
    with pytest.raises(SystemExit) as exc:
        main([*args(fake_ext_repo), "없는명령"])
    assert exc.value.code == 2


def test_input_changed_during_collect_triggers_rebuild(fake_ext_repo):
    root, config, tmp = fake_ext_repo
    cfg = load(config)
    out = tmp / "cache" / "index.sqlite"
    watched = tmp / "main" / "20.원문" / "외부.md"

    class Provider:
        @staticmethod
        def signature_files(cfg, root):
            return [watched]

        @staticmethod
        def collect(cfg, root):
            watched.write_text("# 외부\n모으는 도중 바뀜\n", encoding="utf-8")
            return ext.ExtData(files=[watched])

    assert ensure(cfg, root, out, ext=Provider) is True
    assert ensure(cfg, root, out, ext=Provider) is True


def test_ext_title_not_in_text_is_searchable(fake_ext_repo, monkeypatch):
    monkeypatch.setenv("FAKE_EXT_TITLE", "조문승인기준")
    ix = opened(fake_ext_repo)
    try:
        hits = [(h.file, h.line, h.context) for h in search(ix, ["조문승인기준"])]
        both = [(h.file, h.line) for h in search(ix, ["조문승인기준", "승인한다"])]
    finally:
        ix.conn.close()
    assert hits == [("20.원문/가짜규정.md", 3, "조문승인기준")]
    assert both == [("20.원문/가짜규정.md", 3)]
