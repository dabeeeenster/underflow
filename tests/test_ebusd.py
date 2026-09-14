# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_ebusd.py"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ebusd import Ebusd, Reading, parse_find, parse_read

# --- parsing ebusd's replies -------------------------------------------------
assert parse_read("20\n\n") == (20.0, None)
assert parse_read("0.6\n\n") == (0.6, None)
assert parse_read("  45  \n\n") == (45.0, None)
assert parse_read("value=20\n\n") == (20.0, None)             # read -v style
assert parse_read("Hc1MinFlowTempDesired = value=32\n\n") == (32.0, None)
assert parse_read("20;on;3\n\n") == (20.0, None)              # multi-field message

for bad, frag in [("ERR: read timed out\n\n", "ERR"),
                  ("ERR: element not found\n\n", "ERR"),
                  ("", "empty"),
                  ("   \n\n", "empty"),
                  ("no signal\n\n", "unparseable")]:
    v, e = parse_read(bad)
    assert v is None and e and frag.lower() in e.lower(), (bad, v, e)

# --- the socket layer, without a socket --------------------------------------
class FakeSock:
    def __init__(self, reply, chunk=4):
        self.reply, self.chunk, self.sent, self.closed = reply.encode(), chunk, b"", False
        self.pos = 0
    def settimeout(self, t): pass
    def sendall(self, b): self.sent += b
    def recv(self, n):
        out = self.reply[self.pos:self.pos + self.chunk]; self.pos += len(out); return out
    def close(self): self.closed = True

sock = FakeSock("20\n\n")
bus = Ebusd("h", 1, connect=lambda: sock)
r = bus.read_register("ctlv2", "Hc1MinFlowTempDesired", max_age=600)
assert r.ok and r.value == 20.0 and r.error is None, r
assert sock.sent == b"read -m 600 -c ctlv2 Hc1MinFlowTempDesired\n", sock.sent
assert sock.closed, "socket must be closed even on the happy path"

# The age argument is the whole cost control: a forced read is a real eBUS transaction
# (210-390 ms measured) where a cache-tolerant one is 2.5 ms, and on a WiFi-tunnelled
# adapter the forced one is also timing-sensitive. Assert both spellings reach the wire.
sock2 = FakeSock("20\n\n")
Ebusd("h", 1, connect=lambda: sock2).read_register("ctlv2", "X", max_age=0)
assert sock2.sent == b"read -f -c ctlv2 X\n", sock2.sent
sock3 = FakeSock("20\n\n")
Ebusd("h", 1, connect=lambda: sock3).read_register("ctlv2", "X")
assert sock3.sent == b"read -c ctlv2 X\n", sock3.sent      # None = ebusd's own default
sock4 = FakeSock("20\n\n")
Ebusd("h", 1, connect=lambda: sock4).read_register("ctlv2", "X", max_age=240.7)
assert sock4.sent == b"read -m 240 -c ctlv2 X\n", sock4.sent

# A bus that does not answer is a normal outcome, not an exception.
def boom():
    raise OSError("Connection refused")
r = Ebusd("h", 1, connect=boom).read_register("ctlv2", "X")
assert not r.ok and r.value is None and "Connection refused" in r.error, r
assert isinstance(r.at, dt.datetime)

r = Ebusd("h", 1, connect=lambda: FakeSock("ERR: read timed out\n\n")).read_register("c", "X")
assert not r.ok and "ERR" in r.error, r

# --- find -V: value plus provenance, and nothing on the bus ------------------
LIVE = "hmu FlowTemp = value=72.12 \u00b0C [Temperatur] [ZZ=08, lastup=2026-09-14 08:55:05, active read]\n\n"
v, lastup, err_ = parse_find(LIVE)
assert v == 72.12 and err_ is None and lastup == dt.datetime(2026, 9, 14, 8, 55, 5), (v, lastup, err_)

# Named-field messages have no single number but do have a lastup — still a valid probe.
v, lastup, err_ = parse_find("ctlv2 Currenterror = error=-;error_1=- [Fehler] [ZZ=15, lastup=2026-09-14 08:58:30, active read]\n\n")
assert v is None and err_ is None and lastup.minute == 58, (v, lastup, err_)

v, lastup, err_ = parse_find("hmu RunStatsCompressorStarts = value=8442 [Anzahl] [ZZ=08, lastup=2026-09-14 08:58:37, active read]\n\n")
assert v == 8442.0 and lastup is not None

for bad in ("ERR: element not found\n\n", "", "nonsense with no lastup\n\n"):
    v, lastup, err_ = parse_find(bad)
    assert v is None and lastup is None and err_, bad

sock5 = FakeSock(LIVE)
r = Ebusd("h", 1, connect=lambda: sock5).find_register("hmu", "FlowTemp")
assert sock5.sent == b"find -V -c hmu FlowTemp\n", sock5.sent
assert r.ok and r.value == 72.12 and r.lastup is not None

# age() is the liveness signal; it must tolerate ebusd's naive timestamps against an
# aware clock, since AppDaemon hands us tz-aware datetimes.
now = dt.datetime(2026, 9, 14, 9, 0, 0, tzinfo=dt.timezone.utc)
assert abs(r.age(now) - 295) < 2, r.age(now)
assert Reading(1.0, None, now).age(now) is None, "no lastup -> unknown age, not zero"

r = Ebusd("h", 1, connect=boom).find_register("hmu", "FlowTemp")
assert not r.ok and r.lastup is None and "Connection refused" in r.error

print("ebusd OK: read parser, -m/-f age control, find -V value+lastup, dead bus returns a Reading")
