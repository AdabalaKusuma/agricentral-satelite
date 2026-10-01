"""Landsat Collection 2 Level-2 surface temperature: the current 100 m grid and the
fixed-window composite for the exposure model."""
from __future__ import annotations

import httpx
import numpy as np

from ..errors import UpstreamError
from ..rasters import catalog, catalog_window, fetch_raster, grid_spec, positions
from ..util.numbers import fixed, is_finite, median
from ..util.time import iso_now, parse_ms
from .base import all_settled, map_limit
from .sentinel2 import _cloud

COLLECTION = "landsat-c2-l2"
BANDS = ["lwir11", "qa_pixel"]
SOURCE = "USGS Landsat Collection 2 L2 / Planetary Computer"
SOURCE_URL = "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2"


def _usable(scene: dict) -> bool:
    assets = scene.get("assets") or {}
    return assets.get("lwir11") is not None and assets.get("qa_pixel") is not None


def thermal_grid(raster: np.ndarray, scene: dict, spec: dict, geometry: dict, inside_only: bool = True) -> dict:
    if raster.shape[0] != 3 or raster.shape[2] != spec["width"] or raster.shape[1] != spec["height"]:
        raise UpstreamError("Unexpected thermal bands")
    n = spec["width"] * spec["height"]
    bands = ((scene.get("assets") or {}).get("lwir11") or {}).get("raster:bands") or [{}]
    band = bands[0] or {}
    scale, offset = band.get("scale"), band.get("offset")
    if not is_finite(scale) or not is_finite(offset):
        raise UpstreamError("Thermal calibration unavailable")
    flat = raster.reshape(3, n)
    dn = flat[0]
    qa = np.floor(flat[1] + 0.5).astype(np.int64)
    mask = flat[2]
    clear = (mask > 0) & (dn > 0) & ((qa & 63) == 0) & ((qa & 128) == 0) & (((qa >> 8) & 3) < 2)
    cells = []
    for p in positions(spec, geometry):
        if inside_only and not p["zoneId"]:
            continue
        i = p["i"]
        c = bool(clear[i])
        cells.append({**p, "clear": c, "surfaceC": fixed(dn[i] * scale + offset - 273.15, 2) if c else None})
    return {**spec, "id": scene["id"], "acquiredAt": (scene.get("properties") or {}).get("datetime"), "validFraction": sum(1 for c in cells if c["clear"]) / max(1, len(cells)), "cells": cells}


async def load_thermal(http: httpx.AsyncClient, geometry: dict, inside_only: bool = True) -> dict:
    """`inside_only=False` keeps every cell in the bounding box, not only those whose centre
    falls inside a zone. Required when this grid is a lookup target for a finer one: at
    100 m a small farm has few cell centres inside its boundary, yet every 20 m cell in it
    still sits within one of these pixels. Keeping only centre-inside cells would drop the
    temperature for the entire farm."""
    scenes = [s for s in await catalog(http, COLLECTION, geometry["bounds"], 100, 10) if _usable(s)][:4]
    spec = grid_spec(geometry, 100)

    async def grid_for(s):
        return thermal_grid(await fetch_raster(http, COLLECTION, s["id"], spec, BANDS), s, spec, geometry, inside_only)

    results = await all_settled(grid_for(s) for s in scenes)
    grids = sorted((r.value for r in results if r.ok), key=lambda g: -(parse_ms(g["acquiredAt"]) or 0))
    if not grids:
        raise UpstreamError("No readable Landsat surface-temperature scene")
    current = next((g for g in grids if g["validFraction"] >= 0.6), grids[0])
    return {"status": "available" if current["validFraction"] >= 0.6 else "cloud-limited", "current": current, "source": SOURCE, "sourceUrl": SOURCE_URL,
            "method": "100 m analysis grid; surface-temperature product sampled from its 30 m distribution grid. QA masks fill, cloud, cirrus, shadow, snow and water. Mixed land surface, not coffee leaf or air temperature.",
            "retrievedAt": iso_now()}


async def load_thermal_composite(http: httpx.AsyncClient, geometry: dict, start: str, end: str, padding: float = 250, resolution: float = 100, max_scenes: int = 16) -> dict:
    spec = grid_spec(geometry, resolution, padding)
    scenes = [s for s in await catalog_window(http, COLLECTION, spec["bounds"], start, end) if _usable(s)]
    if not scenes:
        raise UpstreamError(f"No Landsat surface-temperature scenes between {start} and {end}")
    chosen = sorted(scenes, key=_cloud)[:max_scenes]

    async def grid_for(s):
        return thermal_grid(await fetch_raster(http, COLLECTION, s["id"], spec, BANDS), s, spec, geometry, False)

    results = await map_limit(chosen, 6, grid_for)
    grids = [r.value for r in results if r.ok]
    if not grids:
        raise UpstreamError("Landsat composite rasters could not be read")
    cells = []
    for p in positions(spec, geometry):
        obs = [g["cells"][p["i"]] for g in grids if g["cells"][p["i"]]["clear"]]
        cells.append({**p, "surfaceC": median(c["surfaceC"] for c in obs), "n": len(obs)})
    inside = [c for c in cells if c["zoneId"]]
    return {"grid": {**spec, "cells": cells},
            "scenes": sorted(({"id": g["id"], "acquiredAt": g["acquiredAt"], "validFraction": g["validFraction"]} for g in grids), key=lambda s: -(parse_ms(s["acquiredAt"]) or 0)),
            "window": {"start": start, "end": end}, "coverage": sum(1 for c in inside if c["n"] > 0) / max(1, len(inside)),
            "source": "USGS Landsat Collection 2 Level-2 via Microsoft Planetary Computer",
            "method": f"Median surface temperature of QA-clear observations per {resolution:g} m cell over {start} to {end}, buffered {padding:g} m; up to {max_scenes} least-cloudy scenes sampled. Land-surface temperature, not air or leaf temperature."}
