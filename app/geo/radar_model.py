"""Sentinel-1 radar rules, ported from public/radar-model.js with the same policy numbers.

Radar measures structure and water content, not greenness. Values are averaged over a window
in linear power before conversion to decibels, and only scenes from one relative orbit are
compared. A change is a reason to inspect, never a diagnosis.
"""
from __future__ import annotations

import math

import numpy as np

from ..util.numbers import fixed, is_finite, median
from ..util.time import parse_ms

RADAR_VERSION = "coffee-radar-2026-09-v1"
RADAR_POLICY = {"windowDays": 90, "maxScenes": 8, "minReference": 2, "speckleCells": 1, "changeDb": 2, "smallDb": 1, "wetDb": 1.5, "gridResolution": 20, "paddingM": 60}


def to_db(v) -> float | None:
    return 10 * math.log10(v) if is_finite(v) and v > 0 else None


def speckle_mean(values: np.ndarray, width: int, height: int, radius: int = RADAR_POLICY["speckleCells"]) -> np.ndarray:
    """Mean of linear power over a (2r+1)² window, ignoring NaN and non-positive cells."""
    v = np.asarray(values, dtype=np.float64).reshape(height, width)
    ok = np.isfinite(v) & (v > 0)
    vals = np.where(ok, v, 0.0)
    cnt = ok.astype(np.float64)
    pad_v = np.pad(vals, radius)
    pad_c = np.pad(cnt, radius)
    sum_ = np.zeros_like(v)
    n = np.zeros_like(v)
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            sum_ += pad_v[radius + dr: radius + dr + height, radius + dc: radius + dc + width]
            n += pad_c[radius + dr: radius + dr + height, radius + dc: radius + dc + width]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(n > 0, sum_ / n, np.nan)
    return out.reshape(-1)


def choose_orbit(scenes: list[dict]) -> dict | None:
    """The viewing geometry with the most passes, newest first; ties go to the newest pass."""
    groups: dict[str, list[dict]] = {}
    for s in scenes:
        key = f"{s.get('orbitState') or '?'}-{s.get('relativeOrbit') if s.get('relativeOrbit') is not None else '?'}"
        groups.setdefault(key, []).append(s)
    best = None
    for key, lst in groups.items():
        lst.sort(key=lambda s: -(parse_ms(s.get("acquiredAt")) or 0))
        if best is None or len(lst) > len(best["list"]) or (len(lst) == len(best["list"]) and (parse_ms(lst[0]["acquiredAt"]) or 0) > (parse_ms(best["list"][0]["acquiredAt"]) or 0)):
            best = {"key": key, "list": lst}
    return best


def radar_change(scenes: list[dict], positions: list[dict]) -> dict | None:
    """Newest scene against the median of the earlier same-orbit passes, per cell."""
    if not scenes:
        return None
    latest, earlier = scenes[0], scenes[1:]
    refs = earlier[: RADAR_POLICY["maxScenes"] - 1]
    cells = []
    for p in positions:
        i = p["i"]
        vv, vh = latest["vvDb"][i], latest["vhDb"][i]
        ref_vv = median(s["vvDb"][i] for s in refs)
        ref_vh = median(s["vhDb"][i] for s in refs)
        n = sum(1 for s in refs if is_finite(s["vhDb"][i]))
        ok = is_finite(vv) and is_finite(vh) and is_finite(ref_vv) and is_finite(ref_vh) and n >= RADAR_POLICY["minReference"]
        cells.append({**p, "vvDb": fixed(vv, 2), "vhDb": fixed(vh, 2), "refVvDb": fixed(ref_vv, 2), "refVhDb": fixed(ref_vh, 2),
                      "ratioDb": fixed(vh - vv, 2) if is_finite(vv) and is_finite(vh) else None,
                      "dVh": fixed(vh - ref_vh, 2) if ok else None, "dVv": fixed(vv - ref_vv, 2) if ok else None, "refScenes": n})
    inside = [c for c in cells if c["zoneId"] and is_finite(c["dVh"])]
    zone_cells = max(1, sum(1 for c in cells if c["zoneId"]))
    stats = {"dVh": {"min": min(c["dVh"] for c in inside), "max": max(c["dVh"] for c in inside)}, "dVv": {"min": min(c["dVv"] for c in inside), "max": max(c["dVv"] for c in inside)}} if inside else None
    return {"version": RADAR_VERSION,
            "latest": {"id": latest["id"], "acquiredAt": latest["acquiredAt"], "orbitState": latest.get("orbitState"), "relativeOrbit": latest.get("relativeOrbit")},
            "reference": {"count": len(refs), "from": refs[-1]["acquiredAt"] if refs else None, "to": refs[0]["acquiredAt"] if refs else None},
            "cells": cells, "coverage": len(inside) / zone_cells, "stats": stats,
            "thresholds": {"changeDb": RADAR_POLICY["changeDb"], "smallDb": RADAR_POLICY["smallDb"], "wetDb": RADAR_POLICY["wetDb"]}}


def change_class(d_db) -> str:
    if not is_finite(d_db):
        return "none"
    a = abs(d_db)
    if a >= RADAR_POLICY["changeDb"]:
        return "drop" if d_db < 0 else "rise"
    if a >= RADAR_POLICY["smallDb"]:
        return "small-drop" if d_db < 0 else "small-rise"
    return "steady"
