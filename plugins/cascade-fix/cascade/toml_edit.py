import difflib
import json
import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cascade import profiles
from cascade.config import ConfigError, has_repos

ENV_VAR = "CASCADE_REPOS_TOML"
TABLE_HEADER = re.compile(r"^\s*\[")
STRING_VALUE = r'(?:"(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\')'
SAMPLE_ONLY_MARKERS = ("형식 견본이다", "예전처럼 이 파일에")
BACKUP_STAMP = re.compile(r"\.bak-\d{8}-\d{6}(?:-\d{2})?")


class EditError(ConfigError):
    pass


@dataclass(frozen=True)
class Target:
    path: Path
    copy_from: Path | None


def locate(env: Mapping[str, str], user: Path, legacy: Path) -> Target:
    if env.get(ENV_VAR):
        raise EditError(f"{ENV_VAR}({env[ENV_VAR]})이 잡혀 있어 기기 설정을 쓰지 않는다. 변수를 지우고 다시 하라")
    if user.is_file():
        return Target(user, None)
    if legacy.is_file() and has_repos(legacy):
        return Target(user, legacy)
    return Target(user, None)


def base_text(target: Target, sample: Path) -> str:
    if target.path.is_file():
        return target.path.read_text(encoding="utf-8")
    if target.copy_from is not None:
        return target.copy_from.read_text(encoding="utf-8")
    header = []
    for line in sample.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith("#"):
            break
        if not any(marker in line for marker in SAMPLE_ONLY_MARKERS):
            header.append(line)
    return "".join(header)


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _with_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def _set_assignment(line: str, key: str, name: str) -> str:
    match = re.match(rf"^(?P<head>\s*{key}\s*=\s*){STRING_VALUE}(?P<tail>.*)$", line.rstrip("\r\n"), re.DOTALL)
    ending = line[len(line.rstrip("\r\n")):]
    if match:
        return f"{match['head']}{_quote(name)}{match['tail']}{ending}"
    indent = re.match(r"\s*", line).group()
    return f"{indent}{key} = {_quote(name)}{ending or chr(10)}"


def _find_assignment(lines: list[str], key: str, start: int, end: int) -> int | None:
    pattern = re.compile(rf"^\s*{key}\s*=")
    return next((i for i in range(start, end) if pattern.match(lines[i])), None)


def _next_table(lines: list[str], start: int) -> int:
    return next((i for i in range(start, len(lines)) if TABLE_HEADER.match(lines[i])), len(lines))


def _table_comment_start(lines: list[str], table: int) -> int:
    start = table
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    try:
        tomllib.loads("".join(lines[:start]))
    except tomllib.TOMLDecodeError:
        return table
    return start


