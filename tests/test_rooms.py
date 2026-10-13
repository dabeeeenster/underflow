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
print("rooms OK: missing room estimated at its offset, follows the others, dropped after 12 h, "
      "nothing when nothing reports, survives a restart")
