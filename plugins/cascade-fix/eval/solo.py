import argparse
import json
import os
import re
import shlex
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cascade import cli_steps, jobs, patches  # noqa: E402
from cascade.config import RepoConfig, load_repo_config  # noqa: E402
from cascade.consensus import VERDICTS  # noqa: E402
from cascade.findings import Finding  # noqa: E402
from cascade.runners import _execute, extract_json_block  # noqa: E402
from cascade.usage import PRICE_SOURCE, PRICES_AS_OF, UsageFormatError, read_session  # noqa: E402
from cascade.verify import run_commands, run_worker_verifiers  # noqa: E402
from replay import git, load_sample, read_json, write_json  # noqa: E402
from score import TRUTH_KEYS, VERDICT_FACT, VERDICT_NO, VERDICT_NONE, VERDICT_UNREPRODUCIBLE, new_truth_row  # noqa: E402

MODEL = "claude-opus-5-5"
EFFORT = "high"
TIMEOUT = 1800
PROJECTS_ROOT = Path.home() / ".claude" / "projects"
STATUS_OK, STATUS_INVALID, STATUS_ERROR, STATUS_TIMEOUT = "ok", "invalid", "error", "timeout"
FAILED = (STATUS_ERROR, STATUS_TIMEOUT)
VERIFY_PASS, VERIFY_FAIL, VERIFY_EMPTY = "pass", "fail", "empty"
RESULT = "result.json"
TEMPLATE = SKILL_ROOT / "prompts" / "worker-code.md"
TEMPLATE_EDITS = (
    (" — 작업자 {role}", ""),
    ("너는 같은 지적을 서로 따로 처리하는 두 작업자 중 하나다. 다른 작업자는 다른 워크트리에서 같은 지적을 처리한다. "
     "다른 작업자의 워크트리·브랜치·결과를 찾거나 보지 않는다.", "너는 리뷰에서 나온 지적 하나를 처리한다."),
    (" 이 작업의 지적 id는 `{finding_id}`, 역할은 `{role}`이다.", " 이 작업의 지적 id는 `{finding_id}`이다."),
    ("- 모든 명령은 이 워크트리에서 돌린다. 현재 디렉터리는 사용자 저장소이므로 위 절대 경로를 쓴다.",
     "- 모든 명령은 이 워크트리에서 돌린다."),
)
USER_REPO_RULE = "- 현재 디렉터리는 사용자 저장소다."
_worktree_lock = threading.Lock()


@dataclass(frozen=True)
class Options:
    claude: str = "claude"
    model: str = MODEL
    effort: str = EFFORT
    timeout: float = TIMEOUT
    projects_root: Path = PROJECTS_ROOT
    rerun_failed: bool = False


def solo_template(text: str) -> str:
    """작업자 프롬프트에서 두 작업자·사용자 저장소 문장만 바꾼다. 문구가 바뀌어 고칠 곳을 못 찾으면 ValueError."""
    for old, new in TEMPLATE_EDITS:
        if old not in text:
            raise ValueError(f"worker-code.md에서 바꿀 문장을 찾지 못했다: {old[:40]}")
        text = text.replace(old, new)
    return text


def solo_rules() -> str:
    lines = jobs.RULES_CLI.splitlines()
    kept = [line for line in lines if not line.startswith(USER_REPO_RULE)]
    if len(kept) != len(lines) - 1:
        raise ValueError("작업자 규칙에서 사용자 저장소 규칙을 찾지 못했다")
    return "\n".join(kept)


def load_cfg(repo: Path, repos_toml: Path) -> RepoConfig | None:
    return load_repo_config(Path(repo).resolve(), Path(repos_toml))


def build_prompt(item: dict, wt: Path, cfg: RepoConfig | None) -> str:
    raw = item["finding"]
    finding = Finding(id=raw["id"], source=raw["source"], severity=raw["severity"], location=raw["location"],
                      body=raw["body"], evidence=raw.get("evidence") or "")
    values = {
        **jobs._finding_values(finding, "", wt),
        "verify_cmds": jobs._code_verify_cmds(cfg, wt, finding.location) if cfg else "(없음)",
        "rules": solo_rules(),
        "commit_step": jobs._CLI_COMMIT_STEP,
    }
    template = solo_template(TEMPLATE.read_text(encoding="utf-8"))
    return jobs._PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def map_verdict(output: dict | None, patch: str) -> str:
    if output is None:
        return VERDICT_NONE
    verdict = output["verdict"]
    if verdict == VERDICT_FACT:
        return VERDICT_FACT if not output["needs_design_decision"] and patch.strip() else VERDICT_NONE
    if verdict in (VERDICT_NO, VERDICT_UNREPRODUCIBLE):
        return verdict
    return VERDICT_NONE


