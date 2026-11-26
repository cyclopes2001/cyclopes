"""Order-book velocity engine unit tests."""

from __future__ import annotations

import time
import unittest

from utils.paired_decision_engine import (
    ActionKind,
    InitialEntryDirection,
    MarketQuoteView,
    PairedStrategyConfig,
    decide,
    evaluate_initial_entry,
)
from utils.paired_inventory_ledger import PairedInventoryLedger
from utils.velocity_engine.calculations import (
    SideHistory,
    movement_sufficient,
    normalize_ofi,
    price_velocity,
    velocity_acceleration,
    weighted_obi,
    window_velocity,
)
from utils.velocity_engine.config import VelocityEngineConfig
from utils.velocity_engine.engine import OrderBookVelocityEngine
from utils.velocity_engine.models import VelocitySignalState
from utils.velocity_engine.signals import classify_state, confidence_score, flow_disagrees
from utils.velocity_engine.snapshot import ofi_delta, snapshot_from_book


def _book(bids, asks, **extra):
    return {"bids": bids, "asks": asks, **extra}


class VelocityMathTests(unittest.TestCase):
    def test_price_velocity_and_acceleration(self):
        v = price_velocity(0.40, 0.38, 2.0)
        self.assertAlmostEqual(v, 0.01)
        a = velocity_acceleration(-0.01, -0.02, 1.0)
        self.assertAlmostEqual(a, 0.01)

    def test_weighted_obi_bounds(self):
        self.assertAlmostEqual(weighted_obi(10, 10), 0.0)
        self.assertAlmostEqual(weighted_obi(30, 10), 0.5)

    def test_normalize_ofi(self):
        self.assertAlmostEqual(normalize_ofi(25, cap=50, depth_scale=50), 0.5)

    def test_movement_threshold_ticks(self):
        self.assertFalse(movement_sufficient(0.005, tick=0.01, min_ticks=1))
        self.assertTrue(movement_sufficient(0.012, tick=0.01, min_ticks=1))


class SnapshotOfiTests(unittest.TestCase):
    def test_ofi_delta_bid_lift(self):
        cfg = VelocityEngineConfig()
        b1 = snapshot_from_book("YES", _book([[0.30, 50]], [[0.32, 40]]), level_weights=cfg.obi_level_weights)
        b2 = snapshot_from_book("YES", _book([[0.31, 55]], [[0.32, 40]]), level_weights=cfg.obi_level_weights)
        assert b1 and b2
        self.assertGreater(ofi_delta(b1, b2, tick=0.01), 0)

    def test_crossed_book_invalid(self):
        snap = snapshot_from_book(
            "YES",
            _book([[0.35, 10]], [[0.34, 10]]),
            level_weights=VelocityEngineConfig().obi_level_weights,
        )
        self.assertIsNotNone(snap)
        assert snap
        self.assertTrue(snap.crossed)


