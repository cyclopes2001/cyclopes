import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from strategies.directional_fok_strategy import DirectionalFokStrategy


class _Inventory:
    def __init__(self, shares=5.0, entry=0.40):
        self.remaining = shares
        self.entry = entry
        self.realized_pnl = 0.0

    def record_sell(self, side, qty, price):
        qty = min(float(qty), self.remaining)
        trade_pnl = (float(price) - self.entry) * qty
        self.realized_pnl += trade_pnl
        self.remaining -= qty
        return trade_pnl

    def shares(self, side):
        return self.remaining

    def avg_cost(self, side):
        return self.entry


class StopLossAccountingTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, shares=5.0):
        inv = _Inventory(shares=shares)
        worker = SimpleNamespace(
            inventory=inv,
            dashboard={"entry_fill_price": 0.40},
            worker_config=SimpleNamespace(stop_loss_pct=0.50, take_profit_pct=1.0),
            position_side="YES",
            position_size=shares,
            entry_price=0.40,
            record_fill_idempotent=Mock(return_value=True),
            log_trade=Mock(),
            log_pnl=Mock(),
            canonical_market_id=lambda: "condition-1",
        )
        return worker

    async def test_full_stop_loss_exit_updates_realized_pnl(self):
        worker = self.worker()
        await DirectionalFokStrategy()._record_exit_sell(
            worker, "YES", 5.0, 0.20, "order-1", exit_kind="STOP_LOSS",
        )
        self.assertAlmostEqual(worker.inventory.realized_pnl, -1.0)
        worker.log_pnl.assert_called_once()
        args, kwargs = worker.log_pnl.call_args
        self.assertEqual(args[0], "STOP_LOSS")
        self.assertAlmostEqual(args[1], -1.0)
        self.assertEqual(worker.dashboard["exit_order_status"], "FILLED")
        self.assertEqual(worker.dashboard["market_outcome_stamp"], "STOP LOSS")
        self.assertIn("Bought $2.00 Up", worker.dashboard["market_outcome_stamp_buy_line"])
        self.assertIn("Sold $1.00 Up", worker.dashboard["market_outcome_stamp_sell_line"])

    async def test_take_profit_stamp_and_pnl(self):
        worker = self.worker()
        await DirectionalFokStrategy()._record_exit_sell(
            worker, "YES", 5.0, 0.80, "order-2", exit_kind="TAKE_PROFIT",
        )
        worker.log_pnl.assert_called_once()
        args, _ = worker.log_pnl.call_args
        self.assertEqual(args[0], "TAKE_PROFIT")
        self.assertAlmostEqual(args[1], 2.0)
        self.assertEqual(worker.dashboard["market_outcome_stamp"], "TAKE PROFIT")

    async def test_partial_exit_remains_open_and_is_not_counted_as_closed_trade(self):
        worker = self.worker()
        await DirectionalFokStrategy()._record_exit_sell(
            worker, "YES", 3.0, 0.20, "order-3", exit_kind="STOP_LOSS",
        )
        self.assertEqual(worker.inventory.shares("YES"), 2.0)
        self.assertEqual(worker.dashboard["exit_order_status"], "PARTIAL")
        self.assertEqual(worker.dashboard["strategy_state"], "stop_loss_exit_pending")
        worker.log_pnl.assert_not_called()


if __name__ == "__main__":
    unittest.main()