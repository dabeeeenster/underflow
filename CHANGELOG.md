# Changelog

All notable changes to underflow. Versions follow [semantic versioning](https://semver.org/);
while it is 0.x, a minor bump can change config. The running version is published as the
`version` attribute of the status sensor (e.g. `sensor.underflow_179`) and shown on the
dashboard.

## 0.5.3 — 2026-10-13

Fixes from a review of 0.5.0-0.5.2:

- **No room reading, no model planner.** With the comfort-room sensor unavailable, the live
  model planner kept planning from its last estimate and could hold the heat off
  indefinitely: the comfort-floor override and the watchdog's 19.5 °C rule both need the
  reading, and "model has heat off" came before the gate's "no reading, so the thermostats
  decide". Now the price planner stands in (reason "no room reading") and the gate opens.
- **A comfort override from the model skips the dwell.** The dwell's "urgent" flag came from
  the price planner's own comfort check. When it was already boosting a cheap slot it had
  nothing urgent, so the model's floor boost could wait up to 30 minutes. The flag now comes
  from the source in charge.
- Narrative: cooling status codes read "cooling the house", not "heating the house" (177's
  circuit 1 is cooling).

## 0.5.2 — 2026-10-13

- Narrative: the heat-pump line follows the HMU's run-data status code, which is polled
  every ~10 s, rather than the power readings, which lag it by minutes. Until now a
  compressor that had just started read "not running (heat_compressor_active)" (177, 21:37
  on 13 Oct 2026), and one that had just stopped could still read as heating. Each phase
  has its own wording (starting a run, heating, pump overrun, blocked after a run,
  compressor off, standby); power and COP figures are quoted only while the compressor
  runs, with a note when they haven't caught up yet.

## 0.5.1 — 2026-10-13

- The model planner's ceiling follows the comfort top: the warmest room is planned to stay
  under the comfort-top slider plus the room spread, or `model.top`, whichever is lower.
  The pump cuts a boost once the coldest room reaches the comfort top, so heat planned
  above it was never delivered: on 179 at 21:36 on 13 Oct 2026 the plan had 32 °C at
  22:00 with the coldest room at 22.5 against a 22 °C top. The plan's `top` attribute is
  the ceiling actually used.

## 0.5.0 — 2026-10-13

- **Model planner live** (`model.live: true`): the stage-2 model planner's plan for the
  slot now drives the pump instead of the price planner. A level is written as the
  minimum flow; "off" is a true off, as in the step test: the zone goes off, the room
  thermostats go off through the gate (`heat_off`), and the register is held at the
  baseline. Any other level puts a zone this pump switched off back on, after a restart
  too. The comfort band binds as on every source: off at the floor becomes a boost, a boost
  at the top is cut to the baseline. With no usable plan (no fit, a failed tick) the price
  planner stands in and the reason says so. A running step test still overrides it.
  Without `live` the model planner stays a shadow, as before.
  - Status and plan attributes `model_live` and `heat_off`; demand source `model`; the
    narrative says what the model planner is doing, or that the price planner is standing in.
  - The plan is computed before the decision now, not after it.
- **Planner fix** (`mpc.py`): a raise is no longer refused for lifting the warmest room
  over the top *after* the breach it fixes. With a sunny afternoon forecast over the top,
  any night heat also lifted that peak, so every raise was refused: at 21:25 on 13 Oct
  2026 the refitted 179 model planned no heat and let the coldest room fall to 19.8 °C by
  07:30. The thermostat gate handles the afternoon.
- **`tools/fit_log.py --outside house|met`** (default `house`): fit on the outside
  temperature the controller uses since 0.4.0, the `outside_temp` column, or for older
  rows the lower of the two Vaillant sensors. 179 refitted on 10-13 Oct 2026 (81 h): a
  rolling backtest (filter to each hour, then forecast) gives 0.28 / 0.26 / 0.28 K error
  at 3 / 6 / 12 h with no bias, against 0.53 / 0.72 / 0.95 K and a cold bias for the
  11 Oct fit.
- Deployed 13 Oct 2026 with the step test ended a day early: its ceiling had paused every
  heat block since 11:00 that day.

## 0.4.0 — 2026-10-13

- **Thermostat gate** (`gate:`, `gate.py`): underflow switches a pump's room thermostats
  on and off. On 179 the Tados switch the heat pump's demand and the underfloor manifold
  pump in the plant room. On 13 Oct 2026, with zone 1 off and the heat pump in standby,
  that pump still ran, and it stopped within seconds of the three Tados being switched off.
  Opened to their maximums they call all day, so it ran around the clock in mild weather.
  - Off when the comfort room is at `comfort_max` and nothing is boosting; on again
    `hysteresis` (default 0.3 K) under it, at a boost, at the comfort floor, or with no
    room reading. While a step test runs it follows the test: on in a heat block, off when
    the block is off or paused.
  - Changes wait out `min_dwell_minutes` (default 30) unless the comfort floor or the test
    asks.
  - Reconciled every 5 minutes: a thermostat in the wrong mode is written over HA (`climate`
    services), at most every 3 min each; after 3 failed tries there is an alert. Turning
    one back on also sends its setpoint, remembered while it was on, because HomeKit
    reports a Tado's setpoint as null while it is off. Setpoints are otherwise left alone.
  - Its own `dry_run` (default: the pump's), so it can watch while the pump writes;
    `enable_entity` (an `input_boolean`) turns it off from the dashboard.
  - The position, reason and remembered setpoints survive restarts; status attributes
    `gate_*`; CSV columns `gate_open` and `gate_calling` (thermostats calling); a narrative
    line.
- **One outside temperature**: `entities.outside_temp`, used by the model planner and
  the narrative in place of the Met Office's current reading, which stays the fallback
  and still supplies the forecast. On Ben's site it is `sensor.outside_temp_house`, the
  lower of the two Vaillant sensors (179's reads high by day). Logged as `outside_temp`;
  the status sensor's `outside_temp_source` says which was used. The model was fitted on
  the Met Office reading and needs a refit on this one.
- Deployed 13 Oct 2026 at 10:05 with 179's gate in dry run; gate live from 18:17 the same day,
  while the step test was off and paused at its ceiling.

## 0.3.2 — 2026-10-12

- Planner: `max_boost_price` (£/kWh, default none). A slot priced above it is never
  boosted, however it ranks. The cheapest third is relative to what Octopus has
  published, and before ~16:00 that ends at 23:00: at 15:30 on 12 Oct 2026 177 boosted a
  29p slot because it was among the cheapest 5 of 15. The reason reads "cheap for the
  horizon, but 29.2p is over the 20p boost cap"; the comfort floor still boosts.
- Minimum dwell: a minute's slack. The decision tick a full dwell after a write saw
  29:59, because a write lands a second or so into its tick, and held for another slot
  ("holding 32 for another 0 min"): 177 stayed boosted through the 44.5p 16:00 slot on
  12 Oct 2026.

## 0.3.1 — 2026-10-12

- Step test: the ceiling guard no longer latches for the rest of a heat block. While the
  warmest room is at `ceiling` the block is paused (zone off, min flow at baseline); once
  the room is `ceiling_hysteresis` (default 0.5 K) under the ceiling the block resumes at
  its level. With no room reading it stays paused. On 11 Oct 2026 the old latch cost two
  whole heat blocks (32 and 38 °C, 16:00-24:00): sun plus a hot-water run had Upstairs
  over 23.5 °C at the start of each, and the room was back under by 18:40. The reason
  reads "heat paused at the 23.5 °C ceiling (warmest room 24.4 °C), resumes below 23 °C",
  and the log says when a block resumes.

## 0.3.0 — 2026-10-12

One app, several pumps. Prepares underflow to drive 177 (Havenwise was stopped on 12 Oct
2026) alongside 179, as one app rather than a second instance.

- `pump.py`: everything per-pump (decide, reconcile, step test, zone, shadows, model
  planner, data log, saved state, published sensors) moved out of `underflow.py` into a
  `Pump` class with no AppDaemon import. `underflow.py` is now the site: clocks, Agile
  prices and forecasts fetched once per tick, liveness probes, alerts.
- `pumps:` in apps.yaml: one entry per pump. A pump inherits `dry_run`, `min_dwell_minutes`,
  `deadband`, `reconcile`, `entities` and `datalog` from the top level (dicts merge); nothing
  else. With no `pumps:` key the whole file is one pump and every published entity,
  friendly name, CSV column and alert id is as before (checked against 0.2.2 on the live
  179 config).
- Each pump runs inside its own try/except: one that raises is logged and never stops the
  other; after 6 failed ticks in a row there is an alert. A pump that can't start is left
  out, its status sensor set to `refused`, and an alert says why.
- `forbid_registers` (globs): a pump refuses to start if its `ebusd.register`, declared
  `write_register` or zone matches. On 177, circuit 1 / zone 1 is the cooling circuit.
- `entities.comfort_rooms`: the comfort band judged on the coldest of a list of rooms,
  worked out in the app and skipping unavailable ones (instead of an HA template sensor).
  The step-test ceiling uses the warmest of them if no `warmest_room` is given.
- `zone.mode_on` / `zone.mode_off`: the zone select's on and off values (default
  `day`/`off`).
- A slider change re-decides its own pump only.
- Names with a pump: friendly names, alert titles and notification ids include it
  (`underflow 177 ...`, `underflow_177_hc2minflowtempdesired`); log lines are prefixed
  `[177]`. Default `status_entity` is `sensor.underflow_<name>`, default CSV prefix the name.
- `entities.outside_sensors: {name: entity}` replaces `outside_179` / `outside_177` (still
  accepted), and `entities.mirror_source` replaces `mirror_177` (still accepted). Missing
  optional entities no longer raise.
- Renamed on the shadow sensors: `room_temp_179` → `room_temp`, `value_177` →
  `source_value`; the copy shadow is now "copy the other half".
- Narrative: a pump's `notes:` (list of strings) are added to its "what's going on" text,
  for standing context such as "Havenwise was stopped; dry run first". The plan reason no
  longer says "(mirror disabled)" on a pump that never had a mirror, and the stage line no
  longer names step tests on a pump that doesn't run them.
- Deployed 12 Oct 2026: 179 unchanged (live, step test), 177 added in dry run.
- `examples/two-pumps.yaml`: the live 179 config under `pumps:` plus 177 in dry run (price
  planner only, Hc2, `forbid_registers: [Hc1*, Z1*]`, comfort on Kitchen/Upstairs/Tanya).
- `tests/test_pumps.py`.

## 0.2.2 — 2026-10-11

- A write already in flight (< 90 s, same value) is not repeated. The decide tick and the
  5-minute reconcile both fire on the half hour; since writes became non-blocking the
  second still read the old register value and wrote again (seen 12:00 11 Oct).

## 0.2.1 — 2026-10-11

Restart tolerance. Ben restarts Home Assistant routinely for updates, and every HA restart
makes AppDaemon re-initialise the app.

- `persist.py` + `state_file:` — the target, step-test guard latches, "a test was running",
  the zone underflow wants, the Kalman slab estimate (if under 1 h old) and the forecast
  cache are written atomically to a JSON file after every tick and restored on start-up.
  Missing, corrupt, foreign or >24 h-old files are ignored. Before this, a target was
  recovered from an HA sensor that an HA restart wipes, and —
- fixed: a floor guard that had tripped would un-latch after a restart (latches were
  compared with the previous tick's block, empty after a restart), so a cold room's off
  block could resume. Latches are now compared with the block running now.
- The status sensor publishes `state_restored_from` (when the restored state was saved).

## 0.2.0 — 2026-10-11

Stage 2 groundwork: a model planner, running as a **shadow** (plans and publishes, never
writes).

- `forecast.py`: Met Office hourly temperature (`weather.get_forecasts`) and Solcast
  half-hourly PV (the sun proxy) per half-hour slot. Offline fallback: last good forecast
  while under `max_forecast_age_h` (6 h), then current outside temperature held flat and
  **no sun** — errs towards more heat, never less.
- `mpc.py`: simulates slab and rooms with the house model and buys, greedily, the cheapest
  heat (price × heat ÷ COP) that keeps the coldest room above the comfort floor (+0.3 K
  margin) and the warmest below `top`, across the whole horizon so cheap heat is banked in
  the slab ahead of need. Levels are zone off or a minimum flow; every higher level is
  tried per slot (a level below the pump's own curve flow adds nothing). Deterministic.
- `shadow.py`: Kalman filter tracking the hidden slab temperature every 5 min from the
  coldest room and the heat actually going in; publishes `<status>_model_plan` with the
  24 h plan, predicted coldest room at 1/3/6 h, slab estimate, cost and forecast notes,
  plus graphable `_predicted_coldest_3h` and `_slab_estimate` sensors. The data log gains
  `model_level`, `model_pred_3h`, `model_slab_est`.
- `tools/fit_log.py`: fits house, emitter, COP and room spread from the 5-minute data log
  with a held-out forecast check, and prints the `model:` block for apps.yaml.
- New config block `model:` (parameters, planner levels, forecast entities). Without
  `model.params` the shadow reports that it is waiting for a fit.

## 0.1.6 — 2026-10-11

- Works without the internet: when no Agile prices are known (Octopus unreachable long
  enough for the cached rates to run out) the planner holds `planner.no_rates_flow`
  (default: baseline) and **still applies the comfort band**. Before, it returned the
  baseline ahead of the comfort checks, so a cold house got no boost while prices were
  missing. The step test never used prices; alerts already also go to an HA persistent
  notification, so they show on the LAN when push can't get out.

## 0.1.5 — 2026-10-11

- Room targets adjustable from Home Assistant: `targets:` binds the comfort band
  (`comfort_min`, `comfort_max`) and the step test's `test_floor` / `test_ceiling` to
  HA number helpers. Read on every decide tick, and a change triggers a decision straight
  away. apps.yaml values remain the fallback; a value outside sane bounds, or a band
  whose floor isn't below its top, is ignored and logged (`targets.py`).
- The status sensor publishes the effective `comfort_min`, `comfort_max`, `test_floor`
  and `test_ceiling`.

## 0.1.4 — 2026-10-11

- The bus probes no longer sleep 3 s between devices. The spacing dated from when they
  were real `read -f` bus reads; since they became `find -V` cache queries it bought
  nothing and held every 5-minute reconcile for ~10 s ("Excessive time spent in
  callback"). `probes.stagger_seconds` is no longer read.

## 0.1.3 — 2026-10-11

- Writes start the min-flow script with `script.turn_on` instead of calling it and
  waiting. The script's write and read-back take ~30 s, so every write blocked the
  callback until HA's 10 s request timeout and logged "Timed out waiting for request".
  Writes always landed; the next reconcile still verifies them off the bus.

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
