import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PRICES_AS_OF = "2026-10-06"
PRICE_SOURCE = (
    "claude-api 스킬 자료(shared/prompt-caching.md, shared/model-migration.md) 2026-10-06 기준, $/MTok: "
    "캐시 읽기는 모델별 표기값(Haiku 5.5는 입력 단가의 0.1배), 캐시 쓰기는 5분 TTL 입력 단가의 1.25배·1시간 TTL 입력 단가의 2배"
)
LONG_PROMPT_TOKENS = 100_000
CACHE_WRITE_1H_MULTIPLIER = 2.0

PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
LONG_PROMPT_PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625},
}

_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


class UsageFormatError(ValueError):
    pass


@dataclass(frozen=True)
class AgentUsage:
    label: str
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    cost: float | None
    messages: int = 0


def cost(model: str, input: int, output: int, cache_read: int, cache_write: int,
         cache_write_1h: int = 0) -> float | None:
    """cache_write는 5분 TTL 쓰기, cache_write_1h는 1시간 TTL 쓰기 토큰 수다."""
    prices = PRICES.get(model)
    if prices is None:
        return None
    if model in LONG_PROMPT_PRICES and input + cache_read + cache_write + cache_write_1h > LONG_PROMPT_TOKENS:
        prices = LONG_PROMPT_PRICES[model]
    total = 0.0
    for key, tokens in (("input", input), ("output", output), ("cache_read", cache_read), ("cache_write", cache_write)):
        if tokens == 0:
            continue
        if key not in prices:
            return None
        total += tokens * prices[key] / 1_000_000
    return total + cache_write_1h * prices["input"] * CACHE_WRITE_1H_MULTIPLIER / 1_000_000


def find_workflow_dir(ref: str, projects_root: Path = _DEFAULT_PROJECTS_ROOT) -> Path:
    path = Path(ref)
    if path.is_dir():
        return path
    found = sorted(Path(projects_root).glob(f"*/*/subagents/workflows/{ref}"))
    if not found:
        raise UsageFormatError(f"Workflow 기록 폴더를 찾지 못했다: {ref}")
    if len(found) > 1:
        raise UsageFormatError(f"Workflow 실행 id가 둘 이상의 폴더와 맞는다: {ref} ({len(found)}곳)")
    return found[0]


def _read_jsonl(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise UsageFormatError(f"{path.name}을 읽지 못했다: {e}") from e
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise UsageFormatError(f"{path.name} {n}번째 줄이 JSON이 아니다") from e
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _journal_agents(workflow_dir: Path) -> list[tuple[str, str]]:
    agents: dict[str, str] = {}
    for row in _read_jsonl(workflow_dir / "journal.jsonl"):
        if row.get("type") != "started":
            continue
        agent_id, label = row.get("agentId"), row.get("label")
        if not isinstance(agent_id, str) or not isinstance(label, str):
            raise UsageFormatError("journal.jsonl의 started 줄에 agentId나 label이 없다")
        agents.setdefault(agent_id, label)
    if not agents:
        raise UsageFormatError("journal.jsonl에 started 줄이 없다")
    return list(agents.items())


def _agent_model(workflow_dir: Path, agent_id: str) -> str:
    path = workflow_dir / f"agent-{agent_id}.meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise UsageFormatError(f"{path.name}을 읽지 못했다: {e}") from e
    model = meta.get("model") if isinstance(meta, dict) else None
    if not isinstance(model, str) or not model:
        raise UsageFormatError(f"{path.name}에 model이 없다")
    return model


def _one_hour_writes(raw: dict) -> int:
    breakdown = raw.get("cache_creation")
    tokens = breakdown.get("ephemeral_1h_input_tokens") if isinstance(breakdown, dict) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool):
        return 0
    return max(0, min(tokens, raw["cache_creation_input_tokens"]))


def _usage_values(message: dict, raw: dict, name: str) -> tuple[str, dict[str, int]]:
    message_id = message.get("id")
    if not isinstance(message_id, str):
        raise UsageFormatError(f"{name}의 usage 줄에 message.id가 없다")
    for key in _USAGE_KEYS:
        if not isinstance(raw.get(key), int):
            raise UsageFormatError(f"{name}의 usage에 {key}가 없다")
    return message_id, {**{key: raw[key] for key in _USAGE_KEYS}, "cache_write_1h": _one_hour_writes(raw)}


