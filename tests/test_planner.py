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

# --- offline: no rates at all (Octopus unreachable, cache run out) --------------
import datetime as _dt
_now = _dt.datetime(2026, 10, 12, 3, 0, tzinfo=_dt.timezone.utc)
off = PlannerConfig(comfort_min=20.0, comfort_max=22.5, no_rates_flow=26.0)
r = plan([], _now, 21.0, off)
assert r["setpoint_now"] == 26.0 and not r["urgent"] and r["reason"] == "no rates available", r
r = plan([], _now, 19.6, off)                       # cold house: comfort floor still boosts
assert r["setpoint_now"] == off.boost and r["urgent"], r
r = plan([], _now, 22.8, off)                       # warm house: comfort top still eases off
assert r["setpoint_now"] == off.baseline and r["urgent"], r
r = plan([], _now, None, PlannerConfig())           # no no_rates_flow: baseline, as before
assert r["setpoint_now"] == PlannerConfig().baseline, r
print("planner offline OK: neutral flow held, comfort band still applies without prices")