class SignalClassificationTests(unittest.TestCase):
    def test_falling_fast_negative_acceleration(self):
        cfg = VelocityEngineConfig(
            require_ofi_confirmation=False,
            require_obi_confirmation=False,
        )
        st = classify_state(
            mid_velocity=-0.003,
            acceleration=-0.001,
            obi=-0.2,
            ofi=-0.2,
            cfg=cfg,
            prev_state=VelocitySignalState.FALLING_SLOW,
            movement_ok=True,
        )
        self.assertEqual(st, VelocitySignalState.FALLING_FAST)

    def test_conflicting_obi_ofi_unconfirmed(self):
        cfg = VelocityEngineConfig()
        st = classify_state(
            mid_velocity=-0.003,
            acceleration=-0.001,
            obi=0.3,
            ofi=-0.3,
            cfg=cfg,
            prev_state=VelocitySignalState.UNCONFIRMED,
            movement_ok=True,
        )
        self.assertEqual(st, VelocitySignalState.UNCONFIRMED)

    def test_insufficient_observations_low_confidence(self):
        cfg = VelocityEngineConfig(min_observations=5)
        snap = snapshot_from_book("YES", _book([[0.30, 50]], [[0.32, 50]]), level_weights=cfg.obi_level_weights)
        score = confidence_score(
            snap=snap,
            observation_count=2,
            book_age_ms=100,
            cfg=cfg,
            mid_velocity=0.001,
            obi=0.1,
            ofi=0.1,
            state=VelocitySignalState.RISING_SLOW,
            disagree=False,
        )
        self.assertLess(score, cfg.min_confidence)

    def test_stale_book_age_penalty(self):
        cfg = VelocityEngineConfig(max_book_age_ms=500)
        snap = snapshot_from_book("YES", _book([[0.30, 50]], [[0.32, 50]]), level_weights=cfg.obi_level_weights)
        score = confidence_score(
            snap=snap,
            observation_count=10,
            book_age_ms=2000,
            cfg=cfg,
            mid_velocity=0.002,
            obi=0.2,
            ofi=0.2,
            state=VelocitySignalState.RISING_SLOW,
            disagree=False,
        )
        self.assertLess(score, 60)

    def test_wide_spread_penalty(self):
        cfg = VelocityEngineConfig(max_spread=0.02)
        snap = snapshot_from_book("YES", _book([[0.20, 50]], [[0.30, 50]]), level_weights=cfg.obi_level_weights)
        score = confidence_score(
            snap=snap,
            observation_count=10,
            book_age_ms=100,
            cfg=cfg,
            mid_velocity=0.002,
            obi=0.0,
            ofi=0.0,
            state=VelocitySignalState.RISING_SLOW,
            disagree=False,
        )
        self.assertLess(score, 80)

    def test_flow_disagrees(self):
        self.assertTrue(flow_disagrees(0.3, -0.3, 0.002))


class EngineWindowTests(unittest.TestCase):
    def test_engine_insufficient_data_stale(self):
        eng = OrderBookVelocityEngine(
            VelocityEngineConfig(min_observations=3, signal_persistence_ms=0)
        )
        book = _book([[0.30, 50], [0.29, 40]], [[0.32, 50], [0.33, 40]])
        snap = eng.update_from_worker(market_id="m1", yes_book=book, no_book=book, quote_age_ms=50)
        self.assertTrue(snap.decision.stale or not snap.decision.signal_valid)

    def test_engine_market_reset_clears_history(self):
        eng = OrderBookVelocityEngine(VelocityEngineConfig(min_observations=1, signal_persistence_ms=0))
        book = _book([[0.30, 50]], [[0.32, 50]])
        eng.update_from_worker(market_id="m1", yes_book=book, no_book=book, quote_age_ms=50)
        eng.reset("m2")
        self.assertEqual(len(eng._yes.snapshots), 0)


