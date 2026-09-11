"""underflow — a Havenwise-style heat pump controller for Home Assistant + AppDaemon.

Stage 0 (this file): observe only. Every `interval_minutes` it gathers the inputs the
controller will need — outside temperature, room temperature, flow/return, electrical
power in and heat power out, the heat curve and minimum-flow registers, the current
tariff rate — and publishes them as one sensor with attributes. It writes NOTHING.

Later stages add a fitted two-node (slab + air) RC house model, a Carnot-fraction COP
model fitted from the measured power, and a dynamic-programming optimiser that picks the
heat pump's *minimum flow temperature* per half-hour against tariff and weather forecast.
The only write path will be a Home Assistant script the user owns, so the bounds and the
read-back live in HA, not here.

All entity ids come from apps.yaml; nothing is hard-coded to a particular house.
"""
import datetime as dt

import appdaemon.plugins.hass.hassapi as hass

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
