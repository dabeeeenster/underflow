"""underflow.datalog — a 5-minute CSV log for fitting the models offline.

HA keeps full-resolution history for 14 days and only hourly means after that, which
is too coarse for a lag of ~3 h and too short for a season. So every reconcile tick
appends one row to a monthly CSV next to the app's config. One file per month; if the
column set changes (new config), a new file is started rather than mixing headers.

Pure apart from the file write: no HA.
"""
from __future__ import annotations

import csv
import datetime as dt
import os


def row_path(directory: str, prefix: str, now: dt.datetime, columns: list[str]) -> str:
    base = os.path.join(directory, f"{prefix}-{now:%Y-%m}.csv")
    n = 1
    path = base
    while os.path.exists(path):
        with open(path, newline="") as f:
            header = next(csv.reader(f), None)
        if header == columns:
            return path
        n += 1
        path = base[:-4] + f"-{n}.csv"
    return path


def append(directory: str, prefix: str, now: dt.datetime, row: dict) -> str:
    """Append `row` (ordered dict-like; `ts` first) and return the file written."""
    os.makedirs(directory, exist_ok=True)
    columns = list(row.keys())
    path = row_path(directory, prefix, now, columns)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(columns)
        w.writerow(["" if v is None else v for v in row.values()])
    return path


class TimeAverage:
    """Time-weighted mean of a sensor between log rows. A 5-minute snapshot of PV output
    jumps between 200 and 1,700 W as clouds pass; the model needs the energy that
    arrived over the interval, which is the mean, not whatever the last sample was.

    Feed it every state change with `update(value, at)`; `take(now)` returns the mean
    since the previous take (or since the first update) and starts a new interval.
    Unknown/unavailable samples are skipped rather than counted as zero.
    """

    def __init__(self):
        self.value = None      # last good value, held until the next change
        self.since = None      # when `value` took effect within the current interval
        self.start = None      # start of the current interval
        self.area = 0.0        # value-seconds accumulated in the current interval
        self.covered = 0.0     # seconds the interval had a known value

    def update(self, value, at):
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = None
        self._accrue(at)
        self.value = v
        self.since = at
        if self.start is None:
            self.start = at

    def _accrue(self, at):
        if self.value is not None and self.since is not None:
            dt_s = (at - self.since).total_seconds()
            if dt_s > 0:
                self.area += self.value * dt_s
                self.covered += dt_s

    def take(self, now):
        self._accrue(now)
        mean = self.area / self.covered if self.covered > 0 else self.value
        self.area, self.covered = 0.0, 0.0
        self.start, self.since = now, (now if self.value is not None else None)
        return mean