def _message_cost(model: str, m: dict[str, int]) -> float | None:
    return cost(model, m["input_tokens"], m["output_tokens"], m["cache_read_input_tokens"],
                m["cache_creation_input_tokens"] - m["cache_write_1h"], m["cache_write_1h"])


def _last_usage_per_message(workflow_dir: Path, agent_id: str) -> list[dict[str, int]]:
    name = f"agent-{agent_id}.jsonl"
    last: dict[str, dict[str, int]] = {}
    for row in _read_jsonl(workflow_dir / name):
        message = row.get("message")
        raw = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(raw, dict):
            continue
        message_id, values = _usage_values(message, raw, name)
        last[message_id] = values
    if not last:
        raise UsageFormatError(f"{name}에 토큰 사용량 줄이 없다")
    return list(last.values())


def _agent_usage(label: str, model: str, messages: list[dict[str, int]]) -> AgentUsage:
    costs = [_message_cost(model, m) for m in messages]
    return AgentUsage(
        label=label,
        model=model,
        input=sum(m["input_tokens"] for m in messages),
        output=sum(m["output_tokens"] for m in messages),
        cache_read=sum(m["cache_read_input_tokens"] for m in messages),
        cache_write=sum(m["cache_creation_input_tokens"] for m in messages),
        cost=None if any(c is None for c in costs) else sum(costs),
        messages=len(messages),
    )


def read_workflow(dir: Path) -> list[AgentUsage]:
    workflow_dir = Path(dir)
    result = []
    for agent_id, label in _journal_agents(workflow_dir):
        model = _agent_model(workflow_dir, agent_id)
        messages = _last_usage_per_message(workflow_dir, agent_id)
        result.append(_agent_usage(label, model, messages))
    return result


def _parse_time(value, name: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as e:
        raise UsageFormatError(f"{name}의 시각을 읽지 못했다: {value!r}") from e


def _session_files(path: Path, subagents: bool) -> list[tuple[str, Path]]:
    files = [("main", path)]
    if subagents:
        files += [("subagent", p) for p in sorted(path.with_suffix("").glob("subagents/**/*.jsonl")) if p.name != "journal.jsonl"]
    return files


def read_session(path: Path, *, subagents: bool = False, since: str | None = None,
                 until: str | None = None) -> list[AgentUsage]:
    """세션 기록 JSONL의 토큰 사용량을 (main|subagent, 모델)별로 센다. 메시지 id마다 마지막 줄만 센다.

    subagents면 세션 폴더(`<세션>/subagents/` 아래 Workflow 포함)의 하위 에이전트 기록도 센다. since·until(ISO 시각)을
    주면 그 사이(양 끝 포함) 메시지만 센다. 사용량이 모두 0인 메시지(`<synthetic>` 등)는 건너뛴다.
    """
    path = Path(path)
    start = _parse_time(since, "since") if since else None
    end = _parse_time(until, "until") if until else None
    last: dict[tuple[str, str], tuple[str, str, dict[str, int]]] = {}
    for label, file in _session_files(path, subagents):
        for row in _read_jsonl(file):
            message = row.get("message")
            raw = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(raw, dict):
                continue
            message_id, values = _usage_values(message, raw, file.name)
            if not any(values[key] for key in _USAGE_KEYS):
                continue
            if start or end:
                at = _parse_time(row.get("timestamp"), file.name)
                if (start and at < start) or (end and at > end):
                    continue
            model = message.get("model")
            if not isinstance(model, str) or not model:
                raise UsageFormatError(f"{file.name}의 usage 줄에 message.model이 없다")
            last[(str(file), message_id)] = (label, model, values)
    groups: dict[tuple[str, str], list[dict[str, int]]] = {}
    for label, model, values in last.values():
        groups.setdefault((label, model), []).append(values)
    result = []
    for (label, model), messages in groups.items():
        result.append(_agent_usage(label, model, messages))
    return result
