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

Asking ebusd has no such ambiguity, because ebusd knows when it last actually had the
value off the wire. A `read -m SECONDS` returns its cache only if it is younger than
that and otherwise goes to the bus, so either way the answer carries a known maximum age.
This module drives one such read per device per cycle and turns the results into a
debounced alive/dead verdict that automations can trigger on instead of guessing from
staleness.

**It puts nothing on the bus at all.** `find -V` queries ebusd's own state and reports
`lastup` — when ebusd last had that value off the wire. Point it at a register ebusd
already polls (`hmu Status01` refreshes every ~10 s, `ctlv2 Currenterror` every ~15 s)
and the age of `lastup` *is* the liveness signal, for free and for ever. A device that
has dropped off stops advancing it: on 14 Sep 2026 the 177 adapter fell off WiFi at 08:37
and its `lastup` froze at 08:30:07, still frozen 28 minutes later, while 179's was 3
minutes old.

That matters more than traffic share suggests. A forced read is a real eBUS transaction —
210-390 ms against 2.5 ms from cache — and these adapters tunnel a *real-time* protocol
over 2.4 GHz WiFi, which is why ebusd runs `--latency=100` here. Arbitration is timing
sensitive, so on a marginal link the right amount of extra traffic to add for monitoring
is none. Reads are also staggered rather than fired back to back, and backed off once a
device is already known down: a failing link is the last one to hammer.

Debouncing matters because 177's adapter drops out roughly thirty times a day, mostly for
half a minute. A single failed read is a WiFi blip; `dead_after` consecutive failures
across the reconcile cycle is a device that has genuinely gone.

Pure: no HA, no sockets.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from ebusd import MAX_CLOCK_SKEW


@dataclass(frozen=True)
class ProbeTarget:
    """One register whose freshness stands in for one device being on the bus.

    Pick one ebusd polls frequently by itself — otherwise nothing keeps `lastup` moving
    and a healthy device looks dead. `find -V` on a live bus shows which: anything
    reporting an age of seconds is actively polled.
    """
    name: str          # e.g. "179_hmu"
    host: str
    port: int
    circuit: str       # e.g. "hmu"
    register: str      # e.g. "Status01"


@dataclass(frozen=True)
class ProbeState:
    fails: int = 0                        # consecutive failed probes
    failing_since: dt.datetime | None = None
    last_ok: dt.datetime | None = None
    last_value: float | None = None
    last_error: str | None = None
    last_age: float | None = None         # seconds since ebusd last had it off the wire

    def dead(self, dead_after: int) -> bool:
        return self.fails >= dead_after


def evaluate(reading, now: dt.datetime, stale_after: float,
             max_skew: float = MAX_CLOCK_SKEW) -> tuple[bool, str | None]:
    """Is this device still being read successfully? (answering, why_not).

    Note what is NOT a failure: a message with named fields rather than one number (an
    error register, say) has no `value`, and that is fine — `lastup` is the signal.

    A clock running ahead IS a failure, and deliberately so. `lastup` is stamped in
    ebusd's own local time, so if its clock drifts ahead of ours every age goes negative
    and every device looks permanently fresh — the alerting would go quiet and stay quiet.
    Refusing to judge is the safe reading: it surfaces as a fault instead of as silence.
    """
    if reading is None:
        return False, "not probed"
    if reading.error:
        return False, reading.error
    age = reading.age(now)
    if age is None:
        return False, "ebusd reported no lastup"
    if age < -max_skew:
        return False, (f"ebusd's clock is {-age / 60:.0f} min ahead of ours; "
                       "freshness cannot be judged")
    if age > stale_after:
        return False, f"ebusd last had a value {age / 60:.0f} min ago"
    return True, None


def update(state: ProbeState, reading, now: dt.datetime, stale_after: float,
           max_skew: float = MAX_CLOCK_SKEW) -> ProbeState:
    """Fold one probe result into a device's state."""
    answering, why = evaluate(reading, now, stale_after, max_skew)
    age = reading.age(now) if reading is not None else None
    if answering:
        return ProbeState(fails=0, failing_since=None, last_ok=now,
                          last_value=reading.value, last_error=None, last_age=age)
    return replace(
        state,
        fails=state.fails + 1,
        failing_since=state.failing_since or now,
        last_error=why,
        last_age=age,
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
        attrs[f"age_seconds_{name}"] = None if s.last_age is None else round(s.last_age)
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
