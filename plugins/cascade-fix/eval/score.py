import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
from cascade.assignment import first_review_runner, job_cell_key  # noqa: E402
from cascade.cli import PRE_FINAL_DECISIONS  # noqa: E402
from cascade.cli_steps import CLAUDE_JOB_FILES as ALL_CLAUDE_JOB_FILES  # noqa: E402
from cascade.cli_steps import OPINION_ARGS, RECHECK_ARGS, RECHECK_CLI_ARGS, USAGE_FILE, claude_job_entries  # noqa: E402
from cascade.consensus import REASON_BOTH_PASS  # noqa: E402
from cascade.jobs import RUNNER_CLAUDE  # noqa: E402
from cascade.report import JOB_FILES  # noqa: E402
from cascade.report import _money as money  # noqa: E402
from cascade.report import _via_of_file  # noqa: E402
from cascade.runners import STATUS_INVALID  # noqa: E402
from cascade.usage import PRICE_SOURCE, PRICES_AS_OF  # noqa: E402
from cascade.workers import CLAUDE_VIA_WORKFLOW, FALLBACK_LABELS  # noqa: E402

TARGET_AGREEMENT = 0.95
TARGET_OPUS = 0.40
MAX_AGREEMENT_GAP = 0.05
MAX_FALLBACK = 0.10
MAX_ACCURACY_DROP = 0.05
MAX_UNFINISHED = 0.05
VERDICT_FACT = "사실"
VERDICT_NO = "아님"
VERDICT_UNREPRODUCIBLE = "재현 불가"
VERDICT_NONE = "판정 없음"
OUTCOME_AGREED = ("합의-사실", "합의-아님")
OUTCOME_ADJUDICATED = "판가름"
OUTCOMES_TO_HUMAN = ("판단불가", "설계결정", "보류후보")
REASON_PATCH_DIFFERS = "패치가 다름"
BOTH_PASS_REASONS = (REASON_PATCH_DIFFERS, REASON_BOTH_PASS)
REASON_ADJUDICATED_NO = "판가름: 아님"
UNMEASURED = "측정 불가(dry-run은 전체 테스트를 돌리지 않음)"
PATH_KEYS = ("합의", "규칙 채택", "판가름", "Codex 검토", "사람")
CLAUDE_FAMILIES = ("sonnet", "haiku", "opus")
REVIEW_JOB_FILES = (OPINION_ARGS, "opinion-cli-args.json", RECHECK_ARGS, RECHECK_CLI_ARGS)
CLAUDE_JOB_FILES = tuple(name for name in ALL_CLAUDE_JOB_FILES if name not in REVIEW_JOB_FILES)
USAGE_KEYS = ("input", "output", "cache_read")
TRUTH_KEYS = (VERDICT_FACT, VERDICT_NO, VERDICT_UNREPRODUCIBLE)
STEP_APPLY = "apply"
COST_ALL, COST_CORE = "all", "round1-core"
COST_SCOPES = (COST_ALL, COST_CORE)
CORE_ROLES = ("a", "b", "judge_a", "judge_b", "writer", "checker", "adjudicator")
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
COUNTERFACTUAL_FILE = "counterfactual.json"
REASON_COUNTERFACTUAL = "반사실은 round1-core만"
REASON_NO_RUN = "채점된 실행 없음"
TAG_FIRST_REVIEW = "첫 검토"
UNMEASURED_COST = "미측정"
NO_RECORD = "기록 없음"
SELECTION_LABELS = {"accuracy": "정확도", "false_fact": "「아님」 오채택", "completion": "완주", "fallback": "대체 실행",
                    "regression": "회귀"}


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_sample(path) -> dict[str, dict]:
    items = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            items[item["id"]] = item
    return items


def final_decision(ledger: dict, finding_id: str) -> dict | None:
    for rnd in reversed(ledger.get("rounds", [])):
        found = [d for d in rnd.get("decisions", []) if d.get("finding_id") == finding_id]
        if found:
            return found[-1]
    return None


def predict(decision: dict | None) -> str:
    if decision is None:
        return VERDICT_NONE
    outcome, reasons = decision.get("outcome"), decision.get("reasons", [])
    if outcome == "합의-사실" or (outcome == OUTCOME_ADJUDICATED and decision.get("patch_from")):
        return VERDICT_FACT
    if outcome == "합의-아님" or (outcome == OUTCOME_ADJUDICATED and REASON_ADJUDICATED_NO in reasons):
        return VERDICT_NO
    if any(VERDICT_UNREPRODUCIBLE in r for r in reasons):
        return VERDICT_UNREPRODUCIBLE
    return VERDICT_NONE


def path_of(decision: dict | None, opinions: dict, finding_id: str) -> str | None:
    if decision is None:
        return None
    outcome = decision.get("outcome")
    if outcome in OUTCOME_AGREED:
        return "규칙 채택" if any(REASON_BOTH_PASS in r for r in decision.get("reasons", [])) else "합의"
    if outcome == OUTCOME_ADJUDICATED:
        return "판가름"
    if outcome in OUTCOMES_TO_HUMAN:
        return "Codex 검토" if finding_id in opinions else "사람"
    return None


