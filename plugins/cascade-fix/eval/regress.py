import argparse
import json
import sys
from pathlib import Path

from score import is_regression, measured_checks

LEGACY_PROFILE = "legacy"
UNKNOWN_PROFILE = "프로필 불명"
STOP_RUNS, STOP_PATCHES = 10, 50
REACHED_RUNS, REACHED_PATCHES, REACHED_BOTH = "runs", "patches", "both"
SKIP_DRY_RUN = "dry-run 실행이라 세지 않는다"
SKIP_UNREADABLE = "읽지 못한 실행이라 프로필을 알 수 없다"
SKIP_NOT_IN_RUNS = "--runs에 없는 실행이다"
TABLE_COLUMNS = (("프로필", "---"), ("실행", "---:"), ("dry-run 제외", "---:"), ("채택 패치", "---:"), ("간섭 회귀 실행", "---:"),
                 ("회귀 회차", "---:"), ("기준선도 실패", "---:"), ("되돌림", "---:"), ("회귀", "---:"))


class RegressError(Exception):
    pass


class Result(dict):
    def __init__(self, stop_runs: int, stop_patches: int):
        super().__init__()
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.stop = {"runs": stop_runs, "patches": stop_patches}

    def to_dict(self) -> dict:
        return {"profiles": dict(self), "errors": self.errors, "warnings": self.warnings, "stop": self.stop}


