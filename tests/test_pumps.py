# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "scipy>=1.13", "pyyaml>=6"]
# ///
"""Run: uv run tests/test_pumps.py

One app, two pumps (0.3.0). With a stand-in for AppDaemon, the real site class must:
read a pre-0.3.0 single-pump apps.yaml exactly as before (same entity ids, names, alert
id); build each pump under `pumps:` from its own block plus the inherited site keys,
without leaking a step test or register to the other half; refuse a pump pointed at a
forbidden register (177's cooling circuit) while the other still starts; keep running
one pump when the other raises; route a slider change to its own pump only; and judge
comfort on the coldest of a room list, skipping an unavailable room.
"""
import sys, types, pathlib, datetime as dt
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml

BST = dt.timezone(dt.timedelta(hours=1))
NOW = dt.datetime(2026, 10, 12, 12, 0, 5, tzinfo=BST)


class FakeHass:
    """Just enough of appdaemon.plugins.hass.hassapi.Hass."""
    def __init__(self, args, states=None):
        self.args, self.states = args, dict(states or {})
        self.published, self.calls, self.listeners, self.run_ins, self.logs = {}, [], [], [], []

    def get_state(self, entity_id, attribute=None):
        st = self.states.get(entity_id)
        if isinstance(st, dict):
            return st.get(attribute) if attribute else st.get("state")
        return None if attribute else st

    def set_state(self, entity_id, state=None, attributes=None):
        self.published[entity_id] = (state, attributes or {})

    def call_service(self, service, **kw):
        self.calls.append((service, kw))
        return {}

    def listen_state(self, cb, entity, **kw):
        self.listeners.append((cb, entity, kw))

    def run_every(self, cb, start, interval):
        return len(self.run_ins)

    def run_in(self, cb, delay, **kw):
        self.run_ins.append((cb, kw))

    def cancel_timer(self, t):
        pass

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def datetime(self, aware=True):
        return NOW


# appdaemon is not installed here: give underflow.py the stand-in as its base class.
for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
    sys.modules.setdefault(name, types.ModuleType(name))
hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
hassapi.Hass = FakeHass
sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi
import underflow as U
import pump as P


def app(args, states=None):
    a = U.Underflow(args, states)
    a.initialize()
    return a


def alerts(a):
    return [kw for s, kw in a.calls if s == "persistent_notification/create"]


# --- single pump, pre-0.3.0 config: everything named as before ------------------------
example = yaml.safe_load((ROOT / "apps.example.yaml").read_text())["underflow"]
a = app(example, {"sensor.room_temperature_mean": "20.4", "sensor.ebusd_outsidetemp": "11.5"})
assert list(a.pumps) == [None], a.pumps
p = a.pumps[None]
assert (p.status_entity, p.plan_entity) == ("sensor.underflow", "sensor.underflow_planned_min_flow")
assert p.outside_sensors == {"179": "sensor.ebusd_outsidetemp", "177": "sensor.other_outsidetemp"}
a.on_decide()
state, attrs = a.published["sensor.underflow"]
assert state == "observing" and attrs["friendly_name"] == "underflow heat pump controller", (state, attrs)
assert attrs["outside_temp_179_sensor"] == "11.5" and attrs["room_temp_mean"] == "20.4"   # ha_safe: numbers go as strings
assert a.published["sensor.underflow_planned_min_flow"][1]["friendly_name"] == "underflow planned min flow temp"
assert a.published["sensor.underflow_whats_going_on"][1]["friendly_name"] == "underflow: what's going on"
p._alert("test")
assert alerts(a)[-1]["notification_id"] == "underflow_hc1minflowtempdesired"
assert alerts(a)[-1]["title"] == "underflow (Hc1MinFlowTempDesired) needs a look"

