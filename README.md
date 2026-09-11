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

| Stage | What | When (author's house) |
|---|---|---|
| 0 | Observe: publish all model inputs as `sensor.underflow`, write nothing | **done, Sep 2026** |
| 1 | Rule-based Agile shifting: raise min flow in the cheapest third of slots, floor it in the dearest, inside a comfort band | Oct 2026 |
| 2 | Fit the house and COP models; publish a 24 h room-temperature prediction and check it against reality | Nov 2026 |
| 3 | Optimise min flow per half-hour | Dec 2026 |

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
