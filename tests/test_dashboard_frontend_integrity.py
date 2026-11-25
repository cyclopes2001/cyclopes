"""Guardrails for embedded dashboard JavaScript (parse / syntax regressions)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


class DashboardFrontendIntegrityTests(unittest.TestCase):
    def test_render_card_no_duplicate_paired_mode_const(self):
        text = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        m = re.search(r"function renderCard\(bot\)\{([^}]+(?:\{[^}]*\}[^}]*)*)\}", text)
        self.assertIsNotNone(m, "renderCard function not found in main.py")
        body = m.group(1)
        count = len(re.findall(r"\bconst\s+pairedMode\s*=", body))
        self.assertLessEqual(
            count,
            1,
            "duplicate `const pairedMode` breaks the entire dashboard script (SyntaxError)",
        )

    def test_dashboard_json_sanitize_nan(self):
        from main import _sanitize_for_json, dashboard_json_dumps

        payload = {"pnl": float("nan"), "ok": 1.0, "nested": {"x": float("inf")}}
        cleaned = _sanitize_for_json(payload)
        self.assertIsNone(cleaned["pnl"])
        self.assertIsNone(cleaned["nested"]["x"])
        raw = dashboard_json_dumps({"a": 1})
        self.assertIn('"a": 1', raw)


if __name__ == "__main__":
    unittest.main()