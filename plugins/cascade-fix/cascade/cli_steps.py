import copy
import fcntl
import json
import os
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from cascade.assignment import claude_fallback, fallback_target, ledger_profile
from cascade.config import ConfigError, ExitCodes, RepoConfig, detect_candidates, has_repos, is_sensitive_path, load_repo_config
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_CARRIED,
    OUTCOME_UNDECIDABLE,
    VERDICT_FACT,
    VERDICT_UNDECIDED,
    Decision,
    WorkerResult,
)
from cascade.findings import Finding
from cascade.jobs import ROLES, RUNNER_CLAUDE, AdjInput, location_file, split_job_args, split_location, write_job_args
from cascade.patches import DIFF_FLAGS, changed_files, is_lossy, record_setup_paths
from cascade.review import REVIEWER_CLAUDE, REVIEWER_CURSOR, claude_prompt, review_job
from cascade.rounds import STATUS_DONE, Ledger, RoundRecord, ledger_path, needs_recheck
from cascade.runners import STATUS_OK, cursor_available
from cascade.snapshot import Snapshot, add_worktree, tree_diff_stat, user_tree_fingerprint
from cascade.verify import (
    VerifyResult,
    check_snippets,
    new_broken_refs,
    related_tests,
    run_commands,
    run_worker_verifiers,
)
from cascade.workers import expected_model_id

SKILL_ROOT = Path(__file__).resolve().parent.parent
RUN_JOBS = SKILL_ROOT / "scripts" / "run-jobs.js"
USER_REPOS_TOML = Path.home() / ".claude" / "cascade-fix" / "repos.toml"
LEGACY_REPOS_TOML = SKILL_ROOT / "repos.toml"


def find_run_review() -> Path:
    """codex-review 스킬의 run-review.sh. 플러그인 캐시에 설치돼도 개인 스킬 위치를 찾는다."""
    if os.environ.get("CASCADE_RUN_REVIEW"):
        return Path(os.environ["CASCADE_RUN_REVIEW"])
    candidates = [
        SKILL_ROOT.parent / "codex-review" / "scripts" / "run-review.sh",
        Path.home() / ".claude" / "skills" / "codex-review" / "scripts" / "run-review.sh",
    ]
    return next((c for c in candidates if c.is_file()), candidates[-1])


def find_cursor_review() -> Path:
    """cursor-review 스킬의 run-review.sh. 플러그인 캐시에 설치돼도 개인 스킬 위치를 찾는다."""
    if os.environ.get("CASCADE_CURSOR_REVIEW"):
        return Path(os.environ["CASCADE_CURSOR_REVIEW"])
    candidates = [
        SKILL_ROOT.parent / "cursor-review" / "scripts" / "run-review.sh",
        Path.home() / ".claude" / "skills" / "cursor-review" / "scripts" / "run-review.sh",
    ]
    return next((c for c in candidates if c.is_file()), candidates[-1])


RUN_REVIEW = find_run_review()
CURSOR_REVIEW = find_cursor_review()
CLI = f"PYTHONPATH={shlex.quote(str(SKILL_ROOT))} python3 -m cascade.cli"
ROLE_SUFFIX = {"a": "a", "b": "b", "writer": "writer", "adjudicator": "adj"}
DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
SOURCE_DOC = "codex-doc"
DOC_SOURCES = (SOURCE_DOC, "claude-doc", "cursor-doc")
SOURCE_PR = "pr-comment"
EXTERNAL_PREFIX = "외부/"
REASON_NO_PATCH = "패치 없음"
REASON_NON_UTF8 = "UTF-8 아닌 패치"
REASON_OUTSIDE_DOCS = "문서 밖 파일 수정"
CALL_FAILED = "호출 실패"
BASELINE_TAG = "[기준선 실패·판정 제외] "
REPRO_FILE = "repro-cmds.json"
RECHECK_REPO_FILE = "recheck-repo.txt"
RECHECK_SCOPE_FILE = "recheck-scope.json"
BASELINE_FILE = "baseline.json"
OPINION_ARGS = "opinion-args.json"
OPINION_RESULTS = "opinion.json"
RECHECK_ARGS = "recheck-args.json"
RECHECK_RESULTS = "recheck.json"
RECHECK_CLI_ARGS = "recheck-cli-args.json"
RECHECK_CLI_RESULTS = "recheck-cli.json"
USAGE_FILE = "claude-usage.json"
CLAUDE_JOB_FILES = ("stage1-args.json", "stage1-cli-args.json", "stage1-fallback-args.json", "checker-args.json",
                    "checker-cli-args.json", "checker-fallback-args.json", "adj-args.json", "adj-cli-args.json",
                    "adj-fallback-args.json", OPINION_ARGS, "opinion-cli-args.json", RECHECK_ARGS, RECHECK_CLI_ARGS)


def claude_job_entries(data) -> list[dict]:
    """작업 파일 내용에서 Claude가 돌릴 작업. runner가 없는 Workflow 입력은 모두 Claude이고, CLI 입력은 runner가 claude인 것만 센다."""
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return [j for j in jobs or [] if isinstance(j, dict) and j.get("runner", RUNNER_CLAUDE) == RUNNER_CLAUDE]


NON_PROGRESS_STEPS = ("cleanup", "usage")
STAGE1, STAGE_CHECKER, STAGE_ADJ, STAGE_OPINION, STAGE_RECHECK = "stage1", "checker", "adj", "opinion", "recheck"
STAGE_ARGS = {STAGE1: "stage1-args.json", STAGE_CHECKER: "checker-args.json", STAGE_ADJ: "adj-args.json"}
STAGE_OUTPUTS = {STAGE1: "stage1.json", STAGE_CHECKER: "checker.json", STAGE_ADJ: "adj.json"}
STAGE_INPUTS = {STAGE1: "jobs", STAGE_CHECKER: "judge-checker", STAGE_ADJ: "judge", STAGE_OPINION: "adjudicate", STAGE_RECHECK: "apply"}
STOP_PARTS = {"tree": "작업 트리", "head": "HEAD", "head_ref": "현재 브랜치", "stash": "stash",
              "attached": "작업자 워크트리 브랜치"}


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message = message
        self.code = code


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"파일이 없다: {path}") from None
    except ValueError as e:
        raise CliError(f"JSON을 읽을 수 없다({path}): {e}", ExitCodes.PARSE_ERROR) from None


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CliError(f"파일이 없다: {path}") from None