def read_json(path: Path, run_id: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegressError(f"{run_id}: {path.parent.name}/{path.name}을 읽지 못했다 ({exc})") from None


def profile_name(ledger: dict) -> str:
    profile = ledger.get("profile")
    name = profile.get("name") if isinstance(profile, dict) else None
    return name if isinstance(name, str) and name else LEGACY_PROFILE


def check_apply(run_id: str, apply) -> dict:
    if not isinstance(apply, dict) or not isinstance(apply.get("applied", []), list):
        raise RegressError(f"{run_id}: apply.json 형식이 맞지 않는다(applied는 목록이어야 한다)")
    history = apply.get("final_history")
    if history is not None and (not isinstance(history, list) or not all(isinstance(h, dict) for h in history)):
        raise RegressError(f"{run_id}: apply.json 형식이 맞지 않는다(final_history는 객체 목록이어야 한다)")
    return apply


def load_folder(folder: Path) -> dict:
    folder = folder.resolve()
    ledger = read_json(folder / "ledger.json", folder.name)
    if not isinstance(ledger, dict):
        raise RegressError(f"{folder.name}: ledger.json이 객체가 아니다")
    run = {"id": folder.name, "ledger": ledger, "applies": []}
    if not ledger.get("dry_run"):
        run["applies"] = [check_apply(folder.name, read_json(path, folder.name)) for path in sorted(folder.glob("round-*/apply.json"))]
    return run


def load_run(entry) -> dict:
    if isinstance(entry, dict):
        return {**entry, "applies": [check_apply(entry.get("id", "?"), a) for a in entry.get("applies", [])]}
    return load_folder(Path(entry))


def new_stats(stop: bool) -> dict:
    stats = {"runs": 0, "dry_run_excluded": 0, "applied": 0, "interference": 0, "regression_rounds": 0, "baseline_failed": 0,
             "reverts": 0, "regressions": 0, "regression_runs": []}
    if stop:
        stats["stop"] = {"reached": None}
    return stats


def baseline_failed(apply: dict) -> bool:
    return any(h.get("final_ok") is False and h.get("baseline_failed") for h in measured_checks(apply))


def update_stop(stats: dict, stop_runs: int, stop_patches: int) -> None:
    if stats["stop"]["reached"] is not None:
        return
    hit = (stats["runs"] >= stop_runs, stats["applied"] >= stop_patches)
    if any(hit):
        stats["stop"]["reached"] = REACHED_BOTH if all(hit) else REACHED_RUNS if hit[0] else REACHED_PATCHES


def add_run(stats: dict, run: dict, stop_runs: int, stop_patches: int) -> bool:
    regressions = [is_regression(a) for a in run["applies"]]
    stats["runs"] += 1
    stats["applied"] += sum(len(a.get("applied", [])) for a in run["applies"])
    stats["regression_rounds"] += sum(regressions)
    stats["interference"] += any(regressions)
    stats["baseline_failed"] += any(baseline_failed(a) for a in run["applies"])
    update_stop(stats, stop_runs, stop_patches)
    return any(regressions)


def revert_run_id(row) -> str:
    if not isinstance(row, dict) or not isinstance(row.get("run"), str) or not row["run"]:
        raise RegressError(f"되돌림 줄 형식이 맞지 않는다(run이 문자열이어야 한다): {json.dumps(row, ensure_ascii=False)}")
    return row["run"]


def add_unknown_revert(r: Result, hits: dict[str, set[str]], run_id: str, reason: str) -> None:
    r.warnings.append(f"되돌림의 run {run_id}: {reason}. {UNKNOWN_PROFILE}으로 센다")
    r.setdefault(UNKNOWN_PROFILE, new_stats(False))["reverts"] += 1
    hits.setdefault(UNKNOWN_PROFILE, set()).add(run_id)


def apply_reverts(r: Result, reverts: list, ids: dict[str, str], skipped: dict[str, str], hits: dict[str, set[str]]) -> None:
    for row in reverts:
        try:
            run_id = revert_run_id(row)
        except RegressError as exc:
            r.errors.append(str(exc))
            continue
        if run_id in ids:
            r[ids[run_id]]["reverts"] += 1
            hits[ids[run_id]].add(run_id)
        elif skipped.get(run_id) == SKIP_DRY_RUN:
            r.warnings.append(f"되돌림의 run {run_id}: dry-run 실행이라 세지 않는다")
        else:
            add_unknown_revert(r, hits, run_id, skipped.get(run_id, SKIP_NOT_IN_RUNS))


def entry_id(entry) -> str:
    return str(entry.get("id", "?")) if isinstance(entry, dict) else Path(entry).resolve().name


def compute(runs: list, reverts=None, *, stop_runs: int = STOP_RUNS, stop_patches: int = STOP_PATCHES) -> Result:
    r = Result(stop_runs, stop_patches)
    ids: dict[str, str] = {}
    skipped: dict[str, str] = {}
    hits: dict[str, set[str]] = {}
    for entry in runs:
        try:
            run = load_run(entry)
            if run["id"] in ids or run["id"] in skipped:
                raise RegressError(f"{run['id']}: 같은 이름의 실행 폴더가 이미 있다(run id가 겹친다)")
        except RegressError as exc:
            r.errors.append(str(exc))
            skipped.setdefault(entry_id(entry), SKIP_UNREADABLE)
            continue
        name = profile_name(run["ledger"])
        stats = r.setdefault(name, new_stats(True))
        hits.setdefault(name, set())
        if run["ledger"].get("dry_run"):
            stats["dry_run_excluded"] += 1
            skipped[run["id"]] = SKIP_DRY_RUN
            continue
        ids[run["id"]] = name
        if add_run(stats, run, stop_runs, stop_patches):
            hits[name].add(run["id"])
    apply_reverts(r, reverts or [], ids, skipped, hits)
    for name, found in hits.items():
        r[name]["regression_runs"] = sorted(found)
        r[name]["regressions"] = len(found)
    return r


def table_row(cells) -> str:
    return f"| {' | '.join(str(c) for c in cells)} |"


def stats_cells(name: str, s: dict) -> list:
    return [name, s["runs"], s["dry_run_excluded"], s["applied"], s["interference"], s["regression_rounds"], s["baseline_failed"],
            s["reverts"], s["regressions"]]


def reached_text(reached: str | None, stop: dict) -> str:
    if reached == REACHED_BOTH:
        return f"실행 {stop['runs']}건과 채택 패치 {stop['patches']}건에 동시에 닿음"
    if reached == REACHED_RUNS:
        return f"실행 {stop['runs']}건에 먼저 닿음"
    if reached == REACHED_PATCHES:
        return f"채택 패치 {stop['patches']}건에 먼저 닿음"
    return "아직 종료 조건에 닿지 않음"


def progress_line(name: str, s: dict, stop: dict) -> str:
    return (f"- {name}: 실행 {stop['runs']}건 중 {s['runs']}건, 채택 패치 {stop['patches']}건 중 {s['applied']}건 — "
            f"{reached_text(s['stop']['reached'], stop)}")


def render(r: dict) -> str:
    names = list(r)
    header = [table_row(name for name, _ in TABLE_COLUMNS), table_row(align for _, align in TABLE_COLUMNS)]
    rows = [table_row(stats_cells(name, r[name])) for name in names]
    progress = [progress_line(name, r[name], r.stop) for name in names if "stop" in r[name]]
    notes = ["회귀 = 전체 검사 회귀가 있었던 실행 ∪ 되돌림이 있었던 실행(합집합 크기). 전체 검사 회귀는 기준선도 실패한 회차를 빼고 세고, 되돌림은 그런 실행도 센다"]
    return "\n".join([*header, *rows, "", "종료 조건 진행(먼저 닿은 쪽에서 측정을 끝낸다. 위 표는 --runs의 실행을 모두 센다)", "", *progress, "", *notes, ""])


def load_reverts(path: str) -> tuple[list, list[str]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [f"되돌림 파일을 읽지 못했다: {path} ({exc})"]
    rows, errors = [], []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            revert_run_id(row)
        except (ValueError, RegressError):
            errors.append(f"{path}:{number}: 되돌림 줄이 {{\"run\": \"<run id>\", …}} 형식의 JSON이 아니다")
            continue
        rows.append(row)
    return rows, errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="실제 실행의 회귀를 프로필별로 센다(전향 측정)")
    parser.add_argument("--runs", nargs="+", required=True,
                        help="실행 폴더(.cascade-fix/<id>)들. 시간 순서로 주면 종료 조건에 먼저 닿은 쪽을 가린다")
    parser.add_argument("--reverts", help="되돌리거나 다시 고친 채택 패치 JSONL. 줄: {\"run\", \"finding\", \"reason\"}")
    parser.add_argument("--stop-runs", type=int, default=STOP_RUNS, help="종료 조건: 프로필별 실행 수")
    parser.add_argument("--stop-patches", type=int, default=STOP_PATCHES, help="종료 조건: 프로필별 채택 패치 수")
    parser.add_argument("--json", help="결과를 JSON으로 쓴다")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rows, errors = load_reverts(args.reverts) if args.reverts else ([], [])
    r = compute(args.runs, rows, stop_runs=args.stop_runs, stop_patches=args.stop_patches)
    r.errors = [*errors, *r.errors]
    print(render(r))
    for message in r.warnings:
        print(f"경고: {message}", file=sys.stderr)
    for message in r.errors:
        print(f"오류: {message}", file=sys.stderr)
    if args.json:
        try:
            Path(args.json).write_text(json.dumps(r.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"--json 파일을 쓰지 못했다: {args.json} ({exc})", file=sys.stderr)
            return 1
    return 1 if r.errors else 0


if __name__ == "__main__":
    sys.exit(main())
