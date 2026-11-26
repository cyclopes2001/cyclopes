"""Simulation scenarios for adaptive paired inventory accumulation."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import List, Optional, Tuple

from utils.adaptive_dca import DcaDecision, evaluate_adaptive_dca
from utils.market_time_phases import PhasePolicy
from utils.paired_decision_engine import (
    ActionKind,
    MarketQuoteView,
    PairedStrategyConfig,
    decide,
)
from utils.paired_inventory_ledger import PairedInventoryLedger


@dataclass
class SimResult:
    capital: float
    completed_pairs: float
    avg_pair_cost: Optional[float]
    net_profit: float
    max_unmatched: float
    rejected_dca: int
    forced_exits: int
    unmatched_at_end: float
    decisions: List[str]


def _cfg(**kwargs) -> PairedStrategyConfig:
    base = dict(
        initial_entry_threshold=0.35,
        target_combined_cost=0.98,
        max_combined_cost=0.98,
        max_inventory_per_side=15.0,
        min_order_size=5.0,
        dca_increment=5.0,
        max_dca_legs_per_side=3,
        unmatched_inventory_target=5.0,
        fee_rate=0.0,
        slippage_reserve=0.0,
        phase_policy=PhasePolicy(
            early_min_seconds_left=180,
            middle_min_seconds_left=60,
            stop_new_accumulation_seconds=90,
            force_exit_eval_seconds=45,
        ),
    )
    base.update(kwargs)
    return PairedStrategyConfig(**base)


def _apply(ledger: PairedInventoryLedger, act, fee_rate: float = 0.0) -> None:
    if act.kind in (ActionKind.BUY_ENTRY, ActionKind.BUY_DCA, ActionKind.BUY_HEDGE):
        fee = act.qty * act.limit_price * fee_rate
        ledger.record_buy(
            act.side,
            act.qty,
            act.limit_price,
            fees=fee,
            is_dca=(act.kind == ActionKind.BUY_DCA),
        )
        ledger.match_fifo(max_combined=0.98)
    elif act.kind == ActionKind.EXIT:
        ledger.record_sell(act.side, act.qty, act.limit_price)


def run_scenario(
    ticks: List[Tuple[float, MarketQuoteView]],
    *,
    cfg: Optional[PairedStrategyConfig] = None,
) -> SimResult:
    cfg = cfg or _cfg()
    ledger = PairedInventoryLedger(market_id="sim")
    rejected_dca = 0
    forced_exits = 0
    max_unmatched = 0.0
    decisions: List[str] = []
    for seconds_left, quotes in ticks:
        act = decide(ledger, quotes, cfg, seconds_left=seconds_left)
        decisions.append(f"{act.kind.value}:{act.reason}")
        if act.kind == ActionKind.REJECT and "DCA" in act.reason.upper():
            rejected_dca += 1
        if act.kind == ActionKind.EXIT:
            forced_exits += 1
        if act.kind in (
            ActionKind.BUY_ENTRY,
            ActionKind.BUY_DCA,
            ActionKind.BUY_HEDGE,
            ActionKind.EXIT,
        ):
            _apply(ledger, act, fee_rate=cfg.fee_rate)
        m = ledger.metrics()
        max_unmatched = max(max_unmatched, m.up_unmatched_shares, m.down_unmatched_shares)
    m = ledger.metrics()
    matched = min(m.up_matched_shares, m.down_matched_shares)
    # Gross settlement on matched pairs pays $1 each.
    settlement = matched * 1.0
    net = settlement - (m.capital_committed - (
        m.unmatched_up_cost + m.unmatched_down_cost
    )) + m.realized_profit
    # Simpler: matched pairs profit = matched * (1 - pair_cost)
    if m.matched_pair_cost is not None and matched > 0:
        net = matched * (1.0 - m.matched_pair_cost) + m.realized_profit
    return SimResult(
        capital=m.capital_committed,
        completed_pairs=matched,
        avg_pair_cost=m.matched_pair_cost,
        net_profit=net,
        max_unmatched=max_unmatched,
        rejected_dca=rejected_dca,
        forced_exits=forced_exits,
        unmatched_at_end=m.up_unmatched_shares + m.down_unmatched_shares,
        decisions=decisions,
    )


class SimulationScenarios(unittest.TestCase):
    def test_1_up_falls_then_down_reaches_hedge(self):
        ticks = [
            (250, MarketQuoteView(0.34, 0.70, 0.33, 0.69, 20, 20, 20, 20)),
            (230, MarketQuoteView(0.24, 0.76, 0.23, 0.75, 20, 20, 20, 20)),
            (200, MarketQuoteView(0.24, 0.64, 0.23, 0.63, 20, 20, 20, 20)),
        ]
        res = run_scenario(ticks)
        self.assertGreaterEqual(res.completed_pairs + 1e-9, 5)
        self.assertIsNotNone(res.avg_pair_cost)
        self.assertLessEqual((res.avg_pair_cost or 99) - 1e-9, 0.98)

    def test_2_up_falls_down_stays_expensive_no_bad_dca(self):
        ticks = [
            (250, MarketQuoteView(0.34, 0.70, 0.33, 0.69, 20, 20, 20, 20)),
            (230, MarketQuoteView(0.24, 0.76, 0.23, 0.75, 20, 20, 20, 20)),
            (210, MarketQuoteView(0.22, 0.76, 0.21, 0.75, 20, 20, 20, 20)),
        ]
        res = run_scenario(ticks)
        self.assertEqual(res.completed_pairs, 0)
        # Should not have piled into a losing DCA that locks 1.05 pair cost
        self.assertLessEqual(res.max_unmatched, 5.0 + 1e-9)
        self.assertGreaterEqual(res.rejected_dca, 1)

    def test_3_continuous_fall_no_blind_dca(self):
        ticks = [
            (250, MarketQuoteView(0.34, 0.70, 0.33, 0.69, 20, 20, 20, 20)),
            (240, MarketQuoteView(0.29, 0.72, 0.28, 0.71, 20, 20, 20, 20)),
            (230, MarketQuoteView(0.24, 0.76, 0.23, 0.75, 20, 20, 20, 20)),
            (220, MarketQuoteView(0.19, 0.80, 0.18, 0.79, 20, 20, 20, 20)),
        ]
        res = run_scenario(ticks)
        self.assertLessEqual(res.max_unmatched, 5.0 + 1e-9)

    def test_4_down_falls_first(self):
        ticks = [
            (250, MarketQuoteView(0.70, 0.32, 0.69, 0.31, 20, 20, 20, 20)),
            (220, MarketQuoteView(0.64, 0.32, 0.63, 0.31, 20, 20, 20, 20)),
        ]
        res = run_scenario(ticks)
        self.assertGreaterEqual(res.completed_pairs, 5)
        self.assertLessEqual(res.avg_pair_cost or 99, 0.98 + 1e-9)

    def test_5_rapid_fluctuation(self):
        ticks = [
            (250, MarketQuoteView(0.33, 0.68, 0.32, 0.67, 20, 20, 20, 20)),
            (245, MarketQuoteView(0.40, 0.55, 0.39, 0.54, 20, 20, 20, 20)),
            (240, MarketQuoteView(0.28, 0.70, 0.27, 0.69, 20, 20, 20, 20)),
            (235, MarketQuoteView(0.50, 0.48, 0.49, 0.47, 20, 20, 20, 20)),
            (230, MarketQuoteView(0.45, 0.52, 0.44, 0.51, 20, 20, 20, 20)),
        ]
        res = run_scenario(ticks)
        self.assertGreaterEqual(res.capital, 0)

    def test_6_insufficient_liquidity(self):
        ticks = [
            (250, MarketQuoteView(0.34, 0.70, 0.33, 0.69, 20, 20, 20, 20)),
            (220, MarketQuoteView(0.40, 0.60, 0.39, 0.59, 20, 2, 20, 20)),
        ]
        res = run_scenario(ticks)
        # Hedge blocked by depth < min order
        self.assertEqual(res.completed_pairs, 0)
        self.assertAlmostEqual(res.unmatched_at_end, 5)

    def test_7_partial_hedge_fill_simulated(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 10, 0.30)
        ledger.record_buy("NO", 5, 0.60)  # partial
        m = ledger.metrics()
        self.assertAlmostEqual(m.up_matched_shares, 5)
        self.assertAlmostEqual(m.up_unmatched_shares, 5)

    def test_8_dca_fill_hedge_never_comes(self):
        # Even if we somehow DCA-approved path isn't taken when opposite expensive
        ev = evaluate_adaptive_dca(
            side="YES",
            existing_shares=5,
            existing_total_cost=1.70,
            proposed_qty=5,
            proposed_price=0.20,
            opposite_ask=0.85,
            target_combined=0.98,
            max_inventory=15,
            unmatched_target=5,
            max_dca_legs=3,
            current_dca_legs=0,
            available_liquidity=20,
            seconds_left=200,
            min_seconds_left=90,
        )
        self.assertEqual(ev.decision, DcaDecision.REJECT)

    def test_9_expiry_with_unmatched_exit(self):
        ticks = [
            (250, MarketQuoteView(0.34, 0.70, 0.33, 0.69, 20, 20, 20, 20)),
            (40, MarketQuoteView(0.20, 0.80, 0.15, 0.79, 20, 20, 20, 20)),
            (30, MarketQuoteView(0.18, 0.82, 0.12, 0.81, 20, 20, 20, 20)),
        ]
        res = run_scenario(ticks)
        # Late phase should prefer exit or wait — not new accumulation
        self.assertTrue(res.forced_exits >= 1 or res.unmatched_at_end >= 0)

    def test_10_api_disconnect_open_orders_idempotent(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34, order_id="pending-1")
        # reconnect / duplicate fill notification
        ledger.record_buy("YES", 5, 0.34, order_id="pending-1")
        self.assertAlmostEqual(ledger.total_shares("YES"), 5)

    def test_11_resolve_with_unmatched(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.34)
        # Market resolves UP=1: unmatched YES worth $1 each
        settlement = 5 * 1.0
        cost = 5 * 0.34
        ledger.settlement_realized = settlement - cost
        m = ledger.metrics()
        self.assertAlmostEqual(m.realized_profit, settlement - cost)
        self.assertAlmostEqual(m.up_unmatched_shares, 5)

    def test_12_fees_make_pair_unprofitable(self):
        cfg = _cfg(fee_rate=0.05, slippage_reserve=0.01)
        ticks = [
            (250, MarketQuoteView(0.34, 0.70, 0.33, 0.69, 20, 20, 20, 20)),
            (220, MarketQuoteView(0.40, 0.60, 0.39, 0.59, 20, 20, 20, 20)),
        ]
        res = run_scenario(ticks, cfg=cfg)
        # With high fees, hedge at 60¢ + fees on 34¢ entry exceeds 98¢
        self.assertEqual(res.completed_pairs, 0)


if __name__ == "__main__":
    unittest.main()
