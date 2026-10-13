"""underflow.rooms — the comfort rooms, carrying on when a thermostat drops out (0.6.0).

Comfort is judged on the coldest of a few room sensors (the Tados), and the step-test
ceiling on the warmest. Taking the min over whichever happen to be available makes a
dropout silently move the answer: on 179 the Living Room is the coldest room 71% of the
time, the next coldest reads a median 0.5 K (90th percentile 0.9 K) warmer, so losing
Living's Tado would make the house look half a degree warmer and the planner heat less.

So a room that drops out is estimated rather than ignored. While it reports, the tracker
keeps its offset from each other room reporting with it. Once it stops, its estimate is
the mean, over the rooms still reporting, of that room's reading plus the offset: if
Living ran 0.7 K below Bedrooms and 0.9 K below Upstairs, it is taken to still be, as the
house warms or cools. Offsets are pairwise so the estimate doesn't jump when a second
room drops out and the set it is measured against changes.
The estimate is kept for `estimate_hours` (default 12); after that the room is dropped and
the min/max is over what is left. With no room reporting at all there is nothing to
anchor an estimate to, so there is no reading, and the pump falls back as it does for any
missing room reading.

Pure: no HA.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoomsConfig:
    rooms: tuple = field(default_factory=tuple)    # ((name, entity), ...)
    estimate_hours: float = 12.0
    alert_minutes: float = 30.0

    @classmethod
    def from_args(cls, rooms, args: dict | None = None) -> "RoomsConfig":
        """`rooms`: a list of entities, or a {name: entity} mapping."""
        if isinstance(rooms, dict):
            pairs = tuple((str(n), str(e)) for n, e in rooms.items())
        else:
            pairs = tuple((_short(e), str(e)) for e in rooms or [])
        a = dict(args or {})
        return cls(rooms=pairs, **{k: float(v) for k, v in a.items() if k in ("estimate_hours", "alert_minutes")})


def _short(entity: str) -> str:
    return entity.split(".", 1)[-1]


@dataclass
class Seen:
    value: float
    at: dt.datetime
    offsets: dict = field(default_factory=dict)   # other room -> this value minus its value, last seen together


@dataclass
class Reading:
    coldest: float | None
    warmest: float | None
    values: dict             # name -> value used (measured or estimated)
    estimated: dict          # name -> estimated value
    missing: dict            # name -> minutes since last reading (estimated or dropped)
    dropped: list            # names past estimate_hours, left out


class Rooms:
    def __init__(self, cfg: RoomsConfig):
        self.cfg = cfg
        self.seen: dict[str, Seen] = {}

    def update(self, now: dt.datetime, readings: dict) -> Reading:
        """`readings`: name -> float, or None for unavailable."""
        live = {n: float(v) for n, v in readings.items() if v is not None}
        for n, v in live.items():
            prev = self.seen.get(n)
            # Offsets to rooms not reporting now are kept from when they last were.
            offsets = dict(prev.offsets) if prev else {}
            offsets.update({m: v - x for m, x in live.items() if m != n})
            self.seen[n] = Seen(v, now, offsets)
        values, estimated, missing, dropped = dict(live), {}, {}, []
        for n, _ in self.cfg.rooms:
            if n in live:
                continue
            s = self.seen.get(n)
            age_min = None if s is None else (now - s.at).total_seconds() / 60
            missing[n] = None if age_min is None else round(age_min)
            guesses = [live[m] + off for m, off in (s.offsets.items() if s else []) if m in live]
            if not guesses or age_min > self.cfg.estimate_hours * 60:
                dropped.append(n)
                continue
            values[n] = estimated[n] = round(sum(guesses) / len(guesses), 2)
        return Reading(min(values.values()) if values else None,
                       max(values.values()) if values else None,
                       values, estimated, missing, dropped)

    # ---- restart tolerance (persist.py) ----
    def dump(self) -> dict:
        return {n: {"value": s.value, "at": s.at.isoformat(), "offsets": s.offsets} for n, s in self.seen.items()}

    def load(self, doc: dict | None):
        names = {n for n, _ in self.cfg.rooms}
        for n, d in (doc or {}).items():
            try:
                if n in names:
                    self.seen[n] = Seen(float(d["value"]), dt.datetime.fromisoformat(d["at"]),
                                        {str(m): float(o) for m, o in (d.get("offsets") or {}).items() if m in names})
            except (KeyError, TypeError, ValueError):
                continue
