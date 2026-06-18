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
from typing import Any, Optional


def as_float(value: Any, default: float = math.nan) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_metrics_once(args: argparse.Namespace) -> dict[str, Any]:
    if args.metrics_file:
        metrics_path = Path(args.metrics_file)
        try:
            metrics = json.loads(metrics_path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(
                f"PRE-FLIGHT FAIL\n  metrics file invalid: {metrics_path}: {exc}"
            ) from exc
        if not isinstance(metrics, dict):
            raise SystemExit(
                f"PRE-FLIGHT FAIL\n  metrics file invalid: {metrics_path}: root must be a JSON object"
            )
        return metrics

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


def expected_mode_from(args: argparse.Namespace) -> str:
    return (
        str(getattr(args, "expected_mode", "") or "")
        or os.environ.get("ARCHER_EXPECTED_MODE", "")
        or os.environ.get("ARCHER_RUN_MODE", "")
    )


def actual_mode_from(metrics: dict[str, Any]) -> Any:
    run = metrics.get("run", {})
    return run.get("mode") or metrics.get("mode") or os.environ.get("ARCHER_RUN_MODE")


def source_expectations(args: argparse.Namespace) -> tuple[str, str]:
    expected_checksum = getattr(args, "expected_source_checksum", "") or os.environ.get(
        "ARCHER_EXPECTED_SOURCE_CHECKSUM", ""
    )
    expected_commit = getattr(args, "expected_source_commit", "") or os.environ.get(
        "ARCHER_EXPECTED_SOURCE_COMMIT", ""
    )
    return expected_commit, expected_checksum


def source_required_for(args: argparse.Namespace) -> bool:
    if bool(getattr(args, "allow_missing_source", False)):
        return False
    expected_mode = expected_mode_from(args)
    return expected_mode in {"shadow", "canary"} and (
        bool(getattr(args, "static_only", False)) or bool(getattr(args, "post_start", False))
    )


def normalize_path_text(path: Any) -> str:
    if path is None:
        return ""
    return str(Path(str(path)).expanduser())


def validate_local_artifacts(metrics: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    config: dict[str, dict[str, Any]] = {}

    expected_mode = expected_mode_from(args)
    if bool(getattr(args, "static_only", False)) and source_required_for(args):
        expected_commit, expected_checksum = source_expectations(args)
        if not expected_commit or not expected_checksum:
            failures.append(
                "source commit/checksum inputs are required before Archer "
                f"{expected_mode} start"
            )

    if expected_mode and not bool(getattr(args, "static_only", False)):
        actual_mode = actual_mode_from(metrics)
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
                failures.extend(
                    validate_canary_envelope(
                        envelope_path,
                        metrics,
                        rollback_command,
                        config,
                        include_metrics=not bool(getattr(args, "static_only", False)),
                        require_wallet_readiness_fields=expected_mode == "canary"
                        and bool(getattr(args, "post_start", False)),
                    )
                )

    approval_artifact = getattr(args, "canary_approval_artifact_file", "") or os.environ.get(
        "ARCHER_CANARY_APPROVAL_ARTIFACT", ""
    )
    if expected_mode == "canary":
        if not approval_artifact:
            failures.append("canary approval artifact is required before Archer live placement")
        else:
            approval_path = Path(approval_artifact).expanduser()
            if not approval_path.exists():
                failures.append(f"canary approval artifact is missing: {approval_path}")

    return failures


def validate_canary_envelope(
    envelope_path: Path,
    metrics: dict[str, Any],
    rollback_command: str,
    config: Optional[dict[str, dict[str, Any]]] = None,
    include_metrics: bool = True,
    require_wallet_readiness_fields: bool = False,
) -> list[str]:
    failures: list[str] = []
    config = config or {}
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

    failures.extend(validate_config_against_canary_envelope(config, canary))

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

    if include_metrics:
        status = metrics.get("status", {})
        base_total = as_float(status.get("base_free"), 0.0) + as_float(
            status.get("base_locked"), 0.0
        )
        quote_total = as_float(status.get("quote_free"), 0.0) + as_float(
            status.get("quote_locked"), 0.0
        )
        max_wallet_base = as_float(canary.get("max_wallet_base"), math.inf)
        max_wallet_quote = as_float(canary.get("max_wallet_quote"), math.inf)
        if base_total > max_wallet_base:
            failures.append(
                f"wallet base {base_total:.6f} exceeds canary envelope {max_wallet_base:.6f}"
            )
        if quote_total > max_wallet_quote:
            failures.append(
                f"wallet quote {quote_total:.4f} exceeds canary envelope {max_wallet_quote:.4f}"
            )

        failures.extend(validate_wallet_limits(envelope, metrics))
        failures.extend(
            validate_wallet_readiness(
                metrics,
                config,
                require_fields=require_wallet_readiness_fields,
            )
        )

    return failures


def validate_config_against_canary_envelope(
    config: dict[str, dict[str, Any]], canary: dict[str, Any]
) -> list[str]:
    if not config:
        return []
    failures: list[str] = []
    market = config.get("market", {})
    strategy = config.get("strategy", {})
    risk = config.get("risk", {})
    execution = config.get("execution", {})

    expected_market = str(canary.get("market_pubkey", ""))
    if expected_market and str(market.get("market_pubkey", "")) != expected_market:
        failures.append(
            f"config market_pubkey {market.get('market_pubkey')} != canary envelope {expected_market}"
        )

    levels = strategy.get("spread_levels_bps") or []
    max_levels = as_float(canary.get("max_levels_per_side"), math.inf)
    if isinstance(levels, list) and len(levels) > max_levels:
        failures.append(f"config level count {len(levels)} exceeds canary envelope {max_levels:.0f}")

    comparisons = [
        (
            "risk.max_quote_notional_per_level",
            risk.get("max_quote_notional_per_level"),
            canary.get("max_quote_notional_per_level"),
            "<=",
        ),
        (
            "risk.max_total_quote_notional",
            risk.get("max_total_quote_notional"),
            canary.get("max_total_quote_notional"),
            "<=",
        ),
        (
            "risk.min_base_reserve_pct",
            risk.get("min_base_reserve_pct"),
            canary.get("min_base_reserve_pct"),
            ">=",
        ),
        (
            "risk.min_quote_reserve_pct",
            risk.get("min_quote_reserve_pct"),
            canary.get("min_quote_reserve_pct"),
            ">=",
        ),
        (
            "execution.max_tx_per_minute",
            execution.get("max_tx_per_minute"),
            canary.get("max_tx_per_minute"),
            "<=",
        ),
        (
            "execution.max_update_tx_per_10min",
            execution.get("max_update_tx_per_10min"),
            canary.get("max_update_tx_per_10min"),
            "<=",
        ),
    ]
    for label, raw_value, raw_limit, op in comparisons:
        if raw_value is None or raw_limit is None:
            continue
        value = as_float(raw_value)
        limit = as_float(raw_limit)
        if op == "<=" and value > limit:
            failures.append(f"config {label} {value:g} exceeds canary envelope {limit:g}")
        if op == ">=" and value < limit:
            failures.append(f"config {label} {value:g} below canary envelope {limit:g}")

    return failures


def validate_wallet_limits(envelope: dict[str, dict[str, Any]], metrics: dict[str, Any]) -> list[str]:
    wallet_limits = envelope.get("wallet_limits", {})
    if not wallet_limits:
        return []
    failures: list[str] = []
    wallet = metrics.get("wallet", {})
    balances = wallet.get("balances") if isinstance(wallet, dict) else None
    if not isinstance(balances, dict):
        failures.append("wallet balances unavailable for canary envelope")
        return failures

    errors = balances.get("errors") or []
    if errors:
        failures.append(f"wallet balance errors: {errors}")

    checks = [
        ("native SOL", balances.get("native_sol"), wallet_limits.get("max_native_sol_fee_reserve")),
        ("wsol", balances.get("wsol"), wallet_limits.get("max_wsol")),
        ("usdc", balances.get("usdc"), wallet_limits.get("max_usdc")),
    ]
    for label, raw_value, raw_limit in checks:
        if raw_limit is None:
            continue
        value = as_float(raw_value)
        limit = as_float(raw_limit)
        if not math.isfinite(value):
            failures.append(f"wallet {label} balance unavailable")
        elif value > limit:
            failures.append(f"wallet {label} {value:g} exceeds canary envelope {limit:g}")

    return failures


def validate_wallet_readiness(
    metrics: dict[str, Any],
    config: Optional[dict[str, dict[str, Any]]] = None,
    require_fields: bool = False,
) -> list[str]:
    wallet = metrics.get("wallet", {})
    if not isinstance(wallet, dict):
        if require_fields:
            return ["wallet readiness metrics are missing"]
        return []
    failures: list[str] = []
    config = config or {}

    expected_keypair = config.get("market", {}).get("maker_keypair_path")
    actual_keypair = wallet.get("keypair_path") or wallet.get("maker_keypair_path")
    if expected_keypair and not actual_keypair and require_fields:
        failures.append("wallet keypair readiness is missing")
    elif expected_keypair and actual_keypair:
        if normalize_path_text(actual_keypair) != normalize_path_text(expected_keypair):
            failures.append(
                f"wallet keypair {actual_keypair} != config maker_keypair_path {expected_keypair}"
            )

    token_accounts = wallet.get("token_accounts") or wallet.get("token_account_readiness") or {}
    if require_fields and not isinstance(token_accounts, dict) or (
        require_fields and isinstance(token_accounts, dict) and not token_accounts
    ):
        failures.append("token account readiness is missing")
        return failures
    if isinstance(token_accounts, dict):
        if require_fields:
            for required_label in ("wsol", "usdc"):
                if required_label not in token_accounts:
                    failures.append(f"token account readiness is missing for {required_label}")
        for label, state in token_accounts.items():
            if isinstance(state, bool):
                ready = state
                error = ""
            elif isinstance(state, dict):
                ready = state.get("ready")
                if ready is None and "exists" in state:
                    ready = state.get("exists")
                error = str(state.get("error") or "")
            else:
                continue
            if ready is False:
                suffix = f": {error}" if error else ""
                failures.append(f"token account {label} is not ready{suffix}")

    return failures


def wait_for_valid_metrics(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """Wait until metrics are loaded and pass validation.

    A freshly recreated dashboard can briefly return a valid JSON envelope before
    its market/status/strategy probes have populated. Treat that as not ready
    during the normal wait window so the start gate fails only on the final
    stable state.
    """

    if getattr(args, "static_only", False):
        return {}, validate_metrics({}, args)

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
    if bool(getattr(args, "static_only", False)):
        return failures

    config: dict[str, dict[str, Any]] = {}
    config_file = getattr(args, "config_file", "")
    if config_file and Path(config_file).expanduser().exists():
        try:
            config = load_simple_toml(Path(config_file).expanduser())
        except OSError:
            config = {}

    failures.extend(validate_source(metrics, args))
    failures.extend(
        validate_wallet_readiness(
            metrics,
            config,
            require_fields=expected_mode_from(args) == "canary"
            and bool(getattr(args, "post_start", False)),
        )
    )
    failures.extend(validate_supervisor(metrics, args))

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
    expected_mode = expected_mode_from(args)
    post_start = bool(getattr(args, "post_start", False))
    expected_bot_runner = post_start and expected_mode in {"shadow", "canary"}
    expected_controller_runner = post_start and expected_mode == "controller"
    if args.require_no_bot and process.get("bot_running") and not expected_bot_runner:
        failures.append("Archer bot process is already running")
    if args.require_no_controller and process.get("controller_running") and not expected_controller_runner:
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
        source_count = market_intel_source_count(market_intel)
        min_source_count = int(getattr(args, "min_market_intel_source_count", 2))
        if source_count < min_source_count:
            failures.append(
                f"market-intel source_count {source_count} < {min_source_count}"
            )
        route_quality = market_intel_route_quality(market_intel)
        min_route_quality = as_float(getattr(args, "min_market_intel_route_quality", 0.5), 0.5)
        if not math.isfinite(route_quality) or route_quality < min_route_quality:
            failures.append(
                f"market-intel route_quality {route_quality:.2f} < {min_route_quality:.2f}"
            )

    if args.require_readiness_provenance:
        readiness = metrics.get("readiness", {})
        if readiness.get("venue") != "archer":
            failures.append(f"readiness venue is not archer: {readiness.get('venue')}")
        if readiness.get("status") not in {"ready_for_shadow", "ready_for_live"}:
            failures.append(f"readiness status is not ready: {readiness.get('status')}")
        if readiness.get("certification_passed") is not True:
            failures.append("venue certification has not passed")
        if readiness.get("routing_proof_passed") is not True:
            failures.append("cross-venue routing proof has not passed")
        if readiness.get("policy_status") != "accepted":
            failures.append(f"policy status is not accepted: {readiness.get('policy_status')}")
        if readiness.get("mode") == "live" and readiness.get("live_approved") is not True:
            failures.append("live approval missing for live readiness mode")

        provenance = metrics.get("provenance", {})
        if not str(provenance.get("source_commit", "")).strip():
            failures.append("source commit is missing")
        if not str(provenance.get("config_checksum", "")).startswith("sha256:"):
            failures.append("config checksum is missing or not sha256")
        if provenance.get("policy_version") != "propamm.quote-policy.v1":
            failures.append(f"policy version is unsupported: {provenance.get('policy_version')}")

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


def market_intel_source_count(market_intel: dict[str, Any]) -> int:
    raw_count = market_intel.get("source_count")
    if raw_count is not None:
        try:
            return int(raw_count)
        except (TypeError, ValueError):
            return 0
    sources = market_intel.get("sources")
    if isinstance(sources, list):
        return len(sources)
    if isinstance(sources, dict):
        return len(sources)
    return 0


def market_intel_route_quality(market_intel: dict[str, Any]) -> float:
    raw_quality = market_intel.get("route_quality")
    if isinstance(raw_quality, dict):
        return as_float(raw_quality.get("score"))
    return as_float(raw_quality)


def validate_source(metrics: dict[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    source = metrics.get("source") or metrics.get("build") or {}
    expected_commit, expected_checksum = source_expectations(args)
    if source_required_for(args) and (not expected_commit or not expected_checksum):
        failures.append("source commit/checksum inputs are required before post-start validation")
    if source_required_for(args) and not source:
        failures.append("source metrics are missing")
        return failures
    if source_required_for(args) and (not source.get("commit") or not source.get("checksum")):
        failures.append("source metrics are missing commit/checksum")
    if expected_checksum and source.get("checksum") != expected_checksum:
        failures.append(f"source checksum {source.get('checksum')} != expected {expected_checksum}")
    if expected_commit and source.get("commit") != expected_commit:
        failures.append(f"source commit {source.get('commit')} != expected {expected_commit}")
    return failures


def validate_supervisor(metrics: dict[str, Any], args: argparse.Namespace) -> list[str]:
    supervisor = metrics.get("supervisor", {})
    expected_mode = expected_mode_from(args)
    post_start_active_mode = bool(getattr(args, "post_start", False)) and expected_mode in {
        "shadow",
        "canary",
        "controller",
    }
    if not isinstance(supervisor, dict):
        if post_start_active_mode:
            return ["supervisor metrics are missing"]
        return []
    failures: list[str] = []
    if post_start_active_mode and not supervisor:
        failures.append("supervisor metrics are missing")
        return failures
    if "expected_active" in supervisor:
        expected_active = bool(supervisor.get("expected_active"))
        active = supervisor.get("active")
        if active is not expected_active:
            failures.append(f"supervisor active {active} != expected {expected_active}")
    elif post_start_active_mode:
        failures.append("supervisor active metrics are missing")
    if post_start_active_mode and supervisor.get("expected_active") is True:
        process_active = supervisor.get("process_active")
        if process_active is not True:
            failures.append(f"supervisor process active {process_active} != expected True")

    policy = supervisor.get("policy", {})
    if isinstance(policy, dict):
        if post_start_active_mode and not policy:
            failures.append("supervisor policy metrics are missing")
        if policy.get("ok") is False:
            failures.append(f"supervisor policy is not ok: {policy.get('status')}")
    elif supervisor.get("policy_ok") is False:
        failures.append(f"supervisor policy is not ok: {supervisor.get('policy_status')}")
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
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--post-start", action="store_true")
    parser.add_argument("--config-file", default="")
    parser.add_argument("--config-max-age-seconds", type=float, default=0.0)
    parser.add_argument("--canary-envelope-file", default="")
    parser.add_argument("--rollback-command", default="")
    parser.add_argument(
        "--expected-source-checksum",
        default=os.environ.get("ARCHER_EXPECTED_SOURCE_CHECKSUM", ""),
    )
    parser.add_argument(
        "--expected-source-commit",
        default=os.environ.get("ARCHER_EXPECTED_SOURCE_COMMIT", ""),
    )
    parser.add_argument("--expected-profile", default="overnight_balanced_low_churn")
    parser.add_argument("--min-effective-spread-bps", type=float, default=62.0)
    parser.add_argument("--min-market-intel-spread-add-bps", type=float, default=0.0)
    parser.add_argument("--min-market-intel-source-count", type=int, default=2)
    parser.add_argument("--min-market-intel-route-quality", type=float, default=0.5)
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
    parser.add_argument("--allow-missing-readiness-provenance", action="store_true")
    parser.add_argument("--require-canary-envelope", action="store_true")
    parser.add_argument("--canary-approval-artifact-file", default="")
    parser.add_argument("--allow-missing-source", action="store_true")
    args = parser.parse_args()
    args.require_no_bot = not args.allow_running_bot
    args.require_no_controller = not args.allow_running_controller
    args.require_clear_book = not args.allow_live_book
    args.require_owner_match = not args.allow_market_owner_mismatch
    args.require_market_intel = not args.allow_missing_market_intel
    args.require_readiness_provenance = not args.allow_missing_readiness_provenance

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
