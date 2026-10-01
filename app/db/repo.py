"""Database access: read farm geometry, write satellite values.

Two connections, because AgriCentral keeps these in two databases on the same host:

    GEOMETRY_DATABASE_URL  ->  farmfuture_backoffice   "MapandCoordinates", "Coordinate"
                               (BackofficeConnection)   READ ONLY, never written here

    DATABASE_URL           ->  farmfuture    SatelliteCellValues, SatelliteFarmStatus,
                               (DefaultConnection)      SatelliteJobQueue

The satellite tables live in the DefaultConnection database because that is the one with
Entity Framework migration history - ApplicationDbContext. BackofficeDbContext is
database-first and has no migrations, so adding tables there would mean baselining it
first. Nothing is joined across the two: the worker reads geometry, does its own maths,
and writes values.

The tables are created by EF migrations. This module never issues DDL.

Farm polygons are already PostGIS geometry (`Aoi`, Polygon/4326), so a zone's rings come
straight from ST_AsGeoJSON. Farms mapped before that column was populated are rebuilt from
the ordered point rows in "Coordinate" instead.
"""
from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, delete, insert, text, update
from sqlalchemy.engine import Engine

from . import models as m

log = logging.getLogger("satellite.db")

# Zones are rows of "MapandCoordinates" for the farm. SubMapType marks a mapped sub-area;
# ordering puts those first so a farm with both parent and sub areas yields the sub areas.
ZONES_SQL = """
SELECT "Id"::text          AS id,
       "TenantId"::text    AS tenant_id,
       "AreaName"          AS area_name,
       "SubMapType"        AS sub_map_type,
       ST_AsGeoJSON("Aoi") AS geojson
FROM "MapandCoordinates"
WHERE "FarmId" = CAST(:farm_id AS uuid)
ORDER BY "SubMapType" DESC NULLS LAST, "AreaName"
"""

# Fallback for a zone with no Aoi: its boundary points, in the order they were recorded.
POINTS_SQL = """
SELECT "Long" AS lon, "Lat" AS lat
FROM "Coordinate"
WHERE "MapandCoordinatesId" = CAST(:zone_id AS uuid)
ORDER BY "Number"
"""

FARM_IDS_SQL = """
SELECT DISTINCT "FarmId"::text AS farm_id
FROM "MapandCoordinates"
WHERE "FarmId" IS NOT NULL
"""


@dataclass
class FarmGeometry:
    """The shape the dataset loaders expect: zones with rings, plus bounds and centre."""

    farm_id: str
    zones: list[dict] = field(default_factory=list)
    bounds: dict = field(default_factory=dict)
    center: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"zones": self.zones, "bounds": self.bounds, "center": self.center}


@dataclass
class PendingJob:
    id: int
    farm_id: str
    reason: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bounds_of(zones: list[dict]) -> tuple[dict, dict]:
    xs = [p[0] for z in zones for ring in z["rings"] for p in ring]
    ys = [p[1] for z in zones for ring in z["rings"] for p in ring]
    bounds = {"west": min(xs), "east": max(xs), "south": min(ys), "north": max(ys)}
    center = {"lon": (bounds["west"] + bounds["east"]) / 2,
              "lat": (bounds["south"] + bounds["north"]) / 2}
    return bounds, center


def _rings_from_geojson(raw):
    """GeoJSON Polygon coordinates are already [[outer],[hole],...] in [lon, lat] order."""
    if not raw:
        return None
    try:
        g = json.loads(raw)
    except (TypeError, ValueError):
        return None
    kind = g.get("type")
    if kind == "Polygon":
        rings = g.get("coordinates") or []
    elif kind == "MultiPolygon":
        # Largest part by vertex count; a mapped zone is one field in practice.
        parts = g.get("coordinates") or []
        rings = max(parts, key=lambda p: len(p[0]) if p else 0) if parts else []
    else:
        return None
    cleaned = [[[float(p[0]), float(p[1])] for p in ring] for ring in rings if len(ring) >= 4]
    return cleaned or None


