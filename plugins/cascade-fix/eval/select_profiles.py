import argparse
import json
import sys
from pathlib import Path

from score import MAX_ACCURACY_DROP, MAX_FALLBACK, MAX_UNFINISHED, SELECTION_LABELS, money, ratio_text

COST_TIE = 0.10
NOT_APPLICABLE = "해당 없음"
UNMEASURED = "미측정"
COST_EXCLUDED = "비용 미측정으로 제외"
UNSTABLE = "불안정"
METRICS = ("accuracy", "false_fact", "completion", "fallback_ratio", "regressions", "claude_cost", "to_human", "opus_calls")
NUMBER_KEYS = METRICS + ("regressions",)
TABLE_HEADER = ("후보", "정확도", "「아님」 오채택", "완주", "대체 실행", "회귀 실행", "Opus 호출", "Claude 비용", "사람 목록",
                "판정 흔들림")


def stats(values: list) -> dict | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return {"mean": sum(present) / len(present), "min": min(present), "max": max(present)}


def mean_of(metrics: dict, key: str) -> float | None:
    return metrics[key]["mean"] if metrics[key] else None


def flip_rate(predicted_list: list[dict]) -> float:
    items = {item for predicted in predicted_list for item in predicted}
    if not items:
        return 0.0
    flipped = sum(len({predicted.get(item) for predicted in predicted_list}) > 1 for item in items)
    return flipped / len(items)


def run_check(run: dict, key: str) -> dict:
    return ((run.get("selection") or {}).get("checks") or {}).get(key) or {}


def accuracy_check(metrics: dict, base: dict) -> dict:
    cand, ref = mean_of(metrics, "accuracy"), mean_of(base, "accuracy")
    ok = cand is not None and ref is not None and round(ref - cand, 6) <= MAX_ACCURACY_DROP
    return {"ok": ok, "value": cand, "baseline": ref, "limit": MAX_ACCURACY_DROP}


def false_fact_check(metrics: dict, base: dict) -> dict:
    cand, ref = mean_of(metrics, "false_fact"), mean_of(base, "false_fact")
    return {"ok": cand is not None and ref is not None and round(cand - ref, 6) <= 0, "value": cand, "baseline": ref}


def completion_check(metrics: dict) -> dict:
    done = mean_of(metrics, "completion")
    unfinished = None if done is None else 1 - done
    return {"ok": unfinished is not None and round(unfinished, 6) <= MAX_UNFINISHED, "value": unfinished, "limit": MAX_UNFINISHED}


def fallback_check(metrics: dict) -> dict:
    ratio = mean_of(metrics, "fallback_ratio")
    return {"ok": ratio is None or round(ratio, 6) <= MAX_FALLBACK, "value": ratio, "limit": MAX_FALLBACK}


def regression_failed(run: dict) -> bool:
    return run_check(run, "regression").get("ok") is False or (run.get("regression_measured") is True and (run.get("regressions") or 0) > 0)


def regression_measured(run: dict) -> bool:
    return run_check(run, "regression").get("ok") is not None or run.get("regression_measured") is True


def regression_check(runs: list[dict]) -> dict:
    count = sum(regression_failed(run) for run in runs)
    if count:
        return {"ok": False, "value": count, "runs": len(runs)}
    return {"ok": True if any(regression_measured(run) for run in runs) else None, "value": 0, "runs": len(runs)}


CHECK_DETAILS = {
    "accuracy": lambda c: f"평균 {ratio_text(c['value'])}, 기준선 평균 {ratio_text(c['baseline'])}에서 {c['limit'] * 100:.0f}%p 이내",
    "false_fact": lambda c: f"평균 {c['value']}건, 기준선 평균 {c['baseline']}건 이하",
    "completion": lambda c: f"평균 미완료 {ratio_text(c['value'])}, {c['limit'] * 100:.0f}% 이하",
    "fallback": lambda c: f"평균 {ratio_text(c['value'])}, {c['limit'] * 100:.0f}% 이하",
    "regression": lambda c: f"회귀 난 실행 {c['value']}/{c['runs']}",
}


