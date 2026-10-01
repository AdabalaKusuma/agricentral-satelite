"""Process one farm: fetch the three datasets, merge them onto one grid, store the numbers.

The three sources arrive on different grids because that is their real resolution:

    Sentinel-2   20 m   leaf cover and canopy moisture
    Copernicus   30 m   elevation (a surface model - includes tree canopy)
    Landsat     100 m   land surface temperature

The 20 m Sentinel grid defines the output rows, and the coarser values are sampled onto it.
So roughly 25 output cells share one Landsat temperature. Nothing is smoothed or
interpolated to look finer than the instrument that measured it; a cell simply carries the
value of the pixel it falls inside.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from time import perf_counter

import httpx

from ..rasters import catalog, grid_spec
from ..sources import load_dem, load_thermal, load_vegetation
from ..sources.base import all_settled
from ..sources.sentinel2 import COLLECTION as S2_COLLECTION
from .datasets import refresh_datasets

log = logging.getLogger("satellite.process")


class Lookup:
    """Nearest-cell lookup on one dataset's grid, by longitude and latitude.

    The inverse of the grid's own cell placement, so a point resolves to the cell whose
    footprint contains it. Grids that only carry cells inside the farm leave gaps, hence
    the index by cell position rather than list order.
    """

    def __init__(self, grid):
        self.ok = bool(grid) and bool(grid.get("cells")) and grid.get("width") and grid.get("height")
        if not self.ok:
            return
        self.b = grid["bounds"]
        self.w = int(grid["width"])
        self.h = int(grid["height"])
        self.by_index = {int(c["i"]): c for c in grid["cells"]}

    def at(self, lon: float, lat: float):
        if not self.ok:
            return None
        span_x = self.b["east"] - self.b["west"]
        span_y = self.b["north"] - self.b["south"]
        if span_x <= 0 or span_y <= 0:
            return None
        col = int(math.floor((lon - self.b["west"]) / span_x * self.w))
        row = int(math.floor((self.b["north"] - lat) / span_y * self.h))
        if col < 0 or col >= self.w or row < 0 or row >= self.h:
            return None
        return self.by_index.get(row * self.w + col)


def _as_date(value):
    """An ISO instant from the STAC catalog to a plain date, or None."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).date()
    except (TypeError, ValueError):
        return None


async def newest_scene_id(http: httpx.AsyncClient, geometry: dict):
    """The id of the newest Sentinel-2 scene offered for this footprint, or None.

    A catalog search is small and fast; a raster download is neither. Comparing this with
    the scene already stored is what keeps a six-hourly sweep from re-downloading imagery
    that only changes every five days.
    """
    spec = grid_spec(geometry, 20)
    scenes = await catalog(http, S2_COLLECTION, spec["bounds"], 150, 1)
    return scenes[0]["id"] if scenes else None


def merge(farm_id: str, vegetation, thermal, elevation) -> list[dict]:
    """One row per Sentinel cell inside the farm, carrying whichever values are available.

    A missing dataset leaves its columns NULL rather than blocking the others: a farm under
    cloud still gets its elevation, and a farm with no Landsat pass still gets its leaf
    cover. NULL means "not measured", which is not the same as zero.
    """
    current = (vegetation or {}).get("current") or {}
    cells = current.get("cells") or []
    if not cells:
        return []

    baseline = (vegetation or {}).get("baseline") or {}
    thermal_current = (thermal or {}).get("current") or {}

    temp_at = Lookup(thermal_current)
    elev_at = Lookup(elevation)

    observed_on = _as_date(current.get("acquiredAt"))
    baseline_on = _as_date(baseline.get("acquiredAt"))
    # Only date the thermal column when a usable temperature actually came back. Every
    # Landsat pass can be cloud-flagged over a farm - common at these latitudes around the
    # monsoon - and stamping an overpass date on an absent measurement would imply we
    # observed something we did not.
    thermal_on = (_as_date(thermal_current.get("acquiredAt"))
                  if any(c.get("clear") for c in (thermal_current.get("cells") or []))
                  else None)

    # Keys are the EF column names: these dicts go straight into an INSERT.
    rows = []
    for c in cells:
        lon, lat = float(c["lon"]), float(c["lat"])
        t = temp_at.at(lon, lat)
        e = elev_at.at(lon, lat)
        rows.append({
            "FarmId": farm_id,
            "CellRow": int(c["row"]),
            "CellCol": int(c["col"]),
            "ZoneId": c.get("zoneId"),
            "Lat": lat,
            "Lon": lon,
            "Ndvi": c.get("ndvi"),
            "Ndmi": c.get("ndmi"),
            "NdviChange": c.get("ndviChange"),
            "SurfaceTempC": (t or {}).get("surfaceC"),
            "ElevationM": (e or {}).get("elevation"),
            "ObservedOn": observed_on,
            "BaselineOn": baseline_on,
            "ThermalOn": thermal_on,
        })
    return rows


