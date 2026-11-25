"""Single source of truth for paired-inventory dashboard API/UI fields."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from utils.market_time_phases import MarketPhase, allow_new_accumulation
from utils.paired_decision_engine import ActionKind, StrategyAction, max_hedge_prices
from utils.paired_inventory_ledger import PairedInventoryLedger

_EPS = 1e-9
_LEG_STATUS = {
    "FILLED": "FILLED",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "PENDING": "PENDING",
    "AVAILABLE": "AVAILABLE",
    "CANCELLED": "CANCELLED",
    "REJECTED": "REJECTED",
}


def _d(x: float) -> Decimal:
    return Decimal(str(x))


def _money(x: float) -> float:
    return float(_d(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _cents(x: float) -> float:
    if x <= _EPS:
        return 0.0
    return float((_d(x) * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _quantize_shares(x: float) -> float:
    return float(_d(x).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def format_position_line(shares: float, total_cost: float, side: str) -> str:
    label = "UP" if side.upper() == "YES" else "DOWN"
    sh = _quantize_shares(shares)
    return f"{int(sh) if abs(sh - round(sh)) < _EPS else sh:g} {label} @ ${_money(total_cost):.2f}"


def _consume_from_lots(
    lots: List[Tuple[float, float, float, float, Optional[str], str]],
    qty: float,
) -> Tuple[float, float, float, Optional[float], Optional[str]]:
    """Consume qty from front of lot list; return filled, spent, vwap, ts, order_id."""
    need = float(qty)
    filled = 0.0
    spent = 0.0
    px_num = 0.0
    ts: Optional[float] = None
    oid: Optional[str] = None
    while need > _EPS and lots:
        sh, px, cost, lot_ts, lot_oid, _lid = lots[0]
        take = min(need, sh)
        if take <= _EPS:
            lots.pop(0)
            continue
        unit = cost / sh if sh > _EPS else px
        spent += unit * take
        px_num += px * take
        filled += take
        ts = lot_ts if ts is None else max(ts, lot_ts)
        oid = lot_oid or oid
        sh -= take
        cost -= unit * take
        need -= take
        if sh <= _EPS:
            lots.pop(0)
        else:
            lots[0] = (sh, px, cost, lot_ts, lot_oid, _lid)
    avg = px_num / filled if filled > _EPS else 0.0
    return filled, spent, avg, ts, oid


def build_dca_leg_slots(
    ledger: PairedInventoryLedger,
    *,
    side: str,
    target_qty: float = 5.0,
    max_slots: int = 3,
    pending: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Map confirmed fills into standard DCA leg slots (FIFO by lot timestamp)."""
    s = side.upper()
    lots_raw = sorted(
        [lot for lot in ledger.lots if lot.side == s],
        key=lambda x: x.timestamp,
    )
    pool: List[Tuple[float, float, float, float, Optional[str], str]] = [
        (lot.shares, lot.execution_price, lot.total_cost, lot.timestamp, lot.order_id, lot.lot_id)
        for lot in lots_raw
    ]
    pending_side = (pending or {}).get("side", "").upper()
    pending_filled = float((pending or {}).get("filled_qty") or 0.0)
    pending_requested = float((pending or {}).get("requested_qty") or 0.0)
    pending_price = float((pending or {}).get("limit_price") or 0.0)
    pending_status = str((pending or {}).get("status") or "")
    pending_oid = (pending or {}).get("order_id")
    pending_ts = (pending or {}).get("timestamp")
    pending_active = pending_side == s and pending_requested > _EPS

    legs: List[Dict[str, Any]] = []
    leg_index_for_pending: Optional[int] = None
    consumed_before_pending = 0.0
    for n in range(1, max_slots + 1):
        filled, spent, avg, ts, oid = _consume_from_lots(pool, target_qty)
        status = _LEG_STATUS["AVAILABLE"]
        if filled + _EPS >= target_qty:
            status = _LEG_STATUS["FILLED"]
        elif filled > _EPS:
            status = _LEG_STATUS["PARTIALLY_FILLED"]
        elif pending_active and leg_index_for_pending is None:
            # Next empty slot may be pending
            leg_index_for_pending = n
        legs.append(
            {
                "leg": n,
                "side": s,
                "side_label": "UP" if s == "YES" else "DOWN",
                "target_qty": target_qty,
                "filled_qty": _quantize_shares(filled),
                "avg_fill_price": round(avg, 4),
                "avg_fill_c": _cents(avg),
                "spent_usd": _money(spent),
                "status": status,
                "timestamp": ts,
                "order_id": oid,
                "fill_ratio": f"{int(filled) if abs(filled-round(filled))<_EPS else filled:g}/{int(target_qty)}",
            }
        )
        consumed_before_pending += filled

    if pending_active and leg_index_for_pending is not None:
        idx = leg_index_for_pending - 1
        leg = legs[idx]
        # Only add pending qty not already represented in ledger fills
        display_filled = _quantize_shares(leg["filled_qty"] + pending_filled)
        if display_filled > leg["filled_qty"] + _EPS:
            # Pending is extra on top of confirmed (shouldn't duplicate ledger)
            pass
        if pending_status.upper() in ("SUBMITTED", "PENDING", "PARTIAL"):
            if pending_filled + _EPS >= pending_requested:
                leg["status"] = _LEG_STATUS["FILLED"]
            elif pending_filled > _EPS:
                leg["status"] = _LEG_STATUS["PARTIALLY_FILLED"]
                leg["filled_qty"] = _quantize_shares(pending_filled)
            else:
                leg["status"] = _LEG_STATUS["PENDING"]
            leg["fill_ratio"] = (
                f"{int(pending_filled) if abs(pending_filled-round(pending_filled))<_EPS else pending_filled:g}"
                f"/{int(pending_requested)}"
            )
            if pending_price > 0:
                leg["avg_fill_price"] = round(pending_price, 4)
                leg["avg_fill_c"] = _cents(pending_price)
            leg["order_id"] = pending_oid or leg.get("order_id")
            leg["timestamp"] = pending_ts or leg.get("timestamp")
            reserved = pending_filled * pending_price if pending_filled > 0 else pending_requested * pending_price
            leg["reserved_usd"] = _money(reserved)
        elif pending_status.upper() in ("CANCELLED", "CANCELED"):
            leg["status"] = _LEG_STATUS["CANCELLED"]
        elif pending_status.upper() == "REJECTED":
            leg["status"] = _LEG_STATUS["REJECTED"]

    return legs


