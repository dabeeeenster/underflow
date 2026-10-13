# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_rooms.py

A comfort-room sensor dropping out (0.6.0). On 179 the Living Room is the coldest room
71% of the time and the next coldest reads ~0.5 K warmer, so ignoring a lost Living Tado
would make the house look warmer and the planner heat less. The tracker must estimate the
missing room from the others at its usual offset, follow them as they move, give up after
`estimate_hours`, have nothing to say when no room reports, and survive a restart.
"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import rooms as R

T0 = dt.datetime(2026, 10, 14, 6, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
cfg = R.RoomsConfig.from_args({"Living": "sensor.l", "Bedrooms": "sensor.b", "Upstairs": "sensor.u"})
assert [n for n, _ in cfg.rooms] == ["Living", "Bedrooms", "Upstairs"] and cfg.estimate_hours == 12
assert R.RoomsConfig.from_args(["sensor.tado_x"]).rooms == (("tado_x", "sensor.tado_x"),)
assert R.RoomsConfig.from_args([], {"estimate_hours": 6}).estimate_hours == 6.0

t = R.Rooms(cfg)
r = t.update(T0, {"Living": 20.5, "Bedrooms": 21.0, "Upstairs": 21.4})
assert (r.coldest, r.warmest) == (20.5, 21.4) and not r.estimated and not r.missing

# Living drops out: estimated at its offsets from the others (-0.5, -0.9), not ignored
r = t.update(T0 + dt.timedelta(minutes=5), {"Living": None, "Bedrooms": 21.0, "Upstairs": 21.4})
assert r.estimated == {"Living": 20.5} and r.coldest == 20.5 and r.missing == {"Living": 5}, r
# the house cools by 1 K: the estimate follows the others
r = t.update(T0 + dt.timedelta(hours=3), {"Living": None, "Bedrooms": 20.0, "Upstairs": 20.4})
assert r.estimated == {"Living": 19.5} and r.coldest == 19.5, r
# a second room drops too: estimated from the one left, each at its own offset
r = t.update(T0 + dt.timedelta(hours=4), {"Living": None, "Bedrooms": None, "Upstairs": 20.4})
# pairwise offsets: Living's estimate doesn't jump when the set it is measured against changes
assert r.values["Upstairs"] == 20.4 and r.estimated == {"Living": 19.5, "Bedrooms": 20.0}, r
# nothing reporting: no anchor, no reading
r = t.update(T0 + dt.timedelta(hours=5), {"Living": None, "Bedrooms": None, "Upstairs": None})
assert r.coldest is None and r.warmest is None and set(r.dropped) == {"Living", "Bedrooms", "Upstairs"}, r
# past estimate_hours (Living last seen at T0): left out, min over the rest
r = t.update(T0 + dt.timedelta(hours=12, minutes=1), {"Living": None, "Bedrooms": 21.0, "Upstairs": 21.4})
assert "Living" in r.dropped and "Living" not in r.values and r.coldest == 21.0, r
# back: measured again, offset re-learnt
r = t.update(T0 + dt.timedelta(hours=13), {"Living": 20.2, "Bedrooms": 21.0, "Upstairs": 21.4})
assert r.coldest == 20.2 and not r.missing and abs(t.seen["Living"].offsets["Bedrooms"] - (20.2 - 21.0)) < 1e-9

# a room never seen is left out, not invented
t2 = R.Rooms(cfg)
r = t2.update(T0, {"Living": None, "Bedrooms": 21.0, "Upstairs": 21.4})
assert r.dropped == ["Living"] and r.missing == {"Living": None} and r.coldest == 21.0, r

# survives a restart
t3 = R.Rooms(cfg)
t3.load(t.dump())
r = t3.update(T0 + dt.timedelta(hours=13, minutes=5), {"Living": None, "Bedrooms": 21.0, "Upstairs": 21.4})
assert r.estimated == {"Living": 20.2}, r
t3.load({"Nope": {"value": 1, "at": T0.isoformat()}, "Living": {"bad": 1}})   # junk ignored
# --- a sensor that freezes instead of going unavailable ---------------------------------
t = R.Rooms(cfg)
step = dt.timedelta(minutes=5)
def run(t, start, hours, living, bed, up):
    """Feed `hours` of 5-min rows; each of living/bed/up is a value or a function of hours."""
    r = None
    for k in range(int(hours * 12) + 1):
        h = k / 12
        f = lambda x: x(h) if callable(x) else x
        r = t.update(start + k * step, {"Living": f(living), "Bedrooms": f(bed), "Upstairs": f(up)})
    return r
# all three flat overnight for 10 h (the slab holding steady, 13 Oct): not stuck
r = run(t, T0, 10, 21.0, lambda h: 21.5 + 0.02 * h, 22.0)
assert not r.stuck and r.coldest == 21.0, r
# Living frozen at 21.0 while the house warms 0.4 K/h: stuck once every other room has moved
# 1 K and 4 h have passed, then estimated from offsets taken before it froze (-0.7 / -1.0)
t = R.Rooms(cfg)
run(t, T0, 1, lambda h: 21.0 + 0.4 * h, lambda h: 21.7 + 0.4 * h, lambda h: 22.0 + 0.4 * h)
start = T0 + dt.timedelta(hours=1, minutes=5)
r = run(t, start, 3.9, 21.4, lambda h: 22.1 + 0.4 * h, lambda h: 22.4 + 0.4 * h)
assert not r.stuck, r                                   # under 4 h
r = run(t, start + dt.timedelta(hours=4), 0.5, 21.4, lambda h: 23.7 + 0.4 * h, lambda h: 24.0 + 0.4 * h)
assert "Living" in r.stuck and r.stuck["Living"] >= 4.0, r
assert abs(r.estimated["Living"] - 23.2) < 1e-9 and r.coldest == r.estimated["Living"], r
assert r.missing["Living"] >= 240                       # missing since it froze
# its first change clears it
r = t.update(start + dt.timedelta(hours=5), {"Living": 23.2, "Bedrooms": 23.9, "Upstairs": 24.2})
assert not r.stuck and r.coldest == 23.2, r
# one other room flat too (sun on one side, 13 Oct: Lotte was genuinely flat): not stuck
t = R.Rooms(cfg)
r = run(t, T0, 6, 21.0, lambda h: 21.7 + 0.4 * h, 22.0)
assert not r.stuck, r
# survives a restart mid-freeze
t = R.Rooms(cfg)
run(t, T0, 1, lambda h: 21.0 + 0.4 * h, lambda h: 21.7 + 0.4 * h, lambda h: 22.0 + 0.4 * h)
run(t, start, 3, 21.4, lambda h: 22.1 + 0.4 * h, lambda h: 22.4 + 0.4 * h)
t2 = R.Rooms(cfg); t2.load(t.dump())
r = run(t2, start + dt.timedelta(hours=3, minutes=5), 1.5, 21.4, lambda h: 23.4 + 0.4 * h, lambda h: 23.7 + 0.4 * h)
assert "Living" in r.stuck, r

print("rooms OK: frozen sensor caught only when every other room moves, estimated from before it froze, cleared on change;", end=" ")
print("rooms OK: missing room estimated at its offset, follows the others, dropped after 12 h, "
      "nothing when nothing reports, survives a restart")
