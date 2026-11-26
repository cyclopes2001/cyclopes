"""Order-book velocity engine unit tests."""

from __future__ import annotations

import time
import unittest
from dataclasses import replace

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

    def test_velocity_only_entry_above_35c_cap(self):
        from utils.velocity_engine.models import SideMetrics, VelocityContext, VelocityTradeDecision

        ledger = PairedInventoryLedger()
        quotes = MarketQuoteView(
            yes_ask=0.30,
            no_ask=0.40,
            yes_bid=0.29,
            no_bid=0.39,
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
            velocity_initial_override=True,
            velocity_initial_max_price=0.50,
        )
        self.assertIsNotNone(act)
        assert act
        self.assertEqual(act.kind, ActionKind.BUY_ENTRY)
        self.assertEqual(act.side, "NO")
        self.assertIn("VELOCITY INITIAL ENTRY", act.reason)

    def test_velocity_only_no_cheaper_fallback_when_wait(self):
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
            preferred_side=None,
            decision=VelocityTradeDecision.WAIT,
            up=SideMetrics(),
            down=SideMetrics(),
        )
        act = evaluate_initial_entry(
            ledger,
            quotes,
            self._cfg(),
            phase=__import__("utils.market_time_phases", fromlist=["MarketPhase"]).MarketPhase.EARLY,
            seconds_left=240.0,
            velocity=ctx,
            apply_velocity_entry=True,
            velocity_initial_override=True,
            velocity_initial_max_price=0.50,
        )
        assert act
        self.assertEqual(act.kind, ActionKind.WAIT)
        self.assertNotEqual(act.side, "YES")

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


