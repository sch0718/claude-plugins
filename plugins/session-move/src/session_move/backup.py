"""Back up every path an apply will touch, and restore it exactly (removing paths that did not exist)."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import tarfile
import time
from pathlib import Path


def backup(targets: list[Path], state_dir: Path) -> Path:
    out_dir = state_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    out = out_dir / f"{stamp}.tar.gz"
    entries = []
    with tarfile.open(out, "w:gz") as tar:
        for i, t in enumerate(dict.fromkeys(Path(p) for p in targets)):
            existed = t.exists() or t.is_symlink()
            entries.append({"path": str(t), "existed": existed, "member": f"e{i}"})
            if existed:
                tar.add(t, arcname=f"e{i}")
        data = json.dumps({"entries": entries, "createdNs": time.time_ns()}, ensure_ascii=False).encode()
        info = tarfile.TarInfo("index.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    os.chmod(out, 0o600)
    return out


def _remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def restore(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        index = json.loads(tar.extractfile("index.json").read())
        for e in index["entries"]:
            p = Path(e["path"])
            _remove(p)
            if not e["existed"]:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            prefix = e["member"]
            for m in tar.getmembers():
                if m.name != prefix and not m.name.startswith(prefix + "/"):
                    continue
                rel = m.name[len(prefix):].lstrip("/")
                dest = p / rel if rel else p
                if m.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif m.issym():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(m.linkname, dest)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(m) as src, open(dest, "wb") as f:
                        shutil.copyfileobj(src, f)
                    os.chmod(dest, m.mode & 0o777)


def mark_applied(archive: Path) -> None:
    Path(str(archive) + ".applied").write_text(str(time.time_ns()))


def entries(archive: Path) -> tuple[list[Path], int]:
    with tarfile.open(archive, "r:gz") as tar:
        index = json.loads(tar.extractfile("index.json").read())
    since = index.get("createdNs", 0)
    applied = Path(str(archive) + ".applied")
    if applied.is_file():
        since = int(applied.read_text().strip() or since)
    return [Path(e["path"]) for e in index["entries"]], since


def changed_since(archive: Path) -> list[Path]:
    paths, since = entries(archive)
    out = []
    for root in paths:
        if root.is_symlink() or root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = [Path(dp) / n for dp, _, fs in os.walk(root) for n in fs]
        else:
            candidates = []
        out += [c for c in candidates if not c.is_symlink() and c.stat().st_mtime_ns > since]
    return out