def build_tranche_blocks(
    ledger: PairedInventoryLedger,
    *,
    side: str,
    slot_size: float = 5.0,
    max_slots: int = 3,
) -> List[Dict[str, Any]]:
    s = side.upper()
    total = ledger.total_shares(s)
    matched = ledger.matched_shares(s)
    blocks: List[Dict[str, Any]] = []
    for i in range(max_slots):
        start = i * slot_size
        end = min((i + 1) * slot_size, total)
        qty = max(0.0, end - start)
        if qty <= _EPS:
            blocks.append({"index": i + 1, "kind": "EMPTY", "qty": 0.0, "label": "EMPTY"})
            continue
        paired_in = min(qty, max(0.0, matched - start))
        paired_in = min(paired_in, qty)
        unmatched_in = qty - paired_in
        if paired_in >= qty - _EPS:
            kind = "PAIRED"
            label = f"PAIRED {int(qty) if qty==int(qty) else qty:g}"
        elif paired_in > _EPS:
            kind = "MIXED"
            label = f"P{int(paired_in)}/U{int(unmatched_in)}"
        else:
            kind = "UNMATCHED"
            label = f"UNMATCHED {int(qty) if qty==int(qty) else qty:g}"
        blocks.append(
            {
                "index": i + 1,
                "kind": kind,
                "qty": _quantize_shares(qty),
                "paired_qty": _quantize_shares(paired_in),
                "unmatched_qty": _quantize_shares(unmatched_in),
                "label": label,
            }
        )
    return blocks


def capital_breakdown(ledger: PairedInventoryLedger) -> Dict[str, float]:
    up_cost = _money(ledger.total_cost("YES"))
    down_cost = _money(ledger.total_cost("NO"))
    matched_qty = min(ledger.matched_shares("YES"), ledger.matched_shares("NO"))
    paired_capital = 0.0
    if matched_qty > _EPS and ledger.pairs:
        for p in ledger.pairs:
            paired_capital += p.yes_cost + p.no_cost + p.fees
    else:
        # Fallback WAC allocation
        if matched_qty > _EPS:
            paired_capital = (
                ledger.average_cost("YES") * matched_qty
                + ledger.average_cost("NO") * matched_qty
            )
    paired_capital = _money(paired_capital)
    unmatched_capital = _money(
        ledger.unmatched_average_cost("YES") * ledger.unmatched_shares("YES")
        + ledger.unmatched_average_cost("NO") * ledger.unmatched_shares("NO")
    )
    return {
        "up_capital_usd": up_cost,
        "down_capital_usd": down_cost,
        "total_capital_usd": _money(up_cost + down_cost),
        "paired_capital_usd": paired_capital,
        "unmatched_capital_usd": unmatched_capital,
    }


