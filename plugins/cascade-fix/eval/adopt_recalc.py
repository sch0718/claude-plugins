import argparse
import json
import shutil
import sys
import tomllib
from collections import Counter
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
from cascade import cli_steps  # noqa: E402
from cascade.cli import LOSSY_FILE, PRE_FINAL_DECISIONS  # noqa: E402
from cascade.config import ConfigError, RepoConfig, load_repo_config  # noqa: E402
from cascade.consensus import OUTCOME_ADJUDICATE, OUTCOME_UNDECIDABLE, Decision, WorkerResult, decide  # noqa: E402
from cascade.patches import changed_files  # noqa: E402
from cascade.runners import STATUS_OK, Outcome  # noqa: E402
from cascade.verify import VerifyResult  # noqa: E402
from cascade.workers import usage_totals  # noqa: E402
from score import COST_CORE, COUNTERFACTUAL_FILE, TOKEN_KEYS, read_json, read_optional, stored_number  # noqa: E402

ROUND_DIR = "round-1"
ADJ_ARGS, ADJ_RESULTS = "adj-args.json", "adj.json"
ADJ_CLI_RESULTS = "adj-cli.json"
STEP_WORKERS_ADJ = "workers-adj"
ADJ_ARTIFACTS = ((ADJ_ARGS, ADJ_RESULTS), ("adj-cli-args.json", ADJ_CLI_RESULTS),
                 ("adj-fallback-args.json", "adj-fallback.json"))
STEP_ADJUDICATE = "adjudicate"
ADJUDICATOR = "adjudicator"
MODE_CODE = "code"


ITEM_ERRORS = (OSError, ValueError, KeyError, TypeError, StopIteration, AttributeError)


class RecalcError(Exception):
    def __init__(self, reason: str, check: str = "read"):
        super().__init__(reason)
        self.check = check


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def skip_reason(base: Path) -> str | None:
    meta = read_json(base / "meta.json")
    if "error" in meta:
        return "오류 항목"
    if meta.get("incomplete") or meta.get("pending_run_dir"):
        return "미완료 항목"
    if not (base / "ledger.json").is_file():
        return "원장 없음"
    ledger = read_json(base / "ledger.json")
    if ledger.get("mode") != MODE_CODE:
        return "문서 모드: 채택 규칙과 무관"
    if not any(rnd.get("no") == 1 for rnd in ledger.get("rounds", [])):
        return "1회차 없음"
    return None


def first_round(ledger: dict) -> dict:
    return next(rnd for rnd in ledger["rounds"] if rnd["no"] == 1)


def finding_ids(rnd: dict) -> list[str]:
    held = {f["id"] for f in rnd.get("hold_findings") or []}
    return [f["id"] for f in rnd.get("findings", []) if f["id"] not in held]


def adjudication_ids(rd: Path) -> set[str]:
    ids = set()
    for args_name, _ in ADJ_ARTIFACTS:
        data = read_optional(rd / args_name)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        ids.update(job["id"] for job in jobs or [] if isinstance(job, dict) and isinstance(job.get("id"), str))
    return ids


def adjudicator_id(fid: str) -> str:
    return f"{fid}:{ADJUDICATOR}"


def repo_config(base: Path, ledger: dict) -> RepoConfig:
    try:
        cfg = load_repo_config(Path(ledger["root"]), base / "repos.toml")
    except (ConfigError, tomllib.TOMLDecodeError) as e:
        raise RecalcError(f"repos.toml을 읽지 못했다: {e}") from None
    if cfg is None or not cfg.tests:
        raise RecalcError(f"repos.toml에 {ledger['root']} 의 검증 명령(tests)이 없다. judge는 이 설정 없이 돌 수 없다", "config")
    return cfg


def worker(results: dict, fid: str, role: str) -> WorkerResult:
    data = results.get(f"{fid}:{role}")
    if not isinstance(data, dict):
        raise RecalcError(f"results.json에 {fid}:{role} 결과가 없다")
    return WorkerResult.from_dict(data)


