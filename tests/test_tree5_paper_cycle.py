from __future__ import annotations

import json
import unittest
from pathlib import Path

from tree5_paper_cycle import evaluate_paper_cycle

NS = 1_000_000_000
POLICY = json.loads((Path(__file__).resolve().parents[1] / "tree5_ev_policy.example.json").read_text(encoding="utf-8"))


def snapshot(bucket: str, token: str, second: int, bid: str, ask: str) -> dict:
    return {"ready": True, "received_monotonic_ns": second * NS, "market_id": "shanghai-high", "bucket_id": bucket, "token_id": token, "tick_size": "0.01", "min_order_size": "1", "book_hash": f"{bucket}-{second}", "bids": [{"price": bid, "size": "100"}], "asks": [{"price": ask, "size": "10"}]}


def cycle(*, leader_bid: str = "0.72", runner_bid: str = "0.60") -> dict:
    snapshots: list[dict] = []
    for second in range(2_800, 10_000, 150):
        snapshots += [snapshot("b29", "token-29", second, "0.20", "0.22"), snapshot("b30", "token-30", second, leader_bid, "0.74"), snapshot("b31", "token-31", second, runner_bid, "0.62")]
    return {
        "cycle_id": "cycle-1", "t0_monotonic_ns": 10_000 * NS,
        "latest_visible_taf": {"token_id": "token-30", "version_id": "taf-visible-before-t0"},
        "market": {"market_id": "shanghai-high", "bucket_ids": ["b29", "b30", "b31"]},
        "pre_t0_l2_snapshots": snapshots,
        "post_t0_entry_snapshot": snapshot("b30", "token-30", 10_003, "0.73", "0.75"),
        "calibration": {"p_lower": "0.82", "q_fill": "0.80", "oos_sample_count": 120},
        "costs": {"entry_fee_full_fill": "0.01", "expected_exit_cost": "0.04", "latency_slippage_reserve": "0.02"},
        "confirmed_positions": [{"position_id": "old-position", "market_id": "shanghai-high", "direction": "high", "bucket": {"bucket_id": "b29", "lo": 29, "hi": 30}, "token_id": "token-29", "confirmed_shares": "5", "pending_entry_id": "old-entry"}],
        "position_evidence": {"old-position": {"observed_extreme": "30", "consensus": {"status": "PAPER_MARKET_CONSENSUS_READY", "complete_market_coverage": True, "leader_bucket_id": "b30", "bucket_prices": {"b29": "0.20", "b30": "0.72", "b31": "0.60"}}, "time_closure_triggered": False}},
    }


class Tree5PaperCycleTests(unittest.TestCase):
    def test_cycle_requires_alignment_and_positive_ev_before_paper_entry(self) -> None:
        result = evaluate_paper_cycle(cycle(), POLICY)
        self.assertEqual(result["status"], "PAPER_ENTRY_READY")
        self.assertEqual(result["market_alignment"]["status"], "PAPER_ALIGNMENT_READY")
        self.assertEqual(result["entry"]["ev"]["status"], "PAPER_EV_POSITIVE")
        self.assertEqual(result["orders_submitted"], 0)

    def test_new_taf_cycle_skips_when_leader_does_not_clear_twenty_percent(self) -> None:
        result = evaluate_paper_cycle(cycle(leader_bid="0.62", runner_bid="0.60"), POLICY)
        self.assertEqual(result["status"], "BLOCKED_ALIGNMENT")
        self.assertEqual(result["market_alignment"]["status"], "BLOCKED_CONSENSUS_LEAD_INSUFFICIENT")

    def test_position_fact_invalidation_is_independent_from_new_entry(self) -> None:
        result = evaluate_paper_cycle(cycle(), POLICY)
        risk = result["position_risks"][0]["risk"]
        self.assertEqual(risk["risk_state"], "FACT_INVALIDATED")
        kinds = {action["action_type"] for action in risk["actions"]}
        self.assertIn("PAPER_EXIT_CANDIDATE", kinds)
        self.assertIn("PAPER_ROUTE_COMPARISON_REQUIRED", kinds)
        self.assertNotIn("PAPER_INDEPENDENT_NEW_ENTRY_CANDIDATE", kinds)


if __name__ == "__main__":
    unittest.main()