# --- two pumps: own blocks plus inherited site keys, nothing leaking -------------------
two = yaml.safe_load((ROOT / "examples" / "two-pumps.yaml").read_text())["underflow"]
cfgs = dict(U.pump_configs(two))
assert list(cfgs) == ["179", "177"]
c177, c179 = cfgs["177"], cfgs["179"]
assert c177["entities"]["weather"] == "weather.met_office_wood_green"          # inherited
assert c177["entities"]["outside_sensors"]["177"].endswith("177_broadcast_outsidetemp")
assert c177["entities"]["pump_status"].startswith("sensor.heating_ebusd177")   # its own
assert c177["datalog"]["dir"] == "/config/underflow_log" and c177["datalog"]["prefix"] == "177"
assert "model" not in c177 and "zone" not in c177 and "targets" not in c177   # 179's stay 179's
assert c177["dry_run"] is True and c179["dry_run"] is False
assert "probes" not in c177 and "pumps" not in c177
assert c179["zone"]["mode_off"] == "off" and P.zone_value(False, "x") == "off"     # YAML bare off

states = {
    "sensor.179_coldest_room": "20.6",
    "sensor.tado_smart_thermostat_ru2614901760_current_temperature": "21.3",   # 177 Kitchen
    "sensor.tado_smart_thermostat_ru1844530688_current_temperature": "20.2",   # 177 Upstairs
    "sensor.tado_smart_thermostat_ru3769716224_current_temperature": "unavailable",
    "sensor.tado_smart_thermostat_ru1602442752_current_temperature": "19.1",   # Lotte: logged only
}
two["probes"]["targets"] = []     # no sockets in a test
for c in two["pumps"].values():
    c["ebusd"].pop("host")
    c.pop("state_file")
two["datalog"].pop("dir")
a = app(two, states)
assert set(a.pumps) == {"179", "177"}
a.on_decide()
assert a.published["sensor.underflow_177"][1]["room_temp_mean"] == "20.2"      # coldest, Tanya skipped
assert a.published["sensor.underflow_179"][1]["room_temp_mean"] == "20.6"
assert a.published["sensor.underflow_177_planned_min_flow"][1]["friendly_name"] == "underflow 177 planned min flow temp"
assert a.published["sensor.underflow_177"][0] == "observing"                  # dry run
assert a.published["sensor.underflow_179"][0] == "controlling"
a.pumps["177"]._alert("x")
assert alerts(a)[-1]["notification_id"] == "underflow_177_hc2minflowtempdesired"
assert alerts(a)[-1]["title"].startswith("underflow 177 ")

# --- forbid_registers: 177 pointed at its cooling circuit never starts -----------------
bad = yaml.safe_load((ROOT / "examples" / "two-pumps.yaml").read_text())["underflow"]
for c in bad["pumps"].values():
    c["ebusd"].pop("host"); c.pop("state_file")
bad["probes"]["targets"] = []
bad["datalog"].pop("dir")
bad["pumps"]["177"]["ebusd"]["register"] = "Hc1MinFlowTempDesired"
a = app(bad, states)
assert set(a.pumps) == {"179"}, a.pumps                                       # 179 still runs
assert a.published["sensor.underflow_177"][0] == "refused"
assert "Hc1MinFlowTempDesired" in a.published["sensor.underflow_177"][1]["reason"]
assert any(kw["notification_id"] == "underflow_177_refused" for kw in alerts(a))
assert any(lvl == "ERROR" for lvl, _ in a.logs)
# the zone entity and the declared write register are checked too
assert P.forbidden({"forbid_registers": ["Z1*"], "zone": {"entity": "select.heating_ebusd177_ctlv2_z1opmode"},
                    "ebusd": {"register": "Hc2MinFlowTempDesired"}}) == ["z1opmode"]
assert P.forbidden({"forbid_registers": ["hc1*"], "write_register": "Hc1MinFlowTempDesired",
                    "ebusd": {"register": "Hc2MinFlowTempDesired"}}) == ["Hc1MinFlowTempDesired"]
assert P.forbidden({"forbid_registers": ["Hc1*", "Z1*"], "ebusd": {"register": "Hc2MinFlowTempDesired"},
                    "zone": {"entity": "select.heating_ebusd177_ctlv2_z2opmode"}}) == []
# two pumps publishing to the same status sensor: the second is refused
dup = yaml.safe_load((ROOT / "examples" / "two-pumps.yaml").read_text())["underflow"]
for c in dup["pumps"].values():
    c["ebusd"].pop("host"); c.pop("state_file")
dup["probes"]["targets"] = []
dup["datalog"].pop("dir")
dup["pumps"]["177"]["status_entity"] = "sensor.underflow_179"
assert set(app(dup, states).pumps) == {"179"}

