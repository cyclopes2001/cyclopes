"""Directional signal classification and confidence scoring."""

from __future__ import annotations

import math
from typing import Optional, Tuple

from utils.velocity_engine.config import VelocityEngineConfig
from utils.velocity_engine.models import VelocitySignalState
from utils.velocity_engine.snapshot import BookSnapshot


def classify_state(
    *,
    mid_velocity: float,
    acceleration: float,
    obi: float,
    ofi: float,
    cfg: VelocityEngineConfig,
    prev_state: VelocitySignalState,
    movement_ok: bool,
) -> VelocitySignalState:
    if not movement_ok:
        return VelocitySignalState.UNCONFIRMED

    fast = cfg.velocity_fast_threshold
    slow = cfg.velocity_slow_threshold
    acc_thr = cfg.acceleration_threshold

    obi_bull = obi > 0.15
    obi_bear = obi < -0.15
    ofi_bull = ofi > 0.1
    ofi_bear = ofi < -0.1

    if cfg.require_obi_confirmation and cfg.require_ofi_confirmation:
        bullish_flow = obi_bull and ofi_bull
        bearish_flow = obi_bear and ofi_bear
        if mid_velocity > slow and ofi_bull != obi_bull and mid_velocity > fast:
            return VelocitySignalState.UNCONFIRMED
        if mid_velocity < -slow and ofi_bear != obi_bear and mid_velocity < -fast:
            return VelocitySignalState.UNCONFIRMED
    else:
        bullish_flow = obi_bull or ofi_bull
        bearish_flow = obi_bear or ofi_bear

    if mid_velocity <= -fast and acceleration <= -acc_thr:
        if bearish_flow or not (cfg.require_ofi_confirmation or cfg.require_obi_confirmation):
            return VelocitySignalState.FALLING_FAST
        return VelocitySignalState.UNCONFIRMED

    if mid_velocity <= -slow:
        return VelocitySignalState.FALLING_SLOW

    if mid_velocity >= fast and acceleration >= acc_thr:
        if bullish_flow or not (cfg.require_ofi_confirmation or cfg.require_obi_confirmation):
            return VelocitySignalState.RISING_FAST
        return VelocitySignalState.UNCONFIRMED

    if mid_velocity >= slow:
        return VelocitySignalState.RISING_SLOW

    if prev_state in (VelocitySignalState.FALLING_FAST, VelocitySignalState.FALLING_SLOW):
        if mid_velocity < 0 and acceleration > acc_thr:
            return VelocitySignalState.STABILIZING
        if cfg.reversal_confirmation_enabled and mid_velocity > slow and acceleration > 0:
            return VelocitySignalState.REVERSAL_CONFIRMED

    if prev_state == VelocitySignalState.STABILIZING and mid_velocity > slow:
        return VelocitySignalState.REVERSAL_CONFIRMED

    if abs(mid_velocity) < slow * 0.35:
        return VelocitySignalState.STABILIZING

    return VelocitySignalState.UNCONFIRMED


def confidence_score(
    *,
    snap: Optional[BookSnapshot],
    observation_count: int,
    book_age_ms: float,
    cfg: VelocityEngineConfig,
    mid_velocity: float,
    obi: float,
    ofi: float,
    state: VelocitySignalState,
    disagree: bool,
) -> float:
    score = 100.0
    if snap is None or snap.invalid:
        return 0.0
    if book_age_ms > cfg.max_book_age_ms:
        score -= min(50.0, (book_age_ms - cfg.max_book_age_ms) / 10.0)
    if cfg.require_depth_validation:
        depth = min(snap.bid_depth_5, snap.ask_depth_5)
        if depth + 1e-9 < cfg.min_depth:
            score -= 25.0
    if snap.spread > cfg.max_spread:
        score -= min(30.0, (snap.spread - cfg.max_spread) * 400.0)
    if observation_count < cfg.min_observations:
        deficit = cfg.min_observations - observation_count
        score -= deficit * 12.0
    if state == VelocitySignalState.UNCONFIRMED:
        score -= 20.0
    if disagree:
        score -= 25.0
    if not math.isfinite(mid_velocity):
        score -= 30.0
    if cfg.require_obi_confirmation and abs(obi) < 0.05:
        score -= 5.0
    if cfg.require_ofi_confirmation and abs(ofi) < 0.05:
        score -= 5.0
    return max(0.0, min(100.0, score))


def flow_disagrees(obi: float, ofi: float, velocity: float) -> bool:
    if velocity > 0.0005 and (obi < -0.2 or ofi < -0.2):
        return True
    if velocity < -0.0005 and (obi > 0.2 or ofi > 0.2):
        return True
    return False


def primary_window_velocity(vels: dict, windows: tuple) -> Tuple[float, float]:
    """Prefer 3s then 5s then 1s then 10s."""
    for w in (3.0, 5.0, 1.0, 10.0):
        if w in vels:
            return vels[w], w
    if windows and windows[0] in vels:
        return vels[windows[0]], windows[0]
    return 0.0, 3.0