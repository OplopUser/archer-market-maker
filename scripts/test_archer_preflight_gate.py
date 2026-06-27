#!/usr/bin/env python3
"""Regression tests for Archer preflight start gate."""

from __future__ import annotations

import argparse
from unittest import mock
import unittest

from scripts import archer_preflight_gate
from scripts.archer_preflight_gate import validate_metrics, wait_for_valid_metrics


def args(**overrides: object) -> argparse.Namespace:
    defaults = {
        "expected_run_id": "test-run",
        "expected_profile": "overnight_balanced_low_churn",
        "min_effective_spread_bps": 62.0,
        "min_market_intel_spread_add_bps": 0.0,
        "min_base_free": 0.25,
        "min_quote_free": 25.0,
        "min_base_notional": 25.0,
        "max_base_locked": 0.000001,
        "max_quote_locked": 0.0001,
        "max_price_feed_stale": 0.0,
        "max_priority_fee_sampling_failures": 0.0,
        "max_rpc_429": 0.0,
        "max_tx_circuit_breaker": 0.0,
        "max_tx_send_failed": 0.0,
        "require_no_bot": True,
        "require_no_controller": True,
        "require_clear_book": True,
        "require_owner_match": True,
        "require_market_intel": True,
        "require_market_intel_dns": False,
        "market_intel_host": "market-intel",
        "max_market_intel_age_secs": 45.0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def healthy_metrics() -> dict:
    return {
        "run": {"run_id": "test-run"},
        "market": {"command_ok": True, "owner_matches_archer": True},
        "status": {
            "command_ok": True,
            "stale": False,
            "bid_levels": 0,
            "ask_levels": 0,
            "base_free": 1.8,
            "base_locked": 0.0,
            "quote_free": 160.0,
            "quote_locked": 0.0,
        },
        "mid_price": 80.0,
        "process": {"bot_running": False, "controller_running": False},
        "strategy": {
            "active_profile": "overnight_balanced_low_churn",
            "min_effective_spread_bps": 62.0,
            "effective_spreads_bps": [62.0, 80.0],
        },
        "market_intel": {
            "enabled": True,
            "ok": True,
            "quote_enabled": True,
            "url": "http://market-intel:8790/api/signals/scoped/archer/sol_usdc",
            "fair_value": "80.1",
            "source_quality": {"status": "ok", "stale": False},
            "spread_add_bps": "12.0",
        },
        "logs": {
            "counts": {
                "price_feed_stale": 0,
                "priority_fee_sampling_failed": 0,
                "rpc_429": 0,
                "tx_circuit_breaker": 0,
                "tx_send_failed": 0,
            }
        },
    }


class ArcherPreflightGateTests(unittest.TestCase):
    def test_healthy_metrics_pass(self) -> None:
        self.assertEqual(validate_metrics(healthy_metrics(), args()), [])

    def test_rejects_unsafe_start_state(self) -> None:
        metrics = healthy_metrics()
        metrics["status"]["stale"] = True
        metrics["status"]["bid_levels"] = 1
        metrics["process"]["bot_running"] = True
        metrics["strategy"]["effective_spreads_bps"] = [24.0, 42.0]
        metrics["logs"]["counts"]["rpc_429"] = 1

        failures = validate_metrics(metrics, args())

        self.assertTrue(any("stale" in failure for failure in failures))
        self.assertTrue(any("already running" in failure for failure in failures))
        self.assertTrue(any("book is not clear" in failure for failure in failures))
        self.assertTrue(any("tightest effective spread" in failure for failure in failures))
        self.assertTrue(any("rpc_429" in failure for failure in failures))

    def test_waits_for_valid_metrics_after_partial_dashboard_snapshot(self) -> None:
        partial = healthy_metrics()
        partial["strategy"]["active_profile"] = None
        ready = healthy_metrics()
        gate_args = args(wait_seconds=2.0, retry_interval_seconds=0.0, metrics_file="")

        with mock.patch.object(
            archer_preflight_gate, "load_metrics", side_effect=[partial, ready]
        ):
            metrics, failures = wait_for_valid_metrics(gate_args)

        self.assertIs(metrics, ready)
        self.assertEqual(failures, [])

    def test_accepts_per_source_market_intel_quality_list(self) -> None:
        metrics = healthy_metrics()
        metrics["market_intel"]["source_quality"] = [
            {
                "source": "binance",
                "freshness_status": "fresh",
                "quality_status": "healthy",
                "policy_impact": ["quote_blocking"],
            },
            {
                "source": "solana_rpc",
                "freshness_status": "fresh",
                "quality_status": "healthy",
                "policy_impact": ["quote_blocking", "hedge_blocking"],
            },
        ]

        self.assertEqual(validate_metrics(metrics, args()), [])

    def test_accepts_effective_spreads_when_min_effective_field_is_missing(self) -> None:
        metrics = healthy_metrics()
        metrics["strategy"]["min_effective_spread_bps"] = 0.0
        metrics["strategy"]["effective_spreads_bps"] = [16.0, 24.0, 36.0]

        self.assertEqual(
            validate_metrics(metrics, args(min_effective_spread_bps=16.0)),
            [],
        )

    def test_rejects_unresolvable_market_intel_container_dns(self) -> None:
        with mock.patch.object(archer_preflight_gate.socket, "getaddrinfo", side_effect=OSError("no host")):
            failures = validate_metrics(healthy_metrics(), args(require_market_intel_dns=True))

        self.assertTrue(any("market-intel DNS failed" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
