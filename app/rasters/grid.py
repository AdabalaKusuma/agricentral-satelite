"""The common analysis grid: bounds padded in metres, cell counts, and cell centres with their zone.

The arithmetic follows the Worker's gridSpec and positions operation for operation, so the
Python service places every cell exactly where the JavaScript one did.
"""
from __future__ import annotations

import math

import numpy as np

from ..geo.farm_model import zone_ids_for


def grid_spec(geometry: dict, resolution: float = 20, padding: float = 0) -> dict:
    b, lat = geometry["bounds"], geometry["center"]["lat"]
    cos = math.cos(lat * math.pi / 180)
    dy = padding / 111320
    dx = padding / (111320 * cos)
    bounds = {"west": b["west"] - dx, "east": b["east"] + dx, "south": b["south"] - dy, "north": b["north"] + dy}
    width = max(2, math.ceil((bounds["east"] - bounds["west"]) * 111320 * cos / resolution))
    height = max(2, math.ceil((bounds["north"] - bounds["south"]) * 111320 / resolution))
    return {"bounds": bounds, "width": width, "height": height, "resolution": resolution}


def position_arrays(spec: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """cols, rows, lons, lats for every cell index i = row*width + col."""
    w, h, b = spec["width"], spec["height"], spec["bounds"]
    i = np.arange(w * h)
    cols = i % w
    rows = i // w
    lons = b["west"] + (cols + 0.5) / w * (b["east"] - b["west"])
    lats = b["north"] - (rows + 0.5) / h * (b["north"] - b["south"])
    return cols, rows, lons, lats


def positions(spec: dict, geometry: dict) -> list[dict]:
    cols, rows, lons, lats = position_arrays(spec)
    zones = zone_ids_for(lons, lats, geometry["zones"])
    return [{"i": int(i), "col": int(cols[i]), "row": int(rows[i]), "lon": float(lons[i]), "lat": float(lats[i]), "zoneId": zones[i]} for i in range(len(lons))]
