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

A sensor can also freeze rather than vanish: still "available", still the same value.
A flat reading alone proves nothing. Overnight in October 2026 every 179 and 177 Tado
sat on one value for 8-12 h while the others moved 0.0-0.3 K, the slab holding the house
steady. What gives a frozen one away is the other rooms moving while it doesn't. So a
room is taken as stuck when its value hasn't changed for `stuck_hours` (default 4)
while *every* other room reporting has moved at least `stuck_move` (default 1.0 K, its
largest excursion since the room last changed), and it is then treated like a missing
room, estimated from offsets taken before it froze. The first change in its value
clears it. Every other room, not most: on 13 Oct 2026 177's Lotte sat at 22.0 for 6 h
while sun lifted Kitchen 1.0 K and Upstairs 1.4 K, but Tanya only 0.4 K, and it was
genuinely flat (it carried on 22.0 -> 22.1). A median rule called that stuck; replayed
over 10-13 Oct this rule flags nothing.

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
    stuck_hours: float = 4.0
    stuck_move: float = 1.0

    @classmethod
    def from_args(cls, rooms, args: dict | None = None) -> "RoomsConfig":
        """`rooms`: a list of entities, or a {name: entity} mapping."""
        if isinstance(rooms, dict):
            pairs = tuple((str(n), str(e)) for n, e in rooms.items())
        else:
            pairs = tuple((_short(e), str(e)) for e in rooms or [])
        a = dict(args or {})
        return cls(rooms=pairs, **{k: float(v) for k, v in a.items() if k in ("estimate_hours", "alert_minutes", "stuck_hours", "stuck_move")})


def _short(entity: str) -> str:
    return entity.split(".", 1)[-1]


@dataclass
class Seen:
    value: float
    at: dt.datetime                                # last reported (any value)
    offsets: dict = field(default_factory=dict)   # other room -> this value minus its value, last seen together
    changed_at: dt.datetime | None = None          # when the value last changed
    snap: dict | None = None                       # offsets as they were then: what a stuck room is estimated from
    base: dict = field(default_factory=dict)       # the other rooms' values then
    dev: dict = field(default_factory=dict)        # how far each has moved from that since


@dataclass
class Reading:
    coldest: float | None
    warmest: float | None
    values: dict             # name -> value used (measured or estimated)
    estimated: dict          # name -> estimated value
    missing: dict            # name -> minutes since last reading (estimated or dropped)
    dropped: list            # names past estimate_hours, left out
    stuck: dict = field(default_factory=dict)      # name -> hours its value has been frozen


class Rooms:
    def __init__(self, cfg: RoomsConfig):
        self.cfg = cfg
        self.seen: dict[str, Seen] = {}

    def update(self, now: dt.datetime, readings: dict) -> Reading:
        """`readings`: name -> float, or None for unavailable."""
        raw = {n: float(v) for n, v in readings.items() if v is not None}
        changed, stuck = set(), {}
        for n, v in raw.items():
            s = self.seen.get(n)
            others = {m: x for m, x in raw.items() if m != n}
            if s is None or v != s.value or s.changed_at is None:
                # Offsets to rooms not reporting now are kept from when they last were.
                s = self.seen[n] = Seen(v, now, dict(s.offsets) if s else {}, now, None, others, {})
                changed.add(n)
                continue
            s.at = now
            for m, x in others.items():
                if m in s.base:
                    s.dev[m] = max(s.dev.get(m, 0.0), abs(x - s.base[m]))
                else:
                    s.base[m] = x
            moved = [s.dev.get(m, 0.0) for m in others if m in s.base]
            flat_h = (now - s.changed_at).total_seconds() / 3600
            if moved and flat_h >= self.cfg.stuck_hours and min(moved) >= self.cfg.stuck_move:
                stuck[n] = round(flat_h, 1)
        live = {n: v for n, v in raw.items() if n not in stuck}
        for n, v in live.items():
            s = self.seen[n]
            s.offsets.update({m: v - x for m, x in live.items() if m != n})
            if n in changed or s.snap is None:
                s.snap = dict(s.offsets)
        values, estimated, missing, dropped = dict(live), {}, {}, []
        for n, _ in self.cfg.rooms:
            if n in live:
                continue
            s = self.seen.get(n)
            # A stuck room: missing since it froze, estimated from offsets taken before.
            since = None if s is None else (s.changed_at if n in stuck else s.at)
            age_min = None if since is None else (now - since).total_seconds() / 60
            missing[n] = None if age_min is None else round(age_min)
            offs = {} if s is None else (s.snap or s.offsets) if n in stuck else s.offsets
            guesses = [live[m] + off for m, off in offs.items() if m in live]
            if not guesses or age_min > self.cfg.estimate_hours * 60:
                dropped.append(n)
                continue
            values[n] = estimated[n] = round(sum(guesses) / len(guesses), 2)
        return Reading(min(values.values()) if values else None,
                       max(values.values()) if values else None,
                       values, estimated, missing, dropped, stuck)

    # ---- restart tolerance (persist.py) ----
    def dump(self) -> dict:
        return {n: {"value": s.value, "at": s.at.isoformat(), "offsets": s.offsets,
                    "changed_at": s.changed_at.isoformat() if s.changed_at else None,
                    "snap": s.snap, "base": s.base, "dev": s.dev} for n, s in self.seen.items()}

    def load(self, doc: dict | None):
        names = {n for n, _ in self.cfg.rooms}
        for n, d in (doc or {}).items():
            try:
                if n in names:
                    f = lambda k: {str(m): float(o) for m, o in (d.get(k) or {}).items() if m in names}
                    self.seen[n] = Seen(float(d["value"]), dt.datetime.fromisoformat(d["at"]), f("offsets"),
                                        dt.datetime.fromisoformat(d["changed_at"]) if d.get("changed_at") else None,
                                        f("snap") if d.get("snap") is not None else None, f("base"), f("dev"))
            except (KeyError, TypeError, ValueError):
                continue
