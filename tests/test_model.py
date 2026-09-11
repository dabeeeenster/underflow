# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "scipy>=1.13"]
# ///
"""Synthetic recovery test: generate data from known parameters, add noise, fit, compare.
Run:  uv run tests/test_model.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import numpy as np
from model import HouseParams, HouseModel, fit_house, KalmanFilter, CopModel, fit_cop

rng = np.random.default_rng(1)
dt = 1 / 12  # 5-minute steps in hours
n = 12 * 24 * 14  # two weeks

true = HouseParams(c_slab=28.0, c_room=2.5, r_sr=4.0, r_ro=9.0, g_room=0.6, g_slab=0.3)
t = np.arange(n) * dt
t_out = 6 + 4 * np.sin(2 * np.pi * (t - 15) / 24) + rng.normal(0, 0.3, n)
solar = np.clip(3.0 * np.sin(np.pi * ((t % 24) - 7) / 10), 0, None) * (np.sin(2 * np.pi * t / 24) > -2)  # kW proxy
q_in = np.where(((t % 24) > 1) & ((t % 24) < 6) | ((t % 24) > 12) & ((t % 24) < 15), 5.5, 0.0)  # cheap-slot heating
ts, tr = HouseModel(true).simulate(22.5, 20.5, q_in, t_out, solar, dt)
t_room_meas = tr + rng.normal(0, 0.05, n + 1)

p, info = fit_house(t_room_meas, q_in, t_out, solar, dt, split_solar=True)
print("fit rmse %.3f K  slab0 %.2f  success=%s" % (info["rmse"], info["t_slab0"], info["success"]))
for k, tv in vars(true).items():
    fv = getattr(p, k)
    print(f"  {k:7s} true {tv:7.3f}  fit {fv:7.3f}  ({(fv - tv) / tv * 100:+.1f}%)")
assert info["rmse"] < 0.08, "fit did not converge to the noise floor"
assert abs(p.r_ro - true.r_ro) / true.r_ro < 0.15, "envelope resistance off by >15%"
assert abs(p.c_slab - true.c_slab) / true.c_slab < 0.25, "slab capacity off by >25%"

# 24 h forecast from a KF-estimated state, vs truth
kf = KalmanFilter(HouseModel(p), dt); kf.reset(t_room_meas[0])
for k in range(n // 2):
    kf.predict(q_in[k], t_out[k], solar[k]); kf.update(t_room_meas[k + 1])
x = kf.x.copy(); horizon = 12 * 24
ts_f, tr_f = HouseModel(p).simulate(x[0], x[1], q_in[n // 2:n // 2 + horizon], t_out[n // 2:n // 2 + horizon], solar[n // 2:n // 2 + horizon], dt)
err = tr_f - tr[n // 2:n // 2 + horizon + 1]
print("24 h open-loop forecast: rmse %.3f K, max abs %.3f K" % (np.sqrt(np.mean(err ** 2)), np.abs(err).max()))
assert np.sqrt(np.mean(err ** 2)) < 0.3

# COP: 179's hot-water run on 10 Sep 2026, 5-min means off the bus (kW, °C)
p_in = np.array([1.8, 1.8, 1.8, 1.96, 2.06, 2.1, 2.1, 2.16, 2.2, 2.3, 2.3])
p_out = np.array([5.4, 5.4, 5.4, 5.9, 6.0, 6.0, 6.0, 5.58, 5.4, 5.4, 5.4])
t_flow = np.array([50.9, 51.6, 51.6, 55.3, 57.7, 58.6, 58.6, 61.1, 63.7, 64.7, 64.7])
t_out = np.full_like(t_flow, 18.6)
cm, cinfo = fit_cop(p_in, p_out, t_flow, t_out)
print("COP fit on 179 DHW run: eta %.3f, mean COP %.2f, rmse %.2f (n=%d)" % (cinfo["eta"], cinfo["cop_mean"], cinfo["cop_rmse"], cinfo["n"]))
assert 0.25 < cm.eta < 0.45
print("predicted heating COP at 32 °C flow / 5 °C outside: %.2f" % cm.cop(32, 5))
print("OK")
