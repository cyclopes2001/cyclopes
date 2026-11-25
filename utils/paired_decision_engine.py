"""Pure decision engine for adaptive paired inventory accumulation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from utils.adaptive_dca import DcaDecision, DcaEvaluation, evaluate_adaptive_dca
from utils.market_time_phases import MarketPhase, PhasePolicy, allow_new_accumulation, resolve_phase
from utils.paired_inventory_ledger import PairedInventoryLedger


_EPS = 1e-9


class ActionKind(str, Enum):
    WAIT = "WAIT"
    BUY_ENTRY = "BUY_ENTRY"
    BUY_DCA = "BUY_DCA"
    BUY_HEDGE = "BUY_HEDGE"
    EXIT = "EXIT"
    REJECT = "REJECT"


@dataclass(frozen=True)
class StrategyAction:
    kind: ActionKind
    side: Optional[str]
    qty: float
    limit_price: float
    reason: str
    phase: MarketPhase
    max_hedge_up: float = 0.0
    max_hedge_down: float = 0.0
    projected_pair_cost: Optional[float] = None


@dataclass(frozen=True)
class MarketQuoteView:
    yes_ask: float
    no_ask: float
    yes_bid: float
    no_bid: float
    yes_ask_size: float
    no_ask_size: float
    yes_bid_size: float
    no_bid_size: float


@dataclass(frozen=True)
class PairedStrategyConfig:
    initial_entry_threshold: float = 0.35
    target_combined_cost: float = 0.98
    max_combined_cost: float = 0.98
    max_inventory_per_side: float = 15.0
    min_order_size: float = 5.0
    dca_increment: float = 5.0
    max_dca_legs_per_side: int = 3
    unmatched_inventory_target: float = 5.0
    allow_adaptive_dca: bool = True
    enable_dynamic_hedge_pricing: bool = True
    enable_adaptive_hedge_timeout: bool = True
    enable_adaptive_risk: bool = True
    fee_rate: float = 0.0
    slippage_reserve: float = 0.002
    min_net_profit_per_pair: float = 0.02
    min_orderbook_depth: float = 5.0
    phase_policy: PhasePolicy = PhasePolicy()
    exit_max_loss_fraction: float = 0.35
    live_trading_enabled: bool = False


def _side_label(side: str) -> str:
    return "UP" if side.upper() == "YES" else "DOWN"


def max_hedge_prices(ledger: PairedInventoryLedger, target: float) -> Tuple[float, float]:
    """Return (max_up_price, max_down_price) from unmatched opposite inventory."""
    max_up = 0.0
    max_down = 0.0
    if ledger.unmatched_shares("NO") > _EPS:
        max_up = ledger.max_hedge_price("NO", target_combined=target)
    if ledger.unmatched_shares("YES") > _EPS:
        max_down = ledger.max_hedge_price("YES", target_combined=target)
    return max_up, max_down


def evaluate_hedge(
    ledger: PairedInventoryLedger,
    quotes: MarketQuoteView,
    cfg: PairedStrategyConfig,
    *,
    phase: MarketPhase,
) -> Optional[StrategyAction]:
    max_up, max_down = max_hedge_prices(ledger, cfg.target_combined_cost)
    unmatched_yes = ledger.unmatched_shares("YES")
    unmatched_no = ledger.unmatched_shares("NO")

    candidates = []
    if unmatched_yes > _EPS and quotes.no_ask > 0:
        qty = round(min(unmatched_yes, cfg.dca_increment, quotes.no_ask_size), 4)
        if qty + _EPS >= cfg.min_order_size and quotes.no_ask <= max_down + _EPS:
            fee_adj = quotes.no_ask * (1.0 + cfg.fee_rate) + cfg.slippage_reserve
            pair_cost = ledger.unmatched_average_cost("YES") + fee_adj
            if pair_cost <= cfg.max_combined_cost + 1e-6:
                candidates.append(
                    StrategyAction(
                        kind=ActionKind.BUY_HEDGE,
                        side="NO",
                        qty=min(qty, unmatched_yes),
                        limit_price=quotes.no_ask,
                        reason=(
                            f"BUY DOWN APPROVED: {qty:.0f} shares available at "
                            f"{quotes.no_ask * 100:.1f}¢; projected combined cost "
                            f"including fees is {pair_cost * 100:.1f}¢."
                        ),
                        phase=phase,
                        max_hedge_up=max_up,
                        max_hedge_down=max_down,
                        projected_pair_cost=pair_cost,
                    )
                )
        elif unmatched_yes > _EPS and quotes.no_ask > max_down + _EPS:
            candidates.append(
                StrategyAction(
                    kind=ActionKind.REJECT,
                    side="NO",
                    qty=0.0,
                    limit_price=quotes.no_ask,
                    reason="HEDGE REJECTED: Opposite-side ask exceeds maximum permissible hedge price.",
                    phase=phase,
                    max_hedge_up=max_up,
                    max_hedge_down=max_down,
                )
            )

    if unmatched_no > _EPS and quotes.yes_ask > 0:
        qty = round(min(unmatched_no, cfg.dca_increment, quotes.yes_ask_size), 4)
        if qty + _EPS >= cfg.min_order_size and quotes.yes_ask <= max_up + _EPS:
            fee_adj = quotes.yes_ask * (1.0 + cfg.fee_rate) + cfg.slippage_reserve
            pair_cost = ledger.unmatched_average_cost("NO") + fee_adj
            if pair_cost <= cfg.max_combined_cost + 1e-6:
                candidates.append(
                    StrategyAction(
                        kind=ActionKind.BUY_HEDGE,
                        side="YES",
                        qty=min(qty, unmatched_no),
                        limit_price=quotes.yes_ask,
                        reason=(
                            f"BUY UP APPROVED: {qty:.0f} shares available at "
                            f"{quotes.yes_ask * 100:.1f}¢; projected combined cost "
                            f"including fees is {pair_cost * 100:.1f}¢."
                        ),
                        phase=phase,
                        max_hedge_up=max_up,
                        max_hedge_down=max_down,
                        projected_pair_cost=pair_cost,
                    )
                )
        elif unmatched_no > _EPS and quotes.yes_ask > max_up + _EPS:
            candidates.append(
                StrategyAction(
                    kind=ActionKind.REJECT,
                    side="YES",
                    qty=0.0,
                    limit_price=quotes.yes_ask,
                    reason="HEDGE REJECTED: Opposite-side ask exceeds maximum permissible hedge price.",
                    phase=phase,
                    max_hedge_up=max_up,
                    max_hedge_down=max_down,
                )
            )

    buys = [c for c in candidates if c.kind == ActionKind.BUY_HEDGE]
    if buys:
        buys.sort(key=lambda a: (a.projected_pair_cost or 99.0, a.limit_price))
        return buys[0]
    rejects = [c for c in candidates if c.kind == ActionKind.REJECT]
    return rejects[0] if rejects else None


def evaluate_initial_entry(
    ledger: PairedInventoryLedger,
    quotes: MarketQuoteView,
    cfg: PairedStrategyConfig,
    *,
    phase: MarketPhase,
    seconds_left: float,
) -> Optional[StrategyAction]:
    if not allow_new_accumulation(seconds_left, cfg.phase_policy):
        return StrategyAction(
            kind=ActionKind.REJECT,
            side=None,
            qty=0.0,
            limit_price=0.0,
            reason="BUY REJECTED: late phase — no new directional accumulation",
            phase=phase,
        )
    if ledger.unmatched_shares("YES") > _EPS or ledger.unmatched_shares("NO") > _EPS:
        return None  # hedge / DCA path owns unmatched inventory

    candidates = []
    for side, ask, depth in (
        ("YES", quotes.yes_ask, quotes.yes_ask_size),
        ("NO", quotes.no_ask, quotes.no_ask_size),
    ):
        if ask <= 0 or ask > cfg.initial_entry_threshold + _EPS:
            continue
        headroom = cfg.max_inventory_per_side - ledger.total_shares(side)
        qty = min(cfg.min_order_size, cfg.dca_increment, depth, headroom)
        if qty + _EPS < cfg.min_order_size:
            continue
        if depth + _EPS < cfg.min_orderbook_depth:
            continue
        candidates.append((side, ask, qty, depth))

    if not candidates:
        return StrategyAction(
            kind=ActionKind.WAIT,
            side=None,
            qty=0.0,
            limit_price=0.0,
            reason="WAIT: no side below initial entry threshold with capacity",
            phase=phase,
        )

    candidates.sort(key=lambda row: (row[1], -row[3]))
    side, ask, qty, _ = candidates[0]
    max_up, max_down = max_hedge_prices(ledger, cfg.target_combined_cost)
    return StrategyAction(
        kind=ActionKind.BUY_ENTRY,
        side=side,
        qty=qty,
        limit_price=ask,
        reason=(
            f"BUY {_side_label(side)} APPROVED: initial entry {qty:.0f} shares at "
            f"{ask * 100:.1f}¢ (threshold {cfg.initial_entry_threshold * 100:.0f}¢)."
        ),
        phase=phase,
        max_hedge_up=max_up,
        max_hedge_down=max_down,
    )


def evaluate_dca_action(
    ledger: PairedInventoryLedger,
    quotes: MarketQuoteView,
    cfg: PairedStrategyConfig,
    *,
    phase: MarketPhase,
    seconds_left: float,
) -> Optional[StrategyAction]:
    if not cfg.allow_adaptive_dca:
        return None
    if phase == MarketPhase.LATE:
        return StrategyAction(
            kind=ActionKind.REJECT,
            side=None,
            qty=0.0,
            limit_price=0.0,
            reason="BUY REJECTED: LATE phase — speculative DCA disabled",
            phase=phase,
        )
    if not allow_new_accumulation(seconds_left, cfg.phase_policy):
        return None

    unmatched_yes = ledger.unmatched_shares("YES")
    unmatched_no = ledger.unmatched_shares("NO")
    if unmatched_yes <= _EPS and unmatched_no <= _EPS:
        return None
    if unmatched_yes > _EPS and unmatched_no > _EPS:
        return None  # both unmatched should not happen with proper matching

    if unmatched_yes > _EPS:
        side = "YES"
        ask = quotes.yes_ask
        depth = quotes.yes_ask_size
        opposite = quotes.no_ask
        legs = ledger.dca_legs_yes
        existing = unmatched_yes
        # Cost of unmatched only
        cost = ledger.unmatched_average_cost("YES") * unmatched_yes
    else:
        side = "NO"
        ask = quotes.no_ask
        depth = quotes.no_ask_size
        opposite = quotes.yes_ask
        legs = ledger.dca_legs_no
        existing = unmatched_no
        cost = ledger.unmatched_average_cost("NO") * unmatched_no

    # Only consider DCA when at/above soft unmatched target and price is falling.
    if existing + _EPS < cfg.unmatched_inventory_target:
        return None
    if ask <= 0:
        return None

    qty = min(cfg.dca_increment, depth, cfg.max_inventory_per_side - ledger.total_shares(side))
    if qty + _EPS < cfg.min_order_size:
        return None

    ev: DcaEvaluation = evaluate_adaptive_dca(
        side=side,
        existing_shares=existing,
        existing_total_cost=cost,
        proposed_qty=qty,
        proposed_price=ask,
        opposite_ask=opposite,
        target_combined=cfg.target_combined_cost,
        max_inventory=cfg.max_inventory_per_side,
        unmatched_target=cfg.unmatched_inventory_target,
        max_dca_legs=cfg.max_dca_legs_per_side,
        current_dca_legs=legs,
        fee_rate=cfg.fee_rate,
        slippage_reserve=cfg.slippage_reserve,
        seconds_left=seconds_left,
        min_seconds_left=cfg.phase_policy.stop_new_accumulation_seconds,
        available_liquidity=depth,
        require_hedge_feasible_after=True,
    )
    max_up, max_down = max_hedge_prices(ledger, cfg.target_combined_cost)
    if ev.decision == DcaDecision.APPROVE:
        return StrategyAction(
            kind=ActionKind.BUY_DCA,
            side=side,
            qty=qty,
            limit_price=ask,
            reason=ev.reason,
            phase=phase,
            max_hedge_up=max_up,
            max_hedge_down=max_down,
            projected_pair_cost=ev.projected_pair_cost,
        )
    return StrategyAction(
        kind=ActionKind.REJECT,
        side=side,
        qty=0.0,
        limit_price=ask,
        reason=ev.reason,
        phase=phase,
        max_hedge_up=max_up,
        max_hedge_down=max_down,
        projected_pair_cost=ev.projected_pair_cost,
    )


def evaluate_exit(
    ledger: PairedInventoryLedger,
    quotes: MarketQuoteView,
    cfg: PairedStrategyConfig,
    *,
    phase: MarketPhase,
    seconds_left: float,
) -> Optional[StrategyAction]:
    unmatched_yes = ledger.unmatched_shares("YES")
    unmatched_no = ledger.unmatched_shares("NO")
    if unmatched_yes <= _EPS and unmatched_no <= _EPS:
        return None

    side = "YES" if unmatched_yes >= unmatched_no else "NO"
    qty = unmatched_yes if side == "YES" else unmatched_no
    bid = quotes.yes_bid if side == "YES" else quotes.no_bid
    depth = quotes.yes_bid_size if side == "YES" else quotes.no_bid_size
    avg = ledger.unmatched_average_cost(side)
    max_up, max_down = max_hedge_prices(ledger, cfg.target_combined_cost)
    opp_ask = quotes.no_ask if side == "YES" else quotes.yes_ask
    max_hedge = max_down if side == "YES" else max_up

    if bid <= 0 or depth + _EPS < cfg.min_order_size:
        return StrategyAction(
            kind=ActionKind.WAIT,
            side=side,
            qty=0.0,
            limit_price=0.0,
            reason="WAIT: unmatched inventory but insufficient bid liquidity to exit",
            phase=phase,
            max_hedge_up=max_up,
            max_hedge_down=max_down,
        )

    liquidation = bid * qty
    acquisition = avg * qty
    loss_frac = 0.0 if acquisition <= _EPS else max(0.0, (acquisition - liquidation) / acquisition)
    hedge_gap = max(0.0, opp_ask - max_hedge) if max_hedge > _EPS else 1.0

    # Prefer waiting if hedge is close and time remains.
    if seconds_left > cfg.phase_policy.force_exit_eval_seconds and hedge_gap <= 0.03:
        return StrategyAction(
            kind=ActionKind.WAIT,
            side=side,
            qty=0.0,
            limit_price=0.0,
            reason="WAIT: hedge still within reach; continue monitoring opposite ask",
            phase=phase,
            max_hedge_up=max_up,
            max_hedge_down=max_down,
        )

    force = seconds_left <= cfg.phase_policy.force_exit_eval_seconds
    severe = loss_frac >= cfg.exit_max_loss_fraction and hedge_gap > 0.05
    if force or severe:
        exit_qty = min(qty, depth, cfg.dca_increment) if not force else min(qty, depth)
        if exit_qty + _EPS < cfg.min_order_size and depth + _EPS >= cfg.min_order_size:
            exit_qty = cfg.min_order_size
        if exit_qty + _EPS < cfg.min_order_size:
            return StrategyAction(
                kind=ActionKind.WAIT,
                side=side,
                qty=0.0,
                limit_price=0.0,
                reason="WAIT: exit size below minimum order size",
                phase=phase,
                max_hedge_up=max_up,
                max_hedge_down=max_down,
            )
        return StrategyAction(
            kind=ActionKind.EXIT,
            side=side,
            qty=exit_qty,
            limit_price=bid,
            reason=(
                f"EXIT {_side_label(side)}: unmatched inventory risk exceeds benefit of waiting "
                f"(loss_frac={loss_frac:.2f}, hedge_gap={hedge_gap:.3f}, t={seconds_left:.0f}s)."
            ),
            phase=phase,
            max_hedge_up=max_up,
            max_hedge_down=max_down,
        )
    return StrategyAction(
        kind=ActionKind.WAIT,
        side=side,
        qty=0.0,
        limit_price=0.0,
        reason="WAIT: holding unmatched inventory; hedge not yet attractive and exit not justified",
        phase=phase,
        max_hedge_up=max_up,
        max_hedge_down=max_down,
    )


def decide(
    ledger: PairedInventoryLedger,
    quotes: MarketQuoteView,
    cfg: PairedStrategyConfig,
    *,
    seconds_left: float,
) -> StrategyAction:
    phase = resolve_phase(seconds_left, cfg.phase_policy)
    max_up, max_down = max_hedge_prices(ledger, cfg.target_combined_cost)

    if phase == MarketPhase.CLOSED:
        return StrategyAction(
            kind=ActionKind.WAIT,
            side=None,
            qty=0.0,
            limit_price=0.0,
            reason="WAIT: market closed / resolved",
            phase=phase,
            max_hedge_up=max_up,
            max_hedge_down=max_down,
        )

    # 1) Always prioritize a valid hedge when possible.
    hedge = evaluate_hedge(ledger, quotes, cfg, phase=phase)
    if hedge and hedge.kind == ActionKind.BUY_HEDGE:
        return hedge

    # 2) Late: exit evaluation before new risk.
    if phase == MarketPhase.LATE or seconds_left <= cfg.phase_policy.force_exit_eval_seconds:
        exit_act = evaluate_exit(ledger, quotes, cfg, phase=phase, seconds_left=seconds_left)
        if exit_act and exit_act.kind == ActionKind.EXIT:
            return exit_act

    # 3) Adaptive DCA on heavy side (only if unmatched exists).
    dca = evaluate_dca_action(ledger, quotes, cfg, phase=phase, seconds_left=seconds_left)
    if dca and dca.kind == ActionKind.BUY_DCA:
        return dca

    # 4) Initial entry when flat / matched-only.
    entry = evaluate_initial_entry(ledger, quotes, cfg, phase=phase, seconds_left=seconds_left)
    if entry and entry.kind == ActionKind.BUY_ENTRY:
        return entry

    if hedge and hedge.kind == ActionKind.REJECT:
        # Surface hedge rejection as latest reason while waiting.
        if dca and dca.kind == ActionKind.REJECT:
            return dca
        return hedge

    if dca and dca.kind == ActionKind.REJECT:
        return dca

    if entry:
        return entry

    return StrategyAction(
        kind=ActionKind.WAIT,
        side=None,
        qty=0.0,
        limit_price=0.0,
        reason="WAIT: no actionable entry, hedge, DCA, or exit",
        phase=phase,
        max_hedge_up=max_up,
        max_hedge_down=max_down,
    )
