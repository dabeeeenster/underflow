# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_shared.py"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner import PlannerConfig
from shared import SharedConfig, shared_step

pc = PlannerConfig()                       # boost 32, band 20.5-22.5
cfg = SharedConfig(target=21.5)            # gain 2, step 1, deadband 0.2, 20-45

# --- nothing to recover: start from cfg.start --------------------------------
v, why = shared_step(None, 21.5, 21.5, cfg, pc)
assert v == 30.0 and "on target" in why, (v, why)

# --- inside the deadband: hold ------------------------------------------------
v, _ = shared_step(30.0, 21.35, 21.5, cfg, pc)
assert v == 30.0, v

# --- small error moves proportionally, large error is capped at one step ------
v, why = shared_step(30.0, 21.25, 21.5, cfg, pc)
assert abs(v - 30.5) < 1e-9 and "warming" in why, (v, why)
v, _ = shared_step(30.0, 19.0, 21.5, cfg, pc)
assert v == 31.0, "2.5 K cold must still move only one step per tick"
v, why = shared_step(30.0, 23.0, 21.5, cfg, pc)
assert v == 29.0 and "easing" in why, (v, why)

# --- floor and screed cap -------------------------------------------------------
assert shared_step(20.0, 25.0, 21.0, cfg, pc)[0] == 20.0
assert shared_step(45.0, 15.0, 21.0, cfg, pc)[0] == 45.0

# --- 179 too warm wins over a cold shared sensor (8 Oct afternoon: Shelly 21.5,
#     179's west rooms 24.1) ------------------------------------------------------
v, why = shared_step(35.0, 20.1, 24.1, cfg, pc)
assert v == 34.0 and "179 is at 24.1" in why, (v, why)

# --- 179 too cold wins over a warm shared sensor --------------------------------
v, why = shared_step(24.0, 22.5, 20.3, cfg, pc)
assert v == 32.0 and "raised to 32" in why, (v, why)
# ...but never pulls an already-higher value down
assert shared_step(36.0, 21.0, 20.3, cfg, pc)[0] == 37.0

# --- no sensor reading: hold, still subject to 179's limits ---------------------
v, why = shared_step(31.0, None, 21.5, cfg, pc)
assert v == 31.0 and "holding" in why, (v, why)
assert shared_step(31.0, None, 23.0, cfg, pc)[0] == 30.0

# --- a cold shared sensor with 179 comfortable: at most one step per tick -------
v = 30.0
for shelly in [21.0, 20.7, 20.4, 20.1]:
    v, _ = shared_step(v, shelly, 21.5, cfg, pc)
assert v == 34.0, f"four half-hours of a cold Shelly must add at most 4 °C: {v}"

print("test_shared: ok")