def check_reason(key: str, check: dict) -> str | None:
    label = SELECTION_LABELS[key]
    if key == "fallback" and check["value"] is None:
        return f"{label} {NOT_APPLICABLE}"
    if check["ok"] is None:
        return f"{label} {UNMEASURED}"
    if check["ok"]:
        return None
    return f"{label} 미달({CHECK_DETAILS[key](check)})"


def unstable_runs(runs: list[dict]) -> int:
    return sum(run_check(run, "accuracy").get("ok") is False for run in runs)


def is_unstable(runs: list[dict], unstable: int) -> bool | None:
    """한 번이라도 조건 미달이면 불안정. 모든 실행의 정확도 조건이 기록돼 있지 않으면 안정하다고 말하지 않고 None(미측정)."""
    if unstable:
        return True
    return False if all(run_check(run, "accuracy").get("ok") is not None for run in runs) else None


def cost_mean(runs: list[dict]) -> float | None:
    costs = [run.get("claude_cost") for run in runs]
    return None if not costs or any(cost is None for cost in costs) else sum(costs) / len(costs)


def summarize(runs: list[dict], base_metrics: dict | None) -> dict:
    metrics = {key: stats([run.get(key) for run in runs]) for key in METRICS}
    base = metrics if base_metrics is None else base_metrics
    checks = {"accuracy": accuracy_check(metrics, base), "false_fact": false_fact_check(metrics, base),
              "completion": completion_check(metrics), "fallback": fallback_check(metrics), "regression": regression_check(runs)}
    unstable = unstable_runs(runs)
    unstable_state = is_unstable(runs, unstable)
    cost = cost_mean(runs)
    reasons = [reason for key, check in checks.items() if (reason := check_reason(key, check))]
    if unstable:
        reasons.append(f"정확도 {UNSTABLE}(실행 {len(runs)}회 중 {unstable}회 조건 미달)")
    elif unstable_state is None:
        reasons.append(f"정확도 안정성 {UNMEASURED}")
    if cost is None:
        reasons.append(COST_EXCLUDED)
    return {"runs": len(runs), "metrics": metrics, "flip_rate": flip_rate([run.get("predicted") or {} for run in runs]),
            "checks": checks, "passed": all(check["ok"] is not False for check in checks.values()), "unstable": unstable_state,
            "unstable_runs": unstable, "cost_mean": cost, "reasons": reasons}


def to_human_rank(summary: dict) -> float:
    mean = mean_of(summary["metrics"], "to_human")
    return float("inf") if mean is None else mean


def pick(summaries: dict[str, dict], eligible: list[str]) -> str | None:
    priced = [name for name in eligible if summaries[name]["cost_mean"] is not None]
    if not priced:
        return None
    lowest = min(summaries[name]["cost_mean"] for name in priced)
    near = [name for name in priced if round(summaries[name]["cost_mean"] - lowest * (1 + COST_TIE), 6) <= 0]
    return min(near, key=lambda name: (to_human_rank(summaries[name]), summaries[name]["cost_mean"], name))


def decide(cands: dict[str, list[dict]], baseline: str) -> dict:
    base_metrics = {key: stats([run.get(key) for run in cands[baseline]]) for key in METRICS}
    summaries = {name: summarize(runs, base_metrics) for name, runs in cands.items()}
    eligible = [name for name, summary in summaries.items() if summary["passed"]]
    return {"baseline": baseline, "candidates": summaries, "eligible": eligible, "selected": pick(summaries, eligible)}


def spread(stat: dict | None, fmt) -> str:
    return UNMEASURED if stat is None else f"{fmt(stat['mean'])} [{fmt(stat['min'])}~{fmt(stat['max'])}]"


def number(value: float) -> str:
    return f"{value:g}"


def row(name: str, summary: dict) -> tuple[str, ...]:
    m = summary["metrics"]
    regressions = summary["checks"]["regression"]
    stability = {True: f" ({UNSTABLE})", None: f" (안정성 {UNMEASURED})"}.get(summary["unstable"], "")
    return (name + stability,
            spread(m["accuracy"], ratio_text), spread(m["false_fact"], number), spread(m["completion"], ratio_text),
            NOT_APPLICABLE if m["fallback_ratio"] is None else spread(m["fallback_ratio"], ratio_text),
            UNMEASURED if regressions["ok"] is None else f"{regressions['value']}/{regressions['runs']}",
            spread(m["opus_calls"], number), spread(m["claude_cost"], money) if summary["cost_mean"] is not None else COST_EXCLUDED,
            spread(m["to_human"], number), ratio_text(summary["flip_rate"]))


