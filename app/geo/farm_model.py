"""Zone geometry helpers: the point-in-polygon test the Worker imports from farm-model.js,
plus a vectorised version for whole grids."""
from __future__ import annotations

import numpy as np


def point_in_ring(point, ring) -> bool:
    x, y = point
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        ax, ay = ring[i]
        bx, by = ring[j]
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
        j = i
    return inside


def point_in_zone(point, zone) -> bool:
    rings = zone["rings"]
    return point_in_ring(point, rings[0]) and not any(point_in_ring(point, r) for r in rings[1:])


def points_in_ring(xs: np.ndarray, ys: np.ndarray, ring) -> np.ndarray:
    """Ray casting for many points at once; same rule as `point_in_ring`."""
    ring = np.asarray(ring, dtype=np.float64)
    inside = np.zeros(xs.shape, dtype=bool)
    n = len(ring)
    j = n - 1
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[j]
        cond = (ay > ys) != (by > ys)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (bx - ax) * (ys - ay) / (by - ay) + ax
        inside ^= cond & (xs < xint)
        j = i
    return inside


def zone_ids_for(xs: np.ndarray, ys: np.ndarray, zones: list[dict]) -> list[str | None]:
    """The first zone (in list order) containing each point, or None, as `positions()` does."""
    out: list[str | None] = [None] * len(xs)
    assigned = np.zeros(len(xs), dtype=bool)
    for z in zones:
        rings = z["rings"]
        inside = points_in_ring(xs, ys, rings[0])
        for hole in rings[1:]:
            inside &= ~points_in_ring(xs, ys, hole)
        take = inside & ~assigned
        for i in np.flatnonzero(take):
            out[int(i)] = z["id"]
        assigned |= take
    return out
