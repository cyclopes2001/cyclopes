"""High-confidence FOK entry + hold-to-settle tests."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from utils.momentum_risk import stop_loss_price, take_profit_price


class ExitMathTests(unittest.TestCase):
    def test_stop_from_actual_fill(self):
        self.assertAlmostEqual(stop_loss_price(0.50, 0.50), 0.25)

    def test_take_profit_from_fill(self):
        self.assertAlmostEqual(take_profit_price(0.50, 0.07), 0.535)


class StrategyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _worker(self, *, yes=0.0, no=0.0, seconds_left=40):
        inv = MagicMock()
        inv.yes_shares = 0.0
        inv.no_shares = 0.0
        inv.realized_pnl = 0.0
        inv.avg_cost = MagicMock(return_value=0.0)
        inv.shares = MagicMock(return_value=0.0)

        cfg = SimpleNamespace(
            key="btc:5m",
            entry_price=0.97,
            stop_loss_pct=0.50,
            take_profit_pct=0.07,
            entry_window_seconds=60,
            min_entry_seconds_left=15,
            min_order_size=5.0,
            order_size_max=5.0,
            min_orderbook_depth=5.0,
            max_quote_age_seconds=3.0,
            stop_loss_exit_max_retries=2,
            stop_loss_exit_retry_ms=10,
            endgame_enabled=True,
            hold_to_settle=True,
            latency_arb_min_ask=0.97,
            latency_arb_max_ask=0.99,
            momentum_execution_mode="single_taker",
        )

        worker = SimpleNamespace(
            worker_config=cfg,
            inventory=inv,
            prices={"YES": yes, "NO": no},
            bids={"YES": 0.0, "NO": 0.0},
            dashboard={},
            order_state=SimpleNamespace(name="IDLE"),
            _order_lock=asyncio.Lock(),
            _stop_loss_lock=asyncio.Lock(),
            _stop_loss_exit_in_flight=False,
            _market_entry_guard=set(),
            entry_count=0,
            asset_type="btc",
            window_slug="5m",
            active_market={"condition_id": "m1", "start_ts": 900.0},
            _ws_market_data_stale=False,
        )
        worker.canonical_market_id = lambda: "m1"
        worker.market_seconds_left = lambda: seconds_left
        worker.market_seconds_elapsed = lambda: 300.0 - float(seconds_left)
        worker.market_start_ts = lambda: 900.0
        worker.can_enter = lambda: 15 <= seconds_left <= 60
        worker.best_ask = lambda s: worker.prices[s]
        worker.max_quote_age_seconds = lambda: 0.1
        worker.is_dry_run = lambda: True
        worker.entry_order_size = lambda _legs: 5.0
        worker.executable_buy = lambda s, sz: (worker.prices[s], sz) if worker.prices[s] > 0 else None
        worker.record_fill_idempotent = lambda *a, **k: True
        worker.log_trade = lambda *a, **k: None
        return worker

    async def test_no_buy_when_ask_below_floor(self):
        from bot import OrderState
        from strategies.directional_fok_strategy import DirectionalFokStrategy

        w = self._worker(yes=0.90, no=0.12, seconds_left=40)
        w.order_state = OrderState.IDLE
        strat = DirectionalFokStrategy()
        with patch("bot.is_trading_allowed", return_value=True), patch(
            "strategies.directional_fok_strategy.stopped_out_store.is_stopped_out",
            return_value=False,
        ), patch(
            "strategies.directional_fok_strategy.fok_buy_limit",
            new_callable=AsyncMock,
        ) as mock_buy:
            await strat.on_market_tick(w)
            mock_buy.assert_not_called()
            self.assertEqual(w.dashboard.get("entry_block_reason"), "ask_below_floor")

    async def test_buy_yes_when_ask_at_or_above_floor(self):
        from bot import OrderState
        from strategies.directional_fok_strategy import DirectionalFokStrategy

        w = self._worker(yes=0.97, no=0.05, seconds_left=40)
        w.order_state = OrderState.IDLE
        strat = DirectionalFokStrategy()

        async def _fake_buy(worker, side, limit_px, size, *, purpose):
            self.assertEqual(side, "YES")
            self.assertAlmostEqual(limit_px, 0.97)
            return 0.0, 0.0, None

        with patch("bot.is_trading_allowed", return_value=True), patch(
            "strategies.directional_fok_strategy.stopped_out_store.is_stopped_out",
            return_value=False,
        ), patch(
            "strategies.directional_fok_strategy.fok_buy_limit",
            side_effect=_fake_buy,
        ) as mock_buy:
            await strat.on_market_tick(w)
            mock_buy.assert_called_once()

    async def test_hold_to_settle_skips_exits(self):
        from bot import OrderState
        from strategies.directional_fok_strategy import DirectionalFokStrategy

        w = self._worker(yes=0.97, no=0.05, seconds_left=40)
        w.order_state = OrderState.IDLE
        w.inventory.yes_shares = 5.0
        w.inventory.avg_cost = MagicMock(return_value=0.97)
        w.inventory.shares = MagicMock(return_value=5.0)
        w.bids["YES"] = 0.50
        w.best_bid = lambda s: w.bids.get(s, 0.0)
        strat = DirectionalFokStrategy()

        with patch(
            "strategies.directional_fok_strategy.fok_sell_with_retries",
            new_callable=AsyncMock,
        ) as mock_sell:
            await strat.on_market_tick(w)
            mock_sell.assert_not_called()
            self.assertEqual(w.dashboard.get("strategy_state"), "holding_to_settle")


if __name__ == "__main__":
    unittest.main()
