#!/usr/bin/env python3
"""Regression tests for Archer adaptive controller safety decisions."""

from __future__ import annotations

import unittest

from scripts.archer_adaptive_12h import (
    AdaptiveController,
    PROFILES,
    classify_archer_live_status,
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
    failed_count: int = 0,
    price_feed_stale: int = 0,
    rpc_429: int = 0,
    tx_circuit_breaker: int = 0,
    dashboard_ok: bool = True,
    dashboard_stale: bool = False,
    rpc_ok: bool = True,
    rpc_stale: bool = False,
    heartbeat_ok: bool = True,
) -> dict:
    return {
        "market": {"command_ok": True},
        "status": {
            "command_ok": True,
            "stale": dashboard_stale,
            "base_total": base_total,
            "quote_total": quote_total,
        },
        "dashboard_health": {"ok": dashboard_ok, "stale": dashboard_stale},
        "archer_live_status": {
            "rpc_healthy": rpc_ok,
            "rpc_stale": rpc_stale,
            "heartbeat_healthy": heartbeat_ok,
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
        "transactions": {"failed_count": failed_count, "since_start_count": tx_count},
        "logs": {
            "counts": {
                "price_feed_stale": price_feed_stale,
                "rpc_429": rpc_429,
                "tx_circuit_breaker": tx_circuit_breaker,
            }
        },
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

    def test_watchdog_does_not_stop_on_tx_circuit_throttle_only(self) -> None:
        controller = self.controller()
        previous = metrics(net=0.0, quote_delta=0.0, tx_count=3, tx_circuit_breaker=2)
        current = metrics(net=0.0, quote_delta=0.0, tx_count=3, tx_circuit_breaker=3)

        reason = controller.decide_watchdog_stop(current, previous)

        self.assertIsNone(reason)

    def test_watchdog_still_stops_on_stale_feed_burst(self) -> None:
        controller = self.controller()
        previous = metrics(net=0.0, quote_delta=0.0, price_feed_stale=0)
        current = metrics(net=0.0, quote_delta=0.0, price_feed_stale=2)

        reason = controller.decide_watchdog_stop(current, previous)

        self.assertIsNotNone(reason)
        self.assertIn("window_stale=2", reason or "")

    def test_dashboard_down_rpc_healthy_continues_degraded(self) -> None:
        status = classify_archer_live_status(
            metrics(
                net=0.0,
                quote_delta=0.0,
                dashboard_ok=False,
                dashboard_stale=True,
                rpc_ok=True,
                rpc_stale=False,
                heartbeat_ok=True,
            )
        )

        self.assertEqual(status["state"], "dashboard_down_rpc_healthy")
        self.assertEqual(status["action"], "continue_degraded")
        self.assertEqual(status["restart_policy"], "restart_allowed")
        self.assertEqual(status["alert"]["severity"], "warning")

    def test_rpc_stale_dashboard_healthy_quotes_reduce_only(self) -> None:
        status = classify_archer_live_status(
            metrics(
                net=0.0,
                quote_delta=0.0,
                dashboard_ok=True,
                dashboard_stale=False,
                rpc_ok=True,
                rpc_stale=True,
                heartbeat_ok=True,
            )
        )

        self.assertEqual(status["state"], "rpc_stale_dashboard_healthy")
        self.assertEqual(status["action"], "quote_reduce_only")
        self.assertEqual(status["restart_policy"], "manual_only")
        self.assertEqual(status["alert"]["severity"], "warning")

    def test_both_stale_clears_book(self) -> None:
        status = classify_archer_live_status(
            metrics(
                net=0.0,
                quote_delta=0.0,
                dashboard_ok=True,
                dashboard_stale=True,
                rpc_ok=True,
                rpc_stale=True,
                heartbeat_ok=True,
            )
        )

        self.assertEqual(status["state"], "both_stale")
        self.assertEqual(status["action"], "clear_book")
        self.assertEqual(status["restart_policy"], "manual_only")
        self.assertEqual(status["alert"]["severity"], "critical")

    def test_heartbeat_missing_stops_supervised(self) -> None:
        status = classify_archer_live_status(
            metrics(
                net=0.0,
                quote_delta=0.0,
                dashboard_ok=True,
                dashboard_stale=False,
                rpc_ok=True,
                rpc_stale=False,
                heartbeat_ok=False,
            )
        )

        self.assertEqual(status["state"], "heartbeat_missing")
        self.assertEqual(status["action"], "stop_supervised")
        self.assertEqual(status["restart_policy"], "manual_only")
        self.assertEqual(status["alert"]["severity"], "critical")

    def test_stale_dashboard_alone_does_not_make_bot_non_live(self) -> None:
        controller = self.controller()
        current = metrics(
            net=0.0,
            quote_delta=0.0,
            dashboard_ok=True,
            dashboard_stale=True,
            rpc_ok=True,
            rpc_stale=False,
            heartbeat_ok=True,
        )

        status = classify_archer_live_status(current)
        profile, reason = controller.decide_next_profile(current, previous_metrics(), 1)
        stop_reason = controller.decide_watchdog_stop(current, metrics(net=0.0, quote_delta=0.0))

        self.assertEqual(status["state"], "dashboard_down_rpc_healthy")
        self.assertEqual(status["action"], "continue_degraded")
        self.assertEqual(profile, "overnight_balanced_low_churn")
        self.assertNotIn("live status unavailable", reason)
        self.assertIsNone(stop_reason)


if __name__ == "__main__":
    unittest.main()