def table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    return [f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * len(header))} |", *(f"| {' | '.join(r)} |" for r in rows)]


def reason_line(name: str, summary: dict) -> str:
    verdict = "통과" if summary["passed"] else "거름"
    return f"- {name}: {verdict}" + (f" — {' · '.join(summary['reasons'])}" if summary["reasons"] else "")


def selection_line(result: dict) -> str:
    selected = result["selected"]
    if selected is None:
        return "선정: 없음 — 조건을 넘고 비용이 측정된 후보가 없다"
    summary = result["candidates"][selected]
    to_human = mean_of(summary["metrics"], "to_human")
    human_text = UNMEASURED if to_human is None else f"평균 {number(to_human)}"
    return f"선정: {selected} — Claude 비용 평균 {money(summary['cost_mean'])}, 사람 목록 {human_text}"


def render(result: dict) -> str:
    summaries = result["candidates"]
    rows = [row(name, summary) for name, summary in summaries.items()]
    counts = ", ".join(f"{name} {summary['runs']}회" for name, summary in summaries.items())
    lines = [f"기준선: {result['baseline']}", "", f"후보별 평균 [최소~최대] (실행 수: {counts})",
             "", *table(TABLE_HEADER, rows), "", "거름 사유", "", *(reason_line(name, s) for name, s in summaries.items()), "",
             selection_line(result), ""]
    return "\n".join(lines)


class InputError(Exception):
    pass


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def invalid_key(data: dict) -> str | None:
    wrong = [key for key in NUMBER_KEYS if data.get(key) is not None and not is_number(data[key])]
    if data.get("predicted") is not None and not isinstance(data["predicted"], dict):
        wrong.append("predicted")
    selection = data.get("selection")
    checks = selection.get("checks") if isinstance(selection, dict) else None
    if selection is not None and not isinstance(selection, dict):
        wrong.append("selection")
    elif isinstance(selection, dict) and checks is not None and not (
            isinstance(checks, dict) and all(isinstance(check, dict) for check in checks.values())):
        wrong.append("selection.checks")
    return wrong[0] if wrong else None


def load_run(name: str, path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"후보 {name}의 점수 JSON을 읽지 못했다: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise InputError(f"후보 {name}의 점수 JSON이 객체가 아니다: {path}")
    if (wrong := invalid_key(data)) is not None:
        raise InputError(f"후보 {name}의 점수 JSON에서 {wrong} 값의 형식이 맞지 않는다: {path}")
    return data


def parse_candidate(text: str) -> tuple[str, list[str]]:
    name, sep, paths = text.partition("=")
    files = [p for p in paths.split(",") if p]
    if not sep or not name or not files:
        raise argparse.ArgumentTypeError(f"--candidate는 <이름>=<JSON>[,<JSON>…] 형식이다: {text}")
    return name, files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="반복 실행한 후보 프로필을 평균으로 걸러 Claude 비용이 가장 적은 것을 고른다")
    parser.add_argument("--candidate", action="append", required=True, type=parse_candidate, metavar="이름=JSON[,JSON…]",
                        help="후보 이름과 실행마다의 score.py --json 결과(쉼표로 이음). 여러 번 줄 수 있다")
    parser.add_argument("--baseline", required=True, help="기준선 후보 이름(--candidate 중 하나)")
    parser.add_argument("--json", help="선정 결과를 JSON으로 쓴다")
    args = parser.parse_args(argv)
    names = [name for name, _ in args.candidate]
    if len(set(names)) != len(names):
        parser.error("--candidate 이름이 겹친다")
    if args.baseline not in names:
        parser.error(f"--baseline은 후보 중 하나여야 한다: {args.baseline} (후보: {', '.join(names)})")
    try:
        cands = {name: [load_run(name, path) for path in paths] for name, paths in args.candidate}
    except InputError as exc:
        print(exc, file=sys.stderr)
        return 1
    result = decide(cands, args.baseline)
    print(render(result))
    if args.json:
        try:
            Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"--json 파일을 쓰지 못했다: {args.json} ({exc})", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
