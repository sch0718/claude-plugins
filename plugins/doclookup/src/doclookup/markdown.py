import re
from collections.abc import Iterator
from urllib.parse import unquote

from doclookup.model import Link, Unit

STYLES = ("gitlab", "github")

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:(?<=\s)#+|\s+#+)?\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})")
_FRONTMATTER_END = re.compile(r"^(?:---|\.\.\.)\s*$")
_KEY_VALUE = re.compile(r"^([^\s#:-][^:]*?):(?:[ \t]+(.*))?$")
_LIST_ITEM = re.compile(r"\"[^\"]*\"|'[^']*'|[^,]+")
_INLINE_CODE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)", re.S)
_LABEL = r"\[(?P<text>(?:\\.|[^\[\]\\\n]|\[(?:\\.|[^\[\]\\\n])*\])*)\]"
_TITLE = r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?"
_INLINE = re.compile(_LABEL + r"\(\s*(?P<dest><[^<>\n]*>|(?:[^()\s]|\([^()\s]*\))+)" + _TITLE + r"\s*\)")
_DEFINITION = re.compile(r"^ {0,3}" + _LABEL + r":[ \t]*(?P<dest><[^<>\n]*>|\S+)" + _TITLE + r"[ \t]*$")
_ESCAPED_BRACKET = re.compile(r"(?<!\\)(?:\\\\)*\\\[")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_SINGLE_LINE_BLOCK = re.compile(r"^ {0,3}(?:#{1,6}(?:\s|$)|([-*_])(?:[ \t]*\1){2,}[ \t]*$|\|)")
_LIST_START = re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])(?:\s|$)")


def slug(title: str, style: str) -> str:
    """GitHub은 GitLab과 같은 처리에서 연속 하이픈만 합치지 않는다."""
    if style not in STYLES:
        raise ValueError(f"앵커 규칙은 gitlab 또는 github이어야 한다: {style}")
    text = re.sub(r"\[([^\]]*)\]\((?:\\.|[^()\\]|\([^()]*\))*\)", r"\1", title)
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))
    return re.sub(r"-+", "-", text) if style == "gitlab" else text


def split_frontmatter(text: str) -> tuple[str | None, int, str]:
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None, 1, text
    for n, line in enumerate(lines[1:], 1):
        if _FRONTMATTER_END.match(line):
            return "\n".join(lines[1:n]), n + 2, "\n".join(lines[n + 1:])
    return None, 1, text


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def frontmatter_values(block: str, columns: list[str]) -> dict[str, str]:
    values = {}
    for line in block.splitlines():
        match = _KEY_VALUE.match(line)
        if not match or match.group(1) not in columns or not match.group(2):
            continue
        raw = match.group(2).strip()
        if raw.startswith("[") and raw.endswith("]"):
            items = (_unquote(item.strip()) for item in _LIST_ITEM.findall(raw[1:-1]))
            value = ", ".join(item for item in items if item)
        else:
            value = _unquote(raw)
        if value:
            values[match.group(1)] = value
    return values


def _scan(lines: list[str], start: int) -> Iterator[tuple[int, str, bool]]:
    """줄마다 (줄 번호, 줄, 코드 펜스 안인지)를 낸다. 펜스 표시 줄도 펜스 안으로 본다."""
    fence = None
    for n, line in enumerate(lines, start):
        if marker := _FENCE.match(line):
            mark = marker.group(1)
            if not fence:
                fence = mark
                yield n, line, True
                continue
            if mark[0] == fence[0] and len(mark) >= len(fence) and line.strip() == mark:
                fence = None
                yield n, line, True
                continue
        yield n, line, fence is not None