def economics_panel(
    ledger: PairedInventoryLedger,
    *,
    target_combined: float,
    fee_rate: float,
    slippage_reserve: float,
) -> Dict[str, Any]:
    m = ledger.metrics()
    matched_pairs = min(m.up_matched_shares, m.down_matched_shares)
    pair_cost = m.matched_pair_cost
    up_avg = m.up_average_cost
    down_avg = m.down_average_cost
    combined = pair_cost if pair_cost is not None else (up_avg + down_avg if matched_pairs > _EPS else None)

    gross_per_pair = None
    net_per_pair = None
    status = "neutral"
    if combined is not None and matched_pairs > _EPS:
        gross_per_pair = 1.0 - combined
        fee_drag = (up_avg + down_avg) * fee_rate + slippage_reserve
        net_per_pair = gross_per_pair - fee_drag
        if combined <= target_combined - 0.005:
            status = "green"
        elif combined <= target_combined + 0.01:
            status = "amber"
        else:
            status = "red"
    elif matched_pairs <= _EPS and (m.up_unmatched_shares > _EPS or m.down_unmatched_shares > _EPS):
        status = "amber"

    return {
        "up_avg_c": _cents(up_avg),
        "down_avg_c": _cents(down_avg),
        "combined_pair_cost_c": _cents(combined or 0.0),
        "target_combined_c": _cents(target_combined),
        "settlement_value_usd": 1.0,
        "gross_profit_per_pair_c": _cents(gross_per_pair or 0.0) if gross_per_pair is not None else None,
        "net_profit_per_pair_c": _cents(net_per_pair or 0.0) if net_per_pair is not None else None,
        "completed_pairs": _quantize_shares(matched_pairs),
        "economics_status": status,
        "realized_pnl_usd": _money(m.realized_profit),
        "note_unhedged_not_guaranteed": bool(
            m.up_unmatched_shares > _EPS or m.down_unmatched_shares > _EPS
        ),
    }


def hedge_monitor(
    ledger: PairedInventoryLedger,
    *,
    yes_ask: float,
    no_ask: float,
    target_combined: float,
    fee_rate: float,
    slippage_reserve: float,
) -> Dict[str, Any]:
    max_up, max_down = max_hedge_prices(ledger, target_combined)
    panels = []
    u_up = ledger.unmatched_shares("YES")
    u_dn = ledger.unmatched_shares("NO")
    if u_up > _EPS:
        avg = ledger.unmatched_average_cost("YES")
        opp = no_ask + slippage_reserve + no_ask * fee_rate if no_ask > 0 else 0.0
        est = avg + opp if opp > 0 else None
        gap = (no_ask - max_down) if no_ask > 0 and max_down > 0 else None
        status = "WAITING FOR BETTER PRICE"
        if est is not None and est <= target_combined + 1e-6:
            status = "PROFITABLE OPPORTUNITY"
        panels.append(
            {
                "heavy_side": "UP",
                "unmatched_shares": _quantize_shares(u_up),
                "avg_entry_c": _cents(avg),
                "max_hedge_opposite_c": _cents(max_down),
                "current_opposite_ask_c": _cents(no_ask),
                "hedge_gap_c": _cents(gap) if gap is not None else None,
                "estimated_combined_c": _cents(est) if est else None,
                "hedge_status": status,
            }
        )
    if u_dn > _EPS:
        avg = ledger.unmatched_average_cost("NO")
        opp = yes_ask + slippage_reserve + yes_ask * fee_rate if yes_ask > 0 else 0.0
        est = avg + opp if opp > 0 else None
        gap = (yes_ask - max_up) if yes_ask > 0 and max_up > 0 else None
        status = "WAITING FOR BETTER PRICE"
        if est is not None and est <= target_combined + 1e-6:
            status = "PROFITABLE OPPORTUNITY"
        panels.append(
            {
                "heavy_side": "DOWN",
                "unmatched_shares": _quantize_shares(u_dn),
                "avg_entry_c": _cents(avg),
                "max_hedge_opposite_c": _cents(max_up),
                "current_opposite_ask_c": _cents(yes_ask),
                "hedge_gap_c": _cents(gap) if gap is not None else None,
                "estimated_combined_c": _cents(est) if est else None,
                "hedge_status": status,
            }
        )
    return {"panels": panels, "max_hedge_up_c": _cents(max_up), "max_hedge_down_c": _cents(max_down)}


