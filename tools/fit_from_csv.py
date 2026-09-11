# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "scipy>=1.13"]
# ///
"""Fit the house model to a CSV from tools/fetch_stats.py (hourly). Smoke test for the pipeline.

Usage: uv run tools/fit_from_csv.py data/spring_179.csv
Columns are matched by substring: room_* (averaged), outdoor, flow, heat_generated.
"""
import sys, pathlib, csv
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import numpy as np
from model import fit_house, HouseModel, KalmanFilter

path = sys.argv[1]
with open(path) as f:
    r = csv.reader(f); header = next(r); rows = [row for row in r]
cols = {h: i for i, h in enumerate(header)}
def col(sub):
    names = [h for h in header if sub in h]
    arr = np.full((len(rows), len(names)), np.nan)
    for j, h in enumerate(names):
        for i, row in enumerate(rows):
            v = row[cols[h]]; arr[i, j] = float(v) if v not in ("", "None") else np.nan
    return names, arr

_, room = col("tado_smart_thermostat"); room = np.nanmean(room, axis=1)
_, out177 = col("177_outdoor"); _, out179 = col("179_outdoor")
t_out = np.where(np.isnan(out177[:, 0]), out179[:, 0], out177[:, 0])
_, flow = col("flow_temperature"); flow = flow[:, 0]
_, heat = col("heat_generated"); heat = heat[:, 0]  # Wh change per hour (cloud, lumpy)
_, elec = col("consumed_electrical"); elec = elec[:, 0]

print(f"rows {len(rows)}; heat lumps: {np.sum(np.nan_to_num(heat) > 0)} non-zero hours, total {np.nansum(heat)/1000:.0f} kWh; "
      f"elec total {np.nansum(elec)/1000:.0f} kWh; flow stale-hours (unchanged): {np.sum(np.diff(flow) == 0)}/{len(flow)-1}")

# Contiguous stretch with everything present
ok = ~np.isnan(room) & ~np.isnan(t_out) & ~np.isnan(flow)
idx = np.flatnonzero(ok); i0, i1 = idx[0], idx[-1]
room, t_out, flow, heat = room[i0:i1+1], t_out[i0:i1+1], flow[i0:i1+1], np.nan_to_num(heat[i0:i1+1])
# fill small gaps
for a in (room, t_out, flow):
    m = np.isnan(a); a[m] = np.interp(np.flatnonzero(m), np.flatnonzero(~m), a[~m])

# Heat input proxy. The cloud energy counter arrives in weekly lumps, useless hourly, so use
# the emitter law with a fixed k on the (stale) flow reading: Q = k (flow - room) when the
# flow reading is above 30. This is the smoke-test compromise; the bus gives real kW.
k_emit = 0.35  # kW/K, ballpark for a UFH half-house
q_in = np.where(flow > 30, k_emit * np.maximum(flow - room, 0), 0.0)
print(f"proxy heat input: {q_in.sum():.0f} kWh over {len(q_in)} h (cloud counter says {np.nansum(heat)/1000:.0f} kWh)")

dt = 1.0
n = len(q_in) - 1
p, info = fit_house(room, q_in[:n], t_out[:n], np.zeros(n), dt, split_solar=False)
print(f"fit: rmse {info['rmse']:.3f} K over {n} h; success={info['success']}")
for k, v in info["params"].items():
    print(f"  {k:7s} {v:8.3f}")
tau_slab = p.c_slab * p.r_sr; tau_env = (p.c_slab + p.c_room) * p.r_ro
print(f"time constants: slab->room {tau_slab:.1f} h, house->outside {tau_env:.1f} h; "
      f"steady heat loss {1/p.r_ro:.2f} kW/K -> {(21-0)/p.r_ro:.1f} kW at 0 °C outside for 21 °C inside")

# 24 h open-loop forecast from a KF state at the midpoint
kf = KalmanFilter(HouseModel(p), dt); kf.reset(room[0], info["t_slab0"])
mid = n // 2
for k in range(mid):
    kf.predict(q_in[k], t_out[k], 0.0); kf.update(room[k + 1])
ts, tr = HouseModel(p).simulate(kf.x[0], kf.x[1], q_in[mid:mid+24], t_out[mid:mid+24], np.zeros(24), dt)
err = tr - room[mid:mid+25]
print(f"24 h forecast from hour {mid}: rmse {np.sqrt(np.mean(err**2)):.2f} K, max {np.abs(err).max():.2f} K")
