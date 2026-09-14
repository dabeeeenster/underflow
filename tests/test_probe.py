# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_probe.py

The bug this replaces: inferring "the device is gone" from how long a Home Assistant
entity has been unchanged. ebusd publishes only on change, so a heat pump sitting in
standby at a steady 23.75 °C looks exactly like one that has dropped off the bus.
"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ebusd import Reading
from probe import ProbeState, evaluate, summarise, update

tz = dt.timezone.utc
T0 = dt.datetime(2026, 9, 13, 15, 0, tzinfo=tz)
DEAD_AFTER = 3
STALE = 300.0


def ok(v, at, age=10):
    """ebusd answered and last had the value `age` seconds ago."""
    return Reading(v, None, at, at - dt.timedelta(seconds=age))


def err(at, e="ERR: element not found"): return Reading(None, e, at)


def frozen(v, at, age):
    """ebusd answered, but its value has not moved for `age` seconds — a dead device."""
    return Reading(v, None, at, at - dt.timedelta(seconds=age))


# --- a steady value is alive, however long it stays steady --------------------
# This is the false alarm, reproduced: the same reading over and over for hours.
s = ProbeState()
t = T0
for i in range(60):                       # five hours of a standby afternoon
    s = update(s, ok(23.75, t), t, STALE)
    assert not s.dead(DEAD_AFTER), i
    t += dt.timedelta(minutes=5)
assert s.fails == 0 and s.last_value == 23.75 and s.last_ok == t - dt.timedelta(minutes=5)

# --- a blip is not a death ---------------------------------------------------
s = update(s, err(t), t, STALE)
assert s.fails == 1 and not s.dead(DEAD_AFTER) and s.failing_since == t
s = update(s, ok(23.75, t + dt.timedelta(minutes=5)), t + dt.timedelta(minutes=5), STALE)
assert s.fails == 0 and s.failing_since is None, "one recovered read clears the run"

# 177 drops ~30 times a day, mostly for half a minute: alternating blips must never
# accumulate into a dead verdict.
s, t = ProbeState(), T0
for i in range(40):
    s = update(s, err(t) if i % 2 else ok(20.0, t), t, STALE)
    assert not s.dead(DEAD_AFTER), i
    t += dt.timedelta(minutes=5)

# --- a genuinely absent device does get called dead --------------------------
s, t = ProbeState(), T0
for i in range(1, 6):
    s = update(s, err(t), t, STALE)
    assert s.dead(DEAD_AFTER) == (i >= DEAD_AFTER), (i, s.fails)
    t += dt.timedelta(minutes=5)
assert s.failing_since == T0, "down-since must be the first failure, not the latest"

# --- a frozen lastup is the real-world dead signal -------------------------
# 14 Sep 2026: the 177 adapter fell off WiFi at 08:37 and ebusd's lastup for its
# registers stayed pinned at 08:30:07 while 179's kept advancing. ebusd answers the
# query fine — it is the age that gives the device away.
ok_, why = evaluate(ok(20.0, T0, age=14), T0, STALE)
assert ok_ and why is None
ok_, why = evaluate(frozen(20.0, T0, age=1687), T0, STALE)
assert not ok_ and "28 min ago" in why, why

s, t = ProbeState(), T0
for i in range(1, 5):
    s = update(s, frozen(20.0, t, age=600 + 300 * i), t, STALE)
    t += dt.timedelta(minutes=5)
assert s.dead(DEAD_AFTER) and s.last_age is not None and "min ago" in s.last_error

# A register with named fields and no single number is still a valid liveness probe:
# `ctlv2 Currenterror` reports error=-;error_1=-... and no value, but does have a lastup.
ok_, why = evaluate(Reading(None, None, T0, T0 - dt.timedelta(seconds=14)), T0, STALE)
assert ok_ and why is None, (ok_, why)

# ebusd answering but with no lastup at all is NOT proof of life.
ok_, why = evaluate(Reading(20.0, None, T0, None), T0, STALE)
assert not ok_ and "no lastup" in why, why

# --- a clock running ahead must not read as "permanently fresh" ---------------
# ebusd stamps lastup in its own local time. A few seconds of skew is routine and
# harmless; if its clock drifts ahead, every age goes negative and — without this
# guard — every device would look fresh for ever and the alerting would go silent.
ok_, why = evaluate(ok(20.0, T0, age=-8), T0, STALE)
assert ok_ and why is None, "seconds of skew is normal, not a fault"

ok_, why = evaluate(ok(20.0, T0, age=-3600), T0, STALE)
assert not ok_ and "clock" in why and "ahead" in why, why

# And the fault must survive the debounce into a dead verdict rather than silence.
s_, t_ = ProbeState(), T0
for _ in range(DEAD_AFTER):
    s_ = update(s_, ok(20.0, t_, age=-3600), t_, STALE)
    t_ += dt.timedelta(minutes=5)
assert s_.dead(DEAD_AFTER) and "clock" in s_.last_error, s_.last_error

# --- summarise ---------------------------------------------------------------
states = {
    "179_hmu": ProbeState(fails=0, last_ok=T0, last_value=23.75, last_age=9),
    "179_ctlv2": ProbeState(fails=0, last_ok=T0, last_value=20.0),
    "177_hmu": ProbeState(fails=4, failing_since=T0 - dt.timedelta(minutes=20),
                          last_error="ERR: read timed out"),
    "177_ctlv2": ProbeState(fails=1, failing_since=T0, last_value=20.0),
}
st, a = summarise(states, DEAD_AFTER, T0)
assert a["alive_179_hmu"] is True and a["value_179_hmu"] == 23.75 and a["age_seconds_179_hmu"] == 9
assert a["alive_177_hmu"] is False and a["down_minutes_177_hmu"] == 20
assert a["alive_177_ctlv2"] is True, "1 failure is a blip, not a death"
assert a["dead"] == "177_hmu" and a["dead_count"] == 1 and a["probe_count"] == 4
assert "177_hmu" in st and "3 of 4 alive" in st, st
assert len(st) <= 255

st, a = summarise({k: ProbeState(fails=0, last_ok=T0) for k in states}, DEAD_AFTER, T0)
assert st == "4 of 4 alive" and a["dead"] == "" and a["dead_count"] == 0

print("probe OK:", st, "| steady value alive, frozen lastup dead, blips debounced, zero bus cost")
