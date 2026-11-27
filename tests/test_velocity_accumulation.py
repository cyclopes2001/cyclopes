"""Velocity-driven accumulation (unbounded side inventory)."""

from __future__ import annotations

import unittest

from utils.market_time_phases import MarketPhase
from utils.paired_decision_engine import (
    ActionKind,
    MarketQuoteView,
    PairedStrategyConfig,
    _inventory_cap_per_side,
    evaluate_velocity_driven_buy,
)
from utils.paired_inventory_ledger import PairedInventoryLedger
from utils.velocity_engine.models import SideMetrics, VelocityContext, VelocityTradeDecision


class VelocityAccumulationTests(unittest.TestCase):
    def test_unbounded_cap_when_zero(self):
        cfg = PairedStrategyConfig(velocity_max_inventory_per_side=0.0)
        self.assertGreater(_inventory_cap_per_side(cfg, velocity_mode=True), 1_000_000)

    def test_new_cycle_after_matched_pair(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.32)
        ledger.record_buy("NO", 5, 0.66)
        ledger.match_fifo(max_combined=0.99)
        ctx = VelocityContext(
            enabled=True,
            valid=True,
            stale=False,
            confidence=80,
            preferred_side="YES",
            decision=VelocityTradeDecision.BUY_UP,
            up=SideMetrics(),
            down=SideMetrics(),
        )
        quotes = MarketQuoteView(0.40, 0.60, 0.39, 0.59, 50, 50, 50, 50)
        cfg = PairedStrategyConfig(velocity_max_inventory_per_side=0.0)
        act = evaluate_velocity_driven_buy(
            ledger,
            quotes,
            cfg,
            phase=MarketPhase.MIDDLE,
            velocity=ctx,
            velocity_initial_max_price=0.90,
            seconds_left=200.0,
        )
        self.assertIsNotNone(act)
        assert act
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertIn("NEW_CYCLE", act.reason)

    def test_no_buy_when_unmatched_blocks_velocity_leg(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.32)
        ctx = VelocityContext(
            enabled=True,
            valid=True,
            preferred_side="YES",
            decision=VelocityTradeDecision.BUY_UP,
            up=SideMetrics(),
            down=SideMetrics(),
        )
        quotes = MarketQuoteView(0.40, 0.60, 0.39, 0.59, 50, 50, 50, 50)
        act = evaluate_velocity_driven_buy(
            ledger,
            quotes,
            PairedStrategyConfig(),
            phase=MarketPhase.MIDDLE,
            velocity=ctx,
            velocity_initial_max_price=0.90,
            seconds_left=200.0,
        )
        self.assertIsNone(act)


if __name__ == "__main__":
    unittest.main()