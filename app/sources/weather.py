"""Open-Meteo: the forecast fallback, the 31-day radiation history and the season temperature
and rain history. All three are farm-scale model series for the mapped farm centre."""
from __future__ import annotations

import math
from urllib.parse import urlencode

import httpx

from ..errors import UpstreamError
from ..http import request_json
from ..util.numbers import fixed, is_finite
from ..util.time import DAY_MS, iso_date_ist, iso_now, now_ms, utc_hour
from .base import all_settled

SEASON_POLICY = {"baseC": 10, "hotC": 30, "literatureGdd": [2600, 2900], "windowDays": 400, "fallbackDays": 90, "minHours": 20}
FORECAST_URL = "https://api.open-meteo.com/v1/forecast?"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive?"


def _at(series, i):
    try:
        return series[i]
    except (TypeError, IndexError):
        return None


def _js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


def _condition(code) -> str:
    if code == 0:
        return "Clear sky"
    if code <= 3:
        return "Cloud cover"
    if code <= 48:
        return "Fog"
    if code <= 67:
        return "Rain or drizzle"
    if code <= 77:
        return "Snow"
    if code <= 82:
        return "Rain showers"
    if code <= 86:
        return "Snow showers"
    return "Thunderstorm"


def normalize_open_meteo(d: dict, p: dict, now: int | None = None) -> dict:
    now = now_ms() if now is None else now
    h = (d or {}).get("hourly") or {}
    times = h.get("time")
    if not isinstance(times, list):
        raise UpstreamError("Forecast unavailable")
    slots = []
    for i in range(2, len(times)):
        t = times[i] * 1000
        if utc_hour(t) % 3 != 0 or t < now:
            continue
        rain = [_at(h.get("precipitation"), j) for j in (i - 2, i - 1, i)]
        if not all(is_finite(v) for v in rain):
            continue
        if t > now + 120 * 3_600_000:
            continue

        def aggregate(key, fn):
            values = [_at(h.get(key), j) for j in (i - 2, i - 1, i)]
            return fn(values) if all(is_finite(v) for v in values) else None

        code = _at(h.get("weather_code"), i)
        slots.append({"time": t, "temperatureC": _at(h.get("temperature_2m"), i), "humidity": _at(h.get("relative_humidity_2m"), i), "rainMm": sum(rain),
                      "rainProbability": aggregate("precipitation_probability", max), "cloud": _at(h.get("cloud_cover"), i), "windMs": _at(h.get("wind_speed_10m"), i),
                      "peakWindMs": aggregate("wind_speed_10m", max), "minWindMs": aggregate("wind_speed_10m", min), "gustMs": aggregate("wind_gusts_10m", max),
                      "windDegrees": _at(h.get("wind_direction_10m"), i), "code": code, "condition": _condition(code if code is not None else 3), "night": _at(h.get("is_day"), i) == 0})
    if not slots:
        raise UpstreamError("No upcoming forecast intervals")
    return {"slots": slots, "requestedLocation": p, "modelLocation": {"lat": d.get("latitude"), "lon": d.get("longitude")}, "intervalHours": 3, "windFieldsVersion": 1, "windUnit": "m/s",
            "windConvention": "Meteorological FROM degrees; direction at interval end, gust is the maximum over three hourly values", "timezone": "Asia/Kolkata",
            "source": "Open-Meteo automatic forecast (FarmFuture fallback)", "sourceUrl": "https://open-meteo.com/", "rainInterval": "Sum of three hourly amounts ending at the displayed time"}


async def load_open_meteo_forecast(http: httpx.AsyncClient, center: dict) -> dict:
    q = urlencode({"latitude": str(center["lat"]), "longitude": str(center["lon"]),
                   "hourly": "temperature_2m,relative_humidity_2m,precipitation,precipitation_probability,cloud_cover,wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,is_day",
                   "forecast_days": "6", "timezone": "GMT", "timeformat": "unixtime", "wind_speed_unit": "ms"})
    return normalize_open_meteo(await request_json(http, FORECAST_URL + q, timeout_s=20), center)


