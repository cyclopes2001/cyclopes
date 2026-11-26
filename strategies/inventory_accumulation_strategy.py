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
    InitialEntryDirection,
    ActionKind,
    MarketQuoteView,
    PairedStrategyConfig,
    StrategyAction,
    decide,
    max_hedge_prices,
)
from utils.paired_dashboard_payload import build_paired_dashboard
from utils.paired_inventory_ledger import PairedInventoryLedger
from utils.profitability_gate import check_executable_depth
from utils.market_time_phases import allow_new_accumulation as phase_allows_new_accumulation
from utils.velocity_engine import OrderBookVelocityEngine, load_velocity_config
from utils.velocity_engine.entry_selection import classify_proposed_order
from utils.velocity_engine.paper import VelocityPaperJournal

if TYPE_CHECKING:
    from bot import MarketWorker

_LOG = logging.getLogger("paired_inventory")
_EPS = 1e-9


def _cents(x: float) -> float:
    return round(float(x) * 100, 2)


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
        stop_initial_entry_seconds=float(
            getattr(wc, "paired_stop_initial_entry_seconds", 120.0)
        ),
        stop_unmatched_dca_seconds=float(
            getattr(wc, "paired_stop_unmatched_dca_seconds", 90.0)
        ),
        hedge_priority_seconds=float(getattr(wc, "paired_hedge_priority_seconds", 60.0)),
        final_risk_seconds=float(getattr(wc, "paired_final_risk_seconds", 30.0)),
    )
    dir_raw = str(getattr(wc, "paired_initial_entry_direction", "CHEAPER_SIDE")).upper()
    if dir_raw == "CHEAP_SIDE":
        dir_raw = "CHEAPER_SIDE"
    try:
        entry_dir = InitialEntryDirection(dir_raw)
    except ValueError:
        entry_dir = InitialEntryDirection.CHEAPER_SIDE
    return PairedStrategyConfig(
        initial_entry_threshold=float(getattr(wc, "paired_initial_entry_threshold", 0.35)),
        initial_entry_direction=entry_dir,
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
        max_unmatched_shares_per_side=float(
            getattr(
                wc,
                "paired_max_unmatched_shares_per_side",
                getattr(wc, "paired_unmatched_inventory_target", 5.0),
            )
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
        self._activity_feed: dict[str, list] = {}
        self._decision_feed: dict[str, list] = {}
        self._velocity_engines: dict[str, OrderBookVelocityEngine] = {}
        self._velocity_paper: dict[str, VelocityPaperJournal] = {}

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
            vel = self._velocity_engines.get(key)
            if vel is not None:
                vel.reset(cid)
            worker.dashboard["paired_initial_entry_fills"] = 0
            worker.dashboard["paired_dca_fills"] = 0
            worker.dashboard["paired_hedge_fills"] = 0
            worker.dashboard["paired_total_fills"] = 0
            worker.entry_count = 0
        elif cid and not ledger.market_id:
            ledger.market_id = cid
        return ledger

    def _load_ledger(self, worker_key: str) -> PairedInventoryLedger:
        path = _ledger_path(worker_key)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._restore_feeds(worker_key, raw)
                return PairedInventoryLedger.from_dict(raw)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                _LOG.warning("failed to load paired ledger %s: %s", path, exc)
        return PairedInventoryLedger()

    def _persist_ledger(self, worker_key: str, ledger: PairedInventoryLedger) -> None:
        path = _ledger_path(worker_key)
        try:
            blob = ledger.to_dict()
            blob["activity_feed"] = list(self._activity_feed.get(worker_key) or [])[-200:]
            blob["decision_feed"] = list(self._decision_feed.get(worker_key) or [])[-200:]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(blob, f, indent=2)
        except OSError as exc:
            _LOG.warning("failed to persist paired ledger %s: %s", path, exc)

    def _restore_feeds(self, worker_key: str, raw: dict) -> None:
        if worker_key not in self._activity_feed:
            self._activity_feed[worker_key] = list(raw.get("activity_feed") or [])
        if worker_key not in self._decision_feed:
            self._decision_feed[worker_key] = list(raw.get("decision_feed") or [])

    def _push_decision(self, worker: "MarketWorker", message: str, *, category: str = "decision") -> None:
        key = self._key(worker)
        feed = self._decision_feed.setdefault(key, [])
        entry = {"timestamp": time.time(), "message": message, "category": category}
        if not feed or feed[0].get("message") != message:
            feed.insert(0, entry)
            del feed[200:]

    def _push_activity(
        self,
        worker: "MarketWorker",
        *,
        event_type: str,
        explanation: str,
        category: str = "execution",
        side: str = "",
        qty: float = 0.0,
        price: float = 0.0,
        cost_usd: float = 0.0,
        status: str = "FILLED",
        order_id: Optional[str] = None,
    ) -> None:
        key = self._key(worker)
        feed = self._activity_feed.setdefault(key, [])
        label = "UP" if side.upper() == "YES" else ("DOWN" if side.upper() == "NO" else "")
        feed.append(
            {
                "timestamp": time.time(),
                "event_type": event_type,
                "category": category,
                "side": side.upper() if side else "",
                "side_label": label,
                "qty": round(float(qty), 4),
                "price": round(float(price), 4),
                "price_c": round(float(price) * 100, 2) if price > 0 else 0.0,
                "cost_usd": round(float(cost_usd), 2),
                "status": status,
                "explanation": explanation,
                "order_id": order_id,
            }
        )
        del feed[:-200]

    def _reserved_capital(self, worker: "MarketWorker") -> float:
        pending = worker.dashboard.get("paired_pending_order") or {}
        req = float(pending.get("requested_qty") or 0.0)
        filled = float(pending.get("filled_qty") or 0.0)
        px = float(pending.get("limit_price") or 0.0)
        remaining = max(0.0, req - filled)
        return remaining * px if remaining > 0 and px > 0 else 0.0

    def _ensure_velocity_ws_hooks(self, worker: "MarketWorker") -> None:
        key = self._key(worker)
        if getattr(worker, "_velocity_hooks_key", None) == key:
            return
        worker._velocity_hooks_key = key

        def _ingest(side: str, bid: float, ask: float, bid_sz: float, ask_sz: float) -> None:
            vcfg = load_velocity_config(worker.worker_config)
            if not vcfg.engine_enabled:
                return
            eng = self._velocity_engine(worker)
            eng.ingest_ws_quote(
                side,
                best_bid=float(bid),
                best_ask=float(ask),
                best_bid_size=float(bid_sz) if bid_sz > 0 else 0.0,
                best_ask_size=float(ask_sz) if ask_sz > 0 else float(worker.best_ask_size(side)),
            )

        def _disconnect() -> None:
            eng = self._velocity_engines.get(key)
            if eng is not None:
                eng.mark_ws_disconnected()

        worker._velocity_ws_handler = _ingest
        worker._velocity_ws_disconnect = _disconnect

    def _velocity_engine(self, worker: "MarketWorker") -> OrderBookVelocityEngine:
        key = self._key(worker)
        eng = self._velocity_engines.get(key)
        if eng is None:
            eng = OrderBookVelocityEngine(load_velocity_config(worker.worker_config))
            self._velocity_engines[key] = eng
        return eng

    def _update_velocity(self, worker: "MarketWorker"):
        vcfg = load_velocity_config(worker.worker_config)
        if not vcfg.engine_enabled:
            worker.dashboard["order_book_velocity"] = {"enabled": False}
            return None
        eng = self._velocity_engine(worker)
        eng.cfg = vcfg
        cid = worker.canonical_market_id() or ""
        worker.refresh_order_book("YES", max_age_seconds=0.35)
        worker.refresh_order_book("NO", max_age_seconds=0.35)
        age_s = worker.max_quote_age_seconds()
        age_ms = (age_s * 1000.0) if age_s is not None else None
        if getattr(worker, "_ws_market_data_stale", False) and age_ms is not None:
            age_ms = max(age_ms, vcfg.max_book_age_ms + 1.0)
        snap = eng.update_from_worker(
            market_id=cid,
            yes_book=worker.cached_order_book("YES"),
            no_book=worker.cached_order_book("NO"),
            quote_age_ms=age_ms,
        )
        obv = snap.decision.to_dashboard_dict()
        vcfg = load_velocity_config(worker.worker_config)
        if vcfg.paper_mode:
            journal = self._velocity_paper.setdefault(self._key(worker), VelocityPaperJournal())
            up_mid = snap.context.up.best_ask or worker.best_ask("YES")
            dn_mid = snap.context.down.best_ask or worker.best_ask("NO")
            journal.advance(yes_mid=float(up_mid), no_mid=float(dn_mid))
            obv["paper"] = journal.summary()
        worker.dashboard["order_book_velocity"] = obv
        return snap

    def _log_velocity_audit(
        self,
        worker: "MarketWorker",
        ledger: PairedInventoryLedger,
        snap,
        action: Optional[StrategyAction],
        *,
        seconds_left: float,
    ) -> None:
        if snap is None:
            return
        from utils.settlement_risk import settlement_outcomes

        m = ledger.metrics()
        outlook = settlement_outcomes(
            yes_shares=m.up_total_shares,
            no_shares=m.down_total_shares,
            total_cost=m.capital_committed,
        )
        slug = worker.market_slug or worker.canonical_market_id() or ""
        up = snap.context.up
        down = snap.context.down
        extras = {
            "event": "velocity_decision_audit",
            "worker": self._key(worker),
            "market_slug": slug,
            "seconds_left": seconds_left,
            "up_best_bid": up.best_bid,
            "up_best_ask": up.best_ask,
            "down_best_bid": down.best_bid,
            "down_best_ask": down.best_ask,
            "yes_shares_before": m.up_total_shares,
            "no_shares_before": m.down_total_shares,
            "matched_pairs": outlook.matched_pairs,
            "unmatched_yes": m.up_unmatched_shares,
            "unmatched_no": m.down_unmatched_shares,
            "capital_committed": m.capital_committed,
            "pnl_if_yes_wins": outlook.pnl_if_yes_wins,
            "pnl_if_no_wins": outlook.pnl_if_no_wins,
            "worst_case_pnl": outlook.worst_case_pnl,
            "action_kind": action.kind.value if action else None,
            "action_side": action.side if action else None,
            "action_reason": action.reason if action else None,
            "projected_pair_cost": action.projected_pair_cost if action else None,
        }
        _LOG.info(json.dumps(snap.decision.to_audit_dict(extras=extras)))

    def _paper_block_velocity_live(
        self,
        worker: "MarketWorker",
        action: StrategyAction,
        *,
        apply_velocity: bool,
    ) -> StrategyAction:
        vcfg = load_velocity_config(worker.worker_config)
        if not (vcfg.paper_mode and vcfg.entry_enabled and apply_velocity):
            return action
        if worker.is_dry_run():
            return action
        if action.kind not in (ActionKind.BUY_ENTRY, ActionKind.BUY_DCA):
            return action
        return StrategyAction(
            kind=ActionKind.WAIT,
            side=action.side,
            qty=0.0,
            limit_price=action.limit_price,
            reason=(
                f"PAPER: velocity-driven {action.kind.value} blocked for live account — "
                f"{action.reason}"
            ),
            phase=action.phase,
            max_hedge_up=action.max_hedge_up,
            max_hedge_down=action.max_hedge_down,
            projected_pair_cost=action.projected_pair_cost,
        )

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
        from bot import OrderState

        key = self._key(worker)
        yes_ask = float(worker.best_ask("YES"))
        no_ask = float(worker.best_ask("NO"))
        yes_bid = float(worker.best_bid("YES"))
        no_bid = float(worker.best_bid("NO"))
        m = ledger.metrics()
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

        pending = worker.dashboard.get("paired_pending_order")
        order_pending = worker.order_state == OrderState.PENDING
        market_outcome = str(worker.dashboard.get("outcome") or "PENDING").upper()
        market_resolved = market_outcome in ("YES", "NO", "UP", "DOWN", "RESOLVED")

        paired = build_paired_dashboard(
            ledger,
            target_combined=cfg.target_combined_cost,
            fee_rate=cfg.fee_rate,
            slippage_reserve=cfg.slippage_reserve,
            dca_increment=cfg.dca_increment,
            max_dca_legs=cfg.max_dca_legs_per_side,
            max_inventory_per_side=cfg.max_inventory_per_side,
            yes_ask=yes_ask,
            no_ask=no_ask,
            phase=phase,
            seconds_left=seconds_left,
            action=action,
            pending_order=pending,
            order_pending=order_pending,
            activity_feed=self._activity_feed.get(key, []),
            decision_feed=self._decision_feed.get(key, []),
            market_resolved=market_resolved,
            reserved_capital_usd=self._reserved_capital(worker),
            allow_new_accumulation=phase_allows_new_accumulation(seconds_left, cfg.phase_policy),
            initial_entry_threshold=cfg.initial_entry_threshold,
            initial_entry_direction=cfg.initial_entry_direction.value,
        )

        from utils.strategy_display import paired_strategy_label

        worker.dashboard["strategy_mode"] = "paired_inventory_accumulation"
        worker.dashboard["strategy_name"] = "Paired Inventory Accumulation"
        worker.dashboard["strategy_label"] = paired_strategy_label(
            cfg.target_combined_cost,
            initial_entry_max_c=cfg.initial_entry_threshold * 100,
            cheaper_side_first=cfg.initial_entry_direction == InitialEntryDirection.CHEAPER_SIDE,
        )
        worker.dashboard["execution_type"] = "FOK"
        worker.dashboard["paired"] = paired
        worker.dashboard["strategy_state"] = action.kind.value.lower() if action else "idle"
        worker.dashboard["strategy_ui_state"] = paired["strategy_ui"]["state"]
        worker.dashboard["strategy_ui_reason"] = paired["strategy_ui"]["reason"]
        worker.dashboard["market_phase"] = phase.value
        worker.dashboard["seconds_left"] = int(seconds_left)
        worker.dashboard["yes_ask"] = round(yes_ask, 4)
        worker.dashboard["no_ask"] = round(no_ask, 4)
        worker.dashboard["yes_ask_c"] = round(yes_ask * 100, 2)
        worker.dashboard["no_ask_c"] = round(no_ask * 100, 2)
        worker.dashboard["yes_shares"] = paired["positions"]["up"]["shares"]
        worker.dashboard["no_shares"] = paired["positions"]["down"]["shares"]
        worker.dashboard["up_inventory"] = paired["positions"]["up"]["shares"]
        worker.dashboard["down_inventory"] = paired["positions"]["down"]["shares"]
        worker.dashboard["position_up_line"] = paired["positions"]["up"]["line"]
        worker.dashboard["position_down_line"] = paired["positions"]["down"]["line"]
        worker.dashboard["matched_shares"] = paired["pairing"]["matched_shares"]
        worker.dashboard["unmatched_up"] = paired["pairing"]["unmatched_up"]
        worker.dashboard["unmatched_down"] = paired["pairing"]["unmatched_down"]
        worker.dashboard["pairing_percent"] = paired["pairing"]["pairing_percent"]
        worker.dashboard["yes_avg_price_c"] = paired["positions"]["up"]["avg_entry_c"]
        worker.dashboard["no_avg_price_c"] = paired["positions"]["down"]["avg_entry_c"]
        worker.dashboard["combined_pair_cost_c"] = paired["economics"]["combined_pair_cost_c"]
        worker.dashboard["pair_avg_price_c"] = paired["economics"]["combined_pair_cost_c"]
        worker.dashboard["target_combined_cost_c"] = paired["economics"]["target_combined_c"]
        worker.dashboard["economics_status"] = paired["economics"]["economics_status"]
        worker.dashboard["max_hedge_up_c"] = paired["hedge_monitor"]["max_hedge_up_c"]
        worker.dashboard["max_hedge_down_c"] = paired["hedge_monitor"]["max_hedge_down_c"]
        worker.dashboard["capital_committed"] = paired["capital"]["total_capital_usd"]
        worker.dashboard["dca_legs"] = (
            paired["dca"]["up_completed_legs"] + paired["dca"]["down_completed_legs"]
        )
        worker.dashboard["estimated_liquidation_value"] = round(liq, 4)
        worker.dashboard["realized_pnl"] = paired["economics"]["realized_pnl_usd"]
        worker.dashboard["unrealized_pnl"] = round(unreal, 4)
        worker.dashboard["initial_entry_threshold_c"] = round(cfg.initial_entry_threshold * 100, 2)
        worker.dashboard["initial_entry_direction"] = cfg.initial_entry_direction.value
        from utils.settlement_risk import settlement_outcomes

        outlook = settlement_outcomes(
            yes_shares=m.up_total_shares,
            no_shares=m.down_total_shares,
            total_cost=m.capital_committed,
        )
        um_y = m.up_unmatched_shares
        um_n = m.down_unmatched_shares
        at_cap = um_y >= cfg.max_unmatched_shares_per_side - _EPS or um_n >= cfg.max_unmatched_shares_per_side - _EPS
        worker.dashboard["settlement_risk"] = {
            "yes_shares": outlook.yes_shares,
            "no_shares": outlook.no_shares,
            "matched_pairs": outlook.matched_pairs,
            "unmatched_yes": um_y,
            "unmatched_no": um_n,
            "pnl_if_yes_wins": outlook.pnl_if_yes_wins,
            "pnl_if_no_wins": outlook.pnl_if_no_wins,
            "worst_case_pnl": outlook.worst_case_pnl,
            "best_case_pnl": outlook.best_case_pnl,
            "seconds_left": int(seconds_left),
            "unmatched_warning": at_cap,
        }
        worker.dashboard["initial_entry_rule"] = (
            f"EXPENSIVE SIDE FIRST ≥ {cfg.initial_entry_threshold * 100:.0f}¢"
            if cfg.initial_entry_direction == InitialEntryDirection.EXPENSIVE_SIDE
            else f"CHEAPER SIDE FIRST ≤ {cfg.initial_entry_threshold * 100:.0f}¢"
        )
        worker.dashboard["dry_run"] = bool(worker.is_dry_run())
        worker.dashboard["live_trading_enabled"] = bool(cfg.live_trading_enabled)
        worker.dashboard["allow_new_accumulation"] = paired["strategy_ui"]["allow_new_accumulation"]
        if action:
            worker.dashboard["latest_decision"] = action.reason
            worker.dashboard["entry_block_reason"] = (
                action.reason if action.kind in (ActionKind.REJECT, ActionKind.WAIT) else ""
            )
            worker.dashboard["latest_action"] = action.kind.value
        worker.dashboard["position_size"] = (
            paired["positions"]["up"]["shares"] + paired["positions"]["down"]["shares"]
        )
        if paired["pairing"]["unmatched_up"] > paired["pairing"]["unmatched_down"]:
            worker.dashboard["position_side"] = "YES"
        elif paired["pairing"]["unmatched_down"] > _EPS:
            worker.dashboard["position_side"] = "NO"
        elif paired["pairing"]["matched_shares"] > _EPS:
            worker.dashboard["position_side"] = "PAIRED"
        else:
            worker.dashboard["position_side"] = ""
        worker.dashboard["strategy_display_state"] = paired["strategy_ui"]["state"]
        vel = worker.dashboard.get("order_book_velocity")
        if vel and action:
            m = ledger.metrics()
            unmatched = m.up_unmatched_shares > _EPS or m.down_unmatched_shares > _EPS
            completes_hedge = action.kind == ActionKind.BUY_HEDGE and unmatched
            cls = classify_proposed_order(
                action_kind=action.kind.value,
                side=action.side,
                ledger_has_unmatched=unmatched,
                completes_hedge=bool(completes_hedge and action.projected_pair_cost),
            )
            vel = dict(vel)
            vel["entry_classification"] = cls.value
            worker.dashboard["order_book_velocity"] = vel

    def _log_decision(self, worker: "MarketWorker", action: StrategyAction) -> None:
        key = self._key(worker)
        sig = f"{action.kind.value}:{action.side}:{action.reason}"
        if self._last_decision_sig.get(key) == sig:
            return
        self._last_decision_sig[key] = sig
        label = f"{worker.asset_type.upper()} {worker.window_slug}"
        print(f"📋 [{action.kind.value}] {label} | {action.reason}")
        cat = "dca" if "DCA" in action.reason.upper() else (
            "hedge" if "HEDGE" in action.reason.upper() or action.kind == ActionKind.BUY_HEDGE else "decision"
        )
        self._push_decision(worker, action.reason, category=cat)
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

        from utils.settlement_risk import (
            exceeds_unmatched_cap,
            projected_totals,
            projected_unmatched_with_pending,
            settlement_outcomes,
        )

        pending = worker.dashboard.get("paired_pending_order") or {}
        pend_side = pending.get("side")
        pend_rem = max(
            0.0,
            float(pending.get("requested_qty") or 0) - float(pending.get("filled_qty") or 0),
        )
        um_y, um_n = projected_unmatched_with_pending(
            ledger, side, float(action.qty),
            pending_side=str(pend_side) if pend_side else None,
            pending_remaining_qty=pend_rem,
        )
        is_hedge = action.kind == ActionKind.BUY_HEDGE
        if not is_hedge and exceeds_unmatched_cap(
            um_y, um_n,
            max_unmatched_per_side=cfg.max_unmatched_shares_per_side,
            buy_side=side,
        ):
            worker.dashboard["latest_decision"] = (
                f"BUY REJECTED: would exceed unmatched cap "
                f"({cfg.max_unmatched_shares_per_side:g} sh/side) — "
                f"projected UP {um_y:.1f} / DOWN {um_n:.1f}"
            )
            return
        m = ledger.metrics()
        py, pn = projected_totals(ledger, side, float(action.qty))
        add_cost = float(action.qty) * float(action.limit_price) * (1.0 + cfg.fee_rate)
        outlook = settlement_outcomes(
            yes_shares=py,
            no_shares=pn,
            total_cost=m.capital_committed + add_cost,
        )
        worker.dashboard["settlement_risk"] = {
            "unmatched_yes": um_y,
            "unmatched_no": um_n,
            "pnl_if_yes_wins": outlook.pnl_if_yes_wins,
            "pnl_if_no_wins": outlook.pnl_if_no_wins,
            "worst_case_pnl": outlook.worst_case_pnl,
            "best_case_pnl": outlook.best_case_pnl,
        }

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
                worker.dashboard["paired_pending_order"] = {
                    "side": side,
                    "requested_qty": float(action.qty),
                    "filled_qty": 0.0,
                    "limit_price": limit_px,
                    "status": "SUBMITTED",
                    "purpose": purpose,
                    "timestamp": time.time(),
                    "order_id": None,
                }
                self._publish_dashboard(
                    worker, ledger, cfg, action,
                    seconds_left=float(worker.market_seconds_left()),
                    phase=resolve_phase(float(worker.market_seconds_left()), cfg.phase_policy),
                )
                qty, fill_px, oid = await fok_buy_limit(
                    worker, side, limit_px, float(action.qty), purpose=purpose,
                )
                worker.order_state = OrderState.IDLE
                worker.dashboard.pop("paired_pending_order", None)
                if qty <= _EPS or fill_px <= 0:
                    worker.dashboard["latest_decision"] = (
                        f"BUY FAILED: FOK not filled for {side} @ {limit_px:.4f}"
                    )
                    self._push_activity(
                        worker,
                        event_type="ORDER_FAILED",
                        category="execution",
                        side=side,
                        qty=0.0,
                        price=limit_px,
                        status="REJECTED",
                        explanation=f"BUY FAILED: FOK not filled for {side} @ {limit_px*100:.1f}¢",
                    )
                    return
                if qty + _EPS < float(action.qty) - 1e-6:
                    worker.dashboard["paired_pending_order"] = {
                            "side": side,
                        "requested_qty": float(action.qty),
                        "filled_qty": float(qty),
                        "limit_price": fill_px,
                        "status": "PARTIAL",
                        "purpose": purpose,
                        "timestamp": time.time(),
                        "order_id": oid,
                    }
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
                if action.kind == ActionKind.BUY_ENTRY:
                    worker.dashboard["paired_initial_entry_fills"] = (
                        int(worker.dashboard.get("paired_initial_entry_fills") or 0) + 1
                    )
                    worker.entry_count = int(worker.dashboard["paired_initial_entry_fills"])
                elif action.kind == ActionKind.BUY_DCA:
                    worker.dashboard["paired_dca_fills"] = (
                        int(worker.dashboard.get("paired_dca_fills") or 0) + 1
                    )
                elif action.kind == ActionKind.BUY_HEDGE:
                    worker.dashboard["paired_hedge_fills"] = (
                        int(worker.dashboard.get("paired_hedge_fills") or 0) + 1
                    )
                worker.dashboard["paired_total_fills"] = (
                    int(worker.dashboard.get("paired_initial_entry_fills") or 0)
                    + int(worker.dashboard.get("paired_dca_fills") or 0)
                    + int(worker.dashboard.get("paired_hedge_fills") or 0)
                )
                if worker.entry_count < 1:
                    worker.entry_count = 1
                print(
                    f"✅ [PAIRED BUY] {worker.asset_type.upper()} {worker.window_slug} | "
                    f"{side} {qty:.2f} @ {fill_px:.4f} | {action.kind.value}"
                )
                self._update_cycle_phase(worker, ledger, filled_side=side, kind=action.kind)
                side_label = "UP" if side == "YES" else "DOWN"
                cost = qty * fill_px + fee
                self._push_activity(
                    worker,
                    event_type="FILL",
                    category="hedge" if action.kind == ActionKind.BUY_HEDGE else (
                        "dca" if action.kind == ActionKind.BUY_DCA else "execution"
                    ),
                    side=side,
                    qty=qty,
                    price=fill_px,
                    cost_usd=cost,
                    status="PARTIAL" if qty + _EPS < float(action.qty) else "FILLED",
                    order_id=oid,
                    explanation=(
                        f"BOUGHT {qty:g} {side_label} @ {fill_px*100:.1f}¢ — ${cost:.2f}"
                    ),
                )
                if action.kind == ActionKind.BUY_HEDGE:
                    pc = ledger.portfolio_matched_pair_cost()
                    if pc is not None:
                        self._push_activity(
                            worker,
                            event_type="PAIR_COMPLETED",
                            category="hedge",
                            explanation=f"PAIR COMPLETED — combined {_cents(pc):.1f}¢",
                            price=pc,
                        )
                worker.dashboard.pop("paired_pending_order", None)
                self._persist_ledger(key, ledger)
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

        self._ensure_velocity_ws_hooks(worker)
        quotes = self._quotes(worker)
        vel_snap = self._update_velocity(worker)
        vcfg = load_velocity_config(worker.worker_config)
        vel_ctx = vel_snap.context if vel_snap else None
        apply_velocity = bool(
            vel_snap and vcfg.engine_enabled and vcfg.entry_enabled
        )
        action = decide(
            ledger,
            quotes,
            cfg,
            seconds_left=seconds_left,
            velocity=vel_ctx,
            apply_velocity_entry=apply_velocity,
        )
        action = self._paper_block_velocity_live(worker, action, apply_velocity=apply_velocity)
        if vel_snap and vcfg.paper_mode:
            journal = self._velocity_paper.setdefault(self._key(worker), VelocityPaperJournal())
            journal.record(
                yes_mid=float(worker.best_ask("YES")),
                no_mid=float(worker.best_ask("NO")),
                decision=vel_snap.decision.decision,
                preferred_side=vel_snap.decision.preferred_side,
                action_kind=action.kind.value,
            )
        self._log_velocity_audit(worker, ledger, vel_snap, action, seconds_left=seconds_left)
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
