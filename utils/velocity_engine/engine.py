"""Order-book velocity engine orchestrator."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from utils.velocity_engine.calculations import (
    SideHistory,
    aggregate_ofi,
    movement_sufficient,
    window_acceleration,
    window_velocity,
)
from utils.velocity_engine.config import VelocityEngineConfig, load_velocity_config
from utils.velocity_engine.entry_selection import select_entry_context
from utils.velocity_engine.models import (
    SideMetrics,
    VelocityContext,
    VelocityDecision,
    VelocitySignalState,
)
from utils.velocity_engine.signals import (
    classify_state,
    confidence_score,
    flow_disagrees,
    primary_window_velocity,
)
from utils.velocity_engine.snapshot import snapshot_from_book, snapshot_from_quote

_LOG = logging.getLogger("velocity_engine")


@dataclass
class VelocitySnapshot:
    decision: VelocityDecision
    context: VelocityContext


class OrderBookVelocityEngine:
    """Maintains rolling books and emits velocity metrics for YES/NO tokens."""

    def __init__(self, cfg: Optional[VelocityEngineConfig] = None) -> None:
        self.cfg = cfg or VelocityEngineConfig()
        self._yes = SideHistory()
        self._no = SideHistory()
        self._market_id: Optional[str] = None
        self._prev_state: Dict[str, VelocitySignalState] = {
            "YES": VelocitySignalState.UNCONFIRMED,
            "NO": VelocitySignalState.UNCONFIRMED,
        }
        self._state_since: Dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self._last_context: Optional[VelocityContext] = None
        self._last_mono: float = 0.0
        self._ws_disconnected_at: float = 0.0

    def mark_ws_disconnected(self) -> None:
        """Call when the market WebSocket drops; observations pause until resync."""
        self._ws_disconnected_at = time.monotonic()

    def ingest_ws_quote(
        self,
        side: str,
        *,
        best_bid: float,
        best_ask: float,
        best_bid_size: float,
        best_ask_size: float,
        ts_mono: Optional[float] = None,
    ) -> None:
        """Append a WS top-of-book observation (uses prior depth when present)."""
        if not self.cfg.engine_enabled:
            return
        hist = self._yes if side.upper() == "YES" else self._no
        prior = hist.latest()
        snap = snapshot_from_quote(
            side,
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            level_weights=self.cfg.obi_level_weights,
            ts_mono=ts_mono,
            prior=prior,
        )
        if snap and not snap.invalid:
            hist.append(snap)

    def reset(self, market_id: str) -> None:
        self._market_id = market_id
        self._yes.clear()
        self._no.clear()
        self._ws_disconnected_at = 0.0
        self._prev_state = {"YES": VelocitySignalState.UNCONFIRMED, "NO": VelocitySignalState.UNCONFIRMED}
        self._state_since = {"YES": time.monotonic(), "NO": time.monotonic()}
        self._last_context = None

    def update_from_worker(
        self,
        *,
        market_id: str,
        yes_book: Any,
        no_book: Any,
        quote_age_ms: Optional[float] = None,
    ) -> VelocitySnapshot:
        if self._market_id != market_id:
            self.reset(market_id)
        now = time.monotonic()
        self._last_mono = now
        cfg = self.cfg

        yes_snap = snapshot_from_book("YES", yes_book, level_weights=cfg.obi_level_weights, ts_mono=now)
        no_snap = snapshot_from_book("NO", no_book, level_weights=cfg.obi_level_weights, ts_mono=now)

        if yes_snap and not yes_snap.invalid:
            self._yes.append(yes_snap)
        if no_snap and not no_snap.invalid:
            self._no.append(no_snap)

        up = self._side_metrics("YES", self._yes, yes_snap, now, quote_age_ms)
        down = self._side_metrics("NO", self._no, no_snap, now, quote_age_ms)

        confidence = min(up.confidence, down.confidence)
        if up.stale or down.stale or up.invalid_book or down.invalid_book:
            confidence = min(confidence, 40.0)

        signal_valid = (
            not up.stale
            and not down.stale
            and up.observation_count >= cfg.min_observations
            and down.observation_count >= cfg.min_observations
            and confidence >= cfg.min_confidence
        )

        ctx = select_entry_context(
            up=up,
            down=down,
            confidence=confidence,
            min_confidence=cfg.min_confidence,
            signal_valid=signal_valid,
            rising_fast_block=cfg.rising_fast_block_chase,
        )
        ctx.enabled = cfg.engine_enabled
        ctx.valid = ctx.valid and cfg.engine_enabled
        ctx.stale = up.stale or down.stale
        self._last_context = ctx

        decision = VelocityDecision(
            market_id=market_id,
            ts=time.time(),
            valid=ctx.valid,
            stale=ctx.stale,
            confidence=confidence,
            preferred_side=ctx.preferred_side,
            decision=ctx.decision.value,
            reason=ctx.reason,
            entry_classification=ctx.entry_classification.value,
            up_state=up.state.value,
            down_state=down.state.value,
            up_mid_velocity=up.mid_velocity,
            down_mid_velocity=down.mid_velocity,
            up_obi=up.obi,
            down_obi=down.obi,
            up_ofi=up.ofi,
            down_ofi=down.ofi,
            up_acceleration=up.acceleration,
            down_acceleration=down.acceleration,
            signal_valid=signal_valid,
        )
        return VelocitySnapshot(decision=decision, context=ctx)

    def _side_metrics(
        self,
        side: str,
        history: SideHistory,
        snap: Optional[Any],
        now: float,
        quote_age_ms: Optional[float],
    ) -> SideMetrics:
        cfg = self.cfg
        latest = history.latest()
        book_age_ms = quote_age_ms if quote_age_ms is not None else 0.0
        if latest:
            book_age_ms = max(book_age_ms, (now - latest.ts_mono) * 1000.0)
        if self._ws_disconnected_at > 0 and now - self._ws_disconnected_at < 5.0:
            book_age_ms = max(book_age_ms, self.cfg.max_book_age_ms + 1.0)

        stale = book_age_ms > cfg.max_book_age_ms or snap is None
        obs_count = len(history.snapshots)

        vels: Dict[float, float] = {}
        acc = 0.0
        mid_v = 0.0
        for w in cfg.windows_seconds:
            snaps = history.observations_in(w, now)
            vels[w] = window_velocity(snaps, field_name="mid")
            if w == 3.0 or (w == cfg.windows_seconds[0] and not vels):
                acc, mid_v = window_acceleration(history, w, now, field_name="mid")

        mid_v, _ = primary_window_velocity(vels, cfg.windows_seconds)
        bid_v = window_velocity(history.observations_in(3.0, now), field_name="bid")
        ask_v = window_velocity(history.observations_in(3.0, now), field_name="ask")

        ofi_map = aggregate_ofi(
            history,
            cfg.windows_seconds,
            now,
            norm_cap=cfg.ofi_norm_cap,
            latest=latest or snap,
        )
        ofi = ofi_map.get(3.0, ofi_map.get(cfg.windows_seconds[0], 0.0))

        obi = (latest.obi if latest else snap.obi if snap else 0.0)
        movement_ok = movement_sufficient(
            mid_v,
            tick=cfg.tick_size,
            min_ticks=cfg.min_price_movement_ticks,
        )

        disagree = flow_disagrees(obi, ofi, mid_v)
        state = classify_state(
            mid_velocity=mid_v,
            acceleration=acc,
            obi=obi,
            ofi=ofi,
            cfg=cfg,
            prev_state=self._prev_state[side],
            movement_ok=movement_ok,
        )
        if state != self._prev_state[side]:
            self._prev_state[side] = state
            self._state_since[side] = now

        conf = confidence_score(
            snap=latest or snap,
            observation_count=obs_count,
            book_age_ms=book_age_ms,
            cfg=cfg,
            mid_velocity=mid_v,
            obi=obi,
            ofi=ofi,
            state=state,
            disagree=disagree,
        )

        persisted_ms = (now - self._state_since[side]) * 1000.0 if not stale else 0.0
        if persisted_ms < cfg.signal_persistence_ms:
            state = VelocitySignalState.UNCONFIRMED

        return SideMetrics(
            state=state,
            mid_velocity=mid_v,
            bid_velocity=bid_v,
            ask_velocity=ask_v,
            acceleration=acc,
            obi=obi,
            ofi=ofi,
            confidence=conf,
            best_bid=(latest or snap).best_bid if (latest or snap) else 0.0,
            best_ask=(latest or snap).best_ask if (latest or snap) else 0.0,
            spread=(latest or snap).spread if (latest or snap) else 0.0,
            bid_depth_5=(latest or snap).bid_depth_5 if (latest or snap) else 0.0,
            ask_depth_5=(latest or snap).ask_depth_5 if (latest or snap) else 0.0,
            stale=stale,
            invalid_book=bool(snap and getattr(snap, "invalid", False)),
            observation_count=obs_count,
        )

    def last_context(self) -> Optional[VelocityContext]:
        return self._last_context


def velocity_config_from_worker(worker: Any) -> VelocityEngineConfig:
    return load_velocity_config(worker.worker_config)