def normalize_solar_history(d: dict, p: dict, now: int | None = None) -> dict:
    """Past-days radiation from the Open-Meteo weather model at roughly 10 km. Hourly values are
    means over the hour ending at each timestamp; a farm-scale series, not a point measurement."""
    now = now_ms() if now is None else now
    h = (d or {}).get("hourly") or {}
    times = h.get("time")
    if not isinstance(times, list):
        raise UpstreamError("Radiation history unavailable")
    hours = []
    for i, t0 in enumerate(times):
        if not is_finite(t0):
            continue
        t = t0 * 1000
        dni, dhi, ghi = _at(h.get("direct_normal_irradiance"), i), _at(h.get("diffuse_radiation"), i), _at(h.get("shortwave_radiation"), i)
        if t > now or not all(is_finite(v) and v >= 0 for v in (dni, dhi, ghi)):
            continue
        cloud = _at(h.get("cloud_cover"), i)
        hours.append({"time": t, "dniWm2": dni, "diffuseWm2": dhi, "globalWm2": ghi, "cloud": cloud if is_finite(cloud) else None})
    if not hours:
        raise UpstreamError("No completed radiation hours were returned")
    dd = (d or {}).get("daily") or {}
    daily = []
    for i, t0 in enumerate(dd.get("time") or []):
        if not is_finite(t0):
            continue
        t = t0 * 1000
        if t > now:
            continue
        rad, sun = _at(dd.get("shortwave_radiation_sum"), i), _at(dd.get("sunshine_duration"), i)
        daily.append({"time": t, "globalKwh": fixed(rad / 3.6, 2) if is_finite(rad) else None, "sunshineHours": fixed(sun / 3600, 2) if is_finite(sun) else None})
    return {"hours": hours, "daily": daily, "requestedLocation": p, "modelLocation": {"lat": d.get("latitude"), "lon": d.get("longitude")}, "modelElevationM": d.get("elevation"),
            "hourConvention": "Each hourly value is the mean over the hour ending at its timestamp", "units": {"irradiance": "W/m²", "energy": "kWh/m²", "sunshine": "hours"}, "pastDays": 31,
            "source": "Open-Meteo weather-model radiation (~10 km grid), not a farm measurement", "sourceUrl": "https://open-meteo.com/en/docs", "retrievedAt": iso_now()}


async def load_solar_history(http: httpx.AsyncClient, center: dict) -> dict:
    q = urlencode({"latitude": str(center["lat"]), "longitude": str(center["lon"]), "hourly": "direct_normal_irradiance,diffuse_radiation,shortwave_radiation,cloud_cover",
                   "daily": "shortwave_radiation_sum,sunshine_duration", "past_days": "31", "forecast_days": "1", "timezone": "Asia/Kolkata", "timeformat": "unixtime"})
    return normalize_solar_history(await request_json(http, FORECAST_URL + q, timeout_s=20), center)


