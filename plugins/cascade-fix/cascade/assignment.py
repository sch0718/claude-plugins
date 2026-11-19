import copy
from collections.abc import Callable, Iterable
from pathlib import Path

from cascade import profiles
from cascade.jobs import ROLES, RUNNER_CLAUDE
from cascade.profiles import ROLE_KEYS, RUNNER_AUTO
from cascade.review import CLAUDE_SOURCES, REVIEWER_AUTO, resolve
from cascade.runners import RUNNER_CODEX, RUNNER_CURSOR
from cascade.workers import CLAUDE_VIA_CLI, DEFAULT_CURSOR_MODEL, RECIPIENTS, WORKERS_EXTERNAL

UNAVAILABLE_REASON = "미설치 또는 로그인 필요"
NO_CONSENT_REASON = "전송 동의 없음"
NO_SCRIPT_REASON = "검토 스크립트 없음"
FALLBACK_SUFFIX = "(대체)"
EXTERNAL_RUNNERS = (RUNNER_CODEX, RUNNER_CURSOR)

_CELL_LABELS = {
    "review": "첫 검토", "a": "A", "b": "B", "judge_a": "판정자 a", "judge_b": "판정자 b", "writer": "작성자",
    "checker": "점검자", "adjudicator": "판가름", "opinion": "의견", "recheck": "재검토",
}
_RUNNER_LABELS = {RUNNER_CLAUDE: "Claude", RUNNER_CODEX: "Codex", RUNNER_CURSOR: "Cursor"}
_MODE_WORKER_KEYS = {
    "code": {"a": "code.a", "b": "code.b"},
    "doc": {"a": "doc.judge_a", "b": "doc.judge_b", "writer": "doc.writer", "checker": "doc.checker"},
}
_MODE_SHARED_ROLES = ("adjudicator", "opinion", "recheck")
_WORKER_ROLES = ("a", "b", "judge_a", "judge_b", "writer", "checker")
_REVIEW_ROLES = ("review", "opinion", "recheck")
_OVERRIDE_SEPARATOR = " ← "


def _role(key: str) -> str:
    return key.split(".", 1)[1]


def job_cell_key(mode: str, role: str) -> str | None:
    return f"{mode}.{role}" if role in _MODE_SHARED_ROLES else _MODE_WORKER_KEYS.get(mode, {}).get(role)


def _cell_label(key: str) -> str:
    return _CELL_LABELS[_role(key)]


def overrides_line(overrides: Iterable[str]) -> str:
    grouped: dict[str, list[str]] = {}
    for note in overrides:
        cell, _, option = note.partition(_OVERRIDE_SEPARATOR)
        grouped.setdefault(option, []).append(cell)
    if not grouped:
        return ""
    return "덮음: " + " / ".join(f"{'·'.join(cells)}{_OVERRIDE_SEPARATOR}{option}" for option, cells in grouped.items())


def _claude_name(model: str) -> str:
    name, *rest = model.removeprefix("claude-").split("-")
    version = ".".join(part for part in rest if part.isdigit())
    return f"{name.capitalize()} {version}".strip()


def _runner_label(cell: dict) -> str:
    model = _claude_name(cell["model"]) if cell["runner"] == RUNNER_CLAUDE else cell["model"]
    detail = " ".join(part for part in (model, cell["effort"]) if part)
    name = _RUNNER_LABELS.get(cell["runner"], cell["runner"])
    return f"{name}({detail})" if detail else name


def mode_keys(roles: dict, mode: str) -> list[str]:
    return [key for key in ROLE_KEYS if key.startswith(f"{mode}.") and key in roles]


def work_keys(roles: dict, mode: str) -> list[str]:
    return [key for key in mode_keys(roles, mode) if _role(key) not in _REVIEW_ROLES]


def model_nickname(model: str) -> str:
    return _claude_name(model).split()[0]


def first_review_chain(first_review: dict) -> str:
    attempts = first_review.get("attempts") or []
    runners = [a["runner"] for a in attempts] + [(first_review.get("actual") or {}).get("runner", "")]
    chain = "".join(f" → {_RUNNER_LABELS.get(runners[i + 1], runners[i + 1])}({attempts[i].get('reason', '')})"
                    for i in range(len(attempts)))
    return _RUNNER_LABELS.get(runners[0], runners[0]) + chain


