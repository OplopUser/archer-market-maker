import datetime as dt
import json
import pathlib
import tempfile
import unittest

from dashboard.server import (
    DashboardState,
    classify_archer_live_status,
    iso,
    parent_child_attribution,
    parse_market_intel_payload,
    profile_approval_summary,
)


class ArcherDashboardMarketIntelTests(unittest.TestCase):
    def test_parse_market_intel_payload_accepts_archer_scoped_wrapper(self) -> None:
        payload = {
            "consumer": "archer",
            "allowed_source_set": ["binance", "manifest"],
            "excluded_source_set": ["bot_dashboard"],
            "signal": {
                "mode": "normal",
                "recommendation": {
                    "quote_enabled": True,
                    "fair_value": "80.1",
                    "spread_add_bps": "12.0",
                    "size_multiplier": "0.75",
                    "bid_size_multiplier": "0.5",
                    "ask_size_multiplier": "1.0",
                    "reasons": ["test"],
                },
                "checks": {
                    "parent_cluster_id": "sol_stable",
                    "parent_cluster_mode": "normal",
                    "parent_cluster_child_markets": ["sol_usdc", "sol_usdt", "sol_usd1"],
                    "parent_cluster_child_venues": [
                        "manifest:sol_usdc",
                        "manifest:sol_usdt",
                        "manifest:sol_usd1",
                        "archer:sol_usdc",
                    ],
                    "parent_cluster_base_pyth_conf_bps": "8.4",
                    "parent_cluster_hedge_status": "within_rebalance_threshold",
                    "parent_cluster_reason_codes": ["shared_sol_stable_parent_policy"],
                },
            },
        }

        parsed = parse_market_intel_payload(
            payload, "http://market-intel:8790/api/signals/scoped/archer/sol_usdc"
        )

        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["consumer"], "archer")
        self.assertEqual(parsed["excluded_source_set"], ["bot_dashboard"])
        self.assertEqual(parsed["mode"], "normal")
        self.assertEqual(parsed["fair_value"], "80.1")
        self.assertEqual(parsed["parent_cluster_id"], "sol_stable")
        self.assertEqual(parsed["parent_cluster_mode"], "normal")
        self.assertIn("archer:sol_usdc", parsed["parent_cluster_child_venues"])
        self.assertEqual(parsed["parent_cluster_base_pyth_conf_bps"], "8.4")

    def test_parent_child_attribution_marks_aligned_live_archer(self) -> None:
        attribution = parent_child_attribution(
            {
                "status": {"bid_levels": 2, "ask_levels": 2},
                "strategy": {"active_profile": "resting_low_churn_16_30"},
                "market_intel": {"parent_cluster_mode": "normal", "mode": "normal"},
                "archer_live_status": {"action": "continue"},
            }
        )

        self.assertEqual(attribution["parent_child_attribution"], "parent_child_aligned")
        self.assertIn("aligned", attribution["parent_child_deviation_reason"])

    def test_parent_child_attribution_marks_local_fee_guard(self) -> None:
        attribution = parent_child_attribution(
            {
                "status": {"bid_levels": 1, "ask_levels": 1},
                "strategy": {"active_profile": "fee_guard_passive_80"},
                "market_intel": {"parent_cluster_mode": "normal", "mode": "normal"},
                "archer_live_status": {"action": "continue"},
            }
        )

        self.assertEqual(attribution["parent_child_attribution"], "parent_normal_local_fee_guard")

    def test_sample_includes_last_fill_and_window_fill_metrics(self) -> None:
        state = DashboardState.__new__(DashboardState)

        sample = state.sample_from_metrics(
            {
                "time": "2026-06-23T00:00:00Z",
                "pnl": {
                    "seconds_since_last_fill": 2820,
                    "fills_this_window": 0,
                    "total_fills": 4,
                    "fill_detected": False,
                },
                "status": {},
                "transactions": {},
                "market_intel": {},
                "archer_live_status": {},
                "strategy": {
                    "forced_transition_remaining_trial_minutes": 18.0,
                    "requested_profile": "winner_scale_24_40",
                    "effective_profile": "overnight_selective_edge_probe",
                    "profile_approval": {
                        "approved": False,
                        "source": "approval_required",
                    },
                },
            }
        )

        self.assertEqual(sample["seconds_since_last_fill"], 2820)
        self.assertEqual(sample["fills_this_window"], 0)
        self.assertEqual(sample["total_fills"], 4)
        self.assertEqual(sample["forced_transition_remaining_trial_minutes"], 18.0)
        self.assertEqual(sample["requested_profile"], "winner_scale_24_40")
        self.assertEqual(sample["effective_profile"], "overnight_selective_edge_probe")
        self.assertFalse(sample["profile_approval_approved"])
        self.assertEqual(sample["profile_approval_source"], "approval_required")

    def test_compute_pnl_separates_trade_edge_from_hold_and_fees(self) -> None:
        state = DashboardState.__new__(DashboardState)

        pnl = state.compute_pnl(
            {"base_total": 1.4, "quote_total": 180.0},
            {"base_total": 2.0, "quote_total": 100.0, "mid_price": 100.0},
            {"fee_sol_total": 0.001},
            110.0,
        )

        self.assertTrue(pnl["available"])
        self.assertAlmostEqual(pnl["gross_pnl_usdc"], 34.0)
        self.assertAlmostEqual(pnl["hold_pnl_usdc"], 20.0)
        self.assertAlmostEqual(pnl["trading_vs_hold_usdc"], 14.0)
        self.assertAlmostEqual(pnl["fee_usdc"], 0.11)
        self.assertAlmostEqual(pnl["net_trading_vs_hold_usdc"], 13.89)
        self.assertAlmostEqual(pnl["net_portfolio_pnl_usdc"], 33.89)
        self.assertAlmostEqual(pnl["base_delta"], -0.6)
        self.assertAlmostEqual(pnl["quote_delta"], 80.0)
        self.assertTrue(pnl["fill_detected"])

    def test_get_baseline_prefers_run_start_history_over_restart_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            (run_dir / "dashboard-samples.jsonl").write_text(
                json.dumps(
                    {
                        "time": "2026-06-24T14:43:57Z",
                        "mid_price": 69.537,
                        "base_total": 5.361,
                        "quote_total": 442.9356,
                    }
                )
                + "\n"
            )
            (run_dir / "commands.log").write_text(
                """
[2026-06-27T06:26:04Z] $ /app/target/release/archer-market-maker status --config /app/config/adaptive-live.toml
=== Archer Market Maker Status ===
Market:       test
Maker:        test
Mode:         Hybrid
Mid ticks:    71930
Bid levels:   2
Ask levels:   2
Base free:    6.273000
Base locked:  0.410000
Quote free:   283.0792
Quote locked: 75.9055
[exit=0]
"""
            )

            state = DashboardState.__new__(DashboardState)
            state.run_dir = run_dir
            state.sample_path = run_dir / "dashboard-samples.jsonl"
            state.get_market_meta = lambda: {"tick_price_increment": 0.001}

            baseline = state.get_baseline()

        self.assertEqual(baseline["snapshot"], str(run_dir / "dashboard-samples.jsonl"))
        self.assertEqual(baseline["sample_time"], "2026-06-24T14:43:57Z")
        self.assertAlmostEqual(baseline["mid_price"], 69.537)
        self.assertAlmostEqual(baseline["base_total"], 5.361)
        self.assertAlmostEqual(baseline["quote_total"], 442.9356)

    def test_profile_approval_summary_uses_active_strategy_metadata(self) -> None:
        summary = profile_approval_summary(
            "overnight_selective_edge_probe",
            "profile_approval_denied: requested_profile=winner_scale_24_40 effective_profile=overnight_selective_edge_probe",
            {
                "profile_approval": {
                    "approved": False,
                    "profile": "winner_scale_24_40",
                    "source": "approval_required",
                }
            },
        )

        self.assertFalse(summary["approved"])
        self.assertEqual(summary["requested_profile"], "winner_scale_24_40")
        self.assertEqual(summary["effective_profile"], "overnight_selective_edge_probe")

    def test_profile_approval_summary_infers_legacy_operator_approval(self) -> None:
        summary = profile_approval_summary(
            "winner_scale_24_40",
            "Jerome approved normal-size continuous Archer run until manual stop",
            {},
        )

        self.assertTrue(summary["approved"])
        self.assertEqual(summary["source"], "legacy_reason_explicit_approval")
        self.assertEqual(summary["requested_profile"], "winner_scale_24_40")
        self.assertEqual(summary["effective_profile"], "winner_scale_24_40")

    def test_completed_controller_heartbeat_is_not_emergency_stop(self) -> None:
        status = classify_archer_live_status(
            {
                "process": {"bot_running": False},
                "controller_heartbeat": {
                    "healthy": False,
                    "event": "complete",
                    "time": iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=20)),
                },
                "dashboard_health": {"ok": True, "stale": False},
                "market": {"command_ok": True},
                "status": {"command_ok": True, "rpc_stale": False},
            }
        )

        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["action"], "idle")
        self.assertEqual(status["restart_policy"], "manual_only")


if __name__ == "__main__":
    unittest.main()
