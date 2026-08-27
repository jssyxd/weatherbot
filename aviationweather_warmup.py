"""Bounded AviationWeather.gov historical METAR reader for warm-up only.

The source is used only to rebuild the observed IANA-local-day temperature
baseline. It neither produces trade signals nor changes the primary realtime
CheckWX METAR/SPECI source. Callers must retain fail-closed behavior when both
sources are unavailable or insufficient.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AWC_BASE_URL = "https://aviationweather.gov/api/data/metar"
AWC_MAX_ICAOS_PER_REQUEST = 8


class AviationWeatherError(RuntimeError):
    """A deterministic upstream read failed or returned an invalid response."""


def _normalise_record(item: dict[str, Any]) -> dict[str, Any] | None:
    icao = str(item.get("icaoId") or item.get("icao") or item.get("station") or "").upper().strip()
    raw = str(item.get("rawOb") or item.get("raw_text") or item.get("raw") or "").strip()
    observed = item.get("obsTime") or item.get("observed") or item.get("reportTime") or item.get("time")
    if len(icao) != 4 or not icao.isalnum() or not raw or observed is None:
        return None
    # Raw output usually includes the report prefix. Normalising it here keeps
    # the existing METAR/SPECI/COR and temperature parser unchanged.
    if not raw.startswith(("METAR ", "SPECI ")):
        raw = f"METAR {icao} {raw}"
    return {"icao": icao, "raw_text": raw, "observed": observed, "warmup_source": "aviationweather"}


def fetch_aviationweather_history(station_ids: list[str], hours: int = 48, timeout_seconds: int = 20) -> tuple[list[dict[str, Any]], str]:
    """Return raw METAR/SPECI records for at most eight ICAOs.

    Aviation Weather Center documents `ids`, `format=json` and historical
    access under the public Data API. Batches are deliberately bounded below
    the provider's 400-result limit, since a 48-hour airport history can contain
    more than one observation per hour.
    """
    if not station_ids or len(station_ids) > AWC_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"每次 AviationWeather warm-up 请求必须包含 1 至 {AWC_MAX_ICAOS_PER_REQUEST} 个 ICAO")
    if not 1 <= int(hours) <= 72:
        raise ValueError("AviationWeather warm-up hours 必须介于 1 和 72")
    ids = ",".join(str(value).upper().strip() for value in station_ids)
    query = urllib.parse.urlencode({"ids": ids, "format": "json", "hours": int(hours)})
    url = f"{AWC_BASE_URL}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "weatherbot-tree5/5.1 (warmup; contact=repository-issues)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # A documented 204 means a valid query yielded no observations.
        if exc.code == 204:
            return [], url
        raise AviationWeatherError(f"aviationweather_http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AviationWeatherError("aviationweather_network_error") from exc
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AviationWeatherError("aviationweather_invalid_json") from exc
    if not isinstance(decoded, list):
        raise AviationWeatherError("aviationweather_invalid_response_shape")
    records = [_normalise_record(item) for item in decoded if isinstance(item, dict)]
    return [record for record in records if record is not None], url
