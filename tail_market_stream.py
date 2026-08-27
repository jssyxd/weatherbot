"""Public Polymarket market-stream runner for the tree6yes paper strategy.

This module only opens the unauthenticated market-data stream.  It contains no
wallet, signing, order, cancellation, amendment, or user-account functionality.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Iterable

from local_order_book import LocalBookSnapshot
from tree3_runtime import Tree3MarketRuntime
from websocket_market_data import MARKET_WS_URL


SnapshotCallback = Callable[[dict[str, LocalBookSnapshot]], None]


class TailMarketStreamRunner:
    """Reconnectable public CLOB market-data runner with 10-second heartbeat.

    Token sets may change when daily market rules refresh.  The runner replaces
    its subscription atomically; callers should regard any missing/stale local
    book during reconfiguration as a fail-closed condition.
    """

    def __init__(self, token_ids: Iterable[str], *, on_snapshot: SnapshotCallback | None = None, clock=None) -> None:
        self._clock = clock or time.time
        self._on_snapshot = on_snapshot
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._app = None
        self._heartbeat_thread: threading.Thread | None = None
        self._runtime = Tree3MarketRuntime(token_ids, max_book_age_seconds=3.0, clock=self._clock)
        self._token_ids = tuple(self._runtime.stream.token_ids)
        self.last_error: str | None = None
        self.last_connected_at: float | None = None

    @property
    def token_ids(self) -> tuple[str, ...]:
        with self._lock:
            return self._token_ids

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="tree6yes-market-stream", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def reconfigure(self, token_ids: Iterable[str]) -> bool:
        ids = tuple(dict.fromkeys(str(value) for value in token_ids if str(value)))
        if not ids:
            raise ValueError("at_least_one_yes_token_required")
        with self._lock:
            if ids == self._token_ids:
                return False
        self.stop()
        with self._lock:
            self._runtime = Tree3MarketRuntime(ids, max_book_age_seconds=3.0, clock=self._clock)
            self._token_ids = ids
            self.last_error = None
            self._app = None
            self._thread = None
        self.start()
        return True

    def snapshots(self, *, max_age_seconds: float) -> dict[str, LocalBookSnapshot]:
        now = self._clock()
        with self._lock:
            return {
                token_id: snapshot
                for token_id in self._token_ids
                if (snapshot := self._runtime.local_snapshot(token_id)) is not None
                and snapshot.is_fresh(max_age_seconds, now=now)
            }

    def health(self) -> dict[str, object]:
        with self._lock:
            return {
                **self._runtime.health(), "token_count": len(self._token_ids), "last_error": self.last_error,
                "last_connected_at_epoch": self.last_connected_at,
            }

    def _run(self) -> None:
        try:
            import websocket  # websocket-client, intentionally imported only for runtime use.
        except ImportError:
            self.last_error = "websocket_client_dependency_missing"
            return
        retry_seconds = 1.0
        while not self._stop.is_set():
            try:
                app = websocket.WebSocketApp(
                    MARKET_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                with self._lock:
                    self._app = app
                app.run_forever(ping_interval=0, ping_timeout=None)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            if not self._stop.wait(retry_seconds):
                retry_seconds = min(retry_seconds * 2, 30.0)
            else:
                break
        with self._lock:
            self._app = None

    def _on_open(self, app) -> None:
        with self._lock:
            message = self._runtime.connect()
            self.last_connected_at = self._clock()
            self.last_error = None
        app.send(json.dumps(message))
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, name="tree6yes-market-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(10):
            with self._lock:
                app = self._app
            if app is None:
                return
            try:
                app.send("PING")
            except Exception as exc:
                self.last_error = f"heartbeat:{type(exc).__name__}: {exc}"
                return

    def _on_message(self, _app, message) -> None:
        try:
            with self._lock:
                result = self._runtime.on_message(message)
                if result is None:
                    return
                snapshots = self.snapshots(max_age_seconds=3.0)
            if self._on_snapshot is not None and snapshots:
                self._on_snapshot(snapshots)
        except Exception as exc:
            self.last_error = f"market_message:{type(exc).__name__}: {exc}"

    def _on_error(self, _app, error) -> None:
        self.last_error = f"market_socket_error:{error}"

    def _on_close(self, _app, status_code, message) -> None:
        with self._lock:
            self._runtime.disconnect(f"socket_closed:{status_code}:{message}")
