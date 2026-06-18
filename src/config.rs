use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::Parser;
use serde::Deserialize;

use crate::tx::{PriorityFeeConfig, PriorityFeeMode, TxBudgetConfig};

#[derive(Parser, Debug)]
#[command(
    name = "archer-market-maker",
    about = "A simple market maker for Archer Exchange on Solana"
)]
pub enum Cli {
    /// Start the market maker
    Run {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
        /// Force shadow mode for this run, overriding config.
        #[arg(long, default_value_t = false)]
        shadow: bool,
        /// Force live transaction submission for this run. Requires ARCHER_ENABLE_LIVE_TRADING=true.
        #[arg(long, default_value_t = false)]
        live: bool,
    },
    /// Print market metadata without requiring a maker book
    Market {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
    },
    /// Preview generated quotes for a hypothetical balance, without sending transactions
    Preview {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
        /// Reference mid price in quote units per base unit
        #[arg(long)]
        mid: f64,
        /// Hypothetical available base amount
        #[arg(long)]
        base: f64,
        /// Hypothetical available quote amount
        #[arg(long)]
        quote: f64,
        /// Optional realized volatility bps used by the spread scaler
        #[arg(long, default_value_t = 0.0)]
        vol_bps: f64,
        /// Optional market-intel spread add, in bps, for quote preview.
        #[arg(long, default_value_t = 0.0)]
        intel_spread_add_bps: f64,
        /// Optional market-intel global size multiplier for quote preview.
        #[arg(long, default_value_t = 1.0)]
        intel_size_multiplier: f64,
        /// Optional market-intel bid size multiplier for quote preview.
        #[arg(long, default_value_t = 1.0)]
        intel_bid_size_multiplier: f64,
        /// Optional market-intel ask size multiplier for quote preview.
        #[arg(long, default_value_t = 1.0)]
        intel_ask_size_multiplier: f64,
    },
    /// Simulate a market-intel quote policy fixture into an Archer MakerBook update
    SimulatePolicy {
        /// JSON fixture containing policy, current state, market metadata, and simulator caps
        #[arg(short, long)]
        fixture: PathBuf,
    },
    /// Initialize your maker book on-chain (one-time)
    Init {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
    },
    /// Deposit tokens into your maker book
    Deposit {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
        #[arg(long)]
        base: f64,
        #[arg(long)]
        quote: f64,
    },
    /// Withdraw all funds from your maker book
    Withdraw {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
    },
    /// Emergency: clear all orders immediately
    Kill {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
    },
    /// Print current on-chain maker book status
    Status {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
    },
    /// Set the maker book's expiry_in_slots (0 disables the aggregator's expiry-skip check)
    SetExpiry {
        #[arg(short, long, default_value = "config/default.toml")]
        config: PathBuf,
        #[arg(long)]
        slots: u64,
    },
}

#[derive(Debug, Deserialize, Clone)]
pub struct MMConfig {
    pub market: MarketSettings,
    pub connection: ConnectionSettings,
    pub feed: FeedSettings,
    pub strategy: StrategySettings,
    #[serde(default)]
    pub risk: RiskSettings,
    pub execution: ExecutionSettings,
    pub monitoring: MonitoringSettings,
}

#[derive(Debug, Deserialize, Clone)]
pub struct MarketSettings {
    pub market_pubkey: String,
    pub maker_keypair_path: String,
}

