#!/usr/bin/env python3
"""Regression tests for Archer adaptive controller safety decisions."""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.archer_adaptive_12h import (
    AdaptiveController,
    COMPOSE_STOP_PLACEHOLDER_RUN_ID,
    PROFILES,
    PROFIT_GUARD_DEFAULTS,
    classify_archer_live_status,
    resolve_profile_approval,
    profile_edge_floor_bps,
    profile_validation_defaults,
    validate_start_args,
)


def metrics(
    *,
    net: float,
    quote_delta: float,
    fee_usdc: float = 0.002,
    trading_vs_hold_usdc: float | None = None,
    base_delta: float = -0.5,
    base_total: float = 1.8,
    quote_total: float = 167.0,
    mid_price: float = 80.0,
    bid_levels: int = 1,
    ask_levels: int = 1,
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
    fill_detected: bool = True,
    market_intel: dict | None = None,
) -> dict:
    if market_intel is None:
        market_intel = {
            "enabled": True,
            "ok": True,
            "quote_enabled": True,
            "mode": "normal",
            "fair_value": mid_price,
            "spread_add_bps": intel_spread_add_bps,
            "generated_at_unix_secs": time.time(),
            "source_quality": {"status": "ok", "stale": False},
        }
    return {
        "market": {"command_ok": True},
        "status": {
            "command_ok": True,
            "stale": dashboard_stale,
            "base_total": base_total,
            "quote_total": quote_total,
            "bid_levels": bid_levels,
            "ask_levels": ask_levels,
        },
        "dashboard_health": {"ok": dashboard_ok, "stale": dashboard_stale},
        "archer_live_status": {
            "rpc_healthy": rpc_ok,
            "rpc_stale": rpc_stale,
            "heartbeat_healthy": heartbeat_ok,
        },
        "mid_price": mid_price,
        "market_intel": market_intel,
        "pnl": {
            "fee_usdc": fee_usdc,
            "net_trading_vs_hold_usdc": net,
            "trading_vs_hold_usdc": net + fee_usdc if trading_vs_hold_usdc is None else trading_vs_hold_usdc,
            "fill_detected": fill_detected,
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

    def test_rejects_compose_stop_placeholder_start(self) -> None:
        with self.assertRaises(ValueError):
            validate_start_args(COMPOSE_STOP_PLACEHOLDER_RUN_ID, 36000, 1800, "fill_discovery_capped")

    def test_rejects_non_positive_duration_start(self) -> None:
        with self.assertRaises(ValueError):
            validate_start_args("test-run", 0, 1800, "fill_discovery_capped")

    def test_tuxedo_compose_can_be_parsed_for_stop_without_run_env(self) -> None:
        compose = Path(__file__).resolve().parents[1] / "docker-compose.tuxedo.yml"
        text = compose.read_text()

        self.assertNotIn("${ARCHER_RUN_ID:?", text)
        self.assertNotIn("${ARCHER_DURATION_SECONDS:?", text)
        self.assertIn(COMPOSE_STOP_PLACEHOLDER_RUN_ID, text)
        self.assertGreaterEqual(text.count("ARCHER_APPROVED_SCALE_PROFILES:"), 2)
        self.assertGreaterEqual(text.count("ARCHER_ALLOW_SCALE_PROFILES:"), 2)
        self.assertGreaterEqual(text.count("ARCHER_PROFILE_APPROVAL_FALLBACK:"), 2)

    def test_all_profiles_inherit_profit_guards(self) -> None:
        for name, profile in PROFILES.items():
            with self.subTest(profile=name):
                effective_floor = profile_edge_floor_bps(profile)
                self.assertGreater(effective_floor, 0.0)
                self.assertGreater(int(profile["post_fill_cooldown_ms"]), 0)
                self.assertGreater(float(profile["post_fill_side_size_multiplier"]), 0.0)
                self.assertGreater(int(profile["post_fill_markout_check_ms"]), 0)
                self.assertGreater(float(profile["post_fill_adverse_markout_bps"]), 0.0)
                self.assertGreater(int(profile["post_fill_adverse_cooldown_ms"]), 0)

    def test_selective_profile_uses_calibrated_edge_floor(self) -> None:
        profile = PROFILES["overnight_selective_edge_probe"]
        self.assertEqual(profile["spread_levels_bps"], [24.0, 42.0, 62.0])
        self.assertEqual(profile_edge_floor_bps(profile), 24.0)
        self.assertEqual(profile_validation_defaults("overnight_selective_edge_probe")["post_gate_max_break_even_bps"], 42.0)

    def test_fee_guard_min_notional_allows_guarded_size_multiplier(self) -> None:
        profile = PROFILES["fee_guard_passive_80"]
        min_size_multiplier = float(profile.get("min_intel_size_multiplier", 0.20))
        min_viable_notional = float(profile["max_quote_notional_per_level"]) * min_size_multiplier

        self.assertLessEqual(float(profile["min_quote_notional"]), min_viable_notional)

    def test_fee_guard_post_fill_cooldown_keeps_minimal_side_quote_viable(self) -> None:
        profile = PROFILES["fee_guard_passive_80"]
        side_budget = float(profile["max_total_quote_notional"]) * 0.5
        guarded_notional = (
            side_budget
            * 0.75
            * 0.35
            * float(profile["post_fill_side_size_multiplier"])
        )

        self.assertGreaterEqual(guarded_notional, float(profile["min_quote_notional"]))

    def test_resting_low_churn_min_notional_survives_reduced_but_enabled_intel_bid(self) -> None:
        profile = PROFILES["resting_low_churn_16_30"]
        min_intel_size_multiplier = float(
            profile.get(
                "min_intel_size_multiplier",
                PROFIT_GUARD_DEFAULTS["min_intel_size_multiplier"],
            )
        )
        side_budget = float(profile["max_total_quote_notional"]) * 0.5
        minimum_enabled_bid_budget = side_budget * min_intel_size_multiplier

        self.assertGreaterEqual(minimum_enabled_bid_budget, float(profile["min_quote_notional"]))

    def test_estimator_uses_profile_edge_floor_and_intel_multiplier(self) -> None:
        controller = self.controller("overnight_selective_edge_probe")

        estimated = controller.estimated_tightest_spread_bps(
            {"market_intel": {"spread_add_bps": 12.0}}
        )

        self.assertEqual(estimated, 27.0)

    def test_estimator_uses_balanced_profile_floor_without_global_cautious_clamp(self) -> None:
        controller = self.controller("overnight_balanced_low_churn")

        estimated = controller.estimated_tightest_spread_bps(
            {"market_intel": {"spread_add_bps": 12.0}}
        )

        self.assertEqual(estimated, 36.0)

    def test_resting_low_churn_uses_own_spread_floor_without_global_cautious_clamp(self) -> None:
        controller = self.controller("resting_low_churn_16_30")

        estimated = controller.estimated_tightest_spread_bps(
            {"market_intel": {"spread_add_bps": 4.8}}
        )

        self.assertAlmostEqual(estimated, 20.8)

    def test_resting_low_churn_uses_active_fill_baseline_profile_shape(self) -> None:
        profile = PROFILES["resting_low_churn_16_30"]

        self.assertEqual(profile["spread_levels_bps"], [16.0, 24.0, 36.0])
        self.assertEqual(profile["max_quote_notional_per_level"], 10.0)
        self.assertEqual(profile["min_base_reserve_pct"], 65.0)

    def test_fee_guard_passive_profile_stays_wide(self) -> None:
        profile = PROFILES["fee_guard_passive_80"]

        self.assertEqual(profile_edge_floor_bps(profile), 80.0)

    def test_weak_positive_fill_discovery_does_not_auto_enter_unapproved_winner_scale(self) -> None:
        controller = self.controller("fill_discovery_capped")

        requested, raw_reason = controller.decide_next_profile(
            metrics(
                net=0.061,
                fee_usdc=0.026,
                trading_vs_hold_usdc=0.087,
                quote_delta=18.0,
                base_delta=-0.25,
                tx_count=79,
                fill_detected=True,
            ),
            previous_metrics(),
            4,
        )

        self.assertEqual(requested, "winner_scale_24_40")
        with patch.dict(
            os.environ,
            {"ARCHER_APPROVED_SCALE_PROFILES": "", "ARCHER_ALLOW_SCALE_PROFILES": ""},
            clear=False,
        ):
            effective, reason = resolve_profile_approval(
                requested,
                raw_reason,
                source="adaptive:test",
            )

        self.assertEqual(effective, "overnight_selective_edge_probe")
        self.assertIn("profile_approval_denied", reason)
        self.assertIn("requested_profile=winner_scale_24_40", reason)

    def test_explicit_approval_allows_winner_scale_profile(self) -> None:
        with patch.dict(
            os.environ,
            {"ARCHER_APPROVED_SCALE_PROFILES": "winner_scale_24_40", "ARCHER_ALLOW_SCALE_PROFILES": ""},
            clear=False,
        ):
            effective, reason = resolve_profile_approval(
                "winner_scale_24_40",
                "operator approved normal-size run",
                source="direct_runner:start",
            )

        self.assertEqual(effective, "winner_scale_24_40")
        self.assertIn("profile_approval=approved", reason)

    def test_direct_runner_uses_same_denial_for_unapproved_winner_scale(self) -> None:
        with patch.dict(
            os.environ,
            {"ARCHER_APPROVED_SCALE_PROFILES": "", "ARCHER_ALLOW_SCALE_PROFILES": ""},
            clear=False,
        ):
            effective, reason = resolve_profile_approval(
                "winner_scale_24_40",
                "direct normal-spread run; adaptive disabled",
                source="direct_runner:start",
            )

        self.assertEqual(effective, "overnight_selective_edge_probe")
        self.assertIn("direct_runner:start", reason)

    def test_direct_runner_can_delegate_to_adaptive_controller(self) -> None:
        runner = Path(__file__).resolve().with_name("archer_direct_normal_runner.sh").read_text()

        self.assertIn("ARCHER_DIRECT_USE_ADAPTIVE_CONTROLLER", runner)
        self.assertIn("scripts/archer_adaptive_12h.py", runner)
        self.assertIn("--evaluation-seconds", runner)
        self.assertLess(
            runner.index("USE_ADAPTIVE_CONTROLLER="),
            runner.index("python3 scripts/archer_preflight_gate.py"),
        )

    def test_capped_profiles_start_without_approval_friction(self) -> None:
        with patch.dict(
            os.environ,
            {"ARCHER_APPROVED_SCALE_PROFILES": "", "ARCHER_ALLOW_SCALE_PROFILES": ""},
            clear=False,
        ):
            effective, reason = resolve_profile_approval(
                "fill_discovery_capped",
                "bounded fill discovery",
                source="direct_runner:start",
            )

        self.assertEqual(effective, "fill_discovery_capped")
        self.assertIn("profile_approval=approved", reason)

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

    def test_watchdog_keeps_running_on_transient_stale_feed_burst(self) -> None:
        controller = self.controller()
        previous = metrics(net=0.0, quote_delta=0.0, price_feed_stale=0)
        current = metrics(net=0.0, quote_delta=0.0, price_feed_stale=2)

        reason = controller.decide_watchdog_stop(current, previous)

        self.assertIsNone(reason)

    def test_watchdog_still_stops_on_failed_transaction_burst(self) -> None:
        controller = self.controller()
        previous = metrics(net=0.0, quote_delta=0.0, failed_count=0)
        current = metrics(net=0.0, quote_delta=0.0, failed_count=5)

        reason = controller.decide_watchdog_stop(current, previous)

        self.assertIsNotNone(reason)
        self.assertIn("window_failed=5", reason or "")

    def test_watchdog_does_not_stop_on_profitable_tx_churn_only(self) -> None:
        controller = self.controller("winner_scale_24_40")
        previous = metrics(net=0.95, quote_delta=40.0, tx_count=0)
        current = metrics(
            net=1.09,
            quote_delta=40.0,
            tx_count=196,
            failed_count=0,
            price_feed_stale=0,
            rpc_429=0,
        )

        reason = controller.decide_watchdog_stop(current, previous)

        self.assertIsNone(reason)

    def test_winner_profile_demotes_to_active_baseline_after_trailing_drawdown(self) -> None:
        controller = self.controller("winner_scale_24_40")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=0.93,
                fee_usdc=0.07,
                trading_vs_hold_usdc=1.00,
                quote_delta=84.0,
                tx_count=92,
                fill_detected=True,
            ),
            {
                **previous_metrics(),
                "net_trading_vs_hold_usdc": 1.00,
                "trading_vs_hold_usdc": 1.07,
                "fee_usdc": 0.07,
                "base_delta": -1.15,
                "quote_delta": 84.0,
                "tx_count": 88,
            },
            6,
        )

        self.assertEqual(profile, "resting_low_churn_16_30")
        self.assertIn("winner profile performance guard", reason)
        self.assertIn("trailing_drawdown", reason)

    def test_winner_profile_demotes_after_negative_fill_window(self) -> None:
        controller = self.controller("winner_scale_24_40")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=0.17,
                fee_usdc=0.04,
                trading_vs_hold_usdc=0.21,
                quote_delta=124.0,
                base_delta=-1.71,
                tx_count=42,
                fill_detected=True,
            ),
            {
                **previous_metrics(),
                "net_trading_vs_hold_usdc": 0.20,
                "trading_vs_hold_usdc": 0.24,
                "fee_usdc": 0.04,
                "base_delta": -1.15,
                "quote_delta": 84.0,
                "tx_count": 39,
            },
            5,
        )

        self.assertEqual(profile, "resting_low_churn_16_30")
        self.assertIn("negative_fill_window", reason)

    def test_winner_profile_moves_to_fee_guard_when_net_edge_turns_negative(self) -> None:
        controller = self.controller("winner_scale_24_40")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=-0.02,
                fee_usdc=0.08,
                trading_vs_hold_usdc=0.06,
                quote_delta=102.0,
                base_delta=-1.41,
                tx_count=55,
                fill_detected=True,
            ),
            {
                **previous_metrics(),
                "net_trading_vs_hold_usdc": 0.04,
                "trading_vs_hold_usdc": 0.10,
                "fee_usdc": 0.06,
                "base_delta": -1.15,
                "quote_delta": 84.0,
                "tx_count": 51,
            },
            7,
        )

        self.assertEqual(profile, "fee_guard_passive_80")
        self.assertIn("net_edge_nonpositive", reason)

    def test_winner_profile_does_not_fee_guard_on_startup_fees_without_fills(self) -> None:
        controller = self.controller("winner_scale_24_40")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=-0.03,
                fee_usdc=0.03,
                trading_vs_hold_usdc=0.0,
                quote_delta=-0.0001,
                base_delta=0.0,
                tx_count=8,
                fill_detected=True,
            ),
            {
                **previous_metrics(),
                "net_trading_vs_hold_usdc": 0.0,
                "trading_vs_hold_usdc": 0.0,
                "fee_usdc": 0.0,
                "base_delta": 0.0,
                "quote_delta": 0.0,
                "tx_count": 2,
            },
            0,
        )

        self.assertNotEqual(profile, "fee_guard_passive_80")
        self.assertNotIn("net_edge_nonpositive", reason)

    def test_hourly_evaluation_does_not_stop_on_stale_feed_only_window(self) -> None:
        controller = self.controller("overnight_selective_edge_probe")

        profile, reason = controller.decide_next_profile(
            metrics(net=0.0, quote_delta=0.0, price_feed_stale=4),
            {
                **previous_metrics(),
                "price_feed_stale": 0,
                "tx_count": 5,
            },
            1,
        )

        self.assertNotEqual(profile, "__stop__")
        self.assertIn("health/fee guard", reason)

    def test_initial_snapshot_does_not_treat_historical_stale_count_as_new_window(self) -> None:
        controller = self.controller("overnight_balanced_low_churn")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=0.0,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                fill_detected=False,
                price_feed_stale=2,
            ),
            None,
            1,
        )

        self.assertNotEqual(profile, "fee_guard_passive_80")
        self.assertNotIn("window_stale=2", reason)

    def test_initial_snapshot_does_not_apply_cumulative_fee_guard(self) -> None:
        controller = self.controller("overnight_selective_edge_probe")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=-0.063,
                fee_usdc=0.063,
                trading_vs_hold_usdc=0.0,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                fill_detected=False,
            ),
            None,
            0,
        )

        self.assertEqual(profile, "overnight_selective_edge_probe")
        self.assertIn("startup evaluation baseline", reason)

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

    def test_single_quote_reduce_only_sample_holds_profile_pending_confirmation(self) -> None:
        controller = self.controller("resting_low_churn_16_30")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=0.0,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                fill_detected=False,
                rpc_stale=True,
            ),
            {
                **previous_metrics(),
                "tx_count": 5,
                "live_action": "continue",
                "live_state": "healthy",
            },
            3,
        )

        self.assertEqual(profile, "resting_low_churn_16_30")
        self.assertIn("pending confirmation", reason)

    def test_both_stale_uses_controller_backoff_before_clear(self) -> None:
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
        self.assertEqual(status["action"], "staleness_backoff")
        self.assertEqual(status["restart_policy"], "manual_only")
        self.assertEqual(status["alert"]["severity"], "warning")

    def test_watchdog_requires_three_staleness_strikes_before_clear(self) -> None:
        controller = self.controller()
        previous = metrics(net=0.0, quote_delta=0.0)
        stale_metrics = metrics(
            net=0.0,
            quote_delta=0.0,
            dashboard_ok=True,
            dashboard_stale=True,
            rpc_ok=True,
            rpc_stale=True,
            heartbeat_ok=True,
        )

        self.assertIsNone(controller.decide_watchdog_stop(stale_metrics, previous))
        self.assertEqual(controller.decide_watchdog_action(stale_metrics, previous)[0], "continue")
        self.assertEqual(controller.decide_watchdog_action(stale_metrics, previous)[0], "clear_book")
        self.assertEqual(controller.decide_watchdog_action(stale_metrics, previous)[0], "continue")

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

    def test_balanced_profile_enters_selective_discovery_when_gates_clean(self) -> None:
        controller = self.controller("overnight_balanced_low_churn")

        profile, reason = controller.decide_next_profile(
            metrics(
                net=0.0,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                fill_detected=False,
            ),
            previous_metrics(),
            2,
        )

        self.assertEqual(profile, "overnight_selective_edge_probe")
        self.assertIn("clean gated no-fill discovery", reason)

    def test_balanced_profile_holds_when_market_intel_source_stale(self) -> None:
        controller = self.controller("overnight_balanced_low_churn")
        stale_intel = {
            "enabled": True,
            "ok": True,
            "quote_enabled": True,
            "mode": "normal",
            "fair_value": 80.0,
            "spread_add_bps": 0.0,
            "generated_at_unix_secs": time.time() - 120.0,
            "source_quality": {"status": "stale", "stale": True},
        }

        profile, reason = controller.decide_next_profile(
            metrics(
                net=0.0,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                fill_detected=False,
                market_intel=stale_intel,
            ),
            previous_metrics(),
            2,
        )

        self.assertEqual(profile, "overnight_balanced_low_churn")
        self.assertIn("market-intel source freshness", reason)

    def test_passive_fee_guard_enters_bounded_fill_discovery_when_no_fills_and_clean(self) -> None:
        controller = self.controller("fee_guard_passive_80")
        controller.profile_entered_at = time.time() - 7200

        profile, reason = controller.decide_next_profile(
            metrics(
                net=0.0,
                fee_usdc=0.018,
                trading_vs_hold_usdc=0.018,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                tx_count=8,
                fill_detected=False,
            ),
            previous_metrics(),
            3,
        )

        self.assertEqual(profile, "fill_discovery_capped")
        self.assertIn("no_fill_timeout_escape", reason)
        self.assertIn("bounded fill discovery", reason)
        self.assertIn("no_fill_reason=fee_guard_no_edge", reason)

    def test_passive_fee_guard_uses_window_fill_not_cumulative_fill_for_recovery(self) -> None:
        controller = self.controller("fee_guard_passive_80")
        controller.profile_entered_at = time.time() - 7200

        profile, reason = controller.decide_next_profile(
            metrics(
                net=-0.085,
                fee_usdc=0.008,
                trading_vs_hold_usdc=-0.077,
                quote_delta=17.98,
                base_delta=-0.244,
                base_total=4.081,
                quote_total=536.29,
                tx_count=21,
                fill_detected=True,
            ),
            {
                **previous_metrics(),
                "fee_usdc": 0.008,
                "net_trading_vs_hold_usdc": -0.085,
                "trading_vs_hold_usdc": -0.077,
                "base_delta": -0.244,
                "quote_delta": 17.98,
                "tx_count": 20,
            },
            11,
        )

        self.assertEqual(profile, "fill_discovery_capped")
        self.assertIn("bounded fill discovery", reason)
        self.assertIn("no_fill_reason=fee_guard_no_edge", reason)

    def test_resting_low_churn_escapes_after_cumulative_fill_goes_idle(self) -> None:
        controller = self.controller("resting_low_churn_16_30")
        controller.profile_entered_at = time.time() - 7200

        current = metrics(
            net=-0.010,
            fee_usdc=0.013,
            trading_vs_hold_usdc=0.003,
            quote_delta=0.2812,
            base_delta=-0.004,
            base_total=5.338,
            quote_total=444.46,
            mid_price=69.61,
            bid_levels=3,
            ask_levels=2,
            tx_count=38,
            fill_detected=True,
        )
        current["pnl"] |= {
            "seconds_since_last_fill": 3900,
            "fills_this_window": 0,
            "total_fills": 8,
        }

        profile, reason = controller.decide_next_profile(
            current,
            {
                **previous_metrics(),
                "fee_usdc": 0.012,
                "net_trading_vs_hold_usdc": -0.010,
                "trading_vs_hold_usdc": 0.002,
                "base_delta": -0.004,
                "quote_delta": 0.2812,
                "tx_count": 32,
                "live_action": "continue",
                "live_state": "healthy",
            },
            9,
        )

        self.assertEqual(profile, "fill_discovery_capped")
        self.assertIn("resting_low_churn_idle_escape", reason)
        self.assertIn("seconds_since_last_fill=3900", reason)

    def test_passive_fee_guard_stops_when_no_edge_and_discovery_is_not_safe(self) -> None:
        controller = self.controller("fee_guard_passive_80")
        controller.profile_entered_at = time.time() - 7200
        stale_intel = {
            "enabled": True,
            "ok": True,
            "quote_enabled": True,
            "mode": "pause",
            "fair_value": 80.0,
            "spread_add_bps": 0.0,
            "generated_at_unix_secs": time.time() - 120.0,
            "source_quality": {"status": "stale", "stale": True},
        }

        profile, reason = controller.decide_next_profile(
            metrics(
                net=-0.01,
                fee_usdc=0.02,
                trading_vs_hold_usdc=0.01,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                tx_count=8,
                fill_detected=False,
                market_intel=stale_intel,
            ),
            previous_metrics(),
            3,
        )

        self.assertEqual(profile, "__stop__")
        self.assertIn("no-edge fee guard", reason)
        self.assertIn("market-intel", reason)

    def test_compact_metrics_includes_no_fill_and_after_cost_attribution(self) -> None:
        controller = self.controller("fee_guard_passive_80")

        compact = controller.compact_metrics(
            metrics(
                net=0.0,
                fee_usdc=0.018,
                trading_vs_hold_usdc=0.018,
                quote_delta=0.0,
                base_delta=0.0,
                base_total=1.0,
                quote_total=100.0,
                tx_count=8,
                fill_detected=False,
            )
        )

        self.assertEqual(compact["no_fill_reason"], "fee_guard_no_edge")
        self.assertEqual(compact["quote_time_profile"], "fee_guard_passive_80")
        self.assertGreater(compact["break_even_spread_bps"], 0.0)

    def test_compact_metrics_includes_fill_idle_counters_and_forced_transition_budget(self) -> None:
        controller = self.controller("fill_discovery_capped")

        compact = controller.compact_metrics(
            metrics(
                net=0.0,
                quote_delta=0.0,
                base_delta=0.0,
                fill_detected=False,
                tx_count=8,
            )
            | {
                "pnl": {
                    "net_trading_vs_hold_usdc": 0.0,
                    "trading_vs_hold_usdc": 0.0,
                    "fee_usdc": 0.01,
                    "base_delta": 0.0,
                    "quote_delta": 0.0,
                    "fill_detected": False,
                    "seconds_since_last_fill": 3900,
                    "fills_this_window": 0,
                    "total_fills": 2,
                },
                "strategy": {
                    "forced_transition_remaining_trial_minutes": 19.5,
                },
            }
        )

        self.assertEqual(compact["seconds_since_last_fill"], 3900)
        self.assertEqual(compact["fills_this_window"], 0)
        self.assertEqual(compact["total_fills"], 2)
        self.assertEqual(compact["forced_transition_remaining_trial_minutes"], 19.5)

    def test_compact_metrics_includes_parent_cluster_context(self) -> None:
        controller = self.controller("fee_guard_passive_80")

        compact = controller.compact_metrics(
            metrics(
                net=0.0,
                quote_delta=0.0,
                fill_detected=False,
                market_intel={
                    "enabled": True,
                    "ok": True,
                    "quote_enabled": True,
                    "mode": "normal",
                    "fair_value": 80.0,
                    "spread_add_bps": 0.0,
                    "generated_at_unix_secs": time.time(),
                    "source_quality": {"status": "ok", "stale": False},
                    "parent_cluster_id": "sol_stable",
                    "parent_cluster_mode": "normal",
                    "parent_cluster_child_venues": ["manifest:sol_usdc", "archer:sol_usdc"],
                    "parent_cluster_reason_codes": ["shared_sol_stable_parent_policy"],
                },
            )
        )

        self.assertEqual(compact["parent_cluster_id"], "sol_stable")
        self.assertEqual(compact["parent_cluster_mode"], "normal")
        self.assertIn("archer:sol_usdc", compact["parent_cluster_child_venues"])
        self.assertEqual(
            compact["parent_cluster_reason_codes"],
            ["shared_sol_stable_parent_policy"],
        )

    def test_compact_metrics_names_parent_normal_local_fee_guard_deviation(self) -> None:
        controller = self.controller("fee_guard_passive_80")

        compact = controller.compact_metrics(
            metrics(
                net=0.0,
                quote_delta=0.0,
                fill_detected=False,
                market_intel={
                    "enabled": True,
                    "ok": True,
                    "quote_enabled": True,
                    "mode": "normal",
                    "fair_value": 80.0,
                    "spread_add_bps": 0.0,
                    "generated_at_unix_secs": time.time(),
                    "source_quality": {"status": "ok", "stale": False},
                    "parent_cluster_id": "sol_stable",
                    "parent_cluster_mode": "normal",
                    "parent_cluster_reason_codes": ["shared_sol_stable_parent_policy"],
                },
            )
        )

        self.assertEqual(compact["parent_child_attribution"], "parent_normal_local_fee_guard")
        self.assertIn("parent normal", compact["parent_child_deviation_reason"])

    def test_no_edge_fee_guard_reason_includes_parent_normal_local_no_fill(self) -> None:
        controller = self.controller("fee_guard_passive_80")
        controller.profile_entered_at = time.time() - 3600
        current = metrics(
            net=0.0,
            fee_usdc=0.02,
            trading_vs_hold_usdc=0.02,
            quote_delta=0.0,
            base_delta=0.0,
            tx_count=3,
            fill_detected=False,
            market_intel={
                "enabled": True,
                "ok": True,
                "quote_enabled": True,
                "mode": "normal",
                "fair_value": 80.0,
                "spread_add_bps": 0.0,
                "generated_at_unix_secs": time.time(),
                "source_quality": {"status": "ok", "stale": False},
                "parent_cluster_id": "sol_stable",
                "parent_cluster_mode": "normal",
            },
        )

        profile, reason = controller.decide_next_profile(current, previous_metrics(), 1)

        self.assertEqual(profile, "fill_discovery_capped")
        self.assertIn("parent_normal_local_no_fill", reason)


if __name__ == "__main__":
    unittest.main()
