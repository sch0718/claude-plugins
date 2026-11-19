"""저장소 상태를 읽어 cascade-fix를 어떻게 돌릴지 추천한다. 읽기만 하고 아무것도 바꾸지 않는다."""

import json
import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from cascade import assignment
from cascade.config import ConfigError, RepoConfig, detect_candidates, is_sensitive_path, load_repo_config
from cascade.findings import GH_TIMEOUT, ParseError, fetch_pr_findings, parse_codex_code, parse_codex_doc
from cascade.jobs import RUNNER_CLAUDE
from cascade.profiles import Resolved
from cascade.review import (
    CURSOR_SOURCES,
    REVIEWER_AUTO,
    REVIEWER_CLAUDE,
    REVIEW_META,
    label,
    read_review_results,
)
from cascade.runners import RUNNER_CODEX, RUNNER_CURSOR
from cascade.snapshot import RUN_DIR_NAME
from cascade.workers import (
    DEFAULT_CURSOR_MODEL,
    WORKERS_CLAUDE,
    WORKERS_EXTERNAL,
    describe,
    has_external,
)

DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
_LOCATION_FILE = re.compile(r"^(.+?):\d+")
LARGE_CHANGE_FILES = 30
SEVERITY_SPLIT_JOBS = 10  # 1단계 작업이 이보다 많으면 등급별로 나눠 돌리는 안을 낸다
REGISTER_APPROVAL = "검증 명령 등록"
_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _git(root: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=root, capture_output=True, text=True)
    return p.returncode, p.stdout


