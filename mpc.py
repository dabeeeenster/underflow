"""underflow.mpc — plan the next day of heat with the house model (stage 2).

Given the slab/room state now, the outside-temperature and sun forecasts, and the Agile
prices, choose one level per half-hour slot — zone off, or a minimum flow temperature —
so that the coldest room stays at or above `floor` and the warmest at or below `top`,
for the least electricity cost.

Why a simulation and not a rule: 179's slab takes ~2 h to show heat in the rooms and
gives it back for hours after (step test, 10-11 Oct 2026). Only a model that carries
the slab state forward can see that heat bought at 02:00 for 2p is what keeps the rooms
up at 07:00, or that a sunny afternoon will do the job for free.

Method (greedy, deterministic): start every slot at the lowest level; simulate; at the
first slot where the coldest room would fall below the floor, raise the one earlier slot
that buys the most degrees at that moment per penny, re-simulate, repeat. A raise that
would push the warmest room over the top before the breach is not allowed. Overshoot
after it is not held against the raise: heat bought at 03:00 also lifts a sunny
afternoon that is over the top anyway, and counting that blocked every night-time raise
whenever sun was forecast (13 Oct 2026); the thermostat gate handles the afternoon. If
nothing earlier can fix a breach (the slab is too slow), the breach is recorded and
planning moves on.

Pure: numpy, no HA.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from model import CopModel, HouseModel, HouseParams, flow_target

OFF = "off"


@dataclass(frozen=True)
class MpcConfig:
    levels: tuple = (OFF, 20.0, 25.0, 32.0, 38.0)   # ascending heat; OFF only if allowed
    k_emit: float = 0.5        # kW per K of (flow - slab)
    q_max: float = 5.0         # kW the pump can put in at most
    cop_eta: float = 0.35      # Carnot fraction
    curve: float = 0.2         # the pump's own heat curve (flow floor = curve flow)
    zone_setpoint: float = 23.0
    spread: float = 1.0        # warmest room minus coldest, K (fitted from the log)
    lookback_slots: int = 96   # how far before a breach a raise may be placed (whole horizon:
                               # the slab banks cheap heat for many hours)
    margin: float = 0.3        # K above the floor to plan for, so the rooms don't ride it
    dt_h: float = 0.5


@dataclass
class MpcResult:
    levels: list
    coldest: np.ndarray           # predicted coldest room at each slot boundary (n+1)
    slab: np.ndarray
    heat_kw: np.ndarray
    elec_kwh: float
    cost: float
    breaches: list = field(default_factory=list)   # slot indices still below the floor
    over: list = field(default_factory=list)       # slot indices above the top

    @property
    def level_now(self):
        return self.levels[0] if self.levels else None


def _heat(level, t_out, t_slab, cfg: MpcConfig):
    """(kW into the slab, flow temperature) for one slot at `level`."""
    if level == OFF:
        return 0.0, None
    flow = float(flow_target(t_out, cfg.curve, cfg.zone_setpoint, min_flow=float(level)))
    return float(np.clip(cfg.k_emit * (flow - t_slab), 0.0, cfg.q_max)), flow


def simulate(params: HouseParams, cfg: MpcConfig, slab0, room0, levels, t_out, solar, prices):
    model, cop = HouseModel(params), CopModel(cfg.cop_eta)
    n = len(levels)
    ts, tr, q = np.empty(n + 1), np.empty(n + 1), np.zeros(n)
    ts[0], tr[0] = slab0, room0
    elec = cost = 0.0
    for k in range(n):
        q[k], flow = _heat(levels[k], t_out[k], ts[k], cfg)
        if q[k] > 0:
            e = q[k] / float(cop.cop(flow, t_out[k])) * cfg.dt_h
            elec += e
            cost += e * prices[k]
        ts[k + 1], tr[k + 1] = model.step(ts[k], tr[k], q[k], t_out[k], solar[k], cfg.dt_h)
    return ts, tr, q, elec, cost


def plan(params: HouseParams, cfg: MpcConfig, slab0: float, room0: float,
         t_out, solar, prices, floor: float, top: float, allow_off: bool = True) -> MpcResult:
    levels_all = [l for l in cfg.levels if allow_off or l != OFF]
    n = len(t_out)
    prices = [0.25 if p is None else float(p) for p in prices]   # unknown price: flat
    idx = [0] * n
    accepted = set()
    floor = floor + cfg.margin

    def run(ix):
        return simulate(params, cfg, slab0, room0, [levels_all[i] for i in ix], t_out, solar, prices)

    ts, tr, q, elec, cost = run(idx)
    # Budget: every slot can be raised to the top level, and every breach accepted once.
    for _ in range(n * len(levels_all) + n + 1):
        breach = next((k for k in range(1, n + 1) if tr[k] < floor and k not in accepted), None)
        if breach is None:
            break
        best = None
        for j in range(max(0, breach - cfg.lookback_slots), breach):
            # Every higher level, not just the next: when the pump's own curve already gives
            # more than a level's minimum flow, that level adds nothing, and stepping one
            # at a time would stall on it.
            for up in range(idx[j] + 1, len(levels_all)):
                trial = idx.copy(); trial[j] = up
                ts2, tr2, q2, e2, c2 = run(trial)
                gain = tr2[breach] - tr[breach]
                # Don't push the warmest room over the top, or further over it if sun already
                # has, up to the breach being fixed (tr[k] is the room after slot k-1).
                over2, over1 = tr2[1:breach + 1] + cfg.spread - top, tr[1:breach + 1] + cfg.spread - top
                if gain <= 1e-4 or np.any((over2 > 1e-9) & (over2 > over1 + 1e-9)):
                    continue
                score = max(c2 - cost, 1e-6) / gain
                if best is None or score < best[0]:
                    best = (score, trial, (ts2, tr2, q2, e2, c2))
        if best is None:
            accepted.add(breach)                     # can't be fixed from here; move on
            continue
        idx = best[1]
        ts, tr, q, elec, cost = best[2]
    levels = [levels_all[i] for i in idx]
    breaches = [k for k in range(1, n + 1) if tr[k] < floor - cfg.margin - 1e-9]
    over = [k for k in range(1, n + 1) if tr[k] + cfg.spread > top + 1e-9]
    return MpcResult(levels, tr, ts, q, elec, cost, breaches, over)
