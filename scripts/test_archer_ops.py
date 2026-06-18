#!/usr/bin/env python3
"""Tests for operator-safe Archer ops command planning."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = ROOT / "scripts" / "archer_ops.py"


def load_ops_module():
    assert OPS_SCRIPT.exists(), "scripts/archer_ops.py is missing"
    spec = importlib.util.spec_from_file_location("archer_ops", OPS_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArcherOpsCommandTests(unittest.TestCase):
    def test_shadow_start_plan_is_dry_run_and_never_sets_live_gate(self) -> None:
        ops = load_ops_module()

        plan = ops.build_plan(
            "shadow-start",
            run_id="test-shadow",
            env={},
            execute=False,
            confirm_live_canary=False,
        )

        self.assertEqual(plan["mode"], "shadow")
        self.assertFalse(plan["executes_live_transactions"])
        self.assertIn("--profile shadow", " ".join(plan["commands"][1]))
        self.assertNotIn("ARCHER_ENABLE_LIVE_TRADING=true", str(plan))

    def test_shadow_start_splits_static_and_post_start_preflight(self) -> None:
        ops = load_ops_module()

        plan = ops.build_plan(
            "shadow-start",
            run_id="test-shadow",
            env={},
            execute=False,
            confirm_live_canary=False,
        )

        flattened = [" ".join(command) for command in plan["commands"]]
        self.assertIn("--static-only", flattened[0])
        self.assertIn("docker compose", flattened[1])
        self.assertIn("--post-start", flattened[2])
        self.assertTrue(plan["dry_run"])

    def test_canary_start_requires_live_gate_profile_and_approval(self) -> None:
        ops = load_ops_module()

        with self.assertRaisesRegex(ValueError, "ARCHER_ENABLE_LIVE_TRADING=true"):
            ops.build_plan(
                "canary-start",
                run_id="test-canary",
                env={},
                execute=False,
                confirm_live_canary=True,
            )

    def test_canary_start_splits_static_and_post_start_preflight(self) -> None:
        ops = load_ops_module()

        plan = ops.build_plan(
            "canary-start",
            run_id="test-canary",
            env={
                "ARCHER_ENABLE_LIVE_TRADING": "true",
                "ARCHER_CANARY_PROFILE": "first-live-sol-usdc",
                "ARCHER_CANARY_APPROVAL_ID": "approval-123",
            },
            execute=False,
            confirm_live_canary=True,
        )

        flattened = [" ".join(command) for command in plan["commands"]]
        self.assertIn("--static-only", flattened[0])
        self.assertIn("--require-canary-envelope", flattened[0])
        self.assertIn("docker compose", flattened[1])
        self.assertIn("--post-start", flattened[2])
        self.assertIn("--require-canary-envelope", flattened[2])
        self.assertTrue(plan["dry_run"])

        with self.assertRaisesRegex(ValueError, "ARCHER_CANARY_APPROVAL_ID"):
            ops.build_plan(
                "canary-start",
                run_id="test-canary",
                env={
                    "ARCHER_ENABLE_LIVE_TRADING": "true",
                    "ARCHER_CANARY_PROFILE": "first-live-sol-usdc",
                },
                execute=False,
                confirm_live_canary=True,
            )

    def test_rollback_plan_stops_services_and_verifies_clean_book(self) -> None:
        ops = load_ops_module()

        plan = ops.build_plan(
            "rollback-stopped",
            run_id="test-rollback",
            env={},
            execute=False,
            confirm_live_canary=False,
        )

        flattened = [" ".join(command) for command in plan["commands"]]
        self.assertTrue(any("down" in command for command in flattened))
        self.assertTrue(any("emergency-clear" in command for command in flattened))
        self.assertTrue(any("clean-book" in command for command in flattened))
        self.assertFalse(plan["executes_live_transactions"])


if __name__ == "__main__":
    unittest.main()
