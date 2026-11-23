"""보고 시점의 사용량을 보관하고 로컬 실행·세션을 집계한다."""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from cascade import assignment, report, toml_edit, usage
from cascade import cli_steps as s
from cascade.config import ConfigError, ExitCodes, profiling_setting
from cascade.jobs import RUNNER_CLAUDE
from cascade.rounds import Ledger
from cascade.runners import STATUS_OK, STATUS_SKIPPED
from cascade.snapshot import SAFE_NAME
from cascade.workers import CLAUDE_VIA_CLI, FALLBACK_LABELS, UNRECORDED

ENV_VAR = "CASCADE_PROFILING"
IMPORTED = "imported-claude.json"
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
CLI_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens")
GROUP_KEYS = ("role", "model", "runner", "repo", "week")
KNOWN_REASONS = {"", *FALLBACK_LABELS.values(), *s.FAILURE_REASONS.values()}
SESSION_ROLES = {
    "a": "code", "b": "code", "writer": "doc", "checker": "doc",
    "judge_a": "doc", "judge_b": "doc", "adjudicator": "unknown",
    "opinion": "unknown", "recheck": "unknown",
}
SESSION_LABEL = re.compile(r"\S+ (" + "|".join(SESSION_ROLES) + ")")


def _setting() -> tuple[bool, str]:
    override = os.environ.get(ENV_VAR)
    if override in ("0", "1"):
        return override == "1", ENV_VAR
    value = profiling_setting(s.repos_toml())
    return (value, "repos.toml") if value is not None else (False, "기본값")


def _profile_dir() -> Path:
    return Path(os.environ.get("CASCADE_PROFILE_DIR") or Path.home() / ".claude" / "cascade-fix" / "profile")


def _status(_args) -> int:
    try:
        enabled, source = _setting()
    except (OSError, ConfigError, tomllib.TOMLDecodeError) as e:
        raise s.CliError(f"프로파일링 설정을 읽지 못했다: {e}", ExitCodes.PARSE_ERROR) from None
    print(f"프로파일링 {'켜짐' if enabled else '꺼짐'} ({source})")
    return 0


def _toggle(args) -> int:
    enabled = args.profile_command == "on"
    try:
        target = toml_edit.locate(os.environ, s.USER_REPOS_TOML, s.LEGACY_REPOS_TOML)
        before = toml_edit.base_text(target, s.LEGACY_REPOS_TOML)
        after = toml_edit.set_profiling(before, enabled)
        if before != after:
            toml_edit.write_checked(target.path, after, datetime.now().strftime("%Y%m%d-%H%M%S"))
    except (OSError, ConfigError) as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None
    print(f"repos.toml에 프로파일링 {'켜짐' if enabled else '꺼짐'}을 저장했다")
    return _status(args)


