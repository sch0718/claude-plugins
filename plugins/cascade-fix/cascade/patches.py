import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cascade.config import RepoConfig
from cascade.verify import is_test_path, run_format

INHERITED_GIT_ENV = ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]
STDERR_TAIL = 500
DROPPED_PREFIXES = ("index ", "similarity index ", "dissimilarity index ")
GIT_HEADER = "diff --git "
BINARY_MARKERS = ("GIT binary patch", "Binary files ")
DIFF_FLAGS = ["-U3", "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--no-renames", "--no-ext-diff", "--no-textconv", "--binary"]
HUNK_HEADER = re.compile(r"^(@@ -\S+ \+\S+ @@).*$")
SETUP_PATHS = "cascade-setup-paths"
C_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r"}


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"applied": list(self.applied), "failed": [[fid, tail] for fid, tail in self.failed]}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=False)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _git_checked(cwd: Path, *args: str) -> str:
    p = _git(cwd, *args)
    if p.returncode != 0:
        raise RuntimeError(f"git 명령 실패 (git {' '.join(args)}): {_decode(p.stderr).strip()}")
    return _decode(p.stdout)


def _z_list(out: str) -> list[str]:
    return [x for x in out.split("\0") if x]


def _setup_paths_file(wt: Path) -> Path:
    return Path(_git_checked(wt, "rev-parse", "--absolute-git-dir").strip()) / SETUP_PATHS


def record_setup_paths(wt: Path) -> list[str]:
    paths = [p.rstrip("/") for p in _z_list(_git_checked(Path(wt), "ls-files", "--others", "--exclude-standard",
                                                         "--directory", "-z"))]
    _setup_paths_file(Path(wt)).write_text("\n".join(paths), encoding="utf-8")
    return paths


def _setup_paths(wt: Path) -> list[str]:
    path = _setup_paths_file(wt)
    return [p for p in path.read_text(encoding="utf-8").split("\n") if p] if path.is_file() else []


def touched_files(wt: Path, snap_commit: str) -> list[str]:
    wt = Path(wt)
    tracked = _z_list(_git_checked(wt, "diff", "--name-only", "--no-renames", "-z", snap_commit))
    untracked = _z_list(_git_checked(wt, "ls-files", "--others", "--exclude-standard", "-z"))
    setup = _setup_paths(wt)
    files = [f for f in dict.fromkeys([*tracked, *untracked])
             if not any(f == p or f.startswith(p + "/") for p in setup)]
    return files


def extract(wt: Path, snap_commit: str, cfg: RepoConfig | None = None) -> str:
    wt = Path(wt)
    files = touched_files(wt, snap_commit)
    if not files:
        return ""
    if cfg is not None:
        run_format(wt, cfg, files)
    _git_checked(wt, *NO_HOOKS, "add", "-A")
    return _decode(_staged_diff(wt, snap_commit, files))


def _staged_diff(wt: Path, snap_commit: str, files: list[str]) -> bytes:
    args = ("--literal-pathspecs", "-c", "core.quotepath=false", "diff", "--cached", *DIFF_FLAGS, snap_commit, "--", *files)
    p = _git(wt, *args)
    if p.returncode != 0:
        raise RuntimeError(f"git 명령 실패 (git {' '.join(args)}): {_decode(p.stderr).strip()}")
    return p.stdout


