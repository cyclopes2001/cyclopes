"""Velocity-driven entry side selection (Rules A–E)."""

from __future__ import annotations

from typing import Optional, Tuple

from utils.velocity_engine.models import (
    EntryClassification,
    SideMetrics,
    VelocityContext,
    VelocitySignalState,
    VelocityTradeDecision,
)


_WEAK = frozenset(
    {
        VelocitySignalState.FALLING_FAST,
        VelocitySignalState.FALLING_SLOW,
    }
)
_STABLE = frozenset(
    {
        VelocitySignalState.STABILIZING,
        VelocitySignalState.REVERSAL_CONFIRMED,
    }
)
_CHASE = frozenset({VelocitySignalState.RISING_FAST})


def _display_side(side: Optional[str]) -> str:
    if side == "YES":
        return "UP"
    if side == "NO":
        return "DOWN"
    return "—"


def select_entry_context(
    *,
    up: SideMetrics,
    down: SideMetrics,
    confidence: float,
    min_confidence: float,
    signal_valid: bool,
    rising_fast_block: bool,
) -> VelocityContext:
    ctx = VelocityContext(
        enabled=True,
        valid=signal_valid and confidence >= min_confidence,
        stale=up.stale or down.stale,
        confidence=confidence,
        up=up,
        down=down,
        signal_valid=signal_valid,
    )
    avoid: list[str] = []
    recovery: list[str] = []
    preferred: Optional[str] = None
    reason_parts: list[str] = []

    # Rule A: UP weakening → prefer DOWN
    if up.state in _WEAK and up.confidence >= min_confidence * 0.85:
        avoid.append("YES")
        if down.state not in _WEAK:
            preferred = "NO"
            reason_parts.append("UP weakening; evaluate DOWN")
        else:
            reason_parts.append("UP weakening but DOWN also weak")

    # Rule B: DOWN weakening → prefer UP
    if down.state in _WEAK and down.confidence >= min_confidence * 0.85:
        avoid.append("NO")
        if up.state not in _WEAK:
            preferred = "YES"
            reason_parts.append("DOWN weakening; evaluate UP")
        elif preferred is None:
            reason_parts.append("DOWN weakening; UP not confirmed strong")

    # Rule C: stabilization → same-side recovery DCA candidate
    if up.state in _STABLE:
        recovery.append("YES")
    if down.state in _STABLE:
        recovery.append("NO")

    # Rule D: conflicting / UNCONFIRMED
    if up.state == VelocitySignalState.UNCONFIRMED and down.state == VelocitySignalState.UNCONFIRMED:
        ctx.decision = VelocityTradeDecision.WAIT
        ctx.reason = "WAIT: conflicting or unconfirmed velocity signals"
        ctx.avoid_buy_sides = tuple(dict.fromkeys(avoid))
        ctx.allow_recovery_dca_sides = tuple(recovery)
        return ctx

    if preferred is None:
        if up.state in _CHASE or down.state in _CHASE:
            ctx.decision = VelocityTradeDecision.WAIT
            ctx.reason = "WAIT: rapid recovery — avoid chasing"
            ctx.avoid_buy_sides = tuple(dict.fromkeys(avoid))
            ctx.allow_recovery_dca_sides = tuple(recovery)
            return ctx
        # mild preference: stronger OBI+OFI on one side
        up_score = up.obi + up.ofi - abs(min(0.0, up.mid_velocity)) * 50
        down_score = down.obi + down.ofi - abs(min(0.0, down.mid_velocity)) * 50
        if abs(up_score - down_score) > 0.15:
            preferred = "YES" if up_score > down_score else "NO"
            reason_parts.append(f"flow tilt toward {_display_side(preferred)}")

    if preferred:
        chase_side = preferred
        chase_metrics = up if preferred == "YES" else down
        if rising_fast_block and chase_metrics.state in _CHASE:
            ctx.decision = VelocityTradeDecision.WAIT
            ctx.reason = f"WAIT: {_display_side(chase_side)} rising fast — no chase"
            ctx.preferred_side = preferred
            ctx.avoid_buy_sides = tuple(dict.fromkeys(avoid))
            ctx.allow_recovery_dca_sides = tuple(recovery)
            return ctx
        ctx.preferred_side = preferred
        ctx.decision = (
            VelocityTradeDecision.BUY_UP if preferred == "YES" else VelocityTradeDecision.BUY_DOWN
        )
        ctx.reason = "; ".join(reason_parts) if reason_parts else f"Prefer {_display_side(preferred)}"
    else:
        ctx.decision = VelocityTradeDecision.WAIT
        ctx.reason = "WAIT: no velocity entry preference"

    if avoid and preferred in avoid:
        ctx.decision = VelocityTradeDecision.WAIT
        ctx.reason = "WAIT: preferred side blocked by weakening signal"

    ctx.avoid_buy_sides = tuple(dict.fromkeys(avoid))
    ctx.allow_recovery_dca_sides = tuple(recovery)
    ctx.entry_classification = EntryClassification.VELOCITY_ENTRY if preferred else EntryClassification.NONE
    return ctx


def classify_proposed_order(
    *,
    action_kind: str,
    side: Optional[str],
    ledger_has_unmatched: bool,
    completes_hedge: bool,
) -> EntryClassification:
    kind = (action_kind or "").upper()
    if kind == "BUY_HEDGE" and completes_hedge:
        return EntryClassification.QUALIFYING_HEDGE
    if kind == "BUY_HEDGE":
        return EntryClassification.RISK_REDUCTION
    if kind == "BUY_DCA":
        return EntryClassification.RECOVERY_DCA
    if kind == "BUY_ENTRY":
        return EntryClassification.INITIAL_ENTRY
    if kind == "EXIT":
        return EntryClassification.RISK_REDUCTION
    if ledger_has_unmatched and side:
        return EntryClassification.VELOCITY_ENTRY
    return EntryClassification.NONE