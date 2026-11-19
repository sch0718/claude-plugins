from dataclasses import dataclass


@dataclass
class Unit:
    uid: str
    doc: str
    kind: str
    number: str | None
    title: str | None
    path: str
    parent: str | None
    file: str
    line: int
    text: str
    slug: str | None
    source: str
    text_line: int | None


@dataclass
class Link:
    file: str
    line: int
    text: str
    text_start: int
    text_end: int
    target: str
    anchor: str | None
    raw: str


@dataclass
class Ref:
    src_file: str
    src_line: int
    src_uid: str | None
    kind: str
    target: str
    scope: str
    raw: str
    broken: bool


@dataclass
class IdDef:
    name: str
    id: str
    scope: str
    file: str
    line: int
    uid: str | None


@dataclass
class Hit:
    file: str
    line: int
    uid: str
    matched: str
    context: str
    source: str = ""


@dataclass
class Impact:
    markdown: dict[str, list[Hit]]
    text: dict[str, list[Hit]]


@dataclass
class Candidate:
    form: str
    count: int
    rule: str
