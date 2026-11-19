import argparse
import contextlib
import io
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cascade import assignment
from cascade import cli_steps as s
from cascade import profiles, recommend, setup, skill_scripts, toml_edit
from cascade import usage as workflow_usage
from cascade.config import ConfigError, ExitCodes
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_CARRIED,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
    REASON_SENSITIVE,
    REASON_TESTS_CUT,
    VERDICT_FACT,
    Decision,
    after_adjudication,
    decide,
    decide_doc,
)
from cascade.detect import detect, estimate, estimate_line
from cascade.detect import render as render_detect
from cascade.findings import (
    GH_TIMEOUT,
    SEVERITIES,
    Finding,
    ParseError,
    fetch_pr_findings,
    parse_codex_code,
    parse_codex_doc,
    unparsed_code_items,
    write_findings,
)
from cascade.jobs import (
    RUNNER_CLAUDE,
    build_adjudicator_jobs,
    build_checker_jobs,
    build_stage1_jobs,
    location_file,
    read_results,
    split_job_args,
    write_job_args,
)
from cascade.patches import ApplyReport, apply_all, changed_files, cut_test_files, extract
from cascade.report import render, write_prepared
from cascade.review import (
    CALL_FAILED_NOTE,
    CLAUDE_SOURCES,
    CURSOR_SOURCES,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    EFFORTS,
    REVIEW_JOB,
    REVIEW_META,
    REVIEWER_AUTO,
    REVIEWER_CHOICES,
    REVIEWER_CLAUDE,
    REVIEWER_CODEX,
    REVIEWER_CURSOR,
    build_review_prompt,
    claude_prompt,
    codex_code_review,
    first_review_command,
    label,
    load_text,
    model_warning,
    read_review_results,
    review_job,
)
from cascade.rounds import (
    RESOLUTION_HEADER,
    STATUS_DONE,
    STATUS_RUNNING,
    Ledger,
    RoundRecord,
    build_opinion_prompt,
    build_recheck_prompt,
    classify_recheck,
    out_of_scope,
    parse_recheck,
)
from cascade.runners import (
    DETAIL_TAIL,
    MAX_PARALLEL,
    RUNNER_CODEX,
    RUNNER_CURSOR,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_SKIPPED,
    ClaudeRunner,
    CliJob,
    CodexRunner,
    CursorRunner,
    _execute,
    codex_available,
    codex_model,
    cursor_available,
    find_claude,
    find_codex,
    find_cursor_agent,
    head_of,
    restore_head,
    restore_tree,
    run_all,
    tree_of,
)
from cascade.sandbox_check import ensure as ensure_sandbox
from cascade.snapshot import (
    RUN_DIR_NAME,
    add_worktree,
    cleanup,
    commit_worktree,
    create_snapshot,
    ensure_excluded,
    remove_worktree,
    snapshot_tree,
    user_tree_fingerprint,
)
from cascade.verify import repro_cmds, run_repro, run_verifiers
from cascade.workers import (
    CLAUDE_VIA_CHOICES,
    CLAUDE_VIA_CLI,
    CLAUDE_VIA_WORKFLOW,
    DEFAULT_CURSOR_MODEL,
    WORKERS_CHOICES,
    WORKERS_CLAUDE,
    WORKERS_EXTERNAL,
    describe,
    fallback_counts,
    has_external,
    usage_totals,
    workers_flags,
)

SOURCES = ("codex-doc", "codex-code", "claude-doc", "claude-code", "cursor-doc", "cursor-code", "pr-comment")
HOLD_CHOICES = ("지금 반영", "구현 때 테스트로", "보류")
CHOICE_NOW = "지금 반영"
REASON_APPLY_FAILED = "패치 적용 실패"
REASON_CARRIED = "앞서 적용한 패치와 겹쳐 다음 회차에서 바뀐 코드로 다시 고친다"
REASON_INTERFERENCE = "전체 테스트 실패(간섭 의심)"
REASON_FINAL_BASELINE_FAILED = "전체 테스트 실패(기준선도 실패)"
REASON_OUT_OF_SCOPE = "재검토 범위 밖"
REASON_SEVERITY_DEFERRED = "등급 선택(--severity)으로 이번 실행에서 뺌"
SANDBOX_REASON = "샌드박스 점검 실패"
REVIEW_NO_SCRIPT_REASON = assignment.NO_SCRIPT_REASON
REVIEW_DEFAULT_REASON = "실행 오류"
REVIEW_IGNORED_ENVS = ("CASCADE_WORKERS", "CASCADE_REVIEWER")
FIRST_REVIEW_KEYS = ("role", "requested", "actual", "fallback_reason", "attempts")
RAW_PREVIEW_LINES = 5
WORKTREE_INPUTS = "worktree-inputs.json"
FINAL_TAIL_CHARS = 2000


def _print_next(ledger: Ledger, run_dir: Path) -> None:
    print("다음:")
    for line in s.next_steps(ledger, run_dir):
        print(f"  {line}")


def _set_decision(rnd: RoundRecord, fid: str, outcome: str, reason: str) -> None:
    for d in rnd.decisions:
        if d["finding_id"] == fid:
            if s.is_adopted(d) and not d.get("adopted_as"):
                d["adopted_as"] = d["outcome"]
            d.update(outcome=outcome, patch_from=None, reasons=[*d.get("reasons", []), reason])


def _new_run_id(root: Path) -> str:
    base = f"cf-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_id, n = base, 2
    while (root / RUN_DIR_NAME / run_id).exists():
        run_id, n = f"{base}-{n}", n + 1
    return run_id


def _split_docs(root: Path, docs: list[str]) -> tuple[dict[str, str], list[Path]]:
    names: dict[str, str] = {}
    external: list[Path] = []
    for raw in docs:
        path = Path(raw).resolve()
        if not path.is_file():
            raise s.CliError(f"문서가 없다: {raw}")
        if path.is_relative_to(root):
            rel = path.relative_to(root).as_posix()
            ignored = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root).returncode == 0
            if not ignored:
                names[rel] = str(path)
                continue
        external.append(path)
        names[f"{s.EXTERNAL_PREFIX}{path.name}"] = str(path)
    return names, external


FINDING_REQUIRED = ("id", "severity", "location", "body")
FINDING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _load_findings_json(path: str, source: str) -> list[Finding]:
    data = s.read_json(Path(path))
    if not isinstance(data, list):
        raise s.CliError(f"--findings-json은 지적 객체의 목록이다: {path}", ExitCodes.PARSE_ERROR)
    findings = []
    for i, item in enumerate(data, 1):
        missing = [k for k in FINDING_REQUIRED if not isinstance(item, dict) or k not in item]
        if missing:
            raise s.CliError(f"--findings-json {i}번째 지적에 {', '.join(missing)} 가 없다", ExitCodes.PARSE_ERROR)
        if not isinstance(item["id"], str) or not FINDING_ID.match(item["id"]):
            raise s.CliError(f"--findings-json {i}번째 지적의 id가 형식에 맞지 않는다: {item['id']!r}", ExitCodes.PARSE_ERROR)
        findings.append(Finding.from_dict({"source": source, "evidence": "", **item}))
    ids = [f.id for f in findings]
    duplicated = sorted({fid for fid in ids if ids.count(fid) > 1})
    if duplicated:
        raise s.CliError(f"--findings-json에 id가 겹친다: {', '.join(duplicated)}", ExitCodes.PARSE_ERROR)
    return findings


def _run_gh(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 1, "", f"시간 초과({GH_TIMEOUT}초)")


def _warn_pr_head(root: Path, gh: list[str], owner: str, name: str, number: str) -> None:
    p = _run_gh([*gh, "pr", "view", number, "-R", f"{owner}/{name}", "--json", "headRefOid", "--jq", ".headRefOid"])
    pr_head = p.stdout.strip() if p.returncode == 0 else ""
    local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    if not pr_head:
        print(f"경고: PR #{number}의 head 커밋을 확인하지 못했다. 로컬 HEAD가 PR 브랜치인지 직접 확인한다")
    elif pr_head != local:
        print(f"경고: 로컬 HEAD({local[:12]})가 PR #{number}의 head({pr_head[:12]})와 다르다. PR 브랜치를 받아 온 뒤 실행한다")


def _parse_input(args, root: Path, expected_model: str):
    if args.findings_json:
        return _load_findings_json(args.findings_json, args.source), ""
    if not args.input:
        raise s.CliError("--input 또는 --findings-json 중 하나를 준다")
    if args.source == "pr-comment":
        if args.pr_repo:
            owner, _, name = args.pr_repo.partition("/")
        else:
            url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True)
            owner, name = s.parse_remote(url.stdout)
        gh = shlex.split(os.environ.get("CASCADE_GH", "gh"))
        me = args.me
        if me is None:
            p = _run_gh([*gh, "api", "user", "--jq", ".login"])
            me = p.stdout.strip() if p.returncode == 0 else ""
            if not me:
                print("경고: 내 계정을 알 수 없어 본인 코멘트를 거르지 못한다(--me 로 준다)")
        if not args.input.isdigit():
            raise s.CliError(f"pr-comment의 --input은 PR 번호다: {args.input}")
        _warn_pr_head(root, gh, owner, name, args.input)
        warnings: list[str] = []
        found = fetch_pr_findings(owner, name, int(args.input), me, gh, 1, warnings)
        for warning in warnings:
            print(warning)
        return found, ""
    if args.source in CLAUDE_SOURCES:
        return _parse_claude_review(args, expected_model)
    raw = s.read_text(Path(args.input))
    try:
        if args.source in CURSOR_SOURCES:
            return [replace(f, source=args.source) for f in parse_codex_doc(raw, 1)], raw
        parser = parse_codex_doc if args.source == "codex-doc" else parse_codex_code
        return parser(raw, 1), raw
    except ParseError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None


def _parse_claude_review(args, expected_model: str):
    path = Path(args.input)
    if not path.is_file():
        raise s.CliError(f"파일이 없다: {path}")
    raw, model_id = load_text(path, REVIEW_JOB if path.suffix == ".json" else None)
    if not raw.strip():
        raise s.CliError(f"Claude 검토 결과가 비어 있다(Workflow 호출 실패일 수 있다): {path}", ExitCodes.PARSE_ERROR)
    warning = model_warning(REVIEW_JOB, model_id, expected_model)
    if warning:
        print(warning)
    try:
        findings = parse_codex_doc(raw, 1)
    except ParseError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None
    return [replace(f, source=args.source) for f in findings], raw


def _probe(available: dict[str, bool], runners) -> dict[str, bool]:
    for runner in runners:
        if runner not in available:
            available[runner] = codex_available() if runner == RUNNER_CODEX else cursor_available()
    return available


def _cursor_reviewer_ok(available: dict[str, bool]):
    return lambda: s.CURSOR_REVIEW.is_file() and _probe(available, (RUNNER_CURSOR,))[RUNNER_CURSOR]


def _resolve_profile(args, root: Path, available: dict[str, bool]) -> profiles.Resolved:
    """--workers external이 실제로 쓰일 때만 외부 CLI를 미리 확인한다. CASCADE_WORKERS는 --workers가 없고
    프로필을 명시하지 않았을 때만 쓰이므로, 그때는 한 번 풀어 본 뒤 확인하고 다시 푼다."""
    env = os.environ
    cli = profiles.LegacyOptions(
        workers=args.workers, cursor_model=args.cursor_model, reviewer=args.reviewer,
        review_model=getattr(args, "review_model", None), review_effort=getattr(args, "review_effort", None),
        adopt_on_both_pass=getattr(args, "adopt_on_both_pass", False))

    def resolve() -> profiles.Resolved:
        try:
            return profiles.resolve(root, s.repos_toml(), flag=args.profile, cli=cli, env=env, available=available,
                                    codex_model=codex_model(Path.home() / ".codex"))
        except ConfigError as e:
            raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None

    if args.workers == WORKERS_EXTERNAL:
        _probe(available, (RUNNER_CODEX, RUNNER_CURSOR))
    resolved = resolve()
    if args.workers is None and env.get("CASCADE_WORKERS") == WORKERS_EXTERNAL and not resolved.explicit:
        _probe(available, (RUNNER_CODEX, RUNNER_CURSOR))
        resolved = resolve()
    _probe(available, assignment.runners_to_check(resolved.roles, resolved.pinned))
    return resolved


def _decide_roles(args, resolved: profiles.Resolved, source: str, available: dict[str, bool]) -> tuple[dict, list[dict]]:
    try:
        return assignment.decide(resolved.roles, resolved.pinned, source, s.RUN_REVIEW, _cursor_reviewer_ok(available),
                                 available, cursor_model=args.cursor_model or DEFAULT_CURSOR_MODEL, name=resolved.name,
                                 scripts=_review_scripts())
    except profiles.ProfileError as e:
        raise s.CliError(str(profiles.explain_overrides(e, resolved.overrides)), ExitCodes.PARSE_ERROR) from None


def _review_scripts() -> dict[str, bool]:
    return {RUNNER_CODEX: s.RUN_REVIEW.is_file(), RUNNER_CURSOR: s.CURSOR_REVIEW.is_file()}


def _consent_option(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if names == ["none"]:
        return set()
    if not names or any(name not in assignment.EXTERNAL_RUNNERS for name in names):
        raise s.CliError(f"--consent는 {','.join(assignment.EXTERNAL_RUNNERS)} 중에서 쉼표로 고르거나 none이다: {raw}")
    return set(names)


def _read_optional_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _first_review_meta(args, mode: str) -> dict:
    if args.findings_json or not args.input or args.source == "pr-comment":
        return {}
    path = Path(args.input).parent / REVIEW_META
    if not path.is_file():
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"경고: {path}를 읽지 못해 첫 검토 이력을 남기지 못한다")
        return {}
    if not isinstance(meta, dict) or meta.get("role") != f"{mode}.review":
        return {}
    first_review = {key: meta[key] for key in FIRST_REVIEW_KEYS if key in meta}
    if isinstance(measured := _read_optional_json(path.parent / s.USAGE_FILE), dict):
        first_review["usage"] = measured
    if isinstance(first_review.get("requested"), dict):
        first_review["requested"] = {key: first_review["requested"][key] for key in ("runner", "model", "effort")
                                     if key in first_review["requested"]}
    return first_review


def _expected_review_model(args, resolved: profiles.Resolved, mode: str, first_review: dict) -> str:
    actual = first_review.get("actual") or {}
    if actual.get("runner") == REVIEWER_CLAUDE:
        return actual["model"]
    if args.review_model and not resolved.explicit:
        return args.review_model
    return resolved.roles[f"{mode}.review"]["model"] or DEFAULT_MODEL


def cmd_init(args) -> int:
    return _init(args)[0]


