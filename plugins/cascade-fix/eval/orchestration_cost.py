import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
from cascade.usage import CACHE_WRITE_1H_MULTIPLIER, PRICE_SOURCE, PRICES, PRICES_AS_OF, read_session  # noqa: E402

STEP = re.compile(r"(?:cascade\.cli\"?|\bCF)\s+(init|start|go|report)\b")
GO_REPORTED = "멈춤: report까지 끝났다"
RUN_DIR = re.compile(r"^(?:[ \t]*\d+[→\t])?run_dir=(\S+)", re.M)
FINDINGS = re.compile(r"^(?:[ \t]*\d+[→\t])?지적 (\d+)건", re.M)
PROFILE = re.compile(r"^(?:[ \t]*\d+[→\t])?프로필: (\S+)", re.M)
SCOPE = "init(또는 start)를 부른 메시지부터 report 뒤 사람의 다음 입력 직전까지의 메인 세션 메시지(하위 에이전트·detect·첫 검토 제외)"


@dataclass
class Run:
    session: str
    run_dir: str
    findings: int
    profile: str
    dry_run: bool
    start: str
    end: str = ""


def _rows(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("timestamp"):
            rows.append(row)
    return rows


def _content(row: dict) -> list | str | None:
    message = row.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _is_human(row: dict) -> bool:
    content = _content(row)
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(c.get("type") == "tool_result" for c in content if isinstance(c, dict))


def _result_text(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def find_runs(path: Path) -> list[Run]:
    """세션 기록에서 cascade-fix 실행(init~report)을 찾는다. report가 없는 실행은 버린다."""
    runs: list[Run] = []
    calls: dict[str, tuple[str, str, str]] = {}
    current: Run | None = None
    pending_start: str | None = None
    pending_command = ""
    reported = False

    def open_run(text: str, timestamp: str) -> Run:
        findings = FINDINGS.search(text)
        profile = PROFILE.search(text)
        return Run(session=Path(path).stem, run_dir=RUN_DIR.search(text).group(1),
                   findings=int(findings.group(1)) if findings else 0,
                   profile=profile.group(1) if profile else "", dry_run="--dry-run" in pending_command,
                   start=current.start if current else pending_start or timestamp)

    for row in _rows(path):
        if current is not None and reported and _is_human(row):
            runs.append(current)
            current, reported = None, False
        content = _content(row)
        if current is not None and GO_REPORTED in json.dumps(content, ensure_ascii=False):
            reported = True
        if not isinstance(content, list):
            if pending_start is not None and isinstance(content, str) and RUN_DIR.search(content):
                current, pending_start, reported = open_run(content, row["timestamp"]), None, GO_REPORTED in content
            if current is not None and reported:
                current.end = row["timestamp"]
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "Bash":
                command = c.get("input", {}).get("command", "")
                for step in STEP.findall(command):
                    calls[c["id"]] = (step, row["timestamp"], command)
                    if step in ("init", "start"):
                        if current is not None and reported:
                            runs.append(current)
                            current, reported = None, False
                        pending_command = command
                        pending_start = pending_start or row["timestamp"]
            elif c.get("type") == "tool_result":
                text = _result_text(c)
                step, _, command = calls.get(c.get("tool_use_id"), ("", "", ""))
                if step in ("init", "start") and RUN_DIR.search(text):
                    pending_command = command
                    current, pending_start, reported = open_run(text, row["timestamp"]), None, GO_REPORTED in text
                elif step == "report" and current is not None:
                    reported = True
                elif pending_start is not None and RUN_DIR.search(text):
                    current, pending_start, reported = open_run(text, row["timestamp"]), None, GO_REPORTED in text
        if current is not None and reported:
            current.end = row["timestamp"]
    if current is not None and reported:
        runs.append(current)
    return runs


def measure(path: Path) -> list[dict]:
    rows = []
    for run in find_runs(path):
        parts = read_session(path, since=run.start, until=run.end)
        costs = [u.cost for u in parts]
        rows.append({
            **run.__dict__,
            "cost": None if any(c is None for c in costs) else sum(costs),
            "input": sum(u.input for u in parts), "output": sum(u.output for u in parts),
            "cache_read": sum(u.cache_read for u in parts), "cache_write": sum(u.cache_write for u in parts),
            "requests": sum(u.messages for u in parts),
            "by_model": {u.model: u.cost for u in parts},
        })
    return rows


def project(row: dict, removed_requests: int) -> dict:
    """지적당 비용 범위. 캐시 쓰기는 비용 비중이 가장 큰 모델의 1시간 TTL 단가(입력 단가의 2배)로 추정한다. Haiku 장문 단가는 반영하지 않는다."""
    requests = row["requests"]
    if removed_requests < 0 or removed_requests > requests:
        raise ValueError("줄일 요청 수는 0 이상 전체 요청 수 이하여야 한다")
    total, findings = row["cost"], row["findings"]
    if total is None or not requests or not findings:
        return {"lower": None, "upper": None}
    ratio = (requests - removed_requests) / requests
    lower = total * ratio / findings
    if not row["cache_write"]:
        return {"lower": lower, "upper": lower}
    by_model = row["by_model"]
    model = max(by_model, key=lambda m: by_model[m] or 0, default="")
    prices = PRICES.get(model)
    if prices is None:
        return {"lower": lower, "upper": None}
    fixed = min(total, row["cache_write"] * prices["input"] * CACHE_WRITE_1H_MULTIPLIER / 1_000_000)
    return {"lower": lower, "upper": (fixed + (total - fixed) * ratio) / findings}


def unique_runs(rows: list[dict]) -> list[dict]:
    """포크한 세션에 같은 실행이 겹쳐 있으면 하나만 남긴다."""
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        seen.setdefault((row["run_dir"], row["start"]), row)
    return list(seen.values())


def fit(rows: list[dict]) -> dict:
    """진행 비용 = 고정 + 지적당 × 지적 수를 최소제곱으로 맞춘다. 지적 수가 모두 같으면 기울기를 내지 않는다."""
    points = [(r["findings"], r["cost"]) for r in rows if r.get("cost") is not None and r["findings"] > 0]
    if not points:
        return {"runs": 0, "fixed": None, "per_finding": None, "one_finding_run": None, "mean_findings": None,
                "per_finding_at_mean": None, "mean_cost": None}
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx if sxx else None
    fixed = mean_y - slope * mean_x if slope is not None else None
    return {"runs": n, "fixed": fixed, "per_finding": slope,
            "one_finding_run": fixed + slope if slope is not None else None,
            "mean_findings": mean_x, "per_finding_at_mean": sum(y for _, y in points) / sum(x for x, _ in points),
            "mean_cost": mean_y}


def money(value) -> str:
    return "-" if value is None else f"${value:.2f}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="실제 cascade-fix 사용 세션에서 메인 세션의 진행 비용을 추정한다")
    parser.add_argument("--session", action="append", required=True, help="메인 세션 기록 JSONL(여러 번 줄 수 있다)")
    parser.add_argument("--json")
    parser.add_argument("--remove-requests", type=int, help="이 수만큼 요청을 줄였을 때 지적당 비용 범위를 추정한다")
    args = parser.parse_args(argv)
    rows = unique_runs([row for path in args.session for row in measure(Path(path))])
    if args.remove_requests is not None and (args.remove_requests < 0 or any(args.remove_requests > r["requests"] for r in rows)):
        parser.error("줄일 요청 수는 0 이상 각 실행의 전체 요청 수 이하여야 한다")
    result = {"scope": SCOPE, "prices_as_of": PRICES_AS_OF, "price_source": PRICE_SOURCE, "runs": rows, "fit": fit(rows)}
    print("| 세션 | 실행 | 프로필 | dry-run | 지적 | 진행 비용 |")
    print("| --- | --- | --- | --- | ---: | ---: |")
    for r in rows:
        print(f"| {r['session'][:8]} | {Path(r['run_dir']).name} | {r['profile']} | {'예' if r['dry_run'] else '아니오'} "
              f"| {r['findings']} | {money(r['cost'])} |")
        if args.remove_requests is not None:
            estimate = project(r, args.remove_requests)
            print(f"추정: 요청 {args.remove_requests}건 감소 시 지적당 {money(estimate['lower'])}~{money(estimate['upper'])}")
    f = result["fit"]
    print(f"\n맞춤: 고정 {money(f['fixed'])} + 지적당 {money(f['per_finding'])} (실행 {f['runs']}회), "
          f"지적 1건 실행 {money(f['one_finding_run'])}, 평균 규모 {f['mean_findings'] or 0:.1f}건에서 지적당 {money(f['per_finding_at_mean'])}")
    print(f"범위: {SCOPE}")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
