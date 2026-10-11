"""underflow.targets — room-temperature targets that can be changed from Home Assistant.

The comfort band (planner) and the step test's floor/ceiling used to live only in
apps.yaml, so changing a target meant editing a file on the HA box and redeploying.
Each can now be bound to an HA number helper (`input_number.*`) under `targets:`; the
value is read on every decide tick and overrides the apps.yaml value, which stays as
the fallback when a helper is missing, unavailable or nonsensical.

Pure: no HA.
"""
from __future__ import annotations

import dataclasses

# Hard limits a helper value must fall inside to be used at all. Wider than any sane
# setting; they exist so a mis-set slider or a garbage state can't drive the controller.
BOUNDS = {
    "comfort_min": (15.0, 24.0),
    "comfort_max": (17.0, 26.0),
    "test_floor": (15.0, 22.0),
    "test_ceiling": (19.0, 26.0),
}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None   # NaN -> None


def apply(planner_cfg, test_cfg, values: dict) -> tuple[object, object, list[str]]:
    """Return (planner_cfg, test_cfg, notes) with any valid helper values applied.

    `values` maps a key in BOUNDS to the raw helper state. A value outside its bounds is
    ignored, as is a band whose floor is not below its top; each rejection is noted so
    the caller can log it.
    """
    notes = []
    got = {}
    for key, raw in values.items():
        if key not in BOUNDS or raw is None:
            continue
        v = _num(raw)
        lo, hi = BOUNDS[key]
        if v is None:
            if str(raw) not in ("unknown", "unavailable", ""):
                notes.append(f"{key}={raw!r} ignored (not a number)")
            continue
        if not lo <= v <= hi:
            notes.append(f"{key}={raw!r} ignored (outside {lo:g}-{hi:g})")
            continue
        got[key] = v

    cmin = got.get("comfort_min", planner_cfg.comfort_min)
    cmax = got.get("comfort_max", planner_cfg.comfort_max)
    if cmin < cmax:
        planner_cfg = dataclasses.replace(planner_cfg, comfort_min=cmin, comfort_max=cmax)
    elif "comfort_min" in got or "comfort_max" in got:
        notes.append(f"comfort band {cmin:g}-{cmax:g} ignored (floor must be below top)")

    floor = got.get("test_floor", test_cfg.floor)
    ceiling = got.get("test_ceiling", test_cfg.ceiling)
    if ceiling is None or floor < ceiling:
        test_cfg = dataclasses.replace(test_cfg, floor=floor, ceiling=ceiling)
    elif "test_floor" in got or "test_ceiling" in got:
        notes.append(f"test floor/ceiling {floor:g}/{ceiling:g} ignored (floor must be below ceiling)")
    return planner_cfg, test_cfg, notes
