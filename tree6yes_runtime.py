"""Runtime coordinator for the tree6yes paper-only tail-consensus strategy."""
from __future__ import annotations

import threading
import time
from typing import Any

from audit_store import AuditStore
from edge_engine import append_jsonl, atomic_json_write, load_contract_cities
from tail_consensus_strategy import all_yes_token_ids, evaluate_tail_entries, monitor_tail_positions
from tail_market_stream import TailMarketStreamRunner


class Tree6YesRuntime:
    """Coordinate 15-minute scans with public market-stream alert monitoring.

    This coordinator deliberately does not expose any authenticated CLOB action.
    The only effects are local state, JSONL diagnostics and SQLite audit records.
    """

    def __init__(self, observer_module, config: dict[str, Any]) -> None:
        self.observer = observer_module
        self.config = config
        self._state_lock = threading.RLock()
        self._stopping = threading.Event()
        self.stream: TailMarketStreamRunner | None = None

    def _audit_market_signals(self, signals: list[dict[str, Any]]) -> None:
        if not signals:
            return
        state = self.observer.load_state(self.config["state_path"])
        signal_file = append_jsonl(
            self.config["signal_dir"] / f"{self.observer.utc_now().strftime('%Y-%m-%d')}.jsonl", signals,
        )
        audit_store = AuditStore(self.config["audit_db_path"])
        try:
            for signal in signals:
                audit_store.append(
                    created_at_utc=self.observer.iso_now(), event_type="tail_yes_market_protection",
                    correlation_id=str(signal.get("position_key") or signal.get("event_id") or ""),
                    mode=self.config["mode"], token_id=self.observer.signal_token_id(signal),
                    payload={"signal": signal, "execution": signal.get("exit_execution") or {}},
                )
        finally:
            audit_store.close()
        state["last_market_protection_signal_file"] = str(signal_file) if signal_file else None
        atomic_json_write(self.config["state_path"], state)

    def _on_market_snapshot(self, snapshots) -> None:
        """React to a fresh public-book update without performing any real trade."""
        with self._state_lock:
            state = self.observer.load_state(self.config["state_path"])
            cities = load_contract_cities(self.config["contract_cities_path"])
            now = self.observer.utc_now()
            rules = state.get("market_rules", [])
            signals: list[dict[str, Any]] = []
            # Continuously update stability so any intra-interval dip below 90¢
            # resets the 30-minute clock immediately rather than at the next scan.
            if self.config["mode"] in {"paper", "observe"} and self.observer.cache_is_fresh(state.get("market_rules_refreshed_at_utc"), self.config["market_rules_max_age_seconds"]):
                signals.extend(evaluate_tail_entries(state, self.config["tail_consensus"], cities, rules, snapshots, now))
            signals.extend(monitor_tail_positions(
                state, self.config["tail_consensus"], cities, rules, snapshots, now,
            ))
            # 85¢ alerts never rotate without a temperature break already marked
            # by the scheduled CheckWX scan.
            # Persist stability-window resets and 85¢ alert latches even when
            # no externally visible signal was emitted on this update.
            atomic_json_write(self.config["state_path"], state)
            if signals:
                self._audit_market_signals(signals)
                for signal in signals:
                    if signal.get("signal_type") == "market_reversal_alert":
                        print(f"[85¢盘口预警] {signal.get('position_key')} | best bid {signal.get('best_bid')} | 仅告警，不换手")

    def _refresh_stream_tokens(self) -> None:
        state = self.observer.load_state(self.config["state_path"])
        tokens = all_yes_token_ids(state.get("market_rules", []))
        if not tokens:
            return
        if self.stream is None:
            self.stream = TailMarketStreamRunner(tokens, on_snapshot=self._on_market_snapshot)
            self.stream.start()
        else:
            self.stream.reconfigure(tokens)

    def _scan(self) -> dict[str, Any]:
        with self._state_lock:
            snapshots = self.stream.snapshots(max_age_seconds=self.config["local_book_max_age_seconds"]) if self.stream else {}
            result = self.observer.scan_once(self.config, snapshots)
            self._refresh_stream_tokens()
            return result

    def run(self) -> None:
        interval = self.config["scan_interval_seconds"]
        lock_handle = self.observer.acquire_single_instance_lock(self.config["state_path"])
        print(f"tree6yes 已启动：CheckWX/Gamma 每 {interval} 秒扫描；YES 盘口通过公共数据流实时保护；仅纸面执行。")
        try:
            while not self._stopping.is_set():
                try:
                    self._scan()
                except KeyboardInterrupt:
                    raise
                except self.observer.CheckWXRateLimitError as exc:
                    self.observer.write_failure_health(self.config, exc)
                    delay = max(self.config["rate_limit_backoff_seconds"], exc.retry_after_seconds or 0)
                    print(f"[CheckWX 限流退避] {exc}；等待 {delay} 秒后重试。")
                    if self._stopping.wait(delay):
                        break
                    continue
                except Exception as exc:
                    self.observer.write_failure_health(self.config, exc)
                    print(f"[tree6yes 扫描失败] {type(exc).__name__}: {exc}")
                if self._stopping.wait(max(0.1, interval - (time.time() % interval))):
                    break
        except KeyboardInterrupt:
            print("\ntree6yes 已停止。")
        finally:
            if self.stream is not None:
                self.stream.stop()
            lock_handle.close()

    def stop(self) -> None:
        self._stopping.set()
        if self.stream is not None:
            self.stream.stop()
