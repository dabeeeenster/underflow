"""underflow.shadow — the stage-2 model planner, run alongside the live controller.

Holds everything the model planner needs between ticks and never touches the bus:
  * a Kalman filter tracking the hidden slab temperature from the coldest-room reading
    and the heat actually going in (fed every reconcile tick from the data-log row);
  * the forecast cache (forecast.py), so a failed fetch falls back rather than blanks;
  * the fitted parameters (`model:` in apps.yaml). Without them it reports that it is
    waiting for a fit and plans nothing.

Its plan is published as its own sensor so it can be compared with what actually
happened before it is ever allowed to write.

Pure: numpy, no HA.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

import forecast as F
from model import HouseModel, HouseParams, KalmanFilter
from mpc import MpcConfig, OFF, plan as mpc_plan

KF_DT_H = 5 / 60          # reconcile cadence
MAX_GAP_S = 20 * 60       # longer than this between observations: restart the filter


@dataclass(frozen=True)
class ShadowConfig:
    params: HouseParams
    mpc: MpcConfig
    horizon_hours: float = 24.0
    top: float = 23.5               # warmest room ceiling for planning
    allow_off: bool = True
    provisional: bool = True        # parameters from a short/early fit

    @classmethod
    def from_args(cls, a: dict | None):
        a = dict(a or {})
        p = a.get("params")
        if not p:
            return None
        params = HouseParams(**{k: float(v) for k, v in p.items() if k in HouseParams.__dataclass_fields__})
        m = dict(a.get("mpc", {}) or {})
        if "levels" in m:
            m["levels"] = tuple(OFF if str(l).lower() == OFF else float(l) for l in m["levels"])
        mpc = MpcConfig(**{k: v for k, v in m.items() if k in MpcConfig.__dataclass_fields__})
        return cls(params=params, mpc=mpc,
                   horizon_hours=float(a.get("horizon_hours", 24)),
                   top=float(a.get("top", 23.5)), allow_off=bool(a.get("allow_off", True)),
                   provisional=bool(a.get("provisional", True)))


class Shadow:
    def __init__(self, cfg: ShadowConfig, max_forecast_age_h: float = 6.0):
        self.cfg = cfg
        self.kf = KalmanFilter(HouseModel(cfg.params), KF_DT_H)
        self.last_obs: dt.datetime | None = None
        self.cache = F.ForecastCache(max_age_h=max_forecast_age_h)

    def observe(self, now, room, heat_kw, t_out, solar_kw):
        """One reconcile tick of measurements. Missing values are skipped safely."""
        if room is None:
            return
        gap = None if self.last_obs is None else (now - self.last_obs).total_seconds()
        if self.kf.x is None or gap is None or gap > MAX_GAP_S:
            self.kf.reset(room)
        else:
            self.kf.predict(heat_kw or 0.0, t_out if t_out is not None else room, solar_kw or 0.0)
            self.kf.update(room)
        self.last_obs = now

    @property
    def slab(self):
        return None if self.kf.x is None else float(self.kf.x[0])

    @property
    def room(self):
        return None if self.kf.x is None else float(self.kf.x[1])

    def plan(self, now, rates, floor, outside_now, comfort_top=None):
        """rates: [(start, £/kWh)]. Returns a dict for publishing.

        `comfort_top` is the coldest-room comfort top (the slider). The warmest room is
        planned to stay under it plus the room spread, and under `top`, whichever is
        lower: the pump cuts any boost once the coldest room reaches the comfort top, so
        heat planned above it would never be delivered (0.5.1)."""
        if self.kf.x is None:
            return {"state": "waiting", "reason": "no room reading yet"}
        slots = F.slot_starts(now, self.cfg.horizon_hours)
        t_out, solar, notes = self.cache.inputs(slots, now, outside_now)
        price_at = {s: p for s, p in rates}
        prices = [price_at.get(s) for s in slots]
        if all(p is None for p in prices):
            notes.append("no Agile prices: planning for comfort only")
        top = self.cfg.top if comfort_top is None else min(self.cfg.top, comfort_top + self.cfg.mpc.spread)
        r = mpc_plan(self.cfg.params, self.cfg.mpc, self.slab, self.room, t_out, solar, prices,
                     floor=floor, top=top, allow_off=self.cfg.allow_off)
        lvl = r.level_now
        h = lambda k: float(np.round(r.coldest[min(k, len(r.coldest) - 1)], 2))
        return {
            "state": "off" if lvl == OFF else f"{lvl:g}",
            "level_now": lvl,
            "predicted_coldest_1h": h(2), "predicted_coldest_3h": h(6), "predicted_coldest_6h": h(12),
            "predicted_min": float(np.round(r.coldest[1:].min(), 2)),
            "slab_estimate": round(self.slab, 2), "room_estimate": round(self.room, 2),
            "elec_kwh_24h": round(r.elec_kwh, 2), "cost_24h_gbp": round(r.cost, 2),
            "breaches": len(r.breaches), "over_top": len(r.over), "top": round(top, 2),
            "notes": notes, "provisional": self.cfg.provisional,
            "slots": [{"start": s.isoformat(timespec="minutes"),
                       "level": ("off" if l == OFF else l),
                       "price": prices[i], "t_out": round(t_out[i], 1), "sun_kw": round(solar[i], 2),
                       "coldest": round(float(r.coldest[i + 1]), 2)}
                      for i, (s, l) in enumerate(zip(slots, r.levels))],
        }
