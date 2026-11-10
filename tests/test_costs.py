"""Costs apply to executed notional only. Fills are not round trips."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _engine():
    path = Path(__file__).resolve().parents[1] / "services" / "strategy-engine" / "main.py"
    spec = importlib.util.spec_from_file_location("sentinel_strategy_engine", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CostTests(unittest.TestCase):
    def test_flat_book_pays_nothing(self) -> None:
        engine = _engine()
        sim = engine._simulate([100.0, 101.0, 99.0], ["a", "b", "c"], ["hold", "hold", "hold"], 100000.0, 0.0015)
        self.assertEqual(sim["fills"], 0)
        self.assertEqual(sim["round_trips"], 0)
        self.assertAlmostEqual(sim["final_equity"], 100000.0)

    def test_round_trip_cost_is_on_both_notionals(self) -> None:
        engine = _engine()
        rate = 0.0015
        sim = engine._simulate(
            [100.0, 100.0, 100.0],
            ["a", "b", "c"],
            ["buy", "hold", "sell"],
            100000.0,
            rate,
        )
        expected = 100000.0 * (1.0 - rate) / (1.0 + rate)
        self.assertAlmostEqual(sim["final_equity"], expected, places=6)
        self.assertEqual(sim["fills"], 2)
        self.assertEqual(sim["round_trips"], 1)
        self.assertEqual(sim["trades"], 2)


if __name__ == "__main__":
    unittest.main()
