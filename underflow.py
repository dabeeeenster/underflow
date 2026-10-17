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
one. Reads are cheap *when they come from ebusd's cache* — 2.5 ms against 210-390 ms for
a forced bus transaction — which is why nothing here forces one except the read that
confirms a write.

**Verification does not go through Home Assistant.** ebusd publishes to MQTT only on
change, so HA's `number.*` state is a cache with no expiry and reads the same whether
the bus is healthy or the adapter has been dead for a quarter of an hour. `ebusd.py` asks
ebusd instead, which knows the true age of what it holds: `read -m SECONDS` answers from
cache only if it is young enough and otherwise goes to the wire. That distinction is the
whole point — and it is why liveness checking never needs to force, since a stale cache
is itself the evidence. See `ebusd.py` for the measurements.

Writes still go out through a bounded HA script with its own read-back, so the limits
live somewhere the user can see them. This module never writes a register directly.

Later stages add a fitted two-node (slab + air) RC house model, a Carnot-fraction COP
model fitted from the measured power, and a dynamic-programming optimiser that picks the
minimum flow temperature per half-hour against tariff and weather forecast.

**One app, several pumps (0.3.0).** This class is the site: the clocks, Agile prices,
forecasts, liveness probes and alerts. Each heat pump is a `pump.Pump` built from an
entry under `pumps:`, with the site-wide keys in `INHERITED` as its defaults. The clocks
run every pump in turn, each inside its own try/except, so a pump that raises never stops
the other. With no `pumps:` key, the whole of apps.yaml is one unnamed pump and every
published name is what it was before.

All entity ids come from apps.yaml; nothing is hard-coded to a particular house.
"""
import copy
import datetime as dt
import traceback

import appdaemon.plugins.hass.hassapi as hass

from planner import normalise_rates
from hasafe import ha_safe
from ebusd import Ebusd
from probe import ProbeState, ProbeTarget, summarise, update
from pump import Pump, PumpRefused
import forecast as F

try:
    import numpy as np
    import scipy
    NUMERIC_OK = f"numpy {np.__version__}, scipy {scipy.__version__}"
except Exception as exc:  # pragma: no cover
    NUMERIC_OK = f"missing: {exc}"


# Bump on every release (see README, "Releases") and add a CHANGELOG.md entry. Published
# on the status sensor so the dashboard shows which build is actually running.
__version__ = "0.7.1"

# Site-wide keys a pump inherits unless its own entry says otherwise; dicts merge, so
# a pump adds its own `entities` or `datalog.columns` to the shared ones. Everything else
# (planner, test, zone, model, ebusd, mirror, targets, state_file, ...) belongs to one pump
# and is never inherited: a step test or a register must not leak to the other half.
INHERITED = ("dry_run", "min_dwell_minutes", "deadband", "reconcile", "entities", "datalog")

# A pump that raises this many ticks in a row (5-minute reconcile) gets an alert.
FAIL_ALERT_TICKS = 6


def merge(base: dict, over: dict) -> dict:
    """`over` on top of `base`, recursing into dicts; lists and scalars replace."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def pump_configs(args: dict) -> list[tuple]:
    """[(name, config)] from apps.yaml. No `pumps:` key = one unnamed pump, the whole file."""
    pumps = args.get("pumps")
    if not pumps:
        return [(args.get("name"), dict(args))]
    site = {k: args[k] for k in INHERITED if k in args}
    return [(str(name), merge(site, cfg or {})) for name, cfg in pumps.items()]


