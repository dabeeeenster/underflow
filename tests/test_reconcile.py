# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_reconcile.py

The failure this guards against is not "the write did not work" — it is writing on
evidence we do not have. 177 dropped its eBUS signal 30 times in 48 hours, so a read
failure is routine, and the old code treated an unreadable register as a mismatch and
wrote into a dead bus on every cycle.
"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ebusd import Reading
from reconcile import ReconcileConfig, RegisterState, decide

tz = dt.timezone.utc
T0 = dt.datetime(2026, 11, 3, 2, 0, tzinfo=tz)
cfg = ReconcileConfig()
UP = dict(signal_on=True, signal_stable_for=600.0)


def ok(v, at=T0):    return Reading(v, None, at)
def err(e="ERR: read timed out", at=T0): return Reading(None, e, at)


# --- no target yet -----------------------------------------------------------
d, s = decide(None, ok(20), state=RegisterState(), now=T0, cfg=cfg, **UP)
assert d.action == "idle" and not d.writes, d

# --- already applied ---------------------------------------------------------
d, s = decide(32.0, ok(32.0), state=RegisterState(attempts=3, mismatch_since=T0), now=T0, cfg=cfg, **UP)
assert d.action == "confirmed" and s.attempts == 0 and s.mismatch_since is None, (d, s)
assert s.last_confirmed == T0 and s.last_value == 32.0
d, _ = decide(32.0, ok(32.2), state=RegisterState(), now=T0, cfg=cfg, **UP)
assert d.action == "confirmed", "within tolerance must count as applied"

# --- THE REGRESSION: an unreadable bus must never produce a write ------------
st = RegisterState()
for i in range(12):                       # an hour of 5-minute cycles, bus dead
    d, st = decide(32.0, err(), state=st, now=T0 + dt.timedelta(minutes=5 * i), cfg=cfg, **UP)
    assert not d.writes, (i, d)
assert st.attempts == 0, "a failed read is not an attempt"
assert st.unreadable_since == T0
d, st = decide(32.0, None, state=st, now=T0, cfg=cfg, **UP)
assert not d.writes and "did not answer" in d.reason, d
# ...and it alerts once it has gone on long enough, exactly once.
st2 = RegisterState(unreadable_since=T0)
d1, st2 = decide(32.0, err(), state=st2, now=T0 + dt.timedelta(minutes=35), cfg=cfg, **UP)
d2, st2 = decide(32.0, err(), state=st2, now=T0 + dt.timedelta(minutes=40), cfg=cfg, **UP)
assert d1.alert and not d2.alert, "alert once, not every cycle"

# --- a recovered read clears the unreadable clock ----------------------------
d, st3 = decide(32.0, ok(32.0), state=RegisterState(unreadable_since=T0), now=T0, cfg=cfg, **UP)
assert st3.unreadable_since is None and d.action == "confirmed"

# --- mismatch, but the link is down or has only just come back ---------------
d, _ = decide(32.0, ok(20.0), signal_on=False, signal_stable_for=0.0,
              state=RegisterState(), now=T0, cfg=cfg)
assert not d.writes and "signal is down" in d.reason, d
d, _ = decide(32.0, ok(20.0), signal_on=True, signal_stable_for=20.0,
              state=RegisterState(), now=T0, cfg=cfg)
assert not d.writes and "settling" in d.reason, d
# 177's dropouts cluster as 16-32 s flaps, so 20 s of uptime is not a stable link.
d, _ = decide(32.0, ok(20.0), signal_on=True, signal_stable_for=61.0,
              state=RegisterState(), now=T0, cfg=cfg)
assert d.writes, d

# --- mismatch on a healthy link: write, then back off, but never give up -----
st, writes, t = RegisterState(), 0, T0
for i in range(48):                        # four hours of 5-minute cycles, write lost
    d, st = decide(32.0, ok(20.0), state=st, now=t, cfg=cfg, **UP)
    writes += d.writes
    t += dt.timedelta(minutes=5)
assert 3 <= writes <= 12, f"backoff should throttle to a handful of writes, got {writes}"
assert st.attempts == writes and st.mismatch_since == T0
# It is still trying at the end of the four hours, not latched off.
d, _ = decide(32.0, ok(20.0), state=st, now=t + dt.timedelta(minutes=30), cfg=cfg, **UP)
assert d.writes, "a bus that recovers at 4am must be picked up with no intervention"

# --- alert on duration, not on a single failure ------------------------------
st, first_alert = RegisterState(), None
t = T0
for i in range(24):
    d, st = decide(32.0, ok(20.0), state=st, now=t, cfg=cfg, **UP)
    if d.alert and first_alert is None:
        first_alert = (t - T0).total_seconds() / 60
    t += dt.timedelta(minutes=5)
assert first_alert == 30, f"expected the alert at 30 min of sustained mismatch, got {first_alert}"

# A mismatch that clears before the alert window never alerts at all.
st = RegisterState()
t = T0
alerted = False
for i in range(5):
    d, st = decide(32.0, ok(20.0), state=st, now=t, cfg=cfg, **UP)
    alerted |= bool(d.alert)
    t += dt.timedelta(minutes=5)
d, st = decide(32.0, ok(32.0), state=st, now=t, cfg=cfg, **UP)
assert not alerted and d.action == "confirmed" and st.attempts == 0

print(f"reconcile OK: dead bus never writes; {writes} writes in 4 h of failure; alert at 30 min")
