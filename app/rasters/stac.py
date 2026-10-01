"""Planetary Computer STAC search and raster cutout URLs, as the Worker builds them."""
from __future__ import annotations

from urllib.parse import urlencode

import httpx
import numpy as np

from ..http import request_bytes, request_json
from ..util.time import iso_now, now_ms, parse_ms
from .npy import decode_npy

PC = "https://planetarycomputer.microsoft.com"


def raster_url(collection: str, item: str, spec: dict, assets: list[str]) -> str:
    b = spec["bounds"]
    q = [("collection", collection), ("item", item), ("dst_crs", "EPSG:4326"), ("resampling", "nearest"), ("reproject", "nearest"), ("unscale", "false"), ("asset_as_band", "true")]
    q += [("assets", a) for a in assets]
    return f"{PC}/api/data/v1/item/bbox/{b['west']},{b['south']},{b['east']},{b['north']}/{spec['width']}x{spec['height']}.npy?" + urlencode(q)


def _sorted_newest(features: list[dict]) -> list[dict]:
    return sorted(features or [], key=lambda f: -(parse_ms((f.get("properties") or {}).get("datetime")) or 0))


async def catalog(http: httpx.AsyncClient, collection: str, bounds: dict, days: int = 100, limit: int = 14) -> list[dict]:
    q = {"collections": collection, "bbox": ",".join(str(bounds[k]) for k in ("west", "south", "east", "north")), "limit": str(limit)}
    if days:
        now = now_ms()
        q["datetime"] = f"{iso_now(now - days * 86_400_000)}/{iso_now(now)}"
    d = await request_json(http, f"{PC}/api/stac/v1/search?" + urlencode(q), timeout_s=40)
    return _sorted_newest(d.get("features"))


async def catalog_window(http: httpx.AsyncClient, collection: str, bounds: dict, start: str, end: str, limit: int = 60) -> list[dict]:
    q = {"collections": collection, "bbox": ",".join(str(bounds[k]) for k in ("west", "south", "east", "north")), "limit": str(limit), "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z"}
    d = await request_json(http, f"{PC}/api/stac/v1/search?" + urlencode(q), timeout_s=40)
    return _sorted_newest(d.get("features"))


async def fetch_raster(http: httpx.AsyncClient, collection: str, item_id: str, spec: dict, assets: list[str]) -> np.ndarray:
    return decode_npy(await request_bytes(http, raster_url(collection, item_id, spec, assets), timeout_s=40))
