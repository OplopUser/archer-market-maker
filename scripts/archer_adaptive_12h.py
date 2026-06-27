#!/usr/bin/env python3
"""Adaptive 12-hour Archer live strategy controller.

This script is intentionally conservative:
- it keeps a ledger of every evaluation and config change,
- it changes only Archer strategy/config files and Archer screen sessions,
- it clears the MakerBook before each profile switch only when live levels exist.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The adaptive watchdog uses dashboard readback for safety. Live Archer RPC
# commands can exceed the dashboard module's 2s default, so keep the controller
# default aligned with the run dashboard unless the operator overrides it.
os.environ.setdefault("ARCHER_DASHBOARD_COMMAND_TIMEOUT", "20")
os.environ.setdefault("ARCHER_DASHBOARD_RPC_TIMEOUT", "10")

from dashboard.server import (
    DashboardState,
    classify_archer_live_status,
    iso,
    load_simple_toml,
    pubkey_from_keypair_json,
    run_cmd,
)


BIN = ROOT / "target" / "release" / "archer-market-maker"
SOURCE_CONFIG = ROOT / "config" / "live-usdc-style-12h.toml"
ACTIVE_CONFIG = ROOT / "config" / "adaptive-live.toml"
CONTROLLER_SCREEN = "archer-adaptive-12h"
ACTIVE_SCREEN = "archer-adaptive-active"
OLD_SCREENS = ["archer-usdc-style-12h", "archer-usdc-style-active", "archer-sweep-active"]
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

REPAIR_ENTER_QUOTE_PCT = float(os.environ.get("ARCHER_REPAIR_ENTER_QUOTE_PCT", "0.36"))
REPAIR_EXIT_QUOTE_PCT = float(os.environ.get("ARCHER_REPAIR_EXIT_QUOTE_PCT", "0.38"))
QUOTE_REPAIR_ENTER_QUOTE_PCT = float(os.environ.get("ARCHER_QUOTE_REPAIR_ENTER_QUOTE_PCT", "0.68"))
QUOTE_REPAIR_EXIT_QUOTE_PCT = float(os.environ.get("ARCHER_QUOTE_REPAIR_EXIT_QUOTE_PCT", "0.62"))
MAX_REPAIR_SECONDS = int(os.environ.get("ARCHER_MAX_REPAIR_SECONDS", "5400"))
FEE_GUARD_MIN_SECONDS = int(os.environ.get("ARCHER_FEE_GUARD_MIN_SECONDS", "1800"))
FEE_GUARD_DISCOVERY_MIN_SECONDS = int(os.environ.get("ARCHER_FEE_GUARD_DISCOVERY_MIN_SECONDS", "300"))
FEE_GUARD_MAX_DURATION_SECONDS = int(
    float(os.environ.get("ARCHER_MAX_FEE_GUARD_DURATION_MINUTES", "120")) * 60
)
MIN_FILLS_FOR_EDGE_EVALUATION = int(os.environ.get("ARCHER_MIN_FILLS_FOR_EDGE_EVALUATION", "3"))
IDLE_EXPLORATION_AFTER_SECONDS = int(os.environ.get("ARCHER_IDLE_EXPLORATION_AFTER_SECS", "3600"))
WATCHDOG_SECONDS = int(os.environ.get("ARCHER_WATCHDOG_SECONDS", "30"))
WATCHDOG_STALE_STOP = int(os.environ.get("ARCHER_WATCHDOG_STALE_STOP", "2"))
WATCHDOG_RPC429_STOP = int(os.environ.get("ARCHER_WATCHDOG_RPC429_STOP", "6"))
WATCHDOG_FAILED_STOP = int(os.environ.get("ARCHER_WATCHDOG_FAILED_STOP", "5"))
WATCHDOG_TX_STOP = int(os.environ.get("ARCHER_WATCHDOG_TX_STOP", "80"))
STALENESS_CLEAR_STRIKES = int(os.environ.get("ARCHER_STALENESS_CLEAR_STRIKES", "3"))
STALENESS_BACKOFF_SECONDS = [5, 10]
STALENESS_CLEAR_COOLDOWN_SECONDS = int(os.environ.get("ARCHER_STALENESS_CLEAR_COOLDOWN_SECS", "300"))
COMPOSE_STOP_PLACEHOLDER_RUN_ID = "__compose_stop_placeholder__"
APPROVED_CAPPED_PROFILES = {
    "resting_low_churn_16_30",
    "fill_discovery_capped",
    "overnight_selective_edge_probe",
    "defensive_35_55",
}
SAFETY_FALLBACK_PROFILES = {
    "fee_guard_passive_80",
    "inventory_unwind_ask_only",
    "overnight_inventory_repair",
    "overnight_quote_repair_bid_bias",
}
PROFILE_APPROVAL_ENV = "ARCHER_APPROVED_SCALE_PROFILES"
ALLOW_ALL_SCALE_PROFILES_ENV = "ARCHER_ALLOW_SCALE_PROFILES"
PROFILE_APPROVAL_FALLBACK = os.environ.get(
    "ARCHER_PROFILE_APPROVAL_FALLBACK",
    "overnight_selective_edge_probe",
)


PROFIT_GUARD_DEFAULTS: Dict[str, Any] = {
    "intel_spread_add_multiplier": 1.0,
    "max_intel_spread_add_bps": 80.0,
    "max_intel_spread_tighten_bps": 0.0,
    "max_intel_side_spread_add_bps": 40.0,
    "min_effective_spread_bps": 0.0,
    "min_net_edge_bps": 0.0,
    "toxicity_buffer_bps": 0.0,
    "min_intel_size_multiplier": 0.20,
    "post_fill_cooldown_ms": 900000,
    "post_fill_side_size_multiplier": 0.35,
    "post_fill_markout_check_ms": 900000,
    "post_fill_adverse_markout_bps": 12.0,
    "post_fill_adverse_cooldown_ms": 3600000,
}


FILL_TOXICITY_MIN_NOTIONAL = float(os.environ.get("ARCHER_FILL_TOXICITY_MIN_NOTIONAL", "8.0"))
FILL_TOXICITY_MIN_LOSS_USDC = float(os.environ.get("ARCHER_FILL_TOXICITY_MIN_LOSS_USDC", "0.025"))
FILL_TOXICITY_FLOOR_BUFFER_BPS = float(os.environ.get("ARCHER_FILL_TOXICITY_FLOOR_BUFFER_BPS", "8.0"))
FILL_TOXICITY_STOP_FLOOR_BPS = float(os.environ.get("ARCHER_FILL_TOXICITY_STOP_FLOOR_BPS", "95.0"))
WINNER_TRAILING_DRAWDOWN_USDC = float(os.environ.get("ARCHER_WINNER_TRAILING_DRAWDOWN_USDC", "0.05"))
WINNER_NEGATIVE_NET_USDC = float(os.environ.get("ARCHER_WINNER_NEGATIVE_NET_USDC", "0.0"))
WINNER_NEGATIVE_WINDOW_LOSS_USDC = float(os.environ.get("ARCHER_WINNER_NEGATIVE_WINDOW_LOSS_USDC", "0.025"))
WINNER_MIN_FILL_NOTIONAL_USDC = float(os.environ.get("ARCHER_WINNER_MIN_FILL_NOTIONAL_USDC", "8.0"))
WINNER_MIN_SIDE_SIZE_MULTIPLIER = float(os.environ.get("ARCHER_WINNER_MIN_SIDE_SIZE_MULTIPLIER", "0.05"))


def optional_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def market_intel_source_quality_failure(source_quality: Any) -> Optional[str]:
    if isinstance(source_quality, dict):
        source_status = str(source_quality.get("status") or "").lower()
        if source_status not in {"ok", "fresh", "clean", "healthy"} or source_quality.get("stale") is True:
            return f"market-intel source freshness is {source_status or 'unknown'}"
        return None

    if isinstance(source_quality, list) and source_quality:
        for source in source_quality:
            freshness = str(source.get("freshness_status") or source.get("status") or "").lower()
            quality = str(source.get("quality_status") or source.get("quality") or "").lower()
            if freshness not in {"fresh", "ok", "clean", "healthy"}:
                name = source.get("source") or "unknown"
                return f"market-intel source {name} freshness is {freshness or 'unknown'}"
            if quality not in {"healthy", "ok", "fresh", "clean"}:
                name = source.get("source") or "unknown"
                return f"market-intel source {name} quality is {quality or 'unknown'}"
        return None

    return "market-intel source freshness is missing"


PROFILES: Dict[str, Dict[str, Any]] = {
    "overnight_inventory_repair": {
        "description": "Ask-biased overnight repair for SOL-heavy inventory; bids stay off until quote balance recovers.",
        "spread_levels_bps": [22.0, 38.0, 65.0],
        "inventory_pct": 35.0,
        "max_quote_notional_per_level": 22.0,
        "max_total_quote_notional": 140.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 10.0,
        "min_quote_reserve_pct": 80.0,
        "staleness_timeout_ms": 15000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 180000,
        "min_mid_update_ticks": 220,
        "min_full_refresh_interval_ms": 600000,
        "max_update_tx_per_10min": 4,
    },
    "overnight_balanced_low_churn": {
        "description": "Balanced overnight profile after inventory repair: modest spreads, small size, slow repricing.",
        "spread_levels_bps": [24.0, 42.0],
        "inventory_pct": 25.0,
        "max_quote_notional_per_level": 18.0,
        "max_total_quote_notional": 90.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 30.0,
        "min_quote_reserve_pct": 35.0,
        "staleness_timeout_ms": 15000,
        "vol_max_multiplier": 1.0,
        "intel_spread_add_multiplier": 1.0,
        "max_intel_spread_add_bps": 80.0,
        "max_intel_spread_tighten_bps": 0.0,
        "max_intel_side_spread_add_bps": 40.0,
        "min_effective_spread_bps": 24.0,
        "min_intel_size_multiplier": 0.20,
        "post_fill_cooldown_ms": 900000,
        "post_fill_side_size_multiplier": 0.35,
        "post_fill_markout_check_ms": 900000,
        "post_fill_adverse_markout_bps": 12.0,
        "post_fill_adverse_cooldown_ms": 3600000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 240000,
        "min_mid_update_ticks": 260,
        "min_full_refresh_interval_ms": 600000,
        "max_update_tx_per_10min": 4,
    },
    "overnight_selective_edge_probe": {
        "description": "Selective fill-seeking profile: calibrated near the historical profitable band, with explicit edge and toxicity budget.",
        "spread_levels_bps": [24.0, 42.0, 62.0],
        "inventory_pct": 24.0,
        "max_quote_notional_per_level": 20.0,
        "max_total_quote_notional": 120.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 30.0,
        "min_quote_reserve_pct": 30.0,
        "staleness_timeout_ms": 15000,
        "vol_max_multiplier": 1.0,
        "intel_spread_add_multiplier": 0.25,
        "max_intel_spread_add_bps": 40.0,
        "max_intel_spread_tighten_bps": 0.0,
        "max_intel_side_spread_add_bps": 25.0,
        "min_effective_spread_bps": 24.0,
        "min_net_edge_bps": 8.0,
        "toxicity_buffer_bps": 16.0,
        "min_intel_size_multiplier": 0.20,
        "post_fill_cooldown_ms": 900000,
        "post_fill_side_size_multiplier": 0.35,
        "post_fill_markout_check_ms": 900000,
        "post_fill_adverse_markout_bps": 12.0,
        "post_fill_adverse_cooldown_ms": 3600000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 240000,
        "min_mid_update_ticks": 260,
        "min_full_refresh_interval_ms": 600000,
        "max_update_tx_per_10min": 4,
        "validation_spread_floor_bps": 24.0,
        "post_gate_max_break_even_bps": 42.0,
        "post_gate_max_tx_per_hour": 12.0,
    },
    "overnight_quote_repair_bid_bias": {
        "description": "Bid-biased overnight repair if inventory flips USDC-heavy; asks shrink until base recovers.",
        "spread_levels_bps": [28.0, 50.0, 80.0],
        "inventory_pct": 30.0,
        "max_quote_notional_per_level": 18.0,
        "max_total_quote_notional": 110.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 80.0,
        "min_quote_reserve_pct": 15.0,
        "staleness_timeout_ms": 15000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 240000,
        "min_mid_update_ticks": 300,
        "min_full_refresh_interval_ms": 600000,
        "max_update_tx_per_10min": 4,
    },
    "flow_probe_20_32": {
        "description": "Tighter, smaller quote probe to test whether Archer has taker flow near mid.",
        "spread_levels_bps": [20.0, 32.0],
        "inventory_pct": 35.0,
        "max_quote_notional_per_level": 30.0,
        "max_total_quote_notional": 130.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 20.0,
        "min_quote_reserve_pct": 20.0,
        "staleness_timeout_ms": 15000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 120000,
        "min_mid_update_ticks": 180,
    },
    "tight_micro_12_24": {
        "description": "Very small tight probe if wider quotes still get no fills.",
        "spread_levels_bps": [12.0, 24.0],
        "inventory_pct": 25.0,
        "max_quote_notional_per_level": 18.0,
        "max_total_quote_notional": 80.0,
        "min_quote_notional": 6.0,
        "min_base_reserve_pct": 25.0,
        "min_quote_reserve_pct": 25.0,
        "staleness_timeout_ms": 15000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 180000,
        "min_mid_update_ticks": 220,
    },
    "resting_low_churn_16_30": {
        "description": "Active fill baseline: tighter capped quotes with slow repricing when no fills justify churn.",
        "spread_levels_bps": [16.0, 24.0, 36.0],
        "inventory_pct": 20.0,
        "max_quote_notional_per_level": 10.0,
        "max_total_quote_notional": 60.0,
        "min_quote_notional": 1.0,
        "min_base_reserve_pct": 65.0,
        "min_quote_reserve_pct": 30.0,
        "staleness_timeout_ms": 20000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 300000,
        "min_mid_update_ticks": 300,
    },
    "defensive_35_55": {
        "description": "Wider and smaller after fills if trading-vs-hold is negative.",
        "spread_levels_bps": [35.0, 55.0],
        "inventory_pct": 30.0,
        "max_quote_notional_per_level": 22.0,
        "max_total_quote_notional": 100.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 25.0,
        "min_quote_reserve_pct": 25.0,
        "staleness_timeout_ms": 15000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 180000,
        "min_mid_update_ticks": 220,
    },
    "fee_guard_passive_80": {
        "description": "Fee guard: one small wide bid plus one small ask with slow repricing when fees dominate edge.",
        "spread_levels_bps": [80.0],
        "inventory_pct": 20.0,
        "max_quote_notional_per_level": 10.0,
        "max_total_quote_notional": 20.0,
        "min_quote_notional": 1.5,
        "min_base_reserve_pct": 65.0,
        "min_quote_reserve_pct": 20.0,
        "post_fill_side_size_multiplier": 0.75,
        "staleness_timeout_ms": 30000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 900000,
        "min_mid_update_ticks": 1000,
    },
    "fill_discovery_capped": {
        "description": "Bounded fill-discovery probe after clean no-fill fee guard evidence; tiny capped size and explicit edge budget.",
        "spread_levels_bps": [22.0, 34.0],
        "inventory_pct": 12.0,
        "max_quote_notional_per_level": 8.0,
        "max_total_quote_notional": 24.0,
        "min_quote_notional": 1.5,
        "min_base_reserve_pct": 55.0,
        "min_quote_reserve_pct": 35.0,
        "staleness_timeout_ms": 15000,
        "vol_max_multiplier": 1.0,
        "intel_spread_add_multiplier": 0.20,
        "max_intel_spread_add_bps": 35.0,
        "max_intel_spread_tighten_bps": 0.0,
        "max_intel_side_spread_add_bps": 20.0,
        "min_effective_spread_bps": 22.0,
        "min_net_edge_bps": 6.0,
        "toxicity_buffer_bps": 12.0,
        "min_intel_size_multiplier": 0.35,
        "post_fill_side_size_multiplier": 0.75,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 300000,
        "min_mid_update_ticks": 320,
        "min_full_refresh_interval_ms": 600000,
        "max_update_tx_per_10min": 3,
        "post_gate_max_break_even_bps": 34.0,
        "post_gate_max_tx_per_hour": 8.0,
        "forced_transition_trial_minutes": 30.0,
    },
    "inventory_unwind_ask_only": {
        "description": "Loss guard: keep asks dominant but preserve a very wide minimal bid for maker-fee capture.",
        "spread_levels_bps": [200.0, 260.0],
        "inventory_pct": 20.0,
        "max_quote_notional_per_level": 15.0,
        "max_total_quote_notional": 80.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 10.0,
        "min_quote_reserve_pct": 20.0,
        "staleness_timeout_ms": 20000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 600000,
        "min_mid_update_ticks": 500,
    },
    "winner_scale_24_40": {
        "description": "Slightly larger but still fee-aware if fills are profitable after fees.",
        "spread_levels_bps": [24.0, 40.0],
        "inventory_pct": 45.0,
        "max_quote_notional_per_level": 38.0,
        "max_total_quote_notional": 170.0,
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 20.0,
        "min_quote_reserve_pct": 20.0,
        "staleness_timeout_ms": 15000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 120000,
        "min_mid_update_ticks": 180,
    },
}

for _profile in PROFILES.values():
    for _key, _value in PROFIT_GUARD_DEFAULTS.items():
        _profile.setdefault(_key, _value)


def profile_edge_floor_bps(profile: Dict[str, Any]) -> float:
    spread_levels = [
        float(level)
        for level in profile.get("spread_levels_bps", [])
        if float(level) > 0.0
    ]
    configured_floor = min(spread_levels) if spread_levels else 0.0
    min_effective = float(profile.get("min_effective_spread_bps") or 0.0)
    min_net_edge = float(profile.get("min_net_edge_bps") or 0.0)
    toxicity_buffer = float(profile.get("toxicity_buffer_bps") or 0.0)
    return max(configured_floor, min_effective, min_net_edge + toxicity_buffer)


def profile_validation_defaults(profile_name: str) -> Dict[str, float]:
    profile = PROFILES[profile_name]
    edge_floor = profile_edge_floor_bps(profile)
    validation_floor = float(profile.get("validation_spread_floor_bps", edge_floor))
    return {
        "preflight_min_effective_spread_bps": float(
            profile.get("preflight_min_effective_spread_bps", edge_floor)
        ),
        "post_gate_spread_floor_bps": validation_floor,
        "post_gate_min_net_usdc": float(profile.get("post_gate_min_net_usdc", 0.0)),
        "post_gate_max_break_even_bps": float(
            profile.get("post_gate_max_break_even_bps", validation_floor)
        ),
        "post_gate_max_tx_per_hour": float(profile.get("post_gate_max_tx_per_hour", 12.0)),
    }


def _env_csv_set(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def profile_approval_state(profile_name: str) -> Dict[str, Any]:
    if profile_name not in PROFILES:
        raise ValueError(f"unknown profile {profile_name}")

    explicit = _env_csv_set(PROFILE_APPROVAL_ENV)
    if _env_truthy(ALLOW_ALL_SCALE_PROFILES_ENV):
        return {
            "approved": True,
            "profile": profile_name,
            "source": ALLOW_ALL_SCALE_PROFILES_ENV,
            "default_allowed": profile_name in APPROVED_CAPPED_PROFILES,
            "explicitly_allowed": True,
        }
    if profile_name in explicit:
        return {
            "approved": True,
            "profile": profile_name,
            "source": PROFILE_APPROVAL_ENV,
            "default_allowed": profile_name in APPROVED_CAPPED_PROFILES,
            "explicitly_allowed": True,
        }
    if profile_name in APPROVED_CAPPED_PROFILES:
        return {
            "approved": True,
            "profile": profile_name,
            "source": "approved_capped_queue",
            "default_allowed": True,
            "explicitly_allowed": False,
        }
    if profile_name in SAFETY_FALLBACK_PROFILES:
        return {
            "approved": True,
            "profile": profile_name,
            "source": "safety_fallback",
            "default_allowed": True,
            "explicitly_allowed": False,
        }
    return {
        "approved": False,
        "profile": profile_name,
        "source": "approval_required",
        "default_allowed": False,
        "explicitly_allowed": False,
    }


def resolve_profile_approval(
    requested_profile: str,
    reason: str,
    *,
    source: str,
    fallback_profile: Optional[str] = None,
) -> Tuple[str, str]:
    state = profile_approval_state(requested_profile)
    if state["approved"]:
        return (
            requested_profile,
            f"{reason}; profile_approval=approved source={state['source']} "
            f"requested_profile={requested_profile} effective_profile={requested_profile}",
        )

    fallback = fallback_profile or PROFILE_APPROVAL_FALLBACK
    if fallback not in PROFILES or fallback == requested_profile:
        fallback = "resting_low_churn_16_30"
    fallback_state = profile_approval_state(fallback)
    if not fallback_state["approved"]:
        fallback = "resting_low_churn_16_30"

    return (
        fallback,
        "profile_approval_denied: "
        f"source={source}, requested_profile={requested_profile}, "
        f"effective_profile={fallback}, required_env={PROFILE_APPROVAL_ENV}={requested_profile} "
        f"or {ALLOW_ALL_SCALE_PROFILES_ENV}=true; {reason}",
    )


def validate_start_args(
    run_id: str,
    duration_seconds: int,
    evaluation_seconds: int,
    initial_profile: str,
) -> None:
    if run_id == COMPOSE_STOP_PLACEHOLDER_RUN_ID:
        raise ValueError("refusing to start Archer with compose stop placeholder run id")
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if evaluation_seconds <= 0:
        raise ValueError("evaluation_seconds must be positive")
    if initial_profile not in PROFILES:
        raise ValueError(f"unknown initial profile {initial_profile}")


class AdaptiveController:
    def __init__(
        self,
        run_id: str,
        duration_seconds: int,
        evaluation_seconds: int,
        initial_profile: str,
        initial_reason: str,
    ) -> None:
        self.run_id = run_id
        self.duration_seconds = duration_seconds
        self.evaluation_seconds = evaluation_seconds
        self.run_dir = ROOT / "logs" / f"adaptive-12h-{run_id}"
        self.summary = self.run_dir / "summary.log"
        self.ledger_jsonl = self.run_dir / "strategy-ledger.jsonl"
        self.ledger_md = self.run_dir / "strategy-ledger.md"
        self.active_strategy_json = self.run_dir / "active-strategy.json"
        self.heartbeat_json = self.run_dir / "controller-heartbeat.json"
        self.commands_log = self.run_dir / "commands.log"
        self.bot_log = self.run_dir / "bot.log"
        self.config_base = load_simple_toml(SOURCE_CONFIG)
        maker_keypair_path = os.environ.get("ARCHER_MAKER_KEYPAIR_PATH")
        if maker_keypair_path:
            self.config_base.setdefault("market", {})["maker_keypair_path"] = maker_keypair_path
        market_intel_signal_url = os.environ.get("ARCHER_MARKET_INTEL_SIGNAL_URL")
        if market_intel_signal_url:
            feed = self.config_base.setdefault("feed", {})
            if market_intel_signal_url.lower() in {"disabled", "none", "off"}:
                feed.pop("market_intel_signal_url", None)
            else:
                feed["market_intel_signal_url"] = market_intel_signal_url
        shared_blockhash_url = os.environ.get("ARCHER_SHARED_BLOCKHASH_URL")
        if shared_blockhash_url:
            connection = self.config_base.setdefault("connection", {})
            if shared_blockhash_url.lower() in {"disabled", "none", "off"}:
                connection.pop("shared_blockhash_url", None)
            else:
                connection["shared_blockhash_url"] = shared_blockhash_url
        self.rpc_url = str(self.config_base["connection"]["rpc_url"])
        self.wallet_pubkey = self.discover_wallet_pubkey()
        if initial_profile not in PROFILES:
            raise ValueError(f"unknown initial profile {initial_profile}")
        self.current_profile = initial_profile
        self.initial_reason = initial_reason
        self.stop_requested = False
        self.profile_entered_at = time.time()
        self.staleness_strikes = 0
        self.last_staleness_clear_at = 0.0
        self.winner_high_water_net: Optional[float] = None

    def write_controller_heartbeat(self, event: str, reason: Optional[str] = None) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_json.write_text(
            json.dumps(
                {
                    "time": iso(),
                    "run_id": self.run_id,
                    "profile": self.current_profile,
                    "event": event,
                    "reason": reason,
                },
                indent=2,
                sort_keys=True,
            )
        )

    def discover_wallet_pubkey(self) -> str:
        wallet = str(self.config_base["market"]["maker_keypair_path"])
        from_json = pubkey_from_keypair_json(wallet)
        if from_json:
            return from_json
        cmd = run_cmd(["solana-keygen", "pubkey", wallet], timeout=5)
        return cmd["stdout"].strip() if cmd["ok"] else "unknown"

    def log(self, message: str) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}] {message}"
        print(line, flush=True)
        with self.summary.open("a") as fh:
            fh.write(line + "\n")

    def run_logged(self, args: List[str], timeout: float = 30.0, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        with self.commands_log.open("a") as fh:
            fh.write("\n[{time}] $ {cmd}\n".format(time=iso(), cmd=" ".join(shlex.quote(a) for a in args)))
        try:
            proc = subprocess.run(
                args,
                cwd=str(ROOT),
                env=merged_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            with self.commands_log.open("a") as fh:
                fh.write(proc.stdout)
                fh.write(proc.stderr)
                fh.write(f"[exit={proc.returncode}]\n")
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired as exc:
            with self.commands_log.open("a") as fh:
                fh.write(f"timeout after {timeout}s\n")
            return {"ok": False, "returncode": None, "stdout": exc.stdout or "", "stderr": "timeout"}

    def toml_array(self, values: List[float]) -> str:
        return "[" + ", ".join(f"{value:.1f}" for value in values) + "]"

    def optional_toml_line(self, key: str, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return f"{key} = {json.dumps(text)}\n"

    def write_active_config(self, profile_name: str, reason: str) -> None:
        profile = PROFILES[profile_name]
        market = self.config_base["market"]
        connection = self.config_base["connection"]
        feed = self.config_base["feed"]
        execution = self.config_base["execution"]
        monitoring = self.config_base["monitoring"]
        connection_extra = self.optional_toml_line(
            "shared_blockhash_url", connection.get("shared_blockhash_url")
        )
        feed_extra = "".join(
            [
                self.optional_toml_line("cross_symbol", feed.get("cross_symbol")),
                self.optional_toml_line(
                    "market_intel_signal_url", feed.get("market_intel_signal_url")
                ),
            ]
        )
        contents = f"""# Generated by scripts/archer_adaptive_12h.py.