def first_review_runner(first_review: dict | None, source: str | None) -> str | None:
    """첫 검토를 한 runner. 첫 검토 기록이 비어 있으면(--findings-json, review-meta 없는 --input) 해당 없음이라 None."""
    if not first_review:
        return None
    runner = (first_review.get("actual") or {}).get("runner")
    if runner:
        return runner
    return RUNNER_CLAUDE if source in CLAUDE_SOURCES else None


def roles_line(roles: dict, mode: str, first_review: dict | None = None) -> str:
    parts = [f"첫 검토(완료)={first_review_chain(first_review)}"] if first_review else []
    parts += [f"{_cell_label(key)}={_runner_label(roles[key])}" for key in mode_keys(roles, mode)]
    return "역할: " + ", ".join(parts)


def _external_runners(cell: dict) -> set[str]:
    return {r for r in (cell["runner"], cell["fallback"]["runner"]) if r in EXTERNAL_RUNNERS}


def runners_to_check(roles: dict, pinned: Iterable[str]) -> set[str]:
    pinned = set(pinned)
    return {r for key, cell in roles.items() if key not in pinned and cell["runner"] != RUNNER_AUTO
            for r in _external_runners(cell)}


def resolve_auto(roles: dict, source: str, run_review: Path, cursor_ok: bool | Callable[[], bool],
                 *, cursor_model: str = DEFAULT_CURSOR_MODEL, name: str = "auto 해석") -> dict:
    out = copy.deepcopy(roles)
    for key, cell in out.items():
        if cell["runner"] != RUNNER_AUTO:
            continue
        runner = RUNNER_CLAUDE if _role(key) == "review" else resolve(REVIEWER_AUTO, source, run_review, cursor_ok)
        if runner == RUNNER_CLAUDE:
            default = profiles.builtin("claude-only")["roles"][key]
            cell.update(runner=RUNNER_CLAUDE, model=cell["model"] or default["model"],
                        effort=cell["effort"] or default["effort"])
        else:
            out[key] = {**profiles.reset_cell(key, runner, cursor_model), "fallback": cell["fallback"]}
    profiles.validate(name, {"adopt_on_both_pass": True, "roles": out})
    return out


def _usable(runner: str, available: dict[str, bool]) -> bool:
    return runner in (RUNNER_CLAUDE, RUNNER_AUTO) or available.get(runner, False)


def _unusable_reason(key: str, runner: str, available: dict[str, bool], reason: str,
                     scripts: dict[str, bool] | None) -> str | None:
    if not _usable(runner, available):
        return reason
    if scripts is not None and _role(key) in ("opinion", "recheck") and runner in EXTERNAL_RUNNERS \
            and not scripts.get(runner, False):
        return NO_SCRIPT_REASON
    return None


def substitute_unavailable(roles: dict, available: dict[str, bool], reason: str,
                           scripts: dict[str, bool] | None = None) -> tuple[dict, list[dict]]:
    claude_only = profiles.builtin("claude-only")["roles"]
    out, records = copy.deepcopy(roles), []
    for key, cell in list(out.items()):
        runner, fallback = cell["runner"], cell["fallback"]
        if why := _unusable_reason(key, runner, available, reason, scripts):
            if _unusable_reason(key, fallback["runner"], available, reason, scripts) is None:
                new = {**fallback, "fallback": dict(claude_only[key]["fallback"])}
            else:
                new = copy.deepcopy(claude_only[key])
            records.append({"role": key, "from": runner, "to": new["runner"], "reason": why})
            out[key] = new
        elif why := _unusable_reason(key, fallback["runner"], available, reason, scripts):
            records.append({"role": key + FALLBACK_SUFFIX, "from": fallback["runner"], "to": RUNNER_CLAUDE, "reason": why})
            cell["fallback"] = dict(claude_only[key]["fallback"])
    return out, records