class Database:
    """Both connections. `geometry_url` defaults to the values database, which is correct
    only when AgriCentral keeps both in one place."""

    def __init__(self, url: str, geometry_url: str | None = None, echo: bool = False):
        self.engine: Engine = create_engine(url, echo=echo, pool_pre_ping=True, future=True)
        self.geo: Engine = (
            self.engine if not geometry_url or geometry_url == url
            else create_engine(geometry_url, echo=echo, pool_pre_ping=True, future=True)
        )

    # ---- reading AgriCentral's own tables (geometry connection) ----------------------

    def active_farm_ids(self) -> list[str]:
        with self.geo.connect() as c:
            return [r.farm_id for r in c.execute(text(FARM_IDS_SQL))]

    def load_geometry(self, farm_id: str):
        """None when the farm has no usable polygon. The caller records that as a status,
        not a failure: a farm nobody has mapped yet is not an error."""
        with self.geo.connect() as c:
            rows = list(c.execute(text(ZONES_SQL), {"farm_id": farm_id}))
            zones = []
            for r in rows:
                rings = _rings_from_geojson(r.geojson)
                if rings is None:
                    pts = [[float(p.lon), float(p.lat)]
                           for p in c.execute(text(POINTS_SQL), {"zone_id": r.id})]
                    if len(pts) >= 3:
                        if pts[0] != pts[-1]:
                            pts.append(pts[0])
                        rings = [pts]
                if rings:
                    zones.append({"id": r.tenant_id or r.id, "name": r.area_name, "rings": rings})
        if not zones:
            return None
        bounds, center = _bounds_of(zones)
        return FarmGeometry(farm_id=farm_id, zones=zones, bounds=bounds, center=center)

    # ---- the job queue (values connection) -------------------------------------------

    def pending_jobs(self, limit: int = 20) -> list[PendingJob]:
        with self.engine.connect() as c:
            rows = c.execute(
                m.job_queue.select()
                .where(m.job_queue.c.DoneAt.is_(None))
                .order_by(m.job_queue.c.CreatedAt)
                .limit(limit)
            ).mappings().all()
        return [PendingJob(r["Id"], r["FarmId"], r["Reason"]) for r in rows]

    def take_job(self, job_id: int) -> None:
        with self.engine.begin() as c:
            c.execute(update(m.job_queue)
                      .where(m.job_queue.c.Id == job_id)
                      .values(TakenAt=_now()))

    def finish_job(self, job_id: int, error=None) -> None:
        with self.engine.begin() as c:
            c.execute(update(m.job_queue)
                      .where(m.job_queue.c.Id == job_id)
                      .values(DoneAt=_now(), Error=error))

    # ---- farm status -----------------------------------------------------------------

    def last_scene_id(self, farm_id: str):
        with self.engine.connect() as c:
            row = c.execute(
                m.farm_status.select()
                .with_only_columns(m.farm_status.c.SceneId)
                .where(m.farm_status.c.FarmId == farm_id)
            ).first()
        return row.SceneId if row else None

    def set_status(self, farm_id: str, status: str, *, scene_id=None, observed_on=None,
                   cell_count: int = 0, duration_ms=None, message=None) -> None:
        now = _now()
        values = {"Status": status, "LastRunAt": now, "Message": message,
                  "CellCount": cell_count, "DurationMs": duration_ms}
        if scene_id is not None:
            values["SceneId"] = scene_id
        if observed_on is not None:
            values["ObservedOn"] = observed_on
        if status == "ready":
            values["LastSuccessAt"] = now
        with self.engine.begin() as c:
            changed = c.execute(update(m.farm_status)
                                .where(m.farm_status.c.FarmId == farm_id)
                                .values(**values)).rowcount
            if not changed:
                c.execute(insert(m.farm_status).values(FarmId=farm_id, **values))

    def touch_run(self, farm_id: str, message=None) -> None:
        """A run that found nothing new: record that we looked, keep the data as it stands.

        If the farm has no status row yet there is nothing to keep, so write one - otherwise
        a farm whose first run finds unchanged imagery would stay invisible to the API.
        """
        with self.engine.begin() as c:
            changed = c.execute(update(m.farm_status)
                                .where(m.farm_status.c.FarmId == farm_id)
                                .values(LastRunAt=_now(), Message=message)).rowcount
        if not changed:
            self.set_status(farm_id, "preparing", message=message)

    # ---- the values ------------------------------------------------------------------

    # ---- whole datasets --------------------------------------------------------------

    def dataset_state(self, farm_id: str, dataset: str):
        """What is stored for this dataset: its footprint, source and age. Enough to decide
        whether a refresh is needed without pulling the payload itself."""
        with self.engine.connect() as c:
            row = c.execute(
                m.datasets.select()
                .with_only_columns(m.datasets.c.FootprintHash, m.datasets.c.SourceRef,
                                   m.datasets.c.RetrievedAt, m.datasets.c.ExpiresAt,
                                   m.datasets.c.Status)
                .where((m.datasets.c.FarmId == farm_id) & (m.datasets.c.Dataset == dataset))
            ).mappings().first()
        return dict(row) if row else None

    def write_dataset(self, farm_id: str, dataset: str, value, *, footprint: str | None,
                      source_ref: str | None, ttl_ms: int) -> tuple[int, int]:
        """Store a dataset as gzipped JSON and return (raw, compressed) byte counts.

        mtime=0 so the same payload compresses to identical bytes - otherwise every write
        would differ by a timestamp and look like a change to anything comparing them.
        """
        raw = json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")
        blob = gzip.compress(raw, compresslevel=6, mtime=0)
        now = _now()
        values = {
            "Payload": blob, "PayloadBytes": len(raw), "CompressedBytes": len(blob),
            "FootprintHash": footprint, "SourceRef": source_ref,
            "RetrievedAt": now, "ExpiresAt": now + timedelta(milliseconds=ttl_ms),
            "Status": "available", "Message": None,
        }
        self._upsert_dataset(farm_id, dataset, values)
        return len(raw), len(blob)

    def fail_dataset(self, farm_id: str, dataset: str, message: str) -> None:
        """Record why a refresh failed WITHOUT touching the payload. The page keeps serving
        the last good answer, dated and marked, rather than losing it to an outage."""
        with self.engine.begin() as c:
            changed = c.execute(update(m.datasets)
                                .where((m.datasets.c.FarmId == farm_id)
                                       & (m.datasets.c.Dataset == dataset))
                                .values(Status="failed", Message=message[:2000],
                                        RetrievedAt=_now())).rowcount
            if not changed:
                c.execute(insert(m.datasets).values(
                    FarmId=farm_id, Dataset=dataset, Payload=None, PayloadBytes=0,
                    CompressedBytes=0, RetrievedAt=_now(), Status="failed",
                    Message=message[:2000]))

    def _upsert_dataset(self, farm_id: str, dataset: str, values: dict) -> None:
        with self.engine.begin() as c:
            changed = c.execute(update(m.datasets)
                                .where((m.datasets.c.FarmId == farm_id)
                                       & (m.datasets.c.Dataset == dataset))
                                .values(**values)).rowcount
            if not changed:
                c.execute(insert(m.datasets).values(FarmId=farm_id, Dataset=dataset, **values))

    # ---- the per-cell values ----------------------------------------------------------

    def replace_cells(self, farm_id: str, rows: list[dict]) -> int:
        """One transaction: the farm's old cells out, the new ones in. A farm is never left
        half-written, and a changed boundary cannot leave orphaned cells behind."""
        if not rows:
            return 0
        with self.engine.begin() as c:
            c.execute(delete(m.cell_values).where(m.cell_values.c.FarmId == farm_id))
            for start in range(0, len(rows), 1000):
                c.execute(insert(m.cell_values), rows[start:start + 1000])
        return len(rows)
