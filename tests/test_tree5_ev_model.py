from __future__ import annotations

import json
import unittest
from pathlib import Path

from tree5_ev_model import evaluate_paper_entry, evaluate_taf_market_alignment, ev_net_lower_bound

NS = 1_000_000_000
POLICY = json.loads((Path(__file__).resolve().parents[1] / "tree5_ev_policy.example.json").read_text(encoding="utf-8"))


def snapshot(bucket: str, token: str, timestamp_seconds: int, bid: str, ask: str, bid_size: str = "100", ask_size: str = "10") -> dict:
    return {
        "ready": True, "received_monotonic_ns": timestamp_seconds * NS, "market_id": "shanghai-high-2026-08-27",
        "bucket_id": bucket, "token_id": token, "tick_size": "0.01", "min_order_size": "1",
        "book_hash": f"{bucket}-{timestamp_seconds}", "bids": [{"price": bid, "size": bid_size}], "asks": [{"price": ask, "size": ask_size}],
    }


class Tree5EVModelTests(unittest.TestCase):
    t0 = 10_000 * NS
    buckets = ["b29", "b30", "b31"]

    def complete_window(self, *, leader_bid: str = "0.72", runner_bid: str = "0.60") -> list[dict]:
        values: list[dict] = []
        # 90 minutes coverage + final data within 2 minutes meet the 120m/75%/45 snapshot policy.
        for second in range(2_800, 10_000, 150):
            values.extend([
                snapshot("b29", "token-29", second, "0.20", "0.22"),
                snapshot("b30", "token-30", second, leader_bid, "0.74"),
                snapshot("b31", "token-31", second, runner_bid, "0.62"),
            ])
        return values

    def alignment(self, snapshots: list[dict] | None = None) -> dict:
        return evaluate_taf_market_alignment(
            taf_token_id="token-30", market_id="shanghai-high-2026-08-27", bucket_ids=self.buckets,
            snapshots=snapshots if snapshots is not None else self.complete_window(), t0_monotonic_ns=self.t0, policy=POLICY,
        )

    def test_latest_taf_bucket_must_lead_by_absolute_and_relative_margin(self) -> None:
        alignment = self.alignment()
        self.assertEqual(alignment["status"], "PAPER_ALIGNMENT_READY")
        self.assertEqual(alignment["taf_bucket_id"], "b30")
        self.assertEqual(alignment["absolute_lead"], "0.12")
        self.assertEqual(alignment["relative_lead"], "0.2")
        self.assertEqual(alignment["orders_submitted"], 0)

    def test_twenty_percent_without_five_point_absolute_margin_blocks(self) -> None:
        alignment = self.alignment(self.complete_window(leader_bid="0.24", runner_bid="0.20"))
        self.assertEqual(alignment["status"], "BLOCKED_CONSENSUS_LEAD_INSUFFICIENT")

    def test_all_market_buckets_need_recorded_pre_t0_l2(self) -> None:
        incomplete = [item for item in self.complete_window() if item["bucket_id"] != "b29"]
        alignment = self.alignment(incomplete)
        self.assertEqual(alignment["status"], "BLOCKED_INCOMPLETE_MARKET_COVERAGE")
        self.assertIn("b29", alignment["unavailable_bucket_ids"])

    def test_taf_must_identify_market_leader(self) -> None:
        wrong_taf = evaluate_taf_market_alignment(
            taf_token_id="token-31", market_id="shanghai-high-2026-08-27", bucket_ids=self.buckets,
            snapshots=self.complete_window(), t0_monotonic_ns=self.t0, policy=POLICY,
        )
        self.assertEqual(wrong_taf["status"], "BLOCKED_TAF_NOT_MARKET_LEADER")

    def test_paper_ev_requires_positive_lower_bound_with_oos_evidence(self) -> None:
        alignment = self.alignment()
        entry = snapshot("b30", "token-30", 10_003, "0.73", "0.75", ask_size="10")
        result = evaluate_paper_entry(
            alignment=alignment, entry_snapshot=entry,
            calibration={"p_lower": "0.82", "q_fill": "0.80", "oos_sample_count": 120},
            costs={"entry_fee_full_fill": "0.01", "expected_exit_cost": "0.04", "latency_slippage_reserve": "0.02"},
            policy=POLICY, t0_monotonic_ns=self.t0,
        )
        self.assertEqual(result["status"], "PAPER_ENTRY_READY")
        self.assertEqual(result["entry"]["vwap_ask"], "0.75")
        self.assertGreater(float(result["ev"]["ev_net_lower_usdc"]), 0)
        self.assertEqual(result["orders_submitted"], 0)

    def test_no_calibration_sample_or_nonpositive_ev_blocks_entry(self) -> None:
        too_few = ev_net_lower_bound(
            p_lower="0.99", calibration_oos_sample_count=99, q_fill="1", target_shares="5", executable_vwap_ask="0.50",
            entry_fee_full_fill="0.01", expected_exit_cost="0.01", latency_slippage_reserve="0.02", policy=POLICY,
        )
        self.assertEqual(too_few["status"], "BLOCKED_INSUFFICIENT_OOS_CALIBRATION")
        alignment = self.alignment()
        poor = evaluate_paper_entry(
            alignment=alignment, entry_snapshot=snapshot("b30", "token-30", 10_003, "0.70", "0.90"),
            calibration={"p_lower": "0.80", "q_fill": "1", "oos_sample_count": 120},
            costs={"entry_fee_full_fill": "0.01", "expected_exit_cost": "0.04", "latency_slippage_reserve": "0.02"},
            policy=POLICY, t0_monotonic_ns=self.t0,
        )
        self.assertEqual(poor["status"], "BLOCKED_EV_LOWER_BOUND_NONPOSITIVE")

    def test_post_signal_entry_snapshot_must_arrive_within_five_seconds(self) -> None:
        result = evaluate_paper_entry(
            alignment=self.alignment(), entry_snapshot=snapshot("b30", "token-30", 10_006, "0.72", "0.75"),
            calibration={"p_lower": "0.90", "q_fill": "1", "oos_sample_count": 120},
            costs={"entry_fee_full_fill": "0.01", "expected_exit_cost": "0.01", "latency_slippage_reserve": "0.02"},
            policy=POLICY, t0_monotonic_ns=self.t0,
        )
        self.assertEqual(result["status"], "BLOCKED_POST_SIGNAL_ENTRY_DELAY")


if __name__ == "__main__":
    unittest.main()
