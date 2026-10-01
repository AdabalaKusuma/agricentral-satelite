"""Planetary Computer NPY cutouts into numpy, with the Worker's shape and dtype checks."""
from __future__ import annotations

import io

import numpy as np

from ..errors import UpstreamError

ALLOWED = {"<f4", "<f8", "<u2", "<i2", "|u1"}


def decode_npy(buffer: bytes) -> np.ndarray:
    """Return a float64 array of shape (bands, height, width)."""
    if len(buffer) < 10 or buffer[0] != 0x93 or buffer[1:6] != b"NUMPY":
        raise UpstreamError("Invalid satellite raster")
    try:
        arr = np.load(io.BytesIO(buffer), allow_pickle=False)
    except Exception as exc:  # truncated header or data
        raise UpstreamError("Incomplete satellite raster") from exc
    if arr.ndim != 3 or arr.dtype.str not in ALLOWED and arr.dtype.str.lstrip("<|=") not in {d.lstrip("<|") for d in ALLOWED}:
        raise UpstreamError("Unsupported satellite raster format")
    return np.ascontiguousarray(arr, dtype=np.float64)
