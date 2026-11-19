import shlex
from pathlib import Path

from cascade.jobs import ROLES, RUNNER_CLAUDE
from cascade.runners import (
    DEFAULT_MODEL_NAME,
    RUNNER_CODEX,
    RUNNER_CURSOR,
    STATUS_AUTH,
    STATUS_ERROR,
    STATUS_INVALID,
    STATUS_LIMIT,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    UNRECORDED,
    Outcome,
    codex_available,
    codex_model,
    cursor_available,
)

WORKERS_CLAUDE, WORKERS_EXTERNAL = "claude", "external"
WORKERS_CHOICES = (WORKERS_CLAUDE, WORKERS_EXTERNAL)
CLAUDE_VIA_WORKFLOW, CLAUDE_VIA_CLI = "workflow", "cli"
CLAUDE_VIA_CHOICES = (CLAUDE_VIA_WORKFLOW, CLAUDE_VIA_CLI)
DEFAULT_CURSOR_MODEL = "composer-2.5"
WORKER_ROLES = ("a", "b", "writer", "checker")

_SEATS = {"A·writer": ("a", "writer"), "B·checker": ("b", "checker")}
_LABELS = {RUNNER_CLAUDE: "Claude", RUNNER_CODEX: "Codex", RUNNER_CURSOR: "Cursor"}
RECIPIENTS = {RUNNER_CODEX: "OpenAI", RUNNER_CURSOR: "Cursor(팀 프라이버시 모드)"}
FALLBACK_LABELS = {
    STATUS_LIMIT: "한도", STATUS_INVALID: "무효", STATUS_AUTH: "로그인",
    STATUS_TIMEOUT: "시간 초과", STATUS_ERROR: "오류", STATUS_SKIPPED: "건너뜀",
}
LOGIN_COMMANDS = {RUNNER_CODEX: "codex login", RUNNER_CURSOR: "cursor-agent login", RUNNER_CLAUDE: "claude /login"}
_USAGE_KEYS = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cached_input_tokens"}


def _claude(role: str) -> dict:
    model, effort = ROLES[role]
    return {"runner": RUNNER_CLAUDE, "model": model, "effort": effort}


def _external(role: str, runner: str, model: str) -> dict:
    return {"runner": runner, "model": model, "effort": ROLES[role][1]}


def assign(choice: str, available: dict[str, bool], codex_model: str, cursor_model: str) -> dict[str, dict]:
    runners = {role: RUNNER_CLAUDE for role in WORKER_ROLES}
    if choice == WORKERS_EXTERNAL:
        usable = [r for r in (RUNNER_CODEX, RUNNER_CURSOR) if available.get(r)]
        for seat, runner in zip(_SEATS.values(), usable):
            runners.update({role: runner for role in seat})
    models = {RUNNER_CODEX: codex_model, RUNNER_CURSOR: cursor_model}
    return {role: _claude(role) if runner == RUNNER_CLAUDE else _external(role, runner, models[runner])
            for role, runner in runners.items()}


def assign_available(choice: str, cursor_model: str) -> dict[str, dict]:
    if choice != WORKERS_EXTERNAL:
        return {}
    available = {RUNNER_CODEX: codex_available(), RUNNER_CURSOR: cursor_available()}
    return assign(choice, available, codex_model(Path.home() / ".codex"), cursor_model)


def workers_flags(workers: str | None, cursor_model: str | None) -> str:
    """후속 명령에 붙일 작업자 옵션. 받은 값은 기본값과 같아도 넘긴다(프로필 칸을 덮는 옵션이라 빠지면 프로필 값으로 돌아간다)."""
    flags = f" --workers {workers}" if workers else ""
    return flags + (f" --cursor-model {shlex.quote(cursor_model)}" if cursor_model else "")


def expected_model_id(spec: dict) -> str:
    if spec["runner"] == RUNNER_CLAUDE:
        return spec["model"]
    return f"{spec['runner']}:{spec['model'] or DEFAULT_MODEL_NAME}"


def _label(spec: dict) -> str:
    return f"{_LABELS.get(spec['runner'], spec['runner'])}({spec['model'] or DEFAULT_MODEL_NAME})"


def describe(workers: dict[str, dict]) -> str:
    return ", ".join(f"{name}={_label(workers[roles[0]])}" for name, roles in _SEATS.items())


def has_external(workers: dict[str, dict] | None) -> bool:
    return any(spec["runner"] != RUNNER_CLAUDE for spec in (workers or {}).values())


def consent_line(workers: dict[str, dict] | None) -> str:
    used = {spec["runner"] for spec in (workers or {}).values()}
    targets = [RECIPIENTS[r] for r in (RUNNER_CODEX, RUNNER_CURSOR) if r in used]
    return f"작업자 전송 동의: 워크트리의 코드·문서를 {', '.join(targets)}로 보낸다" if targets else ""


def fallback_counts(counts: dict[str, int]) -> tuple[int, str]:
    """상태별 건수에서 대체 실행 건수와 사유별 내역(「한도 1, 무효 2」)을 뽑는다."""
    reasons = {label: counts[status] for status, label in FALLBACK_LABELS.items() if counts.get(status)}
    return sum(reasons.values()), ", ".join(f"{label} {n}" for label, n in reasons.items())


def usage_totals(outcomes: list[Outcome]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for outcome in outcomes:
        total = totals.setdefault(outcome.runner, dict.fromkeys(_USAGE_KEYS, 0))
        for name, key in _USAGE_KEYS.items():
            total[name] += outcome.usage.get(key, 0)
        if outcome.usage.get(UNRECORDED):
            total[UNRECORDED] = total.get(UNRECORDED, 0) + outcome.usage[UNRECORDED]
    return totals
