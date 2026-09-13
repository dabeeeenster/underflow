"""underflow.planner — stage 1 rule-based Agile shifting.

Pure functions, no HA dependency. Given the half-hourly import rates for the next day or
so, the current room temperature and a small config, decide the minimum flow temperature
to ask the heat pump controller for in each slot.

Rules (deliberately simple; the optimiser replaces them in stage 3):
  * cheapest `cheap_fraction` of the upcoming slots      -> `boost` min flow
  * the rest                                             -> `baseline` (curve only)
  * room above `comfort_max`                             -> baseline regardless of price
  * room below `comfort_min`                             -> boost regardless of price
  * negative or near-zero prices                         -> boost (free heat)
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt


@dataclass
class PlannerConfig:
    baseline: float = 20.0       # °C: hands control back to the heating curve
    boost: float = 32.0          # °C: min flow in cheap slots (kept well under the 45 cap)
    cheap_fraction: float = 0.33 # share of upcoming slots treated as cheap
    horizon_hours: int = 24
    comfort_min: float = 20.5    # °C room mean
    comfort_max: float = 22.5
    free_price: float = 0.01     # £/kWh at or below which a slot is always boosted


def normalise_rates(rates_attr) -> list[tuple[dt.datetime, float]]:
    """Accept the Octopus Energy integration's `rates` attribute (list of dicts with
    start/end/value_inc_vat) from one or more event entities; return sorted (start, £/kWh)."""
    out = {}
    for r in rates_attr or []:
        start = r.get("start"); price = r.get("value_inc_vat")
        if start is None or price is None:
            continue
        if isinstance(start, str):
            start = dt.datetime.fromisoformat(start)
        out[start] = float(price)
    return sorted(out.items())


def plan(rates: list[tuple[dt.datetime, float]], now: dt.datetime, room_temp: float | None,
         cfg: PlannerConfig) -> dict:
    """Return {'setpoint_now', 'reason', 'slots': [...], 'threshold'}."""
    horizon_end = now + dt.timedelta(hours=cfg.horizon_hours)
    upcoming = [(s, p) for s, p in rates if s + dt.timedelta(minutes=30) > now and s < horizon_end]
    if not upcoming:
        return {"setpoint_now": cfg.baseline, "reason": "no rates available", "slots": [],
                "threshold": None, "urgent": False}

    # Exactly k cheapest slots (ties broken by time, earliest first) so a flat price
    # band cannot flood the plan with 'cheap' slots.
    k = max(1, int(round(len(upcoming) * cfg.cheap_fraction)))
    order = sorted(range(len(upcoming)), key=lambda i: (upcoming[i][1], upcoming[i][0]))
    cheap_idx = set(order[:k])
    threshold = upcoming[order[k - 1]][1]

    slots = []
    for i, (s, p) in enumerate(upcoming):
        cheap = i in cheap_idx or p <= cfg.free_price
        level = cfg.boost if cheap else cfg.baseline
        slots.append({"start": s.isoformat(), "price": p, "cheap": cheap, "min_flow": level})

    current = next((sl for sl in slots if dt.datetime.fromisoformat(sl["start"]) <= now), slots[0])
    setpoint, reason = current["min_flow"], ("cheap slot" if current["cheap"] else "not a cheap slot")

    # `urgent` marks a comfort override. The caller holds a new setpoint for a minimum
    # dwell so the plan cannot chatter a register on a lossy link, but comfort must not
    # wait out a dwell timer, so it is allowed to jump the queue.
    urgent = False
    if room_temp is not None:
        if room_temp >= cfg.comfort_max and setpoint > cfg.baseline:
            setpoint, reason = cfg.baseline, f"room {room_temp:.1f} at/above comfort max {cfg.comfort_max}"
            urgent = True
        elif room_temp <= cfg.comfort_min and setpoint < cfg.boost:
            setpoint, reason = cfg.boost, f"room {room_temp:.1f} at/below comfort min {cfg.comfort_min}"
            urgent = True

    return {"setpoint_now": setpoint, "reason": reason, "slots": slots, "threshold": threshold,
            "urgent": urgent,
            "cheap_slots": sum(1 for sl in slots if sl["cheap"]), "horizon_slots": len(slots)}
