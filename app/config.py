"""Settings from the environment.

This service holds no credentials of its own beyond the database URL. It never calls the
AgriCentral API, has no users and issues no tokens: it reads farm geometry from the
database, fetches public satellite imagery, and writes numbers back.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(value, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Where the three Satellite* tables live: AgriCentral's DefaultConnection database
    # (farmfuture). That is the one with EF migration history, so that is where the
    # migration puts them.
    database_url: str
    # Where farm geometry lives: the BackofficeConnection database (farmfuture_backoffice),
    # which holds "MapandCoordinates" and "Coordinate". Read only. Same host, different
    # database, so this is a second connection rather than a schema change.
    geometry_database_url: str
    # How often to sweep every farm. Sentinel-2 revisits in about five days, so six hours
    # is four catalog checks a day - frequent enough to pick a new scene up the morning it
    # lands, rare enough to cost nothing when there is none.
    refresh_interval_min: int
    # How often to look for work the .NET API has queued (a new farm, a changed boundary).
    queue_poll_seconds: int
    # Farms processed concurrently. Each one holds its rasters in memory while it works, so
    # this is the main lever on peak memory.
    farm_concurrency: int
    # Skip the download when the newest catalog scene is the one already stored. Turn off
    # only to force a rebuild after changing how values are derived.
    skip_unchanged_scenes: bool
    # Refuse a farm whose BOUNDING BOX exceeds this many 20 m cells. Cost scales with the
    # box, not the mapped area, so one stray coordinate can make a few hundred hectares
    # span hundreds of kilometres. 2,000,000 cells is roughly 800 km2 - far beyond any real
    # estate, so anything above it is a data problem rather than a big farm.
    max_cells: int
    log_level: str
    sql_echo: bool

    @property
    def refresh_interval_ms(self) -> int:
        return self.refresh_interval_min * 60_000


def load_settings() -> Settings:
    e = os.environ.get
    url = e("DATABASE_URL") or ""
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Point it at AgriCentral's DefaultConnection database\n"
            "(farmfuture) - the one holding the Satellite* tables, for example:\n"
            "  postgresql+psycopg://user:pass@host:5432/farmfuture"
        )
    geometry_url = e("GEOMETRY_DATABASE_URL") or ""
    if not geometry_url:
        raise SystemExit(
            "GEOMETRY_DATABASE_URL is not set. Point it at AgriCentral's\n"
            "BackofficeConnection database (farmfuture_backoffice) - the one holding\n"
            '"MapandCoordinates", for example:\n'
            "  postgresql+psycopg://user:pass@host:5432/farmfuture_backoffice\n"
            "Set it to the same value as DATABASE_URL if both live in one database."
        )
    return Settings(
        database_url=url,
        geometry_database_url=geometry_url,
        refresh_interval_min=_int(e("REFRESH_INTERVAL_MIN"), 360),
        queue_poll_seconds=max(5, _int(e("QUEUE_POLL_SECONDS"), 30)),
        farm_concurrency=max(1, _int(e("FARM_CONCURRENCY"), 1)),
        skip_unchanged_scenes=_bool(e("SKIP_UNCHANGED_SCENES"), True),
        max_cells=max(1000, _int(e("MAX_CELLS"), 2_000_000)),
        log_level=(e("LOG_LEVEL") or "INFO").upper(),
        sql_echo=_bool(e("SQL_ECHO"), False),
    )
