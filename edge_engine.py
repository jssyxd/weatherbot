"""Deterministic local-day and temperature-edge engine.

This module deliberately has no LLM dependency and never submits a real order.  It
turns published METAR/SPECI observations into auditable *candidate* edge signals.
TAF and Weather Underground forecasts are used only to set activation edges.
"""
from __future__ import annotations

import calendar
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

AWC_TAF_ENDPOINT = "https://aviationweather.gov/api/data/taf"
OPEN_METEO_FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MODEL = "ecmwf_ifs025"
WUNDERGROUND_FORECAST_KEY = "calendarDayTemperature"
TAF_TEMPERATURE_RE = re.compile(r"\b(TX|TN)(M?\d{2})/(\d{4})Z\b")
WU_DAILY_ENDPOINT_RE = re.compile(
    r"https://api(?:[0-9]+)?\.weather\.com/v3/wx/forecast/daily/(?:3day|10day)\?[^\"'\\s<]+"
)
REMARK_TEMPERATURE_RE = re.compile(r"\bT([01])(\d{3})\b")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_contract_cities(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("contract_cities 配置必须为数组")
    by_icao: dict[str, dict[str, Any]] = {}
    for city in raw:
        if not isinstance(city, dict):
            raise ValueError("contract_cities 每项必须为对象")
        required = (
            "city_id", "name", "icao", "timezone", "market_unit", "wu_forecast_url",
            "latitude", "longitude", "coordinate_source",
        )
        missing = [field for field in required if not city.get(field)]
        if missing:
            raise ValueError(f"合同城市配置缺少字段: {', '.join(missing)}")
        icao = str(city["icao"]).upper()
        if len(icao) != 4 or not icao.isalnum():
            raise ValueError(f"合同城市配置的 ICAO 无效: {icao!r}")
        if city["market_unit"] not in {"C", "F"}:
            raise ValueError(f"{icao} 的 market_unit 必须为 C 或 F")
        latitude, longitude = city["latitude"], city["longitude"]
        if not isinstance(latitude, (int, float)) or not -90.0 <= float(latitude) <= 90.0:
            raise ValueError(f"{icao} 的 latitude 必须为 -90 至 90 的数值")
        if not isinstance(longitude, (int, float)) or not -180.0 <= float(longitude) <= 180.0:
            raise ValueError(f"{icao} 的 longitude 必须为 -180 至 180 的数值")
        ZoneInfo(str(city["timezone"]))
        if icao in by_icao:
            raise ValueError(f"合同城市配置 ICAO 重复: {icao}")
        normalized = dict(city)
        normalized["icao"] = icao
        normalized.setdefault("market_city_slug", str(normalized["city_id"]))
        by_icao[icao] = normalized
    if len(by_icao) != 49:
        raise ValueError(f"严格 METAR 范围应有 49 城，当前为 {len(by_icao)}")
    return by_icao


def local_market_date(report_time_utc: datetime, city: dict[str, Any]) -> str:
    return report_time_utc.astimezone(ZoneInfo(city["timezone"])).date().isoformat()


def resolve_taf_group_time(day_hour_minute: str, reference_utc: datetime) -> datetime | None:
    """Resolve TAF DDHHZ or DDHHMMZ near issue time, including month rollovers."""
    if len(day_hour_minute) not in {4, 6} or not day_hour_minute.isdigit():
        return None
    day, hour = int(day_hour_minute[:2]), int(day_hour_minute[2:4])
    minute = int(day_hour_minute[4:]) if len(day_hour_minute) == 6 else 0
    candidates: list[datetime] = []
    for month_shift in (-1, 0, 1):
        year = reference_utc.year
        month = reference_utc.month + month_shift
        if month < 1:
            year -= 1
            month += 12
        elif month > 12:
            year += 1
            month -= 12
        if day > calendar.monthrange(year, month)[1]:
            continue
        candidates.append(datetime(year, month, day, hour, minute, tzinfo=timezone.utc))
    return min(candidates, key=lambda candidate: abs((candidate - reference_utc).total_seconds()), default=None)


def parse_taf_extremes(raw_taf: str, issue_time_utc: datetime, city: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return TX/TN groups whose occurrence is organized by market-local date."""
    result: dict[str, list[dict[str, Any]]] = {}
    for group, raw_temperature, raw_time in TAF_TEMPERATURE_RE.findall(raw_taf):
        occurrence = resolve_taf_group_time(raw_time, issue_time_utc)
        if occurrence is None:
            continue
        temperature_c = -int(raw_temperature[1:]) if raw_temperature.startswith("M") else int(raw_temperature)
        local_date = local_market_date(occurrence, city)
        result.setdefault(local_date, []).append({
            "kind": "high" if group == "TX" else "low",
            "value_c": float(temperature_c),
            "occurrence_utc": as_utc_string(occurrence),
            "raw_group": f"{group}{raw_temperature}/{raw_time}Z",
        })
    return result


def celsius_to_native(value_c: float, unit: str) -> float:
    return value_c if unit == "C" else value_c * 9.0 / 5.0 + 32.0


def native_to_celsius(value: float, unit: str) -> float:
    return value if unit == "C" else (value - 32.0) * 5.0 / 9.0


def taf_edges_for_city_date(taf: dict[str, Any], city: dict[str, Any], target_local_date: str) -> dict[str, dict[str, Any]]:
    raw_taf = str(taf.get("rawTAF", ""))
    issue_time = parse_utc(taf.get("issueTime") or taf.get("bulletinTime"))
    if not raw_taf or not issue_time:
        return {}
    groups = parse_taf_extremes(raw_taf, issue_time, city).get(target_local_date, [])
    output: dict[str, dict[str, Any]] = {}
    highs = [item for item in groups if item["kind"] == "high"]
    lows = [item for item in groups if item["kind"] == "low"]
    if highs:
        item = max(highs, key=lambda candidate: candidate["value_c"])
        value = celsius_to_native(item["value_c"], city["market_unit"])
        output["high"] = build_edge_config(city, target_local_date, "high", value, "taf_tx", item)
    if lows:
        item = min(lows, key=lambda candidate: candidate["value_c"])
        value = celsius_to_native(item["value_c"], city["market_unit"])
        output["low"] = build_edge_config(city, target_local_date, "low", value, "taf_tn", item)
    return output


def build_edge_config(city: dict[str, Any], local_date: str, direction: str, forecast_value: float, source_type: str, source_detail: dict[str, Any]) -> dict[str, Any]:
    if direction not in {"high", "low"}:
        raise ValueError("direction 必须为 high 或 low")
    activation = forecast_value - 1.0 if direction == "high" else forecast_value + 1.0
    return {
        "city_id": city["city_id"],
        "icao": city["icao"],
        "market_local_date": local_date,
        "timezone": city["timezone"],
        "market_unit": city["market_unit"],
        "direction": direction,
        "source_type": source_type,
        "forecast_value_native": round(forecast_value, 4),
        "activation_edge_native": round(activation, 4),
        "source_detail": source_detail,
        "configured_at_utc": as_utc_string(utc_now()),
    }


def _read_url(url: str, timeout: int = 20) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "weatherbot-edge-config/1.0 (+https://github.com/jssyxd/weatherbot)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8"), response.geturl()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"数据请求失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"数据网络请求失败: {exc.reason}") from exc


def fetch_awc_tafs(stations: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    query = urllib.parse.urlencode({"ids": ",".join(stations), "format": "json"}, safe=",")
    payload, final_url = _read_url(f"{AWC_TAF_ENDPOINT}?{query}")
    parsed = json.loads(payload)
    if not isinstance(parsed, list):
        raise RuntimeError("AWC TAF 返回格式异常")
    return {str(item.get("icaoId", "")).upper(): item for item in parsed if item.get("icaoId")}, final_url


def fetch_openmeteo_ecmwf_forecast(city: dict[str, Any]) -> dict[str, Any]:
    """Fetch one city from the explicit ECMWF IFS 0.25° daily forecast endpoint."""
    query = urllib.parse.urlencode({
        "latitude": f"{float(city['latitude']):.6f}",
        "longitude": f"{float(city['longitude']):.6f}",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": str(city["timezone"]),
        "temperature_unit": "fahrenheit" if city["market_unit"] == "F" else "celsius",
        "forecast_days": "2",
        "models": OPEN_METEO_MODEL,
    }, safe=",")
    payload, final_url = _read_url(f"{OPEN_METEO_FORECAST_ENDPOINT}?{query}")
    parsed = json.loads(payload)
    daily = parsed.get("daily") if isinstance(parsed, dict) else None
    if not isinstance(daily, dict):
        raise RuntimeError("Open-Meteo ECMWF 返回缺少 daily 对象")
    required = ("time", "temperature_2m_max", "temperature_2m_min")
    if any(not isinstance(daily.get(field), list) for field in required):
        raise RuntimeError("Open-Meteo ECMWF 缺少日高、日低或当地日期")
    if parsed.get("timezone") != city["timezone"]:
        raise RuntimeError("Open-Meteo ECMWF 返回时区与合同城市时区不一致")
    return {
        "endpoint": final_url,
        "response": parsed,
        "raw_hash": sha256_text(payload),
        "retrieved_at_utc": as_utc_string(utc_now()),
        "requested_model": OPEN_METEO_MODEL,
        "requested_coordinates": {"latitude": city["latitude"], "longitude": city["longitude"]},
        "returned_coordinates": {
            "latitude": parsed.get("latitude"), "longitude": parsed.get("longitude"),
            "elevation": parsed.get("elevation"),
        },
        "returned_timezone": parsed.get("timezone"),
        "utc_offset_seconds": parsed.get("utc_offset_seconds"),
    }


def openmeteo_ecmwf_edges_for_city_date(snapshot: dict[str, Any], city: dict[str, Any], target_local_date: str) -> dict[str, dict[str, Any]]:
    daily = snapshot["response"]["daily"]
    dates = daily["time"]
    highs = daily["temperature_2m_max"]
    lows = daily["temperature_2m_min"]
    for index, local_date in enumerate(dates):
        if local_date != target_local_date:
            continue
        detail = {
            "endpoint": snapshot["endpoint"], "raw_hash": snapshot["raw_hash"],
            "retrieved_at_utc": snapshot["retrieved_at_utc"], "requested_model": snapshot["requested_model"],
            "requested_coordinates": snapshot["requested_coordinates"],
            "returned_coordinates": snapshot["returned_coordinates"],
            "returned_timezone": snapshot["returned_timezone"],
            "utc_offset_seconds": snapshot["utc_offset_seconds"], "daily_time": local_date,
        }
        output: dict[str, dict[str, Any]] = {}
        if index < len(highs) and isinstance(highs[index], (int, float)):
            output["high"] = build_edge_config(city, target_local_date, "high", float(highs[index]), "openmeteo_ecmwf_ifs025_high", detail)
        if index < len(lows) and isinstance(lows[index], (int, float)):
            output["low"] = build_edge_config(city, target_local_date, "low", float(lows[index]), "openmeteo_ecmwf_ifs025_low", detail)
        return output
    return {}


def _extract_weather_com_daily_url(page_html: str, desired_unit: str) -> str:
    unescaped = html.unescape(page_html).replace("\\/", "/")
    match = WU_DAILY_ENDPOINT_RE.search(unescaped)
    if not match:
        raise RuntimeError("未找到 Wunderground Forecast 的日预报数据地址")
    parsed = urllib.parse.urlsplit(match.group(0))
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params["units"] = ["m" if desired_unit == "C" else "e"]
    params["format"] = ["json"]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(params, doseq=True), ""))


def fetch_wunderground_forecast(city: dict[str, Any], cached_endpoint: str | None = None) -> dict[str, Any]:
    """Fetch a WU daily forecast while avoiding repeated large dynamic-page loads.

    On first use the adapter discovers a page-advertised daily endpoint.  Later
    refreshes reuse the cached endpoint; callers may retry without the cache if it
    fails.  No Weather.com key is stored in code, config, or Git.
    """
    page_url = str(city["wu_forecast_url"])
    endpoint = cached_endpoint
    if endpoint is None:
        page_html, page_url = _read_url(page_url)
        endpoint = _extract_weather_com_daily_url(page_html, str(city["market_unit"]))
    payload, final_endpoint = _read_url(endpoint)
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Wunderground Forecast 返回格式异常")
    required = ("calendarDayTemperatureMax", "calendarDayTemperatureMin", "validTimeLocal")
    if any(not isinstance(parsed.get(field), list) for field in required):
        raise RuntimeError("Wunderground Forecast 缺少日高、日低或当地日期")
    return {
        "page_url": page_url,
        "endpoint": final_endpoint,
        "response": parsed,
        "raw_hash": sha256_text(payload),
        "retrieved_at_utc": as_utc_string(utc_now()),
    }


def wunderground_edges_for_city_date(snapshot: dict[str, Any], city: dict[str, Any], target_local_date: str) -> dict[str, dict[str, Any]]:
    response = snapshot["response"]
    dates = response["validTimeLocal"]
    highs = response["calendarDayTemperatureMax"]
    lows = response["calendarDayTemperatureMin"]
    for index, local_time in enumerate(dates):
        if not isinstance(local_time, str) or local_time[:10] != target_local_date:
            continue
        output: dict[str, dict[str, Any]] = {}
        detail = {
            "page_url": snapshot["page_url"],
            "endpoint": snapshot["endpoint"],
            "raw_hash": snapshot["raw_hash"],
            "retrieved_at_utc": snapshot["retrieved_at_utc"],
            "valid_time_local": local_time,
        }
        if index < len(highs) and isinstance(highs[index], (int, float)):
            output["high"] = build_edge_config(city, target_local_date, "high", float(highs[index]), "wu_forecast_high", detail)
        if index < len(lows) and isinstance(lows[index], (int, float)):
            output["low"] = build_edge_config(city, target_local_date, "low", float(lows[index]), "wu_forecast_low", detail)
        return output
    return {}


def edge_key(city_id: str, local_date: str, direction: str) -> str:
    return f"{city_id}|{local_date}|{direction}"


def _group_missing_by_city(missing: list[tuple[dict[str, Any], str]]) -> dict[str, tuple[dict[str, Any], list[str]]]:
    grouped: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for city, direction in missing:
        stored = grouped.get(city["icao"])
        if stored is None:
            grouped[city["icao"]] = (city, [direction])
        else:
            stored[1].append(direction)
    return grouped


def refresh_edge_configs(state: dict[str, Any], cities: dict[str, dict[str, Any]], target_dates: dict[str, str], wu_pause_seconds: float = 0.3) -> dict[str, Any]:
    """Configure every direction through TAF -> explicit ECMWF -> WU -> fail closed."""
    city_list = list(cities.values())
    tafs, taf_endpoint = fetch_awc_tafs([city["icao"] for city in city_list])
    edges: dict[str, dict[str, Any]] = state.setdefault("edge_configs", {})
    failures: dict[str, str] = state.setdefault("edge_failures", {})
    missing_after_taf: list[tuple[dict[str, Any], str]] = []
    taf_count = 0
    ecmwf_count = 0
    wu_count = 0

    for city in city_list:
        local_date = target_dates[city["icao"]]
        taf_edges = taf_edges_for_city_date(tafs.get(city["icao"], {}), city, local_date)
        for direction, config in taf_edges.items():
            config["source_endpoint"] = taf_endpoint
            key = edge_key(city["city_id"], local_date, direction)
            edges[key] = config
            failures.pop(key, None)
            taf_count += 1
        for direction in ("high", "low"):
            if direction not in taf_edges:
                missing_after_taf.append((city, direction))

    missing_after_ecmwf: list[tuple[dict[str, Any], str]] = []
    for city, directions in _group_missing_by_city(missing_after_taf).values():
        local_date = target_dates[city["icao"]]
        try:
            ecmwf_edges = openmeteo_ecmwf_edges_for_city_date(fetch_openmeteo_ecmwf_forecast(city), city, local_date)
            for direction in directions:
                key = edge_key(city["city_id"], local_date, direction)
                config = ecmwf_edges.get(direction)
                if config is None:
                    missing_after_ecmwf.append((city, direction))
                    continue
                edges[key] = config
                failures.pop(key, None)
                ecmwf_count += 1
        except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            for direction in directions:
                failures[edge_key(city["city_id"], local_date, direction)] = f"openmeteo_ecmwf_unavailable:{type(exc).__name__}"
                missing_after_ecmwf.append((city, direction))

    wu_endpoint_cache: dict[str, str] = state.setdefault("wu_endpoint_cache", {})
    for index, (city, directions) in enumerate(_group_missing_by_city(missing_after_ecmwf).values()):
        local_date = target_dates[city["icao"]]
        try:
            cached_endpoint = wu_endpoint_cache.get(city["icao"])
            try:
                snapshot = fetch_wunderground_forecast(city, cached_endpoint)
            except RuntimeError:
                snapshot = fetch_wunderground_forecast(city, None)
            wu_endpoint_cache[city["icao"]] = snapshot["endpoint"]
            wu_edges = wunderground_edges_for_city_date(snapshot, city, local_date)
            for direction in directions:
                key = edge_key(city["city_id"], local_date, direction)
                config = wu_edges.get(direction)
                if config is None:
                    edges.pop(key, None)
                    failures[key] = "wu_target_local_date_unavailable"
                    continue
                edges[key] = config
                failures.pop(key, None)
                wu_count += 1
        except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            for direction in directions:
                key = edge_key(city["city_id"], local_date, direction)
                edges.pop(key, None)
                failures[key] = f"wu_forecast_unavailable:{type(exc).__name__}"
        if index + 1 < len(_group_missing_by_city(missing_after_ecmwf)):
            time.sleep(max(0.0, wu_pause_seconds))

    state["last_edge_refresh_utc"] = as_utc_string(utc_now())
    return {
        "taf_edges": taf_count,
        "ecmwf_edges": ecmwf_count,
        "wu_edges": wu_count,
        "missing_edges": len(failures),
        "taf_endpoint": taf_endpoint,
        "openmeteo_model": OPEN_METEO_MODEL,
    }


def observed_temperature_native(event: dict[str, Any], city: dict[str, Any]) -> tuple[float, str] | None:
    raw = str(event.get("raw_metar") or "")
    remark = REMARK_TEMPERATURE_RE.search(raw)
    if remark:
        value_c = int(remark.group(2)) / 10.0
        if remark.group(1) == "1":
            value_c = -value_c
        return celsius_to_native(value_c, city["market_unit"]), "metar_remark_tenths_c"
    value = event.get("temperature_c")
    if isinstance(value, (int, float)):
        return celsius_to_native(float(value), city["market_unit"]), "metar_body_integer_c"
    return None


def _day_state_key(city: dict[str, Any], local_date: str) -> str:
    return f"{city['city_id']}|{local_date}"


def _bucket_newly_invalidated(bucket: dict[str, Any], direction: str, previous: float | None, current: float) -> bool:
    lo = float(bucket["lo"])
    hi = bucket.get("hi")
    if direction == "high":
        if hi is None:
            return False
        return current >= float(hi) and (previous is None or previous < float(hi))
    return current < lo and (previous is None or previous >= lo)


def select_adjacent_invalidated_bucket(buckets: list[dict[str, Any]], direction: str, previous: float | None, current: float) -> dict[str, Any] | None:
    candidates = [bucket for bucket in buckets if _bucket_newly_invalidated(bucket, direction, previous, current)]
    if not candidates:
        return None
    if direction == "high":
        return max(candidates, key=lambda bucket: (float(bucket.get("hi", -float("inf"))), float(bucket["lo"])))
    return min(candidates, key=lambda bucket: (float(bucket["lo"]), float(bucket.get("hi", float("inf")))))


def market_rules_for(rules: list[dict[str, Any]], city_id: str, local_date: str, direction: str) -> list[dict[str, Any]]:
    return [
        rule for rule in rules
        if rule.get("city_id") == city_id
        and rule.get("market_local_date") == local_date
        and rule.get("direction") == direction
        and rule.get("enabled", True) is True
    ]


def evaluate_observation(state: dict[str, Any], event: dict[str, Any], city: dict[str, Any], market_rules: list[dict[str, Any]], max_latency_seconds: int = 600, max_edge_config_age_seconds: int = 1800) -> list[dict[str, Any]]:
    report_time = parse_utc(event.get("report_time_utc"))
    fetched_at = parse_utc(event.get("fetched_at_utc")) or utc_now()
    if report_time is None:
        return [{"signal_type": "no_signal", "reason": "missing_report_time", "event_id": event.get("event_id")}]
    age = (fetched_at - report_time).total_seconds()
    if age > max_latency_seconds:
        return [{"signal_type": "no_signal", "reason": "report_too_old", "event_id": event.get("event_id"), "age_seconds": round(age, 3)}]
    if event.get("is_correction") is True:
        return [{
            "signal_type": "no_signal", "reason": "correction_requires_full_day_rebuild",
            "event_id": event.get("event_id"),
            "disclaimer": "COR is retained for audit but cannot produce a candidate until full corrected-day replay is implemented.",
        }]
    temperature = observed_temperature_native(event, city)
    if temperature is None:
        return [{"signal_type": "no_signal", "reason": "missing_temperature", "event_id": event.get("event_id")}]
    value, precision = temperature
    local_date = local_market_date(report_time, city)
    day_key = _day_state_key(city, local_date)
    extrema = state.setdefault("daily_extrema", {})
    day = extrema.get(day_key)
    if day is None:
        extrema[day_key] = {
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "market_unit": city["market_unit"], "high": value, "low": value,
            "initialized_by_event_id": event.get("event_id"), "updated_at_utc": event.get("fetched_at_utc"),
        }
        return [{"signal_type": "no_signal", "reason": "daily_baseline_initialized", "event_id": event.get("event_id"), "local_date": local_date}]

    previous_high, previous_low = float(day["high"]), float(day["low"])
    is_new_high, is_new_low = value > previous_high, value < previous_low
    if is_new_high:
        day["high"] = value
    if is_new_low:
        day["low"] = value
    day["updated_at_utc"] = event.get("fetched_at_utc")

    signals: list[dict[str, Any]] = []
    for direction, previous, is_new in (("high", previous_high, is_new_high), ("low", previous_low, is_new_low)):
        config = state.get("edge_configs", {}).get(edge_key(city["city_id"], local_date, direction))
        if not is_new:
            signals.append({"signal_type": "no_signal", "reason": f"not_new_daily_{direction}", "event_id": event.get("event_id"), "local_date": local_date})
            continue
        if config is None:
            signals.append({"signal_type": "no_signal", "reason": f"edge_source_unavailable_{direction}", "event_id": event.get("event_id"), "local_date": local_date})
            continue
        configured_at = parse_utc(config.get("configured_at_utc"))
        if configured_at is None or (fetched_at - configured_at).total_seconds() > max_edge_config_age_seconds:
            signals.append({
                "signal_type": "no_signal", "reason": f"edge_config_stale_{direction}", "event_id": event.get("event_id"),
                "local_date": local_date, "edge_configured_at_utc": config.get("configured_at_utc"),
                "max_edge_config_age_seconds": max_edge_config_age_seconds,
            })
            continue
        activation = float(config["activation_edge_native"])
        in_edge = value >= activation if direction == "high" else value <= activation
        if not in_edge:
            signals.append({"signal_type": "no_signal", "reason": f"outside_{direction}_edge", "event_id": event.get("event_id"), "local_date": local_date, "value": value, "activation": activation})
            continue
        rules = market_rules_for(market_rules, city["city_id"], local_date, direction)
        bucket_candidates: list[dict[str, Any]] = []
        for rule in rules:
            if rule.get("market_unit") != city["market_unit"]:
                continue
            selected = select_adjacent_invalidated_bucket(rule.get("buckets", []), direction, previous, value)
            if selected:
                candidate = dict(selected)
                candidate["market_rule_id"] = rule.get("market_rule_id")
                candidate["market_id"] = candidate.get("market_id")
                candidate["no_token_id"] = candidate.get("no_token_id")
                bucket_candidates.append(candidate)
        if not bucket_candidates:
            signals.append({"signal_type": "no_signal", "reason": f"no_adjacent_{direction}_bucket_invalidated", "event_id": event.get("event_id"), "local_date": local_date, "value": value})
            continue
        if city["market_unit"] == "F" and precision != "metar_remark_tenths_c":
            signals.append({
                "signal_type": "no_signal", "reason": "f_unit_precision_ambiguous",
                "event_id": event.get("event_id"), "local_date": local_date,
                "temperature_native": round(value, 4), "temperature_precision": precision,
                "disclaimer": "An integer-C METAR body temperature cannot safely invalidate a Fahrenheit contract bucket without an RMK T-group.",
            })
            continue
        selected_bucket = (
            max(bucket_candidates, key=lambda bucket: (float(bucket.get("hi", -float("inf"))), float(bucket.get("lo", -float("inf")))))
            if direction == "high"
            else min(bucket_candidates, key=lambda bucket: (float(bucket.get("lo", float("inf"))), float(bucket.get("hi", float("inf")))))
        )
        handle_key = "|".join((str(selected_bucket.get("market_rule_id")), str(selected_bucket.get("bucket_id"))))
        handled = state.setdefault("handled_candidate_buckets", {})
        if handle_key in handled:
            signals.append({"signal_type": "no_signal", "reason": "candidate_bucket_already_handled", "event_id": event.get("event_id"), "local_date": local_date})
            continue
        handled[handle_key] = event.get("fetched_at_utc")
        signals.append({
            "signal_type": "candidate_no_signal",
            "candidate_status": "candidate_invalidated_by_metar",
            "event_id": event.get("event_id"),
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "direction": direction, "market_unit": city["market_unit"],
            "temperature_native": round(value, 4), "temperature_precision": precision,
            "previous_candidate_extreme": round(previous, 4), "activation_edge_native": activation,
            "edge_source_type": config["source_type"], "edge_config": config,
            "bucket": selected_bucket, "handle_key": handle_key,
            "disclaimer": "METAR/SPECI candidate signal only; not settlement-source confirmation.",
        })
    return signals


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> Path | None:
    if not records:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_market_rules(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("market_rules 必须为数组")
    return [item for item in raw if isinstance(item, dict)]
