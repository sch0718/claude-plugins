"""Keep secrets out of bundles: suspicious file names, token patterns, MCP env and header values."""
from __future__ import annotations

import copy
import fnmatch
import re
from pathlib import Path

NAME_PATTERNS = (".env", ".env.*", "*.pem", "*credentials*", "*token*", "*.key", "id_rsa*", "id_ed25519*")
CONTENT = re.compile(rb"sk-ant-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
                     rb"|xox[bp]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def scan_secrets(path: Path) -> list[str]:
    reasons = []
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in NAME_PATTERNS):
        reasons.append("비밀 파일 이름")
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                if CONTENT.search(tail + chunk):
                    reasons.append("토큰 형식 내용")
                    break
                tail = chunk[-128:]
    except OSError:
        reasons.append("읽기 실패")
    return reasons


def blank_mcp(servers: dict) -> dict:
    out = copy.deepcopy(servers)
    for spec in out.values():
        if not isinstance(spec, dict):
            continue
        for key in ("env", "headers"):
            if isinstance(spec.get(key), dict):
                spec[key] = {k: "" for k in spec[key]}
    return out
