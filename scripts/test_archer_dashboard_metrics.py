#!/usr/bin/env python3
"""Regression tests for Archer dashboard operational metadata."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock
import unittest

from dashboard.server import DashboardState


class ArcherDashboardMetricsTests(unittest.TestCase):
    def test_metrics_emit_profile_source_supervisor_and_wallet_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            keypair_path = tmp_path / "maker.json"
            keypair_path.write_text(str([1] * 64), encoding="utf-8")
            config_path = tmp_path / "archer.toml"
            config_path.write_text(
                f"""
[market]
maker_keypair_path = "{keypair_path}"

[connection]
rpc_url = "http://localhost:8899"

[strategy]
spread_levels_bps = [80.0]
min_effective_spread_bps = 80.0

[execution]
shadow_mode = true
""",
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "ARCHER_RUN_ID": "run-1",
                    "ARCHER_RUN_MODE": "shadow",
                    "ARCHER_EXPECTED_PROFILE": "overnight_balanced_low_churn",
                    "ARCHER_SOURCE_COMMIT": "deadbeef",
                    "ARCHER_SOURCE_CHECKSUM": "sha256:abc123",
                },
                clear=False,
            ):
                state = DashboardState(config_path, None, 30, 0, 0)
                state.get_market_meta = mock.Mock(
                    return_value={"command_ok": True, "tick_price_increment": 0.001}
                )
                state.get_status = mock.Mock(
                    return_value={
                        "command_ok": True,
                        "stale": False,
                        "mid_ticks": 80_000,
                        "base_free": 0.1,
                        "base_locked": 0.0,
                        "quote_free": 10.0,
                        "quote_locked": 0.0,
                    }
                )
                state.get_baseline = mock.Mock(return_value={})
                state.compute_pnl = mock.Mock(return_value={})
                state.get_wallet_balances = mock.Mock(
                    return_value={
                        "native_sol": 0.01,
                        "wsol": 0.1,
                        "usdc": 10.0,
                        "errors": [],
                    }
                )
                state.get_token_account_readiness = mock.Mock(
                    return_value={
                        "wsol": {"ready": True, "exists": True},
                        "usdc": {"ready": True, "exists": True},
                    }
                )
                state.get_process_state = mock.Mock(
                    return_value={"bot_running": True, "controller_running": False}
                )
                state.get_logs = mock.Mock(return_value={"counts": {}})
                state.get_market_intel = mock.Mock(return_value={"enabled": False, "ok": False})
                state.get_strategy_ledger = mock.Mock(return_value={})

                metrics = state.collect_metrics()

        self.assertEqual(metrics["strategy"]["active_profile"], "overnight_balanced_low_churn")
        self.assertEqual(metrics["source"]["commit"], "deadbeef")
        self.assertEqual(metrics["source"]["checksum"], "sha256:abc123")
        self.assertEqual(metrics["supervisor"]["expected_active"], True)
        self.assertEqual(metrics["supervisor"]["active"], True)
        self.assertEqual(metrics["supervisor"]["policy"]["ok"], True)
        self.assertEqual(metrics["wallet"]["keypair_path"], str(keypair_path))
        self.assertEqual(metrics["wallet"]["token_accounts"]["wsol"]["ready"], True)
        self.assertEqual(metrics["wallet"]["token_accounts"]["usdc"]["ready"], True)

    def test_supervisor_active_requires_observed_runner_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "archer.toml"
            config_path.write_text("[execution]\nshadow_mode = true\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"ARCHER_RUN_MODE": "shadow"}, clear=False):
                state = DashboardState(config_path, None, 30, 0, 0)

                supervisor = state.supervisor_state(
                    {"bot_running": False, "controller_running": False},
                    {"commit": "deadbeef", "checksum": "sha256:abc123"},
                )

        self.assertEqual(supervisor["expected_active"], True)
        self.assertEqual(supervisor["active"], False)
        self.assertEqual(supervisor["process_active"], False)


if __name__ == "__main__":
    unittest.main()