def valid_output(output) -> bool:
    return (isinstance(output, dict) and all(key in output for key in jobs.WORKER_SCHEMA["required"])
            and output["verdict"] in VERDICTS and isinstance(output["needs_design_decision"], bool))


def parse_output(stdout: str) -> tuple[str, dict | None, dict]:
    try:
        envelope = json.loads(stdout.strip())
    except ValueError:
        return STATUS_ERROR, None, {"detail": stdout.strip()[-500:]}
    if not isinstance(envelope, dict):
        return STATUS_ERROR, None, {"detail": stdout.strip()[-500:]}
    meta = {key: envelope.get(key) for key in ("session_id", "total_cost_usd", "duration_ms", "num_turns", "subtype")}
    if envelope.get("is_error"):
        return STATUS_ERROR, None, {**meta, "detail": str(envelope.get("result", ""))[-500:]}
    output = envelope.get("structured_output")
    if not isinstance(output, dict):
        output = extract_json_block(str(envelope.get("result") or ""))
    if not valid_output(output):
        return STATUS_INVALID, None, meta
    return STATUS_OK, output, meta


def claude_command(opts: Options) -> list[str]:
    return [opts.claude, "-p", "--model", opts.model, "--effort", opts.effort, "--output-format", "json",
            "--json-schema", json.dumps(jobs.WORKER_SCHEMA, ensure_ascii=False),
            "--setting-sources", "project", "--strict-mcp-config",
            "--permission-mode", "acceptEdits", "--allowedTools", "Bash"]


def child_env() -> dict:
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def add_worktree(repo: Path, wt: Path, commit: str, cfg: RepoConfig | None, hooks_dir: Path) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    with _worktree_lock:
        if wt.exists():
            git(repo, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)
            git(repo, "worktree", "prune", check=False)
        git(repo, "-c", f"core.hooksPath={hooks_dir}", "worktree", "add", "--detach", str(wt), commit)
    wt = wt.resolve()
    for cmd in (cfg.setup if cfg else []):
        result = run_commands(wt, [cmd.replace("{root}", shlex.quote(str(repo)))])[0]
        if not result.ok:
            print(f"경고: {wt}의 setup 명령이 실패했다: {cmd}")
    if cfg is not None and cfg.setup:
        patches.record_setup_paths(wt)
    return wt


def remove_worktree(repo: Path, wt: Path) -> None:
    with _worktree_lock:
        if wt.exists():
            git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)


def verify_patch(repo: Path, commit: str, base: Path, wt: Path, cfg: RepoConfig | None,
                 changed: list[str]) -> tuple[str, list[dict]]:
    """cascade-fix 작업자 검증과 같게 돈다: 기준선에서도 실패하는 build·typecheck·lint는 판정에서 뺀다."""
    if cfg is None or not changed:
        return VERIFY_EMPTY, []
    measured = cli_steps.baseline_excludable(cfg) - set(cfg.format)
    baseline: set[str] = set()
    if measured:
        base_wt = base / "base-wt"
        try:
            base_wt = add_worktree(repo, base_wt, commit, cfg, base / "no-hooks")
            baseline = {t for t, r in run_worker_verifiers(base_wt, cfg, changed) if t in measured and not r.ok}
        finally:
            remove_worktree(repo, base_wt)
    judged, recorded = cli_steps.worker_verify(wt, cfg, changed, baseline)
    ran = [r for r in judged if not r.skipped]
    state = VERIFY_EMPTY if not ran else VERIFY_PASS if all(r.ok for r in ran) else VERIFY_FAIL
    return state, [r.to_dict() for r in recorded]


def project_dir_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def session_files(opts: Options, session_id: str | None, wt: Path, since: float) -> list[Path]:
    if session_id:
        found = sorted(Path(opts.projects_root).glob(f"*/{session_id}.jsonl"))
        if found:
            return found[:1]
    folder = Path(opts.projects_root) / project_dir_name(wt)
    return sorted(p for p in folder.glob("*.jsonl") if p.stat().st_mtime >= since)


def session_usage(files: list[Path], dest: Path) -> tuple[list[dict], float | None, str]:
    rows: list[dict] = []
    for path in files:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest / path.name)
        folder = path.with_suffix("")
        if folder.is_dir():
            shutil.copytree(folder, dest / folder.name, dirs_exist_ok=True)
        try:
            rows += [{"label": u.label, "model": u.model, "input": u.input, "output": u.output,
                      "cache_read": u.cache_read, "cache_write": u.cache_write, "cost": u.cost}
                     for u in read_session(path, subagents=True)]
        except UsageFormatError as e:
            return rows, None, str(e)
    if not files:
        return rows, None, "세션 기록을 찾지 못했다"
    costs = [r["cost"] for r in rows]
    return rows, None if any(c is None for c in costs) else sum(costs), ""


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def previous_result(path: Path, opts: Options) -> dict | None:
    if not path.is_file():
        return None
    prev = read_json(path)
    if prev.get("status") in FAILED and opts.rerun_failed:
        n = 1
        while (path.parent / f"result-failed-{n}.json").exists():
            n += 1
        path.rename(path.parent / f"result-failed-{n}.json")
        return None
    return prev