def git_toplevel(cwd: Path) -> Path:
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise CliError(f"git 저장소가 아니다: {cwd}")
    return Path(p.stdout.strip()).resolve()


def repos_toml() -> Path:
    """저장소 검증 설정 파일. 환경 변수 > ~/.claude/cascade-fix/repos.toml > 스킬 폴더의 repos.toml(예전 위치, 항목이 있을 때만)."""
    if os.environ.get("CASCADE_REPOS_TOML"):
        return Path(os.environ["CASCADE_REPOS_TOML"])
    if USER_REPOS_TOML.is_file():
        return USER_REPOS_TOML
    if LEGACY_REPOS_TOML.is_file() and has_repos(LEGACY_REPOS_TOML):
        return LEGACY_REPOS_TOML
    return USER_REPOS_TOML


def load_cfg(root: Path) -> RepoConfig | None:
    path = repos_toml()
    try:
        return load_repo_config(root, path)
    except tomllib.TOMLDecodeError as e:
        raise CliError(f"저장소 설정 파일을 읽을 수 없다({path}): {e}", ExitCodes.PARSE_ERROR) from None
    except ConfigError as e:
        raise CliError(f"저장소 설정이 잘못됐다({path}): {e}", ExitCodes.PARSE_ERROR) from None


def no_verify_message(root: Path) -> str:
    return "\n".join([
        f"검증 명령이 없다. {repos_toml()} 에 [repos.\"{root}\"] 항목과 tests 명령을 적은 뒤 다시 실행하라.",
        "코드 모드는 테스트가 없는 저장소에서 돌리지 않는다.",
        "저장소에서 찾은 후보:",
        json.dumps(detect_candidates(root), ensure_ascii=False, indent=2),
    ])


def cfg_for(source: str, root: Path) -> RepoConfig:
    cfg = load_cfg(root)
    if source in DOC_SOURCES:
        return cfg or RepoConfig()
    if cfg is None or not cfg.tests:
        raise CliError(no_verify_message(root), ExitCodes.NO_VERIFY_CONFIG)
    return cfg


def load_ledger(run_dir: Path) -> Ledger:
    path = ledger_path(run_dir)
    if not path.is_file():
        raise CliError(f"원장이 없다: {path}")
    return Ledger.load(path)


def save_ledger(run_dir: Path, ledger: Ledger) -> None:
    ledger.save(ledger_path(run_dir))


def get_round(ledger: Ledger, no: int) -> RoundRecord:
    for rnd in ledger.rounds:
        if rnd.no == no:
            return rnd
    raise CliError(f"회차 {no}이 원장에 없다")


def round_dir(run_dir: Path, no: int) -> Path:
    path = Path(run_dir) / f"round-{no}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def step_done(rnd: RoundRecord, step: str) -> bool:
    return any(e.get("step") == step for e in rnd.events)


def mark(rnd: RoundRecord, step: str, **extra) -> None:
    rnd.events.append({"step": step, "at": now(), **extra})


def guard(rnd: RoundRecord, step: str, force: bool) -> None:
    if step_done(rnd, step) and not force:
        raise CliError(f"회차 {rnd.no}의 {step} 단계는 이미 했다. 다시 하려면 --force 를 붙인다")


def snapshot_of(ledger: Ledger) -> Snapshot:
    return Snapshot.from_dict(ledger.snapshot)


def wt_name(fid: str, role: str) -> str:
    return f"{fid}-{ROLE_SUFFIX[role]}"


def worktree(snap: Snapshot, fid: str, role: str) -> Path:
    name = wt_name(fid, role)
    if name not in snap.worktrees:
        raise CliError(f"워크트리가 없다: {name} (jobs·judge 단계를 먼저 한다)")
    return snap.worktrees[name]


def patch_path(rd: Path, fid: str, role: str) -> Path:
    return Path(rd) / f"patch-{fid}-{ROLE_SUFFIX[role]}.diff"


def findings_of(rnd: RoundRecord) -> list[Finding]:
    return [Finding.from_dict(f) for f in rnd.findings]


def failed_result(fid: str, role: str) -> WorkerResult:
    return WorkerResult(
        finding_id=fid, role=role, verdict=VERDICT_UNDECIDED, needs_design_decision=False,
        reason=CALL_FAILED, verify=[], checker_ok=None, model_id="",
    )


def pick(results: dict[str, WorkerResult], fid: str, role: str) -> WorkerResult:
    return results.get(f"{fid}:{role}") or failed_result(fid, role)


def worker_from(results: dict, fid: str, role: str) -> WorkerResult:
    data = results.get(f"{fid}:{role}")
    return WorkerResult.from_dict(data) if isinstance(data, dict) else failed_result(fid, role)


def model_warnings(results: dict[str, WorkerResult], expected_ids: dict[str, str] | None = None) -> list[str]:
    out = []
    for key, r in results.items():
        expected = (expected_ids or {}).get(key) or ROLES.get(r.role, (None,))[0]
        if not r.model_id and r.reason != CALL_FAILED:
            out.append(f"경고: {key} 모델 ID가 비어 있다(기대값 {expected})")
        elif r.model_id and expected and r.model_id != expected:
            out.append(f"경고: {key} 모델 ID가 {r.model_id} 이다(기대값 {expected})")
    return out


def expected_model_ids(ledger: Ledger, results: dict[str, WorkerResult]) -> dict[str, str]:
    return {key: expected_model_id(ledger.workers[r.role]) for key, r in results.items() if r.role in ledger.workers}


def _last(rnd: RoundRecord, step: str) -> int:
    return max((i for i, e in enumerate(rnd.events) if e.get("step") == step), default=-1)


def workers_done(rnd: RoundRecord, stage: str) -> bool:
    """마지막 입력 갱신 뒤에 실행한 workers만 완료로 본다."""
    return _last(rnd, f"workers-{stage}") > _last(rnd, STAGE_INPUTS[stage])


