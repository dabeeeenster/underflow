"""underflow.probe — is each device actually still on the bus?

**The question this answers, and why `last_updated` cannot.** ebusd publishes to MQTT
only when a decoded value changes, so a device sitting at a steady temperature produces
no messages at all and its Home Assistant entities freeze. "No update for 45 minutes"
therefore means *either* nothing happened *or* the device has dropped out of ebusd's
scan, and nothing on the HA side separates the two.

That ambiguity is not theoretical. On 13 Sep 2026 the 179 heat-pump stale alert fired at
15:54 — flow temp "frozen 51 min" — while the pump was in standby on a warm afternoon and
the flow had simply drifted to 23.75 °C and stopped moving. A forced read showed the HMU
answering normally the whole time. It had false-alarmed four times in three days, and it
gets worse in shoulder season when the pump barely runs.

A forced read has no such ambiguity: `read -f` goes to the wire, so the device either
answers or it does not. This module drives a handful of those per cycle — one register
per device — and turns the results into a debounced alive/dead verdict that automations
can trigger on instead of guessing from staleness.

Debouncing matters because 177's adapter drops out roughly thirty times a day, mostly for
half a minute. A single failed read is a WiFi blip; `dead_after` consecutive failures
across the reconcile cycle is a device that has genuinely gone.

Pure: no HA, no sockets.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ProbeTarget:
    """One register whose readability stands in for one device being on the bus."""
    name: str          # e.g. "179_hmu"
    host: str
    port: int
    circuit: str       # e.g. "hmu"
    register: str      # e.g. "FlowTemp"


@dataclass(frozen=True)
class ProbeState:
    fails: int = 0                        # consecutive failed reads
    failing_since: dt.datetime | None = None
    last_ok: dt.datetime | None = None
    last_value: float | None = None
    last_error: str | None = None

    def dead(self, dead_after: int) -> bool:
        return self.fails >= dead_after


def update(state: ProbeState, reading, now: dt.datetime) -> ProbeState:
    """Fold one reading into a probe's state."""
    if reading is not None and reading.ok:
        return ProbeState(fails=0, failing_since=None, last_ok=now,
                          last_value=reading.value, last_error=None)
    return replace(
        state,
        fails=state.fails + 1,
        failing_since=state.failing_since or now,
        last_error=(reading.error if reading is not None else "not read"),
    )


def summarise(states: dict[str, ProbeState], dead_after: int, now: dt.datetime) -> tuple[str, dict]:
    """(state string, attributes) for the published sensor.

    Every probe contributes `alive_<name>`, `fails_<name>` and `value_<name>` so an
    automation can trigger on one device without parsing anything. `dead` and
    `dead_count` summarise across all of them.
    """
    attrs: dict = {}
    dead: list[str] = []
    for name in sorted(states):
        s = states[name]
        is_dead = s.dead(dead_after)
        if is_dead:
            dead.append(name)
        attrs[f"alive_{name}"] = not is_dead
        attrs[f"fails_{name}"] = s.fails
        attrs[f"value_{name}"] = s.last_value
        attrs[f"error_{name}"] = s.last_error
        attrs[f"last_ok_{name}"] = s.last_ok.isoformat(timespec="seconds") if s.last_ok else None
        attrs[f"down_minutes_{name}"] = (
            round((now - s.failing_since).total_seconds() / 60) if s.failing_since else None
        )
    attrs["dead"] = ",".join(dead)
    attrs["dead_count"] = len(dead)
    attrs["probe_count"] = len(states)
    attrs["dead_after"] = dead_after
    attrs["checked"] = now.isoformat(timespec="seconds")
    alive = len(states) - len(dead)
    state = f"{alive} of {len(states)} alive" if states else "no probes configured"
    if dead:
        state += " — " + ", ".join(dead) + " not answering"
    return state[:255], attrs
