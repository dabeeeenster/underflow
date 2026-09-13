"""underflow — a Havenwise-style heat pump controller for Home Assistant + AppDaemon.

Stage 0/1 (this file): observe, plan, and reconcile the one register we control.

**Three clocks, not one.** The actuator is a concrete slab with a multi-hour time
constant sitting behind a 2.4 GHz radio that drops out several times an hour, so
deciding and applying want quite different cadences:

    decide      every `interval_minutes` (30), on the half hour   -> sets `desired`
    reconcile   every `reconcile_minutes` (5)                     -> makes it true
    observe     every reconcile tick                              -> publishes state

Decisions stay on the Agile slot boundary because that is the fastest the price signal
moves, and because every change of mind costs a bus write on a lossy link; a deadband
and a minimum dwell keep normal operation to about two writes an hour. Applying runs six
times as often because a *lost* write is the thing that actually costs heat: at a 30 min
reconcile a dropped boost sits wrong for an entire Agile slot, at 5 min for a sixth of
one. Reads are free, local and take milliseconds, so there is no reason to be stingy
with them.

**Verification does not go through Home Assistant.** ebusd publishes to MQTT only on
change, so HA's `number.*` state is a cache with no expiry and reads the same whether
the bus is healthy or the adapter has been dead for a quarter of an hour. `ebusd.py`
forces a real bus read instead; see its docstring for the measurements.

Writes still go out through a bounded HA script with its own read-back, so the limits
live somewhere the user can see them. This module never writes a register directly.

Later stages add a fitted two-node (slab + air) RC house model, a Carnot-fraction COP
model fitted from the measured power, and a dynamic-programming optimiser that picks the
minimum flow temperature per half-hour against tariff and weather forecast.

All entity ids come from apps.yaml; nothing is hard-coded to a particular house.
"""
import datetime as dt

import appdaemon.plugins.hass.hassapi as hass

from planner import PlannerConfig, plan, normalise_rates
from narrative import describe, summary
from hasafe import ha_safe
from ebusd import Ebusd
from reconcile import ReconcileConfig, RegisterState, decide
from demand import Held, MirrorConfig, choose, hold

try:
    import numpy as np
    import scipy
    NUMERIC_OK = f"numpy {np.__version__}, scipy {scipy.__version__}"
except Exception as exc:  # pragma: no cover
    NUMERIC_OK = f"missing: {exc}"


