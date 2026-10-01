"""Error types shared by every module.

`ApiError` becomes the `{error, code?}` envelope the frontend already understands.
`UpstreamError` carries the HTTP status of a failed outbound call so callers can tell a
FarmFuture denial (401/403) from an outage.
"""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, error: str, code: str | None = None):
        super().__init__(error)
        self.status = status
        self.error = error
        self.code = code


class UpstreamError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def denied(exc: BaseException | None) -> bool:
    status = getattr(exc, "status", None)
    return status in (401, 403)