def uncommitted_files(root: Path) -> list[str]:
    code, out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if code != 0:
        return []
    files: list[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # 이름 바꾸기는 원래 이름이 다음 칸에 온다
        if not path.startswith(f"{RUN_DIR_NAME}/"):
            files.append(path)
    return files


def default_base(root: Path) -> str | None:
    code, out = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and out.strip():
        return out.strip()
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")[0] == 0:
            return name
    return None


def ahead_of(root: Path, base: str) -> tuple[int, list[str]]:
    code, out = _git(root, "rev-list", "--count", f"{base}..HEAD")
    commits = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
    if not commits:
        return 0, []
    code, out = _git(root, "diff", "--name-only", f"{base}...HEAD")
    return commits, [line for line in out.splitlines() if line.strip()] if code == 0 else []


def current_branch(root: Path) -> str:
    code, out = _git(root, "branch", "--show-current")
    return out.strip() if code == 0 else ""


def pr_info(root: Path, gh: list[str], number: str | None = None) -> tuple[dict | None, str | None]:
    """현재 브랜치(또는 번호)의 PR과 해결 안 된 리뷰 스레드 수. 실패하면 (None, 사유)."""
    try:
        p = subprocess.run([*gh, "pr", "view", *([number] if number else []), "--json", "number,url,state"],
                           cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"gh 실행 실패: {e}"
    if p.returncode != 0:
        return None, (p.stderr.strip().splitlines() or ["PR 없음"])[0]
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return None, "gh pr view 출력을 읽지 못했다"
    m = _PR_URL.search(data.get("url", ""))
    if not m:
        return None, f"PR 주소를 해석하지 못했다: {data.get('url')}"
    owner, name, num = m.group(1), m.group(2), int(m.group(3))
    try:
        me = subprocess.run([*gh, "api", "user", "--jq", ".login"], capture_output=True, text=True,
                            timeout=GH_TIMEOUT).stdout.strip()
    except subprocess.TimeoutExpired:
        me = ""
    try:
        findings = fetch_pr_findings(owner, name, num, me, gh)
    except (RuntimeError, ValueError, KeyError) as e:
        return {"number": num, "url": data.get("url"), "state": data.get("state"), "repo": f"{owner}/{name}",
                "unresolved": None}, f"리뷰 스레드를 읽지 못했다: {e}"
    return {"number": num, "url": data.get("url"), "state": data.get("state"), "repo": f"{owner}/{name}",
            "unresolved": len(findings), "me": me}, None


def _location_paths(raw: str) -> list[str] | None:
    """Critical/Warning/Suggestion 형식의 지적 위치에서 파일 경로만 낸다. 형식이 아니면 None."""
    try:
        findings = parse_codex_doc(raw, 1)
    except ParseError:
        return None
    return [m.group(1) if (m := _LOCATION_FILE.match(f.location)) else f.location for f in findings]


def _review_text(raw: str) -> str:
    """Workflow 결과 JSON이면 검토 본문을, 아니면 원문을 낸다."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and "results" in data:
        texts = [text for text, _ in read_review_results(data).values()]
        return texts[0] if texts else ""
    return raw


def _sectioned(raw: str) -> tuple[int, bool] | None:
    """(지적 수, 위치가 모두 문서 파일인가). 형식이 아니면 None."""
    paths = _location_paths(raw)
    if paths is None:
        return None
    return len(paths), bool(paths) and all(p.lower().endswith(DOC_SUFFIXES) for p in paths)


def doc_targets(path: Path, root: Path) -> list[Path]:
    """문서 모드 결과 파일이 가리키는 문서 중 실제로 있는 것. init --doc 으로 넘겨 원장에 등록한다
    (저장소 밖 문서는 --doc 이 없으면 수정용 워크트리에 복사되지 않는다)."""
    out: list[Path] = []
    for p in _location_paths(_review_text(path.read_text(encoding="utf-8"))) or []:
        if not p.lower().endswith(DOC_SUFFIXES):
            continue
        target = (Path(p).expanduser() if Path(p).expanduser().is_absolute() else root / p).resolve()
        if target.is_file() and target not in out:
            out.append(target)
    return out


CURSOR_RESULT_PREFIX = "cursor-review"
CURSOR_SOURCE_BY_FORMAT = {"codex-doc": "cursor-doc", "claude-code": "cursor-code"}


def classify_input(path: Path) -> tuple[str | None, int]:
    """결과 파일이 어떤 --source 인지와 지적 수를 추정한다.

    Claude·Cursor 검토 결과와 codex-doc 결과는 같은 Critical/Warning/Suggestion 형식이라 형식으로는 못 가른다.
    지적 위치가 모두 문서 파일이면 문서 모드, 코드 파일이 하나라도 있으면 코드 모드로 본다.
    파일명이 cursor-review 로 시작하거나(cursor-review 스킬의 기본 이름) CF review 폴더의 review-meta.json이
    실제 runner를 cursor로 적었으면 같은 모드의 cursor-* 로 본다.
    """
    source, found = _classify_format(path)
    if path.name.startswith(CURSOR_RESULT_PREFIX) or _review_meta_runner(path) == RUNNER_CURSOR:
        source = CURSOR_SOURCE_BY_FORMAT.get(source, source)
    return source, found


def _review_meta_runner(path: Path) -> str | None:
    meta = path.parent / REVIEW_META
    try:
        actual = json.loads(meta.read_text(encoding="utf-8")).get("actual") if meta.is_file() else None
    except (OSError, ValueError, AttributeError):
        return None
    return actual.get("runner") if isinstance(actual, dict) else None


def _classify_format(path: Path) -> tuple[str | None, int]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and "results" in data:
        texts = [text for text, _ in read_review_results(data).values()]
        found = _sectioned(texts[0]) if texts else None
        if found is None or not found[0]:
            return "claude-code", -1
        return ("claude-doc" if found[1] else "claude-code"), found[0]
    code = parse_codex_code(raw, 1)
    if code:
        return "codex-code", len(code)
    found = _sectioned(raw)
    if found is None:
        return None, 0
    if found[0] and not found[1]:
        return "claude-code", found[0]
    return "codex-doc", found[0]


def estimate(findings: int, mode: str, reviewer: str, workers: dict[str, dict] | None = None) -> dict:
    """한 회차의 에이전트 수. 판가름·의견은 갈린 지적에만 붙으므로 최대치다."""
    roles = ("a", "b", "writer") if mode == "doc" else ("a", "b")
    by_runner = {runner: 0 for runner in (RUNNER_CLAUDE, RUNNER_CODEX, RUNNER_CURSOR)}
    for role in roles:
        by_runner[(workers or {}).get(role, {}).get("runner", RUNNER_CLAUDE)] += findings
    out = {
        "stage1": findings * len(roles),
        "stage1_by_runner": by_runner,
        "checker_max": findings if mode == "doc" else 0,
        "adjudicator_max": findings,
        "opinion_max": findings if reviewer == REVIEWER_CLAUDE else 0,
        "recheck": 1 if reviewer == REVIEWER_CLAUDE else 0,
    }
    out["min"] = out["stage1"]
    out["max"] = sum(v for k, v in out.items() if k not in ("min", "stage1_by_runner"))
    return out


def _runner_totals(roles: dict, mode: str) -> str:
    claude: dict[str, int] = {}
    external = {RUNNER_CODEX: 0, RUNNER_CURSOR: 0}
    for key in assignment.work_keys(roles, mode):
        runner = roles[key]["runner"]
        if runner == RUNNER_CLAUDE:
            nickname = assignment.model_nickname(roles[key]["model"])
            claude[nickname] = claude.get(nickname, 0) + 1
        elif runner in external:
            external[runner] += 1
    parts = [f"Claude 최대 {'·'.join(f'{name} {n}' for name, n in claude.items())}/지적"] if claude else []
    parts += [f"{label(runner)} {n}/지적" for runner, n in external.items() if n]
    return f" — {', '.join(parts)} + 의견·재검토" if parts else ""


def estimate_line(findings: int, mode: str, reviewer: str, workers: dict[str, dict] | None = None,
                  roles: dict | None = None) -> str:
    e = estimate(findings, mode, reviewer, workers)
    split = e["stage1_by_runner"]
    by_runner = f"(Claude {split[RUNNER_CLAUDE]}·Codex {split[RUNNER_CODEX]}·Cursor {split[RUNNER_CURSOR]})" if has_external(workers) else ""
    parts = [f"작업자 {e['stage1']}{by_runner}"]
    if e["checker_max"]:
        parts.append(f"점검자 최대 {e['checker_max']}")
    parts.append(f"판가름 최대 {e['adjudicator_max']}")
    if e["opinion_max"]:
        parts.append(f"의견 최대 {e['opinion_max']}")
    if e["recheck"]:
        parts.append(f"재검토 {e['recheck']}")
    return f"회차당 에이전트 {e['min']}~{e['max']}개({', '.join(parts)})" + (_runner_totals(roles, mode) if roles else "")


def _config(root: Path, repos_toml: Path) -> tuple[RepoConfig | None, str | None]:
    try:
        return load_repo_config(root, repos_toml), None
    except (ConfigError, ValueError) as e:
        return None, f"검증 설정을 읽지 못했다({repos_toml}): {e}"


def _first_review_ready(runner: str, run_review: Path, cursor_ok: bool | Callable[[], bool]) -> bool:
    if runner == RUNNER_CODEX:
        return Path(run_review).is_file()
    if runner == RUNNER_CURSOR:
        return bool(cursor_ok() if callable(cursor_ok) else cursor_ok)
    return True


def _usable_first_review(roles: dict, substituted: list[dict], key: str, run_review: Path,
                         cursor_ok: bool | Callable[[], bool]) -> tuple[dict, list[dict]]:
    cell, tried = roles[key], set()
    while not _first_review_ready(cell["runner"], run_review, cursor_ok):
        tried.add(cell["runner"])
        new = assignment.fallback_target(cell, set(assignment.EXTERNAL_RUNNERS), tried, "reviewer")
        substituted = [*substituted, {"role": key, "from": cell["runner"], "to": new["runner"],
                                      "reason": assignment.NO_SCRIPT_REASON}]
        cell = new
    return {**roles, key: cell}, substituted


def detect(root: Path, *, repos_toml: Path, run_review: Path, cli: str, gh: list[str] | None, profile: Resolved,
           input_path: Path | None = None, pr: str | None = None, reviewer_choice: str = REVIEWER_AUTO,
           root_flag: str = "", docs: list[Path] | None = None, cwd: Path | None = None,
           cursor_ok: bool | Callable[[], bool] = False, workers_choice: str = WORKERS_CLAUDE,
           cursor_model: str = DEFAULT_CURSOR_MODEL, available: dict[str, bool] | None = None,
           profile_flag: str | None = None, codex_model: str = "", scripts: dict[str, bool] | None = None,
           reviewer_flag: str = "", workers_flag: str = "") -> dict:
    root = Path(root)
    branch = current_branch(root)
    base = default_base(root)
    uncommitted = uncommitted_files(root)
    commits, ahead = ahead_of(root, base) if base else (0, [])
    notes: list[str] = []
    blockers: list[str] = []
    pr_data = None
    if gh is not None and (pr or not (input_path or docs)):
        pr_data, why = pr_info(root, gh, pr)
        if why and (pr or pr_data):
            notes.append(why)

    q = shlex.quote
    # review 는 --doc 을 현재 디렉터리 기준으로 읽는다. --root 로 다른 저장소를 가리켰거나
    # 저장소 하위 디렉터리에서 돌렸으면 저장소 상대 경로가 어긋나므로 절대 경로를 준다
    at_root = cwd is None or Path(cwd).resolve() == root.resolve()
    doc_arg = lambda p: q(str(root / p) if root_flag or not at_root else p)  # noqa: E731
    rec: dict = {"source": None, "mode": None, "steps": [], "reasons": []}
    found = None
    if input_path is not None:
        source, found = classify_input(input_path)
        if source is None:
            blockers.append(f"결과 파일 형식을 알 수 없다: {input_path}")
        else:
            rec.update(source=source, mode="doc" if source.endswith("doc") else "code")
            rec["reasons"].append(f"결과 파일이 {source} 형식이다" + (f"(지적 {found}건)" if found >= 0 else ""))
    elif docs:
        # 설계 단계: 커밋한 스펙처럼 변경으로 잡히지 않는 문서도 지정해서 검토한다
        names = [d.relative_to(root).as_posix() if d.is_relative_to(root) else None for d in docs]
        missing = [n or str(d) for d, n in zip(docs, names) if not d.is_file()]
        if missing:
            blockers.append("문서가 없다: " + ", ".join(missing))
        rec.update(source="claude-doc", mode="doc")
        rec["reasons"].append(f"문서를 직접 지정했다({len(docs)}개) — 문서 모드")
        rec["review_args"] = " ".join(f"--doc {doc_arg(n) if n else q(str(d))}" for d, n in zip(docs, names))
    elif pr_data and pr_data.get("unresolved"):
        found = pr_data["unresolved"]
        rec.update(source="pr-comment", mode="code")
        rec["reasons"].append(f"PR #{pr_data['number']}에 해결 안 된 리뷰 스레드 {found}건이 있다")
    elif uncommitted or ahead:
        changed = uncommitted or ahead
        doc_only = all(p.endswith(DOC_SUFFIXES) for p in changed)
        kind = "doc" if doc_only else "code"
        rec.update(source=f"claude-{kind}", mode=kind)
        if uncommitted:
            rng, why = "--uncommitted", f"커밋하지 않은 변경 {len(uncommitted)}파일"
        else:
            rng, why = f"--base {q(base)}", f"{base}보다 커밋 {commits}개 앞섬({len(ahead)}파일)"
        rec["reasons"].append(why + (" — 모두 문서라 문서 모드" if doc_only else ""))
        if doc_only:
            # 지운 문서는 review --doc 이 읽을 수 없다
            changed_docs = [p for p in changed if (root / p).is_file()]
            if not changed_docs:
                blockers.append("바뀐 문서가 모두 삭제라 문서 검토할 파일이 없다: " + ", ".join(changed[:5]))
            rec["review_args"] = " ".join(f"--doc {doc_arg(p)}" for p in changed_docs)
        else:
            rec["review_args"] = rng
            changed_docs = [p for p in changed if p.endswith(DOC_SUFFIXES) and (root / p).is_file()]
            if changed_docs:
                notes.append(f"문서도 {len(changed_docs)}개 바뀌었다. 코드 검토는 설계 문서로 보지 않으니 문서만 따로 보려면 "
                             f"{cli} detect " + " ".join(f"--doc {doc_arg(p)}" for p in changed_docs) + root_flag)
        if len(changed) > LARGE_CHANGE_FILES:
            notes.append(f"바뀐 파일이 {len(changed)}개다. 지적이 많으면 --severity 로 나눠 돌린다")
    else:
        blockers.append("처리할 것이 없다: 결과 파일·PR 리뷰 스레드·커밋하지 않은 변경·앞선 커밋이 모두 없다"
                        " (이미 커밋한 문서는 --doc <문서> 로 지정한다)")

    mode = rec["mode"] or "code"
    first_key = f"{mode}.review"
    first_review = "review_args" in rec

    def decide_roles(source: str) -> tuple[dict, list[dict]]:
        decided, replaced = assignment.decide(profile.roles, profile.pinned, source, run_review, cursor_ok,
                                              available or {}, cursor_model=cursor_model, name=profile.name, scripts=scripts)
        return _usable_first_review(decided, replaced, first_key, run_review, cursor_ok) if first_review else (decided, replaced)

    original_source = rec["source"]
    roles, substituted = decide_roles(rec["source"] or "")
    if first_review and roles[first_key]["runner"] != RUNNER_CLAUDE:
        rec["source"] = f"{roles[first_key]['runner']}-{mode}"
        roles, substituted = decide_roles(rec["source"])
    roles = assignment.fill_codex_model(roles, codex_model)
    shown = {key: roles[key] for key in assignment.mode_keys(roles, mode) if first_review or key != first_key}
    substituted = [r for r in substituted if first_review or not r["role"].startswith(first_key)]
    legacy = assignment.legacy_fields({"roles": roles}, mode, workers_choice)
    reviewer, workers = legacy["reviewer"], legacy["workers"]
    notes.extend(f"대체: {r['role']} {r['from']} → {r['to']} — {r['reason']}" for r in substituted if r["role"].startswith(f"{mode}."))
    cfg, cfg_error = _config(root, repos_toml)
    if cfg_error:
        blockers.append(cfg_error)
    config = {"path": str(repos_toml), "registered": cfg is not None, "tests": list(cfg.tests) if cfg else []}
    approvals: list[str] = []
    if rec["mode"] == "code" and not (cfg and cfg.tests):
        candidates = detect_candidates(root)
        config["candidates"] = candidates
        if candidates.get("tests"):
            approvals.append(f"{REGISTER_APPROVAL}: {repos_toml} 에 [repos.\"{root}\"] 항목을 후보로 적는다(사용자 확인)")
        else:
            blockers.append("코드 모드인데 테스트 명령을 찾지 못했다. 테스트 없는 저장소의 코드 수정은 이 스킬로 하지 않는다")
    consent = assignment.consent_line(shown, mode)
    if consent:
        approvals.append(consent)
    if workers_choice == WORKERS_EXTERNAL and not has_external(workers):
        notes.append("--workers external 이지만 쓸 수 있는 외부 CLI가 없다. 작업자는 모두 Claude로 돈다")
    if rec["source"] == "pr-comment":
        approvals.append("PR 코멘트의 재현 명령이 있으면 jobs 뒤에 목록을 보이고 승인받는다")

    patterns = (cfg or RepoConfig()).sensitive_paths
    changed_all = list(dict.fromkeys(uncommitted + ahead))
    sensitive = [p for p in changed_all if is_sensitive_path(p, patterns)]
    if sensitive:
        notes.append("보안·인증·삭제 경로가 바뀌었다. 이 경로를 고친 지적은 채택하지 않고 판단 불가로 보낸다: " + ", ".join(sensitive[:5]))
    if uncommitted and original_source in ("pr-comment", "codex-code", "codex-doc", *CURSOR_SOURCES):
        notes.append("커밋하지 않은 변경이 있다. 채택 패치가 작업 트리에 섞이지 않게 하려면 --dry-run 을 쓴다")

    steps = rec["steps"]
    profile_arg = f" --profile {q(profile_flag or profile.name)}" if profile.explicit or profile_flag else ""
    flags = reviewer_flag + workers_flag + profile_arg + root_flag
    if not blockers and rec["source"]:
        if first_review:
            rec["first_review"] = 1
            steps.append(f"{cli} review --kind {rec['mode']} {rec['review_args']}{workers_flag}{profile_arg}{root_flag}")
            steps.append("Workflow(첫 검토) → review.json 저장 → review 출력의 init 명령" if roles[first_key]["runner"] == RUNNER_CLAUDE
                         else "review 출력의 RR·CR 명령 실행 → review 출력의 init 명령")
        elif rec["source"] == "pr-comment":
            steps.append(f"{cli} init --source pr-comment --input {pr_data['number']} --pr-repo {q(pr_data['repo'])}"
                         + (f" --me {q(pr_data['me'])}" if pr_data.get("me") else "") + flags)
        else:
            docs = doc_targets(input_path, root) if rec["mode"] == "doc" else []
            doc_flags = "".join(f" --doc {q(str(d))}" for d in docs)
            steps.append(f"{cli} init --source {rec['source']} --input {q(str(input_path))}{doc_flags}{flags}")
    if found is not None and found > 0 and rec["mode"]:
        rec["estimate"] = estimate(found, rec["mode"], reviewer, workers)
        if rec["estimate"]["stage1"] > SEVERITY_SPLIT_JOBS:
            notes.append(f"1단계 작업이 {rec['estimate']['stage1']}개다. --severity Critical,Warning 으로 먼저 돌리는 안을 함께 제시한다")

    return {
        "root": str(root),
        "branch": branch,
        "base": base,
        "uncommitted": uncommitted,
        "ahead": {"base": base, "commits": commits, "files": ahead},
        "pr": pr_data,
        "reviewer": reviewer,
        "reviewer_label": label(reviewer),
        "workers": workers,
        "profile": {"name": profile.name, "source": profile.source, "explicit": profile.explicit,
                    "overrides": profile.overrides, "substituted": substituted, "roles": shown},
        "config": config,
        "recommendation": rec,
        "approvals": approvals,
        "notes": notes,
        "blockers": blockers,
        "ok": not blockers,
    }


def render(result: dict) -> str:
    rec = result["recommendation"]
    lines = [f"저장소: {result['root']} (브랜치 {result['branch'] or '?'}, 기준 {result['base'] or '없음'})"]
    lines.append(f"커밋하지 않은 변경: {len(result['uncommitted'])}파일 · 앞선 커밋: {result['ahead']['commits']}개")
    pr = result["pr"]
    if pr:
        lines.append(f"PR: #{pr['number']} {pr.get('state', '')} · 해결 안 된 리뷰 스레드 {pr['unresolved'] if pr['unresolved'] is not None else '?'}건")
    profile = result["profile"]
    lines.append(f"프로필: {profile['name']} ({profile['source']})")
    if profile["overrides"]:
        lines.append(assignment.overrides_line(profile["overrides"]))
    lines.append(assignment.roles_line(profile["roles"], rec["mode"] or "code"))
    if rec["source"]:
        lines.append(f"추천: --source {rec['source']} · 모드 {rec['mode']} · 검토자 {result['reviewer_label']}")
        lines.extend(f"  근거: {r}" for r in rec["reasons"])
    if has_external(result["workers"]):
        lines.append(f"작업자: {describe(result['workers'])}")
    if rec.get("estimate"):
        e = rec["estimate"]
        lines.append(f"규모: 에이전트 {e['min']}~{e['max']}개(1회차 기준)")
    elif rec["source"]:
        e = estimate(1, rec["mode"], result["reviewer"], result["workers"])
        first = f"첫 검토 {rec['first_review']}개 + " if rec.get("first_review") else ""
        lines.append(f"규모: {first}지적 1건당 에이전트 {e['stage1']}~{e['max'] - e['recheck']}개"
                     + (f" + 재검토 회차당 {e['recheck']}개" if e["recheck"] else "") + " (지적 수는 첫 검토 뒤에 정해진다)")
    candidates = result["config"].get("candidates") or {}
    for a in result["approvals"]:
        lines.append(f"확인 필요: {a}")
        if a.startswith(REGISTER_APPROVAL):
            lines.extend(f"  후보 {key}: {' / '.join(cmds)}" for key, cmds in candidates.items() if cmds)
    for n in result["notes"]:
        lines.append(f"참고: {n}")
    for b in result["blockers"]:
        lines.append(f"멈춤: {b}")
    if rec["steps"]:
        lines.append("다음:")
        lines.extend(f"  {step}" for step in rec["steps"])
    lines.append(f"ok={'예' if result['ok'] else '아니오'}")
    return "\n".join(lines)
