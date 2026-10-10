# underflow

A local, open-source controller for a Vaillant aroTHERM heat pump in an all-underfloor
house, built on Home Assistant, AppDaemon and ebusd. It learns how the house and the
heat pump behave, then sets the pump's **minimum flow temperature** for each half-hour
against the Octopus Agile tariff and the weather forecast, using the concrete slab to
store heat.

**Status: stage 0, observe only.** It publishes sensors and writes nothing. See
[Roadmap](#roadmap).

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
C_slab · dT_slab/dt = Q_in − (T_slab − T_room) / R_sr
C_room · dT_room/dt = (T_slab − T_room) / R_sr − (T_room − T_out) / R_ro + g · solar
```

`Q_in` is the heat the pump delivers (its yield power on the bus), `T_out` the outside
temperature from the weather entity, and `T_room` the mean of the room sensors. The five parameters are fitted
by least squares on five-minute data, with the slab temperature as a hidden state. A
Kalman filter tracks that state at run time.

**Heat pump.** The Vaillant controller's flow target is
`max(curve(T_out), MinFlow)`. Its cost comes from a Carnot-fraction COP,
`COP = η · T_flow / (T_flow − T_out)` in kelvin, with `η` fitted from measured electrical
and heat power. Heat into the slab is `k · (T_flow − T_slab)`.

**Optimiser.** For each half-hour, pick a minimum flow from a few levels (say 20, 25,
30, 35, 40 °C) to minimise `Σ price × Q / COP`, keeping predicted room temperature in a
comfort band and flow under the screed's limit. 48 slots by 5 levels is a dynamic
programme over slab temperature that runs in under a second in numpy. The model is
refitted weekly on a rolling window; if the fit gets worse, the controller falls back to
simple rules.

Room thermostats are set a couple of degrees above target with no schedule, so every
loop stays open and the flow temperature controls the heat, as Havenwise does. They are
still the comfort sensors.

## Roadmap

| Stage | What | Status (author's house) |
|---|---|---|
| 0 | Observe: publish all model inputs as `sensor.underflow`, write nothing | live since Sep 2026 |
| 1 | Agile rules (`planner.py`): raise min flow in the cheapest third of upcoming slots, baseline otherwise; the comfort band overrides | running in dry run, publishing `sensor.underflow_planned_min_flow`; goes live Oct 2026 |
| 1b | Reliable writes (`reconcile.py`, `ebusd.py`): hold a target, check it against the bus every 5 min, write only if it differs | running in dry run since Sep 2026 |
| 1s | Shadows: publish what two other demand sources would ask for, never written. `sensor.underflow_shadow_mirror` copies the neighbouring controller's min flow; `sensor.underflow_shadow_shared` (`shared.py`) steers by the room sensor that controller uses, with this half's own rooms as limits both ways | added Oct 2026, for a week's comparison before choosing a demand source |
| 2 | House and COP models (`model.py`): 2R2C fit, Kalman filter, Carnot-fraction COP | tested on synthetic data; a real fit needs a month of heating data (Nov 2026) |
| 3 | Optimise min flow per half-hour (dynamic programme over slab temperature) | Dec 2026 |

## Files

| File | Purpose |
|---|---|
| `underflow.py` | The AppDaemon app: decide, reconcile and observe loops; publishes sensors; calls the write script when the bus disagrees |
| `planner.py` | Stage 1 rules. Pure functions, no HA |
| `ebusd.py` | Client for ebusd's TCP command port (see below) |
| `reconcile.py` | Given a target, a fresh reading and the link state, decide whether to write. Pure |
| `demand.py` | Where the target comes from: a neighbouring controller (mirror) or the local planner. Pure |
| `shared.py` | Shadow demand source: steer by the neighbour's room sensor in small capped steps, limited both ways by this half's room mean. Pure |
| `probe.py` | Is each device still on the bus? Uses ebusd's `lastup`, adds no bus traffic. Pure |
| `hasafe.py` | Stops AppDaemon dropping zero and `False` attributes on the way to HA |
| `narrative.py` | Plain-English status bullets, published as `sensor.underflow_whats_going_on` for a markdown card |
| `model.py` | Stage 2: `HouseModel`, `fit_house`, `KalmanFilter`, `CopModel`, `fit_cop`, `flow_target` |
| `tools/ha_dashboard.py` | Get or save a storage-mode dashboard over the websocket |
| `tools/fetch_stats.py` | Export HA long-term statistics to CSV (needs `HASS_URL`, `HASS_TOKEN`) |
| `tools/fit_from_csv.py` | Fit the house model to that CSV; an offline smoke test |
| `apps.example.yaml` | Example AppDaemon config |
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
| `test_testplan.py` | Step-test blocks start and end on schedule, "off" asks for the zone off, floor and ceiling guards trip and latch; the CSV log keeps one header per file |
| `test_demand.py` | Mirror held through a dropout, dropped when stale; caps and comfort band apply |
| `test_shared.py` | Deadband, one step per tick, floor and cap, this half's rooms override the shared sensor |
| `test_probe.py` | A steady value stays alive; blips are debounced; a missing device is found |

### Synthetic test results

Two weeks of 5-minute data from a known 2R2C house, 0.05 K noise. The fit recovers the
envelope resistance within 4 %, the slab capacity within 6 % and the room-side solar
gain within 4 %. The slab-side solar gain is poorly identified (−35 %), so the code can
fall back to a single gain term. COP fitted on a real aroTHERM hot-water run gives
η = 0.33 with a 0.15 residual over 51–65 °C flow.

### Don't fit to cloud data

myVAILLANT cloud statistics are unusable for fitting: flow temperature was frozen in
72 % of hourly buckets, and energy counters arrive in weekly lumps. Use ebusd readings.

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
keep it to about two writes an hour.

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
about 30 °C, and mixing them fits `η` to the wrong conditions. `underflow.py` classifies
the HMU status as `idle`, `dhw`, `heating` or `cooling`, and `fit_cop(..., mode=...)`
keeps only `heating`.

## Requirements

- Home Assistant OS with the **AppDaemon** add-on (Community Apps repository),
  configured with `python_packages: [numpy, scipy]`.
- The ebusd add-on, exposing the controller's `HcNHeatCurve`, `HcNMinFlowTempDesired`
  and `HcNMaxFlowTempDesired` registers and the HMU's `CurrentConsumedPower` and
  `CurrentYieldPower` as HA entities.
- Room temperature sensors in HA (Tado over HomeKit, Heatmiser, anything).
- A weather entity and, for prices, the Octopus Energy integration.
- A Home Assistant **script** that writes the minimum flow temperature with bounds and
  a forced read-back. underflow only ever calls that script, never the register.
  Example in `docs/set_min_flow_temp.example.yaml`.

## Install

1. Copy every top-level `*.py` file into AppDaemon's `apps/` directory.
2. Copy `apps.example.yaml` to `apps/apps.yaml` and fill in your entity ids.
3. Check the AppDaemon add-on's log. You should see
   `underflow: initialised; dry_run=True; ... numeric stack: numpy …, scipy …`, then a
   `plan:` line every half hour.

If you later add a new block to the app's config in `apps.yaml`, restart the AppDaemon
add-on. AppDaemon can fail to reload the config (`KeyError` in `deep_compare`) and keep
running the app with the old settings.

## Safety

- `dry_run: true` is the default and the only mode in stage 0.
- underflow never writes a register directly. It calls a script you own, which enforces
  the bounds and reads the value back off the bus.
- Before enabling any control stage, cap `MaxFlowTempDesired` on the heating circuit at
  what your floor can take (45 °C is usual for screed).

## Licence

MIT. Built by Ben Rometsch with Claude Code.