def exhausted_runners(rnd: RoundRecord, stage: str | None = None) -> dict[str, dict]:
    """회차(마지막 jobs 이후)의 workers 단계에서 한도·인증으로 멈춘 runner와 그 상태·사유."""
    found: dict[str, dict] = {}
    for e in rnd.events[_last(rnd, "jobs") + 1:]:
        if str(e.get("step", "")).startswith("workers-") and (stage is None or e["step"] == f"workers-{stage}"):
            for runner, entry in (e.get("exhausted") or {}).items():
                found.setdefault(runner, entry)
    return found


def judged_after_workers_input(rnd: RoundRecord, stage: str) -> bool:
    """마지막 입력 갱신 뒤에 결과를 소비한 단계가 있으면 재실행을 막는다."""
    source = _last(rnd, STAGE_INPUTS[stage])
    if stage == STAGE_OPINION:
        return any(e.get("step") == "apply" or str(e.get("step", "")).startswith("opinion:")
                   for e in rnd.events[source + 1:])
    consumer = {STAGE_ADJ: "adjudicate", STAGE_RECHECK: "recheck"}.get(stage, "judge")
    return _last(rnd, consumer) > source


def checker_pending(rnd: RoundRecord) -> bool:
    """judge 1차가 마지막 1단계 갱신(jobs·workers stage1) 뒤에 점검자 작업을 만들었다."""
    return _last(rnd, "judge-checker") > max(_last(rnd, "jobs"), _last(rnd, f"workers-{STAGE1}"))


def clear_cli_outputs(rd: Path, *stages: str) -> None:
    for stage in stages:
        for name in (f"{stage}-cli.json", f"{stage}-fallback-args.json", f"{stage}-fallback.json"):
            (Path(rd) / name).unlink(missing_ok=True)


def has_jobs(path: Path) -> bool:
    return Path(path).is_file() and bool(read_json(path).get("jobs"))


def fallback_job(entry: dict, cell: dict) -> dict:
    target = claude_fallback(cell, entry["role"])
    model, effort = target["model"], target["effort"]
    return {"id": entry["id"], "label": entry["id"].replace(":", " "), "model": model, "effort": effort,
            "kind": entry["kind"], "promptPath": entry["promptPath"]}


def _role_of(job_id: str) -> str:
    return job_id.rpartition(":")[2]


def load_stage_results(rd: Path, stage: str, claude: Path | None) -> tuple[dict, dict[str, str]]:
    """Claude 결과 → 성공한 CLI 결과 → 대체 실행 결과 순으로 합친다. 같은 id는 뒤가 앞을 덮는다."""
    rd = Path(rd)
    merged: dict[str, dict] = {}
    expected: dict[str, str] = {}
    if claude is not None:
        merged.update({item["id"]: item for item in read_json(claude).get("results", [])})
    cli, cli_args, fallback = rd / f"{stage}-cli.json", rd / f"{stage}-cli-args.json", rd / f"{stage}-fallback.json"
    if cli.is_file():
        data = read_json(cli)
        ok = {o["id"] for o in data.get("outcomes", []) if o.get("status") == STATUS_OK}
        merged.update({item["id"]: item for item in data.get("results", []) if item["id"] in ok})
        reruns = data.get("fallback_runs", {})
        expected.update({job["id"]: expected_model_id(reruns.get(job["id"], job))
                         for job in read_json(cli_args)["jobs"] if job["id"] in ok})
    if fallback.is_file():
        items = read_json(fallback).get("results", [])
        merged.update({item["id"]: item for item in items})
        fallback_args = rd / f"{stage}-fallback-args.json"
        models = {job["id"]: job["model"] for job in read_json(fallback_args).get("jobs", [])} if fallback_args.is_file() else {}
        expected.update({item["id"]: models.get(item["id"]) or ROLES[_role_of(item["id"])][0]
                         for item in items if item["id"] in models or _role_of(item["id"]) in ROLES})
    return {"results": list(merged.values())}, expected


def stage_inputs(run_dir: Path, rnd: RoundRecord, rd: Path, stage: str, claude: str | None, option: str) -> tuple[dict, dict[str, str]]:
    rd = Path(rd)
    if (rd / f"{stage}-cli-args.json").is_file() and (
            not workers_done(rnd, stage) or (stage == STAGE_ADJ and not (rd / f"{stage}-cli.json").is_file())):
        raise CliError(f"회차 {rnd.no}의 {stage} CLI 작업이 남았다. workers 단계를 먼저 한다: "
                       + _cmd("workers", run_dir, f"--round {rnd.no}", f"--stage {stage}"))
    fallback_args, fallback_out = rd / f"{stage}-fallback-args.json", rd / f"{stage}-fallback.json"
    if fallback_args.is_file() and not fallback_out.is_file():
        raise CliError(f"{stage} 대체 실행 결과({fallback_out.name})가 없다. 먼저 한다: " + workflow_step(fallback_args, fallback_out))
    if claude is None and has_jobs(rd / STAGE_ARGS[stage]):
        raise CliError(f"{option} 가 필요하다: {STAGE_ARGS[stage]} 에 Claude 작업이 있다. 먼저 한다: "
                       + workflow_step(rd / STAGE_ARGS[stage], rd / STAGE_OUTPUTS[stage]))
    return load_stage_results(rd, stage, Path(claude) if claude else None)


def is_sensitive(paths: list[str], cfg: RepoConfig) -> bool:
    return any(is_sensitive_path(path, cfg.sensitive_paths) for path in paths)


def is_adopted(d: dict) -> bool:
    return d["outcome"] == OUTCOME_AGREED_FACT or (d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from"))


def first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0].strip() if lines else "(출력 없음)"


def doc_checks(
    wt: Path, base_wt: Path | None, patch: str, ledger: Ledger, cfg: RepoConfig
) -> tuple[list[str], list[VerifyResult]]:
    changed = changed_files(patch)
    docs = [p for p in changed if p in ledger.docs] or [p for p in changed if p.endswith(DOC_SUFFIXES)]
    missing: list[str] = []
    snippets: list[VerifyResult] = []
    for rel in docs:
        path = Path(wt) / rel
        if not path.is_file():
            continue
        base_doc = Path(base_wt) / rel if base_wt is not None else None
        missing += [m for m in new_broken_refs(base_wt, base_doc, wt, path) if m not in missing]
        snippets += check_snippets(wt, path, cfg)
    return missing, snippets


