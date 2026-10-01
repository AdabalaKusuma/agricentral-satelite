"""The Source interface: one class per dataset, registered by name.

A source declares its name, cache TTL and version tag, and loads the documented JSON shape
for a farm geometry. Bump the tag when the output shape or selection rules change, so cached
answers from the previous rules are not served for the rest of their TTL.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, TypeVar

import httpx

T = TypeVar("T")
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Source:
    name: str
    ttl_ms: int
    tag: str | None
    load: Callable[[httpx.AsyncClient, dict], Awaitable[dict]]
    description: str = ""

    @property
    def cache_prefix(self) -> str:
        return self.name + (f"-{self.tag}" if self.tag else "")


@dataclass
class Settled:
    ok: bool
    value: Any = None
    error: BaseException | None = None

    @property
    def reason(self) -> str | None:
        return None if self.ok else (str(self.error) or self.error.__class__.__name__)


async def settle(coro: Awaitable[T]) -> Settled:
    try:
        return Settled(True, await coro)
    except Exception as exc:  # noqa: BLE001 - a failed scene must not fail the dataset
        return Settled(False, None, exc)


async def all_settled(coros: Iterable[Awaitable]) -> list[Settled]:
    return list(await asyncio.gather(*(settle(c) for c in coros)))


async def map_limit(items: list, limit: int, fn: Callable[[Any], Awaitable]) -> list[Settled]:
    """Run `fn` over `items` with at most `limit` in flight, keeping the input order."""
    sem = asyncio.Semaphore(max(1, limit))

    async def run(item):
        async with sem:
            return await settle(fn(item))

    return list(await asyncio.gather(*(run(it) for it in items)))
