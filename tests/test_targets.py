# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_targets.py

Room targets come from sliders in HA now, so a bad value must never reach the
controller: out of range, unparsable, or a band upside down all fall back to apps.yaml.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from planner import PlannerConfig
from testplan import TestConfig
import targets

pc = PlannerConfig(comfort_min=20.0, comfort_max=22.5)
tc = TestConfig(enabled=True, floor=19.0, ceiling=23.5)

p, t, n = targets.apply(pc, tc, {})
assert (p, t, n) == (pc, tc, []), (p, t, n)

p, t, n = targets.apply(pc, tc, {"comfort_min": "20.5", "comfort_max": "22", "test_floor": "18.5", "test_ceiling": "24"})
assert (p.comfort_min, p.comfort_max, t.floor, t.ceiling) == (20.5, 22.0, 18.5, 24.0) and not n, (p, t, n)

p, t, n = targets.apply(pc, tc, {"comfort_min": "unavailable", "comfort_max": "40"})
assert (p.comfort_min, p.comfort_max) == (20.0, 22.5) and len(n) == 1 and "40" in n[0], n  # unavailable is silent, 40 noted

p, t, n = targets.apply(pc, tc, {"comfort_min": "23", "comfort_max": "22"})
assert (p.comfort_min, p.comfort_max) == (20.0, 22.5) and "floor must be below top" in n[0], n

p, t, n = targets.apply(pc, tc, {"test_floor": "21", "test_ceiling": "20.5"})
assert (t.floor, t.ceiling) == (19.0, 23.5) and n, n

p, t, n = targets.apply(pc, TestConfig(floor=19.0, ceiling=None), {"test_floor": "18"})
assert t.floor == 18.0 and t.ceiling is None, t                                    # no ceiling configured

p, t, n = targets.apply(pc, tc, {"comfort_min": "21"})                             # one end only
assert (p.comfort_min, p.comfort_max) == (21.0, 22.5), p
print("test_targets: all passed")
