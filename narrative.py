"""underflow.narrative — plain-English description of what the controller sees and plans.

Pure function: takes the observation dict and the planner result, returns markdown bullets.
Shown on the dashboard the way Predbat's "plan textual description" is.
"""
from __future__ import annotations

import datetime as dt


def _p(price):  # £/kWh -> "12.3p"
    return f"{price * 100:.1f}p"


def _windows(slots, now):
    """Group consecutive cheap slots into (start, end, min_price, max_price) windows."""
    out = []
    cur = None
    for sl in slots:
        s = dt.datetime.fromisoformat(sl["start"])
        e = s + dt.timedelta(minutes=30)
        if not sl["cheap"]:
            if cur:
                out.append(cur); cur = None
            continue
        if cur and cur["end"] == s:
            cur["end"] = e
            cur["lo"] = min(cur["lo"], sl["price"]); cur["hi"] = max(cur["hi"], sl["price"])
        else:
            if cur:
                out.append(cur)
            cur = {"start": s, "end": e, "lo": sl["price"], "hi": sl["price"]}
    if cur:
        out.append(cur)
    return out


def _fmt_t(t: dt.datetime, now: dt.datetime):
    local = t.astimezone(now.tzinfo) if now.tzinfo else t
    day = "" if local.date() == now.date() else ("tomorrow " if local.date() == now.date() + dt.timedelta(days=1) else local.strftime("%a "))
    return f"{day}{local:%H:%M}"


def _dur(a: dt.datetime, b: dt.datetime):
    m = int((b - a).total_seconds() // 60)
    if m < 60:
        return f"{m} minutes"
    h, r = divmod(m, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {r} min" if r else "")


def describe(obs: dict, plan: dict, now: dt.datetime, cfg, dry_run: bool, stage: str = "1") -> list[str]:
    lines = []

    # --- house and weather
    out = obs.get("outside_temp_met_office"); room = obs.get("room_temp_mean")
    if out is not None and room is not None:
        band = f"{cfg.comfort_min:g}–{cfg.comfort_max:g} °C"
        where = ("above" if room > cfg.comfort_max else "below" if room < cfg.comfort_min else "inside")
        lines.append(f"It is {out:.1f} °C outside and the house is at {room:.1f} °C, {where} the comfort band of {band}.")

    # --- heat pump
    status = obs.get("pump_status") or "unknown"
    pin, pout, cop, flow = obs.get("power_in_kw"), obs.get("power_out_kw"), obs.get("cop_now"), obs.get("flow_temp")
    if pin and pout and pin > 0.2:
        what = "heating hot water" if "hwc" in status else "heating the house" if "hc" in status or "heat" in status else f"running ({status})"
        cop_txt = f" for a COP of {cop:.1f}" if cop else ""
        lines.append(f"The heat pump is {what}: drawing {pin:.1f} kW and delivering {pout:.1f} kW{cop_txt}, flow at {flow:.0f} °C." if flow is not None
                     else f"The heat pump is {what}: drawing {pin:.1f} kW and delivering {pout:.1f} kW{cop_txt}.")
    else:
        lines.append("The heat pump is in standby." if status == "standby" else f"The heat pump is not running ({status}).")

    # --- prices
    rate = obs.get("agile_rate_now"); slots = plan.get("slots") or []
    if rate is not None and slots:
        prices = [s["price"] for s in slots]
        lo, hi = min(prices), max(prices)
        thr = plan.get("threshold")
        rank = "cheap" if thr is not None and rate <= thr else "expensive" if rate >= lo + 0.75 * (hi - lo) else "mid-priced"
        lines.append(f"Electricity is {_p(rate)} now, which is {rank} for the next {plan.get('horizon_slots', 0) // 2} hours (range {_p(lo)} to {_p(hi)}).")
        wins = _windows(slots, now)
        upcoming = [w for w in wins if w["end"] > now]
        if upcoming:
            parts = []
            for w in upcoming[:4]:
                pr = _p(w["lo"]) if abs(w["hi"] - w["lo"]) < 0.005 else f"{_p(w['lo'])}–{_p(w['hi'])}"
                if w["start"] <= now:
                    parts.append(f"now until {_fmt_t(w['end'], now)} ({pr})")
                else:
                    parts.append(f"{_fmt_t(w['start'], now)} for {_dur(w['start'], w['end'])} ({pr})")
            lines.append("Cheap windows: " + "; ".join(parts) + ".")
    elif not slots:
        lines.append("No Agile rates are available yet, so the plan falls back to the heating curve.")

    # --- the plan
    sp = plan.get("setpoint_now"); reason = plan.get("reason", "")
    if sp is not None:
        if sp <= cfg.baseline:
            action = f"leave the minimum flow temperature at {sp:g} °C so the heating curve alone decides"
        else:
            action = f"raise the minimum flow temperature to {sp:g} °C to push heat into the slab"
        verb = "would" if dry_run else "will"
        lines.append(f"The plan {verb} {action} ({reason}).")
    if dry_run:
        curve = obs.get("heat_curve")
        curve_txt = f"the {curve:g} heating curve" if curve is not None else "the heating curve"
        lines.append(f"Dry run: nothing is being written to the heat pump. The room thermostats and {curve_txt} are in charge.")
    else:
        lines.append("Live: the controller writes the minimum flow temperature through the bounded script with read-back.")
    lines.append(f"Stage {stage}: rule-based Agile shifting. The house and COP models are not yet fitted; that needs a month of heating data.")
    return lines


def summary(lines: list[str], limit: int = 250) -> str:
    """A one-line state for the sensor (HA states are capped at 255 chars)."""
    s = " ".join(lines[:2])
    return s if len(s) <= limit else s[: limit - 1] + "…"
