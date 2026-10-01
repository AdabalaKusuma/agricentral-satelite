"""Fetch and store the whole earth-observation datasets the WSB page reads.

Separate from process.py on purpose. That module extracts four numbers per cell for the
rule engine; this one stores each dataset in full, because the page computes its cards,
layers and exposure maps in the browser and needs the complete documents.

Three rules govern a refresh:

  footprint   a boundary edit changes the hash, so the stored raster no longer describes
              the farm and must be rebuilt regardless of age
  age         past ExpiresAt, ask again
  failure     a failed refresh never clears the payload - the page keeps serving the last
              good answer, dated, rather than losing it to an outage
"""
from __future__ import annotations

import logging
from time import perf_counter

import httpx

from ..sources import CENTRE_DATASETS, DATASETS
from ..util.js import footprint_hash
from ..util.time import now_ms

log = logging.getLogger("satellite.datasets")


def needs_refresh(state: dict | None, footprint: str, force: bool) -> tuple[bool, str]:
    """Whether to fetch, and why - the reason goes in the log so a surprising amount of
    network traffic can be explained after the fact."""
    if force:
        return True, "forced"
    if state is None:
        return True, "absent"
    if state.get("FootprintHash") != footprint:
        return True, "footprint changed"
    if state.get("Status") == "failed":
        return True, "retry after failure"
    expires = state.get("ExpiresAt")
    if expires is None:
        return True, "no expiry recorded"
    # Compare in epoch ms; the column is timezone-aware so this is a plain instant check.
    if expires.timestamp() * 1000 <= now_ms():
        return True, "expired"
    return False, "fresh"


def _source_ref(name: str, value) -> str | None:
    """A short label for what the payload was built from, so a refresh can tell whether
    anything actually changed: the scene id for optical and radar, the window for the
    seasonal composites."""
    if not isinstance(value, dict):
        return None
    current = value.get("current")
    if isinstance(current, dict) and current.get("id"):
        return str(current["id"])[:200]
    window = value.get("window")
    if isinstance(window, dict):
        return f"{window.get('start')}..{window.get('end')}"[:200]
    ids = value.get("ids")
    if isinstance(ids, list) and ids:
        return ",".join(str(i) for i in ids)[:200]
    return None


async def refresh_datasets(db, http: httpx.AsyncClient, farm_id: str, geometry: dict,
                           *, force: bool = False, only: tuple[str, ...] | None = None) -> dict:
    """Bring every dataset for one farm up to date. Returns a per-dataset summary.

    Datasets are fetched one at a time rather than together: they hit the same provider,
    and a farm with eight parallel raster downloads is how one large estate starves every
    other farm in the sweep.
    """
    footprint = footprint_hash(geometry)
    summary: dict[str, str] = {}

    for name, source in DATASETS.items():
        if only and name not in only:
            continue

        state = db.dataset_state(farm_id, name)
        refresh, why = needs_refresh(state, footprint, force)
        if not refresh:
            summary[name] = "fresh"
            continue

        started = perf_counter()
        try:
            # The weather histories describe a point, not an area.
            arg = geometry["center"] if name in CENTRE_DATASETS else geometry
            value = await source.load(http, arg)
            raw, packed = db.write_dataset(
                farm_id, name, value,
                footprint=footprint, source_ref=_source_ref(name, value), ttl_ms=source.ttl_ms)
            summary[name] = "stored"
            log.info("%s %s: %s -> %d KB raw / %d KB gz in %.1fs",
                     farm_id[:8], name, why, raw // 1024, packed // 1024,
                     perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 - one dataset must not stop the others
            summary[name] = "failed"
            db.fail_dataset(farm_id, name, f"{type(exc).__name__}: {exc}")
            log.warning("%s %s failed (%s): %s", farm_id[:8], name, why, exc)

    return summary
