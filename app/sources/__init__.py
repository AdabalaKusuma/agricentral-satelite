"""The datasets this service produces, and how long each stays fresh.

Two groups:

  CELL_SOURCES    extracted into SatelliteCellValues, one row per 20 m cell. These feed the
                  .NET rule engine, which wants numbers it can query in SQL.

  DATASETS        stored whole in SatelliteDatasets as gzipped JSON. The WSB page computes
                  its cards, layers and exposure maps in the browser, so it needs each
                  dataset in full - the per-cell summary is not enough.

The same Sentinel-2 fetch feeds both: `satellite` is stored whole AND extracted into cells.

TTLs follow how fast the real thing changes, not how often someone might ask. Sentinel-2
revisits in about five days and Landsat in sixteen, so six hours is frequent enough to
catch a new scene the morning it lands. Elevation never changes, hence thirty days. The
seasonal composites cover a fixed Feb-Jun window, so once that window is complete they
change only when a late scene arrives.
"""
from __future__ import annotations

from .base import DAY_MS, HOUR_MS, Source, all_settled, map_limit
from .cwsb import environmental_window, load_cwsb
from .dem import load_dem, load_surface
from .landsat import load_thermal
from .radar import load_radar
from .sentinel2 import load_vegetation
from .truecolour import load_true_colour
from .weather import load_season_history, load_solar_history

# Fetched for the per-cell table. `thermal` and `elevation` are lookup targets for the 20 m
# Sentinel grid, so process.py loads them over the whole bounding box rather than only cells
# whose centre falls inside a zone.
CELL_SOURCES = ("vegetation", "thermal", "elevation")

# Stored whole, under the name the WSB page asks for: GET /api/satellite, /api/drainage, ...
DATASETS: dict[str, Source] = {
    "satellite": Source("satellite", 6 * HOUR_MS, "v2", load_vegetation,
                        "Sentinel-2 NDVI / NDMI with a comparison baseline, 20 m"),
    # 900 m of surrounding land, so the 3D view sits on real relief and water can be traced
    # across the boundary.
    "drainage": Source("drainage", 30 * DAY_MS, "v3", load_surface,
                       "Copernicus GLO-30 elevation, 30 m, buffered 900 m"),
    "thermal": Source("thermal", 6 * HOUR_MS, None, load_thermal,
                      "Landsat surface temperature, 100 m"),
    "radar": Source("radar", 6 * HOUR_MS, None, load_radar,
                    "Sentinel-1 backscatter and change - sees through cloud"),
    "imagery": Source("imagery", 6 * HOUR_MS, None, load_true_colour,
                      "Sentinel-2 true colour, the photo base under the data layers"),
    "cwsb": Source("cwsb", 7 * DAY_MS, None, load_cwsb,
                   "Hot/dry season composites: NDVI and surface-temperature medians plus terrain"),
    "solar-history": Source("solar-history", 3 * HOUR_MS, None, load_solar_history,
                            "Open-Meteo 31-day radiation history for the farm centre"),
    "season-history": Source("season-history", 6 * HOUR_MS, None, load_season_history,
                             "Open-Meteo season temperature and rainfall history"),
}

# These two take the farm's centre point rather than its geometry.
CENTRE_DATASETS = ("solar-history", "season-history")

__all__ = ["CELL_SOURCES", "DATASETS", "CENTRE_DATASETS", "Source", "all_settled",
           "map_limit", "HOUR_MS", "DAY_MS", "environmental_window",
           "load_vegetation", "load_thermal", "load_dem"]
