# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "scipy>=1.13"]
# ///
"""Fit 179's models from underflow's 5-minute data log and print a `model:` block for apps.yaml.

Usage:
  uv run tools/fit_log.py 179-2026-10-2.csv [more.csv ...] [--holdout-hours 6]

Fits, on the coldest-room series the controller actually keys on:
  * house  — slab + room RC model with solar gain (model.fit_house), heat input = the
             pump's heat output read off the bus while space heating (hot-water runs heat
             the cylinder, not the slab, so they count as zero); sun = PV kW (mean over
             each 5 min); outside = Met Office.
  * emitter — kW delivered per K of (flow − estimated slab), and the most it delivered.
  * COP    — Carnot fraction from heating rows only (model.fit_cop).
  * spread — mean warmest − coldest room.
The last `--holdout-hours` are left out of the fit and used to check the model forecasts
the coldest room open-loop, which is what the planner relies on.
"""
import argparse, csv, sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import numpy as np
from model import HouseModel, fit_house, fit_cop

DT_H = 5 / 60


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def load(paths):
    rows = []
    for p in paths:
        with open(p, newline="") as f:
            rows.extend(csv.DictReader(f))
    rows.sort(key=lambda r: r["ts"])
    # one row per 5-minute grid point, forward-filling gaps of up to 15 min
    out, last = [], None
    for r in rows:
        t = dt.datetime.fromisoformat(r["ts"])
        t = t.replace(second=0, microsecond=0, minute=t.minute - t.minute % 5)
        if last is not None:
            gap = int((t - last[0]).total_seconds() // 300)
            if gap <= 0:
                continue
            if 1 < gap <= 3:
                for i in range(1, gap):
                    out.append((last[0] + dt.timedelta(minutes=5 * i), last[1]))
            elif gap > 3:
                print(f"gap of {gap * 5} min before {t}; fitting across it", file=sys.stderr)
        out.append((t, r)); last = (t, r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--holdout-hours", type=float, default=6.0)
    a = ap.parse_args()
    data = load(a.csv)
    ts = [t for t, _ in data]
    g = lambda k: np.array([num(r.get(k)) for _, r in data])
    room, warm = g("room_temp_mean"), g("warmest")
    t_out = g("outside_temp_met_office")
    heat = np.where(np.array([r.get("mode") == "heating" for _, r in data]), g("heat_out_kw_bus"), 0.0)
    heat = np.nan_to_num(heat, nan=0.0)
    sun = np.nan_to_num(g("pv_w"), nan=0.0) / 1000.0
    # fill sensor gaps (Tado/HomeKit blips) by carrying the last value
    for arr in (room, t_out):
        for i in range(1, len(arr)):
            if np.isnan(arr[i]):
                arr[i] = arr[i - 1]
    ok = ~np.isnan(room)
    first = int(np.argmax(ok))
    room, t_out, heat, sun, warm, ts = room[first:], t_out[first:], heat[first:], sun[first:], warm[first:], ts[first:]
    n = len(room) - 1
    hold = int(a.holdout_hours * 12)
    nf = max(24, n - hold)
    print(f"{len(room)} rows {ts[0]} -> {ts[-1]}; fitting {nf} steps, holding out {n - nf}", file=sys.stderr)

    p, info = fit_house(room[:nf + 1], heat[:nf], t_out[:nf], sun[:nf], DT_H, split_solar=True)
    print(f"house fit rmse {info['rmse']:.3f} K, slab0 {info['t_slab0']:.2f}", file=sys.stderr)

    # simulate the whole series from the fitted start, for the emitter fit and the holdout check
    ts_sim, tr_sim = HouseModel(p).simulate(info["t_slab0"], room[0], heat[:n], t_out[:n], sun[:n], DT_H)
    hold_rmse = float(np.sqrt(np.nanmean((tr_sim[nf + 1:] - room[nf + 1:]) ** 2))) if n > nf else None

    flow = g("flow_hmu_bus")[first:]
    m = (heat[:-1] > 0.5) & np.isfinite(flow[:-1])
    d = flow[:-1][m] - ts_sim[:-1][m]
    k_emit = float(np.sum(heat[:-1][m] * d) / np.sum(d * d)) if m.sum() > 5 and np.sum(d * d) > 0 else None
    q_max = float(np.nanpercentile(heat[heat > 0.5], 95)) if (heat > 0.5).sum() > 5 else None

    modes = [r.get("mode") for _, r in data][first:]
    cop, cinfo = fit_cop(g("power_in_kw_bus")[first:], g("heat_out_kw_bus")[first:], flow, t_out, mode=modes)
    spread = float(np.nanmean(warm - room))

    tau_sr = p.c_slab * p.r_sr
    tau_ro = (p.c_slab + p.c_room) * p.r_ro
    print(f"time constants: slab->room ~{tau_sr:.1f} h, house->outside ~{tau_ro:.0f} h; "
          f"loss {1 / p.r_ro:.2f} kW/K; holdout rmse {hold_rmse if hold_rmse is None else round(hold_rmse, 3)} K; "
          f"k_emit {k_emit}; q_max {q_max}; COP eta {cinfo.get('eta')} (n={cinfo.get('n')}); spread {spread:.2f}",
          file=sys.stderr)

    print("  model:")
    print(f"    provisional: true   # fitted {dt.date.today()} on {ts[0]:%d %b %H:%M}-{ts[-1]:%d %b %H:%M}; "
          f"rmse {info['rmse']:.2f} K, holdout {hold_rmse if hold_rmse is None else round(hold_rmse, 2)} K")
    print("    params:")
    for k, v in vars(p).items():
        print(f"      {k}: {v:.4g}")
    print("    mpc:")
    if k_emit:
        print(f"      k_emit: {k_emit:.3g}")
    if q_max:
        print(f"      q_max: {q_max:.3g}")
    print(f"      cop_eta: {cop.eta:.3g}")
    print(f"      spread: {spread:.2f}")


if __name__ == "__main__":
    main()
