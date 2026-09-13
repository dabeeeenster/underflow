"""underflow.demand — where the desired minimum flow temperature comes from.

Two sources, in priority order:

**The mirror (stage 1).** 177 and 179 are not two houses; they are one open thermal
volume with two heat pumps, measured 0.91 K apart — closer than two rooms inside either
half. Two independent closed-loop controllers over one air mass fight each other, so
once Havenwise drives 177, 179 follows its demand rather than closing its own loop.
Havenwise's decisions arrive over 177's local eBUS: no cloud lag, no spend from the
~400/day myVAILLANT budget.

**The planner (fallback).** The local rule-based Agile schedule, used whenever the
mirror is disabled or its signal has gone stale.

Mirroring is deliberately not a blind copy. We read the demand signal over 177's link,
which is the *bad* one — 52 minutes offline in the 48 h to 13 Sep 2026 against 179's 32
seconds — so the follower must not be steered by a reading it cannot get. A last-good
value is held across dropouts, and once it ages past `max_age_seconds` the mirror is
abandoned for the local planner rather than following a frozen number all night. 179's
own comfort band and the screed cap bind on top, always.

Pure: no HA, no sockets.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class MirrorConfig:
    enabled: bool = False
    max_age_seconds: float = 2700.0   # 45 min, i.e. survive any plausible 177 outage
    floor: float = 20.0               # below this the curve alone decides anyway
    ceiling: float = 45.0             # the screed cap; never mirror past it


@dataclass(frozen=True)
class Held:
    """The last reading the mirror source actually gave us."""
    value: float | None = None
    at: dt.datetime | None = None

    def age(self, now: dt.datetime) -> float | None:
        return None if self.at is None else (now - self.at).total_seconds()


def hold(held: Held, reading, now: dt.datetime) -> Held:
    """Advance the last-good value if this reading worked; otherwise keep what we had."""
    if reading is not None and reading.ok:
        return Held(reading.value, reading.at or now)
    return held


def choose(held: Held, planner_setpoint: float | None, room_temp: float | None,
           planner_cfg, mirror_cfg: MirrorConfig, now: dt.datetime) -> tuple[float | None, str, str]:
    """Return (desired, source, reason). `source` is "mirror", "planner" or "none"."""
    if mirror_cfg.enabled:
        age = held.age(now)
        if held.value is not None and age is not None and age <= mirror_cfg.max_age_seconds:
            value = min(max(held.value, mirror_cfg.floor), mirror_cfg.ceiling)
            value, why = _comfort(value, room_temp, planner_cfg)
            fresh = "" if age < 60 else f", {age / 60:.0f} min old"
            return value, "mirror", f"following 177 at {held.value:g} °C{fresh}{why}"
        stale = "no reading yet" if age is None else f"last reading {age / 60:.0f} min old"
        if planner_setpoint is None:
            return None, "none", f"mirror unavailable ({stale}) and no plan"
        return planner_setpoint, "planner", f"mirror unavailable ({stale}), falling back to the local plan"
    if planner_setpoint is None:
        return None, "none", "no plan yet"
    return planner_setpoint, "planner", "local plan (mirror disabled)"


def _comfort(value: float, room_temp: float | None, cfg) -> tuple[float, str]:
    """179's own comfort band still binds on a mirrored setpoint — the slabs are
    separate even though the air is shared, so the follower keeps its own floor."""
    if room_temp is None:
        return value, ""
    if room_temp <= cfg.comfort_min and value < cfg.boost:
        return cfg.boost, f"; raised to {cfg.boost:g} as 179 is at {room_temp:.1f} °C"
    if room_temp >= cfg.comfort_max and value > cfg.baseline:
        return cfg.baseline, f"; cut to {cfg.baseline:g} as 179 is at {room_temp:.1f} °C"
    return value, ""
