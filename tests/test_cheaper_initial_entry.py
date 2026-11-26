"""Cheaper-side-first initial entry (≤ 35¢) — decision engine coverage."""

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
        initial_entry_threshold=0.35,
        initial_entry_direction=InitialEntryDirection.CHEAPER_SIDE,
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


class CheaperInitialEntryTests(unittest.TestCase):
    def test_up_35_qualifies(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.35, 0.65), _cfg(),
            phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "YES")

    def test_down_35_qualifies(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.65, 0.35), _cfg(),
            phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "NO")

    def test_up_34_qualifies(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.34, 0.66), _cfg(),
            phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "YES")

    def test_down_34_qualifies(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.66, 0.34), _cfg(),
            phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.side, "NO")

    def test_up_36_rejected(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.36, 0.64), _cfg(),
            phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertIn("36", act.reason)

    def test_down_36_rejected(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.64, 0.36), _cfg(),
            phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_up_cheap_hedge_down(self):
        ledger = PairedInventoryLedger()
        act = decide(ledger, _q(0.34, 0.66), _cfg(), seconds_left=250)
        self.assertEqual(act.side, "YES")
        ledger.record_buy("YES", 5, 0.34)
        _, max_down = max_hedge_prices(ledger, 0.98)
        self.assertAlmostEqual(max_down, 0.64, places=2)
        hedge = evaluate_hedge(ledger, _q(0.40, 0.64), _cfg(), phase=MarketPhase.EARLY)
        self.assertEqual(hedge.kind, ActionKind.BUY_HEDGE)
        self.assertEqual(hedge.side, "NO")

    def test_expensive_64_does_not_trigger_cheaper_mode(self):
        act = decide(PairedInventoryLedger(), _q(0.67, 0.66), _cfg(), seconds_left=250)
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_neither_qualifies(self):
        act = decide(PairedInventoryLedger(), _q(0.50, 0.50), _cfg(), seconds_left=250)
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_insufficient_depth(self):
        q = MarketQuoteView(0.34, 0.66, 0.33, 0.65, 2, 20, 2, 20)
        act = evaluate_initial_entry(
            PairedInventoryLedger(), q, _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertNotEqual(act.kind, ActionKind.BUY_ENTRY)

    def test_duplicate_initial_blocked(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        self.assertIsNone(
            evaluate_initial_entry(
                ledger, _q(0.30, 0.70), _cfg(), phase=MarketPhase.EARLY, seconds_left=250,
            )
        )

    def test_existing_position_survives_config_change(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.67)
        act = evaluate_hedge(
            ledger, _q(0.40, 0.31), _cfg(), phase=MarketPhase.EARLY,
        )
        self.assertEqual(act.kind, ActionKind.BUY_HEDGE)

    def test_picks_cheaper_when_both_eligible(self):
        act = evaluate_initial_entry(
            PairedInventoryLedger(), _q(0.35, 0.34), _cfg(),
            phase=MarketPhase.EARLY, seconds_left=250,
        )
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "NO")

    def test_fees_in_hedge(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        cfg = _cfg(fee_rate=0.01, slippage_reserve=0.002)
        hedge = evaluate_hedge(ledger, _q(0.40, 0.63), cfg, phase=MarketPhase.EARLY)
        self.assertEqual(hedge.kind, ActionKind.BUY_HEDGE)

    def test_partial_fill_hedge_cap(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 3, 0.34)
        _, max_down = max_hedge_prices(ledger, 0.98)
        self.assertAlmostEqual(max_down, 0.64, places=2)


if __name__ == "__main__":
    unittest.main()