def is_lossy(wt: Path, snap_commit: str) -> bool:
    """extract가 뽑은 패치가 UTF-8로 읽히지 않아 바꿔 읽은 바이트가 있는지. 파일에 원래 있던 U+FFFD는 손실이 아니다."""
    files = touched_files(Path(wt), snap_commit)
    if not files:
        return False
    try:
        _staged_diff(Path(wt), snap_commit, files).decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def normalize(diff: str) -> str:
    lines = []
    for line in diff.split("\n"):
        if line.startswith(DROPPED_PREFIXES):
            continue
        line = line.rstrip(" \t")
        m = HUNK_HEADER.match(line)
        lines.append(m.group(1) if m else line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def same(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def _unquote(rest: str) -> tuple[str, str] | None:
    out = bytearray()
    i = 1
    while i < len(rest):
        c = rest[i]
        if c == '"':
            return out.decode("utf-8", errors="replace"), rest[i + 1:]
        if c != "\\":
            out += c.encode("utf-8")
            i += 1
        elif rest[i + 1:i + 4].isdigit():
            out.append(int(rest[i + 1:i + 4], 8))
            i += 4
        else:
            out += C_ESCAPES.get(rest[i + 1:i + 2], rest[i + 1:i + 2]).encode("utf-8")
            i += 2
    return None


def _git_header_path(line: str) -> str | None:
    rest = line[len(GIT_HEADER):]
    if rest.startswith('"'):
        first = _unquote(rest)
        second = _unquote(first[1][1:]) if first and first[1].startswith(' "') else None
        if second and not second[1] and first[0].startswith("a/") and second[0] == "b/" + first[0][2:]:
            return first[0][2:]
        return None
    rest = rest[len("a/"):] if rest.startswith("a/") else ""
    size = (len(rest) - len(" b/")) // 2
    if size > 0 and rest[:size] == rest[size + len(" b/"):] and rest[size:size + 3] == " b/":
        return rest[:size]
    return None


def changed_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.split("\n"):
        if line.startswith(GIT_HEADER):
            path = _git_header_path(line.rstrip("\r"))
            if path is not None and path not in files:
                files.append(path)
    return files


def _cuts_lines(section: str) -> bool:
    in_hunk = False
    new_file = False
    for line in section.split("\n")[1:]:
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if line.startswith("-"):
                return True
        elif line.startswith("new file mode"):
            new_file = True
        elif line.startswith("deleted file mode") or (line.startswith(BINARY_MARKERS) and not new_file):
            return True
    return False


def cut_test_files(diff: str) -> list[str]:
    return list(dict.fromkeys(path for path, text in _sections(diff)
                              if path is not None and is_test_path(path) and _cuts_lines(text)))


def _sections(diff: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, list[str]]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith(GIT_HEADER) or not sections:
            path = _git_header_path(line.rstrip("\r\n")) if line.startswith(GIT_HEADER) else None
            sections.append((path, []))
        sections[-1][1].append(line)
    return [(path, "".join(lines)) for path, lines in sections]


def _apply_outside(work: Path, rel: str, original: Path, text: str) -> tuple[bytes | None, str | None]:
    target = work / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if original.is_file():
        target.write_bytes(original.read_bytes())
    patch_file = work / "section.patch"
    patch_file.write_bytes(text.encode("utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_GIT_ENV}
    env["GIT_CEILING_DIRECTORIES"] = str(work.parent)
    p = subprocess.run(["git", "apply", str(patch_file)], cwd=work, env=env, capture_output=True, check=False)
    if p.returncode != 0:
        return None, _decode(p.stderr)[-STDERR_TAIL:]
    return (target.read_bytes() if target.exists() else None), None


def apply_all(root: Path, patches: list[tuple[str, str]], external: dict[str, Path] | None = None) -> ApplyReport:
    root = Path(root)
    external = {k: Path(v) for k, v in (external or {}).items()}
    report = ApplyReport()
    with tempfile.TemporaryDirectory() as tmp:
        for index, (finding_id, diff) in enumerate(patches):
            if not diff.strip():
                report.applied.append(finding_id)
                continue
            sections = _sections(diff)
            inner = "".join(text for path, text in sections if path not in external)
            outside: dict[str, bytes | None] = {}
            error = None
            for n, (path, text) in enumerate(sections):
                if path in external:
                    content, error = _apply_outside(Path(tmp) / f"{index}-{n}", path, external[path], text)
                    if error is not None:
                        break
                    outside[path] = content
            if error is None and inner.strip():
                patch_file = Path(tmp) / f"{index}.patch"
                patch_file.write_bytes(inner.encode("utf-8"))
                check = _git(root, "apply", "--check", str(patch_file))
                if check.returncode == 0:
                    check = _git(root, "apply", str(patch_file))
                if check.returncode != 0:
                    error = _decode(check.stderr)[-STDERR_TAIL:]
            if error is not None:
                report.failed.append((finding_id, error))
                continue
            for path, content in outside.items():
                if content is None:
                    external[path].unlink(missing_ok=True)
                else:
                    external[path].write_bytes(content)
            report.applied.append(finding_id)
    return report