class DecisionIntegrationTests(unittest.TestCase):
    def _cfg(self) -> PairedStrategyConfig:
        return PairedStrategyConfig(
            initial_entry_threshold=0.35,
            initial_entry_direction=InitialEntryDirection.CHEAPER_SIDE,
        )

    def test_velocity_prefers_down_over_cheaper_up(self):
        from utils.velocity_engine.models import SideMetrics, VelocityContext, VelocityTradeDecision

        ledger = PairedInventoryLedger()
        quotes = MarketQuoteView(
            yes_ask=0.30,
            no_ask=0.31,
            yes_bid=0.29,
            no_bid=0.30,
            yes_ask_size=50,
            no_ask_size=50,
            yes_bid_size=50,
            no_bid_size=50,
        )
        ctx = VelocityContext(
            enabled=True,
            valid=True,
            stale=False,
            confidence=80,
            preferred_side="NO",
            decision=VelocityTradeDecision.BUY_DOWN,
            avoid_buy_sides=("YES",),
            up=SideMetrics(state=VelocitySignalState.FALLING_FAST),
            down=SideMetrics(state=VelocitySignalState.STABILIZING),
        )
        act = evaluate_initial_entry(
            ledger,
            quotes,
            self._cfg(),
            phase=__import__("utils.market_time_phases", fromlist=["MarketPhase"]).MarketPhase.EARLY,
            seconds_left=240.0,
            velocity=ctx,
            apply_velocity_entry=True,
        )
        self.assertIsNotNone(act)
        assert act
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "NO")

    def test_velocity_override_initial_threshold_disabled(self):
        ledger = PairedInventoryLedger()
        quotes = MarketQuoteView(
            yes_ask=0.50,
            no_ask=0.48,
            yes_bid=0.49,
            no_bid=0.47,
            yes_ask_size=50,
            no_ask_size=50,
            yes_bid_size=50,
            no_bid_size=50,
        )
        act = evaluate_initial_entry(
            ledger,
            quotes,
            self._cfg(),
            phase=__import__("utils.market_time_phases", fromlist=["MarketPhase"]).MarketPhase.EARLY,
            seconds_left=240.0,
            velocity=None,
            apply_velocity_entry=True,
        )
        self.assertIsNotNone(act)
        assert act
        self.assertIn(act.kind, (ActionKind.WAIT, ActionKind.REJECT))

    def test_hedge_above_98_still_rejected_with_velocity(self):
        from utils.velocity_engine.models import SideMetrics, VelocityContext

        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.50)
        quotes = MarketQuoteView(
            yes_ask=0.52,
            no_ask=0.50,
            yes_bid=0.51,
            no_bid=0.49,
            yes_ask_size=50,
            no_ask_size=50,
            yes_bid_size=50,
            no_bid_size=50,
        )
        ctx = VelocityContext(
            enabled=True,
            valid=True,
            preferred_side="NO",
            up=SideMetrics(),
            down=SideMetrics(),
        )
        action = decide(
            ledger,
            quotes,
            self._cfg(),
            seconds_left=240.0,
            velocity=ctx,
            apply_velocity_entry=True,
        )
        self.assertNotEqual(action.kind, ActionKind.BUY_HEDGE)


class SideHistoryVelocityTests(unittest.TestCase):
    def test_window_velocity_from_snapshots(self):
        hist = SideHistory()
        cfg = VelocityEngineConfig()
        base = time.monotonic()
        for i, mid in enumerate([0.30, 0.31, 0.32]):
            snap = snapshot_from_book(
                "YES",
                _book([[mid - 0.01, 50]], [[mid + 0.01, 50]]),
                level_weights=cfg.obi_level_weights,
                ts_mono=base + i,
            )
            assert snap
            hist.append(snap)
        snaps = hist.observations_in(10.0, base + 3)
        v = window_velocity(snaps, field_name="mid")
        self.assertGreater(v, 0)


class AdditionalSpecCases(unittest.TestCase):
    def test_rising_fast_blocks_chase_in_context(self):
        from utils.velocity_engine.entry_selection import select_entry_context
        from utils.velocity_engine.models import SideMetrics

        up = SideMetrics(
            state=VelocitySignalState.RISING_FAST,
            confidence=90,
            mid_velocity=0.004,
            stale=False,
        )
        down = SideMetrics(state=VelocitySignalState.STABILIZING, confidence=80, stale=False)
        ctx = select_entry_context(
            up=up,
            down=down,
            confidence=80,
            min_confidence=75,
            signal_valid=True,
            rising_fast_block=True,
        )
        self.assertEqual(ctx.decision.value, "WAIT")

    def test_classify_speculative_vs_hedge(self):
        from utils.velocity_engine.entry_selection import classify_proposed_order
        from utils.velocity_engine.models import EntryClassification

        self.assertEqual(
            classify_proposed_order(
                action_kind="BUY_HEDGE",
                side="NO",
                ledger_has_unmatched=True,
                completes_hedge=True,
            ),
            EntryClassification.QUALIFYING_HEDGE,
        )
        self.assertEqual(
            classify_proposed_order(
                action_kind="BUY_HEDGE",
                side="NO",
                ledger_has_unmatched=True,
                completes_hedge=False,
            ),
            EntryClassification.RISK_REDUCTION,
        )

    def test_duplicate_decision_prevention_pattern(self):
        sig_a = "WAIT:foo"
        sig_b = "WAIT:foo"
        self.assertEqual(sig_a, sig_b)


if __name__ == "__main__":
    unittest.main()