def run_item(item: dict, out: Path, repos_toml: Path, opts: Options) -> dict:
    base = (Path(out) / item["id"]).resolve()
    base.mkdir(parents=True, exist_ok=True)
    prev = previous_result(base / RESULT, opts)
    if prev is not None:
        return prev
    repo = Path(item["repo"]).resolve()
    wt = base / "wt"
    record = {"id": item["id"], "status": STATUS_ERROR, "verdict": VERDICT_NONE, "model": opts.model,
              "effort": opts.effort, "started_at": now_text()}
    started = time.time()
    try:
        cfg = load_cfg(repo, repos_toml)
        wt = add_worktree(repo, wt, item["commit"], cfg, base / "no-hooks")
        prompt = build_prompt(item, wt, cfg)
        (base / "prompt.md").write_text(prompt, encoding="utf-8")
        run = _execute(claude_command(opts), prompt, wt, child_env(), opts.timeout)
        (base / "claude-output.json").write_text(run.stdout, encoding="utf-8")
        (base / "claude-stderr.txt").write_text(run.stderr, encoding="utf-8")
        if run.timed_out:
            status, output, meta = STATUS_TIMEOUT, None, {"detail": f"{opts.timeout}초 시간 초과"}
        else:
            status, output, meta = parse_output(run.stdout)
        patch = patches.extract(wt, item["commit"], cfg)
        (base / "patch.diff").write_text(patch, encoding="utf-8")
        changed = patches.changed_files(patch)
        verify_state, verify_rows = verify_patch(repo, item["commit"], base, wt, cfg, changed)
        usage_rows, cost, usage_error = session_usage(session_files(opts, meta.get("session_id"), wt, started),
                                                      base / "session")
        record.update({
            "status": status, "output": output, "verdict": map_verdict(output, patch) if status == STATUS_OK else VERDICT_NONE,
            "patch_empty": not patch.strip(), "changed": changed, "verify_state": verify_state, "verify": verify_rows,
            "usage": usage_rows, "claude_cost": cost, "usage_error": usage_error,
            "cli_cost_usd": meta.get("total_cost_usd"), "cli_duration_ms": meta.get("duration_ms"),
            "num_turns": meta.get("num_turns"), "session_id": meta.get("session_id"), "detail": meta.get("detail", ""),
        })
    except Exception as e:
        record["detail"] = f"{type(e).__name__}: {e}"
    finally:
        remove_worktree(repo, wt)
        record["ended_at"] = now_text()
        record["duration_s"] = round(time.time() - started, 1)
        write_json(base / RESULT, record)
    return record


def money(value) -> str:
    return "-" if value is None else f"${value:.2f}"


def run_all(items: list[dict], out: Path, repos_toml: Path, opts: Options, parallel: int) -> int:
    def one(item):
        try:
            r = run_item(item, out, repos_toml, opts)
        except Exception as e:
            r = {"status": STATUS_ERROR, "verdict": VERDICT_NONE, "detail": str(e)}
        print(f"{item['id']}: {r['status']} {r['verdict']} {money(r.get('claude_cost'))} {r.get('duration_s', '-')}초",
              flush=True)
        return r

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        results = list(pool.map(one, items))
    failed = sum(r["status"] in FAILED for r in results)
    print(f"항목 {len(items)}건, 오류·시간 초과 {failed}건")
    return 1 if failed else 0


