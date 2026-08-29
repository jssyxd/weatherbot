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
from decimal import Decimal
import uuid

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
from execution import OrderIntent, OrderRecord, OrderState, PaperExecutor, RiskGate
from clob_market_data import CLOBDataError, CLOBMarketData
from audit_store import AuditStore

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
CHECKWX_BASE_URL = "https://api.checkwx.com/v2"
CHECKWX_API_KEY_ENV_DEFAULT = "CHECKWX_API_KEY"
MIN_SCAN_INTERVAL_SECONDS = 60
CHECKWX_MAX_ICAOS_PER_REQUEST = 25
SUPPORTED_TYPES = {"METAR", "SPECI"}
# 暖机备用数据源(主数据源仍为 CheckWX 实时 short;暖机可回退到免费 NOAA 真实 METAR)
NOAA_METAR_API_URL = "https://aviationweather.gov/api/data/metar"
WARMUP_SOURCES = {"auto", "checkwx", "noaa"}
DEFAULT_WARMUP_MIN_OBS = 6
DEFAULT_WARMUP_HIGH_GATE_LOCAL_HOUR = 12
DEFAULT_WARMUP_LOW_GATE_LOCAL_HOUR = 6


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
    market_refresh_backoff = int(config.get("market_refresh_backoff_seconds", 1800))
    if market_refresh_backoff < 300:
        raise ValueError("market_refresh_backoff_seconds 不得小于 300")
    warmup_retry = int(config.get("warmup_retry_seconds", 60))
    if warmup_retry < 60:
        raise ValueError("warmup_retry_seconds 不得小于 60")
    warmup_source = str(config.get("warmup_source", "auto")).lower().strip()
    if warmup_source not in WARMUP_SOURCES:
        raise ValueError(f"warmup_source 只能为 {'、'.join(sorted(WARMUP_SOURCES))}")
    awc_warmup_hours = float(config.get("awc_warmup_history_hours", 30))
    if not 1 <= awc_warmup_hours <= 48:
        raise ValueError("awc_warmup_history_hours 必须介于 1 和 48")
    awc_fallback_hours = float(config.get("awc_fallback_history_hours", 2))
    if not 1 <= awc_fallback_hours <= 48:
        raise ValueError("awc_fallback_history_hours 必须介于 1 和 48")
    warmup_min_obs = int(config.get("warmup_min_obs", DEFAULT_WARMUP_MIN_OBS))
    if warmup_min_obs < 1:
        raise ValueError("warmup_min_obs 不得小于 1")
    warmup_high_gate = int(config.get("warmup_high_gate_local_hour", DEFAULT_WARMUP_HIGH_GATE_LOCAL_HOUR))
    warmup_low_gate = int(config.get("warmup_low_gate_local_hour", DEFAULT_WARMUP_LOW_GATE_LOCAL_HOUR))
    if not 0 <= warmup_high_gate <= 23 or not 0 <= warmup_low_gate <= 23:
        raise ValueError("warmup_*_gate_local_hour 必须介于 0 和 23")
    warmup_chunk_size = int(config.get("warmup_stations_per_request", CHECKWX_MAX_ICAOS_PER_REQUEST))
    if warmup_chunk_size < 1 or warmup_chunk_size > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"warmup_stations_per_request 必须介于 1 和 {CHECKWX_MAX_ICAOS_PER_REQUEST}")
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
    try:
        if float(target_order_shares) < 1:
            raise ValueError("target_order_shares 必须 >= 1")
    except (TypeError, ValueError) as e:
        raise ValueError("target_order_shares 必须是 >=1 的数字") from e
    min_execution_price = float(config.get("min_execution_price", 0.40))
    max_execution_price = float(config.get("max_execution_price", 0.99))
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
        "market_refresh_backoff_seconds": market_refresh_backoff,
        "warmup_retry_seconds": warmup_retry,
        "warmup_stations_per_request": warmup_chunk_size,
        "warmup_source": warmup_source,
        "awc_warmup_history_hours": awc_warmup_hours,
        "awc_fallback_history_hours": awc_fallback_hours,
        "warmup_min_obs": warmup_min_obs,
        "warmup_high_gate_local_hour": warmup_high_gate,
        "warmup_low_gate_local_hour": warmup_low_gate,
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


