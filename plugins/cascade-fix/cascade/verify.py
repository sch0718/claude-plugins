import os
import re
import shlex
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from cascade.config import RepoConfig
from cascade.findings import Finding

TIMEOUT = 600
TAIL_CHARS = 2000
REPRO_TAIL_CHARS = 500
TIMEOUT_TAIL = "시간 초과(600초)"
SKIP_DIRS = {"node_modules", ".git", ".cascade-fix", ".venv", "venv"}
INHERITED_GIT_ENV = ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")
FILES = "{files}"
SKIP_REF_CHARS = ("~", "<", "$", "{")
TEST_DIRS = {"tests", "test"}
TEST_NAME = re.compile(r"^(?:test_.+\.py|.+_test\.py|.+\.test\..+|.+\.spec\..+)$")
TEST_PATH_DIRS = {*TEST_DIRS, "__tests__"}
TEST_PATH_NAME = re.compile(r"^.+_test\.go$")
JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
DOC_EXTENSIONS = (".py", ".js", ".ts", ".md", ".toml", ".json", ".yaml", ".yml", ".sh", ".css", ".html")
EXTERNAL_PREFIX = "외부/"
FENCE = re.compile(r"^[ \t]*```([^\n`]*)\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)
URL = re.compile(r"https?://\S+")
PATH_LINE = re.compile(r"([\w./-]+):\d+(?:-\d+)?")
BACKTICK = re.compile(r"`([^`\s]+)`")
LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
NO_TESTS_FULL_TAG = "[관련 테스트 없음·전체 테스트로 대신] "
NO_TESTS_SKIP_TAG = "[관련 테스트 없음·실행 안 함] "


@dataclass
class VerifyResult:
    cmd: str
    ok: bool
    tail: str
    skipped: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        if not self.skipped:
            del data["skipped"]
        return data


def _run(cmd: str, cwd: Path) -> tuple[int, str, bool]:
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return -1, "", True
    return proc.returncode, (out or "") + (err or ""), False


def _result(cmd: str, cwd: Path) -> VerifyResult:
    rc, output, timed_out = _run(cmd, cwd)
    if timed_out:
        return VerifyResult(cmd=cmd, ok=False, tail=TIMEOUT_TAIL)
    return VerifyResult(cmd=cmd, ok=rc == 0, tail=output[-TAIL_CHARS:])


def _git_files(root: Path) -> list[str] | None:
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_GIT_ENV}
    try:
        p = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root, env=env,
                           capture_output=True, check=False)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    return sorted({x for x in p.stdout.decode("utf-8", errors="replace").split("\0") if x})


def _iter_files(root: Path):
    listed = _git_files(root)
    if listed is not None:
        for rel in listed:
            path = root / rel
            if not SKIP_DIRS & set(Path(rel).parts[:-1]) and path.is_file():
                yield path
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _python_modules(rel: Path) -> list[str]:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))]


def _python_import_pattern(rel: Path) -> re.Pattern | None:
    modules = _python_modules(rel)
    if not modules:
        return None
    alt = "|".join(re.escape(m) for m in modules)
    stem = re.escape(modules[-1].rsplit(".", 1)[-1])
    parents = [m.rsplit(".", 1)[0] for m in modules if "." in m]
    package = "|".join([r"\.+", *(rf"\.*{re.escape(p)}" for p in parents)])
    return re.compile(
        rf"^[ \t]*(?:import\s+(?:[\w.]+\s*,\s*)*(?:{alt})\b"
        rf"|from\s+(?:\.*(?:{alt}))\s+import\b"
        rf"|from\s+(?:{package})\s+import\s+(?:\((?:[^)#]|#[^\n]*\n)*?|[^\n#]*)\b{stem}\b)",
        re.M,
    )


