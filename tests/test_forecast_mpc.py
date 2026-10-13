# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "scipy>=1.13"]
# ///
"""Run: uv run tests/test_forecast_mpc.py

Forecasts: parsed from the real response shapes, interpolated onto half-hour slots, and
when missing or stale the fallback errs towards more heat (flat temperature, no sun).
Planner: on a known synthetic house it keeps the room above the floor, buys the heat in
the cheap slots ahead of the need (the slab lag), heats less when sun is forecast, never
heats a warm house, and is deterministic.
"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import numpy as np
import forecast as F
from model import HouseParams
from mpc import MpcConfig, plan, OFF

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 12, 0, 0, tzinfo=UTC)

# --- forecast parsing ---------------------------------------------------------------
resp = {"weather.met_office_wood_green": {"forecast": [
    {"datetime": "2026-10-12T00:00:00+00:00", "temperature": 10.0},
    {"datetime": "2026-10-12T01:00:00+00:00", "temperature": 12.0}]}}
met = F.parse_met(F.find_forecast_list({"result": {"response": resp}}))   # nested wrapper
assert met == [(T0, 10.0), (T0 + dt.timedelta(hours=1), 12.0)], met
sol = F.parse_solcast([{"period_start": "2026-10-12T00:00:00+00:00", "pv_estimate": 0.5}],
                      [{"period_start": "2026-10-12T00:30:00+00:00", "pv_estimate": 1.5}])
assert sol == [(T0, 0.5), (T0 + dt.timedelta(minutes=30), 1.5)], sol

cache = F.ForecastCache(max_age_h=6)
cache.offer(T0, met=met, sol=sol)
slots = F.slot_starts(T0 + dt.timedelta(minutes=10), 1.5)
assert slots == [T0, T0 + dt.timedelta(minutes=30), T0 + dt.timedelta(hours=1)], slots
t_out, solar, notes = cache.inputs(slots, T0, outside_now=9.0)
assert abs(t_out[0] - 10.5) < 1e-9 and abs(t_out[1] - 11.5) < 1e-9, t_out   # mid-slot interpolation
assert t_out[2] == 11.5 and solar == [0.5, 1.5, 0.0], (t_out, solar)            # beyond: held, no sun
assert any("covers 2 of 3" in n for n in notes), notes

stale = T0 + dt.timedelta(hours=7)                                             # forecast too old
t_out, solar, notes = cache.inputs(F.slot_starts(stale, 1), stale, outside_now=8.0)
assert t_out == [8.0, 8.0] and solar == [0.0, 0.0], (t_out, solar)
assert any("no usable temperature" in n for n in notes) and any("assuming no sun" in n for n in notes)
t_out, _, _ = F.ForecastCache().inputs(F.slot_starts(T0, 0.5), T0, outside_now=None)
assert t_out == [10.0], t_out                                                  # nothing at all

# --- planner on a synthetic house -------------------------------------------------------
P = HouseParams(c_slab=25.0, c_room=4.0, r_sr=1.5, r_ro=10.0, g_room=0.6, g_slab=0.2)
cfg = MpcConfig(levels=(OFF, 20.0, 25.0, 32.0, 38.0), k_emit=0.6, q_max=5.0, cop_eta=0.4,
                curve=0.2, zone_setpoint=23.0, spread=1.0)
n = 48
cold_night = [8.0] * n
dark = [0.0] * n
cheap_night = [0.02 if k < 12 else 0.30 for k in range(n)]       # first 6 h cheap
r = plan(P, cfg, slab0=22.0, room0=21.0, t_out=cold_night, solar=dark, prices=cheap_night,
         floor=20.0, top=23.5)
assert not r.breaches, (r.breaches, r.coldest.min())
assert (r.coldest[1:] + cfg.spread).max() <= 23.5 + 1e-6
early = sum(r.heat_kw[:12]); late = sum(r.heat_kw[12:])
assert early > late, ("cheap first six hours should carry most of the heat (slab banking)", early, late)
assert r.coldest[1:].min() >= 20.0 + cfg.margin - 1e-6, "plans to the floor plus margin"

# Same night, flat prices: the cheap-night plan costs less than an all-at-38 plan
r_all = plan(P, cfg, 22.0, 21.0, cold_night, dark, cheap_night, floor=20.0, top=99)  # unconstrained top
assert r.cost <= r_all.cost + 1e-9

# Sun forecast reduces the heat bought
sunny = [2.0 if 16 <= k < 32 else 0.0 for k in range(n)]
r_sun = plan(P, cfg, 22.0, 21.0, cold_night, sunny, cheap_night, floor=20.0, top=23.5)
assert r_sun.elec_kwh < r.elec_kwh, (r_sun.elec_kwh, r.elec_kwh)

# Warm house, mild weather: nothing bought
r_warm = plan(P, cfg, 23.0, 22.0, [16.0] * n, dark, cheap_night, floor=19.0, top=23.5)
assert all(l == OFF for l in r_warm.levels), r_warm.levels

# A sunny afternoon over the top must not block the night's heat. 179 at 21:25 on 13 Oct
# 2026, fitted params, that evening's forecast: the old rule (no raise may lift the warmest
# room further over the top anywhere in 24 h) bought nothing and let the coldest room
# fall to 19.8 by 07:30, because any night heat also lifted the next afternoon's sun peak.
P179 = HouseParams(c_slab=10.72, c_room=0.4413, r_sr=2.005, r_ro=3.615, g_room=1.216, g_slab=5.474)
c179 = MpcConfig(levels=(OFF, 20.0, 25.0, 32.0, 38.0, 42.0), k_emit=0.326, q_max=7.01, cop_eta=0.383,
                 spread=1.02, curve=0.2, zone_setpoint=23)
t13 = [15.8, 15.5, 15.2, 14.9, 14.5, 14.2, 14.0, 13.7, 13.5, 13.4, 13.2, 12.9, 12.7, 12.5, 12.3, 12.2,
       12.0, 11.7, 11.5, 11.3, 11.2, 11.3, 11.5, 11.8, 12.2, 12.7, 13.4, 14.2, 15.0, 15.7, 16.4, 17.0,
       17.4, 17.8, 18.0, 18.2, 18.4, 18.4, 18.4, 18.1, 17.9, 17.5, 17.2, 16.9, 16.6, 16.3, 16.1, 15.8]
s13 = [0.0] * 21 + [0.02, 0.15, 0.32, 0.51, 0.69, 0.85, 0.98, 1.07, 1.14, 1.12, 1.02, 0.94, 0.86, 0.75,
                    0.65, 0.53, 0.43, 0.31, 0.18, 0.05, 0.01] + [0.0] * 6
p13 = [.31, .286, .271, .25, .307, .292, .286, .265, .261, .263, .267, .262, .253, .249, .267, .266,
       .322, .336, .37, .382, .392, .416, .389, .383, .339, .29, .245, .196, .168, .154, .146, .137,
       .145, .14, .157, .165, .189, .213, .412, .449, .473, .485, .48, .496, .366, .366, .353, .33]
r13 = plan(P179, c179, 26.58, 23.14, t13, s13, p13, floor=20.0, top=23.5)
assert not r13.breaches and r13.coldest[1:].min() >= 20.0, (r13.breaches, r13.coldest[1:].min())
assert any(l != OFF for l in r13.levels[:21]), ("night heat blocked by the afternoon sun", r13.levels)

# Deterministic
r2 = plan(P, cfg, 22.0, 21.0, cold_night, dark, cheap_night, floor=20.0, top=23.5)
assert r2.levels == r.levels and abs(r2.cost - r.cost) < 1e-12

# Unknown prices still produce a comfort-only plan
r_np = plan(P, cfg, 22.0, 21.0, cold_night, dark, [None] * n, floor=20.0, top=23.5)
assert not r_np.breaches

# Off not allowed: lowest level is the baseline flow
r_no_off = plan(P, cfg, 23.0, 22.0, [16.0] * n, dark, cheap_night, floor=19.0, top=23.5, allow_off=False)
assert OFF not in r_no_off.levels

print(f"forecast+mpc OK: cheap-night plan {r.elec_kwh:.1f} kWh £{r.cost:.2f} "
      f"(heat in cheap 6 h: {early*0.5:.1f} kWh, after: {late*0.5:.1f}); sunny day {r_sun.elec_kwh:.1f} kWh")

# --- shadow: config parsing, filter, plan output -------------------------------------
import shadow as SH
sc = SH.ShadowConfig.from_args({"provisional": True, "top": 23.5,
                                "params": {"c_slab": 15.0, "c_room": 1.08, "r_sr": 1.14, "r_ro": 3.63, "g_room": 1.75, "g_slab": 0.0},
                                "mpc": {"k_emit": 0.27, "q_max": 6.9, "cop_eta": 0.39, "spread": 1.0, "levels": ["off", 20, 25, 32, 38]}})
assert sc and sc.mpc.levels[0] == OFF and sc.mpc.levels[-1] == 38.0, sc
assert SH.ShadowConfig.from_args({"mpc": {}}) is None           # no params: no shadow
sh = SH.Shadow(sc)
assert sh.plan(T0, [], 20.0, 10.0)["state"] == "waiting"
for i in range(12):                                             # an hour of observations
    sh.observe(T0 + dt.timedelta(minutes=5 * i), 20.8 - 0.01 * i, 0.0, 10.0, 0.0)
assert sh.slab is not None and abs(sh.room - 20.7) < 0.2, (sh.slab, sh.room)
sh.cache.offer(T0, met=[(T0 + dt.timedelta(hours=h), 9.0) for h in range(30)], sol=[])
rates = [(T0 + dt.timedelta(minutes=30 * k), 0.02 if k < 12 else 0.3) for k in range(48)]
out = sh.plan(T0 + dt.timedelta(hours=1), rates, 20.0, 9.5)
assert out["state"] in ("off", "20", "25", "32", "38") and len(out["slots"]) == 48, out["state"]
assert out["predicted_coldest_3h"] is not None and any("sun" in n for n in out["notes"]), out["notes"]
# the comfort-top slider lowers the planning ceiling (top + spread), never raises it (0.5.1)
assert sh.plan(T0 + dt.timedelta(hours=1), rates, 20.0, 9.5, comfort_top=22.0)["top"] == 23.0
assert sh.plan(T0 + dt.timedelta(hours=1), rates, 20.0, 9.5, comfort_top=23.0)["top"] == 23.5
assert out["top"] == 23.5
sh.observe(T0 + dt.timedelta(hours=3), 20.5, 0.0, 10.0, 0.0)    # 2 h gap: filter restarts on the reading
assert abs(sh.room - 20.5) < 1e-9
print("shadow OK: plan", out["state"], "now, coldest in 3 h", out["predicted_coldest_3h"], "slab", out["slab_estimate"])
