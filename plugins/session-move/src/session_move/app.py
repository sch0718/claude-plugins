from __future__ import annotations

import os
import subprocess


def app_is_running() -> bool:
    fake = os.environ.get("SESSION_MOVE_FAKE_APP_RUNNING")
    if fake is not None:
        return fake == "1"
    out = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True, check=True).stdout
    return any(
        line.endswith("/Claude.app/Contents/MacOS/Claude") or "/Application Support/Claude/claude-code/" in line
        for line in out.splitlines()
    )