def normalize_season_history(archive: dict | None, recent: dict | None, p: dict, now: int | None = None) -> dict:
    """Season temperature and rain history for the mapped farm centre: the Open-Meteo archive for
    the long window, with the recent-days weather model filling any lag. Hourly temperatures are
    instantaneous; rain is the IST calendar-day total, withheld for incomplete days."""
    now = now_ms() if now is None else now
    hourly: dict[float, float] = {}
    source_of: dict[float, str] = {}
    rain: dict[str, float] = {}

    def take(d, source):
        h = (d or {}).get("hourly") or {}
        times = h.get("time")
        if isinstance(times, list):
            for i, t0 in enumerate(times):
                if not is_finite(t0):
                    continue
                t, v = t0 * 1000, _at(h.get("temperature_2m"), i)
                if t > now or not is_finite(v) or t in hourly:
                    continue
                hourly[t] = v
                source_of[t] = source
        dd = (d or {}).get("daily") or {}
        dtimes = dd.get("time")
        if isinstance(dtimes, list):
            for i, t0 in enumerate(dtimes):
                if not is_finite(t0):
                    continue
                t, v = t0 * 1000, _at(dd.get("precipitation_sum"), i)
                if t > now or not is_finite(v) or v < 0:
                    continue
                rain.setdefault(iso_date_ist(t), v)

    take(archive, "archive")
    take(recent, "recent")
    if not hourly:
        raise UpstreamError("Season temperature history unavailable")
    radiation: dict[float, list] = {}

    def take_radiation(d):
        h = (d or {}).get("hourly") or {}
        times = h.get("time")
        if not isinstance(times, list):
            return
        for i, t0 in enumerate(times):
            if not is_finite(t0):
                continue
            t = t0 * 1000
            dni, dhi, ghi = _at(h.get("direct_normal_irradiance"), i), _at(h.get("diffuse_radiation"), i), _at(h.get("shortwave_radiation"), i)
            if t > now or t in radiation or not all(is_finite(v) and v >= 0 for v in (dni, dhi, ghi)):
                continue
            radiation[t] = [_js_round(t / 1000), _js_round(dni), _js_round(dhi), _js_round(ghi)]

    take_radiation(archive)
    take_radiation(recent)
    today = iso_date_ist(now)
    by_date: dict[str, dict] = {}
    for t, v in hourly.items():
        date = iso_date_ist(t)
        d = by_date.setdefault(date, {"date": date, "temps": [], "sources": []})
        d["temps"].append(v)
        if source_of[t] not in d["sources"]:
            d["sources"].append(source_of[t])
    days = []
    for d in sorted(by_date.values(), key=lambda x: x["date"]):
        n = len(d["temps"])
        partial = d["date"] == today or n < 24
        days.append({"date": d["date"], "hours": n, "tMean": fixed(sum(d["temps"]) / n, 2), "tMax": fixed(max(d["temps"]), 1), "tMin": fixed(min(d["temps"]), 1),
                     "hotHours": sum(1 for v in d["temps"] if v >= SEASON_POLICY["hotC"]), "rainMm": None if partial else rain.get(d["date"]),
                     "source": "mixed" if len(d["sources"]) > 1 else d["sources"][0], "partial": partial})
    model = archive if (archive or {}).get("hourly") else recent
    return {"days": days, "from": days[0]["date"], "to": days[-1]["date"],
            "radiation": {"hours": [radiation[t] for t in sorted(radiation)],
                          "convention": "Every hour, night included; each entry is [unix seconds, direct normal, diffuse, global] in W/m², mean over the hour ending at the timestamp",
                          "source": "Open-Meteo archive (ERA5-family reanalysis) with recent-days weather model fill, roughly 10 km grid"},
            "requestedLocation": p, "modelLocation": {"lat": (model or {}).get("latitude"), "lon": (model or {}).get("longitude")}, "modelElevationM": (model or {}).get("elevation"),
            "sources": {"archive": {"available": bool((archive or {}).get("hourly")), "model": "Open-Meteo historical weather archive (ERA5-family reanalysis, roughly 10 km grid)", "url": "https://open-meteo.com/en/docs/historical-weather-api"},
                        "recent": {"available": bool((recent or {}).get("hourly")), "model": "Open-Meteo weather-model past days (roughly 10 km grid)", "url": "https://open-meteo.com/en/docs"}},
            "hotThresholdC": SEASON_POLICY["hotC"], "hourConvention": "Hourly temperatures are instantaneous values at each hour; rain is the IST calendar-day total and is withheld for incomplete days",
            "units": {"temperature": "°C", "rain": "mm"}, "windowDays": SEASON_POLICY["windowDays"], "retrievedAt": iso_now()}


async def load_season_history(http: httpx.AsyncClient, center: dict, log=None) -> dict:
    now = now_ms()
    common = {"latitude": str(center["lat"]), "longitude": str(center["lon"]), "hourly": "temperature_2m,direct_normal_irradiance,diffuse_radiation,shortwave_radiation", "daily": "precipitation_sum", "timezone": "Asia/Kolkata", "timeformat": "unixtime"}
    archive_q = urlencode({**common, "start_date": iso_date_ist(now - SEASON_POLICY["windowDays"] * DAY_MS), "end_date": iso_date_ist(now)})
    recent_q = urlencode({**common, "past_days": "14", "forecast_days": "1"})
    archive, recent = await all_settled([request_json(http, ARCHIVE_URL + archive_q, timeout_s=30), request_json(http, FORECAST_URL + recent_q, timeout_s=20)])
    if not archive.ok and log:
        log({"event": "season_archive_unavailable"})
    return normalize_season_history(archive.value if archive.ok else None, recent.value if recent.ok else None, center, now)