def map_location(location: str, root: Path, docs: dict[str, str]) -> str:
    path, line = split_location(location)
    if not path or path in ("?", "-"):
        return location
    target = Path(path)
    if target.is_absolute():
        resolved = target.resolve()
        for key, original in docs.items():
            if Path(original).resolve() == resolved:
                return f"{key}{line}"
        root = Path(root).resolve()
        return f"{resolved.relative_to(root).as_posix()}{line}" if resolved.is_relative_to(root) else location
    if path in docs:
        return location
    parts = target.parts
    matches = [key for key, original in docs.items() if Path(original).parts[-len(parts):] == parts]
    return f"{matches[0]}{line}" if len(matches) == 1 else location


def prompt_findings(ledger: Ledger, findings: list[Finding]) -> list[Finding]:
    return [replace(f, location=map_location(f.location, Path(ledger.root), ledger.docs)) for f in findings]


def new_worktree(snap: Snapshot, name: str, commit: str, cfg: RepoConfig | None) -> Path:
    path = add_worktree(snap, name, commit)
    for cmd in (cfg.setup if cfg else []):
        result = run_commands(path, [cmd.replace("{root}", shlex.quote(str(snap.root)))])[0]
        if not result.ok:
            print(f"경고: {name} 워크트리의 setup 명령이 실패했다: {cmd} — {first_line(result.tail)}")
    if cfg is not None and cfg.setup:
        record_setup_paths(path)
    return path


def worker_verify(wt: Path, cfg: RepoConfig, changed: list[str], baseline: set[str]) -> tuple[list[VerifyResult], list[VerifyResult]]:
    judged: list[VerifyResult] = []
    recorded: list[VerifyResult] = []
    for template, r in run_worker_verifiers(wt, cfg, changed):
        if template in baseline and template in baseline_excludable(cfg):
            recorded.append(VerifyResult(cmd=BASELINE_TAG + r.cmd, ok=r.ok, tail=r.tail))
            continue
        judged.append(r)
        recorded.append(r)
    return judged, recorded


def baseline_failed(rd: Path) -> set[str]:
    path = Path(rd) / BASELINE_FILE
    if not path.is_file():
        return set()
    data = read_json(path)
    return set(data.get("failed", [])) if isinstance(data, dict) else set()


def baseline_excludable(cfg: RepoConfig) -> set[str]:
    return {*cfg.build, *cfg.typecheck, *cfg.lint, *cfg.format} - set(cfg.tests)


def write_baseline(rd: Path, base_wt: Path, cfg: RepoConfig, changed: list[str]) -> list[str]:
    measured = baseline_excludable(cfg) - set(cfg.format)
    results = [(t, r) for t, r in run_worker_verifiers(base_wt, cfg, changed) if t in measured]
    failed = sorted({t for t, r in results if not r.ok})
    write_json(Path(rd) / BASELINE_FILE, {
        "failed": failed,
        "results": [{"template": t, **r.to_dict()} for t, r in results],
    })
    return failed


def last_event(rnd: RoundRecord, step: str) -> dict | None:
    return next((e for e in reversed(rnd.events) if e.get("step") == step), None)


def check_user_tree(ledger: Ledger, rnd: RoundRecord) -> None:
    event = last_event(rnd, "jobs")
    before = (event or {}).get("fingerprint")
    if not before:
        return
    snap = snapshot_of(ledger)
    now = user_tree_fingerprint(snap.root, snap.run_dir)
    changed = [k for k in STOP_PARTS if k in before and before[k] != now.get(k)]
    old, new = set(before.get("branches", [])), set(now.get("branches", []))
    branch_lines = [f"- {b}" for b in sorted(old - new)] + [f"+ {b}" for b in sorted(new - old)]
    if not changed:
        if branch_lines:
            print("경고: jobs 이후 사용자 저장소의 브랜치 목록이 바뀌었다(다른 세션의 fetch·삭제일 수 있다). "
                  "작업 트리·HEAD·stash는 그대로라 계속한다")
            print("\n".join(branch_lines))
        return
    lines = [
        "jobs 이후 사용자 저장소가 바뀌었다(작업자가 워크트리 밖을 건드렸을 수 있다): "
        + ", ".join(STOP_PARTS[k] for k in changed),
    ]
    if "tree" in changed:
        lines += ["", f"git diff --stat {before['tree']} {now['tree']}", tree_diff_stat(snap.root, before["tree"], now["tree"])]
    if "head" in changed:
        lines.append(f"HEAD: {before['head']} → {now['head']}")
    if "head_ref" in changed:
        lines.append(f"현재 브랜치: {before['head_ref']} → {now['head_ref']}")
    if "stash" in changed:
        lines.append(f"refs/stash: {before['stash'] or '(없음)'} → {now['stash'] or '(없음)'}")
    if "attached" in changed:
        lines += ["브랜치에 붙은 작업자 워크트리(jobs는 --detach로 만든다):", *now["attached"]]
    lines += branch_lines
    lines += ["", "사람이 확인한다. 의도한 변경이면 되돌리거나 jobs --force 로 다시 시작한다."]
    raise CliError("\n".join(lines), ExitCodes.NEEDS_USER)


def repro_listed(rd: Path) -> dict[str, list[str]]:
    path = Path(rd) / REPRO_FILE
    data = read_json(path) if path.is_file() else {}
    return data if isinstance(data, dict) else {}


def approved_repro(rd: Path, allow: str | None) -> dict[str, list[str]] | None:
    listed = repro_listed(rd)
    if not any(listed.values()):
        return None
    if allow is None:
        print(f"경고: 재현 명령을 승인받지 않아 재현 검증을 건너뛴다. 목록 {Path(rd) / REPRO_FILE} 을 사용자에게 보이고 승인받으면 --allow-repro 로 넘긴다")
        return None
    if read_json(Path(allow)) != listed:
        raise CliError(f"--allow-repro 내용이 {Path(rd) / REPRO_FILE} 와 다르다. 승인한 목록 그대로 넘긴다")
    return listed


