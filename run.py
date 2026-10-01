"""Entry point.

    python run.py                      run the worker (both loops) - what the container does
    python run.py --once               one sweep of every farm, then exit
    python run.py --farm <uuid>        one farm, then exit - use this to measure a big farm
    python run.py --farm <uuid> --force  ignore the stored scene id and re-fetch

The --farm form is how to answer "does this survive our largest estates?" before wiring
anything up: it prints the cell count, the elapsed time and the peak memory for one farm.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.config import load_settings
from app.db import Database
from app.worker.loop import run_worker
from app.worker.process import process_farm


def parse_args(argv):
    p = argparse.ArgumentParser(description="AgriCentral satellite worker")
    p.add_argument("--once", action="store_true", help="one sweep of every farm, then exit")
    p.add_argument("--farm", metavar="UUID", help="process one farm, then exit")
    p.add_argument("--force", action="store_true", help="re-fetch even if the scene is unchanged")
    return p.parse_args(argv)


async def run_one(db, settings, farm_id: str, force: bool) -> int:
    import httpx
    from time import perf_counter

    started = perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0)) as http:
        summary = await process_farm(db, http, farm_id,
                                     skip_unchanged=settings.skip_unchanged_scenes,
                                     force=force, max_cells=settings.max_cells)
    elapsed = perf_counter() - started
    print()
    print(f"  farm      {summary.get('farm')}")
    print(f"  result    {summary.get('result')}")
    print(f"  cells     {summary.get('cells', 0)}")
    print(f"  scene     {summary.get('scene') or '-'}")
    if summary.get("missing"):
        print(f"  missing   {', '.join(summary['missing'])}")
    if summary.get("reason"):
        print(f"  reason    {summary['reason']}")
    print(f"  elapsed   {elapsed:.1f}s")
    print(f"  peak RSS  {_peak_mb()}")
    print()
    return 0 if summary.get("result") in {"ready", "unchanged"} else 1


async def run_sweep(db, settings) -> int:
    import httpx
    from time import perf_counter

    farm_ids = db.active_farm_ids()
    print(f"{len(farm_ids)} mapped farms")
    started = perf_counter()
    results: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0)) as http:
        for farm_id in farm_ids:
            try:
                summary = await process_farm(db, http, farm_id,
                                             skip_unchanged=settings.skip_unchanged_scenes,
                                             max_cells=settings.max_cells)
                key = summary.get("result", "?")
            except Exception as exc:  # noqa: BLE001
                key = "error"
                print(f"  {farm_id}  ERROR  {exc}")
            results[key] = results.get(key, 0) + 1
    print(f"\n  {dict(sorted(results.items()))}")
    print(f"  elapsed   {perf_counter() - started:.1f}s")
    print(f"  peak RSS  {_peak_mb()}\n")
    return 0


def _peak_mb() -> str:
    """Peak resident memory, where the platform will tell us."""
    try:
        import platform
        import resource  # POSIX only
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes; macOS reports bytes.
        mb = peak / 1048576 if platform.system() == "Darwin" else peak / 1024
        return f"{mb:.0f} MB"
    except ImportError:
        try:
            import psutil  # optional
            return f"{psutil.Process().memory_info().rss / 1048576:.0f} MB"
        except Exception:  # noqa: BLE001
            return "n/a (install psutil, or measure with docker stats)"


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db = Database(settings.database_url,
                  geometry_url=settings.geometry_database_url,
                  echo=settings.sql_echo)

    if args.farm:
        return asyncio.run(run_one(db, settings, args.farm, args.force))
    if args.once:
        return asyncio.run(run_sweep(db, settings))
    asyncio.run(run_worker(db, settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
