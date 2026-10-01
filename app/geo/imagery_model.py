"""Photo-base rules, ported from public/imagery-model.js: the true-colour stretch, the
clearest-scene choice and the Web Mercator tile maths used by the farm-bounded tile proxy."""
from __future__ import annotations

import math

import numpy as np

from ..util.time import parse_ms

IMAGERY_VERSION = "coffee-imagery-2026-09-v1"
IMAGERY_POLICY = {"resolution": 10, "paddingM": 320, "clearClasses": [4, 5, 6, 7, 11], "percentiles": [2, 98], "gamma": 1.15, "minClear": 0.6, "maxTiles": 256}


def percentile(sorted_values, p: float) -> float:
    n = len(sorted_values)
    if not n:
        return float("nan")
    k = (n - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def true_colour_stretch(channels, clear, n: int, percentiles=None, gamma: float | None = None):
    """Reflectance channels to 8-bit RGB: percentile stretch over cloud-free farm pixels, mild gamma."""
    percentiles = percentiles or IMAGERY_POLICY["percentiles"]
    gamma = IMAGERY_POLICY["gamma"] if gamma is None else gamma
    clear = np.asarray(clear).astype(bool)
    rgb = np.zeros(n * 3, dtype=np.uint8)
    ranges = []
    for b in range(3):
        ch = np.asarray(channels[b], dtype=np.float64)
        ok = np.isfinite(ch) & (ch > 0)
        vals = np.sort(ch[clear & ok])
        if vals.size < 10:
            vals = np.sort(ch[ok])
        lo = percentile(vals, percentiles[0]) if vals.size else 0.0
        hi = max(percentile(vals, percentiles[1]), lo + 1e-4) if vals.size else 1.0
        ranges.append([float(lo), float(hi)])
        t = np.where(np.isfinite(ch), np.clip((ch - lo) / (hi - lo), 0, 1), 0.0)
        # Math.round(x*255): round half up, as JavaScript does for positive values.
        rgb[b::3] = np.floor(np.power(t, 1 / gamma) * 255 + 0.5).astype(np.uint8)
    return rgb, ranges


def choose_clearest(scored: list[dict]) -> dict | None:
    """The least cloudy scene wins; ties go to the newest."""
    if not scored:
        return None

    def when(s):
        scene = s.get("scene") or {}
        return parse_ms(((scene.get("properties") or {}).get("datetime")) or s.get("acquiredAt")) or 0

    return sorted(scored, key=lambda s: (-s["clearFraction"], -when(s)))[0]


def lon_lat_to_tile(lon: float, lat: float, z: int) -> dict:
    n = 2 ** z
    x = math.floor((lon + 180) / 360 * n)
    lat_r = lat * math.pi / 180
    y = math.floor((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return {"x": min(n - 1, max(0, x)), "y": min(n - 1, max(0, y))}


def tile_bounds(z: int, x: int, y: int) -> dict:
    n = 2 ** z
    west = x / n * 360 - 180
    east = (x + 1) / n * 360 - 180
    north = math.atan(math.sinh(math.pi * (1 - 2 * y / n))) * 180 / math.pi
    south = math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))) * 180 / math.pi
    return {"west": west, "south": south, "east": east, "north": north}


def tile_range(bounds: dict, z: int) -> dict:
    a = lon_lat_to_tile(bounds["west"], bounds["north"], z)
    b = lon_lat_to_tile(bounds["east"], bounds["south"], z)
    x_min, x_max, y_min, y_max = min(a["x"], b["x"]), max(a["x"], b["x"]), min(a["y"], b["y"]), max(a["y"], b["y"])
    nw, se = tile_bounds(z, x_min, y_min), tile_bounds(z, x_max, y_max)
    return {"z": z, "xMin": x_min, "xMax": x_max, "yMin": y_min, "yMax": y_max, "cols": x_max - x_min + 1, "rows": y_max - y_min + 1,
            "bounds": {"west": nw["west"], "north": nw["north"], "east": se["east"], "south": se["south"]}}


def tile_intersects(z: int, x: int, y: int, bounds: dict, pad_deg: float = 0) -> bool:
    t = tile_bounds(z, x, y)
    return not (t["east"] < bounds["west"] - pad_deg or t["west"] > bounds["east"] + pad_deg or t["north"] < bounds["south"] - pad_deg or t["south"] > bounds["north"] + pad_deg)