def recheck_worktree_name(no: int) -> str:
    return f"recheck-r{no}"


def round_worktree_names(rnd: RoundRecord) -> list[str]:
    return [wt_name(f["id"], role) for f in rnd.findings for role in ROLE_SUFFIX]


def scope_files(rd: Path) -> set[str] | None:
    path = Path(rd) / RECHECK_SCOPE_FILE
    if not path.is_file():
        return None
    data = read_json(path)
    return set(data) if isinstance(data, list) else None


def recheck_scope(ledger: Ledger, rd: Path, adopted: list[Finding], combined: list[Finding]) -> set[str] | None:
    scope = scope_files(rd)
    if scope is None:
        return None
    root = Path(ledger.root)
    mapped = lambda f: location_file(map_location(f.location, root, ledger.docs))  # noqa: E731
    scope |= {m for f in adopted if (m := mapped(f))}
    return scope | {location_file(f.location) for f in combined if mapped(f) in scope and location_file(f.location)}


def doc_problems(missing: list[str], snippets: list[VerifyResult]) -> list[str]:
    return [*missing, *(f"코드 블록 검사 실패 {r.cmd}" for r in snippets if not r.ok)]


def doc_verify(missing: list[str], snippets: list[VerifyResult]) -> list[VerifyResult]:
    refs = VerifyResult(cmd="참조 실존", ok=not missing, tail=", ".join(missing))
    return [refs, *snippets]


def needs_checker(a: WorkerResult, b: WorkerResult, problems: list[str], writer_patch: str) -> bool:
    return bool(
        writer_patch.strip()
        and not problems
        and a.verdict == b.verdict == VERDICT_FACT
        and not a.needs_design_decision
        and not b.needs_design_decision
    )


def adj_input(ledger: Ledger, f: Finding, results: dict, verify: dict, wt: Path, cfg: RepoConfig, reasons: list[str]) -> AdjInput:
    a = worker_from(results, f.id, "a")
    b = worker_from(results, f.id, "b")
    v = verify.get(f.id, {})
    if ledger.mode == "doc":
        writer = worker_from(results, f.id, "writer")
        checker_data = results.get(f"{f.id}:checker")
        checker = WorkerResult.from_dict(checker_data) if isinstance(checker_data, dict) else None
        return AdjInput(
            finding=f, a=a, b=b, patch_a=writer.patch, patch_b="", verify_a=v.get("writer", []), verify_b=[],
            worktree=wt, mode="doc", cfg=cfg, checker=checker, escalation_reasons=list(reasons),
        )
    return AdjInput(
        finding=f, a=a, b=b, patch_a=a.patch, patch_b=b.patch, verify_a=v.get("a", []), verify_b=v.get("b", []),
        worktree=wt, mode="code", cfg=cfg, checker=None, escalation_reasons=list(reasons),
    )


def rechecks_done(ledger: Ledger, exclude: int | None = None) -> int:
    return sum(1 for r in ledger.rounds if r.recheck_opened and not r.final and r.no != exclude)


def recheck_needed(ledger: Ledger, rnd: RoundRecord) -> bool:
    if ledger.source == SOURCE_PR or not rnd.adopted:
        return False
    if rnd.final:
        return needs_recheck(replace(rnd, final=False), 1, 0)
    adopted = set(rnd.adopted)
    if any(f.get("severity") == "Critical" for f in rnd.findings if f.get("id") in adopted):
        return True
    return needs_recheck(rnd, ledger.max_rechecks, rechecks_done(ledger, exclude=rnd.no))


def base_of(ledger: Ledger, rnd: RoundRecord) -> str:
    return rnd.base_commit or ledger.snapshot.get("commit", "")


def base_name(rnd: RoundRecord) -> str:
    return "base" if rnd.no == 1 else f"base-r{rnd.no}"


def external_docs(ledger: Ledger) -> dict[str, Path]:
    return {k: Path(v) for k, v in ledger.docs.items() if k.startswith(EXTERNAL_PREFIX)}


def range_diff(root: Path, old: str, new: str, files: list[str] | None = None) -> str:
    paths = ["--", *files] if files else []
    p = subprocess.run(["git", "--literal-pathspecs", "-c", "core.quotepath=false", "diff", *DIFF_FLAGS, old, new, *paths],
                       cwd=root, capture_output=True)
    if p.returncode != 0:
        raise CliError(f"git diff 실패: {p.stderr.decode('utf-8', errors='replace').strip()}")
    return p.stdout.decode("utf-8", errors="replace")


def without_empty_patch(d: Decision, patches: dict[str, str], adjudicated: bool) -> Decision:
    if not is_adopted(d.to_dict()) or patches.get(d.patch_from, "").strip():
        return d
    if adjudicated or d.outcome != OUTCOME_AGREED_FACT:
        return Decision(d.finding_id, OUTCOME_UNDECIDABLE, None, [*d.reasons, REASON_NO_PATCH])
    return Decision(d.finding_id, OUTCOME_ADJUDICATE, None, [*d.reasons, REASON_NO_PATCH])


def without_lossy_patch(d: Decision, worktree_of: Callable[[str], Path], base: str) -> Decision:
    if not is_adopted(d.to_dict()) or not is_lossy(worktree_of(d.patch_from), base):
        return d
    return Decision(d.finding_id, OUTCOME_UNDECIDABLE, None, [*d.reasons, REASON_NON_UTF8])


def without_outside_docs(d: Decision, patches: dict[str, str], docs: dict[str, str]) -> Decision:
    if not is_adopted(d.to_dict()):
        return d
    outside = [p for p in changed_files(patches.get(d.patch_from, "")) if p not in docs]
    if not outside:
        return d
    return Decision(d.finding_id, OUTCOME_UNDECIDABLE, None, [*d.reasons, f"{REASON_OUTSIDE_DOCS}: {', '.join(outside)}"])


