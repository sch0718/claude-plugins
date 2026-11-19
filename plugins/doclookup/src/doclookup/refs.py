import bisect
import functools
import posixpath
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from doclookup.config import Config, IdKind
from doclookup.markdown import frontmatter_values
from doclookup.model import IdDef, Link, Ref, Unit
from doclookup.paths import matches_glob

UNKNOWN_SCOPE = "범위 불명"

_ID_EDGE = "A-Za-z0-9_-"


@functools.lru_cache(maxsize=256)
def bounded_id(pattern: str) -> re.Pattern:
    """ID 앞뒤에 영문·숫자·`_`·`-`가 붙으면 다른 ID의 일부로 본다. 한글 조사는 붙어도 된다."""
    flags = re.match(r"(?:\(\?[aiLmsux]+\))+", pattern)
    head, body = (flags.group(0), pattern[flags.end():]) if flags else ("", pattern)
    return re.compile(rf"{head}(?<![{_ID_EDGE}])(?:{body})(?![{_ID_EDGE}])")


def link_path(src_file: str, link: Link) -> str:
    """링크 대상의 저장소 상대 경로. `/`로 시작하면 저장소 루트 기준, 아니면 원본 파일 폴더 기준이다."""
    if not link.target:
        return src_file
    if link.target.startswith("/"):
        joined = link.target.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(src_file), link.target)
    return unicodedata.normalize("NFC", posixpath.normpath(joined))


def canonical_anchor(anchor: str) -> str:
    return unicodedata.normalize("NFC", anchor).lower()


def located_path(rel: str, root: Path, roots: dict[str, Path]) -> Path:
    """저장소 상대 경로의 실제 위치. `roots` 접두어가 맞으면 그 폴더 기준이다."""
    for prefix, folder in roots.items():
        if rel.startswith(prefix):
            return Path(folder) / rel[len(prefix):]
    return root / rel


def _exists(rel: str, root: Path, roots: dict[str, Path], file_set: set[str]) -> bool:
    return rel in file_set or located_path(rel, root, roots).exists()


def resolve_link(src_file: str, link: Link, root: Path, roots: dict[str, Path], file_set: set[str],
                 slugs: dict[str, set[str]]) -> tuple[str, bool]:
    """앵커는 색인한 마크다운 파일(`slugs`에 있는 파일)에서만 검사한다. 그 밖의 파일은 앵커를 확인할 수 없어 깨짐으로 보지 않는다."""
    rel = link_path(src_file, link)
    if not _exists(rel, root, roots, file_set):
        return rel, True
    if link.anchor and rel in slugs:
        known = slugs[rel]
        anchor = unicodedata.normalize("NFC", link.anchor)
        if anchor not in known and anchor.lower() not in {s.lower() for s in known}:
            return rel, True
    return rel, False


def _scope(kind: IdKind, *paths: str) -> str:
    if not kind.scope:
        return ""
    for path in paths:
        if path and (match := re.search(kind.scope, path)):
            return match.group(1) if match.groups() else match.group(0)
    return UNKNOWN_SCOPE


def _body_lines(unit: Unit) -> Iterator[tuple[int, str]]:
    start = unit.text_line or unit.line
    for offset, line in enumerate((unit.text or "").splitlines()):
        yield start + offset, line


def unit_lines(unit: Unit) -> Iterator[tuple[int, str]]:
    """단위의 (파일 줄 번호, 줄)을 낸다. 마크다운 절은 제목 줄을 먼저 낸다. 확장 단위는 제목이 본문에 없을 때만
    제목을 단위 첫 줄(`line`) 번호로 낸다. 조문처럼 제목과 본문 첫 줄이 같은 줄에 있는 꼴이라 번호가 겹칠 수 있다."""
    if unit.title and unit.source == "markdown" and unit.kind == "절":
        yield unit.line, unit.title
    elif unit.title and unit.source.startswith("ext:") and unit.title not in (unit.text or ""):
        yield unit.line, unit.title
    yield from _body_lines(unit)


def _heading_ids(kind: IdKind, regex: re.Pattern, unit: Unit) -> Iterator[tuple[str, int]]:
    if unit.kind == "절" and unit.title and (match := regex.match(unit.title.lstrip("`*_ "))):
        yield match.group(0), unit.line


def _table_ids(kind: IdKind, regex: re.Pattern, unit: Unit) -> Iterator[tuple[str, int]]:
    cell = re.compile(rf"^\s*\|\s*(`?)(?P<id>{regex.pattern})\1\s*\|")
    for n, line in _body_lines(unit):
        if match := cell.match(line):
            yield match.group("id"), n


