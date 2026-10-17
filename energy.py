"""underflow.energy — the energy-signature planner (0.7.0).

A different kind of model from mpc.py. That one simulates the slab and the rooms half-hour
by half-hour, and has to know how much heat the slab holds, which a big slab under a
wooden floor hides for hours. This one only answers "how much heat does this half of the
house need over the next day", from a straight line fitted to a winter of daily data:

    heat (kW) = heat_loss × (balance_point − outside)     when outside < balance_point

For 177, fitted 17 Oct 2026 on 26 weeks of myVAILLANT daily heat (Nov 2025 – May 2026)
against Open-Meteo outside temperatures: 0.37 kW/K below 13.3 °C, R² 0.81. Over a day the
slab's storage mostly cancels out, and the slab is what lets the heat be bought whenever
it is cheapest.

The pump's own curve delivers some heat in every slot (`baseline_kw`). The rest is bought
as upgrades of single slots: curve → `levels[0]`, then → `levels[1]`, and so on, each
priced at the slot's Agile rate × the extra electricity (heat ÷ COP) it takes. The cheapest
upgrades, per kWh of heat, are taken until the day's need is met. A higher level in a slot
always costs more per kWh than the level below it (lower COP), so the greedy order never
takes a step before the one under it; that is checked, not assumed.

Pure: no HA, no sockets.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from forecast import _temp_at

SLOT = dt.timedelta(minutes=30)


@dataclass(frozen=True)
class EnergyConfig:
    heat_loss: float                    # kW per K below the balance point
    balance_point: float                # °C outside at which no heating is needed
    baseline_kw: float                  # heat the curve alone delivers per hour (min flow at baseline)
    baseline_cop: float
    levels: tuple = ()                  # ((min flow °C, heat kW, COP), ...) ascending
    max_price: float | None = None      # £/kWh: never buy heat in a slot above this
    free_price: float = 0.01            # at or below: take the top level whatever the need
    horizon_hours: int = 24
    live: bool = False
    weather: str | None = None          # HA weather entity for the hourly temperature forecast

    @classmethod
    def from_args(cls, a: dict) -> "EnergyConfig":
        levels = []
        for flow, v in sorted((a.get("levels") or {}).items(), key=lambda kv: float(kv[0])):
            levels.append((float(flow), float(v["kw"]), float(v["cop"])))
        if not levels:
            raise ValueError("energy.levels: at least one {min flow: {kw, cop}} is needed")
        cfg = cls(heat_loss=float(a["heat_loss"]), balance_point=float(a["balance_point"]),
                  baseline_kw=float(a.get("baseline_kw", 0.0)), baseline_cop=float(a.get("baseline_cop", 3.5)),
                  levels=tuple(levels),
                  max_price=None if a.get("max_price") is None else float(a["max_price"]),
                  free_price=float(a.get("free_price", 0.01)),
                  horizon_hours=int(a.get("horizon_hours", 24)), live=bool(a.get("live", False)),
                  weather=a.get("weather"))
        steps = [(cfg.baseline_kw, cfg.baseline_cop)] + [(kw, cop) for _, kw, cop in levels]
        for (k0, c0), (k1, c1) in zip(steps, steps[1:]):
            if k1 <= k0:
                raise ValueError("energy.levels: heat must rise with each level")
        per_kwh = [(k1 / c1 - k0 / c0) / (k1 - k0) for (k0, c0), (k1, c1) in zip(steps, steps[1:])]
        if any(b < a for a, b in zip(per_kwh, per_kwh[1:])):
            raise ValueError("energy.levels: each level must cost more per kWh of heat than the one below")
        return cfg

    def need_kw(self, outside: float) -> float:
        return max(0.0, self.heat_loss * (self.balance_point - outside))


@dataclass
class EnergyPlan:
    level_now: float | None
    need_kwh: float
    baseline_kwh: float
    planned_kwh: float
    shortfall_kwh: float
    elec_kwh: float
    cost_gbp: float
    outside_mean: float | None
    hours: float
    slots: list = field(default_factory=list)   # [{"start", "price", "min_flow"}]
    notes: list = field(default_factory=list)

    def summary(self) -> dict:
        boosted = [s for s in self.slots if s["min_flow"] is not None]
        return {
            "level_now": self.level_now, "need_kwh": round(self.need_kwh, 1),
            "curve_kwh": round(self.baseline_kwh, 1), "planned_kwh": round(self.planned_kwh, 1),
            "shortfall_kwh": round(self.shortfall_kwh, 1), "elec_kwh": round(self.elec_kwh, 1),
            "cost_gbp": round(self.cost_gbp, 2),
            "outside_mean": None if self.outside_mean is None else round(self.outside_mean, 1),
            "horizon_hours": round(self.hours, 1), "boosted_slots": len(boosted),
            "next_boost": boosted[0]["start"] if boosted else None,   # slots start at the current one
            "plan": [f"{dt.datetime.fromisoformat(s['start']):%H:%M} {s['min_flow']:g}" for s in boosted],
            "notes": self.notes,
        }


def plan_energy(rates: list[tuple[dt.datetime, float]], met: list[tuple[dt.datetime, float]] | None,
                now: dt.datetime, outside_now: float | None, cfg: EnergyConfig) -> EnergyPlan | None:
    """The cheapest way to deliver the next `horizon_hours` of heat, or None with no prices.

    The horizon is the slots Octopus has priced, capped at `horizon_hours`: before ~16:00
    that ends at 23:00, and the need is worked out for the same span, so a short horizon
    plans a smaller amount, not the whole day crammed into it."""
    end = now + dt.timedelta(hours=cfg.horizon_hours)
    slots = [(s, p) for s, p in rates if s + SLOT > now and s < end]
    if not slots:
        return None
    notes = []
    temps = []
    for s, _ in slots:
        t = _temp_at(met, s + SLOT / 2) if met else None
        temps.append(t)
    known = [t for t in temps if t is not None]
    if not known:
        if outside_now is None:
            return None
        notes.append(f"no temperature forecast: outside held at {outside_now:.1f} °C")
        temps = [outside_now] * len(slots)
    elif len(known) < len(temps):
        fill = known[-1]
        notes.append("forecast shorter than the horizon: last value held")
        temps = [t if t is not None else fill for t in temps]
    # The line was fitted to daily means, so the need is worked out from the horizon's
    # mean temperature, not slot by slot (which would add heat on days that cross the
    # balance point, which the fit already averages over).
    hours = len(slots) * 0.5
    mean_t = sum(temps) / len(temps)
    need = cfg.need_kw(mean_t) * hours
    base = cfg.baseline_kw * hours
    extra = need - base

    chosen = [0] * len(slots)       # index into levels: 0 = curve, i = levels[i - 1]
    steps = [(cfg.baseline_kw, cfg.baseline_cop)] + [(kw, cop) for _, kw, cop in cfg.levels]
    top = len(cfg.levels)
    for i, (_, p) in enumerate(slots):
        if p <= cfg.free_price:
            chosen[i] = top
    got = sum((steps[c][0] - cfg.baseline_kw) * 0.5 for c in chosen)

    upgrades = []
    for i, (_, p) in enumerate(slots):
        if chosen[i] or (cfg.max_price is not None and p > cfg.max_price):
            continue
        for j in range(1, top + 1):
            (k0, c0), (k1, c1) = steps[j - 1], steps[j]
            gain = (k1 - k0) * 0.5
            upgrades.append((p * (k1 / c1 - k0 / c0) / (k1 - k0), i, j, gain))
    upgrades.sort(key=lambda u: (u[0], slots[u[1]][0]))
    for _, i, j, gain in upgrades:
        if got >= extra - 1e-9:
            break
        if chosen[i] != j - 1:      # the level below must already be taken (checked, not assumed)
            continue
        chosen[i] = j
        got += gain

    shortfall = max(0.0, extra - got)
    if shortfall > 0.05:
        capped = "" if cfg.max_price is None else f" under the {cfg.max_price * 100:g}p cap"
        notes.append(f"{shortfall:.1f} kWh short: not enough slots{capped} at the top level")
    elec = cost = 0.0
    out = []
    for (s, p), c in zip(slots, chosen):
        kw, cop = steps[c]
        e = kw / cop * 0.5
        elec += e
        cost += e * p
        out.append({"start": s.isoformat(), "price": p, "min_flow": cfg.levels[c - 1][0] if c else None})
    planned = base + got
    now_slot = next((o for o in out if dt.datetime.fromisoformat(o["start"]) <= now), out[0])
    return EnergyPlan(level_now=now_slot["min_flow"], need_kwh=need, baseline_kwh=base, planned_kwh=planned,
                      shortfall_kwh=shortfall, elec_kwh=elec, cost_gbp=cost, outside_mean=mean_t,
                      hours=hours, slots=out, notes=notes)
