"""Expensive-side-first initial entry (≥ 64¢) — decision engine coverage."""

from __future__ import annotations

import unittest

from utils.market_time_phases import MarketPhase, PhasePolicy
from utils.paired_decision_engine import (
    ActionKind,
    InitialEntryDirection,
    MarketQuoteView,
    PairedStrategyConfig,
    decide,
    evaluate_hedge,
    evaluate_initial_entry,
    max_hedge_prices,
)
from utils.paired_inventory_ledger import PairedInventoryLedger


def _q(yes_ask: float, no_ask: float, depth: float = 20.0) -> MarketQuoteView:
    return MarketQuoteView(
        yes_ask, no_ask, yes_ask - 0.01, no_ask - 0.01,
        depth, depth, depth, depth,
    )


def _cfg(**kwargs) -> PairedStrategyConfig:
    base = dict(
        initial_entry_threshold=0.64,
        initial_entry_direction=InitialEntryDirection.EXPENSIVE_SIDE,
        target_combined_cost=0.98,
        max_combined_cost=0.98,
        slippage_reserve=0.0,
        fee_rate=0.0,
        min_orderbook_depth=5.0,
        phase_policy=PhasePolicy(
            early_min_seconds_left=180,
            middle_min_seconds_left=60,
            stop_new_accumulation_seconds=90,
            force_exit_eval_seconds=45,
        ),
    )
    base.update(kwargs)
    return PairedStrategyConfig(**base)


class ExpensiveInitialEntryTests(unittest.TestCase):
    def test_up_64_qualifies(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.64, 0.36), _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "YES")

    def test_down_64_qualifies(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.36, 0.64), _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "NO")

    def test_up_63_rejected(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.63, 0.37), _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertIn("63", act.reason)

    def test_down_63_rejected_via_highest_side(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.37, 0.63), _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_up_67_hedge_side_down(self):
        ledger = PairedInventoryLedger()
        quotes = _q(0.67, 0.33)
        act = decide(ledger, quotes, _cfg(), seconds_left=250)
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "YES")
        ledger.record_buy("YES", 5, 0.67)
        max_up, max_down = max_hedge_prices(ledger, 0.98)
        self.assertAlmostEqual(max_down, 0.31, places=2)
        hedge = evaluate_hedge(
            ledger, _q(0.67, 0.31), _cfg(), phase=MarketPhase.EARLY,
        )
        self.assertEqual(hedge.kind, ActionKind.BUY_HEDGE)
        self.assertEqual(hedge.side, "NO")

    def test_down_68_hedge_side_up(self):
        ledger = PairedInventoryLedger()
        act = decide(ledger, _q(0.32, 0.68), _cfg(), seconds_left=250)
        self.assertEqual(act.side, "NO")
        ledger.record_buy("NO", 5, 0.68)
        _, max_down = max_hedge_prices(ledger, 0.98)
        max_up, _ = max_hedge_prices(ledger, 0.98)
        self.assertAlmostEqual(max_up, 0.30, places=2)

    def test_max_hedge_price_up_64_first_leg(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.64)
        _, max_down = max_hedge_prices(ledger, 0.98)
        self.assertAlmostEqual(max_down, 0.34, places=2)

    def test_fees_included_in_hedge_projected_cost(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.67)
        cfg = _cfg(fee_rate=0.01, slippage_reserve=0.002)
        hedge = evaluate_hedge(ledger, _q(0.70, 0.30), cfg, phase=MarketPhase.EARLY)
        self.assertEqual(hedge.kind, ActionKind.BUY_HEDGE)
        self.assertIsNotNone(hedge.projected_pair_cost)
        self.assertLessEqual(hedge.projected_pair_cost or 99, 0.98)

    def test_partial_first_leg_updates_hedge_cap(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 3, 0.67)
        _, max_down = max_hedge_prices(ledger, 0.98)
        self.assertAlmostEqual(max_down, 0.31, places=2)

    def test_insufficient_depth_blocks_entry(self):
        q = MarketQuoteView(0.67, 0.33, 0.66, 0.32, 2, 20, 2, 20)
        act = evaluate_initial_entry(
            PairedInventoryLedger(), q, _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_neither_side_64_no_entry(self):
        act = decide(PairedInventoryLedger(), _q(0.50, 0.50), _cfg(), seconds_left=250)
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_duplicate_initial_blocked_when_unmatched(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.67)
        act = evaluate_initial_entry(
            ledger, _q(0.70, 0.30), _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertIsNone(act)

    def test_existing_ledger_unchanged_after_config_change(self):
        ledger = PairedInventoryLedger.from_dict({
            "market_id": "m1",
            "lots": [{"side": "YES", "shares": 5, "execution_price": 0.34,
                      "total_cost": 1.7, "timestamp": 1.0, "lot_id": "a", "is_dca": False}],
        })
        self.assertAlmostEqual(ledger.unmatched_average_cost("YES"), 0.34)
        act = evaluate_hedge(
            ledger, _q(0.40, 0.64), _cfg(), phase=MarketPhase.EARLY,
        )
        self.assertEqual(act.kind, ActionKind.BUY_HEDGE)

    def test_picks_more_expensive_when_both_qualify(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.65, 0.66), _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "NO")

    def test_dca_path_unchanged_with_existing_unmatched(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.67)
        act = decide(ledger, _q(0.50, 0.48), _cfg(), seconds_left=200)
        self.assertIn(act.kind, (ActionKind.BUY_HEDGE, ActionKind.WAIT, ActionKind.REJECT, ActionKind.BUY_DCA))


if __name__ == "__main__":
    unittest.main()