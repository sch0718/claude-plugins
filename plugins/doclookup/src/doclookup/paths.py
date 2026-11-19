import functools
import hashlib
import re
import unicodedata
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, encoding="utf-8", errors="replace", timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None


def checkout_root(cwd: Path) -> Path:
    top = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(top) if top else cwd


def normalize_remote(url: str) -> str:
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    elif match := _SCP_LIKE.match(url):
        host, path = match.group(1).lower(), match.group(2)
    else:
        host, path = "", url
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")].rstrip("/")
    return f"{host}/{path}" if host else path


def repo_keys(checkout: Path) -> list[str]:
    keys = []
    origin = _git(checkout, "remote", "get-url", "origin")
    if origin:
        keys.append(normalize_remote(origin))
    common_dir = _git(checkout, "rev-parse", "--git-common-dir")
    name = checkout.name
    if common_dir:
        resolved = (checkout / common_dir).resolve()
        if resolved.name == ".git":
            name = resolved.parent.name
    if name not in keys:
        keys.append(name)
    return keys


def cache_root(env: Mapping[str, str], platform: str) -> Path:
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Caches" / "doclookup"
    xdg = env.get("XDG_CACHE_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / "doclookup"
    return home / ".cache" / "doclookup"


def index_path(checkout: Path, env: Mapping[str, str], platform: str) -> Path:
    root = cache_root(env, platform)
    absolute = checkout.resolve()
    digest = hashlib.sha256(str(absolute).encode("utf-8")).hexdigest()[:12]
    folder = root / f"{absolute.name}-{digest}"
    for directory in (root, folder):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return folder / "index.sqlite"


def _segment(part: str) -> str:
    return "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part)


@functools.lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern:
    """`**`는 폴더 여러 단을(없어도 됨), `*`·`?`는 한 단 안의 글자를 맞춘다. 맞춤은 저장소 루트 기준 전체 경로다."""
    parts = unicodedata.normalize("NFC", pattern).strip("/").split("/")
    out = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
        else:
            out.append(_segment(part) + ("" if last else "/"))
    return re.compile("".join(out))


def matches_glob(rel: str, patterns: list[str]) -> bool:
    return any(glob_regex(p).fullmatch(rel) for p in patterns)