class Underflow(hass.Hass):
    def initialize(self):
        self.interval = int(self.args.get("interval_minutes", 30))
        self.reconcile_interval = int(self.args.get("reconcile_minutes", 5))
        self.alert_service = self.args.get("alert_service")  # e.g. notify/mobile_app_...
        self._timers = []
        self._memo = {}
        self._forecast_err = None

        # --- liveness probes: is each device still answering on its bus?
        # Not a controller concern, but this app is the only thing with an ebusd client
        # and a 5-minute cycle, and the alternative (inferring death from how long an HA
        # entity has been unchanged) false-alarms every time a pump sits in standby.
        pb = self.args.get("probes", {}) or {}
        self.probe_entity = pb.get("entity", "sensor.underflow_bus_probe")
        self.probe_dead_after = int(pb.get("dead_after", 3))
        self.probe_timeout = float(pb.get("timeout", 3.0))
        # Liveness is read from ebusd's own `lastup`, which puts NOTHING on the bus.
        # A device counts as answering while ebusd has had its value off the wire within
        # stale_after; the targets are registers ebusd polls every ~10-15 s by itself.
        self.probe_stale_after = float(pb.get("stale_after", 300.0))
        # Once a device is known down, stop probing it every cycle. A failing link is the
        # last thing to add traffic to, and recovery within a few cycles is soon enough.
        self.probe_down_every = int(pb.get("down_every_cycles", 3))
        self._probe_cycle = 0
        self.probes = [ProbeTarget(**t) for t in (pb.get("targets") or [])]
        self._probe_clients = {}
        for t in self.probes:
            self._probe_clients.setdefault((t.host, t.port),
                                           Ebusd(t.host, t.port, self.probe_timeout))
        self.probe_states = {t.name: ProbeState() for t in self.probes}

        # --- the pumps. One that is misconfigured (or refuses its config) is left out
        # and said so loudly; the others still start.
        self.pumps = {}
        for name, cfg in pump_configs(self.args):
            try:
                p = Pump(self, name, cfg, __version__, NUMERIC_OK)
            except PumpRefused as exc:
                self._refuse(name, cfg, f"refused to start: {exc}")
                continue
            except Exception as exc:
                self.log(traceback.format_exc(), level="ERROR")
                self._refuse(name, cfg, f"failed to start: {exc!r}")
                continue
            if any(q.status_entity == p.status_entity for q in self.pumps.values()):
                self._refuse(name, cfg, f"status_entity {p.status_entity} is already another pump's")
                continue
            self.pumps[name] = p
            # A slider moved: re-decide that pump only.
            for ent in p.target_helpers.values():
                self.listen_state(self._on_target_change, ent, pump=name)
            for avg_name, ent in p.log_averaged.items():
                self.listen_state(self._on_averaged, ent, pump=name, avg=avg_name)

        self._schedule(self.on_decide, self.interval)
        self._schedule(self.on_reconcile, self.reconcile_interval)
        self.run_in(self.on_decide, 10)
        self.log(f"initialised v{__version__}; pumps={[p.name for p in self.pumps.values()]}; "
                 f"decide/{self.interval}min reconcile/{self.reconcile_interval}min; "
                 f"probes={len(self.probes)}; numeric stack: {NUMERIC_OK}")

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

    def _publish(self, entity_id, state, attributes):
        """set_state with every value run through ha_safe(). See its docstring."""
        self.set_state(entity_id, state=ha_safe(state), attributes=ha_safe(attributes))

    def _refuse(self, name, cfg, why):
        label = f"underflow {name}" if name else "underflow"
        self.log(f"{label} {why}", level="ERROR")
        self.alert(f"{label} is not running", why, f"underflow_{name or 'app'}_refused")
        status = cfg.get("status_entity", f"sensor.underflow_{name}" if name else "sensor.underflow")
        self._publish(status, "refused", {"friendly_name": cfg.get("friendly_name", label),
                                          "icon": "mdi:alert", "reason": why, "version": __version__})

    def alert(self, title, message, notification_id):
        self.log(f"ALERT: {message}", level="WARNING")
        self.call_service("persistent_notification/create", title=title, message=message,
                          notification_id=notification_id)
        if self.alert_service:
            self.call_service(self.alert_service, title=title, message=message)

    def _memoised(self, key, fetch, ttl=120):
        """The pumps run one after another inside a tick; share what they both read."""
        now = self.datetime(aware=True)
        hit = self._memo.get(key)
        if hit and (now - hit[0]).total_seconds() < ttl:
            return hit[1]
        value = fetch()
        self._memo[key] = (now, value)
        return value

    # ---- shared inputs ---------------------------------------------------
    def rates(self, today, tomorrow):
        def fetch():
            raw = []
            for ent in (today, tomorrow):
                if ent:
                    r = self.get_state(ent, attribute="rates")
                    if isinstance(r, list):
                        raw.extend(r)
            return normalise_rates(raw)
        return self._memoised(("rates", today, tomorrow), fetch)

    def forecasts(self, weather, solcast):
        """(Met Office hourly temperatures, Solcast PV) for the model planners."""
        def fetch():
            met = None
            if weather:
                try:
                    resp = self.call_service("weather/get_forecasts", entity_id=weather, type="hourly",
                                             return_response=True)
                    met = F.parse_met(F.find_forecast_list(resp))
                    if not met:
                        raise ValueError(f"no hourly forecast in response ({type(resp).__name__})")
                except Exception as exc:
                    if repr(exc) != self._forecast_err:
                        self.log(f"temperature forecast unavailable: {exc!r}", level="WARNING")
                        self._forecast_err = repr(exc)
            lists = [self.get_state(e, attribute="detailedForecast") for e in solcast or []]
            sol = F.parse_solcast(*[l for l in lists if isinstance(l, list)])
            return met, sol
        return self._memoised(("forecasts", weather, tuple(solcast or [])), fetch)

    # ---- the clocks ------------------------------------------------------
    def _each(self, what, pumps=None):
        """Run `what` on each pump; one raising is logged, counted and alerted on, and
        never stops the next."""
        for p in (pumps if pumps is not None else list(self.pumps.values())):
            try:
                getattr(p, what)()
                p.failures = 0
            except Exception as exc:
                p.failures += 1
                self.log(f"{p.label} {what} failed ({p.failures} in a row): "
                         f"{traceback.format_exc()}", level="ERROR")
                if p.failures == FAIL_ALERT_TICKS:
                    self.alert(f"{p.label} keeps failing",
                               f"{what} has raised {p.failures} times in a row: {exc!r}",
                               f"underflow_{p.name or 'app'}_failing")

    def on_decide(self, kwargs=None):
        self._each("decide")
        self._run_probes(self.datetime(aware=True))

    def on_reconcile(self, kwargs=None):
        self._each("reconcile")
        self._run_probes(self.datetime(aware=True))

    def _decide_one(self, kwargs):
        p = self.pumps.get(kwargs.get("pump"))
        if p is not None:
            self._each("decide", [p])

    def _on_target_change(self, entity, attribute, old, new, kwargs):
        """A slider moved: decide that pump now rather than at the next half hour. Comfort
        moves are urgent in the planner, so they skip the minimum dwell."""
        if new != old and new not in (None, "unknown", "unavailable"):
            self.log(f"target changed: {entity} {old} -> {new}; deciding {kwargs.get('pump') or 'now'}")
            self.run_in(self._decide_one, 2, pump=kwargs.get("pump"))

    def _on_averaged(self, entity, attribute, old, new, kwargs):
        p = self.pumps.get(kwargs.get("pump"))
        if p is not None:
            p.on_averaged(kwargs.get("avg"), new, self.datetime(aware=True))

    # ---- liveness probes -----------------------------------------------
    def _run_probes(self, now):
        """Ask ebusd when it last had each device's value off the wire (`find -V`) and
        publish a debounced alive/dead verdict.

        This is a query to ebusd's cache, not a bus transaction, so the probes run back
        to back. A single miss is a WiFi blip; `dead_after` consecutive ones is a device
        gone.
        """
        if not self.probes:
            return
        self._probe_cycle += 1
        for t in self.probes:
            was_dead = self.probe_states[t.name].dead(self.probe_dead_after)
            if was_dead and self._probe_cycle % self.probe_down_every:
                continue          # already down: don't hammer a failing link every cycle
            reading = self._probe_clients[(t.host, t.port)].find_register(t.circuit, t.register)
            self.probe_states[t.name] = update(
                self.probe_states[t.name], reading, now, self.probe_stale_after)
            is_dead = self.probe_states[t.name].dead(self.probe_dead_after)
            if is_dead != was_dead:
                self.log(f"probe {t.name}: {'NOT ANSWERING' if is_dead else 'answering again'}"
                         f" ({t.circuit} {t.register})", level="WARNING" if is_dead else "INFO")
        state, attrs = summarise(self.probe_states, self.probe_dead_after, now)
        attrs["stale_after_seconds"] = self.probe_stale_after
        self._publish(self.probe_entity, state,
                      {"friendly_name": "underflow eBUS device probe", "icon": "mdi:lan-check", **attrs})
