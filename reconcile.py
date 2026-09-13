"""underflow.reconcile — decide whether to write a register, given what the bus says.

Pure: no HA, no sockets, no clock of its own. The caller supplies the desired value, a
fresh `Reading`, the link state and the previous `RegisterState`; this returns a
`Decision` and the next state.

The shape of the problem is a converging reconciler, not a fire-and-forget write. The
177 adapter dropped its eBUS signal 30 times in the 48 h to 13 Sep 2026 — 52.5 minutes
offline, median 32 s, worst 18.1 minutes — so any single write has a real chance of
being lost, and the only way to know is to look again later. Hence: hold a desired
value, re-read every cycle, and write **only** when a fresh reading disagrees.

Four rules earn their place:

* **A read that failed is not a mismatch.** The bus not answering tells you nothing
  about the register, so it can never justify a write. Silence is held, not acted on.
* **Wait for the link to settle.** 177's dropouts come in clusters of 16-32 s flaps.
  Writing into a link that came back two seconds ago invites a half-applied value, so a
  write needs the signal to have been up for `settle_seconds`.
* **Back off, but never give up.** Retries slow from every cycle to every half hour and
  stay there. A genuinely dead bus must not generate 288 notifications a day, and a bus
  that recovers at 4 a.m. must be picked up without anyone intervening.
* **Alert on duration, not on failure.** One failed write is a WiFi blip and is normal.
  A mismatch that survives `alert_after_seconds` is a fault worth a human.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class ReconcileConfig:
    tolerance: float = 0.25          # °C; closer than this counts as applied
    settle_seconds: float = 60.0     # link must be up this long before we write
    alert_after_seconds: float = 1800.0
    # Seconds to wait before the nth retry, indexed by attempts already made; the last
    # entry repeats forever. With a 5 min cycle: write, +5 min, +5 min, +15, +30, +30...
    backoff_seconds: tuple[float, ...] = (0.0, 0.0, 0.0, 900.0, 1800.0)


@dataclass(frozen=True)
class RegisterState:
    attempts: int = 0
    last_attempt: dt.datetime | None = None
    last_confirmed: dt.datetime | None = None
    last_value: float | None = None
    mismatch_since: dt.datetime | None = None
    unreadable_since: dt.datetime | None = None
    alerted: bool = False

    def as_attributes(self) -> dict:
        """Flat dict for publishing onto the status sensor."""
        return {
            "attempts": self.attempts,
            "last_attempt": _iso(self.last_attempt),
            "last_confirmed": _iso(self.last_confirmed),
            "last_value": self.last_value,
            "mismatch_since": _iso(self.mismatch_since),
            "unreadable_since": _iso(self.unreadable_since),
        }


@dataclass(frozen=True)
class Decision:
    action: str              # "write" | "hold" | "confirmed" | "idle"
    reason: str
    alert: str | None = None

    @property
    def writes(self) -> bool:
        return self.action == "write"


def _iso(t: dt.datetime | None) -> str | None:
    return None if t is None else t.isoformat(timespec="seconds")


def _age(then: dt.datetime | None, now: dt.datetime) -> float:
    return 0.0 if then is None else (now - then).total_seconds()


def decide(
    desired: float | None,
    reading,                      # ebusd.Reading, or None if not attempted
    signal_on: bool,
    signal_stable_for: float,     # seconds the eBUS signal has been continuously up
    state: RegisterState,
    now: dt.datetime,
    cfg: ReconcileConfig = ReconcileConfig(),
) -> tuple[Decision, RegisterState]:
    if desired is None:
        return Decision("idle", "no target set yet"), state

    # --- the bus did not answer -------------------------------------------------
    # A failed read is not evidence about the register, so it can never cause a write.
    if reading is None or not reading.ok:
        why = (reading.error if reading is not None else "not read")
        since = state.unreadable_since or now
        nxt = dataclasses.replace(state, unreadable_since=since)
        if _age(since, now) >= cfg.alert_after_seconds and not state.alerted:
            return (
                Decision("hold", f"bus did not answer ({why})",
                         alert=f"no reading from the bus for {_age(since, now) / 60:.0f} min: {why}"),
                dataclasses.replace(nxt, alerted=True),
            )
        return Decision("hold", f"bus did not answer ({why})"), nxt

    nxt = dataclasses.replace(state, unreadable_since=None, last_value=reading.value)

    # --- applied ----------------------------------------------------------------
    if abs(reading.value - desired) <= cfg.tolerance:
        return (
            Decision("confirmed", f"bus reports {reading.value:g}, target {desired:g}"),
            dataclasses.replace(nxt, attempts=0, mismatch_since=None, alerted=False,
                                last_confirmed=now),
        )

    # --- mismatch ---------------------------------------------------------------
    since = state.mismatch_since or now
    nxt = dataclasses.replace(nxt, mismatch_since=since)
    stale = _age(since, now)
    alert = None
    if stale >= cfg.alert_after_seconds and not state.alerted:
        alert = (f"{desired:g} has not stuck for {stale / 60:.0f} min — the bus still "
                 f"reports {reading.value:g} after {state.attempts} attempts")
        nxt = dataclasses.replace(nxt, alerted=True)

    if not signal_on:
        return Decision("hold", "eBUS signal is down", alert=alert), nxt
    if signal_stable_for < cfg.settle_seconds:
        return (
            Decision("hold", f"link only back for {signal_stable_for:.0f}s, settling", alert=alert),
            nxt,
        )

    wait = cfg.backoff_seconds[min(state.attempts, len(cfg.backoff_seconds) - 1)]
    if state.last_attempt is not None and _age(state.last_attempt, now) < wait:
        left = wait - _age(state.last_attempt, now)
        return (
            Decision("hold", f"backing off after {state.attempts} attempts, {left / 60:.0f} min to go",
                     alert=alert),
            nxt,
        )

    return (
        Decision("write", f"bus reports {reading.value:g}, target {desired:g}"
                          f" (attempt {state.attempts + 1})", alert=alert),
        dataclasses.replace(nxt, attempts=state.attempts + 1, last_attempt=now),
    )
