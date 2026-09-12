"""ha_safe must survive AppDaemon's HTTP kwarg cleaning without loss or mangling.

The two functions below are copied verbatim from appdaemon/utils.py (4.x) so the test
fails loudly if that behaviour ever changes and the workaround becomes unnecessary.
"""
import sys, pathlib
from collections.abc import Mapping, Iterable
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from hasafe import ha_safe


def clean_kwargs(val, *, http=False):
    match val:
        case True if http:
            return "true"
        case str() | int() | float() | bool() | None:
            return val
        case Mapping():
            return {k: clean_kwargs(v, http=http) for k, v in val.items()}
        case Iterable():
            return [clean_kwargs(v, http=http) for v in val]
        case _:
            return str(val)


def remove_literals(val, literal):
    match val:
        case str():
            return val
        case Mapping():
            return {k: remove_literals(v, literal) for k, v in val.items() if v not in literal}
        case Iterable():
            return [remove_literals(v, literal) for v in val if v not in literal]
        case _:
            return val


def through_appdaemon(attrs):
    return remove_literals(clean_kwargs(attrs, http=True), (None, False))


# The bug, demonstrated on values this app really publishes.
raw = {"power_in_kw": 0.0, "power_out_kw": 0.0, "heat_curve": 1.0,
       "dry_run": False, "cheap_slots": 1, "min_flow_temp": 20.0, "cop_now": None}
mangled = through_appdaemon(raw)
assert "power_in_kw" not in mangled, "expected AppDaemon to drop 0.0"
assert "dry_run" not in mangled, "expected AppDaemon to drop False"
assert mangled["heat_curve"] == 1.0, "1.0 is untouched: `case True` is matched with `is`"
assert mangled["cheap_slots"] == 1
assert through_appdaemon({"b": True})["b"] == "true", "only the True singleton is stringified"
print("reproduced AppDaemon mangling:", mangled)

# ha_safe prevents all of it.
safe = through_appdaemon(ha_safe(raw))
assert safe["power_in_kw"] == "0.0", safe
assert safe["power_out_kw"] == "0.0", safe
assert safe["heat_curve"] == "1.0", safe
assert safe["dry_run"] == "false", safe
assert safe["cheap_slots"] == "1", safe
assert safe["min_flow_temp"] == "20.0", safe
assert "cop_now" not in safe, "None is still pruned, which reads as 'no value'"
assert float(safe["power_in_kw"]) == 0.0 and float(safe["heat_curve"]) == 1.0

# Nested structures too: the plan sensor's slot list lost every `cheap: False`.
slots = [{"start": "2026-09-12T10:00:00+01:00", "price": -0.026, "cheap": True, "min_flow": 32},
         {"start": "2026-09-12T11:00:00+01:00", "price": 0.11, "cheap": False, "min_flow": 20}]
assert "cheap" not in through_appdaemon(slots)[1], "expected AppDaemon to drop cheap=False"
safe_slots = through_appdaemon(ha_safe(slots))
assert safe_slots[0]["cheap"] == "true" and safe_slots[1]["cheap"] == "false", safe_slots
assert safe_slots[1]["min_flow"] == "20", safe_slots

# Strings and None pass through untouched.
assert ha_safe("standby") == "standby" and ha_safe(None) is None
print("ha_safe OK")
