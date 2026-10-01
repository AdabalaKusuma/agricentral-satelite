"""Sentinel-1 RTC radar: newest passes from one viewing geometry, speckle-averaged in linear
power, converted to dB, the newest compared with the median of the earlier passes."""
from __future__ import annotations

import httpx
import numpy as np

from ..errors import UpstreamError
from ..geo.radar_model import RADAR_POLICY, choose_orbit, radar_change, speckle_mean
from ..rasters import catalog_window, fetch_raster, grid_spec, positions
from ..util.time import DAY_MS, iso_now, iso_utc_date, now_ms, parse_ms
from .base import map_limit

COLLECTION = "sentinel-1-rtc"
SOURCE = "Copernicus Sentinel-1 RTC via Microsoft Planetary Computer"


def _to_db_array(v: np.ndarray) -> list:
    with np.errstate(divide="ignore", invalid="ignore"):
        db = np.where(np.isfinite(v) & (v > 0), 10 * np.log10(np.where(v > 0, v, 1)), np.nan)
    return [float(x) if np.isfinite(x) else None for x in db]


async def load_radar(http: httpx.AsyncClient, geometry: dict) -> dict:
    spec = grid_spec(geometry, RADAR_POLICY["gridResolution"], RADAR_POLICY["paddingM"])
    now = now_ms()
    since, until = iso_utc_date(now - RADAR_POLICY["windowDays"] * DAY_MS), iso_utc_date(now)
    items = await catalog_window(http, COLLECTION, spec["bounds"], since, until, 40)
    if not items:
        return {**spec, "status": "unavailable", "reason": f"No Sentinel-1 scenes between {since} and {until}.", "cells": [], "scenes": [], "source": SOURCE, "retrievedAt": iso_now()}
    meta = [{"id": it["id"], "acquiredAt": it["properties"].get("datetime"), "orbitState": it["properties"].get("sat:orbit_state"), "relativeOrbit": it["properties"].get("sat:relative_orbit")} for it in items]
    orbit = choose_orbit(meta)
    chosen = orbit["list"][: RADAR_POLICY["maxScenes"]]
    n = spec["width"] * spec["height"]

    async def scene_for(s):
        raster = await fetch_raster(http, COLLECTION, s["id"], spec, ["vv", "vh"])
        if raster.shape[2] != spec["width"] or raster.shape[1] != spec["height"] or raster.shape[0] < 2:
            raise UpstreamError("Unexpected radar bands")
        flat = raster.reshape(raster.shape[0], n)
        mask = flat[2] if raster.shape[0] > 2 else None

        def clean(b):
            v = flat[b].copy()
            bad = ~np.isfinite(v) | (v <= 0)
            if mask is not None:
                bad |= ~(mask > 0)
            v[bad] = np.nan
            return v

        vv = speckle_mean(clean(0), spec["width"], spec["height"])
        vh = speckle_mean(clean(1), spec["width"], spec["height"])
        return {**s, "vvDb": _to_db_array(vv), "vhDb": _to_db_array(vh), "valid": int(np.isfinite(vv).sum()) / n}

    results = await map_limit(chosen, 4, scene_for)
    scenes = sorted((r.value for r in results if r.ok), key=lambda s: -(parse_ms(s["acquiredAt"]) or 0))
    if not scenes:
        raise UpstreamError("Sentinel-1 rasters could not be read")
    change = radar_change(scenes, positions(spec, geometry))
    k = 2 * RADAR_POLICY["speckleCells"] + 1
    return {**spec, "status": "available" if len(scenes) > RADAR_POLICY["minReference"] else "reference-limited", **change,
            "scenes": [{"id": s["id"], "acquiredAt": s["acquiredAt"], "orbitState": s["orbitState"], "relativeOrbit": s["relativeOrbit"], "valid": s["valid"]} for s in scenes],
            "orbit": orbit["key"], "orbitsAvailable": list(dict.fromkeys(f"{it['properties'].get('sat:orbit_state')}-{it['properties'].get('sat:relative_orbit')}" for it in items)),
            "window": {"start": since, "end": until}, "source": SOURCE, "sourceUrl": "https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc",
            "method": f"Sentinel-1 radiometrically terrain-corrected gamma0 backscatter (VV, VH) on a {RADAR_POLICY['gridResolution']} m grid buffered {RADAR_POLICY['paddingM']} m, averaged over {k}×{k} cells in linear power before conversion to dB. Scenes from one relative orbit only; the newest is compared with the median of up to {RADAR_POLICY['maxScenes'] - 1} earlier passes. Radar responds to structure and water content, not greenness; changes under shade coffee need field confirmation.",
            "retrievedAt": iso_now()}
