#!/usr/bin/env python3
"""Tests for retained Archer shadow metrics artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.archer_shadow_metrics_capture import build_capture_event, write_capture_event


def dashboard_metrics() -> dict:
    return {
        "time": "2026-06-18T01:02:03Z",
        "run": {"run_id": "shadow-run-001"},
        "config_path": "/tmp/archer-shadow.toml",
        "market": {
            "symbol": "SOL/USDC",
            "market_pubkey": "ArcherMarket111",
            "command_ok": True,
        },
        "status": {
            "source": "fixture-makerbook",
            "command_ok": True,
            "stale": False,
            "bid_levels": 2,
            "ask_levels": 2,
            "base_total": 1.25,
            "quote_total": 120.5,
        },
        "market_intel": {
            "enabled": True,
            "ok": True,
            "mode": "normal",
            "fair_value": 134.25,
            "spread_add_bps": 12.0,
            "reasons": ["flow_neutral"],
        },
        "transactions": {
            "ok": True,
            "since_start_count": 3,
            "failed_count": 0,
            "kind_counts": {"update_book": 2, "clear": 1},
            "last": [{"signature": "fixture-sig", "kind": "clear"}],
        },
        "pnl": {
            "fill_detected": True,
            "base_delta": 0.1,
            "quote_delta": -13.4,
            "net_trading_vs_hold_usdc": 0.04,
            "fee_usdc": 0.002,
        },
        "strategy": {
            "active_profile": "shadow_probe",
            "policy_version": "policy-2026-06-18",
            "simulated_makerbook_update": {
                "bid_levels": 2,
                "ask_levels": 2,
                "quote_notional": 80.0,
            },
        },
        "clean_book_actions": [
            {"time": "2026-06-18T01:02:00Z", "action": "clear_book", "reason": "start_clean"}
        ],
    }


class ArcherShadowMetricsCaptureTests(unittest.TestCase):
    def test_builds_retained_jsonl_event_from_dashboard_metrics(self) -> None:
        event = build_capture_event(
            dashboard_metrics(),
            run_id="shadow-run-001",
            source="/tmp/dashboard-metrics.json",
            market="SOL/USDC",
        )

        self.assertEqual(event["run_id"], "shadow-run-001")
        self.assertEqual(event["mode"], "shadow")
        self.assertEqual(event["venue"], "archer")
        self.assertEqual(event["market"], "SOL/USDC")
        self.assertEqual(event["policy_version"], "policy-2026-06-18")
        self.assertEqual(event["source"]["path"], "/tmp/dashboard-metrics.json")
        self.assertIn("clean_book_action", event["reason_codes"])
        self.assertEqual(event["makerbook_status"]["bid_levels"], 2)
        self.assertEqual(event["market_intel_snapshot"]["reasons"], ["flow_neutral"])
        self.assertEqual(event["tx_summary"]["kind_counts"]["clear"], 1)
        self.assertTrue(event["fill_summary"]["fill_detected"])
        self.assertEqual(event["clean_book_actions"][0]["reason"], "start_clean")

    def test_writes_one_json_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "captures" / "archer-shadow.jsonl"

            event = build_capture_event(dashboard_metrics(), source="fixture.json")
            write_capture_event(output, event)

            lines = output.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["venue"], "archer")

    def test_carries_replay_risk_and_portfolio_fields_when_present(self) -> None:
        metrics = dashboard_metrics()
        metrics["config_checksum"] = "sha256:fixture-config"
        metrics["static_config"] = {"status": "pass", "checksum": "sha256:fixture-config"}
        metrics["signal_multipliers"] = {"status": "pass", "size_multiplier": 0.8}
        metrics["quote_policy_control"] = {"status": "pass", "version": "policy-2026-06-18"}
        metrics["route_quality"] = {"score": 0.93, "best_route": "archer_direct"}
        metrics["strategy"]["quote_decision"] = {"decision": "update_full", "reason_codes": ["normal_policy"]}
        metrics["strategy"]["no_fill_exposure"] = {"bid_notional": 55.0, "ask_notional": 40.0}
        metrics["strategy"]["expected_fill"] = {"probability": 0.22}
        metrics["strategy"]["expected_edge"] = {"bps": 3.6}
        metrics["portfolio_exposure"] = {
            "base_net": 1.65,
            "quote_net": 200.0,
            "hedge_target": {"target_base_delta": -1.65},
        }

        event = build_capture_event(metrics, source="fixture.json")

        self.assertEqual(event["config_checksum"], "sha256:fixture-config")
        self.assertEqual(event["static_config"]["status"], "pass")
        self.assertEqual(event["signal_multipliers"]["size_multiplier"], 0.8)
        self.assertEqual(event["quote_policy_control"]["version"], "policy-2026-06-18")
        self.assertEqual(event["route_quality"]["score"], 0.93)
        self.assertEqual(event["quote_decision"]["decision"], "update_full")
        self.assertEqual(event["no_fill_exposure"]["bid_notional"], 55.0)
        self.assertEqual(event["expected_fill"]["probability"], 0.22)
        self.assertEqual(event["expected_edge"]["bps"], 3.6)
        self.assertEqual(event["portfolio_exposure"]["hedge_target"]["target_base_delta"], -1.65)


if __name__ == "__main__":
    unittest.main()
