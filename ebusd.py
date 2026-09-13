"""underflow.ebusd — read registers straight off ebusd's TCP command port.

**Why this exists at all.** Home Assistant's ebusd entities cannot be used to verify
that a setting landed. ebusd publishes to MQTT *only when a decoded value changes*, so
an unchanged register is never republished and HA's `number.*` state is a cache with no
expiry: measured 13 Sep 2026, `Hc1MinFlowTempDesired` had a `last_reported` 19 hours
old, and publishing `ebusd179/ctlv2/Hc1MinFlowTempDesired/get` then waiting 30 s did not
move it. That entity reads `20` just as confidently when the adapter has been offline
for eighteen minutes as when the bus is healthy, and nothing on the entity distinguishes
the two.

`read -f` on the command port does distinguish them. The `-f` forces a fresh read from
the bus instead of serving ebusd's cache, so the reply is either a value that was on the
wire moments ago or an `ERR:`. One call is both the reading and the liveness probe.

Ports (both instances live on the Home Assistant box; `--httpport=8889` is internal to
each container and not host-mapped, so the command port is the way in):

    177  192.168.1.9:8888     adapter 192.168.1.22
    179  192.168.1.9:8890     adapter 192.168.1.138

Writes do NOT go through here. They go through a bounded Home Assistant script with a
forced read-back, so the limits stay somewhere the user can see them.
"""
from __future__ import annotations

import datetime as dt
import socket
from dataclasses import dataclass

TERMINATOR = "\n\n"


@dataclass(frozen=True)
class Reading:
    """One attempt to read one register. `ok` means the bus answered with a number."""
    value: float | None
    error: str | None
    at: dt.datetime

    @property
    def ok(self) -> bool:
        return self.value is not None and self.error is None


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

    def read_register(self, circuit: str, name: str, force: bool = True) -> Reading:
        """Force a fresh bus read of one register. Never raises: a dead bus is a Reading
        with an error, which is a normal, expected outcome several times an hour."""
        cmd = f"read {'-f ' if force else ''}-c {circuit} {name}"
        try:
            raw = self.command(cmd)
        except (OSError, socket.timeout) as exc:
            return Reading(None, f"{type(exc).__name__}: {exc}", self._clock())
        value, error = parse_read(raw)
        return Reading(value, error, self._clock())
