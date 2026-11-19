"""Merge scheduled-task entries: add the ones the target does not have, never overwrite."""
from __future__ import annotations

import copy


def merge_schedules(src: dict, dst: dict, keep_enabled: bool) -> tuple[dict, list[str]]:
    merged = copy.deepcopy(dst) if dst else {}
    tasks = merged.setdefault("scheduledTasks", [])
    have = {t.get("id") for t in tasks}
    added = []
    for t in (src or {}).get("scheduledTasks", []):
        if t.get("id") in have:
            continue
        item = copy.deepcopy(t)
        if not keep_enabled:
            item["enabled"] = False
        tasks.append(item)
        added.append(item["id"])
    return merged, added
