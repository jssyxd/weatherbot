#!/usr/bin/env python3
"""Aggregate the last-15-minute weatherbot runtime status and push it to Telegram.

Reads the host-side ``data/`` volume and ``docker logs`` for the weatherbot
container, composes a compact report, then delivers it through the locally
configured Hermes messaging gateway (``hermes send``). No LLM is involved.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
CONTAINER = "weatherbot-tree12"
TARGET = "telegram:liudi"
WINDOW_MINUTES = 15


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def count_recent(pattern: str, since_epoch: float) -> int:
    total = 0
    for path in sorted(BASE.glob(pattern)):
        try:
            if path.stat().st_mtime >= since_epoch:
                total += sum(1 for _ in open(path, encoding="utf-8"))
        except Exception:
            continue
    return total


def docker_logs(since_minutes: int) -> str:
    """Return recent container logs, or the host observer.log fallback."""
    try:
        proc = subprocess.run(
            ["docker", "logs", "--since", f"{since_minutes}m", CONTAINER],
            capture_output=True, text=True, timeout=25,
        )
        if proc.returncode == 0:
            return (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 - fall through to file log
        pass
    log_path = DATA / "observer.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-400:])
        except Exception:
            return "(observer.log 不可读)"
    return "(无容器日志，也无 observer.log)"


def main() -> int:
    state = read_json(DATA / "state.json", {})
    health = read_json(DATA / "health.json", {})
    since_epoch = __import__("time").time() - WINDOW_MINUTES * 60

    initial = float(state.get("paper_initial_capital_usdc", 1000.0))
    debit = float(state.get("paper_total_debit_usdc", 0.0))
    remaining = initial - debit

    events = count_recent("data/observations/*.jsonl", since_epoch)
    signals = count_recent("data/signals/*.jsonl", since_epoch)
    tree12 = count_recent("data/tree12_actions/*.jsonl", since_epoch)
    tree5 = count_recent("data/tree5_actions/*.jsonl", since_epoch)

    lines = [
        "🌤 weatherbot 运行报告（近15分钟）",
        f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
        f"健康: {health.get('status', 'unknown')}",
        f"最近成功扫描: {state.get('last_successful_scan_utc') or '-'}",
        f"纸面资金: 初始 {initial:.2f} USDC | 已用 {debit:.2f} | 剩余 {remaining:.2f}",
        f"近15min新增: METAR事件 {events} | 信号 {signals} | tree12动作 {tree12} | tree5动作 {tree5}",
    ]

    log = docker_logs(WINDOW_MINUTES)
    errors = [
        line for line in log.splitlines()
        if any(key in line for key in ("错误", "失败", "Error", "Traceback", "429", "异常", "限流"))
    ]
    if errors:
        lines.append(f"异常/失败 {len(errors)} 条（最近5条）:")
        lines.extend(f"  {e[:160]}" for e in errors[-5:])
    else:
        lines.append("异常/失败: 无")

    message = "\n".join(lines)
    print(message)
    try:
        subprocess.run(["hermes", "send", "--to", TARGET, message], check=True, timeout=60)
        print("[已发送 Telegram]", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[发送失败] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
