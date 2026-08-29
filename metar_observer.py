#!/usr/bin/env python3
"""CheckWX v2 observer and tail-end consensus YES paper strategy (tree6yes).

The program pulls published CheckWX METAR/SPECI reports every 15 minutes and
uses public Polymarket market data only to form paper-only YES-side intents.
It never uses a forecast model, loads a wallet, signs an order, or submits,
cancels, or amends a real trade. ``mode=live`` remains explicitly blocked.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from edge_engine import append_jsonl, atomic_json_write, load_contract_cities, load_market_rules, local_market_date, observed_temperature_native
from market_adapter import refresh_market_rules
from audit_store import AuditStore
from local_order_book import LocalBookSnapshot
from tail_consensus_strategy import TailConsensusConfig, evaluate_tail_entries, mark_temperature_breaks, monitor_tail_positions

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
CHECKWX_BASE_URL = "https://api.checkwx.com/v2"
CHECKWX_API_KEY_ENV_DEFAULT = "CHECKWX_API_KEY"
AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
AWC_WARMUP_HOURS_DEFAULT = 48
AWC_FALLBACK_HOURS_DEFAULT = 2
AWC_REQUEST_TIMEOUT_SECONDS = 5
AWC_MAX_CONCURRENT_REQUESTS = 4
AWC_FALLBACK_MAX_ICAOS_PER_REQUEST = 25
MIN_SCAN_INTERVAL_SECONDS = 120
CHECKWX_MAX_ICAOS_PER_REQUEST = 25
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
    awc_warmup_hours = int(config.get("awc_warmup_hours", AWC_WARMUP_HOURS_DEFAULT))
    if awc_warmup_hours < 24 or awc_warmup_hours > 72:
        raise ValueError("awc_warmup_hours 必须介于 24 和 72")
    awc_fallback_hours = int(config.get("awc_fallback_hours", AWC_FALLBACK_HOURS_DEFAULT))
    if awc_fallback_hours < 1 or awc_fallback_hours > 24:
        raise ValueError("awc_fallback_hours 必须介于 1 和 24")
    warmup_max_stations_per_cycle = int(config.get("warmup_max_stations_per_cycle", 8))
    if warmup_max_stations_per_cycle < 1 or warmup_max_stations_per_cycle > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"warmup_max_stations_per_cycle 必须介于 1 和 {CHECKWX_MAX_ICAOS_PER_REQUEST}")
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
    market_rules_max_age = int(config.get("market_rules_max_age_seconds", 1800))
    if market_rules_max_age < 600:
        raise ValueError("market_rules_max_age_seconds 不得小于 600")
    mode = str(config.get("mode", "paper")).lower().strip()
    if mode not in {"observe", "paper", "live"}:
        raise ValueError("mode 只能为 observe、paper 或 live")
    execution_engine = str(config.get("execution_engine", "tree6yes")).lower().strip()
    if execution_engine != "tree6yes":
        raise ValueError("tree6yes 仅支持 execution_engine=tree6yes；已移除所有 NO 侧执行路径")
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
    tail_consensus = TailConsensusConfig.from_mapping({**config, "local_book_max_age_seconds": local_book_max_age})
    return {
        "scan_interval_seconds": interval,
        "rate_limit_backoff_seconds": rate_limit_backoff,
        "stations_per_request": chunk_size,
        "checkwx_api_key_env": api_key_env,
        "awc_warmup_hours": awc_warmup_hours,
        "awc_fallback_hours": awc_fallback_hours,
        "warmup_max_stations_per_cycle": warmup_max_stations_per_cycle,
        "max_report_age_seconds": max_age,
        "failure_pause_after_seconds": failure_pause,
        "warmup_retry_seconds": warmup_retry,
        "market_rules_max_age_seconds": market_rules_max_age,
        "mode": mode,
        "execution_engine": execution_engine,
        "execution_order_type": order_type,
        "target_order_shares": target_order_shares,
        "min_execution_price": min_execution_price,
        "max_execution_price": max_execution_price,
        "max_slippage": max_slippage,
        "local_book_max_age_seconds": local_book_max_age,
        "market_ws_enabled": bool(config.get("market_ws_enabled", True)),
        "tail_consensus": tail_consensus,
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


def fetch_checkwx_reports(station_ids: list[str], api_key_env: str) -> tuple[list[dict[str, Any]], str]:
    """Fetch only CheckWX v2 current short METAR reports without exposing the API key in URLs."""
    if not station_ids or len(station_ids) > CHECKWX_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"每次 CheckWX 请求必须包含 1 至 {CHECKWX_MAX_ICAOS_PER_REQUEST} 个 ICAO")
    icaos = ",".join(str(item).upper().strip() for item in station_ids)
    safe_icaos = urllib.parse.quote(icaos, safe=",")
    url = f"{CHECKWX_BASE_URL}/metar/{safe_icaos}/short"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "weatherbot-tree6yes/6.0 (+https://github.com/jssyxd/weatherbot)",
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
                    retry_after = max(0, int(raw_retry_after))
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


def _normalize_awc_observation(item: Any) -> dict[str, Any] | None:
    """Map AWC's public JSON response to the existing raw-observation contract."""
    if not isinstance(item, dict):
        return None
    icao = str(item.get("icaoId") or item.get("icao") or "").upper().strip()
    report_type = str(item.get("metarType") or "METAR").upper().strip()
    raw = str(item.get("rawOb") or item.get("raw_text") or "").strip()
    observed = as_utc_string(item.get("reportTime") or item.get("obsTime"))
    if report_type not in SUPPORTED_TYPES or len(icao) != 4 or not icao.isalnum() or not raw or observed is None:
        return None
    if not raw.startswith(("METAR ", "SPECI ")):
        raw = f"{report_type} {raw}"
    return {"icao": icao, "raw_text": raw, "observed": observed}