def _js_import_pattern(rel: Path) -> re.Pattern:
    stem = re.escape(rel.stem)
    return re.compile(
        rf"""(?:from\s+|require\(\s*|import\s*\(\s*|import\s+)['"][^'"\n]*/{stem}(?:\.\w+)?['"]"""
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_test_path(path: str) -> bool:
    p = Path(path)
    return bool(TEST_NAME.match(p.name) or TEST_PATH_NAME.match(p.name) or TEST_PATH_DIRS & set(p.parts[:-1]))


def related_tests(root: Path, changed: list[str]) -> list[str]:
    root = Path(root)
    all_tests = [p for p in _iter_files(root) if TEST_NAME.match(p.name)]
    texts: dict[Path, str | None] = {}
    found: set[str] = set()
    for rel_str in changed:
        rel = Path(rel_str)
        stem = rel.stem
        name_matches = {f"test_{stem}", f"{stem}_test", f"{stem}.test", f"{stem}.spec"}
        import_pattern = None
        if rel.suffix == ".py":
            import_pattern = _python_import_pattern(rel)
        elif rel.suffix in JS_SUFFIXES:
            import_pattern = _js_import_pattern(rel)
        for test in all_tests:
            test_rel = test.relative_to(root)
            if test_rel.parent == rel.parent:
                found.add(test_rel.as_posix())
            elif TEST_DIRS & set(test_rel.parts[:-1]) and test.stem in name_matches:
                found.add(test_rel.as_posix())
            elif import_pattern is not None:
                if test not in texts:
                    texts[test] = _read_text(test)
                text = texts[test]
                if text is not None and import_pattern.search(text):
                    found.add(test_rel.as_posix())
    return sorted(found)


def _quoted(files: list[str]) -> str:
    return " ".join(shlex.quote(f) for f in files)


def format_command(cmd: str, files: list[str]) -> str:
    if FILES in cmd:
        return cmd.replace(FILES, _quoted(files))
    return f"{cmd} {_quoted(files)}" if files else cmd


def existing(wt: Path, files: list[str]) -> list[str]:
    return [f for f in files if (Path(wt) / f).is_file()]


def test_commands(cfg: RepoConfig, tests: list[str]) -> list[tuple[str, str, str]]:
    commands = []
    fell_back = False
    for c in cfg.tests:
        if FILES not in c or tests:
            commands.append((c, c.replace(FILES, _quoted(tests)).rstrip(), ""))
        elif not cfg.full_tests:
            commands.append((c, c.replace(FILES, "").rstrip(), NO_TESTS_SKIP_TAG))
        elif not fell_back:
            commands += [(c, full, NO_TESTS_FULL_TAG) for full in cfg.full_tests]
            fell_back = True
    return commands


def worker_commands(wt: Path, cfg: RepoConfig, changed: list[str]) -> list[tuple[str, str, str]]:
    wt = Path(wt)
    commands = test_commands(cfg, related_tests(wt, changed))
    commands += [(c, c, "") for c in (*cfg.build, *cfg.typecheck, *cfg.lint)]
    targets = existing(wt, changed)
    if targets:
        commands += [(c, format_command(c, targets), "") for c in cfg.format]
    return commands


def _tagged_result(cmd: str, tag: str, cwd: Path) -> VerifyResult:
    if tag == NO_TESTS_SKIP_TAG:
        return VerifyResult(cmd=tag + cmd, ok=False, tail="", skipped=True)
    result = _result(cmd, cwd)
    result.cmd = tag + result.cmd
    return result


def run_verifiers(
    wt: Path, cfg: RepoConfig, changed: list[str], stage: Literal["worker", "final"]
) -> list[VerifyResult]:
    wt = Path(wt)
    if stage == "final":
        return [_result(cmd, wt) for cmd in cfg.full_tests]
    return [_tagged_result(cmd, tag, wt) for _, cmd, tag in worker_commands(wt, cfg, changed)]


def run_worker_verifiers(wt: Path, cfg: RepoConfig, changed: list[str]) -> list[tuple[str, VerifyResult]]:
    return [(template, _tagged_result(cmd, tag, Path(wt))) for template, cmd, tag in worker_commands(wt, cfg, changed)]


def run_format(wt: Path, cfg: RepoConfig, files: list[str]) -> None:
    targets = existing(wt, files)
    if not targets:
        return
    for cmd in cfg.format:
        _run(format_command(cmd, targets), Path(wt))


def run_commands(wt: Path, commands: list[str]) -> list[VerifyResult]:
    return [_result(cmd, Path(wt)) for cmd in commands]


def _strip_fences(text: str) -> str:
    return FENCE.sub("", text)


def _looks_like_path(wt: Path, token: str) -> bool:
    if token.endswith(DOC_EXTENSIONS):
        return True
    return "/" in token and (wt / token.split("/", 1)[0]).exists()


def _repo_paths(wt: Path) -> set[str]:
    paths = set()
    for f in _iter_files(wt):
        rel = f.relative_to(wt)
        paths.add(rel.as_posix())
        paths.update(p.as_posix() for p in rel.parents if p != Path("."))
    return paths


def _ends_with(paths: set[str], ref: str) -> bool:
    return any(p == ref or p.endswith("/" + ref) for p in paths)


def check_doc_refs(wt: Path, doc: Path) -> list[str]:
    wt = Path(wt)
    wt_root = wt.resolve()
    text = URL.sub("", _strip_fences(Path(doc).read_text(encoding="utf-8")))
    candidates = [m.group(1) for m in PATH_LINE.finditer(text)]
    candidates += [LINE_SUFFIX.sub("", m.group(1)).rstrip(".,;)") for m in BACKTICK.finditer(text)]
    missing = set()
    repo_paths = None
    for path in candidates:
        if not path or not _looks_like_path(wt, path):
            continue
        if path.startswith(("http://", "https://", EXTERNAL_PREFIX)) or "*" in path:
            continue
        if any(ch in path for ch in SKIP_REF_CHARS):
            continue
        target = (wt / path).resolve()
        if not target.is_relative_to(wt_root):
            continue
        if target.exists():
            continue
        if repo_paths is None:
            repo_paths = _repo_paths(wt)
        if not _ends_with(repo_paths, path.strip("/")):
            missing.add(path)
    return sorted(missing)


def new_broken_refs(base_wt: Path | None, base_doc: Path | None, wt: Path, doc: Path) -> list[str]:
    after = check_doc_refs(wt, doc)
    if base_wt is None or base_doc is None or not Path(base_doc).is_file():
        return after
    before = set(check_doc_refs(base_wt, base_doc))
    return [m for m in after if m not in before]


def check_snippets(wt: Path, doc: Path, cfg: RepoConfig) -> list[VerifyResult]:
    wt = Path(wt)
    text = Path(doc).read_text(encoding="utf-8")
    results = []
    for n, match in enumerate(FENCE.finditer(text)):
        info = match.group(1).strip().split()
        lang = info[0] if info else ""
        command = cfg.doc_snippet_check.get(lang) or cfg.doc_snippet_check.get(lang.lower())
        if not lang or command is None:
            continue
        rel = f".cascade-fix/snippets/{n}.{lang}"
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(match.group(2), encoding="utf-8")
        result = _result(command.replace("{file}", rel), wt)
        result.cmd = f"[블록 {n}] {result.cmd}"
        results.append(result)
    return results


def _confirmed_paragraph(body: str) -> str | None:
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("**확인한 내용")), None)
    if start is None:
        return None
    collected = []
    in_fence = False
    for line in lines[start:]:
        stripped = line.strip()
        if not in_fence and collected and stripped.startswith(("**근거", "답글(")):
            break
        if stripped.startswith("```"):
            in_fence = not in_fence
        collected.append(line)
    return "\n".join(collected)


