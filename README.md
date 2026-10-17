# underflow

A local, open-source controller for a Vaillant aroTHERM heat pump in an all-underfloor
house, built on Home Assistant, AppDaemon and ebusd. It learns how the house and the
heat pump behave, then sets the pump's **minimum flow temperature** for each half-hour
against the Octopus Agile tariff and the weather forecast, using the concrete slab to
store heat.

**Status: stage 2, live on the author's house** (October 2026). One app drives two
aroTHERMs. On one, a model planner plans the next 24 h against the weather, sun and price
forecasts; the other runs on simpler price rules until it has a fitted model. Everything
defaults to `dry_run: true`. See [Roadmap](#roadmap).

## Why

[Havenwise](https://www.havenwise.co.uk/) does this for £7/month, through the
manufacturer's cloud. As of September 2026 no open-source project does the whole job:

- Predbat's Predheat forecasts but doesn't control.
- EMHASS optimises electrical power but never picks a flow temperature.
- SAT tunes heating curves for boilers over OpenTherm.

underflow runs entirely on your LAN against ebusd, so there is no cloud API limit.

## How it works

Three small physical models, each with a few parameters. A few weeks of data won't
train a black-box model that copes with a −3 °C night it has never seen; a two-node
thermal model with five parameters will.

**House.** Two thermal masses, the slab and the room air:

```
C_slab · dT_slab/dt = Q_in + g_slab · solar − (T_slab − T_room) / R_sr
C_room · dT_room/dt = (T_slab − T_room) / R_sr − (T_room − T_out) / R_ro + g_room · solar
```

`Q_in` is the heat the pump delivers (its yield power on the bus), `solar` the PV output
as a proxy for sun, `T_out` the outside temperature (`entities.outside_temp`, falling
back to the weather entity's current reading), and `T_room` the comfort room: the coldest
of the room sensors (`entities.comfort_rooms`) or an HA sensor (`entities.room_mean`).
The six parameters are fitted by least squares on the five-minute data log
(`tools/fit_log.py`), with the slab temperature as a hidden state. A Kalman filter tracks
that state at run time. On the author's house a fit on an 81 h step test forecasts the
coldest room 3-12 h ahead to within about 0.3 K.

**Heat pump.** The Vaillant controller's flow target is
`max(curve(T_out), MinFlow)`. Its cost comes from a Carnot-fraction COP,
`COP = η · T_flow / (T_flow − T_out)` in kelvin, with `η` fitted from measured electrical
and heat power. Heat into the slab is `k · (T_flow − T_slab)`.

**Price planner** (`planner.py`, stage 1). Boost the minimum flow in the cheapest third of
the next 24 h of Agile slots (never above `max_boost_price`), leave the heating curve in
charge otherwise. It knows nothing about weather or the slab's lag, but it needs nothing
but prices, so it is the fallback whenever the model planner has no plan.

**Energy planner** (`energy.py`, 0.7.0). A simpler model for a slab too big and slow to
simulate: the next day's heat is `heat_loss × (balance_point − mean forecast outside)`, a
straight line fitted to a winter of daily heat (177: 0.37 kW/K below 13.3 °C, R² 0.81 over
26 weeks). The curve's own share (`baseline_kw`) comes off; the rest is bought as
upgrades of single half-hours to a higher min flow (32, then 38 °C, each with a measured
heat output and COP), cheapest per kWh of heat first, never above `max_price`. A shadow
unless `energy.live`. `baseline_kw` was measured in mild weather; the curve gives more when
it is cold, so on cold days the plan overstates what it has to buy until that is refitted.

**Model planner** (`mpc.py`, `shadow.py`). For each half-hour of the next 24, pick zone off or a
minimum flow from a few levels (20, 25, 32, 38, 42 °C) so the coldest room stays above the
comfort floor for the least `Σ price × Q / COP`. It is greedy: start with everything off,
simulate the house against the weather and sun forecasts, and at the first slot where the
coldest room would fall below the floor, raise the earlier slot that buys the most degrees
at that moment per penny; repeat. A raise may not push the warmest room over the top (the
comfort top plus the usual room spread) before that moment. Because the slab carries heat for hours, this banks cheap night heat
for the morning and skips heat a sunny afternoon will supply.

With `model.live: true` (0.5.0) its plan for the slot now drives the pump: a level is
written as the minimum flow, "off" turns the zone and the room thermostats off. Without a
plan (no fit, a failed tick, no room reading) the price planner stands in. The comfort band
binds on both: at the floor anything becomes a boost, at the top a boost is cut, and
neither waits out the dwell. Without `live` the plan is only published (`sensor.underflow_model_plan`), a
shadow to compare against what happened. Refit with `tools/fit_log.py` as data comes in.

Room thermostats are set a couple of degrees above target with no schedule, so every
loop stays open and the flow temperature controls the heat, as Havenwise does. They are
still the comfort sensors.

Open thermostats call all day, though, and a call does more than ask for heat: it can
run the underfloor manifold's own pump. With a `gate:` block (0.4.0, `gate.py`) underflow
switches the thermostats off when it has no reason to heat (the model planner has heat
off, or the coldest room is at the comfort top with nothing to boost) and back on when
there is: 0.3 K under the top, at a boost, at the comfort floor, or when a step test heats. Their setpoints are left alone, so each still caps its
room. A dwell stops it chattering, a 5-minute reconcile puts back any thermostat that
drifts, and an `input_boolean` turns the gate off from the dashboard.

**Room sensors that fail** (`rooms.py`). A room sensor that goes unavailable, or freezes on
one value while every other room moves, is estimated from the other rooms at its usual
offset from each, with an alert, rather than silently ignored: dropping the room that is
usually coldest would make the house look warmer and the planner heat less.

## Roadmap

| Stage | What | Status (author's house) |
|---|---|---|
| 0 | Observe: publish all model inputs as `sensor.underflow`, write nothing | live since Sep 2026 |
| 1 | Agile rules (`planner.py`): raise min flow in the cheapest third of upcoming slots, baseline otherwise; the comfort band overrides | live on one pump from 10 Oct 2026, the second from 12 Oct; since 0.5.0 the fallback where the model planner runs |
| 1b | Reliable writes (`reconcile.py`, `ebusd.py`): hold a target, check it against the bus every 5 min, write only if it differs | live since 10 Oct 2026 |
| 1s | Shadows: publish what two other demand sources would ask for, never written. `sensor.underflow_shadow_mirror` copies the neighbouring controller's min flow; `sensor.underflow_shadow_shared` (`shared.py`) steers by the room sensor that controller uses | tried Oct 2026; retired in 0.3.1 when the neighbouring controller was replaced by underflow itself |
| 1g | Thermostat gate (`gate.py`): switch the room thermostats off when there's no reason to heat | live from 13 Oct 2026 (0.4.0) |
| 2 | House and COP models (`model.py`): 2R2C fit, Kalman filter, Carnot-fraction COP; model planner (`mpc.py`) | step test 10-13 Oct 2026, fitted on its 81 h; planner shadowed from 11 Oct, live on one pump from 13 Oct 2026 (0.5.0). The second pump needs a fit of its own |
| 3 | Optimise min flow per half-hour (dynamic programme over slab temperature) | Dec 2026 |

## Files

| File | Purpose |
|---|---|
| `underflow.py` | The AppDaemon app, i.e. the site: the decide and reconcile clocks, Agile prices and forecasts (fetched once per tick), liveness probes, alerts; runs each pump in turn |
| `pump.py` | One heat pump: decide, reconcile and observe; publishes its sensors; calls its write script when the bus disagrees. No AppDaemon import |
| `planner.py` | The price planner (stage 1 rules, and the fallback). Pure |
| `mpc.py` | The model planner: greedy 24 h plan of zone off or a min-flow level per slot. Pure |
| `energy.py` | The energy planner: the day's heat need from a fitted heat-loss line, bought in the cheapest half-hours. Pure |
| `shadow.py` | Runs the model planner each tick: Kalman slab estimate, forecast cache, publishes the plan. Pure |
| `forecast.py` | Met Office temperature and Solcast PV forecasts to half-hour slots, with offline fallbacks |
| `testplan.py` | Step tests: fixed blocks of min flow or zone off, with floor and ceiling guards |
| `targets.py` | Comfort band and test guards from HA `input_number` sliders |
| `persist.py` | Saved state, so a restart keeps the target, slab estimate, gate and room offsets |
| `datalog.py` | The 5-minute CSV log the models are fitted from |
| `ebusd.py` | Client for ebusd's TCP command port (see below) |
| `reconcile.py` | Given a target, a fresh reading and the link state, decide whether to write. Pure |
| `demand.py` | Where the target comes from: the live model planner, a neighbouring controller (mirror) or the local price planner. Pure |
| `shared.py` | Shadow demand source (retired on the author's house): steer by the neighbour's room sensor in small capped steps. Pure |
| `probe.py` | Is each device still on the bus? Uses ebusd's `lastup`, adds no bus traffic. Pure |
| `hasafe.py` | Stops AppDaemon dropping zero and `False` attributes on the way to HA |
| `rooms.py` | The comfort rooms: coldest and warmest, with a room that drops out or freezes estimated from the others at its usual offset. Pure |
| `gate.py` | When the room thermostats should be allowed to call: off when the model has heat off or at the comfort top, on to boost or heat, following a step test. Pure |
| `narrative.py` | Plain-English status bullets, published as `sensor.underflow_whats_going_on` for a markdown card |
| `model.py` | Stage 2: `HouseModel`, `fit_house`, `KalmanFilter`, `CopModel`, `fit_cop`, `flow_target` |
| `tools/ha_dashboard.py` | Get or save a storage-mode dashboard over the websocket |
| `tools/fetch_stats.py` | Export HA long-term statistics to CSV (needs `HASS_URL`, `HASS_TOKEN`) |
| `tools/fit_from_csv.py` | Fit the house model to that CSV; an offline smoke test |
| `tools/fit_log.py` | Fit house, emitter, COP and room spread from the data log and print the `model:` block for apps.yaml (`--outside house\|met`) |
| `apps.example.yaml` | Example AppDaemon config, one pump |
| `examples/two-pumps.yaml` | Example config for one app driving two pumps (`pumps:`) |
| `docs/set_min_flow_temp.example.yaml` | Example write script with bounds and read-back |

Tests are standalone scripts: `uv run tests/<name>.py` (`test_model.py` also needs
`--with numpy --with scipy`).

| Test | Checks |
|---|---|
| `test_model.py` | Fits two weeks of synthetic data from known parameters and recovers them; 24 h forecast; COP fit on a real hot-water run |
| `test_ha_safe.py` | `ha_safe` survives a verbatim copy of AppDaemon's attribute cleaner |
| `test_planner.py` | Planner rules on a synthetic Agile day |
| `test_ebusd.py` | Reply parsing, the `-f` flag, a dead bus returns a `Reading` instead of raising |
| `test_reconcile.py` | An unreadable bus never causes a write; backoff slows but never stops; alerts fire on duration |
| `test_forecast_mpc.py` | Forecast parsing and offline fallback; model planner keeps the room above the floor, banks heat in cheap slots, heats less with sun, nothing for a warm house, a sunny afternoon over the top doesn't block night heat (13 Oct 2026), deterministic; shadow filter and plan output |
| `test_energy.py` | Energy planner: need from the forecast mean, the curve's share off, the cheapest slots bought, price cap and free slots, short price horizon, fallbacks, config refuses levels that break the greedy order |
| `test_persist.py` | Saved state round-trips; a tripped step-test guard stays tripped across a restart; stale, corrupt or foreign state files are ignored |
| `test_targets.py` | HA slider values override the comfort band and test guards; out-of-range, unparsable or upside-down values fall back to apps.yaml |
| `test_testplan.py` | Step-test blocks start and end on schedule, "off" asks for the zone off, the floor guard trips and latches, the ceiling pauses a heat block and lets it resume 0.5 K under; the CSV log keeps one header per file |
| `test_rooms.py` | A missing room is estimated at its offsets, follows the others, holds steady when a second drops, is left out after 12 h, gives nothing when nothing reports, and survives a restart; a frozen sensor is caught only when every other room moves, and is cleared by its first change |
| `test_narrative.py` | The status text: comfort band, cheap windows, step test and model planner lines; the pump line follows the status code, not lagging power |
| `test_gate.py` | The thermostat gate closes at the comfort top and reopens 0.3 K under it, opens to boost or at the floor, follows a step test, fails open, and holds non-urgent changes for the dwell |
| `test_demand.py` | Model planner's level or heat off, comfort band binding on it; mirror held through a dropout, dropped when stale; caps and comfort band apply |
| `test_shared.py` | Deadband, one step per tick, floor and cap, this half's rooms override the shared sensor |
| `test_probe.py` | A steady value stays alive; blips are debounced; a missing device is found |
| `test_pumps.py` | A one-pump apps.yaml works as before; two pumps get their own config plus the inherited site keys, nothing leaking; a pump pointed at a forbidden register is refused while the other starts; a pump that raises never stops the other; a slider re-decides its own pump only; comfort on the coldest available room; the thermostat gate decides, writes over HomeKit with the remembered setpoint, holds in flight, respects its switch and survives a restart; the house outside temperature with the Met Office fallback; the live model planner drives level, zone and thermostats and falls back to the price planner, including with no room reading; a room sensor dropping out or freezing is estimated, alerted once and dismissed on return |

### Synthetic test results

Two weeks of 5-minute data from a known 2R2C house, 0.05 K noise. The fit recovers the
envelope resistance within 4 %, the slab capacity within 6 % and the room-side solar
gain within 4 %. The slab-side solar gain is poorly identified (−35 %), so the code can
fall back to a single gain term. COP fitted on a real aroTHERM hot-water run gives
η = 0.33 with a 0.15 residual over 51–65 °C flow.

### Don't fit to cloud data

myVAILLANT cloud statistics are unusable for fitting a half-hourly model: flow temperature
was frozen in 72 % of hourly buckets, and energy counters arrive in weekly lumps. Use ebusd
readings. The daily energy buckets (`emf/v2/.../buckets?resolution=DAY`, 60 days per
request) are another matter: 177's winter has no empty or lumped days, and its daily heat
tracks outside temperature (r = −0.77), which is what the energy planner was fitted on.

## Writing over a flaky link

The eBUS adapters here are ESP32-C6 sticks on 2.4 GHz WiFi. The weaker one, at −71 dBm,
**dropped its eBUS signal 30 times in 48 hours**: 52 minutes offline, median 32 s,
longest 18 minutes. Writes have to assume this is normal.

**Home Assistant can't confirm a write.** ebusd publishes to MQTT only when a value
changes, so HA's `number.*` entity is a cache that never expires. One min-flow register
had a `last_reported` 19 hours old. The entity shows `20` whether the bus is healthy or
the adapter has been dead for 15 minutes, and none of its fields tell the two apart.

`read -f` on ebusd's TCP command port forces a real bus read: you get either a fresh
value or an `ERR:`. That is what `ebusd.py` is for. Writes still go through a Home
Assistant script with its own bounds and read-back, so the limits stay visible to the user.

**One write isn't enough.** `reconcile.py` holds a target and re-checks it every cycle,
writing only when a fresh reading differs. The rules:

- **A failed read is not a mismatch.** No answer from the bus says nothing about the
  register, so it never triggers a write. (The bug this fixed compared `None` to the
  target and wrote into a dead bus every cycle.)
- **Wait 60 s of steady signal before writing.** Dropouts come in clusters of 16–32 s,
  so a link that has just come back often drops again mid-write.
- **Back off, never stop.** Retries slow from every cycle to every 30 min, about 11
  tries over four hours of failure. A dead bus shouldn't send hundreds of alerts, and
  one that recovers at 4 a.m. should be picked up automatically.
- **Alert on duration.** One failed write is normal. 30 minutes of mismatch gets an
  alert.

### Telling idle from dead

Publish-on-change also breaks the obvious health check, "no updates for 45 minutes
means the device is gone". A heat pump idling at a steady temperature sends nothing, so
idle and dead look the same. In this house that check false-alarmed four times in three
days, once with the pump in standby and a forced read showing the HMU answering
normally.

`probe.py` measures instead, without touching the bus. `find -V` returns ebusd's cached
value and `lastup`, the time ebusd last saw it on the wire. Pointed at a register ebusd
already polls (`hmu Status01` refreshes about every 10 s), the age of `lastup` shows
whether the device is alive. When the 177 adapter dropped off WiFi at 08:37 on 14 Sep,
its `lastup` stayed at 08:30:07 for the next 28 minutes, while 179's stayed under 3
minutes old.

Cost on a healthy bus:

| Command | Cost |
|---|---|
| `read -f` (force a bus read) | 210–390 ms, sometimes 2 s |
| `read -m 600` (accept ebusd's cache) | 2.5 ms |
| `find -V` (ask ebusd what it has) | no bus traffic |

eBUS is timing-sensitive and these adapters carry it over WiFi (ebusd runs with
`--latency=100` for this reason), so monitoring should add no bus traffic at all.
Results are debounced over `dead_after` failures, spaced out rather than sent back to
back, and a device already known to be down is probed less often.

`lastup` is in ebusd's local time with no time zone. If ebusd's clock runs ahead, every
age is negative and every device looks fresh. A few seconds of skew is ignored; beyond
`MAX_CLOCK_SKEW` the probe reports a fault instead of guessing.

If underflow itself stops, the probe attributes freeze and alerts go quiet. Watch the
probe sensor's own staleness for that: it is republished every cycle, so a stale value
does mean it has stopped.

### Two loop speeds

Deciding runs every 30 minutes. The slab takes hours to respond and prices change every
half hour, so deciding faster gains nothing. Near its threshold the "cheapest third"
rule could flip back and forth, each flip costing a write; a deadband and minimum dwell
keep it to about two writes an hour. `max_boost_price` caps what counts as cheap: before
Octopus publishes tomorrow (~16:00) the horizon ends at 23:00, and the cheapest third of
that can still be dear.

Reconciling runs every 5 minutes, because a lost write is what costs heat: a dropped
boost stays wrong for 5 minutes instead of a whole slot. Reads are local and take
milliseconds. The median dropout is shorter than one cycle; the worst lasts three or
four.

## Which flow sensor

There are two candidate registers on a Vaillant, and only one works for the COP fit:

- `hmu FlowTemp`, the heat pump's outlet sensor. **Use this one.** It reads correctly
  whatever the pump is doing.
- `ctlv2 Hc1FlowTemp`, the heating circuit sensor. When the circuit is idle it measures
  standing water and drifts slowly. On the author's house it read 47.5 °C with the pump
  in standby and a 19.5 °C return, and stayed at 30 °C through a hot-water charge that
  took the pump outlet to 70 °C.

Keep the circuit sensor as a diagnostic (`flow_temp_circuit`), since it shows what the floor
loops see, but don't feed it to the model.

**Don't fit COP on hot-water cycles.** Hot water runs at 60–70 °C flow, space heating at
about 30 °C, and mixing them fits `η` to the wrong conditions. `pump.py` classifies
the HMU status as `idle`, `dhw`, `heating` or `cooling`, and `fit_cop(..., mode=...)`
keeps only `heating`.

## Requirements

- Home Assistant OS with the **AppDaemon** add-on (Community Apps repository),
  configured with `python_packages: [numpy, scipy]`.
- The ebusd add-on, exposing the controller's `HcNHeatCurve`, `HcNMinFlowTempDesired`
  and `HcNMaxFlowTempDesired` registers and the HMU's `CurrentConsumedPower` and
  `CurrentYieldPower` as HA entities.
- Room temperature sensors in HA (Tado over HomeKit, Heatmiser, anything).
- A weather entity and, for prices, the Octopus Energy integration. The model planner
  also uses the weather entity's hourly forecast and, optionally, Solcast's PV forecast as
  a proxy for sun.
- A Home Assistant **script** that writes the minimum flow temperature with bounds and
  a forced read-back. underflow only ever calls that script, never the register.
  Example in `docs/set_min_flow_temp.example.yaml`.

## Install

1. Copy every top-level `*.py` file into AppDaemon's `apps/` directory.
2. Copy `apps.example.yaml` to `apps/apps.yaml` and fill in your entity ids.
3. Check the AppDaemon add-on's log. You should see
   `underflow: initialised vX.Y.Z; pumps=[...]; ... numeric stack: numpy …, scipy …`,
   then a `plan:` line per pump every half hour.

Install from a [release](https://github.com/dabeeeenster/underflow/releases) rather than
`main`, so you know which version is running: the status sensor's `version` attribute
says which build AppDaemon actually loaded.

### Two or more pumps

One app drives every pump; don't run a second instance. Put each pump's config under
`pumps:` (see `examples/two-pumps.yaml`). A pump inherits `dry_run`, `min_dwell_minutes`,
`deadband`, `reconcile`, `entities` and `datalog` from the top level, with dicts merged and
its own keys winning. Nothing else is inherited, so a step test or a register can't leak
to the other pump. Clocks, probes and `alert_service` stay at the top level. Each pump
needs its own `status_entity`, `state_file` and `datalog.prefix`. Without `pumps:`, the
whole file is one pump, as before 0.3.0.

Per pump:
- `forbid_registers: ["Hc1*", "Z1*"]` refuses to start the pump if its `ebusd.register`,
  its declared `write_register` or its zone points at a match. Use it where a circuit must
  never be written, such as a cooling circuit. The refused pump's status sensor reads
  `refused`, and an alert says why.
- `entities.comfort_rooms` (a list of entities, or `{name: entity}`) judges comfort on the
  coldest of these rooms, and a step-test ceiling on the warmest, worked out in the app.
  It's an alternative to a `room_mean` sensor. A room that drops out is estimated from the
  others at its usual offset from each (`rooms.py`, 0.6.0) for up to
  `rooms.estimate_hours` (default 12), then left out; after `rooms.alert_minutes` (default
  30) offline there is an alert, dismissed when it reports again. A room whose reading
  hasn't changed for `rooms.stuck_hours` (default 4) while every other room has moved
  `rooms.stuck_move` (default 1.0 K) is treated the same way, as stuck (0.6.1). Rooms you
  only want logged go in `datalog.columns`.
- `zone.mode_on` / `zone.mode_off` are what the zone select calls on and off (default
  `day` / `off`).
- `entities.outside_sensors: {name: entity}` lists outside sensors to log as
  `outside_temp_<name>_sensor`. The older `outside_179` / `outside_177` keys still work.
- `entities.outside_temp` is the one outside temperature the model planner and the
  narrative use (logged as `outside_temp`). Without it, or while it is unavailable, they
  use the weather entity's current reading.
- `gate:` switches room thermostats on and off (`climates`, `mode_on`/`mode_off`,
  `hysteresis`, `min_dwell_minutes`, `enable_entity`, its own `dry_run`, and
  `max_setpoints: {climate: °C}`, the most it remembers for each one to reopen at). See
  `examples/two-pumps.yaml`. Pair it with an HA watchdog that turns the thermostats back
  on if underflow stops publishing.

If you later add a new block to the app's config in `apps.yaml`, restart the AppDaemon
add-on. AppDaemon can fail to reload the config (`KeyError` in `deep_compare`) and keep
running the app with the old settings.

## Safety

- `dry_run: true` is the default, for the pump and for the thermostat gate separately.
- The comfort floor overrides every demand source, and skips the dwell.
- Where information is missing, it fails towards heat: no room reading hands over to the
  price planner with the thermostats on; no prices hold a neutral flow; missing forecasts
  assume no sun.
- underflow never writes a register directly. It calls a script you own, which enforces
  the bounds and reads the value back off the bus.
- `forbid_registers` stops a pump starting if it is configured to touch a circuit it must
  never write.
- Pair zone and thermostat switching with HA watchdogs that turn them back on if
  underflow goes quiet or the house gets cold (see the gate notes above).
- Before enabling any control stage, cap `MaxFlowTempDesired` on the heating circuit at
  what your floor can take (45 °C is usual for screed).

## Releases

Each deploy to a live pump is a tagged release, so the version on the dashboard maps to
exact code.

1. Bump `__version__` in `underflow.py` (semver; 0.x minor bumps may change config).
2. Add a section to `CHANGELOG.md`.
3. Run every test: `for t in tests/test_*.py; do uv run $t; done`.
4. Commit, tag and publish:
   `git tag -a vX.Y.Z -m vX.Y.Z && git push --follow-tags &&
   gh release create vX.Y.Z --title vX.Y.Z --notes-file <the changelog section>`.
5. Deploy that tag's `*.py` files and check the status sensor reports the new `version`.

## Licence

MIT. Built by Ben Rometsch with Claude Code.
