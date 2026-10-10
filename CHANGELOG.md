# Changelog

All notable changes to underflow. Versions follow [semantic versioning](https://semver.org/);
while it is 0.x, a minor bump can change config. The running version is published as the
`version` attribute of the status sensor (e.g. `sensor.underflow_179`) and shown on the
dashboard.

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