# Active profile: {profile_name}
# Reason: {reason}

[market]
market_pubkey = "{market['market_pubkey']}"
maker_keypair_path = "{market['maker_keypair_path']}"

[connection]
rpc_url = "{connection['rpc_url']}"
{connection_extra}

[feed]
binance_symbol = "{feed.get('binance_symbol', 'SOLUSDT')}"
binance_ws_url = "{feed.get('binance_ws_url', 'wss://data-stream.binance.vision/ws')}"
{feed_extra}market_intel_poll_ms = {int(feed.get('market_intel_poll_ms', 2000))}
staleness_timeout_ms = {int(profile['staleness_timeout_ms'])}

[strategy]
spread_levels_bps = {self.toml_array(profile['spread_levels_bps'])}
inventory_pct = {profile['inventory_pct']:.1f}
vol_window = {int(self.config_base['strategy'].get('vol_window', 300))}
vol_baseline_bps = {float(profile.get('vol_baseline_bps', self.config_base['strategy'].get('vol_baseline_bps', 5.0))):.1f}
vol_max_multiplier = {float(profile.get('vol_max_multiplier', self.config_base['strategy'].get('vol_max_multiplier', 2.0))):.1f}
intel_spread_add_multiplier = {float(profile.get('intel_spread_add_multiplier', self.config_base['strategy'].get('intel_spread_add_multiplier', 1.0))):.2f}
max_intel_spread_add_bps = {float(profile.get('max_intel_spread_add_bps', self.config_base['strategy'].get('max_intel_spread_add_bps', 80.0))):.1f}
max_intel_spread_tighten_bps = {float(profile.get('max_intel_spread_tighten_bps', self.config_base['strategy'].get('max_intel_spread_tighten_bps', 0.0))):.1f}
max_intel_side_spread_add_bps = {float(profile.get('max_intel_side_spread_add_bps', self.config_base['strategy'].get('max_intel_side_spread_add_bps', 40.0))):.1f}
min_effective_spread_bps = {float(profile.get('min_effective_spread_bps', self.config_base['strategy'].get('min_effective_spread_bps', 0.0))):.1f}
min_net_edge_bps = {float(profile.get('min_net_edge_bps', self.config_base['strategy'].get('min_net_edge_bps', 0.0))):.2f}
toxicity_buffer_bps = {float(profile.get('toxicity_buffer_bps', self.config_base['strategy'].get('toxicity_buffer_bps', 0.0))):.2f}
min_intel_size_multiplier = {float(profile.get('min_intel_size_multiplier', self.config_base['strategy'].get('min_intel_size_multiplier', 0.20))):.2f}
post_fill_cooldown_ms = {int(profile.get('post_fill_cooldown_ms', self.config_base['strategy'].get('post_fill_cooldown_ms', 900000)))}
post_fill_side_size_multiplier = {float(profile.get('post_fill_side_size_multiplier', self.config_base['strategy'].get('post_fill_side_size_multiplier', 0.35))):.2f}
post_fill_markout_check_ms = {int(profile.get('post_fill_markout_check_ms', self.config_base['strategy'].get('post_fill_markout_check_ms', 900000)))}
post_fill_adverse_markout_bps = {float(profile.get('post_fill_adverse_markout_bps', self.config_base['strategy'].get('post_fill_adverse_markout_bps', 12.0))):.2f}
post_fill_adverse_cooldown_ms = {int(profile.get('post_fill_adverse_cooldown_ms', self.config_base['strategy'].get('post_fill_adverse_cooldown_ms', 3600000)))}
max_fee_guard_duration_minutes = {FEE_GUARD_MAX_DURATION_SECONDS / 60:.1f}
min_fills_for_edge_evaluation = {MIN_FILLS_FOR_EDGE_EVALUATION}
idle_exploration_after_secs = {IDLE_EXPLORATION_AFTER_SECONDS}
forced_transition_trial_minutes = {float(profile.get('forced_transition_trial_minutes', 0.0)):.1f}

