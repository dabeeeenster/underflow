# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_persist.py

Ben restarts Home Assistant for updates; every restart re-initialises the app. What was
saved must come back exactly (and a step-test guard that had tripped must stay tripped),
and a missing, corrupt, foreign or stale file must simply be ignored.
"""
import sys, pathlib, datetime as dt, tempfile, os, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import persist
from testplan import TestConfig, current_block, test_step

BST = dt.timezone(dt.timedelta(hours=1))
T0 = dt.datetime(2026, 10, 11, 1, 0, tzinfo=BST)
cfg = TestConfig.from_args({"enabled": True, "start": "2026-10-11T00:00:00+01:00",
                            "blocks": [[6, "off"], [3, 38]], "floor": 19.0})

with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "state.json")
    assert persist.load(path, T0) is None                         # nothing saved yet

    # the floor guard trips at 01:00 in the 6 h off block
    blk = current_block(cfg, T0)
    s = test_step(cfg, T0, coldest=19.0, baseline=20)
    assert s.guarded
    persist.save(path, {"desired": 25.0, "guard_block": persist.block_key(blk),
                        "zone_desired": "day", "test_was_active": True,
                        "forecast": {"met": persist.series_out([(T0, 9.5)]), "met_at": T0}}, T0)

    # HA restarts; at 01:20 the room has recovered a little
    later = T0 + dt.timedelta(minutes=20)
    doc = persist.load(path, later)
    assert doc and doc["desired"] == 25.0 and doc["zone_desired"] == "day", doc
    cur = current_block(cfg, later)
    latched = persist.same_block(doc["guard_block"], cur)
    s2 = test_step(cfg, later, coldest=19.3, baseline=20, guard_latched=latched)
    assert latched and s2.guarded and s2.zone == "day", s2        # stays on: no off-again after restart
    # ...and without the saved latch it would have switched off again (the bug)
    assert test_step(cfg, later, coldest=19.3, baseline=20).zone == "off"
    # the latch does not leak into the next block
    nxt = current_block(cfg, T0 + dt.timedelta(hours=6))
    assert not persist.same_block(doc["guard_block"], nxt)
    assert persist.series_in(doc["forecast"]["met"]) == [(T0, 9.5)]

    # stale, corrupt and foreign files are ignored
    assert persist.load(path, T0 + dt.timedelta(hours=25)) is None
    open(path, "w").write("{not json")
    assert persist.load(path, later) is None
    json.dump({"version": 99, "saved_at": T0.isoformat()}, open(path, "w"))
    assert persist.load(path, later) is None
    assert not os.path.exists(path + ".tmp")                       # atomic write leaves no temp file
print("persist OK: guard latch survives a restart; stale/corrupt/foreign files ignored")
