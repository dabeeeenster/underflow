"""One heat pump under underflow's control: its register, bus, plan, step test, zone,
room thermostats, shadows, data log and saved state.

`underflow.py` is the site: it holds what the pumps share (timers, Agile prices,
forecasts, liveness probes, alerts) and runs one `Pump` per entry under `pumps:`. With no
`pumps:` key the whole of apps.yaml is one pump, exactly as before 0.3.0.

This module has no AppDaemon import. Everything it asks of Home Assistant goes through
the site object (`get_state`, `set_state`, `call_service`, `log`, `datetime`), so the
tests can drive it with a fake.
"""
import dataclasses
import datetime as dt
import fnmatch

import numpy as np

from planner import PlannerConfig, plan
from narrative import describe, summary
from hasafe import ha_safe
from ebusd import MAX_CLOCK_SKEW, Ebusd
from reconcile import ReconcileConfig, RegisterState, decide
from demand import Held, MirrorConfig, choose, from_model, hold
from shared import SharedConfig, shared_step
from testplan import TestConfig, Step, test_step, current_block
import gate as G
import persist
import targets
import shadow as SH
import rooms as R
import datalog


class PumpRefused(Exception):
    """The pump's config points somewhere it must never write (see `forbid_registers`)."""


def zone_value(v, default):
    """YAML 1.1 reads a bare `off` value as False and `on` as True; take either spelling."""
    if v is None:
        return default
    if v is False:
        return "off"
    if v is True:
        return "on"
    return str(v)


def forbidden(cfg: dict) -> list[str]:
    """Registers this pump is configured to touch that match one of its
    `forbid_registers` globs (case-insensitive). On 177, Hc1 and zone 1 are the cooling
    circuit, so its config forbids `Hc1*` and `Z1*`.

    Checked: the register it verifies (`ebusd.register`), the register its write script
    writes (`write_register`, declared alongside `write_script`, since the script itself is
    opaque from here), and the zone it switches (`zone.register`, or the last word of
    `zone.entity`, e.g. `..._ctlv2_z1opmode` -> `z1opmode`)."""
    globs = [str(g).lower() for g in (cfg.get("forbid_registers") or [])]
    if not globs:
        return []
    eb = cfg.get("ebusd", {}) or {}
    zc = cfg.get("zone", {}) or {}
    touched = [eb.get("register", "Hc1MinFlowTempDesired"), cfg.get("write_register"),
               zc.get("register") or (str(zc["entity"]).rsplit("_", 1)[-1] if zc.get("entity") else None)]
    return [t for t in touched if t and any(fnmatch.fnmatchcase(str(t).lower(), g) for g in globs)]


