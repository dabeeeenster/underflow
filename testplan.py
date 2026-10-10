"""underflow.testplan — step tests for fitting the house model (stage 2).

The rule-based planner makes data that is hard to fit: boosts land on whatever slots
are cheap, mostly in daylight when 179's south glazing is also heating the rooms, and
the lag between heat in and warmer rooms (~1.5-2.5 h dead time, ~3 h to a visible rise,
measured 10 Oct 2026) is lost in it. A step test replaces the planner for a fixed
schedule of blocks, each either a minimum flow temperature or a true "off":

  * a number   -> zone 1 on (`day`), minimum flow at that value
  * "off"      -> zone 1 `off`: no space heating at all, whatever the Tados ask.
                  A low minimum flow is NOT off — with the Tados calling, the curve
                  alone still gives ~25 °C flow (177 showed this all night on 9 Oct).

Block lengths are deliberately mixed (2-6 h) so the fit can separate the fast room
response from the slow slab, and price is ignored (Ben, 10 Oct 2026: he would rather
pay for better data).

Two guards, each latched for the rest of its block once tripped:
  * floor   — an "off" block runs for hours, so if the coldest room falls to `floor`
              the block is abandoned for `guard_flow`.
  * ceiling — for the tests the Tados are opened to 25 °C so they don't cut heat blocks
              short (a satisfied Tado stops calling and the pump stops, which censors
              the step). Instead, if the warmest room reaches `ceiling` a heat block is
              ended early with the zone off.

Pure: no HA, no sockets.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

OFF = "off"


@dataclass(frozen=True)
class TestConfig:
    enabled: bool = False
    start: str | None = None                  # ISO datetime with offset, e.g. 2026-10-10T18:00:00+01:00
    blocks: tuple = field(default_factory=tuple)   # ((hours, level), ...); level a °C or "off"
    floor: float = 19.0                       # coldest room at/below this ends an off block early
    guard_flow: float = 25.0                  # min flow used while the guard holds
    ceiling: float | None = None              # warmest room at/above this ends a heat block early

    @classmethod
    def from_args(cls, args: dict | None) -> "TestConfig":
        a = dict(args or {})
        blocks = tuple((float(h), _level(l)) for h, l in (a.pop("blocks", None) or []))
        known = {k: v for k, v in a.items() if k in cls.__dataclass_fields__}
        return cls(blocks=blocks, **known)

    def start_at(self) -> dt.datetime | None:
        if not self.start:
            return None
        t = dt.datetime.fromisoformat(str(self.start))
        if t.tzinfo is None:
            raise ValueError("test.start needs a UTC offset, e.g. +01:00")
        return t

    def end_at(self) -> dt.datetime | None:
        s = self.start_at()
        return None if s is None else s + dt.timedelta(hours=sum(h for h, _ in self.blocks))


def _level(v):
    if isinstance(v, str) and v.strip().lower() == OFF:
        return OFF
    return float(v)


@dataclass(frozen=True)
class Block:
    index: int
    level: object            # float or OFF
    start: dt.datetime
    end: dt.datetime


def current_block(cfg: TestConfig, now: dt.datetime) -> Block | None:
    """The block `now` falls in, or None before the start, after the end, or disabled."""
    if not cfg.enabled or not cfg.blocks:
        return None
    t = cfg.start_at()
    if t is None or now < t:
        return None
    for i, (hours, level) in enumerate(cfg.blocks):
        end = t + dt.timedelta(hours=hours)
        if now < end:
            return Block(i, level, t, end)
        t = end
    return None


@dataclass(frozen=True)
class Step:
    active: bool
    min_flow: float | None = None     # what to write to the min-flow register
    zone: str | None = None           # "off" / "day"; None = leave the zone alone
    reason: str = ""
    block: Block | None = None
    guarded: bool = False          # floor guard holding (off block abandoned)
    capped: bool = False           # ceiling guard holding (heat block ended)


def test_step(cfg: TestConfig, now: dt.datetime, coldest: float | None,
              baseline: float, guard_latched: bool = False,
              warmest: float | None = None, cap_latched: bool = False) -> Step:
    """What the test wants right now. `guard_latched` is the caller's memory that the
    guard already tripped in this block, so a room recovering by 0.1 K does not flip the
    zone back off and chatter."""
    b = current_block(cfg, now)
    if b is None:
        return Step(False)
    left = (b.end - now).total_seconds() / 3600
    tag = f"test block {b.index + 1}/{len(cfg.blocks)}, {left:.1f} h left"
    if b.level == OFF:
        if guard_latched or (coldest is not None and coldest <= cfg.floor):
            room = "" if coldest is None else f" (coldest room {coldest:.1f} °C)"
            return Step(True, cfg.guard_flow, "day",
                        f"{tag}: off block abandoned at the {cfg.floor:g} °C floor{room}", b, True)
        # Leave the min-flow register at baseline so nothing high is waiting when the
        # zone comes back on.
        return Step(True, baseline, "off", f"{tag}: heating off", b)
    if cfg.ceiling is not None and (cap_latched or (warmest is not None and warmest >= cfg.ceiling)):
        room = "" if warmest is None else f" (warmest room {warmest:.1f} °C)"
        return Step(True, baseline, "off",
                    f"{tag}: heat block ended at the {cfg.ceiling:g} °C ceiling{room}", b, capped=True)
    return Step(True, float(b.level), "day", f"{tag}: min flow {b.level:g} °C", b)
