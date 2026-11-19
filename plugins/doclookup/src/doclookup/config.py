import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from doclookup.paths import repo_keys

ANCHOR_RULES = ("gitlab", "github")
DEFAULT_SUFFIX_PAIRS = [("장", " 리더"), ("장", "리더")]

_TOP_KEYS = {"match", "anchor", "files", "frontmatter", "ids", "aliases", "roles", "variants", "hint", "extensions", "search"}
_MISSING = object()


class ConfigError(Exception):
    pass


@dataclass
class IdKind:
    name: str
    pattern: str
    defined_by: str
    scope: str | None = None
    in_: list[str] = field(default_factory=list)


@dataclass
class AliasGroup:
    canonical: str
    variants: list[str]


@dataclass
class Roles:
    names: list[str] = field(default_factory=list)
    suspect: str | None = None


@dataclass
class Extension:
    name: str
    command: list[str]
    commands: list[str] = field(default_factory=list)


@dataclass
class Hint:
    enabled: bool = False
    min_chars: int = 20000


@dataclass
class Search:
    priority: list[str] = field(default_factory=list)


@dataclass
class Config:
    path: Path
    match: list[str]
    anchor: str = "gitlab"
    markdown: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    frontmatter_columns: list[str] = field(default_factory=list)
    ids: list[IdKind] = field(default_factory=list)
    aliases: list[AliasGroup] = field(default_factory=list)
    roles: Roles = field(default_factory=Roles)
    extensions: list[Extension] = field(default_factory=list)
    suffix_pairs: list[tuple[str, str]] = field(default_factory=lambda: list(DEFAULT_SUFFIX_PAIRS))
    hint: Hint = field(default_factory=Hint)
    search: Search = field(default_factory=Search)


class _Reader:
    def __init__(self, path: Path):
        self.path = path

    def fail(self, where: str, problem: str) -> ConfigError:
        return ConfigError(f"{self.path}: {where}: {problem}")

    def table(self, value: Any, where: str, allowed: set[str]) -> dict:
        if not isinstance(value, dict):
            raise self.fail(where, "표여야 한다")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise self.fail(where, f"알 수 없는 키 {', '.join(unknown)} (쓸 수 있는 키: {', '.join(sorted(allowed))})")
        return value

    def tables(self, value: Any, where: str, allowed: set[str]) -> list[dict]:
        if not isinstance(value, list):
            raise self.fail(where, f"[[{where}]] 표 배열이어야 한다")
        return [self.table(item, f"{where}[{i}]", allowed) for i, item in enumerate(value)]

    def string(self, table: dict, key: str, where: str, default: Any = _MISSING) -> Any:
        if key not in table:
            if default is _MISSING:
                raise self.fail(f"{where}.{key}", "값이 없다")
            return default
        value = table[key]
        if not (isinstance(value, str) and value):
            raise self.fail(f"{where}.{key}", "빈 문자열이 아닌 문자열이어야 한다")
        return value

    def strings(self, table: dict, key: str, where: str, required: bool = False) -> list[str]:
        if key not in table:
            if required:
                raise self.fail(f"{where}.{key}", "값이 없다")
            return []
        value = table[key]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise self.fail(f"{where}.{key}", "빈 문자열이 아닌 문자열 목록이어야 한다")
        if required and not value:
            raise self.fail(f"{where}.{key}", "항목이 하나 이상 있어야 한다")
        return value

    def regex(self, value: str | None, where: str) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as err:
                raise self.fail(where, f"정규식이 틀렸다 ({err})") from None
        return value


def _id_kind(r: _Reader, table: dict, where: str) -> IdKind:
    defined_by = r.string(table, "defined_by", where)
    head, _, field_name = defined_by.partition(":")
    if defined_by not in ("heading", "table_first_cell") and not (head == "frontmatter" and field_name):
        raise r.fail(f"{where}.defined_by", "heading, table_first_cell, frontmatter:<필드> 가운데 하나여야 한다")
    return IdKind(
        name=r.string(table, "name", where),
        pattern=r.regex(r.string(table, "pattern", where), f"{where}.pattern"),
        defined_by=defined_by,
        scope=r.regex(r.string(table, "scope", where, None), f"{where}.scope"),
        in_=r.strings(table, "in", where),
    )


def _suffix_pairs(r: _Reader, table: dict) -> list[tuple[str, str]]:
    if "suffix_pairs" not in table:
        return list(DEFAULT_SUFFIX_PAIRS)
    value = table["suffix_pairs"]
    if not isinstance(value, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) for v in pair) for pair in value
    ):
        raise r.fail("variants.suffix_pairs", '[["장", " 리더"]]처럼 문자열 두 개짜리 목록의 목록이어야 한다')
    return [(a, b) for a, b in value]


