"""Sentinel-2 L2A: the current vegetation grid with its comparison baseline, and the
fixed-window composites used by the white stem borer exposure model."""
from __future__ import annotations

import math

import httpx
import numpy as np

from ..errors import UpstreamError
from ..rasters import catalog, catalog_window, fetch_raster, grid_spec, positions
from ..util.numbers import fixed, is_finite, median
from ..util.time import DAY_MS, iso_now, parse_ms
from .base import all_settled, map_limit

COLLECTION = "sentinel-2-l2a"
BANDS = ["B04", "B08", "B11", "SCL"]
SOURCE = "Copernicus Sentinel-2 L2A via Microsoft Planetary Computer"
SOURCE_URL = "https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a"


def _cloud(scene: dict) -> float:
    v = (scene.get("properties") or {}).get("eo:cloud_cover")
    return v if is_finite(v) else 100


def _when(scene: dict) -> float:
    return parse_ms((scene.get("properties") or {}).get("datetime")) or 0


def baseline_offset(scene: dict) -> int:
    try:
        return -1000 if float((scene.get("properties") or {}).get("s2:processing_baseline")) >= 4 else 0
    except (TypeError, ValueError):
        return 0


def vegetation_grid(raster: np.ndarray, scene: dict, spec: dict, geometry: dict, inside_only: bool = True) -> dict:
    if raster.shape[0] != 5 or raster.shape[2] != spec["width"] or raster.shape[1] != spec["height"]:
        raise UpstreamError("Unexpected Sentinel bands")
    n = spec["width"] * spec["height"]
    offset = baseline_offset(scene)
    flat = raster.reshape(5, n)
    scl = np.floor(flat[3] + 0.5)
    mask = flat[4]
    red, nir, swir = (flat[0] + offset) / 10000, (flat[1] + offset) / 10000, (flat[2] + offset) / 10000
    clear = (mask > 0) & ((scl == 4) | (scl == 5)) & (red >= 0) & (nir > 0) & (swir >= 0)
    cells = []
    for p in positions(spec, geometry):
        if inside_only and not p["zoneId"]:
            continue
        i = p["i"]
        c = bool(clear[i])
        ndvi = fixed((nir[i] - red[i]) / (nir[i] + red[i]), 4) if c and nir[i] + red[i] > 0 else None
        ndmi = fixed((nir[i] - swir[i]) / (nir[i] + swir[i]), 4) if c and nir[i] + swir[i] > 0 else None
        cells.append({**p, "scl": int(scl[i]) if math.isfinite(scl[i]) else None, "clear": c, "ndvi": ndvi, "ndmi": ndmi})
    props = scene.get("properties") or {}
    return {**spec, "id": scene["id"], "acquiredAt": props.get("datetime"), "sceneCloudPercent": props.get("eo:cloud_cover"),
            "validFraction": sum(1 for c in cells if c["clear"]) / max(1, len(cells)), "offset": offset, "scale": 0.0001, "cells": cells}


def scene_candidates(scenes: list[dict], newest: int = 4, clearest: int = 3, older: int = 3, gap_days: int = 10) -> list[dict]:
    """Which catalog scenes to download: the newest few, the clearest few, and the clearest scenes
    old enough to serve as a comparison baseline. Scene cloud cover is a hint; farm pixels decide."""
    if not scenes:
        return []
    latest = _when(scenes[0])
    aged = sorted((s for s in scenes if latest - _when(s) >= gap_days * DAY_MS), key=_cloud)[:older]
    merged: dict[str, dict] = {}
    for s in [*scenes[:newest], *sorted(scenes, key=_cloud)[:clearest], *aged]:
        merged.setdefault(s["id"], s)
    return list(merged.values())


def choose_baseline(grids: list[dict], current: dict | None, preferred_gap_days: int = 10, min_gap_days: int = 1, min_clear: float = 0.6) -> dict | None:
    """Prefer the newest clear scene at least ten days older; otherwise the oldest clear earlier
    scene, reported as a short interval."""
    if not current:
        return None
    t = parse_ms(current["acquiredAt"]) or 0
    gap = lambda g: (t - (parse_ms(g["acquiredAt"]) or 0)) / DAY_MS
    clear = [g for g in grids if g["id"] != current["id"] and g["validFraction"] >= min_clear and gap(g) >= min_gap_days]
    standard = sorted((g for g in clear if gap(g) >= preferred_gap_days), key=lambda g: -(parse_ms(g["acquiredAt"]) or 0))
    if standard:
        return {"baseline": standard[0], "gapDays": _js_round(gap(standard[0])), "interval": "standard"}
    short = sorted(clear, key=lambda g: parse_ms(g["acquiredAt"]) or 0)
    return {"baseline": short[0], "gapDays": max(1, _js_round(gap(short[0]))), "interval": "short"} if short else None


def _js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


