"""White stem borer exposure composites: hot / dry season medians of Sentinel-2 NDVI and Landsat
surface temperature plus a context-buffered surface model. The combination itself runs in the
browser so the lifecycle phase follows the viewing date."""
from __future__ import annotations

import httpx

from ..errors import UpstreamError
from ..util.time import iso_date_ist, iso_now, now_ms
from .base import all_settled
from .dem import load_dem
from .landsat import load_thermal_composite
from .sentinel2 import load_vegetation_composite

CWSB_POLICY = {"window": {"start": "02-01", "end": "06-01"}, "contextBufferM": 250}


def environmental_window(now: int | None = None) -> dict:
    today = iso_date_ist(now_ms() if now is None else now)
    year = int(today[:4]) - (1 if int(today[5:7]) < 6 else 0)
    return {"year": year, "start": f"{year}-{CWSB_POLICY['window']['start']}", "end": f"{year}-{CWSB_POLICY['window']['end']}"}


async def load_cwsb(http: httpx.AsyncClient, geometry: dict, window: dict | None = None) -> dict:
    window = window or environmental_window()
    pad = CWSB_POLICY["contextBufferM"]
    ndvi, lst, dem = await all_settled([
        load_vegetation_composite(http, geometry, window["start"], window["end"], padding=pad),
        load_thermal_composite(http, geometry, window["start"], window["end"], padding=pad),
        load_dem(http, geometry, padding=pad),
    ])
    if not ndvi.ok and not dem.ok:
        raise UpstreamError("exposure composites unavailable: " + "; ".join(r for r in (ndvi.reason, dem.reason) if r))
    unavailable = {k: r.reason for k, r in (("ndvi", ndvi), ("lst", lst), ("dem", dem)) if not r.ok}
    return {"window": window, "ndvi": ndvi.value, "lst": lst.value, "dem": dem.value, "unavailable": unavailable, "partial": bool(unavailable),
            "source": "Sentinel-2 L2A, Landsat Collection 2 Level-2 and Copernicus GLO-30 via Microsoft Planetary Computer", "retrievedAt": iso_now()}
