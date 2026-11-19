import functools
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterator

from doclookup.config import Config
from doclookup.index import Index, bare
from doclookup.markdown import slug
from doclookup.model import Candidate, Hit, IdDef, Impact, Ref, Unit
from doclookup.paths import matches_glob
from doclookup.refs import UNKNOWN_SCOPE, bounded_id, canonical_anchor, unit_lines

_LIKE = "LIKE ? ESCAPE '\\'"


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def _like(text: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", text)


def _forms(words: list[str]) -> list[str]:
    out = []
    for word in words:
        out += [word, word.replace(" ", "")]
    return list(dict.fromkeys(w for w in out if w))


def expand(cfg: Config, term: str, exact: bool) -> list[str]:
    """낱말이 묶음의 대표나 다른 표기와 같으면(띄어쓰기 무시) 묶음 전체를 함께 찾는다."""
    term = _nfc(term)
    words = [term]
    if not exact:
        key = term.replace(" ", "")
        for group in cfg.aliases:
            members = [_nfc(group.canonical), *(_nfc(v) for v in group.variants)]
            if key in {m.replace(" ", "") for m in members}:
                words += members
    return _forms(words)


def _unit(row: sqlite3.Row) -> Unit:
    return Unit(**{key: row[key] for key in row.keys()})


def _select(ix: Index, forms: list[str], where: dict[str, str] | None) -> list[Unit]:
    """`forms`는 공백을 뺀 표기다. 공백을 뺀 본문(`bare`)에서 찾으므로 띄어쓰기만 다른 꼴도 함께 고른다."""
    parts, params = [], []
    for form in forms:
        if ix.trigram and len(form) >= 3:
            parts.append("rowid IN (SELECT rowid FROM bare_tri WHERE bare_tri MATCH ?)")
            params.append('"' + form.replace('"', '""') + '"')
        else:
            parts.append(f"rowid IN (SELECT rowid FROM bare WHERE text {_LIKE} OR title {_LIKE})")
            params += [f"%{_like(form)}%"] * 2
    sql = "SELECT * FROM units WHERE (" + " OR ".join(parts) + ")"
    for key, value in (where or {}).items():
        sql += (" AND file IN (SELECT file FROM frontmatter WHERE key=? AND "
                f"(value=? OR ', '||value||', ' {_LIKE}))")
        params += [key, _nfc(value), f"%, {_like(_nfc(value))}, %"]
    return [_unit(row) for row in ix.conn.execute(sql + " ORDER BY file, line", params)]


def line_text(ix: Index, uid: str | None, line: int) -> str:
    row = ix.conn.execute("SELECT * FROM units WHERE uid=?", (uid,)).fetchone() if uid else None
    if row is None:
        return ""
    return next((text for n, text in unit_lines(_unit(row)) if n == line), "")


def _fold(line: str) -> tuple[str, list[int]]:
    folded, owner = [], []
    for index, char in enumerate(line):
        piece = char.casefold()
        folded.append(piece)
        owner += [index] * len(piece)
    return "".join(folded), owner


@functools.lru_cache(maxsize=256)
def _loose(form: str) -> re.Pattern:
    """표기의 글자 사이에 공백이 몇 개 있어도(없어도) 맞는 정규식. casefold한 줄에 쓴다."""
    return re.compile(r"\s*".join(re.escape(c) for c in form.casefold() if not c.isspace()))


def _first(line: str, forms: list[str]) -> str | None:
    """줄에서 가장 앞에 나온 표기를 원래 표기 그대로 돌려준다. 같은 자리면 긴 것을 고른다."""
    folded, owner = _fold(line)
    best = None
    for form in forms:
        found = _loose(form).search(folded)
        if found and (best is None or found.start() < best.start()
                      or (found.start() == best.start() and found.end() > best.end())):
            best = found
    if best is None:
        return None
    if best.end() == best.start():
        return ""
    return line[owner[best.start()]:owner[best.end() - 1] + 1]


def _in(file: str, in_glob: str | None) -> bool:
    if not in_glob:
        return True
    prefix = _nfc(in_glob).strip("/")
    return matches_glob(file, [prefix]) or file.startswith(prefix + "/")


def search(ix: Index, terms: list[str], *, in_glob: str | None = None, where: dict[str, str] | None = None,
           exact: bool = False) -> list[Hit]:
    """단위를 색인으로 고른 뒤 단위 본문을 줄마다 훑어 출현 줄을 만든다. 한 줄에 표기가 여럿이면 가장 앞의 것을 적는다.
    띄어쓰기는 가리지 않는다: 붙여 쓴 낱말도 띄어 쓴 줄을, 띄어 쓴 낱말도 붙여 쓴 줄을 찾고 `matched`는 줄의 표기다."""
    forms = list(dict.fromkeys(b for t in terms if _nfc(t) for f in expand(ix.cfg, t, exact) if (b := bare(f))))
    if not forms:
        return []
    hits = []
    for unit in _select(ix, forms, where):
        if not _in(unit.file, in_glob):
            continue
        seen = set()
        for n, line in unit_lines(unit):
            if n not in seen and (matched := _first(line, forms)):
                seen.add(n)
                hits.append(Hit(unit.file, n, unit.uid, matched, line.strip(), unit.source))
    return hits


def ranked(cfg: Config, hits: list[Hit]) -> list[Hit]:
    """`search.priority`의 앞에 적은 범위부터, 어디에도 안 맞으면 확장 단위(원문) → 마크다운 → 텍스트, 같은 묶음은 파일·줄 순."""
    priority = cfg.search.priority

    def key(hit: Hit):
        group = next((i for i, scope in enumerate(priority) if _in(hit.file, scope)), len(priority))
        kind = 0 if hit.source.startswith("ext:") else 2 if hit.source == "text" else 1
        return group, kind, hit.file, hit.line

    return sorted(hits, key=key)


def section(ix: Index, doc: str, key: str) -> list[Unit]:
    """`doc`은 파일 경로 전체나 일부, `key`는 앵커 앞부분(`#` 생략 가능) 또는 제목 일부다."""
    doc, key = _nfc(doc), _nfc(key).lstrip("#")
    files = [r[0] for r in ix.conn.execute("SELECT DISTINCT file FROM units WHERE source != 'text' ORDER BY file")]
    exact = [f for f in files if f == doc.strip("/")]
    stem = [f for f in files if f.rsplit("/", 1)[-1].rsplit(".", 1)[0] == doc]
    chosen = exact or stem or [f for f in files if doc in f]
    if not chosen or not key:
        return []
    anchors = list(dict.fromkeys([key, slug(key, ix.cfg.anchor)]))
    sql = (f"SELECT * FROM units WHERE file IN ({','.join('?' * len(chosen))}) AND slug IS NOT NULL AND ("
           + " OR ".join(f"slug {_LIKE}" for _ in anchors) + f" OR title {_LIKE}) ORDER BY file, line")
    params = [*chosen, *(f"{_like(a)}%" for a in anchors), f"%{_like(key)}%"]
    return [_unit(row) for row in ix.conn.execute(sql, params)]


def _scoped(scope: str | None):
    return lambda value: scope is None or value in (scope, UNKNOWN_SCOPE)


def _group(items, key) -> dict:
    grouped: dict = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def find_id(ix: Index, ident: str, scope: str | None) -> dict[str, list[IdDef]]:
    keep = _scoped(scope)
    rows = ix.conn.execute("SELECT * FROM ids WHERE id=? ORDER BY file, line", (_nfc(ident),))
    return _group([IdDef(**dict(r)) for r in rows if keep(r["scope"])], lambda d: d.scope)


def _ref(row: sqlite3.Row) -> Ref:
    data = dict(row)
    data["broken"] = bool(data["broken"])
    return Ref(**data)


def is_id(cfg: Config, target: str) -> bool:
    return any(bounded_id(kind.pattern).fullmatch(target) for kind in cfg.ids)


def _id_refs(ix: Index, ident: str) -> list[Ref]:
    """ID 출현에 더해, 정의 절 앵커를 가리키는 링크도 그 정의의 범위로 넣는다. 같은 범위의 같은 줄은 한 번만 센다."""
    found, seen = [], set()
    for row in ix.conn.execute("SELECT * FROM refs WHERE kind='id' AND target=?", (ident,)):
        ref = _ref(row)
        if (ref.scope, ref.src_file, ref.src_line) not in seen:
            seen.add((ref.scope, ref.src_file, ref.src_line))
            found.append(ref)
    for d in ix.conn.execute("SELECT i.scope, i.file, u.slug FROM ids i JOIN units u ON u.uid=i.uid WHERE i.id=?",
                             (ident,)):
        if not d["slug"]:
            continue
        for row in ix.conn.execute("SELECT * FROM refs WHERE kind='link' AND target=?", (f"{d['file']}#{d['slug']}",)):
            ref = _ref(row)
            ref.scope = d["scope"]
            if (ref.scope, ref.src_file, ref.src_line) not in seen:
                seen.add((ref.scope, ref.src_file, ref.src_line))
                found.append(ref)
    return found


def _path_refs(ix: Index, target: str) -> list[Ref]:
    path, _, anchor = target.partition("#")
    path = path.lstrip("/")
    if anchor:
        sql, params = "SELECT * FROM refs WHERE target=?", [f"{path}#{canonical_anchor(anchor)}"]
    else:
        sql, params = f"SELECT * FROM refs WHERE target=? OR target {_LIKE}", [path, f"{_like(path)}#%"]
    return [_ref(r) for r in ix.conn.execute(sql, params)]


def refs_to(ix: Index, target: str, scope: str | None) -> dict[str, list[Ref]]:
    """`target`은 ID, 파일 경로, `파일#앵커`, 확장 uid다. 링크·인용은 범위가 없어 묶음 키가 ""다."""
    target = _nfc(target)
    if is_id(ix.cfg, target):
        found = _id_refs(ix, target)
    else:
        found = _path_refs(ix, target)
        if not found:
            found = [_ref(r) for r in ix.conn.execute("SELECT * FROM refs WHERE target=?", (target,))]
    keep = _scoped(scope)
    found = sorted((r for r in found if keep(r.scope) or r.scope == ""), key=lambda r: (r.src_file, r.src_line))
    return _group(found, lambda r: r.scope)


def broken_links(ix: Index) -> list[Ref]:
    return [_ref(r) for r in ix.conn.execute("SELECT * FROM refs WHERE broken=1 ORDER BY src_file, src_line")]


def _context(ix: Index, uid: str | None, line: int, fallback: str) -> str:
    return line_text(ix, uid, line).strip() or fallback


def _hit(ix: Index, ref: Ref) -> Hit:
    return Hit(ref.src_file, ref.src_line, ref.src_uid or "", ref.raw, _context(ix, ref.src_uid, ref.src_line, ref.raw))


def _def_hit(ix: Index, d: IdDef) -> Hit:
    return Hit(d.file, d.line, d.uid or "", d.id, _context(ix, d.uid, d.line, d.id))


def _is_path(ix: Index, target: str) -> bool:
    path = target.partition("#")[0].lstrip("/")
    if not path:
        return False
    known = ix.conn.execute("SELECT 1 FROM units WHERE file=? LIMIT 1", (path,)).fetchone()
    linked = ix.conn.execute(f"SELECT 1 FROM refs WHERE kind='link' AND (target=? OR target {_LIKE}) LIMIT 1",
                             (path, f"{_like(path)}#%")).fetchone()
    return bool(known or linked or (ix.root / path).is_file())


def _title_links(ix: Index, term: str) -> list[Ref]:
    """낱말이 절 제목과 같으면(대소문자 무시) 그 절 앵커를 가리키는 링크."""
    found = []
    for row in ix.conn.execute("SELECT file, slug, title FROM units WHERE slug IS NOT NULL"):
        if row["title"] and _nfc(row["title"]).casefold() == term.casefold():
            found += _path_refs(ix, f"{row['file']}#{row['slug']}")
    return found


def target_kind(ix: Index, target: str) -> str:
    """`impact`가 대상을 무엇으로 보는지: "id", "path", "cite"(확장 uid), "term"."""
    target = _nfc(target)
    if is_id(ix.cfg, target):
        return "id"
    if _is_path(ix, target):
        return "path"
    if ix.conn.execute("SELECT 1 FROM refs WHERE kind='cite' AND target=? LIMIT 1", (target,)).fetchone():
        return "cite"
    return "term"


def impact(ix: Index, target: str, scope: str | None) -> Impact:
    """바꾸면 같이 고칠 곳. ID는 정의·출현·정의 절 링크, 경로는 그것을 가리키는 링크, 확장 uid는 인용, 그 밖에는 낱말
    (표기 묶음 포함)과 그 낱말이 제목인 절을 가리키는 링크다. 같은 파일·줄은 한 번만 넣는다."""
    target = _nfc(target)
    kind = target_kind(ix, target)
    hits: list[Hit] = []
    if kind == "id":
        hits += [_def_hit(ix, d) for defs in find_id(ix, target, scope).values() for d in defs]
        hits += [_hit(ix, r) for refs in refs_to(ix, target, scope).values() for r in refs]
    elif kind == "path":
        hits += [_hit(ix, r) for r in _path_refs(ix, target)]
    elif kind == "cite":
        hits += [_hit(ix, _ref(r)) for r in ix.conn.execute("SELECT * FROM refs WHERE kind='cite' AND target=?", (target,))]
    else:
        hits += search(ix, [target])
        hits += [_hit(ix, r) for r in _title_links(ix, target)]
    sources = dict(ix.conn.execute("SELECT DISTINCT file, source FROM units").fetchall())
    result = Impact({}, {})
    seen = set()
    for hit in sorted(hits, key=lambda h: (h.file, h.line)):
        if (hit.file, hit.line) in seen:
            continue
        seen.add((hit.file, hit.line))
        bucket = result.text if sources.get(hit.file) == "text" else result.markdown
        bucket.setdefault(hit.file, []).append(hit)
    return result


def _occurrences(ix: Index, regex: re.Pattern, like: str) -> Counter:
    counts: Counter = Counter()
    for row in ix.conn.execute(f"SELECT * FROM units WHERE text {_LIKE} OR title {_LIKE}",
                               (f"%{_like(like)}%", f"%{_like(like)}%")):
        for _, line in unit_lines(_unit(row)):
            counts.update(m.group(0) for m in regex.finditer(line))
    return counts


def variants(ix: Index, term: str) -> list[Candidate]:
    """묶음에 없는 비슷한 표기 후보. 띄어쓰기만 다른 꼴, 접미사 짝, 같은 줄기(마지막 세 글자 앞을 줄기로, 끝 글자를 같게)를
    본다. 이미 묶음에 든 표기와 낱말 자신은 뺀다. 결과는 모두 후보다."""
    term = _nfc(term)
    bare = term.replace(" ", "")
    if not bare:
        return []
    excluded = set(expand(ix.cfg, term, False))
    found: dict[str, Candidate] = {}

    def add(counts: Counter, rule: str) -> None:
        for form, count in counts.items():
            if form not in excluded and form not in found:
                found[form] = Candidate(form, count, rule)

    add(_occurrences(ix, re.compile(" ?".join(re.escape(c) for c in bare)), bare[0]), "띄어쓰기")
    for a, b in ix.cfg.suffix_pairs:
        for old, new in ((a, b), (b, a)):
            if old and term.endswith(old) and len(term) > len(old):
                form = term[: -len(old)] + new
                add(_occurrences(ix, re.compile(re.escape(form)), form), "접미사 짝")
    if len(bare) >= 5:
        stem = bare[:-3]
        add(_occurrences(ix, re.compile(re.escape(stem) + r"[가-힣]{1,3}" + re.escape(bare[-1])), stem), "같은 줄기")
    return sorted(found.values(), key=lambda c: (-c.count, c.form))


def _spaced(name: str) -> str:
    return " ?".join(re.escape(c) for c in name.replace(" ", ""))


def roles(ix: Index) -> list[Hit]:
    """`roles.suspect`에 맞는데 `roles.names`와 묶음 표기(띄어쓰기 무시)에 없는 역할 이름의 출현. 전체 매치를 이름으로 보고,
    매치가 그 줄의 정의된 이름(예: 「운영 적용 승인 책임자」) 안에 들어 있으면 뺀다."""
    suspect = ix.cfg.roles.suspect
    if not suspect:
        return []
    regex = re.compile(suspect)
    names = [_nfc(n) for n in ix.cfg.roles.names]
    for group in ix.cfg.aliases:
        names += [_nfc(f) for f in [group.canonical, *group.variants]]
    defined = {n.replace(" ", "") for n in names if n}
    known = re.compile("|".join(sorted((_spaced(n) for n in defined), key=len, reverse=True))) if defined else None
    hits = []
    for row in ix.conn.execute("SELECT * FROM units ORDER BY file, line"):
        unit = _unit(row)
        for n, line in unit_lines(unit):
            spans = [m.span() for m in known.finditer(line)] if known else []
            for match in regex.finditer(line):
                name = match.group(0).strip()
                if not name or name.replace(" ", "") in defined:
                    continue
                if any(a <= match.start() and match.end() <= b for a, b in spans):
                    continue
                hits.append(Hit(unit.file, n, unit.uid, name, line.strip()))
    return hits
