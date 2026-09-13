# underflow

An open, local, Havenwise-style controller for a Vaillant aroTHERM heat pump on an
all-underfloor house, built on Home Assistant, AppDaemon and ebusd. It learns how the
house and the heat pump behave, then plans the pump's **minimum flow temperature** per
half-hour against the Octopus Agile tariff and the weather forecast, using the concrete
slab as the thermal battery.

**Status: stage 0, observe only.** It publishes a sensor and writes nothing. See
[Roadmap](#roadmap).

## Why

[Havenwise](https://www.havenwise.co.uk/) does this well, for £7/month, through the
manufacturer cloud. As of September 2026 nothing open source does the whole loop:
Predbat's Predheat forecasts but doesn't control, EMHASS optimises electrical power but
never chooses a flow temperature, SAT autotunes a heating curve for boilers over
OpenTherm. The pieces exist; this project is the loop. It runs entirely on your LAN
against ebusd, so there is no cloud API budget and nothing to fight with.

## How it works (the plan)

The controller runs three deliberately simple, physically grounded models. Grey-box, not
black-box: with a few weeks of data you cannot train something that extrapolates to a
−3 °C night it has never seen, but a two-node thermal circuit with five parameters will.

**House.** Two thermal masses, the slab and the room air:

```
C_slab · dT_slab/dt = Q_in − (T_slab − T_room) / R_sr
C_room · dT_room/dt = (T_slab − T_room) / R_sr − (T_room − T_out) / R_ro + g · solar
```

`Q_in` is the heat the pump delivers (from the bus yield power), `T_out` the weather
observation, `T_room` the mean of the room stats. The five parameters are fitted by least
squares on five-minute data with the slab temperature as a hidden state; a Kalman filter
keeps that state current at run time.

**Heat pump.** What the Vaillant controller will do with a given minimum flow setpoint
is known exactly: flow target = max(curve(T_out), MinFlow). What it costs is a
Carnot-fraction COP, `COP = η · T_flow / (T_flow − T_out)` in kelvin, with `η` fitted from
the measured electrical and heat power. Heat into the slab is `k · (T_flow − T_slab)`.

**Optimiser.** Decision variable: minimum flow setpoint per half-hour from a small set of
levels (say 20, 25, 30, 35, 40 °C). Cost `Σ price × Q / COP`. Constraints: a comfort band
on predicted room temperature and a hard flow cap for the screed. Forty-eight slots by
five levels is a dynamic programme over slab temperature that runs in well under a
second in numpy. Parameters are refitted weekly on a rolling window; if the fit degrades
the controller falls back to a rule-based schedule.

Room thermostats are treated the Havenwise way: set them a couple of degrees above the
target with no schedule so every loop stays open, and let the flow temperature do the
work. They remain the comfort sensors.

## Roadmap

| Stage | What | Status (author's house) |
|---|---|---|
| 0 | Observe: publish all model inputs as `sensor.underflow`, write nothing | **live, Sep 2026** |
| 1 | Rule-based Agile shifting (`planner.py`): boost min flow in the cheapest third of upcoming slots, baseline otherwise, comfort band overrides | **built and running in dry run**; publishes `sensor.underflow_planned_min_flow`. Goes live Oct 2026 |
| 1b | Tolerant writes (`reconcile.py`, `ebusd.py`): hold a target, verify it against the bus every 5 min, write only on drift | **built, running in dry run Sep 2026** |
| 2 | House + COP models (`model.py`): 2R2C fit, Kalman filter, Carnot-fraction COP | **built and tested on synthetic data**; real fit needs a month of heating data (Nov 2026) |
| 3 | Optimise min flow per half-hour (dynamic programme over slab temperature) | Dec 2026 |

## Files

| File | Purpose |
|---|---|
| `underflow.py` | The AppDaemon app: three clocks (decide / reconcile / observe), publish sensors, call the write script when the bus disagrees |
| `planner.py` | Stage 1 rules. Pure functions, no HA dependency |
| `ebusd.py` | Client for ebusd's TCP command port. `read -f` forces a real bus read, which is the only way to verify a setting — see below |
| `reconcile.py` | The converging write loop: given a target, a fresh reading and the link state, decide whether to write. Pure |
| `demand.py` | Where the target comes from: a mirrored neighbouring controller, or the local planner. Pure |
| `hasafe.py` | Sanitises values so AppDaemon's HTTP kwarg cleaning cannot drop zeros and `False` on the way to HA |
| `narrative.py` | Plain-English "what's going on" bullets from the observation and the plan, published as `sensor.underflow_whats_going_on` for a markdown card |
| `tools/ha_dashboard.py` | Get/save a storage-mode dashboard over the websocket |
| `model.py` | Stage 2 physics: `HouseModel`, `fit_house`, `KalmanFilter`, `CopModel`, `fit_cop`, `flow_target` |
| `tests/test_model.py` | Synthetic recovery test: generates two weeks from known parameters, fits, checks recovery within a few %, checks a 24 h forecast, fits COP on a real hot-water run |
| `tests/test_ha_safe.py` | Reproduces AppDaemon's attribute pruning against a verbatim copy of its cleaner, then checks `ha_safe` defeats it |
| `tests/test_planner.py` | Planner rules on a synthetic Agile day |
| `tests/test_ebusd.py` | Reply parsing, the `-f` flag, and that a dead bus returns a `Reading` rather than raising |
| `tests/test_reconcile.py` | That an unreadable bus never causes a write, backoff throttles but never latches off, alerts fire on duration |
| `tests/test_demand.py` | Mirror held across a dropout, abandoned when stale, caps and comfort band bind |
| `tools/fetch_stats.py` | Pull HA long-term statistics to CSV over the websocket (needs `HASS_URL`, `HASS_TOKEN`) |
| `tools/fit_from_csv.py` | Fit the house model to such a CSV; the offline smoke test for the pipeline |
| `apps.example.yaml` | Example AppDaemon config |
| `docs/set_min_flow_temp.example.yaml` | Example bounded write script with read-back |

Run the tests with `uv run tests/<name>.py` — each is a standalone script with no test-runner dependency.

### What the synthetic test shows

Two weeks of 5-minute data from a known 2R2C house with cheap-slot heating, 0.05 K
measurement noise. The fitter recovers the envelope resistance within 4 %, the slab
capacity within 6 % and the room-side solar gain within 4 %; the slab-side solar gain is
poorly identified (−35 %), which is expected and why the code can fall back to a single
gain term. COP fitted on a real aroTHERM plus hot-water run gives η = 0.33 with a 0.15
COP residual across a 51 to 65 °C flow range.

### A caution about cloud data

Fitting to myVAILLANT cloud statistics does not work: the flow temperature was frozen in
72 % of hourly buckets and energy counters arrive in weekly lumps. Use the bus (ebusd)
readings; that is what the controller does.

## Writing a setting over a link that drops out

The eBUS adapters here are ESP32-C6 sticks on 2.4 GHz. One of the two is signal-starved
at −71 dBm and **dropped its eBUS signal 30 times in 48 hours** — 52 minutes offline,
median 32 s, worst 18 minutes. That is not an outlier to engineer around later; it is the
normal operating condition, and it makes fire-and-forget writes unusable.

Two things follow, and both are less obvious than they look.

**Home Assistant cannot verify the write.** ebusd publishes to MQTT *only when a decoded
value changes*, so an unchanged register is never republished and HA's `number.*` state is
a cache with no expiry. Measured: a min-flow register with a `last_reported` 19 hours old,
unmoved by publishing its `/get` topic and waiting 30 s. That entity reads `20` exactly as
confidently when the adapter has been dead for a quarter of an hour as when the bus is
healthy, and nothing on it — state, `last_updated`, `last_reported` — tells the two apart.

`read -f` on ebusd's TCP command port does. It forces a read from the bus rather than
serving ebusd's cache, so the reply is either a value that was on the wire moments ago or
an `ERR:`, which makes one call both the reading and the liveness probe. That is all
`ebusd.py` is for. Writes still go out through a bounded Home Assistant script with its
own read-back, so the limits stay somewhere the user can see them.

**A single write is not a control action.** `reconcile.py` holds a desired value and
re-checks it every cycle, writing only when a *fresh* reading disagrees. Four rules carry
the weight:

- **A read that failed is not a mismatch.** The bus not answering says nothing about the
  register, so it can never justify a write. The bug this replaced compared `None` to the
  setpoint, got "not equal", and wrote into a dead bus on every cycle.
- **Wait for the link to settle** — 60 s of continuous signal before writing. Dropouts
  arrive in clusters of 16–32 s flaps, so a link that just came back is likely to go again
  inside the write-and-verify round trip.
- **Back off, but never give up.** Retries slow from every cycle to every half hour and
  stay there: about 11 attempts across four hours of sustained failure. A dead bus must not
  generate hundreds of notifications a day, and a bus that recovers at 4 a.m. must be
  picked up with nobody involved.
- **Alert on duration, not on failure.** One failed write is a blip and is normal. Thirty
  minutes of sustained mismatch is a fault worth a human.

### Why the loop runs at two speeds

Deciding and applying want different clocks. The actuator is a concrete slab with a
multi-hour time constant and the price signal is half-hourly, so there is nothing to gain
from deciding faster than the tariff slot — and a "cheapest third" rule near its threshold
will flip as the horizon rolls, each flip costing a write on a lossy link. A deadband and
a minimum dwell hold that to roughly two writes an hour.

Applying runs six times as often, because a *lost* write is what actually costs heat: at a
30 min reconcile a dropped boost sits wrong for a whole tariff slot, at 5 min for a sixth
of one. Reads are local and take milliseconds, so there is no reason to be stingy with
them. The median dropout is shorter than one cycle, and the worst one costs three or four.

## Picking the flow temperature sensor

Get this one right or the COP fit is meaningless. On a Vaillant there are two candidate
registers and they are not interchangeable:

* `hmu FlowTemp` — the heat pump's own outlet sensor. **This is the one to use.** It is
  what the Carnot expression means by `T_flow`, and it reads correctly whatever the
  pump is doing.
* `ctlv2 Hc1FlowTemp` — the *heating circuit* sensor. Whenever the circuit is idle it
  measures stagnant water, so it drifts down from whatever the last run left in the
  pipe over many hours. On the author's house it sat at 47.5 °C with the pump in
  standby and a 19.5 °C return, and stayed flat at 30 °C right through a hot-water
  charge that took the pump outlet to 70 °C.

Keep the circuit sensor as a diagnostic (`flow_temp_circuit`) — it tells you what the
floor loops actually see — but never feed it to the model.

Equally, **do not fit the COP on hot-water cycles.** A cylinder charge runs 60–70 °C
flow against a small lift; space heating runs ~30 °C. Mixing them fits `eta` to the
wrong regime. `underflow.py` classifies the HMU status code into
`idle`/`dhw`/`heating`/`cooling` and `fit_cop(..., mode=...)` keeps only `heating`.

## Requirements

- Home Assistant OS with the **AppDaemon** add-on (Community Apps repository), configured
  with `python_packages: [numpy, scipy]`.
- Local eBUS access via the ebusd add-on, exposing the controller's `HcNHeatCurve`,
  `HcNMinFlowTempDesired`, `HcNMaxFlowTempDesired` registers and the HMU's
  `CurrentConsumedPower` / `CurrentYieldPower` as HA entities.
- Room temperature sensors in HA (Tado over HomeKit, Heatmiser, anything).
- A weather entity and, for the tariff, the Octopus Energy integration.
- A Home Assistant **script** that writes the minimum flow temperature with bounds and a
  forced read-back. underflow will only ever call that script, never the register
  directly. Example in `docs/set_min_flow_temp.example.yaml`.

## Install

1. Copy `underflow.py` into the AppDaemon `apps/` directory.
2. Copy `apps.example.yaml` to `apps/apps.yaml` and fill in your entity ids.
3. Watch `ha apps logs a0d7b954_appdaemon`; you should see
   `underflow: initialised; dry_run=True; numeric stack: numpy …, scipy …` and then an
   `observing:` line every half hour.

## Safety

- `dry_run: true` is the default and the only mode in stage 0.
- The controller never writes a register. It calls a script you own, which enforces the
  bounds and reads the value back off the bus.
- Cap `MaxFlowTempDesired` on the heating circuit at what your floor can take (45 °C is
  usual for screed) before enabling any control stage.

## Licence

MIT. Built by Ben Rometsch with Claude Code; the physics is standard building-services
practice, the novelty is only that it ends by writing a Vaillant register.
