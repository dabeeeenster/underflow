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


# What the HMU's run-data status code says the pump is doing. The code is polled every ~10 s;
# the power readings lag it by minutes, so the code decides the words and the power figures
# are only quoted while the compressor runs. (Until 0.5.2 the power decided, and a compressor
# just started read "not running (heat_compressor_active)": 177, 13 Oct 2026.)
_PHASES = (("compressor_active", "{active}"), ("prerun", "starting a {job} run"),
           ("overrun", "finishing a {job} run (pump overrun)"),
           ("compressor_blocked", "pausing between {job} runs (compressor blocked after a run)"),
           ("compressor_shutdown", "between {job} runs (compressor off)"))


def _pump_line(obs: dict) -> str:
    status = (obs.get("pump_status") or "unknown").strip().lower()
    pin, pout, cop, flow = obs.get("power_in_kw"), obs.get("power_out_kw"), obs.get("cop_now"), obs.get("flow_temp")
    if status == "standby":
        return "The heat pump is in standby."
    job, active = (("hot water", "heating hot water") if "hwc" in status or "dhw" in status else
                   ("cooling", "cooling the house") if "cool" in status else
                   ("heating", "heating the house"))
    phase = next((t for key, t in _PHASES if key in status), None)
    if phase is None:
        if pin and pout and pin > 0.2:
            return f"The heat pump is running ({status}): drawing {pin:.1f} kW and delivering {pout:.1f} kW."
        return f"The heat pump is not running ({status})."
    doing = phase.format(job=job, active=active)
    if "compressor_active" not in status:
        return f"The heat pump is {doing}."
    flow_txt = f", flow at {flow:.0f} °C" if flow is not None else ""
    if pin and pout and pin > 0.2:
        cop_txt = f" for a COP of {cop:.1f}" if cop else ""
        return f"The heat pump is {doing}: drawing {pin:.1f} kW and delivering {pout:.1f} kW{cop_txt}{flow_txt}."
    return f"The heat pump is {doing}{flow_txt}; its power readings haven't caught up yet."


def describe(obs: dict, plan: dict, now: dt.datetime, cfg, dry_run: bool, stage: str = "1",
             test: dict | None = None, room_label: str = "the house",
             notes: list[str] | None = None, gate: dict | None = None,
             model: dict | None = None) -> list[str]:
    """`test`: {"reason": ..., "ends": datetime} while a step test is in charge (it replaces
    the plan line). `room_label` names what `room_temp_mean` actually is. `notes` are the
    pump's own standing lines from config (what it is in the middle of), added before the
    stage line. `gate`: {"open", "reason", "dry_run", "enabled"} when the pump switches its
    room thermostats (gate.py). `model`: {"used", "heat_off", "setpoint", "reason"} when the
    model planner is live (0.5.0); while it is in charge it replaces the plan line."""
    lines = []

    # --- house and weather
    out = obs.get("outside_temp", obs.get("outside_temp_met_office")); room = obs.get("room_temp_mean")
    if out is not None and room is not None:
        band = f"{cfg.comfort_min:g}–{cfg.comfort_max:g} °C"
        where = ("above" if room > cfg.comfort_max else "below" if room < cfg.comfort_min else "inside")
        lines.append(f"It is {out:.1f} °C outside and {room_label} is at {room:.1f} °C, {where} the comfort band of {band}.")

    # --- heat pump
    lines.append(_pump_line(obs))

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
        held = getattr(cfg, "no_rates_flow", None)
        held_txt = f"holds a neutral {held:g} °C minimum flow" if held is not None else "falls back to the heating curve"
        lines.append(f"No Agile prices are available (Octopus unreachable?), so the plan {held_txt}; the comfort band still applies.")

    # --- the plan
    sp = plan.get("setpoint_now"); reason = plan.get("reason", "")
    if test:
        ends = test.get("ends")
        until = f" until {_fmt_t(ends, now)}" if ends else ""
        lines.append(f"Step test in charge{until}, price ignored: {test.get('reason')}.")
    elif model and model.get("used"):
        verb = "would" if dry_run else "will"
        if model.get("heat_off"):
            lines.append(f"The model planner has the heating off: zone and room thermostats off, nothing circulating ({model.get('reason')}).")
        else:
            msp = model.get("setpoint")
            action = ("hold the minimum flow" if msp is None else
                      f"leave the minimum flow at {msp:g} °C so the heating curve decides" if msp <= cfg.baseline
                      else f"set the minimum flow to {msp:g} °C")
            lines.append(f"The model planner {verb} {action} ({model.get('reason')}).")
    elif sp is not None:
        if sp <= cfg.baseline:
            action = f"leave the minimum flow temperature at {sp:g} °C so the heating curve alone decides"
        else:
            action = f"raise the minimum flow temperature to {sp:g} °C to push heat into the slab"
        verb = "would" if dry_run else "will"
        lines.append(f"The plan {verb} {action} ({reason}).")
    if gate and gate.get("open") is not None:
        state = "on" if gate["open"] else "off"
        if not gate.get("enabled", True):
            lines.append(f"Room thermostats: switching is disabled from the dashboard; underflow would have them {state} ({gate.get('reason')}).")
        elif gate.get("dry_run"):
            lines.append(f"Room thermostats: underflow would have them {state} ({gate.get('reason')}), but in dry run it leaves them alone.")
        else:
            why = "" if gate["open"] else ", so nothing calls for heat and the circulation pumps rest"
            lines.append(f"Room thermostats are {state}{why} ({gate.get('reason')}).")
    if dry_run:
        curve = obs.get("heat_curve")
        curve_txt = f"the {curve:g} heating curve" if curve is not None else "the heating curve"
        lines.append(f"Dry run: nothing is being written to the heat pump. The room thermostats and {curve_txt} are in charge.")
    else:
        lines.append("Live: the controller writes the minimum flow temperature through the bounded script with read-back.")
    lines.extend(n for n in (notes or []) if n)
    if test:
        lines.append("Stage 2: fixed blocks of heat and no heat, to fit the house model (lag, slab, sun). The planner resumes when the test ends.")
    elif model:
        fallback = "" if model.get("used") else " It has no plan right now, so the rule-based price planner is standing in."
        lines.append("Stage 2: the model planner is in charge. It simulates the slab and rooms against the weather, sun and price "
                     "forecasts and buys the cheapest heat that keeps the coldest room above the floor." + fallback)
    else:
        lines.append(f"Stage {stage}: rule-based Agile shifting. The house and COP models are not yet fitted; the 5-minute log is collecting the data.")
    return lines


def summary(lines: list[str], limit: int = 250) -> str:
    """A one-line state for the sensor (HA states are capped at 255 chars)."""
    s = " ".join(lines[:2])
    return s if len(s) <= limit else s[: limit - 1] + "…"
