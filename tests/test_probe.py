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
from probe import ProbeState, summarise, update

tz = dt.timezone.utc
T0 = dt.datetime(2026, 9, 13, 15, 0, tzinfo=tz)
DEAD_AFTER = 3


def ok(v, at):  return Reading(v, None, at)
def err(at, e="ERR: element not found"): return Reading(None, e, at)


# --- a steady value is alive, however long it stays steady --------------------
# This is the false alarm, reproduced: the same reading over and over for hours.
s = ProbeState()
t = T0
for i in range(60):                       # five hours of a standby afternoon
    s = update(s, ok(23.75, t), t)
    assert not s.dead(DEAD_AFTER), i
    t += dt.timedelta(minutes=5)
assert s.fails == 0 and s.last_value == 23.75 and s.last_ok == t - dt.timedelta(minutes=5)

# --- a blip is not a death ---------------------------------------------------
s = update(s, err(t), t)
assert s.fails == 1 and not s.dead(DEAD_AFTER) and s.failing_since == t
s = update(s, ok(23.75, t), t + dt.timedelta(minutes=5))
assert s.fails == 0 and s.failing_since is None, "one recovered read clears the run"

# 177 drops ~30 times a day, mostly for half a minute: alternating blips must never
# accumulate into a dead verdict.
s, t = ProbeState(), T0
for i in range(40):
    s = update(s, err(t) if i % 2 else ok(20.0, t), t)
    assert not s.dead(DEAD_AFTER), i
    t += dt.timedelta(minutes=5)

# --- a genuinely absent device does get called dead --------------------------
s, t = ProbeState(), T0
for i in range(1, 6):
    s = update(s, err(t), t)
    assert s.dead(DEAD_AFTER) == (i >= DEAD_AFTER), (i, s.fails)
    t += dt.timedelta(minutes=5)
assert s.failing_since == T0, "down-since must be the first failure, not the latest"

# --- summarise ---------------------------------------------------------------
states = {
    "179_hmu": ProbeState(fails=0, last_ok=T0, last_value=23.75),
    "179_ctlv2": ProbeState(fails=0, last_ok=T0, last_value=20.0),
    "177_hmu": ProbeState(fails=4, failing_since=T0 - dt.timedelta(minutes=20),
                          last_error="ERR: read timed out"),
    "177_ctlv2": ProbeState(fails=1, failing_since=T0, last_value=20.0),
}
st, a = summarise(states, DEAD_AFTER, T0)
assert a["alive_179_hmu"] is True and a["value_179_hmu"] == 23.75
assert a["alive_177_hmu"] is False and a["down_minutes_177_hmu"] == 20
assert a["alive_177_ctlv2"] is True, "1 failure is a blip, not a death"
assert a["dead"] == "177_hmu" and a["dead_count"] == 1 and a["probe_count"] == 4
assert "177_hmu" in st and "3 of 4 alive" in st, st
assert len(st) <= 255

st, a = summarise({k: ProbeState(fails=0, last_ok=T0) for k in states}, DEAD_AFTER, T0)
assert st == "4 of 4 alive" and a["dead"] == "" and a["dead_count"] == 0

print("probe OK:", st, "| steady value stays alive; blips debounced; absent device found in", DEAD_AFTER, "cycles")
