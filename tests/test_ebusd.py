# /// script
# requires-python = ">=3.11"
# ///
"""Run: uv run tests/test_ebusd.py"""
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ebusd import Ebusd, parse_read

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
r = bus.read_register("ctlv2", "Hc1MinFlowTempDesired")
assert r.ok and r.value == 20.0 and r.error is None, r
assert sock.sent == b"read -f -c ctlv2 Hc1MinFlowTempDesired\n", sock.sent
assert sock.closed, "socket must be closed even on the happy path"

# -f is what makes the reading fresh rather than ebusd's cache; the whole design
# rests on it, so assert it is really on the wire.
sock2 = FakeSock("20\n\n")
Ebusd("h", 1, connect=lambda: sock2).read_register("ctlv2", "X", force=False)
assert sock2.sent == b"read -c ctlv2 X\n", sock2.sent

# A bus that does not answer is a normal outcome, not an exception.
def boom():
    raise OSError("Connection refused")
r = Ebusd("h", 1, connect=boom).read_register("ctlv2", "X")
assert not r.ok and r.value is None and "Connection refused" in r.error, r
assert isinstance(r.at, dt.datetime)

r = Ebusd("h", 1, connect=lambda: FakeSock("ERR: read timed out\n\n")).read_register("c", "X")
assert not r.ok and "ERR" in r.error, r

print("ebusd OK: parser, -f flag, socket close, dead bus returns a Reading not an exception")
