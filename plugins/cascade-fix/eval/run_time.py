import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
from cascade.usage import UsageFormatError, find_workflow_dir  # noqa: E402

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
# cascade.cli_steps.STAGE_INPUTS의 작업 단계만 잰다. CLI 의견은 측정에서 제외한다.
CLI_STAGE_INPUTS = {"workers-stage1": "jobs", "workers-checker": "judge-checker", "workers-adj": "judge"}
SCOPE = ("항목당 처리 시간 합: Claude 에이전트 기록의 처음~끝 시각 + CLI 작업자(원장 단계별 입력→workers-* 간격). "
         "단일 Claude는 세션 시간(duration_ms). 동시 실행으로 겹친 시간도 각각 더한다. 하네스 검증·CLI 의견 시간은 빼고 잰다")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def agent_seconds(workflow_dir: Path, label: str) -> float | None:
    """Workflow에서 그 이름표로 시작한 에이전트 기록의 첫 시각~마지막 시각(초)."""
    agent = next((r["agentId"] for r in _rows(workflow_dir / "journal.jsonl")
                  if r.get("type") == "started" and r.get("label") == label), None)
    path = workflow_dir / f"agent-{agent}.jsonl"
    if agent is None or not path.is_file():
        return None
    stamps = [_time(r["timestamp"]) for r in _rows(path) if isinstance(r.get("timestamp"), str)]
    return (max(stamps) - min(stamps)).total_seconds() if stamps else None


def cli_seconds(ledger: dict) -> float:
    total = 0.0
    for rnd in ledger.get("rounds", []):
        inputs = {}
        for event in rnd.get("events", []):
            step = event.get("step")
            if not event.get("at"):
                continue
            if step in CLI_STAGE_INPUTS.values():
                inputs[step] = _time(event["at"])
            elif step in CLI_STAGE_INPUTS:
                start = inputs.get(CLI_STAGE_INPUTS[step])
                if start is not None:
                    total += (_time(event["at"]) - start).total_seconds()
    return total


def cascade_item(item_dir: Path, projects_root: Path, cache: dict | None = None) -> dict:
    cache = {} if cache is None else cache
    claude, missing = 0.0, []
    for usage_path in sorted(Path(item_dir).glob("run/round-*/claude-usage.json")):
        for wf, rows in json.loads(usage_path.read_text(encoding="utf-8")).get("workflows", {}).items():
            for row in rows:
                if wf not in cache:
                    try:
                        cache[wf] = find_workflow_dir(wf, projects_root)
                    except UsageFormatError:
                        cache[wf] = None
                seconds = agent_seconds(cache[wf], row["label"]) if cache[wf] else None
                if seconds is None:
                    missing.append(f"{wf} {row['label']}")
                else:
                    claude += seconds
    ledger_path = Path(item_dir) / "ledger.json"
    cli = cli_seconds(json.loads(ledger_path.read_text(encoding="utf-8"))) if ledger_path.is_file() else 0.0
    return {"claude_s": claude, "cli_s": cli, "missing": missing}


def summary(items: list[dict]) -> dict:
    total = sum(i["claude_s"] + i["cli_s"] for i in items)
    return {"items": len(items), "claude_s": sum(i["claude_s"] for i in items), "cli_s": sum(i["cli_s"] for i in items),
            "minutes_per_item": total / len(items) / 60 if items else None,
            "missing": [m for i in items for m in i["missing"]], "scope": SCOPE}


def cascade_runs(runs: Path, projects_root: Path) -> dict:
    cache: dict = {}
    items = [cascade_item(d, projects_root, cache) for d in sorted(Path(runs).iterdir())
             if (d / "ledger.json").is_file() or (d / "run").is_dir()]
    return summary(items)


def solo_runs(runs: Path) -> dict:
    items = []
    for path in sorted(Path(runs).glob("*/result.json")):
        ms = json.loads(path.read_text(encoding="utf-8")).get("cli_duration_ms")
        items.append({"claude_s": (ms or 0) / 1000, "cli_s": 0.0, "missing": [] if ms else [path.parent.name]})
    return summary(items)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="구성 실행 한 번의 항목당 처리 시간 합을 낸다")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--cascade", help="replay 결과 폴더(켬 값은 adopt-recalc 폴더)")
    group.add_argument("--solo", help="eval/solo.py 결과 폴더")
    parser.add_argument("--projects-root", default=str(PROJECTS_ROOT))
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    result = cascade_runs(Path(args.cascade), Path(args.projects_root)) if args.cascade else solo_runs(Path(args.solo))
    minutes = result["minutes_per_item"]
    print(f"항목 {result['items']}건, 항목당 {minutes:.1f}분" if minutes is not None else "항목 없음",
          f"(기록 없음 {len(result['missing'])}건)" if result["missing"] else "")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
