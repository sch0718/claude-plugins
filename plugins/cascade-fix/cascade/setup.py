import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from cascade import profiles, recommend
from cascade.runners import (
    DETAIL_TAIL,
    RUNNER_CODEX,
    RUNNER_CURSOR,
    STATUS_AUTH,
    STATUS_LIMIT,
    STATUS_OK,
    CodexRunner,
    CursorRunner,
    _execute,
    classify_failure,
    codex_available,
    codex_model,
    cursor_available,
    find_codex,
    find_cursor_agent,
)
from cascade.skill_scripts import RUNNER_SKILLS, SAME
from cascade.toml_edit import Target
from cascade.workers import DEFAULT_CURSOR_MODEL

SMOKE_PROMPT = "cascade-fix 설정 시험 호출이다. 다른 말 없이 OK라고만 답하라."
LOGGED_IN, NEEDS_LOGIN, ABSENT = "로그인됨", "로그인 필요", "없음"
CHECK_OK, CHECK_NEEDS_LOGIN, CHECK_LIMIT, CHECK_FAILED = "성공", "로그인 필요", "한도", "실패"
ACP_CLEAN, ACP_LEAK, ACP_UNKNOWN = "누출 0건", "누출 있음", "확인 못 함"
WORKER, REVIEW = "worker", "review"
RECORD_SCHEMA = 1
EXIT_NEEDS_LOGIN, EXIT_LIMIT = 77, 75
LOG_TAIL_LINES = 20
_EXTERNAL = (RUNNER_CODEX, RUNNER_CURSOR)
_ACP_READ_KEYS = ("doc.judge_a", "doc.judge_b", "doc.checker")
_LEAKS = re.compile(r"^합계:.*누출 (\d+)건", re.MULTILINE)
_SETUP_HINT = "「cascade-fix 설정해줘」로 바꿀 수 있다"
_CHECK_STATUS = {STATUS_OK: CHECK_OK, STATUS_AUTH: CHECK_NEEDS_LOGIN, STATUS_LIMIT: CHECK_LIMIT}


def availability() -> dict[str, str]:
    def state(found: str | None, usable) -> str:
        if found is None:
            return ABSENT
        return LOGGED_IN if usable() else NEEDS_LOGIN

    return {RUNNER_CODEX: state(find_codex(), codex_available),
            RUNNER_CURSOR: state(find_cursor_agent(), cursor_available)}


def _repos_toml(target: Target | None, target_error: str | None) -> dict:
    info = {"path": None, "copy_from": None, "error": target_error, "default_profile": None, "overridden_builtins": []}
    if target is None:
        return info
    info["path"] = str(target.path)
    info["copy_from"] = str(target.copy_from) if target.copy_from else None
    source = target.path if target.path.is_file() else target.copy_from
    if source is None:
        return info
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        info["error"] = f"repos.toml을 읽지 못했다: {e}"
        return info
    default = data.get("default_profile")
    info["default_profile"] = default if isinstance(default, str) else None
    raw = data.get("profiles")
    if isinstance(raw, dict):
        info["overridden_builtins"] = [name for name in profiles.BUILTIN_NAMES if name in raw]
    return info


def inspect(*, table: dict, availability: dict[str, str], codex_model: str, target: Target | None,
            target_error: str | None, scripts: dict[str, tuple[str, list[str]]], script_dirs: dict[str, str],
            record: dict | None) -> dict:
    env = recommend.env_key(availability.get(RUNNER_CODEX) == LOGGED_IN, availability.get(RUNNER_CURSOR) == LOGGED_IN)
    return {
        "env": env,
        "availability": availability,
        "codex_model": codex_model,
        "repos_toml": _repos_toml(target, target_error),
        "scripts": scripts,
        "script_dirs": script_dirs,
        "record": record,
        "table_version": table["version"],
        "options": {priority: recommend.options(table, env, priority) for priority in recommend.PRIORITIES},
    }


def _path_of(key: str) -> str | None:
    if key in profiles._WORKER_KEYS:
        return WORKER
    return REVIEW if key.split(".", 1)[1] in profiles._REVIEW_ROLES else None


def _external_uses(roles: dict) -> list[tuple[str, str, dict]]:
    return [(target["runner"], path, target)
            for key in profiles.ROLE_KEYS if key in roles and (path := _path_of(key))
            for target in (roles[key], roles[key]["fallback"])
            if target["runner"] in _EXTERNAL]


def check_paths(roles: dict) -> list[tuple[str, str]]:
    return sorted({(runner, path) for runner, path, _ in _external_uses(roles)})


def cell_model(roles: dict, runner: str, path: str) -> tuple[str, str]:
    model, effort = next(((t["model"], t["effort"]) for r, p, t in _external_uses(roles) if (r, p) == (runner, path)), ("", ""))
    if runner == RUNNER_CODEX and not model:
        model = codex_model(Path.home() / ".codex")
    return model, effort


def check_cells(roles: dict) -> list[tuple[str, str, str, str]]:
    cells = set()
    for runner, path, target in _external_uses(roles):
        model = target["model"]
        if runner == RUNNER_CODEX and not model:
            model = codex_model(Path.home() / ".codex")
        cells.add((runner, path, model, target["effort"]))
    return sorted(cells)