def final_suite_cfg(cfg: RepoConfig | None, mode: str, root: Path, changed: list[str]) -> tuple[RepoConfig | None, str | None]:
    if cfg is None or cfg.full_tests or mode == "doc" or not cfg.tests:
        return cfg, None
    files = " ".join(shlex.quote(f) for f in related_tests(root, changed))
    fallback = replace(cfg, full_tests=[c.replace("{files}", files) for c in cfg.tests])
    return fallback, "경고: full_tests가 없어 바뀐 파일 전체의 관련 테스트(tests 명령)로 대신 돌린다"



def renumber(items: list[Finding], round_no: int, source: str) -> list[Finding]:
    return [replace(f, id=f"r{round_no}-{i:02d}", source=source) for i, f in enumerate(items, 1)]


def unresolved_copy(f: Finding) -> Finding:
    return replace(f, body=f"[재검토: {f.id} 해소 안 됨]\n{f.body}")


def carried_ids(rnd: RoundRecord, applied: list[str], failed: list[str], patches: dict[str, str]) -> set[str]:
    if rnd.final:
        return set()
    carried_before = {f["id"] for f in rnd.findings if f.get("carried_from")}
    applied_files = {p for fid in applied for p in changed_files(patches.get(fid, ""))}
    return {fid for fid in failed
            if fid not in carried_before and applied_files & set(changed_files(patches.get(fid, "")))}


def carried_findings(rnd: RoundRecord) -> list[Finding]:
    ids = {d["finding_id"] for d in rnd.decisions if d["outcome"] == OUTCOME_CARRIED}
    applied = [f"{f.id} {f.location} — {first_line(f.body)}" for f in findings_of(rnd) if f.id in rnd.adopted]
    return [replace(f, carried_from=f.id, carried_after=applied) for f in findings_of(rnd) if f.id in ids]


EXTERNAL_AUTH_FAILURE = re.compile(r"\b401\b|unauthori[sz]ed|refresh[ _]token|codex login|not logged in|Authentication required", re.IGNORECASE)
EXTERNAL_USAGE_LIMIT = re.compile(r"usage limit", re.IGNORECASE)
EXTERNAL_CLASSIFY_TAIL_LINES = 20
FAILURE_AUTH, FAILURE_LIMIT, FAILURE_ERROR = "auth", "limit", "error"
FAILURE_REASONS = {FAILURE_LIMIT: "이용 한도", FAILURE_ERROR: "실행 오류"}


def review_cell(ledger: Ledger, role: str) -> dict:
    """role 칸의 지금 값. 실행 중 대체가 있으면 가장 나중 것의 `cell`이고, 없으면 init·jobs가 확정한 칸이다."""
    profile = ledger_profile(ledger)
    key = f"{ledger.mode}.{role}"
    for record in reversed(profile.get("runtime_substitutions", [])):
        if record["role"] == key:
            return record["cell"]
    return profile["roles"][key]


def is_claude(ledger: Ledger, role: str) -> bool:
    return review_cell(ledger, role)["runner"] == REVIEWER_CLAUDE


def external_failure(result: Path) -> str | None:
    """외부 검토자 결과(`<로그>.final`)가 없거나 비었을 때의 원인. 결과가 있거나 실행 로그가 없으면(아직 안 돌림) None.

    로그 앞부분에는 프롬프트(지적 본문·패치)가 그대로 찍히므로 CLI 자신의 오류 출력이 있는 끝 줄만 본다."""
    result = Path(result)
    if result.is_file() and result.read_text(encoding="utf-8").strip():
        return None
    log = result.with_suffix("") if result.suffix == ".final" else None
    if log is None or not log.is_file():
        return None
    tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-EXTERNAL_CLASSIFY_TAIL_LINES:])
    if EXTERNAL_AUTH_FAILURE.search(tail):
        return FAILURE_AUTH
    return FAILURE_LIMIT if EXTERNAL_USAGE_LIMIT.search(tail) else FAILURE_ERROR


def review_event_extra(ledger: Ledger, role: str) -> dict:
    """붙인 의견·재검토 이벤트에 남길 실제 runner·모델과, 실행 중 대체였으면 그 내용."""
    cell = review_cell(ledger, role)
    extra = {"runner": cell["runner"], "model": cell["model"]}
    found = [r for r in ledger_profile(ledger).get("runtime_substitutions", []) if r["role"] == f"{ledger.mode}.{role}"]
    if found:
        extra["fallback"] = {"from": found[-1]["from"], "to": found[-1]["to"], "reason": found[-1]["reason"]}
    return extra


def substitute_review(ledger: Ledger, role: str, reason: str, round_no: int | None = None) -> dict:
    """검토 칸(opinion·recheck)의 실행 중 대체를 `profile.runtime_substitutions`에 남기고 새 칸을 준다.

    `profile.roles`·`profile.substituted`(init·jobs가 확정한 배정)는 바꾸지 않는다. 동의 밖이거나 같은 CLI면 내장 claude-only 값이다."""
    record = copy.deepcopy(ledger_profile(ledger))
    key = f"{ledger.mode}.{role}"
    old = review_cell(ledger, role)
    new = fallback_target(old, record["consented"], {old["runner"]}, "reviewer")
    rnd = ledger.current_round()
    record.setdefault("runtime_substitutions", []).append(
        {"role": key, "from": old["runner"], "to": new["runner"], "cell": new, "reason": reason,
         "round": round_no if round_no is not None else rnd.no if rnd else 0})
    ledger.profile = record
    return new


def opinion_job_id(fid: str) -> str:
    return f"{fid}:opinion"


def recheck_job_id(no: int) -> str:
    return f"round-{no}:recheck"


def write_review_args(rd: Path, name: str, ledger: Ledger, items: list[tuple[str, str, str, str]], role: str,
                      claude_cli: bool = False) -> Path:
    """items: (job id, 표시 이름, 프롬프트, 참고 저장소). role 칸의 모델·effort로 Claude 검토자 입력을 rd/name 에 쓴다."""
    cell = review_cell(ledger, role)
    jobs = [review_job(job_id, text, claude_prompt(prompt, repo), cell["model"], cell["effort"])
            for job_id, text, prompt, repo in items]
    out = Path(rd) / name
    if claude_cli:
        jobs = [j | {"role": role, "runner": RUNNER_CLAUDE, "worktree": str(repo), "writable": False}
                for j, (_, _, _, repo) in zip(jobs, items)]
        _, args = split_job_args({"jobs": jobs}, Path(rd) / "prompts", claude_cli=True)
    else:
        args = write_job_args({"jobs": jobs}, Path(rd) / "prompts")
    write_json(out, args)
    return out


