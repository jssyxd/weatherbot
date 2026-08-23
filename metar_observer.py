#!/usr/bin/env python3
"""Two-minute METAR/SPECI dead-bucket observer (tree1).

The program scans published AviationWeather.gov METAR/SPECI reports every two
minutes for 49 verified contract stations. It never uses forecasts: the day's
first report initialises the IANA-local baseline and every later new daily
extreme marks the previous-extreme temperature bucket as dead (candidate
BUY_NO). It never loads credentials, reads a wallet, signs an order, or
submits a real trade. ``mode=live`` is an explicitly blocked compatibility
boundary until a separately reviewed executor is implemented by the user.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from edge_engine import (
    append_jsonl,
    atomic_json_write,
    evaluate_observation,
    load_contract_cities,
    load_market_rules,
    local_market_date,
    observed_temperature_native,
)
from market_adapter import refresh_market_rules
from paper_execution import simulate_paper_fak

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
AWC_ENDPOINT = "https://aviationweather.gov/api/data/metar"
SUPPORTED_TYPES = {"METAR", "SPECI"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    """Accept aware datetimes, AWC ISO strings or epoch seconds, returning UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def as_utc_string(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 {path}: {exc}") from exc


def normalize_stations(raw_stations: Any) -> list[dict[str, str]]:
    """Retained for compatibility and validation of external station lists."""
    if raw_stations is None:
        return []
    if not isinstance(raw_stations, list) or not raw_stations:
        raise ValueError("stations 必须是至少包含一个 ICAO 站点的数组")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw_stations:
        if isinstance(entry, str):
            icao = entry.upper().strip()
            name = icao
        elif isinstance(entry, dict):
            icao = str(entry.get("icao", "")).upper().strip()
            name = str(entry.get("name") or icao).strip()
        else:
            raise ValueError("stations 的每个项目必须是 ICAO 字符串或包含 icao/name 的对象")
        if len(icao) != 4 or not icao.isalnum():
            raise ValueError(f"无效 ICAO 代码: {icao!r}")
        if icao not in seen:
            normalized.append({"icao": icao, "name": name})
            seen.add(icao)
    return normalized


def load_config(config_path: Path) -> dict[str, Any]:
    config = load_json(config_path, None)
    if config is None:
        raise RuntimeError(f"未找到 {config_path.name}。请先复制 config.example.json 为 config.json。")
    if not isinstance(config, dict):
        raise ValueError("配置根节点必须是 JSON 对象")
    interval = int(config.get("scan_interval_seconds", 60))
    if interval < 60:
        raise ValueError("scan_interval_seconds 不得小于 60；AWC 全量缓存按分钟更新")
    history_hours = int(config.get("history_hours", 1))
    if history_hours < 1 or history_hours > 24:
        raise ValueError("history_hours 必须介于 1 和 24")
    chunk_size = int(config.get("stations_per_request", 49))
    if chunk_size < 1 or chunk_size > 100:
        raise ValueError("stations_per_request 必须介于 1 和 100")
    max_age = int(config.get("max_report_age_seconds", 900))
    if max_age < 60:
        raise ValueError("max_report_age_seconds 不得小于 60")
    failure_pause = int(config.get("failure_pause_after_seconds", 1800))
    if failure_pause < 60:
        raise ValueError("failure_pause_after_seconds 不得小于 60")
    warmup_retry = int(config.get("warmup_retry_seconds", 60))
    if warmup_retry < 60:
        raise ValueError("warmup_retry_seconds 不得小于 60")
    warmup_history_hours = int(config.get("warmup_history_hours", 30))
    if warmup_history_hours < 25 or warmup_history_hours > 96:
        raise ValueError("warmup_history_hours 必须介于 25 和 96，以覆盖 IANA 夏令时回拨日")
    warmup_chunk_size = int(config.get("warmup_stations_per_request", 10))
    if warmup_chunk_size < 1 or warmup_chunk_size > 20:
        raise ValueError("warmup_stations_per_request 必须介于 1 和 20，以降低历史报文单次响应截断风险")
    market_rules_max_age = int(config.get("market_rules_max_age_seconds", 1800))
    if market_rules_max_age < 600:
        raise ValueError("market_rules_max_age_seconds 不得小于 600")
    mode = str(config.get("mode", "paper")).lower().strip()
    if mode not in {"paper", "live"}:
        raise ValueError("mode 只能为 paper 或 live")
    return {
        "scan_interval_seconds": interval,
        "history_hours": history_hours,
        "stations_per_request": chunk_size,
        "max_report_age_seconds": max_age,
        "failure_pause_after_seconds": failure_pause,
        "warmup_retry_seconds": warmup_retry,
        "warmup_history_hours": warmup_history_hours,
        "warmup_stations_per_request": warmup_chunk_size,
        "market_rules_max_age_seconds": market_rules_max_age,
        "mode": mode,
        "state_path": BASE_DIR / str(config.get("state_path", "data/state.json")),
        "event_dir": BASE_DIR / str(config.get("event_dir", "data/observations")),
        "signal_dir": BASE_DIR / str(config.get("signal_dir", "data/signals")),
        "health_path": BASE_DIR / str(config.get("health_path", "data/health.json")),
        "contract_cities_path": BASE_DIR / str(config.get("contract_cities_path", "config/contract_cities.json")),
        "market_rules_path": BASE_DIR / str(config.get("market_rules_path", "data/market_rules.json")),
        "stations": normalize_stations(config.get("stations")) if config.get("stations") is not None else [],
    }


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def fetch_awc_reports(station_ids: list[str], history_hours: int) -> tuple[list[dict[str, Any]], str]:
    query = urllib.parse.urlencode({"ids": ",".join(station_ids), "format": "json", "hours": str(history_hours)}, safe=",")
    url = f"{AWC_ENDPOINT}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "weatherbot/2.0 (+https://github.com/jssyxd/weatherbot)"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"AWC 请求失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"AWC 网络请求失败: {exc.reason}") from exc
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWC 返回了不可解析的 JSON") from exc
    if not isinstance(result, list):
        raise RuntimeError("AWC 返回格式异常：预期为报告数组")
    return result, url


def report_key(report: dict[str, Any]) -> str:
    icao = str(report.get("icaoId", "")).upper()
    kind = str(report.get("metarType", "")).upper()
    report_time = as_utc_string(report.get("reportTime") or report.get("obsTime")) or "unknown-time"
    raw = str(report.get("rawOb", ""))
    return "|".join((icao, kind, report_time, raw))


def normalize_report(report: dict[str, Any], station_names: dict[str, str], source_endpoint: str, fetched_at: str) -> dict[str, Any] | None:
    report_type = str(report.get("metarType", "")).upper()
    icao = str(report.get("icaoId", "")).upper()
    raw = str(report.get("rawOb", "")).strip()
    if report_type not in SUPPORTED_TYPES or not icao or not raw:
        return None
    report_time = parse_time(report.get("reportTime") or report.get("obsTime"))
    receipt_time = parse_time(report.get("receiptTime"))
    delay_seconds: float | None = None
    delay_status = "unavailable"
    if report_time and receipt_time:
        computed_delay = round((receipt_time - report_time).total_seconds(), 3)
        if computed_delay >= 0:
            delay_seconds = computed_delay
            delay_status = "available"
        else:
            delay_status = "source_time_inconsistent"
    return {
        "event_id": report_key(report),
        "source": "AviationWeather.gov Data API",
        "source_endpoint": source_endpoint,
        "fetched_at_utc": fetched_at,
        "airport_icao": icao,
        "airport_name": station_names.get(icao, report.get("name") or icao),
        "report_type": report_type,
        "report_time_utc": as_utc_string(report.get("reportTime") or report.get("obsTime")),
        "receipt_time_utc": as_utc_string(report.get("receiptTime")),
        "awc_receipt_delay_seconds": delay_seconds,
        "awc_receipt_delay_status": delay_status,
        "temperature_c": report.get("temp"),
        "dewpoint_c": report.get("dewp"),
        "wind_direction_degrees": report.get("wdir"),
        "wind_speed_kt": report.get("wspd"),
        "visibility_meters": report.get("visib"),
        "flight_category": report.get("fltCat"),
        "is_correction": " COR " in f" {raw} ",
        "raw_metar": raw,
    }


def load_state(state_path: Path) -> dict[str, Any]:
    state = load_json(state_path, {"seen": {}, "last_successful_scan_utc": None})
    if not isinstance(state, dict) or not isinstance(state.get("seen", {}), dict):
        raise RuntimeError("状态文件结构无效；请先备份并删除该文件后重试")
    for key, default in (
        ("seen", {}), ("last_successful_scan_utc", None), ("edge_configs", {}),
        ("edge_failures", {}), ("daily_extrema", {}), ("daily_warmup", {}), ("handled_candidate_buckets", {}),
        ("market_rules", []), ("market_rules_refreshed_at_utc", None), ("market_failures", {}), ("consecutive_failure_started_utc", None),
        ("paper_city_day_notional", {}), ("paper_city_day_total_debit", {}), ("execution_paused", False),
    ):
        state.setdefault(key, default)
    return state


def prune_seen(seen: dict[str, str], keep_hours: int = 72) -> dict[str, str]:
    cutoff = utc_now() - timedelta(hours=keep_hours)
    return {key: first_seen for key, first_seen in seen.items() if (timestamp := parse_time(first_seen)) and timestamp >= cutoff}


def prune_state(state: dict[str, Any], keep_days: int = 3) -> None:
    cutoff = (utc_now() - timedelta(days=keep_days)).date().isoformat()
    for key in ("daily_extrema", "daily_warmup"):
        state[key] = {item_key: value for item_key, value in state.get(key, {}).items() if str(value.get("market_local_date", "")) >= cutoff}


def _warmup_state_key(city: dict[str, Any], local_date: str) -> str:
    return f"{city['city_id']}|{local_date}"


def _warmup_is_complete(state: dict[str, Any], city: dict[str, Any], local_date: str) -> bool:
    entry = state.get("daily_warmup", {}).get(_warmup_state_key(city, local_date), {})
    return entry.get("status") == "complete" and entry.get("market_local_date") == local_date


def _warmup_due_cities(state: dict[str, Any], cities: dict[str, dict[str, Any]], target_dates: dict[str, str], retry_seconds: int) -> list[dict[str, Any]]:
    now = utc_now()
    due: list[dict[str, Any]] = []
    for city in cities.values():
        local_date = target_dates[city["icao"]]
        if _warmup_is_complete(state, city, local_date):
            continue
        entry = state.get("daily_warmup", {}).get(_warmup_state_key(city, local_date), {})
        attempted = parse_time(entry.get("last_attempt_utc"))
        if attempted is None or (now - attempted).total_seconds() >= retry_seconds:
            due.append(city)
    return due


def _rebuild_daily_extrema_from_history(state: dict[str, Any], cities: dict[str, dict[str, Any]], reports: list[dict[str, Any]], fetched_at: str, source_endpoint: str, target_dates: dict[str, str]) -> dict[str, int]:
    """Rebuild only current market-local days; this path never emits signals or marks events seen."""
    values: dict[str, list[tuple[float, str]]] = {}
    for report in reports:
        report_type = str(report.get("metarType", "")).upper()
        icao = str(report.get("icaoId", "")).upper()
        city = cities.get(icao)
        raw = str(report.get("rawOb", "")).strip()
        report_time = parse_time(report.get("reportTime") or report.get("obsTime"))
        if report_type not in SUPPORTED_TYPES or city is None or not raw or report_time is None:
            continue
        local_date = local_market_date(report_time, city)
        if local_date != target_dates[icao]:
            continue
        temperature = observed_temperature_native({"raw_metar": raw, "temperature_c": report.get("temp")}, city)
        if temperature is None:
            continue
        value, _precision = temperature
        values.setdefault(_warmup_state_key(city, local_date), []).append((value, as_utc_string(report_time)))

    extrema: dict[str, Any] = state.setdefault("daily_extrema", {})
    warmups: dict[str, Any] = state.setdefault("daily_warmup", {})
    complete_count = 0
    missing_count = 0
    for city in cities.values():
        local_date = target_dates[city["icao"]]
        key = _warmup_state_key(city, local_date)
        series = values.get(key, [])
        if not series:
            extrema.pop(key, None)
            warmups[key] = {
                "status": "failed_no_current_local_day_reports", "city_id": city["city_id"], "icao": city["icao"],
                "market_local_date": local_date, "last_attempt_utc": fetched_at, "source_endpoint": source_endpoint,
                "history_replay_emits_signals": False,
            }
            missing_count += 1
            continue
        extrema[key] = {
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "market_unit": city["market_unit"], "high": max(item[0] for item in series), "low": min(item[0] for item in series),
            "initialized_by_event_id": "iana_local_day_warmup", "updated_at_utc": fetched_at,
            "warmup_report_count": len(series), "warmup_earliest_report_time_utc": min(item[1] for item in series),
            "warmup_latest_report_time_utc": max(item[1] for item in series),
        }
        warmups[key] = {
            "status": "complete", "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "last_attempt_utc": fetched_at, "completed_at_utc": fetched_at, "source_endpoint": source_endpoint,
            "history_report_count": len(series), "history_replay_emits_signals": False,
        }
        complete_count += 1
    return {"complete": complete_count, "missing_current_local_day_reports": missing_count}


def warm_up_current_local_days(config: dict[str, Any], state: dict[str, Any], cities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Fail closed until a deterministic reportTime-to-IANA replay has completed for every current local day."""
    target_dates = {icao: local_market_date(utc_now(), city) for icao, city in cities.items()}
    due = _warmup_due_cities(state, cities, target_dates, config["warmup_retry_seconds"])
    if not due:
        return {"status": "already_complete", "city_count": len(cities)}
    fetched_at = iso_now()
    station_ids = list(cities)
    all_reports: list[dict[str, Any]] = []
    endpoints: list[str] = []
    try:
        for station_chunk in chunks(station_ids, config["warmup_stations_per_request"]):
            reports, endpoint = fetch_awc_reports(station_chunk, config["warmup_history_hours"])
            all_reports.extend(reports)
            endpoints.append(endpoint)
    except Exception as exc:
        warmups: dict[str, Any] = state.setdefault("daily_warmup", {})
        for city in due:
            local_date = target_dates[city["icao"]]
            warmups[_warmup_state_key(city, local_date)] = {
                "status": "failed_fetch", "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "last_attempt_utc": fetched_at, "error": f"{type(exc).__name__}: {exc}", "history_replay_emits_signals": False,
            }
        return {"status": "failed_fetch", "due_city_count": len(due), "error": f"{type(exc).__name__}: {exc}"}
    due_by_icao = {city["icao"]: city for city in due}
    summary = _rebuild_daily_extrema_from_history(state, due_by_icao, all_reports, fetched_at, ";".join(endpoints), target_dates)
    return {"status": "completed", "due_city_count": len(due), "reports_seen": len(all_reports), "history_hours": config["warmup_history_hours"], **summary}


def cache_is_fresh(timestamp: Any, max_age_seconds: int) -> bool:
    parsed = parse_time(timestamp)
    return parsed is not None and 0 <= (utc_now() - parsed).total_seconds() <= max_age_seconds


def market_rules_cover_local_days(state: dict[str, Any], cities: dict[str, dict[str, Any]]) -> bool:
    """True when cached rules include every city's current IANA-local market day.

    Guards against the local-midnight gap: without it, a city that rolled to a
    new local day could keep matching yesterday's rules until the refresh
    interval elapses, silently suppressing candidates.
    """
    rules = state.get("market_rules", [])
    if not rules:
        return False
    for city in cities.values():
        local_date = local_market_date(utc_now(), city)
        if not any(
            rule.get("city_id") == city["city_id"] and rule.get("market_local_date") == local_date
            for rule in rules
        ):
            return False
    return True


def refresh_market_rules_if_due(state: dict[str, Any], config: dict[str, Any], cities: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Refresh Gamma market rules when stale or when any local day rolled over.

    tree1 removes all forecast-edge configuration; only market rules (bucket /
    token structure) are refreshed.
    """
    if cache_is_fresh(state.get("market_rules_refreshed_at_utc"), config["market_rules_max_age_seconds"]) and market_rules_cover_local_days(state, cities):
        return None
    local_dates = {icao: local_market_date(utc_now(), city) for icao, city in cities.items()}
    try:
        rules, failures = refresh_market_rules(cities, local_dates)
        state["market_rules"] = rules
        state["market_failures"] = failures
        state["market_rules_refreshed_at_utc"] = iso_now()
        return {"market_rules": len(rules), "market_failures": len(failures)}
    except Exception as exc:
        state["last_market_refresh_error"] = f"{type(exc).__name__}: {exc}"
        return {"market_refresh_error": state["last_market_refresh_error"]}


def enrich_execution(signal: dict[str, Any], mode: str, state: dict[str, Any]) -> dict[str, Any]:
    """Attach a read-only paper-fill estimate or a live safety block; never submit an order."""
    if signal.get("signal_type") != "candidate_no_signal":
        return signal
    result = dict(signal)
    if mode == "paper":
        result["execution"] = simulate_paper_fak(signal, state)
    else:
        result["execution"] = {
            "mode": "live",
            "status": "blocked_no_live_executor",
            "message": "此版本不包含钱包、签名或订单提交器；live 模式只产生阻断审计记录。",
        }
    return result


def format_event(event: dict[str, Any]) -> str:
    delay = event.get("awc_receipt_delay_seconds")
    delay_text = f" | AWC延迟 {delay:.0f}s" if isinstance(delay, (int, float)) else ""
    temperature = event.get("temperature_c")
    temperature_text = f" | {temperature}°C" if temperature is not None else ""
    return f"[新{event['report_type']}] {event['airport_icao']} {event['report_time_utc']}{temperature_text}{delay_text}\n  {event['raw_metar']}"


def scan_once(config: dict[str, Any]) -> dict[str, Any]:
    fetched_at = iso_now()
    state = load_state(config["state_path"])
    cities = load_contract_cities(config["contract_cities_path"])
    warmup_summary = warm_up_current_local_days(config, state, cities)
    # Process published observations with the last known good market rules first.
    market_rules = state.get("market_rules") or load_market_rules(config["market_rules_path"])
    station_names = {icao: city["name"] for icao, city in cities.items()}
    station_ids = list(station_names)
    all_reports: list[dict[str, Any]] = []
    endpoints: list[str] = []
    for station_chunk in chunks(station_ids, config["stations_per_request"]):
        reports, endpoint = fetch_awc_reports(station_chunk, config["history_hours"])
        all_reports.extend(reports)
        endpoints.append(endpoint)
    normalized = [
        record for report in all_reports
        if (record := normalize_report(report, station_names, ";".join(endpoints), fetched_at)) is not None
    ]
    normalized.sort(key=lambda record: (record["report_time_utc"] or "", record["airport_icao"], record["report_type"]))
    seen: dict[str, str] = state["seen"]
    new_events = [record for record in normalized if record["event_id"] not in seen]
    for record in new_events:
        seen[record["event_id"]] = fetched_at
    state["seen"] = prune_seen(seen)
    state["last_successful_scan_utc"] = None  # Set to completion time only after every deterministic stage succeeds.
    state["last_report_count"] = len(normalized)
    state["last_new_event_count"] = len(new_events)
    state["consecutive_failure_started_utc"] = None

    signals: list[dict[str, Any]] = []
    for event in new_events:
        city = cities.get(event["airport_icao"])
        if city is None:
            continue
        report_time = parse_time(event.get("report_time_utc"))
        local_date = local_market_date(report_time, city) if report_time else None
        if local_date is None or not _warmup_is_complete(state, city, local_date):
            signals.append({
                "signal_type": "no_signal", "reason": "daily_extrema_untrusted_warmup_incomplete",
                "event_id": event.get("event_id"), "icao": city["icao"], "market_local_date": local_date,
                "disclaimer": "No candidate is allowed until reportTime-to-IANA historical warm-up completes for this local market day.",
            })
            continue
        if not cache_is_fresh(state.get("market_rules_refreshed_at_utc"), config["market_rules_max_age_seconds"]):
            signals.append({
                "signal_type": "no_signal", "reason": "market_rules_stale",
                "event_id": event.get("event_id"), "icao": city["icao"], "market_local_date": local_date,
                "market_rules_refreshed_at_utc": state.get("market_rules_refreshed_at_utc"),
                "max_market_rules_age_seconds": config["market_rules_max_age_seconds"],
            })
            continue
        for signal in evaluate_observation(
            state, event, city, market_rules, config["max_report_age_seconds"],
        ):
            signals.append(enrich_execution(signal, config["mode"], state))
    # Refresh market rules only after this round's time-sensitive observations.
    refresh_summary = refresh_market_rules_if_due(state, config, cities)
    state["last_successful_scan_utc"] = iso_now()
    prune_state(state)
    atomic_json_write(config["state_path"], state)
    health = write_health_snapshot(config, state, cities)
    event_file = append_jsonl(config["event_dir"] / f"{utc_now().strftime('%Y-%m-%d')}.jsonl", new_events)
    signal_file = append_jsonl(config["signal_dir"] / f"{utc_now().strftime('%Y-%m-%d')}.jsonl", signals)
    for event in new_events:
        print(format_event(event))
    candidate_count = sum(item.get("signal_type") == "candidate_no_signal" for item in signals)
    print(
        f"[扫描完成] {fetched_at} | 合同站 {len(station_ids)} | 近期报告 {len(normalized)} | 新增事件 {len(new_events)} | "
        f"候选NO信号 {candidate_count} | 模式 {config['mode']}" + (f" | 配置刷新 {refresh_summary}" if refresh_summary else "")
    )
    return {
        "fetched_at_utc": fetched_at,
        "station_count": len(station_ids),
        "reports_seen": len(normalized),
        "new_events": len(new_events),
        "candidate_signals": candidate_count,
        "warmup": warmup_summary,
        "health_status": health["status"],
        "event_file": str(event_file) if event_file else None,
        "signal_file": str(signal_file) if signal_file else None,
        "mode": config["mode"],
    }


def sleep_to_next_interval(interval_seconds: int) -> None:
    now = time.time()
    time.sleep(max(0.1, interval_seconds - (now % interval_seconds)))


def acquire_single_instance_lock(state_path: Path):
    """Prevent concurrent observers from clobbering each other's state writes.

    The 2026-08-23 toronto audit showed an old process overwriting a freshly
    corrected state.json during restart; an exclusive flock makes that
    read-modify-write race impossible.
    """
    lock_path = state_path.parent / "observer.lock"
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"已有 metar_observer 实例持有 {lock_path}；请先停止旧进程（如 pgrep -f metar_observer）再启动。"
        ) from exc
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def run_loop(config: dict[str, Any]) -> None:
    interval = config["scan_interval_seconds"]
    lock_handle = acquire_single_instance_lock(config["state_path"])
    print(f"候选边缘扫描器已启动：每 {interval} 秒拉取已发布 METAR/SPECI；默认仅 paper 意图，绝不提交真实订单。")
    # Startup: IANA local-day warm-up first, then fresh market rules.
    startup_state = load_state(config["state_path"])
    startup_cities = load_contract_cities(config["contract_cities_path"])
    startup_warmup = warm_up_current_local_days(config, startup_state, startup_cities)
    startup_rules = refresh_market_rules_if_due(startup_state, config, startup_cities)
    atomic_json_write(config["state_path"], startup_state)
    print(f"[启动 IANA warm-up] {startup_warmup}")
    print(f"[启动市场规则] {startup_rules or {'status': 'cached'}}")
    failure_started: datetime | None = None
    while True:
        try:
            scan_once(config)
            failure_started = None
        except KeyboardInterrupt:
            print("\n扫描器已停止。")
            lock_handle.close()
            return
        except Exception as exc:
            write_failure_health(config, exc)
            failure_started = failure_started or utc_now()
            elapsed = (utc_now() - failure_started).total_seconds()
            status = "[扫描暂停：连续失败达到阈值]" if elapsed >= config["failure_pause_after_seconds"] else "[扫描失败]"
            print(f"{status} {type(exc).__name__}: {exc}", file=sys.stderr)
        sleep_to_next_interval(interval)


def build_health_snapshot(config: dict[str, Any], state: dict[str, Any], cities: dict[str, dict[str, Any]], last_error: str | None = None) -> dict[str, Any]:
    now = utc_now()
    target_dates = {icao: local_market_date(now, city) for icao, city in cities.items()}
    untrusted_warmups = [
        {"icao": city["icao"], "market_local_date": target_dates[city["icao"]], "status": state.get("daily_warmup", {}).get(_warmup_state_key(city, target_dates[city["icao"]]), {}).get("status", "missing")}
        for city in cities.values() if not _warmup_is_complete(state, city, target_dates[city["icao"]])
    ]
    stale_rules: list[dict[str, str]] = []
    for city in cities.values():
        local_date = target_dates[city["icao"]]
        if not market_rules_cover_local_days(state, cities):
            stale_rules.append({"icao": city["icao"], "market_local_date": local_date, "detail": "market_rules_missing_current_local_day"})
    last_scan_fresh = cache_is_fresh(state.get("last_successful_scan_utc"), config["scan_interval_seconds"] * 2 + 30)
    market_rules_fresh = cache_is_fresh(state.get("market_rules_refreshed_at_utc"), config["market_rules_max_age_seconds"])
    healthy = (
        not last_error and last_scan_fresh and market_rules_fresh and not untrusted_warmups and not stale_rules
        and not state.get("market_failures", {})
    )
    return {
        "generated_at_utc": iso_now(), "status": "healthy" if healthy else "degraded",
        "critical_path": "deterministic_iana_state_machine_only", "llm_in_minute_path": False,
        "last_successful_scan_utc": state.get("last_successful_scan_utc"), "last_scan_fresh": last_scan_fresh,
        "market_rules_refreshed_at_utc": state.get("market_rules_refreshed_at_utc"), "market_rules_fresh": market_rules_fresh,
        "untrusted_warmup_count": len(untrusted_warmups), "untrusted_warmups": untrusted_warmups,
        "stale_rule_count": len(stale_rules), "stale_rules": stale_rules,
        "market_failure_count": len(state.get("market_failures", {})),
        "last_error": last_error,
    }


def write_health_snapshot(config: dict[str, Any], state: dict[str, Any], cities: dict[str, dict[str, Any]], last_error: str | None = None) -> dict[str, Any]:
    snapshot = build_health_snapshot(config, state, cities, last_error)
    atomic_json_write(config["health_path"], snapshot)
    return snapshot


def write_failure_health(config: dict[str, Any], error: Exception) -> None:
    prior = load_json(config["health_path"], {})
    if not isinstance(prior, dict):
        prior = {}
    prior.update({
        "generated_at_utc": iso_now(), "status": "degraded", "critical_path": "deterministic_iana_state_machine_only",
        "llm_in_minute_path": False, "last_error": f"{type(error).__name__}: {error}",
    })
    atomic_json_write(config["health_path"], prior)


def show_status(config: dict[str, Any]) -> None:
    state = load_state(config["state_path"])
    cities = load_contract_cities(config["contract_cities_path"])
    health = build_health_snapshot(config, state, cities)
    print(json.dumps({
        "contract_station_count": len(cities),
        "scan_interval_seconds": config["scan_interval_seconds"],
        "max_report_age_seconds": config["max_report_age_seconds"],
        "mode": config["mode"],
        "live_executor": "not_present",
        "last_successful_scan_utc": state.get("last_successful_scan_utc"),
        "market_rules": len(state.get("market_rules", [])),
        "market_failures": len(state.get("market_failures", {})),
        "dedupe_entries": len(state.get("seen", {})),
        "health_status": health["status"],
        "untrusted_warmup_count": health["untrusted_warmup_count"],
        "market_rules_fresh": health["market_rules_fresh"],
    }, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每分钟扫描已发布 METAR/SPECI 的候选温度边缘工具")
    parser.add_argument("command", choices=("once", "run", "status"), nargs="?", default="once")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径（默认：config.json）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        if args.command == "once":
            lock_handle = acquire_single_instance_lock(config["state_path"])
            try:
                scan_once(config)
            finally:
                lock_handle.close()
        elif args.command == "run":
            run_loop(config)
        else:
            show_status(config)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