# --- one pump raising never stops the other --------------------------------------------
a = app(two, states)
calls = []
def boom():
    raise RuntimeError("177 bus exploded")
a.pumps["177"].decide = boom
a.pumps["177"].reconcile = boom
real = a.pumps["179"].reconcile
a.pumps["179"].reconcile = lambda: (calls.append("179"), real())
order = list(a.pumps)
for i in range(U.FAIL_ALERT_TICKS):
    a.on_reconcile()
assert calls == ["179"] * U.FAIL_ALERT_TICKS, calls
assert a.pumps["177"].failures == U.FAIL_ALERT_TICKS and a.pumps["179"].failures == 0
assert sum(kw["notification_id"] == "underflow_177_failing" for kw in alerts(a)) == 1
a.on_decide()                                                                # decide too
assert a.published["sensor.underflow_179"][0] == "controlling"

# --- a slider re-decides its own pump only --------------------------------------------
a = app(two, states)
slider = [(cb, kw) for cb, ent, kw in a.listeners if ent == "input_number.underflow_179_comfort_floor"]
assert len(slider) == 1 and slider[0][1]["pump"] == "179"
assert all(kw.get("pump") in ("179", "177") for _, _, kw in a.listeners)
decided = []
for n, p in a.pumps.items():
    p.decide = (lambda n=n: decided.append(n))
cb, kw = slider[0]
cb("input_number.underflow_179_comfort_floor", "state", "20.0", "20.5", kw)
fn, rkw = a.run_ins[-1]
fn(rkw)
assert decided == ["179"], decided
# averaged columns (PV) are fed to each pump separately: both log pv_w
assert {kw["pump"] for _, ent, kw in a.listeners if ent == "sensor.solar_panel_production_w"} == {"179", "177"}

# --- shared inputs are fetched once per tick ------------------------------------------
a = app(two, states)
a.on_decide()
gets = [s for s, _ in a.calls if s == "weather/get_forecasts"]
assert len(gets) == 1, gets                     # 179's model planner
a.forecasts("weather.met_office_wood_green", two["pumps"]["179"]["model"]["forecast"]["solcast"])
assert len([s for s, _ in a.calls if s == "weather/get_forecasts"]) == 1      # memoised within the tick

# --- step-test ceiling pauses a heat block, then lets it resume (0.3.1) ------------------
import testplan as TP
a = app(two, states)
p = a.pumps["179"]
p.test_cfg = TP.TestConfig.from_args({"enabled": True, "start": (NOW - dt.timedelta(hours=1)).isoformat(),
                                      "blocks": [[3, 38]], "ceiling": 23.5})
p.entities["warmest_room"] = "sensor.179_warmest_room"
for warm, capped, zone in (("24.4", True, "off"), ("23.2", True, "off"), ("22.9", False, "day"), ("23.4", False, "day")):
    a.states["sensor.179_warmest_room"] = warm
    p._update_step({"room_temp_mean": 21.0}, NOW)
    assert p.step.capped is capped and p.step.zone == zone, (warm, p.step)
    assert (p._cap_block is not None) is capped, (warm, p._cap_block)
assert p.step.min_flow == 38
assert any("block resumes" in m for _, m in a.logs), a.logs[-5:]

# --- dwell: the tick a full dwell after a write moves on (0.3.2) -------------------------
# 177 wrote 32 at 15:30:06 on 12 Oct 2026; at 16:00:05 it held "for another 0 min".
a = app(two, states)
p = a.pumps["177"]
for since, moved in ((dt.timedelta(minutes=29, seconds=59), True), (dt.timedelta(minutes=20), False)):
    p.desired, p.desired_since = 42.0, NOW - since
    p.decide()
    assert (p.desired != 42.0) is moved, (since, p.desired)
assert any("(dwell)" in m for _, m in a.logs)

# --- thermostat gate (0.4.0): 179's Tados off at the comfort top, on again below ---------
import tempfile, os
TADOS = two["pumps"]["179"]["gate"]["climates"]
gstates = dict(states, **{"sensor.179_coldest_room": "22.6", "sensor.outside_temp_house": "15.0",
                          "weather.met_office_wood_green": {"state": "cloudy", "temperature": 14.0}})