def map_ui_strategy_state(
    action: Optional[StrategyAction],
    *,
    phase: MarketPhase,
    seconds_left: float,
    order_pending: bool,
    pending_purpose: str,
    market_resolved: bool,
) -> Tuple[str, str]:
    reason = action.reason if action else "Monitoring UP/DOWN markets for paired accumulation opportunities."
    if market_resolved:
        return "MARKET RESOLVED", reason
    if seconds_left <= 0:
        return "MARKET EXPIRING", reason
    if order_pending:
        p = pending_purpose.lower()
        if "hedge" in p:
            return "HEDGE ORDER PLACED", reason
        if "dca" in p:
            return "DCA APPROVED", reason
        if "exit" in p:
            return "EXIT ORDER PLACED", reason
        return "INITIAL ACCUMULATION", reason
    if phase == MarketPhase.LATE and seconds_left <= 90:
        return "RISK MANAGEMENT", reason
    if not action:
        return "SCANNING", reason
    kind = action.kind
    if kind == ActionKind.BUY_ENTRY:
        return "INITIAL ACCUMULATION", reason
    if kind == ActionKind.BUY_DCA:
        return "DCA EVALUATION", reason
    if kind == ActionKind.BUY_HEDGE:
        return "HEDGE MONITORING", reason
    if kind == ActionKind.EXIT:
        return "EXIT EVALUATION", reason
    if kind == ActionKind.WAIT:
        if phase == MarketPhase.LATE:
            return "RISK MANAGEMENT", reason
        if "hedge" in reason.lower() or "HEDGE" in reason:
            return "HEDGE MONITORING", reason
        return "WAITING FOR BETTER PRICE", reason
    if kind == ActionKind.REJECT:
        if "DCA" in reason:
            return "DCA EVALUATION", reason
        return "RISK MANAGEMENT", reason
    return "SCANNING", reason


def rebuild_execution_history(ledger: PairedInventoryLedger) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for lot in sorted(ledger.lots, key=lambda x: x.timestamp):
        label = "UP" if lot.side == "YES" else "DOWN"
        events.append(
            {
                "timestamp": lot.timestamp,
                "event_type": "FILL",
                "category": "execution",
                "side": lot.side,
                "side_label": label,
                "qty": _quantize_shares(lot.shares),
                "price": round(lot.execution_price, 4),
                "price_c": _cents(lot.execution_price),
                "cost_usd": _money(lot.total_cost),
                "status": "FILLED",
                "explanation": f"BOUGHT {lot.shares:g} {label} @ {_cents(lot.execution_price):.1f}¢ — ${_money(lot.total_cost):.2f}",
                "order_id": lot.order_id,
            }
        )
    for pair in sorted(ledger.pairs, key=lambda x: x.timestamp):
        events.append(
            {
                "timestamp": pair.timestamp,
                "event_type": "PAIR_COMPLETED",
                "category": "hedge",
                "side": "",
                "qty": _quantize_shares(pair.qty),
                "price": round(pair.combined_cost_per_pair, 4),
                "price_c": _cents(pair.combined_cost_per_pair),
                "cost_usd": _money(pair.yes_cost + pair.no_cost + pair.fees),
                "status": "COMPLETED",
                "explanation": (
                    f"PAIR COMPLETED — {pair.qty:g} pairs @ "
                    f"{_cents(pair.combined_cost_per_pair):.1f}¢ combined"
                ),
                "order_id": pair.pair_id,
            }
        )
    return sorted(events, key=lambda x: x["timestamp"])


