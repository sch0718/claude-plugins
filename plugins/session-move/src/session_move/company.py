"""Decide whether a session's work folder belongs to the company, without calling git."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_URL = re.compile(r'^\s*url\s*=\s*(\S+)', re.M)


def _norm(s: str) -> str:
    return re.sub(r"[-_.\s]+", "", s.lower())


def _git_config_for(cwd: Path) -> Path | None:
    for d in [cwd, *cwd.parents]:
        dot = d / ".git"
        if dot.is_dir():
            return dot / "config"
        if dot.is_file():
            text = dot.read_text(encoding="utf-8", errors="ignore").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (d / gitdir).resolve()
            common = gitdir / "commondir"
            if common.is_file():
                base = Path(common.read_text(encoding="utf-8").strip())
                base = base if base.is_absolute() else (gitdir / base).resolve()
                return base / "config"
            return gitdir / "config"
    return None


def origin_url(cwd: str) -> str | None:
    cfg = _git_config_for(Path(cwd))
    if not cfg or not cfg.is_file():
        return None
    text = cfg.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'\[remote "origin"\](.*?)(?=^\[|\Z)', text, re.S | re.M)
    if not m:
        return None
    u = _URL.search(m.group(1))
    return u.group(1) if u else None


def _url_parts(url: str) -> tuple[str, str]:
    path = re.sub(r"^[a-z+]+://[^/]+/", "", url)
    path = re.sub(r"^[^@]+@[^:]+:", "", path)
    path = path.removesuffix(".git").strip("/")
    parts = path.split("/")
    return (parts[-2] if len(parts) >= 2 else "", parts[-1] if parts else "")


@dataclass(frozen=True)
class CompanyRules:
    orgs: tuple[str, ...]
    repos: tuple[str, ...]
    path_prefixes: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> "CompanyRules | None":
        if not path.is_file():
            return None
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return None
        rules = cls(
            tuple(str(x) for x in data.get("orgs", []) if str(x).strip()),
            tuple(str(x) for x in data.get("repos", []) if str(x).strip()),
            tuple(str(x).rstrip("/") for x in data.get("path_prefixes", []) if str(x).strip()),
        )
        return rules if (rules.orgs or rules.repos or rules.path_prefixes) else None

    def terms(self) -> list[str]:
        return [*self.orgs, *self.repos]

    def is_company(self, cwd: str) -> bool:
        if not cwd:
            return False
        for prefix in self.path_prefixes:
            if cwd == prefix or cwd.startswith(prefix + "/"):
                return True
        orgs = {_norm(o) for o in self.orgs}
        repos = {_norm(r) for r in self.repos}
        if any(_norm(part) in repos for part in Path(cwd).parts):
            return True
        url = origin_url(cwd)
        if url:
            org, repo = _url_parts(url)
            if _norm(org) in orgs or _norm(repo) in repos:
                return True
        return False
