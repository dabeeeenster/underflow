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
| 2 | House + COP models (`model.py`): 2R2C fit, Kalman filter, Carnot-fraction COP | **built and tested on synthetic data**; real fit needs a month of heating data (Nov 2026) |
| 3 | Optimise min flow per half-hour (dynamic programme over slab temperature) | Dec 2026 |

## Files

| File | Purpose |
|---|---|
| `underflow.py` | The AppDaemon app: observe every 30 min, run the planner, publish sensors, optionally call the write script |
| `planner.py` | Stage 1 rules. Pure functions, no HA dependency |
| `model.py` | Stage 2 physics: `HouseModel`, `fit_house`, `KalmanFilter`, `CopModel`, `fit_cop`, `flow_target` |
| `tests/test_model.py` | Synthetic recovery test: generates two weeks from known parameters, fits, checks recovery within a few %, checks a 24 h forecast, fits COP on a real hot-water run |
| `tests/test_planner.py` | Planner rules on a synthetic Agile day |
| `tools/fetch_stats.py` | Pull HA long-term statistics to CSV over the websocket (needs `HASS_URL`, `HASS_TOKEN`) |
| `tools/fit_from_csv.py` | Fit the house model to such a CSV; the offline smoke test for the pipeline |
| `apps.example.yaml` | Example AppDaemon config |
| `docs/set_min_flow_temp.example.yaml` | Example bounded write script with read-back |

Run the tests with `uv run tests/test_model.py` and `uv run tests/test_planner.py`.

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
