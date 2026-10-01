"""The worker: two loops in one process.

    queue loop   every ~30 seconds   farms the .NET API has asked for by name
    sweep loop   every ~6 hours      every mapped farm, in turn

The queue exists so a grower who has just had their farm mapped does not wait for the next
sweep. The sweep exists so imagery that lands overnight is picked up without anyone asking.

Both call the same `process_farm`, and a farm being worked on is never started twice: the
in-flight set below is the only coordination needed, because this process is the single
writer of these tables. That is also why the container runs exactly one replica - two would
download the same imagery twice and race on the same rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal

import httpx

from ..util.time import now_ms
from .process import process_farm

log = logging.getLogger("satellite.worker")


def event(**fields) -> None:
    """One JSON line per notable thing, so logs are greppable in a container."""
    log.info(json.dumps(fields, default=str))


class Worker:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.in_flight: set[str] = set()
        self.stopping = asyncio.Event()
        self.last_sweep_ms = 0

    async def run(self) -> None:
        limits = httpx.Limits(max_connections=12, max_keepalive_connections=6)
        timeout = httpx.Timeout(60.0, connect=20.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout,
                                     headers={"User-Agent": "agricentral-satellite/1.0"}) as http:
            self.http = http
            event(event="worker_started",
                  sweepEveryMin=self.settings.refresh_interval_min,
                  queuePollSeconds=self.settings.queue_poll_seconds,
                  concurrency=self.settings.farm_concurrency)
            await asyncio.gather(self._queue_loop(), self._sweep_loop())
        event(event="worker_stopped")

    # ---- loops -----------------------------------------------------------------------

    async def _queue_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                jobs = self.db.pending_jobs()
                for job in jobs:
                    if self.stopping.is_set():
                        break
                    if job.farm_id in self.in_flight:
                        continue   # the sweep has it; the queue row waits for the next pass
                    self.db.take_job(job.id)
                    error = None
                    try:
                        # A queued farm is usually new or re-mapped, so its stored scene id
                        # is stale or absent: force a fetch rather than trust the skip.
                        summary = await self._guarded(job.farm_id, force=True)
                        event(event="queue_job", reason=job.reason, **summary)
                    except Exception as exc:  # noqa: BLE001
                        error = str(exc)
                        event(event="queue_job_failed", farm=job.farm_id, message=error)
                    self.db.finish_job(job.id, error)
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any one failure
                event(event="queue_loop_error", message=str(exc))
            await self._sleep(self.settings.queue_poll_seconds)

    async def _sweep_loop(self) -> None:
        while not self.stopping.is_set():
            due = now_ms() - self.last_sweep_ms >= self.settings.refresh_interval_ms
            if due:
                self.last_sweep_ms = now_ms()
                try:
                    await self._sweep()
                except Exception as exc:  # noqa: BLE001
                    event(event="sweep_error", message=str(exc))
            await self._sleep(30)

    async def _sweep(self) -> None:
        farm_ids = self.db.active_farm_ids()
        event(event="sweep_started", farms=len(farm_ids))
        started = now_ms()
        counts: dict[str, int] = {}
        sem = asyncio.Semaphore(self.settings.farm_concurrency)

        async def one(farm_id: str) -> None:
            async with sem:
                if self.stopping.is_set() or farm_id in self.in_flight:
                    return
                try:
                    summary = await self._guarded(farm_id)
                    counts[summary["result"]] = counts.get(summary["result"], 0) + 1
                except Exception as exc:  # noqa: BLE001 - one bad farm is not a bad sweep
                    counts["error"] = counts.get("error", 0) + 1
                    event(event="farm_failed", farm=farm_id, message=str(exc))

        await asyncio.gather(*(one(f) for f in farm_ids))
        event(event="sweep_finished", farms=len(farm_ids),
              seconds=round((now_ms() - started) / 1000, 1), **counts)

    # ---- helpers ---------------------------------------------------------------------

    async def _guarded(self, farm_id: str, force: bool = False) -> dict:
        self.in_flight.add(farm_id)
        try:
            return await process_farm(
                self.db, self.http, farm_id,
                skip_unchanged=self.settings.skip_unchanged_scenes,
                force=force,
                max_cells=self.settings.max_cells,
            )
        finally:
            self.in_flight.discard(farm_id)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown so `docker stop` is not a 30s wait."""
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        event(event="shutdown_requested")
        self.stopping.set()


async def run_worker(db, settings) -> None:
    worker = Worker(db, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:
            # Windows: no signal handlers on the proactor loop. Ctrl+C still raises.
            pass
    await worker.run()
