"""Rewrite absolute paths when moving to a device with a different home folder."""
from __future__ import annotations

import json
import re
from pathlib import Path

_ENC = re.compile(r"[^A-Za-z0-9-]")
SESSION_PATH_KEYS = ("cwd", "originCwd", "worktreePath", "gitAnchorsFolderRealpath", "gitAnchors", "shellFolders")


def encode_project_dir(cwd: str) -> str:
    """Claude Code names a transcript folder after the cwd with every non [A-Za-z0-9-] char turned into '-'."""
    return _ENC.sub("-", cwd)


class PathMap:
    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = sorted(((o.rstrip("/"), n.rstrip("/")) for o, n in pairs), key=lambda p: len(p[0]), reverse=True)

    @classmethod
    def parse(cls, specs: list[str]) -> "PathMap":
        pairs = []
        for s in specs or []:
            if "=" not in s:
                raise ValueError(f"--map-path 는 OLD=NEW 형식입니다: {s}")
            old, new = s.split("=", 1)
            pairs.append((old, new))
        return cls(pairs)

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def apply(self, s: str) -> str:
        for old, new in self.pairs:
            if s == old or s.startswith(old + "/"):
                return new + s[len(old):]
        return s

    def apply_in_text(self, s: str) -> str:
        for old, new in self.pairs:
            s = re.sub(re.escape(old) + r"(?=/|$|[\s\"'])", new.replace("\\", "\\\\"), s)
        return s

    def map_project_dir(self, name: str) -> str:
        for old, new in self.pairs:
            eo = encode_project_dir(old)
            if name == eo or name.startswith(eo + "-"):
                return encode_project_dir(new) + name[len(eo):]
        return name


def _deep(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_deep(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _deep(v, fn) for k, v in value.items()}
    return value


def remap_session(rec: dict, pm: PathMap) -> dict:
    out = dict(rec)
    for key in SESSION_PATH_KEYS:
        if key in out:
            out[key] = _deep(out[key], pm.apply)
    return out


def remap_schedule(item: dict, pm: PathMap) -> dict:
    out = dict(item)
    for key in ("cwd", "filePath"):
        if isinstance(out.get(key), str):
            out[key] = pm.apply(out[key])
    return out


def remap_backlog_item(item: dict, pm: PathMap) -> dict:
    out = dict(item)
    if isinstance(out.get("cwd"), str):
        out["cwd"] = pm.apply(out["cwd"])
    return out


def remap_settings(settings: dict, pm: PathMap) -> dict:
    return _deep(settings, pm.apply_in_text)


def rewrite_jsonl(src: Path, dst: Path, pm: PathMap) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, encoding="utf-8", errors="surrogateescape") as fin, \
            open(dst, "w", encoding="utf-8", errors="surrogateescape") as fout:
        for line in fin:
            stripped = line.rstrip("\n")
            try:
                obj = json.loads(stripped)
            except ValueError:
                fout.write(line)
                continue
            if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                new = pm.apply(obj["cwd"])
                if new != obj["cwd"]:
                    obj["cwd"] = new
                    fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    continue
            fout.write(line)
