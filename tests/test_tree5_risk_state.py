from __future__ import annotations

import json
import unittest
from pathlib import Path

from tree5_risk_state import plan_position_risk

POLICY = json.loads((Path(__file__).resolve().parents[1] / "tree5_ev_policy.example.json").read_text(encoding="utf-8"))


def position(*, direction: str = "high", bucket: dict | None = None, shares: str = "5", pending: bool = True) -> dict:
    return {
        "position_id": "position-1", "market_id": "shanghai-high", "direction": direction,
        "bucket": bucket or {"bucket_id": "b30", "lo": 30, "hi": 31}, "token_id": "yes-b30",
        "confirmed_shares": shares, "pending_entry_id": "entry-1" if pending else None,
    }


def consensus(*, leader: str = "b31", held_price: str = "0.50", leader_price: str = "0.70", complete: bool = True) -> dict:
    return {
        "status": "PAPER_MARKET_CONSENSUS_READY", "complete_market_coverage": complete, "leader_bucket_id": leader,
        "bucket_prices": {"b29": "0.20", "b30": held_price, "b31": leader_price},
    }


class Tree5RiskStateTests(unittest.TestCase):
    def test_fact_invalidated_has_highest_priority_and_separate_exit_actions(self) -> None:
        result = plan_position_risk(position=position(), observed_extreme="31", consensus=consensus(), time_closure_triggered=True, alternative_entry={"status": "PAPER_ENTRY_READY"}, policy=POLICY)
        self.assertEqual(result["risk_state"], "FACT_INVALIDATED")
        action_types = {action["action_type"] for action in result["actions"]}
        self.assertEqual(action_types, {"PAPER_STOP_NEW_ENTRIES", "PAPER_CANCEL_CANDIDATE", "PAPER_EXIT_CANDIDATE", "PAPER_ROUTE_COMPARISON_REQUIRED"})
        self.assertNotIn("PAPER_INDEPENDENT_NEW_ENTRY_CANDIDATE", action_types)
        exit_action = next(action for action in result["actions"] if action["action_type"] == "PAPER_EXIT_CANDIDATE")
        self.assertEqual(exit_action["status"], "PAPER_EXIT_FACT_INVALIDATED")
        self.assertEqual(exit_action["confirmed_shares"], "5")
        self.assertEqual(result["orders_submitted"], 0)

    def test_low_bucket_boundary_is_strict(self) -> None:
        low = position(direction="low", bucket={"bucket_id": "l27", "lo": 27, "hi": 28})
        still_possible = plan_position_risk(position=low, observed_extreme="27", consensus=None, time_closure_triggered=False, alternative_entry=None, policy=POLICY)
        self.assertEqual(still_possible["risk_state"], "ACTIVE")
        invalid = plan_position_risk(position=low, observed_extreme="26.9", consensus=None, time_closure_triggered=False, alternative_entry=None, policy=POLICY)
        self.assertEqual(invalid["risk_state"], "FACT_INVALIDATED")

    def test_thirty_percent_consensus_reversal_cancels_and_exits_without_new_evidence(self) -> None:
        result = plan_position_risk(position=position(), observed_extreme="30.5", consensus=consensus(held_price="0.50", leader_price="0.65"), time_closure_triggered=False, alternative_entry=None, policy=POLICY)
        self.assertEqual(result["risk_state"], "CONSENSUS_REVERSAL")
        self.assertEqual(result["consensus"]["relative_lead"], "0.3")
        self.assertIn("PAPER_CANCEL_CANDIDATE", {action["action_type"] for action in result["actions"]})
        self.assertIn("PAPER_EXIT_CANDIDATE", {action["action_type"] for action in result["actions"]})

    def test_time_closure_upgrades_a_confirmed_consensus_reversal(self) -> None:
        result = plan_position_risk(position=position(), observed_extreme="30.5", consensus=consensus(), time_closure_triggered=True, alternative_entry=None, policy=POLICY)
        self.assertEqual(result["risk_state"], "TIME_CLOSURE_AND_CONSENSUS_REVERSAL")

    def test_incomplete_or_weak_consensus_cannot_trigger_exit(self) -> None:
        incomplete = plan_position_risk(position=position(), observed_extreme="30.5", consensus=consensus(complete=False), time_closure_triggered=True, alternative_entry=None, policy=POLICY)
        self.assertEqual(incomplete["risk_state"], "ACTIVE")
        weak = plan_position_risk(position=position(), observed_extreme="30.5", consensus=consensus(held_price="0.60", leader_price="0.70"), time_closure_triggered=False, alternative_entry=None, policy=POLICY)
        self.assertEqual(weak["risk_state"], "ACTIVE")

    def test_no_confirmed_shares_means_no_exit_candidate(self) -> None:
        result = plan_position_risk(position=position(shares="0"), observed_extreme="31", consensus=None, time_closure_triggered=False, alternative_entry=None, policy=POLICY)
        self.assertEqual(result["risk_state"], "FACT_INVALIDATED")
        self.assertNotIn("PAPER_EXIT_CANDIDATE", {action["action_type"] for action in result["actions"]})

    def test_alternative_needs_independent_ev_and_is_not_atomic_switch(self) -> None:
        active = plan_position_risk(position=position(pending=False), observed_extreme="30.5", consensus=None, time_closure_triggered=False, alternative_entry={"status": "PAPER_ENTRY_READY", "token_id": "new"}, policy=POLICY)
        actions = [action for action in active["actions"] if action["action_type"] == "PAPER_INDEPENDENT_NEW_ENTRY_CANDIDATE"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["status"], "PAPER_NEW_ENTRY_REQUIRES_SEPARATE_RECONCILIATION")
        self.assertNotIn("switch", str(actions[0]).lower())


if __name__ == "__main__":
    unittest.main()
