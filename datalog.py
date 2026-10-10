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
