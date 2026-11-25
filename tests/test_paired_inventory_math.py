"""Unit tests for paired inventory ledger and hedge math."""

from __future__ import annotations

import unittest

from utils.paired_inventory_ledger import (
    PairedInventoryLedger,
    new_average_cost,
)
from utils.paired_decision_engine import (
    ActionKind,
    MarketQuoteView,
    PairedStrategyConfig,
    decide,
)
from utils.adaptive_dca import DcaDecision, evaluate_adaptive_dca
from utils.market_time_phases import MarketPhase, PhasePolicy, resolve_phase


class LedgerMathTests(unittest.TestCase):
    def test_new_average_cost_example(self):
        # 5 @ 0.34 then 5 @ 0.24 → avg 0.29
        avg = new_average_cost(5, 5 * 0.34, 5, 0.24)
        self.assertAlmostEqual(avg, 0.29)

    def test_max_hedge_price_from_unmatched_wac(self):
        ledger = PairedInventoryLedger(market_id="m1")
        ledger.record_buy("YES", 5, 0.34)
        self.assertAlmostEqual(ledger.max_hedge_price("YES", target_combined=0.98), 0.64)

    def test_dca_recalculates_hedge_threshold(self):
        ledger = PairedInventoryLedger(market_id="m1")
        ledger.record_buy("YES", 5, 0.34)
        ledger.record_buy("YES", 5, 0.24, is_dca=True)
        self.assertAlmostEqual(ledger.unmatched_average_cost("YES"), 0.29)
        self.assertAlmostEqual(ledger.max_hedge_price("YES", target_combined=0.98), 0.69)

    def test_fifo_matching_no_double_count(self):
        ledger = PairedInventoryLedger(market_id="m1")
        ledger.record_buy("YES", 5, 0.34)
        ledger.record_buy("NO", 5, 0.60)
        m = ledger.metrics()
        self.assertAlmostEqual(m.up_matched_shares, 5)
        self.assertAlmostEqual(m.down_matched_shares, 5)
        self.assertAlmostEqual(m.up_unmatched_shares, 0)
        self.assertAlmostEqual(m.down_unmatched_shares, 0)
        self.assertAlmostEqual(m.matched_pair_cost, 0.94)

    def test_partial_hedge_leaves_unmatched(self):
        ledger = PairedInventoryLedger(market_id="m1")
        ledger.record_buy("YES", 10, 0.30)
        ledger.record_buy("NO", 5, 0.60)
        m = ledger.metrics()
        self.assertAlmostEqual(m.up_matched_shares, 5)
        self.assertAlmostEqual(m.up_unmatched_shares, 5)
        self.assertAlmostEqual(m.down_unmatched_shares, 0)

    def test_idempotent_order_key(self):
        ledger = PairedInventoryLedger(market_id="m1")
        a = ledger.record_buy("YES", 5, 0.34, order_id="oid-1")
        b = ledger.record_buy("YES", 5, 0.34, order_id="oid-1")
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.assertAlmostEqual(ledger.total_shares("YES"), 5)


class AdaptiveDcaTests(unittest.TestCase):
    def test_reject_when_opposite_stays_expensive(self):
        # Spec scenario 2: UP 34→24, DOWN 76 → projected 1.05
        ev = evaluate_adaptive_dca(
            side="YES",
            existing_shares=5,
            existing_total_cost=5 * 0.34,
            proposed_qty=5,
            proposed_price=0.24,
            opposite_ask=0.76,
            target_combined=0.98,
            max_inventory=15,
            unmatched_target=5,
            max_dca_legs=3,
            current_dca_legs=0,
            available_liquidity=10,
            seconds_left=200,
            min_seconds_left=90,
        )
        self.assertEqual(ev.decision, DcaDecision.REJECT)
        self.assertAlmostEqual(ev.new_avg, 0.29)
        self.assertGreater(ev.projected_pair_cost or 0, 0.98)

    def test_approve_when_hedge_becomes_feasible(self):
        ev = evaluate_adaptive_dca(
            side="YES",
            existing_shares=5,
            existing_total_cost=5 * 0.34,
            proposed_qty=5,
            proposed_price=0.24,
            opposite_ask=0.64,
            target_combined=0.98,
            max_inventory=15,
            unmatched_target=5,
            max_dca_legs=3,
            current_dca_legs=0,
            available_liquidity=10,
            seconds_left=200,
            min_seconds_left=90,
            slippage_reserve=0.0,
        )
        self.assertEqual(ev.decision, DcaDecision.APPROVE)
        self.assertLessEqual(ev.projected_pair_cost or 99, 0.98)


class DecisionEngineTests(unittest.TestCase):
    def _cfg(self) -> PairedStrategyConfig:
        return PairedStrategyConfig(
            initial_entry_threshold=0.64,
            target_combined_cost=0.98,
            max_combined_cost=0.98,
            phase_policy=PhasePolicy(
                early_min_seconds_left=180,
                middle_min_seconds_left=60,
                stop_new_accumulation_seconds=90,
                force_exit_eval_seconds=45,
            ),
            slippage_reserve=0.0,
        )

    def test_initial_entry_picks_expensive_side(self):
        ledger = PairedInventoryLedger()
        quotes = MarketQuoteView(0.33, 0.67, 0.32, 0.66, 20, 20, 20, 20)
        act = decide(ledger, quotes, self._cfg(), seconds_left=250)
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "NO")

    def test_hedge_approved_at_threshold(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.67)
        quotes = MarketQuoteView(0.40, 0.31, 0.39, 0.30, 20, 20, 20, 20)
        act = decide(ledger, quotes, self._cfg(), seconds_left=200)
        self.assertEqual(act.kind, ActionKind.BUY_HEDGE)
        self.assertEqual(act.side, "NO")

    def test_hedge_rejected_when_too_expensive(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.67)
        quotes = MarketQuoteView(0.40, 0.76, 0.39, 0.75, 20, 20, 20, 20)
        act = decide(ledger, quotes, self._cfg(), seconds_left=200)
        self.assertIn(act.kind, (ActionKind.REJECT, ActionKind.WAIT))
        self.assertTrue(
            "HEDGE REJECTED" in act.reason or "DCA" in act.reason or "WAIT" in act.reason
        )

    def test_late_phase_blocks_new_entry(self):
        ledger = PairedInventoryLedger()
        quotes = MarketQuoteView(0.30, 0.70, 0.29, 0.69, 20, 20, 20, 20)  # UP cheap — no new entry in late phase
        act = decide(ledger, quotes, self._cfg(), seconds_left=50)
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_phase_resolution(self):
        policy = PhasePolicy()
        self.assertEqual(resolve_phase(250, policy), MarketPhase.EARLY)
        self.assertEqual(resolve_phase(120, policy), MarketPhase.MIDDLE)
        self.assertEqual(resolve_phase(30, policy), MarketPhase.LATE)
        self.assertEqual(resolve_phase(0, policy), MarketPhase.CLOSED)


if __name__ == "__main__":
    unittest.main()
