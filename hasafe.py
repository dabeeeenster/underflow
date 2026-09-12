"""underflow.hasafe — publish values through AppDaemon without them being mangled.

Pure; no HA or AppDaemon dependency, so it is testable offline.
"""
from __future__ import annotations


def ha_safe(value):
    """Make a value survive AppDaemon's HTTP kwarg cleaning unmangled.

    On the way to HA's REST API, AppDaemon 4.x runs attributes through
    `clean_http_kwargs`, which calls `remove_literals(cleaned, (None, False))`. That
    uses the `in` operator, so membership is tested with `==` rather than `is`, and
    since `0 == 0.0 == False` in Python every zero is dropped along with every False.
    A pump drawing 0.0 kW therefore arrives at HA as a *missing* attribute -- "off" and
    "no reading" become indistinguishable -- and `cheap: False` disappears from every
    slot in the plan sensor.

    (`clean_kwargs` also maps True to the string "true", but that is a singleton
    pattern matched with `is`, so it catches only True itself and leaves 1 and 1.0
    alone. Harmless; it is the pruning that loses data.)

    Numbers therefore go over as strings and booleans as explicit "true"/"false", both
    of which pass through untouched. None is left alone: it is pruned, and an absent
    attribute is a fair reading of "no value". Consumers should cast, e.g. `| float(0)`.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return {k: ha_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [ha_safe(v) for v in value]
    return value
