import tomllib
from pathlib import Path

from cascade import profiles
from cascade.config import ConfigError

TABLE_PATH = Path(__file__).with_name("recommendations.toml")
ENV_KEYS = ("none", "codex", "cursor", "codex+cursor")
PRIORITIES = ("save-claude", "fast")
MEASURED = "measured"
PROVISIONAL = "provisional"
STATUSES = (MEASURED, PROVISIONAL)
FAST_PROFILE = "claude-only"
_TABLE_KEYS = ("version", "updated", "source", "env")
_ROW_KEYS = ("recommend", "status", "alternatives")
_ALTERNATIVE_KEYS = ("profile", "status", "note")
_EXTERNAL_RUNNERS = ("codex", "cursor")


def load_table(path: Path = TABLE_PATH) -> dict:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"추천 표를 읽을 수 없다: {path}: {e}") from e
    validate_table(table)
    return table


def env_key(codex: bool, cursor: bool) -> str:
    return "+".join(name for name, present in (("codex", codex), ("cursor", cursor)) if present) or "none"


def env_runners(env: str) -> set[str]:
    return set() if env == "none" else set(env.split("+"))


def _profile_runners(name: str) -> set[str]:
    return {cell["runner"] for cell in profiles.builtin(name)["roles"].values()} & set(_EXTERNAL_RUNNERS)


def _check_profile(env: str, name, status) -> None:
    if name not in profiles.BUILTIN_NAMES:
        raise ConfigError(f"추천 표 env.{env}: 내장 프로필이 아닌 이름이다: {name!r}. 알려진 이름: {', '.join(profiles.BUILTIN_NAMES)}")
    if status not in STATUSES:
        raise ConfigError(f"추천 표 env.{env}: 모르는 status다: {status!r}. 알려진 값: {', '.join(STATUSES)}")
    absent = _profile_runners(name) - env_runners(env)
    if absent:
        raise ConfigError(f"추천 표 env.{env}: 이 환경에 없는 runner({', '.join(sorted(absent))})를 쓰는 프로필이다: {name}")


def _check_keys(where: str, row, expected: tuple[str, ...]) -> None:
    if not isinstance(row, dict) or set(row) != set(expected):
        raise ConfigError(f"추천 표 {where}: 키는 {', '.join(expected)}여야 한다")


def validate_table(table: dict) -> None:
    _check_keys("최상위", table, _TABLE_KEYS)
    if not isinstance(table["version"], int) or isinstance(table["version"], bool):
        raise ConfigError("추천 표 version은 정수여야 한다")
    for key in ("updated", "source"):
        if not isinstance(table[key], str):
            raise ConfigError(f"추천 표 {key}는 문자열이어야 한다")
    if not isinstance(table["env"], dict) or set(table["env"]) != set(ENV_KEYS):
        raise ConfigError(f"추천 표 env의 환경 키는 {', '.join(ENV_KEYS)} 넷이어야 한다")
    for env, row in table["env"].items():
        _check_keys(f"env.{env}", row, _ROW_KEYS)
        _check_profile(env, row["recommend"], row["status"])
        if not isinstance(row["alternatives"], list):
            raise ConfigError(f"추천 표 env.{env}: alternatives는 목록이어야 한다")
        for alternative in row["alternatives"]:
            _check_keys(f"env.{env} alternatives", alternative, _ALTERNATIVE_KEYS)
            _check_profile(env, alternative["profile"], alternative["status"])
            if not isinstance(alternative["note"], str):
                raise ConfigError(f"추천 표 env.{env}: note는 문자열이어야 한다")


def recommended(table: dict, env: str, priority: str) -> str:
    if priority == "fast":
        return FAST_PROFILE
    return table["env"][env]["recommend"]


def options(table: dict, env: str, priority: str) -> list[dict]:
    row = table["env"][env]
    entries = [{"profile": row["recommend"], "status": row["status"], "note": ""}, *row["alternatives"]]
    first = recommended(table, env, priority)
    known = next((e for e in entries if e["profile"] == first), {"status": PROVISIONAL, "note": ""})
    result = [{"profile": first, "status": known["status"], "note": known["note"], "recommended": True}]
    for entry in entries:
        if entry["profile"] not in {o["profile"] for o in result}:
            result.append(dict(entry))
    return result