def report_key(report: dict[str, Any]) -> str:
    icao = str(report.get("icao", "")).upper()
    raw = str(report.get("raw_text", "")).strip()
    report_type = raw.split(maxsplit=1)[0].upper() if raw else "UNKNOWN"
    observed = as_utc_string(report.get("observed")) or "unknown-time"
    return "|".join((icao, report_type, observed, raw))


def fetch_noaa_warmup_reports(station_ids: list[str], hours: int = 48) -> tuple[list[dict[str, Any]], str]:
    """Fetch METAR history from the free NOAA aviationweather.gov API.

    Normalizes each record to the same shape the CheckWX short endpoint uses:
    ``{icao, raw_text, observed}``. NOAA ``rawOb`` sometimes lacks the leading
    ``METAR``/``SPECI`` token, so it is prepended deterministically when the
    first token is a 4-letter ICAO.
    """
    if not station_ids:
        return [], f"{NOAA_METAR_API_URL}?ids="
    icaos = ",".join(str(item).upper().strip() for item in station_ids)
    url = f"{NOAA_METAR_API_URL}?ids={urllib.parse.quote(icaos, safe=',')}&hours={hours}&format=json"
    request = urllib.request.Request(url, headers={"User-Agent": "weatherbot-tree4/4.0 (+https://github.com/jssyxd/weatherbot)"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"NOAA 请求失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"NOAA 网络请求失败: {exc.reason}") from exc
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("NOAA 返回了不可解析的 JSON") from exc
    if not isinstance(parsed, list):
        raise RuntimeError("NOAA 返回格式异常：预期为数组")
    normalized: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("rawOb") or "").strip()
        if not raw:
            continue
        first = raw.split(maxsplit=1)[0].upper()
        if first not in SUPPORTED_TYPES and len(first) == 4 and first.isalnum():
            raw = "METAR " + raw
        normalized.append({
            "icao": str(item.get("icaoId") or "").upper(),
            "raw_text": raw,
            "observed": str(item.get("reportTime") or item.get("obsTimeUtc") or item.get("obsTime") or ""),
        })
    return normalized, url


def fetch_warmup_history(config: dict[str, Any], cities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    """Fetch warm-up history following the configured source chain.

    ``warmup_source``:
      - ``checkwx``: CheckWX previous endpoint only (requires premium plan).
      - ``noaa``: free NOAA aviationweather.gov real METAR history.
      - ``auto`` (default): try CheckWX previous, fall back to NOAA. The first
        source that yields at least one report wins.

    Only deterministic, already-published observations are used; no modeled or
    reanalysis data is ever treated as settlement evidence. Returns
    ``(reports, source_endpoint, source_used)``. The live CheckWX ``/short``
    scan path is never touched.
    """
    source = config.get("warmup_source", "auto")
    station_ids = [city["icao"] for city in cities]
    chain = [source] if source != "auto" else ["checkwx", "noaa"]
    last_error: Exception | None = None
    for candidate in chain:
        try:
            reports: list[dict[str, Any]] = []
            endpoint = ""
            if candidate == "checkwx":
                endpoints: list[str] = []
                for station_chunk in chunks(station_ids, CHECKWX_MAX_ICAOS_PER_REQUEST):
                    chunk_reports, chunk_endpoint = fetch_checkwx_reports(
                        station_chunk, config["checkwx_api_key_env"], previous_limit=config["checkwx_previous_limit"]
                    )
                    reports.extend(chunk_reports)
                    endpoints.append(chunk_endpoint)
                endpoint = ";".join(endpoints)
            elif candidate == "noaa":
                endpoints = []
                for station_chunk in chunks(station_ids, CHECKWX_MAX_ICAOS_PER_REQUEST):
                    chunk_reports, chunk_endpoint = fetch_noaa_warmup_reports(station_chunk, hours=int(config.get("awc_warmup_history_hours", 30)))
                    reports.extend(chunk_reports)
                    endpoints.append(chunk_endpoint)
                endpoint = ";".join(endpoints)
            if reports:
                return reports, endpoint, candidate
        except Exception as exc:  # noqa: BLE001 - chain fallback by design
            last_error = exc
    if last_error is not None:
        raise last_error
    return [], "", "none"


def normalize_report(report: dict[str, Any], station_names: dict[str, str], source_endpoint: str, fetched_at: str) -> dict[str, Any] | None:
    icao = str(report.get("icao", "")).upper().strip()
    raw = str(report.get("raw_text", "")).strip()
    report_type = raw.split(maxsplit=1)[0].upper() if raw else ""
    report_time = parse_time(report.get("observed"))
    if report_type not in SUPPORTED_TYPES or len(icao) != 4 or not icao.isalnum() or not raw or report_time is None:
        return None
    fetched_time = parse_time(fetched_at)
    report_age_seconds = round((fetched_time - report_time).total_seconds(), 3) if fetched_time else None
    return {
        "event_id": report_key(report),
        "source": "CheckWX Aviation Weather API v2",
        "source_endpoint": source_endpoint,
        "fetched_at_utc": fetched_at,
        "airport_icao": icao,
        "airport_name": station_names.get(icao, icao),
        "report_type": report_type,
        "report_time_utc": as_utc_string(report.get("observed")),
        "checkwx_report_age_seconds": report_age_seconds,
        "checkwx_report_age_status": "available" if report_age_seconds is not None and report_age_seconds >= 0 else "source_time_inconsistent",
        "temperature_c": None,
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


def _rebuild_daily_extrema_from_history(
    state: dict[str, Any], cities: dict[str, dict[str, Any]], reports: list[dict[str, Any]],
    fetched_at: str, source_endpoint: str, target_dates: dict[str, str],
    warmup_min_obs: int = DEFAULT_WARMUP_MIN_OBS, warmup_source: str = "checkwx",
) -> dict[str, int]:
    """Rebuild only current local days from warm-up history; never emit signals.

    Reports are real published METAR/SPECI observations (CheckWX previous or
    NOAA aviationweather.gov). A day is only marked complete when it has at
    least ``warmup_min_obs`` observations, so a one-report "warm-up" can never
    silently unlock candidate signals.
    """
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
    insufficient_count = 0
    for city in cities.values():
        local_date = target_dates[city["icao"]]
        key = _warmup_state_key(city, local_date)
        series = values.get(key, [])
        if not series:
            extrema.pop(key, None)
            warmups[key] = {
                "status": "failed_no_current_local_day_reports", "city_id": city["city_id"], "icao": city["icao"],
                "market_local_date": local_date, "last_attempt_utc": fetched_at, "source_endpoint": source_endpoint,
                "warmup_source": warmup_source, "history_replay_emits_signals": False,
            }
            missing_count += 1
            continue
        if len(series) < warmup_min_obs:
            extrema.pop(key, None)
            warmups[key] = {
                "status": "incomplete_insufficient_obs", "city_id": city["city_id"], "icao": city["icao"],
                "market_local_date": local_date, "last_attempt_utc": fetched_at, "source_endpoint": source_endpoint,
                "warmup_source": warmup_source, "history_report_count": len(series),
                "warmup_min_obs": warmup_min_obs, "history_replay_emits_signals": False,
            }
            insufficient_count += 1
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
            "warmup_source": warmup_source, "history_report_count": len(series), "history_replay_emits_signals": False,
        }
        complete_count += 1
    return {
        "complete": complete_count, "missing_current_local_day_reports": missing_count,
        "insufficient_obs": insufficient_count,
    }


def warm_up_current_local_days(config: dict[str, Any], state: dict[str, Any], cities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Fail closed until a deterministic observed-time-to-IANA replay completes for every current local day."""
    target_dates = {icao: local_market_date(utc_now(), city) for icao, city in cities.items()}
    due = _warmup_due_cities(state, cities, target_dates, config["warmup_retry_seconds"])
    if not due:
        return {"status": "already_complete", "city_count": len(cities)}
    fetched_at = iso_now()
    due_by_icao = {city["icao"]: city for city in due}
    try:
        all_reports, source_endpoint, source_used = fetch_warmup_history(config, due)
    except Exception as exc:
        warmups: dict[str, Any] = state.setdefault("daily_warmup", {})
        for city in due:
            local_date = target_dates[city["icao"]]
            warmups[_warmup_state_key(city, local_date)] = {
                "status": "failed_fetch", "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "last_attempt_utc": fetched_at, "error": f"{type(exc).__name__}: {exc}", "history_replay_emits_signals": False,
                "warmup_source": config["warmup_source"],
            }
        return {"status": "failed_fetch", "due_city_count": len(due), "error": f"{type(exc).__name__}: {exc}"}
    summary = _rebuild_daily_extrema_from_history(
        state, due_by_icao, all_reports, fetched_at, source_endpoint, target_dates,
        warmup_min_obs=config["warmup_min_obs"], warmup_source=source_used,
    )
    return {
        "status": "completed", "due_city_count": len(due), "reports_seen": len(all_reports),
        "warmup_source": source_used, "checkwx_previous_limit": config["checkwx_previous_limit"], **summary,
    }


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
    # Back off after repeated refresh failures so a slow/unreachable Gamma path
    # cannot stall the scan loop for tens of minutes.
    failed_at = parse_time(state.get("last_market_refresh_failed_at"))
    if failed_at is not None and (utc_now() - failed_at).total_seconds() < config.get("market_refresh_backoff_seconds", 1800):
        return {"market_refresh_backoff": True, "since": state.get("last_market_refresh_failed_at")}
    local_dates = {icao: local_market_date(utc_now(), city) for icao, city in cities.items()}
    try:
        rules, failures = refresh_market_rules(cities, local_dates)
        # Partial success is accepted: as long as at least half of the expected
        # rules (~1 per city) parsed, refresh the cache instead of falling into
        # a 30-minute backoff, so a handful of flaky Gamma slugs never blocks
        # the whole market-rule refresh for the rest of the scan day.
        expected_rule_count = max(1, len(cities) * 2)
        if len(rules) >= max(1, len(cities)):
            state["market_rules"] = rules
            state["market_failures"] = failures
            state["market_rules_refreshed_at_utc"] = iso_now()
            state["last_market_refresh_failed_at"] = None
            state["last_market_refresh_error"] = None
            # 注意：summary 键不能与 state 数据键重名（scan_once 会 state.update(summary)），
            # 否则会把 state["market_rules"] 覆盖成数量而不是规则列表。
            return {"refreshed_rule_count": len(rules), "refreshed_failure_count": len(failures)}
        state["last_market_refresh_error"] = (
            f"partial_refresh_insufficient: rules={len(rules)} < {max(1, len(cities))} (期望 {expected_rule_count} 的一半)"
        )
        state["last_market_refresh_failed_at"] = iso_now()
        return {"market_refresh_error": state["last_market_refresh_error"], "refreshed_rule_count": len(rules)}
    except Exception as exc:
        state["last_market_refresh_error"] = f"{type(exc).__name__}: {exc}"
        state["last_market_refresh_failed_at"] = iso_now()
        return {"market_refresh_error": state["last_market_refresh_error"]}


# --- unified execution layer (paper FAK against fresh L2 book; live fail-closed) ---
TARGET_ORDER_SHARES = Decimal("1")   # paper order size (user decision: keep 1 share)
MAX_EXECUTION_PRICE = Decimal("0.98")
MIN_EXECUTION_PRICE = Decimal("0.40")
MAX_SLIPPAGE = Decimal("0.10")
MAX_BOOK_AGE_SECONDS = 3.0
CITY_DAY_MAX_TOTAL_DEBIT = Decimal("20.00")  # per city-local-day paper spend cap

_market_data = CLOBMarketData()
_risk_gate = RiskGate(
    min_price=MIN_EXECUTION_PRICE,
    max_price=MAX_EXECUTION_PRICE,
    max_slippage=MAX_SLIPPAGE,
    max_book_age_seconds=MAX_BOOK_AGE_SECONDS,
    min_order_size=Decimal("1"),
    max_order_size=Decimal("100"),
)
_paper_executor = PaperExecutor(min_price=MIN_EXECUTION_PRICE)


def enrich_execution(signal: dict[str, Any], mode: str, state: dict[str, Any]) -> dict[str, Any]:
    """Attach a read-only paper-fill estimate or a live safety block; never submit an order."""
    if signal.get("signal_type") != "candidate_no_signal":
        return signal
    result = dict(signal)
    if mode not in {"paper", "observe"}:
        result["execution"] = {
            "mode": "live",
            "status": "blocked_no_live_executor",
            "decision_code": "LIVE_EXECUTOR_DISABLED",
            "message": "此版本不包含钱包、签名或订单提交器；live 模式只产生阻断审计记录。",
        }
        return result
    result["execution"] = _paper_execution(signal, mode, state)
    return result


def _paper_execution(signal: dict[str, Any], mode: str, state: dict[str, Any]) -> dict[str, Any]:
    no_token_id = str(signal.get("bucket", {}).get("no_token_id") or "")
    if not no_token_id:
        return {"mode": "paper", "status": "paper_fill_rejected_missing_no_token", "decision_code": "MISSING_NO_TOKEN"}
    city_day_key = f"{signal.get('city_id')}|{signal.get('market_local_date')}"
    ledger = state.setdefault("paper_city_day_total_debit", {})
    already_spent = Decimal(str(ledger.get(city_day_key, 0.0)))
    remaining_budget = CITY_DAY_MAX_TOTAL_DEBIT - already_spent
    if remaining_budget <= 0:
        return {"mode": "paper", "status": "paper_fill_rejected_city_day_cap", "decision_code": "INSUFFICIENT_BALANCE",
                "message": "该城市当地日含费用总现金余量已用尽。"}
    intent = OrderIntent(
        order_id=f"paper-{uuid.uuid4().hex[:12]}",
        token_id=no_token_id,
        side="BUY_NO",
        price=MAX_EXECUTION_PRICE,
        quantity=TARGET_ORDER_SHARES,
        order_type="FAK",
        strategy=str(signal.get("signal_type", "edge_engine")),
        signal_reason=str(signal.get("reason") or "dead_bucket_no_signal"),
        created_at=iso_now(),
    )
    try:
        book = _market_data.fetch_books([no_token_id]).get(no_token_id)
        fee_raw = _market_data.fetch_fee_rate(no_token_id)
        fee_rate = Decimal(str(fee_raw.get("base_fee"))) / Decimal("10000")
    except CLOBDataError as exc:
        return {"mode": "paper", "status": "paper_fill_unavailable", "decision_code": "CLOB_ERROR", "message": str(exc)}
    decision = _risk_gate.evaluate(intent, book, mode=mode, available_balance_usdc=remaining_budget)
    if not decision.allowed:
        record = OrderRecord(
            intent=intent, state=OrderState.RISK_REJECTED, reject_reason=decision.code,
            book_timestamp=decision.book_timestamp, book_age_seconds=decision.book_age_seconds, book_hash=decision.book_hash,
        )
        return {"mode": "paper", "status": "paper_risk_rejected", "decision_code": decision.code, "order": record.as_dict()}
    record = _paper_executor.execute(intent, book, fee_rate)
    if record.filled_shares > 0 and record.average_fill_price is not None:
        debit = record.filled_shares * record.average_fill_price
        ledger[city_day_key] = float(already_spent + debit)
    return {"mode": "paper", "status": f"paper_{record.state.value.lower()}", "decision_code": record.state.value, "order": record.as_dict()}


def format_event(event: dict[str, Any]) -> str:
    report_age = event.get("checkwx_report_age_seconds")
    age_text = f" | CheckWX报文龄期 {report_age:.0f}s" if isinstance(report_age, (int, float)) else ""
    return f"[新{event['report_type']}] {event['airport_icao']} {event['report_time_utc']}{age_text}\n  {event['raw_metar']}"


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
    all_reports: list[dict[str, Any]] = []
    endpoints: list[str] = []
    # CheckWX is the primary live source, but its free-tier daily quota (and
    # occasional blocking) yields HTTP 429 with retry_after up to the next UTC
    # midnight; sleeping through that would blind the observer for hours. On
    # CheckWXRateLimitError we fall back to the free NOAA aviationweather.gov
    # endpoint (same normalized {icao, raw_text, observed} shape) for this scan
    # and persist a cooldown so CheckWX is skipped until retry_after expires
    # instead of being hammered with repeated 429s.
    cooldown_until = parse_time(state.get("checkwx_cooldown_until_utc"))
    checkwx_blocked = cooldown_until is not None and utc_now() < cooldown_until
    for station_chunk in chunks(station_ids, config["stations_per_request"]):
        if not checkwx_blocked:
            try:
                reports, endpoint = fetch_checkwx_reports(station_chunk, config["checkwx_api_key_env"])
                all_reports.extend(reports)
                endpoints.append(endpoint)
                continue
            except CheckWXRateLimitError as exc:
                cooldown_until = utc_now() + timedelta(seconds=max(config["rate_limit_backoff_seconds"], exc.retry_after_seconds or 0))
                state["checkwx_cooldown_until_utc"] = as_utc_string(cooldown_until)
                atomic_json_write(config["state_path"], state)
                checkwx_blocked = True
                print(f"[CheckWX 429 回退 NOAA] {exc}；冷却至 {state['checkwx_cooldown_until_utc']}，本次改用 NOAA 实时源。", file=sys.stderr)
        reports, endpoint = fetch_noaa_warmup_reports(station_chunk, hours=2)
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
            warmup_high_gate_local_hour=config["warmup_high_gate_local_hour"],
            warmup_low_gate_local_hour=config["warmup_low_gate_local_hour"],
        ):
            signals.append(enrich_execution(signal, config["mode"], state))
    state["last_successful_scan_utc"] = iso_now()
    prune_state(state)
    atomic_json_write(config["state_path"], state)
    # Refresh market rules only after this round's observations are safely persisted,
    # so a slow/failing Gamma refresh can never drop time-sensitive observation data.
    refresh_summary = refresh_market_rules_if_due(state, config, cities)
    if refresh_summary is not None:
        state.update(refresh_summary)
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
            # enforce risk_ledger: only a paper fill (filled_size > 0) changes
            # position and debits the city-day ledger (closes audit B-M3).
            order = execution.get("order") or {}
            try:
                filled = Decimal(str(order.get("filled_size") or "0"))
            except Exception:
                filled = Decimal("0")
            if filled > 0:
                avg = Decimal(str(order.get("average_fill_price") or "0"))
                token_id = str(signal.get("bucket", {}).get("no_token_id") or "")
                debit_key = f"debit|{signal.get('city_id')}|{signal.get('market_local_date')}"
                pos_key = f"position|{token_id}|BUY_NO"
                audit_store.set_ledger(debit_key, str(Decimal(audit_store.get_ledger(debit_key)) + filled * avg), iso_now())
                audit_store.set_ledger(pos_key, str(Decimal(audit_store.get_ledger(pos_key)) + filled), iso_now())

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
            backoff = min(max(config["rate_limit_backoff_seconds"], exc.retry_after_seconds or 0), 600)
            print(f"[CheckWX 限流退避] {exc}；等待 {backoff} 秒后重试（正常路径已回退 NOAA，此路径仅当 NOAA 亦失败时出现）。", file=sys.stderr)
            time.sleep(backoff)
            continue
        except Exception as exc:
            write_failure_health(config, exc)
            failure_started = failure_started or utc_now()
            elapsed = (utc_now() - failure_started).total_seconds()
            if elapsed >= config["failure_pause_after_seconds"]:
                print(f"[扫描暂停：连续失败达到阈值] {type(exc).__name__}: {exc}；暂停 300 秒后恢复尝试", file=sys.stderr)
                time.sleep(300)
                continue
            print(f"[扫描失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        sleep_to_next_interval(interval)


def build_health_snapshot(config: dict[str, Any], state: dict[str, Any], cities: dict[str, dict[str, Any]], last_error: str | None = None) -> dict[str, Any]:
    now = utc_now()
    target_dates = {icao: local_market_date(now, city) for icao, city in cities.items()}
    untrusted_warmups = []
    for city in cities.values():
        if _warmup_is_complete(state, city, target_dates[city["icao"]]):
            continue
        entry = state.get("daily_warmup", {}).get(_warmup_state_key(city, target_dates[city["icao"]]), {})
        untrusted_warmups.append({
            "icao": city["icao"], "market_local_date": target_dates[city["icao"]],
            "status": entry.get("status", "missing"),
            "error": entry.get("error"),
            "last_attempt_utc": entry.get("last_attempt_utc"),
        })
    warmup_error_summary: dict[str, int] = {}
    for entry in untrusted_warmups:
        err = entry.get("error") or entry.get("status") or "unknown"
        key = str(err).split(":")[0][:80]
        warmup_error_summary[key] = warmup_error_summary.get(key, 0) + 1
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
        "warmup_error_summary": warmup_error_summary,
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