#[derive(Debug, Deserialize, Clone)]
pub struct ConnectionSettings {
    pub rpc_url: String,
    #[serde(default)]
    pub shared_blockhash_url: Option<String>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct FeedSettings {
    pub binance_symbol: String,
    #[serde(default)]
    pub cross_symbol: String,
    #[serde(default = "default_binance_ws")]
    pub binance_ws_url: String,
    #[serde(default)]
    pub market_intel_signal_url: Option<String>,
    #[serde(default = "default_market_intel_poll_ms")]
    pub market_intel_poll_ms: u64,
    #[serde(default = "default_staleness_ms")]
    pub staleness_timeout_ms: u64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct StrategySettings {
    pub spread_levels_bps: Vec<f64>,
    #[serde(default = "default_inventory_pct")]
    pub inventory_pct: f64,
    #[serde(default = "default_vol_window")]
    pub vol_window: usize,
    #[serde(default = "default_vol_baseline_bps")]
    pub vol_baseline_bps: f64,
    #[serde(default = "default_vol_max_multiplier")]
    pub vol_max_multiplier: f64,
    #[serde(default = "default_intel_spread_add_multiplier")]
    pub intel_spread_add_multiplier: f64,
    #[serde(default = "default_max_intel_spread_add_bps")]
    pub max_intel_spread_add_bps: f64,
    #[serde(default = "default_max_intel_spread_tighten_bps")]
    pub max_intel_spread_tighten_bps: f64,
    #[serde(default = "default_max_intel_side_spread_add_bps")]
    pub max_intel_side_spread_add_bps: f64,
    #[serde(default = "default_min_effective_spread_bps")]
    pub min_effective_spread_bps: f64,
    #[serde(default = "default_min_net_edge_bps")]
    pub min_net_edge_bps: f64,
    #[serde(default = "default_toxicity_buffer_bps")]
    pub toxicity_buffer_bps: f64,
    #[serde(default = "default_min_intel_size_multiplier")]
    pub min_intel_size_multiplier: f64,
    #[serde(default = "default_post_fill_cooldown_ms")]
    pub post_fill_cooldown_ms: u64,
    #[serde(default = "default_post_fill_side_size_multiplier")]
    pub post_fill_side_size_multiplier: f64,
    #[serde(default = "default_post_fill_markout_check_ms")]
    pub post_fill_markout_check_ms: u64,
    #[serde(default = "default_post_fill_adverse_markout_bps")]
    pub post_fill_adverse_markout_bps: f64,
    #[serde(default = "default_post_fill_adverse_cooldown_ms")]
    pub post_fill_adverse_cooldown_ms: u64,
}

#[derive(Debug, Deserialize, Clone)]
pub struct RiskSettings {
    #[serde(default = "default_min_quote_notional")]
    pub min_quote_notional: f64,
    #[serde(default = "default_max_quote_notional_per_level")]
    pub max_quote_notional_per_level: f64,
    #[serde(default = "default_max_total_quote_notional")]
    pub max_total_quote_notional: f64,
    #[serde(default = "default_min_base_reserve_pct")]
    pub min_base_reserve_pct: f64,
    #[serde(default = "default_min_quote_reserve_pct")]
    pub min_quote_reserve_pct: f64,
}

impl Default for RiskSettings {
    fn default() -> Self {
        Self {
            min_quote_notional: default_min_quote_notional(),
            max_quote_notional_per_level: default_max_quote_notional_per_level(),
            max_total_quote_notional: default_max_total_quote_notional(),
            min_base_reserve_pct: default_min_base_reserve_pct(),
            min_quote_reserve_pct: default_min_quote_reserve_pct(),
        }
    }
}

#[derive(Debug, Deserialize, Clone)]
pub struct ExecutionSettings {
    #[serde(default = "default_heartbeat_ms")]
    pub heartbeat_interval_ms: u64,
    #[serde(default)]
    pub priority_fee_mode: PriorityFeeMode,
    #[serde(default = "default_priority_fee")]
    pub priority_fee_microlamports: u64,
    #[serde(default = "default_priority_fee_min")]
    pub priority_fee_min_microlamports: u64,
    #[serde(default = "default_priority_fee_max")]
    pub priority_fee_max_microlamports: u64,
    #[serde(default = "default_priority_fee_percentile")]
    pub priority_fee_percentile: u8,
    #[serde(default = "default_priority_fee_cache_ms")]
    pub priority_fee_cache_ms: u64,
    #[serde(default = "default_priority_fee_emergency_multiplier")]
    pub priority_fee_emergency_multiplier: u64,
    #[serde(default = "default_min_mid_update_interval_ms")]
    pub min_mid_update_interval_ms: u64,
    #[serde(default = "default_min_mid_update_ticks")]
    pub min_mid_update_ticks: u64,
    #[serde(default = "default_min_full_refresh_interval_ms")]
    pub min_full_refresh_interval_ms: u64,
    #[serde(default = "default_max_tx_per_minute")]
    pub max_tx_per_minute: u64,
    #[serde(default = "default_max_update_tx_per_10min")]
    pub max_update_tx_per_10min: u64,
    #[serde(default = "default_max_clear_book_per_5min")]
    pub max_clear_book_per_5min: u64,
    #[serde(default = "default_min_clear_book_interval_ms")]
    pub min_clear_book_interval_ms: u64,
    #[serde(default = "default_maker_book_poll_interval_ms")]
    pub maker_book_poll_interval_ms: u64,
    #[serde(default)]
    pub shadow_mode: bool,
}

impl ExecutionSettings {
    pub fn priority_fee_config(&self) -> PriorityFeeConfig {
        PriorityFeeConfig {
            mode: self.priority_fee_mode,
            fixed_fee_microlamports: self.priority_fee_microlamports,
            min_fee_microlamports: self.priority_fee_min_microlamports,
            max_fee_microlamports: self.priority_fee_max_microlamports,
            fallback_fee_microlamports: self.priority_fee_microlamports,
            percentile: self.priority_fee_percentile,
            cache_ttl: std::time::Duration::from_millis(self.priority_fee_cache_ms),
            emergency_multiplier: self.priority_fee_emergency_multiplier,
        }
    }

    pub fn tx_budget_config(&self) -> TxBudgetConfig {
        TxBudgetConfig {
            max_tx_per_minute: self.max_tx_per_minute,
            max_update_tx_per_10min: self.max_update_tx_per_10min,
            max_clear_book_per_5min: self.max_clear_book_per_5min,
            min_clear_book_interval: std::time::Duration::from_millis(
                self.min_clear_book_interval_ms,
            ),
        }
    }
}

#[derive(Debug, Deserialize, Clone)]
pub struct MonitoringSettings {
    #[serde(default = "default_log_level")]
    pub log_level: String,
}

pub fn load_config(path: &Path) -> Result<MMConfig> {
    let contents =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let config: MMConfig =
        toml::from_str(&contents).with_context(|| format!("parsing {}", path.display()))?;
    validate_config(&config)?;
    Ok(config)
}

fn validate_config(c: &MMConfig) -> Result<()> {
    anyhow::ensure!(!c.market.market_pubkey.is_empty(), "market_pubkey required");
    anyhow::ensure!(
        !c.market.maker_keypair_path.is_empty(),
        "maker_keypair_path required"
    );
    anyhow::ensure!(!c.connection.rpc_url.is_empty(), "rpc_url required");
    anyhow::ensure!(!c.feed.binance_symbol.is_empty(), "binance_symbol required");
    anyhow::ensure!(
        !c.strategy.spread_levels_bps.is_empty(),
        "need at least 1 spread level"
    );
    anyhow::ensure!(
        c.strategy.spread_levels_bps.len() <= 16,
        "max 16 levels per side"
    );
    anyhow::ensure!(
        c.strategy.spread_levels_bps.iter().all(|&s| s > 0.0),
        "all spread levels must be positive"
    );
    anyhow::ensure!(
        c.strategy.inventory_pct > 0.0 && c.strategy.inventory_pct <= 100.0,
        "inventory_pct must be between 0 and 100"
    );
    anyhow::ensure!(c.strategy.vol_window >= 2, "vol_window must be >= 2");
    anyhow::ensure!(
        c.strategy.vol_baseline_bps > 0.0,
        "vol_baseline_bps must be positive"
    );
    anyhow::ensure!(
        c.strategy.vol_max_multiplier >= 1.0,
        "vol_max_multiplier must be >= 1.0"
    );
    anyhow::ensure!(
        c.strategy.intel_spread_add_multiplier >= 0.0,
        "intel_spread_add_multiplier must be >= 0"
    );
    anyhow::ensure!(
        c.strategy.max_intel_spread_add_bps >= 0.0,
        "max_intel_spread_add_bps must be >= 0"
    );
    anyhow::ensure!(
        c.strategy.max_intel_spread_tighten_bps >= 0.0,
        "max_intel_spread_tighten_bps must be >= 0"
    );
    anyhow::ensure!(
        c.strategy.max_intel_side_spread_add_bps >= 0.0,
        "max_intel_side_spread_add_bps must be >= 0"
    );
    anyhow::ensure!(
        c.strategy.min_effective_spread_bps >= 0.0,
        "min_effective_spread_bps must be >= 0"
    );
    anyhow::ensure!(
        c.strategy.min_net_edge_bps >= 0.0,
        "min_net_edge_bps must be >= 0"
    );
    anyhow::ensure!(
        c.strategy.toxicity_buffer_bps >= 0.0,
        "toxicity_buffer_bps must be >= 0"
    );
    anyhow::ensure!(
        (0.0..=1.0).contains(&c.strategy.min_intel_size_multiplier),
        "min_intel_size_multiplier must be between 0 and 1"
    );
    anyhow::ensure!(
        (0.0..=1.0).contains(&c.strategy.post_fill_side_size_multiplier),
        "post_fill_side_size_multiplier must be between 0 and 1"
    );
    anyhow::ensure!(
        c.strategy.post_fill_adverse_markout_bps >= 0.0,
        "post_fill_adverse_markout_bps must be >= 0"
    );
    anyhow::ensure!(
        c.risk.min_quote_notional >= 0.0,
        "min_quote_notional must be >= 0"
    );
    anyhow::ensure!(
        c.risk.max_quote_notional_per_level > 0.0,
        "max_quote_notional_per_level must be positive"
    );
    anyhow::ensure!(
        c.risk.max_total_quote_notional > 0.0,
        "max_total_quote_notional must be positive"
    );
    anyhow::ensure!(
        (0.0..100.0).contains(&c.risk.min_base_reserve_pct),
        "min_base_reserve_pct must be >= 0 and < 100"
    );
    anyhow::ensure!(
        (0.0..100.0).contains(&c.risk.min_quote_reserve_pct),
        "min_quote_reserve_pct must be >= 0 and < 100"
    );
    anyhow::ensure!(
        c.execution.priority_fee_percentile <= 100,
        "priority_fee_percentile must be <= 100"
    );
    anyhow::ensure!(
        c.execution.priority_fee_max_microlamports >= c.execution.priority_fee_min_microlamports,
        "priority_fee_max_microlamports must be >= priority_fee_min_microlamports"
    );
    anyhow::ensure!(
        c.execution.priority_fee_emergency_multiplier >= 1,
        "priority_fee_emergency_multiplier must be >= 1"
    );
    anyhow::ensure!(
        c.execution.max_tx_per_minute > 0,
        "max_tx_per_minute must be positive"
    );
    anyhow::ensure!(
        c.execution.max_update_tx_per_10min > 0,
        "max_update_tx_per_10min must be positive"
    );
    anyhow::ensure!(
        c.execution.min_full_refresh_interval_ms >= 1_000,
        "min_full_refresh_interval_ms must be at least 1000"
    );
    anyhow::ensure!(
        c.execution.max_clear_book_per_5min > 0,
        "max_clear_book_per_5min must be positive"
    );
    anyhow::ensure!(
        c.execution.maker_book_poll_interval_ms >= 1_000,
        "maker_book_poll_interval_ms must be at least 1000"
    );
    Ok(())
}

fn default_binance_ws() -> String {
    "wss://stream.binance.com:9443/ws".into()
}
fn default_staleness_ms() -> u64 {
    5000
}
fn default_market_intel_poll_ms() -> u64 {
    2_000
}
fn default_inventory_pct() -> f64 {
    80.0
}
fn default_vol_window() -> usize {
    300
}
fn default_vol_baseline_bps() -> f64 {
    5.0
}
fn default_vol_max_multiplier() -> f64 {
    5.0
}
fn default_intel_spread_add_multiplier() -> f64 {
    1.0
}
fn default_max_intel_spread_add_bps() -> f64 {
    80.0
}
fn default_max_intel_spread_tighten_bps() -> f64 {
    0.0
}
fn default_max_intel_side_spread_add_bps() -> f64 {
    40.0
}
fn default_min_effective_spread_bps() -> f64 {
    62.0
}
fn default_min_net_edge_bps() -> f64 {
    0.0
}
fn default_toxicity_buffer_bps() -> f64 {
    0.0
}
fn default_min_intel_size_multiplier() -> f64 {
    0.20
}
fn default_post_fill_cooldown_ms() -> u64 {
    900_000
}
fn default_post_fill_side_size_multiplier() -> f64 {
    0.0
}
fn default_post_fill_markout_check_ms() -> u64 {
    900_000
}
fn default_post_fill_adverse_markout_bps() -> f64 {
    12.0
}
fn default_post_fill_adverse_cooldown_ms() -> u64 {
    3_600_000
}
fn default_min_quote_notional() -> f64 {
    5.0
}
fn default_max_quote_notional_per_level() -> f64 {
    25.0
}
fn default_max_total_quote_notional() -> f64 {
    200.0
}
fn default_min_base_reserve_pct() -> f64 {
    20.0
}
fn default_min_quote_reserve_pct() -> f64 {
    20.0
}
fn default_heartbeat_ms() -> u64 {
    100
}
fn default_priority_fee() -> u64 {
    100
}
fn default_priority_fee_min() -> u64 {
    0
}
fn default_priority_fee_max() -> u64 {
    5_000
}
fn default_priority_fee_percentile() -> u8 {
    50
}
fn default_priority_fee_cache_ms() -> u64 {
    10_000
}
fn default_priority_fee_emergency_multiplier() -> u64 {
    4
}
fn default_min_mid_update_interval_ms() -> u64 {
    5_000
}
fn default_min_mid_update_ticks() -> u64 {
    25
}
fn default_min_full_refresh_interval_ms() -> u64 {
    600_000
}
fn default_max_tx_per_minute() -> u64 {
    20
}
fn default_max_update_tx_per_10min() -> u64 {
    6
}
fn default_max_clear_book_per_5min() -> u64 {
    2
}
fn default_min_clear_book_interval_ms() -> u64 {
    30_000
}
fn default_maker_book_poll_interval_ms() -> u64 {
    2_000
}
fn default_log_level() -> String {
    "info".into()
}

pub fn resolve_path(s: &str) -> PathBuf {
    if let Some(rest) = s.strip_prefix("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return PathBuf::from(format!("{}/{}", home, rest));
        }
    }
    PathBuf::from(s)
}
