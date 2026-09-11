# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_planner.py"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner import PlannerConfig, plan, normalise_rates

tz = dt.timezone.utc
now = dt.datetime(2026, 10, 15, 17, 10, tzinfo=tz)
# A typical Agile day: cheap 01:00-06:00 and 12:00-15:00, peak 16:00-19:00
raw = []
for i in range(48):
    s = dt.datetime(2026, 10, 15, 0, 0, tzinfo=tz) + dt.timedelta(minutes=30 * i)
    h = s.hour
    price = 0.09 if 1 <= h < 6 else 0.12 if 12 <= h < 15 else 0.38 if 16 <= h < 19 else 0.22
    raw.append({"start": s.isoformat(), "end": (s + dt.timedelta(minutes=30)).isoformat(), "value_inc_vat": price})
rates = normalise_rates(raw)
cfg = PlannerConfig()

p = plan(rates, now, room_temp=21.5, cfg=cfg)
assert p["setpoint_now"] == cfg.baseline, p            # 17:10 is peak -> baseline
assert p["cheap_slots"] >= 4
p2 = plan(rates, dt.datetime(2026, 10, 15, 2, 0, tzinfo=tz), 21.5, cfg)
assert p2["setpoint_now"] == cfg.boost, p2             # 02:00 is cheap -> boost
p3 = plan(rates, dt.datetime(2026, 10, 15, 2, 0, tzinfo=tz), 23.0, cfg)
assert p3["setpoint_now"] == cfg.baseline and "comfort max" in p3["reason"], p3
p4 = plan(rates, now, 20.0, cfg)
assert p4["setpoint_now"] == cfg.boost and "comfort min" in p4["reason"], p4
assert plan([], now, 21.0, cfg)["setpoint_now"] == cfg.baseline
print("planner OK:", p["cheap_slots"], "cheap of", p["horizon_slots"], "slots; threshold £%.3f" % p["threshold"])