def judged(verify: dict, fid: str, role: str) -> list[VerifyResult]:
    entries = (verify.get(fid) or {}).get(role)
    if not isinstance(entries, list):
        raise RecalcError(f"verify.json에 {fid}의 {role} 검증이 없다")
    return [VerifyResult(**e) for e in entries if not e["cmd"].startswith(cli_steps.BASELINE_TAG)]


def decide_with(fid: str, results: dict, verify: dict, cfg: RepoConfig, adopt_on_both_pass: bool) -> Decision:
    a, b = worker(results, fid, "a"), worker(results, fid, "b")
    sensitive = cli_steps.is_sensitive(changed_files(a.patch) + changed_files(b.patch), cfg)
    d = decide(a, b, judged(verify, fid, "a"), judged(verify, fid, "b"), a.patch, b.patch, sensitive, adopt_on_both_pass)
    return cli_steps.without_empty_patch(d, {"a": a.patch, "b": b.patch}, adjudicated=False)


def without_lossy(d: Decision, lossy, fid: str) -> tuple[Decision, bool]:
    """(결정, 손실 여부를 알 수 없었는지). 판단은 cli_steps.without_lossy_patch와 같되 worktree 대신 lossy.json을 읽는다."""
    if not cli_steps.is_adopted(d.to_dict()):
        return d, False
    flag = ((lossy or {}).get(fid) or {}).get(d.patch_from)
    if flag is None:
        return d, True
    if flag:
        return Decision(d.finding_id, OUTCOME_UNDECIDABLE, None, [*d.reasons, cli_steps.REASON_NON_UTF8]), False
    return d, False


def recompute(base: Path, ledger: dict) -> list[dict]:
    if ledger.get("adopt_on_both_pass"):
        raise RecalcError("원장의 adopt_on_both_pass가 이미 참이다. 채택 규칙을 끄고 돌린 결과 폴더를 준다", "adopt")
    rd = base / "run" / ROUND_DIR
    rnd = first_round(ledger)
    try:
        results, verify = read_json(rd / "results.json"), read_json(rd / "verify.json")
    except (OSError, ValueError) as e:
        raise RecalcError(f"1회차 결과를 읽지 못했다: {e}") from None
    lossy, cfg, adjudicated = read_optional(rd / LOSSY_FILE), repo_config(base, ledger), adjudication_ids(rd)
    stored = {d["finding_id"]: d for d in rnd.get("decisions", [])}
    rows = []
    for fid in finding_ids(rnd):
        off, unknown_off = without_lossy(decide_with(fid, results, verify, cfg, False), lossy, fid)
        on, unknown_on = without_lossy(decide_with(fid, results, verify, cfg, True), lossy, fid)
        rows.append({"item": base.name, "finding_id": fid, "ledger": stored.get(fid), "off": off.to_dict(), "on": on.to_dict(),
                     "flipped": (off.outcome, off.patch_from) != (on.outcome, on.patch_from),
                     "adjudication_in_ledger": adjudicator_id(fid) in adjudicated,
                     "lossy_unknown": unknown_on and cli_steps.is_adopted(on.to_dict())})
    return rows


def raw_usage_rows(data) -> list[dict]:
    workflows = data.get("workflows") if isinstance(data, dict) else None
    return [row for share in (workflows.values() if isinstance(workflows, dict) else []) if isinstance(share, list)
            for row in share if isinstance(row, dict)]


def token_sum(rows: list[dict]) -> dict[str, int | float]:
    return {key: sum(stored_number(row.get(key)) or 0 for row in rows) for key in TOKEN_KEYS}


def replace_decision(decisions: list, new: dict) -> list:
    return [{**new, "adopted_as": None} if d.get("finding_id") == new["finding_id"] else d for d in decisions]