[risk]
min_quote_notional = {profile['min_quote_notional']:.1f}
max_quote_notional_per_level = {profile['max_quote_notional_per_level']:.1f}
max_total_quote_notional = {profile['max_total_quote_notional']:.1f}
min_base_reserve_pct = {profile['min_base_reserve_pct']:.1f}
min_quote_reserve_pct = {profile['min_quote_reserve_pct']:.1f}

[execution]
heartbeat_interval_ms = {int(profile['heartbeat_interval_ms'])}
priority_fee_mode = "{execution.get('priority_fee_mode', 'dynamic')}"
priority_fee_microlamports = {int(execution.get('priority_fee_microlamports', 0))}
priority_fee_min_microlamports = {int(execution.get('priority_fee_min_microlamports', 0))}
priority_fee_max_microlamports = {int(execution.get('priority_fee_max_microlamports', 1000))}
priority_fee_percentile = {int(execution.get('priority_fee_percentile', 25))}
priority_fee_cache_ms = {int(execution.get('priority_fee_cache_ms', 30000))}
priority_fee_emergency_multiplier = {int(execution.get('priority_fee_emergency_multiplier', 2))}
maker_book_poll_interval_ms = {int(execution.get('maker_book_poll_interval_ms', 2000))}
min_mid_update_interval_ms = {int(profile['min_mid_update_interval_ms'])}
min_mid_update_ticks = {int(profile['min_mid_update_ticks'])}
min_full_refresh_interval_ms = {int(profile.get('min_full_refresh_interval_ms', execution.get('min_full_refresh_interval_ms', 600000)))}
min_empty_side_recovery_interval_ms = {int(profile.get('min_empty_side_recovery_interval_ms', execution.get('min_empty_side_recovery_interval_ms', 60000)))}
max_tx_per_minute = {int(execution.get('max_tx_per_minute', 20))}
max_update_tx_per_10min = {int(profile.get('max_update_tx_per_10min', execution.get('max_update_tx_per_10min', 6)))}
max_recovery_update_tx_per_10min = {int(profile.get('max_recovery_update_tx_per_10min', execution.get('max_recovery_update_tx_per_10min', 4)))}
max_clear_book_per_5min = {int(execution.get('max_clear_book_per_5min', 2))}
max_safety_clear_book_per_5min = {int(execution.get('max_safety_clear_book_per_5min', 4))}
min_clear_book_interval_ms = {int(execution.get('min_clear_book_interval_ms', 30000))}
min_safety_clear_book_interval_ms = {int(execution.get('min_safety_clear_book_interval_ms', 10000))}
shadow_mode = true

