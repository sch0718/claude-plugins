"""Merge backlog items by id; the newer one (filedAt, else suggestedAt) wins."""
from __future__ import annotations

import copy


def _stamp(item: dict):
    return item.get("filedAt", item.get("suggestedAt"))


def merge_backlog(src: dict, dst: dict) -> tuple[dict, list[str]]:
    merged = copy.deepcopy(dst) if dst else {"version": (src or {}).get("version", 1)}
    items = merged.setdefault("items", [])
    pos = {it.get("id"): i for i, it in enumerate(items)}
    changed = []
    for it in (src or {}).get("items", []):
        iid = it.get("id")
        if iid not in pos:
            items.append(copy.deepcopy(it))
            pos[iid] = len(items) - 1
            changed.append(iid)
            continue
        a, b = _stamp(it), _stamp(items[pos[iid]])
        if a is not None and b is not None and a > b:
            items[pos[iid]] = copy.deepcopy(it)
            changed.append(iid)
    return merged, changed
