import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RUN_DIR_NAME = ".cascade-fix"
EXCLUDE_LINE = f"{RUN_DIR_NAME}/"
BRANCH_PREFIX = "cascade-fix/"
EXTERNAL_PREFIX = "외부"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INHERITED_GIT_ENV = ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")
COMMIT_IDENTITY = ["-c", "user.name=cascade-fix", "-c", "user.email=cascade-fix@local", "-c", "commit.gpgsign=false"]


@dataclass
class Snapshot:
    root: Path
    run_id: str
    branch: str
    commit: str
    run_dir: Path
    worktrees: dict[str, Path] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "run_id": self.run_id,
            "branch": self.branch,
            "commit": self.commit,
            "run_dir": str(self.run_dir),
            "worktrees": {name: str(path) for name, path in self.worktrees.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(
            root=Path(data["root"]),
            run_id=data["run_id"],
            branch=data["branch"],
            commit=data["commit"],
            run_dir=Path(data["run_dir"]),
            worktrees={name: Path(path) for name, path in data.get("worktrees", {}).items()},
        )


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    full_env = {k: v for k, v in os.environ.items() if k not in INHERITED_GIT_ENV}
    full_env.update(env or {})
    try:
        p = subprocess.run(["git", *args], cwd=cwd, env=full_env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git 명령 실패 (git {' '.join(args)}): {e.stderr.strip()}") from e
    return p.stdout.strip()


def _no_hooks(run_dir: Path) -> list[str]:
    empty = run_dir / "no-hooks"
    empty.mkdir(parents=True, exist_ok=True)
    return ["-c", f"core.hooksPath={empty}"]


def _check_name(name: str, what: str) -> None:
    if not SAFE_NAME.fullmatch(name):
        raise RuntimeError(f"{what}에 쓸 수 없는 값: {name!r} (영문·숫자로 시작하고 영문·숫자·._- 만)")


def ensure_excluded(root: Path) -> None:
    root = Path(root)
    exclude = Path(_git(root, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = root / exclude
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if EXCLUDE_LINE in text.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if text == "" or text.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}{EXCLUDE_LINE}\n")


def create_snapshot(root: Path, run_id: str, extra_files: list[Path]) -> Snapshot:
    _check_name(run_id, "실행 ID")
    root = Path(root).resolve()
    try:
        head = _git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    except RuntimeError:
        raise RuntimeError("커밋이 없는 저장소") from None
    names = [Path(p).name for p in extra_files]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise RuntimeError(f"저장소 밖 문서의 파일명이 겹친다: {', '.join(duplicated)}")

    ensure_excluded(root)
    run_dir = root / RUN_DIR_NAME / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tree = _write_tree(root, run_dir, head, extra_files)
    no_hooks = _no_hooks(run_dir)
    commit = _git(root, *no_hooks, *COMMIT_IDENTITY, "commit-tree", tree, "-p", head,
                  "-m", f"cascade-fix 스냅샷 {run_id}")
    branch = f"{BRANCH_PREFIX}{run_id}"
    _git(root, *no_hooks, "branch", branch, commit)
    return Snapshot(root=root, run_id=run_id, branch=branch, commit=commit, run_dir=run_dir)


def _write_tree(root: Path, run_dir: Path, head: str, extra_files: list[Path]) -> str:
    no_hooks = _no_hooks(run_dir)
    env = {"GIT_INDEX_FILE": str(run_dir / "index.tmp")}
    _git(root, *no_hooks, "read-tree", head, env=env)
    _git(root, *no_hooks, "add", "-A", env=env)
    if extra_files:
        external = run_dir / "external"
        external.mkdir(exist_ok=True)
        for src in extra_files:
            copy = external / Path(src).name
            shutil.copyfile(src, copy)
            blob = _git(root, *no_hooks, "hash-object", "-w", str(copy))
            _git(root, *no_hooks, "update-index", "--add", "--cacheinfo",
                 f"100644,{blob},{EXTERNAL_PREFIX}/{copy.name}", env=env)
    return _git(root, *no_hooks, "write-tree", env=env)


def snapshot_tree(snap: Snapshot, extra_files: list[Path]) -> str:
    root = Path(snap.root)
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    ref = f"refs/heads/{snap.branch}"
    parent = _git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    tree = _write_tree(root, snap.run_dir, head, extra_files)
    no_hooks = _no_hooks(snap.run_dir)
    commit = _git(root, *no_hooks, *COMMIT_IDENTITY, "commit-tree", tree, "-p", parent,
                  "-m", f"cascade-fix 스냅샷 {snap.run_id} 갱신")
    _git(root, *no_hooks, "update-ref", ref, commit, parent)
    return commit


def add_worktree(snap: Snapshot, name: str, commit: str | None = None) -> Path:
    _check_name(name, "워크트리 이름")
    path = snap.run_dir / "wt" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(snap.root, *_no_hooks(snap.run_dir), "worktree", "add", "--detach", str(path), commit or snap.commit)
    snap.worktrees[name] = path
    return path


def remove_worktree(snap: Snapshot, name: str) -> None:
    path = snap.worktrees.pop(name, None)
    if path is not None and Path(path).exists():
        _git(snap.root, *_no_hooks(snap.run_dir), "worktree", "remove", "--force", str(path))


def user_tree_fingerprint(root: Path, run_dir: Path) -> dict:
    root = Path(root)
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    no_hooks = _no_hooks(run_dir)
    env = {"GIT_INDEX_FILE": str(Path(run_dir) / "index.fingerprint")}
    _git(root, *no_hooks, "read-tree", head, env=env)
    _git(root, *no_hooks, "add", "-A", env=env)
    tree = _git(root, *no_hooks, "write-tree", env=env)
    (Path(run_dir) / "index.fingerprint").unlink(missing_ok=True)
    try:
        stash = _git(root, "rev-parse", "-q", "--verify", "refs/stash")
    except RuntimeError:
        stash = ""
    head_ref = _git(root, "rev-parse", "--symbolic-full-name", "HEAD")
    refs = _git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    branches = sorted(line for line in refs.splitlines() if not line.startswith(f"refs/heads/{BRANCH_PREFIX}"))
    return {"tree": tree, "head": head, "head_ref": head_ref, "stash": stash,
            "attached": _attached_run_worktrees(root, run_dir), "branches": branches}


def _attached_run_worktrees(root: Path, run_dir: Path) -> list[str]:
    wt_dir = (Path(run_dir) / "wt").resolve()
    attached, path = [], None
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch ") and path is not None and path.parent == wt_dir:
            attached.append(f"{path.name} {line.removeprefix('branch ')}")
    return sorted(attached)


def tree_diff_stat(root: Path, old_tree: str, new_tree: str) -> str:
    try:
        return _git(Path(root), "diff", "--stat", old_tree, new_tree)
    except RuntimeError as e:
        return str(e)


def commit_worktree(wt: Path, message: str) -> str | None:
    wt = Path(wt)
    no_hooks = ["-c", "core.hooksPath=/dev/null"]
    _git(wt, *no_hooks, "add", "-A")
    if _git(wt, *no_hooks, "status", "--porcelain") == "":
        return None
    _git(wt, *no_hooks, *COMMIT_IDENTITY, "commit", "-q", "--no-verify", "-m", message)
    return _git(wt, "rev-parse", "HEAD")


def _run_worktrees(snap: Snapshot) -> list[Path]:
    wt_root = (snap.run_dir / "wt").resolve()
    listed = _git(snap.root, "worktree", "list", "--porcelain").splitlines()
    paths = [Path(line[len("worktree "):]) for line in listed if line.startswith("worktree ")]
    return [p for p in paths if p.resolve().is_relative_to(wt_root)]


def cleanup(snap: Snapshot, keep_run_dir: bool) -> None:
    run_dir = snap.run_dir.resolve()
    if run_dir.parent != (snap.root / RUN_DIR_NAME).resolve():
        raise RuntimeError(f"run_dir이 {snap.root / RUN_DIR_NAME} 아래가 아니라 정리하지 않는다: {snap.run_dir}")
    no_hooks = _no_hooks(snap.run_dir)
    errors: list[str] = []

    def attempt(*args: str) -> None:
        try:
            _git(snap.root, *no_hooks, *args)
        except RuntimeError as e:
            errors.append(str(e))

    try:
        paths = set(_run_worktrees(snap))
    except RuntimeError as e:
        errors.append(str(e))
        paths = set()
    paths |= {p for p in snap.worktrees.values()}
    for path in sorted(paths):
        if path.exists():
            attempt("worktree", "remove", "--force", str(path))
    snap.worktrees.clear()
    try:
        if _git(snap.root, "branch", "--list", snap.branch):
            attempt("branch", "-D", snap.branch)
    except RuntimeError as e:
        errors.append(str(e))

    if keep_run_dir:
        shutil.rmtree(snap.run_dir / "wt", ignore_errors=True)
        (snap.run_dir / "index.tmp").unlink(missing_ok=True)
    else:
        shutil.rmtree(snap.run_dir, ignore_errors=True)
    attempt("worktree", "prune")
    if errors:
        raise RuntimeError("정리 실패 (나머지는 정리함):\n" + "\n".join(errors))
