"""Human-readable strategy states for dashboard/API."""

from __future__ import annotations

from typing import Any, Mapping


def strategy_label_from_entry(entry_price: float, *, hold_to_settle: bool = True) -> str:
    cents = round(float(entry_price) * 100)
    if hold_to_settle:
        return f"Buy ≥ {cents}¢ / Hold to Settle"
    return f"Buy ≥ {cents}¢"


def display_strategy_state(raw: str, *, stopped_out: bool, has_position: bool) -> str:
    key = (raw or "idle").strip().lower()
    if stopped_out and not has_position:
        return "MARKET BLOCKED — EXIT COMPLETE"
    mapping = {
        "idle": "WAITING FOR ENTRY",
        "waiting": "WAITING FOR ENTRY",
        "entry_eligible": "ENTRY ELIGIBLE — BUY",
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
    entry = float(out.get("entry_price") or out.get("minimum_entry_price") or 0.97)
    hold = bool(out.get("hold_to_settle", True))
    label = strategy_label_from_entry(entry, hold_to_settle=hold)
    out["strategy_name"] = label
    out["strategy_label"] = label
    out["execution_type"] = "FOK"
    stopped = bool(out.get("market_stopped_out"))
    has_pos = float(out.get("position_size") or 0) > 0
    raw = str(out.get("strategy_state") or "idle")
    out["strategy_display_state"] = display_strategy_state(
        raw, stopped_out=stopped, has_position=has_pos,
    )
    return out
