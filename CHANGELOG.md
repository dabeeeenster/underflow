# Changelog

All notable changes to underflow. Versions follow [semantic versioning](https://semver.org/);
while it is 0.x, a minor bump can change config. The running version is published as the
`version` attribute of the status sensor (e.g. `sensor.underflow_179`) and shown on the
dashboard.

## 0.1.2 — 2026-10-10

Data-log fixes after its first hour:

- `datalog.bus`: registers read off the bus for each row (`read -m 60`). ebusd polls the
  HMU power and flow registers only every ~10 min, so the HA entities showed heat out
  while the compressor was blocked and 0 kW in while it ran.
- `datalog.averaged`: time-weighted means between rows (PV), instead of a snapshot.
- One row per 5-minute slot: the half-hourly decide tick no longer adds a duplicate.
- Changing the columns starts a new CSV (`…-2.csv`); existing files are never rewritten.

## 0.1.1 — 2026-10-10

- The "what's going on" narrative knows about step tests: while one runs it says which
  block is in charge and until when, instead of describing a price plan that isn't being
  followed, and the stage line says stage 2.
- New optional `room_label` (default "the house") so the narrative names what the room
  sensor actually is — on 179 it is now the coldest room, not a mean.

## 0.1.0 — 2026-10-10

First tagged release. Everything to date, as running on 179:

- **Observe** (stage 0): every model input published to the status sensor; plain-English
  "what's going on" sensor; a websocket dashboard tool.
- **Rule-based Agile planner** (stage 1): boost the minimum flow in the cheapest third of
  upcoming slots and on negative prices, baseline otherwise, with a comfort band that
  overrides price. Deadband and minimum dwell so a lossy link isn't written on every flip.
- **Safe writes over a flaky eBUS link:** three clocks (decide / reconcile / observe);
  verification reads from ebusd's command port, not HA's cached entity; a failed read is
  never a mismatch; wait for a stable link before writing; back off but never give up;
  alert after sustained mismatch, never in dry run.
- **Bus liveness probes** from ebusd's `lastup`, at zero bus cost.
- **Mirror of 177's demand** and two dry-run shadows (copy 177, shared sensor). The mirror
  is off; the shadows are kept for the record.
- **Stage 2 groundwork:** house (2R2C), COP and flow-target models with offline fitting
  tools; **step tests** (a schedule of `[hours, min flow | "off"]` blocks that replaces the
  planner, with floor and ceiling guards) and a **5-minute CSV data log** for fitting.
- Config: the data log lives under `datalog:` (AppDaemon reserves `log:`).
