"""Outbound HTTP with per-call budgets, mirroring the Worker's requestJson."""
from __future__ import annotations

from typing import Any

import httpx

from .errors import UpstreamError


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(40.0),
        headers={"User-Agent": "coffee-estate-twin/python"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
    )


async def request_json(client: httpx.AsyncClient, url: str, *, method: str = "GET", headers: dict | None = None,
                       json_body: Any = None, timeout_s: float = 25.0) -> Any:
    try:
        r = await client.request(method, url, headers=headers, json=json_body, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise UpstreamError("The data service timed out") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Upstream request failed: {exc.__class__.__name__}") from exc
    if r.status_code >= 400:
        raise UpstreamError(f"Upstream HTTP {r.status_code}", status=r.status_code)
    try:
        return r.json()
    except ValueError as exc:
        raise UpstreamError("Upstream returned invalid JSON") from exc


async def request_bytes(client: httpx.AsyncClient, url: str, *, timeout_s: float = 40.0) -> bytes:
    try:
        r = await client.get(url, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise UpstreamError("Satellite source timed out") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Satellite source unreachable: {exc.__class__.__name__}") from exc
    if r.status_code >= 400:
        raise UpstreamError(f"Satellite source HTTP {r.status_code}", status=r.status_code)
    return r.content