async def load_vegetation(http: httpx.AsyncClient, geometry: dict) -> dict:
    scenes = await catalog(http, COLLECTION, geometry["bounds"], 150, 30)
    spec = grid_spec(geometry, 20)
    if not scenes:
        return {"status": "unavailable", "reason": "No Sentinel-2 scenes were returned for this footprint.", "scenes": [], "current": None, "baseline": None}
    candidates = scene_candidates(scenes)

    async def grid_for(s):
        return vegetation_grid(await fetch_raster(http, COLLECTION, s["id"], spec, BANDS), s, spec, geometry)

    results = await all_settled(grid_for(s) for s in candidates)
    grids = sorted((r.value for r in results if r.ok), key=lambda g: -(parse_ms(g["acquiredAt"]) or 0))
    if not grids:
        raise UpstreamError("Satellite scenes were found but their raster data could not be read")
    current = next((g for g in grids if g["validFraction"] >= 0.6), grids[0])
    t_current = parse_ms(current["acquiredAt"]) or 0
    usable = lambda g: g["validFraction"] >= 0.6 and t_current - (parse_ms(g["acquiredAt"]) or 0) >= 10 * DAY_MS
    baseline = next((g for g in grids if usable(g)), None)
    if baseline is None:
        # Scene cloud cover describes a whole tile, not this footprint: sample the clearest scenes
        # old enough to compare, a few at a time, until one has enough clear farm pixels.
        tried = {g["id"] for g in grids}
        pool = sorted((s for s in scenes if s["id"] not in tried and t_current - _when(s) >= 10 * DAY_MS), key=_cloud)[:6]
        for s in pool:
            try:
                g = vegetation_grid(await fetch_raster(http, COLLECTION, s["id"], spec, BANDS), s, spec, geometry)
            except Exception:
                continue
            grids.append(g)
            if usable(g):
                baseline = g
                break
        grids.sort(key=lambda g: -(parse_ms(g["acquiredAt"]) or 0))
    choice = choose_baseline(grids, current)
    baseline = choice["baseline"] if choice else None
    baseline_cells = {c["i"]: c for c in baseline["cells"]} if baseline else {}
    current["cells"] = [{**c, "ndviChange": fixed(c["ndvi"] - baseline_cells[c["i"]]["ndvi"], 4) if c["clear"] and baseline_cells.get(c["i"], {}).get("clear") else None} for c in current["cells"]]
    cover = current["validFraction"]
    return {"status": "available" if cover >= 0.6 else "cloud-limited", "current": current,
            "baseline": {"id": baseline["id"], "acquiredAt": baseline["acquiredAt"], "validFraction": baseline["validFraction"], "gapDays": choice["gapDays"], "interval": choice["interval"]} if choice else None,
            "scenes": [{"id": g["id"], "acquiredAt": g["acquiredAt"], "validFraction": g["validFraction"], "sceneCloudPercent": g["sceneCloudPercent"]} for g in grids],
            "source": SOURCE, "sourceUrl": SOURCE_URL,
            "method": "20 m common analysis grid; nearest-neighbour sampling of 10 m red/NIR and 20 m SWIR/classification. SCL 4/5 only; clouds, shadows, water and uncertain pixels excluded. Processing-baseline offsets applied. Mixed coffee/shade-tree canopy; no pest identification.",
            "retrievedAt": iso_now()}


async def load_vegetation_composite(http: httpx.AsyncClient, geometry: dict, start: str, end: str, padding: float = 250, resolution: float = 20, max_scenes: int = 24) -> dict:
    """Per-cell median of clear observations over a fixed window, on a grid buffered beyond the
    boundary so the surrounding context is available for normalisation."""
    spec = grid_spec(geometry, resolution, padding)
    scenes = await catalog_window(http, COLLECTION, spec["bounds"], start, end)
    if not scenes:
        raise UpstreamError(f"No Sentinel-2 scenes between {start} and {end}")
    chosen = sorted(scenes, key=_cloud)[:max_scenes]

    async def grid_for(s):
        return vegetation_grid(await fetch_raster(http, COLLECTION, s["id"], spec, BANDS), s, spec, geometry, False)

    results = await map_limit(chosen, 6, grid_for)
    grids = [r.value for r in results if r.ok]
    if not grids:
        raise UpstreamError("Sentinel-2 composite rasters could not be read")
    cells = []
    for p in positions(spec, geometry):
        obs = [g["cells"][p["i"]] for g in grids if g["cells"][p["i"]]["clear"]]
        cells.append({**p, "ndvi": median(c["ndvi"] for c in obs), "ndmi": median(c["ndmi"] for c in obs), "n": len(obs)})
    inside = [c for c in cells if c["zoneId"]]
    return {"grid": {**spec, "cells": cells},
            "scenes": sorted(({"id": g["id"], "acquiredAt": g["acquiredAt"], "validFraction": g["validFraction"]} for g in grids), key=lambda s: -(parse_ms(s["acquiredAt"]) or 0)),
            "window": {"start": start, "end": end}, "coverage": sum(1 for c in inside if c["n"] > 0) / max(1, len(inside)), "source": SOURCE,
            "method": f"Median NDVI of clear (SCL 4/5) observations per {resolution:g} m cell over {start} to {end}, on a grid buffered {padding:g} m beyond the boundary; up to {max_scenes} least-cloudy scenes sampled."}
