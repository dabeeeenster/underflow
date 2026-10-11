"""underflow.forecast — outside temperature and sun for the planning horizon, with an
offline fallback.

Inputs per half-hour slot, the same units the house model is fitted in:
  * outside temperature (°C) from the Met Office hourly forecast (`weather.get_forecasts`)
  * solar (kW) from Solcast's half-hourly PV estimate. PV output is the proxy for sun on
    the south glazing; the model learns how much room heat each kW of PV means.

Forecasts come from the internet and will sometimes be missing, so every series falls
back, in order:
  1. the latest forecast, if it covers the slot;
  2. the last good forecast, while it is younger than `max_age_h` (default 6 h);
  3. persistence: the current measured outside temperature held flat, and **no sun**.
Each fallback errs towards more heat, never less: assuming no sun can only make the
planner heat more than it needed to, never leave the house cold.

Pure: no HA.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

SLOT = dt.timedelta(minutes=30)


def _ts(v) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v
    try:
        return dt.datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def find_forecast_list(resp):
    """Dig the hourly list out of a weather.get_forecasts response, whatever wrapper the
    caller's API put round it ({'weather.x': {'forecast': [...]}} or nested deeper)."""
    if isinstance(resp, dict):
        if isinstance(resp.get("forecast"), list):
            return resp["forecast"]
        for v in resp.values():
            got = find_forecast_list(v)
            if got is not None:
                return got
    return None


def parse_met(forecast_list) -> list[tuple[dt.datetime, float]]:
    out = []
    for f in forecast_list or []:
        t, temp = _ts(f.get("datetime")), f.get("temperature")
        if t is not None and isinstance(temp, (int, float)):
            out.append((t, float(temp)))
    return sorted(out)


def parse_solcast(*detailed) -> list[tuple[dt.datetime, float]]:
    """Merge Solcast `detailedForecast` lists (today, tomorrow). pv_estimate is kW."""
    out = {}
    for lst in detailed:
        for f in lst or []:
            t, kw = _ts(f.get("period_start")), f.get("pv_estimate")
            if t is not None and isinstance(kw, (int, float)):
                out[t] = float(kw)
    return sorted(out.items())


def _temp_at(series, t):
    """Linear interpolation between hourly points; None outside the series."""
    if not series or t < series[0][0] or t > series[-1][0]:
        return None
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds()
            return v0 if span <= 0 else v0 + (v1 - v0) * (t - t0).total_seconds() / span
    return series[-1][1] if t == series[-1][0] else None


def _solar_at(series, t):
    """The half-hour period containing t; None outside the series."""
    for t0, v in series:
        if t0 <= t < t0 + SLOT:
            return v
    return None


@dataclass
class ForecastCache:
    """Holds the last good forecast of each kind so a failed fetch doesn't erase it."""
    max_age_h: float = 6.0
    met: list = field(default_factory=list)
    met_at: dt.datetime | None = None
    sol: list = field(default_factory=list)
    sol_at: dt.datetime | None = None

    def offer(self, now, met=None, sol=None):
        if met:
            self.met, self.met_at = met, now
        if sol:
            self.sol, self.sol_at = sol, now

    def _fresh(self, at, now):
        return at is not None and (now - at).total_seconds() <= self.max_age_h * 3600

    def inputs(self, slot_starts, now, outside_now: float | None):
        """(t_out[], solar[], notes) for each slot start."""
        met_ok, sol_ok = self._fresh(self.met_at, now), self._fresh(self.sol_at, now)
        t_out, solar = [], []
        miss_t = miss_s = 0
        last_t = outside_now
        for s in slot_starts:
            mid = s + SLOT / 2
            v = _temp_at(self.met, mid) if met_ok else None
            if v is None:
                miss_t += 1
                v = last_t if last_t is not None else 10.0   # persistence; 10 °C if nothing at all
            else:
                last_t = v
            t_out.append(v)
            p = _solar_at(self.sol, s) if sol_ok else None
            if p is None:
                miss_s += 1
                p = 0.0                                   # no sun: errs towards more heat
            solar.append(p)
        notes = []
        n = len(slot_starts)
        if not met_ok:
            notes.append("no usable temperature forecast: holding the current outside temperature")
        elif miss_t:
            notes.append(f"temperature forecast covers {n - miss_t} of {n} slots; rest held flat")
        if not sol_ok:
            notes.append("no usable sun forecast: assuming no sun")
        elif miss_s:
            notes.append(f"sun forecast covers {n - miss_s} of {n} slots; rest assumed sunless")
        return t_out, solar, notes


def slot_starts(now: dt.datetime, hours: float) -> list[dt.datetime]:
    """Half-hour slot starts from the slot containing now, for `hours`."""
    base = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    return [base + i * SLOT for i in range(int(hours * 2))]
