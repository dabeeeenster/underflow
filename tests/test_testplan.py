# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_testplan.py

A step test switches 179's heating fully off for hours at a time, so the things that
matter are: blocks start and end exactly where the schedule says, "off" really asks for
the zone to be off, the cold-room guard trips and then stays tripped for the rest of
the block, and nothing is active before the start, after the end, or when disabled.
"""
import sys, pathlib, datetime as dt, tempfile, os, csv
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from testplan import TestConfig, current_block, test_step, OFF
import datalog

BST = dt.timezone(dt.timedelta(hours=1))
T0 = dt.datetime(2026, 10, 10, 18, 0, tzinfo=BST)
cfg = TestConfig.from_args({"enabled": True, "start": "2026-10-10T18:00:00+01:00",
                            "blocks": [[3, "off"], [3, 38], [6, "OFF"], [2, 25]],
                            "floor": 19.0, "guard_flow": 25})
h = lambda x: T0 + dt.timedelta(hours=x)

# --- parsing ---------------------------------------------------------------
assert cfg.blocks == ((3.0, OFF), (3.0, 38.0), (6.0, OFF), (2.0, 25.0)), cfg.blocks
assert cfg.end_at() == h(14)
try:
    TestConfig.from_args({"enabled": True, "start": "2026-10-10T18:00:00", "blocks": [[1, 30]]}).start_at()
    raise AssertionError("naive start must be rejected")
except ValueError:
    pass

# --- block boundaries --------------------------------------------------------
assert current_block(cfg, h(-0.01)) is None
assert current_block(cfg, h(0)).index == 0
assert current_block(cfg, h(2.99)).index == 0
assert current_block(cfg, h(3)).index == 1
assert current_block(cfg, h(6)).level == OFF and current_block(cfg, h(6)).index == 2
assert current_block(cfg, h(13.99)).index == 3
assert current_block(cfg, h(14)) is None
assert current_block(TestConfig.from_args({**{"start": cfg.start}, "blocks": [[3, 30]]}), h(1)) is None  # disabled

# --- steps -------------------------------------------------------------------
s = test_step(cfg, h(1), coldest=20.5, baseline=20)
assert s.active and s.zone == "off" and s.min_flow == 20 and not s.guarded, s
s = test_step(cfg, h(4), coldest=20.0, baseline=20)
assert s.zone == "day" and s.min_flow == 38, s
s = test_step(cfg, h(15), coldest=20.0, baseline=20)
assert not s.active and s.zone is None, s

# --- guard ---------------------------------------------------------------------
s = test_step(cfg, h(8), coldest=19.0, baseline=20)
assert s.guarded and s.zone == "day" and s.min_flow == 25, s
# room recovers a little: still guarded because the caller latched it
s = test_step(cfg, h(9), coldest=19.3, baseline=20, guard_latched=True)
assert s.guarded and s.zone == "day", s
# unknown room temperature never trips the guard on its own
s = test_step(cfg, h(8), coldest=None, baseline=20)
assert not s.guarded and s.zone == "off", s
# the guard only applies to off blocks
s = test_step(cfg, h(13), coldest=18.0, baseline=20)
assert not s.guarded and s.min_flow == 25 and s.zone == "day", s

# --- ceiling -------------------------------------------------------------------
cc = TestConfig.from_args({"enabled": True, "start": cfg.start, "blocks": [[3, 38], [3, "off"]],
                           "ceiling": 23.5})
s = test_step(cc, h(1), coldest=21, baseline=20, warmest=23.4)
assert s.zone == "day" and s.min_flow == 38 and not s.capped, s
s = test_step(cc, h(1), coldest=21, baseline=20, warmest=23.5)
assert s.capped and s.zone == "off" and s.min_flow == 20, s
s = test_step(cc, h(2), coldest=21, baseline=20, warmest=22.9, cap_latched=True)
assert s.capped and s.zone == "off", s
s = test_step(cc, h(4), coldest=21, baseline=20, warmest=24.0)     # off block: ceiling irrelevant
assert s.zone == "off" and not s.capped, s
assert not test_step(cfg, h(4), coldest=21, baseline=20, warmest=30).capped   # no ceiling configured

# --- datalog ---------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    p1 = datalog.append(d, "u", T0, {"ts": "a", "x": 1, "y": None})
    p2 = datalog.append(d, "u", T0, {"ts": "b", "x": 2, "y": 3})
    assert p1 == p2
    rows = list(csv.reader(open(p1)))
    assert rows == [["ts", "x", "y"], ["a", "1", ""], ["b", "2", "3"]], rows
    p3 = datalog.append(d, "u", T0, {"ts": "c", "z": 9})      # new columns -> new file
    assert p3 != p1 and os.path.basename(p3) == "u-2026-10-2.csv", p3
    assert datalog.append(d, "u", T0, {"ts": "d", "x": 1, "y": 1}) == p1   # old header still found

print("test_testplan: all passed")