def _parse(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise EditError(f"repos.toml을 읽지 못했다: {e}") from e


def _checked(out: str, expected: dict, manual: str) -> str:
    try:
        edited = tomllib.loads(out)
    except tomllib.TOMLDecodeError:
        edited = None
    if edited != expected:
        raise EditError(f"고친 결과가 예상과 달라 자동으로 고치지 않는다. 직접 {manual} 줄을 넣어라")
    return out


def set_default_profile(text: str, name: str) -> str:
    expected = {**_parse(text), "default_profile": name}
    return _checked(_default_profile_text(text, name), expected, f"맨 위에 default_profile = {_quote(name)}")


def _default_profile_text(text: str, name: str) -> str:
    lines = text.splitlines(keepends=True)
    first_table = _next_table(lines, 0)
    found = _find_assignment(lines, "default_profile", 0, first_table)
    if found is not None:
        lines[found] = _set_assignment(lines[found], "default_profile", name)
        return "".join(lines)
    return _insert_top_level(text, lines, first_table, f"default_profile = {_quote(name)}\n")


def _insert_top_level(text: str, lines: list[str], first_table: int, entry: str) -> str:
    if first_table < len(lines):
        at = _table_comment_start(lines, first_table)
        lines[at:at] = [entry, "\n"]
        return "".join(lines)
    head = _with_newline(text)
    if head and head.splitlines()[-1].strip():
        head += "\n"
    return head + entry


def set_profiling(text: str, enabled: bool) -> str:
    expected = {**_parse(text), "profiling": enabled}
    value = "true" if enabled else "false"
    entry = f"profiling = {value}"
    lines = text.splitlines(keepends=True)
    first_table = _next_table(lines, 0)
    key = r"""(?:profiling|"profiling"|'profiling')"""
    found = _find_assignment(lines, key, 0, first_table)
    if found is None:
        out = _insert_top_level(text, lines, first_table, entry + "\n")
    else:
        line = lines[found]
        lines[found], count = re.subn(rf"^(\s*{key}\s*=\s*)(?:true|false)(?=\s|#|$)",
                                      lambda m: m[1] + value, line)
        if not count:
            lines[found] = entry + "\n"
        out = "".join(lines)
    return _checked(out, expected, f"맨 위에 {entry}")


def _repo_key(repos: dict, root: Path) -> str | None:
    root = Path(root).resolve()
    return next((k for k in repos if Path(k).expanduser().resolve() == root), None)


def _header_pattern(key: str) -> re.Pattern:
    quoted = "|".join(re.escape(form) for form in (_quote(key), f"'{key}'"))
    return re.compile(rf"^\s*\[\s*repos\s*\.\s*(?:{quoted})\s*\]\s*(?:#.*)?$")


def set_repo_profile(text: str, root: Path, name: str) -> str:
    data = _parse(text)
    repos = data.get("repos", {})
    key = _repo_key(repos, root) if isinstance(repos, dict) else None
    if key is None:
        table = f"[repos.{_quote(str(root))}]\nprofile = {_quote(name)}\n"
        head = _with_newline(text)
        return f"{head}\n{table}" if head else table
    lines = text.splitlines(keepends=True)
    pattern = _header_pattern(key)
    header = next((i for i, line in enumerate(lines) if pattern.match(line.rstrip("\r\n"))), None)
    if header is None:
        raise EditError(
            f"[repos.{_quote(key)}] 표 머리를 찾지 못해 자동으로 고치지 않는다. "
            f"그 저장소 항목에 직접 profile = {_quote(name)} 줄을 넣어라"
        )
    end = _next_table(lines, header + 1)
    found = _find_assignment(lines, "profile", header + 1, end)
    if found is not None:
        lines[found] = _set_assignment(lines[found], "profile", name)
    else:
        lines[header] = _with_newline(lines[header])
        lines.insert(header + 1, f"profile = {_quote(name)}\n")
    expected = {**data, "repos": {**repos, key: {**repos[key], "profile": name}}}
    return _checked("".join(lines), expected, f"[repos.{_quote(key)}] 항목에 profile = {_quote(name)}")


def diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=str(path), tofile=str(path),
    ))


def backups(path: Path) -> list[Path]:
    if not path.parent.is_dir():
        return []
    return sorted((p for p in path.parent.iterdir()
                   if p.name.startswith(path.name) and BACKUP_STAMP.fullmatch(p.name[len(path.name):])),
                  key=lambda p: p.name, reverse=True)


def _backup(path: Path, stamp: str) -> Path:
    target = path.with_name(f"{path.name}.bak-{stamp}")
    count = 0
    while target.exists():
        count += 1
        target = path.with_name(f"{path.name}.bak-{stamp}-{count:02d}")
    shutil.copy2(path, target)
    return target


def _prune(path: Path, keep: int) -> None:
    for old in backups(path)[keep:]:
        old.unlink()


def write_checked(path: Path, text: str, stamp: str, keep: int = 5) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.is_file() else None
    backup = _backup(path, stamp) if original is not None else None
    path.write_text(text, encoding="utf-8")
    try:
        tomllib.loads(text)
        profiles.load_profiles(path)
    except (tomllib.TOMLDecodeError, ConfigError) as e:
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)
            backup.unlink()
        raise EditError(f"쓴 내용을 읽지 못해 되돌렸다: {e}") from e
    _prune(path, keep)
    return backup


def restore_latest(path: Path, stamp: str) -> tuple[Path, Path]:
    found = backups(path)
    if not found:
        raise EditError(f"되돌릴 백업이 없다: {path}")
    if not path.is_file():
        raise EditError(f"현재 파일이 없어 되돌리지 못한다: {path}")
    latest = found[0]
    kept = _backup(path, stamp)
    shutil.copyfile(latest, path)
    _prune(path, max(len(found), 5))
    return latest, kept
