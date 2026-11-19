import json
import shlex
from collections.abc import Callable
from pathlib import Path

from cascade.jobs import KIND_REVIEW, ROLES, _render

REVIEWER_CODEX = "codex"
REVIEWER_CURSOR = "cursor"
REVIEWER_CLAUDE = "claude"
REVIEWER_AUTO = "auto"
REVIEWER_CHOICES = (REVIEWER_AUTO, REVIEWER_CODEX, REVIEWER_CURSOR, REVIEWER_CLAUDE)
REVIEWER_LABELS = {REVIEWER_CODEX: "Codex", REVIEWER_CURSOR: "Cursor", REVIEWER_CLAUDE: "Claude"}
CLAUDE_SOURCES = ("claude-code", "claude-doc")
CURSOR_SOURCES = ("cursor-code", "cursor-doc")
DEFAULT_MODEL, DEFAULT_EFFORT = ROLES["reviewer"]
EFFORTS = ("low", "medium", "high", "xhigh", "max")
REVIEW_JOB = "review:reviewer"
REVIEW_META = "review-meta.json"
REVIEW_OUT = "review.out"
CALL_FAILED_NOTE = "(Claude 검토자 호출 실패 — 의견 없음)"

_CLAUDE_SUFFIX = """

## Claude 검토자 응답 규칙

- 참고할 저장소: `{repo}`. 읽기만 한다. 파일을 고치거나 커밋하지 않고, `git checkout`·`git switch`·`git reset`·`git stash`를 쓰지 않는다.
- 위 질문·출력 형식에 맞춘 답 전체를 `text`에 그대로 넣는다(마크다운 그대로). 스키마 밖에 글을 쓰지 않는다.
- `model_id`에는 너의 모델 ID를 넣는다.
"""


def label(reviewer: str) -> str:
    return REVIEWER_LABELS.get(reviewer, reviewer)


def resolve(choice: str, source: str, run_review: Path, cursor_ok: bool | Callable[[], bool] = False) -> str:
    if choice != REVIEWER_AUTO:
        return choice
    if source in CLAUDE_SOURCES:
        return REVIEWER_CLAUDE
    if Path(run_review).is_file():
        return REVIEWER_CODEX
    available = cursor_ok() if callable(cursor_ok) else cursor_ok
    return REVIEWER_CURSOR if available else REVIEWER_CLAUDE


def first_review_command(cell: dict, args: dict, review_dir: Path) -> tuple[str, str]:
    """외부 첫 검토(Codex `RR`, Cursor `CR`)를 돌릴 명령과 결과 파일(`<review_dir>/review.out.final`) 경로."""
    from cascade import cli_steps  # cli_steps가 이 모듈을 먼저 불러오므로 함수 안에서 부른다

    cursor = cell["runner"] == REVIEWER_CURSOR
    script = cli_steps.CURSOR_REVIEW if cursor else cli_steps.RUN_REVIEW
    if args["kind"] == "doc":
        scope = " ".join(f"--doc {shlex.quote(doc)}" for doc in args["docs"])
    elif args["uncommitted"]:
        scope = "--uncommitted"
    elif args["commit"]:
        scope = f"--commit {shlex.quote(args['commit'])}"
    else:
        scope = f"--base {shlex.quote(args['base'])}"
    out = Path(review_dir) / REVIEW_OUT
    focus = args["focus"] and not codex_code_review(cell, args)
    command = (f"{shlex.quote(str(script))} {scope}" + (f" --focus {shlex.quote(args['focus'])}" if focus else "")
               + f" --repo {shlex.quote(args['root'])} --out {shlex.quote(str(out))}"
               + (f" --model {shlex.quote(cell['model'])}" if cursor and cell["model"] else ""))
    if codex_code_review(cell, args):
        command = f"cd {shlex.quote(args['root'])} && {command}"  # codex-review의 코드 모드는 --repo를 무시하고 현재 디렉터리를 본다
    return command, f"{out}.final"


def codex_code_review(cell: dict, args: dict) -> bool:
    """codex-review의 코드 모드(`codex review`)는 범위와 지시문을 함께 받지 못해 --focus를 쓰지 못한다."""
    return args["kind"] == "code" and cell["runner"] == REVIEWER_CODEX


def claude_prompt(prompt: str, repo: str | Path) -> str:
    return prompt.rstrip("\n") + _CLAUDE_SUFFIX.format(repo=repo)


def review_job(job_id: str, label_text: str, prompt: str, model: str, effort: str) -> dict:
    return {"id": job_id, "label": label_text, "model": model, "effort": effort, "kind": KIND_REVIEW, "prompt": prompt}


def read_review_results(data) -> dict[str, tuple[str, str]]:
    """Workflow 반환값 {results: [{id, output: {text, model_id}}]} 에서 성공한 결과만 꺼낸다."""
    out: dict[str, tuple[str, str]] = {}
    items = data.get("results", []) if isinstance(data, dict) else []
    for item in items:
        output = item.get("output") if isinstance(item, dict) else None
        if not isinstance(output, dict):
            continue
        text, model_id = output.get("text"), output.get("model_id")
        if isinstance(text, str) and text.strip() and isinstance(model_id, str):
            out[item["id"]] = (text, model_id)
    return out


def load_text(path: Path, job_id: str | None = None) -> tuple[str, str | None]:
    """결과 파일을 읽는다. Workflow 결과 JSON이면 job_id(없으면 첫 성공 결과)의 text를, 아니면 파일 본문을 준다.

    반환: (본문, 모델 ID 또는 None). JSON인데 결과가 없으면 본문은 빈 문자열이다.
    """
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        return raw, None
    if not isinstance(data, dict) or "results" not in data:
        return raw, None
    results = read_review_results(data)
    if job_id is not None:
        text, model_id = results.get(job_id, ("", ""))
        return text, model_id or None
    if not results:
        return "", None
    text, model_id = next(iter(results.values()))
    return text, model_id


def model_warning(key: str, model_id: str | None, expected: str) -> str | None:
    if model_id is None:
        return None
    if not model_id:
        return f"경고: {key} 모델 ID가 비어 있다(기대값 {expected})"
    if model_id != expected:
        return f"경고: {key} 모델 ID가 {model_id} 이다(기대값 {expected})"
    return None


def _q(value) -> str:
    return shlex.quote(str(value))


def build_review_prompt(kind: str, root: Path, *, base: str | None = None, uncommitted: bool = False,
                        commit: str | None = None, docs: list[str] | None = None, focus: str = "") -> str:
    focus_note = f"특히 볼 관점: {focus}" if focus else "관점 제한 없음."
    if kind == "doc":
        listed = "\n".join(f"- `{d}`" for d in docs or []) or "(없음)"
        return _render("review-doc.md", {"repo": str(root), "docs": listed, "focus": focus_note})
    if uncommitted:
        scope = "커밋하지 않은 변경 전부(스테이징 여부 무관, 추적하지 않는 새 파일 포함)"
        cmds = [f"git -C {_q(root)} diff HEAD", f"git -C {_q(root)} ls-files --others --exclude-standard"]
    elif commit:
        scope = f"커밋 `{commit}` 하나의 변경"
        cmds = [f"git -C {_q(root)} show {_q(commit)}"]
    else:
        scope = f"`{base}` 브랜치에서 갈라진 뒤 HEAD까지의 변경"
        cmds = [f"git -C {_q(root)} diff {_q(f'{base}...HEAD')}"]
    return _render("review-code.md", {
        "repo": str(root),
        "scope": scope,
        "diff_cmds": "\n".join(f"- `{c}`" for c in cmds),
        "focus": focus_note,
    })
