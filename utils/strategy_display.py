"""Human-readable strategy states for dashboard/API."""

from __future__ import annotations

from typing import Any, Mapping


def display_strategy_state(raw: str, *, stopped_out: bool, has_position: bool) -> str:
    key = (raw or "idle").strip().lower()
    if stopped_out and not has_position:
        return "MARKET BLOCKED — EXIT COMPLETE"
    mapping = {
        "idle": "WAITING FOR ENTRY",
        "waiting": "WAITING FOR ENTRY",
        "entry_eligible": "ENDGAME SIGNAL — BUY IMPLIED",
        "entry_pending": "BUY ORDER SUBMITTED",
        "in_position": "POSITION OPEN",
        "holding_to_settle": "HOLDING TO SETTLE",
        "position_open": "POSITION OPEN",
        "stop_loss_triggered": "STOP LOSS TRIGGERED",
        "stop_loss_exit": "SELL ORDER SUBMITTED",
        "stop_loss_exit_pending": "EXIT RETRY",
        "take_profit_triggered": "TAKE PROFIT TRIGGERED",
        "take_profit_exit": "SELL ORDER SUBMITTED",
        "take_profit_exit_pending": "EXIT RETRY",
        "position_closed": "POSITION CLOSED",
        "stopped_out": "POSITION CLOSED",
        "market_completed": "MARKET COMPLETED",
        "entry_failed": "WAITING FOR ENTRY",
    }
    return mapping.get(key, key.replace("_", " ").upper())


def enrich_dashboard_payload(d: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(d)
    out["strategy_name"] = "Endgame Confirmed Move / Hold to Settle"
    out["execution_type"] = "FOK"
    stopped = bool(out.get("market_stopped_out"))
    has_pos = float(out.get("position_size") or 0) > 0
    raw = str(out.get("strategy_state") or "idle")
    out["strategy_display_state"] = display_strategy_state(
        raw, stopped_out=stopped, has_position=has_pos,
    )
    return out