def remaining_workers_event(event: dict, results: dict) -> dict:
    """남은 판가름 결과로 workers-adj 이벤트의 건수·토큰·대체 실행 집계를 다시 계산한다."""
    replaced = {o["id"]: o for o in results.get("replaced", [])}
    second = [o for o in results.get("outcomes", []) if o["id"] in replaced]
    first = [replaced.get(o["id"], o) for o in results.get("outcomes", [])]
    fresh = {
        "counts": dict(Counter(o["status"] for o in first)),
        "usage": usage_totals([Outcome(**o) for o in [*first, *second]]),
        "fallback": [o["id"] for o in first if o["status"] != STATUS_OK],
        "fallback_runs": {job_id: spec["runner"] for job_id, spec in results.get("fallback_runs", {}).items()},
    }
    return {**event, **{key: value for key, value in fresh.items() if key in event}}


def rewrite_ledger(path: Path, flipped: list[dict], drop_event: bool, rd: Path) -> None:
    if not path.is_file():
        return
    ledger = read_json(path)
    rnd = first_round(ledger)
    for row in flipped:
        rnd["decisions"] = replace_decision(rnd["decisions"], row["on"])
    if drop_event:
        dropped = {STEP_ADJUDICATE, STEP_WORKERS_ADJ}
        rnd["events"] = [e for e in rnd.get("events", []) if e.get("step") not in dropped]
    else:
        results = read_optional(rd / ADJ_CLI_RESULTS)
        if isinstance(results, dict):
            rnd["events"] = [remaining_workers_event(e, results) if e.get("step") == STEP_WORKERS_ADJ else e
                             for e in rnd.get("events", [])]
    write_json(path, ledger)


def rewrite_decisions(path: Path, flipped: list[dict]) -> None:
    if path.is_file():
        decisions = read_json(path)
        for row in flipped:
            decisions = replace_decision(decisions, row["on"])
        write_json(path, decisions)


def drop_adjudications(rd: Path, dropped: set[str]) -> bool:
    """판가름 작업과 결과를 지운다. 이 회차에 판가름이 하나도 안 남으면 True."""
    had_args = False
    for args_name, results_name in ADJ_ARTIFACTS:
        args = read_optional(rd / args_name)
        if not isinstance(args, dict):
            continue
        had_args = True
        jobs = [job for job in args.get("jobs", []) if job.get("id") not in dropped]
        results = read_optional(rd / results_name)
        if jobs:
            write_json(rd / args_name, {**args, "jobs": jobs})
            if isinstance(results, dict):
                results = {**results, "results": [r for r in results.get("results", []) if r.get("id") not in dropped]}
                for key in ("outcomes", "replaced"):
                    if key in results:
                        results[key] = [r for r in results[key] if r.get("id") not in dropped]
                if "fallback_runs" in results:
                    results["fallback_runs"] = {k: v for k, v in results["fallback_runs"].items() if k not in dropped}
                write_json(rd / results_name, results)
        else:
            (rd / args_name).unlink()
            (rd / results_name).unlink(missing_ok=True)
    return had_args and not adjudication_ids(rd)


def drop_usage(rd: Path, dropped: set[str]) -> None:
    data = read_optional(rd / "claude-usage.json")
    workflows = data.get("workflows") if isinstance(data, dict) else None
    if not isinstance(workflows, dict):
        return
    data["workflows"] = {wf: [r for r in share if not (isinstance(r, dict) and r.get("job_id") in dropped)]
                         if isinstance(share, list) else share for wf, share in workflows.items()}
    write_json(rd / "claude-usage.json", data)


