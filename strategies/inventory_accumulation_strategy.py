"""Adaptive paired inventory accumulation for Polymarket 5-minute UP/DOWN markets.

Accumulate complementary YES (UP) / NO (DOWN) shares at a combined acquisition
cost of <= target (default 98¢), with adaptive DCA, dynamic hedge pricing,
phase-aware risk controls, and persistent pairing ledger.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Optional

from strategies.fok_execution import fok_buy_limit, fok_sell_with_retries
from utils.graceful_shutdown import shutdown_requested
from utils.inventory_strategy_store import AccumulationPhase, inventory_strategy_store
from utils.market_time_phases import MarketPhase, PhasePolicy, resolve_phase
from utils.paired_decision_engine import (
    ActionKind,
    MarketQuoteView,
    PairedStrategyConfig,
    StrategyAction,
    decide,
    max_hedge_prices,
)
from utils.paired_inventory_ledger import PairedInventoryLedger
from utils.profitability_gate import check_executable_depth

if TYPE_CHECKING:
    from bot import MarketWorker

_LOG = logging.getLogger("paired_inventory")
_EPS = 1e-9


def _ledger_path(worker_key: str) -> str:
    safe = worker_key.replace(":", "_")
    base = os.getenv("PAIRED_LEDGER_DIR", ".")
    return os.path.join(base, f"paired_ledger_{safe}.json")


def _cfg_from_worker(worker: "MarketWorker") -> PairedStrategyConfig:
    wc = worker.worker_config
    policy = PhasePolicy(
        early_min_seconds_left=float(getattr(wc, "paired_early_min_seconds_left", 180.0)),
        middle_min_seconds_left=float(getattr(wc, "paired_middle_min_seconds_left", 60.0)),
        late_min_seconds_left=0.0,
        stop_new_accumulation_seconds=float(
            getattr(wc, "paired_stop_new_accumulation_seconds", 90.0)
        ),
        force_exit_eval_seconds=float(getattr(wc, "paired_force_exit_eval_seconds", 45.0)),
    )
    return PairedStrategyConfig(
        initial_entry_threshold=float(
            getattr(wc, "paired_initial_entry_threshold", getattr(wc, "cheap_side_max_price", 0.35))
        ),
        target_combined_cost=float(
            getattr(wc, "target_combined_pair_cost", getattr(wc, "inventory_max_pair_cost", 0.98))
        ),
        max_combined_cost=float(
            getattr(wc, "max_combined_pair_cost", getattr(wc, "inventory_max_pair_cost", 0.98))
        ),
        max_inventory_per_side=float(getattr(wc, "paired_max_inventory_per_side", wc.max_shares)),
        min_order_size=float(wc.min_order_size),
        dca_increment=float(getattr(wc, "paired_dca_increment", wc.order_size_max)),
        max_dca_legs_per_side=int(getattr(wc, "paired_max_dca_legs_per_side", 3)),
        unmatched_inventory_target=float(
            getattr(wc, "paired_unmatched_inventory_target", 5.0)
        ),
        allow_adaptive_dca=bool(getattr(wc, "paired_allow_adaptive_dca", True)),
        enable_dynamic_hedge_pricing=bool(
            getattr(wc, "paired_enable_dynamic_hedge_pricing", True)
        ),
        enable_adaptive_hedge_timeout=bool(
            getattr(wc, "paired_enable_adaptive_hedge_timeout", True)
        ),
        enable_adaptive_risk=bool(getattr(wc, "paired_enable_adaptive_risk", True)),
        fee_rate=float(wc.spread_capture_fee_rate),
        slippage_reserve=float(wc.spread_capture_slippage_buffer),
        min_net_profit_per_pair=float(wc.min_net_profit_per_pair),
        min_orderbook_depth=float(wc.min_orderbook_depth),
        phase_policy=policy,
        exit_max_loss_fraction=float(getattr(wc, "paired_exit_max_loss_fraction", 0.35)),
        live_trading_enabled=bool(getattr(wc, "paired_live_trading_enabled", False)),
    )


class InventoryAccumulationStrategy:
    """Paired UP/DOWN inventory accumulation with adaptive DCA."""

    def __init__(self) -> None:
        self._ledgers: dict[str, PairedInventoryLedger] = {}
        self._last_decision_sig: dict[str, str] = {}
        self._order_in_flight: dict[str, bool] = {}

    def _key(self, worker: "MarketWorker") -> str:
        return worker.worker_config.key

    def _ledger(self, worker: "MarketWorker") -> PairedInventoryLedger:
        key = self._key(worker)
        cid = worker.canonical_market_id() or ""
        ledger = self._ledgers.get(key)
        if ledger is None:
            ledger = self._load_ledger(key)
            self._ledgers[key] = ledger
        if cid and ledger.market_id and ledger.market_id != cid:
            ledger.reset(cid)
            self._persist_ledger(key, ledger)
        elif cid and not ledger.market_id:
            ledger.market_id = cid
        return ledger

    def _load_ledger(self, worker_key: str) -> PairedInventoryLedger:
        path = _ledger_path(worker_key)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return PairedInventoryLedger.from_dict(json.load(f))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                _LOG.warning("failed to load paired ledger %s: %s", path, exc)
        return PairedInventoryLedger()

    def _persist_ledger(self, worker_key: str, ledger: PairedInventoryLedger) -> None:
        path = _ledger_path(worker_key)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(ledger.to_dict(), f, indent=2)
        except OSError as exc:
            _LOG.warning("failed to persist paired ledger %s: %s", path, exc)

    def _quotes(self, worker: "MarketWorker") -> MarketQuoteView:
        worker.refresh_order_book("YES")
        worker.refresh_order_book("NO")
        yes_exe = worker.executable_buy("YES", float(worker.worker_config.min_order_size))
        no_exe = worker.executable_buy("NO", float(worker.worker_config.min_order_size))
        return MarketQuoteView(
            yes_ask=float(worker.best_ask("YES")),
            no_ask=float(worker.best_ask("NO")),
            yes_bid=float(worker.best_bid("YES")),
            no_bid=float(worker.best_bid("NO")),
            yes_ask_size=float(yes_exe[1]) if yes_exe else float(worker.best_ask_size("YES")),
            no_ask_size=float(no_exe[1]) if no_exe else float(worker.best_ask_size("NO")),
            yes_bid_size=float(worker.best_ask_size("YES")),  # best available proxy
            no_bid_size=float(worker.best_ask_size("NO")),
        )

    def _sync_worker_inventory(self, worker: "MarketWorker", ledger: PairedInventoryLedger) -> None:
        """Keep PositionInventory aligned with the pairing ledger (restart-safe rebuild)."""
        inv = worker.inventory
        inv.reset()
        for lot in ledger.lots:
            if lot.shares > _EPS:
                inv.record_buy(lot.side, lot.shares, lot.execution_price)
        inv.realized_pnl = ledger.realized_profit + ledger.settlement_realized

    def _publish_dashboard(
        self,
        worker: "MarketWorker",
        ledger: PairedInventoryLedger,
        cfg: PairedStrategyConfig,
        action: Optional[StrategyAction],
        *,
        seconds_left: float,
        phase: MarketPhase,
    ) -> None:
        m = ledger.metrics()
        max_up, max_down = max_hedge_prices(ledger, cfg.target_combined_cost)
        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        yes_bid = float(worker.best_bid("YES"))
        no_bid = float(worker.best_bid("NO"))
        liq = (
            m.up_unmatched_shares * yes_bid
            + m.down_unmatched_shares * no_bid
            + min(m.up_matched_shares, m.down_matched_shares) * 1.0
        )
        unreal = 0.0
        if m.up_total_shares > _EPS:
            unreal += (yes_bid - m.up_average_cost) * m.up_total_shares
        if m.down_total_shares > _EPS:
            unreal += (no_bid - m.down_average_cost) * m.down_total_shares

        worker.dashboard["strategy_mode"] = "paired_inventory_accumulation"
        worker.dashboard["strategy_name"] = "Paired Inventory Accumulation"
        worker.dashboard["strategy_label"] = "Paired Inventory ≤ 98¢"
        worker.dashboard["execution_type"] = "FOK"
        worker.dashboard["strategy_state"] = (
            action.kind.value.lower() if action else "idle"
        )
        worker.dashboard["market_phase"] = phase.value
        worker.dashboard["seconds_left"] = int(seconds_left)
        worker.dashboard["yes_ask"] = round(yes_ask, 4)
        worker.dashboard["no_ask"] = round(no_ask, 4)
        worker.dashboard["yes_ask_c"] = round(yes_ask * 100, 2)
        worker.dashboard["no_ask_c"] = round(no_ask * 100, 2)
        worker.dashboard["up_inventory"] = round(m.up_total_shares, 4)
        worker.dashboard["down_inventory"] = round(m.down_total_shares, 4)
        worker.dashboard["yes_shares"] = round(m.up_total_shares, 4)
        worker.dashboard["no_shares"] = round(m.down_total_shares, 4)
        worker.dashboard["matched_shares"] = round(
            min(m.up_matched_shares, m.down_matched_shares), 4
        )
        worker.dashboard["unmatched_up"] = round(m.up_unmatched_shares, 4)
        worker.dashboard["unmatched_down"] = round(m.down_unmatched_shares, 4)
        worker.dashboard["up_avg_cost"] = round(m.up_average_cost, 4)
        worker.dashboard["down_avg_cost"] = round(m.down_average_cost, 4)
        worker.dashboard["yes_avg_price"] = round(m.up_average_cost, 4)
        worker.dashboard["no_avg_price"] = round(m.down_average_cost, 4)
        worker.dashboard["yes_avg_price_c"] = round(m.up_average_cost * 100, 2)
        worker.dashboard["no_avg_price_c"] = round(m.down_average_cost * 100, 2)
        worker.dashboard["max_hedge_up"] = round(max_up, 4)
        worker.dashboard["max_hedge_down"] = round(max_down, 4)
        worker.dashboard["max_hedge_up_c"] = round(max_up * 100, 2)
        worker.dashboard["max_hedge_down_c"] = round(max_down * 100, 2)
        pair_cost = m.matched_pair_cost
        worker.dashboard["combined_pair_cost"] = round(pair_cost, 4) if pair_cost else 0.0
        worker.dashboard["combined_pair_cost_c"] = (
            round(pair_cost * 100, 2) if pair_cost else 0.0
        )
        worker.dashboard["pair_avg_price_c"] = worker.dashboard["combined_pair_cost_c"]
        worker.dashboard["dca_legs_up"] = m.dca_legs_yes
        worker.dashboard["dca_legs_down"] = m.dca_legs_no
        worker.dashboard["dca_legs"] = m.dca_legs_yes + m.dca_legs_no
        worker.dashboard["capital_committed"] = round(m.capital_committed, 4)
        worker.dashboard["estimated_liquidation_value"] = round(liq, 4)
        worker.dashboard["realized_pnl"] = round(m.realized_profit, 4)
        worker.dashboard["unrealized_pnl"] = round(unreal, 4)
        worker.dashboard["initial_entry_threshold_c"] = round(
            cfg.initial_entry_threshold * 100, 2
        )
        worker.dashboard["target_combined_cost_c"] = round(cfg.target_combined_cost * 100, 2)
        worker.dashboard["dry_run"] = bool(worker.is_dry_run())
        worker.dashboard["live_trading_enabled"] = bool(cfg.live_trading_enabled)
        if action:
            worker.dashboard["latest_decision"] = action.reason
            worker.dashboard["entry_block_reason"] = (
                action.reason if action.kind in (ActionKind.REJECT, ActionKind.WAIT) else ""
            )
            worker.dashboard["latest_action"] = action.kind.value
        worker.dashboard["position_size"] = round(
            m.up_total_shares + m.down_total_shares, 4
        )
        if m.up_unmatched_shares > m.down_unmatched_shares and m.up_unmatched_shares > _EPS:
            worker.dashboard["position_side"] = "YES"
        elif m.down_unmatched_shares > _EPS:
            worker.dashboard["position_side"] = "NO"
        elif m.up_matched_shares > _EPS:
            worker.dashboard["position_side"] = "PAIRED"
        else:
            worker.dashboard["position_side"] = ""

    def _log_decision(self, worker: "MarketWorker", action: StrategyAction) -> None:
        key = self._key(worker)
        sig = f"{action.kind.value}:{action.side}:{action.reason}"
        if self._last_decision_sig.get(key) == sig:
            return
        self._last_decision_sig[key] = sig
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        print(f"📋 [{action.kind.value}] {label} | {action.reason}")
        _LOG.info(
            json.dumps(
                {
                    "event": "paired_decision",
                    "worker": key,
                    "kind": action.kind.value,
                    "side": action.side,
                    "qty": action.qty,
                    "limit": action.limit_price,
                    "reason": action.reason,
                    "phase": action.phase.value,
                    "projected_pair_cost": action.projected_pair_cost,
                    "ts": time.time(),
                }
            )
        )

    def _trading_allowed(self, worker: "MarketWorker", cfg: PairedStrategyConfig) -> bool:
        # Live execution requires explicit switch; dry_run always allowed for paper fills.
        if worker.is_dry_run():
            return True
        return bool(cfg.live_trading_enabled)

    async def _execute_buy(
        self,
        worker: "MarketWorker",
        ledger: PairedInventoryLedger,
        cfg: PairedStrategyConfig,
        action: StrategyAction,
        *,
        is_dca: bool,
    ) -> None:
        from bot import MIN_SHARES, OrderState

        side = action.side
        if not side:
            return
        key = self._key(worker)
        if self._order_in_flight.get(key):
            return
        if not self._trading_allowed(worker, cfg):
            worker.dashboard["entry_block_reason"] = (
                "LIVE_TRADING_DISABLED: set paired_live_trading_enabled=true and dry_run=false"
            )
            print(
                f"🛑 [LIVE BLOCKED] {worker.asset_type.upper()} {worker.window_slug} | "
                f"paired live trading disabled — decision was: {action.reason}"
            )
            return
        if ledger.total_shares(side) + action.qty > cfg.max_inventory_per_side + _EPS:
            worker.dashboard["latest_decision"] = (
                f"BUY REJECTED: {side} inventory at hard cap {cfg.max_inventory_per_side}"
            )
            return

        exe = worker.executable_buy(side, float(action.qty))
        depth_err = check_executable_depth(
            exe, float(action.qty), min_orderbook_depth=cfg.min_orderbook_depth
        )
        if depth_err:
            worker.dashboard["latest_decision"] = f"BUY REJECTED: {depth_err}"
            return
        limit_px = float(action.limit_price)
        if exe and exe[0] > limit_px + _EPS:
            worker.dashboard["latest_decision"] = (
                "BUY REJECTED: executable ask moved above approved limit"
            )
            return

        purpose = {
            ActionKind.BUY_ENTRY: "paired_entry",
            ActionKind.BUY_DCA: "paired_dca",
            ActionKind.BUY_HEDGE: "paired_hedge",
        }.get(action.kind, "paired_buy")

        self._order_in_flight[key] = True
        try:
            async with worker._order_lock:
                if worker.order_state != OrderState.IDLE:
                    return
                worker.order_state = OrderState.PENDING
                worker.dashboard["strategy_state"] = f"{purpose}_pending"
                qty, fill_px, oid = await fok_buy_limit(
                    worker, side, limit_px, float(action.qty), purpose=purpose,
                )
                worker.order_state = OrderState.IDLE
                if qty <= MIN_SHARES - 1e-9 or fill_px <= 0:
                    worker.dashboard["latest_decision"] = (
                        f"BUY FAILED: FOK not filled for {side} @ {limit_px:.4f}"
                    )
                    return
                if oid and not worker.record_fill_idempotent(oid, side, qty, fill_px):
                    return
                fee = qty * fill_px * cfg.fee_rate
                ledger.record_buy(
                    side,
                    qty,
                    fill_px,
                    fees=fee,
                    order_id=oid,
                    token_id=str(worker._token_id(side) or ""),
                    market_id=worker.canonical_market_id() or "",
                    is_dca=is_dca,
                )
                # Enforce profitability on newly formed pairs.
                ledger.match_fifo(max_combined=cfg.max_combined_cost)
                self._sync_worker_inventory(worker, ledger)
                self._persist_ledger(key, ledger)
                worker.log_trade(side, fill_px, "BUY", size=qty, order_id=oid)
                worker.entry_count = max(worker.entry_count, 1)
                print(
                    f"✅ [PAIRED BUY] {worker.asset_type.upper()} {worker.window_slug} | "
                    f"{side} {qty:.2f} @ {fill_px:.4f} | {action.kind.value}"
                )
                self._update_cycle_phase(worker, ledger, filled_side=side, kind=action.kind)
        finally:
            self._order_in_flight[key] = False

    async def _execute_exit(
        self,
        worker: "MarketWorker",
        ledger: PairedInventoryLedger,
        cfg: PairedStrategyConfig,
        action: StrategyAction,
    ) -> None:
        from bot import MIN_SHARES, OrderState

        side = action.side
        if not side:
            return
        key = self._key(worker)
        if self._order_in_flight.get(key):
            return
        if not self._trading_allowed(worker, cfg):
            worker.dashboard["entry_block_reason"] = "LIVE_TRADING_DISABLED"
            return

        self._order_in_flight[key] = True
        try:
            async with worker._order_lock:
                if worker.order_state != OrderState.IDLE:
                    return
                worker.order_state = OrderState.PENDING
                qty, px, oid = await fok_sell_with_retries(
                    worker,
                    side,
                    float(action.qty),
                    purpose="paired_exit",
                    limit_price=float(action.limit_price),
                    max_retries=worker.worker_config.stop_loss_exit_max_retries,
                    retry_delay_ms=worker.worker_config.stop_loss_exit_retry_ms,
                )
                worker.order_state = OrderState.IDLE
                if qty <= MIN_SHARES - 1e-9 or px <= 0:
                    worker.dashboard["latest_decision"] = "EXIT FAILED: sell not filled"
                    return
                if oid and oid not in ("stop-loss", "dry-run") and not worker.record_fill_idempotent(
                    oid, side, qty, px,
                ):
                    return
                fee = qty * px * cfg.fee_rate
                pnl = ledger.record_sell(side, qty, px, fees=fee)
                self._sync_worker_inventory(worker, ledger)
                self._persist_ledger(key, ledger)
                worker.log_trade(side, px, "SELL", size=qty, order_id=oid)
                worker.log_pnl(
                    "PAIRED_EXIT",
                    round(pnl, 4),
                    {"side": side, "qty": qty, "price": px, "market_id": worker.canonical_market_id()},
                )
                print(
                    f"💸 [PAIRED EXIT] {worker.asset_type.upper()} {worker.window_slug} | "
                    f"{side} {qty:.2f} @ {px:.4f} | pnl={pnl:.4f}"
                )
        finally:
            self._order_in_flight[key] = False

    def _update_cycle_phase(
        self,
        worker: "MarketWorker",
        ledger: PairedInventoryLedger,
        *,
        filled_side: str,
        kind: ActionKind,
    ) -> None:
        cid = worker.canonical_market_id()
        if not cid:
            return
        rec = inventory_strategy_store.get_or_create_cycle(self._key(worker), cid)
        m = ledger.metrics()
        if kind == ActionKind.BUY_ENTRY:
            inventory_strategy_store.start_cycle(
                self._key(worker),
                cid,
                first_leg_side=filled_side,
            )
            return
        if kind == ActionKind.BUY_HEDGE and m.up_unmatched_shares <= _EPS and m.down_unmatched_shares <= _EPS:
            rec.phase = AccumulationPhase.PAIR_COMPLETED
            rec.initial_hedge_complete = True
            rec.target_achieved = bool(
                m.matched_pair_cost is not None
                and m.matched_pair_cost
                <= float(worker.worker_config.target_combined_pair_cost) + _EPS
            )
            inventory_strategy_store.upsert(rec)
        elif kind == ActionKind.BUY_DCA:
            rec.phase = AccumulationPhase.WAITING_FOR_OPPOSITE_ENTRY
            meta = dict(rec.metadata or {})
            meta["dca_legs"] = m.dca_legs_yes + m.dca_legs_no
            rec.metadata = meta
            inventory_strategy_store.upsert(rec)

    async def on_market_tick(self, worker: "MarketWorker") -> None:
        if shutdown_requested():
            return
        cfg = _cfg_from_worker(worker)
        ledger = self._ledger(worker)
        seconds_left = float(worker.market_seconds_left())
        phase = resolve_phase(seconds_left, cfg.phase_policy)

        if not worker.active_market or not worker.canonical_market_id():
            self._publish_dashboard(
                worker, ledger, cfg, None, seconds_left=seconds_left, phase=phase,
            )
            return

        quotes = self._quotes(worker)
        action = decide(ledger, quotes, cfg, seconds_left=seconds_left)
        self._log_decision(worker, action)
        self._publish_dashboard(
            worker, ledger, cfg, action, seconds_left=seconds_left, phase=phase,
        )

        if action.kind in (ActionKind.WAIT, ActionKind.REJECT):
            return
        if worker.order_state.name == "PENDING" or worker._order_lock.locked():
            return

        if action.kind in (ActionKind.BUY_ENTRY, ActionKind.BUY_DCA, ActionKind.BUY_HEDGE):
            await self._execute_buy(
                worker,
                ledger,
                cfg,
                action,
                is_dca=(action.kind == ActionKind.BUY_DCA),
            )
        elif action.kind == ActionKind.EXIT:
            await self._execute_exit(worker, ledger, cfg, action)

        self._publish_dashboard(
            worker,
            ledger,
            cfg,
            action,
            seconds_left=float(worker.market_seconds_left()),
            phase=resolve_phase(float(worker.market_seconds_left()), cfg.phase_policy),
        )

    async def evaluate(self, worker: "MarketWorker"):
        return None

    async def execute(self, worker: "MarketWorker", decision) -> None:
        return None


# Active strategy aliases — bot imports MomentumStrategy.
MomentumStrategy = InventoryAccumulationStrategy
GabagoolStrategy = InventoryAccumulationStrategy
SpreadCaptureStrategy = InventoryAccumulationStrategy
LateEntryStrategy = InventoryAccumulationStrategy

# Re-export directional strategy for callers that still need it.
from strategies.directional_fok_strategy import DirectionalFokStrategy  # noqa: E402
