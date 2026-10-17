# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_energy.py

The energy-signature planner (0.7.0): the day's heat need from the forecast, the curve's
share taken off, the rest bought in the cheapest slots, 32 before 38 in any slot, the
price cap and free slots honoured, a short price horizon planning a short day."""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from energy import EnergyConfig, plan_energy

tz = dt.timezone.utc
now = dt.datetime(2026, 10, 17, 10, 5, tzinfo=tz)
start = now.replace(minute=0)


def day(price_fn, n=48):
    return [(start + dt.timedelta(minutes=30 * i), price_fn(i)) for i in range(n)]


def flat_met(t, hours=30):
    return [(start + dt.timedelta(hours=h), t) for h in range(hours)]


# 177's numbers, 17 Oct 2026
ARGS = {"heat_loss": 0.37, "balance_point": 13.3, "baseline_kw": 0.44, "baseline_cop": 3.6,
        "levels": {32: {"kw": 1.2, "cop": 3.2}, 38: {"kw": 2.3, "cop": 2.8}}, "max_price": 0.20}
cfg = EnergyConfig.from_args(ARGS)
assert [l[0] for l in cfg.levels] == [32.0, 38.0]

# --- need from the mean forecast; curve share subtracted ------------------------------
rates = day(lambda i: 0.10 + 0.002 * i)          # cheapest first, rising
p = plan_energy(rates, flat_met(9.0), now, 9.0, cfg)
need = 0.37 * (13.3 - 9.0) * 24
assert abs(p.need_kwh - need) < 1e-6 and abs(p.baseline_kwh - 0.44 * 24) < 1e-6, p
assert p.shortfall_kwh == 0 and p.planned_kwh >= need - 1e-6, p
assert p.planned_kwh - need < (2.3 - 1.2) * 0.5 + 1e-6, p      # overshoots by at most one step
# Per kWh, 38 in a cheap slot can beat 32 in a dear one, so levels mix; what must hold is
# that the boosted slots are the cheapest ones (any cheaper slot's 32 would have come first).
boosted = [s for s in p.slots if s["min_flow"] is not None]
prices = sorted(s["price"] for s in p.slots)
assert max(s["price"] for s in boosted) <= prices[len(boosted) - 1] + 1e-9, boosted
assert p.level_now == 38.0, p.level_now                         # the first slot is the cheapest
assert p.summary()["plan"][0].endswith(" 38"), p.summary()

# --- mild day: the curve covers it, nothing bought -------------------------------------
p = plan_energy(rates, flat_met(12.5), now, 12.5, cfg)
assert p.need_kwh < p.baseline_kwh and not [s for s in p.slots if s["min_flow"]], p
assert p.level_now is None
# above the balance point: no need at all
assert plan_energy(rates, flat_met(15.0), now, 15.0, cfg).need_kwh == 0

# --- price cap: slots over it are never bought; shortfall reported ---------------------
dear = day(lambda i: 0.25 if i >= 4 else 0.10)
p = plan_energy(dear, flat_met(2.0), now, 2.0, cfg)
assert all(s["min_flow"] is None for s in p.slots[4:]), p.slots[:6]
assert all(s["min_flow"] == 38.0 for s in p.slots[:4]), p.slots[:6]
assert p.shortfall_kwh > 0 and "short" in p.notes[-1], p.notes

# --- free slots take the top level whatever the need ------------------------------------
free = day(lambda i: -0.02 if i == 10 else 0.15)
p = plan_energy(free, flat_met(14.0), now, 14.0, cfg)
assert p.slots[10]["min_flow"] == 38.0 and sum(1 for s in p.slots if s["min_flow"]) == 1, p.slots[8:12]

# --- short horizon (prices to 23:00): a short day's need, not the whole day crammed in -
short = day(lambda i: 0.15, n=26)
p = plan_energy(short, flat_met(9.0), now, 9.0, cfg)
assert abs(p.hours - 13) < 1e-9 and abs(p.need_kwh - 0.37 * 4.3 * 13) < 1e-6, (p.hours, p.need_kwh)

# --- forecast missing: outside now held, with a note; no forecast, no reading: no plan --
p = plan_energy(rates, None, now, 9.0, cfg)
assert abs(p.need_kwh - need) < 1e-6 and "no temperature forecast" in p.notes[0], p.notes
assert plan_energy(rates, None, now, None, cfg) is None
assert plan_energy([], flat_met(9.0), now, 9.0, cfg) is None

# --- config refuses levels that would make the greedy order wrong ----------------------
for bad in ({32: {"kw": 1.2, "cop": 3.2}, 38: {"kw": 1.0, "cop": 2.8}},     # heat falls
            {32: {"kw": 1.2, "cop": 2.0}, 38: {"kw": 2.3, "cop": 3.5}}):    # 38 cheaper per kWh
    try:
        EnergyConfig.from_args(dict(ARGS, levels=bad))
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass

print("ok: need from the forecast mean, curve share off, cheapest slots bought, cap, free slots, short horizon, fallbacks, config checks")
