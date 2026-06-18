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

from dashboard.server import DashboardState, iso, load_simple_toml, pubkey_from_keypair_json, run_cmd


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
WATCHDOG_SECONDS = int(os.environ.get("ARCHER_WATCHDOG_SECONDS", "30"))
WATCHDOG_STALE_STOP = int(os.environ.get("ARCHER_WATCHDOG_STALE_STOP", "2"))
WATCHDOG_RPC429_STOP = int(os.environ.get("ARCHER_WATCHDOG_RPC429_STOP", "6"))
WATCHDOG_FAILED_STOP = int(os.environ.get("ARCHER_WATCHDOG_FAILED_STOP", "5"))
WATCHDOG_TX_STOP = int(os.environ.get("ARCHER_WATCHDOG_TX_STOP", "80"))


PROFIT_GUARD_DEFAULTS: Dict[str, Any] = {
    "intel_spread_add_multiplier": 1.0,
    "max_intel_spread_add_bps": 80.0,
    "max_intel_spread_tighten_bps": 0.0,
    "max_intel_side_spread_add_bps": 40.0,
    "min_effective_spread_bps": 62.0,
    "min_net_edge_bps": 0.0,
    "toxicity_buffer_bps": 0.0,
    "min_intel_size_multiplier": 0.20,
    "post_fill_cooldown_ms": 900000,
    "post_fill_side_size_multiplier": 0.0,
    "post_fill_markout_check_ms": 900000,
    "post_fill_adverse_markout_bps": 12.0,
    "post_fill_adverse_cooldown_ms": 3600000,
}