def _hint(r: _Reader, table: dict) -> Hint:
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise r.fail("hint.enabled", "true 또는 false여야 한다")
    min_chars = table.get("min_chars", Hint.min_chars)
    if isinstance(min_chars, bool) or not isinstance(min_chars, int) or min_chars < 0:
        raise r.fail("hint.min_chars", "0 이상의 정수여야 한다")
    return Hint(enabled=enabled, min_chars=min_chars)


def load(path: Path) -> Config:
    r = _Reader(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise ConfigError(f"{path}: 설정 파일을 읽을 수 없다 ({err.strerror})") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: 설정 파일이 UTF-8이 아니다") from None
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"{path}: TOML 형식이 틀렸다 ({err})") from None

    r.table(data, "설정", _TOP_KEYS)
    files = r.table(data.get("files", {}), "files", {"markdown", "text", "exclude"})
    frontmatter = r.table(data.get("frontmatter", {}), "frontmatter", {"columns"})
    roles = r.table(data.get("roles", {}), "roles", {"names", "suspect"})
    variants = r.table(data.get("variants", {}), "variants", {"suffix_pairs"})
    hint = r.table(data.get("hint", {}), "hint", {"enabled", "min_chars"})
    search = r.table(data.get("search", {}), "search", {"priority"})

    anchor = r.string(data, "anchor", "설정", "gitlab")
    if anchor not in ANCHOR_RULES:
        raise r.fail("anchor", f"{', '.join(ANCHOR_RULES)} 가운데 하나여야 한다")

    return Config(
        path=path,
        match=r.strings(data, "match", "설정", required=True),
        anchor=anchor,
        markdown=r.strings(files, "markdown", "files"),
        text=r.strings(files, "text", "files"),
        exclude=r.strings(files, "exclude", "files"),
        frontmatter_columns=r.strings(frontmatter, "columns", "frontmatter"),
        ids=[
            _id_kind(r, t, f"ids[{i}]")
            for i, t in enumerate(r.tables(data.get("ids", []), "ids", {"name", "pattern", "defined_by", "scope", "in"}))
        ],
        aliases=[
            AliasGroup(canonical=r.string(t, "canonical", f"aliases[{i}]"), variants=r.strings(t, "variants", f"aliases[{i}]", required=True))
            for i, t in enumerate(r.tables(data.get("aliases", []), "aliases", {"canonical", "variants"}))
        ],
        roles=Roles(
            names=r.strings(roles, "names", "roles"),
            suspect=r.regex(r.string(roles, "suspect", "roles", None), "roles.suspect"),
        ),
        extensions=[
            Extension(
                name=r.string(t, "name", f"extensions[{i}]"),
                command=r.strings(t, "command", f"extensions[{i}]", required=True),
                commands=r.strings(t, "commands", f"extensions[{i}]"),
            )
            for i, t in enumerate(r.tables(data.get("extensions", []), "extensions", {"name", "command", "commands"}))
        ],
        suffix_pairs=_suffix_pairs(r, variants),
        hint=_hint(r, hint),
        search=Search(priority=r.strings(search, "priority", "search")),
    )


def _missing_message(keys: list[str], dirs: list[Path], has_user_dir: bool) -> str:
    name = keys[-1] + ".toml"
    if has_user_dir:
        where = f"새 설정을 {dirs[0] / name}에 만든다."
    else:
        # 플러그인 폴더는 업데이트 때 바뀌므로 기기의 설정 폴더를 안내한다
        where = (f"새 설정을 ~/.config/doclookup/repos/{name}에 만들고 "
                 "~/.claude/settings.json의 env에 DOCLOOKUP_CONFIG_DIR로 그 폴더를 등록한다.")
    return "\n".join(
        [
            "이 체크아웃에 맞는 설정이 없다.",
            f"시도한 키: {', '.join(keys)}",
            f"찾아본 폴더: {', '.join(str(d) for d in dirs)}",
            f"{where} 최소 예시:",
            "",
            f'match = ["{keys[0]}"]',
            "",
            "[files]",
            'markdown = ["**/*.md"]',
        ]
    )


def find_config(checkout: Path, env: Mapping[str, str], bundled: Path) -> Config:
    keys = repo_keys(checkout)
    dirs = []
    if env_dir := env.get("DOCLOOKUP_CONFIG_DIR"):
        user_dir = Path(env_dir).expanduser()
        if not user_dir.is_dir():
            raise ConfigError(f"DOCLOOKUP_CONFIG_DIR가 가리키는 폴더가 없다: {user_dir}")
        dirs.append(user_dir)
    dirs.append(bundled)
    for directory in dirs:
        configs = [load(p) for p in sorted(directory.glob("*.toml"))] if directory.is_dir() else []
        for key in keys:
            for cfg in configs:
                if key in cfg.match:
                    return cfg
    raise ConfigError(_missing_message(keys, dirs, has_user_dir=len(dirs) > 1))
