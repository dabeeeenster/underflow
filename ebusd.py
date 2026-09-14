"""underflow.ebusd — read registers straight off ebusd's TCP command port.

**Why this exists at all.** Home Assistant's ebusd entities cannot be used to verify
that a setting landed. ebusd publishes to MQTT *only when a decoded value changes*, so
an unchanged register is never republished and HA's `number.*` state is a cache with no
expiry: measured 13 Sep 2026, `Hc1MinFlowTempDesired` had a `last_reported` 19 hours
old, and publishing `ebusd179/ctlv2/Hc1MinFlowTempDesired/get` then waiting 30 s did not
move it. That entity reads `20` just as confidently when the adapter has been offline
for eighteen minutes as when the bus is healthy, and nothing on the entity distinguishes
the two.

`read` on the command port does distinguish them, because ebusd knows when it last
actually had the value off the wire. `read -m SECONDS` returns its cached value only if
it is younger than that, and otherwise goes to the bus; `read -f` is the same thing with
a zero age. Either way the reply is a value of known maximum age or an `ERR:` — never the
indefinitely-old cache an HA entity gives you.

**Prefer `-m` over `-f`.** ebusd already polls these registers, so a cache-tolerant read
is almost always free, and forcing one is not: measured on a healthy bus, `-f` costs
210–390 ms (occasionally 2 s) because it is a real eBUS transaction, against 2.5 ms for
`-m 600`. Over a WiFi-tunnelled `ens:` adapter that transaction is also *timing*-sensitive
— eBUS arbitration is real-time, which is why ebusd runs `--latency=100` here — so extra
forced reads on a marginal link are far more costly than their share of traffic suggests.
Force only when the answer has to be from the wire right now: verifying a write. A
liveness check does not need that, because a stale cache is itself the evidence.

Ports (both instances live on the Home Assistant box; `--httpport=8889` is internal to
each container and not host-mapped, so the command port is the way in):

    177  192.168.1.9:8888     adapter 192.168.1.22
    179  192.168.1.9:8890     adapter 192.168.1.138

Writes do NOT go through here. They go through a bounded Home Assistant script with a
forced read-back, so the limits stay somewhere the user can see them.
"""
from __future__ import annotations

import datetime as dt
import re
import socket
from dataclasses import dataclass