def _init(args, print_next: bool = True) -> tuple[int, Path | None]:
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    s.cfg_for(args.source, root)
    consent = _consent_option(args.consent)
    available: dict[str, bool] = {}
    resolved = _resolve_profile(args, root, available)
    mode = "doc" if args.source in s.DOC_SOURCES else "code"
    first_review = _first_review_meta(args, mode)
    findings, raw = _parse_input(args, root, _expected_review_model(args, resolved, mode, first_review))
    skipped = unparsed_code_items(raw) if args.source == "codex-code" else []
    if not findings:
        print("지적 0건 — 원문을 확인하라")
        for line in raw.splitlines()[:RAW_PREVIEW_LINES]:
            print(f"  | {line}")
        if "[P" in raw and not skipped:
            print("경고: 원문에 「[P」가 있다. 파서가 항목을 놓쳤을 수 있다")
    if skipped:
        print(f"경고: 형식을 읽지 못한 항목 {len(skipped)}건: {' / '.join(skipped)}")
    if not findings:
        print(f"status={STATUS_DONE}")
        return 0, None
    keep = _severity_filter(args.severity)
    deferred = [f for f in findings if f.severity not in keep]
    findings = [f for f in findings if f.severity in keep]
    if not findings:
        raise s.CliError(f"--severity {args.severity} 에 맞는 지적이 없다(전체 {len(deferred)}건). 등급을 넓힌다")
    docs, external = _split_docs(root, args.doc or [])
    roles, substituted = _decide_roles(args, resolved, args.source, available)
    roles = assignment.fill_codex_model(roles, codex_model(Path.home() / ".codex"))
    if consent is not None:
        roles, dropped = assignment.apply_consent(roles, consent)
        substituted = [*substituted, *dropped]
    consented = assignment.used_recipients(roles, mode)
    record = {"name": resolved.name, "source": resolved.source, "explicit": resolved.explicit, "roles": roles,
              "overrides": resolved.overrides, "substituted": substituted, "consented": consented, "first_review": first_review}
    requested = args.workers or (None if resolved.explicit else os.environ.get("CASCADE_WORKERS") or None)
    legacy = assignment.legacy_fields({**record, "adopt_on_both_pass": resolved.adopt_on_both_pass}, mode, requested)
    reviewer, workers, workers_choice = legacy["reviewer"], legacy["workers"], legacy["workers_choice"]
    ensure_excluded(root)
    snap = create_snapshot(root, _new_run_id(root), external)
    stamp = s.now()
    ledger = Ledger(
        run_id=snap.run_id,
        source=args.source,
        mode=mode,
        root=str(root),
        snapshot=snap.to_dict(),
        dry_run=args.dry_run,
        max_rechecks=args.max_rechecks,
        rounds=[RoundRecord(no=1, findings=[f.to_dict() for f in findings],
                            decisions=[Decision(f.id, OUTCOME_HOLD, None, [REASON_SEVERITY_DEFERRED]).to_dict() for f in deferred],
                            adopted=[], recheck_opened=False, started_at=stamp, events=[{"step": "init", "at": stamp}],
                            base_commit=snap.commit, hold_findings=[f.to_dict() for f in deferred])],
        docs=docs,
        reviewer=reviewer,
        review_model=legacy["review_model"],
        review_effort=legacy["review_effort"],
        workers_choice=workers_choice,
        workers=workers,
        adopt_on_both_pass=legacy["adopt_on_both_pass"],
        profile=record,
        claude_via=getattr(args, "claude_via", None) or CLAUDE_VIA_WORKFLOW,
    )
    write_findings(s.round_dir(snap.run_dir, 1) / "findings.json", findings)
    s.save_ledger(snap.run_dir, ledger)
    if first_review:
        s.write_json(Path(args.input).parent / REVIEW_USED, {"run_dir": str(snap.run_dir)})
    counts = Counter(f.severity for f in findings)
    print(f"run_dir={snap.run_dir}")
    print(f"status={STATUS_RUNNING}")
    print(f"지적 {len(findings)}건: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if deferred:
        print(f"등급 선택으로 뺀 지적 {len(deferred)}건은 보류 후보로 둔다(나중에 resume 「지금 반영」으로 돌릴 수 있다)")
    _print_assignment(resolved, roles, substituted, mode, first_review)
    print(f"규모: {estimate_line(len(findings), ledger.mode, reviewer, workers, roles if resolved.explicit else None)}")
    if workers_choice == WORKERS_EXTERNAL:
        if has_external(workers):
            print(f"작업자: {describe(workers)}")
        else:
            print("작업자: 쓸 수 있는 외부 CLI가 없다. 모두 Claude로 돈다(--workers claude 와 같다)")
    if args.dry_run:
        print("dry-run: 채택 패치를 작업 트리에 적용하지 않는다")
    if ledger.adopt_on_both_pass:
        print("한쪽 채택: 같은 판정에 패치가 달라도 둘 다 검증을 통과하면 작은 패치를 채택한다(판가름 안 함)")
    _print_review_cells(ledger)
    if print_next:
        _print_next(ledger, snap.run_dir)
    return 0, snap.run_dir


def _print_review_cells(ledger: Ledger) -> None:
    opinion, recheck = (s.review_cell(ledger, role) for role in ("opinion", "recheck"))
    same = all(opinion[k] == recheck[k] for k in ("runner", "model", "effort"))
    for what, role in (("의견·재검토", "opinion"),) if same else (("의견", "opinion"), ("재검토", "recheck")):
        cell = s.review_cell(ledger, role)
        if cell["runner"] == REVIEWER_CLAUDE:
            print(f"{what}: Claude 단독({cell['model']}, {cell['effort']}) — 외부 전송 없음")
            continue
        script = s.review_script(ledger, role)
        print(f"{what}: {label(cell['runner'])}({script})")
        if not script.is_file():
            skill = "cursor-review" if cell["runner"] == REVIEWER_CURSOR else "codex-review"
            print(f"경고: {skill} 스킬이 없다. Claude 단독으로 돌리려면 --reviewer claude 로 다시 init 한다")


def _print_assignment(resolved: profiles.Resolved, roles: dict, substituted: list[dict], mode: str,
                      first_review: dict | None = None) -> None:
    print(f"프로필: {resolved.name} ({resolved.source})")
    for notice in resolved.notices:
        print(f"참고: {notice}")
    if resolved.overrides:
        print(assignment.overrides_line(resolved.overrides))
    for record in substituted:
        if record["role"].startswith(f"{mode}.") and not record["role"].startswith(f"{mode}.review"):
            print(f"대체: {record['role']} {record['from']} → {record['to']} — {record['reason']}")
    pending = {key: cell for key, cell in roles.items() if key != f"{mode}.review"}
    print(assignment.roles_line(pending, mode, first_review))
    consent = assignment.consent_line(pending, mode)
    if consent:
        print(consent)


def _severity_filter(raw: str | None) -> set[str]:
    if not raw:
        return set(SEVERITIES)
    chosen = {x.strip().capitalize() for x in raw.split(",") if x.strip()}
    unknown = chosen - set(SEVERITIES)
    if unknown or not chosen:
        raise s.CliError(f"--severity 는 {', '.join(SEVERITIES)} 중에서 쉼표로 고른다: {raw}")
    return chosen


def _root_flag(args, root: Path) -> str:
    """--root 로 다른 저장소를 가리켰으면 다음 명령에도 붙인다(빼면 현재 디렉터리의 저장소가 대상이 된다)."""
    return f" --root {shlex.quote(str(root))}" if args.root else ""


def cmd_detect(args) -> int:
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    gh = None if args.no_gh else shlex.split(os.environ.get("CASCADE_GH", "gh"))
    available: dict[str, bool] = {}
    resolved = _resolve_profile(args, root, available)
    env = {} if resolved.explicit else os.environ
    reviewer = args.reviewer or env.get("CASCADE_REVIEWER")
    reviewer_choice = reviewer or REVIEWER_AUTO
    workers_choice = args.workers or env.get("CASCADE_WORKERS") or WORKERS_CLAUDE
    cursor_model = args.cursor_model or DEFAULT_CURSOR_MODEL
    try:
        result = detect(root, repos_toml=s.repos_toml(), run_review=s.RUN_REVIEW, cursor_ok=_cursor_reviewer_ok(available), cli=s.CLI, gh=gh,
                        input_path=Path(args.input).resolve() if args.input else None, pr=args.pr,
                        reviewer_choice=reviewer_choice, root_flag=_root_flag(args, root),
                        docs=[Path(d).resolve() for d in args.doc or []], cwd=Path.cwd(),
                        workers_choice=workers_choice, cursor_model=cursor_model,
                        profile=resolved, available=available, profile_flag=args.profile,
                        codex_model=codex_model(Path.home() / ".codex"), scripts=_review_scripts(),
                        reviewer_flag=f" --reviewer {reviewer}" if reviewer else "",
                        workers_flag=_next_workers_flags(args, resolved.explicit))
    except profiles.ProfileError as e:
        raise s.CliError(str(profiles.explain_overrides(e, resolved.overrides)), ExitCodes.PARSE_ERROR) from None
    shown = result["profile"]
    result["notes"].extend(_setup_notes(_notice_roles(resolved.roles, shown["roles"], shown["substituted"])))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_detect(result))
    return 0 if result["ok"] else ExitCodes.NEEDS_USER


def _review_scope(args, root: Path) -> dict:
    docs = [str(Path(raw).resolve()) for raw in args.doc or []]
    if args.kind == "doc":
        if not args.doc:
            raise s.CliError("문서 검토는 --doc <문서> 를 하나 이상 준다")
        for raw, path in zip(args.doc, docs):
            if not Path(path).is_file():
                raise s.CliError(f"문서가 없다: {raw}")
    elif sum(bool(x) for x in (args.base, args.uncommitted, args.commit)) != 1:
        raise s.CliError("코드 검토는 --base <브랜치> / --uncommitted / --commit <SHA> 중 하나를 준다")
    return {"kind": args.kind, "base": args.base, "uncommitted": args.uncommitted, "commit": args.commit,
            "docs": docs, "focus": args.focus or "", "root": str(root)}


def _review_prompt(scope: dict) -> str:
    root = Path(scope["root"])
    if scope["kind"] == "doc":
        docs = [Path(d).relative_to(root).as_posix() if Path(d).is_relative_to(root) else d for d in scope["docs"]]
        return build_review_prompt("doc", root, docs=docs, focus=scope["focus"])
    return build_review_prompt("code", root, base=scope["base"], uncommitted=scope["uncommitted"],
                               commit=scope["commit"], focus=scope["focus"])


def _review_profile(args, root: Path, flag: str | None) -> profiles.Resolved:
    env = {name: value for name, value in os.environ.items() if name not in REVIEW_IGNORED_ENVS}
    try:
        return profiles.resolve(root, s.repos_toml(), flag=flag, cli=profiles.LegacyOptions(cursor_model=args.cursor_model),
                                env=env, available={}, codex_model="")
    except ConfigError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None


def _first_review_cell(resolved: profiles.Resolved, mode: str) -> dict:
    try:
        roles = assignment.resolve_auto(resolved.roles, f"claude-{mode}", s.RUN_REVIEW, False, name=resolved.name)
    except profiles.ProfileError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None
    return roles[f"{mode}.review"]


def _apply_review_options(args, name: str, key: str, cell: dict) -> dict:
    options = [(field, flag, value) for field, flag, value
               in (("model", "--review-model", args.review_model), ("effort", "--review-effort", args.review_effort)) if value]
    if not options:
        return cell
    if cell["runner"] != REVIEWER_CLAUDE:
        flags = " ".join(f"{flag} {value}" for _, flag, value in options)
        raise s.CliError(f"{key} 칸의 runner가 {cell['runner']}라 {flags}를 쓸 수 없다. 두 옵션은 claude 칸에만 쓴다",
                         ExitCodes.PARSE_ERROR)
    notes = [f"{key} ← {flag} {value}" for _, flag, value in options]
    out = {**cell, **{field: value for field, _, value in options}}
    try:
        profiles.validate(name, {"adopt_on_both_pass": True, "roles": {**profiles.builtin("claude-only")["roles"], key: out}})
    except profiles.ProfileError as e:
        raise s.CliError(str(profiles.explain_overrides(e, notes)), ExitCodes.PARSE_ERROR) from None
    return out


def _first_review_unusable(runner: str, available: dict[str, bool]) -> str | None:
    if runner == REVIEWER_CLAUDE:
        return None
    if not _probe(available, (runner,))[runner]:
        return assignment.UNAVAILABLE_REASON
    script = s.CURSOR_REVIEW if runner == REVIEWER_CURSOR else s.RUN_REVIEW
    return None if script.is_file() else REVIEW_NO_SCRIPT_REASON


def _consent_first_review(cell: dict, key: str, mode: str, consent: set[str] | None) -> tuple[dict, list[str], list[dict]]:
    if consent is None:
        return cell, assignment.used_recipients({key: cell}, mode), []
    roles, _ = assignment.apply_consent({key: cell}, consent)
    attempts = [{"runner": cell["runner"], "reason": assignment.NO_CONSENT_REASON}] if roles[key]["runner"] != cell["runner"] else []
    return {**cell, "fallback": roles[key]["fallback"]}, assignment.used_recipients(roles, mode), attempts


def _first_review_target(cell: dict, consented: set[str], attempts: list[dict], available: dict[str, bool]) -> dict:
    def target() -> dict:
        return assignment.fallback_target(cell, consented, {a["runner"] for a in attempts}, "reviewer")

    current = target() if attempts else cell
    while reason := _first_review_unusable(current["runner"], available):
        attempts.append({"runner": current["runner"], "reason": reason})
        current = target()
    return current


def _new_review_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir, n = root / RUN_DIR_NAME / f"review-{stamp}", 2
    while out_dir.exists():
        out_dir, n = root / RUN_DIR_NAME / f"review-{stamp}-{n}", n + 1
    return out_dir


def _runner_values(cell: dict) -> dict:
    return {key: cell[key] for key in ("runner", "model", "effort")}


def _emit_first_review(args, review_dir: Path, meta: dict, new_attempts: list[dict], root_flag: str) -> None:
    scope, actual, key = meta["args"], meta["actual"], meta["role"]
    root = Path(scope["root"])
    source = f"{actual['runner']}-{scope['kind']}"
    if actual["runner"] == REVIEWER_CLAUDE:
        model, effort = actual["model"], actual["effort"]
        job = review_job(REVIEW_JOB, f"{scope['kind']} review", claude_prompt(_review_prompt(scope), root), model, effort)
        args_path, result = review_dir / "review-args.json", review_dir / "review.json"
        s.write_json(args_path, write_job_args({"jobs": [job]}, review_dir / "prompts"))
        reviewer = f"Claude({model}, {effort})"
        step = s.workflow_step(args_path, result)
    else:
        step, result = first_review_command(actual, scope, review_dir)
        reviewer = label(actual["runner"]) + (f"({actual['model']})" if actual["model"] else "")
    s.write_json(review_dir / REVIEW_META, meta)
    print(f"review_dir={review_dir}")
    for i, attempt in enumerate(new_attempts):
        to = new_attempts[i + 1]["runner"] if i + 1 < len(new_attempts) else actual["runner"]
        print(f"대체: {key} {attempt['runner']} → {to} — {attempt['reason']}")
    print(f"검토자: {reviewer}")
    if scope["focus"] and codex_code_review(actual, scope):
        print("경고: Codex 코드 검토는 --focus를 받지 못한다(codex review는 범위와 지시문을 함께 받지 않는다). 관점 없이 검토한다")
    if meta["explicit"]:
        carried = f" --profile {shlex.quote(meta['profile'])}"
    else:
        carried = ((f" --review-model {shlex.quote(args.review_model)}" if args.review_model else "")
                   + (f" --review-effort {args.review_effort}" if args.review_effort else ""))
    docs_args = "".join(f" --doc {shlex.quote(d)}" for d in scope["docs"])
    consent = f" --consent {','.join(meta['consent']) or 'none'}" if "consent" in meta else ""
    print("다음:")
    print(f"  {step}")
    print(f"  {s.CLI} init --source {source} --input {shlex.quote(str(result))}{docs_args}{carried}"
          f"{meta.get('workers_flags', '')}{consent}{root_flag}")


def _next_workers_flags(args, explicit: bool) -> str:
    """detect·review에 준 작업자 옵션을 다음 명령으로 넘긴다. 준 값은 기본값과 같아도 넘기고,
    CASCADE_WORKERS는 init과 같은 규칙으로 읽어(프로필을 명시하면 읽지 않는다) external일 때만 넘긴다."""
    env_value = None if explicit else os.environ.get("CASCADE_WORKERS")
    return workers_flags(args.workers or (env_value if env_value == WORKERS_EXTERNAL else None), args.cursor_model)


def _load_review_meta(review_dir: Path) -> dict:
    path = review_dir / REVIEW_META
    if not path.is_file():
        raise s.CliError(f"{path}가 없다. CF review가 만든 첫 검토 폴더를 --review-dir로 준다", ExitCodes.PARSE_ERROR)
    meta = s.read_json(path)
    try:
        [meta[name] for name in ("role", "actual", "attempts", "profile", "explicit", "consented")]
        meta["actual"]["runner"], meta["requested"]["fallback"]["runner"], meta["args"]["root"]
    except (KeyError, TypeError):
        raise s.CliError(f"{path}의 형식이 맞지 않는다", ExitCodes.PARSE_ERROR) from None
    return meta


def _review_fallback(args) -> int:
    if not args.fallback_from:
        raise s.CliError("--review-dir은 --fallback-from과 함께 쓴다", ExitCodes.PARSE_ERROR)
    if not args.review_dir:
        raise s.CliError("--fallback-from은 처음 첫 검토 폴더를 가리키는 --review-dir과 함께 쓴다", ExitCodes.PARSE_ERROR)
    review_dir = Path(args.review_dir).resolve()
    meta = _load_review_meta(review_dir)
    current = meta["actual"]["runner"]
    if args.fallback_from not in assignment.EXTERNAL_RUNNERS or current != args.fallback_from:
        raise s.CliError(f"--fallback-from {args.fallback_from}가 지금 첫 검토를 맡은 runner({current})와 다르다", ExitCodes.PARSE_ERROR)
    scope, key = meta["args"], meta["role"]
    attempts = [*meta["attempts"], {"runner": args.fallback_from, "reason": args.reason or REVIEW_DEFAULT_REASON}]
    added = len(meta["attempts"])
    actual = _apply_review_options(args, meta["profile"], key,
                                   _first_review_target(meta["requested"], set(meta["consented"]), attempts, {}))
    meta = {**meta, "actual": _runner_values(actual), "fallback_reason": attempts[-1]["reason"], "attempts": attempts}
    _emit_first_review(args, review_dir, meta, attempts[added:], f" --root {shlex.quote(scope['root'])}")
    return 0


def cmd_review(args) -> int:
    if args.review_dir or args.fallback_from:
        return _review_fallback(args)
    if not args.kind:
        build_parser().error("the following arguments are required: --kind")
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    scope = _review_scope(args, root)
    consent = _consent_option(args.consent)
    resolved = _review_profile(args, root, args.profile)
    key = f"{args.kind}.review"
    requested = _apply_review_options(args, resolved.name, key, _first_review_cell(resolved, args.kind))
    cell, consented, attempts = _consent_first_review(requested, key, args.kind, consent)
    actual = _first_review_target(cell, set(consented), attempts, {})
    workers = _next_workers_flags(args, resolved.explicit)
    ensure_excluded(root)
    meta = {"role": key, "requested": {**_runner_values(requested), "fallback": _runner_values(cell["fallback"])},
            "actual": _runner_values(actual), "fallback_reason": attempts[-1]["reason"] if attempts else None,
            "attempts": attempts, "consented": consented, "args": scope, "profile": resolved.name,
            "explicit": resolved.explicit, **({"workers_flags": workers} if workers else {}),
            **({"consent": sorted(consent)} if consent is not None else {})}
    _emit_first_review(args, _new_review_dir(root), meta, attempts, _root_flag(args, root))
    return 0


LOSSY_FILE = "lossy.json"
PRE_FINAL_DECISIONS = "decisions-pre-final.json"
_JUDGE_OUTPUTS = ("decisions.json", "results.json", "verify.json", LOSSY_FILE, "adj-args.json", "adj.json", s.OPINION_ARGS,
                  s.OPINION_RESULTS, s.STAGE_ARGS[s.STAGE_CHECKER], f"{s.STAGE_CHECKER}-cli-args.json",
                  s.STAGE_OUTPUTS[s.STAGE_CHECKER])


