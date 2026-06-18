#!/usr/bin/env python3
"""Capture Archer shadow metrics into retained JSONL artifacts.

This script is intentionally read-only: it loads dashboard metrics from an
existing JSON file or HTTP endpoint and writes normalized retained artifacts.
It does not start Archer services, call RPC directly, or submit transactions.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def source_metadata(source: str) -> Dict[str, str]:
    if source.startswith("http://") or source.startswith("https://"):
        return {"type": "url", "url": source}
    return {"type": "file", "path": source}


def load_metrics_source(source: str, timeout_seconds: float = 5.0) -> Dict[str, Any]:
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    return json.loads(Path(source).read_text(errors="replace"))


def _market(metrics: Dict[str, Any], explicit_market: Optional[str]) -> str:
    if explicit_market:
        return explicit_market
    market = metrics.get("market", {})
    return str(
        market.get("symbol")
        or market.get("name")
        or market.get("market")
        or market.get("market_pubkey")
        or "unknown"
    )


def _policy_version(metrics: Dict[str, Any], explicit_policy_version: Optional[str]) -> Optional[str]:
    if explicit_policy_version:
        return explicit_policy_version
    strategy = metrics.get("strategy", {})
    ledger = metrics.get("strategy_ledger", {})
    active = ledger.get("active", {}) if isinstance(ledger, dict) else {}
    return (
        strategy.get("policy_version")
        or active.get("policy_version")
        or active.get("version")
    )


def _reason_codes(metrics: Dict[str, Any], clean_book_actions: Any) -> list[str]:
    codes = ["metrics_capture"]
    status = metrics.get("status", {})
    market = metrics.get("market", {})
    tx = metrics.get("transactions", {})
    market_intel = metrics.get("market_intel", {})
    pnl = metrics.get("pnl", {})
    kind_counts = tx.get("kind_counts", {}) if isinstance(tx, dict) else {}

    if not market.get("command_ok", True):
        codes.append("market_read_unhealthy")
    if not status.get("command_ok", True):
        codes.append("makerbook_read_unhealthy")
    if status.get("stale"):
        codes.append("makerbook_stale")
    if market_intel and not market_intel.get("ok", True):
        codes.append("market_intel_unhealthy")
    if int(tx.get("failed_count") or 0) > 0:
        codes.append("tx_failures")
    if pnl.get("fill_detected"):
        codes.append("fill_detected")
    if clean_book_actions or int(kind_counts.get("clear") or kind_counts.get("clear_book") or 0) > 0:
        codes.append("clean_book_action")

    return codes


def _first_dict(*values: Any) -> Dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def build_capture_event(
    metrics: Dict[str, Any],
    *,
    run_id: Optional[str] = None,
    source: str = "unknown",
    market: Optional[str] = None,
    policy_version: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize one dashboard metrics snapshot for retained JSONL storage."""
    run = metrics.get("run", {}) if isinstance(metrics.get("run"), dict) else {}
    status = metrics.get("status", {}) if isinstance(metrics.get("status"), dict) else {}
    transactions = (
        metrics.get("transactions", {})
        if isinstance(metrics.get("transactions"), dict)
        else {}
    )
    pnl = metrics.get("pnl", {}) if isinstance(metrics.get("pnl"), dict) else {}
    strategy = metrics.get("strategy", {}) if isinstance(metrics.get("strategy"), dict) else {}
    clean_book_actions = metrics.get("clean_book_actions") or []
    event_policy_version = _policy_version(metrics, policy_version)
    market_intel = metrics.get("market_intel", {}) if isinstance(metrics.get("market_intel"), dict) else {}

    tx_summary = {
        "ok": transactions.get("ok"),
        "since_start_count": transactions.get("since_start_count", 0),
        "failed_count": transactions.get("failed_count", 0),
        "fees_complete": transactions.get("fees_complete"),
        "fee_sol_total": transactions.get("fee_sol_total"),
        "priority_fee_sol_estimate": transactions.get("priority_fee_sol_estimate"),
        "kind_counts": transactions.get("kind_counts", {}),
        "last": transactions.get("last", []),
    }
    fill_summary = {
        "fill_detected": pnl.get("fill_detected", False),
        "base_delta": pnl.get("base_delta"),
        "quote_delta": pnl.get("quote_delta"),
        "trading_vs_hold_usdc": pnl.get("trading_vs_hold_usdc"),
        "net_trading_vs_hold_usdc": pnl.get("net_trading_vs_hold_usdc"),
        "fee_usdc": pnl.get("fee_usdc"),
        "fee_sol": pnl.get("fee_sol"),
    }
    makerbook_status = {
        "source": status.get("source"),
        "command_ok": status.get("command_ok"),
        "stale": status.get("stale"),
        "bid_levels": status.get("bid_levels"),
        "ask_levels": status.get("ask_levels"),
        "base_total": status.get("base_total"),
        "quote_total": status.get("quote_total"),
        "base_free": status.get("base_free"),
        "quote_free": status.get("quote_free"),
        "mid_ticks": status.get("mid_ticks"),
    }

    return {
        "event_type": "archer_shadow_metrics",
        "run_id": run_id or run.get("run_id") or "unknown",
        "timestamp": timestamp or metrics.get("time") or iso_now(),
        "mode": "shadow",
        "venue": "archer",
        "market": _market(metrics, market),
        "source": source_metadata(source),
        "policy_version": event_policy_version,
        "reason_codes": _reason_codes(metrics, clean_book_actions),
        "config_checksum": metrics.get("config_checksum"),
        "static_config": _first_dict(metrics.get("static_config"), strategy.get("static_config")),
        "signal_multipliers": _first_dict(
            metrics.get("signal_multipliers"),
            strategy.get("signal_multipliers"),
        ),
        "quote_policy_control": _first_dict(
            metrics.get("quote_policy_control"),
            strategy.get("quote_policy_control"),
        ),
        "route_quality": _first_dict(metrics.get("route_quality"), market_intel.get("route_quality")),
        "quote_decision": _first_dict(metrics.get("quote_decision"), strategy.get("quote_decision")),
        "no_fill_exposure": _first_dict(
            metrics.get("no_fill_exposure"),
            strategy.get("no_fill_exposure"),
        ),
        "expected_fill": _first_dict(metrics.get("expected_fill"), strategy.get("expected_fill")),
        "expected_edge": _first_dict(metrics.get("expected_edge"), strategy.get("expected_edge")),
        "portfolio_exposure": _first_dict(metrics.get("portfolio_exposure")),
        "promotion_inputs": _first_dict(metrics.get("promotion_inputs"), strategy.get("promotion_inputs")),
        "dashboard_metrics": metrics,
        "market_intel_snapshot": market_intel,
        "makerbook_readback": makerbook_status,
        "makerbook_status": makerbook_status,
        "tx_summary": tx_summary,
        "fill_summary": fill_summary,
        "clean_book_actions": clean_book_actions,
        "simulated_makerbook_update": (
            metrics.get("simulated_makerbook_update")
            or strategy.get("simulated_makerbook_update")
        ),
    }


def write_capture_event(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def capture_once(
    source: str,
    output_path: Path,
    *,
    run_id: Optional[str] = None,
    market: Optional[str] = None,
    policy_version: Optional[str] = None,
    timeout_seconds: float = 5.0,
) -> Dict[str, Any]:
    metrics = load_metrics_source(source, timeout_seconds=timeout_seconds)
    event = build_capture_event(
        metrics,
        run_id=run_id,
        source=source,
        market=market,
        policy_version=policy_version,
    )
    write_capture_event(output_path, event)
    return event


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--metrics-file", help="dashboard metrics JSON fixture/path")
    source.add_argument("--metrics-url", help="dashboard /api/metrics URL")
    parser.add_argument("--output", required=True, type=Path, help="retained JSONL output path")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--market", default=None)
    parser.add_argument("--policy-version", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    args = parser.parse_args()

    event = capture_once(
        args.metrics_file or args.metrics_url,
        args.output,
        run_id=args.run_id,
        market=args.market,
        policy_version=args.policy_version,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps({"captured": True, "output": str(args.output), "run_id": event["run_id"]}))


if __name__ == "__main__":
    main()