def within_round(no: int | None, round_no: int | None) -> bool:
    return round_no is None or (no is not None and no <= round_no)


def round_files(run_dir: Path, name: str, round_no: int | None = None):
    for path in sorted(run_dir.glob(f"round-*/{name}")):
        suffix = path.parent.name.removeprefix("round-")
        if within_round(int(suffix) if suffix.isdigit() else None, round_no):
            yield read_json(path)


def count_roles(run_dir: Path, role: str, round_no: int | None = None) -> list[dict]:
    out = []
    for merged in round_files(run_dir, "results.json", round_no):
        out += [w for key, w in merged.items() if key.endswith(f":{role}")]
    return out


def checker_stats(ledger: dict, run_dir: Path, round_no: int | None = None) -> tuple[int, int]:
    if ledger.get("mode") != "doc":
        return 0, 0
    false_ids, fixed = 0, 0
    for merged in round_files(run_dir, "results.json", round_no):
        for key, w in merged.items():
            if key.endswith(":checker") and w.get("checker_ok") is False:
                false_ids += 1
                adj = merged.get(f"{key.rpartition(':')[0]}:adjudicator")
                if adj and adj.get("verdict") == VERDICT_FACT:
                    fixed += 1
    return fixed, false_ids


def claude_calls(run_dir: Path, round_no: int | None = None) -> dict[str, int]:
    calls = dict.fromkeys(CLAUDE_FAMILIES, 0)
    for name in CLAUDE_JOB_FILES:
        for data in round_files(run_dir, name, round_no):
            for job in claude_job_entries(data):
                for family in CLAUDE_FAMILIES:
                    calls[family] += str(job.get("model", "")).startswith(f"claude-{family}-")
    return calls


def workers_events(ledger: dict) -> list[dict]:
    last: dict[tuple, dict] = {}
    for rnd in ledger.get("rounds", []):
        for event in rnd.get("events", []):
            if str(event.get("step", "")).startswith("workers-"):
                last[(rnd.get("no"), event["step"])] = event
    return list(last.values())


def add_counts(total: dict, part: dict) -> None:
    for key, value in part.items():
        total[key] = total.get(key, 0) + value


def workers_stats(ledger: dict) -> tuple[int, dict[str, int], int, dict[str, dict[str, int]]]:
    counts: dict[str, int] = {}
    fallback_counts: dict[str, int] = {}
    usage: dict[str, dict[str, int]] = {}
    for event in workers_events(ledger):
        add_counts(counts, event.get("counts") or {})
        if event["step"] != "workers-opinion":
            add_counts(fallback_counts, event.get("counts") or {})
        for runner, tokens in (event.get("usage") or {}).items():
            add_counts(usage.setdefault(runner, dict.fromkeys(USAGE_KEYS, 0)), {k: tokens.get(k, 0) for k in USAGE_KEYS})
    fallback = {label: fallback_counts[status] for status, label in FALLBACK_LABELS.items() if fallback_counts.get(status)}
    return sum(fallback_counts.values()), fallback, fallback_counts.get(STATUS_INVALID, 0), usage


def measured_checks(apply: dict) -> list[dict]:
    return [apply, *(apply.get("final_history") or [])]


def is_regression(apply: dict) -> bool:
    return any(h.get("final_ok") is False and not h.get("baseline_failed") for h in apply.get("final_history") or [apply])


def is_complete(ledger: dict) -> bool:
    rounds = ledger.get("rounds", [])
    return bool(rounds) and all(rnd.get("decisions") and any(e.get("step") == STEP_APPLY for e in rnd.get("events", []))
                                for rnd in rounds)


def limit_rounds(ledger: dict, round_no: int | None) -> dict:
    return {**ledger, "rounds": [rnd for rnd in ledger.get("rounds", []) if within_round(rnd.get("no"), round_no)]}


def read_record(path: Path):
    try:
        return read_json(path)
    except (OSError, ValueError) as e:
        raise ValueError(f"{path.parent.name}/{path.name}을 읽지 못했다 ({e})") from None


def use_pre_final(ledger: dict, run_dir: Path) -> dict:
    rounds = []
    for rnd in ledger["rounds"]:
        saved = run_dir / f"round-{rnd.get('no')}" / PRE_FINAL_DECISIONS
        decisions = read_record(saved) if saved.is_file() else None
        if saved.is_file() and not isinstance(decisions, list):
            raise ValueError(f"{saved.parent.name}/{saved.name}이 판정 목록이 아니다")
        rounds.append({**rnd, "decisions": decisions} if saved.is_file() else rnd)
    return {**ledger, "rounds": rounds}


