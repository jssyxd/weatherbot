#!/usr/bin/env python3
"""Minute-by-minute METAR/SPECI candidate-edge observer.

The program scans published AviationWeather.gov METAR/SPECI reports every minute
for 49 verified contract stations.  It uses TAF first and Wunderground Forecast
only as a 15-minute edge-configuration input.  It never loads credentials,
reads a wallet, signs an order, or submits a real trade.  ``mode=live`` is an
explicitly blocked compatibility boundary until a separately reviewed executor
is implemented by the user.
"""
from __future__ import annotations

import argparse
import json
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
    refresh_edge_configs,
)
from market_adapter import refresh_market_rules

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
AWC_ENDPOINT = "https://aviationweather.gov/api/data/metar"
SUPPORTED_TYPES = {"METAR", "SPECI"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    """Accept AWC ISO strings or epoch seconds, returning aware UTC datetime."""
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
    edge_interval = int(config.get("edge_refresh_interval_seconds", 900))
    if edge_interval < 900:
        raise ValueError("edge_refresh_interval_seconds 不得小于 900")
    max_age = int(config.get("max_report_age_seconds", 600))
    if max_age < 60:
        raise ValueError("max_report_age_seconds 不得小于 60")
    failure_pause = int(config.get("failure_pause_after_seconds", 1800))
    if failure_pause < 60:
        raise ValueError("failure_pause_after_seconds 不得小于 60")
    mode = str(config.get("mode", "paper")).lower().strip()
    if mode not in {"paper", "live"}:
        raise ValueError("mode 只能为 paper 或 live")
    return {
        "scan_interval_seconds": interval,
        "history_hours": history_hours,
        "stations_per_request": chunk_size,
        "edge_refresh_interval_seconds": edge_interval,
        "max_report_age_seconds": max_age,
        "failure_pause_after_seconds": failure_pause,
        "mode": mode,
        "state_path": BASE_DIR / str(config.get("state_path", "data/state.json")),
        "event_dir": BASE_DIR / str(config.get("event_dir", "data/observations")),
        "signal_dir": BASE_DIR / str(config.get("signal_dir", "data/signals")),
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
        ("edge_failures", {}), ("daily_extrema", {}), ("handled_candidate_buckets", {}),
        ("market_rules", []), ("market_failures", {}), ("consecutive_failure_started_utc", None),
        ("paper_city_day_notional", {}), ("execution_paused", False),
    ):
        state.setdefault(key, default)
    return state


def prune_seen(seen: dict[str, str], keep_hours: int = 72) -> dict[str, str]:
    cutoff = utc_now() - timedelta(hours=keep_hours)
    return {key: first_seen for key, first_seen in seen.items() if (timestamp := parse_time(first_seen)) and timestamp >= cutoff}


def prune_state(state: dict[str, Any], keep_days: int = 3) -> None:
    cutoff = (utc_now() - timedelta(days=keep_days)).date().isoformat()
    for key in ("daily_extrema",):
        state[key] = {item_key: value for item_key, value in state.get(key, {}).items() if str(value.get("market_local_date", "")) >= cutoff}


def edge_refresh_due(state: dict[str, Any], interval_seconds: int) -> bool:
    prior = parse_time(state.get("last_edge_refresh_utc"))
    return prior is None or (utc_now() - prior).total_seconds() >= interval_seconds


def refresh_configuration(state: dict[str, Any], config: dict[str, Any], cities: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not edge_refresh_due(state, config["edge_refresh_interval_seconds"]):
        return None
    local_dates = {icao: local_market_date(utc_now(), city) for icao, city in cities.items()}
    edge_summary: dict[str, Any]
    try:
        edge_summary = refresh_edge_configs(state, cities, local_dates)
    except Exception as exc:  # retain prior valid configurations; record an auditable failure
        state["last_edge_refresh_error"] = f"{type(exc).__name__}: {exc}"
        edge_summary = {"edge_refresh_error": state["last_edge_refresh_error"]}
    try:
        rules, failures = refresh_market_rules(cities, local_dates)
        state["market_rules"] = rules
        state["market_failures"] = failures
    except Exception as exc:
        state["last_market_refresh_error"] = f"{type(exc).__name__}: {exc}"
    return edge_summary


def enrich_execution(signal: dict[str, Any], mode: str, state: dict[str, Any]) -> dict[str, Any]:
    """Attach a capped paper intent or a live safety block; never submit an order."""
    if signal.get("signal_type") != "candidate_no_signal":
        return signal
    result = dict(signal)
    city_day_key = f"{signal['city_id']}|{signal['market_local_date']}"
    if mode == "paper":
        ledger: dict[str, float] = state.setdefault("paper_city_day_notional", {})
        spent = float(ledger.get(city_day_key, 0.0))
        if spent + 1.0 > 2.0:
            status = "paper_intent_skipped_city_day_cap"
            notional = 0.0
        else:
            ledger[city_day_key] = round(spent + 1.0, 2)
            status = "paper_order_intent_pending_price_gate"
            notional = 1.0
        result["execution"] = {
            "mode": "paper",
            "status": status,
            "notional_usdc": notional,
            "spent_city_day_usdc": round(spent, 2),
            "max_city_day_notional_usdc": 2.0,
            "order_type": "FAK",
            "side": "BUY_NO",
            "max_price_exclusive": 0.96,
            "price_gate": "not_evaluated_no_clob_executor",
            "message": "模拟意图；不连接 CLOB、不加载凭据、不提交订单。",
        }
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
    # Process published observations with the last known good edge configuration first.
    # The slower TAF/Wunderground refresh runs after this minute's fact path.
    refresh_summary: dict[str, Any] | None = None
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
    state["last_successful_scan_utc"] = fetched_at
    state["last_report_count"] = len(normalized)
    state["last_new_event_count"] = len(new_events)
    state["consecutive_failure_started_utc"] = None

    signals: list[dict[str, Any]] = []
    for event in new_events:
        city = cities.get(event["airport_icao"])
        if city is None:
            continue
        for signal in evaluate_observation(state, event, city, market_rules, config["max_report_age_seconds"]):
            signals.append(enrich_execution(signal, config["mode"], state))
    # Refresh future edge/rule inputs only after this minute's time-sensitive observations.
    refresh_summary = refresh_configuration(state, config, cities)
    prune_state(state)
    atomic_json_write(config["state_path"], state)
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
        "event_file": str(event_file) if event_file else None,
        "signal_file": str(signal_file) if signal_file else None,
        "mode": config["mode"],
    }


def sleep_to_next_interval(interval_seconds: int) -> None:
    now = time.time()
    time.sleep(max(0.1, interval_seconds - (now % interval_seconds)))


def run_loop(config: dict[str, Any]) -> None:
    interval = config["scan_interval_seconds"]
    print(f"候选边缘扫描器已启动：每 {interval} 秒拉取已发布 METAR/SPECI；默认仅 paper 意图，绝不提交真实订单。")
    # User-confirmed startup behavior: obtain edge inputs once before the first fact scan.
    startup_state = load_state(config["state_path"])
    startup_cities = load_contract_cities(config["contract_cities_path"])
    startup_summary = refresh_configuration(startup_state, config, startup_cities)
    atomic_json_write(config["state_path"], startup_state)
    print(f"[启动边缘配置] {startup_summary or {'status': 'cached'}}")
    failure_started: datetime | None = None
    while True:
        try:
            scan_once(config)
            failure_started = None
        except KeyboardInterrupt:
            print("\n扫描器已停止。")
            return
        except Exception as exc:
            failure_started = failure_started or utc_now()
            elapsed = (utc_now() - failure_started).total_seconds()
            status = "[扫描暂停：连续失败达到阈值]" if elapsed >= config["failure_pause_after_seconds"] else "[扫描失败]"
            print(f"{status} {type(exc).__name__}: {exc}", file=sys.stderr)
        sleep_to_next_interval(interval)


def show_status(config: dict[str, Any]) -> None:
    state = load_state(config["state_path"])
    cities = load_contract_cities(config["contract_cities_path"])
    print(json.dumps({
        "contract_station_count": len(cities),
        "scan_interval_seconds": config["scan_interval_seconds"],
        "edge_refresh_interval_seconds": config["edge_refresh_interval_seconds"],
        "mode": config["mode"],
        "live_executor": "not_present",
        "last_successful_scan_utc": state.get("last_successful_scan_utc"),
        "last_edge_refresh_utc": state.get("last_edge_refresh_utc"),
        "active_edge_configs": len(state.get("edge_configs", {})),
        "market_rules": len(state.get("market_rules", [])),
        "edge_failures": len(state.get("edge_failures", {})),
        "dedupe_entries": len(state.get("seen", {})),
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
            scan_once(config)
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
