"""underflow.shared — steer 179 by the same room sensor Havenwise steers 177 by.

The mirror (demand.py) copies Havenwise's *flow*, and on Havenwise's first real day
that meant copying 177's catch-up after a night without heat: 44-45 °C for two hours
while 179's rooms were already at 20-21 °C and rising. Havenwise's flow encodes how
cold 177 is, not how cold the building is.

This is the alternative: both pumps aim at one number on one sensor. Havenwise reads
a single Shelly at the head of the party-wall void for 177; if 179 works to bring that
same sensor to the same target, the two halves pull the same way at the one place
their air mixes, and cannot fight by construction. The Shelly mostly reflects 177
(overnight on 7-8 Oct 2026 it fell 1.8 K with 177's rooms, against 0.7 K on 179) and
reads about 2 K above the room mean in the heating season, so 179's own rooms set
the limits in both directions:

  too warm   179's room mean at or above `comfort_max` -> step down every tick,
             whatever the Shelly says (a sunny west afternoon)
  too cold   179's room mean at or below `comfort_min` -> at least `boost`,
             whatever the Shelly says

Between the limits it is a slow integrator: each decide tick moves the flow by
`gain` × (target − sensor), capped at `step` °C, with a deadband. The slab answers in
hours, so anything faster than about 1 °C per half hour just winds up.

Stage 1 runs this as a shadow: computed and published every decide tick for a
season-long comparison, never written. Pure: no HA, no sockets.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SharedConfig:
    target: float = 21.5      # °C on the shared sensor, i.e. in that sensor's own terms
    deadband: float = 0.2     # K of error ignored
    gain: float = 2.0         # °C of flow per K of error, per decide tick
    step: float = 1.0         # cap on any one tick's move, °C
    floor: float = 20.0       # below this the heating curve alone decides anyway
    ceiling: float = 45.0     # the screed cap
    start: float = 30.0       # first value when there is nothing to recover


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def shared_step(prev: float | None, sensor: float | None, room_179: float | None,
                cfg: SharedConfig, planner_cfg) -> tuple[float, str]:
    """One decide tick. Return (min flow, reason)."""
    cur = cfg.start if prev is None else prev

    if room_179 is not None and room_179 >= planner_cfg.comfort_max:
        v = _clamp(cur - cfg.step, cfg.floor, cfg.ceiling)
        return v, f"179 is at {room_179:.1f} °C, at or above {planner_cfg.comfort_max:g}: easing off"

    if sensor is None:
        v = _clamp(cur, cfg.floor, cfg.ceiling)
        reason = "no shared sensor reading; holding"
    else:
        err = cfg.target - sensor
        if abs(err) <= cfg.deadband:
            move, verb = 0.0, "on target"
        else:
            move = _clamp(cfg.gain * err, -cfg.step, cfg.step)
            verb = "below target, warming" if move > 0 else "above target, easing"
        v = _clamp(cur + move, cfg.floor, cfg.ceiling)
        reason = f"shared sensor {sensor:.1f} °C against {cfg.target:g}: {verb}"

    if room_179 is not None and room_179 <= planner_cfg.comfort_min and v < planner_cfg.boost:
        return planner_cfg.boost, f"{reason}; raised to {planner_cfg.boost:g} as 179 is at {room_179:.1f} °C"
    return v, reason