for e in TADOS:
    gstates[e] = {"state": "heat", "temperature": 25, "hvac_action": "heating"}
a = app(two, gstates)
p = a.pumps["179"]
assert p.gate_cfg is not None and p.gate_dry_run is True and p.dry_run is False   # watch while live
assert a.pumps["177"].gate_cfg is None
a.on_decide()
attrs = a.published["sensor.underflow_179"][1]
assert p.gate_open is False and attrs["gate_open"] == "false" and "comfort top" in attrs["gate_reason"], attrs
assert not [c for c in a.calls if c[0].startswith("climate/")]                 # dry run: no writes
assert sum("DRY RUN: would set climate." in m for _, m in a.logs) == 3
assert p.gate_setpoints == {e: 25.0 for e in TADOS}                            # remembered while on
assert "Room thermostats: underflow would have them off" in a.published["sensor.underflow_179_whats_going_on"][1]["text"]
# one outside temperature: the house sensor, else the Met Office
assert attrs["outside_temp"] == "15.0" and attrs["outside_temp_source"] == "house"
a.states["sensor.outside_temp_house"] = "unavailable"
assert p._observe()["outside_temp"] == 14.0 and p._observe()["outside_temp_source"] == "met_office"

# live: three HomeKit writes, not repeated while in flight
p.gate_dry_run, p._gate_last_write = False, {}
a.calls.clear()
p.reconcile()
offs = [kw for s, kw in a.calls if s == "climate/set_hvac_mode"]
assert sorted(kw["entity_id"] for kw in offs) == sorted(TADOS) and {kw["hvac_mode"] for kw in offs} == {"off"}
p.reconcile()
assert len([c for c in a.calls if c[0].startswith("climate/")]) == 3
# Tados off (HomeKit then reports no setpoint); room falls under the top but within hysteresis: stays off
for e in TADOS:
    a.states[e] = {"state": "off", "temperature": None, "hvac_action": "idle"}
a.states["sensor.179_coldest_room"] = "22.3"
p.decide()
assert p.gate_open is False and "reopens below 22.2" in p.gate_reason, p.gate_reason
# under the hysteresis, but inside the dwell: held off
a.states["sensor.179_coldest_room"] = "22.0"
p.gate_since = NOW - dt.timedelta(minutes=10)
p.decide()
assert p.gate_open is False and "dwell" in p.gate_reason, p.gate_reason
# after the dwell: on, with the remembered setpoint (HomeKit forgot it)
p.gate_since, p._gate_last_write = NOW - dt.timedelta(minutes=31), {}
a.calls.clear()
p.decide()
assert p.gate_open is True
ons = [kw for s, kw in a.calls if s == "climate/set_temperature"]
assert len(ons) == 3 and all(kw["hvac_mode"] == "heat" and kw["temperature"] == 25.0 for kw in ons), ons
# the dashboard switch off: nothing written
a.states["input_boolean.underflow_179_thermostat_gate"] = "off"
p._gate_last_write = {}
a.calls.clear()
p.reconcile()
assert not [c for c in a.calls if c[0].startswith("climate/")]
assert "gate disabled" in a.published["sensor.underflow_179"][1]["gate_action"]
# survives a restart
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "state.json")
    p.state_file = path
    p._save_state(NOW)
    cfg179 = dict(U.pump_configs(two))["179"]
    cfg179["state_file"] = path
    q = P.Pump(a, "179", cfg179, "test", True)
    assert q.gate_open is True and q.gate_setpoints == {e: 25.0 for e in TADOS}, (q.gate_open, q.gate_setpoints)

# --- the live model planner (0.5.0): its plan drives the pump, the price planner stands in -
ZONE = "select.heating_ebusd179_ctlv2_z1opmode"
mstates = dict(gstates, **{"sensor.179_coldest_room": "21.0", ZONE: "day",
                           two["pumps"]["179"]["ebusd"]["signal_entity"]: {"state": "on", "last_changed": NOW - dt.timedelta(hours=1)}})
a = app(two, mstates)
p = a.pumps["179"]
p.model_live, p.gate_dry_run, p.dry_run = True, False, False
plans = {"off": {"state": "off", "level_now": "off", "predicted_min": 20.7},
         "32": {"state": "32", "level_now": 32.0, "predicted_min": 20.9}}
