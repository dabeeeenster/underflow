"""underflow.demand — where the desired minimum flow temperature comes from.

**The model planner (stage 2, `model.live`).** Its plan for the slot now (mpc.py): a
minimum flow, or heat off altogether. Comfort binds on top, as on everything else.
Without a usable plan the pump falls back to the sources below. See `from_model`.

The older sources, in priority order:

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
    return planner_setpoint, "planner", "local plan"


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


def from_energy(plan, room_temp: float | None, cfg) -> tuple[float, str, bool] | None:
    """The live energy planner's demand for now (energy.py): (min flow, reason, urgent),
    or None without a plan, when the price planner stands in. A slot it didn't buy runs
    on the curve (the baseline). The comfort band binds on top, as everywhere."""
    if plan is None:
        return None
    lvl = plan.level_now
    value = cfg.baseline if lvl is None else float(lvl)
    short = f", {plan.shortfall_kwh:.0f} kWh short under the price cap" if plan.shortfall_kwh > 0.5 else ""
    tally = (f"the next {plan.hours:g} h need {plan.need_kwh:.0f} kWh, the curve gives {plan.baseline_kwh:.0f}, "
             f"the plan buys {plan.planned_kwh - plan.baseline_kwh:.0f} more{short}")
    why = (f"energy planner: {lvl:g} °C in a bought slot; {tally}" if lvl is not None else
           f"energy planner: curve only this slot; {tally}")
    if room_temp is not None:
        if room_temp <= cfg.comfort_min and value < cfg.boost:
            return cfg.boost, f"{why}; raised to {cfg.boost:g} as the room is at {room_temp:.1f} °C", True
        if room_temp >= cfg.comfort_max and value > cfg.baseline:
            return cfg.baseline, f"{why}; cut to the curve as the room is at {room_temp:.1f} °C", True
    return value, why, False


def from_model(plan: dict | None, room_temp: float | None, cfg) -> tuple[float, bool, str, bool] | None:
    """The live model planner's demand for now: (min flow, heat_off, reason, urgent), or
    None if there is no usable plan, when the caller falls back to the price planner.

    "off" means no space heating: the pump turns the zone and the room thermostats off,
    and holds the register at the baseline so nothing is left boosted. The comfort band
    still binds: at or below the floor an "off" or a low level becomes a boost, and at or
    above the top a boost is cut to the baseline; either override is `urgent`, so it
    doesn't wait out the dwell.

    No room reading is no plan: with the comfort room unavailable the model would keep
    planning from a stale estimate, and nothing could catch it getting cold (the floor
    override and the watchdog both need the reading). The price planner and the gate both
    fail towards heat without one."""
    if room_temp is None:
        return None
    if not plan or plan.get("level_now") is None or plan.get("state") in (None, "waiting"):
        return None
    lvl = plan["level_now"]
    pred = plan.get("predicted_min")
    ahead = f"; coldest room forecast {pred:.1f} °C over the next day" if isinstance(pred, (int, float)) else ""
    if str(lvl).lower() == "off":
        if room_temp <= cfg.comfort_min:
            return cfg.boost, False, (f"model planner has heat off, but the room is {room_temp:.1f} °C "
                                      f"at the {cfg.comfort_min:g} °C floor, so boosting to {cfg.boost:g}"), True
        return cfg.baseline, True, f"model planner: heat off{ahead}", False
    value, why = _comfort(float(lvl), room_temp, cfg)
    return value, False, f"model planner: {float(lvl):g} °C{ahead}{why}", bool(why)
