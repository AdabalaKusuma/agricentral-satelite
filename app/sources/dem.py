"""Copernicus DEM GLO-30 on a 30 m grid buffered around the farm, the base of the drainage
screen, the hillshade and the sun model. Another elevation source (for example state DEM
GeoTIFFs) plugs in by returning this same shape."""
from __future__ import annotations

import httpx
import numpy as np

from ..errors import UpstreamError
from ..rasters import catalog, fetch_raster, grid_spec, positions
from ..util.numbers import fixed
from ..util.time import iso_now
from .base import all_settled

COLLECTION = "cop-dem-glo-30"


SURFACE_PADDING_M = 900


async def load_surface(http: httpx.AsyncClient, geometry: dict) -> dict:
    """The estate surface for the 3D views and drainage screen, buffered well beyond the boundary."""
    return await load_dem(http, geometry, padding=SURFACE_PADDING_M)


async def load_dem(http: httpx.AsyncClient, geometry: dict, padding: float = 320) -> dict:
    spec = grid_spec(geometry, 30, padding)
    scenes = await catalog(http, COLLECTION, spec["bounds"], 0, 4)
    if not scenes:
        raise UpstreamError("No elevation tile available")

    async def tile_for(s):
        return {"id": s["id"], "r": await fetch_raster(http, COLLECTION, s["id"], spec, ["data"])}

    results = await all_settled(tile_for(s) for s in scenes if (s.get("assets") or {}).get("data") is not None)
    valid = [r.value for r in results if r.ok and r.value["r"].shape[0] == 2]
    if not valid:
        raise UpstreamError("Elevation raster unavailable")
    n = spec["width"] * spec["height"]
    flats = [(t["r"].reshape(2, n)) for t in valid]
    cells = []
    for c in positions(spec, geometry):
        i = c["i"]
        elevation = None
        for f in flats:
            if f[1, i] > 0 and np.isfinite(f[0, i]):
                elevation = fixed(float(f[0, i]), 2)
                break
        cells.append({**c, "elevation": elevation})
    return {**spec, "status": "available", "ids": [t["id"] for t in valid], "catalogDate": (scenes[0].get("properties") or {}).get("datetime"), "cells": cells,
            "source": "Copernicus DEM GLO-30 / Planetary Computer", "sourceUrl": "https://planetarycomputer.microsoft.com/dataset/cop-dem-glo-30",
            "method": "Approximately 30 m surface model, including tree canopy. Drainage screening needs a ground survey; the 90 m buffer does not contain the full upstream catchment. Catalog date is not a recent terrain survey.",
            "retrievedAt": iso_now()}
