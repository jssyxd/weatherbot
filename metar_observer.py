#!/usr/bin/env python3
"""CheckWX v2 METAR/SPECI dead-bucket observer (tree4).

The program reads published CheckWX Aviation Weather API METAR/SPECI reports
for the verified contract stations. It never uses forecasts: after a complete
CheckWX historical replay for the IANA-local day, each later new daily extreme
may mark an already-impossible temperature bucket as a candidate BUY_NO. The
API key is read only from an environment variable, never from configuration,
URLs, audit records, or source control. It never loads a wallet, signs an
order, or submits a real trade. ``mode=live`` remains explicitly blocked.
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
from tree2_execution import simulate as simulate_tree2
from audit_store import AuditStore

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
CHECKWX_BASE_URL = "https://api.checkwx.com/v2"
CHECKWX_API_KEY_ENV_DEFAULT = "CHECKWX_API_KEY"
AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
MIN_SCAN_INTERVAL_SECONDS = 60
CHECKWX_MAX_ICAOS_PER_REQUEST = 25
AWC_MAX_RESULTS_PER_QUERY = 400
SUPPORTED_TYPES = {"METAR", "SPECI"}


class CheckWXRateLimitError(RuntimeError):
    """A 429 response carrying an optional, vendor-specified retry delay."""

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("CheckWX 请求频率或日限额已达到上限（HTTP 429）")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    """Accept aware datetimes, ISO-8601 strings or epoch seconds, returning UTC."""
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
    interval = int(config.get("scan_interval_seconds", MIN_SCAN_INTERVAL_SECONDS))
    if interval < MIN_SCAN_INTERVAL_SECONDS:
        raise ValueError(f"scan_interval_seconds 不得小于 {MIN_SCAN_INTERVAL_SECONDS}")
    rate_limit_backoff = int(config.get("rate_limit_backoff_seconds", interval))
    if rate_limit_backoff < interval:
        raise ValueError("rate_limit_backoff_seconds 不得小于 scan_interval_seconds")
    chunk_size = int(config.get("stations_per_request", CHECKWX_MAX_ICAOS_PER_REQUEST))
    if chunk_size < 1 or chunk_size > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"stations_per_request 必须介于 1 和 {CHECKWX_MAX_ICAOS_PER_REQUEST}")
    history_limit = int(config.get("checkwx_previous_limit", 50))
    if history_limit < 2 or history_limit > 50:
        raise ValueError("checkwx_previous_limit 必须介于 2 和 50")
    api_key_env = str(config.get("checkwx_api_key_env", CHECKWX_API_KEY_ENV_DEFAULT)).strip()
    if not api_key_env or not api_key_env.replace("_", "").isalnum() or api_key_env[0].isdigit():
        raise ValueError("checkwx_api_key_env 必须是有效的环境变量名")
    max_age = int(config.get("max_report_age_seconds", 900))
    if max_age < 60:
        raise ValueError("max_report_age_seconds 不得小于 60")
    failure_pause = int(config.get("failure_pause_after_seconds", 1800))
    if failure_pause < 60:
        raise ValueError("failure_pause_after_seconds 不得小于 60")
    warmup_retry = int(config.get("warmup_retry_seconds", 60))
    if warmup_retry < 60:
        raise ValueError("warmup_retry_seconds 不得小于 60")
    warmup_chunk_size = int(config.get("warmup_stations_per_request", CHECKWX_MAX_ICAOS_PER_REQUEST))
    if warmup_chunk_size < 1 or warmup_chunk_size > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"warmup_stations_per_request 必须介于 1 和 {CHECKWX_MAX_ICAOS_PER_REQUEST}")
    warmup_source = str(config.get("warmup_source", "awc")).lower().strip()
    if warmup_source not in {"auto", "checkwx", "awc"}:
        raise ValueError("warmup_source 只能为 auto、checkwx 或 awc")
    awc_warmup_hours = float(config.get("awc_warmup_history_hours", 30))
    if not 1 <= awc_warmup_hours <= 48:
        raise ValueError("awc_warmup_history_hours 必须介于 1 和 48")
    awc_fallback_hours = float(config.get("awc_fallback_history_hours", 2))
    if not 0.25 <= awc_fallback_hours <= 24:
        raise ValueError("awc_fallback_history_hours 必须介于 0.25 和 24")
    awc_chunk_size = int(config.get("awc_stations_per_request", 1))
    if awc_chunk_size < 1 or awc_chunk_size > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"awc_stations_per_request 必须介于 1 和 {CHECKWX_MAX_ICAOS_PER_REQUEST}")
    market_rules_max_age = int(config.get("market_rules_max_age_seconds", 1800))
    if market_rules_max_age < 600:
        raise ValueError("market_rules_max_age_seconds 不得小于 600")
    mode = str(config.get("mode", "paper")).lower().strip()
    if mode not in {"observe", "paper", "live"}:
        raise ValueError("mode 只能为 observe、paper 或 live")
    execution_engine = str(config.get("execution_engine", "legacy")).lower().strip()
    if execution_engine not in {"legacy", "tree2", "tree3"}:
        raise ValueError("execution_engine 只能为 legacy、tree2 或 tree3")
    order_type = str(config.get("execution_order_type", "FAK")).upper().strip()
    if order_type not in {"FAK", "FOK"}:
        raise ValueError("execution_order_type 只能为 FAK 或 FOK")
    target_order_shares = str(config.get("target_order_shares", "5"))
    if target_order_shares != "5":
        raise ValueError("当前 tree3 只允许固定 5 shares")
    min_execution_price = float(config.get("min_execution_price", 0.05))
    max_execution_price = float(config.get("max_execution_price", 0.98))
    max_slippage = float(config.get("max_slippage", 0.10))
    if not 0 <= min_execution_price <= max_execution_price <= 1:
        raise ValueError("执行价格门必须满足 0 <= min <= max <= 1")
    if max_slippage < 0:
        raise ValueError("max_slippage 不得为负")
    local_book_max_age = float(config.get("local_book_max_age_seconds", 3))
    if local_book_max_age <= 0:
        raise ValueError("local_book_max_age_seconds 必须大于 0")
    return {
        "scan_interval_seconds": interval,
        "rate_limit_backoff_seconds": rate_limit_backoff,
        "stations_per_request": chunk_size,
        "checkwx_previous_limit": history_limit,
        "checkwx_api_key_env": api_key_env,
        "max_report_age_seconds": max_age,
        "failure_pause_after_seconds": failure_pause,
        "warmup_retry_seconds": warmup_retry,
        "warmup_stations_per_request": warmup_chunk_size,
        "warmup_source": warmup_source,
        "awc_warmup_history_hours": awc_warmup_hours,
        "awc_fallback_history_hours": awc_fallback_hours,
        "awc_stations_per_request": awc_chunk_size,
        "market_rules_max_age_seconds": market_rules_max_age,
        "mode": mode,
        "execution_engine": execution_engine,
        "execution_order_type": order_type,
        "target_order_shares": target_order_shares,
        "min_execution_price": min_execution_price,
        "max_execution_price": max_execution_price,
        "max_slippage": max_slippage,
        "local_book_max_age_seconds": local_book_max_age,
        "market_ws_enabled": bool(config.get("market_ws_enabled", False)),
        "state_path": BASE_DIR / str(config.get("state_path", "data/state.json")),
        "event_dir": BASE_DIR / str(config.get("event_dir", "data/observations")),
        "signal_dir": BASE_DIR / str(config.get("signal_dir", "data/signals")),
        "health_path": BASE_DIR / str(config.get("health_path", "data/health.json")),
        "audit_db_path": BASE_DIR / str(config.get("audit_db_path", "data/audit.sqlite3")),
        "contract_cities_path": BASE_DIR / str(config.get("contract_cities_path", "config/contract_cities.json")),
        "market_rules_path": BASE_DIR / str(config.get("market_rules_path", "data/market_rules.json")),
        "stations": normalize_stations(config.get("stations")) if config.get("stations") is not None else [],
    }


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _checkwx_api_key(api_key_env: str) -> str:
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(
            f"未设置 CheckWX API 密钥环境变量 {api_key_env}；"
            "请在启动进程的环境中设置该变量，切勿将密钥写入 config.json 或提交到仓库。"
        )
    return api_key


def fetch_checkwx_reports(
    station_ids: list[str],
    api_key_env: str,
    previous_limit: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch CheckWX v2 short METAR reports without exposing the API key in URLs."""
    if not station_ids or len(station_ids) > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"每次 CheckWX 请求必须包含 1 至 {CHECKWX_MAX_ICAOS_PER_REQUEST} 个 ICAO")
    icaos = ",".join(str(item).upper().strip() for item in station_ids)
    safe_icaos = urllib.parse.quote(icaos, safe=",")
    if previous_limit is None:
        path = f"/metar/{safe_icaos}/short"
    else:
        if previous_limit < 2 or previous_limit > 50:
            raise ValueError("CheckWX 历史请求的 previous_limit 必须介于 2 和 50")
        path = f"/metar/{safe_icaos}/previous/{previous_limit}/short"
    url = f"{CHECKWX_BASE_URL}{path}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "weatherbot-tree4/4.0 (+https://github.com/jssyxd/weatherbot)",
            "X-API-Key": _checkwx_api_key(api_key_env),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            detail = "CheckWX 拒绝了 API 密钥（HTTP 401）"
        elif exc.code == 403:
            detail = "CheckWX 拒绝访问该端点（HTTP 403）；当前密钥可能不具备历史 METAR 权限"
        elif exc.code == 429:
            retry_after: int | None = None
            raw_retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
            try:
                if raw_retry_after is not None:
                    retry_after = max(MIN_SCAN_INTERVAL_SECONDS, int(raw_retry_after))
            except (TypeError, ValueError):
                retry_after = None
            raise CheckWXRateLimitError(retry_after) from exc
        else:
            detail = f"CheckWX 请求失败（HTTP {exc.code}）"
        raise RuntimeError(detail) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CheckWX 网络请求失败: {exc.reason}") from exc
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("CheckWX 返回了不可解析的 JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("results"), int) or not isinstance(result.get("data"), list):
        raise RuntimeError("CheckWX 返回格式异常：预期为含 results 整数与 data 数组的对象")
    if result["results"] != len(result["data"]):
        raise RuntimeError("CheckWX 返回格式异常：results 与 data 长度不一致")
    if not all(isinstance(item, dict) for item in result["data"]):
        raise RuntimeError("CheckWX 短格式响应异常：data 必须是对象数组")
    return result["data"], url


def _canonical_awc_report(report: dict[str, Any], source_endpoint: str) -> dict[str, Any] | None:
    """Map the official AWC JSON contract to tree4's provider-neutral METAR shape."""
    icao = str(report.get("icaoId", "")).upper().strip()
    raw = str(report.get("rawOb", "")).strip()
    report_type = str(report.get("metarType", "")).upper().strip() or raw.split(maxsplit=1)[0].upper()
    observed = report.get("obsTime") or report.get("reportTime")
    if report_type not in SUPPORTED_TYPES or len(icao) != 4 or not icao.isalnum() or not raw or parse_time(observed) is None:
        return None
    if not raw.startswith(("METAR ", "SPECI ")):
        raw = f"{report_type} {raw}"
    return {
        "icao": icao,
        "raw_text": raw,
        "observed": observed,
        "temperature_c": report.get("temp"),
        "_source": "AviationWeather.gov Data API",
        "_source_kind": "awc_fallback",
        "_source_endpoint": source_endpoint,
    }


def fetch_awc_reports(station_ids: list[str], hours: float, latest_only: bool = False) -> tuple[list[dict[str, Any]], str]:
    """Fetch official published AWC METAR/SPECI reports for warm-up or missing-ICAO fallback only."""
    if not station_ids or len(station_ids) > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"每次 AWC 请求必须包含 1 至 {CHECKWX_MAX_ICAOS_PER_REQUEST} 个 ICAO")
    query = urllib.parse.urlencode({"ids": ",".join(str(item).upper().strip() for item in station_ids), "format": "json", "hours": str(hours)}, safe=",")
    url = f"{AWC_METAR_URL}?{query}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "weatherbot-tree4/4.3 (+https://github.com/jssyxd/weatherbot)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if getattr(response, "status", 200) == 204:
                return [], url
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            return [], url
        raise RuntimeError(f"AWC 请求失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"AWC 网络请求失败: {exc.reason}") from exc
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWC 返回了不可解析的 JSON") from exc
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise RuntimeError("AWC 返回格式异常：预期为报告对象数组")
    if len(result) >= AWC_MAX_RESULTS_PER_QUERY:
        raise RuntimeError(
            f"AWC 查询返回 {len(result)} 条，已触及单查询 {AWC_MAX_RESULTS_PER_QUERY} 条上限；"
            "为防止历史暖机静默截断，需缩小 AWC 分块或回看窗口"
        )
    canonical = [record for item in result if (record := _canonical_awc_report(item, url)) is not None]
    if not latest_only:
        return canonical, url
    latest_by_icao: dict[str, dict[str, Any]] = {}
    for record in canonical:
        icao = str(record["icao"])
        existing = latest_by_icao.get(icao)
        if existing is None or parse_time(record["observed"]) > parse_time(existing["observed"]):
            latest_by_icao[icao] = record
    return list(latest_by_icao.values()), url


def _tag_checkwx_reports(reports: list[dict[str, Any]], source_endpoint: str) -> list[dict[str, Any]]:
    return [
        {**report, "_source": "CheckWX Aviation Weather API v2", "_source_kind": "checkwx", "_source_endpoint": source_endpoint}
        for report in reports
    ]


def report_key(report: dict[str, Any]) -> str:
    icao = str(report.get("icao", "")).upper()
    raw = str(report.get("raw_text", "")).strip()
    report_type = raw.split(maxsplit=1)[0].upper() if raw else "UNKNOWN"
    observed = as_utc_string(report.get("observed")) or "unknown-time"
    return "|".join((icao, report_type, observed, raw))


def normalize_report(report: dict[str, Any], station_names: dict[str, str], source_endpoint: str, fetched_at: str) -> dict[str, Any] | None:
    icao = str(report.get("icao", "")).upper().strip()
    raw = str(report.get("raw_text", "")).strip()
    report_type = raw.split(maxsplit=1)[0].upper() if raw else ""
    report_time = parse_time(report.get("observed"))
    if report_type not in SUPPORTED_TYPES or len(icao) != 4 or not icao.isalnum() or not raw or report_time is None:
        return None
    fetched_time = parse_time(fetched_at)
    report_age_seconds = round((fetched_time - report_time).total_seconds(), 3) if fetched_time else None
    source_kind = str(report.get("_source_kind", "checkwx"))
    source = str(report.get("_source", "CheckWX Aviation Weather API v2"))
    source_endpoint = str(report.get("_source_endpoint", source_endpoint))
    age_status = "available" if report_age_seconds is not None and report_age_seconds >= 0 else "source_time_inconsistent"
    event = {
        "event_id": report_key(report),
        "source": source,
        "source_kind": source_kind,
        "source_endpoint": source_endpoint,
        "fetched_at_utc": fetched_at,
        "airport_icao": icao,
        "airport_name": station_names.get(icao, icao),
        "report_type": report_type,
        "report_time_utc": as_utc_string(report.get("observed")),
        "report_age_seconds": report_age_seconds,
        "report_age_status": age_status,
        "temperature_c": report.get("temperature_c"),
        "is_correction": " COR " in f" {raw} ",
        "raw_metar": raw,
    }
    if source_kind == "checkwx":
        event["checkwx_report_age_seconds"] = report_age_seconds
        event["checkwx_report_age_status"] = age_status
    else:
        event["awc_report_age_seconds"] = report_age_seconds
        event["awc_report_age_status"] = age_status
    return event


def load_state(state_path: Path) -> dict[str, Any]:
    state = load_json(state_path, {"seen": {}, "last_successful_scan_utc": None})
    if not isinstance(state, dict) or not isinstance(state.get("seen", {}), dict):
        raise RuntimeError("状态文件结构无效；请先备份并删除该文件后重试")
    for key, default in (
        ("seen", {}), ("last_successful_scan_utc", None), ("edge_configs", {}),
        ("edge_failures", {}), ("daily_extrema", {}), ("daily_warmup", {}), ("handled_candidate_buckets", {}),
        ("market_rules", []), ("market_rules_refreshed_at_utc", None), ("market_failures", {}), ("consecutive_failure_started_utc", None),
        ("paper_city_day_notional", {}), ("paper_city_day_total_debit", {}), ("execution_paused", False),
        ("last_data_source_summary", {}),
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
    """Rebuild only current local days from CheckWX records; never emit signals."""
    values: dict[str, list[tuple[float, str]]] = {}
    for report in reports:
        icao = str(report.get("icao", "")).upper()
        city = cities.get(icao)
        raw = str(report.get("raw_text", "")).strip()
        report_type = raw.split(maxsplit=1)[0].upper() if raw else ""
        report_time = parse_time(report.get("observed"))
        if report_type not in SUPPORTED_TYPES or city is None or not raw or report_time is None:
            continue
        local_date = local_market_date(report_time, city)
        if local_date != target_dates[icao]:
            continue
        temperature = observed_temperature_native({"raw_metar": raw, "temperature_c": None}, city)
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
    """Rebuild current IANA-local-day extrema from published METAR/SPECI, failing closed on any unavailable due station."""
    target_dates = {icao: local_market_date(utc_now(), city) for icao, city in cities.items()}
    due = _warmup_due_cities(state, cities, target_dates, config["warmup_retry_seconds"])
    if not due:
        return {"status": "already_complete", "city_count": len(cities)}
    fetched_at = iso_now()
    station_ids = [city["icao"] for city in due]
    all_reports: list[dict[str, Any]] = []
    endpoints: list[str] = []
    source_used: list[str] = []
    checkwx_error: str | None = None
    checkwx_returned: set[str] = set()

    warmup_source = str(config.get("warmup_source", "awc"))
    if warmup_source in {"auto", "checkwx"}:
        try:
            for station_chunk in chunks(station_ids, config["warmup_stations_per_request"]):
                reports, endpoint = fetch_checkwx_reports(
                    station_chunk, config["checkwx_api_key_env"], previous_limit=config["checkwx_previous_limit"],
                )
                tagged = _tag_checkwx_reports(reports, endpoint)
                all_reports.extend(tagged)
                checkwx_returned.update(str(item.get("icao", "")).upper() for item in tagged)
                endpoints.append(endpoint)
            source_used.append("checkwx_previous")
        except Exception as exc:
            checkwx_error = f"{type(exc).__name__}: {exc}"
            if warmup_source == "checkwx":
                warmups: dict[str, Any] = state.setdefault("daily_warmup", {})
                for city in due:
                    local_date = target_dates[city["icao"]]
                    warmups[_warmup_state_key(city, local_date)] = {
                        "status": "failed_fetch", "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                        "last_attempt_utc": fetched_at, "error": checkwx_error, "warmup_source": "checkwx_previous",
                        "history_replay_emits_signals": False,
                    }
                return {"status": "failed_fetch", "due_city_count": len(due), "error": checkwx_error, "warmup_source": "checkwx_previous"}

    awc_station_ids = station_ids if warmup_source == "awc" else [icao for icao in station_ids if icao not in checkwx_returned]
    awc_error: str | None = None
    if awc_station_ids:
        try:
            for station_chunk in chunks(awc_station_ids, int(config.get("awc_stations_per_request", 1))):
                reports, endpoint = fetch_awc_reports(station_chunk, float(config.get("awc_warmup_history_hours", 30)))
                all_reports.extend(reports)
                endpoints.append(endpoint)
            source_used.append("awc")
        except Exception as exc:
            awc_error = f"{type(exc).__name__}: {exc}"

    if awc_error:
        warmups = state.setdefault("daily_warmup", {})
        for city in due:
            local_date = target_dates[city["icao"]]
            warmups[_warmup_state_key(city, local_date)] = {
                "status": "failed_fetch", "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "last_attempt_utc": fetched_at, "error": awc_error, "checkwx_history_error": checkwx_error,
                "warmup_source": "+".join(source_used or ["awc"]), "history_replay_emits_signals": False,
            }
        return {"status": "failed_fetch", "due_city_count": len(due), "error": awc_error, "checkwx_history_error": checkwx_error, "warmup_source": "+".join(source_used or ["awc"])}

    due_by_icao = {city["icao"]: city for city in due}
    summary = _rebuild_daily_extrema_from_history(state, due_by_icao, all_reports, fetched_at, ";".join(endpoints), target_dates)
    return {
        "status": "completed", "due_city_count": len(due), "reports_seen": len(all_reports),
        "warmup_source": "+".join(source_used), "checkwx_history_error": checkwx_error,
        **summary,
    }


def fetch_current_reports_with_fallback(config: dict[str, Any], station_ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use CheckWX as primary; fetch only missing ICAOs from AWC without promoting AWC to the normal path."""
    reports: list[dict[str, Any]] = []
    checkwx_errors: list[str] = []
    checkwx_endpoints: list[str] = []
    returned_icaos: set[str] = set()
    for station_chunk in chunks(station_ids, config["stations_per_request"]):
        try:
            current, endpoint = fetch_checkwx_reports(station_chunk, config["checkwx_api_key_env"])
        except CheckWXRateLimitError as exc:
            checkwx_errors.append(f"{','.join(station_chunk)}: {type(exc).__name__}: {exc}")
            continue
        except Exception as exc:
            checkwx_errors.append(f"{','.join(station_chunk)}: {type(exc).__name__}: {exc}")
            continue
        tagged = _tag_checkwx_reports(current, endpoint)
        reports.extend(tagged)
        checkwx_endpoints.append(endpoint)
        returned_icaos.update(str(item.get("icao", "")).upper() for item in tagged)

    missing_before_fallback = [icao for icao in station_ids if icao not in returned_icaos]
    awc_errors: list[str] = []
    awc_endpoints: list[str] = []
    awc_returned: set[str] = set()
    if missing_before_fallback:
        for station_chunk in chunks(missing_before_fallback, int(config.get("awc_stations_per_request", 1))):
            try:
                current, endpoint = fetch_awc_reports(station_chunk, float(config.get("awc_fallback_history_hours", 2)), latest_only=True)
            except Exception as exc:
                awc_errors.append(f"{','.join(station_chunk)}: {type(exc).__name__}: {exc}")
                continue
            reports.extend(current)
            awc_endpoints.append(endpoint)
            awc_returned.update(str(item.get("icao", "")).upper() for item in current)

    missing_after_fallback = [icao for icao in missing_before_fallback if icao not in awc_returned]
    summary = {
        "primary": "checkwx",
        "checkwx_report_count": len(returned_icaos),
        "checkwx_errors": checkwx_errors,
        "checkwx_endpoints": checkwx_endpoints,
        "awc_fallback_requested_icaos": missing_before_fallback,
        "awc_fallback_report_count": len(awc_returned),
        "awc_errors": awc_errors,
        "awc_endpoints": awc_endpoints,
        "missing_after_fallback": missing_after_fallback,
    }
    return reports, summary


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
    if mode in {"paper", "observe"}:
        if state.get("execution_engine") == "tree3":
            result["execution"] = {"mode": "paper", "status": "tree3_local_book_required", "decision_code": "LOCAL_BOOK_PATH_NOT_ATTACHED", "message": "tree3 已要求 WebSocket 本地盘口；主循环尚未自动连接真实行情时 fail-closed。"}
        else:
            result["execution"] = simulate_tree2(signal, state) if state.get("execution_engine") == "tree2" else simulate_paper_fak(signal, state)
    else:
        result["execution"] = {
            "mode": "live",
            "status": "blocked_no_live_executor",
            "message": "此版本不包含钱包、签名或订单提交器；live 模式只产生阻断审计记录。",
        }
    return result


def format_event(event: dict[str, Any]) -> str:
    report_age = event.get("report_age_seconds")
    source = str(event.get("source", "未知来源"))
    age_text = f" | 报文龄期 {report_age:.0f}s" if isinstance(report_age, (int, float)) else ""
    return f"[新{event['report_type']}] {event['airport_icao']} {event['report_time_utc']}{age_text} | 来源 {source}\n  {event['raw_metar']}"


def scan_once(config: dict[str, Any]) -> dict[str, Any]:
    fetched_at = iso_now()
    state = load_state(config["state_path"])
    state["execution_engine"] = config.get("execution_engine", "legacy")
    cities = load_contract_cities(config["contract_cities_path"])
    warmup_summary = warm_up_current_local_days(config, state, cities)
    # Process published observations with the last known good market rules first.
    market_rules = state.get("market_rules") or load_market_rules(config["market_rules_path"])
    station_names = {icao: city["name"] for icao, city in cities.items()}
    station_ids = list(station_names)
    all_reports, source_summary = fetch_current_reports_with_fallback(config, station_ids)
    state["last_data_source_summary"] = source_summary
    source_endpoints = source_summary["checkwx_endpoints"] + source_summary["awc_endpoints"]
    normalized = [
        record for report in all_reports
        if (record := normalize_report(report, station_names, ";".join(source_endpoints), fetched_at)) is not None
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
                "disclaimer": "No candidate is allowed until observed-time-to-IANA historical warm-up completes for this local market day.",
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
    audit_store = AuditStore(config["audit_db_path"])
    try:
        for signal in signals:
            execution = signal.get("execution") or {}
            audit_store.append(
                created_at_utc=iso_now(),
                event_type="signal_execution_decision",
                correlation_id=str(signal.get("event_id") or signal.get("bucket", {}).get("bucket_id") or ""),
                mode=config["mode"],
                token_id=str(signal.get("bucket", {}).get("no_token_id") or "") or None,
                payload={"signal": signal, "execution": execution},
            )
    finally:
        audit_store.close()
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
        "data_sources": source_summary,
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
    lock_path.parent.mkdir(parents=True, exist_ok=True)
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
    startup_state["execution_engine"] = config.get("execution_engine", "legacy")
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
        except CheckWXRateLimitError as exc:
            write_failure_health(config, exc)
            failure_started = failure_started or utc_now()
            backoff = max(config["rate_limit_backoff_seconds"], exc.retry_after_seconds or 0)
            print(f"[CheckWX 限流退避] {exc}；等待 {backoff} 秒后重试。", file=sys.stderr)
            time.sleep(backoff)
            continue
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
    source_summary = state.get("last_data_source_summary", {})
    if not isinstance(source_summary, dict):
        source_summary = {}
    source_degraded = bool(
        source_summary.get("checkwx_errors") or source_summary.get("awc_errors") or source_summary.get("missing_after_fallback")
    )
    healthy = (
        not last_error and last_scan_fresh and market_rules_fresh and not untrusted_warmups and not stale_rules
        and not state.get("market_failures", {}) and not source_degraded
    )
    return {
        "generated_at_utc": iso_now(), "status": "healthy" if healthy else "degraded",
        "critical_path": "deterministic_iana_state_machine_only", "llm_in_minute_path": False,
        "last_successful_scan_utc": state.get("last_successful_scan_utc"), "last_scan_fresh": last_scan_fresh,
        "market_rules_refreshed_at_utc": state.get("market_rules_refreshed_at_utc"), "market_rules_fresh": market_rules_fresh,
        "untrusted_warmup_count": len(untrusted_warmups), "untrusted_warmups": untrusted_warmups,
        "stale_rule_count": len(stale_rules), "stale_rules": stale_rules,
        "market_failure_count": len(state.get("market_failures", {})),
        "data_source_summary": source_summary,
        "data_source_degraded": source_degraded,
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
        "data_source_degraded": health["data_source_degraded"],
        "data_source_summary": health["data_source_summary"],
    }, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按配置周期扫描已发布 METAR/SPECI 的候选温度边缘工具")
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
