"""Hedge timeout exit tests."""

from __future__ import annotations

import unittest

from utils.market_time_phases import MarketPhase, PhasePolicy
from utils.paired_decision_engine import (
    ActionKind,
    MarketQuoteView,
    PairedStrategyConfig,
    decide,
    evaluate_hedge_timeout_exit,
)
from utils.paired_inventory_ledger import PairedInventoryLedger


def _cfg(timeout: float = 20.0) -> PairedStrategyConfig:
    return PairedStrategyConfig(
        hedge_timeout_seconds=timeout,
        phase_policy=PhasePolicy(),
    )


class HedgeTimeoutTests(unittest.TestCase):
    def test_timeout_triggers_exit_after_20s(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.32)
        quotes = MarketQuoteView(
            yes_ask=0.35,
            no_ask=0.66,
            yes_bid=0.31,
            no_bid=0.65,
            yes_ask_size=50,
            no_ask_size=50,
            yes_bid_size=50,
            no_bid_size=50,
        )
        act = evaluate_hedge_timeout_exit(
            ledger,
            quotes,
            _cfg(20),
            phase=MarketPhase.MIDDLE,
            unmatched_since_mono=100.0,
            now_mono=121.0,
            hedge_locked=False,
        )
        self.assertIsNotNone(act)
        assert act
        self.assertEqual(act.kind, ActionKind.EXIT)
        self.assertEqual(act.side, "YES")

    def test_timeout_blocked_when_hedge_locked(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.32)
        ledger.record_buy("NO", 5, 0.66)
        ledger.match_fifo(max_combined=0.99)
        quotes = MarketQuoteView(0.35, 0.66, 0.31, 0.65, 50, 50, 50, 50)
        act = evaluate_hedge_timeout_exit(
            ledger,
            quotes,
            _cfg(20),
            phase=MarketPhase.MIDDLE,
            unmatched_since_mono=100.0,
            now_mono=121.0,
            hedge_locked=True,
        )
        self.assertIsNone(act)

    def test_decide_prioritizes_timeout_before_dca(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.32)
        quotes = MarketQuoteView(0.30, 0.70, 0.29, 0.69, 50, 50, 50, 50)
        action = decide(
            ledger,
            quotes,
            _cfg(20),
            seconds_left=200.0,
            hedge_timeout_since_mono=50.0,
            hedge_locked=False,
            now_mono=75.0,
        )
        self.assertEqual(action.kind, ActionKind.EXIT)


if __name__ == "__main__":
    unittest.main()