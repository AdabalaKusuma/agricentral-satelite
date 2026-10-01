"""JSON.stringify-compatible serialisation, so footprint hashes match the Worker's cache keys."""
from __future__ import annotations

import hashlib
import math
from typing import Any


def _number(v: float) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if not math.isfinite(v):
        return "null"
    if v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    s = repr(v)
    if "e" in s:
        mant, exp = s.split("e")
        exp_i = int(exp)
        s = f"{mant}e{'+' if exp_i > 0 else '-'}{abs(exp_i)}"
    return s


def js_stringify(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        out = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return f'"{out}"'
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(js_stringify(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{js_stringify(str(k))}:{js_stringify(v)}" for k, v in value.items() if v is not None or True) + "}"
    return js_stringify(str(value))


def footprint_of(geometry: dict) -> str:
    return js_stringify([[z["id"], z["rings"]] for z in geometry["zones"]])


def footprint_hash(geometry: dict) -> str:
    """SHA-256 of the zone ids and rings, first 12 bytes as hex, as the Worker computes it."""
    return hashlib.sha256(footprint_of(geometry).encode("utf-8")).hexdigest()[:24]
