import unittest

from config import load_worker_configs


class DashboardConfigTests(unittest.TestCase):
    def test_entry_and_stop_defaults(self):
        wc = load_worker_configs()[0]
        self.assertAlmostEqual(wc.entry_price, 0.97)
        self.assertAlmostEqual(wc.stop_loss_pct, 0.50)
        self.assertGreaterEqual(wc.entry_window_seconds, 1)
        self.assertTrue(wc.endgame_enabled)
        self.assertTrue(wc.hold_to_settle)
        self.assertAlmostEqual(wc.latency_arb_min_ask, 0.97)
        self.assertAlmostEqual(wc.latency_arb_max_ask, 0.99)
        self.assertEqual(wc.listener_activate_secs, 90)
        self.assertEqual(wc.order_type, "FOK")


if __name__ == "__main__":
    unittest.main()