def score(runs: Path, sample: dict[str, dict]) -> dict:
    m = {"total": len(sample), "items": 0, "errors": 0, "incomplete": 0, "missing": 0, "correct": 0, "judged": 0,
         "false_fact": 0, "to_human": 0, "predicted": {}, "by_truth": {t: new_truth_row(t) for t in TRUTH_KEYS},
         "opus_calls": 0, "regressions": 0, "regression_measured": False, "fallback_ratio": None,
         "verify_failed_fact": 0, "verify_empty_fact": 0, "duration_s": 0.0, "cost_by_model": {},
         "prices_as_of": PRICES_AS_OF, "price_source": PRICE_SOURCE}
    costs = []
    for item_id, item in sample.items():
        truth = item["truth"]["verdict"]
        row = m["by_truth"].setdefault(truth, new_truth_row(truth))
        row["total"] += 1
        path = Path(runs) / item_id / RESULT
        if not path.is_file():
            m["missing"] += 1
            continue
        r = read_json(path)
        m["opus_calls"] += 1
        costs.append(r.get("claude_cost"))
        m["duration_s"] += r.get("duration_s") or 0
        for u in r.get("usage", []):
            if u.get("cost") is not None:
                m["cost_by_model"][u["model"]] = m["cost_by_model"].get(u["model"], 0.0) + u["cost"]
        if r["status"] in FAILED:
            m["errors"] += 1
            continue
        verdict = r["verdict"]
        m["items"] += 1
        m["predicted"][item_id] = verdict
        if verdict != VERDICT_NONE:
            m["judged"] += 1
            row["judged"] += 1
        else:
            m["to_human"] += 1
            row["to_human"] += 1
        if verdict == truth:
            m["correct"] += 1
            row["correct"] += 1
        if verdict == VERDICT_FACT and truth == VERDICT_NO:
            m["false_fact"] += 1
            row["false_fact"] += 1
        if verdict == VERDICT_FACT:
            m["verify_failed_fact"] += r.get("verify_state") == VERIFY_FAIL
            m["verify_empty_fact"] += r.get("verify_state") == VERIFY_EMPTY
    m["claude_cost"] = None if not costs or any(c is None for c in costs) else sum(costs)
    m["accuracy"] = m["correct"] / m["total"] if m["total"] else None
    m["agreement"] = m["correct"] / m["judged"] if m["judged"] else None
    m["scored"] = m["total"] - m["errors"] - m["incomplete"] - m["missing"]
    m["completion"] = m["scored"] / m["total"] if m["total"] else None
    return m


def score_text(m: dict) -> str:
    pct = (lambda v: "-" if v is None else f"{v * 100:.1f}%")
    rows = [("정확도", f"{m['correct']}/{m['total']} ({pct(m['accuracy'])})"), ("「아님」 오채택", str(m["false_fact"])),
            ("사람 목록", str(m["to_human"])), ("오류·시간 초과 · 누락", f"{m['errors']} · {m['missing']}"),
            ("검증 실패 채택 · 검증 없는 채택", f"{m['verify_failed_fact']} · {m['verify_empty_fact']}"),
            ("Claude 비용", money(m["claude_cost"])), ("세션 시간 합", f"{m['duration_s'] / 60:.1f}분")]
    lines = ["| 값 | 단일 Claude |", "| --- | ---: |", *(f"| {k} | {v} |" for k, v in rows)]
    return "\n".join(lines) + f"\n\n단가 기준: {m['prices_as_of']} {m['price_source']}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="단일 Claude 기준선: 지적마다 Claude Code 세션 하나가 판정·수정·검증한다")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="표본 지적을 그 커밋의 워크트리에서 claude -p로 처리한다")
    run.add_argument("--sample", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--repos-toml", required=True, help="저장소 검증 명령(setup·tests·lint…)을 읽을 repos.toml")
    run.add_argument("--limit", type=int)
    run.add_argument("--only", help="쉼표로 나눈 항목 id만 돌린다")
    run.add_argument("--parallel", type=int, default=1, help="동시에 띄울 세션 수(기본 1)")
    run.add_argument("--model", default=MODEL)
    run.add_argument("--effort", default=EFFORT)
    run.add_argument("--timeout", type=float, default=TIMEOUT, help="항목당 초(기본 1800)")
    run.add_argument("--claude", default="claude")
    run.add_argument("--projects-root", default=str(PROJECTS_ROOT), help="세션 기록을 찾을 폴더")
    run.add_argument("--rerun-failed", action="store_true", help="오류·시간 초과로 끝난 항목을 다시 돌린다(앞 결과는 남긴다)")
    sc = sub.add_parser("score", help="결과를 score.py --json과 같은 키로 채점한다")
    sc.add_argument("--runs", required=True)
    sc.add_argument("--sample", required=True)
    sc.add_argument("--json")
    args = parser.parse_args(argv)
    if args.command == "score":
        sample = {item["id"]: item for item in (json.loads(line) for line in
                  Path(args.sample).read_text(encoding="utf-8").splitlines() if line.strip())}
        m = score(Path(args.runs), sample)
        print(score_text(m))
        if args.json:
            write_json(Path(args.json), m)
        return 0
    items = load_sample(args.sample)
    if args.only:
        wanted = set(args.only.split(","))
        items = [item for item in items if item["id"] in wanted]
    items = items[: args.limit]
    opts = Options(claude=args.claude, model=args.model, effort=args.effort, timeout=args.timeout,
                   projects_root=Path(args.projects_root), rerun_failed=args.rerun_failed)
    return run_all(items, Path(args.out), Path(args.repos_toml), opts, args.parallel)


if __name__ == "__main__":
    sys.exit(main())