def repro_cmds(finding: Finding) -> list[str]:
    paragraph = _confirmed_paragraph(finding.body)
    if paragraph is None:
        return []
    cmds = []
    for match in FENCE.finditer(paragraph):
        lines = [line.strip() for line in match.group(2).splitlines()]
        prompted = [line[2:].strip() for line in lines if line.startswith("$ ")]
        cmds += [line for line in (prompted if prompted else lines) if line and not line.startswith("#")]
    return cmds


def run_repro(base_wt: Path, fix_wt: Path, cmds: list[str]) -> list[VerifyResult]:
    results = []
    for cmd in cmds:
        base_rc, _, base_timeout = _run(cmd, Path(base_wt))
        fix_rc, fix_out, fix_timeout = _run(cmd, Path(fix_wt))
        base_failed = base_timeout or base_rc != 0
        fix_passed = not fix_timeout and fix_rc == 0
        base_label = "시간 초과" if base_timeout else base_rc
        fix_label = "시간 초과" if fix_timeout else fix_rc
        tail = f"수정 전 rc={base_label} / 수정 후 rc={fix_label}\n{fix_out[-REPRO_TAIL_CHARS:]}"
        results.append(VerifyResult(cmd=cmd, ok=base_failed and fix_passed, tail=tail))
    return results