async def process_farm(db, http: httpx.AsyncClient, farm_id: str, *,
                       skip_unchanged: bool = True, force: bool = False,
                       max_cells: int = 2_000_000, with_datasets: bool = True) -> dict:
    """Bring one farm up to date. Returns a small summary for the log.

    Every outcome is recorded in farm_status, including the uninteresting ones, so the .NET
    API can always say something truthful about a farm: ready, preparing, or why not.
    """
    started = perf_counter()
    geometry = db.load_geometry(farm_id)
    if geometry is None:
        db.set_status(farm_id, "preparing", message="No mapped boundary for this farm yet.")
        return {"farm": farm_id, "result": "no_geometry"}

    geo = geometry.as_dict()

    # Refuse an implausible footprint before downloading anything.
    #
    # Cost scales with the BOUNDING BOX, not the mapped area, so a farm whose zones are
    # scattered - test data, a mis-keyed coordinate, one stray point in another state -
    # can span hundreds of kilometres while its polygons cover a few hundred hectares.
    # Such a farm would request gigabyte rasters per scene and exhaust memory, and in a
    # sweep it would take every other farm down with it. Record it and move on: a boundary
    # this size is a data-entry problem for someone to look at, not something to compute.
    spec = grid_spec(geo, 20)
    cells = spec["width"] * spec["height"]
    if max_cells and cells > max_cells:
        b = geo["bounds"]
        km_x = (b["east"] - b["west"]) * 111.32 * math.cos(math.radians(geo["center"]["lat"]))
        km_y = (b["north"] - b["south"]) * 111.32
        message = (f"Boundary spans {km_x:.0f} x {km_y:.0f} km ({cells:,} analysis cells, "
                   f"limit {max_cells:,}). Check the farm's mapped zones for a stray point.")
        log.warning("%s: %s", farm_id, message)
        db.set_status(farm_id, "failed", message=message,
                      duration_ms=int((perf_counter() - started) * 1000))
        return {"farm": farm_id, "result": "too_large", "cells": cells, "reason": message}

    if skip_unchanged and not force:
        try:
            newest = await newest_scene_id(http, geo)
        except Exception as exc:  # noqa: BLE001 - a catalog hiccup must not fail the farm
            newest = None
            log.warning("catalog check failed for %s: %s", farm_id, exc)
        if newest and newest == db.last_scene_id(farm_id):
            db.touch_run(farm_id, message="No new imagery since the last run.")
            return {"farm": farm_id, "result": "unchanged", "scene": newest}

    veg, therm, elev = await all_settled([
        # Sentinel-2 defines the output cells, so it keeps only cells inside a zone.
        load_vegetation(http, geo),
        # The coarser grids are lookup targets and must cover the whole bounding box: a
        # 20 m cell inside the farm can sit within a 100 m pixel whose own centre is
        # outside it, which on a small farm is true of nearly every pixel.
        load_thermal(http, geo, inside_only=False),
        load_dem(http, geo),
    ])

    for name, settled in (("vegetation", veg), ("thermal", therm), ("elevation", elev)):
        if not settled.ok:
            log.warning("%s unavailable for %s: %s", name, farm_id, settled.reason)

    if not veg.ok:
        # Without the Sentinel grid there are no cells to hang the other values on.
        db.set_status(farm_id, "failed", message=f"Sentinel-2 unavailable: {veg.reason}",
                      duration_ms=int((perf_counter() - started) * 1000))
        return {"farm": farm_id, "result": "failed", "reason": veg.reason}

    rows = merge(farm_id, veg.value, therm.value if therm.ok else None,
                 elev.value if elev.ok else None)
    if not rows:
        db.set_status(farm_id, "preparing",
                      message="No clear Sentinel-2 pixels inside the boundary yet.",
                      duration_ms=int((perf_counter() - started) * 1000))
        return {"farm": farm_id, "result": "no_cells"}

    written = db.replace_cells(farm_id, rows)

    # The whole datasets the WSB page reads. Done after the cells so a farm has usable
    # numbers for the rule engine even if a raster download later fails, and because the
    # Sentinel fetch above already warmed the provider's cache for this footprint.
    datasets = await refresh_datasets(db, http, farm_id, geo, force=force) if with_datasets else {}

    current = (veg.value or {}).get("current") or {}

    # A source can succeed and still yield nothing usable - Landsat most often, when every
    # recent pass is cloud-flagged over the farm. That is a different situation from the
    # source failing, and the status message should say which.
    missing = [n for n, s in (("thermal", therm), ("elevation", elev)) if not s.ok]
    if therm.ok and not any(r["SurfaceTempC"] is not None for r in rows):
        missing.append("thermal (no cloud-free pass)")
    db.set_status(
        farm_id, "ready",
        scene_id=current.get("id"),
        observed_on=_as_date(current.get("acquiredAt")),
        cell_count=written,
        duration_ms=int((perf_counter() - started) * 1000),
        message=("Partial: " + ", ".join(missing) + " unavailable.") if missing else None,
    )
    return {"farm": farm_id, "result": "ready", "cells": written,
            "scene": current.get("id"), "missing": missing, "datasets": datasets}