use = {"plan": plans["off"]}
p._shadow_plan = lambda obs, now, rates: use["plan"]
p.desired_since = NOW - dt.timedelta(hours=2)
p.decide()
attrs = a.published["sensor.underflow_179"][1]
assert p.source == "model" and p.heat_off and p.desired == 20.0, (p.source, p.heat_off, p.desired)
assert p.zone_desired == "off" and p.gate_open is False and "model planner" in p.gate_reason, (p.zone_desired, p.gate_reason)
assert attrs["heat_off"] == "true" and attrs["model_live"] == "true"
assert any(kw.get("variables") == {"mode": "off"} for s, kw in a.calls if s == "script/turn_on"), a.calls
assert "model planner has the heating off" in a.published["sensor.underflow_179_whats_going_on"][1]["text"]
# it wants heat: level written, zone back on, thermostats on after the gate's dwell
a.states[ZONE] = "off"
use["plan"] = plans["32"]
p.desired_since, p.gate_since, p._zone_last_write = NOW - dt.timedelta(hours=1), NOW - dt.timedelta(hours=1), None
p.decide()
assert p.desired == 32.0 and not p.heat_off and p.zone_desired == "day" and p.gate_open is True, (p.desired, p.zone_desired)
assert "set the minimum flow to 32" in a.published["sensor.underflow_179_whats_going_on"][1]["text"]
# no plan this tick: the price planner stands in, and says why; the zone goes back on
use["plan"] = None
p.desired_since = NOW - dt.timedelta(hours=1)
p.decide()
assert p.source == "planner" and "model planner unavailable" in p.source_reason and not p.heat_off, p.source_reason
assert "price planner is standing in" in a.published["sensor.underflow_179_whats_going_on"][1]["text"]
# the comfort-room sensor goes away: the model steps aside, the price planner stands in and
# the thermostats open (0.5.3); before, a stale plan could hold the heat off unwatched
use["plan"] = plans["off"]
a.states["sensor.179_coldest_room"] = "unavailable"
p.desired_since, p.gate_since = NOW - dt.timedelta(hours=1), NOW - dt.timedelta(hours=1)
p.decide()
assert p.source == "planner" and "no room reading" in p.source_reason and not p.heat_off, p.source_reason
assert p.gate_open is True, p.gate_reason
a.states["sensor.179_coldest_room"] = "21.0"
# a comfort-floor boost from the model skips the dwell even when the price planner, already
# boosting a cheap slot, has nothing urgent of its own (0.5.3)
a.states["sensor.179_coldest_room"] = "19.9"
p.desired, p.desired_since = 20.0, NOW - dt.timedelta(minutes=5)
real_plan = P.plan
P.plan = lambda *args, **kw: dict(real_plan(*args, **kw), setpoint_now=32.0, urgent=False)
try:
    p.decide()
finally:
    P.plan = real_plan
assert p.source == "model" and p.desired == 32.0, (p.source, p.desired, p.source_reason)
a.states["sensor.179_coldest_room"] = "21.0"
# a running step test still wins
use["plan"] = plans["off"]
p.test_cfg = p._base_test_cfg = TP.TestConfig.from_args({"enabled": True, "start": (NOW - dt.timedelta(hours=1)).isoformat(),
                                      "blocks": [[3, 38]]})
p.decide()
assert p.source == "test" and not p.heat_off and p.zone_desired == "day", (p.source, p.zone_desired)
# shadow pumps never act on it
q = app(two, mstates).pumps["179"]
q._shadow_plan = lambda obs, now, rates: plans["off"]
q.decide()
assert q.source == "planner" and not q.heat_off

# --- a comfort-room sensor drops out (0.6.0): estimated, alerted once, dismissed on return -
KIT, UPS, TAN = ("sensor.tado_smart_thermostat_ru2614901760_current_temperature",
           "sensor.tado_smart_thermostat_ru1844530688_current_temperature",
           "sensor.tado_smart_thermostat_ru3769716224_current_temperature")
