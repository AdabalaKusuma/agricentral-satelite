"""Time helpers that mirror the JavaScript Worker: Unix milliseconds, ISO strings, IST dates."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DAY_MS = 86_400_000
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_now(ms: int | None = None) -> str:
    """`new Date(ms).toISOString()`: UTC with milliseconds and a Z suffix."""
    ms = now_ms() if ms is None else int(ms)
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_date_ist(ms: float) -> str:
    """The IST calendar date (YYYY-MM-DD) of a Unix-millisecond instant."""
    return datetime.fromtimestamp(ms / 1000, tz=IST).strftime("%Y-%m-%d")


def parse_ms(value) -> float | None:
    """`Date.parse` for the ISO-8601 forms the sources use; None when unparseable (JS NaN)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    if _ISO_DATE.match(s):
        # A bare date is UTC midnight in JavaScript.
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
    try:
        iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
        # Python 3.11 accepts most ISO forms; trim fractional seconds longer than 6 digits.
        m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", iso)
        if m and len(m.group(2)) > 6:
            iso = f"{m.group(1)}.{m.group(2)[:6]}{m.group(3)}"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            # JavaScript treats a date-time without an offset as local time; the Worker ran in
            # UTC, so UTC keeps the same meaning here.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except ValueError:
        return None


def is_iso_date(value) -> bool:
    return isinstance(value, str) and bool(_ISO_DATE.match(value))


def ist_midnight_ms(date: str) -> float | None:
    """`Date.parse(date + 'T00:00:00+05:30')`."""
    return parse_ms(f"{date}T00:00:00+05:30")


def ist_end_of_day_ms(date: str) -> float | None:
    return parse_ms(f"{date}T23:59:59+05:30")


def utc_hour(ms: float) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).hour


def iso_utc_date(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
