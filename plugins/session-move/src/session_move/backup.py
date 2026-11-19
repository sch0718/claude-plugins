"""Back up every path an apply will touch, and restore it exactly (removing paths that did not exist)."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import tarfile
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
        data = json.dumps({"entries": entries}, ensure_ascii=False).encode()
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