def fetch_awc_reports(station_ids: str | list[str], *, hours: int, purpose: str) -> tuple[list[dict[str, Any]], str]:
    """Fetch published AWC METAR/SPECI without forecasts or model data.

    Warm-up calls pass one ICAO to avoid the public endpoint's per-query result
    limit.  Short two-hour disaster-recovery calls may batch up to 25 ICAOs.
    AWC HTTP 204 represents a valid no-data response.
    """
    raw_ids = [station_ids] if isinstance(station_ids, str) else station_ids
    icaos = [str(station_id).upper().strip() for station_id in raw_ids]
    if not icaos or len(icaos) > AWC_FALLBACK_MAX_ICAOS_PER_REQUEST:
        raise ValueError(f"每次 AWC 请求必须包含 1 至 {AWC_FALLBACK_MAX_ICAOS_PER_REQUEST} 个 ICAO")
    if any(len(icao) != 4 or not icao.isalnum() for icao in icaos):
        raise ValueError(f"无效 ICAO 代码: {station_ids!r}")
    if not 1 <= int(hours) <= 72:
        raise ValueError("AWC hours 必须介于 1 和 72")
    params = urllib.parse.urlencode({"ids": ",".join(icaos), "format": "json", "hours": str(int(hours))})
    url = f"{AWC_METAR_URL}?{params}"
    try:
        response = subprocess.run(
            [
                "curl", "--location", "--silent", "--show-error", "--connect-timeout", "3", "--max-time", str(AWC_REQUEST_TIMEOUT_SECONDS),
                "--header", "Accept: application/json", "--header", "User-Agent: weatherbot-tree6yes/6.2 (+https://github.com/jssyxd/weatherbot)",
                "--output", "-", "--write-out", "\n%{http_code}", url,
            ],
            capture_output=True, text=True, timeout=AWC_REQUEST_TIMEOUT_SECONDS + 2, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"AWC {purpose} 请求进程超时") from exc
    stdout = response.stdout or ""
    body, separator, status = stdout.rpartition("\n")
    if not separator:
        raise RuntimeError(f"AWC {purpose} 响应缺少 HTTP 状态")
    if response.returncode != 0:
        detail = (response.stderr or "").strip() or f"curl_exit_{response.returncode}"
        raise RuntimeError(f"AWC {purpose} 网络请求失败: {detail}")
    if status == "204":
        return [], url
    if status != "200":
        raise RuntimeError(f"AWC {purpose} 请求失败（HTTP {status}）")
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AWC {purpose} 返回了不可解析的 JSON") from exc
    if not isinstance(result, list):
        raise RuntimeError(f"AWC {purpose} 返回格式异常：预期为观测对象数组")
    return [record for item in result if (record := _normalize_awc_observation(item)) is not None], url