class Underflow(hass.Hass):
    def initialize(self):
        self.entities = self.args["entities"]
        self.status_entity = self.args.get("status_entity", "sensor.underflow")
        self.dry_run = bool(self.args.get("dry_run", True))
        self.interval = int(self.args.get("interval_minutes", 30))
        self.reconcile_interval = int(self.args.get("reconcile_minutes", 5))
        self.min_dwell = float(self.args.get("min_dwell_minutes", 30)) * 60
        self.deadband = float(self.args.get("deadband", 0.5))
        self._timers = []

        pc = self.args.get("planner", {}) or {}
        self.planner_cfg = PlannerConfig(**{k: v for k, v in pc.items() if k in PlannerConfig.__dataclass_fields__})
        self.plan_entity = self.args.get("plan_entity", self.status_entity + "_planned_min_flow")
        self.narrative_entity = self.args.get("narrative_entity", self.status_entity + "_whats_going_on")
        self.write_script = self.args.get("write_script")  # e.g. script/set_179_min_flow_temp
        self.alert_service = self.args.get("alert_service")  # e.g. notify/mobile_app_...

        # --- the register we own, and how we verify it
        eb = self.args.get("ebusd", {}) or {}
        self.circuit = eb.get("circuit", "ctlv2")
        self.register = eb.get("register", "Hc1MinFlowTempDesired")
        self.bus = Ebusd(eb["host"], eb["port"], float(eb.get("timeout", 4.0))) if eb.get("host") else None
        self.signal_entity = eb.get("signal_entity")
        rc = self.args.get("reconcile", {}) or {}
        self.reconcile_cfg = ReconcileConfig(**{k: v for k, v in rc.items() if k in ReconcileConfig.__dataclass_fields__})
        self.reg_state = RegisterState()

        # --- the demand source (177's Havenwise decisions, once they exist)
        mc = dict(self.args.get("mirror", {}) or {})
        self.mirror_host, self.mirror_port = mc.pop("host", None), mc.pop("port", None)
        self.mirror_circuit = mc.pop("circuit", "ctlv2")
        self.mirror_register = mc.pop("register", "Hc2MinFlowTempDesired")
        mc.pop("timeout", None)
        self.mirror_cfg = MirrorConfig(**{k: v for k, v in mc.items() if k in MirrorConfig.__dataclass_fields__})
        self.mirror_bus = (Ebusd(self.mirror_host, self.mirror_port, float(eb.get("timeout", 4.0)))
                           if self.mirror_cfg.enabled and self.mirror_host else None)
        self.held = Held()

        self.desired = self._recover_desired()
        self.desired_since = self.datetime(aware=True) if self.desired is not None else None
        self.source, self.source_reason = "none", "starting up"
        self._unknown_codes = set()
        self._last_decision = None

        self._schedule(self.on_decide, self.interval)
        self._schedule(self.on_reconcile, self.reconcile_interval)
        self.run_in(self.on_decide, 10)
        self.log(f"initialised; dry_run={self.dry_run}; decide/{self.interval}min "
                 f"reconcile/{self.reconcile_interval}min; recovered desired={self.desired}; "
                 f"numeric stack: {NUMERIC_OK}")

    def terminate(self):
        for t in self._timers:
            self.cancel_timer(t)

    # ---- helpers -------------------------------------------------------
    def _schedule(self, callback, minutes):
        """Run `callback` every `minutes`, aligned to the wall clock so decisions land on
        the Agile slot boundary rather than wherever the app happened to restart."""
        now = self.datetime(aware=True)
        offset = (minutes - now.minute % minutes) % minutes or minutes
        first = now.replace(second=5, microsecond=0) + dt.timedelta(minutes=offset)
        self._timers.append(self.run_every(callback, first, minutes * 60))

    def _recover_desired(self):
        """An AppDaemon restart must not forget the target and rewrite blind. The plan
        sensor already holds it, so read it back."""
        try:
            v = self.get_state(self.plan_entity)
            return None if v in (None, "unknown", "unavailable", "") else float(v)
        except (TypeError, ValueError):
            return None

    def _num(self, entity_id, attribute=None):
        try:
            v = self.get_state(entity_id, attribute=attribute)
            return None if v in (None, "unknown", "unavailable", "") else float(v)
        except (TypeError, ValueError):
            return None

    def _publish(self, entity_id, state, attributes):
        """set_state with every value run through ha_safe(). See its docstring."""
        self.set_state(entity_id, state=ha_safe(state), attributes=ha_safe(attributes))

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

        The codes look like 'standby', 'hwc_compressor_active', and (once the heating
        season starts) an 'hc'/'heating' equivalent. This matters because a cylinder
        charge runs the flow to 60-70 C, a completely different operating point from
        30 C space heating: mixing the two fits the COP model to the wrong regime.
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

    # ---- observe -------------------------------------------------------
    def _observe(self):
        e = self.entities
        obs = {
            "outside_temp_met_office": self._num(e["weather"], "temperature"),
            "outside_temp_179_sensor": self._num(e["outside_179"]),
            "outside_temp_177_sensor": self._num(e["outside_177"]),
            "room_temp_mean": self._num(e["room_mean"]),
            "flow_temp": self._num(e["flow_temp"]),
            "flow_temp_circuit": self._num(e["flow_temp_circuit"]) if e.get("flow_temp_circuit") else None,
            "return_temp": self._num(e["return_temp"]),
            "power_in_kw": self._num(e["power_in"]),
            "power_out_kw": self._num(e["power_out"]),
            "heat_curve": self._num(e["heat_curve"]),
            "min_flow_temp": self._num(e["min_flow"]),
            "max_flow_temp": self._num(e["max_flow"]),
            "agile_rate_now": self._num(e["agile_rate"]),
            "pump_status": self.get_state(e["pump_status"]),
        }
        obs["mode"] = self._mode(obs["pump_status"])
        pin, pout = obs["power_in_kw"], obs["power_out_kw"]
        obs["cop_now"] = round(pout / pin, 2) if pin and pout and pin > 0.2 else None
        obs["dry_run"] = self.dry_run
        obs["numeric_stack"] = NUMERIC_OK
        obs["last_run"] = self.datetime(aware=True).isoformat(timespec="seconds")
        return obs

    def _publish_status(self, obs, extra):
        state = "observing" if self.dry_run else "controlling"
        self._publish(
            self.status_entity,
            state,
            {"friendly_name": self.args.get("friendly_name", "underflow heat pump controller"),
             "icon": "mdi:heat-pump", **obs, **extra},
        )
        return state

    # ---- decide: what should the register be? --------------------------
    def on_decide(self, kwargs=None):
        obs = self._observe()
        now = self.datetime(aware=True)
        result = plan(self._rates(), now, obs.get("room_temp_mean"), self.planner_cfg)

        proposed, source, reason = choose(self.held, result["setpoint_now"],
                                          obs.get("room_temp_mean"), self.planner_cfg,
                                          self.mirror_cfg, now)
        self.source, self.source_reason = source, reason

        # Deadband and minimum dwell: a "cheapest third" rule near its threshold flips as
        # the horizon rolls, and each flip is a write on a link that drops 30 times a
        # day. Comfort overrides skip the dwell.
        if proposed is not None and self.desired is not None:
            held_for = (now - self.desired_since).total_seconds() if self.desired_since else self.min_dwell
            if abs(proposed - self.desired) < self.deadband:
                proposed = self.desired
            elif held_for < self.min_dwell and not result.get("urgent"):
                self.log(f"holding {self.desired:g} for another {(self.min_dwell - held_for) / 60:.0f} min "
                         f"rather than moving to {proposed:g} (dwell)")
                proposed = self.desired
        if proposed != self.desired:
            self.log(f"target {self.desired} -> {proposed} ({source}: {reason})")
            self.desired, self.desired_since = proposed, now

        self._publish_plan(result, obs, now)
        self._reconcile(obs, now)

    def _rates(self):
        raw = []
        for key in ("agile_rates_today", "agile_rates_tomorrow"):
            ent = self.entities.get(key)
            if ent:
                r = self.get_state(ent, attribute="rates")
                if isinstance(r, list):
                    raw.extend(r)
        return normalise_rates(raw)

    def _publish_plan(self, result, obs, now):
        cfg = self.planner_cfg
        self._publish(
            self.plan_entity,
            self.desired if self.desired is not None else result["setpoint_now"],
            {
                "friendly_name": "underflow planned min flow temp",
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
        lines = describe(obs, result, now, cfg, self.dry_run)
        self._publish(
            self.narrative_entity,
            summary(lines),
            {"friendly_name": "underflow: what's going on", "icon": "mdi:text-long",
             "text": "\n".join(f"- {l}" for l in lines), "lines": lines,
             "updated": now.isoformat(timespec="seconds")},
        )

    # ---- reconcile: make the register match ----------------------------
    def on_reconcile(self, kwargs=None):
        self._reconcile(self._observe(), self.datetime(aware=True))

    def _reconcile(self, obs, now):
        # Refresh the demand signal first: it is read over 177's link, which is the bad
        # one, so a failure here must leave the last-good value standing, not clear it.
        mirror_reading = None
        if self.mirror_bus is not None:
            mirror_reading = self.mirror_bus.read_register(self.mirror_circuit, self.mirror_register)
            self.held = hold(self.held, mirror_reading, now)

        reading = self.bus.read_register(self.circuit, self.register) if self.bus else None
        signal_on, stable_for = self._signal()
        decision, self.reg_state = decide(self.desired, reading, signal_on, stable_for,
                                          self.reg_state, now, self.reconcile_cfg)

        extra = {
            "desired_min_flow": self.desired,
            "demand_source": self.source,
            "demand_reason": self.source_reason,
            "bus_value": reading.value if reading else None,
            "bus_error": reading.error if reading else None,
            "bus_fresh": bool(reading and reading.ok),
            "bus_signal": signal_on,
            "bus_signal_stable_s": None if stable_for == float("inf") else round(stable_for),
            "mirror_value": self.held.value,
            "mirror_error": mirror_reading.error if mirror_reading else None,
            "reconcile_action": decision.action,
            "reconcile_reason": decision.reason,
            **self.reg_state.as_attributes(),
        }
        self._publish_status(obs, extra)

        if decision.action != self._last_decision or decision.action == "write":
            self.log(f"reconcile: {decision.action} — {decision.reason}")
        self._last_decision = decision.action

        if decision.alert:
            self._alert(decision.alert)
        if decision.writes:
            self._write(self.desired)

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
        self.call_service(self.write_script, flow_temp=value, quiet=True)

    def _alert(self, message):
        self.log(f"ALERT: {message}", level="WARNING")
        title = f"underflow ({self.register}) needs a look"
        self.call_service("persistent_notification/create", title=title, message=message,
                          notification_id=f"underflow_{self.register.lower()}")
        if self.alert_service:
            self.call_service(self.alert_service, title=title, message=message)
