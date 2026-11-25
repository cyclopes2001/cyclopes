import unittest

from config import load_worker_configs


class DashboardConfigTests(unittest.TestCase):
    def test_paired_inventory_defaults(self):
        wc = load_worker_configs()[0]
        self.assertEqual(wc.strategy_mode, "paired_inventory_accumulation")
        self.assertAlmostEqual(wc.paired_initial_entry_threshold, 0.35)
        self.assertAlmostEqual(wc.target_combined_pair_cost, 0.98)
        self.assertAlmostEqual(wc.paired_max_inventory_per_side, 15.0)
        self.assertAlmostEqual(wc.paired_unmatched_inventory_target, 5.0)
        self.assertFalse(wc.paired_live_trading_enabled)
        self.assertTrue(wc.dry_run)
        self.assertEqual(wc.order_type, "FOK")
        self.assertEqual(wc.listener_activate_secs, 300)


if __name__ == "__main__":
    unittest.main()