def report_key(report: dict[str, Any]) -> str:
    icao = str(report.get("icao", "")).upper()
    raw = str(report.get("raw_text", "")).strip()
    report_type = raw.split(maxsplit=1)[0].upper() if raw else "UNKNOWN"
    observed = as_utc_string(report.get("observed")) or "unknown-time"
    return "|".join((icao, report_type, observed, raw))


def normalize_report(
    report: dict[str, Any], station_names: dict[str, str], source_endpoint: str, fetched_at: str,
    source_name: str = "CheckWX Aviation Weather API v2",
) -> dict[str, Any] | None:
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
        "source": source_name,
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
        ("tail_consensus", {}), ("tail_positions", {}),
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
    state["tail_positions"] = {
        item_key: value for item_key, value in state.get("tail_positions", {}).items()
        if isinstance(value, dict) and str(value.get("market_local_date", "")) >= cutoff
    }
    state["tail_consensus"] = {
        item_key: value for item_key, value in state.get("tail_consensus", {}).items()
        if isinstance(value, dict) and str(value.get("last_seen_utc", "")) >= cutoff
    }


def _warmup_state_key(city: dict[str, Any], local_date: str) -> str:
    return f"{city['city_id']}|{local_date}"


def _warmup_is_complete(state: dict[str, Any], city: dict[str, Any], local_date: str) -> bool:
    entry = state.get("daily_warmup", {}).get(_warmup_state_key(city, local_date), {})
    return entry.get("status") == "complete" and entry.get("market_local_date") == local_date


def _warmup_due_cities(state: dict[str, Any], cities: dict[str, dict[str, Any]], target_dates: dict[str, str], retry_seconds: int) -> list[dict[str, Any]]:
    """Return due cities with never-attempted warm-ups before retrying failures."""
    now = utc_now()
    due: list[tuple[int, float, dict[str, Any]]] = []
    for city in cities.values():
        local_date = target_dates[city["icao"]]
        if _warmup_is_complete(state, city, local_date):
            continue
        entry = state.get("daily_warmup", {}).get(_warmup_state_key(city, local_date), {})
        attempted = parse_time(entry.get("last_attempt_utc"))
        if attempted is None:
            due.append((0, 0.0, city))
        elif (now - attempted).total_seconds() >= retry_seconds:
            due.append((1, attempted.timestamp(), city))
    due.sort(key=lambda item: (item[0], item[1], item[2]["icao"]))
    return [city for _priority, _attempted_at, city in due]


