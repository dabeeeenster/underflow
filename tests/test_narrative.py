# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_narrative.py"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner import PlannerConfig, plan, normalise_rates
from narrative import describe, summary

tz = dt.timezone(dt.timedelta(hours=1))
now = dt.datetime(2026, 10, 15, 17, 10, tzinfo=tz)
raw = []
for i in range(60):
    s = dt.datetime(2026, 10, 15, 0, 0, tzinfo=tz) + dt.timedelta(minutes=30 * i)
    h = s.hour
    price = 0.09 if 1 <= h < 6 else 0.12 if 12 <= h < 15 else 0.38 if 16 <= h < 19 else 0.22
    raw.append({"start": s.isoformat(), "end": (s + dt.timedelta(minutes=30)).isoformat(), "value_inc_vat": price})
cfg = PlannerConfig()
p = plan(normalise_rates(raw), now, 21.4, cfg)
obs = {"outside_temp_met_office": 8.3, "room_temp_mean": 21.4, "pump_status": "hc_compressor_active",
       "power_in_kw": 1.7, "power_out_kw": 5.8, "cop_now": 3.4, "flow_temp": 30, "agile_rate_now": 0.38}
lines = describe(obs, p, now, cfg, dry_run=True)
print("\n".join("- " + l for l in lines)); print("state:", summary(lines))
assert any("comfort band" in l for l in lines) and any("Cheap windows" in l for l in lines)
assert len(summary(lines)) <= 255
obs2 = {**obs, "power_in_kw": 0.0, "power_out_kw": 0.0, "cop_now": None, "pump_status": "standby"}
lines2 = describe(obs2, plan([], now, None, cfg), now, cfg, dry_run=False)
assert any("standby" in l for l in lines2) and any("No Agile rates" in l for l in lines2)
print("narrative OK")