def _reopen_round(ledger, rnd, rd: Path) -> None:
    """jobs 를 다시 하면 그 뒤 단계(작업자·판정·의견·판가름)가 남긴 상태를 지워 이전 판정이 이어지지 않게 한다."""
    if s.step_done(rnd, "apply") or s.step_done(rnd, "apply-patched"):
        raise s.CliError(f"회차 {rnd.no}는 이미 apply 했다(패치 적용 포함). 적용한 회차는 jobs 로 다시 열지 않는다")
    first = next(i for i, e in enumerate(rnd.events) if e.get("step") == "jobs")
    rnd.events = rnd.events[:first]
    held = {f["id"] for f in rnd.hold_findings}
    rnd.decisions = [d for d in rnd.decisions if d["finding_id"] in held]
    for f in rnd.findings:
        ledger.opinions.pop(f["id"], None)
    for name in _JUDGE_OUTPUTS:
        (rd / name).unlink(missing_ok=True)
    if (rd / s.USAGE_FILE).is_file():
        k = next(n for n in itertools.count(1) if not (rd / f"claude-usage.superseded-{n}.json").exists())
        (rd / s.USAGE_FILE).rename(rd / f"claude-usage.superseded-{k}.json")
    s.clear_cli_outputs(rd, s.STAGE_ADJ, s.STAGE_OPINION)
    (rd / "adj-cli-args.json").unlink(missing_ok=True)
    for path in rd.glob("opinion-*"):
        path.unlink()


def cmd_jobs(args) -> int:
    with s.workers_lock(Path(args.run_dir)):
        return _jobs(args)


def _jobs(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "jobs", args.force)
    if getattr(args, "claude_via", None):
        ledger.claude_via = args.claude_via
    if s.step_done(rnd, "jobs"):
        _reopen_round(ledger, rnd, s.round_dir(run_dir, rnd.no))
    cfg = s.cfg_for(ledger.source, Path(ledger.root))
    snap = s.snapshot_of(ledger)
    base = s.base_of(ledger, rnd)
    base_wt = snap.worktrees.get(s.base_name(rnd)) or s.new_worktree(snap, s.base_name(rnd), base, cfg)
    roles = ("a", "b", "writer") if ledger.mode == "doc" else ("a", "b")
    findings = s.prompt_findings(ledger, s.findings_of(rnd))
    wts: dict[str, dict[str, Path]] = {}
    for f in findings:
        wts[f.id] = {}
        for role in roles:
            name = s.wt_name(f.id, role)
            wts[f.id][role] = snap.worktrees[name] if name in snap.worktrees else s.new_worktree(snap, name, base, cfg)
    rd = s.round_dir(run_dir, rnd.no)
    _pin_worktrees(rd / WORKTREE_INPUTS, [wt for roles_wt in wts.values() for wt in roles_wt.values()])
    failed: list[str] = []
    if ledger.mode == "code" and cfg.tests:
        located = list(dict.fromkeys(p for f in findings if (p := location_file(f.location))))
        failed = s.write_baseline(rd, base_wt, cfg, located)
        if failed:
            print(f"기준선에서 이미 실패하는 검증 명령(판정에서 제외): {', '.join(failed)}")
    job_workers = assignment.job_workers(ledger)
    if ledger.mode == "code" and has_external(job_workers):
        checked, notices = ensure_sandbox(Path(ledger.root), cfg, base_wt, job_workers, failed,
                                          consented=assignment.ledger_profile(ledger)["consented"])
        for notice in notices:
            print(notice)
        if notices:
            assignment.apply_sandbox_substitution(ledger, job_workers, checked, SANDBOX_REASON)
            s.save_ledger(run_dir, ledger)
            job_workers = assignment.job_workers(ledger)
    if ledger.source == s.SOURCE_PR:
        listed = {f.id: repro_cmds(f) for f in findings}
        s.write_json(rd / s.REPRO_FILE, listed)
        if any(listed.values()):
            print(f"재현 명령(승인 전에는 돌리지 않는다): {rd / s.REPRO_FILE}")
            for fid, cmds in listed.items():
                for cmd in cmds:
                    print(f"  {fid}: {cmd}")
    out = rd / "stage1-args.json"
    claude_cli = ledger.claude_via == CLAUDE_VIA_CLI
    claude_args, cli_args = split_job_args(
        build_stage1_jobs(findings, cfg, wts, ledger.mode, assignment.build_workers(ledger), claude_cli), rd / "prompts", claude_cli)
    s.write_json(out, claude_args)
    cli_out = rd / "stage1-cli-args.json"
    if cli_args["jobs"]:
        s.write_json(cli_out, cli_args)
    else:
        cli_out.unlink(missing_ok=True)
    s.clear_cli_outputs(rd, s.STAGE1, s.STAGE_CHECKER)
    for stage in (s.STAGE1, s.STAGE_CHECKER):
        (rd / s.STAGE_OUTPUTS[stage]).unlink(missing_ok=True)
    ledger.snapshot = snap.to_dict()
    fingerprint = user_tree_fingerprint(snap.root, snap.run_dir)
    s.mark(rnd, "jobs", worktrees={fid: {r: str(p) for r, p in m.items()} for fid, m in wts.items()},
           fingerprint=fingerprint)
    s.save_ledger(run_dir, ledger)
    print(f"작업 {len(claude_args['jobs'])}건: {out}")
    if cli_args["jobs"]:
        print(f"CLI 작업 {len(cli_args['jobs'])}건: {cli_out}")
    _print_next(ledger, run_dir)
    return 0


def _cli_job(entry: dict) -> CliJob:
    return CliJob(entry["id"], entry["role"], entry["kind"], entry["runner"], entry["model"], entry["effort"],
                  Path(entry["promptPath"]), Path(entry["worktree"]), entry["writable"])


_CLI_MAKERS = {RUNNER_CODEX: (find_codex, CodexRunner), RUNNER_CURSOR: (find_cursor_agent, CursorRunner),
               RUNNER_CLAUDE: (find_claude, ClaudeRunner)}


def _cli_runners(jobs: list[CliJob]) -> dict[str, object]:
    runners = {}
    for name in {job.runner for job in jobs} & _CLI_MAKERS.keys():
        find, make = _CLI_MAKERS[name]
        exe = find()
        if exe:
            runners[name] = make(exe)
    return runners


def _run_cli_jobs(jobs: list[CliJob], exhausted: dict[str, dict], max_parallel: int = MAX_PARALLEL) -> list:
    """실행기를 만들어 돌리고, 상주 서버(Cursor ACP)를 둔 실행기는 끝나면 닫는다."""
    runners = _cli_runners(jobs)
    try:
        return run_all(jobs, runners, max_parallel=max_parallel, exhausted=exhausted)
    finally:
        for runner in runners.values():
            close = getattr(runner, "close", None)
            if close is not None:
                close()


def _pin_worktrees(path: Path, worktrees: list[Path]) -> None:
    """작업 워크트리를 회차의 첫 입력 상태로 맞춘다. 처음 보는 워크트리는 지금 상태를 적어 두고, 적어 둔 워크트리는 그 상태로 되돌린다."""
    saved = s.read_json(path) if path.is_file() else {}
    for wt in worktrees:
        key = str(Path(wt).resolve())
        if key not in saved:
            saved[key] = [tree_of(wt), head_of(wt)]
            continue
        tree, head = saved[key]
        try:
            restore_head(wt, head)
            restore_tree(wt, tree)
        except Exception as e:
            raise s.CliError(f"워크트리를 회차의 첫 입력 상태로 되돌리지 못했다({wt}): {e}") from None
    s.write_json(path, saved)


def _rerun_with_fallbacks(entries: list[dict], first: list, cells: dict[str, dict], consented: set[str],
                          exhausted: dict[str, dict]) -> tuple[dict[str, dict], dict[str, object]]:
    by_id = {e["id"]: e for e in entries}
    targets: dict[str, dict] = {}
    for outcome in first:
        if outcome.status == STATUS_OK:
            continue
        role = by_id[outcome.id]["role"]
        target = assignment.fallback_target(cells.get(role, {}), consented, set(exhausted), role)
        if target["runner"] != RUNNER_CLAUDE:
            targets[outcome.id] = target
    jobs = [CliJob(i, by_id[i]["role"], by_id[i]["kind"], t["runner"], t["model"], t["effort"],
                   Path(by_id[i]["promptPath"]), Path(by_id[i]["worktree"]), by_id[i]["writable"])
            for i, t in targets.items()]
    if not jobs:
        return {}, {}
    return targets, {o.id: o for o in _run_cli_jobs(jobs, exhausted)}


def cmd_workers(args) -> int:
    with s.workers_lock(Path(args.run_dir)):
        return _workers(args)


def _record_claude_usage(rd: Path, mode: str, entries: list[dict], outcomes: list, stage: str) -> None:
    """Claude 러너의 호출(대체 전 실패 포함)마다 봉투의 토큰·비용을 claude-usage.json에 시도별로 남긴다. 봉투가 없던 시도는 비용 없는 행(미측정)으로 남긴다. 다시 돌려도 앞 시도를 덮지 않는다."""
    by_id = {e["id"]: e for e in entries}
    rows = [{"job_id": o.id, "label": o.id, "cell": assignment.job_cell_key(mode, by_id[o.id]["role"]), "via": CLAUDE_VIA_CLI, "model": by_id[o.id]["model"],
             "input": o.usage.get("input_tokens"), "output": o.usage.get("output_tokens"),
             "cache_read": o.usage.get("cached_input_tokens"), "cache_write": o.usage.get("cache_write_tokens"),
             "cost": o.usage.get("cost_usd")}
            for o in outcomes if o.runner == RUNNER_CLAUDE and o.usage and o.id in by_id]
    if not rows:
        return
    out = rd / s.USAGE_FILE
    data = _read_optional_json(out)
    data = data if isinstance(data, dict) else {}
    workflows = data["workflows"] if isinstance(data.get("workflows"), dict) else {}
    key = next(f"cli-{stage}-{n}" for n in itertools.count(1) if f"cli-{stage}-{n}" not in workflows)
    s.write_json(out, {**data, "prices_as_of": data.get("prices_as_of") or workflow_usage.PRICES_AS_OF,
                       "price_source": data.get("price_source") or workflow_usage.PRICE_SOURCE,
                       "workflows": {**workflows, key: rows}})


def _write_recheck_fallback(rd: Path, ledger: Ledger, rnd: RoundRecord, repo: str, failed: bool) -> None:
    """재검토 CLI 호출이 실패하면 같은 임시 워크트리를 읽는 Workflow 입력(recheck-args.json)을 만든다. 성공하면 남은 입력을 치운다."""
    if failed:
        (rd / s.RECHECK_RESULTS).unlink(missing_ok=True)
        s.write_review_args(rd, s.RECHECK_ARGS, ledger,
                            [(s.recheck_job_id(rnd.no), f"회차 {rnd.no} recheck", s.read_text(rd / "recheck-prompt.md"), repo)], "recheck")
    else:
        (rd / s.RECHECK_ARGS).unlink(missing_ok=True)