def _rebuild_daily_extrema_from_history(
    state: dict[str, Any], cities: dict[str, dict[str, Any]], reports: list[dict[str, Any]], fetched_at: str,
    source_endpoint: str | dict[str, str], target_dates: dict[str, str], *, source_name: str = "AviationWeather.gov Data API",
    source_errors: dict[str, str] | None = None,
) -> dict[str, int]:
    """Rebuild only current local days from published METAR/SPECI; never emit signals."""
    source_errors = source_errors or {}
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
        endpoint = source_endpoint.get(city["icao"], "") if isinstance(source_endpoint, dict) else source_endpoint
        if not series:
            extrema.pop(key, None)
            status = "failed_awc_fetch" if city["icao"] in source_errors else "failed_no_current_local_day_reports"
            warmups[key] = {
                "status": status, "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "last_attempt_utc": fetched_at, "source_endpoint": endpoint, "warmup_source": source_name,
                "error": source_errors.get(city["icao"]), "history_replay_emits_signals": False,
            }
            missing_count += 1
            continue
        extrema[key] = {
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "market_unit": city["market_unit"], "high": max(item[0] for item in series), "low": min(item[0] for item in series),
            "initialized_by_event_id": "iana_local_day_warmup", "updated_at_utc": fetched_at, "warmup_source": source_name,
            "warmup_report_count": len(series), "warmup_earliest_report_time_utc": min(item[1] for item in series),
            "warmup_latest_report_time_utc": max(item[1] for item in series),
        }
        warmups[key] = {
            "status": "complete", "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "last_attempt_utc": fetched_at, "completed_at_utc": fetched_at, "source_endpoint": endpoint,
            "warmup_source": source_name, "history_report_count": len(series), "history_replay_emits_signals": False,
        }
        complete_count += 1
    return {"complete": complete_count, "missing_current_local_day_reports": missing_count}


def fetch_awc_history_for_stations(station_ids: list[str], *, hours: int, purpose: str) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """Fetch AWC independently per ICAO with bounded concurrency and partial success."""
    reports: list[dict[str, Any]] = []
    endpoints: dict[str, str] = {}
    errors: dict[str, str] = {}
    normalized_ids = [str(station_id).upper().strip() for station_id in station_ids]
    if not normalized_ids:
        return reports, endpoints, errors
    def fetch_one(icao: str) -> tuple[str, list[dict[str, Any]], str]:
        records, endpoint = fetch_awc_reports(icao, hours=hours, purpose=purpose)
        return icao, records, endpoint
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(AWC_MAX_CONCURRENT_REQUESTS, len(normalized_ids))) as pool:
        futures = {pool.submit(fetch_one, icao): icao for icao in normalized_ids}
        for future in concurrent.futures.as_completed(futures):
            icao = futures[future]
            try:
                resolved_icao, records, endpoint = future.result()
                reports.extend(records)
                endpoints[resolved_icao] = endpoint
            except Exception as exc:
                errors[icao] = f"{type(exc).__name__}: {exc}"
    return reports, endpoints, errors


def _is_supported_published_observation(report: dict[str, Any]) -> bool:
    raw = str(report.get("raw_text") or "").strip()
    report_type = raw.split(maxsplit=1)[0].upper() if raw else ""
    return report_type in SUPPORTED_TYPES and parse_time(report.get("observed")) is not None


def fetch_current_reports_with_awc_fallback(config: dict[str, Any], station_ids: list[str]) -> tuple[list[tuple[dict[str, Any], str, str]], dict[str, Any]]:
    """Read current reports from CheckWX, then use AWC only for missing/failed ICAOs.

    The return records carry their individual source and endpoint so AWC fallback
    cannot be misrepresented as CheckWX data in downstream audit events.
    """
    target_ids = [str(value).upper().strip() for value in station_ids]
    batches: list[tuple[dict[str, Any], str, str]] = []
    returned_ids: set[str] = set()
    checkwx_errors: dict[str, str] = {}
    for station_chunk in chunks(target_ids, config["stations_per_request"]):
        try:
            reports, endpoint = fetch_checkwx_reports(station_chunk, config["checkwx_api_key_env"])
            for report in reports:
                icao = str(report.get("icao", "")).upper().strip()
                if icao in station_chunk and _is_supported_published_observation(report):
                    returned_ids.add(icao)
                    batches.append((report, "CheckWX Aviation Weather API v2", endpoint))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            for station_id in station_chunk:
                checkwx_errors[station_id] = error
    fallback_ids = [station_id for station_id in target_ids if station_id not in returned_ids]
    awc_reports: list[dict[str, Any]] = []
    awc_endpoints: dict[str, str] = {}
    awc_errors: dict[str, str] = {}
    for fallback_chunk in chunks(fallback_ids, AWC_FALLBACK_MAX_ICAOS_PER_REQUEST):
        try:
            records, endpoint = fetch_awc_reports(fallback_chunk, hours=config["awc_fallback_hours"], purpose="fallback")
            awc_reports.extend(records)
            for station_id in fallback_chunk:
                awc_endpoints[station_id] = endpoint
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            for station_id in fallback_chunk:
                awc_errors[station_id] = error
    for report in awc_reports:
        icao = str(report.get("icao", "")).upper().strip()
        endpoint = awc_endpoints.get(icao)
        if endpoint:
            batches.append((report, "AviationWeather.gov Data API (fallback)", endpoint))
    return batches, {
        "checkwx_returned_city_count": len(returned_ids), "awc_fallback_city_count": len(fallback_ids),
        "checkwx_errors": checkwx_errors, "awc_fallback_errors": awc_errors,
    }