def decide(roles: dict, pinned: Iterable[str], source: str, run_review: Path,
           cursor_ok: bool | Callable[[], bool], available: dict[str, bool],
           *, cursor_model: str = DEFAULT_CURSOR_MODEL, name: str = "auto 해석",
           scripts: dict[str, bool] | None = None) -> tuple[dict, list[dict]]:
    kept = set(pinned) | {key for key, cell in roles.items() if cell["runner"] == RUNNER_AUTO}
    resolved = resolve_auto(roles, source, run_review, cursor_ok, cursor_model=cursor_model, name=name)
    replaced, records = substitute_unavailable({k: c for k, c in resolved.items() if k not in kept},
                                               available, UNAVAILABLE_REASON, scripts)
    return {**resolved, **replaced}, records


def fill_codex_model(roles: dict, model: str) -> dict:
    out = copy.deepcopy(roles)
    if not model:
        return out
    for key, cell in out.items():
        if _role(key) not in _WORKER_ROLES:
            continue
        for target in (cell, cell["fallback"]):
            if target["runner"] == RUNNER_CODEX and not target["model"]:
                target["model"] = model
    return out


def recipients(roles: dict, mode: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {runner: [] for runner in EXTERNAL_RUNNERS}
    for key in mode_keys(roles, mode):
        cell = roles[key]
        if cell["runner"] in out:
            out[cell["runner"]].append(key)
        if cell["fallback"]["runner"] in out:
            out[cell["fallback"]["runner"]].append(key + FALLBACK_SUFFIX)
    return out


def consent_line(roles: dict, mode: str) -> str:
    parts, fallback_parts = [], []
    for runner, keys in recipients(roles, mode).items():
        main = [_cell_label(key) for key in keys if not key.endswith(FALLBACK_SUFFIX)]
        fallback = [_cell_label(key.removesuffix(FALLBACK_SUFFIX)) for key in keys if key.endswith(FALLBACK_SUFFIX)]
        if main:
            parts.append(f"{'·'.join(main)} → {RECIPIENTS[runner]}")
        if fallback:
            fallback_parts.append(f"대체 실행 때 {'·'.join(fallback)} → {RECIPIENTS[runner]}")
    parts += fallback_parts
    return "전송 동의: " + " / ".join(parts) if parts else ""


def used_recipients(roles: dict, mode: str) -> list[str]:
    return [runner for runner, keys in recipients(roles, mode).items() if keys]


def apply_consent(roles: dict, consented: set[str]) -> tuple[dict, list[dict]]:
    claude_only = profiles.builtin("claude-only")["roles"]
    out, records = copy.deepcopy(roles), []
    for key, cell in list(out.items()):
        runner, fallback = cell["runner"], cell["fallback"]
        if runner != RUNNER_CLAUDE and runner not in consented:
            records.append({"role": key, "from": runner, "to": RUNNER_CLAUDE, "reason": NO_CONSENT_REASON})
            out[key] = copy.deepcopy(claude_only[key])
        elif fallback["runner"] != RUNNER_CLAUDE and fallback["runner"] not in consented:
            records.append({"role": key + FALLBACK_SUFFIX, "from": fallback["runner"], "to": RUNNER_CLAUDE,
                            "reason": NO_CONSENT_REASON})
            cell["fallback"] = dict(claude_only[key]["fallback"])
    return out, records


def mode_workers(roles: dict, mode: str) -> dict[str, dict]:
    return {role: copy.deepcopy(roles[key]) for role, key in _MODE_WORKER_KEYS[mode].items()}


_VALUE_KEYS = ("runner", "model", "effort")


def claude_only_cell(role: str) -> dict:
    model, effort = ROLES[role]
    return {"runner": RUNNER_CLAUDE, "model": model, "effort": effort}


def claude_fallback(cell: dict, role: str) -> dict:
    fallback = cell.get("fallback")
    return dict(fallback) if fallback and fallback["runner"] == RUNNER_CLAUDE else claude_only_cell(role)


def fallback_target(cell: dict, consented: Iterable[str], exhausted: Iterable[str], role: str) -> dict:
    fallback = cell.get("fallback") or claude_only_cell(role)
    runner = fallback["runner"]
    if runner != RUNNER_CLAUDE and (runner not in set(consented) or runner in set(exhausted)):
        fallback = claude_only_cell(role)
    return {**fallback, "fallback": claude_only_cell(role)}


def _is_claude_default(workers: dict[str, dict], mode: str) -> bool:
    defaults = profiles.builtin("claude-only")["roles"]
    return all([spec[k] for k in _VALUE_KEYS] == [defaults[_MODE_WORKER_KEYS[mode][role]][k] for k in _VALUE_KEYS]
               for role, spec in workers.items())


def adjudicator_cell(ledger) -> dict:
    return ledger_profile(ledger)["roles"][f"{ledger.mode}.adjudicator"]


def job_workers(ledger) -> dict[str, dict] | None:
    workers = mode_workers(ledger_profile(ledger)["roles"], ledger.mode)
    return None if _is_claude_default(workers, ledger.mode) else workers


def build_workers(ledger) -> dict[str, dict] | None:
    """작업 파일을 만들 때 쓰는 칸 배정. `claude_via == cli`면 모두 Claude여도 작업마다 runner·워크트리를 싣도록 배정을 준다."""
    return job_workers(ledger) or (mode_workers(ledger_profile(ledger)["roles"], ledger.mode) if ledger.claude_via == CLAUDE_VIA_CLI else None)


def apply_sandbox_substitution(ledger, before: dict[str, dict], after: dict[str, dict], reason: str) -> None:
    record = copy.deepcopy(ledger_profile(ledger))
    keys = _MODE_WORKER_KEYS[ledger.mode]
    for role, spec in after.items():
        key = keys[role]
        if before[role]["runner"] != spec["runner"]:
            record["substituted"].append({"role": key, "from": before[role]["runner"], "to": spec["runner"], "reason": reason})
        else:
            old, new = (before[role].get("fallback") or {}).get("runner"), (spec.get("fallback") or {}).get("runner")
            if old and old != new:
                record["substituted"].append({"role": key + FALLBACK_SUFFIX, "from": old, "to": new, "reason": reason})
        record["roles"][key] = copy.deepcopy(spec)
    record["consented"] = [r for r in record["consented"] if r in used_recipients(record["roles"], ledger.mode)]
    ledger.profile = record
    ledger.workers = copy.deepcopy(after)


def legacy_fields(record: dict, mode: str, requested_workers: str | None = None) -> dict:
    roles = record["roles"]
    opinion = roles[f"{mode}.opinion"]
    workers = mode_workers(roles, mode)
    external = any(spec["runner"] != RUNNER_CLAUDE for spec in workers.values())
    is_claude = opinion["runner"] == RUNNER_CLAUDE
    if requested_workers == WORKERS_EXTERNAL or external:
        workers_choice = WORKERS_EXTERNAL
    else:
        workers_choice = RUNNER_CLAUDE
        if _is_claude_default(workers, mode):
            workers = {}
    return {
        "reviewer": opinion["runner"],
        "review_model": opinion["model"] if is_claude else "",
        "review_effort": opinion["effort"] if is_claude else "",
        "workers_choice": workers_choice,
        "workers": workers,
        "adopt_on_both_pass": record.get("adopt_on_both_pass", False),
    }


def ledger_profile(ledger) -> dict:
    if ledger.profile:
        return ledger.profile
    roles = profiles.builtin("claude-only")["roles"]
    for mode in ("code", "doc"):
        for role in ("opinion", "recheck"):
            key = f"{mode}.{role}"
            if ledger.reviewer == RUNNER_CLAUDE:
                roles[key] = {**roles[key], "model": ledger.review_model or roles[key]["model"],
                              "effort": ledger.review_effort or roles[key]["effort"]}
            else:
                roles[key] = {**profiles.reset_cell(key, ledger.reviewer), "fallback": roles[key]["fallback"]}
    for role, key in _MODE_WORKER_KEYS[ledger.mode].items():
        spec = (ledger.workers or {}).get(role)
        if spec:
            roles[key] = {"runner": spec["runner"], "model": spec["model"], "effort": spec["effort"],
                          "fallback": roles[key]["fallback"]}
    consented = [r for r in EXTERNAL_RUNNERS if recipients(roles, ledger.mode)[r]]
    return {"name": "legacy", "source": "legacy", "explicit": False, "roles": roles, "overrides": [],
            "substituted": [], "consented": consented, "first_review": {}}

