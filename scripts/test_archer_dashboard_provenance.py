#!/usr/bin/env python3
"""Regression tests for Archer dashboard provenance/readiness helpers."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from dashboard.server import config_checksum, build_readiness


class ArcherDashboardProvenanceTests(unittest.TestCase):
    def test_config_checksum_uses_sha256_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "archer.toml"
            path.write_text("[execution]\nshadow_mode = true\n")

            self.assertTrue(config_checksum(path).startswith("sha256:"))

    def test_readiness_fails_closed_without_certification_and_routing_artifacts(self) -> None:
        readiness = build_readiness(
            config={"execution": {"shadow_mode": True}},
            market={"market": "SOL/USDC"},
            market_intel={"ok": True},
            certification={"passed": False},
            routing_proof={"passed": False},
        )

        self.assertEqual(readiness["mode"], "shadow")
        self.assertEqual(readiness["status"], "blocked")
        self.assertFalse(readiness["certification_passed"])
        self.assertFalse(readiness["routing_proof_passed"])


if __name__ == "__main__":
    unittest.main()