def pending_opinions(ledger: Ledger, rd: Path) -> list[str]:
    return [fid for p in sorted(Path(rd).glob("opinion-*.md")) if (fid := p.stem[len("opinion-"):]) not in ledger.opinions]


def workflow_step(args: Path, out: Path) -> str:
    folder = Path(args).parent
    round_dir_name = re.fullmatch(r"round-(\d+)", folder.name)
    where = f"--run-dir {_q(folder.parent)} --round {round_dir_name.group(1)}" if round_dir_name else f"--review-dir {_q(folder)}"
    return (f"Workflow 실행: script={RUN_JOBS} 내용, args={args} 내용 → 결과를 {out} 에 저장"
            f" → 끝나면 CF usage {where} --workflow <Workflow 실행 id>")


def parse_remote(url: str) -> tuple[str, str]:
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not m:
        raise CliError(f"origin 주소에서 owner/name을 읽지 못했다: {url} (--pr-repo owner/name 으로 준다)")
    return m.group(1), m.group(2)


def _q(value) -> str:
    return shlex.quote(str(value))


def cursor_reviewer_available() -> bool:
    return CURSOR_REVIEW.is_file() and cursor_available()


def review_script(ledger: Ledger, role: str) -> Path:
    return CURSOR_REVIEW if review_cell(ledger, role)["runner"] == REVIEWER_CURSOR else RUN_REVIEW


def run_review_cmd(mode: str, prompt: Path, root: str, out: Path, script: Path = RUN_REVIEW, model: str = "") -> str:
    return f"{_q(script)} --{mode} {_q(prompt)} --repo {_q(root)} --out {_q(out)}" + (f" --model {_q(model)}" if model else "")


def review_cmd(ledger: Ledger, role: str, prompt: Path, root: str, out: Path) -> str:
    cell = review_cell(ledger, role)
    model = cell["model"] if cell["runner"] == REVIEWER_CURSOR else ""
    return run_review_cmd(role, prompt, root, out, review_script(ledger, role), model)


WORKERS_LOCK = "workers.lock"
GO_LOCK = "go.lock"


