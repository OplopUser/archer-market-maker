#!/usr/bin/env python3
"""Regression tests for Archer adaptive controller safety decisions."""

from __future__ import annotations

import unittest

from scripts.archer_adaptive_12h import (
    AdaptiveController,
    PROFILES,
    profile_edge_floor_bps,
    profile_validation_defaults,
)


def metrics(
    *,
    net: float,
    quote_delta: float,
    base_delta: float = -0.5,
    base_total: float = 1.8,
    quote_total: float = 167.0,
    mid_price: float = 80.0,
    intel_spread_add_bps: float = 0.0,
    tx_count: int = 5,
) -> dict:
    return {
        "market": {"command_ok": True},
        "status": {
            "command_ok": True,
            "stale": False,
            "base_total": base_total,
            "quote_total": quote_total,
        },
        "mid_price": mid_price,
        "market_intel": {"spread_add_bps": intel_spread_add_bps},
        "pnl": {
            "fee_usdc": 0.002,
            "net_trading_vs_hold_usdc": net,
            "trading_vs_hold_usdc": net + 0.002,
            "fill_detected": True,
            "base_delta": base_delta,
            "quote_delta": quote_delta,
        },
        "transactions": {"failed_count": 0, "since_start_count": tx_count},
        "logs": {"counts": {"price_feed_stale": 0, "rpc_429": 0}},
    }


def previous_metrics() -> dict:
    return {
        "fee_usdc": 0,
        "net_trading_vs_hold_usdc": 0,
        "trading_vs_hold_usdc": 0,
        "failed_count": 0,
        "tx_count": 0,
        "price_feed_stale": 0,
        "rpc_429": 0,
        "base_delta": 0,
        "quote_delta": 0,
    }


class AdaptiveControllerProfitGuardTests(unittest.TestCase):
    def controller(self, profile: str = "overnight_balanced_low_churn") -> AdaptiveController:
        return AdaptiveController("test", 60, 30, profile, "test")

    def test_all_profiles_inherit_profit_guards(self) -> None:
        for name, profile in PROFILES.items():
            with self.subTest(profile=name):
                effective_floor = profile_edge_floor_bps(profile)
                self.assertGreater(effective_floor, 0.0)
                self.assertGreater(int(profile["post_fill_cooldown_ms"]), 0)
                self.assertEqual(float(profile["post_fill_side_size_multiplier"]), 0.0)
                self.assertGreater(int(profile["post_fill_markout_check_ms"]), 0)
                self.assertGreater(float(profile["post_fill_adverse_markout_bps"]), 0.0)
                self.assertGreater(int(profile["post_fill_adverse_cooldown_ms"]), 0)

    def test_selective_profile_uses_calibrated_edge_floor(self) -> None:
        profile = PROFILES["overnight_selective_edge_probe"]
        self.assertEqual(profile["spread_levels_bps"], [24.0, 42.0, 62.0])
        self.assertEqual(profile_edge_floor_bps(profile), 24.0)
        self.assertEqual(profile_validation_defaults("overnight_selective_edge_probe")["post_gate_max_break_even_bps"], 42.0)

    def test_estimator_uses_profile_edge_floor_and_intel_multiplier(self) -> None:
        controller = self.controller("overnight_selective_edge_probe")

        estimated = controller.estimated_tightest_spread_bps(
            {"market_intel": {"spread_add_bps": 12.0}}
        )

        self.assertEqual(estimated, 27.0)

    def test_estimator_does_not_add_intel_on_top_of_hard_floor(self) -> None:
        controller = self.controller("overnight_balanced_low_churn")

        estimated = controller.estimated_tightest_spread_bps(
            {"market_intel": {"spread_add_bps": 12.0}}
        )

        self.assertEqual(estimated, 62.0)

    def test_toxic_fill_preempts_inventory_repair_routing(self) -> None:
        controller = self.controller()

        profile, reason = controller.decide_next_profile(
            metrics(net=-0.06, quote_delta=45.0, base_total=2.5, quote_total=50.0),
            previous_metrics(),
            1,
        )

        self.assertEqual(profile, "fee_guard_passive_80")
        self.assertIn("fill toxicity guard", reason)

    def test_toxic_fill_stops_when_passive_guard_is_still_negative(self) -> None:
        controller = self.controller("fee_guard_passive_80")

        profile, reason = controller.decide_next_profile(
            metrics(net=-0.06, quote_delta=45.0),
            previous_metrics(),
            1,
        )

        self.assertEqual(profile, "__stop__")
        self.assertIn("stopping instead of spending more fees", reason)

    def test_small_fill_loss_does_not_trigger_toxicity_guard(self) -> None:
        controller = self.controller()

        profile, reason = controller.decide_next_profile(
            metrics(net=-0.01, quote_delta=45.0),
            previous_metrics(),
            1,
        )

        self.assertEqual(profile, "overnight_balanced_low_churn")
        self.assertNotIn("fill toxicity guard", reason)


if __name__ == "__main__":
    unittest.main()