def acp_models(roles: dict) -> list[str]:
    cells = [cell for key in _ACP_READ_KEYS if key in roles for cell in (roles[key], roles[key]["fallback"])]
    return sorted({cell["model"] or DEFAULT_CURSOR_MODEL for cell in cells if cell["runner"] == RUNNER_CURSOR})


def _check_worker(runner: str, model: str, effort: str, timeout: int) -> dict:
    exe = find_codex() if runner == RUNNER_CODEX else find_cursor_agent()
    if exe is None:
        return {"status": CHECK_FAILED, "detail": f"{runner} 실행 파일 없음"}
    with tempfile.TemporaryDirectory(prefix="cascade-setup-check-") as tmp:
        if runner == RUNNER_CODEX:
            outcome = CodexRunner(exe, timeout=timeout).ask(Path(tmp), SMOKE_PROMPT, model, effort)
        else:
            outcome = CursorRunner(exe, timeout=timeout).ask(Path(tmp), SMOKE_PROMPT, model)
    status = _CHECK_STATUS.get(outcome.status, CHECK_FAILED)
    return {"status": status, "detail": "" if status == CHECK_OK else outcome.detail}


def _check_review(runner: str, model: str, script: Path | None, timeout: int) -> dict:
    script = script or Path.home() / ".claude" / "skills" / RUNNER_SKILLS[runner] / "scripts" / "run-review.sh"
    if not script.is_file():
        return {"status": CHECK_FAILED, "detail": f"스크립트 없음: {script}"}
    with tempfile.TemporaryDirectory(prefix="cascade-setup-check-") as tmp:
        init = _execute(["git", "init", "-q", tmp], "", Path(tmp), dict(os.environ), timeout)
        if init.returncode != 0:
            return {"status": CHECK_FAILED, "detail": f"빈 git 저장소를 만들지 못했다: {init.stderr.strip()}"}
        prompt = Path(tmp) / "prompt.md"
        prompt.write_text(SMOKE_PROMPT, encoding="utf-8")
        out = Path(tmp) / "out"
        cmd = [str(script), "--opinion", str(prompt), "--repo", tmp, "--out", str(out),
               *(["--model", model] if runner == RUNNER_CURSOR and model else [])]
        run = _execute(cmd, "", Path(tmp), dict(os.environ), timeout)
        log = "\n".join(out.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:]) if out.is_file() else ""
    if run.timed_out:
        return {"status": CHECK_FAILED, "detail": f"시간 초과({timeout}초)"}
    if run.missing:
        return {"status": CHECK_FAILED, "detail": f"스크립트를 실행하지 못했다: {run.stderr}"}
    tail = f"{log}\n{run.stdout}\n{run.stderr}".strip()[-DETAIL_TAIL:]
    if run.returncode == 0:
        return {"status": CHECK_OK, "detail": ""}
    if run.returncode == EXIT_NEEDS_LOGIN:
        return {"status": CHECK_NEEDS_LOGIN, "detail": tail}
    if run.returncode == EXIT_LIMIT:
        return {"status": CHECK_LIMIT, "detail": tail}
    status = _CHECK_STATUS.get(classify_failure(tail), CHECK_FAILED)
    return {"status": status, "detail": f"종료 코드 {run.returncode}: {tail}"}


def run_check(runner: str, path: str, model: str, effort: str, scripts: dict[str, Path], timeout: int = 300) -> dict:
    if path == WORKER:
        return _check_worker(runner, model, effort, timeout)
    return _check_review(runner, model, scripts.get(runner), timeout)


def acp_result(output: str, code: int) -> str:
    found = _LEAKS.search(output)
    if found is None:
        return ACP_UNKNOWN
    return ACP_CLEAN if int(found.group(1)) == 0 else ACP_LEAK


def read_record(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_record(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": RECORD_SCHEMA, **data}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _table_notice(record: dict, table: dict) -> str | None:
    old, env, priority = record.get("table_version"), record.get("env"), record.get("priority")
    if not isinstance(old, int) or table["version"] <= old or env not in table["env"] or priority not in recommend.PRIORITIES:
        return None
    new = recommend.recommended(table, env, priority)
    if new in (record.get("recommended"), record.get("profile")):
        return None
    return f"추천 표가 바뀌었다(v{old}→v{table['version']}): 이 기기 추천은 {new}. {_SETUP_HINT}"


def notices(*, record: dict | None, table: dict, default_profile: str | None, roles: dict,
            scripts: dict[str, tuple[str, list[str]]]) -> list[str]:
    found = []
    if record is None and default_profile is None:
        found.append("이 기기 설정 전이다. 「cascade-fix 설정해줘」로 쓸 수 있는 도구에 맞는 프로필을 고른다")
    if record is not None and (table_notice := _table_notice(record, table)):
        found.append(table_notice)
    for runner in sorted({runner for runner, path in check_paths(roles) if path == REVIEW}):
        skill = RUNNER_SKILLS[runner]
        if scripts.get(skill, (SAME, []))[0] != SAME:
            found.append(f"{skill} 스크립트가 플러그인 판과 다르다. {_SETUP_HINT}")
    return found
