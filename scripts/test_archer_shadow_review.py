#!/usr/bin/env python3
"""Tests for Archer shadow run/review automation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import archer_shadow_review
from scripts.archer_shadow_review import review_capture_file, run_shadow_observation


def capture_event(**overrides: object) -> dict:
    event = {
        "event_type": "archer_shadow_metrics",
        "run_id": "shadow-run-001",
        "timestamp": "2026-06-18T01:02:03Z",
        "mode": "shadow",
        "venue": "archer",
        "market": "SOL/USDC",
        "source": {"type": "file", "path": "fixture.json"},
        "policy_version": "policy-fixture",
        "reason_codes": ["metrics_capture"],
        "makerbook_status": {
            "command_ok": True,
            "stale": False,
            "bid_levels": 2,
            "ask_levels": 2,
            "base_total": 1.25,
            "quote_total": 120.5,
        },
        "market_intel_snapshot": {"enabled": True, "ok": True, "reasons": []},
        "tx_summary": {"since_start_count": 0, "failed_count": 0, "kind_counts": {}},
        "fill_summary": {"fill_detected": False},
        "clean_book_actions": [],
        "simulated_makerbook_update": {
            "bid_levels": 2,
            "ask_levels": 2,
            "quote_notional": 80.0,
        },
    }
    event.update(overrides)
    return event


class ArcherShadowReviewTests(unittest.TestCase):
    def test_passes_fixture_shadow_capture_inside_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = Path(tmp) / "capture.jsonl"
            output_path = Path(tmp) / "review.json"
            policy_path = Path(tmp) / "policy.json"
            capture_path.write_text(json.dumps(capture_event()) + "\n")
            policy_path.write_text(
                json.dumps(
                    {
                        "version": "policy-fixture",
                        "max_failed_tx": 0,
                        "require_market_intel_ok": True,
                        "makerbook": {
                            "max_bid_levels": 3,
                            "max_ask_levels": 3,
                            "max_quote_notional": 100.0,
                        },
                    }
                )
            )

            review = review_capture_file(
                capture_path,
                output_path=output_path,
                policy_path=policy_path,
                observation_seconds=60,
            )

            self.assertEqual(review["status"], "pass")
            self.assertEqual(review["run_id"], "shadow-run-001")
            self.assertEqual(review["mode"], "shadow")
            self.assertEqual(review["venue"], "archer")
            self.assertEqual(review["policy_version"], "policy-fixture")
            self.assertEqual(review["started_at"], "2026-06-18T01:02:03Z")
            self.assertEqual(review["completed_at"], "2026-06-18T01:02:03Z")
            self.assertEqual(json.loads(output_path.read_text())["status"], "pass")

    def test_fails_when_simulated_makerbook_update_exceeds_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture_path = Path(tmp) / "capture.jsonl"
            policy_path = Path(tmp) / "policy.json"
            capture_path.write_text(
                json.dumps(
                    capture_event(
                        simulated_makerbook_update={
                            "bid_levels": 4,
                            "ask_levels": 2,
                            "quote_notional": 150.0,
                        }
                    )
                )
                + "\n"
            )
            policy_path.write_text(
                json.dumps(
                    {
                        "version": "policy-fixture",
                        "max_failed_tx": 0,
                        "makerbook": {
                            "max_bid_levels": 3,
                            "max_ask_levels": 3,
                            "max_quote_notional": 100.0,
                        },
                    }
                )
            )

            review = review_capture_file(
                capture_path,
                policy_path=policy_path,
                observation_seconds=60,
            )

            self.assertEqual(review["status"], "fail")
            self.assertIn("simulated_update_policy_violation", review["reason_codes"])

    def test_replay_report_includes_policy_route_exposure_edge_and_promotion_gate(self) -> None:
        events = [
            capture_event(
                config_checksum="sha256:fixture-config",
                static_config={"status": "pass", "checksum": "sha256:fixture-config"},
                signal_multipliers={"status": "pass", "spread_multiplier": 1.0, "size_multiplier": 0.8},
                quote_policy_control={"status": "pass", "version": "policy-fixture"},
                route_quality={"score": 0.91, "best_route": "archer_direct"},
                no_fill_exposure={"bid_notional": 55.0, "ask_notional": 40.0},
                expected_fill={"probability": 0.22},
                expected_edge={"bps": 3.6},
                promotion_inputs={
                    "shadow_passed": True,
                    "live_canary_passed": False,
                    "after_cost_edge_bps": 1.4,
                    "min_after_cost_edge_bps": 1.0,
                    "cross_venue_safe": True,
                    "requested_capital_usdc": 500.0,
                    "approved_capital_usdc": 1000.0,
                },
            )
        ]

        review = archer_shadow_review.evaluate_shadow_events(
            events,
            policy={
                "version": "policy-fixture",
                "min_route_quality_score": 0.80,
                "max_no_fill_quote_notional": 125.0,
                "min_expected_edge_bps": 1.0,
                "max_failed_tx": 0,
            },
            observation_seconds=60,
        )

        self.assertEqual(review["config_checksum"], "sha256:fixture-config")
        self.assertEqual(review["control_validation"]["static_config"]["status"], "pass")
        self.assertEqual(review["control_validation"]["signal_multipliers"]["status"], "pass")
        self.assertEqual(review["control_validation"]["quote_policy"]["status"], "pass")
        self.assertEqual(review["route_quality"]["status"], "pass")
        self.assertEqual(review["route_quality"]["min_score"], 0.91)
        self.assertEqual(review["no_fill_exposure"]["max_quote_notional"], 95.0)
        self.assertEqual(review["expected_fill_edge"]["avg_expected_fill_probability"], 0.22)
        self.assertEqual(review["expected_fill_edge"]["avg_expected_edge_bps"], 3.6)
        self.assertEqual(review["blockers"], ["live_canary_not_passed"])
        self.assertFalse(review["promotion_gate"]["live_canary_passed"])
        self.assertFalse(review["promotion_gate"]["multi_venue_allowed"])

    def test_url_observation_polls_until_fixed_window_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(archer_shadow_review, "capture_once") as capture_once:
                with mock.patch.object(archer_shadow_review, "review_capture_file", return_value={"status": "pass"}):
                    with mock.patch.object(archer_shadow_review.time, "sleep"):
                        with mock.patch.object(
                            archer_shadow_review.time,
                            "monotonic",
                            side_effect=[0.0, 0.0, 61.0],
                        ):
                            run_shadow_observation(
                                ["http://127.0.0.1:8787/api/metrics"],
                                Path(tmp),
                                run_id="shadow-run-001",
                                observation_seconds=60,
                                interval_seconds=15,
                            )

        self.assertEqual(capture_once.call_count, 2)

    def test_fixture_observation_keeps_writing_rows_until_fixed_window_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metrics_path = tmp_path / "dashboard-metrics.json"
            output_dir = tmp_path / "captures"
            metrics_path.write_text(
                json.dumps(
                    {
                        "time": "2026-06-18T01:02:03Z",
                        "run": {"run_id": "shadow-run-001"},
                        "market": {"symbol": "SOL/USDC", "command_ok": True},
                        "status": {"command_ok": True, "stale": False},
                        "transactions": {"since_start_count": 0, "failed_count": 0},
                    }
                )
            )

            with mock.patch.object(archer_shadow_review.time, "sleep"):
                with mock.patch.object(
                    archer_shadow_review.time,
                    "monotonic",
                    side_effect=[0.0, 0.0, 0.6, 1.1],
                ):
                    run_shadow_observation(
                        [str(metrics_path)],
                        output_dir,
                        run_id="shadow-run-001",
                        observation_seconds=1,
                        interval_seconds=0.5,
                    )

            capture_path = output_dir / "archer-shadow-shadow-run-001.jsonl"
            self.assertGreater(len(capture_path.read_text().splitlines()), 1)

    def test_policy_review_writes_loaded_policy_version_to_capture_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            metrics_path = tmp_path / "dashboard-metrics.json"
            policy_path = tmp_path / "policy.json"
            output_dir = tmp_path / "captures"
            metrics_path.write_text(
                json.dumps(
                    {
                        "time": "2026-06-18T01:02:03Z",
                        "run": {"run_id": "shadow-run-001"},
                        "market": {"symbol": "SOL/USDC", "command_ok": True},
                        "status": {"command_ok": True, "stale": False},
                        "transactions": {"since_start_count": 0, "failed_count": 0},
                    }
                )
            )
            policy_path.write_text(json.dumps({"version": "policy-from-review"}))

            run_shadow_observation(
                [str(metrics_path)],
                output_dir,
                run_id="shadow-run-001",
                observation_seconds=0,
                interval_seconds=0.5,
                policy_path=policy_path,
            )

            capture_path = output_dir / "archer-shadow-shadow-run-001.jsonl"
            rows = [json.loads(line) for line in capture_path.read_text().splitlines()]
            self.assertEqual(rows[0]["policy_version"], "policy-from-review")


if __name__ == "__main__":
    unittest.main()
