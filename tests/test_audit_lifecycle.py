from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from audit_store import AuditStore, ORDER_STAGES
from tree12_allno_strategy import (
    new_order_id, plan_tree12_entries, paper_fill_working_order,
    start_tree12_exit_chase, position_key, tree12_paper_fill,
    ensure_tree12_state,
)


class AuditStoreLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = AuditStore(Path(self._tmp.name) / "audit.sqlite3")

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_append_carries_order_id(self) -> None:
        row_id = self.db.append(
            created_at_utc="2026-08-30T00:00:00Z", event_type="tree12_submit_entry",
            payload={"key": "k1"}, order_id="t12-abc123",
        )
        self.assertGreater(row_id, 0)
        events = self.db.events_for_order("t12-abc123")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "tree12_submit_entry")
        self.assertEqual(events[0]["order_id"], "t12-abc123")

    def test_order_lifecycle_stages_are_recorded(self) -> None:
        for i, stage in enumerate(ORDER_STAGES):
            self.db.append_order_event(
                created_at_utc=f"2026-08-30T00:00:0{i}Z",
                order_id="t12-life", stage=stage, mode="paper", token_id="no-1",
            )
        events = self.db.events_for_order("t12-life")
        self.assertEqual([e["payload"]["stage"] for e in events], list(ORDER_STAGES))

    def test_unknown_stage_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.db.append_order_event(
                created_at_utc="2026-08-30T00:00:00Z", order_id="t12-x",
                stage="NOT_A_STAGE",
            )

    def test_events_for_order_oldest_first(self) -> None:
        for i in range(3):
            self.db.append(
                created_at_utc=f"2026-08-30T00:00:0{i}Z", event_type="tick",
                payload={"i": i}, order_id="t12-seq",
            )
        events = self.db.events_for_order("t12-seq")
        self.assertEqual([e["payload"]["i"] for e in events], [0, 1, 2])

    def test_legacy_db_migrates_order_id_column(self) -> None:
        # Re-open a store that was created before the column existed.
        self.db.close()
        self.db = AuditStore(Path(self._tmp.name) / "audit.sqlite3")
        row_id = self.db.append(
            created_at_utc="2026-08-30T00:00:00Z", event_type="tree12_paper_fill",
            payload={"key": "k1"}, order_id="t12-migrated",
        )
        self.assertGreater(row_id, 0)
        self.assertEqual(len(self.db.events_for_order("t12-migrated")), 1)


def city_shanghai():
    return {"city_id": "shanghai", "icao": "ZSPD", "timezone": "Asia/Shanghai", "market_unit": "C"}


class Tree12OrderIdTests(unittest.TestCase):
    def test_new_order_id_is_unique_prefixed(self) -> None:
        ids = {new_order_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(i.startswith("t12-") for i in ids))

    def test_submit_fill_position_share_order_id(self) -> None:
        city = city_shanghai()
        local_date = "2026-09-10"
        now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # lead=28h ∈ (18,30]
        rules = [{"enabled": True, "city_id": "shanghai", "market_local_date": local_date, "direction": "high",
                  "buckets": [
                      {"bucket_id": "b29", "lo": 29, "hi": 30, "no_token_id": "no-29"},
                      {"bucket_id": "b30", "lo": 30, "hi": 31, "no_token_id": "no-30"},
                      {"bucket_id": "b31", "lo": 31, "hi": 32, "no_token_id": "no-31"},
                      {"bucket_id": "b32", "lo": 32, "hi": 33, "no_token_id": "no-32"},
                  ]}]
        books = {"no-29": {"best_ask": "0.85", "tick_size": "0.01",
                           "asks": [{"price": "0.85", "size": "10"}], "bids": []},
                 "no-30": {"best_ask": "0.87", "tick_size": "0.01",
                           "asks": [{"price": "0.87", "size": "10"}], "bids": []},
                 "no-31": {"best_ask": "0.91", "tick_size": "0.01",
                           "asks": [{"price": "0.91", "size": "10"}], "bids": []},
                 "no-32": {"best_ask": "0.95", "tick_size": "0.01",
                           "asks": [{"price": "0.95", "size": "10"}], "bids": []}}
        actions = plan_tree12_entries({}, {"shanghai": city}, rules, books, now,
                                      {"target_order_shares": "5", "mode": "paper"})
        submits = [a for a in actions if a.get("action_type") == "tree12_submit_entry"]
        fills = [a for a in actions if a.get("action_type") == "tree12_paper_fill"]
        self.assertTrue(submits and fills)
        self.assertEqual(submits[0]["order_id"], fills[0]["order_id"])
        tree = ensure_tree12_state({})
        # replay the same state to verify position linkage
        state = {"tree12": {"working_orders": {}, "positions": {}, "exit_chases": {}, "ws_ask_samples": {}}}
        state["tree12"]["working_orders"][submits[0]["key"]] = dict(submits[0])
        key = submits[0]["key"]
        paper_fill_working_order(state, key, Decimal("5"), Decimal("0.86"), now)
        pos = state["tree12"]["positions"][key]
        self.assertEqual(pos["order_id"], submits[0]["order_id"])

    def test_exit_chase_links_position_order_id(self) -> None:
        key = position_key("shanghai", "2026-09-10", "high", "b30")
        state = {"tree12": {"working_orders": {}, "positions": {key: {"key": key, "order_id": "t12-exit-link",
                 "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high",
                 "bucket_id": "b30", "token_id": "no-30", "shares": "5"}},
                 "exit_chases": {}, "ws_ask_samples": {}}}
        now = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
        chase = start_tree12_exit_chase(state, key, "no-30", Decimal("5"), "metar_hit_no_bucket", now)
        self.assertEqual(chase["order_id"], "t12-exit-link")

    def test_paper_fill_result_carries_order_id(self) -> None:
        state = {"paper_initial_capital_usdc": 1000.0, "paper_total_debit_usdc": 0.0,
                 "tree12": {"working_orders": {"k1": {"key": "k1", "order_id": "t12-fill-carrier",
                    "status": "working_gtc_buy_no", "remaining_shares": "5",
                    "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high",
                    "bucket_id": "b32", "token_id": "no-32", "lo": 32, "hi": 33, "limit_price": "0.90"}},
                    "positions": {}, "exit_chases": {}, "ws_ask_samples": {}}}
        now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # lead=28h ∈ (18,30]
        book = {"best_ask": "0.90", "asks": [{"price": "0.90", "size": "3"}], "bids": []}
        result = tree12_paper_fill(state, "k1", Decimal("5"), book, now)
        self.assertEqual(result["status"], "paper_filled")
        self.assertEqual(result["order_id"], "t12-fill-carrier")


if __name__ == "__main__":
    unittest.main()
