#!/usr/bin/env python3
"""Read-only, minute-by-minute observer for published METAR and SPECI reports.

The scanner requests official AviationWeather.gov data only after reports have
been published. It never calls forecasting models, Polymarket, trading APIs, or
wallet services. New reports are deduplicated, printed, and retained as JSONL
with report, receipt, and local fetch timestamps for later latency analysis.
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

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"
AWC_ENDPOINT = "https://aviationweather.gov/api/data/metar"
SUPPORTED_TYPES = {"METAR", "SPECI"}

DEFAULT_STATIONS = [
    {"icao": "KLGA", "name": "New York LaGuardia"},
    {"icao": "KORD", "name": "Chicago O'Hare"},
    {"icao": "KMIA", "name": "Miami"},
    {"icao": "KDAL", "name": "Dallas Love Field"},
    {"icao": "KSEA", "name": "Seattle"},
    {"icao": "KATL", "name": "Atlanta"},
    {"icao": "KLAX", "name": "Los Angeles"},
    {"icao": "KDEN", "name": "Denver"},
    {"icao": "KPHX", "name": "Phoenix"},
    {"icao": "KIAH", "name": "Houston"},
    {"icao": "KBOS", "name": "Boston"},
    {"icao": "EGLC", "name": "London City"},
    {"icao": "LFPG", "name": "Paris Charles de Gaulle"},
    {"icao": "EDDM", "name": "Munich"},
    {"icao": "LTAC", "name": "Ankara Esenboğa"},
    {"icao": "EHAM", "name": "Amsterdam Schiphol"},
    {"icao": "LEMD", "name": "Madrid Barajas"},
    {"icao": "LIRF", "name": "Rome Fiumicino"},
    {"icao": "ESSA", "name": "Stockholm Arlanda"},
    {"icao": "RKSI", "name": "Seoul Incheon"},
    {"icao": "RJTT", "name": "Tokyo Haneda"},
    {"icao": "ZSPD", "name": "Shanghai Pudong"},
    {"icao": "WSSS", "name": "Singapore Changi"},
    {"icao": "VILK", "name": "Lucknow"},
    {"icao": "LLBG", "name": "Tel Aviv Ben Gurion"},
    {"icao": "OMDB", "name": "Dubai"},
    {"icao": "VABB", "name": "Mumbai"},
    {"icao": "VTBS", "name": "Bangkok Suvarnabhumi"},
    {"icao": "WIII", "name": "Jakarta Soekarno-Hatta"},
    {"icao": "CYYZ", "name": "Toronto Pearson"},
    {"icao": "SBGR", "name": "São Paulo Guarulhos"},
    {"icao": "SAEZ", "name": "Buenos Aires Ezeiza"},
    {"icao": "NZWN", "name": "Wellington"},
    {"icao": "YSSY", "name": "Sydney"},
    {"icao": "FAOR", "name": "Johannesburg"},
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    """Accept AWC ISO strings or epoch seconds, returning an aware UTC datetime."""
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


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 {path}: {exc}") from exc


def normalize_stations(raw_stations: Any) -> list[dict[str, str]]:
    if raw_stations is None:
        return DEFAULT_STATIONS
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
        raise RuntimeError(
            f"未找到 {config_path.name}。请先复制 config.example.json 为 config.json，并按需修改 stations。"
        )
    if not isinstance(config, dict):
        raise ValueError("配置根节点必须是 JSON 对象")
    interval = int(config.get("scan_interval_seconds", 60))
    if interval < 60:
        raise ValueError("scan_interval_seconds 不得小于 60；AWC 全量缓存按分钟更新")
    history_hours = int(config.get("history_hours", 1))
    if history_hours < 1 or history_hours > 24:
        raise ValueError("history_hours 必须介于 1 和 24")
    chunk_size = int(config.get("stations_per_request", 35))
    if chunk_size < 1 or chunk_size > 100:
        raise ValueError("stations_per_request 必须介于 1 和 100")
    return {
        "scan_interval_seconds": interval,
        "history_hours": history_hours,
        "stations_per_request": chunk_size,
        "state_path": BASE_DIR / str(config.get("state_path", "data/state.json")),
        "event_dir": BASE_DIR / str(config.get("event_dir", "data/observations")),
        "stations": normalize_stations(config.get("stations")),
    }


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def fetch_awc_reports(station_ids: list[str], history_hours: int) -> tuple[list[dict[str, Any]], str]:
    query = urllib.parse.urlencode({
        "ids": ",".join(station_ids),
        "format": "json",
        "hours": str(history_hours),
    }, safe=",")
    url = f"{AWC_ENDPOINT}?{query}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "metar-observer/1.0 (+https://github.com/jssyxd/weatherbot)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"AWC 请求失败（HTTP {exc.code}）: {url}") from exc
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
            # Some upstream reports contain a receipt timestamp that predates the
            # report timestamp. Preserve both source timestamps, but do not present
            # an impossible negative value as a distribution delay.
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
        "raw_metar": raw,
    }


def load_state(state_path: Path) -> dict[str, Any]:
    state = load_json(state_path, {"seen": {}, "last_successful_scan_utc": None})
    if not isinstance(state, dict) or not isinstance(state.get("seen", {}), dict):
        raise RuntimeError("状态文件结构无效；请先备份并删除该文件后重试")
    state.setdefault("seen", {})
    state.setdefault("last_successful_scan_utc", None)
    return state


def prune_seen(seen: dict[str, str], keep_hours: int = 72) -> dict[str, str]:
    cutoff = utc_now() - timedelta(hours=keep_hours)
    retained: dict[str, str] = {}
    for key, first_seen in seen.items():
        timestamp = parse_time(first_seen)
        if timestamp and timestamp >= cutoff:
            retained[key] = first_seen
    return retained


def append_jsonl(event_dir: Path, events: list[dict[str, Any]]) -> Path | None:
    if not events:
        return None
    event_dir.mkdir(parents=True, exist_ok=True)
    output = event_dir / f"{utc_now().strftime('%Y-%m-%d')}.jsonl"
    with output.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return output


def format_event(event: dict[str, Any]) -> str:
    delay = event.get("awc_receipt_delay_seconds")
    delay_text = f" | AWC延迟 {delay:.0f}s" if isinstance(delay, (int, float)) else ""
    temperature = event.get("temperature_c")
    temperature_text = f" | {temperature}°C" if temperature is not None else ""
    return (
        f"[新{event['report_type']}] {event['airport_icao']} {event['report_time_utc']}"
        f"{temperature_text}{delay_text}\n  {event['raw_metar']}"
    )


def scan_once(config: dict[str, Any]) -> dict[str, Any]:
    fetched_at = iso_now()
    state = load_state(config["state_path"])
    station_names = {item["icao"]: item["name"] for item in config["stations"]}
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
    atomic_json_write(config["state_path"], state)
    event_file = append_jsonl(config["event_dir"], new_events)

    for event in new_events:
        print(format_event(event))
    print(
        f"[扫描完成] {fetched_at} | 站点 {len(station_ids)} | 近期报告 {len(normalized)} | "
        f"新增事件 {len(new_events)}" + (f" | 写入 {event_file}" if event_file else "")
    )
    return {
        "fetched_at_utc": fetched_at,
        "station_count": len(station_ids),
        "reports_seen": len(normalized),
        "new_events": len(new_events),
        "event_file": str(event_file) if event_file else None,
    }


def sleep_to_next_interval(interval_seconds: int) -> None:
    now = time.time()
    sleep_for = interval_seconds - (now % interval_seconds)
    time.sleep(max(0.1, sleep_for))


def run_loop(config: dict[str, Any]) -> None:
    interval = config["scan_interval_seconds"]
    print(
        "METAR/SPECI 只读扫描器已启动。"
        f"每 {interval} 秒查询一次已发布报告；不会请求预测模型、市场数据或交易接口。"
    )
    while True:
        try:
            scan_once(config)
        except KeyboardInterrupt:
            print("\n扫描器已停止。")
            return
        except Exception as exc:
            print(f"[扫描失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        sleep_to_next_interval(interval)


def show_status(config: dict[str, Any]) -> None:
    state = load_state(config["state_path"])
    print(json.dumps({
        "station_count": len(config["stations"]),
        "scan_interval_seconds": config["scan_interval_seconds"],
        "history_hours": config["history_hours"],
        "last_successful_scan_utc": state.get("last_successful_scan_utc"),
        "last_report_count": state.get("last_report_count", 0),
        "last_new_event_count": state.get("last_new_event_count", 0),
        "dedupe_entries": len(state.get("seen", {})),
        "event_dir": str(config["event_dir"]),
    }, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每分钟扫描已发布 METAR/SPECI 的只读观察工具")
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
