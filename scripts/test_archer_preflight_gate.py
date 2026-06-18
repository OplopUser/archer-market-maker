#!/usr/bin/env python3
"""Regression tests for Archer preflight start gate."""

from __future__ import annotations

import argparse
import os
import tempfile
import time
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
        "min_market_intel_source_count": 2,
        "min_market_intel_route_quality": 0.5,
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
        "require_readiness_provenance": True,
        "expected_mode": "",
        "config_file": "",
        "config_max_age_seconds": 0.0,
        "canary_envelope_file": "",
        "canary_approval_artifact_file": "",
        "rollback_command": "",
        "require_canary_envelope": False,
        "static_only": False,
        "post_start": False,
        "expected_source_checksum": "",
        "expected_source_commit": "",
        "allow_missing_source": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def healthy_metrics() -> dict:
    return {
        "run": {"run_id": "test-run", "mode": "shadow"},
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
            "url": "http://market-intel:8790/api/signals/sol_usdc",
            "fair_value": "80.1",
            "spread_add_bps": "12.0",
            "source_count": 2,
            "route_quality": {"score": 0.95, "status": "ok"},
        },
        "readiness": {
            "venue": "archer",
            "market": "SOL/USDC",
            "mode": "shadow",
            "status": "ready_for_shadow",
            "certification_passed": True,
            "routing_proof_passed": True,
            "live_approved": False,
            "policy_status": "accepted",
        },
        "provenance": {
            "source_branch": "codex/archer-certification",
            "source_commit": "abc1234",
            "config_checksum": "sha256:test-config",
            "policy_version": "propamm.quote-policy.v1",
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

    def test_rejects_missing_readiness_provenance_or_routing_proof(self) -> None:
        metrics = healthy_metrics()
        metrics["readiness"]["routing_proof_passed"] = False
        metrics["provenance"]["config_checksum"] = ""

        failures = validate_metrics(metrics, args())

        self.assertTrue(any("routing proof" in failure for failure in failures))
        self.assertTrue(any("config checksum" in failure for failure in failures))

    def test_rejects_live_mode_without_live_approval(self) -> None:
        metrics = healthy_metrics()
        metrics["readiness"]["mode"] = "live"
        metrics["readiness"]["status"] = "ready_for_live"

        failures = validate_metrics(metrics, args())

        self.assertTrue(any("live approval" in failure for failure in failures))

    def test_rejects_wrong_expected_mode(self) -> None:
        failures = validate_metrics(healthy_metrics(), args(expected_mode="canary"))

        self.assertTrue(any("run mode shadow != expected canary" in failure for failure in failures))

    def test_accepts_expected_mode_from_service_env_when_dashboard_omits_mode(self) -> None:
        metrics = healthy_metrics()
        metrics["run"].pop("mode")

        with mock.patch.dict(os.environ, {"ARCHER_RUN_MODE": "canary"}):
            failures = validate_metrics(metrics, args(expected_mode="canary"))

        self.assertFalse(any("run mode" in failure for failure in failures))

    def test_rejects_missing_canary_envelope_and_rollback(self) -> None:
        failures = validate_metrics(
            healthy_metrics(),
            args(expected_mode="canary", require_canary_envelope=True),
        )

        self.assertTrue(any("canary envelope is required" in failure for failure in failures))
        self.assertTrue(any("rollback command is required" in failure for failure in failures))

    def test_rejects_stale_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-canary.toml")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("[execution]\nshadow_mode = true\n")
            stale_mtime = time.time() - 120.0
            os.utime(config_path, (stale_mtime, stale_mtime))

            failures = validate_metrics(
                healthy_metrics(),
                args(config_file=config_path, config_max_age_seconds=60.0),
            )

        self.assertTrue(any("config is stale" in failure for failure in failures))

    def test_static_only_validates_local_artifacts_without_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-shadow.toml")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("[execution]\nshadow_mode = true\n")

            failures = validate_metrics(
                {},
                args(
                    static_only=True,
                    expected_mode="shadow",
                    config_file=config_path,
                    expected_source_checksum="sha256:abc123",
                    expected_source_commit="deadbeef",
                    expected_run_id="",
                    expected_profile="",
                    require_market_intel=False,
                    require_owner_match=False,
                ),
            )

        self.assertEqual(failures, [])

    def test_static_only_canary_does_not_require_dashboard_wallet_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-canary.toml")
            envelope_path = os.path.join(tmp, "envelope.toml")
            approval_path = os.path.join(tmp, "approval.toml")
            rollback_path = os.path.join(tmp, "rollback")
            with open(rollback_path, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env sh\nexit 0\n")
            os.chmod(rollback_path, 0o755)
            with open(approval_path, "w", encoding="utf-8") as fh:
                fh.write("[approval]\nid = \"test-approval\"\n")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[market]
market_pubkey = "market-1"
maker_keypair_path = "/tmp/archer.json"

[strategy]
spread_levels_bps = [80.0]

[risk]
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0

[execution]
shadow_mode = false
max_tx_per_minute = 2
max_update_tx_per_10min = 2
"""
                )
            with open(envelope_path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""
[canary_envelope]
approved_profile = "first-live-sol-usdc"
market = "SOL/USDC"
market_pubkey = "market-1"
max_levels_per_side = 1
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
max_wallet_base = 0.30
max_wallet_quote = 50.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0
max_tx_per_minute = 2
max_update_tx_per_10min = 2
max_runtime_minutes = 30

[manifest_coexistence]
same_market_rule = "manifest_same_market_must_be_stopped"

[wallet_limits]
max_native_sol_fee_reserve = 0.05
max_wsol = 0.30
max_usdc = 50.0

[rollback]
command = "{rollback_path}"
"""
                )

            failures = validate_metrics(
                {},
                args(
                    static_only=True,
                    expected_mode="canary",
                    config_file=config_path,
                    expected_source_checksum="sha256:abc123",
                    expected_source_commit="deadbeef",
                    canary_envelope_file=envelope_path,
                    canary_approval_artifact_file=approval_path,
                    rollback_command=rollback_path,
                    require_canary_envelope=True,
                    expected_run_id="",
                    expected_profile="",
                ),
            )

        self.assertEqual(failures, [])

    def test_static_only_requires_source_identity_or_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-shadow.toml")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("[execution]\nshadow_mode = true\n")

            failures = validate_metrics(
                {},
                args(
                    static_only=True,
                    expected_mode="shadow",
                    config_file=config_path,
                    expected_run_id="",
                    expected_profile="",
                    require_market_intel=False,
                    require_owner_match=False,
                ),
            )
            override_failures = validate_metrics(
                {},
                args(
                    static_only=True,
                    expected_mode="shadow",
                    config_file=config_path,
                    expected_run_id="",
                    expected_profile="",
                    require_market_intel=False,
                    require_owner_match=False,
                    allow_missing_source=True,
                ),
            )

        self.assertTrue(any("source commit/checksum" in failure for failure in failures))
        self.assertEqual(override_failures, [])

    def test_canary_envelope_rejects_oversized_wallet_and_token_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope_path = os.path.join(tmp, "envelope.toml")
            approval_path = os.path.join(tmp, "approval.toml")
            rollback_path = os.path.join(tmp, "rollback")
            with open(rollback_path, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env sh\nexit 0\n")
            os.chmod(rollback_path, 0o755)
            with open(approval_path, "w", encoding="utf-8") as fh:
                fh.write("[approval]\nid = \"test-approval\"\n")
            with open(envelope_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[canary_envelope]
approved_profile = "first-live-sol-usdc"
market = "SOL/USDC"
market_pubkey = "market-1"
max_levels_per_side = 1
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
max_wallet_base = 0.30
max_wallet_quote = 50.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0
max_tx_per_minute = 2
max_update_tx_per_10min = 2
max_runtime_minutes = 30

[manifest_coexistence]
same_market_rule = "manifest_same_market_must_be_stopped"

[wallet_limits]
max_native_sol_fee_reserve = 0.05
max_wsol = 0.30
max_usdc = 50.0

[rollback]
command = "rollback"
"""
                )

            metrics = healthy_metrics()
            metrics["wallet"] = {
                "balances": {
                    "native_sol": 0.02,
                    "wsol": 0.45,
                    "usdc": 12.0,
                    "errors": ["usdc: token account missing"],
                },
                "token_accounts": {"wsol": {"exists": True}, "usdc": {"exists": False}},
            }
            failures = validate_metrics(
                metrics,
                args(
                    expected_mode="canary",
                    canary_envelope_file=envelope_path,
                    rollback_command=rollback_path,
                    require_canary_envelope=True,
                ),
            )

        self.assertTrue(any("wallet wsol" in failure for failure in failures))
        self.assertTrue(any("wallet balance errors" in failure for failure in failures))
        self.assertTrue(any("token account usdc" in failure for failure in failures))

    def test_canary_envelope_checks_config_against_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope_path = os.path.join(tmp, "envelope.toml")
            config_path = os.path.join(tmp, "archer-canary.toml")
            approval_path = os.path.join(tmp, "approval.toml")
            rollback_path = os.path.join(tmp, "rollback")
            with open(rollback_path, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env sh\nexit 0\n")
            os.chmod(rollback_path, 0o755)
            with open(approval_path, "w", encoding="utf-8") as fh:
                fh.write("[approval]\nid = \"test-approval\"\n")
            with open(envelope_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[canary_envelope]
approved_profile = "first-live-sol-usdc"
market = "SOL/USDC"
market_pubkey = "market-1"
max_levels_per_side = 1
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
max_wallet_base = 0.30
max_wallet_quote = 50.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0
max_tx_per_minute = 2
max_update_tx_per_10min = 2
max_runtime_minutes = 30

[manifest_coexistence]
same_market_rule = "manifest_same_market_must_be_stopped"

[wallet_limits]
max_native_sol_fee_reserve = 0.05
max_wsol = 0.30
max_usdc = 50.0

[rollback]
command = "rollback"
"""
                )
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[market]
market_pubkey = "market-1"
maker_keypair_path = "/tmp/archer.json"

[strategy]
spread_levels_bps = [80.0, 90.0]
inventory_pct = 20.0

[risk]
max_quote_notional_per_level = 15.0
max_total_quote_notional = 30.0
min_base_reserve_pct = 50.0
min_quote_reserve_pct = 50.0

[execution]
shadow_mode = false
max_tx_per_minute = 5
max_update_tx_per_10min = 4
"""
                )

            failures = validate_metrics(
                healthy_metrics(),
                args(
                    expected_mode="canary",
                    config_file=config_path,
                    canary_envelope_file=envelope_path,
                    canary_approval_artifact_file=approval_path,
                    rollback_command=rollback_path,
                    require_canary_envelope=True,
                ),
            )

        self.assertTrue(any("config level count" in failure for failure in failures))
        self.assertTrue(any("max_total_quote_notional" in failure for failure in failures))
        self.assertTrue(any("max_tx_per_minute" in failure for failure in failures))

    def test_rejects_source_supervisor_and_wallet_readiness_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-shadow.toml")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[market]
maker_keypair_path = "/tmp/expected-maker.json"

[execution]
shadow_mode = true
"""
                )

            metrics = healthy_metrics()
            metrics["source"] = {"checksum": "bad", "commit": "bad"}
            metrics["wallet"] = {
                "keypair_path": "/tmp/other-maker.json",
                "token_accounts": {"wsol": {"ready": True}, "usdc": {"ready": False}},
            }
            metrics["supervisor"] = {
                "expected_active": True,
                "active": False,
                "policy": {"ok": False, "status": "blocked"},
            }

            failures = validate_metrics(
                metrics,
                args(
                    config_file=config_path,
                    expected_mode="shadow",
                    expected_source_checksum="abc123",
                    expected_source_commit="deadbeef",
                ),
            )

        self.assertTrue(any("source checksum" in failure for failure in failures))
        self.assertTrue(any("source commit" in failure for failure in failures))
        self.assertTrue(any("wallet keypair" in failure for failure in failures))
        self.assertTrue(any("token account usdc" in failure for failure in failures))
        self.assertTrue(any("supervisor active" in failure for failure in failures))
        self.assertTrue(any("supervisor policy" in failure for failure in failures))

    def test_post_start_shadow_requires_source_and_supervisor_blocks(self) -> None:
        metrics = healthy_metrics()

        failures = validate_metrics(
            metrics,
            args(
                post_start=True,
                expected_mode="shadow",
                expected_source_checksum="sha256:abc123",
                expected_source_commit="deadbeef",
            ),
        )

        self.assertTrue(any("source metrics are missing" in failure for failure in failures))
        self.assertTrue(any("supervisor metrics are missing" in failure for failure in failures))

    def test_post_start_shadow_rejects_supervisor_active_without_runner_process(self) -> None:
        metrics = healthy_metrics()
        metrics["source"] = {"checksum": "sha256:abc123", "commit": "deadbeef"}
        metrics["supervisor"] = {
            "expected_active": True,
            "active": True,
            "process_active": False,
            "policy": {"ok": True},
        }

        failures = validate_metrics(
            metrics,
            args(
                post_start=True,
                expected_mode="shadow",
                expected_source_checksum="sha256:abc123",
                expected_source_commit="deadbeef",
            ),
        )

        self.assertTrue(any("supervisor process active" in failure for failure in failures))

    def test_post_start_shadow_accepts_active_runner_process(self) -> None:
        metrics = healthy_metrics()
        metrics["process"]["bot_running"] = True
        metrics["source"] = {"checksum": "sha256:abc123", "commit": "deadbeef"}
        metrics["supervisor"] = {
            "expected_active": True,
            "active": True,
            "process_active": True,
            "policy": {"ok": True},
        }

        failures = validate_metrics(
            metrics,
            args(
                post_start=True,
                expected_mode="shadow",
                expected_source_checksum="sha256:abc123",
                expected_source_commit="deadbeef",
            ),
        )

        self.assertEqual(failures, [])

    def test_canary_requires_wallet_readiness_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-canary.toml")
            envelope_path = os.path.join(tmp, "envelope.toml")
            rollback_path = os.path.join(tmp, "rollback")
            with open(rollback_path, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env sh\nexit 0\n")
            os.chmod(rollback_path, 0o755)
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[market]
market_pubkey = "market-1"
maker_keypair_path = "/tmp/archer.json"

[strategy]
spread_levels_bps = [80.0]

[risk]
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0

[execution]
shadow_mode = false
max_tx_per_minute = 2
max_update_tx_per_10min = 2
"""
                )
            with open(envelope_path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""
[canary_envelope]
approved_profile = "first-live-sol-usdc"
market = "SOL/USDC"
market_pubkey = "market-1"
max_levels_per_side = 1
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
max_wallet_base = 0.30
max_wallet_quote = 50.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0
max_tx_per_minute = 2
max_update_tx_per_10min = 2
max_runtime_minutes = 30

[manifest_coexistence]
same_market_rule = "manifest_same_market_must_be_stopped"

[wallet_limits]
max_native_sol_fee_reserve = 0.05
max_wsol = 0.30
max_usdc = 50.0

[rollback]
command = "{rollback_path}"
"""
                )

            metrics = healthy_metrics()
            metrics["run"]["mode"] = "canary"
            metrics["strategy"]["active_profile"] = "first-live-sol-usdc"
            metrics["strategy"]["min_effective_spread_bps"] = 80.0
            metrics["strategy"]["effective_spreads_bps"] = [80.0]
            metrics["status"]["base_free"] = 0.1
            metrics["status"]["quote_free"] = 12.0
            metrics["source"] = {"checksum": "sha256:abc123", "commit": "deadbeef"}
            metrics["supervisor"] = {"expected_active": True, "active": True, "policy": {"ok": True}}
            metrics["wallet"] = {
                "pubkey": "maker",
                "balances": {"native_sol": 0.01, "wsol": 0.1, "usdc": 12.0, "errors": []},
            }

            failures = validate_metrics(
                metrics,
                args(
                    post_start=True,
                    expected_mode="canary",
                    expected_profile="first-live-sol-usdc",
                    expected_source_checksum="sha256:abc123",
                    expected_source_commit="deadbeef",
                    config_file=config_path,
                    canary_envelope_file=envelope_path,
                    rollback_command=rollback_path,
                    require_canary_envelope=True,
                    min_base_free=0.0,
                    min_quote_free=0.0,
                    min_base_notional=0.0,
                ),
            )

        self.assertTrue(any("wallet keypair readiness is missing" in failure for failure in failures))
        self.assertTrue(any("token account readiness is missing" in failure for failure in failures))

    def test_canary_accepts_matching_wallet_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-canary.toml")
            envelope_path = os.path.join(tmp, "envelope.toml")
            approval_path = os.path.join(tmp, "approval.toml")
            rollback_path = os.path.join(tmp, "rollback")
            with open(rollback_path, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env sh\nexit 0\n")
            os.chmod(rollback_path, 0o755)
            with open(approval_path, "w", encoding="utf-8") as fh:
                fh.write("[approval]\nid = \"test-approval\"\n")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[market]
market_pubkey = "market-1"
maker_keypair_path = "/tmp/archer.json"

[strategy]
spread_levels_bps = [80.0]

[risk]
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0

[execution]
shadow_mode = false
max_tx_per_minute = 2
max_update_tx_per_10min = 2
"""
                )
            with open(envelope_path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""
[canary_envelope]
approved_profile = "first-live-sol-usdc"
market = "SOL/USDC"
market_pubkey = "market-1"
max_levels_per_side = 1
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
max_wallet_base = 0.30
max_wallet_quote = 50.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0
max_tx_per_minute = 2
max_update_tx_per_10min = 2
max_runtime_minutes = 30

[manifest_coexistence]
same_market_rule = "manifest_same_market_must_be_stopped"

[wallet_limits]
max_native_sol_fee_reserve = 0.05
max_wsol = 0.30
max_usdc = 50.0

[rollback]
command = "{rollback_path}"
"""
                )

            metrics = healthy_metrics()
            metrics["run"]["mode"] = "canary"
            metrics["strategy"]["active_profile"] = "first-live-sol-usdc"
            metrics["strategy"]["min_effective_spread_bps"] = 80.0
            metrics["strategy"]["effective_spreads_bps"] = [80.0]
            metrics["status"]["base_free"] = 0.1
            metrics["status"]["quote_free"] = 12.0
            metrics["process"]["bot_running"] = True
            metrics["source"] = {"checksum": "sha256:abc123", "commit": "deadbeef"}
            metrics["supervisor"] = {
                "expected_active": True,
                "active": True,
                "process_active": True,
                "policy": {"ok": True},
            }
            metrics["wallet"] = {
                "pubkey": "maker",
                "keypair_path": "/tmp/archer.json",
                "balances": {"native_sol": 0.01, "wsol": 0.1, "usdc": 12.0, "errors": []},
                "token_accounts": {
                    "wsol": {"ready": True, "exists": True},
                    "usdc": {"ready": True, "exists": True},
                },
            }

            failures = validate_metrics(
                metrics,
                args(
                    post_start=True,
                    expected_mode="canary",
                    expected_profile="first-live-sol-usdc",
                    expected_source_checksum="sha256:abc123",
                    expected_source_commit="deadbeef",
                    config_file=config_path,
                    canary_envelope_file=envelope_path,
                    canary_approval_artifact_file=approval_path,
                    rollback_command=rollback_path,
                    require_canary_envelope=True,
                    min_base_free=0.0,
                    min_quote_free=0.0,
                    min_base_notional=0.0,
                ),
            )

        self.assertEqual(failures, [])

    def test_invalid_metrics_file_fails_closed_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            metrics_path = os.path.join(tmp, "metrics.json")
            with open(metrics_path, "w", encoding="utf-8") as fh:
                fh.write("")

            with self.assertRaises(SystemExit) as raised:
                wait_for_valid_metrics(args(metrics_file=metrics_path))

        self.assertIn("PRE-FLIGHT FAIL", str(raised.exception))
        self.assertIn("metrics file invalid", str(raised.exception))

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

    def test_rejects_degraded_market_intel_route_quality_and_source_count(self) -> None:
        metrics = healthy_metrics()
        metrics["market_intel"]["source_count"] = 1
        metrics["market_intel"]["route_quality"] = {"score": 0.20, "status": "degraded"}

        failures = validate_metrics(metrics, args())

        self.assertTrue(any("market-intel source_count" in failure for failure in failures))
        self.assertTrue(any("market-intel route_quality" in failure for failure in failures))

    def test_live_canary_requires_approval_artifact_before_placement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "archer-canary.toml")
            envelope_path = os.path.join(tmp, "envelope.toml")
            rollback_path = os.path.join(tmp, "rollback")
            with open(rollback_path, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env sh\nexit 0\n")
            os.chmod(rollback_path, 0o755)
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write(
                    """
[market]
market_pubkey = "market-1"
maker_keypair_path = "/tmp/archer.json"

[strategy]
spread_levels_bps = [80.0]

[risk]
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0

[execution]
shadow_mode = false
max_tx_per_minute = 2
max_update_tx_per_10min = 2
"""
                )
            with open(envelope_path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""
[canary_envelope]
approved_profile = "first-live-sol-usdc"
market = "SOL/USDC"
market_pubkey = "market-1"
max_levels_per_side = 1
max_quote_notional_per_level = 10.0
max_total_quote_notional = 20.0
max_wallet_base = 0.30
max_wallet_quote = 50.0
min_base_reserve_pct = 75.0
min_quote_reserve_pct = 75.0
max_tx_per_minute = 2
max_update_tx_per_10min = 2
max_runtime_minutes = 30

[manifest_coexistence]
same_market_rule = "manifest_same_market_must_be_stopped"

[wallet_limits]
max_native_sol_fee_reserve = 0.05
max_wsol = 0.30
max_usdc = 50.0

[rollback]
command = "{rollback_path}"
"""
                )

            metrics = healthy_metrics()
            metrics["run"]["mode"] = "canary"
            metrics["strategy"]["active_profile"] = "first-live-sol-usdc"
            metrics["strategy"]["min_effective_spread_bps"] = 80.0
            metrics["strategy"]["effective_spreads_bps"] = [80.0]
            metrics["source"] = {"checksum": "sha256:abc123", "commit": "deadbeef"}
            metrics["supervisor"] = {
                "expected_active": True,
                "active": True,
                "process_active": True,
                "policy": {"ok": True},
            }
            metrics["wallet"] = {
                "keypair_path": "/tmp/archer.json",
                "balances": {"native_sol": 0.01, "wsol": 0.1, "usdc": 12.0, "errors": []},
                "token_accounts": {
                    "wsol": {"ready": True, "exists": True},
                    "usdc": {"ready": True, "exists": True},
                },
            }

            failures = validate_metrics(
                metrics,
                args(
                    post_start=True,
                    expected_mode="canary",
                    expected_profile="first-live-sol-usdc",
                    expected_source_checksum="sha256:abc123",
                    expected_source_commit="deadbeef",
                    config_file=config_path,
                    canary_envelope_file=envelope_path,
                    rollback_command=rollback_path,
                    require_canary_envelope=True,
                    min_base_free=0.0,
                    min_quote_free=0.0,
                    min_base_notional=0.0,
                ),
            )

        self.assertTrue(any("approval artifact is required" in failure for failure in failures))

    def test_compose_canary_profile_requires_approval_artifact(self) -> None:
        compose = (
            archer_preflight_gate.Path(__file__).resolve().parents[1]
            / "deploy"
            / "docker-compose.archer.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("ARCHER_CANARY_APPROVAL_ARTIFACT", compose)
        self.assertIn("test -f \"$$ARCHER_CANARY_APPROVAL_ARTIFACT\"", compose)


if __name__ == "__main__":
    unittest.main()