FILL_TOXICITY_MIN_NOTIONAL = float(os.environ.get("ARCHER_FILL_TOXICITY_MIN_NOTIONAL", "8.0"))
FILL_TOXICITY_MIN_LOSS_USDC = float(os.environ.get("ARCHER_FILL_TOXICITY_MIN_LOSS_USDC", "0.025"))
FILL_TOXICITY_FLOOR_BUFFER_BPS = float(os.environ.get("ARCHER_FILL_TOXICITY_FLOOR_BUFFER_BPS", "8.0"))
FILL_TOXICITY_STOP_FLOOR_BPS = float(os.environ.get("ARCHER_FILL_TOXICITY_STOP_FLOOR_BPS", "95.0"))


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
        "min_effective_spread_bps": 62.0,
        "min_intel_size_multiplier": 0.20,
        "post_fill_cooldown_ms": 900000,
        "post_fill_side_size_multiplier": 0.0,
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
        "post_fill_side_size_multiplier": 0.0,
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
        "description": "Fee guard: keep quotes live but slow repricing when no fills justify churn.",
        "spread_levels_bps": [16.0, 30.0],
        "inventory_pct": 20.0,
        "max_quote_notional_per_level": 12.0,
        "max_total_quote_notional": 60.0,
        "min_quote_notional": 6.0,
        "min_base_reserve_pct": 30.0,
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
        "min_quote_notional": 8.0,
        "min_base_reserve_pct": 65.0,
        "min_quote_reserve_pct": 20.0,
        "staleness_timeout_ms": 30000,
        "heartbeat_interval_ms": 1000,
        "min_mid_update_interval_ms": 900000,
        "min_mid_update_ticks": 1000,
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
        self.commands_log = self.run_dir / "commands.log"
        self.bot_log = self.run_dir / "bot.log"
        self.config_base = load_simple_toml(SOURCE_CONFIG)
        maker_keypair_path = os.environ.get("ARCHER_MAKER_KEYPAIR_PATH")
        if maker_keypair_path:
            self.config_base.setdefault("market", {})["maker_keypair_path"] = maker_keypair_path
        self.rpc_url = str(self.config_base["connection"]["rpc_url"])
        self.wallet_pubkey = self.discover_wallet_pubkey()
        if initial_profile not in PROFILES:
            raise ValueError(f"unknown initial profile {initial_profile}")
        self.current_profile = initial_profile
        self.initial_reason = initial_reason
        self.stop_requested = False
        self.profile_entered_at = time.time()

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
post_fill_side_size_multiplier = {float(profile.get('post_fill_side_size_multiplier', self.config_base['strategy'].get('post_fill_side_size_multiplier', 0.0))):.2f}
post_fill_markout_check_ms = {int(profile.get('post_fill_markout_check_ms', self.config_base['strategy'].get('post_fill_markout_check_ms', 900000)))}
post_fill_adverse_markout_bps = {float(profile.get('post_fill_adverse_markout_bps', self.config_base['strategy'].get('post_fill_adverse_markout_bps', 12.0))):.2f}
post_fill_adverse_cooldown_ms = {int(profile.get('post_fill_adverse_cooldown_ms', self.config_base['strategy'].get('post_fill_adverse_cooldown_ms', 3600000)))}

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
max_tx_per_minute = {int(execution.get('max_tx_per_minute', 20))}
max_update_tx_per_10min = {int(profile.get('max_update_tx_per_10min', execution.get('max_update_tx_per_10min', 6)))}
max_clear_book_per_5min = {int(execution.get('max_clear_book_per_5min', 2))}
min_clear_book_interval_ms = {int(execution.get('min_clear_book_interval_ms', 30000))}
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

    def compact_metrics(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        pnl = metrics.get("pnl", {})
        tx = metrics.get("transactions", {})
        status = metrics.get("status", {})
        market = metrics.get("market", {})
        logs = metrics.get("logs", {}).get("counts", {})
        return {
            "mid_price": metrics.get("mid_price"),
            "market_command_ok": market.get("command_ok"),
            "status_command_ok": status.get("command_ok"),
            "status_stale": status.get("stale"),
            "status_source": status.get("source"),
            "bid_levels": status.get("bid_levels"),
            "ask_levels": status.get("ask_levels"),
            "base_total": status.get("base_total"),
            "quote_total": status.get("quote_total"),
            "base_delta": pnl.get("base_delta"),
            "quote_delta": pnl.get("quote_delta"),
            "fill_detected": pnl.get("fill_detected"),
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
            metrics = state.collect_cached(force=True, record=True)
            last_metrics = metrics
            market = metrics.get("market", {})
            status = metrics.get("status", {})
            if market.get("command_ok") and status.get("command_ok") and not status.get("stale"):
                return metrics
            self.log(
                "Live metrics collection returned non-live status "
                f"(attempt {attempt}/3): "
                f"market_ok={market.get('command_ok')}, "
                f"status_ok={status.get('command_ok')}, "
                f"status_stale={status.get('stale')}, "
                f"status_source={status.get('source')}, "
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

    def decide_next_profile(self, metrics: Dict[str, Any], previous: Optional[Dict[str, Any]], hour_index: int) -> Tuple[str, str]:
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

        if not market.get("command_ok") or not status.get("command_ok") or status.get("stale"):
            return (
                "__stop__",
                "live status unavailable; refusing to trade on stale or snapshot metrics: "
                f"market_ok={market.get('command_ok')}, "
                f"status_ok={status.get('command_ok')}, "
                f"status_stale={status.get('stale')}, "
                f"status_source={status.get('source')}, "
                f"status_error={status.get('command_error') or market.get('command_error')}",
            )

        prev_fee = float(previous.get("fee_usdc") or 0.0) if previous else 0.0
        window_fee = max(0.0, fee_usdc - prev_fee)
        prev_net = float(previous.get("net_trading_vs_hold_usdc") or 0.0) if previous else 0.0
        prev_trading = float(previous.get("trading_vs_hold_usdc") or 0.0) if previous else 0.0
        prev_failed = int(previous.get("failed_count") or 0) if previous else 0
        prev_tx_count = int(previous.get("tx_count") or 0) if previous else 0
        prev_stale = int(previous.get("price_feed_stale") or 0) if previous else 0
        prev_rpc_429 = int(previous.get("rpc_429") or 0) if previous else 0
        prev_base_delta = float(previous.get("base_delta") or 0.0) if previous else 0.0
        prev_quote_delta = float(previous.get("quote_delta") or 0.0) if previous else 0.0
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

        if window_stale >= 4 or window_rpc_429 >= 6 or window_failed >= 5:
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

        fill_toxicity = self.fill_toxicity_decision(
            window_fill,
            window_net,
            window_quote_delta,
            metrics,
        )
        if fill_toxicity is not None:
            return fill_toxicity

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

        if net < 0 and window_fee > max(0.015, window_trading * 0.8):
            return (
                "fee_guard_passive_80",
                f"fee drag exceeds fresh trading edge: window_fee=${window_fee:.3f}, window_trading=${window_trading:.3f}, window_tx={window_tx_count}",
            )

        if net < 0 and fee_usdc > max(0.05, trading * 1.5):
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

    def decide_watchdog_stop(self, metrics: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Optional[str]:
        status = metrics.get("status", {})
        market = metrics.get("market", {})
        if not market.get("command_ok") or not status.get("command_ok") or status.get("stale"):
            return (
                "fast watchdog: live status unavailable; stopping instead of trading blind: "
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

        if (
            window_tx_circuit > 0
            or window_stale >= WATCHDOG_STALE_STOP
            or window_rpc_429 >= WATCHDOG_RPC429_STOP
            or window_failed >= WATCHDOG_FAILED_STOP
            or window_tx >= WATCHDOG_TX_STOP
        ):
            return (
                "fast watchdog stop: "
                f"window_stale={window_stale}, window_rpc_429={window_rpc_429}, "
                f"window_failed={window_failed}, window_tx={window_tx}, "
                f"window_tx_circuit={window_tx_circuit}"
            )

        return None

    def switch_profile(self, next_profile: str, reason: str, metrics: Optional[Dict[str, Any]], event: str) -> None:
        if next_profile == "__stop__":
            self.log(f"Stopping adaptive run: {reason}")
            self.append_ledger(event, self.current_profile, reason, metrics=metrics, changed=False)
            self.stop_screen(ACTIVE_SCREEN)
            self.stop_bot_processes()
            self.clear_book()
            self.stop_requested = True
            return
        changed = next_profile != self.current_profile
        if changed:
            self.log(f"Switching profile {self.current_profile} -> {next_profile}: {reason}")
            self.current_profile = next_profile
            self.profile_entered_at = time.time()
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
                sleep_for = min(WATCHDOG_SECONDS, sleep_until - time.time(), end_at - time.time())
                if sleep_for > 0:
                    time.sleep(sleep_for)
                if self.stop_requested or time.time() >= sleep_until or time.time() >= end_at:
                    break
                try:
                    watchdog_metrics = self.collect_metrics()
                    stop_reason = self.decide_watchdog_stop(watchdog_metrics, watchdog_previous)
                    watchdog_previous = watchdog_metrics
                    if stop_reason:
                        self.switch_profile("__stop__", stop_reason, metrics=watchdog_metrics, event=f"watchdog_hour_{hour_index}")
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
            previous = self.load_last_eval_metrics()
            metrics = self.collect_metrics()
            watchdog_previous = metrics
            next_profile, reason = self.decide_next_profile(metrics, previous, hour_index)
            self.switch_profile(next_profile, reason, metrics=metrics, event=f"hour_{hour_index}_evaluation")

        self.snapshot("98_before_stop")
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
    AdaptiveController(
        args.run_id,
        args.duration_seconds,
        args.evaluation_seconds,
        args.initial_profile,
        args.initial_reason,
    ).run()


if __name__ == "__main__":
    main()
