#!/usr/bin/env python3
"""Regression tests for Archer dashboard sample analysis."""

from __future__ import annotations

import unittest

from scripts.analyze_dashboard_samples import (
    calibrate_spread_floors,
    choose_calibrated_floor,
    retained_fill_replay,
)


def summary(*, edge_bps: float, fee_usdc: float = 0.02) -> dict:
    mid = 100.0
    size = 0.5
    bid_price = mid * (1.0 - edge_bps / 10_000.0)
    return {
        "end_mid": mid,
        "fee_usdc": fee_usdc,
        "fills": [
            {
                "side": "bid",
                "base_delta": size,
                "quote_delta": -size * bid_price,
                "actual_edge_bps": edge_bps,
            }
        ],
    }


class AnalyzeDashboardSamplesTests(unittest.TestCase):
    def test_retained_fill_replay_keeps_only_fills_at_or_wider_than_floor(self) -> None:
        run = {
            "end_mid": 100.0,
            "fee_usdc": 0.02,
            "fills": [
                {
                    "side": "bid",
                    "base_delta": 0.5,
                    "quote_delta": -49.9,
                    "actual_edge_bps": 20.0,
                },
                {
                    "side": "ask",
                    "base_delta": -0.5,
                    "quote_delta": 50.2,
                    "actual_edge_bps": 40.0,
                },
            ],
        }

        replay = retained_fill_replay(run, 24.0)

        self.assertEqual(len(replay["fills"]), 1)
        self.assertAlmostEqual(replay["fill_notional"], 50.2)
        self.assertAlmostEqual(replay["net_vs_hold"], 0.18)

    def test_calibration_recommends_lowest_floor_with_retained_fill_surface(self) -> None:
        summaries = [summary(edge_bps=30.0), summary(edge_bps=32.0), summary(edge_bps=34.0)]

        calibration = calibrate_spread_floors(
            summaries,
            [16.0, 24.0, 42.0],
            min_retained_runs=3,
            min_retained_notional=100.0,
            min_retained_net_usdc=0.0,
        )
        recommendation = choose_calibrated_floor(calibration)

        self.assertIsNotNone(recommendation)
        assert recommendation is not None
        self.assertEqual(recommendation["floor_bps"], 16.0)
        self.assertTrue(recommendation["passes"])
        forty_two = next(row for row in calibration if row["floor_bps"] == 42.0)
        self.assertFalse(forty_two["passes"])


if __name__ == "__main__":
    unittest.main()
