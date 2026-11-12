"""Hysteresis and negative-score cash rule."""
from __future__ import annotations

import unittest

from shared.selection import apply_hysteresis


def _row(name: str, score: float) -> dict:
    return {"name": name, "score": score}


class HysteresisTests(unittest.TestCase):
    def test_no_switch_inside_the_margin(self) -> None:
        choice, reason = apply_hysteresis(
            [_row("sma_crossover", 0.10), _row("bollinger_breakout", 0.11)],
            current="sma_crossover",
            margin=0.02,
        )
        self.assertEqual(choice, "sma_crossover")
        self.assertIn("short of", reason)

    def test_switch_when_lead_meets_margin(self) -> None:
        choice, reason = apply_hysteresis(
            [_row("sma_crossover", 0.10), _row("bollinger_breakout", 0.12)],
            current="sma_crossover",
            margin=0.02,
        )
        self.assertEqual(choice, "bollinger_breakout")
        self.assertIn("Switched", reason)

    def test_negative_best_score_is_cash(self) -> None:
        choice, reason = apply_hysteresis(
            [_row("sma_crossover", -0.01), _row("rsi_mean_reversion", -0.20)],
            current=None,
            margin=0.02,
        )
        self.assertEqual(choice, "cash")
        self.assertIn("negative", reason)

    def test_negative_incumbent_without_a_clear_challenger_is_cash(self) -> None:
        choice, _reason = apply_hysteresis(
            [_row("sma_crossover", -0.05), _row("bollinger_breakout", -0.02)],
            current="sma_crossover",
            margin=0.02,
        )
        self.assertEqual(choice, "cash")

    def test_negative_incumbent_can_switch_to_a_non_negative_leader(self) -> None:
        choice, _reason = apply_hysteresis(
            [_row("sma_crossover", -0.01), _row("bollinger_breakout", 0.02)],
            current="sma_crossover",
            margin=0.02,
        )
        self.assertEqual(choice, "bollinger_breakout")

    def test_buy_and_hold_is_ignored(self) -> None:
        choice, _reason = apply_hysteresis(
            [_row("buy_and_hold", 0.50), _row("sma_crossover", 0.01)],
            current=None,
            margin=0.02,
        )
        self.assertEqual(choice, "sma_crossover")


if __name__ == "__main__":
    unittest.main()