[monitoring]
log_level = "{monitoring.get('log_level', 'info')}"
"""
        ACTIVE_CONFIG.write_text(contents)
        self.active_strategy_json.write_text(
            json.dumps(
                {
                    "time": iso(),
                    "profile": profile_name,
                    "reason": reason,
                    "description": profile["description"],
                    "config_path": str(ACTIVE_CONFIG),
                    "settings": profile,
                    "profile_approval": profile_approval_state(profile_name),
                },
                indent=2,
                sort_keys=True,
            )
        )

    def append_ledger(
        self,
        event: str,
        profile: str,
        reason: str,
        metrics: Optional[Dict[str, Any]] = None,
        changed: bool = False,
    ) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": iso(),
            "event": event,
            "profile": profile,
            "changed": changed,
            "reason": reason,
            "profile_settings": PROFILES.get(profile),
            "metrics": self.compact_metrics(metrics) if metrics else None,
        }
        with self.ledger_jsonl.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        with self.ledger_md.open("a") as fh:
            fh.write(f"- {entry['time']} | {event} | profile={profile} | changed={changed} | {reason}\n")

    def after_cost_attribution(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        pnl = metrics.get("pnl", {})
        tx = metrics.get("transactions", {})
        profile = PROFILES.get(self.current_profile, {})
        fill_notional = abs(float(pnl.get("quote_delta") or 0.0))
        quote_notional = max(float(profile.get("max_total_quote_notional") or 0.0), 1.0)
        evidence_notional = fill_notional if fill_notional > 0.0 else quote_notional
        fee_usdc = float(pnl.get("fee_usdc") or 0.0)
        net = float(pnl.get("net_trading_vs_hold_usdc") or 0.0)
        trading = float(pnl.get("trading_vs_hold_usdc") or 0.0)
        failed = int(tx.get("failed_count") or 0)
        break_even_spread_bps = fee_usdc / evidence_notional * 10_000.0
        after_cost_edge_bps = net / evidence_notional * 10_000.0
        gross_edge_bps = trading / evidence_notional * 10_000.0
        failed_tx_cost_bps = failed * 0.001 / evidence_notional * 10_000.0
        return {
            "fill_notional_usdc": fill_notional,
            "evidence_notional_usdc": evidence_notional,
            "break_even_spread_bps": break_even_spread_bps,
            "after_cost_edge_bps": after_cost_edge_bps,
            "gross_edge_bps": gross_edge_bps,
            "failed_tx_cost_bps": failed_tx_cost_bps,
        }

    def no_fill_attribution(
        self,
        metrics: Dict[str, Any],
        *,
        live_status: Optional[Dict[str, Any]] = None,
        current_window_fill: Optional[bool] = None,
    ) -> Dict[str, Any]:
        pnl = metrics.get("pnl", {})
        status = metrics.get("status", {})
        market_intel = metrics.get("market_intel", {})
        if live_status is None:
            live_status = metrics.get("archer_live_status")
            if not isinstance(live_status, dict) or "action" not in live_status:
                live_status = classify_archer_live_status(metrics)

        bid_levels = int(status.get("bid_levels") or 0)
        ask_levels = int(status.get("ask_levels") or 0)
        fill_detected = bool(pnl.get("fill_detected")) if current_window_fill is None else current_window_fill
        reason = "unknown"
        if fill_detected:
            reason = "filled"
        elif live_status.get("action") not in {None, "continue"}:
            reason = "stale_or_guarded"
        elif market_intel.get("ok") is False or market_intel.get("quote_enabled") is False:
            reason = "stale_or_guarded"
        elif bid_levels == 0 or ask_levels == 0:
            reason = "side_missing"
        elif self.current_profile == "fee_guard_passive_80":
            reason = "fee_guard_no_edge"
        elif self.estimated_tightest_spread_bps(metrics) >= 70.0:
            reason = "too_wide"
        elif float(PROFILES.get(self.current_profile, {}).get("max_total_quote_notional") or 0.0) <= 24.0:
            reason = "size_too_small"
        return {
            "quote_time_profile": self.current_profile,
            "quote_time_bid_levels": bid_levels,
            "quote_time_ask_levels": ask_levels,
            "quote_time_intel_mode": market_intel.get("mode"),
            "quote_time_spread_bps": self.estimated_tightest_spread_bps(metrics),
            "no_fill_reason": reason,
        }

    def parent_child_attribution(self, metrics: Dict[str, Any], no_fill: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        market_intel = metrics.get("market_intel", {})
        if not isinstance(market_intel, dict):
            market_intel = {}
        parent_mode = str(market_intel.get("parent_cluster_mode") or "").lower()
        intel_mode = str(market_intel.get("mode") or "").lower()
        if no_fill is None:
            no_fill = self.no_fill_attribution(metrics)
        no_fill_reason = str(no_fill.get("no_fill_reason") or "unknown")
        if parent_mode == "normal" and self.current_profile == "fee_guard_passive_80":
            return {
                "parent_child_attribution": "parent_normal_local_fee_guard",
                "parent_child_deviation_reason": (
                    "parent normal; Archer local fee/no-fill guard is driving passive profile"
                ),
            }
        if parent_mode == "normal" and no_fill_reason not in {"filled", "unknown"}:
            return {
                "parent_child_attribution": f"parent_normal_local_{no_fill_reason}",
                "parent_child_deviation_reason": f"parent normal; Archer local {no_fill_reason} condition is driving behavior",
            }
        if parent_mode and parent_mode not in {"normal"}:
            return {
                "parent_child_attribution": "parent_cluster_guard",
                "parent_child_deviation_reason": f"parent cluster mode is {parent_mode}",
            }
        if intel_mode in {"pause", "reduce_only", "cautious"}:
            return {
                "parent_child_attribution": "child_market_intel_guard",
                "parent_child_deviation_reason": f"child market-intel mode is {intel_mode}",
            }
        return {
            "parent_child_attribution": "parent_child_aligned",
            "parent_child_deviation_reason": "parent and Archer local controls are aligned",
        }

    def compact_metrics(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        pnl = metrics.get("pnl", {})
        tx = metrics.get("transactions", {})
        status = metrics.get("status", {})
        market = metrics.get("market", {})
        market_intel = metrics.get("market_intel", {})
        logs = metrics.get("logs", {}).get("counts", {})
        live_status = metrics.get("archer_live_status")
        if not isinstance(live_status, dict) or "action" not in live_status:
            live_status = classify_archer_live_status(metrics)
        alert = live_status.get("alert", {})
        attribution = self.no_fill_attribution(metrics, live_status=live_status)
        parent_attribution = self.parent_child_attribution(metrics, attribution)
        after_cost = self.after_cost_attribution(metrics)
        return {
            "mid_price": metrics.get("mid_price"),
            "market_command_ok": market.get("command_ok"),
            "status_command_ok": status.get("command_ok"),
            "status_stale": status.get("stale"),
            "status_source": status.get("source"),
            "live_state": live_status.get("state"),
            "live_action": live_status.get("action"),
            "live_restart_policy": live_status.get("restart_policy"),
            "live_alert_severity": alert.get("severity"),
            "live_alert_reason": alert.get("reason"),
            "bid_levels": status.get("bid_levels"),
            "ask_levels": status.get("ask_levels"),
            "base_total": status.get("base_total"),
            "quote_total": status.get("quote_total"),
            "base_delta": pnl.get("base_delta"),
            "quote_delta": pnl.get("quote_delta"),
            "fill_detected": pnl.get("fill_detected"),
            "seconds_since_last_fill": pnl.get("seconds_since_last_fill"),
            "fills_this_window": pnl.get("fills_this_window"),
            "total_fills": pnl.get("total_fills"),
            "trading_vs_hold_usdc": pnl.get("trading_vs_hold_usdc"),
            "net_trading_vs_hold_usdc": pnl.get("net_trading_vs_hold_usdc"),
            "fee_usdc": pnl.get("fee_usdc"),
            "fee_sol": pnl.get("fee_sol"),
            "tx_count": tx.get("since_start_count"),
            "failed_count": tx.get("failed_count"),
            "transactions_ok": tx.get("ok"),
            "price_feed_stale": logs.get("price_feed_stale"),
            "tx_circuit_breaker": logs.get("tx_circuit_breaker"),
            "rpc_429": logs.get("rpc_429"),
            "forced_transition_remaining_trial_minutes": metrics.get("strategy", {}).get(
                "forced_transition_remaining_trial_minutes"
            ),
            "parent_cluster_id": market_intel.get("parent_cluster_id"),
            "parent_cluster_mode": market_intel.get("parent_cluster_mode"),
            "parent_cluster_child_markets": market_intel.get("parent_cluster_child_markets") or [],
            "parent_cluster_child_venues": market_intel.get("parent_cluster_child_venues") or [],
            "parent_cluster_base_pyth_conf_bps": market_intel.get("parent_cluster_base_pyth_conf_bps"),
            "parent_cluster_hedge_status": market_intel.get("parent_cluster_hedge_status"),
            "parent_cluster_reason_codes": market_intel.get("parent_cluster_reason_codes") or [],
            **parent_attribution,
            **attribution,
            **after_cost,
        }

    def stop_screen(self, name: str) -> None:
        subprocess.run(["screen", "-S", name, "-X", "quit"], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)

    def stop_bot_processes(self) -> None:
        cmd = run_cmd(["pgrep", "-f", f"{BIN} run --config"], timeout=5)
        pids = [line.split()[0] for line in cmd["stdout"].splitlines() if line.strip()]
        if pids:
            self.log(f"Stopping Archer bot pids: {' '.join(pids)}")
            subprocess.run(["kill", *pids], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)

    def stop_old_controller_processes(self) -> None:
        cmd = run_cmd(["pgrep", "-fl", "archer_usdc_style_12h.sh"], timeout=5)
        pids = [line.split()[0] for line in cmd["stdout"].splitlines() if line.strip()]
        if pids:
            self.log(f"Stopping old USDC-style controller pids: {' '.join(pids)}")
            subprocess.run(["kill", *pids], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)

    @staticmethod
    def parse_status_levels(output: str) -> Optional[Tuple[int, int]]:
        bid_levels: Optional[int] = None
        ask_levels: Optional[int] = None
        for line in output.splitlines():
            key, sep, value = line.partition(":")
            if sep != ":":
                continue
            if key.strip() == "Bid levels":
                try:
                    bid_levels = int(value.strip())
                except ValueError:
                    pass
            elif key.strip() == "Ask levels":
                try:
                    ask_levels = int(value.strip())
                except ValueError:
                    pass
        if bid_levels is None or ask_levels is None:
            return None
        return bid_levels, ask_levels

    def clear_book(self) -> None:
        status = self.run_logged([str(BIN), "status", "--config", str(ACTIVE_CONFIG)], timeout=20)
        levels = self.parse_status_levels(status["stdout"] + status["stderr"])
        if levels == (0, 0):
            self.log("Archer book already clear; skipping clear transaction")
            return
        if levels is None:
            self.log("Clearing Archer book because status levels could not be confirmed")
        else:
            self.log(f"Clearing Archer book bid_levels={levels[0]} ask_levels={levels[1]}")
        self.run_logged(
            [str(BIN), "kill", "--config", str(ACTIVE_CONFIG)],
            timeout=30,
            env={"ARCHER_ENABLE_LIVE_TRADING": "true"},
        )
        time.sleep(5)

    def snapshot(self, label: str) -> None:
        out = self.run_dir / f"{label}.snapshot.txt"
        lines: List[str] = [f"=== {label} ===", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"), ""]
        lines.extend(["--- config ---", str(ACTIVE_CONFIG), ""])
        lines.extend(["--- archer status ---"])
        status = self.run_logged([str(BIN), "status", "--config", str(ACTIVE_CONFIG)], timeout=20)
        lines.append(status["stdout"] + status["stderr"])
        lines.extend(["", "--- wallet balances ---"])
        try:
            balances = DashboardState(ACTIVE_CONFIG, self.run_dir, 30, 0, 0).get_wallet_balances()
            lines.append(json.dumps(balances, indent=2, sort_keys=True))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"wallet balance collection failed: {exc}")
        out.write_text("\n".join(lines))
        self.log(f"Wrote snapshot {out}")

    def start_bot(self) -> None:
        self.log(f"Starting Archer adaptive live bot with profile={self.current_profile}")
        with self.bot_log.open("a") as fh:
            fh.write(f"\n[{iso()}] starting profile={self.current_profile}\n")
        quoted = (
            f"cd {shlex.quote(str(ROOT))} && "
            f"ARCHER_ENABLE_LIVE_TRADING=true {shlex.quote(str(BIN))} "
            f"run --config {shlex.quote(str(ACTIVE_CONFIG))} --live >> {shlex.quote(str(self.bot_log))} 2>&1"
        )
        subprocess.run(["screen", "-dmS", ACTIVE_SCREEN, "zsh", "-lc", quoted], cwd=str(ROOT), check=False)
        time.sleep(70)
        proc = run_cmd(["pgrep", "-fl", f"{BIN} run --config {ACTIVE_CONFIG} --live"], timeout=5)
        if not proc["stdout"].strip():
            raise RuntimeError("adaptive Archer bot did not stay up after startup")
        self.log("Adaptive bot startup confirmed")

    def collect_metrics(self) -> Dict[str, Any]:
        state = DashboardState(ACTIVE_CONFIG, self.run_dir, 30, 5000, 120)
        last_metrics: Optional[Dict[str, Any]] = None
        for attempt in range(1, 4):
            self.write_controller_heartbeat("collect_metrics", f"attempt {attempt}")
            metrics = state.collect_cached(force=True, record=True)
            last_metrics = metrics
            market = metrics.get("market", {})
            status = metrics.get("status", {})
            live_status = metrics.get("archer_live_status") or classify_archer_live_status(metrics)
            if live_status.get("action") in {"continue", "continue_degraded", "quote_reduce_only"}:
                return metrics
            self.log(
                "Live metrics collection returned non-live status "
                f"(attempt {attempt}/3): "
                f"market_ok={market.get('command_ok')}, "
                f"status_ok={status.get('command_ok')}, "
                f"status_stale={status.get('stale')}, "
                f"status_source={status.get('source')}, "
                f"live_state={live_status.get('state')}, "
                f"live_action={live_status.get('action')}, "
                f"status_error={status.get('command_error') or market.get('command_error')}"
            )
            time.sleep(2 * attempt)
        if last_metrics is None:
            raise RuntimeError("metrics collection produced no result")
        return last_metrics

    def load_last_eval_metrics(self) -> Optional[Dict[str, Any]]:
        if not self.ledger_jsonl.exists():
            return None
        last = None
        for line in self.ledger_jsonl.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("metrics"):
                last = entry["metrics"]
        return last

    def estimated_tightest_spread_bps(self, metrics: Dict[str, Any]) -> float:
        profile = PROFILES.get(self.current_profile, {})
        levels = profile.get("spread_levels_bps") or []
        try:
            configured_spread = min(float(level) for level in levels) if levels else 0.0
        except (TypeError, ValueError):
            configured_spread = 0.0
        try:
            edge_floor = profile_edge_floor_bps(profile)
        except (TypeError, ValueError):
            edge_floor = configured_spread
        try:
            intel_multiplier = float(profile.get("intel_spread_add_multiplier") or 0.0)
        except (TypeError, ValueError):
            intel_multiplier = 0.0
        try:
            intel_add = float(
                metrics.get("market_intel", {}).get("spread_add_bps") or 0.0
            )
        except (TypeError, ValueError):
            intel_add = 0.0
        return max(
            edge_floor,
            configured_spread + max(0.0, intel_add) * max(0.0, intel_multiplier),
        )

    def fill_toxicity_decision(
        self,
        window_fill: bool,
        window_net: float,
        window_quote_delta: float,
        metrics: Dict[str, Any],
    ) -> Optional[Tuple[str, str]]:
        fill_notional = abs(window_quote_delta)
        if (
            not window_fill
            or fill_notional < FILL_TOXICITY_MIN_NOTIONAL
            or window_net >= -FILL_TOXICITY_MIN_LOSS_USDC
        ):
            return None

        current_floor = self.estimated_tightest_spread_bps(metrics)
        extra_edge_bps = max(0.0, -window_net) / fill_notional * 10_000.0
        required_floor_bps = current_floor + extra_edge_bps
        reason = (
            "fill toxicity guard: "
            f"window_net=${window_net:.3f}, fill_notional=${fill_notional:.2f}, "
            f"current_floor={current_floor:.1f}bps, "
            f"required_floor~{required_floor_bps:.1f}bps"
        )

        if (
            self.current_profile == "fee_guard_passive_80"
            or required_floor_bps >= FILL_TOXICITY_STOP_FLOOR_BPS
        ):
            return "__stop__", reason + "; stopping instead of spending more fees"

        if required_floor_bps >= current_floor + FILL_TOXICITY_FLOOR_BUFFER_BPS:
            return "fee_guard_passive_80", reason + "; switching to passive 80bps guard"
        return None

    def discovery_gates_clean(
        self,
        metrics: Dict[str, Any],
        *,
        live_status: Dict[str, Any],
        window_stale: int,
        window_rpc_429: int,
        window_failed: int,
    ) -> Tuple[bool, str]:
        status = metrics.get("status", {})
        dashboard_health = metrics.get("dashboard_health", {})
        market_intel = metrics.get("market_intel", {})
        if live_status.get("action") != "continue":
            return False, f"live action is {live_status.get('action')}"
        if dashboard_health.get("ok") is not True or dashboard_health.get("stale") is True:
            return False, "dashboard health is not fresh"
        if status.get("command_ok") is not True or status.get("stale") is True:
            return False, "dashboard status readback is not fresh"
        if live_status.get("rpc_healthy") is not True or live_status.get("rpc_stale") is True:
            return False, "direct RPC readback is not fresh"
        if live_status.get("heartbeat_healthy") is not True:
            return False, "controller heartbeat is not healthy"
        if window_stale > 0 or window_rpc_429 > 0 or window_failed > 0:
            return (
                False,
                f"recent error delta present: stale={window_stale}, rpc_429={window_rpc_429}, failed={window_failed}",
            )
        if market_intel.get("ok") is not True or market_intel.get("quote_enabled") is not True:
            return False, "market-intel is not ok or has disabled quoting"
        if str(market_intel.get("mode") or "").lower() == "pause":
            return False, "market-intel mode is pause"
        fair_value = float(market_intel.get("fair_value") or 0.0)
        if not math.isfinite(fair_value) or fair_value <= 0.0:
            return False, "market-intel fair value is invalid"
        source_quality_failure = market_intel_source_quality_failure(market_intel.get("source_quality"))
        if source_quality_failure:
            return False, source_quality_failure
        generated_at = market_intel.get("generated_at_unix_secs")
        if generated_at is not None:
            try:
                age_secs = time.time() - float(generated_at)
            except (TypeError, ValueError):
                return False, "market-intel source freshness timestamp is invalid"
            if age_secs > 45.0:
                return False, f"market-intel source freshness age is {age_secs:.0f}s"
        return True, "clean"

    def no_edge_fee_guard_decision(
        self,
        metrics: Dict[str, Any],
        *,
        live_status: Dict[str, Any],
        profile_age: float,
        hour_index: int,
        window_fill: bool,
        window_stale: int,
        window_rpc_429: int,
        window_failed: int,
    ) -> Optional[Tuple[str, str]]:
        pnl = metrics.get("pnl", {})
        tx = metrics.get("transactions", {})
        if self.current_profile != "fee_guard_passive_80" or window_fill:
            return None
        if profile_age < FEE_GUARD_DISCOVERY_MIN_SECONDS and hour_index < 1:
            return None

        after_cost = self.after_cost_attribution(metrics)
        no_fill = self.no_fill_attribution(
            metrics,
            live_status=live_status,
            current_window_fill=window_fill,
        )
        net = float(pnl.get("net_trading_vs_hold_usdc") or 0.0)
        tx_count = int(tx.get("since_start_count") or 0)
        total_fills = int(pnl.get("total_fills") or 0)
        seconds_since_last_fill = pnl.get("seconds_since_last_fill")
        try:
            seconds_since_last_fill_f = float(seconds_since_last_fill)
        except (TypeError, ValueError):
            seconds_since_last_fill_f = profile_age if not bool(pnl.get("fill_detected")) else 0.0
        idle_ready = seconds_since_last_fill_f >= IDLE_EXPLORATION_AFTER_SECONDS
        timeout_escape = total_fills == 0 and profile_age >= FEE_GUARD_MAX_DURATION_SECONDS
        if not timeout_escape and not idle_ready and total_fills < MIN_FILLS_FOR_EDGE_EVALUATION:
            return None
        no_edge = net <= 0.02 or after_cost["after_cost_edge_bps"] <= 2.0
        enough_exposure = (
            tx_count >= 1
            or profile_age >= FEE_GUARD_DISCOVERY_MIN_SECONDS
            or hour_index >= 1
        )
        if not no_edge or not enough_exposure:
            return None

        gates_clean, gate_reason = self.discovery_gates_clean(
            metrics,
            live_status=live_status,
            window_stale=window_stale,
            window_rpc_429=window_rpc_429,
            window_failed=window_failed,
        )
        reason = (
            "no-edge fee guard: "
            f"no_fill_reason={no_fill['no_fill_reason']}, "
            f"quote_time_spread={no_fill['quote_time_spread_bps']:.1f}bps, "
            f"break_even={after_cost['break_even_spread_bps']:.1f}bps, "
            f"after_cost_edge={after_cost['after_cost_edge_bps']:.1f}bps, "
            f"tx_count={tx_count}, age={int(profile_age)}s"
        )
        if str(metrics.get("market_intel", {}).get("parent_cluster_mode") or "").lower() == "normal":
            reason += "; parent_normal_local_no_fill"
        if gates_clean:
            if timeout_escape:
                reason = "no_fill_timeout_escape: " + reason
            return (
                "fill_discovery_capped",
                reason + "; entering bounded fill discovery",
            )
        return (
            "__stop__",
            reason + f"; discovery unsafe, stopping/shadowing instead: {gate_reason}",
        )

    def resting_low_churn_idle_decision(
        self,
        metrics: Dict[str, Any],
        *,
        live_status: Dict[str, Any],
        profile_age: float,
        window_fill: bool,
        window_stale: int,
        window_rpc_429: int,
        window_failed: int,
    ) -> Optional[Tuple[str, str]]:
        if self.current_profile != "resting_low_churn_16_30" or window_fill:
            return None

        pnl = metrics.get("pnl", {})
        try:
            fills_this_window = int(pnl.get("fills_this_window") or 0)
        except (TypeError, ValueError):
            fills_this_window = 0
        if fills_this_window > 0:
            return None

        try:
            total_fills = int(pnl.get("total_fills") or 0)
        except (TypeError, ValueError):
            total_fills = 0
        if total_fills <= 0:
            return None

        try:
            seconds_since_last_fill = float(pnl.get("seconds_since_last_fill"))
        except (TypeError, ValueError):
            return None
        if seconds_since_last_fill < IDLE_EXPLORATION_AFTER_SECONDS:
            return None

        net = float(pnl.get("net_trading_vs_hold_usdc") or 0.0)
        if net < -0.05:
            return None

        gates_clean, gate_reason = self.discovery_gates_clean(
            metrics,
            live_status=live_status,
            window_stale=window_stale,
            window_rpc_429=window_rpc_429,
            window_failed=window_failed,
        )
        no_fill = self.no_fill_attribution(
            metrics,
            live_status=live_status,
            current_window_fill=False,
        )
        reason = (
            "resting_low_churn_idle_escape: "
            f"seconds_since_last_fill={seconds_since_last_fill:.0f}, "
            f"fills_this_window={fills_this_window}, total_fills={total_fills}, "
            f"no_fill_reason={no_fill['no_fill_reason']}, "
            f"quote_time_spread={no_fill['quote_time_spread_bps']:.1f}bps, "
            f"age={int(profile_age)}s"
        )
        if not gates_clean:
            return (
                self.current_profile,
                reason + f"; discovery gate blocked by {gate_reason}",
            )
        return (
            "fill_discovery_capped",
            reason + "; entering capped fill discovery",
        )

    def winner_profile_guard_decision(
        self,
        *,
        net: float,
        trading: float,
        prev_net: float,
        window_net: float,
        window_fill: bool,
        window_quote_delta: float,
    ) -> Optional[Tuple[str, str]]:
        if self.current_profile != "winner_scale_24_40":
            self.winner_high_water_net = None
            return None

        starting_high_water = prev_net if self.winner_high_water_net is None else self.winner_high_water_net
        self.winner_high_water_net = max(starting_high_water, net)
        trailing_drawdown = self.winner_high_water_net - net
        fill_notional = abs(window_quote_delta)
        min_fill_notional = max(
            WINNER_MIN_FILL_NOTIONAL_USDC,
            float(PROFILES["winner_scale_24_40"].get("min_quote_notional") or 0.0)
            * WINNER_MIN_SIDE_SIZE_MULTIPLIER,
        )
        material_fill_window = window_fill and fill_notional >= min_fill_notional
        negative_fill_window = material_fill_window and window_net <= -WINNER_NEGATIVE_WINDOW_LOSS_USDC
        trailing_loss = (
            self.winner_high_water_net > 0.0
            and trailing_drawdown >= WINNER_TRAILING_DRAWDOWN_USDC
        )
        net_edge_nonpositive = material_fill_window and net <= WINNER_NEGATIVE_NET_USDC
        if not (net_edge_nonpositive or negative_fill_window or trailing_loss):
            return None

        target = "fee_guard_passive_80" if net_edge_nonpositive else "resting_low_churn_16_30"
        reasons = []
        if net_edge_nonpositive:
            reasons.append("net_edge_nonpositive")
        if negative_fill_window:
            reasons.append("negative_fill_window")
        if trailing_loss:
            reasons.append("trailing_drawdown")
        return (
            target,
            "winner profile performance guard: "
            f"reasons={','.join(reasons)}, "
            f"net_vs_hold=${net:.3f}, trading_vs_hold=${trading:.3f}, "
            f"high_water=${self.winner_high_water_net:.3f}, "
            f"trailing_drawdown=${trailing_drawdown:.3f}, "
            f"window_net=${window_net:.3f}, fill_notional=${fill_notional:.2f}",
        )

    def decide_next_profile(self, metrics: Dict[str, Any], previous: Optional[Dict[str, Any]], hour_index: int) -> Tuple[str, str]:
        has_previous = previous is not None
        pnl = metrics.get("pnl", {})
        tx = metrics.get("transactions", {})
        logs = metrics.get("logs", {}).get("counts", {})
        fee_usdc = float(pnl.get("fee_usdc") or 0.0)
        net = float(pnl.get("net_trading_vs_hold_usdc") or 0.0)
        trading = float(pnl.get("trading_vs_hold_usdc") or 0.0)
        fill_detected = bool(pnl.get("fill_detected"))
        failed = int(tx.get("failed_count") or 0)
        tx_count = int(tx.get("since_start_count") or 0)
        fail_rate = failed / tx_count if tx_count else 0.0
        stale = int(logs.get("price_feed_stale") or 0)
        rpc_429 = int(logs.get("rpc_429") or 0)
        status = metrics.get("status", {})
        market = metrics.get("market", {})
        live_status = metrics.get("archer_live_status")
        if not isinstance(live_status, dict) or "action" not in live_status:
            live_status = classify_archer_live_status(metrics)
        live_action = live_status.get("action")
        live_alert = live_status.get("alert", {})

        if live_action == "stop_supervised":
            return (
                "__stop__",
                "live status failover action requires supervised stop: "
                f"state={live_status.get('state')}, action={live_action}, "
                f"restart_policy={live_status.get('restart_policy')}, "
                f"alert={live_alert.get('reason')}; "
                f"market_ok={market.get('command_ok')}, "
                f"status_ok={status.get('command_ok')}, "
                f"status_stale={status.get('stale')}, "
                f"status_source={status.get('source')}, "
                f"status_error={status.get('command_error') or market.get('command_error')}",
            )
        if live_action == "quote_reduce_only":
            prev_live_action = previous.get("live_action") if previous else None
            prev_live_state = previous.get("live_state") if previous else None
            prev_stale_for_quote_reduce = int(previous.get("price_feed_stale") or stale) if previous else stale
            prev_rpc_429_for_quote_reduce = int(previous.get("rpc_429") or rpc_429) if previous else rpc_429
            prev_failed_for_quote_reduce = int(previous.get("failed_count") or failed) if previous else failed
            confirmed_quote_reduce = (
                prev_live_action == "quote_reduce_only"
                or prev_live_state == "rpc_stale_dashboard_healthy"
                or max(0, stale - prev_stale_for_quote_reduce) >= 2
                or max(0, rpc_429 - prev_rpc_429_for_quote_reduce) >= 3
                or max(0, failed - prev_failed_for_quote_reduce) >= 2
            )
            if not confirmed_quote_reduce:
                return (
                    self.current_profile,
                    "single live status failover action quote_reduce_only; "
                    "holding current profile pending confirmation: "
                    f"state={live_status.get('state')}, "
                    f"restart_policy={live_status.get('restart_policy')}, "
                    f"alert={live_alert.get('reason')}",
                )
            return (
                "fee_guard_passive_80",
                "confirmed live status failover action quote_reduce_only: "
                f"state={live_status.get('state')}, "
                f"restart_policy={live_status.get('restart_policy')}, "
                f"alert={live_alert.get('reason')}",
            )

        prev_fee = float(previous.get("fee_usdc") or 0.0) if previous else fee_usdc
        window_fee = max(0.0, fee_usdc - prev_fee)
        prev_net = float(previous.get("net_trading_vs_hold_usdc") or 0.0) if previous else net
        prev_trading = float(previous.get("trading_vs_hold_usdc") or 0.0) if previous else trading
        prev_failed = int(previous.get("failed_count") or 0) if previous else failed
        prev_tx_count = int(previous.get("tx_count") or 0) if previous else tx_count
        prev_stale = int(previous.get("price_feed_stale") or 0) if previous else stale
        prev_rpc_429 = int(previous.get("rpc_429") or 0) if previous else rpc_429
        prev_base_delta = (
            float(previous.get("base_delta") or 0.0)
            if previous
            else float(pnl.get("base_delta") or 0.0)
        )
        prev_quote_delta = (
            float(previous.get("quote_delta") or 0.0)
            if previous
            else float(pnl.get("quote_delta") or 0.0)
        )
        window_net = net - prev_net
        window_trading = trading - prev_trading
        window_quote_delta = float(pnl.get("quote_delta") or 0.0) - prev_quote_delta
        window_failed = max(0, failed - prev_failed)
        window_tx_count = max(0, tx_count - prev_tx_count)
        window_stale = max(0, stale - prev_stale)
        window_rpc_429 = max(0, rpc_429 - prev_rpc_429)
        window_fail_rate = window_failed / window_tx_count if window_tx_count else 0.0
        significant_window_fail_rate = window_tx_count >= 20 and window_fail_rate > 0.03
        window_fill = (
            abs(float(pnl.get("base_delta") or 0.0) - prev_base_delta) > 0.000001
            or abs(float(pnl.get("quote_delta") or 0.0) - prev_quote_delta) > 0.0001
        )
        mid_price = float(metrics.get("mid_price") or 0.0)
        base_total = float(status.get("base_total") or 0.0)
        quote_total = float(status.get("quote_total") or 0.0)
        portfolio_value = base_total * mid_price + quote_total
        quote_pct = quote_total / portfolio_value if portfolio_value > 0 else None
        base_pct = 1.0 - quote_pct if quote_pct is not None else None
        profile_age = time.time() - self.profile_entered_at

        if window_rpc_429 >= 6 or window_failed >= 5:
            return (
                "__stop__",
                "runaway error guard: "
                f"window_stale={window_stale}, window_rpc_429={window_rpc_429}, "
                f"window_failed={window_failed}, window_tx={window_tx_count}",
            )

        if (
            window_rpc_429 >= 3
            or window_stale >= 2
            or significant_window_fail_rate
            or window_failed >= 2
            or (window_tx_count >= 45 and window_fee > 0.04 and window_net < 0.05)
        ):
            return (
                "fee_guard_passive_80",
                "health/fee guard: "
                f"stale={stale}, rpc_429={rpc_429}, fail_rate={fail_rate:.2%}, "
                f"window_stale={window_stale}, window_rpc_429={window_rpc_429}, "
                f"window_fail_rate={window_fail_rate:.2%}, window_failed={window_failed}",
            )

        winner_guard = self.winner_profile_guard_decision(
            net=net,
            trading=trading,
            prev_net=prev_net,
            window_net=window_net,
            window_fill=window_fill,
            window_quote_delta=window_quote_delta,
        )
        if winner_guard is not None:
            return winner_guard

        fill_toxicity = self.fill_toxicity_decision(
            window_fill,
            window_net,
            window_quote_delta,
            metrics,
        )
        if fill_toxicity is not None:
            return fill_toxicity

        no_edge_fee_guard = self.no_edge_fee_guard_decision(
            metrics,
            live_status=live_status,
            profile_age=profile_age,
            hour_index=hour_index,
            window_fill=window_fill,
            window_stale=window_stale,
            window_rpc_429=window_rpc_429,
            window_failed=window_failed,
        )
        if no_edge_fee_guard is not None:
            return no_edge_fee_guard

        resting_idle = self.resting_low_churn_idle_decision(
            metrics,
            live_status=live_status,
            profile_age=profile_age,
            window_fill=window_fill,
            window_stale=window_stale,
            window_rpc_429=window_rpc_429,
            window_failed=window_failed,
        )
        if resting_idle is not None:
            return resting_idle

        if quote_pct is not None:
            if self.current_profile == "fee_guard_passive_80" and profile_age >= FEE_GUARD_MIN_SECONDS:
                if REPAIR_EXIT_QUOTE_PCT <= quote_pct <= QUOTE_REPAIR_EXIT_QUOTE_PCT:
                    return (
                        "overnight_balanced_low_churn",
                        f"fee guard cooled down; returning to balanced profile: quote_pct={quote_pct:.1%}, age={int(profile_age)}s",
                    )
            if self.current_profile in {"overnight_inventory_repair", "overnight_quote_repair_bid_bias"}:
                if self.current_profile == "overnight_inventory_repair" and quote_pct >= REPAIR_EXIT_QUOTE_PCT:
                    return (
                        "overnight_balanced_low_churn",
                        f"SOL-heavy repair complete; returning to balanced quoting: quote_pct={quote_pct:.1%}",
                    )
                if self.current_profile == "overnight_quote_repair_bid_bias" and quote_pct <= QUOTE_REPAIR_EXIT_QUOTE_PCT:
                    return (
                        "overnight_balanced_low_churn",
                        f"USDC-heavy repair complete; returning to balanced quoting: quote_pct={quote_pct:.1%}",
                    )
                if profile_age >= MAX_REPAIR_SECONDS:
                    return (
                        "fee_guard_passive_80",
                        f"max repair duration reached; forcing two-sided fee guard: quote_pct={quote_pct:.1%}, age={int(profile_age)}s",
                    )
                return (
                    self.current_profile,
                    f"holding inventory repair until balance normalizes: quote_pct={quote_pct:.1%}, base_pct={base_pct:.1%}, age={int(profile_age)}s",
                )
            if quote_pct < REPAIR_ENTER_QUOTE_PCT:
                return (
                    "overnight_inventory_repair",
                    f"SOL-heavy inventory repair: quote_pct={quote_pct:.1%}, base_pct={base_pct:.1%}, quote_total=${quote_total:.2f}",
                )
            if quote_pct > QUOTE_REPAIR_ENTER_QUOTE_PCT:
                return (
                    "overnight_quote_repair_bid_bias",
                    f"USDC-heavy inventory repair: quote_pct={quote_pct:.1%}, base_pct={base_pct:.1%}, base_total={base_total:.4f}",
                )
            if self.current_profile == "overnight_balanced_low_churn":
                if not fill_detected and not window_fill:
                    gates_clean, gate_reason = self.discovery_gates_clean(
                        metrics,
                        live_status=live_status,
                        window_stale=window_stale,
                        window_rpc_429=window_rpc_429,
                        window_failed=window_failed,
                    )
                    if gates_clean:
                        return (
                            "overnight_selective_edge_probe",
                            "clean gated no-fill discovery: "
                            f"quote_pct={quote_pct:.1%}, net_vs_hold=${net:.3f}, "
                            "entering selective edge probe",
                        )
                    return (
                        "overnight_balanced_low_churn",
                        f"holding balanced profile; no-fill discovery gate blocked by {gate_reason}: "
                        f"quote_pct={quote_pct:.1%}, net_vs_hold=${net:.3f}",
                    )
                return (
                    "overnight_balanced_low_churn",
                    f"holding overnight balanced low-churn profile: quote_pct={quote_pct:.1%}, net_vs_hold=${net:.3f}",
                )

        base_delta = float(pnl.get("base_delta") or 0.0)
        if self.current_profile == "inventory_unwind_ask_only" and base_delta > 0.02 and net < 0.05:
            return (
                "inventory_unwind_ask_only",
                f"holding ask-only unwind while long inventory remains: base_delta={base_delta:.4f}, net_vs_hold=${net:.3f}",
            )

        if self.current_profile == "fee_guard_passive_80" and net < 0.10:
            return (
                "fee_guard_passive_80",
                f"holding fee guard until net edge recovers: net_vs_hold=${net:.3f}, window_fee=${window_fee:.3f}",
            )

        if not has_previous:
            return (
                self.current_profile,
                "startup evaluation baseline captured; waiting for a post-start "
                f"window before applying fee/trading guards: fee=${fee_usdc:.3f}, "
                f"trading_vs_hold=${trading:.3f}",
            )

        if failed >= 5 and net < 0.02:
            return (
                "fee_guard_passive_80",
                f"failed transactions are accumulating while net edge is weak: failed={failed}, net_vs_hold=${net:.3f}",
            )

        if net < -0.05 and (previous and prev_net < -0.02):
            return (
                "fee_guard_passive_80",
                f"two-hour net loss guard: prev_net=${prev_net:.3f}, net_vs_hold=${net:.3f}",
            )

        if net < 0 and window_fill and window_fee > max(0.015, window_trading * 0.8):
            return (
                "fee_guard_passive_80",
                f"fee drag exceeds fresh trading edge: window_fee=${window_fee:.3f}, window_trading=${window_trading:.3f}, window_tx={window_tx_count}",
            )

        if net < 0 and window_fill and fee_usdc > max(0.05, trading * 1.5):
            return (
                "fee_guard_passive_80",
                f"cumulative fees dominate gross edge: fee=${fee_usdc:.3f}, trading_vs_hold=${trading:.3f}",
            )

        if fill_detected and (trading < -0.45 or net < -0.50):
            return (
                "inventory_unwind_ask_only",
                f"hard loss guard: stop bids and unwind long inventory; net_vs_hold=${net:.3f}, trading_vs_hold=${trading:.3f}",
            )

        if not fill_detected and not window_fill:
            if self.current_profile.startswith("overnight_"):
                quote_pct_text = f"{quote_pct:.1%}" if quote_pct is not None else "unknown"
                return (
                    self.current_profile,
                    f"holding overnight profile despite no fills; quote_pct={quote_pct_text}, window_fee=${window_fee:.3f}",
                )
            if hour_index <= 1 and self.current_profile != "tight_micro_12_24":
                return (
                    "tight_micro_12_24",
                    "no inventory change after first evaluation, tightening with smaller size to discover flow",
                )
            if window_fee > 0.20 or fee_usdc > 0.75:
                return (
                    "resting_low_churn_16_30",
                    f"no fills and fee drag is rising: window_fee=${window_fee:.3f}, total_fee=${fee_usdc:.3f}",
                )
            return (
                "resting_low_churn_16_30",
                "still no inventory change, lowering churn and keeping a small near-mid resting probe",
            )

        if net > 0.05 and trading > 0.05:
            return (
                "winner_scale_24_40",
                f"fills appear profitable after fees: net_vs_hold=${net:.3f}, trading_vs_hold=${trading:.3f}",
            )

        if trading < -0.05 or net < -0.30:
            return (
                "defensive_35_55",
                f"fills or fees are negative: net_vs_hold=${net:.3f}, trading_vs_hold=${trading:.3f}",
            )

        return (
            self.current_profile,
            f"holding profile; fill_detected={fill_detected}, net_vs_hold=${net:.3f}, window_fee=${window_fee:.3f}",
        )

    def decide_watchdog_action(self, metrics: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
        status = metrics.get("status", {})
        market = metrics.get("market", {})
        live_status = metrics.get("archer_live_status")
        if not isinstance(live_status, dict) or "action" not in live_status:
            live_status = classify_archer_live_status(metrics)
        live_action = live_status.get("action")
        if live_action == "staleness_backoff":
            now = time.time()
            if now - self.last_staleness_clear_at < STALENESS_CLEAR_COOLDOWN_SECONDS:
                return (
                    "continue",
                    "staleness clear cooldown active: "
                    f"state={live_status.get('state')}, action={live_action}, "
                    f"cooldown_secs={STALENESS_CLEAR_COOLDOWN_SECONDS}",
                )
            self.staleness_strikes += 1
            if self.staleness_strikes < STALENESS_CLEAR_STRIKES:
                backoff = STALENESS_BACKOFF_SECONDS[min(self.staleness_strikes - 1, len(STALENESS_BACKOFF_SECONDS) - 1)]
                return (
                    "continue",
                    "staleness backoff before clear: "
                    f"strike={self.staleness_strikes}/{STALENESS_CLEAR_STRIKES}, "
                    f"retry_after_secs={backoff}, state={live_status.get('state')}",
                )
            self.staleness_strikes = 0
            self.last_staleness_clear_at = now
            return (
                "clear_book",
                "staleness strike threshold reached; clearing book without supervised stop: "
                f"state={live_status.get('state')}, action={live_action}, "
                f"cooldown_secs={STALENESS_CLEAR_COOLDOWN_SECONDS}",
            )
        self.staleness_strikes = 0
        if live_action == "stop_supervised":
            return (
                "stop",
                "fast watchdog: live status failover action requires supervised stop: "
                f"state={live_status.get('state')}, action={live_action}, "
                f"restart_policy={live_status.get('restart_policy')}, "
                f"alert={live_status.get('alert', {}).get('reason')}; "
                f"market_ok={market.get('command_ok')}, "
                f"status_ok={status.get('command_ok')}, "
                f"status_stale={status.get('stale')}, "
                f"status_error={status.get('command_error') or market.get('command_error')}"
            )

        tx = metrics.get("transactions", {})
        logs = metrics.get("logs", {}).get("counts", {})
        tx_count = int(tx.get("since_start_count") or 0)
        failed = int(tx.get("failed_count") or 0)
        stale = int(logs.get("price_feed_stale") or 0)
        rpc_429 = int(logs.get("rpc_429") or 0)
        tx_circuit = int(logs.get("tx_circuit_breaker") or 0)

        prev_tx_count = int(previous.get("transactions", {}).get("since_start_count") or 0) if previous else tx_count
        prev_failed = int(previous.get("transactions", {}).get("failed_count") or 0) if previous else failed
        prev_logs = previous.get("logs", {}).get("counts", {}) if previous else {}
        prev_stale = int(prev_logs.get("price_feed_stale") or 0) if previous else stale
        prev_rpc_429 = int(prev_logs.get("rpc_429") or 0) if previous else rpc_429
        prev_tx_circuit = int(prev_logs.get("tx_circuit_breaker") or 0) if previous else tx_circuit

        window_tx = max(0, tx_count - prev_tx_count)
        window_failed = max(0, failed - prev_failed)
        window_stale = max(0, stale - prev_stale)
        window_rpc_429 = max(0, rpc_429 - prev_rpc_429)
        window_tx_circuit = max(0, tx_circuit - prev_tx_circuit)

        stale_only_burst = (
            window_stale >= WATCHDOG_STALE_STOP
            and window_rpc_429 < WATCHDOG_RPC429_STOP
            and window_failed < WATCHDOG_FAILED_STOP
            and window_tx < WATCHDOG_TX_STOP
            and live_action in {None, "continue", "continue_degraded", "quote_reduce_only"}
        )
        if stale_only_burst:
            return "continue", None

        if (
            window_stale >= WATCHDOG_STALE_STOP
            or window_rpc_429 >= WATCHDOG_RPC429_STOP
            or window_failed >= WATCHDOG_FAILED_STOP
        ):
            return (
                "stop",
                "fast watchdog stop: "
                f"window_stale={window_stale}, window_rpc_429={window_rpc_429}, "
                f"window_failed={window_failed}, window_tx={window_tx}, "
                f"window_tx_circuit={window_tx_circuit}"
            )

        return "continue", None

    def decide_watchdog_stop(self, metrics: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Optional[str]:
        action, reason = self.decide_watchdog_action(metrics, previous)
        return reason if action == "stop" else None

    def switch_profile(self, next_profile: str, reason: str, metrics: Optional[Dict[str, Any]], event: str) -> None:
        if next_profile == "__stop__":
            self.log(f"Stopping adaptive run: {reason}")
            self.append_ledger(event, self.current_profile, reason, metrics=metrics, changed=False)
            self.stop_screen(ACTIVE_SCREEN)
            self.stop_bot_processes()
            self.clear_book()
            self.stop_requested = True
            return
        next_profile, reason = resolve_profile_approval(
            next_profile,
            reason,
            source=f"adaptive:{event}",
        )
        changed = next_profile != self.current_profile
        if changed:
            self.log(f"Switching profile {self.current_profile} -> {next_profile}: {reason}")
            self.current_profile = next_profile
            self.profile_entered_at = time.time()
            self.winner_high_water_net = None
            self.write_active_config(next_profile, reason)
            self.stop_screen(ACTIVE_SCREEN)
            self.stop_bot_processes()
            self.clear_book()
            self.start_bot()
        else:
            self.log(f"Keeping profile {self.current_profile}: {reason}")
            self.write_active_config(next_profile, reason)
        self.append_ledger(event, self.current_profile, reason, metrics=metrics, changed=changed)

    def handle_signal(self, *_: object) -> None:
        self.stop_requested = True
        self.log("Stop requested")

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"Starting adaptive Archer 12h run_id={self.run_id} duration_seconds={self.duration_seconds} evaluation_seconds={self.evaluation_seconds}")
        self.log(f"Logs: {self.run_dir}")
        self.write_controller_heartbeat("start", self.initial_reason)
        self.write_active_config(self.current_profile, self.initial_reason)

        for screen in OLD_SCREENS + [ACTIVE_SCREEN]:
            self.stop_screen(screen)
        self.stop_old_controller_processes()
        self.stop_bot_processes()
        self.clear_book()
        self.snapshot("00_before")
        self.append_ledger("start", self.current_profile, self.initial_reason, changed=True)
        self.start_bot()
        self.snapshot("01_started")
        watchdog_previous: Optional[Dict[str, Any]] = None
        try:
            metrics = self.collect_metrics()
            watchdog_previous = metrics
            next_profile, reason = self.decide_next_profile(metrics, None, 0)
            self.switch_profile(next_profile, reason, metrics=metrics, event="initial_evaluation")
        except Exception as exc:  # noqa: BLE001
            self.log(f"Initial adaptive evaluation failed; continuing with initial profile: {exc}")

        end_at = time.time() + self.duration_seconds
        hour_index = 0
        while not self.stop_requested and time.time() < end_at:
            remaining = max(0.0, end_at - time.time())
            if remaining <= self.evaluation_seconds:
                self.log(f"Sleeping {int(remaining)}s until run end")
                if remaining > 0:
                    time.sleep(remaining)
                break
            sleep_until = time.time() + self.evaluation_seconds
            self.log(f"Sleeping {int(self.evaluation_seconds)}s before next adaptive evaluation")
            while not self.stop_requested and time.time() < sleep_until and time.time() < end_at:
                self.write_controller_heartbeat("watchdog_sleep", f"hour_index={hour_index}")
                sleep_for = min(WATCHDOG_SECONDS, sleep_until - time.time(), end_at - time.time())
                if sleep_for > 0:
                    time.sleep(sleep_for)
                if self.stop_requested or time.time() >= sleep_until or time.time() >= end_at:
                    break
                try:
                    watchdog_metrics = self.collect_metrics()
                    watchdog_action, watchdog_reason = self.decide_watchdog_action(watchdog_metrics, watchdog_previous)
                    watchdog_previous = watchdog_metrics
                    if watchdog_action == "stop" and watchdog_reason:
                        self.switch_profile("__stop__", watchdog_reason, metrics=watchdog_metrics, event=f"watchdog_hour_{hour_index}")
                        break
                    if watchdog_action == "clear_book" and watchdog_reason:
                        self.log(watchdog_reason)
                        self.append_ledger(
                            f"watchdog_hour_{hour_index}",
                            self.current_profile,
                            watchdog_reason,
                            metrics=watchdog_metrics,
                            changed=False,
                        )
                        self.clear_book()
                        break
                except Exception as exc:  # noqa: BLE001
                    self.log(f"Fast watchdog metrics failed; stopping to avoid blind live trading: {exc}")
                    self.switch_profile(
                        "__stop__",
                        f"fast watchdog metrics collection failed: {exc}",
                        metrics=None,
                        event=f"watchdog_hour_{hour_index}",
                    )
                    break
            if self.stop_requested or time.time() >= end_at:
                break
            hour_index += 1
            self.write_controller_heartbeat("hourly_evaluation", f"hour_index={hour_index}")
            previous = self.load_last_eval_metrics()
            metrics = self.collect_metrics()
            watchdog_previous = metrics
            next_profile, reason = self.decide_next_profile(metrics, previous, hour_index)
            self.switch_profile(next_profile, reason, metrics=metrics, event=f"hour_{hour_index}_evaluation")

        self.snapshot("98_before_stop")
        self.write_controller_heartbeat("final_clear", "adaptive run ending")
        self.stop_screen(ACTIVE_SCREEN)
        self.stop_bot_processes()
        self.clear_book()
        final_metrics = None
        try:
            final_metrics = self.collect_metrics()
        except Exception as exc:  # noqa: BLE001
            self.log(f"Final metrics collection failed: {exc}")
        self.snapshot("99_after_clear")
        self.append_ledger("complete", self.current_profile, "adaptive 12h controller completed; book cleared", metrics=final_metrics)
        self.write_controller_heartbeat("complete", "adaptive 12h controller completed; book cleared")
        self.log("Adaptive Archer run complete. Book cleared; funds remain in MakerBook free balances unless withdrawn separately.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an adaptive 12-hour Archer live strategy")
    parser.add_argument("--run-id", default=dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--duration-seconds", type=int, default=int(os.environ.get("ARCHER_ADAPTIVE_DURATION_SECONDS", "43200")))
    parser.add_argument("--evaluation-seconds", type=int, default=int(os.environ.get("ARCHER_ADAPTIVE_EVALUATION_SECONDS", "3600")))
    parser.add_argument("--initial-profile", default=os.environ.get("ARCHER_ADAPTIVE_INITIAL_PROFILE", "flow_probe_20_32"))
    parser.add_argument(
        "--initial-reason",
        default=os.environ.get("ARCHER_ADAPTIVE_INITIAL_REASON", "initial adaptive flow probe"),
    )
    args = parser.parse_args()
    try:
        validate_start_args(
            args.run_id,
            args.duration_seconds,
            args.evaluation_seconds,
            args.initial_profile,
        )
    except ValueError as exc:
        parser.error(str(exc))
    initial_profile, initial_reason = resolve_profile_approval(
        args.initial_profile,
        args.initial_reason,
        source="adaptive:start",
    )
    AdaptiveController(
        args.run_id,
        args.duration_seconds,
        args.evaluation_seconds,
        initial_profile,
        initial_reason,
    ).run()


if __name__ == "__main__":
    main()