def _frontmatter_ids(kind: IdKind, regex: re.Pattern, unit: Unit) -> Iterator[tuple[str, int]]:
    if unit.kind != "프론트매터":
        return
    name = kind.defined_by.partition(":")[2]
    for n, line in _body_lines(unit):
        value = frontmatter_values(line, [name]).get(name)
        if value:
            for match in regex.finditer(value):
                yield match.group(0), n


_DEFINERS = {"heading": _heading_ids, "table_first_cell": _table_ids}


def id_definitions(cfg: Config, units: list[Unit]) -> list[IdDef]:
    found = []
    for kind in cfg.ids:
        regex = bounded_id(kind.pattern)
        definer = _DEFINERS.get(kind.defined_by, _frontmatter_ids)
        for unit in units:
            if kind.in_ and not matches_glob(unit.file, kind.in_):
                continue
            for ident, line in definer(kind, regex, unit):
                found.append(IdDef(kind.name, ident, _scope(kind, unit.file), unit.file, line, unit.uid))
    return found


def _masked(line: str, line_links: list[Link]) -> tuple[str, list[tuple[int, int, Link]]]:
    """링크 대상 부분을 공백으로 가리고 링크 글자 범위를 돌려준다. 단위 본문은 첫 줄 앞 공백이 빠져 있어 열 대신 원문 표기를 찾는다."""
    chars, spans, pos = list(line), [], 0
    for link in line_links:
        raw = link.raw.lstrip()
        start = line.find(raw, pos)
        if start < 0:
            continue
        text_start = start + 1
        text_end = text_start + len(link.text)
        for i in range(text_end + 1, start + len(raw)):
            chars[i] = " "
        spans.append((text_start, text_end, link))
        pos = start + len(raw)
    return "".join(chars), spans


def id_mentions(cfg: Config, units: list[Unit], links: list[Link]) -> list[Ref]:
    by_line: dict[tuple[str, int], list[Link]] = {}
    for link in links:
        by_line.setdefault((link.file, link.line), []).append(link)
    kinds = [(kind, bounded_id(kind.pattern)) for kind in cfg.ids]
    found = []
    for unit in units:
        for n, line in unit_lines(unit):
            masked, spans = _masked(line, by_line.get((unit.file, n), []))
            for kind, regex in kinds:
                for match in regex.finditer(masked):
                    inside = next((link for a, b, link in spans if a <= match.start() and match.end() <= b), None)
                    target_path = link_path(unit.file, inside) if inside else None
                    scope = _scope(kind, target_path, unit.file)
                    found.append(Ref(unit.file, n, unit.uid, "id", match.group(0), scope, match.group(0), False))
    return found


def _locator(units: list[Unit]):
    starts: dict[str, list[tuple[int, str]]] = {}
    for unit in units:
        starts.setdefault(unit.file, []).append((unit.line, unit.uid))
    for rows in starts.values():
        rows.sort()

    def uid_at(file: str, line: int) -> str | None:
        rows = starts.get(file, [])
        i = bisect.bisect_right(rows, (line, "\U0010ffff")) - 1
        return rows[i][1] if i >= 0 else None

    return uid_at


def extract(cfg: Config, root: Path, units: list[Unit], links: list[Link], roots: dict[str, Path],
            cites: list[Ref] = ()) -> tuple[list[Ref], list[IdDef]]:
    """ID가 정의된 줄의 그 ID는 출현으로 세지 않는다. 확장이 준 인용(`cites`)은 들어 있는 단위 uid를 채워 넣는다."""
    file_set = {unit.file for unit in units}
    slugs: dict[str, set[str]] = {}
    for unit in units:
        if unit.source == "markdown":
            slugs.setdefault(unit.file, set())
            if unit.slug:
                slugs[unit.file].add(unit.slug)
    uid_at = _locator(units)
    refs = []
    for link in links:
        rel, broken = resolve_link(link.file, link, root, roots, file_set, slugs)
        target = rel + (f"#{canonical_anchor(link.anchor)}" if link.anchor else "")
        refs.append(Ref(link.file, link.line, uid_at(link.file, link.line), "link", target, "", link.raw, broken))
    refs += [replace(c, src_uid=uid_at(c.src_file, c.src_line)) for c in cites]
    defs = id_definitions(cfg, units)
    defined = {(d.file, d.line, d.id) for d in defs}
    refs += [r for r in id_mentions(cfg, units, links) if (r.src_file, r.src_line, r.target) not in defined]
    return refs, defs