def load_run(base: Path, run_dir: Path, round_no: int | None, pre_final: bool) -> dict | None:
    if not (base / "ledger.json").is_file():
        return None
    ledger = read_record(base / "ledger.json")
    if not isinstance(ledger, dict):
        raise ValueError(f"{base.name}/ledger.json이 원장 객체가 아니다")
    limited = limit_rounds(ledger, round_no)
    return {"ledger": use_pre_final(limited, run_dir) if pre_final else limited, "run_dir": run_dir,
            "complete": is_complete(limited)}


def add_run_metrics(m: dict, run: dict, round_no: int | None) -> tuple[int, int]:
    ledger, run_dir = run["ledger"], run["run_dir"]
    m["opus_calls"] += len(count_roles(run_dir, "adjudicator", round_no))
    add_counts(m["claude_calls"], claude_calls(run_dir, round_no))
    cli_jobs, fallback, invalid, usage = workers_stats(ledger)
    m["cli_jobs"] += cli_jobs
    m["invalid"] += invalid
    add_counts(m["fallback"], fallback)
    for runner, tokens in usage.items():
        add_counts(m["usage"].setdefault(runner, dict.fromkeys(USAGE_KEYS, 0)), tokens)
    applies = list(round_files(run_dir, "apply.json", round_no))
    m["regression_measured"] |= any(h.get("final_ok") is not None for a in applies for h in measured_checks(a))
    m["regressions"] += any(is_regression(a) for a in applies)
    return checker_stats(ledger, run_dir, round_no)


def locate(runs: Path, item_id: str, item: dict, round_no: int | None,
           pre_final: bool) -> tuple[str, dict | str | None, str | None]:
    """(실행 키, 실행·"errors"·"incomplete"·None, 읽기 실패 사유). 기록을 읽지 못한 실행은 오류 항목으로 센다."""
    run_key = item.get("run") or item_id
    try:
        return run_key, find_run(runs, item_id, item, round_no, pre_final), None
    except (OSError, ValueError) as e:
        return run_key, "errors", f"{run_key}: {e}"


def find_run(runs: Path, item_id: str, item: dict, round_no: int | None, pre_final: bool) -> dict | str | None:
    if item.get("run"):
        base = runs / item["run"]
        return load_run(base, base, round_no, pre_final)
    base = runs / item_id
    if not (base / "meta.json").is_file():
        return None
    meta = read_record(base / "meta.json")
    if not isinstance(meta, dict):
        raise ValueError(f"{item_id}/meta.json이 객체가 아니다")
    if "error" in meta:
        return "errors"
    if meta.get("incomplete"):
        return "incomplete"
    run = load_run(base, base / "run", round_no, pre_final)
    return run and {**run, "complete": True}