def _workers(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    step = f"workers-{args.stage}"
    if not s.step_done(rnd, "jobs"):
        raise s.CliError(f"회차 {rnd.no}의 jobs 단계를 먼저 한다")
    if s.judged_after_workers_input(rnd, args.stage):
        consumer = {s.STAGE_ADJ: "adjudicate", s.STAGE_OPINION: "opinion/apply", s.STAGE_RECHECK: "recheck"}.get(args.stage, "judge")
        reopen = "apply --force로 적용 단계를 다시 한다" if args.stage == s.STAGE_RECHECK else "jobs --force로 회차를 다시 연다"
        raise s.CliError(f"회차 {rnd.no}의 {consumer}를 이미 했다. 작업자 결과를 다시 만들려면 {reopen}")
    if s.workers_done(rnd, args.stage) and not args.force:
        raise s.CliError(f"회차 {rnd.no}의 {step} 단계는 이미 했다. 다시 하려면 --force 를 붙인다")
    if args.stage == s.STAGE_CHECKER and not s.checker_pending(rnd):
        raise s.CliError(f"회차 {rnd.no}의 judge 단계를 먼저 한다(1차 호출이 점검자 작업을 만든다)")
    if args.stage in (s.STAGE_ADJ, s.STAGE_OPINION, s.STAGE_RECHECK) and not s.step_done(rnd, s.STAGE_INPUTS[args.stage]):
        raise s.CliError(f"회차 {rnd.no}의 {s.STAGE_INPUTS[args.stage]} 단계를 먼저 한다")
    rd = s.round_dir(run_dir, rnd.no)
    cli_args = rd / f"{args.stage}-cli-args.json"
    if not cli_args.is_file():
        raise s.CliError(f"CLI 작업이 없다: {cli_args.name} 가 없다(jobs 단계가 CLI 작업을 만든 회차에서만 쓴다)")
    entries = s.read_json(cli_args)["jobs"]
    jobs = [_cli_job(e) for e in entries]
    _pin_worktrees(rd / WORKTREE_INPUTS, [job.worktree for job in jobs if job.writable])
    profile = assignment.ledger_profile(ledger)
    cells = assignment.mode_workers(profile["roles"], ledger.mode)
    review_stage = args.stage in (s.STAGE_OPINION, s.STAGE_RECHECK)
    exhausted = {} if review_stage and args.force else s.exhausted_runners(rnd, args.stage if review_stage else None)
    if args.stage == s.STAGE_ADJ:
        cells["adjudicator"] = assignment.adjudicator_cell(ledger)
    first = _run_cli_jobs(jobs, exhausted, max_parallel=1) if args.stage == s.STAGE_OPINION else _run_cli_jobs(jobs, exhausted)
    reruns, second = ({}, {}) if args.stage in (s.STAGE_ADJ, s.STAGE_OPINION, s.STAGE_RECHECK) else _rerun_with_fallbacks(
        entries, first, cells, set(profile["consented"]), exhausted)
    outcomes = [second.get(o.id, o) for o in first]
    out = rd / f"{args.stage}-cli.json"
    s.write_json(out, {"results": [{"id": o.id, "output": o.output} for o in outcomes],
                       "outcomes": [o.to_dict() for o in outcomes],
                       "fallback_runs": {job_id: {k: spec[k] for k in ("runner", "model", "effort")}
                                         for job_id, spec in reruns.items()},
                       "replaced": [o.to_dict() for o in first if o.id in reruns]})
    failed = [o.id for o in first if o.status != STATUS_OK]
    unresolved = {o.id for o in outcomes if o.status != STATUS_OK}
    fallback_args = rd / f"{args.stage}-fallback-args.json"
    (rd / f"{args.stage}-fallback.json").unlink(missing_ok=True)
    if args.stage == s.STAGE1:
        s.clear_cli_outputs(rd, s.STAGE_CHECKER)
    if args.stage == s.STAGE_RECHECK:
        _write_recheck_fallback(rd, ledger, rnd, entries[0]["worktree"], bool(unresolved))
    elif unresolved and args.stage != s.STAGE_OPINION:
        s.write_json(fallback_args, {"jobs": [s.fallback_job(e, cells.get(e["role"], {}))
                                              for e in entries if e["id"] in unresolved]})
    else:
        fallback_args.unlink(missing_ok=True)
    counts = dict(Counter(o.status for o in first))
    _record_claude_usage(rd, ledger.mode, entries, [*first, *second.values()], args.stage)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.mark(rnd, step, counts=counts, usage=usage_totals([*first, *second.values()]),
           fallback=[] if args.stage == s.STAGE_OPINION else failed,
           **({"failed": failed} if args.stage == s.STAGE_OPINION else {}),
           fallback_runs={job_id: spec["runner"] for job_id, spec in reruns.items()},
           **({"exhausted": exhausted} if exhausted else {}))
    s.save_ledger(run_dir, ledger)
    n, reasons = fallback_counts(counts)
    rerun_ok = sum(1 for o in second.values() if o.status == STATUS_OK)
    if args.stage == s.STAGE_OPINION:
        print(f"CLI 작업 {len(outcomes)}건: 성공 {counts.get(STATUS_OK, 0)}, 의견 없음 {len(unresolved)}")
    else:
        print(f"CLI 작업 {len(outcomes)}건: 성공 {counts.get(STATUS_OK, 0)}, 대체 실행 {n}" + (f"({reasons})" if n else "")
              + (f" → 외부 재실행 성공 {rerun_ok}, Claude 대체 {len(unresolved)}" if n else ""))
    if reruns:
        print(f"외부 CLI로 다시 돌린 작업 {len(reruns)}건: " + ", ".join(f"{i}→{spec['runner']}" for i, spec in reruns.items()))
    skipped = [o for o in [*first, *second.values()] if o.status == STATUS_SKIPPED]
    if skipped:
        print("건너뛴 작업: " + "; ".join(dict.fromkeys(o.detail.replace("\n", " ") for o in skipped)))
    print(f"결과: {out}")
    _print_next(ledger, run_dir)
    return 0


def _judge_code(ledger, rnd, f, results, snap, cfg, baseline, repro):
    wt_a, wt_b = s.worktree(snap, f.id, "a"), s.worktree(snap, f.id, "b")
    a, b = s.pick(results, f.id, "a"), s.pick(results, f.id, "b")
    base = s.base_of(ledger, rnd)
    pa, pb = extract(wt_a, base, cfg), extract(wt_b, base, cfg)
    a.patch, b.patch = pa, pb
    if a.verdict != VERDICT_FACT and b.verdict != VERDICT_FACT and not pa.strip() and not pb.strip():
        va, vb, ra, rb = [], [], [], []
    else:
        va, ra = s.worker_verify(wt_a, cfg, changed_files(pa), baseline)
        vb, rb = s.worker_verify(wt_b, cfg, changed_files(pb), baseline)
        if repro and repro.get(f.id):
            base_wt = snap.worktrees[s.base_name(rnd)]
            for wt, judged, recorded in ((wt_a, va, ra), (wt_b, vb, rb)):
                extra = run_repro(base_wt, wt, repro[f.id])
                judged += extra
                recorded += extra
    sensitive = s.is_sensitive(changed_files(pa) + changed_files(pb), cfg)
    tests_cut = bool(cut_test_files(pa) or cut_test_files(pb))
    d = decide(a, b, va, vb, pa, pb, sensitive, ledger.adopt_on_both_pass, tests_cut)
    d = s.without_empty_patch(d, {"a": pa, "b": pb}, adjudicated=False)
    d = s.without_lossy_patch(d, lambda role: s.worktree(snap, f.id, role), base)
    workers = {"a": a, "b": b}
    lossy = {"a": s.is_lossy(wt_a, base), "b": s.is_lossy(wt_b, base)}
    return d, workers, {"a": [r.to_dict() for r in ra], "b": [r.to_dict() for r in rb]}, lossy


def _judge_doc_inputs(ledger, rnd, f, results, snap, cfg):
    wt = s.worktree(snap, f.id, "writer")
    a, b, writer = (s.pick(results, f.id, r) for r in ("a", "b", "writer"))
    writer.patch = extract(wt, s.base_of(ledger, rnd), cfg)
    missing, snippets = s.doc_checks(wt, snap.worktrees.get(s.base_name(rnd)), writer.patch, ledger, cfg)
    return {"a": a, "b": b, "writer": writer}, s.doc_problems(missing, snippets), [r.to_dict() for r in snippets]


def cmd_judge(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "judge", args.force)
    if not s.step_done(rnd, "jobs"):
        raise s.CliError(f"회차 {rnd.no}의 jobs 단계를 먼저 한다")
    s.check_user_tree(ledger, rnd)
    cfg = s.cfg_for(ledger.source, Path(ledger.root))
    snap = s.snapshot_of(ledger)
    rd = s.round_dir(run_dir, rnd.no)
    stage1_json, loaded_ids = s.stage_inputs(run_dir, rnd, rd, s.STAGE1, args.results, "--results")
    results = read_results(stage1_json)
    checker_results = None
    if args.checker_results or ((rd / "checker-cli-args.json").is_file() and s.checker_pending(rnd)):
        checker_json, checker_ids = s.stage_inputs(run_dir, rnd, rd, s.STAGE_CHECKER, args.checker_results, "--checker-results")
        checker_results = read_results(checker_json)
        loaded_ids |= checker_ids
    baseline = s.baseline_failed(rd)
    repro = s.approved_repro(rd, args.allow_repro) if ledger.source == s.SOURCE_PR else None
    judged = {**results, **(checker_results or {})}
    for warning in s.model_warnings(judged, {**s.expected_model_ids(ledger, judged), **loaded_ids}):
        print(warning)
    findings = s.findings_of(rnd)
    merged: dict[str, dict] = {}
    verify: dict[str, dict] = {}
    decisions: list[Decision] = []
    pending: list = []
    writer_patches: dict[str, str] = {}
    doc_problems: dict[str, list[str]] = {}
    lossy: dict[str, dict[str, bool]] = {}
    for f in findings:
        if ledger.mode == "doc":
            workers, problems, snippets = _judge_doc_inputs(ledger, rnd, f, results, snap, cfg)
            verify[f.id] = {"writer": snippets}
            doc_problems[f.id] = problems
            writer_patches[f.id] = workers["writer"].patch
            if s.needs_checker(workers["a"], workers["b"], problems, workers["writer"].patch):
                pending.append(f)
                if checker_results is not None and f"{f.id}:checker" in checker_results:
                    workers["checker"] = checker_results[f"{f.id}:checker"]
        else:
            d, workers, verify[f.id], lossy[f.id] = _judge_code(ledger, rnd, f, results, snap, cfg, baseline, repro)
            decisions.append(d)
        for role, w in workers.items():
            merged[f"{f.id}:{role}"] = w.to_dict()
            if role in ("a", "b", "writer"):
                s.patch_path(rd, f.id, role).write_text(w.patch, encoding="utf-8")
    s.write_json(rd / "results.json", merged)
    s.write_json(rd / "verify.json", verify)
    if ledger.mode == "code":
        s.write_json(rd / LOSSY_FILE, lossy)

    if ledger.mode == "doc":
        if pending and checker_results is None:
            out = rd / "checker-args.json"
            wts = {f.id: {"writer": s.worktree(snap, f.id, "writer")} for f in pending}
            jobs = build_checker_jobs(s.prompt_findings(ledger, pending), wts, writer_patches, assignment.build_workers(ledger))
            claude_args, cli_args = split_job_args(jobs, rd / "prompts", ledger.claude_via == CLAUDE_VIA_CLI)
            s.write_json(out, claude_args)
            if cli_args["jobs"]:
                s.write_json(rd / "checker-cli-args.json", cli_args)
            else:
                (rd / "checker-cli-args.json").unlink(missing_ok=True)
            s.mark(rnd, "judge-checker")
            s.save_ledger(run_dir, ledger)
            print(f"점검자 작업 {len(claude_args['jobs'])}건: {out}")
            if cli_args["jobs"]:
                print(f"CLI 점검자 작업 {len(cli_args['jobs'])}건: {rd / 'checker-cli-args.json'}")
            _print_next(ledger, run_dir)
            return 0
        for f in findings:
            a, b = s.worker_from(merged, f.id, "a"), s.worker_from(merged, f.id, "b")
            checker = s.worker_from(merged, f.id, "checker") if f"{f.id}:checker" in merged else None
            d = decide_doc(a, b, checker, doc_problems[f.id])
            d = s.without_empty_patch(d, {"writer": writer_patches[f.id]}, adjudicated=False)
            d = s.without_lossy_patch(d, lambda role: s.worktree(snap, f.id, role), s.base_of(ledger, rnd))
            decisions.append(s.without_outside_docs(d, {"writer": writer_patches[f.id]}, ledger.docs))

    by_id = {f.id: f for f in s.prompt_findings(ledger, findings)}
    adj_items = []
    for d in decisions:
        if d.outcome == OUTCOME_ADJUDICATE:
            name = s.wt_name(d.finding_id, "adjudicator")
            wt = snap.worktrees[name] if name in snap.worktrees else s.new_worktree(snap, name, s.base_of(ledger, rnd), cfg)
            _pin_worktrees(rd / WORKTREE_INPUTS, [wt])
            adj_items.append(s.adj_input(ledger, by_id[d.finding_id], merged, verify, wt, cfg, d.reasons))
    if adj_items:
        claude_cli = ledger.claude_via == CLAUDE_VIA_CLI
        adj_jobs = build_adjudicator_jobs(adj_items, assignment.adjudicator_cell(ledger), claude_cli)
        if claude_cli:
            claude_args, cli_args = split_job_args(adj_jobs, rd / "prompts", claude_cli=True)
            s.write_json(rd / "adj-args.json", claude_args)
            s.write_json(rd / "adj-cli-args.json", cli_args)
        else:
            s.write_json(rd / "adj-args.json", write_job_args(adj_jobs, rd / "prompts"))
    elif ledger.claude_via == CLAUDE_VIA_CLI:
        s.clear_cli_outputs(rd, s.STAGE_ADJ)
        for name in ("adj-args.json", "adj-cli-args.json"):
            (rd / name).unlink(missing_ok=True)
    held = {f["id"] for f in rnd.hold_findings}
    rnd.decisions = [d for d in rnd.decisions if d["finding_id"] in held] + [d.to_dict() for d in decisions]
    s.write_json(rd / "decisions.json", rnd.decisions)
    ledger.snapshot = snap.to_dict()
    rnd.approved_repro = dict(repro or {})
    s.mark(rnd, "judge", repro_allowed=repro is not None)
    s.save_ledger(run_dir, ledger)
    print("판정: " + ", ".join(f"{k} {v}" for k, v in Counter(d.outcome for d in decisions).items()))
    _print_next(ledger, run_dir)
    return 0


def _adjudicate_verify(ledger, rnd, f, wt, patch, snap, cfg, rd):
    if ledger.mode == "doc":
        missing, snippets = s.doc_checks(wt, snap.worktrees.get(s.base_name(rnd)), patch, ledger, cfg)
        results = s.doc_verify(missing, snippets)
        return results, results
    judged, recorded = s.worker_verify(wt, cfg, changed_files(patch), s.baseline_failed(rd))
    cmds = rnd.approved_repro.get(f.id)
    if ledger.source == s.SOURCE_PR and patch.strip() and cmds:
        extra = run_repro(snap.worktrees[s.base_name(rnd)], wt, cmds)
        judged += extra
        recorded += extra
    return judged, recorded


def cmd_adjudicate(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "adjudicate", args.force)
    if not s.step_done(rnd, "judge"):
        raise s.CliError(f"회차 {rnd.no}의 judge 단계를 먼저 한다")
    s.check_user_tree(ledger, rnd)
    cfg = s.cfg_for(ledger.source, Path(ledger.root))
    snap = s.snapshot_of(ledger)
    rd = s.round_dir(run_dir, rnd.no)
    loaded_ids = {}
    if args.results:
        data = s.read_json(args.results)
    elif (rd / "adj-cli-args.json").is_file():
        data, loaded_ids = s.stage_inputs(run_dir, rnd, rd, s.STAGE_ADJ, None, "--results")
    else:
        raise s.CliError("--results 가 필요하다: adj-args.json 의 Workflow 결과를 준다")
    results = read_results(data)
    expected = assignment.adjudicator_cell(ledger)["model"]
    for warning in s.model_warnings(results, {**{key: expected for key, r in results.items() if r.role == "adjudicator"}, **loaded_ids}):
        print(warning)
    merged = s.read_json(rd / "results.json")
    verify = s.read_json(rd / "verify.json")
    by_id = {f.id: f for f in s.prompt_findings(ledger, s.findings_of(rnd))}
    new_decisions = []
    opinion_jobs = []
    for data in rnd.decisions:
        d = Decision.from_dict(data)
        if d.outcome != OUTCOME_ADJUDICATE:
            new_decisions.append(data)
            continue
        f = by_id[d.finding_id]
        wt = s.worktree(snap, f.id, "adjudicator")
        adj = s.pick(results, f.id, "adjudicator")
        adj.patch = extract(wt, s.base_of(ledger, rnd), cfg)
        vadj, vrecorded = _adjudicate_verify(ledger, rnd, f, wt, adj.patch, snap, cfg, rd)
        if ledger.mode == "code" and s.is_sensitive(changed_files(adj.patch), cfg) and REASON_SENSITIVE not in d.reasons:
            d.reasons.append(REASON_SENSITIVE)
        if ledger.mode == "code" and cut_test_files(adj.patch) and REASON_TESTS_CUT not in d.reasons:
            d.reasons.append(REASON_TESTS_CUT)
        patches = {role: s.worker_from(merged, f.id, role).patch for role in ("a", "b", "writer")}
        patches["adjudicator"] = adj.patch
        new = s.without_lossy_patch(s.without_empty_patch(after_adjudication(d, adj, vadj), patches, adjudicated=True),
                                    lambda role: s.worktree(snap, f.id, role), s.base_of(ledger, rnd))
        if ledger.mode == "doc":
            new = s.without_outside_docs(new, patches, ledger.docs)
        if adj.patch.strip():
            s.patch_path(rd, f.id, "adjudicator").write_text(adj.patch, encoding="utf-8")
        merged[f"{f.id}:adjudicator"] = adj.to_dict()
        verify.setdefault(f.id, {})["adjudicator"] = [r.to_dict() for r in vrecorded]
        if new.outcome == OUTCOME_UNDECIDABLE:
            item = s.adj_input(ledger, f, merged, verify, wt, cfg, new.reasons)
            prompt = build_opinion_prompt(item, adj)
            (rd / f"opinion-{f.id}.md").write_text(prompt, encoding="utf-8")
            opinion_jobs.append((s.opinion_job_id(f.id), f"{f.id} opinion", prompt,
                                 snap.worktrees.get(s.base_name(rnd)) or ledger.root))
        new_decisions.append(new.to_dict())
    rnd.decisions = new_decisions
    if opinion_jobs and s.is_claude(ledger, "opinion"):
        claude_cli = ledger.claude_via == CLAUDE_VIA_CLI and s.base_name(rnd) in snap.worktrees
        name = "opinion-cli-args.json" if claude_cli else s.OPINION_ARGS
        s.write_review_args(rd, name, ledger, opinion_jobs, "opinion", claude_cli)
    s.write_json(rd / "results.json", merged)
    s.write_json(rd / "verify.json", verify)
    s.write_json(rd / "decisions.json", rnd.decisions)
    s.mark(rnd, "adjudicate")
    s.save_ledger(run_dir, ledger)
    print("판가름 뒤: " + ", ".join(f"{k} {v}" for k, v in Counter(d["outcome"] for d in new_decisions).items()))
    _print_next(ledger, run_dir)
    return 0


def _claude_opinions(args, run_dir: Path, ledger: Ledger, rnd: RoundRecord) -> int:
    rd = s.round_dir(run_dir, rnd.no)
    pending = s.pending_opinions(ledger, rd) if not args.force else [
        p.stem[len("opinion-"):] for p in sorted(rd.glob("opinion-*.md"))]
    if not pending:
        raise s.CliError(f"회차 {rnd.no}에 붙일 의견이 없다. 다시 붙이려면 --force")
    results = read_review_results(s.read_json(Path(args.results)))
    for fid in pending:
        text, model_id = results.get(s.opinion_job_id(fid), ("", None))
        warning = model_warning(s.opinion_job_id(fid), model_id, s.review_cell(ledger, "opinion")["model"])
        if warning:
            print(warning)
        if not text.strip():
            print(f"경고: {fid} Claude 의견이 없다(호출 실패). 「{CALL_FAILED_NOTE}」로 적고 넘어간다")
            text = CALL_FAILED_NOTE
        ledger.opinions[fid] = text.strip()
        s.mark(rnd, f"opinion:{fid}", **s.review_event_extra(ledger, "opinion"))
    s.save_ledger(run_dir, ledger)
    print(f"Claude 의견 {len(pending)}건을 원장에 붙였다(자동 채택하지 않는다)")
    _print_next(ledger, run_dir)
    return 0


EXTERNAL_LOG_TAIL_LINES = 5
LOGIN_COMMANDS = {REVIEWER_CODEX: "codex login", REVIEWER_CURSOR: "cursor-agent login"}


def _external_result_text(result: Path, what: str, reviewer: str) -> str:
    """외부 검토자(Codex·Cursor) 결과(`<로그>.final`)를 읽는다. 없거나 비면 실행 로그와 인증 실패·이용 한도 여부를 짚어 멈춘다."""
    text = result.read_text(encoding="utf-8").strip() if result.is_file() else ""
    if text:
        return text
    problem = "없다" if not result.is_file() else "비어 있다"
    log = result.with_suffix("") if result.suffix == ".final" else None
    lines = [f"{label(reviewer)} {what} 결과 파일이 {problem}: {result}"]
    if log is None or not log.is_file():
        lines.append(f"실행 로그도 없다({log or '로그 경로를 알 수 없다'}). RR 명령의 --out 경로와 종료 코드를 확인한다")
    else:
        log_text = log.read_text(encoding="utf-8", errors="replace")
        tail = log_text.strip().splitlines()[-EXTERNAL_LOG_TAIL_LINES:]
        lines.append(f"실행 로그: {log}")
        lines.extend(f"  | {line}" for line in tail)
        if s.EXTERNAL_AUTH_FAILURE.search(log_text):
            lines.append("로그에 인증 실패 흔적이 있다(401·refresh token·Authentication required).")
        if s.EXTERNAL_USAGE_LIMIT.search(log_text):
            lines.append("로그에 이용 한도 도달 흔적이 있다(usage limit). 한도가 풀린 뒤 RR을 다시 돌린다.")
    lines.append(f"인증 실패면 터미널에서 `{LOGIN_COMMANDS.get(reviewer, LOGIN_COMMANDS[REVIEWER_CODEX])}`으로 다시 로그인한 뒤 RR을 다시 돌린다.")
    if reviewer == REVIEWER_CODEX:
        lines.append("RR은 한 번에 하나씩 돌린다(동시에 돌리면 refresh token이 겹쳐 소모돼 로그인이 풀릴 수 있다)")
    raise s.CliError("\n".join(lines))


def cmd_opinion(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    if args.results:
        return _claude_opinions(args, run_dir, ledger, rnd)
    if not args.id or not args.result:
        raise s.CliError("--id 와 --result 를 함께 주거나(Codex), --results 하나만 준다(Claude Workflow 결과)")
    s.guard(rnd, f"opinion:{args.id}", args.force)
    if args.id not in {d["finding_id"] for d in rnd.decisions}:
        raise s.CliError(f"회차 {rnd.no}에 {args.id} 판정이 없다")
    runner = s.review_cell(ledger, "opinion")["runner"]
    failure = s.external_failure(Path(args.result))
    if failure in s.FAILURE_REASONS and runner != REVIEWER_CLAUDE:
        return _opinion_fallback(args, run_dir, ledger, rnd, failure)
    text = _external_result_text(Path(args.result), "의견", runner)
    ledger.opinions[args.id] = text
    s.mark(rnd, f"opinion:{args.id}", **s.review_event_extra(ledger, "opinion"))
    s.save_ledger(run_dir, ledger)
    print(f"{args.id} {label(runner)} 의견을 원장에 붙였다(자동 채택하지 않는다)")
    _print_next(ledger, run_dir)
    return 0


def _substitute_review(ledger: Ledger, role: str, failure: str, what: str, result: str, round_no: int) -> dict:
    old = s.review_cell(ledger, role)["runner"]
    new = s.substitute_review(ledger, role, s.FAILURE_REASONS[failure], round_no)
    print(f"{label(old)} {what} 결과가 없다({s.FAILURE_REASONS[failure]} 흔적): {result}")
    print(f"대체: {ledger.mode}.{role} {old} → {new['runner']} — {s.FAILURE_REASONS[failure]}")
    return new


def _opinion_fallback(args, run_dir: Path, ledger: Ledger, rnd: RoundRecord, failure: str) -> int:
    new = _substitute_review(ledger, "opinion", failure, "의견", args.result, rnd.no)
    if new["runner"] == REVIEWER_CLAUDE:
        rd = s.round_dir(run_dir, rnd.no)
        fids = [fid for fid in sorted({*s.pending_opinions(ledger, rd), args.id}) if (rd / f"opinion-{fid}.md").is_file()]
        repo = s.snapshot_of(ledger).worktrees.get(s.base_name(rnd)) or ledger.root
        items = [(s.opinion_job_id(fid), f"{fid} opinion", s.read_text(rd / f"opinion-{fid}.md"), repo) for fid in fids]
        s.write_review_args(rd, s.OPINION_ARGS, ledger, items, "opinion")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def _recheck_fallback(args, run_dir: Path, ledger: Ledger, rnd: RoundRecord, failure: str) -> int:
    new = _substitute_review(ledger, "recheck", failure, "재검토", args.result, rnd.no)
    if new["runner"] == REVIEWER_CLAUDE:
        rd = s.round_dir(run_dir, rnd.no)
        repo_file = rd / s.RECHECK_REPO_FILE
        repo = repo_file.read_text(encoding="utf-8").strip() if repo_file.is_file() else ledger.root
        prompt = s.read_text(rd / "recheck-prompt.md")
        s.write_review_args(rd, s.RECHECK_ARGS, ledger, [(s.recheck_job_id(rnd.no), f"회차 {rnd.no} recheck", prompt, repo)],
                            "recheck")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def _final_suite(root: Path, base_wt: Path | None, cfg, mode: str, changed: list[str]) -> dict:
    cfg, warning = s.final_suite_cfg(cfg, mode, root, changed)
    if warning:
        print(warning)
    if cfg is None or not cfg.full_tests:
        return {"final_ok": None, "final_tail": ""}
    results = run_verifiers(root, cfg, changed, "final")
    tail = "\n".join(r.tail for r in results if not r.ok)[-FINAL_TAIL_CHARS:]
    final = {"final_ok": all(r.ok for r in results), "final_tail": tail}
    if not final["final_ok"] and base_wt is not None and Path(base_wt).is_dir():
        final["baseline_failed"] = not all(r.ok for r in run_verifiers(base_wt, cfg, changed, "final"))
    return final


def _adopted_patches(rnd: RoundRecord, rd: Path) -> list[tuple[str, str]]:
    patches = []
    for d in rnd.decisions:
        if s.is_adopted(d):
            patches.append((d["finding_id"], s.read_text(s.patch_path(rd, d["finding_id"], d["patch_from"]))))
    return patches


def _record_apply(run_dir: Path, ledger: Ledger, rnd: RoundRecord, rd: Path, report: ApplyReport, data: dict,
                  patches: list[tuple[str, str]]) -> None:
    carried = s.carried_ids(rnd, report.applied, [fid for fid, _ in report.failed], dict(patches))
    for fid, tail in report.failed:
        reason = f"{REASON_APPLY_FAILED}: {s.first_line(tail)}"
        if fid in carried:
            _set_decision(rnd, fid, OUTCOME_CARRIED, f"{reason} — {REASON_CARRIED}")
        else:
            _set_decision(rnd, fid, OUTCOME_UNDECIDABLE, reason)
    data["carried"] = [fid for fid, _ in report.failed if fid in carried]
    rnd.adopted = list(report.applied)
    s.write_json(rd / "apply.json", data)
    s.write_json(rd / "decisions.json", rnd.decisions)
    s.mark(rnd, "apply-patched", result_commit=rnd.result_commit)
    s.save_ledger(run_dir, ledger)


def _patch_dry_run(run_dir, ledger, rnd, rd, patches) -> Path:
    snap = s.snapshot_of(ledger)
    name = f"result-r{rnd.no}"
    if name in snap.worktrees:
        remove_worktree(snap, name)
    wt = add_worktree(snap, name, s.base_of(ledger, rnd))
    ledger.snapshot = snap.to_dict()
    report = apply_all(wt, patches)
    rnd.result_commit = commit_worktree(wt, f"cascade-fix {ledger.run_id} 회차 {rnd.no} 결과(dry-run)") or s.base_of(ledger, rnd)
    data = {"dry_run": True, **report.to_dict(), "would_apply": list(report.applied), "result_worktree": str(wt)}
    _record_apply(run_dir, ledger, rnd, rd, report, data, patches)
    print(f"dry-run: 작업 트리에는 적용하지 않고 {wt} 에 적용했다 (채택 {len(report.applied)}건, 실패 {len(report.failed)}건)")
    return wt


def _patch_user_tree(run_dir, ledger, rnd, rd, patches, root) -> None:
    report = apply_all(root, patches, s.external_docs(ledger))
    _record_apply(run_dir, ledger, rnd, rd, report, report.to_dict(), patches)
    print(f"적용 {len(report.applied)}건, 실패 {len(report.failed)}건")
    _snapshot_result(run_dir, ledger, rnd)


def _snapshot_result(run_dir, ledger, rnd) -> None:
    rnd.result_commit = snapshot_tree(s.snapshot_of(ledger), list(s.external_docs(ledger).values()))
    s.save_ledger(run_dir, ledger)


def _remove_round_worktrees(ledger: Ledger, rnd: RoundRecord) -> None:
    snap = s.snapshot_of(ledger)
    for name in s.round_worktree_names(rnd):
        if name in snap.worktrees:
            remove_worktree(snap, name)
    ledger.snapshot = snap.to_dict()


def _recheck_via_cli(ledger: Ledger, rnd: RoundRecord) -> bool:
    """재검토를 `claude -p`로 돌릴 수 있는 경우. 읽을 상태가 커밋(result_commit)으로 있어야 하고, 사용자 저장소 밖 문서가 없어야 한다."""
    return ledger.claude_via == CLAUDE_VIA_CLI and bool(rnd.result_commit) and not s.external_docs(ledger)


def _recheck_worktree(ledger: Ledger, rnd: RoundRecord, recheck_repo: str) -> Path:
    """재검토가 읽을 곳. dry-run은 이미 실행 소유 워크트리(result-rN)라 그대로 쓰고, 아니면 적용 뒤 상태의 복사를 새로 연다."""
    if ledger.dry_run:
        return Path(recheck_repo)
    snap = s.snapshot_of(ledger)
    name = s.recheck_worktree_name(rnd.no)
    leftover = snap.run_dir / "wt" / name
    if name not in snap.worktrees and leftover.exists():
        snap.worktrees[name] = leftover
    if name in snap.worktrees:
        remove_worktree(snap, name)
    wt = add_worktree(snap, name, rnd.result_commit)
    ledger.snapshot = snap.to_dict()
    return wt


def cmd_apply(args) -> int:
    with s.workers_lock(Path(args.run_dir)):
        return _apply(args)


def _apply(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "apply", args.force)
    if not s.step_done(rnd, "judge"):
        raise s.CliError(f"회차 {rnd.no}의 judge 단계를 먼저 한다")
    if any(d["outcome"] == OUTCOME_ADJUDICATE for d in rnd.decisions) and not s.step_done(rnd, "adjudicate"):
        raise s.CliError(f"회차 {rnd.no}의 판가름(adjudicate)을 먼저 한다")
    root = Path(ledger.root)
    cfg = s.cfg_for(ledger.source, root)
    rd = s.round_dir(run_dir, rnd.no)
    recheck_repo = str(root)
    if s.step_done(rnd, "apply-patched"):
        print("패치는 이미 적용했다(apply-patched) — 적용을 건너뛰고 뒷단계만 한다")
        data = s.read_json(rd / "apply.json")
        if ledger.dry_run:
            recheck_repo = data.get("result_worktree", recheck_repo)
        elif not rnd.result_commit:
            _snapshot_result(run_dir, ledger, rnd)
    else:
        patches = _adopted_patches(rnd, rd)
        if ledger.dry_run:
            recheck_repo = str(_patch_dry_run(run_dir, ledger, rnd, rd, patches))
        else:
            _patch_user_tree(run_dir, ledger, rnd, rd, patches, root)
        data = s.read_json(rd / "apply.json")
    texts = {fid: s.read_text(s.patch_path(rd, fid, d["patch_from"]))
             for d in rnd.decisions if s.is_adopted(d) for fid in [d["finding_id"]]}
    applied = [fid for fid in data.get("applied", []) if fid in texts]
    changed = list(dict.fromkeys(p for fid in applied for p in changed_files(texts[fid])))
    if not ledger.dry_run:
        _remove_round_worktrees(ledger, rnd)
        base_wt = s.snapshot_of(ledger).worktrees.get(s.base_name(rnd))
        if not (rd / PRE_FINAL_DECISIONS).exists() and data.get("final_ok") is not False and not data.get("interference"):
            s.write_json(rd / PRE_FINAL_DECISIONS, rnd.decisions)
        if "final_history" not in data and "final_ok" in data:
            data["final_history"] = [{"final_ok": data["final_ok"], "baseline_failed": bool(data.get("baseline_failed")),
                                      "interference": bool(data.get("interference")), "applied": data.get("applied", []),
                                      "at": None}]
        final = _final_suite(root, base_wt, cfg, ledger.mode, changed) if applied else {"final_ok": None, "final_tail": ""}
        data.update(final)
        data["final_history"] = [*data.get("final_history", []), {
            "final_ok": final["final_ok"], "baseline_failed": bool(final.get("baseline_failed")),
            "interference": final["final_ok"] is False, "applied": applied, "at": s.now()}]
        if final["final_ok"] is False:
            data["interference"] = True
            reason = REASON_FINAL_BASELINE_FAILED if final.get("baseline_failed") else REASON_INTERFERENCE
            for fid in [*applied, *data.get("carried", [])]:
                _set_decision(rnd, fid, OUTCOME_UNDECIDABLE, reason)
            data["carried"] = []
            rnd.adopted = []
            print(f"{reason}. 적용한 패치는 작업 트리에 그대로 두었다 — 되돌릴지는 사람이 정한다")
    s.write_json(rd / "apply.json", data)
    s.write_json(rd / "decisions.json", rnd.decisions)
    if s.recheck_needed(ledger, rnd):
        adopted = [f for f in s.findings_of(rnd) if f.id in rnd.adopted]
        scope = list(dict.fromkeys(p for fid in rnd.adopted if fid in texts for p in changed_files(texts[fid])))
        if rnd.result_commit:
            diff = s.range_diff(root, s.base_of(ledger, rnd), rnd.result_commit, scope)
        else:
            diff = "".join(texts[f.id] for f in adopted)
        prompt = build_recheck_prompt(adopted, diff, ledger.mode)
        (rd / "recheck-prompt.md").write_text(prompt, encoding="utf-8")
        recheck_cli = s.is_claude(ledger, "recheck") and _recheck_via_cli(ledger, rnd)
        if recheck_cli:
            recheck_repo = str(_recheck_worktree(ledger, rnd, recheck_repo))
        (rd / s.RECHECK_REPO_FILE).write_text(recheck_repo + "\n", encoding="utf-8")
        if s.is_claude(ledger, "recheck"):
            items = [(s.recheck_job_id(rnd.no), f"회차 {rnd.no} recheck", prompt, recheck_repo)]
            name, stale = (s.RECHECK_CLI_ARGS, s.RECHECK_ARGS) if recheck_cli else (s.RECHECK_ARGS, s.RECHECK_CLI_ARGS)
            s.write_review_args(rd, name, ledger, items, "recheck", recheck_cli)
            for stale_name in (stale, s.RECHECK_CLI_RESULTS, s.RECHECK_RESULTS):
                (rd / stale_name).unlink(missing_ok=True)
        s.write_json(rd / s.RECHECK_SCOPE_FILE, scope)
        rnd.recheck_opened = True
        print(f"재검토 필요: {rd / 'recheck-prompt.md'} (--repo {recheck_repo})")
    elif s.carried_findings(rnd):
        nxt = _open_round(run_dir, ledger, rnd, s.renumber(s.carried_findings(rnd), rnd.no + 1, ledger.source))
        print(f"재검토 없음: 적용하지 못한 {len(nxt.findings)}건으로 회차 {nxt.no}을 연다")
    else:
        ledger.status = STATUS_DONE
        print("재검토 없음: 종료")
    s.mark(rnd, "apply")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def _open_round(run_dir: Path, ledger: Ledger, rnd: RoundRecord, findings: list[Finding]) -> RoundRecord:
    no = rnd.no + 1
    write_findings(s.round_dir(run_dir, no) / "findings.json", findings)
    nxt = RoundRecord(no=no, findings=[f.to_dict() for f in findings], decisions=[], adopted=[], recheck_opened=False,
                      started_at=s.now(), base_commit=rnd.result_commit or s.base_of(ledger, rnd))
    ledger.rounds.append(nxt)
    return nxt


def _hold_reason(f, rnd: RoundRecord, max_reached: bool, outside: set[str]) -> str:
    if f.id in outside:
        return REASON_OUT_OF_SCOPE
    if rnd.final:
        return "「지금 반영」 뒤 마지막 재검토: 회차를 열지 않음"
    if f.severity == "Suggestion":
        return "재검토의 Suggestion: 회차를 열지 않음"
    return "재검토 상한 도달" if max_reached else "수렴: Critical 없고 2건 이하"


def _recheck_parse_fallback(run_dir: Path, ledger: Ledger, rnd: RoundRecord, result: Path, problem: str) -> int:
    """CLI 재검토 결과가 스키마는 통과했지만 지적 형식을 해석하지 못하면, 그 호출을 무효로 고쳐 적고 Workflow 대체 실행으로 넘긴다."""
    rd = s.round_dir(run_dir, rnd.no)
    data = s.read_json(result)
    for outcome in data.get("outcomes", []):
        if outcome.get("id") == s.recheck_job_id(rnd.no):
            outcome.update(status=STATUS_INVALID, output=None, detail=f"재검토 결과 {problem}")
    data["results"] = [{**item, "output": None} for item in data.get("results", [])]
    s.write_json(result, data)
    repo_file = rd / s.RECHECK_REPO_FILE
    _write_recheck_fallback(rd, ledger, rnd, repo_file.read_text(encoding="utf-8").strip(), True)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, rnd.no)
    event = next(e for e in reversed(rnd.events) if e.get("step") == "workers-recheck")
    event.update(counts={STATUS_INVALID: 1}, fallback=[s.recheck_job_id(rnd.no)])
    s.save_ledger(run_dir, ledger)
    print(f"CLI 재검토 결과를 해석하지 못해 Workflow로 대체한다: {problem}")
    _print_next(ledger, run_dir)
    return 0


def _locations_under_root(findings: list[Finding], rd: Path, ledger: Ledger) -> list[Finding]:
    """임시 워크트리를 읽은 재검토가 그 안의 절대 경로를 적었으면 저장소 상대 경로로 바꿔, 워크트리를 치운 뒤에도 위치가 유효하고 범위 판정이 되게 한다."""
    repo_file = rd / s.RECHECK_REPO_FILE
    repo = repo_file.read_text(encoding="utf-8").strip() if repo_file.is_file() else ""
    if not repo or repo == ledger.root:
        return findings
    return [replace(f, location=f.location[len(repo) + 1:]) if f.location.startswith(repo + "/") else f for f in findings]


def _remove_recheck_worktree(ledger: Ledger, rnd: RoundRecord) -> None:
    snap = s.snapshot_of(ledger)
    name = s.recheck_worktree_name(rnd.no)
    if name in snap.worktrees:
        remove_worktree(snap, name)
        ledger.snapshot = snap.to_dict()


def cmd_recheck(args) -> int:
    with s.workers_lock(Path(args.run_dir)):
        return _recheck(args)


def _recheck(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "recheck", args.force)
    if not rnd.recheck_opened:
        raise s.CliError(f"회차 {rnd.no}은 재검토를 열지 않았다")
    is_json = args.result.endswith(".json")
    cell = s.review_cell(ledger, "recheck")
    if not is_json:
        failure = s.external_failure(Path(args.result))
        if failure in s.FAILURE_REASONS and cell["runner"] != REVIEWER_CLAUDE:
            return _recheck_fallback(args, run_dir, ledger, rnd, failure)
        _external_result_text(Path(args.result), "재검토", cell["runner"])
    elif not Path(args.result).is_file():
        raise s.CliError(f"파일이 없다: {args.result}")
    text, model_id = load_text(Path(args.result), s.recheck_job_id(rnd.no) if is_json else None)
    if not text.strip():
        raise s.CliError(f"재검토 결과가 비어 있다(Workflow 호출 실패일 수 있다). 재검토 Workflow를 다시 돌린다: {args.result}")
    warning = model_warning(s.recheck_job_id(rnd.no), model_id, cell["model"])
    if warning:
        print(warning)
    rd = s.round_dir(run_dir, rnd.no)
    (rd / "recheck-result.md").write_text(text, encoding="utf-8")
    try:
        resolved, new = parse_recheck(text, rnd.no + 1)
    except ParseError as e:
        if Path(args.result).name != s.RECHECK_CLI_RESULTS:
            raise s.CliError(f"재검토 결과 {e}", ExitCodes.PARSE_ERROR) from None
        return _recheck_parse_fallback(run_dir, ledger, rnd, Path(args.result), str(e))
    new = _locations_under_root(new, rd, ledger)
    has_header = any(line.strip() == RESOLUTION_HEADER for line in text.splitlines())
    if not has_header:
        print(f"경고: 「{RESOLUTION_HEADER}」 절이 없다. 고친 지적을 모두 해소 안 됨으로 본다")
        resolved = {}
    adopted = [f for f in s.findings_of(rnd) if f.id in rnd.adopted]
    missing = [f.id for f in adopted if f.id not in resolved]
    if has_header and missing:
        print(f"경고: 해소 여부가 없는 지적은 해소 안 됨으로 본다: {', '.join(missing)}")
    unresolved = [s.unresolved_copy(f) for f in adopted if resolved.get(f.id) is not True]
    numbered = s.renumber(unresolved + new + s.carried_findings(rnd), rnd.no + 1, ledger.source)
    combined, carried = numbered[:len(unresolved) + len(new)], numbered[len(unresolved) + len(new):]
    max_reached = s.rechecks_done(ledger) >= ledger.max_rechecks
    in_scope = s.recheck_scope(ledger, rd, adopted, combined)
    outside = {f.id for f in out_of_scope(combined, in_scope)}
    next_round, hold, undecidable = classify_recheck(combined, rnd, max_reached, final=rnd.final, in_scope=in_scope)
    for f in hold:
        rnd.hold_findings.append(f.to_dict())
        rnd.decisions.append(Decision(f.id, OUTCOME_HOLD, None, [_hold_reason(f, rnd, max_reached, outside)]).to_dict())
    for f in undecidable:
        reason = "「지금 반영」 뒤 재검토의 Critical" if rnd.final else "같은 위치 Critical 두 회차 연속"
        rnd.hold_findings.append(f.to_dict())
        rnd.decisions.append(Decision(f.id, OUTCOME_UNDECIDABLE, None, [reason]).to_dict())
    s.write_json(rd / "decisions.json", rnd.decisions)
    _remove_recheck_worktree(ledger, rnd)
    s.mark(rnd, "recheck", **s.review_event_extra(ledger, "recheck"))
    print(f"해소 {sum(1 for v in resolved.values() if v)}건, 해소 안 됨 {len(unresolved)}건, 새 지적 {len(new)}건")
    next_round += carried
    print(f"다음 회차 {len(next_round)}건(적용 못 해 넘긴 {len(carried)}건 포함), "
          f"보류 후보 {len(hold)}건(범위 밖 {len(outside)}건), 판단 불가 {len(undecidable)}건")
    if next_round:
        _open_round(run_dir, ledger, rnd, next_round)
    else:
        ledger.status = STATUS_DONE
    print(f"status={ledger.status}")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def cmd_resume(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    if ledger.status != STATUS_DONE and not args.force:
        raise s.CliError("아직 진행 중이다. 회차가 끝난 뒤(status=종료) 보류 후보를 고른다")
    choices = s.read_json(args.choices)
    if not isinstance(choices, dict) or not choices:
        raise s.CliError("선택 파일은 {\"<지적 id>\": \"지금 반영|구현 때 테스트로|보류\"} 형식이다")
    if any(s.step_done(r, "cleanup") for r in ledger.rounds):
        raise s.CliError("cleanup으로 스냅샷을 이미 지웠다. resume은 cleanup 전에 한다")
    holds = {d["finding_id"]: r for r in ledger.rounds for d in r.decisions if d["outcome"] == OUTCOME_HOLD}
    for fid, choice in choices.items():
        if fid not in holds:
            raise s.CliError(f"보류 후보가 아니다: {fid}")
        if choice not in HOLD_CHOICES:
            raise s.CliError(f"알 수 없는 선택: {choice} ({' / '.join(HOLD_CHOICES)})")
        if fid in ledger.hold_choices and not args.force:
            raise s.CliError(f"{fid}는 이미 골랐다({ledger.hold_choices[fid]}). 바꾸려면 --force")
    ledger.hold_choices.update(choices)
    chosen = [fid for fid, c in choices.items() if c == CHOICE_NOW]
    if chosen:
        no = max(r.no for r in ledger.rounds) + 1
        findings = [next(f for f in [*holds[fid].findings, *holds[fid].hold_findings] if f["id"] == fid) for fid in chosen]
        last = ledger.rounds[-1]
        rnd = RoundRecord(no=no, findings=findings, decisions=[], adopted=[], recheck_opened=False,
                          final=True, started_at=s.now(), base_commit=last.result_commit or last.base_commit)
        ledger.rounds.append(rnd)
        s.write_json(s.round_dir(run_dir, no) / "findings.json", findings)
        ledger.status = STATUS_RUNNING
        print(f"「지금 반영」 {len(chosen)}건으로 회차 {no}을 연다(마지막 회차: 재검토 한 번, 그 지적은 목록으로만)")
    else:
        print("「지금 반영」 없음: 새 회차를 열지 않는다")
    s.mark(ledger.current_round(), "resume")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def _interference_note(ledger: Ledger, run_dir: Path) -> str:
    lines = []
    for rnd in ledger.rounds:
        path = run_dir / f"round-{rnd.no}" / "apply.json"
        data = s.read_json(path) if path.is_file() else {}
        if data.get("interference"):
            lines += [
                f"- 회차 {rnd.no}: 적용한 패치 {', '.join(data.get('applied', []))} 를 모두 넣은 뒤 전체 테스트가 깨졌다.",
                "  작업 트리는 되돌리지 않았다. 되돌릴지·어느 패치를 뺄지는 사람이 정한다.",
                *(["  기준선(수정 전)에서도 전체 테스트가 실패했다."] if data.get("baseline_failed") else []),
                f"  마지막 출력: {s.first_line(data.get('final_tail', ''))}",
            ]
    return "\n".join(["", "## 간섭 의심", "", *lines, ""]) if lines else ""


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    text = render(ledger, ledger.dry_run, run_dir) + _interference_note(ledger, run_dir)
    write_prepared(run_dir, ledger)
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    rnd = ledger.current_round()
    if rnd is not None:
        s.mark(rnd, "report", round=rnd.no)
        s.save_ledger(run_dir, ledger)
    print(text)
    print(f"보고서: {run_dir / 'report.md'} · 준비됨: {run_dir / 'prepared'}")
    return 0


def cmd_cleanup(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = ledger.current_round()
    s.guard(rnd, "cleanup", args.force)
    snap = s.snapshot_of(ledger)
    cleanup(snap, keep_run_dir=True)
    ledger.snapshot = snap.to_dict()
    s.mark(rnd, "cleanup")
    s.save_ledger(run_dir, ledger)
    print(f"임시 브랜치 {snap.branch}와 워크트리를 지웠다. 원장은 남긴다: {run_dir}")
    return 0


def _label_job_ids(files: list[Path]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for path in files:
        data = _read_optional_json(path)
        for job in data.get("jobs", []) if isinstance(data, dict) else []:
            if isinstance(job, dict) and isinstance(job.get("id"), str) and isinstance(job.get("label"), str):
                labels.setdefault(job["label"], job["id"])
    return labels


def _usage_record(workflow: str, labels: dict[str, str], cell_of, out: Path) -> dict:
    """Workflow 기록 폴더를 읽어 out(claude-usage.json)의 그 Workflow 몫을 덮어쓴다. 읽지 못하면 error만 돌려준다."""
    try:
        agents = workflow_usage.read_workflow(workflow_usage.find_workflow_dir(workflow))
    except workflow_usage.UsageFormatError as e:
        return {"workflow": Path(workflow).name, "jobs": 0, "unmatched": 0, "error": str(e)}
    name = Path(workflow).name
    rows, unmatched = [], []
    for agent in agents:
        job_id = labels.get(agent.label)
        if job_id is None:
            unmatched.append(agent.label)
            continue
        rows.append({"job_id": job_id, "label": agent.label, "cell": cell_of(job_id), "model": agent.model,
                     "input": agent.input, "output": agent.output, "cache_read": agent.cache_read,
                     "cache_write": agent.cache_write, "cost": agent.cost})
    data = _read_optional_json(out)
    if not isinstance(data, dict):
        if out.is_file():
            print(f"경고: 기존 {out.name}이 JSON 객체가 아니어서 덮어쓴다. 다른 Workflow의 기록이 있었다면 CF usage를 다시 돌린다")
        data = {}
    workflows = data["workflows"] if isinstance(data.get("workflows"), dict) else {}
    skipped = data["unmatched"] if isinstance(data.get("unmatched"), dict) else {}
    s.write_json(out, {"prices_as_of": workflow_usage.PRICES_AS_OF, "price_source": workflow_usage.PRICE_SOURCE,
                       "workflows": {**workflows, name: rows}, "unmatched": {**skipped, name: unmatched}})
    if unmatched:
        print(f"경고: 이름표가 작업과 일치하지 않는 에이전트는 어느 칸에도 넣지 않았다: {', '.join(unmatched)}")
    return {"workflow": name, "jobs": len(rows), "unmatched": len(unmatched), "error": None}


START_JOBS_LIMIT = 10
MODEL_WARNING = re.compile(r"^경고: .*모델 ID", re.M)
REVIEW_WARNING_STOP = ("모델 ID 경고가 났다. 이 단계는 아직 기록하지 않았다. 사용자에게 알린다. 결과를 쓰지 않으려면 Workflow를 다시 돌려 "
                       "go --workflow-output 으로 바꾸고, 그대로 쓰려면 아래 명령을 직접 부른다")
MODEL_WARNING_STOP = ("모델 ID 경고가 났다. 사용자에게 알린다. 이 결과를 쓰지 않으려면 jobs --force 로 회차를 다시 열고, "
                      "그대로 쓰려면 go 를 다시 부른다")
GO_MAX_STEPS = 200
REVIEW_SCRIPT_TAIL_LINES = 20


def _stage1_jobs(ledger: Ledger) -> int:
    rnd = ledger.current_round()
    return estimate(len(s.findings_of(rnd)), ledger.mode, ledger.reviewer, ledger.workers)["stage1"]


def cmd_start(args) -> int:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code, run_dir = _init(args, print_next=False)
    print(buffer.getvalue(), end="")
    if run_dir is None:
        return code
    if _model_warnings(buffer.getvalue()):
        return _go_stop(run_dir, MODEL_WARNING_STOP, [])
    ledger = s.load_ledger(run_dir)
    jobs = _stage1_jobs(ledger)
    if jobs > START_JOBS_LIMIT and not args.severity:
        print(f"멈춤: 1단계 작업이 {jobs}개로 {START_JOBS_LIMIT}개를 넘는다. 「전부 / Critical·Warning만」을 사용자에게 묻는다")
        print("다음:")
        print(f"  전부: {s._cmd('go', run_dir)}")
        print(f"  Critical·Warning만: {s._cmd('cleanup', run_dir)} 뒤 같은 인자에 --severity Critical,Warning 을 붙여 CF start")
        return 0
    return _advance(run_dir, None, None, None)


def cmd_go(args) -> int:
    return _advance(Path(args.run_dir), args.workflow_output, args.workflow, args.allow_repro)


def _model_warnings(text: str) -> list[str]:
    return MODEL_WARNING.findall(text)


def _run_review_script(command: str) -> tuple[str | None, tuple[str, int] | None]:
    """검토 스크립트(RR·CR)를 돌린다. 반환: (참고 줄, 멈춤(이유, 종료 코드)).

    실행 전에 옛 로그·결과를 치워 이번 시도가 쓴 것만 남긴다. 스크립트가 로그를 쓰고 끝난 실패는 다음 단계(opinion·recheck)가
    로그로 가른다. 로그 없이 끝났으면 종료 코드 77(로그인 필요)은 로그인 안내로, 그 밖은 이번 stderr와 함께 오류로 멈춘다(대체하지 않는다)."""
    argv = shlex.split(command)
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv and argv.index("--out") + 1 < len(argv) else None
    if out:
        out.unlink(missing_ok=True)
        Path(f"{out}.final").unlink(missing_ok=True)
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
    except OSError as e:
        return None, (f"검토 스크립트를 실행하지 못했다: {e}", 1)
    if not result.returncode:
        return None, None
    note = f"참고: 검토 스크립트가 종료 코드 {result.returncode}로 끝났다. 결과가 없으면 다음 단계가 대체 실행으로 처리한다"
    if out and out.is_file():
        return note, None
    detail = "\n".join(result.stderr.strip().splitlines()[-REVIEW_SCRIPT_TAIL_LINES:]
                       + result.stdout.strip().splitlines()[-REVIEW_SCRIPT_TAIL_LINES:])
    if result.returncode == 77:
        runner = RUNNER_CURSOR if argv[0] == str(s.CURSOR_REVIEW) else RUNNER_CODEX
        return None, (f"로그인이 필요하다 — {LOGIN_HINTS[runner]}\n{detail}", 0)
    return None, (f"검토 스크립트가 로그 없이 종료 코드 {result.returncode}로 끝났다. 대체하지 않는다\n{detail}", 1)


def _review_model_warnings(ledger: Ledger, argv: list[str]) -> list[str]:
    """Claude 검토자 Workflow 결과(opinion·recheck)는 단계를 기록하기 전에 모델 ID를 본다. 기록한 뒤에는 되돌릴 수 없다."""
    if argv[0] not in ("opinion", "recheck"):
        return []
    option = "--results" if "--results" in argv else "--result"
    result = argv[argv.index(option) + 1] if option in argv and argv.index(option) + 1 < len(argv) else ""
    if not result.endswith(".json") or not Path(result).is_file():
        return []
    try:
        items = s.read_json(Path(result)).get("results", [])
    except (OSError, ValueError, AttributeError):
        return []
    expected = s.review_cell(ledger, argv[0])["model"]
    warnings = [model_warning(item["id"], item["output"].get("model_id"), expected) for item in items
                if isinstance(item, dict) and isinstance(item.get("output"), dict) and isinstance(item.get("id"), str)]
    return [w for w in warnings if w]


def _run_step(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    lines = buffer.getvalue().rstrip("\n").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index] == "다음:":
            if all(line.startswith("  ") for line in lines[index + 1:]):
                lines = lines[:index]
            break
    return code, "\n".join(lines)


def _save_workflow_result(action: s.Action, output: Path) -> None:
    try:
        result = json.loads(Path(output).read_text(encoding="utf-8"))["result"]
        got = {item["id"] for item in result["results"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise s.CliError(f"Workflow 완료 알림의 output-file에서 결과를 읽지 못했다: {output} ({e})", ExitCodes.PARSE_ERROR) from None
    expected = {job["id"] for job in s.read_json(action.args_path)["jobs"]}
    if got != expected:
        raise s.CliError(f"이 Workflow 결과의 작업이 기다리는 작업과 다르다(받은 {len(got)}건, 기다리는 {len(expected)}건): {action.args_path.name}",
                         ExitCodes.PARSE_ERROR)
    action.out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def _usage_argv(action: s.Action, run_dir: Path, workflow: str) -> list[str]:
    folder = action.args_path.parent
    round_no = re.fullmatch(r"round-(\d+)", folder.name)
    return ["usage", "--run-dir", str(run_dir), "--round", round_no.group(1), "--workflow", workflow]


def _pick_workflow(pending: list[s.Action], saved: list[s.Action], output: str | None) -> s.Action | None:
    """결과를 줄 Workflow 단계. 기다리는 것을 먼저 보고, 없으면 저장했지만 끝나지 않은 단계(뒤 명령이 실패한 단계)를 본다.
    둘 이상이면 작업 id가 맞는 쪽을 고른다."""
    candidates = [*pending, *saved]
    if not output or len(candidates) < 2:
        return candidates[0] if candidates else None
    try:
        got = {item["id"] for item in json.loads(Path(output).read_text(encoding="utf-8"))["result"]["results"]}
    except (OSError, ValueError, KeyError, TypeError):
        return candidates[0]
    return next((a for a in candidates if {job["id"] for job in s.read_json(a.args_path)["jobs"]} == got), candidates[0])


def _go_stop(run_dir: Path, reason: str, actions: list[s.Action], then: str | None = None) -> int:
    shown = [action for action in actions if not action.hidden]
    has_workflow = any(action.kind == "workflow" for action in shown)
    pending_workflow = False
    print(f"멈춤: {reason}")
    print("다음:")
    for action in shown:
        print(f"  {action.text}")
        if action.kind == "workflow" and not action.out_path.is_file():
            pending_workflow = True
            try:
                data = json.loads(action.args_path.read_text(encoding="utf-8"))
                args_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            except (OSError, ValueError):
                continue
            if len(args_json) <= 8000:
                print(f"    args: {args_json}")
    if then:
        print(f"  {then}")
    elif has_workflow:
        print(f"  (Workflow가 끝나면) {s._cmd('go', run_dir, '--workflow-output <완료 알림의 output-file>', '--workflow <Workflow 실행 id>')}")
    if pending_workflow:
        script_path = Path(run_dir).resolve() / "run-jobs.js"
        try:
            script = s.RUN_JOBS.read_bytes()
            if not script_path.exists() or script_path.read_bytes() != script:
                script_path.write_bytes(script)
        except OSError:
            return 0
        print(f"    scriptPath: {script_path}")
    return 0


def _advance(run_dir: Path, output: str | None, workflow: str | None, allow_repro: str | None) -> int:
    used = []
    try:
        with s.go_lock(run_dir):
            code = _advance_steps(run_dir, output, workflow, allow_repro, used)
    except s.CliError as e:
        if e.message != "go가 이미 돈다":
            raise
        print("멈춤: go가 이미 돈다. 그 go가 끝난 뒤 알림을 읽고 필요하면 다시 부른다")
        return 0
    if output and not used:
        print(f"참고: 기다리는 Workflow가 없어 --workflow-output {output} 은 쓰지 않았다")
    return code


def _advance_steps(run_dir: Path, output: str | None, workflow: str | None, allow_repro: str | None, used: list) -> int:
    seen = None
    for _ in range(GO_MAX_STEPS):
        ledger = s.load_ledger(run_dir)
        actions = s.next_actions(ledger, run_dir)
        steps = [a for a in actions if a.kind != "note" and not (a.kind == "workflow" and a.out_path.is_file())]
        if not steps:
            return _go_stop(run_dir, "할 일이 없다", actions)
        flows = [a for a in steps if a.kind == "workflow"]
        target = _pick_workflow(flows, [a for a in actions if a.kind == "workflow" and a not in flows], output)
        if output and target:
            _save_workflow_result(target, Path(output))
            print(f"Workflow 결과를 저장했다: {target.out_path}")
            if workflow:
                code, text = _run_step(_usage_argv(target, run_dir, workflow))
                print(text)
            else:
                print("Claude 토큰 미측정: --workflow 를 주지 않았다")
            output = None
            used.append(True)
            continue
        first = steps[0]
        if first.kind == "cmd" and first.background:
            if s.workers_running(run_dir):
                if flows:
                    return _go_stop(run_dir, "workers가 돌고 있다. 같은 단계를 다시 띄우지 않고 남은 Workflow만 띄운다", flows)
                return _go_stop(run_dir, "workers가 돌고 있다. 같은 단계를 다시 띄우지 않는다", [], then=f"(workers가 끝나면) {s._cmd('go', run_dir)}")
            if [a.text for a in s.next_actions(s.load_ledger(run_dir), run_dir)] != [a.text for a in actions]:
                continue
        if first.kind == "workflow":
            return _go_stop(run_dir, "Workflow 결과가 필요하다", actions)
        if first.kind == "cmd" and first.background and flows:
            return _go_stop(run_dir, "workers와 Workflow를 동시에 띄운다", actions)
        if first.kind == "cmd" and first.argv[0] == "cleanup":
            return _go_stop(run_dir, "report까지 끝났다. 보고 뒤 보류 후보 선택을 받고 cleanup 한다", actions)
        signature = [a.text for a in steps]
        if signature == seen:
            print("오류: 같은 단계를 다시 해도 진행되지 않는다. CF status 로 상태를 본다")
            return 1
        seen = signature
        if first.kind == "shell":
            note, stop = _run_review_script(first.text)
            if note:
                print(note)
            if stop:
                reason, code = stop
                if code:
                    print(f"오류: {reason}")
                    return code
                return _go_stop(run_dir, reason, [], then=f"(로그인한 뒤) {s._cmd('go', run_dir)}")
            first = steps[1]
        argv = list(first.argv)
        if first.argv[0] == "judge":
            if any("--allow-repro" in a.text for a in actions if a.kind == "note"):
                if not allow_repro:
                    return _go_stop(run_dir, "PR 재현 명령 승인이 필요하다. 명령 목록을 사용자에게 보이고 승인받으면 go에 --allow-repro <파일>을 준다", actions)
                argv += ["--allow-repro", allow_repro]
        warnings = _review_model_warnings(ledger, argv)
        if warnings:
            print("\n".join(warnings))
            return _go_stop(run_dir, REVIEW_WARNING_STOP, actions)
        code, text = _run_step(argv)
        print(text)
        if code:
            return code
        if _model_warnings(text):
            return _go_stop(run_dir, MODEL_WARNING_STOP, [])
    print(f"오류: {GO_MAX_STEPS}단계를 넘겨도 끝나지 않았다. CF status 로 상태를 본다")
    return 1


def cmd_usage(args) -> int:
    if bool(args.run_dir) == bool(args.review_dir):
        raise s.CliError("--run-dir(과 --round)와 --review-dir 중 하나만 준다", ExitCodes.PARSE_ERROR)
    if args.run_dir:
        if args.round is None:
            raise s.CliError("--run-dir은 --round와 함께 쓴다", ExitCodes.PARSE_ERROR)
        run_dir = Path(args.run_dir)
        ledger = s.load_ledger(run_dir)
        rnd = s.get_round(ledger, args.round)
        rd = s.round_dir(run_dir, rnd.no)
        labels = _label_job_ids([rd / name for name in s.CLAUDE_JOB_FILES])
        mode = ledger.mode

        def cell_of(job_id: str) -> str | None:
            return assignment.job_cell_key(mode, job_id.rsplit(":", 1)[-1])
    else:
        rd = Path(args.review_dir)
        if not rd.is_dir():
            raise s.CliError(f"첫 검토 폴더가 아니다: {rd}", ExitCodes.PARSE_ERROR)
        meta = _read_optional_json(rd / REVIEW_META)
        role = meta.get("role") if isinstance(meta, dict) else None
        labels = _label_job_ids([rd / "review-args.json"])

        def cell_of(job_id: str) -> str | None:
            return role
    out = rd / s.USAGE_FILE
    result = _usage_record(args.workflow, labels, cell_of, out)
    if args.run_dir:
        ledger = s.load_ledger(run_dir)
        s.mark(s.get_round(ledger, args.round), "usage", **result)
        s.save_ledger(run_dir, ledger)
    if result["error"]:
        print(f"Claude 토큰 미측정: {result['error']}")
    else:
        print(f"Claude 토큰: Workflow {result['workflow']} 작업 {result['jobs']}건 기록, 일치하지 않음 {result['unmatched']}건 → {out}")
    if args.review_dir and (used := _read_optional_json(rd / REVIEW_USED)):
        print(f"경고: 이 첫 검토 폴더는 이미 init에 쓰였다({used.get('run_dir')}). 지금 기록한 토큰은 그 실행의 보고서에 들어가지 않는다. "
              "첫 검토 토큰은 init 전에 CF usage --review-dir로 기록한다")
    return 0


def cmd_status(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = ledger.current_round()
    print(f"run_id={ledger.run_id} source={ledger.source} mode={ledger.mode} status={ledger.status}")
    if rnd is not None:
        steps = [e.get("step") for e in rnd.events]
        print(f"회차 {rnd.no}{' (마지막)' if rnd.final else ''}: 지적 {len(rnd.findings)}건, 한 단계 {', '.join(steps) or '없음'}")
    _print_next(ledger, run_dir)
    return 0


REVIEW_USED = "review-used.json"
SETUP_RECORD = "setup.json"
SETUP_CHECK = "setup-check.json"
STATUS_LABELS = {recommend.MEASURED: "측정됨", recommend.PROVISIONAL: "잠정"}
LOGIN_HINTS = {RUNNER_CODEX: "codex login", RUNNER_CURSOR: "cursor-agent login"}
ACP_CHECK_TIMEOUT = 600
ACP_STOP_GRACE = 30
ENV_SCRIPT_REDIRECTS = ("CASCADE_RUN_REVIEW", "CASCADE_CURSOR_REVIEW")
CHECK_DETAIL_CHARS = 200
SCRIPTS_REPLACED = "교체함"


def _state_dir() -> Path:
    return s.USER_REPOS_TOML.parent


def _skill_scripts() -> dict[str, Path]:
    return {skill_scripts.RUNNER_SKILLS[RUNNER_CODEX]: s.RUN_REVIEW, skill_scripts.RUNNER_SKILLS[RUNNER_CURSOR]: s.CURSOR_REVIEW}


def _script_states(names: set[str] | None = None) -> dict[str, tuple[str, list[str]]]:
    return {name: skill_scripts.compare(name, skill_scripts.skill_dir(script))
            for name, script in _skill_scripts().items() if names is None or name in names}


def _default_profile() -> str | None:
    value = profiles.load_profiles(s.repos_toml())[1].get("default_profile")
    return value if isinstance(value, str) else None


def _notice_roles(chosen: dict, shown: dict, substituted: list[dict]) -> dict:
    """스크립트 알림은 검토 스크립트가 없어 detect가 대체한 칸만 대체 전 값으로 센다. CLI가 없어 대체한 칸은 이번 실행이 스크립트를 쓰지 않는다."""
    no_script = {r["role"].removesuffix(assignment.FALLBACK_SUFFIX) for r in substituted
                 if r["reason"] == assignment.NO_SCRIPT_REASON}
    return {key: chosen[key] if key in no_script and key in chosen else cell for key, cell in shown.items()}


def _setup_notes(roles: dict) -> list[str]:
    try:
        used = {skill_scripts.RUNNER_SKILLS[runner] for runner, path in setup.check_paths(roles) if path == setup.REVIEW}
        return setup.notices(record=setup.read_record(_state_dir() / SETUP_RECORD), table=recommend.load_table(),
                             default_profile=_default_profile(), roles=roles, scripts=_script_states(used))
    except (ConfigError, OSError) as e:
        return [f"설정 알림을 건너뛴다: {e}"]


def _setup_target() -> toml_edit.Target:
    return toml_edit.locate(os.environ, s.USER_REPOS_TOML, s.LEGACY_REPOS_TOML)


def _target_source(target: toml_edit.Target) -> Path | None:
    return target.path if target.path.is_file() else target.copy_from


def _known_profiles(source: Path | None) -> dict[str, dict]:
    return profiles.load_profiles(source)[0]


def _require_known(name: str, known: dict[str, dict]) -> None:
    if name not in known:
        raise s.CliError(f"모르는 프로필이다: {name!r}. 알려진 이름: {', '.join(known)}", ExitCodes.PARSE_ERROR)


def _backup_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _plugin_version() -> str | None:
    manifest = setup.read_record(s.SKILL_ROOT / ".claude-plugin" / "plugin.json")
    return manifest.get("version") if manifest else None


def _dump(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_next_commands(*commands: str) -> None:
    print("다음:")
    for command in commands:
        print(f"  {command}")


def _setup_inspect(table: dict, target: toml_edit.Target | None, target_error: str | None) -> dict:
    return setup.inspect(table=table, availability=setup.availability(), codex_model=codex_model(Path.home() / ".codex"),
                         target=target, target_error=target_error, scripts=_script_states(),
                         script_dirs={name: str(skill_scripts.skill_dir(script)) for name, script in _skill_scripts().items()},
                         record=setup.read_record(_state_dir() / SETUP_RECORD))


def _render_setup_detect(info: dict) -> str:
    availability = info["availability"]
    lines = [f"환경: {info['env']} ({RUNNER_CODEX} {availability[RUNNER_CODEX]}, {RUNNER_CURSOR} {availability[RUNNER_CURSOR]})"]
    lines.extend(f"안내: {runner}는 로그인이 필요하다 — {LOGIN_HINTS[runner]}"
                 for runner in (RUNNER_CODEX, RUNNER_CURSOR) if availability[runner] == setup.NEEDS_LOGIN)
    if availability[RUNNER_CODEX] != setup.ABSENT:
        lines.append(f"Codex 모델: {info['codex_model'] or '없음(~/.codex/config.toml에 model이 없다)'}")
    toml = info["repos_toml"]
    if toml["path"]:
        copied = f" (예전 위치 {toml['copy_from']}를 복사해 쓴다)" if toml["copy_from"] else ""
        lines.append(f"repos.toml: {toml['path']}{copied}")
    if toml["error"]:
        lines.append(f"repos.toml 문제: {toml['error']}")
    lines.append(f"현재 default_profile: {toml['default_profile'] or '없음'}")
    if toml["overridden_builtins"]:
        lines.append(f"덮은 내장 프로필: {', '.join(toml['overridden_builtins'])} — 추천 이름은 같아도 칸 값은 덮은 값이 쓰인다")
    saved, fast = (info["options"][priority] for priority in recommend.PRIORITIES)
    lines.append(f"추천(Claude 한도 아끼기): {saved[0]['profile']} [{STATUS_LABELS[saved[0]['status']]}]")
    lines.append(f"추천(빨리 끝내기): {fast[0]['profile']}")
    lines.extend(f"  대안: {o['profile']} [{STATUS_LABELS[o['status']]}] {o['note']}".rstrip() for o in saved[1:])
    for name, (state, details) in info["scripts"].items():
        lines.append(f"스크립트 {name}: {state} ({info['script_dirs'][name]})" + (f" — {'; '.join(details)}" if details else ""))
    record = info["record"]
    if record:
        lines.append(f"지난 설정: {record.get('setup_at')} {record.get('profile')} (표 v{record.get('table_version')})")
        backups = record.get("backups")
        lines.extend(f"  백업: {b}" for b in (backups if isinstance(backups, list) else []))
    return "\n".join(lines)


def _setup_detect(args) -> int:
    try:
        target, error = _setup_target(), None
    except toml_edit.EditError as e:
        target, error = None, str(e)
    info = _setup_inspect(recommend.load_table(), target, error)
    print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else _render_setup_detect(info))
    return 0


def _acp_check(model: str) -> dict:
    run = _execute([sys.executable, "-m", "cascade.cursor_acp_check", "--model", model], "", s.SKILL_ROOT,
                   {**os.environ, "PYTHONPATH": str(s.SKILL_ROOT)}, ACP_CHECK_TIMEOUT, grace=ACP_STOP_GRACE)
    if run.timed_out or run.missing:
        return {"status": setup.ACP_UNKNOWN, "detail": "시간 초과" if run.timed_out else run.stderr}
    output = f"{run.stdout}\n{run.stderr}".strip()
    return {"status": setup.acp_result(output, run.returncode), "detail": output[-DETAIL_TAIL:]}


def _check_passed(result: dict) -> bool:
    return result["status"] in (setup.CHECK_OK, setup.ACP_CLEAN)


def _merge_check_into_record(profile: str, results: dict) -> bool:
    path = _state_dir() / SETUP_RECORD
    record = setup.read_record(path)
    if record is None or record.get("profile") != profile:
        return False
    old = record.get("checks")
    checks = {**(old if isinstance(old, dict) else {}), **{name: result["status"] for name, result in results.items()}}
    setup.write_record(path, {**record, "checks": checks})
    return True


def _check_roles(name: str, roles: dict) -> dict:
    """검토 칸의 auto를 Claude가 아닌 입력일 때처럼 푼다. codex는 스크립트와 CLI가 다 있을 때만 고른다."""
    available: dict[str, bool] = {}
    codex_ok = s.RUN_REVIEW.is_file() and _probe(available, (RUNNER_CODEX,))[RUNNER_CODEX]
    try:
        return assignment.resolve_auto(roles, f"{RUNNER_CODEX}-code", s.RUN_REVIEW if codex_ok else Path(os.devnull),
                                       _cursor_reviewer_ok(available), name=name)
    except profiles.ProfileError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None


def _setup_check(args) -> int:
    if not args.profile:
        raise s.CliError("--check 는 --profile <이름>이 필요하다")
    if args.dry_run:
        raise s.CliError("--check 는 --dry-run 을 받지 않는다(시험 문장을 실제로 보낸다)")
    known = _known_profiles(s.repos_toml())
    _require_known(args.profile, known)
    roles = _check_roles(args.profile, known[args.profile]["roles"])
    scripts = {runner: _skill_scripts()[skill] for runner, skill in skill_scripts.RUNNER_SKILLS.items()}
    results = {}
    cells = setup.check_cells(roles)
    for runner, path, model, effort in cells:
        same_path = sum(1 for cell in cells if cell[:2] == (runner, path))
        name = f"{runner}-{path}" if same_path == 1 else f"{runner}-{path}:{model or 'default'}/{effort or 'default'}"
        results[name] = setup.run_check(runner, path, model, effort, scripts)
    models = setup.acp_models(roles)
    for model in models:
        results["cursor_acp" if len(models) == 1 else f"cursor_acp:{model}"] = _acp_check(model)
    check = {"profile": args.profile, "checked_at": s.now(), "results": results}
    if _merge_check_into_record(args.profile, results):
        (_state_dir() / SETUP_CHECK).unlink(missing_ok=True)
    else:
        setup.write_record(_state_dir() / SETUP_CHECK, check)
    passed = all(_check_passed(result) for result in results.values())
    if args.json:
        _dump(check)
    else:
        if not results:
            print("외부 runner를 쓰지 않아 시험할 것이 없다")
        for name, result in results.items():
            detail = " ".join(result["detail"].split())[:CHECK_DETAIL_CHARS]
            print(f"시험 {name}: {result['status']}" + (f" — {detail}" if detail and not _check_passed(result) else ""))
        if passed:
            _print_next_commands(f"{s.CLI} setup --apply --profile {shlex.quote(args.profile)} --priority <save-claude|fast> --dry-run")
        else:
            _print_next_commands("실패한 runner를 빼고 추천을 다시 고르거나, 고친 뒤 "
                                 f"{s.CLI} setup --check --profile {shlex.quote(args.profile)} 로 다시 시험한다")
    return 0 if passed else ExitCodes.NEEDS_USER


def _replace_skills(raw: str | None) -> list[str]:
    runners = [x.strip() for x in (raw or "").split(",") if x.strip()]
    unknown = [x for x in runners if x not in skill_scripts.RUNNER_SKILLS]
    if unknown:
        raise s.CliError(f"--replace-scripts 는 {', '.join(skill_scripts.RUNNER_SKILLS)} 중에서 쉼표로 고른다: {', '.join(unknown)}")
    return [skill_scripts.RUNNER_SKILLS[x] for x in dict.fromkeys(runners)]


def _plan_diff(target: toml_edit.Target, base: str, new_text: str) -> str:
    return toml_edit.diff(base if target.path.is_file() else "", new_text, target.path)


def _plan_lines(target: toml_edit.Target, diff: str) -> list[str]:
    lines = []
    if target.copy_from:
        lines.append(f"예전 위치 {target.copy_from}를 {target.path}로 복사한다(예전 파일은 고치지 않는다)")
    lines.append(f"repos.toml: {target.path} ({'바꿀 내용이 없다' if not diff else '새로 만든다' if not target.path.is_file() else '고친다'})")
    if diff:
        lines.append(diff.rstrip("\n"))
    return lines


def _apply_command(args, replace_raw: str | None) -> str:
    choice = "--keep" if args.keep else f"--profile {shlex.quote(args.profile)}"
    replace = f" --replace-scripts {shlex.quote(replace_raw)}" if replace_raw else ""
    return f"{s.CLI} setup --apply {choice} --priority {args.priority}{replace}"


def _setup_apply(args) -> int:
    if bool(args.profile) == bool(args.keep):
        raise s.CliError("--apply 는 --profile <이름> 또는 --keep 중 하나만 받는다")
    if not args.priority:
        raise s.CliError(f"--apply 는 --priority ({'|'.join(recommend.PRIORITIES)})가 필요하다")
    replace = _replace_skills(args.replace_scripts)
    redirected = [name for name in ENV_SCRIPT_REDIRECTS if replace and os.environ.get(name)]
    if redirected:
        raise s.CliError(f"{', '.join(redirected)}이 잡혀 있어 검토 스크립트를 바꾸지 않는다. 변수를 지우고 다시 하라", ExitCodes.PARSE_ERROR)
    target = _setup_target()
    known = _known_profiles(_target_source(target))
    if args.profile:
        _require_known(args.profile, known)
    table = recommend.load_table()
    info = _setup_inspect(table, target, None)
    base = toml_edit.base_text(target, s.LEGACY_REPOS_TOML)
    new_text = base if args.keep else toml_edit.set_default_profile(base, args.profile)
    diff = "" if args.keep else _plan_diff(target, base, new_text)
    replacing = [name for name in replace if info["scripts"][name][0] != skill_scripts.SAME]
    if args.dry_run:
        lines = ["repos.toml: 건드리지 않는다(지금 값 유지)"] if args.keep else _plan_lines(target, diff)
        for name in replace:
            state, details = info["scripts"][name]
            note = f"{state}({'; '.join(details)})" + ("; 원래 파일은 .bak으로 남긴다" if state == skill_scripts.DIFFERENT else "")
            lines.append(f"스크립트 교체 {name}: {skill_scripts.skill_dir(_skill_scripts()[name])} — " + (note if name in replacing else "이미 같다"))
        if args.json:
            _dump({"dry_run": True, "path": str(target.path), "copy_from": str(target.copy_from) if target.copy_from else None,
                   "diff": diff, "replace_scripts": {name: info["scripts"][name] for name in replace}})
        else:
            print("\n".join(lines))
            _print_next_commands(_apply_command(args, args.replace_scripts))
        return 0
    stamp = _backup_stamp()
    repos_backup = toml_edit.write_checked(target.path, new_text, stamp) if diff else None
    backups: list[Path] = [repos_backup] if repos_backup else []
    try:
        for name in replacing:
            backups.extend(skill_scripts.install(name, skill_scripts.skill_dir(_skill_scripts()[name]), stamp))
    except OSError as e:
        if repos_backup:
            written = f"repos.toml {target.path}는 이미 고쳤다(백업: {', '.join(map(str, backups))}). 되돌리려면 {s.CLI} setup --restore"
        else:
            written = f"repos.toml {target.path}를 새로 만들었다(백업 없음)" if diff else "repos.toml은 바꾸지 않았다"
        raise s.CliError(f"스크립트 설치에 실패했다: {e}. {written}. 설정 기록은 쓰지 않았다") from None
    profile = info["repos_toml"]["default_profile"] if args.keep else args.profile
    check_path = _state_dir() / SETUP_CHECK
    check = setup.read_record(check_path)
    matched = check is not None and check.get("profile") == profile and isinstance(check.get("results"), dict)
    checks = {name: result.get("status") if isinstance(result, dict) else result
              for name, result in check["results"].items()} if matched else {}
    record = {
        "setup_at": s.now(), "plugin_version": _plugin_version(), "table_version": table["version"], "env": info["env"],
        "priority": args.priority, "recommended": recommend.recommended(table, info["env"], args.priority),
        "profile": profile, "kept_existing": args.keep, "checks": checks,
        "scripts": {name: SCRIPTS_REPLACED if name in replacing else state for name, (state, _) in info["scripts"].items()},
        "backups": [str(b) for b in backups],
    }
    setup.write_record(_state_dir() / SETUP_RECORD, record)
    if matched:
        check_path.unlink(missing_ok=True)
    if args.json:
        _dump({"dry_run": False, "path": str(target.path), "record": record})
        return 0
    if args.keep:
        print("repos.toml: 건드리지 않았다(지금 값 유지)")
    else:
        print(f'repos.toml: {target.path}에 default_profile = "{profile}"를 썼다' if diff else f"repos.toml: {target.path} 바꿀 내용이 없다")
    print(f"설정 기록: {_state_dir() / SETUP_RECORD}")
    print("\n".join(f"백업: {b}" for b in backups) or "백업: 없음")
    for name in replacing:
        print(f"스크립트 교체함: {name}")
    _print_next_commands(f"{s.CLI} setup --restore --dry-run  # repos.toml을 되돌리려면" if repos_backup else f"{s.CLI} setup --detect")
    return 0


def _setup_repo_profile(args) -> int:
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    target = _setup_target()
    _require_known(args.repo_profile, _known_profiles(_target_source(target)))
    base = toml_edit.base_text(target, s.LEGACY_REPOS_TOML)
    new_text = toml_edit.set_repo_profile(base, root, args.repo_profile)
    diff = _plan_diff(target, base, new_text)
    if args.dry_run:
        if args.json:
            _dump({"dry_run": True, "path": str(target.path), "copy_from": str(target.copy_from) if target.copy_from else None,
                   "root": str(root), "diff": diff})
        else:
            print("\n".join(_plan_lines(target, diff)))
            _print_next_commands(f"{s.CLI} setup --repo-profile {shlex.quote(args.repo_profile)} --root {shlex.quote(str(root))}")
        return 0
    backup = toml_edit.write_checked(target.path, new_text, _backup_stamp()) if diff else None
    if args.json:
        _dump({"dry_run": False, "path": str(target.path), "root": str(root), "backup": str(backup) if backup else None})
        return 0
    if not diff:
        print(f"repos.toml: {target.path} 바꿀 내용이 없다")
        return 0
    print(f'repos.toml: {target.path}에 {root}의 profile = "{args.repo_profile}"를 썼다')
    print(f"백업: {backup or '없음'}")
    _print_next_commands(f"{s.CLI} setup --restore --dry-run  # repos.toml을 되돌리려면")
    return 0


def _setup_restore(args) -> int:
    target = _setup_target()
    path = target.path
    if args.dry_run:
        found = toml_edit.backups(path)
        if not found:
            raise toml_edit.EditError(f"되돌릴 백업이 없다: {path}")
        if not path.is_file():
            raise toml_edit.EditError(f"현재 파일이 없어 되돌리지 못한다: {path}")
        diff = toml_edit.diff(path.read_text(encoding="utf-8"), found[0].read_text(encoding="utf-8"), path)
        if args.json:
            _dump({"dry_run": True, "path": str(path), "backup": str(found[0]), "diff": diff})
        else:
            print(f"되돌릴 백업: {found[0]}")
            print(diff.rstrip("\n") or "현재 파일과 백업이 같다")
            _print_next_commands(f"{s.CLI} setup --restore")
        return 0
    used, kept = toml_edit.restore_latest(path, _backup_stamp())
    if args.json:
        _dump({"dry_run": False, "path": str(path), "restored_from": str(used), "backup": str(kept)})
    else:
        print(f"되돌렸다: {used} → {path}")
        print(f"되돌리기 전 파일은 {kept}에 남겼다")
        _print_next_commands(f"{s.CLI} setup --detect")
    return 0


SETUP_HANDLERS = {"detect": _setup_detect, "check": _setup_check, "apply": _setup_apply,
                  "repo_profile": _setup_repo_profile, "restore": _setup_restore}


def cmd_setup(args) -> int:
    action = next(name for name in SETUP_HANDLERS if getattr(args, name) not in (None, False))
    try:
        return SETUP_HANDLERS[action](args)
    except ConfigError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cascade.cli", description="리뷰 지적 캐스케이드 처리")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, *, run_dir=True, round_no=False, force=True):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        if run_dir:
            p.add_argument("--run-dir", required=True)
        if round_no:
            p.add_argument("--round", type=int, required=True)
        if force:
            p.add_argument("--force", action="store_true")
        return p

    def add_workers_args(p):
        p.add_argument("--workers", choices=WORKERS_CHOICES,
                       help="1단계 작업자(기본: 환경 변수 CASCADE_WORKERS, 없으면 claude). "
                            "external: 쓸 수 있는 Codex·Cursor CLI에 A·writer, B·checker 자리를 배정")
        p.add_argument("--cursor-model", help=f"Cursor 작업자 모델(기본 {DEFAULT_CURSOR_MODEL})")

    def add_init_args(p):
        p.add_argument("--source", choices=SOURCES, required=True)
        p.add_argument("--input")
        p.add_argument("--findings-json")
        p.add_argument("--doc", action="append")
        p.add_argument("--pr-repo")
        p.add_argument("--me")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--adopt-on-both-pass", action="store_true",
                       help="코드 모드에서 같은 판정에 패치가 달라도 둘 다 검증을 통과하면 판가름 없이 작은 패치를 채택")
        p.add_argument("--max-rechecks", type=int, default=2)
        p.add_argument("--root")
        p.add_argument("--consent", help="전송에 동의한 외부 수신처(codex,cursor 중 쉼표 구분, 또는 none). 기본: 이 프로필이 쓰는 수신처 전부")
        p.add_argument("--reviewer", choices=REVIEWER_CHOICES,
                       help="판가름 뒤 의견·재검토를 맡을 검토자(기본: 환경 변수 CASCADE_REVIEWER, 없으면 auto). "
                            "auto: claude-* 입력이면 claude, 아니면 codex-review 스킬이 있으면 codex, 없으면 cursor를 쓸 수 있을 때 cursor, 아니면 claude")
        p.add_argument("--review-model", help=f"Claude 검토자 모델(기본 {DEFAULT_MODEL})")
        p.add_argument("--review-effort", choices=EFFORTS, help=f"Claude 검토자 effort(기본 {DEFAULT_EFFORT})")
        p.add_argument("--profile", help="역할 프로필 이름(기본: 환경 변수 CASCADE_PROFILE, repos.toml, 없으면 내장 default)")
        p.add_argument("--claude-via", choices=CLAUDE_VIA_CHOICES,
                       help="1단계 Claude 작업을 돌리는 방식(기본 workflow). cli: CF workers가 `claude -p`로 돌린다(Workflow 없이 go가 이어 간다)")
        p.add_argument("--severity", help="이번 실행에서 처리할 등급(쉼표 구분, 예: Critical,Warning). 나머지는 보류 후보")
        add_workers_args(p)
    p = add("init", cmd_init, run_dir=False, force=False)
    add_init_args(p)
    p = add("start", cmd_start, run_dir=False, force=False)
    add_init_args(p)
    p = add("go", cmd_go, force=False)
    p.add_argument("--workflow-output", help="Workflow 완료 알림의 output-file. 기다리는 Workflow 결과를 저장하고 이어 간다")
    p.add_argument("--workflow", help="그 Workflow 실행 id(wf_…). 주면 CF usage도 한다")
    p.add_argument("--allow-repro", help="사용자가 승인한 재현 명령 목록(judge에 넘긴다)")
    p = add("detect", cmd_detect, run_dir=False, force=False)
    p.add_argument("--input")
    p.add_argument("--doc", action="append", help="검토할 문서(설계·계획 문서). 변경으로 잡히지 않는 커밋한 문서도 지정한다")
    p.add_argument("--pr")
    p.add_argument("--root")
    p.add_argument("--no-gh", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reviewer", choices=REVIEWER_CHOICES)
    p.add_argument("--profile", help="역할 프로필 이름(기본: 환경 변수 CASCADE_PROFILE, repos.toml, 없으면 내장 default)")
    add_workers_args(p)
    p = add("setup", cmd_setup, run_dir=False, force=False)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--detect", action="store_true", help="이 기기의 환경·현재 설정·추천을 보인다(파일을 쓰지 않는다)")
    action.add_argument("--check", action="store_true", help="--profile 이 쓰는 외부 runner 경로마다 시험 문장을 보내 동작을 확인한다")
    action.add_argument("--apply", action="store_true", help="--profile <이름>(또는 --keep)을 repos.toml default_profile과 설정 기록에 쓴다")
    action.add_argument("--repo-profile", metavar="이름", help="--root 저장소 항목의 profile을 정한다(항상 Claude만 등)")
    action.add_argument("--restore", action="store_true", help="repos.toml을 가장 최근 백업으로 되돌린다")
    p.add_argument("--profile", help="--check·--apply 가 다룰 프로필 이름")
    p.add_argument("--priority", choices=recommend.PRIORITIES, help="--apply 의 우선순위(save-claude: Claude 한도 아끼기, fast: 빨리 끝내기)")
    p.add_argument("--keep", action="store_true", help="--apply 에서 repos.toml은 건드리지 않고 기록만 쓴다(지금 값 유지)")
    p.add_argument("--replace-scripts", help="--apply 에서 플러그인 판으로 바꿀 검토 스크립트의 runner(codex,cursor 중 쉼표 구분)")
    p.add_argument("--root", help="--repo-profile 의 저장소 루트(기본: 현재 디렉터리의 git 루트)")
    p.add_argument("--dry-run", action="store_true", help="--apply·--repo-profile·--restore 가 바꿀 내용만 보이고 쓰지 않는다")
    p.add_argument("--json", action="store_true")
    p = add("review", cmd_review, run_dir=False, force=False)
    p.add_argument("--kind", choices=("code", "doc"))
    p.add_argument("--base")
    p.add_argument("--uncommitted", action="store_true")
    p.add_argument("--commit")
    p.add_argument("--doc", action="append")
    p.add_argument("--focus")
    p.add_argument("--root")
    p.add_argument("--profile", help="역할 프로필 이름(기본: 환경 변수 CASCADE_PROFILE, repos.toml, 없으면 내장 default)")
    p.add_argument("--review-model")
    p.add_argument("--review-effort", choices=EFFORTS)
    add_workers_args(p)
    p.add_argument("--consent", help="전송에 동의한 외부 수신처(codex,cursor 중 쉼표 구분, 또는 none). 기본: 이 프로필의 첫 검토 칸이 쓰는 수신처 전부")
    p.add_argument("--review-dir", help="--fallback-from으로 이력을 고칠 처음 첫 검토 폴더(review_dir=로 찍힌 곳)")
    p.add_argument("--fallback-from", help="실패한 첫 검토 runner(codex·cursor). 마지막으로 시도한 runner와 같아야 한다")
    p.add_argument("--reason", help=f"--fallback-from의 사유(기본 {REVIEW_DEFAULT_REASON})")
    p = add("jobs", cmd_jobs, round_no=True)
    p.add_argument("--claude-via", choices=CLAUDE_VIA_CHOICES,
                   help="Claude 작업을 돌리는 방식을 이 값으로 바꿔 작업 파일을 다시 만든다(--force로 회차를 다시 열 때)")
    p = add("usage", cmd_usage, run_dir=False, force=False)
    p.add_argument("--run-dir")
    p.add_argument("--round", type=int)
    p.add_argument("--review-dir", help="CF review가 만든 첫 검토 폴더(review_dir=로 찍힌 곳)")
    p.add_argument("--workflow", required=True, help="Workflow 실행 id 또는 그 기록 폴더 경로")
    p = add("workers", cmd_workers, round_no=True)
    p.add_argument("--stage", choices=(s.STAGE1, s.STAGE_CHECKER, s.STAGE_ADJ, s.STAGE_OPINION, s.STAGE_RECHECK), required=True)
    p = add("judge", cmd_judge, round_no=True)
    p.add_argument("--results")
    p.add_argument("--checker-results")
    p.add_argument("--allow-repro")
    p = add("adjudicate", cmd_adjudicate, round_no=True)
    p.add_argument("--results")
    p = add("opinion", cmd_opinion, round_no=True)
    p.add_argument("--id")
    p.add_argument("--result")
    p.add_argument("--results", help="Claude 검토자 Workflow 결과(opinion.json). --id/--result 대신 쓴다")
    add("apply", cmd_apply, round_no=True)
    p = add("recheck", cmd_recheck, round_no=True)
    p.add_argument("--result", required=True)
    p = add("resume", cmd_resume)
    p.add_argument("--choices", required=True)
    add("report", cmd_report, force=False)
    add("cleanup", cmd_cleanup)
    add("status", cmd_status, force=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except s.CliError as e:
        print(f"오류: {e.message}")
        return e.code
    except RuntimeError as e:
        print(f"오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
