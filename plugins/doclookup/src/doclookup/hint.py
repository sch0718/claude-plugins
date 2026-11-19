"""PreToolUse(Read) 훅: 설정한 범위의 큰 파일을 통째로 읽으려 하면 조회 명령을 안내한다. 막지 않는다."""

import json
import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from doclookup.config import find_config
from doclookup.index import target_files
from doclookup.paths import checkout_root

MESSAGE = "이 파일은 {n}자다. 먼저 doclookup search·section·impact로 필요한 범위를 찾고 Read의 offset·limit으로 읽는다."


def _nfc(path: Path) -> str:
    return unicodedata.normalize("NFC", str(path))


def _guidance(stdin: str, cwd: Path, env: Mapping[str, str], bundled: Path) -> str | None:
    payload = json.loads(stdin)
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None
    file_path = tool_input.get("file_path")
    if not (isinstance(file_path, str) and file_path):
        return None
    base = Path(payload["cwd"]) if isinstance(payload.get("cwd"), str) else cwd
    file = Path(os.path.abspath(base / file_path))
    if not file.is_file():
        return None

    root = checkout_root(file.parent)
    cfg = find_config(root, env, bundled)
    if not cfg.hint.enabled or file.stat().st_size < cfg.hint.min_chars:
        return None
    targets = {_nfc(p) for p in target_files(cfg, root)}
    if not {_nfc(file), _nfc(file.resolve())} & targets:
        return None
    n = len(file.read_text(encoding="utf-8"))
    return MESSAGE.format(n=n) if n >= cfg.hint.min_chars else None


def run(stdin: str, cwd: Path, env: Mapping[str, str], bundled: Path) -> tuple[str, int]:
    """(표준 출력에 낼 글, 종료 코드). 안내할 것이 없거나 어떤 예외가 나도 ("", 0)이다."""
    try:
        message = _guidance(stdin, cwd, env, bundled)
    except Exception:
        return "", 0
    if message is None:
        return "", 0
    output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}}
    return json.dumps(output, ensure_ascii=False), 0