def _identifier(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[\w.:-]+(?: [\w.:-]+)*", value):
        return value
    return None


def _requested(cell: dict) -> dict:
    return {key: _identifier(cell.get(key)) for key in ("runner", "model", "effort")}


def _tokens(data: dict, keys: tuple[str, ...] = TOKEN_KEYS) -> dict | None:
    values = {key: report._stored_number(data.get(source)) for key, source in zip(TOKEN_KEYS, keys)}
    return values if any(value is not None for value in values.values()) else None


def _openai_cost(model, tokens: dict | None) -> float | None:
    if not tokens or not model or tokens["input"] is None or tokens["output"] is None:
        return None
    return usage.openai_cost(model, tokens["input"], tokens["output"], tokens["cache_read"] or 0)


def _actual(runner, model, tokens=None, cost=None) -> dict:
    return {"runner": _identifier(runner), "model": _identifier(model), "calls": 1,
            "tokens": tokens, "cost": report._stored_number(cost)}


def _add_numbers(total, value):
    if value is None:
        return total
    return (total or 0) + value


def _add_tokens(total: dict | None, tokens: dict | None) -> dict | None:
    if tokens is None:
        return total
    return {key: _add_numbers((total or {}).get(key), tokens.get(key)) for key in TOKEN_KEYS}


def _group_actuals(items: list[dict]) -> list[dict]:
    groups = {}
    for item in items:
        # 비용이 빠진 호출도 함께 보관해 집계에서 따로 셀 수 있게 한다.
        key = (item["runner"], item["model"], item["cost"] is not None, item["tokens"] is not None)
        if key not in groups:
            groups[key] = item.copy()
            continue
        group = groups[key]
        group["calls"] += item["calls"]
        group["tokens"] = _add_tokens(group["tokens"], item["tokens"])
        group["cost"] = _add_numbers(group["cost"], item["cost"])
    return list(groups.values())


def _cell(requested: dict, calls: int | None, reasons: dict, items: list[dict]) -> dict:
    counts = Counter()
    # 직접 적은 대체 사유에는 오류 본문이나 경로가 들어올 수 있다.
    for reason, count in reasons.items():
        counts[reason if reason in KNOWN_REASONS else "기타"] += count
    return {"requested": _requested(requested), "calls": calls, "fallbacks": dict(counts),
            "actual": _group_actuals(items)}


def _by_job(rd: Path, name: str, field: str = "jobs") -> dict:
    data = report._read_json(rd / name)
    if not isinstance(data, dict):
        return {}
    return {job["id"]: job for job in data.get(field) or []
            if isinstance(job, dict) and isinstance(job.get("id"), str)}


def _model_from_result(data: dict, runner: str) -> str | None:
    model = data.get("model_id")
    return model.removeprefix(f"{runner}:") if isinstance(model, str) else None


def _cli_attempts(ledger: Ledger, run_dir: Path):
    """보고서가 읽는 단계별 CLI 결과에서 대체 전과 대체 후의 시도를 나눈다."""
    for no, stage in report._last_stage_events(ledger):
        data = report._stage_cli_data(run_dir, no, stage) or {}
        rd = run_dir / f"round-{no}"
        specs = _by_job(rd, f"{stage}-cli-args.json")
        replacements = data.get("fallback_runs") or {}
        for field in ("replaced", "outcomes"):
            for item in data.get(field) or []:
                if not isinstance(item, dict):
                    continue
                job = item.get("id")
                runner = item.get("runner")
                if not isinstance(job, str) or not runner:
                    continue
                spec = replacements.get(job) if field == "outcomes" else None
                if not isinstance(spec, dict):
                    spec = specs.get(job, {})
                model = _model_from_result(item.get("output") or {}, runner) or spec.get("model")
                yield no, job, runner, model, item


def _round_actuals(ledger: Ledger, run_dir: Path, roles: dict, claude: report._ClaudeUsage):
    actuals = defaultdict(list)
    matched = set()
    jobs_with_attempts = set()
    claude_cli = Counter()
    claude_workflows = set()

    for cell, rows in claude.rows.items():
        if cell not in roles:
            continue
        for row in rows:
            job, no = row.get("job_id"), row["round"]
            actuals[cell].append(_actual(RUNNER_CLAUDE, row.get("model"), _tokens(row), row.get("cost")))
            jobs_with_attempts.add((no, job))
            if row.get("via") == CLAUDE_VIA_CLI:
                claude_cli[no, job] += 1
            else:
                claude_workflows.add((no, job))
                matched.add((job, RUNNER_CLAUDE))

    for no, job, runner, model, outcome in _cli_attempts(ledger, run_dir):
        cell = report._cell_of(ledger, job)
        if cell not in roles or outcome.get("status") == STATUS_SKIPPED:
            continue
        jobs_with_attempts.add((no, job))
        if outcome.get("status") == STATUS_OK:
            matched.add((job, runner))
        if runner == RUNNER_CLAUDE:
            if claude_cli[no, job]:
                claude_cli[no, job] -= 1
                continue
            tokens = None
        else:
            raw = outcome.get("usage") or {}
            tokens = None if raw.get(UNRECORDED) else _tokens(raw, CLI_KEYS)
        actuals[cell].append(_actual(runner, model, tokens, _openai_cost(model, tokens)))
    matched.update((job, RUNNER_CLAUDE) for (_, job), remaining in claude_cli.items() if remaining)

    work = set(assignment.work_keys(roles, ledger.mode))
    for rnd in ledger.rounds:
        rd = run_dir / f"round-{rnd.no}"
        for stage in s.STAGE_ARGS:
            completed = _by_job(rd, f"{stage}-fallback.json", "results")
            if not completed:
                continue
            specs = _by_job(rd, f"{stage}-fallback-args.json")
            for job, output in completed.items():
                cell = report._cell_of(ledger, job)
                if cell in work and (rnd.no, job) not in claude_workflows:
                    model = _model_from_result(output.get("output") or {}, RUNNER_CLAUDE) or specs.get(job, {}).get("model")
                    actuals[cell].append(_actual(RUNNER_CLAUDE, model))
                    jobs_with_attempts.add((rnd.no, job))
        results = report._load_results(run_dir, rnd.no) or {}
        specs = {}
        for name in report.JOB_FILES:
            specs.update(_by_job(rd, name))
        for job in sorted(report._job_ids(run_dir, rnd.no) or ()):
            cell = report._cell_of(ledger, job)
            if cell in work and (rnd.no, job) not in jobs_with_attempts:
                spec = specs.get(job, roles[cell])
                runner = spec.get("runner", RUNNER_CLAUDE)
                model = _model_from_result(results.get(job) or {}, runner) or spec.get("model")
                actuals[cell].append(_actual(runner, model))
    return actuals, matched


def _reviews(ledger: Ledger, data: dict, claude: report._ClaudeUsage,
             actuals: dict, matched: set, reasons: dict) -> dict:
    mode, roles = ledger.mode, data["roles"]
    cells = {}
    first = report._first_review_runner(ledger)
    if first:
        key = f"{mode}.review"
        meta = data["first_review"]
        actual = meta.get("actual") or {}
        items = [_actual(RUNNER_CLAUDE, row.get("model"), _tokens(row), row.get("cost"))
                 for row in claude.first_review_rows]
        attempts = report._first_review_attempts(ledger)
        first_reasons = {attempts[-1].get("reason", ""): 1} if attempts else {}
        cells[key] = _cell(meta.get("requested") or roles[key], 1, first_reasons,
                           items or [_actual(first, actual.get("model"))])

    entries = {
        "opinion": [(s.opinion_job_id(fid), report._opinion_runner(ledger, fid), report._opinion_event(ledger, fid))
                    for fid in ledger.opinions],
        "recheck": [(s.recheck_job_id(rnd.no), report._recheck_runner(ledger, rnd), report._recheck_event(ledger, rnd))
                    for rnd in ledger.rounds if rnd.recheck_opened],
    }
    for role, calls in entries.items():
        key = f"{mode}.{role}"
        fallbacks = Counter(reasons.get(key, {}) if role == "recheck" else {})
        for job, runner, event in calls:
            event = event or {}
            if fallback := event.get("fallback"):
                fallbacks[fallback.get("reason", "")] += 1
            if (job, runner) not in matched:
                cell = s.review_cell(ledger, role)
                model = event.get("model", cell["model"] if cell["runner"] == runner else None)
                actuals[key].append(_actual(runner, model))
        cells[key] = _cell(roles[key], len(calls), fallbacks, actuals[key])
    return cells


def _workflows(data) -> set[str]:
    workflows = data.get("workflows") if isinstance(data, dict) else None
    return {key for key in workflows if re.fullmatch(r"wf_[\w.-]+", key)} if isinstance(workflows, dict) else set()


def _run_date(ledger: Ledger) -> str:
    if ledger.rounds:
        rnd = ledger.rounds[0]
        start = report._parse_time(rnd.started_at) or next(
            (t for event in rnd.events if event.get("step") not in report.UNTIMED_STEPS
             if (t := report._parse_time(event.get("at", ""))) is not None), None)
        if start:
            return start.date().isoformat()
    return date.today().isoformat()


def _run_record(ledger: Ledger, run_dir: Path) -> dict:
    data = assignment.ledger_profile(ledger)
    roles = data["roles"]
    claude = report._ClaudeUsage(ledger, run_dir)
    calls = report._cell_calls(ledger, run_dir)
    reasons, _ = report._worker_fallbacks(ledger, run_dir)
    actuals, matched = _round_actuals(ledger, run_dir, roles, claude)
    cells = {
        key: _cell(roles[key], calls.get(key, 0) if calls is not None else None, reasons.get(key, {}), actuals[key])
        for key in assignment.work_keys(roles, ledger.mode)
    }
    cells.update(_reviews(ledger, data, claude, actuals, matched, reasons))
    workflows = _workflows((data.get("first_review") or {}).get("usage"))
    for rnd in ledger.rounds:
        workflows.update(_workflows(report._read_json(run_dir / f"round-{rnd.no}" / s.USAGE_FILE)))
    manifest = report._read_json(s.SKILL_ROOT / ".claude-plugin" / "plugin.json") or {}
    return {"version": 1, "run_id": ledger.run_id, "repo_id": hashlib.sha1(ledger.root.encode()).hexdigest()[:8],
            "date": _run_date(ledger), "mode": ledger.mode, "profile": _identifier(data["name"]),
            "repo": Path(ledger.root).name, "cascade_fix_version": manifest.get("version"),
            "workflows": sorted(workflows), "cells": cells,
            "rounds": [{"no": rnd.no, "elapsed_seconds": report._elapsed_seconds(rnd)} for rnd in ledger.rounds]}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(text)
            f.close()
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def record_run(ledger: Ledger, run_dir: Path) -> None:
    try:
        if not _setting()[0]:
            return
        if not SAFE_NAME.fullmatch(ledger.run_id) or f"{ledger.run_id}.json" == IMPORTED:
            raise ValueError("실행 ID 형식이 맞지 않는다")
        record = _run_record(ledger, run_dir)
        directory = _profile_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{ledger.run_id}.json"
        # 다른 저장소가 같은 실행 ID를 쓰면 서로 덮어쓰지 않게 이름에 repo_id를 붙인다.
        if path.exists() and (report._read_json(path) or {}).get("repo_id") != record["repo_id"]:
            path = directory / f"{ledger.run_id}-{record['repo_id']}.json"
        _write_json(path, record)
    except Exception as e:  # 기록 실패가 report를 깨뜨리지 않게 한다
        print(f"경고: 프로파일링 기록을 저장하지 못했다 ({type(e).__name__})", file=sys.stderr)


def _runs(directory: Path) -> list[dict]:
    records = []
    for path in sorted(directory.glob("*.json")):
        if path.name != IMPORTED and isinstance(data := report._read_json(path), dict) and data.get("version") == 1:
            records.append(data)
    return records


def _claimed_workflows(runs: list[dict]) -> set[str]:
    return {workflow for run in runs for workflow in run["workflows"]}


def _session_role(label: str) -> tuple[str, str] | None:
    if label in ("code review", "doc review"):
        return label.split()[0], "review"
    match = SESSION_LABEL.fullmatch(label)
    if match:
        role = match[1]
        return SESSION_ROLES[role], role
    return None


def _import_sessions(args) -> int:
    directory = _profile_dir()
    projects = Path(os.environ.get("CASCADE_PROJECTS_ROOT") or Path.home() / ".claude" / "projects")
    claimed = _claimed_workflows(_runs(directory))
    records = []
    failures = 0
    for journal in sorted(projects.rglob("journal.jsonl")):
        workflow = journal.parent.name
        if workflow in claimed:
            continue
        try:
            day = datetime.fromtimestamp(journal.stat().st_mtime).date()
            if args.since and day < args.since:
                continue
            agents = usage.read_workflow(journal.parent)
        except (OSError, ValueError):
            failures += 1
            continue
        # 문서 모드 판정 작업의 라벨은 code와 같은 a·b다. 같은 Workflow에 writer·checker가 있으면 문서 모드로 본다.
        doc_flow = any((m := SESSION_LABEL.fullmatch(a.label)) and m[1] in ("writer", "checker") for a in agents)
        for agent in agents:
            if match := _session_role(agent.label):
                mode, role = match
                if role in ("a", "b") and doc_flow:
                    mode, role = "doc", f"judge_{role}"
                records.append({"source": "claude-session", "workflow_id": workflow, "date": day.isoformat(),
                                "mode": mode, "role": role, "model": _identifier(agent.model), "calls": 1,
                                "tokens": {key: getattr(agent, key) for key in TOKEN_KEYS}, "cost": agent.cost})
    try:
        _write_json(directory / IMPORTED, records)
    except (OSError, ValueError) as e:
        raise s.CliError(f"가져온 프로파일링 기록을 쓰지 못했다: {e}") from None
    print(f"Claude 세션 작업 {len(records)}건 가져옴 · 읽기 실패 {failures}건")
    return 0


def _run_items(run: dict):
    for role, cell in run["cells"].items():
        items = cell["actual"]
        fallbacks = sum(cell["fallbacks"].values())
        for i, item in enumerate(items):
            yield {**item, "role": role, "repo": run["repo"], "date": run["date"],
                   "fallbacks": fallbacks if i == 0 else 0}


def _mismatch(cell: dict) -> bool:
    requested = cell["requested"]["model"]
    actual = {item["model"] for item in cell["actual"] if item["model"]}
    return bool(requested and actual and requested not in actual)


def _sum_items(items: list[dict]) -> dict:
    tokens, cost = None, None
    calls = priced_calls = token_calls = fallbacks = 0
    for item in items:
        calls += item["calls"]
        fallbacks += item["fallbacks"]
        tokens = _add_tokens(tokens, item["tokens"])
        if item["tokens"] is not None:
            token_calls += item["calls"]
        if item["cost"] is not None:
            cost = _add_numbers(cost, item["cost"])
            priced_calls += item["calls"]
    avg_tokens = {key: value / token_calls if value is not None and token_calls else None
                  for key, value in tokens.items()} if tokens is not None else None
    return {"calls": calls, "tokens": tokens, "cost": cost, "unknown_cost_calls": calls - priced_calls,
            "avg_tokens": avg_tokens, "avg_cost": cost / priced_calls if priced_calls else None,
            "fallbacks": fallbacks}


def _cache_write_ratio(items: list[dict], cost: float | None) -> float | None:
    if not cost:
        return None
    known = [item for item in items if item["cost"] is not None and item["model"] in usage.PRICES
             and (item["tokens"] or {}).get("cache_write") is not None]
    if not known:
        return None
    estimate = sum(item["tokens"]["cache_write"] * usage.PRICES[item["model"]]["cache_write"] for item in known)
    return estimate / 1_000_000 / cost


def _aggregate(directory: Path, since: date | None, by: str | None) -> dict:
    all_runs = _runs(directory)
    claimed = _claimed_workflows(all_runs)
    earliest = since.isoformat() if since else ""
    runs = [run for run in all_runs if run["date"] >= earliest]
    items = [item for run in runs for item in _run_items(run)]
    imported = report._read_json(directory / IMPORTED)
    for entry in imported if isinstance(imported, list) else []:
        if entry["date"] >= earliest and entry["workflow_id"] not in claimed:
            items.append({**entry, "role": f"{entry['mode']}.{entry['role']}",
                          "runner": RUNNER_CLAUDE, "repo": "(가져옴)", "fallbacks": 0})
    keys = (by,) if by else ("role", "model")
    grouped = defaultdict(list)
    for item in items:
        iso = date.fromisoformat(item["date"]).isocalendar()
        item["week"] = f"{iso.year}-W{iso.week:02d}"
        group = tuple(item[key] or "" for key in keys)
        grouped[group].append(item)
    groups = [{**dict(zip(keys, group)), **_sum_items(group_items)} for group, group_items in sorted(grouped.items())]
    total = _sum_items(items)
    mismatches = sum(_mismatch(cell) for run in runs for cell in run["cells"].values())
    return {"by": by or "role×model", "since": since.isoformat() if since else None, "groups": groups, "total": total,
            "model_mismatch_cells": mismatches, "cache_write_ratio": _cache_write_ratio(items, total["cost"])}


def _token_text(tokens: dict | None, format_spec: str = "") -> str:
    return " / ".join(format((tokens or {})[key], format_spec) if (tokens or {}).get(key) is not None else "-"
                      for key in TOKEN_KEYS)


def _cost_text(row: dict) -> str:
    unknown = row["unknown_cost_calls"]
    if row["cost"] is None:
        return f"단가 모름 {unknown}건" if unknown else "-"
    cost = report._money(row["cost"])
    return f"{cost} + 단가 모름 {unknown}건" if unknown else cost


def _report_text(data: dict) -> str:
    columns = "역할 · 모델" if data["by"] == "role×model" else {
        "role": "역할", "model": "모델", "runner": "러너", "repo": "저장소", "week": "주",
    }[data["by"]]
    out = [f"| {columns} | 호출 | 토큰(입력/출력/캐시 읽기/캐시 쓰기) | 비용 | 호출당 토큰 | 호출당 비용 | 대체 |",
           "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in [*data["groups"], data["total"]]:
        if row is data["total"]:
            label = "합계"
        elif data["by"] == "role×model":
            label = f"{row['role']} · {row['model'] or '(모델 모름)'}"
        else:
            label = row[data["by"]] or "(알 수 없음)"
        avg_cost = "-" if row["avg_cost"] is None else report._money(row["avg_cost"])
        out.append(f"| {label.replace('|', '/')} | {row['calls']} | {_token_text(row['tokens'])} | {_cost_text(row)} | "
                   f"{_token_text(row['avg_tokens'], '.1f')} | {avg_cost} | {row['fallbacks']} |")
    ratio = data["cache_write_ratio"]
    out.extend(["", f"요청 모델과 실제 모델이 다른 칸 {data['model_mismatch_cells']}개",
                "전체 비용 중 캐시 쓰기 비율(5분 TTL 기준 추정) "
                + ("알 수 없음" if ratio is None else f"{ratio:.1%}")])
    return "\n".join(out)


def _report(args) -> int:
    data = _aggregate(_profile_dir(), args.since, args.by)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else _report_text(data))
    return 0


def _since(value: str) -> date:
    try:
        day = date.fromisoformat(value)
        if value == day.isoformat():
            return day
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("--since는 YYYY-MM-DD로 적어야 한다")


def register(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="profile_command", required=True)
    sub.add_parser("on", help="보고서를 만들 때 사용량을 저장한다").set_defaults(func=_toggle)
    sub.add_parser("off", help="사용량 저장을 끈다").set_defaults(func=_toggle)
    sub.add_parser("status", help="지금 적용되는 저장 설정을 보인다").set_defaults(func=_status)
    summary = sub.add_parser("report", help="보관한 사용량을 집계한다")
    summary.set_defaults(func=_report)
    summary.add_argument("--since", type=_since, metavar="YYYY-MM-DD")
    summary.add_argument("--by", choices=GROUP_KEYS, help="집계 기준(기본: 역할×모델)")
    summary.add_argument("--json", action="store_true")
    sessions = sub.add_parser("import-sessions", help="로컬 Claude Workflow의 사용량을 가져온다")
    sessions.set_defaults(func=_import_sessions)
    sessions.add_argument("--since", type=_since, metavar="YYYY-MM-DD")