def flip_item(out_base: Path, flipped: list[dict]) -> None:
    rd = out_base / "run" / ROUND_DIR
    dropped = {adjudicator_id(row["finding_id"]) for row in flipped}
    results = read_json(rd / "results.json")
    write_json(rd / "results.json", {k: v for k, v in results.items() if k not in dropped})
    drop_usage(rd, dropped)
    drop_event = drop_adjudications(rd, dropped)
    for ledger_path in (out_base / "ledger.json", out_base / "run" / "ledger.json"):
        rewrite_ledger(ledger_path, flipped, drop_event, rd)
    for name in ("decisions.json", PRE_FINAL_DECISIONS):
        rewrite_decisions(rd / name, flipped)


def mismatch(item: str, check: str, reason: str) -> dict:
    return {"item": item, "check": check, "reason": reason}


def check_item(base: Path, out_base: Path, rows: list[dict]) -> tuple[list[dict], dict]:
    """검증 셋: 끈 결과의 판가름 분기가 원장과 같다 / 바뀐 항목 수 = 빠진 판가름 수 / 뺀 토큰 = 판가름 토큰 합."""
    item, found = base.name, []
    for row in rows:
        off_adjudicates = row["off"]["outcome"] == OUTCOME_ADJUDICATE
        if off_adjudicates != row["adjudication_in_ledger"]:
            found.append(mismatch(item, "branch", f"{row['finding_id']}: 끈 재계산은 {row['off']['outcome']}인데 원장의 "
                                  f"판가름 작업은 {'있다' if row['adjudication_in_ledger'] else '없다'}"))
        if row["flipped"] and row["lossy_unknown"]:
            found.append(mismatch(item, "lossy", f"{row['finding_id']}: 손실 여부 미확인({LOSSY_FILE}에 기록이 없다)"))
    flipped = [row for row in rows if row["flipped"]]
    rd, out_rd = base / "run" / ROUND_DIR, out_base / "run" / ROUND_DIR
    removed_jobs = len(adjudication_ids(rd) - adjudication_ids(out_rd))
    if len(flipped) != removed_jobs:
        found.append(mismatch(item, "flipped", f"바뀐 결정 {len(flipped)}건, 빠진 판가름 {removed_jobs}건"))
    before = raw_usage_rows(read_optional(rd / "claude-usage.json"))
    after = raw_usage_rows(read_optional(out_rd / "claude-usage.json"))
    prefixes = tuple(f"{row['finding_id']}:" for row in flipped)
    expected = token_sum([r for r in before if r.get("cell") == f"{MODE_CODE}.{ADJUDICATOR}"
                          and str(r.get("job_id", "")).startswith(prefixes)])
    removed = {key: token_sum(before)[key] - token_sum(after)[key] for key in TOKEN_KEYS}
    if removed != expected:
        found.append(mismatch(item, "tokens", f"뺀 토큰 {removed}, 판가름 토큰 합 {expected}"))
    return found, {"removed_jobs": removed_jobs, "tokens": removed}


def copy_runs(runs: Path, out: Path) -> None:
    target = out.resolve()

    def ignore(directory, names):
        here = Path(directory).resolve()
        return [n for n in names if (here / n).resolve() == target or (here.parent == runs.resolve() and n == "wt")]

    shutil.copytree(runs, out, ignore=ignore, dirs_exist_ok=True)


def recalc_item(base: Path, out_base: Path) -> str | tuple[list[dict], list[dict], dict]:
    """건너뛴 항목은 사유 문자열, 아니면 (지적별 행, 어긋난 것, 측정값)."""
    reason = skip_reason(base)
    if reason:
        return reason
    rows = recompute(base, read_json(base / "ledger.json"))
    flipped = [row for row in rows if row["flipped"]]
    if flipped:
        flip_item(out_base, flipped)
    found, measured = check_item(base, out_base, rows)
    return rows, found, measured


def mark_errors(out: Path, mismatches: list[dict]) -> None:
    """검증에 실패한 항목은 끈 결과(또는 반쯤 바뀐 결과)가 켠 항목으로 채점되지 않게 오류 항목으로 적는다."""
    for item in dict.fromkeys(m["item"] for m in mismatches):
        meta_path = out / item / "meta.json"
        meta = read_optional(meta_path)
        reasons = "; ".join(f"[{m['check']}] {m['reason']}" for m in mismatches if m["item"] == item)
        write_json(meta_path, {**(meta if isinstance(meta, dict) else {}), "error": f"채택 규칙 재계산 실패: {reasons}"})


