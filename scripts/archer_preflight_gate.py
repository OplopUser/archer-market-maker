#!/usr/bin/env python3
"""Validate Archer is safe to start before spending live transaction fees."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def as_float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_metrics_once(args: argparse.Namespace) -> dict[str, Any]:
    if args.metrics_file:
        return json.loads(Path(args.metrics_file).read_text(errors="replace"))

    with urllib.request.urlopen(args.metrics_url, timeout=args.timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def load_metrics(args: argparse.Namespace) -> dict[str, Any]:
    """Load a metrics snapshot, waiting for HTTP availability when needed."""

    if args.metrics_file:
        return load_metrics_once(args)

    deadline = time.monotonic() + args.wait_seconds
    while True:
        try:
            return load_metrics_once(args)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError) as exc:
            if time.monotonic() >= deadline:
                raise SystemExit(f"PRE-FLIGHT FAIL\n  metrics unavailable: {exc}") from exc
            time.sleep(args.retry_interval_seconds)


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        items = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        return [parse_scalar(item) for item in items]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def load_simple_toml(path: Path) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    section = ""
    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        key, sep, value = line.partition("=")
        if sep != "=":
            continue
        data.setdefault(section, {})[key.strip()] = parse_scalar(value)
    return data


def validate_local_artifacts(metrics: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []

    expected_mode = getattr(args, "expected_mode", "")
    if expected_mode:
        run = metrics.get("run", {})
        actual_mode = run.get("mode") or metrics.get("mode")
        if actual_mode != expected_mode:
            failures.append(f"run mode {actual_mode} != expected {expected_mode}")

    config_file = getattr(args, "config_file", "")
    if config_file:
        config_path = Path(config_file).expanduser()
        if not config_path.exists():
            failures.append(f"config file is missing: {config_path}")
        else:
            max_age = float(getattr(args, "config_max_age_seconds", 0.0) or 0.0)
            if max_age > 0.0:
                age = time.time() - config_path.stat().st_mtime
                if age > max_age:
                    failures.append(f"config is stale: age_seconds={age:.0f} > {max_age:.0f}")
            try:
                config = load_simple_toml(config_path)
                shadow_mode = config.get("execution", {}).get("shadow_mode")
                if expected_mode == "shadow" and shadow_mode is not True:
                    failures.append("shadow mode requires execution.shadow_mode = true")
                if expected_mode == "canary" and shadow_mode is True:
                    failures.append("canary mode requires execution.shadow_mode = false")
            except OSError as exc:
                failures.append(f"config could not be read: {exc}")

    rollback_command = getattr(args, "rollback_command", "")
    needs_rollback = expected_mode == "canary" or bool(getattr(args, "require_canary_envelope", False))
    if needs_rollback:
        if not rollback_command:
            failures.append("rollback command is required before Archer canary start")
        else:
            rollback_path = Path(str(rollback_command).split()[0]).expanduser()
            if not rollback_path.exists():
                failures.append(f"rollback command is missing: {rollback_path}")
            elif not os.access(rollback_path, os.X_OK):
                failures.append(f"rollback command is not executable: {rollback_path}")

    envelope_file = getattr(args, "canary_envelope_file", "")
    require_envelope = bool(getattr(args, "require_canary_envelope", False)) or expected_mode == "canary"
    if require_envelope:
        if not envelope_file:
            failures.append("canary envelope is required before Archer canary start")
        else:
            envelope_path = Path(envelope_file).expanduser()
            if not envelope_path.exists():
                failures.append(f"canary envelope is missing: {envelope_path}")
            else:
                failures.extend(validate_canary_envelope(envelope_path, metrics, rollback_command))

    return failures


def validate_canary_envelope(
    envelope_path: Path, metrics: dict[str, Any], rollback_command: str
) -> list[str]:
    failures: list[str] = []
    try:
        envelope = load_simple_toml(envelope_path)
    except OSError as exc:
        return [f"canary envelope could not be read: {exc}"]

    canary = envelope.get("canary_envelope", {})
    if canary.get("approved_profile") != "first-live-sol-usdc":
        failures.append(f"canary envelope profile is not approved: {canary.get('approved_profile')}")
    if canary.get("market") != "SOL/USDC":
        failures.append(f"canary envelope market is not SOL/USDC: {canary.get('market')}")
    if as_float(canary.get("max_levels_per_side"), 0.0) > 1.0:
        failures.append("canary envelope allows more than one level per side")
    if as_float(canary.get("max_total_quote_notional"), math.inf) > 25.0:
        failures.append("canary envelope max_total_quote_notional is above tiny canary cap")
    if as_float(canary.get("max_runtime_minutes"), math.inf) > 60.0:
        failures.append("canary envelope runtime exceeds one-hour first-live cap")
    if as_float(canary.get("max_tx_per_minute"), math.inf) > 2.0:
        failures.append("canary envelope tx budget exceeds first-live cap")

    coexistence = envelope.get("manifest_coexistence", {})
    if coexistence.get("same_market_rule") not in {
        "manifest_same_market_must_be_stopped",
        "manifest_same_market_reduce_only_no_asks",
    }:
        failures.append("Manifest same-market coexistence rule is unresolved")

    rollback = envelope.get("rollback", {})
    envelope_rollback = str(rollback.get("command", ""))
    if rollback_command and envelope_rollback and rollback_command not in envelope_rollback:
        failures.append("rollback command does not match canary envelope")

    status = metrics.get("status", {})
    base_total = as_float(status.get("base_free"), 0.0) + as_float(status.get("base_locked"), 0.0)
    quote_total = as_float(status.get("quote_free"), 0.0) + as_float(
        status.get("quote_locked"), 0.0
    )
    max_wallet_base = as_float(canary.get("max_wallet_base"), math.inf)
    max_wallet_quote = as_float(canary.get("max_wallet_quote"), math.inf)
    if base_total > max_wallet_base:
        failures.append(f"wallet base {base_total:.6f} exceeds canary envelope {max_wallet_base:.6f}")
    if quote_total > max_wallet_quote:
        failures.append(f"wallet quote {quote_total:.4f} exceeds canary envelope {max_wallet_quote:.4f}")

    return failures


def wait_for_valid_metrics(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Wait until metrics are loaded and pass validation.

    A freshly recreated dashboard can briefly return a valid JSON envelope before
    its market/status/strategy probes have populated. Treat that as not ready
    during the normal wait window so the start gate fails only on the final
    stable state.
    """

    if args.metrics_file:
        metrics = load_metrics(args)
        return metrics, validate_metrics(metrics, args)

    deadline = time.monotonic() + args.wait_seconds
    last_failures: list[str] = []
    while True:
        metrics = load_metrics(args)
        failures = validate_metrics(metrics, args)
        if not failures:
            return metrics, failures
        last_failures = failures
        if time.monotonic() >= deadline:
            return metrics, last_failures
        time.sleep(args.retry_interval_seconds)


