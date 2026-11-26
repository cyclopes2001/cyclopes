"""Unmatched inventory cap and settlement risk math."""

from __future__ import annotations

import unittest

from utils.market_time_phases import MarketPhase, PhasePolicy
from utils.paired_decision_engine import (
    ActionKind,
    InitialEntryDirection,
    MarketQuoteView,
    PairedStrategyConfig,
    decide,
    evaluate_dca_action,
)
from utils.paired_inventory_ledger import PairedInventoryLedger
from utils.settlement_risk import (
    projected_unmatched,
    projected_unmatched_with_pending,
    settlement_outcomes,
    verify_sample_settlement_loss,
)


def _cfg(**kwargs) -> PairedStrategyConfig:
    base = dict(
        initial_entry_threshold=0.35,
        initial_entry_direction=InitialEntryDirection.CHEAPER_SIDE,
        target_combined_cost=0.98,
        max_inventory_per_side=15.0,
        max_unmatched_shares_per_side=5.0,
        unmatched_inventory_target=5.0,
        dca_increment=5.0,
        min_order_size=5.0,
        phase_policy=PhasePolicy(
            stop_initial_entry_seconds=120,
            stop_unmatched_dca_seconds=90,
            stop_new_accumulation_seconds=90,
        ),
    )
    base.update(kwargs)
    return PairedStrategyConfig(**base)


class UnmatchedCapTests(unittest.TestCase):
    def test_sample_settlement_loss(self):
        self.assertTrue(verify_sample_settlement_loss())

    def test_yes_wins_scenario(self):
        o = settlement_outcomes(yes_shares=15, no_shares=10, total_cost=11.15)
        self.assertAlmostEqual(o.pnl_if_yes_wins, 3.85, places=2)

    def test_dca_blocked_at_five_unmatched_yes(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        q = MarketQuoteView(0.24, 0.76, 0.23, 0.75, 20, 20, 20, 20)
        act = evaluate_dca_action(
            ledger, q, _cfg(), phase=MarketPhase.EARLY, seconds_left=200,
        )
        self.assertEqual(act.kind, ActionKind.REJECT)
        self.assertIn("unmatched cap", act.reason.lower())

    def test_hedge_still_allowed_at_cap(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        q = MarketQuoteView(0.40, 0.63, 0.39, 0.62, 20, 20, 20, 20)
        act = decide(ledger, q, _cfg(), seconds_left=200)
        self.assertEqual(act.kind, ActionKind.BUY_HEDGE)

    def test_dca_blocked_below_90s(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        q = MarketQuoteView(0.24, 0.76, 0.23, 0.75, 20, 20, 20, 20)
        act = evaluate_dca_action(
            ledger, q, _cfg(), phase=MarketPhase.MIDDLE, seconds_left=85,
        )
        self.assertEqual(act.kind, ActionKind.REJECT)
        self.assertIn("DCA stopped", act.reason)

    def test_projected_unmatched_includes_pending(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        um_y, um_n = projected_unmatched_with_pending(
            ledger, "YES", 5.0, pending_side="YES", pending_remaining_qty=5.0,
        )
        self.assertGreater(um_y, 5.0)

    def test_initial_entry_stopped_at_120s(self):
        q = MarketQuoteView(0.34, 0.66, 0.33, 0.65, 20, 20, 20, 20)
        act = decide(PairedInventoryLedger(), q, _cfg(), seconds_left=100)
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_no_dca_bypasses_combined_cost(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        q = MarketQuoteView(0.24, 0.76, 0.23, 0.75, 20, 20, 20, 20)
        act = decide(ledger, q, _cfg(), seconds_left=200)
        self.assertNotEqual(act.kind, ActionKind.BUY_DCA)

    def test_cannot_stack_to_fifteen_unmatched(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        q = MarketQuoteView(0.24, 0.76, 0.23, 0.75, 20, 20, 20, 20)
        act = evaluate_dca_action(
            ledger, q, _cfg(), phase=MarketPhase.EARLY, seconds_left=200,
        )
        self.assertEqual(act.kind, ActionKind.REJECT)
        self.assertLessEqual(ledger.unmatched_shares("YES"), 5.0 + 1e-9)


if __name__ == "__main__":
    unittest.main()