@contextmanager
def _run_lock(run_dir: Path, name: str, message: str):
    if not (Path(run_dir) / "ledger.json").is_file():
        raise CliError(f"원장이 없다: {Path(run_dir) / 'ledger.json'}")
    handle = open(Path(run_dir) / name, "a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CliError(message) from None
        yield
    finally:
        handle.close()


def workers_lock(run_dir: Path):
    """실행 폴더당 workers 하나만 돈다. 같은 Codex 로그인을 쓰는 프로세스가 겹치면 로그인이 풀릴 수 있다."""
    return _run_lock(run_dir, WORKERS_LOCK, "workers가 이미 돈다. 하나가 끝난 뒤 다시 한다")


def go_lock(run_dir: Path):
    """실행 폴더당 go 하나만 돈다. 둘이 같은 단계를 동시에 하면 원장과 작업 트리가 엇갈린다."""
    return _run_lock(run_dir, GO_LOCK, "go가 이미 돈다")


def workers_running(run_dir: Path) -> bool:
    try:
        with workers_lock(run_dir):
            return False
    except CliError:
        return True


def _cmd(name: str, run_dir: Path, *rest: str) -> str:
    return " ".join([CLI, name, "--run-dir", _q(run_dir), *rest])


@dataclass(frozen=True)
class Action:
    """next_steps의 한 줄. kind: cmd(CF 명령) | workflow(Workflow 도구) | shell(검토 스크립트) | note(안내만)."""
    kind: str
    text: str
    argv: tuple[str, ...] = ()
    args_path: Path | None = None
    out_path: Path | None = None
    background: bool = False
    hidden: bool = False


def _note(text: str) -> Action:
    return Action("note", text)


def _cmd_action(name: str, run_dir: Path, *rest: str, background: bool = False) -> Action:
    text = _cmd(name, run_dir, *rest)
    argv = tuple(shlex.split(" ".join([name, "--run-dir", _q(run_dir), *rest])))
    return Action("cmd", f"(백그라운드로) {text}" if background else text, argv, background=background)


def _workflow_action(args: Path, out: Path) -> Action:
    return Action("workflow", workflow_step(args, out), args_path=Path(args), out_path=Path(out))


def _shell_action(ledger: Ledger, role: str, prompt: Path, root: str, out: Path) -> Action:
    return Action("shell", review_cmd(ledger, role, prompt, root, out))


def _judge_action(run_dir: Path, rd: Path, no: int, stage: str) -> Action:
    rest = [f"--results {_q(rd / STAGE_OUTPUTS[STAGE1])}"] if has_jobs(rd / STAGE_ARGS[STAGE1]) else []
    if stage == STAGE_CHECKER and has_jobs(rd / STAGE_ARGS[STAGE_CHECKER]):
        rest.append(f"--checker-results {_q(rd / STAGE_OUTPUTS[STAGE_CHECKER])}")
    return _cmd_action("judge", run_dir, f"--round {no}", *rest)


def _judge_ready(rnd: RoundRecord, rd: Path, stage: str) -> bool:
    return not (Path(rd) / f"{stage}-cli-args.json").is_file() or workers_done(rnd, stage)


def _stage_actions(rnd: RoundRecord, run_dir: Path, rd: Path, stage: str) -> list[Action]:
    claude = [_workflow_action(rd / STAGE_ARGS[stage], rd / STAGE_OUTPUTS[stage])] if has_jobs(rd / STAGE_ARGS[stage]) else []
    if not (rd / f"{stage}-cli-args.json").is_file():
        return [*claude, _judge_action(run_dir, rd, rnd.no, stage)]
    if not workers_done(rnd, stage):
        workers = _cmd_action("workers", run_dir, f"--round {rnd.no}", f"--stage {stage}", background=True)
        return [workers, *claude, *([_note("Workflow와 workers는 서로 기다리지 않으므로 두 단계를 동시에 띄운다")] if claude else [])]
    fallback_args, fallback_out = rd / f"{stage}-fallback-args.json", rd / f"{stage}-fallback.json"
    fallback = [_workflow_action(fallback_args, fallback_out)] if fallback_args.is_file() else []
    saved = [replace(a, hidden=a.out_path.is_file()) for a in [*claude, *fallback]]
    return [*saved, _judge_action(run_dir, rd, rnd.no, stage)]


def _stage_steps(rnd: RoundRecord, run_dir: Path, rd: Path, stage: str) -> list[str]:
    return [action.text for action in _stage_actions(rnd, run_dir, rd, stage) if not action.hidden]


def next_actions(ledger: Ledger, run_dir: Path) -> list[Action]:
    rnd = ledger.current_round()
    if rnd is None:
        return [_note("회차가 없다")]
    if ledger.status == STATUS_DONE:
        steps = []
        latest = next((e for e in reversed(rnd.events) if e.get("step") not in NON_PROGRESS_STEPS), {})
        reported = latest.get("step") == "report" and latest.get("round") == rnd.no
        if not reported or not (Path(run_dir) / "report.md").is_file():
            steps.append(_cmd_action("report", run_dir))
        elif not any(step_done(r, "cleanup") for r in ledger.rounds):
            steps.append(_cmd_action("cleanup", run_dir))
        holds = [d["finding_id"] for r in ledger.rounds for d in r.decisions
                 if d["outcome"] == "보류후보" and d["finding_id"] not in ledger.hold_choices]
        if holds:
            steps.append(_note(f"(선택, cleanup 전에) 보류 후보 {', '.join(holds)}의 선택을 받으면: "
                               + _cmd("resume", run_dir, "--choices <JSON>")))
        return steps or [_note("할 일 없음")]
    rd = Path(run_dir) / f"round-{rnd.no}"
    n = f"--round {rnd.no}"
    if not step_done(rnd, "jobs"):
        return [_cmd_action("jobs", run_dir, n)]
    if not step_done(rnd, "judge"):
        if checker_pending(rnd) and (rd / STAGE_ARGS[STAGE_CHECKER]).is_file():
            return _stage_actions(rnd, run_dir, rd, STAGE_CHECKER)
        steps = _stage_actions(rnd, run_dir, rd, STAGE1)
        if _judge_ready(rnd, rd, STAGE1) and any(repro_listed(rd).values()):
            steps.append(_note(f"(재현 명령 {rd / REPRO_FILE} 을 사용자에게 보이고 승인받았으면 judge에 --allow-repro {_q(rd / REPRO_FILE)} 를 붙인다)"))
        return steps
    if (rd / "adj-cli-args.json").is_file() and not step_done(rnd, "adjudicate"):
        if not workers_done(rnd, STAGE_ADJ):
            return [_cmd_action("workers", run_dir, n, "--stage adj", background=True)]
        fallback_args, fallback_out = rd / "adj-fallback-args.json", rd / "adj-fallback.json"
        fallback = [replace(_workflow_action(fallback_args, fallback_out), hidden=fallback_out.is_file())] if has_jobs(fallback_args) else []
        return [*fallback, _cmd_action("adjudicate", run_dir, n)]
    if (rd / "adj-args.json").is_file() and not step_done(rnd, "adjudicate"):
        return [
            _workflow_action(rd / "adj-args.json", rd / "adj.json"),
            _cmd_action("adjudicate", run_dir, n, f"--results {_q(rd / 'adj.json')}"),
        ]
    pending = pending_opinions(ledger, rd)
    if pending and is_claude(ledger, "opinion") and (rd / "opinion-cli-args.json").is_file():
        if not workers_done(rnd, STAGE_OPINION):
            return [_cmd_action("workers", run_dir, n, "--stage opinion", background=True)]
        return [_cmd_action("opinion", run_dir, n, f"--results {_q(rd / 'opinion-cli.json')}")]
    if pending and is_claude(ledger, "opinion"):
        return [
            _workflow_action(rd / OPINION_ARGS, rd / OPINION_RESULTS),
            _cmd_action("opinion", run_dir, n, f"--results {_q(rd / OPINION_RESULTS)}"),
        ]
    if pending:
        fid = pending[0]
        out = rd / f"opinion-{fid}.out"
        return [
            _shell_action(ledger, "opinion", rd / f"opinion-{fid}.md", ledger.root, out),
            _cmd_action("opinion", run_dir, n, f"--id {fid}", f"--result {_q(str(out) + '.final')}"),
        ]
    if not step_done(rnd, "apply"):
        return [_cmd_action("apply", run_dir, n)]
    if rnd.recheck_opened and not step_done(rnd, "recheck") and is_claude(ledger, "recheck"):
        if (rd / RECHECK_CLI_ARGS).is_file():
            if not workers_done(rnd, STAGE_RECHECK):
                return [_cmd_action("workers", run_dir, n, "--stage recheck", background=True)]
            if not (rd / RECHECK_ARGS).is_file():
                return [_cmd_action("recheck", run_dir, n, f"--result {_q(rd / RECHECK_CLI_RESULTS)}")]
        return [
            _workflow_action(rd / RECHECK_ARGS, rd / RECHECK_RESULTS),
            _cmd_action("recheck", run_dir, n, f"--result {_q(rd / RECHECK_RESULTS)}"),
        ]
    if rnd.recheck_opened and not step_done(rnd, "recheck"):
        out = rd / "recheck.out"
        repo_file = rd / RECHECK_REPO_FILE
        repo = repo_file.read_text(encoding="utf-8").strip() if repo_file.is_file() else ledger.root
        return [
            _shell_action(ledger, "recheck", rd / "recheck-prompt.md", repo, out),
            _cmd_action("recheck", run_dir, n, f"--result {_q(str(out) + '.final')}"),
        ]
    return [_cmd_action("report", run_dir)]


def next_steps(ledger: Ledger, run_dir: Path) -> list[str]:
    return [action.text for action in next_actions(ledger, run_dir) if not action.hidden]
