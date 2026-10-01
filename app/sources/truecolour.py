"""Sentinel-2 true colour: the least cloudy of the newest and clearest recent scenes, stretched
over cloud-free farm pixels. A photograph for orientation under the analysis layers."""
from __future__ import annotations

import base64

import httpx
import numpy as np

from ..errors import UpstreamError
from ..geo.imagery_model import IMAGERY_POLICY, choose_clearest, true_colour_stretch
from ..rasters import catalog, fetch_raster, grid_spec, position_arrays
from ..geo.farm_model import zone_ids_for
from ..util.numbers import fixed
from ..util.time import iso_now
from .base import map_limit
from .sentinel2 import COLLECTION, SOURCE, SOURCE_URL, baseline_offset, scene_candidates


async def load_true_colour(http: httpx.AsyncClient, geometry: dict) -> dict:
    spec = grid_spec(geometry, IMAGERY_POLICY["resolution"], IMAGERY_POLICY["paddingM"])
    scenes = await catalog(http, COLLECTION, geometry["bounds"], 150, 30)
    if not scenes:
        return {**spec, "status": "unavailable", "kind": "sentinel2", "reason": "No Sentinel-2 scenes were returned for this footprint.", "source": SOURCE, "retrievedAt": iso_now()}
    candidates = scene_candidates(scenes, newest=4, clearest=4, older=0)
    n = spec["width"] * spec["height"]

    async def raster_for(s):
        raster = await fetch_raster(http, COLLECTION, s["id"], spec, ["B04", "B03", "B02", "SCL"])
        if raster.shape[2] != spec["width"] or raster.shape[1] != spec["height"] or raster.shape[0] < 4:
            raise UpstreamError("Unexpected Sentinel bands")
        return {"scene": s, "raster": raster.reshape(raster.shape[0], n)}

    results = await map_limit(candidates, 4, raster_for)
    grids = [r.value for r in results if r.ok]
    if not grids:
        raise UpstreamError("Sentinel-2 rasters could not be read")
    _, _, lons, lats = position_arrays(spec)
    inside = np.array([z is not None for z in zone_ids_for(lons, lats, geometry["zones"])])
    inside_count = int(inside.sum())
    scored = []
    for g in grids:
        flat = g["raster"]
        scl = np.floor(flat[3] + 0.5)
        clear = np.isin(scl, IMAGERY_POLICY["clearClasses"])
        if flat.shape[0] > 4:
            clear &= flat[4] > 0
        scored.append({"scene": g["scene"], "raster": flat, "clear": clear, "clearFraction": float(clear[inside].sum() / inside_count) if inside_count else 0.0})
    best = choose_clearest(scored)
    offset = baseline_offset(best["scene"])
    channels = [(best["raster"][b] + offset) / 10000 for b in range(3)]
    rgb, _ = true_colour_stretch(channels, best["clear"], n)
    props = best["scene"].get("properties") or {}
    return {**spec, "status": "available" if best["clearFraction"] >= IMAGERY_POLICY["minClear"] else "cloud-limited", "kind": "sentinel2", "id": best["scene"]["id"], "acquiredAt": props.get("datetime"),
            "clearFraction": fixed(best["clearFraction"], 3),
            "scenesTried": [{"id": s["scene"]["id"], "acquiredAt": (s["scene"].get("properties") or {}).get("datetime"), "clearFraction": fixed(s["clearFraction"], 3)} for s in scored],
            "rgb": base64.b64encode(rgb.tobytes()).decode("ascii"), "channels": 3, "source": SOURCE, "sourceUrl": SOURCE_URL,
            "method": f"Sentinel-2 true colour (red, green, blue bands) at {IMAGERY_POLICY['resolution']} m, stretched between the {IMAGERY_POLICY['percentiles'][0]}th and {IMAGERY_POLICY['percentiles'][1]}th percentiles of cloud-free farm pixels with gamma {IMAGERY_POLICY['gamma']}; the least cloudy of the newest and clearest recent scenes. A photograph for orientation, not an analysis layer.",
            "retrievedAt": iso_now()}
