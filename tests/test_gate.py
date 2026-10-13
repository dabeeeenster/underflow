# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_gate.py

The thermostat gate (0.4.0): when should the room thermostats be allowed to call? On 179
they switch the heat pump's demand and the plant-room manifold pump, which ran around the
clock with the Tados open (13 Oct 2026). The rules must close them only when underflow
has no reason to heat, never chatter at the comfort top, follow a step test exactly, and
fail towards heat.
"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import gate as G
from testplan import Step

cfg = G.GateConfig.from_args({"climates": ["climate.a", "climate.b"], "mode_off": False})
assert cfg.climates == ("climate.a", "climate.b") and cfg.mode_off == "off"   # YAML bare off
assert G.GateConfig.from_args(None) is None and G.GateConfig.from_args({"climates": []}) is None
assert cfg.hysteresis == 0.3 and cfg.dry_run is None


def w(prev, room, sp=20.0, step=None):
    return G.wanted(cfg, prev, room, sp, baseline=20.0, comfort_min=20.0, comfort_max=22.0, step=step)


# 13 Oct 2026 08:00: coldest room 22.2 °C over a 22 °C top, nothing to boost -> off
d = w(True, 22.2)
assert d.open is False and "comfort top" in d.reason and not d.urgent, d
# under the top -> on
assert w(None, 21.5).open is True
# hysteresis: once off, stays off until 0.3 K under the top
assert w(False, 21.9).open is False and "reopens below 21.7" in w(False, 21.9).reason
assert w(False, 21.7).open is True
assert w(True, 21.9).open is True                    # an open gate isn't closed early
# boosting (cheap slot) opens it even near the top
assert w(False, 21.9, sp=32.0).open is True
# comfort floor: open, and urgent (skips the dwell)
d = w(False, 19.8)
assert d.open is True and d.urgent, d
# no room reading: open, so the Tados decide as they always did
assert w(False, None).open is True

# step test: follows the block, urgent either way
off = Step(True, 20.0, "off", "test block 20/29: heating off")
heat = Step(True, 32.0, "day", "test block 21/29: min flow 32")
paused = Step(True, 20.0, "off", "heat paused at the ceiling", capped=True)
assert w(True, 21.0, step=off).open is False and w(True, 21.0, step=off).urgent
assert w(False, 22.9, step=heat).open is True       # the test ignores the comfort top
assert w(True, 21.0, step=paused).open is False
assert w(True, 21.0, step=Step(False)).open is True  # inactive step = planner rules

# the live model planner's "heat off" closes them; the comfort floor still wins (0.5.0)
d = G.wanted(cfg, True, 21.0, 20.0, 20.0, 20.0, 22.0, heat_off=True)
assert d.open is False and "model planner" in d.reason and not d.urgent, d
assert G.wanted(cfg, False, 19.9, 20.0, 20.0, 20.0, 22.0, heat_off=True).open is True
assert G.wanted(cfg, True, 21.0, 20.0, 20.0, 20.0, 22.0, step=heat, heat_off=True).open is True

# dwell: a non-urgent change waits out min_dwell_minutes (with a minute's slack)
now = dt.datetime(2026, 10, 14, 19, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=1)))
close = w(True, 22.2)
assert not G.may_change(cfg, close, True, now - dt.timedelta(minutes=10), now)
assert G.may_change(cfg, close, True, now - dt.timedelta(minutes=29, seconds=5), now)
assert G.may_change(cfg, close, None, None, now)                  # first decision
assert G.may_change(cfg, w(True, 19.5), False, now - dt.timedelta(minutes=1), now)   # urgent

print("ok: off at the comfort top with hysteresis, on to boost or at the floor, follows the "
      "step test, fails open, dwell holds non-urgent changes")