def build_paired_dashboard(
    ledger: PairedInventoryLedger,
    *,
    target_combined: float,
    fee_rate: float,
    slippage_reserve: float,
    dca_increment: float,
    max_dca_legs: int,
    max_inventory_per_side: float,
    yes_ask: float,
    no_ask: float,
    phase: MarketPhase,
    seconds_left: float,
    action: Optional[StrategyAction],
    pending_order: Optional[Dict[str, Any]],
    order_pending: bool,
    activity_feed: List[Dict[str, Any]],
    decision_feed: List[Dict[str, Any]],
    market_resolved: bool,
    reserved_capital_usd: float,
    allow_new_accumulation: bool,
    initial_entry_threshold: float = 0.64,
    initial_entry_direction: str = "EXPENSIVE_SIDE",
) -> Dict[str, Any]:
    m = ledger.metrics()
    cap = capital_breakdown(ledger)
    econ = economics_panel(
        ledger, target_combined=target_combined, fee_rate=fee_rate, slippage_reserve=slippage_reserve
    )
    hedge = hedge_monitor(
        ledger,
        yes_ask=yes_ask,
        no_ask=no_ask,
        target_combined=target_combined,
        fee_rate=fee_rate,
        slippage_reserve=slippage_reserve,
    )
    max_slots = max(1, int(max_inventory_per_side / max(dca_increment, 1.0)))
    max_slots = min(max_slots, max_dca_legs)

    up_legs = build_dca_leg_slots(
        ledger, side="YES", target_qty=dca_increment, max_slots=max_slots, pending=pending_order
    )
    down_legs = build_dca_leg_slots(
        ledger, side="NO", target_qty=dca_increment, max_slots=max_slots, pending=pending_order
    )

    up_filled_legs = sum(1 for leg in up_legs if leg["status"] == _LEG_STATUS["FILLED"])
    down_filled_legs = sum(1 for leg in down_legs if leg["status"] == _LEG_STATUS["FILLED"])

    matched = min(m.up_matched_shares, m.down_matched_shares)
    total_inv = max(m.up_total_shares, m.down_total_shares, 1.0)
    pairing_pct = round((matched / total_inv) * 100.0, 1) if total_inv > _EPS else 0.0

    ui_state, ui_reason = map_ui_strategy_state(
        action,
        phase=phase,
        seconds_left=seconds_left,
        order_pending=order_pending,
        pending_purpose=str((pending_order or {}).get("purpose") or ""),
        market_resolved=market_resolved,
    )

    up_cost = ledger.total_cost("YES")
    down_cost = ledger.total_cost("NO")

    history = list(activity_feed)
    if not history:
        history = rebuild_execution_history(ledger)

    reserved = float(reserved_capital_usd)
    if reserved <= _EPS and pending_order:
        req = float(pending_order.get("requested_qty") or 0.0)
        filled = float(pending_order.get("filled_qty") or 0.0)
        px = float(pending_order.get("limit_price") or 0.0)
        remaining = max(0.0, req - filled)
        reserved = remaining * px if remaining > _EPS and px > 0 else 0.0

    return {
        "positions": {
            "up": {
                "shares": _quantize_shares(m.up_total_shares),
                "total_cost_usd": _money(up_cost),
                "line": format_position_line(m.up_total_shares, up_cost, "YES"),
                "avg_entry_c": _cents(m.up_average_cost),
            },
            "down": {
                "shares": _quantize_shares(m.down_total_shares),
                "total_cost_usd": _money(down_cost),
                "line": format_position_line(m.down_total_shares, down_cost, "NO"),
                "avg_entry_c": _cents(m.down_average_cost),
            },
        },
        "pairing": {
            "up_inventory": _quantize_shares(m.up_total_shares),
            "down_inventory": _quantize_shares(m.down_total_shares),
            "matched_shares": _quantize_shares(matched),
            "unmatched_up": _quantize_shares(m.up_unmatched_shares),
            "unmatched_down": _quantize_shares(m.down_unmatched_shares),
            "pairing_percent": pairing_pct,
            "up_blocks": build_tranche_blocks(ledger, side="YES", slot_size=dca_increment, max_slots=max_slots),
            "down_blocks": build_tranche_blocks(ledger, side="NO", slot_size=dca_increment, max_slots=max_slots),
        },
        "dca": {
            "up_legs": up_legs,
            "down_legs": down_legs,
            "up_progress_shares": f"{_quantize_shares(m.up_total_shares)} / {max_inventory_per_side:g}",
            "down_progress_shares": f"{_quantize_shares(m.down_total_shares)} / {max_inventory_per_side:g}",
            "up_completed_legs": up_filled_legs,
            "down_completed_legs": down_filled_legs,
            "max_slots": max_slots,
        },
        "economics": econ,
        "hedge_monitor": hedge,
        "capital": {
            **cap,
            "reserved_open_orders_usd": _money(reserved),
        },
        "strategy_ui": {
            "state": ui_state,
            "reason": ui_reason,
            "phase": phase.value,
            "seconds_left": int(seconds_left),
            "allow_new_accumulation": bool(allow_new_accumulation),
            "initial_entry_direction": initial_entry_direction,
            "initial_entry_threshold_c": _cents(initial_entry_threshold),
            "initial_entry_rule": (
                f"EXPENSIVE SIDE FIRST ≥ {_cents(initial_entry_threshold):.0f}¢"
                if initial_entry_direction == "EXPENSIVE_SIDE"
                else f"CHEAP SIDE FIRST ≤ {_cents(initial_entry_threshold):.0f}¢"
            ),
        },
        "execution_history": history,
        "decision_feed": list(decision_feed)[:100],
        "pending_order": pending_order,
    }