def read_optional(path: Path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def stored_number(value) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def usage_rows(data, cell: str | None = None) -> list[dict]:
    workflows = data.get("workflows") if isinstance(data, dict) else None
    rows = [row for share in (workflows.values() if isinstance(workflows, dict) else []) if isinstance(share, list)
            for row in share if isinstance(row, dict)]
    return [{**row, "cell": cell or row["cell"]} for row in rows if cell or isinstance(row.get("cell"), str)]


def in_scope(cell: str, scope: str) -> bool:
    return scope == COST_ALL or cell.partition(".")[2] in CORE_ROLES


def cost_round(round_no: int | None, scope: str) -> int | None:
    if scope == COST_ALL:
        return round_no
    return 1 if round_no is None else min(round_no, 1)


def round_dirs(run_dir: Path, round_no: int | None) -> list[Path]:
    suffixes = {path: path.name.removeprefix("round-") for path in sorted(run_dir.glob("round-*"))}
    return [path for path, suffix in suffixes.items() if suffix.isdigit() and within_round(int(suffix), round_no)]


def job_ids(round_dir: Path, names: tuple[str, ...]) -> set[str]:
    ids: set[str] = set()
    for name in names:
        data = read_optional(round_dir / name)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        ids.update(j["id"] for j in jobs or [] if isinstance(j, dict) and isinstance(j.get("id"), str))
    return ids


def scoped_cells(ids, mode: str, scope: str) -> list[str]:
    cells = (job_cell_key(mode, job_id.rsplit(":", 1)[-1]) for job_id in ids)
    return [cell for cell in cells if cell and in_scope(cell, scope)]


def new_cell() -> dict:
    return {"calls": 0, "fallback": 0, "tokens": None, "cost": None}


def fallback_job_ids(ledger: dict) -> list[str]:
    return [job_id for event in workers_events(ledger) for job_id in event.get("fallback") or []]


def claude_job_names(scope: str) -> tuple[str, ...]:
    return (*CLAUDE_JOB_FILES, *(REVIEW_JOB_FILES if scope == COST_ALL else ()))


def claude_jobs(round_dir: Path, mode: str, scope: str) -> set[str]:
    ids = {job["id"] for name in claude_job_names(scope) for job in claude_job_entries(read_optional(round_dir / name))
           if isinstance(job.get("id"), str)}
    return {job_id for job_id in ids if scoped_cells([job_id], mode, scope)}


def claude_expected(round_dir: Path, mode: str, scope: str) -> Counter:
    """기록이 있어야 하는 Claude 호출을 (작업 id, 실행 경로)별 횟수로 센다. CLI 호출과 그 대체 Workflow 호출은 따로 센다."""
    return Counter((job["id"], _via_of_file(name)) for name in claude_job_names(scope)
                   for job in claude_job_entries(read_optional(round_dir / name))
                   if isinstance(job.get("id"), str) and scoped_cells([job["id"]], mode, scope))


def first_review_of(ledger: dict) -> dict:
    review = (ledger.get("profile") or {}).get("first_review")
    return review if isinstance(review, dict) else {}


def first_review_is_claude(ledger: dict, review: dict) -> bool:
    return first_review_runner(review, ledger.get("source")) == RUNNER_CLAUDE


def add_cell_calls(cells: dict[str, dict], ledger: dict, rounds: list[Path], scope: str) -> None:
    mode = ledger.get("mode")
    names = (*JOB_FILES, *(REVIEW_JOB_FILES if scope == COST_ALL else ()))
    for round_dir in rounds:
        results = read_optional(round_dir / "results.json")
        ids = job_ids(round_dir, names) | set(results if isinstance(results, dict) else ())
        for cell in scoped_cells(ids, mode, scope):
            cells.setdefault(cell, new_cell())["calls"] += 1
    for cell in scoped_cells(fallback_job_ids(ledger), mode, scope):
        cells.setdefault(cell, new_cell())["fallback"] += 1
    if scope == COST_ALL and first_review_of(ledger):
        cells.setdefault(f"{mode}.review", new_cell())["calls"] += 1


def add_cell_usage(cells: dict[str, dict], unmeasured: set[str], rows: list[dict]) -> None:
    for row in rows:
        cell = cells.setdefault(row["cell"], new_cell())
        cell["tokens"] = cell["tokens"] or dict.fromkeys(TOKEN_KEYS, 0)
        for key in TOKEN_KEYS:
            cell["tokens"][key] += stored_number(row.get(key)) or 0
        cost = stored_number(row.get("cost"))
        if cost is None:
            unmeasured.add(row["cell"])
        else:
            cell["cost"] = (cell["cost"] or 0.0) + cost


def collect_usage(ledger: dict, rounds: list[Path], scope: str) -> tuple[list[dict], list[str], list[str], set[str]]:
    """(쓸 행, 단가 기준 날짜, 미측정 표지, 기록이 빠진 작업이 있는 칸). 표지는 ""(이 실행의 작업)나 첫 검토다."""
    mode = ledger.get("mode")
    rows: list[dict] = []
    dates: list[str] = []
    tags: list[str] = []
    gaps: set[str] = set()
    for round_dir in rounds:
        data = read_optional(round_dir / USAGE_FILE)
        found = [row for row in usage_rows(data) if in_scope(row["cell"], scope)]
        if found and isinstance(data.get("prices_as_of"), str):
            dates.append(data["prices_as_of"])
        rows += found
        have = Counter((row.get("job_id"), row.get("via", CLAUDE_VIA_WORKFLOW)) for row in found)
        unrecorded = [job_id for (job_id, via), n in claude_expected(round_dir, mode, scope).items() if n > have[(job_id, via)]]
        if unrecorded:
            tags.append("")
            gaps.update(scoped_cells(unrecorded, mode, scope))
    review = first_review_of(ledger)
    if scope == COST_ALL and review:
        found = usage_rows(review.get("usage"), f"{mode}.review")
        if found and isinstance((review.get("usage") or {}).get("prices_as_of"), str):
            dates.append(review["usage"]["prices_as_of"])
        rows += found
        if not found and first_review_is_claude(ledger, review):
            tags.append(TAG_FIRST_REVIEW)
            gaps.add(f"{mode}.review")
    if any(stored_number(row.get("cost")) is None for row in rows):
        tags.append("")
    return rows, dates, list(dict.fromkeys(tags)), gaps


def counterfactual_scope(runs: Path) -> str | None:
    path = runs / COUNTERFACTUAL_FILE
    if not path.is_file():
        return None
    data = read_optional(path)
    declared = data.get("cost_scope") if isinstance(data, dict) else None
    return declared if declared in COST_SCOPES else COST_CORE


def add_run_cost(m: dict, acc: dict, run_key: str, run: dict, round_no: int | None, scope: str) -> None:
    ledger = limit_rounds(run["ledger"], cost_round(round_no, scope))
    rounds = round_dirs(Path(run["run_dir"]), cost_round(round_no, scope))
    add_cell_calls(m["cells"], ledger, rounds, scope)
    mode = ledger.get("mode")
    if acc["refused"]:
        for round_dir in rounds:
            acc["gaps"].update(scoped_cells(claude_jobs(round_dir, mode, scope), mode, scope))
        return
    rows, dates, tags, gaps = collect_usage(ledger, rounds, scope)
    acc["gaps"] |= gaps
    acc["counted"] += 1
    acc["dates"] += dates
    acc["missing"] += [f"{run_key}: {tag}" if tag else run_key for tag in tags]
    acc["total"] += sum(c for c in (stored_number(row.get("cost")) for row in rows) if c is not None)
    add_cell_usage(m["cells"], acc["unmeasured"], rows)


def finish_cost(m: dict, acc: dict) -> None:
    for cell in acc["unmeasured"] | acc["gaps"]:
        m["cells"].setdefault(cell, new_cell())["cost"] = None
    for cell in acc["gaps"]:
        m["cells"].setdefault(cell, new_cell())["tokens"] = None
    m["cells_unmeasured"] = sorted(acc["gaps"])
    if acc["refused"]:
        m["cost_missing"] = [REASON_COUNTERFACTUAL]
    else:
        m["cost_missing"] = acc["missing"] or ([] if acc["counted"] else [REASON_NO_RUN])
    m["claude_cost"] = None if m["cost_missing"] else acc["total"]
    dates = sorted(set(acc["dates"]))
    m["prices_as_of"] = dates[0] if len(dates) == 1 else dates or PRICES_AS_OF
    m["price_source"] = PRICE_SOURCE


def fallback_ratio(m: dict) -> float | None:
    return sum(m["fallback"].values()) / m["cli_jobs"] if m["cli_jobs"] else None


def new_truth_row(truth: str) -> dict:
    return {"total": 0, "correct": 0, "judged": 0, "to_human": 0, "false_fact": 0 if truth == VERDICT_NO else None}


def compute(runs, sample: dict[str, dict], *, round_no: int | None = None, pre_final: bool = False,
            cost_scope: str = COST_ALL) -> dict:
    if cost_scope not in COST_SCOPES:
        raise ValueError(f"알 수 없는 비용 범위: {cost_scope}")
    runs = Path(runs)
    m = {"items": 0, "errors": 0, "incomplete": 0, "missing": 0, "regression_measured": False, "agree": 0, "judged": 0, "opus_calls": 0, "baseline_calls": 0,
         "patch_differs": 0, "regressions": 0, "paths": dict.fromkeys(PATH_KEYS, 0), "predicted": {},
         "claude_calls": dict.fromkeys(CLAUDE_FAMILIES, 0), "fallback": {}, "cli_jobs": 0, "invalid": 0, "usage": {},
         "total": len(sample), "correct": 0, "false_fact": 0, "to_human": 0, "cells": {}, "cost_scope": cost_scope, "read_errors": [],
         "by_truth": {truth: new_truth_row(truth) for truth in TRUTH_KEYS}}
    refused = cost_scope == COST_ALL and counterfactual_scope(runs) not in (None, COST_ALL)
    acc = {"refused": refused, "counted": 0, "total": 0.0, "missing": [], "dates": [], "unmeasured": set(), "gaps": set()}
    checker_fixed = checker_false = 0
    counted: set[str] = set()
    for item_id, item in sample.items():
        truth = item["truth"]["verdict"]
        row = m["by_truth"].setdefault(truth, new_truth_row(truth))
        row["total"] += 1
        run_key, run, read_error = locate(runs, item_id, item, round_no, pre_final)
        if read_error and read_error not in m["read_errors"]:
            m["read_errors"].append(read_error)
        if run is None:
            m["missing"] += 1
            continue
        if isinstance(run, str):
            m[run] += 1
            continue
        if not run["complete"]:
            m["incomplete"] += 1
            continue
        ledger = run["ledger"]
        fid = item["finding"]["id"]
        decision = final_decision(ledger, fid)
        verdict = predict(decision)
        m["items"] += 1
        m["predicted"][item_id] = verdict
        if verdict != VERDICT_NONE:
            m["judged"] += 1
            row["judged"] += 1
            m["agree"] += verdict == truth
        if verdict == truth:
            m["correct"] += 1
            row["correct"] += 1
        if verdict == VERDICT_FACT and truth == VERDICT_NO:
            m["false_fact"] += 1
            row["false_fact"] += 1
        if decision and decision.get("outcome") in OUTCOMES_TO_HUMAN:
            m["to_human"] += 1
            row["to_human"] += 1
        path = path_of(decision, ledger.get("opinions", {}), fid)
        if path:
            m["paths"][path] += 1
        if any(key in r for rnd in ledger.get("rounds", []) for d in rnd.get("decisions", [])
               if d.get("finding_id") == fid for r in d.get("reasons", []) for key in BOTH_PASS_REASONS):
            m["patch_differs"] += 1
        m["baseline_calls"] += item.get("baseline_opus_calls", 1)
        if run_key not in counted:
            counted.add(run_key)
            fixed, false_ids = add_run_metrics(m, run, round_no)
            checker_fixed += fixed
            checker_false += false_ids
            add_run_cost(m, acc, run_key, run, round_no, cost_scope)
    m["opus_ratio"] = m["opus_calls"] / m["baseline_calls"] if m["baseline_calls"] else None
    m["agreement"] = m["agree"] / m["judged"] if m["judged"] else None
    m["accuracy"] = m["correct"] / m["total"] if m["total"] else None
    m["scored"] = m["total"] - m["errors"] - m["incomplete"] - m["missing"]
    m["completion"] = m["scored"] / m["total"] if m["total"] else None
    m["checker"] = (checker_fixed, checker_false)
    m["fallback_ratio"] = fallback_ratio(m)
    finish_cost(m, acc)
    return m


def pct(num: int, den: int) -> str:
    return f"{num}/{den} ({num / den * 100:.1f}%)" if den else "-"


def ratio_text(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "-"


def passed(ok: bool) -> str:
    return "통과" if ok else "미달"


def regression_text(m: dict) -> str:
    return f"회귀 {m['regressions']}건" if m["regression_measured"] else "회귀 미측정"


def verdict_line(m: dict) -> str:
    agree_ok = m["agreement"] is not None and m["agreement"] >= TARGET_AGREEMENT
    opus_ok = m["opus_ratio"] is not None and m["opus_ratio"] <= TARGET_OPUS
    return (f"성공 기준: 판정 일치 {ratio_text(m['agreement'])} (목표 95%) {passed(agree_ok)}"
            f" · Opus 호출 {ratio_text(m['opus_ratio'])} (목표 40% 이하) {passed(opus_ok)}"
            f" · {regression_text(m)} (목표 0)" + ("" if not m["regression_measured"] else f" {passed(m['regressions'] == 0)}"))


def cost_text(m: dict) -> str:
    if m["claude_cost"] is None:
        return f"{UNMEASURED_COST}({', '.join(m['cost_missing'])})"
    return money(m["claude_cost"])


def price_basis_line(m: dict) -> str:
    as_of = m["prices_as_of"] if isinstance(m["prices_as_of"], str) else ", ".join(m["prices_as_of"])
    return f"단가 기준: {as_of} {m['price_source']}"


def cell_rows(m: dict) -> list[tuple[str, ...]]:
    rows = []
    for key, cell in m["cells"].items():
        tokens = cell["tokens"]
        absent = UNMEASURED_COST if key in m["cells_unmeasured"] else NO_RECORD
        tokens_text = absent if tokens is None else "/".join(str(tokens[k]) for k in TOKEN_KEYS[:3])
        cost = absent if tokens is None else UNMEASURED_COST if cell["cost"] is None else money(cell["cost"])
        rows.append((key, str(cell["calls"]), str(cell["fallback"]), tokens_text, cost))
    return rows


def has_cell_usage(m: dict) -> bool:
    return bool(m["cli_jobs"] or m["cells_unmeasured"]) or any(cell["tokens"] for cell in m["cells"].values())


def claude_calls_text(m: dict) -> str:
    return " · ".join(f"{family.capitalize()} {m['claude_calls'][family]}" for family in CLAUDE_FAMILIES)


def fallback_text(m: dict) -> str:
    if not m["cli_jobs"]:
        return "-"
    reasons = ", ".join(f"{label} {n}" for label, n in m["fallback"].items())
    return pct(sum(m["fallback"].values()), m["cli_jobs"]) + (f" — {reasons}" if reasons else "")


def usage_text(tokens: dict | None) -> str:
    if tokens is None:
        return "-"
    return f"입력 {tokens['input']} · 출력 {tokens['output']} · 캐시 읽기 {tokens['cache_read']}"


def workers_rows(m: dict) -> list[tuple[str, str]]:
    return [("대체 실행", fallback_text(m)), ("무효 결과", pct(m["invalid"], m["cli_jobs"])),
            ("Claude 호출", claude_calls_text(m)),
            *((f"CLI 사용량({runner})", usage_text(tokens)) for runner, tokens in m["usage"].items())]


def truth_rows(m: dict) -> list[tuple[str, ...]]:
    return [(truth, str(r["correct"]), str(r["judged"]), str(r["total"]), str(r["to_human"]),
             "-" if r["false_fact"] is None else str(r["false_fact"])) for truth, r in m["by_truth"].items()]


def truth_cell(row: dict) -> str:
    cell = f"{row['correct']}/{row['judged']}/{row['total']} · 사람 목록 {row['to_human']}"
    return cell if row["false_fact"] is None else f"{cell} · 아님→사실 채택 {row['false_fact']}"


def table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    return [f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * len(header))} |", *(f"| {' | '.join(row)} |" for row in rows)]


def render(m: dict) -> str:
    paths = ", ".join(f"{k} {v}" for k, v in m["paths"].items())
    rows = [
        ("정확도", f"{pct(m['correct'], m['total'])} — 맞음/표본 전체"),
        ("완료율", f"{pct(m['scored'], m['total'])} — 채점됨/표본 전체"),
        ("판정 일치율", f"{pct(m['agree'], m['judged'])} — 일치/판정 있음"),
        ("아님→사실 채택", str(m["false_fact"])),
        ("사람 목록", str(m["to_human"])),
        ("채택 경로 분포", paths),
        ("패치 다르지만 둘 다 통과", pct(m["patch_differs"], m["items"])),
        ("Opus 호출 수", str(m["opus_calls"])),
        ("기준 Opus 호출 수", str(m["baseline_calls"])),
        ("Opus 호출 비율", ratio_text(m["opus_ratio"])),
        ("회귀 건수", str(m["regressions"]) if m["regression_measured"] else UNMEASURED),
        ("Claude 비용(추정)", f"{cost_text(m)} — 범위 {m['cost_scope']}"),
        ("문서 점검자 정확도", pct(*m["checker"])),
        *(workers_rows(m) if m["cli_jobs"] else []),
        ("채점한 항목 수", str(m["items"])),
        ("오류 항목 수", str(m["errors"])),
        ("미완료 항목 수", str(m["incomplete"])),
        ("실행 기록 없는 항목 수", str(m["missing"])),
    ]
    truth = table(("정답", "맞음", "판정 있음", "전체", "사람 목록", "아님→사실 채택"), truth_rows(m))
    cells = table(("칸", "호출", "대체 실행", "Claude 토큰(입력/출력/캐시 읽기)", "Claude 비용(추정)"), cell_rows(m))
    return "\n".join([*table(("값", "결과"), rows), "", "정답별", "", *truth, "", *(["칸별 Claude 사용량", "", *cells, ""] if has_cell_usage(m) else []),
                      price_basis_line(m), "", verdict_line(m), ""])


def unfinished(m: dict) -> int:
    return m["errors"] + m["incomplete"] + m["missing"]


def accuracy_check(m: dict, base: dict) -> dict:
    cand, ref = m["accuracy"], base["accuracy"]
    ok = cand is not None and ref is not None and round(ref - cand, 6) <= MAX_ACCURACY_DROP
    return {"ok": ok, "value": cand, "baseline": ref, "limit": MAX_ACCURACY_DROP}


def completion_check(m: dict) -> dict:
    ratio = unfinished(m) / m["total"] if m["total"] else None
    return {"ok": ratio is not None and round(ratio, 6) <= MAX_UNFINISHED, "value": ratio, "count": unfinished(m),
            "total": m["total"], "limit": MAX_UNFINISHED}


def fallback_check(m: dict) -> dict:
    ratio = fallback_ratio(m)
    return {"ok": ratio is None or round(ratio, 6) <= MAX_FALLBACK, "value": ratio, "limit": MAX_FALLBACK}


def regression_check(m: dict) -> dict:
    if not m["regression_measured"]:
        return {"ok": None, "value": None, "limit": 0}
    return {"ok": m["regressions"] == 0, "value": m["regressions"], "limit": 0}


def selection(m: dict, base: dict) -> dict:
    checks = {"accuracy": accuracy_check(m, base),
              "false_fact": {"ok": m["false_fact"] <= base["false_fact"], "value": m["false_fact"], "baseline": base["false_fact"]},
              "completion": completion_check(m), "fallback": fallback_check(m), "regression": regression_check(m)}
    return {"passed": all(check["ok"] is not False for check in checks.values()), "checks": checks,
            "claude_cost": m["claude_cost"], "cost_missing": m["cost_missing"]}


SELECTION_DETAILS = {
    "accuracy": lambda c: f"{ratio_text(c['value'])}, 기준선 {ratio_text(c['baseline'])}에서 {c['limit'] * 100:.0f}%p 이내",
    "false_fact": lambda c: f"{c['value']}건, 기준선 {c['baseline']}건 이하",
    "completion": lambda c: f"오류·미완료·누락 {c['count']}/{c['total']}, {c['limit'] * 100:.0f}% 이하",
    "fallback": lambda c: "외부 작업 없음" if c["value"] is None else f"{ratio_text(c['value'])}, {c['limit'] * 100:.0f}% 이하",
    "regression": lambda c: f"{c['value']}건",
}


def selection_part(key: str, check: dict) -> str:
    if check["ok"] is None:
        return f"{SELECTION_LABELS[key]} 미측정"
    return f"{SELECTION_LABELS[key]} {passed(check['ok'])}({SELECTION_DETAILS[key](check)})"


def selection_line(sel: dict) -> str:
    failed = [SELECTION_LABELS[key] for key, check in sel["checks"].items() if check["ok"] is False]
    cost = money(sel["claude_cost"]) if sel["claude_cost"] is not None else UNMEASURED_COST
    parts = [selection_part(key, check) for key, check in sel["checks"].items()]
    return f"선정 조건: {'통과' if not failed else '미달(' + ', '.join(failed) + ')'} — {' · '.join(parts)} · Claude 비용 {cost}"


def compare_causes(m: dict, base: dict) -> list[str]:
    causes = []
    if m["agreement"] is None or base["agreement"] is None or agreement_gap(m, base) > MAX_AGREEMENT_GAP * 100:
        causes.append("일치율")
    if m["opus_calls"] > base["opus_calls"]:
        causes.append("Opus 호출")
    if not m["cli_jobs"]:
        causes.append("대체 실행 미측정")
    elif sum(m["fallback"].values()) > m["cli_jobs"] * MAX_FALLBACK:
        causes.append("대체 실행")
    if m["regression_measured"] and m["regressions"]:
        causes.append("회귀")
    return causes


def agreement_gap(m: dict, base: dict) -> float:
    return round((base["agreement"] - m["agreement"]) * 100, 6)


def compare_verdict_line(m: dict, base: dict) -> str:
    causes = compare_causes(m, base)
    gap = f"{agreement_gap(m, base):.1f}%p" if m["agreement"] is not None and base["agreement"] is not None else "-"
    fallback = pct(sum(m["fallback"].values()), m["cli_jobs"])
    return (f"비교 판정: {'통과' if not causes else '미달(' + ', '.join(causes) + ')'}"
            f" — 일치율 차이 {gap} (목표 {MAX_AGREEMENT_GAP * 100:.0f}%p 이하)"
            f" · Opus 호출 {m['opus_calls']}/{base['opus_calls']} (목표 기준 이하)"
            f" · 대체 실행 {fallback} (목표 {MAX_FALLBACK * 100:.0f}% 이하)"
            f" · {regression_text(m)} (목표 0)")


def render_compare(m: dict, base: dict) -> str:
    runners = list(dict.fromkeys([*base["usage"], *m["usage"]]))
    rows = [
        ("정확도", pct(base["correct"], base["total"]), pct(m["correct"], m["total"])),
        ("판정 일치율", pct(base["agree"], base["judged"]), pct(m["agree"], m["judged"])),
        ("Opus 호출 수", str(base["opus_calls"]), str(m["opus_calls"])),
        ("Opus 호출 비율", ratio_text(base["opus_ratio"]), ratio_text(m["opus_ratio"])),
        ("Claude 호출", claude_calls_text(base), claude_calls_text(m)),
        ("대체 실행", fallback_text(base), fallback_text(m)),
        ("무효 결과", pct(base["invalid"], base["cli_jobs"]), pct(m["invalid"], m["cli_jobs"])),
        *((f"CLI 사용량({runner})", usage_text(base["usage"].get(runner)), usage_text(m["usage"].get(runner))) for runner in runners),
        ("회귀 건수", *(str(x["regressions"]) if x["regression_measured"] else UNMEASURED for x in (base, m))),
        ("Claude 비용(추정)", cost_text(base), cost_text(m)),
        ("채점한 항목 수", str(base["items"]), str(m["items"])),
    ]
    truth = [(name, truth_cell(base["by_truth"][name]), truth_cell(m["by_truth"][name])) for name in m["by_truth"]
             if name in base["by_truth"]]
    return "\n".join([*table(("값", "Claude 작업자", "외부 작업자"), rows), "", "정답별 (맞음/판정 있음/전체)", "",
                      *table(("정답", "Claude 작업자", "외부 작업자"), truth), "", price_basis_line(m), "",
                      selection_line(selection(m, base)), compare_verdict_line(m, base), ""])


def write_output(path: str, text: str) -> bool:
    try:
        Path(path).write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"결과를 쓰지 못했다: {path} ({e})", file=sys.stderr)
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="소급 재생 결과 채점")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--baseline-runs", help="Claude 작업자로 돌린 결과 폴더. 주면 --runs(외부 작업자)와 나란히 비교한다")
    parser.add_argument("--round", type=int, dest="round_no", help="이 회차까지만 채점한다")
    parser.add_argument("--pre-final", action="store_true", help="전체 검사 전 판정(decisions-pre-final.json)으로 채점한다")
    parser.add_argument("--cost-scope", choices=COST_SCOPES, default=COST_ALL, dest="cost_scope",
                        help="Claude 비용 범위. round1-core는 1회차 작업자·점검자·판가름과 그 대체 실행만 센다")
    parser.add_argument("--out")
    parser.add_argument("--json", help="지표(와 기준선이 있으면 선정 판정)를 JSON으로 쓴다")
    args = parser.parse_args(argv)
    sample = load_sample(args.sample)
    options = {"round_no": args.round_no, "pre_final": args.pre_final, "cost_scope": args.cost_scope}
    m = compute(args.runs, sample, **options)
    base = compute(args.baseline_runs, sample, **options) if args.baseline_runs else None
    text = render_compare(m, base) if base else render(m)
    print(text)
    for result in (m, base):
        for line in (result or {}).get("read_errors", []):
            print(f"읽지 못해 오류 항목으로 셌다: {line}", file=sys.stderr)
    failed = False
    if args.out:
        failed |= not write_output(args.out, text)
    if args.json:
        failed |= not write_output(args.json, json.dumps({**m, "selection": selection(m, base) if base else None},
                                                         ensure_ascii=False, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