def warm_up_current_local_days(config: dict[str, Any], state: dict[str, Any], cities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Fail closed until AWC published-observation replay completes for each local day."""
    target_dates = {icao: local_market_date(utc_now(), city) for icao, city in cities.items()}
    due = _warmup_due_cities(state, cities, target_dates, config["warmup_retry_seconds"])
    if not due:
        return {"status": "already_complete", "city_count": len(cities)}
    selected = due[:int(config.get("warmup_max_stations_per_cycle", 8))]
    fetched_at = iso_now()
    station_ids = [city["icao"] for city in selected]
    all_reports, endpoints, errors = fetch_awc_history_for_stations(
        station_ids, hours=config["awc_warmup_hours"], purpose="warmup",
    )
    due_by_icao = {city["icao"]: city for city in selected}
    summary = _rebuild_daily_extrema_from_history(
        state, due_by_icao, all_reports, fetched_at, endpoints, target_dates,
        source_name="AviationWeather.gov Data API (warmup)", source_errors=errors,
    )
    state["warmup_error_summary"] = {
        "generated_at_utc": fetched_at, "source": "AviationWeather.gov Data API", "due_city_count": len(due),
        "attempted_city_count": len(selected), "deferred_city_count": len(due) - len(selected),
        "failed_city_count": len(errors), "errors": errors,
    }
    return {
        "status": "completed" if not errors and len(selected) == len(due) else "partial", "due_city_count": len(due),
        "attempted_city_count": len(selected), "deferred_city_count": len(due) - len(selected), "reports_seen": len(all_reports),
        "awc_warmup_hours": config["awc_warmup_hours"], "failed_city_count": len(errors), **summary,
    }


def cache_is_fresh(timestamp: Any, max_age_seconds: int) -> bool:
    parsed = parse_time(timestamp)
    return parsed is not None and 0 <= (utc_now() - parsed).total_seconds() <= max_age_seconds


def market_rules_cover_local_days(state: dict[str, Any], cities: dict[str, dict[str, Any]]) -> bool:
    """True when both directions are resolved for each current IANA-local day.

    A direction may be resolved by a parsed rule or an explicit failure from the
    most recent Gamma refresh (for example, an event not yet listed).  Treating
    the latter as known prevents a no-market day from triggering a full 98-event
    Gamma retry every two minutes, while a local-date rollover still invalidates
    the cache because its directional keys are absent.
    """
    rules = state.get("market_rules", [])
    failures = state.get("market_failures", {})
    for city in cities.values():
        local_date = local_market_date(utc_now(), city)
        for direction in ("high", "low"):
            key = f"{city['city_id']}|{local_date}|{direction}"
            has_rule = any(
                rule.get("city_id") == city["city_id"] and rule.get("market_local_date") == local_date
                and rule.get("direction") == direction
                for rule in rules
            )
            if not has_rule and key not in failures:
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
        state["last_market_refresh_error"] = None
        state["last_market_refresh_failed_at"] = None
        return {"market_rules": len(rules), "market_failures": len(failures)}
    except Exception as exc:
        state["last_market_refresh_error"] = f"{type(exc).__name__}: {exc}"
        state["last_market_refresh_failed_at"] = iso_now()
        return {"market_refresh_error": state["last_market_refresh_error"]}


def signal_token_id(signal: dict[str, Any]) -> str | None:
    """Return the audited YES token without ever falling back to a NO token."""
    execution = signal.get("execution") if isinstance(signal.get("execution"), dict) else {}
    token_id = execution.get("token_id") or signal.get("token_id")
    if token_id:
        return str(token_id)
    bucket = signal.get("bucket") if isinstance(signal.get("bucket"), dict) else {}
    new_bucket = signal.get("new_bucket") if isinstance(signal.get("new_bucket"), dict) else {}
    return str(bucket.get("yes_token_id") or new_bucket.get("yes_token_id") or "") or None


def format_event(event: dict[str, Any]) -> str:
    report_age = event.get("checkwx_report_age_seconds")
    source = str(event.get("source") or "unknown")
    age_text = f" | 报文龄期 {report_age:.0f}s" if isinstance(report_age, (int, float)) else ""
    return f"[新{event['report_type']}] {event['airport_icao']} {event['report_time_utc']} | {source}{age_text}\n  {event['raw_metar']}"


def scan_once(config: dict[str, Any], local_books: dict[str, LocalBookSnapshot] | None = None) -> dict[str, Any]:
    """Run one two-minute weather/market-structure scan and emit YES-only paper signals.

    ``local_books`` is injected by the public market-stream monitor.  Missing,
    stale, incomplete or ambiguous books never create an entry, exit or rotation.
    """
    fetched_at = iso_now()
    scan_now = utc_now()
    state = load_state(config["state_path"])
    state["execution_engine"] = config["execution_engine"]
    cities = load_contract_cities(config["contract_cities_path"])
    warmup_summary = warm_up_current_local_days(config, state, cities)
    station_names = {icao: city["name"] for icao, city in cities.items()}
    station_ids = list(station_names)
    sourced_reports, fallback_summary = fetch_current_reports_with_awc_fallback(config, station_ids)
    normalized = [
        record for report, source_name, endpoint in sourced_reports
        if (record := normalize_report(report, station_names, endpoint, fetched_at, source_name)) is not None
    ]
    normalized.sort(key=lambda record: (record["report_time_utc"] or "", record["airport_icao"], record["report_type"]))
    seen: dict[str, str] = state["seen"]
    new_events = [record for record in normalized if record["event_id"] not in seen]
    for record in new_events:
        seen[record["event_id"]] = fetched_at
    state["seen"] = prune_seen(seen)
    state["last_successful_scan_utc"] = None
    state["last_report_count"] = len(normalized)
    state["last_new_event_count"] = len(new_events)
    state["last_current_source_summary"] = fallback_summary
    state["consecutive_failure_started_utc"] = None

    signals: list[dict[str, Any]] = []
    for event in new_events:
        city = cities.get(event["airport_icao"])
        report_time = parse_time(event.get("report_time_utc"))
        local_date = local_market_date(report_time, city) if city is not None and report_time else None
        if city is None or local_date is None or not _warmup_is_complete(state, city, local_date):
            signals.append({"signal_type": "no_signal", "reason": "daily_extrema_untrusted_warmup_incomplete", "event_id": event.get("event_id"), "icao": event.get("airport_icao"), "market_local_date": local_date})
            continue
        age = (scan_now - report_time).total_seconds()
        if age > config["max_report_age_seconds"]:
            signals.append({"signal_type": "no_signal", "reason": "report_too_old", "event_id": event.get("event_id"), "city_id": city["city_id"], "age_seconds": round(age, 3)})
            continue
        # This is the only trigger that can mark a held YES bucket for exit.
        signals.extend(mark_temperature_breaks(state, config["tail_consensus"], event, city, scan_now))

    # Refresh after processing this round's observed temperatures, then run the
    # consensus state machine against the latest discovered YES-token mappings.
    refresh_summary = refresh_market_rules_if_due(state, config, cities)
    market_rules = state.get("market_rules") or load_market_rules(config["market_rules_path"])
    books = local_books or {}
    if config["mode"] in {"paper", "observe"}:
        signals.extend(evaluate_tail_entries(state, config["tail_consensus"], cities, market_rules, books, scan_now))
        signals.extend(monitor_tail_positions(state, config["tail_consensus"], cities, market_rules, books, scan_now))
    else:
        signals.append({"signal_type": "no_signal", "reason": "live_executor_disabled", "disclaimer": "tree6yes has no live executor; no wallet, signature, or CLOB order path exists."})

    state["last_successful_scan_utc"] = iso_now()
    prune_state(state)
    atomic_json_write(config["state_path"], state)
    health = write_health_snapshot(config, state, cities)
    event_file = append_jsonl(config["event_dir"] / f"{utc_now().strftime('%Y-%m-%d')}.jsonl", new_events)
    signal_file = append_jsonl(config["signal_dir"] / f"{utc_now().strftime('%Y-%m-%d')}.jsonl", signals)
    audit_store = AuditStore(config["audit_db_path"])
    try:
        for signal in signals:
            execution = signal.get("execution") or signal.get("entry_execution") or {}
            bucket = signal.get("bucket") if isinstance(signal.get("bucket"), dict) else signal.get("new_bucket")
            if not isinstance(bucket, dict):
                bucket = {}
            audit_store.append(
                created_at_utc=iso_now(), event_type="tail_yes_signal_decision",
                correlation_id=str(signal.get("event_id") or signal.get("position_key") or bucket.get("bucket_id") or ""),
                mode=config["mode"], token_id=signal_token_id(signal), payload={"signal": signal, "execution": execution},
            )
    finally:
        audit_store.close()
    for event in new_events:
        print(format_event(event))
    entry_count = sum(item.get("signal_type") == "tail_yes_entry" for item in signals)
    alert_count = sum(item.get("signal_type") == "market_reversal_alert" for item in signals)
    rotation_count = sum(item.get("signal_type") == "tail_yes_rotation" for item in signals)
    print(
        f"[扫描完成] {fetched_at} | 合同站 {len(station_ids)} | 近期报告 {len(normalized)} | 新增事件 {len(new_events)} | "
        f"候选YES入场 {entry_count} | 85¢盘口预警 {alert_count} | 温度确认换手 {rotation_count} | 模式 {config['mode']}"
        + (f" | 配置刷新 {refresh_summary}" if refresh_summary else "")
    )
    return {
        "fetched_at_utc": fetched_at, "station_count": len(station_ids), "reports_seen": len(normalized),
        "new_events": len(new_events), "tail_yes_entries": entry_count, "market_reversal_alerts": alert_count,
        "tail_yes_rotations": rotation_count, "open_tail_positions": len(state.get("tail_positions", {})),
        "warmup": warmup_summary, "health_status": health["status"], "event_file": str(event_file) if event_file else None,
        "signal_file": str(signal_file) if signal_file else None, "mode": config["mode"],
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
    """Run the tree6yes coordinator: two-minute scans plus public book protection."""
    from tree6yes_runtime import Tree6YesRuntime
    Tree6YesRuntime(sys.modules[__name__], config).run()


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
        "warmup_error_summary": state.get("warmup_error_summary"),
        "last_current_source_summary": state.get("last_current_source_summary"),
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
        "strategy": "tail_consensus_yes_only",
        "open_tail_positions": len(state.get("tail_positions", {})),
        "tail_consensus_tracked_tokens": len(state.get("tail_consensus", {})),
        "market_reversal_alert_threshold": str(config["tail_consensus"].market_alert_bid),
        "warmup_error_summary": state.get("warmup_error_summary"),
        "last_current_source_summary": state.get("last_current_source_summary"),
    }, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每 2 分钟扫描 CheckWX 数据、以公共盘口保护尾盘 YES 纸面策略的工具")
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