def sections(file: str, text: str, style: str) -> list[Unit]:
    """GitLab은 만든 접미사(-1)가 다른 제목과 겹쳐도 그대로 두므로 앵커는 규칙대로, uid만 ~n을 붙여 가른다."""
    if style not in STYLES:
        raise ValueError(f"앵커 규칙은 gitlab 또는 github이어야 한다: {style}")
    block, start, body = split_frontmatter(text.removeprefix("\ufeff"))
    units, used, uids, stack = [], {}, set(), []

    def add(kind: str, title: str, anchor: str | None, line: int, path: str, lines: list[str], first_line: int) -> None:
        first = next((i for i, raw in enumerate(lines) if raw.strip()), None)
        if anchor is None and first is None:
            return
        uid = base = f"{file}#{kind if anchor is None else anchor}"
        suffix = 0
        while uid in uids:
            suffix += 1
            uid = f"{base}~{suffix}"
        uids.add(uid)
        text_line = first_line + first if first is not None else None
        units.append(Unit(uid, file, kind, None, title, path, None, file, line, "\n".join(lines).strip(), anchor,
                          "markdown", text_line))

    if block is not None:
        add("프론트매터", "프론트매터", None, 1, "프론트매터", block.splitlines(), 2)
    current = {"kind": "머리말", "title": "머리말", "slug": None, "line": start, "path": "머리말", "body": [],
               "start": start}
    for n, line, code in _scan(body.splitlines(), start):
        heading = None if code else _HEADING.match(line)
        if not heading:
            current["body"].append(line)
            continue
        add(current["kind"], current["title"], current["slug"], current["line"], current["path"], current["body"],
            current["start"])
        level, title = len(heading.group(1)), heading.group(2)
        anchor = base = slug(title, style)
        if style == "github":
            while anchor in used:
                used[base] += 1
                anchor = f"{base}-{used[base]}"
            used[anchor] = 0
        elif anchor in used:
            used[anchor] += 1
            anchor = f"{anchor}-{used[anchor]}"
        else:
            used[anchor] = 0
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        current = {"kind": "절", "title": title, "slug": anchor, "line": n,
                   "path": " > ".join(t for _, t in stack), "body": [], "start": n + 1}
    add(current["kind"], current["title"], current["slug"], current["line"], current["path"], current["body"],
        current["start"])
    return units


def _link(file: str, n: int, line: str, match: re.Match) -> Link | None:
    dest = line[match.start("dest"):match.end("dest")]
    if dest.startswith("<"):
        dest = dest[1:-1]
    if _SCHEME.match(dest):
        return None
    target, _, anchor = dest.partition("#")
    start, end = match.span("text")
    return Link(file, n, line[start:end], start, end, unquote(target), unquote(anchor) or None,
                line[match.start():match.end()])


def _paragraphs(text: str) -> Iterator[list[tuple[int, str]]]:
    """코드 스팬은 줄을 넘을 수 있으나 블록 경계는 넘지 못한다. 빈 줄·코드 펜스에서 끊고, 제목·구분선·표 줄은 한 줄로,
    목록 항목은 새 묶음으로 시작한다."""
    group = []
    for n, line, code in _scan(text.splitlines(), 1):
        single = _SINGLE_LINE_BLOCK.match(line)
        if code or not line.strip() or single or _LIST_START.match(line):
            if group:
                yield group
            group = []
            if code or not line.strip():
                continue
            if single:
                yield [(n, line)]
                continue
        group.append((n, line))
    if group:
        yield group


def links(file: str, text: str) -> list[Link]:
    """줄 안의 열을 지키려고 인라인 코드를 같은 길이의 공백으로 가린 줄에서 찾고, 글자는 원래 줄에서 잘라 낸다."""
    found = []
    for group in _paragraphs(text):
        joined = _INLINE_CODE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), "\n".join(line for _, line in group))
        for (n, line), masked in zip(group, joined.split("\n")):
            definition = _DEFINITION.match(masked)
            if definition and not definition.group("text").startswith("^"):
                matches = [definition]
            elif definition:
                continue
            else:
                escaped = {m.end() - 1 for m in _ESCAPED_BRACKET.finditer(masked)}
                matches = [m for m in _INLINE.finditer(masked) if m.start() not in escaped]
            found.extend(link for m in matches if (link := _link(file, n, line, m)))
    return found
