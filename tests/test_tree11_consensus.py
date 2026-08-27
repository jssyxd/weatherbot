from __future__ import annotations

import unittest

from tree11_consensus import evaluate_price_depth_consensus


NS = 1_000_000_000


def policy() -> dict:
    return {
        "consensus": {
            "lookback_window_seconds": 100,
            "minimum_snapshot_count": 3,
            "maximum_final_snapshot_age_seconds": 20,
            "minimum_coverage_ratio": "0.75",
            "old_bucket_required_rank": 1,
            "minimum_price_lead": "0.02",
            "minimum_old_bucket_depth_usdc": "10",
            "minimum_old_bucket_depth_share_multiple": "3",
        },
        "new_bucket_entry": {
            "target_shares": "5", "maximum_book_age_seconds": 20, "maximum_entry_delay_seconds": 5,
            "pricing_profiles": [
                {"id": "best", "limit_kind": "best_ask", "maximum_price": "0.95"},
                {"id": "plus-one", "limit_kind": "best_ask_plus_ticks", "ticks": 1, "maximum_price": "0.95"},
            ],
        },
    }


def signal() -> dict:
    return {
        "signal_id": "signal-1", "status": "PENDING_CONSENSUS", "old_market_rule_id": "rule-high", "market_rule_id": "rule-high",
        "old_bucket": {"bucket_id": "old"}, "new_bucket": {"bucket_id": "new"},
        "market_bucket_ids": ["old", "new", "other"],
    }


def snapshot(bucket: str, timestamp_seconds: int, bid: str, ask: str, bid_shares: str = "20", ask_shares: str = "20") -> dict:
    return {
        "ready": True, "received_monotonic_ns": timestamp_seconds * NS, "market_rule_id": "rule-high",
        "bucket_id": bucket, "token_id": f"token-{bucket}", "tick_size": "0.01", "min_order_size": "5",
        "book_hash": f"hash-{bucket}-{timestamp_seconds}",
        "bids": [{"price": bid, "size": bid_shares}], "asks": [{"price": ask, "size": ask_shares}],
    }


class Tree11ConsensusTests(unittest.TestCase):
    def all_snapshots(self, *, new_ask_shares: str = "20") -> list[dict]:
        result = []
        for timestamp in (9900, 9950, 9990):
            result.extend([
                snapshot("old", timestamp, "0.70", "0.72"),
                snapshot("new", timestamp, "0.20", "0.22", ask_shares=new_ask_shares),
                snapshot("other", timestamp, "0.40", "0.42"),
            ])
        return result

    def entry_snapshot(self, *, at_seconds: int = 10_001, ask_shares: str = "20") -> dict:
        return snapshot("new", at_seconds, "0.20", "0.22", ask_shares=ask_shares)

    def test_old_bucket_requires_price_and_depth_consensus_then_makes_paper_profiles(self) -> None:
        result = evaluate_price_depth_consensus(signal(), self.all_snapshots(), policy(), 10_000 * NS, self.entry_snapshot())
        self.assertEqual(result["status"], "PAPER_INTENT_READY")
        self.assertEqual(result["old_bucket_rank"], 1)
        self.assertEqual(result["old_bucket_price_lead"], "0.30")
        self.assertEqual(len(result["new_bucket_profiles"]), 2)
        self.assertTrue(all(profile["status"] == "PAPER_INTENT_READY" for profile in result["new_bucket_profiles"]))
        self.assertEqual(result["new_bucket_profiles"][0]["limit_price"], "0.22")
        self.assertEqual(result["new_bucket_profiles"][1]["limit_price"], "0.23")
        self.assertEqual(result["orders_submitted"], 0)

    def test_missing_any_market_bucket_snapshot_blocks_rank_claim(self) -> None:
        incomplete = [item for item in self.all_snapshots() if item["bucket_id"] != "other"]
        result = evaluate_price_depth_consensus(signal(), incomplete, policy(), 10_000 * NS)
        self.assertEqual(result["status"], "BLOCKED_INCOMPLETE_CONSENSUS_COVERAGE")
        self.assertIn("other", result["missing_or_unready_bucket_ids"])

    def test_thin_new_ask_blocks_quick_paper_fill_claim(self) -> None:
        result = evaluate_price_depth_consensus(signal(), self.all_snapshots(), policy(), 10_000 * NS, self.entry_snapshot(ask_shares="4.99"))
        self.assertEqual(result["status"], "BLOCKED_NEW_BUCKET_EXECUTION")
        self.assertTrue(all(profile["status"] == "BLOCKED_INSUFFICIENT_VISIBLE_ASK_DEPTH" for profile in result["new_bucket_profiles"]))

    def test_pre_signal_or_slow_entry_quote_is_rejected(self) -> None:
        result = evaluate_price_depth_consensus(signal(), self.all_snapshots(), policy(), 10_000 * NS, self.entry_snapshot(at_seconds=10_006))
        self.assertEqual(result["status"], "BLOCKED_POST_SIGNAL_ENTRY_DELAY")
        pre_signal = evaluate_price_depth_consensus(signal(), self.all_snapshots(), policy(), 10_000 * NS, self.entry_snapshot(at_seconds=9_999))
        self.assertEqual(pre_signal["status"], "BLOCKED_POST_SIGNAL_ENTRY_DELAY")

    def test_old_price_leader_without_depth_is_rejected(self) -> None:
        thin_old = self.all_snapshots()
        for item in thin_old:
            if item["bucket_id"] == "old":
                item["bids"] = [{"price": "0.70", "size": "1"}]
        result = evaluate_price_depth_consensus(signal(), thin_old, policy(), 10_000 * NS)
        self.assertEqual(result["status"], "BLOCKED_OLD_BUCKET_INSUFFICIENT_DEPTH_CONSENSUS")

    def test_stale_final_snapshot_blocks_even_if_prices_are_strong(self) -> None:
        stale = []
        for timestamp in (9800, 9850, 9900):
            stale.extend([snapshot("old", timestamp, "0.70", "0.72"), snapshot("new", timestamp, "0.20", "0.22"), snapshot("other", timestamp, "0.40", "0.42")])
        result = evaluate_price_depth_consensus(signal(), stale, policy(), 10_000 * NS)
        self.assertEqual(result["status"], "BLOCKED_INCOMPLETE_CONSENSUS_COVERAGE")
        self.assertIn("old", result["missing_or_unready_bucket_ids"])


if __name__ == "__main__":
    unittest.main()