a = app(two, dict(states, **{KIT: "21.3", UPS: "20.2", TAN: "21.0"}))
p = a.pumps["177"]
clock = [NOW]
a.datetime = lambda aware=True: clock[0]
p.decide()
assert p._observe()["room_temp_mean"] == 20.2
a.states[UPS] = "unavailable"                         # the coldest room's Tado goes
clock[0] = NOW + dt.timedelta(minutes=10)
obs = p._observe()
assert obs["room_temp_mean"] == 20.2 and obs["rooms_estimated"] == {UPS.split(".", 1)[1]: 20.2}, obs
assert not [kw for kw in alerts(a) if "room_" in kw["notification_id"]]      # not before 30 min
a.states[KIT], a.states[TAN] = "20.8", "20.5"           # the house cools 0.5 K: the estimate follows
clock[0] = NOW + dt.timedelta(minutes=40)
p.decide()
assert abs(p._observe()["room_temp_mean"] - 19.7) < 1e-9, p._observe()["room_temp_mean"]
room_alerts = [kw for kw in alerts(a) if "room_" in kw["notification_id"]]
assert len(room_alerts) == 1 and "estimating it at 19.7" in room_alerts[0]["message"], room_alerts
p.decide()
assert len([kw for kw in alerts(a) if "room_" in kw["notification_id"]]) == 1   # once per outage
assert "is estimated at 19.7" in a.published["sensor.underflow_177_whats_going_on"][1]["text"]
assert a.published["sensor.underflow_177"][1]["rooms_estimated"]
a.states[UPS] = "19.9"                                # back
clock[0] = NOW + dt.timedelta(minutes=45)
p._observe()
assert any(s == "persistent_notification/dismiss" and "room_" in kw["notification_id"] for s, kw in a.calls)
assert p.room_reading.coldest == 19.9 and not p._room_alerts
# a frozen sensor (0.6.1): Upstairs stuck at 20.2 while Kitchen and Tanya warm 0.3 K/h
a = app(two, dict(states, **{KIT: "21.3", UPS: "20.2", TAN: "21.0"}))
p = a.pumps["177"]
clock = [NOW]
a.datetime = lambda aware=True: clock[0]
for k in range(0, 5 * 12 + 1):
    h = k / 12
    clock[0] = NOW + dt.timedelta(hours=h)
    a.states[KIT], a.states[TAN] = f"{21.3 + 0.3 * h:.1f}", f"{21.0 + 0.3 * h:.1f}"
    p._observe()
UPN = UPS.split(".", 1)[1]                          # list config: rooms named by entity
assert UPN in p.room_reading.stuck and p.room_reading.coldest > 20.2, p.room_reading
stuck_alerts = [kw for kw in alerts(a) if "looks stuck" in kw["message"]]
assert len(stuck_alerts) == 1 and "looks stuck" in stuck_alerts[0]["message"], stuck_alerts
p.decide()
assert "looks stuck" in a.published["sensor.underflow_177_whats_going_on"][1]["text"]
a.states[UPS] = "21.6"                              # it moves: cleared, alert dismissed
clock[0] += dt.timedelta(minutes=5)
p._observe()
assert not p.room_reading.stuck and p.room_reading.coldest == 21.6 and not p._room_alerts
# HA restart with the Tados not back yet: no alert inside the first 30 min
a = app(two, dict(states, **{KIT: "unavailable", UPS: "unavailable", TAN: "unavailable"}))
p = a.pumps["177"]
p._observe()
assert p._comfort_room() is None and not [kw for kw in alerts(a) if "room_" in kw["notification_id"]]
# the offsets survive a restart
with tempfile.TemporaryDirectory() as d:
    a = app(two, dict(states, **{KIT: "21.3", UPS: "20.2", TAN: "21.0"}))
    p = a.pumps["177"]
    p.state_file = os.path.join(d, "s.json")
    p._observe(); p._save_state(NOW)
    cfg177 = dict(U.pump_configs(two))["177"]
    cfg177["state_file"] = p.state_file
    a.states[UPS] = "unavailable"
    q = P.Pump(a, "177", cfg177, "test", True)
    assert q._observe()["room_temp_mean"] == 20.2

print("ok: comfort rooms estimated through a dropout; model planner live drives level, zone and thermostats, falls back to the price planner; "
      "single-pump config unchanged, two pumps built and isolated, forbidden registers refused, "
      "a failing pump never stops the other, sliders routed, comfort on the coldest available room, "
      "thermostat gate decides, writes, holds and restores")
