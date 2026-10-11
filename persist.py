"""underflow.persist — survive restarts.

Ben restarts Home Assistant routinely for updates, and every HA restart makes AppDaemon
stop and re-initialise the app. State held only in memory, or in sensors the app creates
(HA forgets those on restart), was lost each time:
  * the step test's floor/ceiling latches — a cold room that had tripped the floor guard
    would see its off block resume after the restart;
  * "a test was running" — a test ending during the restart could leave zone 1 off;
  * the target, the Kalman slab estimate (hours to re-converge), the forecast cache.

This keeps them in a JSON file next to the app's config, written atomically (temp file +
rename, so a crash mid-write leaves the previous copy intact) after every tick, and read
back on start-up. Anything missing, corrupt or too old is ignored and the app starts as
it would have without the file.

Pure apart from the file I/O: no HA.
"""
from __future__ import annotations

import datetime as dt
import json
import os

VERSION = 1


def _iso(t):
    return t.isoformat() if isinstance(t, dt.datetime) else None


def _dt(s):
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def save(path: str, state: dict, now: dt.datetime) -> None:
    doc = {"version": VERSION, "saved_at": _iso(now), **state}
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, default=_iso)
    os.replace(tmp, path)


def load(path: str, now: dt.datetime, max_age_h: float = 24.0) -> dict | None:
    """The saved state, or None if absent, unreadable, from another format, or older than
    `max_age_h` (a state that old describes a world that has moved on)."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
        return None
    saved = _dt(doc.get("saved_at"))
    if saved is None or (now - saved).total_seconds() > max_age_h * 3600:
        return None
    doc["saved_at"] = saved
    return doc


def block_key(block):
    """A step-test block as (index, start ISO) — stable across restarts."""
    return None if block is None else [block.index, _iso(block.start)]


def same_block(key, block) -> bool:
    return bool(key and block and key[0] == block.index and _dt(key[1]) == block.start)


def series_out(series):
    return [[_iso(t), v] for t, v in series or []]


def series_in(raw):
    out = []
    for item in raw or []:
        try:
            t, v = _dt(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if t is not None:
            out.append((t, v))
    return out