class Pump:
    def __init__(self, app, name, cfg, version, numeric_ok):
        """`app` is the site (underflow.Underflow); `name` is None in single-pump mode, which
        keeps every published name and alert exactly as it was before 0.3.0."""
        self.app, self.name, self.args = app, name, cfg
        self.version, self.numeric_ok = version, numeric_ok
        bad = forbidden(cfg)
        if bad:
            raise PumpRefused(f"configured to touch {', '.join(map(str, bad))}, which "
                              f"forbid_registers {cfg.get('forbid_registers')} rules out")
        self.label = f"underflow {name}" if name else "underflow"
        self.entities = dict(cfg.get("entities", {}) or {})
        self.status_entity = cfg.get("status_entity", f"sensor.underflow_{name}" if name else "sensor.underflow")
        self.dry_run = bool(cfg.get("dry_run", True))
        self.min_dwell = float(cfg.get("min_dwell_minutes", 30)) * 60
        # Ticks fire at :00:05 and :30:05 but a write lands a second or so into one, so the
        # tick a full dwell later saw 29:59 and held for another slot (177, 16:00 12 Oct 2026).
        self.dwell_slack = 60.0
        self.deadband = float(cfg.get("deadband", 0.5))

        pc = cfg.get("planner", {}) or {}
        self.planner_cfg = PlannerConfig(**{k: v for k, v in pc.items() if k in PlannerConfig.__dataclass_fields__})
        self.plan_entity = cfg.get("plan_entity", self.status_entity + "_planned_min_flow")
        self.narrative_entity = cfg.get("narrative_entity", self.status_entity + "_whats_going_on")
        self.write_script = cfg.get("write_script")  # e.g. script/set_179_min_flow_temp

        # --- the register we own, and how we verify it
        eb = cfg.get("ebusd", {}) or {}
        self.circuit = eb.get("circuit", "ctlv2")
        self.register = eb.get("register", "Hc1MinFlowTempDesired")
        self.bus = Ebusd(eb["host"], eb["port"], float(eb.get("timeout", 4.0))) if eb.get("host") else None
        # Routine verification accepts a cached value of known age; only the read that
        # confirms a write we just made has to come off the wire. See ebusd.py.
        self.bus_max_age = float(eb.get("max_age", 600.0))
        self._force_next_read = False
        self.signal_entity = eb.get("signal_entity")
        rc = cfg.get("reconcile", {}) or {}
        self.reconcile_cfg = ReconcileConfig(**{k: v for k, v in rc.items() if k in ReconcileConfig.__dataclass_fields__},
                                             dry_run=self.dry_run)
        self.reg_state = RegisterState()

        # --- the demand source (177's Havenwise decisions, once they exist)
        mc = dict(cfg.get("mirror", {}) or {})
        self.mirror_host, self.mirror_port = mc.pop("host", None), mc.pop("port", None)
        self.mirror_circuit = mc.pop("circuit", "ctlv2")
        self.mirror_register = mc.pop("register", "Hc2MinFlowTempDesired")
        mc.pop("timeout", None)
        self.mirror_cfg = MirrorConfig(**{k: v for k, v in mc.items() if k in MirrorConfig.__dataclass_fields__})
        self.mirror_bus = (Ebusd(self.mirror_host, self.mirror_port, float(eb.get("timeout", 4.0)))
                           if self.mirror_cfg.enabled and self.mirror_host else None)
        self.held = Held()

        # --- shadows: alternative demand sources computed every decide tick and
        # published as their own sensors, never written.
        sc = dict(cfg.get("shared", {}) or {})
        self.shared_sensor = sc.pop("sensor", None)
        self.shared_entity = sc.pop("entity", self.status_entity + "_shadow_shared")
        self.shared_cfg = SharedConfig(**{k: v for k, v in sc.items() if k in SharedConfig.__dataclass_fields__})
        self.shared_value = self._num(self.shared_entity) if self.shared_sensor else None
        # The mirror shadow reads the other half's register from HA's ebusd cache rather
        # than the bus: a shadow is not worth a transaction on 177's poor link.
        self.mirror_shadow_source = self.entities.get("mirror_source") or self.entities.get("mirror_177")
        self.mirror_shadow_entity = self.status_entity + "_shadow_mirror"

        # Outside sensors to log, in order: {"179": entity, "177": entity}. `outside_179` /
        # `outside_177` are the pre-0.3.0 spelling and still work.
        self.outside_sensors = dict(self.entities.get("outside_sensors", {}) or {})
        if not self.outside_sensors:
            for k in ("outside_179", "outside_177"):
                if self.entities.get(k):
                    self.outside_sensors[k.split("_", 1)[1]] = self.entities[k]
        # The room the comfort band is judged on: an HA sensor (`room_mean`), or the
        # coldest of `comfort_rooms` (a list, or {name: entity}), worked out here. A room
        # that drops out is estimated from the others for a while rather than ignored
        # (rooms.py, 0.6.0; `rooms:` tunes it). Rooms only logged (a flaky sensor, a cold
        # spare room) stay out.
        cr = self.entities.get("comfort_rooms") or []
        self.room_tracker = R.Rooms(R.RoomsConfig.from_args(cr, cfg.get("rooms"))) if cr else None
        self.room_reading = None
        self._room_alerts = {}        # name -> "estimated" / "dropped": what we last alerted
        self._rooms_since = None

        # --- stage-2 step tests: a fixed schedule of min-flow / zone-off blocks that
        # replaces the planner while it runs. See testplan.py.
        self.test_cfg = TestConfig.from_args(cfg.get("test"))
        zc = cfg.get("zone", {}) or {}
        self.zone_entity = zc.get("entity")          # select.heating_ebusd179_ctlv2_z1opmode
        self.zone_script = zc.get("script")          # script.set_179_zone_mode
        # What the zone select calls "on" and "off" (testplan speaks "day" / "off"). Not
        # keyed `on:` / `off:`: YAML 1.1 reads those keys as booleans.
        self.zone_on = zone_value(zc.get("mode_on"), "day")
        self.zone_off = zone_value(zc.get("mode_off"), "off")
        self.zone_desired = None                     # None = leave the zone alone
        self._zone_last_write = None
        self.step = Step(False)
        # Room targets adjustable from HA (targets.py): {key: input_number entity}. The
        # site listens to them and routes a change back to this pump only.
        self.target_helpers = dict(cfg.get("targets", {}) or {})
        self._base_planner_cfg, self._base_test_cfg = self.planner_cfg, self.test_cfg
        self._targets_seen = None
        self._guard_block = None
        self._cap_block = None
        self._test_was_active = False

        # --- the room thermostats, switched on and off (gate.py, 0.4.0). They gate both
        # the heat pump's demand and, on 179, the manifold pump in the plant room. Its own
        # `dry_run` lets it watch while the pump is live.
        try:
            self.gate_cfg = G.GateConfig.from_args(cfg.get("gate"))
        except Exception as exc:
            self.gate_cfg = None
            self.log(f"gate config rejected: {exc!r}", level="WARNING")
        self.gate_dry_run = (self.dry_run if self.gate_cfg is None or self.gate_cfg.dry_run is None
                             else bool(self.gate_cfg.dry_run))
        self.gate_open, self.gate_since, self.gate_reason = None, None, ""
        self.gate_setpoints = {}       # each thermostat's setpoint, last seen while on
        self._gate_last_write, self._gate_misses = {}, {}

        # --- 5-minute CSV for fitting the models offline. See datalog.py.
        lc = cfg.get("datalog", {}) or {}   # not "log": AppDaemon reserves it
        self.log_dir = lc.get("dir")
        self.log_prefix = lc.get("prefix", name or "underflow")
        self.log_columns = dict(lc.get("columns", {}) or {})
        # Time-weighted means between rows (e.g. PV), fed by state changes the site
        # subscribes to. Each pump keeps its own, since `take` empties it for the next row.
        self.log_averaged = dict(lc.get("averaged", {}) or {})
        self._averages = {}
        for n, ent in self.log_averaged.items():
            avg = datalog.TimeAverage()
            avg.update(self.get_state(ent), self.datetime(aware=True))
            self._averages[n] = avg
        # Registers read straight off the bus for each row ("circuit Register").
        self.log_bus = dict(lc.get("bus", {}) or {})
        self._last_log = None

        # --- stage-2 model planner (shadow.py): plans and publishes every decide tick.
        # Needs fitted parameters under `model.params`; without them it says so. With
        # `model.live` (0.5.0) its plan drives the pump, the price planner standing in
        # whenever it has none; otherwise it is a shadow and never writes.
        mc = cfg.get("model", {}) or {}
        self.model_live = bool(mc.get("live", False))
        self.heat_off = False          # the live model planner wants no heat this slot
        self.model_entity = mc.get("entity", self.status_entity + "_model_plan")
        self.model_forecast = dict(mc.get("forecast", {}) or {})
        try:
            scfg = SH.ShadowConfig.from_args(mc)
        except Exception as exc:
            scfg = None
            self.log(f"model config rejected: {exc!r}", level="WARNING")
        self.shadow = SH.Shadow(scfg, float(mc.get("max_forecast_age_h", 6))) if scfg else None
        self._last_row = None
        self._last_shadow = None
        self._write_at = None

        self.desired = self._num(self.plan_entity)
        self.desired_since = self.datetime(aware=True) if self.desired is not None else None
        # Everything above that matters across a restart is overwritten from the state
        # file if there is a recent one (persist.py). Ben restarts HA for updates.
        self.state_file = cfg.get("state_file")
        self.restored = self._restore_state()
        self.source, self.source_reason = "none", "starting up"
        self._unknown_codes = set()
        self._last_decision = None
        self.failures = 0          # consecutive ticks this pump raised; see the site

        if self.gate_cfg is not None:
            self.log(f"gate: {len(self.gate_cfg.climates)} thermostats, dry_run={self.gate_dry_run}, "
                     f"reopen {self.gate_cfg.hysteresis:g} K under the comfort top")
        if self.test_cfg.enabled:
            self.log(f"step test configured: {len(self.test_cfg.blocks)} blocks from "
                     f"{self.test_cfg.start} to {self.test_cfg.end_at()}; floor {self.test_cfg.floor:g}")
        self.log(f"pump ready: dry_run={self.dry_run}; register {self.circuit} {self.register}; "
                 f"log={self.log_dir or 'off'}; recovered desired={self.desired}")

    # ---- the site's Home Assistant handle --------------------------------
    def get_state(self, *a, **kw):
        return self.app.get_state(*a, **kw)

    def call_service(self, *a, **kw):
        return self.app.call_service(*a, **kw)

    def datetime(self, **kw):
        return self.app.datetime(**kw)

    def log(self, msg, level="INFO"):
        self.app.log(f"[{self.name}] {msg}" if self.name else msg, level=level)

    # ---- helpers -------------------------------------------------------
    def _num(self, entity_id, attribute=None):
        if not entity_id:
            return None
        try:
            v = self.get_state(entity_id, attribute=attribute)
            return None if v in (None, "unknown", "unavailable", "") else float(v)
        except (TypeError, ValueError):
            return None

    def _publish(self, entity_id, state, attributes):
        """set_state with every value run through ha_safe(). See its docstring."""
        self.app.set_state(entity_id, state=ha_safe(state), attributes=ha_safe(attributes))

    def _signal(self):
        """(up, seconds it has been continuously up). The one ebusd entity that is
        genuinely live — it flaps in real time, unlike the registers."""
        if not self.signal_entity:
            return True, float("inf")
        try:
            state = self.get_state(self.signal_entity)
            changed = self.get_state(self.signal_entity, attribute="last_changed")
        except Exception:
            return False, 0.0
        if state != "on":
            return False, 0.0
        if isinstance(changed, str):
            changed = dt.datetime.fromisoformat(changed)
        if not isinstance(changed, dt.datetime):
            return True, float("inf")
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=self.datetime(aware=True).tzinfo)
        return True, max(0.0, (self.datetime(aware=True) - changed).total_seconds())

    def _mode(self, status):
        """Classify the HMU run-data status code into idle / dhw / heating / cooling.

        A cylinder charge runs the flow to 60-70 C, a completely different operating point
        from 30 C space heating: mixing the two fits the COP model to the wrong regime.
        Unrecognised codes are logged once and never counted as heating.
        """
        s = (status or "").strip().lower()
        if s in ("", "unknown", "unavailable"):
            return "unknown"
        if "standby" in s or s == "off":
            return "idle"
        if "hwc" in s or "dhw" in s or "water" in s:
            return "dhw"
        if "cool" in s:
            return "cooling"
        if "hc" in s or "heat" in s:
            return "heating"
        if s not in self._unknown_codes:
            self._unknown_codes.add(s)
            self.log(f"unrecognised HMU status code {status!r}; not counted as heating", level="WARNING")
        return "other"

    def _comfort_room(self):
        if self.entities.get("room_mean"):
            return self._num(self.entities["room_mean"])
        return self.room_reading.coldest if self.room_reading else None

    def _warmest_room(self):
        if self.entities.get("warmest_room"):
            return self._num(self.entities["warmest_room"])
        return self.room_reading.warmest if self.room_reading else None

    def _update_rooms(self, now):
        """Read the comfort rooms once per observation, estimating any that dropped out,
        and alert once when one has been missing `alert_minutes`, and again if it is
        given up on."""
        if self.room_tracker is None:
            return
        cfg = self.room_tracker.cfg
        r = self.room_reading = self.room_tracker.update(now, {n: self._num(e) for n, e in cfg.rooms})
        self._rooms_since = self._rooms_since or now
        for n, mins in r.missing.items():
            state = "dropped" if n in r.dropped else "stuck" if n in r.stuck else "estimated"
            # A room not seen since start counts from the start: after an HA restart the
            # thermostats can be unavailable for a minute while HomeKit reconnects.
            waited = mins if mins is not None else (now - self._rooms_since).total_seconds() / 60
            if waited < cfg.alert_minutes:
                continue
            if self._room_alerts.get(n) == state:
                continue
            self._room_alerts[n] = state
            if state == "stuck":
                frozen = self.room_tracker.seen[n].value
                msg = (f"{n}'s room sensor has read {frozen:.1f} °C for {r.stuck[n]:.1f} h while every other room "
                       f"moved {cfg.stuck_move:g} °C or more, so it looks stuck. underflow is estimating it at "
                       f"{r.estimated[n]:.1f} °C from the other rooms until its reading changes."
                       if n in r.estimated else
                       f"{n}'s room sensor has read {frozen:.1f} °C for {r.stuck[n]:.1f} h while every other room "
                       f"moved {cfg.stuck_move:g} °C or more, so it looks stuck; comfort is judged without it.")
            elif state == "estimated":
                msg = (f"{n}'s room sensor has been unavailable for {mins} min. underflow is estimating it "
                       f"at {r.estimated[n]:.1f} °C from the other rooms (its usual offset from them), "
                       f"for up to {cfg.estimate_hours:g} h.")
            elif mins is None:
                msg = f"{n}'s room sensor hasn't reported since underflow started; comfort is judged without it."
            else:
                msg = (f"{n}'s room sensor has been unavailable for {mins / 60:.1f} h"
                       + (f", past the {cfg.estimate_hours:g} h estimate" if mins >= cfg.estimate_hours * 60
                          else " and no other room is reporting to estimate it from")
                       + "; comfort is judged without it.")
            self._room_alert(n, msg)
        for n in [n for n in self._room_alerts if n not in r.missing]:
            self.log(f"room {n} reporting again ({r.values.get(n)} °C)")
            del self._room_alerts[n]
            self.call_service("persistent_notification/dismiss", notification_id=self._room_alert_id(n))

    def _room_alert_id(self, room):
        who = f"{self.name}_" if self.name else ""
        return f"underflow_{who}room_" + "".join(c if c.isalnum() else "_" for c in room.lower())

    def _room_alert(self, room, message):
        who = f"{self.name} " if self.name else ""
        self.app.alert(f"underflow {who}room sensor offline", message, self._room_alert_id(room))

    # ---- observe -------------------------------------------------------
    def _observe(self):
        e = self.entities
        self._update_rooms(self.datetime(aware=True))
        obs = {"outside_temp_met_office": self._num(e.get("weather"), "temperature")}
        for n, ent in self.outside_sensors.items():
            obs[f"outside_temp_{n}_sensor"] = self._num(ent)
        # The one outside temperature the models and the narrative use (0.4.0): the
        # house's own sensor (`entities.outside_temp`; on Ben's site the lower of the two
        # Vaillant sensors), falling back to the Met Office's current reading.
        house = self._num(e.get("outside_temp"))
        obs["outside_temp"] = house if house is not None else obs["outside_temp_met_office"]
        obs["outside_temp_source"] = ("house" if house is not None else
                                      "met_office" if obs["outside_temp_met_office"] is not None else None)
        obs.update({
            "room_temp_mean": self._comfort_room(),
            "flow_temp": self._num(e.get("flow_temp")),
            "flow_temp_circuit": self._num(e.get("flow_temp_circuit")),
            "return_temp": self._num(e.get("return_temp")),
            "power_in_kw": self._num(e.get("power_in")),
            "power_out_kw": self._num(e.get("power_out")),
            "heat_curve": self._num(e.get("heat_curve")),
            "min_flow_temp": self._num(e.get("min_flow")),
            "max_flow_temp": self._num(e.get("max_flow")),
            "agile_rate_now": self._num(e.get("agile_rate")),
            "pump_status": self.get_state(e["pump_status"]) if e.get("pump_status") else None,
        })
        if self.room_reading is not None:
            rr = self.room_reading
            obs["rooms"] = rr.values
            obs["rooms_estimated"] = rr.estimated or None
            obs["rooms_missing_minutes"] = rr.missing or None
            obs["rooms_dropped"] = rr.dropped or None
            obs["rooms_stuck_hours"] = rr.stuck or None
        obs["mode"] = self._mode(obs["pump_status"])
        pin, pout = obs["power_in_kw"], obs["power_out_kw"]
        obs["cop_now"] = round(pout / pin, 2) if pin and pout and pin > 0.2 else None
        obs["dry_run"] = self.dry_run
        obs["version"] = self.version
        obs["numeric_stack"] = self.numeric_ok
        obs["last_run"] = self.datetime(aware=True).isoformat(timespec="seconds")
        return obs

    def _publish_status(self, obs, extra):
        state = "observing" if self.dry_run else "controlling"
        self._publish(
            self.status_entity,
            state,
            {"friendly_name": self.args.get("friendly_name", f"{self.label} heat pump controller"),
             "icon": "mdi:heat-pump", **({"pump": self.name} if self.name else {}), **obs, **extra},
        )
        return state

    # ---- decide: what should the register be? --------------------------
    def _apply_targets(self):
        if not self.target_helpers:
            return
        values = {k: self.get_state(ent) for k, ent in self.target_helpers.items()}
        self.planner_cfg, self.test_cfg, notes = targets.apply(
            self._base_planner_cfg, self._base_test_cfg, values)
        seen = (self.planner_cfg.comfort_min, self.planner_cfg.comfort_max,
                self.test_cfg.floor, self.test_cfg.ceiling, tuple(notes))
        if seen != self._targets_seen:
            self.log(f"targets: comfort {seen[0]:g}-{seen[1]:g}, test floor {seen[2]:g} "
                     f"ceiling {seen[3]}" + (f"; {'; '.join(notes)}" if notes else ""),
                     level="WARNING" if notes else "INFO")
            self._targets_seen = seen

    def decide(self):
        self._apply_targets()
        obs = self._observe()
        now = self.datetime(aware=True)
        rates = self._rates()
        result = plan(rates, now, obs.get("room_temp_mean"), self.planner_cfg)
        model_out = self._shadow_plan(obs, now, rates)

        proposed, source, reason = choose(self.held, result["setpoint_now"],
                                          obs.get("room_temp_mean"), self.planner_cfg,
                                          self.mirror_cfg, now)

        # The live model planner beats the price planner; without a plan this tick it
        # falls back to it, and says so.
        self.heat_off = False
        if self.model_live:
            m = from_model(model_out, obs.get("room_temp_mean"), self.planner_cfg)
            if m is not None:
                proposed, self.heat_off, reason, urgent = m
                source = "model"
                # A comfort override skips the dwell. The price planner's own flag says
                # whether *its* setpoint was overridden, which is not the question here.
                result = dict(result, urgent=urgent)
            else:
                why = ("no fitted parameters" if self.shadow is None else
                       "no room reading" if obs.get("room_temp_mean") is None else "no plan this tick")
                reason = f"model planner unavailable ({why}), so the price planner: {reason}"

        # A running step test overrides everything above, price included.
        self._update_step(obs, now)
        if self.step.active:
            proposed, source, reason = self.step.min_flow, "test", self.step.reason
            self.heat_off = False                # the test drives the zone itself
            result = dict(result, urgent=True)   # block edges must not wait out the dwell
        elif self.model_live and self.zone_entity:
            # Heat off is a true off: zone off, as in the step test. Anything else puts a
            # zone this pump switched off back on (also after a restart, when only the
            # zone's state says so).
            if self.heat_off:
                self.zone_desired = self.zone_off
            elif self.get_state(self.zone_entity) == self.zone_off:
                self.zone_desired = self.zone_on
        self.source, self.source_reason = source, reason

        # Deadband and minimum dwell: a "cheapest third" rule near its threshold flips as
        # the horizon rolls, and each flip is a write on a link that drops 30 times a
        # day. Comfort overrides skip the dwell.
        if proposed is not None and self.desired is not None:
            held_for = (now - self.desired_since).total_seconds() if self.desired_since else self.min_dwell
            if abs(proposed - self.desired) < self.deadband:
                proposed = self.desired
            elif held_for < self.min_dwell - self.dwell_slack and not result.get("urgent"):
                self.log(f"holding {self.desired:g} for another {(self.min_dwell - held_for) / 60:.0f} min "
                         f"rather than moving to {proposed:g} (dwell)")
                proposed = self.desired
        if proposed != self.desired:
            self.log(f"target {self.desired} -> {proposed} ({source}: {reason})")
            self.desired, self.desired_since = proposed, now
        self._decide_gate(obs, now)

        self._publish_plan(result, obs, now)
        self._publish_shadows(result, obs, now)
        self._reconcile(obs, now)

    def _rates(self):
        """Agile rates for this pump's meter, fetched once per tick by the site."""
        return self.app.rates(self.entities.get("agile_rates_today"),
                              self.entities.get("agile_rates_tomorrow"))

    def _publish_plan(self, result, obs, now):
        cfg = self.planner_cfg
        self._publish(
            self.plan_entity,
            self.desired if self.desired is not None else result["setpoint_now"],
            {
                "friendly_name": f"{self.label} planned min flow temp",
                "icon": "mdi:thermometer-water",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
                "reason": self.source_reason,
                "source": self.source,
                "planner_setpoint": result["setpoint_now"],
                "planner_reason": result["reason"],
                "mirror_value": self.held.value,
                "mirror_age_minutes": (round(self.held.age(now) / 60, 1)
                                       if self.held.age(now) is not None else None),
                "dry_run": self.dry_run,
                "model_live": self.model_live,
                "heat_off": self.heat_off,
                "cheap_threshold_gbp_kwh": result.get("threshold"),
                "cheap_slots": result.get("cheap_slots"),
                "horizon_slots": result.get("horizon_slots"),
                "baseline": cfg.baseline, "boost": cfg.boost,
                "comfort_min": cfg.comfort_min, "comfort_max": cfg.comfort_max,
                "slots": result["slots"],
            },
        )
        self.log(f"plan: min flow {self.desired} via {self.source} ({self.source_reason}); "
                 f"{result.get('cheap_slots')} cheap of {result.get('horizon_slots')} slots")
        test = ({"reason": self.step.reason, "ends": self.step.block.end if self.step.block else None}
                if self.step.active else None)
        gate = ({"open": self.gate_open, "reason": self.gate_reason, "dry_run": self.gate_dry_run,
                 "enabled": self._gate_enabled()} if self.gate_cfg is not None else None)
        model = ({"used": self.source == "model", "heat_off": self.heat_off,
                  "setpoint": self.desired, "reason": self.source_reason}
                 if self.model_live else None)
        lines = describe(obs, result, now, cfg, self.dry_run, test=test,
                         rooms=self.room_reading,
                         room_label=self.args.get("room_label", "the house"),
                         notes=self.args.get("notes"), gate=gate, model=model)
        self._publish(
            self.narrative_entity,
            summary(lines),
            {"friendly_name": f"{self.label}: what's going on", "icon": "mdi:text-long",
             "text": "\n".join(f"- {l}" for l in lines), "lines": lines,
             "updated": now.isoformat(timespec="seconds")},
        )

    def _publish_shadows(self, result, obs, now):
        """Compute and publish what the alternative demand sources would ask for.
        Nothing here touches `self.desired` or the bus."""
        room = obs.get("room_temp_mean")
        common = {"icon": "mdi:thermometer-water", "unit_of_measurement": "°C",
                  "device_class": "temperature", "state_class": "measurement",
                  "shadow": True, "room_temp": room, "actual_desired": self.desired,
                  "updated": now.isoformat(timespec="seconds")}

        if self.shared_sensor:
            sensor = self._num(self.shared_sensor)
            self.shared_value, why = shared_step(self.shared_value, sensor, room,
                                                 self.shared_cfg, self.planner_cfg)
            self._publish(self.shared_entity, self.shared_value, {
                **common, "friendly_name": f"{self.label} shadow: aim at shared sensor",
                "reason": why, "shared_sensor": self.shared_sensor,
                "shared_sensor_temp": sensor, "target": self.shared_cfg.target})

        if self.mirror_shadow_source:
            v = self._num(self.mirror_shadow_source)
            held = Held(v, now) if v is not None else Held()
            value, _, why = choose(held, result["setpoint_now"], room, self.planner_cfg,
                                   dataclasses.replace(self.mirror_cfg, enabled=True), now)
            self._publish(self.mirror_shadow_entity, value, {
                **common, "friendly_name": f"{self.label} shadow: copy the other half",
                "reason": why, "source_value": v})

    # ---- step test ------------------------------------------------------
    def _update_step(self, obs, now):
        prev = self.step
        # Latches are keyed to the block they tripped in and compared with the block
        # running *now* — not the previous tick's, which is empty after a restart.
        cur = current_block(self.test_cfg, now)
        step = test_step(self.test_cfg, now, obs.get("room_temp_mean"), self.planner_cfg.baseline,
                         guard_latched=persist.same_block(self._guard_block, cur),
                         warmest=self._warmest_room(),
                         cap_latched=persist.same_block(self._cap_block, cur))
        if step.block:
            k = persist.block_key(step.block)
            if step.guarded and not persist.same_block(self._guard_block, step.block):
                self.log(f"step test guard: {step.reason}", level="WARNING")
                self._alert(f"{self.label} test: {step.reason}")
                self._guard_block = k
            if step.capped and not persist.same_block(self._cap_block, step.block):
                self.log(f"step test ceiling: {step.reason}")
                self._cap_block = k
            elif not step.capped and persist.same_block(self._cap_block, step.block):
                self.log(f"step test ceiling: room back under, block resumes: {step.reason}")
                self._cap_block = None
        if step.active:
            if not prev.active or (prev.block and step.block and prev.block.index != step.block.index):
                self.log(f"step test: {step.reason}")
            self.zone_desired = {"off": self.zone_off, "day": self.zone_on}.get(step.zone, step.zone)
            self._test_was_active = True
        elif self._test_was_active:
            # Test over (or removed from config): put the zone back to normal once.
            self.log(f"step test finished; zone back to {self.zone_on}, planner resumes")
            self.zone_desired = self.zone_on
            self._test_was_active = False
        self.step = step

    def _reconcile_zone(self, signal_on, stable_for, now):
        """Keep the zone's mode where the test wants it. The script does the bus write and
        its own read-back; this only notices drift and asks again, at most every 3 min."""
        if not (self.zone_entity and self.zone_desired):
            return None, None
        current = self.get_state(self.zone_entity)
        if current == self.zone_desired:
            if self.zone_desired == self.zone_on and not self.step.active:
                self.zone_desired = None             # restored; stop managing the zone
            return current, "applied"
        if not signal_on or stable_for < self.reconcile_cfg.settle_seconds:
            return current, "waiting for a stable link"
        if self._zone_last_write and (now - self._zone_last_write).total_seconds() < 180:
            return current, "write in flight"
        self._zone_last_write = now
        if self.dry_run or not self.zone_script:
            self.log(f"DRY RUN: would set zone {current} -> {self.zone_desired}")
            return current, "dry run"
        self.log(f"WRITE: {self.zone_script} mode={self.zone_desired} (was {current})")
        self.call_service("script/turn_on", entity_id=self.zone_script,
                          variables={"mode": self.zone_desired})
        return current, "write"

    # ---- room thermostats (gate.py) --------------------------------------
    def _gate_enabled(self):
        ent = self.gate_cfg.enable_entity if self.gate_cfg else None
        return not ent or self.get_state(ent) != "off"

    def _decide_gate(self, obs, now):
        if self.gate_cfg is None:
            return
        c = self.planner_cfg
        d = G.wanted(self.gate_cfg, self.gate_open, obs.get("room_temp_mean"), self.desired,
                     c.baseline, c.comfort_min, c.comfort_max, self.step, heat_off=self.heat_off)
        if d.open == self.gate_open:
            self.gate_reason = d.reason
        elif G.may_change(self.gate_cfg, d, self.gate_open, self.gate_since, now, self.dwell_slack):
            self.log(f"gate: thermostats {'on' if d.open else 'off'} ({d.reason})")
            self.gate_open, self.gate_since, self.gate_reason = d.open, now, d.reason
        else:
            held = "on" if self.gate_open else "off"
            self.gate_reason = f"{d.reason}; held {held} for the dwell"
            self.log(f"gate: holding thermostats {held} rather than switching ({d.reason}; dwell)")

    def _reconcile_gate(self, now):
        """Keep each thermostat in the mode the gate wants: a HomeKit write if one has
        drifted (someone used the Tado app, a write was lost), at most every 3 min each.
        Turning one back on sends its last-seen setpoint too, since HomeKit forgets it
        while the thermostat is off (seen 13 Oct 2026)."""
        if self.gate_cfg is None:
            return {}
        g = self.gate_cfg
        enabled = self._gate_enabled()
        states, actions = {}, []
        for e in g.climates:
            st = self.get_state(e)
            states[e] = st
            if st == g.mode_on:
                sp = self._num(e, "temperature")
                if sp is not None:
                    self.gate_setpoints[e] = g.cap(e, sp)
            if self.gate_open is None:
                continue
            want = g.mode_on if self.gate_open else g.mode_off
            if st in (None, "unknown", "unavailable"):
                actions.append(f"{e} unavailable")
                continue
            if st == want:
                self._gate_misses.pop(e, None)
                continue
            if not enabled:
                actions.append(f"{e} is {st}; gate disabled")
                continue
            last = self._gate_last_write.get(e)
            if last and (now - last).total_seconds() < 180:
                actions.append(f"{e} write in flight")
                continue
            self._gate_last_write[e] = now
            sp = self.gate_setpoints.get(e, g.default_setpoint) if self.gate_open else None
            if self.gate_dry_run:
                self.log(f"DRY RUN: would set {e} {st} -> {want}" + (f" at {sp:g} °C" if sp is not None else ""))
                actions.append(f"{e} dry run")
                continue
            misses = self._gate_misses[e] = self._gate_misses.get(e, 0) + 1
            if misses == 4:
                self._alert(f"{e} is still {st} after 3 tries to set it {want}; "
                            f"is it reachable over HomeKit, or being changed by hand?")
            self.log(f"WRITE: {e} {st} -> {want}" + (f" at {sp:g} °C" if sp is not None else ""))
            if sp is not None:
                self.call_service("climate/set_temperature", entity_id=e, hvac_mode=want, temperature=sp)
            else:
                self.call_service("climate/set_hvac_mode", entity_id=e, hvac_mode=want)
            actions.append(f"{e} write")
        return {"gate_open": self.gate_open, "gate_reason": self.gate_reason,
                "gate_since": self.gate_since.isoformat(timespec="minutes") if self.gate_since else None,
                "gate_enabled": enabled, "gate_dry_run": self.gate_dry_run,
                "gate_states": states, "gate_setpoints": dict(self.gate_setpoints),
                "gate_action": "; ".join(actions) or ("applied" if self.gate_open is not None else "undecided")}

    def on_averaged(self, name, value, at):
        avg = self._averages.get(name)
        if avg is not None:
            avg.update(value, at)

    def _log_row(self, obs, now, zone_state, reading):
        if not self.log_dir:
            return
        # The decide tick also runs a reconcile; one row per 5-minute slot is enough.
        if self._last_log and (now - self._last_log).total_seconds() < 60:
            return
        self._last_log = now
        try:
            row = {"ts": now.isoformat(timespec="seconds")}
            for k in ("outside_temp", "outside_temp_met_office", *(f"outside_temp_{n}_sensor" for n in self.outside_sensors),
                      "room_temp_mean", "flow_temp", "flow_temp_circuit", "return_temp",
                      "power_in_kw", "power_out_kw", "heat_curve", "min_flow_temp", "max_flow_temp",
                      "agile_rate_now", "pump_status", "mode"):
                row[k] = obs.get(k)
            for n, spec in self.log_columns.items():
                ent, _, attr = str(spec).partition("|")
                row[n] = self.get_state(ent, attribute=attr) if attr else self.get_state(ent)
            for n, avg in self._averages.items():
                m = avg.take(now)
                row[n] = None if m is None else round(m, 1)
            for n, spec in self.log_bus.items():
                circuit, _, register = str(spec).partition(" ")
                r = self.bus.read_register(circuit, register, max_age=60) if self.bus else None
                row[n] = r.value if r is not None and r.ok else None
            row.update({
                "desired_min_flow": self.desired, "bus_min_flow": reading.value if reading else None,
                "source": self.source, "zone_desired": self.zone_desired, "zone_state": zone_state,
                "test_block": (self.step.block.index + 1) if self.step.block else None,
                "test_level": (self.step.block.level if self.step.block else None),
                "test_guarded": self.step.guarded if self.step.active else None,
                "test_capped": self.step.capped if self.step.active else None,
                "dry_run": self.dry_run,
                "rooms_estimated": ";".join(self.room_reading.estimated) if self.room_reading and self.room_reading.estimated else None,
            })
            if self.gate_cfg is not None:
                row["gate_open"] = self.gate_open
                row["gate_calling"] = sum(1 for e in self.gate_cfg.climates
                                          if self.get_state(e, attribute="hvac_action") == "heating")
            if self._last_shadow:
                row["model_level"] = self._last_shadow.get("state")
                row["model_pred_3h"] = self._last_shadow.get("predicted_coldest_3h")
                row["model_slab_est"] = self._last_shadow.get("slab_estimate")
            datalog.append(self.log_dir, self.log_prefix, now, row)
            self._last_row = row
        except Exception as exc:   # the log must never take the controller down
            self.log(f"datalog failed: {exc!r}", level="WARNING")

    # ---- restart tolerance ----------------------------------------------
    def _save_state(self, now):
        if not self.state_file:
            return
        try:
            st = {"desired": self.desired, "desired_since": self.desired_since,
                  "guard_block": self._guard_block, "cap_block": self._cap_block,
                  "test_was_active": self._test_was_active, "zone_desired": self.zone_desired,
                  "last_log": self._last_log}
            if self.gate_cfg is not None:
                st["gate"] = {"open": self.gate_open, "since": self.gate_since,
                              "setpoints": self.gate_setpoints}
            if self.room_tracker is not None:
                st["rooms"] = self.room_tracker.dump()
            if self.shadow is not None:
                kf, c = self.shadow.kf, self.shadow.cache
                st["kf"] = None if kf.x is None else {"x": kf.x.tolist(), "P": kf.P.tolist(),
                                                      "at": self.shadow.last_obs}
                st["forecast"] = {"met": persist.series_out(c.met), "met_at": c.met_at,
                                  "sol": persist.series_out(c.sol), "sol_at": c.sol_at}
            persist.save(self.state_file, st, now)
        except Exception as exc:          # never let persistence take control down
            self.log(f"state save failed: {exc!r}", level="WARNING")

    def _restore_state(self):
        if not self.state_file:
            return None
        now = self.datetime(aware=True)
        doc = persist.load(self.state_file, now)
        if not doc:
            self.log(f"no recent saved state at {self.state_file}; starting fresh")
            return None
        restored = []
        try:
            if doc.get("desired") is not None:
                self.desired = float(doc["desired"])
                self.desired_since = persist._dt(doc.get("desired_since")) or now
                restored.append(f"target {self.desired:g}")
            self._guard_block, self._cap_block = doc.get("guard_block"), doc.get("cap_block")
            if self._guard_block or self._cap_block:
                restored.append("test guard latches")
            self._test_was_active = bool(doc.get("test_was_active"))
            if doc.get("zone_desired"):
                self.zone_desired = doc["zone_desired"]
                restored.append(f"zone wanted {self.zone_desired}")
            self._last_log = persist._dt(doc.get("last_log"))
            gs = doc.get("gate")
            if self.gate_cfg is not None and gs:
                self.gate_open, self.gate_since = gs.get("open"), persist._dt(gs.get("since"))
                self.gate_setpoints = {k: self.gate_cfg.cap(k, float(v))
                                       for k, v in (gs.get("setpoints") or {}).items()
                                       if k in self.gate_cfg.climates}
                if self.gate_open is not None:
                    restored.append(f"thermostats {'on' if self.gate_open else 'off'}")
            if self.room_tracker is not None and doc.get("rooms"):
                self.room_tracker.load(doc["rooms"])
                restored.append("room offsets")
            kf = doc.get("kf")
            at = persist._dt(kf.get("at")) if kf else None
            if self.shadow is not None and kf and at and (now - at).total_seconds() < 3600:
                self.shadow.kf.x = np.array(kf["x"], float)
                self.shadow.kf.P = np.array(kf["P"], float)
                self.shadow.last_obs = at
                restored.append(f"slab estimate {self.shadow.slab:.1f}")
            fc = doc.get("forecast")
            if self.shadow is not None and fc:
                c = self.shadow.cache
                c.met, c.met_at = persist.series_in(fc.get("met")), persist._dt(fc.get("met_at"))
                c.sol, c.sol_at = persist.series_in(fc.get("sol")), persist._dt(fc.get("sol_at"))
                restored.append("forecasts")
        except Exception as exc:
            self.log(f"saved state unusable ({exc!r}); starting fresh", level="WARNING")
            return None
        age = (now - doc["saved_at"]).total_seconds() / 60
        self.log(f"restored state saved {age:.0f} min ago: {', '.join(restored) or 'nothing to restore'}")
        return doc["saved_at"]

    # ---- stage-2 model planner (shadow, or live with model.live) -----------
    def _shadow_observe(self, obs, now):
        if not self.shadow or not self._last_row:
            return
        try:
            row = self._last_row
            heat = None
            if obs.get("mode") == "heating":
                try:
                    heat = float(row.get(self.model_forecast.get("heat_column", "heat_out_kw_bus")))
                except (TypeError, ValueError):
                    heat = None
            pv = row.get("pv_w")
            self.shadow.observe(now, obs.get("room_temp_mean"), heat or 0.0,
                                obs.get("outside_temp"),
                                (float(pv) / 1000.0) if isinstance(pv, (int, float)) else 0.0)
        except Exception as exc:
            self.log(f"model observe failed: {exc!r}", level="WARNING")

    def _shadow_plan(self, obs, now, rates):
        """Plan, publish, and return this tick's plan (None if there is none)."""
        role = "live" if self.model_live else "shadow"
        if not self.shadow:
            if self.args.get("model"):
                self._publish(self.model_entity, "waiting", {
                    "friendly_name": f"{self.label} model planner ({role})", "icon": "mdi:brain",
                    "reason": "no fitted parameters yet (model.params)"})
            return None
        try:
            met, sol = self.app.forecasts(self.model_forecast.get("weather"),
                                          self.model_forecast.get("solcast", []))
            self.shadow.cache.offer(now, met=met or None, sol=sol or None)
            out = self.shadow.plan(now, rates, self.planner_cfg.comfort_min,
                                   obs.get("outside_temp"), comfort_top=self.planner_cfg.comfort_max)
            self._last_shadow = out
            state = out.pop("state")
            self._publish(self.model_entity, state, {
                "friendly_name": f"{self.label} model planner ({role})", "icon": "mdi:brain",
                "shadow": not self.model_live, "live": self.model_live,
                "updated": now.isoformat(timespec="seconds"),
                "floor": self.planner_cfg.comfort_min, **out})
            out["state"] = state
            for key, label in (("predicted_coldest_3h", "predicted coldest room in 3 h"),
                               ("slab_estimate", "estimated slab temperature")):
                if out.get(key) is not None:
                    self._publish(f"{self.model_entity}_{key}", out[key], {
                        "friendly_name": f"{self.label} model: {label}", "unit_of_measurement": "°C",
                        "device_class": "temperature", "state_class": "measurement",
                        "icon": "mdi:brain"})
            self.log(f"model ({role}): {state} now; coldest in 3 h {out.get('predicted_coldest_3h')} °C; "
                     f"24 h {out.get('elec_kwh_24h')} kWh £{out.get('cost_24h_gbp')}"
                     + (f"; {'; '.join(out['notes'])}" if out.get("notes") else ""))
            return out
        except Exception as exc:
            self.log(f"model plan failed: {exc!r}", level="WARNING")
            return None

    # ---- reconcile: make the register match ----------------------------
    def reconcile(self):
        self._reconcile(self._observe(), self.datetime(aware=True))

    def _reconcile(self, obs, now):
        # Refresh the demand signal first: it is read over 177's link, which is the bad
        # one, so a failure here must leave the last-good value standing, not clear it.
        mirror_reading = None
        if self.mirror_bus is not None:
            mirror_reading = self.mirror_bus.read_register(
                self.mirror_circuit, self.mirror_register, max_age=self.bus_max_age)
            self.held = hold(self.held, mirror_reading, now)

        reading = self._read_own_register(now)
        signal_on, stable_for = self._signal()
        decision, self.reg_state = decide(self.desired, reading, signal_on, stable_for,
                                          self.reg_state, now, self.reconcile_cfg)
        zone_state, zone_action = self._reconcile_zone(signal_on, stable_for, now)
        gate_attrs = self._reconcile_gate(now)

        extra = {
            "desired_min_flow": self.desired,
            "demand_source": self.source,
            "demand_reason": self.source_reason,
            "bus_value": reading.value if reading else None,
            "bus_error": reading.error if reading else None,
            "bus_fresh": bool(reading and reading.ok),
            "bus_value_age_s": (round(reading.age(now)) if reading and reading.age(now) is not None else None),
            "bus_signal": signal_on,
            "bus_signal_stable_s": None if stable_for == float("inf") else round(stable_for),
            "mirror_value": self.held.value,
            "mirror_error": mirror_reading.error if mirror_reading else None,
            "reconcile_action": decision.action,
            "reconcile_reason": decision.reason,
            "zone_state": zone_state,
            "zone_desired": self.zone_desired,
            "zone_action": zone_action,
            "test_active": self.step.active,
            "model_live": self.model_live,
            "heat_off": self.heat_off,
            "state_restored_from": self.restored.isoformat(timespec="minutes") if self.restored else None,
            "comfort_min": self.planner_cfg.comfort_min,
            "comfort_max": self.planner_cfg.comfort_max,
            "test_floor": self.test_cfg.floor if self.test_cfg.enabled else None,
            "test_ceiling": self.test_cfg.ceiling if self.test_cfg.enabled else None,
            "test_reason": self.step.reason if self.step.active else None,
            "test_ends": (self.test_cfg.end_at().isoformat(timespec="minutes")
                          if self.test_cfg.enabled and self.test_cfg.end_at() else None),
            **gate_attrs,
            **self.reg_state.as_attributes(),
        }
        self._publish_status(obs, extra)

        if decision.action != self._last_decision or decision.action == "write":
            self.log(f"reconcile: {decision.action} - {decision.reason}")
        self._last_decision = decision.action

        if decision.alert:
            self._alert(decision.alert)
        if decision.writes:
            # Since writes start the script without waiting (0.1.3), a reconcile right
            # after one still reads the old value; don't fire the same write twice while
            # the first (~30 s with read-back) is in flight.
            last = self._write_at
            if last and last[1] == self.desired and (now - last[0]).total_seconds() < 90:
                self.log(f"write of {self.desired} already in flight; not repeating")
            else:
                self._write(self.desired)
                self._write_at = (now, self.desired)
        self._log_row(obs, now, zone_state, reading)
        self._shadow_observe(obs, now)
        self._save_state(now)

    def _read_own_register(self, now):
        """Get the register we control, preferring ebusd's cache when its provenance is
        good enough. `find -V` costs nothing on the bus and returns the value *with* the
        time ebusd last had it off the wire, so we only pay for a real transaction when
        that is too old to act on — or when we have just written and need the wire."""
        if self.bus is None:
            return None
        if not self._force_next_read:
            found = self.bus.find_register(self.circuit, self.register)
            age = found.age(now)
            # -MAX_CLOCK_SKEW guards the same hazard as the probe: a clock running ahead
            # would make any stale value look new. Fall through to a real read instead.
            if found.ok and age is not None and -MAX_CLOCK_SKEW <= age <= self.bus_max_age:
                return found
        self._force_next_read = False
        return self.bus.read_register(self.circuit, self.register, max_age=0)

    def _write(self, value):
        if self.dry_run:
            self.log(f"DRY RUN: would call {self.write_script} flow_temp={value}")
            return
        if not self.write_script:
            self.log("no write_script configured; not writing", level="WARNING")
            return
        self.log(f"WRITE: {self.write_script} flow_temp={value}")
        # quiet=True: the script's own two-try alert would fire on every WiFi blip, and
        # this loop is the thing that retries. Sustained failure is alerted from here.
        # Started with script.turn_on, not called directly: the script's write + read-back
        # takes ~30 s, and calling it blocked this callback until HA's 10 s request
        # timeout on every write. The next reconcile reads the register off the wire.
        entity = self.write_script.replace("/", ".", 1) if "/" in self.write_script else self.write_script
        self.call_service("script/turn_on", entity_id=entity,
                          variables={"flow_temp": value, "quiet": True})
        # The next reconcile must see the wire, not a cache that predates the write.
        self._force_next_read = True

    def _alert(self, message):
        who = f"{self.name} " if self.name else ""
        self.app.alert(f"underflow {who}({self.register}) needs a look", message,
                       f"underflow_{who.strip() + '_' if who else ''}{self.register.lower()}")