TERMINATOR = "\n\n"
# How far ebusd's clock may run ahead of ours before a `lastup` age is untrustworthy.
# Skew of seconds is routine; beyond this we refuse to judge freshness at all rather
# than mistake a wrong clock for a healthy device.
MAX_CLOCK_SKEW = 120.0
LASTUP = re.compile(r"lastup=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass(frozen=True)
class Reading:
    """One attempt to get one register. `ok` means we have a number.

    `lastup` is ebusd's own record of when it last had this value off the wire, when the
    query supplied one (`find -V` does; `read` does not). It is the provenance that makes
    a cached value safe to act on, and its age is a zero-cost liveness signal.
    """
    value: float | None
    error: str | None
    at: dt.datetime
    lastup: dt.datetime | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None and self.error is None

    def age(self, now: dt.datetime) -> float | None:
        """Seconds since ebusd last had this off the wire, or None if unknown.

        **Can be negative.** ebusd stamps `lastup` in its own container's local time with
        no zone, so this is only as good as the two clocks agreeing. A few seconds of skew
        is normal and harmless; a large negative age means ebusd's clock is ahead, and the
        result must NOT be read as "very fresh" — that would make a dead device look alive
        for ever. Callers guard with `MAX_CLOCK_SKEW`; the raw signed value is returned
        here deliberately, so that a persistent skew stays visible instead of being
        silently clamped away.
        """
        if self.lastup is None:
            return None
        lastup = self.lastup if self.lastup.tzinfo else self.lastup.replace(tzinfo=now.tzinfo)
        return (now - lastup).total_seconds()


def parse_read(raw: str) -> tuple[float | None, str | None]:
    """Turn one `read` reply into (value, error). Exactly one of the two is None.

    ebusd answers a scalar read with the bare value and a blank line ("20\\n\\n"), a
    failure with "ERR: <reason>", and a multi-field message with ";"-separated fields.
    `read -v` and some definitions prefix the value with "name=", hence the rsplit.
    """
    text = (raw or "").strip()
    if not text:
        return None, "empty response"
    first = text.splitlines()[0].strip()
    if first.upper().startswith("ERR"):
        return None, first
    if "=" in first:
        first = first.rsplit("=", 1)[1].strip()
    field = first.split(";")[0].strip()
    try:
        return float(field), None
    except ValueError:
        return None, f"unparseable response {first!r}"


def parse_find(raw: str) -> tuple[float | None, dt.datetime | None, str | None]:
    """Parse one `find -V` reply into (value, lastup, error).

    The shape is  `<circuit> <name> = value=20.5 °C [Label] [ZZ=15, lastup=..., active read]`
    with the fields before the first bracket and the metadata after. Messages with named
    fields (`error=-;error_1=-;...`) have no single number, which is fine: `lastup` alone
    is what liveness needs, so a missing value is not an error here.
    """
    text = (raw or "").strip()
    if not text:
        return None, None, "empty response"
    first = text.splitlines()[0].strip()
    if first.upper().startswith("ERR"):
        return None, None, first
    m = LASTUP.search(first)
    lastup = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None
    body = first.split("[", 1)[0]
    body = body.split(" = ", 1)[1] if " = " in body else body
    field = body.split(";")[0].strip()
    if "=" in field:
        field = field.rsplit("=", 1)[1].strip()
    try:
        value = float(field.split()[0]) if field else None
    except (ValueError, IndexError):
        value = None
    if lastup is None and value is None:
        return None, None, f"no value or lastup in {first!r}"
    return value, lastup, None


class Ebusd:
    """Minimal client for one ebusd instance's command port.

    A fresh connection per command: they are local, cost under a millisecond, and a
    short-lived socket cannot be left half-open by a dropped adapter. `connect` is
    injectable so the tests never touch a real socket.
    """

    def __init__(self, host: str, port: int, timeout: float = 4.0, connect=None, clock=None):
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self._connect = connect or (lambda: socket.create_connection((self.host, self.port), self.timeout))
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def command(self, cmd: str) -> str:
        """Send one command, return its raw reply. Raises OSError if the socket fails."""
        sock = self._connect()
        try:
            sock.settimeout(self.timeout)
            sock.sendall((cmd.rstrip("\n") + "\n").encode())
            chunks: list[str] = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data.decode("utf-8", "replace"))
                if "".join(chunks).endswith(TERMINATOR):
                    break
            return "".join(chunks)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def read_register(self, circuit: str, name: str, max_age: float | None = None) -> Reading:
        """Read one register, accepting a cached value up to `max_age` seconds old.

        `max_age=0` forces a real bus transaction (ebusd's `-f`); `None` leaves ebusd to
        its own default. Prefer a non-zero age — see the module docstring for why forcing
        is expensive on these adapters. Never raises: a dead bus is a Reading with an
        error, which is a normal, expected outcome several times an hour.
        """
        if max_age is None:
            age = ""
        elif max_age <= 0:
            age = "-f "
        else:
            age = f"-m {int(max_age)} "
        cmd = f"read {age}-c {circuit} {name}"
        try:
            raw = self.command(cmd)
        except (OSError, socket.timeout) as exc:
            return Reading(None, f"{type(exc).__name__}: {exc}", self._clock())
        value, error = parse_read(raw)
        return Reading(value, error, self._clock())

    def find_register(self, circuit: str, name: str) -> Reading:
        """Ask ebusd what it holds and when it last had it off the wire.

        **This puts nothing on the bus.** It queries ebusd's own state, so it is free to
        call as often as you like — which is the whole point on an adapter that tunnels a
        real-time protocol over a marginal 2.4 GHz link. The returned `lastup` is the
        authoritative answer to "is this device still being read successfully", because
        ebusd only advances it on a successful read from the wire.
        """
        try:
            raw = self.command(f"find -V -c {circuit} {name}")
        except (OSError, socket.timeout) as exc:
            return Reading(None, f"{type(exc).__name__}: {exc}", self._clock())
        value, lastup, error = parse_find(raw)
        return Reading(value, error, self._clock(), lastup)
