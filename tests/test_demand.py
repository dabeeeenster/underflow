# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_demand.py"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ebusd import Reading
from planner import PlannerConfig
from demand import Held, MirrorConfig, choose, hold

tz = dt.timezone.utc
T0 = dt.datetime(2026, 11, 3, 2, 0, tzinfo=tz)
pc = PlannerConfig()                       # baseline 20, boost 32, band 20.5-22.5
off = MirrorConfig(enabled=False)
on = MirrorConfig(enabled=True)

# --- mirror disabled: the local plan decides ---------------------------------
v, src, why = choose(Held(), 32.0, 21.5, pc, off, T0)
assert (v, src) == (32.0, "planner") and "disabled" in why, (v, src, why)

# --- hold() advances only on a good reading ----------------------------------
h = hold(Held(), Reading(30.0, None, T0), T0)
assert h.value == 30.0 and h.at == T0
h2 = hold(h, Reading(None, "ERR: read timed out", T0), T0 + dt.timedelta(minutes=5))
assert h2 == h, "a failed read must leave the last-good value standing, not clear it"

# --- mirror fresh: follow 177 ------------------------------------------------
v, src, why = choose(h, 20.0, 21.5, pc, on, T0)
assert (v, src) == (30.0, "mirror") and "177" in why, (v, src, why)

# --- mirror held across a 177 dropout ----------------------------------------
v, src, _ = choose(h, 20.0, 21.5, pc, on, T0 + dt.timedelta(minutes=20))
assert (v, src) == (30.0, "mirror"), "a 20 min 177 outage must not change 179's target"

# --- ...but abandoned once it is genuinely stale -----------------------------
v, src, why = choose(h, 24.0, 21.5, pc, on, T0 + dt.timedelta(minutes=50))
assert (v, src) == (24.0, "planner") and "50 min old" in why, (v, src, why)
v, src, why = choose(Held(), 24.0, 21.5, pc, on, T0)
assert (v, src) == (24.0, "planner") and "no reading yet" in why, (v, src, why)

# --- the screed cap and the floor bind on a mirrored value -------------------
v, _, _ = choose(Held(60.0, T0), 20.0, 21.5, pc, on, T0)
assert v == 45.0, "never mirror past the 45 C screed cap"
v, _, _ = choose(Held(12.0, T0), 20.0, 21.5, pc, on, T0)
assert v == 20.0

# --- 179 keeps its own comfort band: the slabs are separate ------------------
v, _, why = choose(Held(20.0, T0), 20.0, 20.0, pc, on, T0)
assert v == pc.boost and "raised" in why, (v, why)          # 179 cold, 177 asking for nothing
v, _, why = choose(Held(40.0, T0), 20.0, 23.0, pc, on, T0)
assert v == pc.baseline and "cut" in why, (v, why)          # 179 already warm

# --- nothing available at all ------------------------------------------------
v, src, _ = choose(Held(), None, 21.5, pc, on, T0)
assert v is None and src == "none"

print("demand OK: mirror held across a dropout, abandoned at 45 min, cap and comfort band bind")
