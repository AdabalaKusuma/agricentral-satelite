"""Offline tests for the only genuinely new logic in this service: sampling three grids
of different resolutions onto one set of rows.

No network, no database. The grids are built with the real `grid_spec` / `positions` so the
cell geometry is the same arithmetic the loaders use.
"""
from __future__ import annotations

import math

from app.rasters import grid_spec, positions
from app.worker.process import Lookup, merge

# A small square farm near Chikmagalur, one zone, about 15 hectares.
GEOMETRY = {
    "zones": [{
        "id": "zone-a",
        "name": "Block A",
        "rings": [[
            [76.0500, 11.9960],
            [76.0540, 11.9960],
            [76.0540, 12.0000],
            [76.0500, 12.0000],
            [76.0500, 11.9960],
        ]],
    }],
    "bounds": {"west": 76.0500, "east": 76.0540, "south": 11.9960, "north": 12.0000},
    "center": {"lon": 76.0520, "lat": 11.9980},
}


def _grid(resolution: float, value_key: str, value_fn, inside_only: bool = True) -> dict:
    """A dataset grid shaped exactly as the real loaders return one."""
    spec = grid_spec(GEOMETRY, resolution)
    cells = []
    for p in positions(spec, GEOMETRY):
        if inside_only and not p["zoneId"]:
            continue
        cells.append({**p, value_key: value_fn(p)})
    return {**spec, "id": f"scene-{resolution:g}m", "acquiredAt": "2026-09-28T05:21:14Z",
            "validFraction": 1.0, "cells": cells}


def vegetation_grid() -> dict:
    g = _grid(20, "ndvi", lambda p: 0.40 + 0.01 * p["col"])
    for c in g["cells"]:
        c["ndmi"] = 0.10
        c["ndviChange"] = -0.12
        c["clear"] = True
    return {"status": "available", "current": g,
            "baseline": {**g, "id": "scene-older", "acquiredAt": "2026-07-24T05:19:02Z"}}


def thermal_grid(clear: bool = True) -> dict:
    # 100 m cells: deliberately much coarser than the 20 m output grid. The real loader
    # covers the whole bounding box (inside_only=False), because a 20 m cell inside the
    # farm routinely sits in a 100 m pixel whose own centre is outside it.
    g = _grid(100, "surfaceC", lambda p: 25.0 + p["row"], inside_only=False)
    for c in g["cells"]:
        c["clear"] = clear
        if not clear:
            c["surfaceC"] = None
    return {"status": "available" if clear else "cloud-limited", "current": g}


def dem_grid() -> dict:
    g = _grid(30, "elevation", lambda p: 1000.0 + p["col"], inside_only=False)
    return {**g, "status": "available"}


# ---- Lookup -----------------------------------------------------------------------------

def test_lookup_finds_the_cell_containing_a_point():
    g = dem_grid()
    look = Lookup(g)
    for c in g["cells"][:25]:
        hit = look.at(c["lon"], c["lat"])
        assert hit is not None, "a cell centre must resolve to its own cell"
        assert hit["i"] == c["i"]


def test_lookup_rejects_points_outside_the_grid():
    look = Lookup(dem_grid())
    assert look.at(70.0, 11.998) is None
    assert look.at(76.052, 20.0) is None


def test_lookup_on_an_absent_dataset_is_safe():
    """A failed source must not raise; it must simply have no value anywhere."""
    assert Lookup(None).at(76.052, 11.998) is None
    assert Lookup({}).at(76.052, 11.998) is None
    assert Lookup({"cells": [], "width": 0, "height": 0}).at(76.052, 11.998) is None


# ---- merge ------------------------------------------------------------------------------

def test_merge_produces_one_row_per_sentinel_cell():
    veg = vegetation_grid()
    rows = merge("farm-1", veg, thermal_grid(), dem_grid())
    assert len(rows) == len(veg["current"]["cells"])
    assert {r["FarmId"] for r in rows} == {"farm-1"}
    assert all(r["ZoneId"] == "zone-a" for r in rows)


def test_merge_carries_every_value_and_date():
    rows = merge("farm-1", vegetation_grid(), thermal_grid(), dem_grid())
    r = rows[0]
    assert r["Ndvi"] is not None and r["Ndmi"] == 0.10
    assert r["NdviChange"] == -0.12
    assert r["SurfaceTempC"] is not None
    assert r["ElevationM"] is not None
    assert str(r["ObservedOn"]) == "2026-09-28"
    assert str(r["BaselineOn"]) == "2026-07-24"
    assert str(r["ThermalOn"]) == "2026-09-28"


def test_coarse_values_are_shared_not_interpolated():
    """One 100 m Landsat pixel covers ~25 of the 20 m output cells, and each of those cells
    carries that pixel's value unchanged. Nothing is smoothed to look finer than the
    instrument that measured it."""
    rows = merge("farm-1", vegetation_grid(), thermal_grid(), dem_grid())
    temps = {r["SurfaceTempC"] for r in rows}
    assert len(temps) < len(rows) / 5, "temperatures must repeat across neighbouring cells"
    assert len(temps) >= 2, "but still vary across the farm"


def test_a_missing_dataset_leaves_nulls_and_keeps_the_rest():
    """Cloud over Landsat must not cost us the leaf cover for the whole farm."""
    rows = merge("farm-1", vegetation_grid(), None, dem_grid())
    assert rows, "rows are still produced"
    assert all(r["SurfaceTempC"] is None for r in rows)
    assert all(r["Ndvi"] is not None for r in rows)
    assert all(r["ElevationM"] is not None for r in rows)
    assert all(r["ThermalOn"] is None for r in rows)


def test_a_fully_clouded_thermal_pass_leaves_no_date():
    """Landsat can return a grid in which every pixel is cloud-flagged - common at these
    latitudes around the monsoon. The values are NULL, and ThermalOn must be NULL too:
    stamping the overpass date on an absent measurement would claim an observation we
    never made."""
    rows = merge("farm-1", vegetation_grid(), thermal_grid(clear=False), dem_grid())
    assert rows
    assert all(r["SurfaceTempC"] is None for r in rows)
    assert all(r["ThermalOn"] is None for r in rows)
    # The other datasets are unaffected by Landsat's cloud.
    assert all(r["Ndvi"] is not None and r["ElevationM"] is not None for r in rows)


def test_no_sentinel_grid_means_no_rows():
    """Sentinel defines the cells, so without it there is nothing to hang values on."""
    assert merge("farm-1", None, thermal_grid(), dem_grid()) == []
    assert merge("farm-1", {"status": "unavailable", "current": None}, None, None) == []


def test_cells_fall_inside_the_boundary():
    rows = merge("farm-1", vegetation_grid(), thermal_grid(), dem_grid())
    b = GEOMETRY["bounds"]
    for r in rows:
        assert b["west"] <= r["Lon"] <= b["east"]
        assert b["south"] <= r["Lat"] <= b["north"]


def test_row_and_col_identify_a_cell_uniquely():
    """(farm_id, cell_row, cell_col) is the primary key, so it must not collide."""
    rows = merge("farm-1", vegetation_grid(), thermal_grid(), dem_grid())
    keys = {(r["CellRow"], r["CellCol"]) for r in rows}
    assert len(keys) == len(rows)
