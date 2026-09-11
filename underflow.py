"""underflow — a Havenwise-style heat pump controller for Home Assistant + AppDaemon.

Stage 0/1 (this file): observe, and plan in dry run. Every `interval_minutes` it gathers the inputs the
controller will need — outside temperature, room temperature, flow/return, electrical
power in and heat power out, the heat curve and minimum-flow registers, the current
tariff rate — and publishes them as one sensor with attributes. Stage 1 (planner.py) then ranks the
upcoming Agile slots and publishes the minimum flow temperature the rules would set; with
`dry_run: true` (the default) it writes NOTHING. With dry_run false it calls the HA script
named in `write_script`, and only that script, when the setpoint changes.

Later stages add a fitted two-node (slab + air) RC house model, a Carnot-fraction COP
model fitted from the measured power, and a dynamic-programming optimiser that picks the
heat pump's *minimum flow temperature* per half-hour against tariff and weather forecast.
The only write path will be a Home Assistant script the user owns, so the bounds and the
read-back live in HA, not here.

All entity ids come from apps.yaml; nothing is hard-coded to a particular house.
"""
import datetime as dt

import appdaemon.plugins.hass.hassapi as hass

from planner import PlannerConfig, plan, normalise_rates
from narrative import describe, summary

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
        self._timer = None
        pc = self.args.get("planner", {}) or {}
        self.planner_cfg = PlannerConfig(**{k: v for k, v in pc.items() if k in PlannerConfig.__dataclass_fields__})
        self.plan_entity = self.args.get("plan_entity", self.status_entity + "_planned_min_flow")
        self.narrative_entity = self.args.get("narrative_entity", self.status_entity + "_whats_going_on")
        self.write_script = self.args.get("write_script")  # e.g. script/set_179_min_flow_temp
        self._last_written = None

        # First tick shortly after start, then on the half hour.
        now = self.datetime(aware=True)
        minutes = (self.interval - now.minute % self.interval) % self.interval or self.interval
        first = now.replace(second=5, microsecond=0) + dt.timedelta(minutes=minutes)
        self._timer = self.run_every(self.tick, first, self.interval * 60)
        self.run_in(self.tick, 10)
        self.log(f"initialised; dry_run={self.dry_run}; numeric stack: {NUMERIC_OK}")

    def terminate(self):
        if self._timer is not None:
            self.cancel_timer(self._timer)

    # ---- helpers -------------------------------------------------------
    def _num(self, entity_id, attribute=None):
        try:
            v = self.get_state(entity_id, attribute=attribute)
            return None if v in (None, "unknown", "unavailable", "") else float(v)
        except (TypeError, ValueError):
            return None

    # ---- main loop -----------------------------------------------------
    def tick(self, kwargs=None):
        e = self.entities
        obs = {
            "outside_temp_met_office": self._num(e["weather"], "temperature"),
            "outside_temp_179_sensor": self._num(e["outside_179"]),
            "outside_temp_177_sensor": self._num(e["outside_177"]),
            "room_temp_mean": self._num(e["room_mean"]),
            "flow_temp": self._num(e["flow_temp"]),
            "return_temp": self._num(e["return_temp"]),
            "power_in_kw": self._num(e["power_in"]),
            "power_out_kw": self._num(e["power_out"]),
            "heat_curve": self._num(e["heat_curve"]),
            "min_flow_temp": self._num(e["min_flow"]),
            "max_flow_temp": self._num(e["max_flow"]),
            "agile_rate_now": self._num(e["agile_rate"]),
            "pump_status": self.get_state(e["pump_status"]),
        }
        pin, pout = obs["power_in_kw"], obs["power_out_kw"]
        obs["cop_now"] = round(pout / pin, 2) if pin and pout and pin > 0.2 else None
        obs["dry_run"] = self.dry_run
        obs["numeric_stack"] = NUMERIC_OK
        obs["last_run"] = self.datetime(aware=True).isoformat(timespec="seconds")

        state = "observing" if self.dry_run else "controlling"
        self.set_state(
            self.status_entity,
            state=state,
            attributes={"friendly_name": self.args.get("friendly_name", "underflow heat pump controller"), "icon": "mdi:heat-pump", **obs},
        )
        self.log(
            f"{state}: out={obs['outside_temp_met_office']} room={obs['room_temp_mean']} "
            f"flow={obs['flow_temp']} minflow={obs['min_flow_temp']} "
            f"P={pin}/{pout} kW cop={obs['cop_now']} agile={obs['agile_rate_now']}"
        )
        self.run_planner(obs)

    # ---- stage 1: rule-based Agile shifting ---------------------------------
    def _rates(self):
        raw = []
        for key in ("agile_rates_today", "agile_rates_tomorrow"):
            ent = self.entities.get(key)
            if ent:
                r = self.get_state(ent, attribute="rates")
                if isinstance(r, list):
                    raw.extend(r)
        return normalise_rates(raw)

    def run_planner(self, obs):
        cfg = self.planner_cfg
        now = self.datetime(aware=True)
        result = plan(self._rates(), now, obs.get("room_temp_mean"), cfg)
        setpoint = result["setpoint_now"]
        self.set_state(
            self.plan_entity,
            state=setpoint,
            attributes={
                "friendly_name": "underflow planned min flow temp",
                "icon": "mdi:thermometer-water",
                "unit_of_measurement": "\u00b0C",
                "device_class": "temperature",
                "state_class": "measurement",
                "reason": result["reason"],
                "dry_run": self.dry_run,
                "cheap_threshold_gbp_kwh": result.get("threshold"),
                "cheap_slots": result.get("cheap_slots"),
                "horizon_slots": result.get("horizon_slots"),
                "baseline": cfg.baseline, "boost": cfg.boost,
                "comfort_min": cfg.comfort_min, "comfort_max": cfg.comfort_max,
                "slots": result["slots"],
            },
        )
        self.log(f"plan: min flow {setpoint} ({result['reason']}); {result.get('cheap_slots')} cheap of {result.get('horizon_slots')} slots")
        lines = describe(obs, result, now, cfg, self.dry_run)
        self.set_state(
            self.narrative_entity,
            state=summary(lines),
            attributes={"friendly_name": "underflow: what's going on", "icon": "mdi:text-long",
                        "text": "\n".join(f"- {l}" for l in lines), "lines": lines,
                        "updated": now.isoformat(timespec="seconds")},
        )
        if self.dry_run or not self.write_script:
            return
        if self._last_written == setpoint and obs.get("min_flow_temp") == setpoint:
            return
        self.log(f"WRITE: {self.write_script} flow_temp={setpoint}")
        self.call_service(self.write_script, flow_temp=setpoint)
        self._last_written = setpoint