def validate_metrics(metrics: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    failures.extend(validate_local_artifacts(metrics, args))

    run = metrics.get("run", {})
    if args.expected_run_id and run.get("run_id") != args.expected_run_id:
        failures.append(f"dashboard run_id {run.get('run_id')} != expected {args.expected_run_id}")

    market = metrics.get("market", {})
    if not market.get("command_ok"):
        failures.append(f"market command failed: {market.get('command_error')}")
    if args.require_owner_match and market.get("owner_matches_archer") is not True:
        failures.append("market owner does not match expected Archer program")

    status = metrics.get("status", {})
    if not status.get("command_ok"):
        failures.append(f"status command failed: {status.get('command_error')}")
    if status.get("stale"):
        failures.append("status metrics are stale")

    process = metrics.get("process", {})
    if args.require_no_bot and process.get("bot_running"):
        failures.append("Archer bot process is already running")
    if args.require_no_controller and process.get("controller_running"):
        failures.append("adaptive controller process is already running")

    bid_levels = as_float(status.get("bid_levels"), 0.0)
    ask_levels = as_float(status.get("ask_levels"), 0.0)
    base_locked = as_float(status.get("base_locked"), 0.0)
    quote_locked = as_float(status.get("quote_locked"), 0.0)
    if args.require_clear_book and (bid_levels != 0.0 or ask_levels != 0.0):
        failures.append(f"book is not clear: bids={bid_levels:.0f}, asks={ask_levels:.0f}")
    if base_locked > args.max_base_locked:
        failures.append(f"base locked {base_locked:.6f} > {args.max_base_locked:.6f}")
    if quote_locked > args.max_quote_locked:
        failures.append(f"quote locked {quote_locked:.4f} > {args.max_quote_locked:.4f}")

    base_free = as_float(status.get("base_free"), 0.0)
    quote_free = as_float(status.get("quote_free"), 0.0)
    mid_price = as_float(metrics.get("mid_price"), 0.0)
    base_notional = base_free * mid_price if mid_price > 0.0 else math.nan
    if base_free < args.min_base_free:
        failures.append(f"base free {base_free:.6f} < {args.min_base_free:.6f}")
    if quote_free < args.min_quote_free:
        failures.append(f"quote free {quote_free:.4f} < {args.min_quote_free:.4f}")
    if math.isfinite(base_notional) and base_notional < args.min_base_notional:
        failures.append(f"base notional {base_notional:.2f} < {args.min_base_notional:.2f}")

    strategy = metrics.get("strategy", {})
    if args.expected_profile and strategy.get("active_profile") != args.expected_profile:
        failures.append(
            f"active profile {strategy.get('active_profile')} != expected {args.expected_profile}"
        )
    min_effective = as_float(strategy.get("min_effective_spread_bps"))
    if not math.isfinite(min_effective) or min_effective < args.min_effective_spread_bps:
        failures.append(
            f"min effective spread {min_effective:.2f}bps < {args.min_effective_spread_bps:.2f}bps"
        )
    effective_spreads = strategy.get("effective_spreads_bps") or []
    if effective_spreads:
        tightest = min(as_float(value) for value in effective_spreads)
        if tightest < args.min_effective_spread_bps:
            failures.append(
                f"tightest effective spread {tightest:.2f}bps < {args.min_effective_spread_bps:.2f}bps"
            )

    market_intel = metrics.get("market_intel", {})
    if args.require_market_intel:
        if not market_intel.get("enabled"):
            failures.append("market-intel is disabled")
        if not market_intel.get("ok"):
            failures.append("market-intel is not ok")
        if not market_intel.get("quote_enabled"):
            failures.append("market-intel has disabled quoting")
        if "market-intel" not in str(market_intel.get("url", "")):
            failures.append(f"market-intel URL is not shared service: {market_intel.get('url')}")
        fair_value = as_float(market_intel.get("fair_value"))
        if not math.isfinite(fair_value) or fair_value <= 0.0:
            failures.append(f"market-intel fair value is invalid: {market_intel.get('fair_value')}")
        spread_add = as_float(market_intel.get("spread_add_bps"), 0.0)
        if spread_add < args.min_market_intel_spread_add_bps:
            failures.append(
                f"market-intel spread_add {spread_add:.2f}bps < {args.min_market_intel_spread_add_bps:.2f}bps"
            )

    logs = metrics.get("logs", {}).get("counts", {})
    limits = {
        "price_feed_stale": args.max_price_feed_stale,
        "priority_fee_sampling_failed": args.max_priority_fee_sampling_failures,
        "rpc_429": args.max_rpc_429,
        "tx_circuit_breaker": args.max_tx_circuit_breaker,
        "tx_send_failed": args.max_tx_send_failed,
    }
    for key, limit in limits.items():
        value = as_float(logs.get(key), 0.0)
        if value > limit:
            failures.append(f"{key} count {value:.0f} > {limit:.0f}")

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-url", default="http://127.0.0.1:8787/api/metrics")
    parser.add_argument("--metrics-file", default="")
    parser.add_argument("--wait-seconds", type=float, default=20.0)
    parser.add_argument("--retry-interval-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--expected-run-id", default="")
    parser.add_argument("--expected-mode", default="")
    parser.add_argument("--config-file", default="")
    parser.add_argument("--config-max-age-seconds", type=float, default=0.0)
    parser.add_argument("--canary-envelope-file", default="")
    parser.add_argument("--rollback-command", default="")
    parser.add_argument("--expected-profile", default="overnight_balanced_low_churn")
    parser.add_argument("--min-effective-spread-bps", type=float, default=62.0)
    parser.add_argument("--min-market-intel-spread-add-bps", type=float, default=0.0)
    parser.add_argument("--min-base-free", type=float, default=0.25)
    parser.add_argument("--min-quote-free", type=float, default=25.0)
    parser.add_argument("--min-base-notional", type=float, default=25.0)
    parser.add_argument("--max-base-locked", type=float, default=0.000001)
    parser.add_argument("--max-quote-locked", type=float, default=0.0001)
    parser.add_argument("--max-price-feed-stale", type=float, default=0.0)
    parser.add_argument("--max-priority-fee-sampling-failures", type=float, default=0.0)
    parser.add_argument("--max-rpc-429", type=float, default=0.0)
    parser.add_argument("--max-tx-circuit-breaker", type=float, default=0.0)
    parser.add_argument("--max-tx-send-failed", type=float, default=0.0)
    parser.add_argument("--allow-running-bot", action="store_true")
    parser.add_argument("--allow-running-controller", action="store_true")
    parser.add_argument("--allow-live-book", action="store_true")
    parser.add_argument("--allow-market-owner-mismatch", action="store_true")
    parser.add_argument("--allow-missing-market-intel", action="store_true")
    parser.add_argument("--require-canary-envelope", action="store_true")
    args = parser.parse_args()
    args.require_no_bot = not args.allow_running_bot
    args.require_no_controller = not args.allow_running_controller
    args.require_clear_book = not args.allow_live_book
    args.require_owner_match = not args.allow_market_owner_mismatch
    args.require_market_intel = not args.allow_missing_market_intel

    metrics, failures = wait_for_valid_metrics(args)
    if failures:
        print("PRE-FLIGHT FAIL")
        for failure in failures:
            print(f"  {failure}")
        raise SystemExit(1)

    status = metrics.get("status", {})
    strategy = metrics.get("strategy", {})
    market_intel = metrics.get("market_intel", {})
    print("PRE-FLIGHT PASS")
    print(
        "  "
        f"profile={strategy.get('active_profile')} "
        f"effective_spreads={strategy.get('effective_spreads_bps')} "
        f"base_free={status.get('base_free')} quote_free={status.get('quote_free')} "
        f"market_intel_mode={market_intel.get('mode')} "
        f"spread_add_bps={market_intel.get('spread_add_bps')}"
    )


if __name__ == "__main__":
    main()
