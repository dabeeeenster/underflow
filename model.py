"""underflow.model — the physics the controller runs on.

Three small, physically grounded pieces, all numpy/scipy, no HA dependency, so they can
be fitted and tested offline:

* HouseModel      two-node RC circuit (slab + room air) with solar gain
* fit_house       least-squares fit of the five (or six) house parameters to logged data
* KalmanFilter    2-state linear KF that keeps the hidden slab temperature current
* CopModel        Carnot-fraction COP, one parameter, fitted from measured power
* flow_target     what the Vaillant controller will do with a curve + minimum flow

Units: temperatures in °C (COP uses kelvin internally), power in kW, energy in kWh,
time step dt in hours. Capacities therefore in kWh/K, resistances in K/kW.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import numpy as np
from scipy.optimize import least_squares


# --------------------------------------------------------------------------- house
@dataclass
class HouseParams:
    c_slab: float   # kWh/K   thermal mass of the slab (+ everything coupled to it)
    c_room: float   # kWh/K   air + light contents
    r_sr: float     # K/kW    slab -> room
    r_ro: float     # K/kW    room -> outside (envelope + ventilation)
    g_room: float   # K·kW/kW solar gain to room per kW of solar proxy (dimensionless kW/kW)
    g_slab: float = 0.0  # solar landing on the floor, per kW of proxy

    def as_vector(self, split_solar: bool) -> np.ndarray:
        v = [self.c_slab, self.c_room, self.r_sr, self.r_ro, self.g_room]
        if split_solar:
            v.append(self.g_slab)
        return np.array(v, dtype=float)

    @classmethod
    def from_vector(cls, v: np.ndarray, split_solar: bool) -> "HouseParams":
        return cls(*v[:5], g_slab=float(v[5]) if split_solar else 0.0)


class HouseModel:
    """Discrete-time two-node model.

        C_s dT_s/dt = Q_in + g_s·S − (T_s − T_r)/R_sr
        C_r dT_r/dt = (T_s − T_r)/R_sr − (T_r − T_out)/R_ro + g_r·S
    """

    def __init__(self, p: HouseParams):
        self.p = p

    def step(self, t_slab, t_room, q_in, t_out, solar, dt):
        p = self.p
        flow_sr = (t_slab - t_room) / p.r_sr
        flow_ro = (t_room - t_out) / p.r_ro
        t_slab_new = t_slab + dt * (q_in + p.g_slab * solar - flow_sr) / p.c_slab
        t_room_new = t_room + dt * (flow_sr - flow_ro + p.g_room * solar) / p.c_room
        return t_slab_new, t_room_new

    def simulate(self, t_slab0, t_room0, q_in, t_out, solar, dt):
        """Roll forward over arrays. Returns (t_slab[], t_room[]) of len(q_in)+1."""
        n = len(q_in)
        ts = np.empty(n + 1); tr = np.empty(n + 1)
        ts[0], tr[0] = t_slab0, t_room0
        for k in range(n):
            ts[k + 1], tr[k + 1] = self.step(ts[k], tr[k], q_in[k], t_out[k], solar[k], dt)
        return ts, tr

    def matrices(self, dt):
        """Linear state-space form x[k+1] = A x[k] + B u[k], x=[T_s,T_r], u=[Q,T_out,S]."""
        p = self.p
        a_ss = 1 - dt / (p.c_slab * p.r_sr)
        a_sr = dt / (p.c_slab * p.r_sr)
        a_rs = dt / (p.c_room * p.r_sr)
        a_rr = 1 - dt / (p.c_room * p.r_sr) - dt / (p.c_room * p.r_ro)
        A = np.array([[a_ss, a_sr], [a_rs, a_rr]])
        B = np.array([[dt / p.c_slab, 0.0, dt * p.g_slab / p.c_slab],
                      [0.0, dt / (p.c_room * p.r_ro), dt * p.g_room / p.c_room]])
        return A, B


def fit_house(t_room, q_in, t_out, solar, dt, *, split_solar=False,
              t_slab0=None, x0: HouseParams | None = None) -> tuple[HouseParams, dict]:
    """Fit HouseParams so the simulated room temperature tracks the measured one.

    t_room: measured room temperature, len n+1. q_in, t_out, solar: len n.
    The slab temperature is a hidden state; its initial value is fitted too unless given.
    Returns (params, info) where info has rmse, n, and the residual vector.
    """
    t_room = np.asarray(t_room, float); q_in = np.asarray(q_in, float)
    t_out = np.asarray(t_out, float); solar = np.asarray(solar, float)
    n = len(q_in)
    assert len(t_room) == n + 1, "t_room needs one more sample than the inputs"
    fit_slab0 = t_slab0 is None
    if x0 is None:
        # Sensible UFH starting point: 30 kWh/K slab, 3 kWh/K room, 5 K/kW slab->room, 8 K/kW envelope
        x0 = HouseParams(30.0, 3.0, 5.0, 8.0, 0.5, 0.5)
    v0 = list(x0.as_vector(split_solar))
    if fit_slab0:
        v0.append(t_room[0] + 1.0)
    lo = [1.0, 0.2, 0.2, 0.5, 0.0] + ([0.0] if split_solar else []) + ([t_room[0] - 10] if fit_slab0 else [])
    hi = [400.0, 50.0, 50.0, 100.0, 20.0] + ([20.0] if split_solar else []) + ([t_room[0] + 15] if fit_slab0 else [])

    def resid(v):
        p = HouseParams.from_vector(v, split_solar)
        s0 = v[-1] if fit_slab0 else t_slab0
        _, tr = HouseModel(p).simulate(s0, t_room[0], q_in, t_out, solar, dt)
        return tr - t_room

    res = least_squares(resid, v0, bounds=(lo, hi), x_scale="jac", max_nfev=2000)
    p = HouseParams.from_vector(res.x, split_solar)
    rmse = float(np.sqrt(np.mean(res.fun ** 2)))
    info = {"rmse": rmse, "n": n, "success": bool(res.success), "message": res.message,
            "t_slab0": float(res.x[-1]) if fit_slab0 else t_slab0, "params": asdict(p)}
    return p, info


# --------------------------------------------------------------------------- kalman
class KalmanFilter:
    """Linear KF on x=[T_slab, T_room]; only T_room is measured."""

    def __init__(self, model: HouseModel, dt, q_slab=0.02, q_room=0.05, r_meas=0.1):
        self.model, self.dt = model, dt
        self.A, self.B = model.matrices(dt)
        self.Q = np.diag([q_slab, q_room]) ** 2
        self.R = np.array([[r_meas ** 2]])
        self.H = np.array([[0.0, 1.0]])
        self.x = None; self.P = None

    def reset(self, t_room, t_slab=None):
        self.x = np.array([t_room if t_slab is None else t_slab, t_room], float)
        self.P = np.diag([4.0, 0.25])

    def predict(self, q_in, t_out, solar):
        u = np.array([q_in, t_out, solar])
        self.x = self.A @ self.x + self.B @ u
        self.P = self.A @ self.P @ self.A.T + self.Q
        return self.x.copy()

    def update(self, t_room_meas):
        y = np.array([t_room_meas]) - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x = self.x + (K @ y)
        self.P = (np.eye(2) - K @ self.H) @ self.P
        return self.x.copy()


# --------------------------------------------------------------------------- heat pump
@dataclass
class CopModel:
    """COP = eta · T_flow_K / (T_flow_K − T_out_K), floored so it never goes silly."""
    eta: float = 0.35
    cop_max: float = 7.0

    def cop(self, t_flow, t_out):
        t_flow = np.asarray(t_flow, float); t_out = np.asarray(t_out, float)
        lift = np.maximum(t_flow - t_out, 3.0)
        return np.minimum(self.eta * (t_flow + 273.15) / lift, self.cop_max)


def fit_cop(p_in, p_out, t_flow, t_out, min_p_in=0.5, mode=None,
            keep_modes=("heating",)) -> tuple[CopModel, dict]:
    """Fit eta from measured electrical (kW) and heat (kW) power.

    Samples below min_p_in kW are ignored. If per-sample `mode` labels are supplied
    (see Underflow._mode) only `keep_modes` are fitted: a domestic hot water charge
    runs the flow to 60-70 C against a 5 K lift, which is a different operating point
    from 30 C space heating and drags eta well away from the value the planner needs.
    """
    p_in = np.asarray(p_in, float); p_out = np.asarray(p_out, float)
    t_flow = np.asarray(t_flow, float); t_out = np.asarray(t_out, float)
    m = (p_in >= min_p_in) & (p_out > 0) & np.isfinite(t_flow) & np.isfinite(t_out)
    if mode is not None:
        keep = set(keep_modes)
        m &= np.array([str(x) in keep for x in mode], dtype=bool)
    if m.sum() < 3:
        return CopModel(), {"n": int(m.sum()), "note": "too few running samples; default eta kept"}
    cop = p_out[m] / p_in[m]
    carnot = (t_flow[m] + 273.15) / np.maximum(t_flow[m] - t_out[m], 3.0)
    # energy-weighted eta: sum(heat) / sum(elec*carnot) — robust to the quantised yield readings
    eta = float(p_out[m].sum() / (p_in[m] * carnot).sum())
    pred = eta * carnot
    return CopModel(eta), {"n": int(m.sum()), "eta": eta,
                           "cop_mean": float(cop.mean()), "cop_rmse": float(np.sqrt(np.mean((pred - cop) ** 2)))}


def flow_target(t_out, curve, room_setpoint=20.0, min_flow=20.0, max_flow=45.0):
    """Approximate Vaillant heating-curve flow target with the min/max clamps applied.

    Vaillant's curves are published as a family of lines through (T_out=20, flow=room
    setpoint); slope ≈ curve × 1.0 per K of (setpoint − T_out) for the low curves used on
    underfloor. Good to a degree or two in 0.4–0.8; refine against Hc1ActualFlowTempDesired.
    """
    t_out = np.asarray(t_out, float)
    raw = room_setpoint + curve * (room_setpoint - t_out) * 1.0 + 4.0 * curve  # +offset so 0.6 @ 0°C ≈ 34
    return np.clip(np.maximum(raw, min_flow), None, max_flow)


def heat_into_slab(t_flow, t_slab, k_emit):
    """kW delivered by the floor loops: k_emit (kW/K) × (flow − slab)."""
    return np.maximum(k_emit * (np.asarray(t_flow, float) - np.asarray(t_slab, float)), 0.0)
