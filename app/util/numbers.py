"""Number helpers that reproduce the Worker's JavaScript semantics."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def is_finite(value: Any) -> bool:
    """`Number.isFinite` for JSON values: ints and floats only, never booleans or strings."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_number(value: Any) -> float | int | None:
    return value if is_finite(value) else None


def fixed(value: Any, digits: int) -> float | None:
    """`+(x).toFixed(d)`: round half up on the exact binary value, returned as a number.

    Returns None when the value is not finite, so callers can pass the result straight into JSON.
    """
    if not is_finite(value):
        return None
    if digits <= 0:
        return float(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(value).quantize(q, rounding=ROUND_HALF_UP))


def median(values) -> float | None:
    v = sorted(x for x in values if is_finite(x))
    if not v:
        return None
    n = len(v)
    return v[(n - 1) // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def clean(obj: Any) -> Any:
    """Replace NaN and infinities by None recursively so the JSON is valid and matches the Worker."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj
