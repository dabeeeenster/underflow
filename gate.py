"""underflow.gate — switch the room thermostats on and off (0.4.0).

The minimum flow temperature decides how hot the water is, not whether it moves. On 179
the Tados switch the heat pump's demand *and* the underfloor manifold pump in the plant
room: on 13 Oct 2026, with zone 1 off and the heat pump in standby, the cupboard pump
still ran, and it stopped within seconds of the three Tados being switched off. Opened
to their maximums (or 25 °C for the step test) they call all day, so that pump ran
around the clock in mild weather, circulating with nothing to deliver.

The gate closes the thermostats when underflow has no reason to heat and opens them when
it does. Their setpoints are left alone: open, each Tado still caps its own room.

  * step test running   -> follow it: open in a heat block, closed when the block is off
                           or paused at the ceiling
  * comfort floor       -> open
  * model planner off   -> closed (0.5.0: the live model planner has heat off this slot)
  * boosting            -> open (a cheap slot, or the comfort floor)
  * no room reading     -> open (fail towards heat, as the Tados alone would)
  * room at comfort_max -> closed, and stays closed until it is `hysteresis` under it
  * otherwise           -> open

A change waits out `min_dwell_minutes` unless the test or the comfort floor asks for it,
so the thermostats can't chatter. Pure: no HA.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GateConfig:
    climates: tuple = field(default_factory=tuple)   # climate.* entities to switch
    mode_on: str = "heat"
    mode_off: str = "off"
    hysteresis: float = 0.3        # K under comfort_max before a closed gate reopens
    min_dwell_minutes: float = 30.0
    # Setpoint to send when reopening a thermostat whose own setpoint isn't known. HomeKit
    # reports a Tado's setpoint as null while it is off, so the gate remembers each one
    # while it is on; this is only for a thermostat never seen on.
    default_setpoint: float | None = None
    enable_entity: str | None = None   # input_boolean: off = leave the thermostats alone
    dry_run: bool | None = None        # None = the pump's dry_run

    @classmethod
    def from_args(cls, args: dict | None) -> "GateConfig | None":
        if not args or not args.get("climates"):
            return None
        a = dict(args)
        a["climates"] = tuple(a["climates"])
        # YAML 1.1 reads a bare `off` as False.
        for k, default in (("mode_on", "heat"), ("mode_off", "off")):
            v = a.get(k)
            a[k] = default if v is None else ("off" if v is False else "on" if v is True else str(v))
        return cls(**{k: v for k, v in a.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class GateDecision:
    open: bool
    reason: str
    urgent: bool = False     # may skip the dwell


def wanted(cfg: GateConfig, prev_open: bool | None, room: float | None, setpoint: float | None,
           baseline: float, comfort_min: float, comfort_max: float, step=None,
           heat_off: bool = False) -> GateDecision:
    """Should the thermostats be calling? `prev_open` is the gate's current position
    (None before the first decision); `setpoint` the minimum flow underflow is asking for;
    `step` the step test's `Step` (testplan.py), or None; `heat_off` True when the live
    model planner wants no heat this slot."""
    if step is not None and getattr(step, "active", False):
        if step.zone == "off":
            return GateDecision(False, "the step test has heat off", True)
        return GateDecision(True, "the step test is heating", True)
    if room is not None and room <= comfort_min:
        return GateDecision(True, f"room {room:.1f} °C at/below the {comfort_min:g} °C comfort floor", True)
    if heat_off:
        return GateDecision(False, "the model planner has heat off")
    if setpoint is not None and setpoint > baseline:
        return GateDecision(True, f"boosting to {setpoint:g} °C")
    if room is None:
        return GateDecision(True, "no room reading, so the thermostats decide")
    if room >= comfort_max:
        return GateDecision(False, f"room {room:.1f} °C at/above the {comfort_max:g} °C comfort top")
    reopen = comfort_max - cfg.hysteresis
    if prev_open is False and room > reopen:
        return GateDecision(False, f"room {room:.1f} °C, reopens below {reopen:g} °C")
    return GateDecision(True, f"room {room:.1f} °C, under the {comfort_max:g} °C comfort top")


def may_change(cfg: GateConfig, decision: GateDecision, prev_open: bool | None,
               since: dt.datetime | None, now: dt.datetime, slack_s: float = 60.0) -> bool:
    """True if the gate may move to `decision.open` now."""
    if prev_open is None or decision.open == prev_open or decision.urgent or since is None:
        return True
    return (now - since).total_seconds() >= cfg.min_dwell_minutes * 60 - slack_s