def recalc(runs: Path, out: Path) -> tuple[list[dict], dict]:
    rows, mismatches, skipped = [], [], []
    removed_jobs, tokens = 0, dict.fromkeys(TOKEN_KEYS, 0)
    for base in sorted(p for p in runs.iterdir() if (p / "meta.json").is_file()):
        try:
            result = recalc_item(base, out / base.name)
            if isinstance(result, str):
                skipped.append({"item": base.name, "reason": result})
                continue
            item_rows, found, measured = result
        except RecalcError as e:
            item_rows, found, measured = [], [mismatch(base.name, e.check, str(e))], {"removed_jobs": 0, "tokens": dict.fromkeys(TOKEN_KEYS, 0)}
        except ITEM_ERRORS as e:
            reason = f"{type(e).__name__}: {e}"
            item_rows, found, measured = [], [mismatch(base.name, "read", f"결과를 읽지 못했다({reason})")], {"removed_jobs": 0, "tokens": dict.fromkeys(TOKEN_KEYS, 0)}
        rows += item_rows
        mismatches += found
        removed_jobs += measured["removed_jobs"]
        tokens = {key: tokens[key] + measured["tokens"][key] for key in TOKEN_KEYS}
    mark_errors(out, mismatches)
    flipped_count = sum(row["flipped"] for row in rows)
    check = {"ok": not mismatches, "cost_scope": COST_CORE, "items": len({row["item"] for row in rows}), "flipped": flipped_count,
             "removed_adjudications": removed_jobs, "removed_tokens": tokens, "mismatches": mismatches, "skipped": skipped}
    return rows, check


def report(check: dict) -> str:
    lines = [f"항목 {check['items']}건 재계산, 건너뜀 {len(check['skipped'])}건",
             f"바뀐 항목 {check['flipped']}건, 빠진 판가름 {check['removed_adjudications']}건",
             "뺀 토큰: " + ", ".join(f"{key} {value}" for key, value in check["removed_tokens"].items())]
    lines += [f"건너뜀 {row['item']}: {row['reason']}" for row in check["skipped"]]
    if check["ok"]:
        lines.append("검증 통과")
    else:
        lines.append(f"검증 실패 {len(check['mismatches'])}건")
        lines += [f"  {m['item']} [{m['check']}] {m['reason']}" for m in check["mismatches"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="채택 규칙(둘 다 검증 통과면 한쪽 채택)을 켠 결과를 모델 호출 없이 다시 계산한다")
    parser.add_argument("--runs", required=True, help="채택 규칙을 끄고 돌린 replay 결과 폴더")
    parser.add_argument("--out", required=True, help="켬 값으로 바꾼 채점용 결과 폴더(비어 있거나 없어야 한다. --runs 안에 둬도 된다)")
    args = parser.parse_args(argv)
    runs, out = Path(args.runs), Path(args.out)
    if not runs.is_dir():
        parser.error(f"--runs 폴더가 없다: {runs}")
    if (runs / COUNTERFACTUAL_FILE).is_file():
        parser.error(f"--runs가 이미 반사실 폴더다({COUNTERFACTUAL_FILE}). 채택 규칙을 끈 실제 결과 폴더를 준다")
    if out.exists() and any(out.iterdir()):
        parser.error(f"--out 폴더가 비어 있지 않다: {out}")
    copy_runs(runs, out)
    rows, check = recalc(runs, out)
    write_json(out / COUNTERFACTUAL_FILE, {"cost_scope": COST_CORE})
    (out / "decisions.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    write_json(out / "check.json", check)
    print(report(check))
    return 0 if check["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