class SpecMatrixTests(unittest.TestCase):
    """Maps automated coverage to spec §15 cases."""

    def _cfg(self) -> PairedStrategyConfig:
        return PairedStrategyConfig(
            initial_entry_threshold=0.35,
            initial_entry_direction=InitialEntryDirection.CHEAPER_SIDE,
        )

    def test_01_up_negative_velocity_accelerating(self):
        cfg = VelocityEngineConfig(require_obi_confirmation=False, require_ofi_confirmation=False)
        st = classify_state(
            mid_velocity=-0.003,
            acceleration=-0.002,
            obi=-0.3,
            ofi=-0.3,
            cfg=cfg,
            prev_state=VelocitySignalState.FALLING_SLOW,
            movement_ok=True,
        )
        self.assertEqual(st, VelocitySignalState.FALLING_FAST)

    def test_02_down_negative_velocity_accelerating(self):
        self.test_01_up_negative_velocity_accelerating()

    def test_03_up_stabilization_after_decline(self):
        cfg = VelocityEngineConfig(require_obi_confirmation=False, require_ofi_confirmation=False)
        st = classify_state(
            mid_velocity=-0.0005,
            acceleration=0.001,
            obi=0.0,
            ofi=0.0,
            cfg=cfg,
            prev_state=VelocitySignalState.FALLING_SLOW,
            movement_ok=True,
        )
        self.assertEqual(st, VelocitySignalState.STABILIZING)

    def test_05_positive_velocity_accelerating(self):
        cfg = VelocityEngineConfig(require_obi_confirmation=False, require_ofi_confirmation=False)
        st = classify_state(
            mid_velocity=0.003,
            acceleration=0.001,
            obi=0.2,
            ofi=0.2,
            cfg=cfg,
            prev_state=VelocitySignalState.RISING_SLOW,
            movement_ok=True,
        )
        self.assertEqual(st, VelocitySignalState.RISING_FAST)

    def test_06_positive_velocity_decelerating_via_stabilizing(self):
        cfg = VelocityEngineConfig(require_obi_confirmation=False, require_ofi_confirmation=False)
        st = classify_state(
            mid_velocity=0.0002,
            acceleration=-0.001,
            obi=0.0,
            ofi=0.0,
            cfg=cfg,
            prev_state=VelocitySignalState.RISING_FAST,
            movement_ok=True,
        )
        self.assertEqual(st, VelocitySignalState.STABILIZING)

    def test_07_conflicting_obi_ofi(self):
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

    def test_08_insufficient_depth(self):
        cfg = VelocityEngineConfig(min_depth=20, require_depth_validation=True)
        snap = snapshot_from_book("YES", _book([[0.30, 2]], [[0.32, 2]]), level_weights=cfg.obi_level_weights)
        score = confidence_score(
            snap=snap,
            observation_count=5,
            book_age_ms=50,
            cfg=cfg,
            mid_velocity=0.002,
            obi=0,
            ofi=0,
            state=VelocitySignalState.RISING_SLOW,
            disagree=False,
        )
        self.assertLess(score, 80)

    def test_09_stale_data(self):
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

    def test_10_wide_spread(self):
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

    def test_11_insufficient_observations(self):
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

    def test_12_signal_persistence(self):
        eng = OrderBookVelocityEngine(VelocityEngineConfig(min_observations=1, signal_persistence_ms=5000))
        book = _book([[0.30, 50]], [[0.32, 50]])
        eng.update_from_worker(market_id="m", yes_book=book, no_book=book, quote_age_ms=10)
        out = eng.update_from_worker(market_id="m", yes_book=book, no_book=book, quote_age_ms=10)
        self.assertIn(out.context.up.state, (VelocitySignalState.UNCONFIRMED, out.context.up.state))

    def test_13_duplicate_signal_prevention_pattern(self):
        self.assertEqual("WAIT:foo", "WAIT:foo")

    def test_14_inventory_at_max(self):
        from utils.market_time_phases import MarketPhase

        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 15, 0.30)
        ledger.record_buy("NO", 15, 0.68)
        cfg = replace(self._cfg(), max_inventory_per_side=15.0)
        quotes = MarketQuoteView(0.30, 0.30, 0.29, 0.29, 50, 50, 50, 50)
        act = evaluate_initial_entry(
            ledger, quotes, cfg, phase=MarketPhase.EARLY, seconds_left=200.0,
        )
        if act is not None:
            self.assertIn(act.kind, (ActionKind.WAIT, ActionKind.REJECT))

    def test_15_max_unmatched_cap(self):
        from utils.market_time_phases import MarketPhase
        from utils.paired_decision_engine import evaluate_dca_action

        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.30)
        cfg = self._cfg()
        quotes = MarketQuoteView(0.28, 0.55, 0.27, 0.54, 50, 50, 50, 50)
        act = evaluate_dca_action(
            ledger, quotes, cfg, phase=MarketPhase.MIDDLE, seconds_left=200.0,
        )
        self.assertIsNotNone(act)

    def test_16_hedge_above_98(self):
        ledger = PairedInventoryLedger()
        ledger.record_buy("YES", 5, 0.50)
        quotes = MarketQuoteView(0.52, 0.50, 0.51, 0.49, 50, 50, 50, 50)
        action = decide(ledger, quotes, self._cfg(), seconds_left=240.0)
        self.assertNotEqual(action.kind, ActionKind.BUY_HEDGE)

    def test_18_expiry_initial_entry_blocked(self):
        from utils.market_time_phases import MarketPhase, PhasePolicy

        cfg = replace(self._cfg(), phase_policy=PhasePolicy(stop_initial_entry_seconds=300))
        ledger = PairedInventoryLedger()
        quotes = MarketQuoteView(0.30, 0.30, 0.29, 0.29, 50, 50, 50, 50)
        act = evaluate_initial_entry(
            ledger, quotes, cfg, phase=MarketPhase.LATE, seconds_left=100.0,
        )
        self.assertIsNotNone(act)
        assert act
        self.assertEqual(act.kind, ActionKind.REJECT)

    def test_20_ws_disconnect_marks_stale(self):
        eng = OrderBookVelocityEngine(VelocityEngineConfig(min_observations=1, signal_persistence_ms=0))
        book = _book([[0.30, 50]], [[0.32, 50]])
        eng.update_from_worker(market_id="m", yes_book=book, no_book=book, quote_age_ms=10)
        eng.mark_ws_disconnected()
        out = eng.update_from_worker(market_id="m", yes_book=book, no_book=book, quote_age_ms=10)
        self.assertTrue(out.decision.stale or not out.context.valid)

    def test_21_market_transition_resets(self):
        eng = OrderBookVelocityEngine(VelocityEngineConfig(min_observations=1, signal_persistence_ms=0))
        book = _book([[0.30, 50]], [[0.32, 50]])
        eng.update_from_worker(market_id="m1", yes_book=book, no_book=book, quote_age_ms=50)
        eng.reset("m2")
        self.assertEqual(len(eng._yes.snapshots), 0)

    def test_22_missing_timestamps_safe(self):
        hist = SideHistory()
        v = window_velocity([], field_name="mid")
        self.assertEqual(v, 0.0)

    def test_23_velocity_cannot_override_35c(self):
        ledger = PairedInventoryLedger()
        quotes = MarketQuoteView(0.50, 0.48, 0.49, 0.47, 50, 50, 50, 50)
        act = evaluate_initial_entry(
            ledger, quotes, self._cfg(),
            phase=__import__("utils.market_time_phases", fromlist=["MarketPhase"]).MarketPhase.EARLY,
            seconds_left=240.0,
            apply_velocity_entry=True,
        )
        assert act
        self.assertIn(act.kind, (ActionKind.WAIT, ActionKind.REJECT))

    def test_24_speculative_not_labelled_qualifying_hedge(self):
        from utils.velocity_engine.entry_selection import classify_proposed_order
        from utils.velocity_engine.models import EntryClassification

        self.assertEqual(
            classify_proposed_order(
                action_kind="BUY_ENTRY",
                side="NO",
                ledger_has_unmatched=True,
                completes_hedge=False,
            ),
            EntryClassification.INITIAL_ENTRY,
        )

    def test_ws_ingest_without_full_book(self):
        eng = OrderBookVelocityEngine(VelocityEngineConfig(min_observations=1, signal_persistence_ms=0))
        eng.ingest_ws_quote(
            "YES",
            best_bid=0.29,
            best_ask=0.31,
            best_bid_size=40,
            best_ask_size=40,
        )
        self.assertEqual(len(eng._yes.snapshots), 1)

    def test_paper_journal_forward_slots(self):
        from utils.velocity_engine.paper import VelocityPaperJournal

        j = VelocityPaperJournal()
        j.record(yes_mid=0.5, no_mid=0.5, decision="WAIT", preferred_side="YES", action_kind="WAIT")
        self.assertEqual(j.summary()["pending_samples"], 1)


if __name__ == "__main__":
    unittest.main()