use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use crate::archer::{
    accounts::{active_ask_levels, active_bid_levels, maker_balances},
    config::MarketConfig,
    math::{BookUpdate, Quote, TwoSidedQuote, base_lots_to_amount, build_book_update},
    types::{MARKET_STATE_DISCRIMINATOR, MAX_LEVELS, MakerBook, MarketStateHeader},
};
use crate::quote_policy::{PolicyMode, QuotePolicy, SidePolicy};
use solana_sdk::pubkey::Pubkey;

#[derive(Debug, Clone)]
pub struct SimulationInput {
    pub policy: QuotePolicy,
    pub state: CurrentState,
    pub market_config: MarketConfig,
    pub config: SimulatorConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SimulationFixture {
    #[serde(default)]
    pub policy: Option<QuotePolicy>,
    pub state: CurrentState,
    pub market: MarketConfigFixture,
    #[serde(default)]
    pub config: SimulatorConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MarketConfigFixture {
    #[serde(default)]
    pub market_pubkey: Option<String>,
    #[serde(default)]
    pub base_mint: Option<String>,
    #[serde(default)]
    pub quote_mint: Option<String>,
    pub base_atoms_per_base_lot: u64,
    pub quote_atoms_per_quote_lot: u64,
    pub tick_size_in_quote_atoms_per_base_unit: u64,
    pub raw_base_units_per_base_unit: u64,
    #[serde(default)]
    pub maker_fee_ppm: i32,
    #[serde(default)]
    pub taker_fee_ppm: i32,
    pub base_decimals: u8,
    pub quote_decimals: u8,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CurrentState {
    pub mid_price: f64,
    #[serde(default)]
    pub cached_mid_ticks: u64,
    pub base_available: f64,
    pub quote_available: f64,
    #[serde(default)]
    pub maker_book: Option<CurrentMakerBookState>,
    #[serde(default)]
    pub wallet: Option<WalletBalanceContext>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct CurrentMakerBookState {
    pub sequence_number: u64,
    pub mid_price_ticks: u64,
    pub active_bid_levels: usize,
    pub active_ask_levels: usize,
    pub base_free: f64,
    pub base_locked: f64,
    pub quote_free: f64,
    pub quote_locked: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WalletBalanceContext {
    pub source: String,
    #[serde(default)]
    pub native_sol: Option<f64>,
    #[serde(default)]
    pub base: Option<f64>,
    #[serde(default)]
    pub quote: Option<f64>,
    #[serde(default)]
    pub errors: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct SimulatorConfig {
    #[serde(default)]
    pub now_ms: Option<u64>,
    #[serde(default)]
    pub offline_preview: bool,
    #[serde(default = "default_local_max_levels")]
    pub local_max_levels: usize,
    #[serde(default = "default_max_total_quote_notional")]
    pub max_total_quote_notional: f64,
    #[serde(default = "default_max_quote_notional_per_level")]
    pub max_quote_notional_per_level: f64,
    #[serde(default = "default_min_quote_notional")]
    pub min_quote_notional: f64,
    #[serde(default = "default_min_spread_bps")]
    pub min_spread_bps: f64,
    #[serde(default = "default_max_spread_bps")]
    pub max_spread_bps: f64,
    #[serde(default)]
    pub min_base_reserve: f64,
    #[serde(default)]
    pub min_quote_reserve: f64,
    #[serde(default = "default_stale_hold_ms")]
    pub stale_hold_ms: u64,
}

impl Default for SimulatorConfig {
    fn default() -> Self {
        Self {
            now_ms: None,
            offline_preview: false,
            local_max_levels: default_local_max_levels(),
            max_total_quote_notional: default_max_total_quote_notional(),
            max_quote_notional_per_level: default_max_quote_notional_per_level(),
            min_quote_notional: default_min_quote_notional(),
            min_spread_bps: default_min_spread_bps(),
            max_spread_bps: default_max_spread_bps(),
            min_base_reserve: 0.0,
            min_quote_reserve: 0.0,
            stale_hold_ms: default_stale_hold_ms(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SimulationOutput {
    pub desired_policy: QuotePolicy,
    pub action: SimulatedAction,
    pub applied_update: AppliedBookUpdate,
    pub current_state: CurrentState,
    pub capped_fields: Vec<String>,
    pub reject_reasons: Vec<String>,
    pub bid_levels: Vec<PreviewLevel>,
    pub ask_levels: Vec<PreviewLevel>,
    pub max_notional: f64,
    pub mode: PolicyMode,
    pub live_transactions_enabled: bool,
    pub post_only: bool,
    pub crossing_prevention: String,
    pub local_max_levels: usize,
    pub notional_caps: NotionalCaps,
    pub reserves: Reserves,
    pub min_spread_bps: f64,
    pub max_spread_bps: f64,
    pub stale_hold_ms: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SimulatedAction {
    UpdateBook,
    ClearBook,
    Hold,
}

#[derive(Debug, Clone)]
pub struct LiveDryRunSnapshot {
    pub policy: Option<QuotePolicy>,
    pub read_errors: Vec<String>,
    pub current_state: CurrentState,
    pub market_config: MarketConfig,
    pub config: SimulatorConfig,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppliedBookUpdate {
    pub new_mid_price_ticks: u64,
    pub mid_price_changed: bool,
    pub num_bid_levels: usize,
    pub num_ask_levels: usize,
    pub bid_levels: Vec<AppliedLevel>,
    pub ask_levels: Vec<AppliedLevel>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppliedLevel {
    pub size_in_base_lots: u64,
    pub price_offset_ticks: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct PreviewLevel {
    pub price: f64,
    pub size_base: f64,
    pub size_in_base_lots: u64,
    pub price_offset_ticks: i64,
    pub notional: f64,
    pub spread_bps: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct NotionalCaps {
    pub max_total_quote_notional: f64,
    pub max_quote_notional_per_level: f64,
    pub min_quote_notional: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct Reserves {
    pub min_base_reserve: f64,
    pub min_quote_reserve: f64,
}

pub fn simulate_quote_policy(input: SimulationInput) -> Result<SimulationOutput> {
    validate_input(&input)?;
    let mut capped_fields = Vec::new();
    let mut reject_reasons = fail_closed_reasons(&input);
    let mode = input.policy.mode;

    if !reject_reasons.is_empty() {
        return Ok(output(
            input.policy,
            input.state.clone(),
            empty_update(
                input.state.mid_price,
                input.state.cached_mid_ticks,
                &input.market_config,
            ),
            Vec::new(),
            Vec::new(),
            capped_fields,
            reject_reasons,
            mode,
            &input.config,
        ));
    }

    let local_max_levels = input.config.local_max_levels.min(MAX_LEVELS);
    let requested_level_count = input.policy.level_count.min(local_max_levels);
    if requested_level_count != input.policy.level_count {
        capped_fields.push(format!("level_count:{requested_level_count}"));
    }

    let active_sides =
        usize::from(input.policy.bid.enabled) + usize::from(input.policy.ask.enabled);
    let per_active_side_cap = if active_sides > 0 {
        input.config.max_total_quote_notional / active_sides as f64
    } else {
        0.0
    };
    let max_total_bid_notional = quote_capacity(&input).min(per_active_side_cap);
    let max_total_ask_notional = base_capacity(&input).min(per_active_side_cap);
    let mut bids = side_quotes(
        Side::Bid,
        &input.policy.bid,
        requested_level_count,
        input.state.mid_price,
        max_total_bid_notional,
        &input.config,
        &mut capped_fields,
    );
    let mut asks = side_quotes(
        Side::Ask,
        &input.policy.ask,
        requested_level_count,
        input.state.mid_price,
        max_total_ask_notional,
        &input.config,
        &mut capped_fields,
    );

    if let (Some(best_bid), Some(best_ask)) = (bids.first(), asks.first()) {
        if best_bid.price >= best_ask.price {
            reject_reasons.push("quote_policy_crosses_mid".to_string());
            bids.clear();
            asks.clear();
        }
    }

    let applied_update = if bids.is_empty() && asks.is_empty() {
        empty_update(
            input.state.mid_price,
            input.state.cached_mid_ticks,
            &input.market_config,
        )
    } else {
        let two_sided = TwoSidedQuote {
            bids: bids
                .iter()
                .map(|level| Quote {
                    price: level.price,
                    size: level.size_base,
                })
                .collect(),
            asks: asks
                .iter()
                .map(|level| Quote {
                    price: level.price,
                    size: level.size_base,
                })
                .collect(),
        };
        let reference_mid_ticks = if input.state.cached_mid_ticks > 0 {
            input.state.cached_mid_ticks
        } else {
            crate::archer::math::price_to_ticks(input.state.mid_price, &input.market_config)?
        };
        mirror_book_update(build_book_update(
            &two_sided,
            reference_mid_ticks,
            &input.market_config,
        )?)
    };

    align_preview_to_update(&mut bids, &applied_update.bid_levels, &input.market_config);
    align_preview_to_update(&mut asks, &applied_update.ask_levels, &input.market_config);

    Ok(output(
        input.policy,
        input.state.clone(),
        applied_update,
        bids,
        asks,
        capped_fields,
        reject_reasons,
        mode,
        &input.config,
    ))
}

pub fn simulate_fixture(fixture: SimulationFixture) -> Result<SimulationOutput> {
    let market_config = fixture.market.to_market_config()?;
    match fixture.policy {
        Some(policy) => simulate_quote_policy(SimulationInput {
            policy,
            state: fixture.state,
            market_config,
            config: fixture.config,
        }),
        None => {
            let mode = PolicyMode::Shadow;
            let policy = QuotePolicy {
                market_pair: "unknown".to_string(),
                quote_enabled: false,
                mode,
                level_count: 0,
                bid: SidePolicy {
                    enabled: false,
                    spreads_bps: Vec::new(),
                    level_sizes_base: Vec::new(),
                    size_multiplier: 0.0,
                },
                ask: SidePolicy {
                    enabled: false,
                    spreads_bps: Vec::new(),
                    level_sizes_base: Vec::new(),
                    size_multiplier: 0.0,
                },
                reason_codes: Vec::new(),
                timestamp_ms: None,
                max_age_ms: None,
            };
            Ok(output(
                policy,
                fixture.state.clone(),
                empty_update(
                    fixture.state.mid_price,
                    fixture.state.cached_mid_ticks,
                    &market_config,
                ),
                Vec::new(),
                Vec::new(),
                Vec::new(),
                vec!["quote_policy_missing".to_string()],
                mode,
                &fixture.config,
            ))
        }
    }
}

pub fn simulate_live_dry_run_snapshot(snapshot: LiveDryRunSnapshot) -> Result<SimulationOutput> {
    let LiveDryRunSnapshot {
        policy,
        read_errors,
        current_state,
        market_config,
        config,
    } = snapshot;
    match policy {
        Some(policy) if read_errors.is_empty() => simulate_quote_policy(SimulationInput {
            policy,
            state: current_state,
            market_config,
            config,
        }),
        maybe_policy => {
            validate_state_and_config(&current_state, &config)?;
            let policy = maybe_policy.unwrap_or_else(missing_policy);
            let mut reject_reasons = read_errors;
            if !reject_reasons
                .iter()
                .any(|reason| reason == "quote_policy_missing")
                && !policy.quote_enabled
                && policy.reason_codes.iter().any(|reason| reason == "missing")
            {
                reject_reasons.push("quote_policy_missing".to_string());
            }
            Ok(output(
                policy,
                current_state.clone(),
                empty_update(
                    current_state.mid_price,
                    current_state.cached_mid_ticks,
                    &market_config,
                ),
                Vec::new(),
                Vec::new(),
                Vec::new(),
                reject_reasons,
                PolicyMode::Shadow,
                &config,
            ))
        }
    }
}

pub fn current_maker_book_state(
    book: &MakerBook,
    market_config: &MarketConfig,
) -> CurrentMakerBookState {
    let balances = maker_balances(book, market_config);
    CurrentMakerBookState {
        sequence_number: book.last_updated_sequence_number,
        mid_price_ticks: book.mid_price_ticks,
        active_bid_levels: active_bid_levels(book),
        active_ask_levels: active_ask_levels(book),
        base_free: balances.base_free,
        base_locked: balances.base_locked,
        quote_free: balances.quote_free,
        quote_locked: balances.quote_locked,
    }
}

pub fn quote_policy_from_market_intel_json(value: serde_json::Value) -> Result<QuotePolicy> {
    serde_json::from_value::<QuotePolicy>(value.clone())
        .or_else(|_| {
            value
                .get("quote_policy")
                .cloned()
                .ok_or_else(|| anyhow::anyhow!("market-intel response missing quote_policy"))
                .and_then(|value| serde_json::from_value::<QuotePolicy>(value).map_err(Into::into))
        })
        .or_else(|_| {
            value
                .pointer("/recommendation/quote_policy")
                .cloned()
                .ok_or_else(|| {
                    anyhow::anyhow!("market-intel response missing recommendation.quote_policy")
                })
                .and_then(|value| serde_json::from_value::<QuotePolicy>(value).map_err(Into::into))
        })
        .context("market-intel response did not contain a quote policy")
}

pub fn mid_price_from_market_intel_json(value: &serde_json::Value) -> Option<f64> {
    [
        "/recommendation/fair_value",
        "/reference/price",
        "/sources/manifest/mid",
        "/sources/binance/mid",
        "/sources/hyperliquid/mid",
    ]
    .iter()
    .find_map(|path| value.pointer(path).and_then(serde_json::Value::as_f64))
    .filter(|price| price.is_finite() && *price > 0.0)
}

fn missing_policy() -> QuotePolicy {
    QuotePolicy {
        market_pair: "unknown".to_string(),
        quote_enabled: false,
        mode: PolicyMode::Shadow,
        level_count: 0,
        bid: SidePolicy {
            enabled: false,
            spreads_bps: Vec::new(),
            level_sizes_base: Vec::new(),
            size_multiplier: 0.0,
        },
        ask: SidePolicy {
            enabled: false,
            spreads_bps: Vec::new(),
            level_sizes_base: Vec::new(),
            size_multiplier: 0.0,
        },
        reason_codes: vec!["missing".to_string()],
        timestamp_ms: None,
        max_age_ms: None,
    }
}

impl MarketConfigFixture {
    pub fn to_market_config(&self) -> Result<MarketConfig> {
        let market_pubkey = parse_fixture_pubkey(&self.market_pubkey, 1)?;
        let header = MarketStateHeader {
            discriminator: MARKET_STATE_DISCRIMINATOR,
            market_id: market_pubkey,
            base_mint: parse_fixture_pubkey(&self.base_mint, 2)?,
            quote_mint: parse_fixture_pubkey(&self.quote_mint, 3)?,
            base_vault: fixed_pubkey(4),
            quote_vault: fixed_pubkey(5),
            admin: fixed_pubkey(6),
            base_atoms_per_base_lot: self.base_atoms_per_base_lot,
            quote_atoms_per_quote_lot: self.quote_atoms_per_quote_lot,
            tick_size_in_quote_atoms_per_base_unit: self.tick_size_in_quote_atoms_per_base_unit,
            raw_base_units_per_base_unit: self.raw_base_units_per_base_unit,
            uncollected_fees_quote_lots: 0,
            collected_fees_quote_lots: 0,
            maker_fee_ppm: self.maker_fee_ppm,
            taker_fee_ppm: self.taker_fee_ppm,
            base_decimals: self.base_decimals,
            quote_decimals: self.quote_decimals,
            status: 0,
            mode: 0,
            market_bump: 0,
            sync_fee_multiplier: 0,
            min_async_delay_slots: 0,
            max_async_delay_slots: 0,
            _reserved: 0,
        };
        Ok(MarketConfig::from_header(
            market_pubkey,
            &header,
            self.base_decimals,
            self.quote_decimals,
            spl_token::id(),
            spl_token::id(),
        ))
    }
}

fn parse_fixture_pubkey(value: &Option<String>, fallback_tag: u8) -> Result<Pubkey> {
    value
        .as_deref()
        .map(str::parse)
        .transpose()
        .map_err(|e| anyhow::anyhow!("invalid fixture pubkey: {e}"))
        .map(|parsed| parsed.unwrap_or_else(|| fixed_pubkey(fallback_tag)))
}

fn fixed_pubkey(tag: u8) -> Pubkey {
    Pubkey::new_from_array([tag; 32])
}

fn validate_input(input: &SimulationInput) -> Result<()> {
    validate_state_and_config(&input.state, &input.config)
}

fn validate_state_and_config(state: &CurrentState, config: &SimulatorConfig) -> Result<()> {
    ensure!(
        state.mid_price.is_finite() && state.mid_price > 0.0,
        "mid_price must be positive"
    );
    ensure!(
        state.base_available.is_finite() && state.base_available >= 0.0,
        "base_available must be >= 0"
    );
    ensure!(
        state.quote_available.is_finite() && state.quote_available >= 0.0,
        "quote_available must be >= 0"
    );
    ensure!(
        config.max_total_quote_notional.is_finite() && config.max_total_quote_notional >= 0.0,
        "max_total_quote_notional must be >= 0"
    );
    ensure!(
        config.max_quote_notional_per_level.is_finite()
            && config.max_quote_notional_per_level >= 0.0,
        "max_quote_notional_per_level must be >= 0"
    );
    ensure!(
        config.min_quote_notional.is_finite() && config.min_quote_notional >= 0.0,
        "min_quote_notional must be >= 0"
    );
    ensure!(
        config.min_spread_bps.is_finite()
            && config.max_spread_bps.is_finite()
            && config.min_spread_bps >= 0.0
            && config.max_spread_bps >= config.min_spread_bps,
        "spread bounds must be finite and ordered"
    );
    Ok(())
}

fn fail_closed_reasons(input: &SimulationInput) -> Vec<String> {
    let mut reasons = Vec::new();
    if !input.policy.quote_enabled {
        reasons.push("quote_policy_disabled".to_string());
    }
    if !input.config.offline_preview {
        match (input.policy.timestamp_ms, input.config.now_ms) {
            (Some(ts), Some(now)) => {
                let max_age = input
                    .policy
                    .max_age_ms
                    .unwrap_or(input.config.stale_hold_ms);
                if now.saturating_sub(ts) > max_age {
                    reasons.push("quote_policy_stale".to_string());
                }
            }
            (None, _) => reasons.push("quote_policy_missing_timestamp".to_string()),
            (_, None) => reasons.push("simulator_now_missing".to_string()),
        }
    }
    reasons
}

#[derive(Debug, Clone, Copy)]
enum Side {
    Bid,
    Ask,
}

impl Side {
    fn label(self) -> &'static str {
        match self {
            Side::Bid => "bid",
            Side::Ask => "ask",
        }
    }
}

fn side_quotes(
    side: Side,
    policy: &SidePolicy,
    level_count: usize,
    mid_price: f64,
    side_notional_cap: f64,
    config: &SimulatorConfig,
    capped_fields: &mut Vec<String>,
) -> Vec<PreviewLevel> {
    if !policy.enabled || level_count == 0 {
        return Vec::new();
    }
    if policy.size_multiplier < 1.0 {
        capped_fields.push(format!(
            "{}.size_multiplier:{}",
            side.label(),
            trim_float(policy.size_multiplier)
        ));
    }

    let mut remaining_notional = side_notional_cap.max(0.0);
    let mut levels = Vec::with_capacity(level_count);
    for index in 0..level_count {
        let raw_spread = policy.spreads_bps.get(index).copied().unwrap_or_else(|| {
            policy
                .spreads_bps
                .last()
                .copied()
                .unwrap_or(config.min_spread_bps)
        });
        let spread_bps = raw_spread.clamp(config.min_spread_bps, config.max_spread_bps);
        if spread_bps != raw_spread {
            capped_fields.push(format!(
                "{}.spread_bps:{}",
                side.label(),
                trim_float(spread_bps)
            ));
        }

        let raw_size = policy
            .level_sizes_base
            .get(index)
            .copied()
            .or_else(|| policy.level_sizes_base.last().copied())
            .unwrap_or(0.0);
        if !raw_size.is_finite() || raw_size <= 0.0 || policy.size_multiplier <= 0.0 {
            continue;
        }

        let price = match side {
            Side::Bid => mid_price * (1.0 - spread_bps / 10_000.0),
            Side::Ask => mid_price * (1.0 + spread_bps / 10_000.0),
        };
        if price <= 0.0 {
            continue;
        }
        let requested_notional = raw_size * policy.size_multiplier.min(1.0) * price;
        let capped_notional = requested_notional
            .min(config.max_quote_notional_per_level)
            .min(remaining_notional);
        if capped_notional < config.min_quote_notional {
            continue;
        }
        if capped_notional < requested_notional {
            capped_fields.push(format!(
                "{}.notional:{}",
                side.label(),
                trim_float(capped_notional)
            ));
        }
        let size_base = capped_notional / price;
        remaining_notional -= capped_notional;
        levels.push(PreviewLevel {
            price,
            size_base,
            size_in_base_lots: 0,
            price_offset_ticks: 0,
            notional: capped_notional,
            spread_bps,
        });
    }
    levels
}

fn quote_capacity(input: &SimulationInput) -> f64 {
    (input.state.quote_available - input.config.min_quote_reserve).max(0.0)
}

fn base_capacity(input: &SimulationInput) -> f64 {
    (input.state.base_available - input.config.min_base_reserve).max(0.0) * input.state.mid_price
}

fn empty_update(
    mid_price: f64,
    cached_mid_ticks: u64,
    market_config: &MarketConfig,
) -> AppliedBookUpdate {
    let new_mid_price_ticks = if cached_mid_ticks > 0 {
        cached_mid_ticks
    } else {
        crate::archer::math::price_to_ticks(mid_price, market_config).unwrap_or(0)
    };
    AppliedBookUpdate {
        new_mid_price_ticks,
        mid_price_changed: false,
        num_bid_levels: 0,
        num_ask_levels: 0,
        bid_levels: Vec::new(),
        ask_levels: Vec::new(),
    }
}

fn mirror_book_update(book_update: BookUpdate) -> AppliedBookUpdate {
    AppliedBookUpdate {
        new_mid_price_ticks: book_update.new_mid_price_ticks,
        mid_price_changed: book_update.mid_price_changed,
        num_bid_levels: book_update.bid_levels.len(),
        num_ask_levels: book_update.ask_levels.len(),
        bid_levels: book_update
            .bid_levels
            .into_iter()
            .map(|level| AppliedLevel {
                size_in_base_lots: level.size_in_base_lots,
                price_offset_ticks: level.price_offset_ticks,
            })
            .collect(),
        ask_levels: book_update
            .ask_levels
            .into_iter()
            .map(|level| AppliedLevel {
                size_in_base_lots: level.size_in_base_lots,
                price_offset_ticks: level.price_offset_ticks,
            })
            .collect(),
    }
}

fn align_preview_to_update(
    preview: &mut [PreviewLevel],
    applied: &[AppliedLevel],
    market_config: &MarketConfig,
) {
    for (level, applied) in preview.iter_mut().zip(applied.iter()) {
        level.size_in_base_lots = applied.size_in_base_lots;
        level.price_offset_ticks = applied.price_offset_ticks;
        level.size_base = base_lots_to_amount(applied.size_in_base_lots, market_config);
        level.notional = level.price * level.size_base;
    }
}

fn output(
    desired_policy: QuotePolicy,
    current_state: CurrentState,
    applied_update: AppliedBookUpdate,
    bid_levels: Vec<PreviewLevel>,
    ask_levels: Vec<PreviewLevel>,
    capped_fields: Vec<String>,
    reject_reasons: Vec<String>,
    mode: PolicyMode,
    config: &SimulatorConfig,
) -> SimulationOutput {
    let max_notional = bid_levels
        .iter()
        .chain(ask_levels.iter())
        .map(|level| level.notional)
        .sum::<f64>();
    let action = simulated_action(&applied_update, &reject_reasons, &current_state);
    SimulationOutput {
        desired_policy,
        action,
        applied_update,
        current_state,
        capped_fields,
        reject_reasons,
        bid_levels,
        ask_levels,
        max_notional,
        mode,
        live_transactions_enabled: false,
        post_only: true,
        crossing_prevention: "reject_crossed_book".to_string(),
        local_max_levels: config.local_max_levels.min(MAX_LEVELS),
        notional_caps: NotionalCaps {
            max_total_quote_notional: config.max_total_quote_notional,
            max_quote_notional_per_level: config.max_quote_notional_per_level,
            min_quote_notional: config.min_quote_notional,
        },
        reserves: Reserves {
            min_base_reserve: config.min_base_reserve,
            min_quote_reserve: config.min_quote_reserve,
        },
        min_spread_bps: config.min_spread_bps,
        max_spread_bps: config.max_spread_bps,
        stale_hold_ms: config.stale_hold_ms,
    }
}

fn simulated_action(
    applied_update: &AppliedBookUpdate,
    reject_reasons: &[String],
    current_state: &CurrentState,
) -> SimulatedAction {
    if reject_reasons.is_empty()
        && (applied_update.num_bid_levels > 0 || applied_update.num_ask_levels > 0)
    {
        return SimulatedAction::UpdateBook;
    }
    if current_state
        .maker_book
        .map(|book| book.active_bid_levels > 0 || book.active_ask_levels > 0)
        .unwrap_or(false)
    {
        SimulatedAction::ClearBook
    } else {
        SimulatedAction::Hold
    }
}

fn trim_float(value: f64) -> String {
    if (value.fract()).abs() < f64::EPSILON {
        format!("{value:.0}")
    } else {
        value.to_string()
    }
}

fn default_local_max_levels() -> usize {
    MAX_LEVELS
}

fn default_max_total_quote_notional() -> f64 {
    200.0
}

fn default_max_quote_notional_per_level() -> f64 {
    25.0
}

fn default_min_quote_notional() -> f64 {
    5.0
}

fn default_min_spread_bps() -> f64 {
    0.0
}

fn default_max_spread_bps() -> f64 {
    500.0
}

fn default_stale_hold_ms() -> u64 {
    10_000
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::quote_policy::{PolicyMode, QuotePolicy, SidePolicy};

    fn market_config() -> crate::archer::config::MarketConfig {
        use solana_sdk::pubkey::Pubkey;

        let market_pubkey = Pubkey::new_unique();
        let header = crate::archer::types::MarketStateHeader {
            discriminator: crate::archer::types::MARKET_STATE_DISCRIMINATOR,
            market_id: market_pubkey,
            base_mint: Pubkey::new_unique(),
            quote_mint: Pubkey::new_unique(),
            base_vault: Pubkey::new_unique(),
            quote_vault: Pubkey::new_unique(),
            admin: Pubkey::new_unique(),
            base_atoms_per_base_lot: 1_000_000,
            quote_atoms_per_quote_lot: 1_000,
            tick_size_in_quote_atoms_per_base_unit: 10_000,
            raw_base_units_per_base_unit: 1_000_000_000,
            uncollected_fees_quote_lots: 0,
            collected_fees_quote_lots: 0,
            maker_fee_ppm: 0,
            taker_fee_ppm: 0,
            base_decimals: 9,
            quote_decimals: 6,
            status: 0,
            mode: 0,
            market_bump: 0,
            sync_fee_multiplier: 0,
            min_async_delay_slots: 0,
            max_async_delay_slots: 0,
            _reserved: 0,
        };
        crate::archer::config::MarketConfig::from_header(
            market_pubkey,
            &header,
            9,
            6,
            spl_token::id(),
            spl_token::id(),
        )
    }

    fn side(enabled: bool, spreads_bps: &[f64], sizes_base: &[f64], multiplier: f64) -> SidePolicy {
        SidePolicy {
            enabled,
            spreads_bps: spreads_bps.to_vec(),
            level_sizes_base: sizes_base.to_vec(),
            size_multiplier: multiplier,
        }
    }

    fn policy() -> QuotePolicy {
        QuotePolicy {
            market_pair: "SOL/USDC".to_string(),
            quote_enabled: true,
            mode: PolicyMode::Shadow,
            level_count: 2,
            bid: side(true, &[20.0, 40.0], &[0.25, 0.50], 1.0),
            ask: side(true, &[25.0, 45.0], &[0.20, 0.30], 0.5),
            reason_codes: vec!["normal".to_string()],
            timestamp_ms: Some(1_000),
            max_age_ms: Some(10_000),
        }
    }

    #[test]
    fn simulator_outputs_exact_book_update_and_preview_levels() {
        let input = SimulationInput {
            policy: policy(),
            state: CurrentState {
                mid_price: 100.0,
                cached_mid_ticks: 0,
                base_available: 10.0,
                quote_available: 1_000.0,
                maker_book: None,
                wallet: None,
            },
            market_config: market_config(),
            config: SimulatorConfig {
                now_ms: Some(5_000),
                offline_preview: false,
                local_max_levels: 16,
                max_total_quote_notional: 250.0,
                max_quote_notional_per_level: 80.0,
                min_quote_notional: 5.0,
                min_spread_bps: 10.0,
                max_spread_bps: 100.0,
                min_base_reserve: 1.0,
                min_quote_reserve: 100.0,
                stale_hold_ms: 10_000,
            },
        };

        let output = simulate_quote_policy(input).expect("policy should simulate");

        assert_eq!(output.mode, PolicyMode::Shadow);
        assert!(!output.live_transactions_enabled);
        assert_eq!(output.reject_reasons, Vec::<String>::new());
        assert_eq!(output.bid_levels.len(), 2);
        assert_eq!(output.ask_levels.len(), 2);
        assert_eq!(output.applied_update.num_bid_levels, 2);
        assert_eq!(output.applied_update.num_ask_levels, 2);
        assert_eq!(
            output.applied_update.bid_levels[0].size_in_base_lots,
            output.bid_levels[0].size_in_base_lots
        );
        assert!(output.bid_levels[0].price < 100.0);
        assert!(output.ask_levels[0].price > 100.0);
        assert_eq!(
            output.capped_fields,
            vec!["ask.size_multiplier:0.5".to_string()]
        );
        assert!(output.max_notional <= 250.0);
    }

    #[test]
    fn simulator_fails_closed_for_disabled_missing_or_stale_policy() {
        let base_input = SimulationInput {
            policy: policy(),
            state: CurrentState {
                mid_price: 100.0,
                cached_mid_ticks: 0,
                base_available: 10.0,
                quote_available: 1_000.0,
                maker_book: None,
                wallet: None,
            },
            market_config: market_config(),
            config: SimulatorConfig {
                now_ms: Some(20_000),
                offline_preview: false,
                local_max_levels: 16,
                max_total_quote_notional: 250.0,
                max_quote_notional_per_level: 80.0,
                min_quote_notional: 5.0,
                min_spread_bps: 10.0,
                max_spread_bps: 100.0,
                min_base_reserve: 1.0,
                min_quote_reserve: 100.0,
                stale_hold_ms: 10_000,
            },
        };

        let stale = simulate_quote_policy(base_input).expect("stale policy returns closed output");
        assert!(stale.applied_update.bid_levels.is_empty());
        assert!(stale.applied_update.ask_levels.is_empty());
        assert_eq!(stale.reject_reasons, vec!["quote_policy_stale".to_string()]);
        assert_eq!(stale.action, SimulatedAction::Hold);

        let mut disabled = policy();
        disabled.quote_enabled = false;
        let disabled = simulate_quote_policy(SimulationInput {
            policy: disabled,
            config: SimulatorConfig {
                now_ms: Some(1_500),
                offline_preview: false,
                local_max_levels: 16,
                max_total_quote_notional: 250.0,
                max_quote_notional_per_level: 80.0,
                min_quote_notional: 5.0,
                min_spread_bps: 10.0,
                max_spread_bps: 100.0,
                min_base_reserve: 1.0,
                min_quote_reserve: 100.0,
                stale_hold_ms: 10_000,
            },
            state: CurrentState {
                mid_price: 100.0,
                cached_mid_ticks: 0,
                base_available: 10.0,
                quote_available: 1_000.0,
                maker_book: Some(CurrentMakerBookState {
                    sequence_number: 42,
                    mid_price_ticks: 100_000,
                    active_bid_levels: 1,
                    active_ask_levels: 1,
                    base_free: 8.0,
                    base_locked: 2.0,
                    quote_free: 900.0,
                    quote_locked: 100.0,
                }),
                wallet: None,
            },
            market_config: market_config(),
        })
        .expect("disabled policy returns closed output");
        assert_eq!(
            disabled.reject_reasons,
            vec!["quote_policy_disabled".to_string()]
        );
        assert!(disabled.applied_update.bid_levels.is_empty());
        assert_eq!(disabled.action, SimulatedAction::ClearBook);
        assert_eq!(
            disabled.current_state.maker_book.unwrap().sequence_number,
            42
        );
    }

    #[test]
    fn simulator_outputs_current_state_wallet_context_and_clear_book_action() {
        let mut disabled_policy = policy();
        disabled_policy.quote_enabled = false;
        let output = simulate_quote_policy(SimulationInput {
            policy: disabled_policy,
            state: CurrentState {
                mid_price: 100.0,
                cached_mid_ticks: 77_000,
                base_available: 4.0,
                quote_available: 500.0,
                maker_book: Some(CurrentMakerBookState {
                    sequence_number: 99,
                    mid_price_ticks: 77_000,
                    active_bid_levels: 2,
                    active_ask_levels: 0,
                    base_free: 3.5,
                    base_locked: 0.5,
                    quote_free: 450.0,
                    quote_locked: 50.0,
                }),
                wallet: Some(WalletBalanceContext {
                    source: "rpc_token_accounts".to_string(),
                    native_sol: Some(0.2),
                    base: Some(1.25),
                    quote: Some(42.0),
                    errors: vec!["quote token account unavailable".to_string()],
                }),
            },
            market_config: market_config(),
            config: SimulatorConfig {
                now_ms: Some(1_500),
                offline_preview: false,
                local_max_levels: 16,
                max_total_quote_notional: 250.0,
                max_quote_notional_per_level: 80.0,
                min_quote_notional: 5.0,
                min_spread_bps: 10.0,
                max_spread_bps: 100.0,
                min_base_reserve: 1.0,
                min_quote_reserve: 100.0,
                stale_hold_ms: 10_000,
            },
        })
        .expect("disabled live state returns closed output");

        assert_eq!(output.action, SimulatedAction::ClearBook);
        assert_eq!(
            output
                .current_state
                .maker_book
                .as_ref()
                .unwrap()
                .sequence_number,
            99
        );
        assert_eq!(
            output.current_state.wallet.as_ref().unwrap().source,
            "rpc_token_accounts"
        );
        assert_eq!(
            output.current_state.wallet.as_ref().unwrap().errors,
            vec!["quote token account unavailable".to_string()]
        );
    }

    #[test]
    fn live_dry_run_snapshot_builds_input_without_network_clients() {
        let snapshot = LiveDryRunSnapshot {
            policy: Some(policy()),
            read_errors: Vec::new(),
            current_state: CurrentState {
                mid_price: 100.0,
                cached_mid_ticks: 88_000,
                base_available: 6.0,
                quote_available: 700.0,
                maker_book: Some(CurrentMakerBookState {
                    sequence_number: 7,
                    mid_price_ticks: 88_000,
                    active_bid_levels: 1,
                    active_ask_levels: 1,
                    base_free: 5.0,
                    base_locked: 1.0,
                    quote_free: 650.0,
                    quote_locked: 50.0,
                }),
                wallet: Some(WalletBalanceContext {
                    source: "rpc_token_accounts".to_string(),
                    native_sol: Some(0.3),
                    base: Some(2.0),
                    quote: Some(20.0),
                    errors: Vec::new(),
                }),
            },
            market_config: market_config(),
            config: SimulatorConfig {
                now_ms: Some(5_000),
                offline_preview: false,
                ..SimulatorConfig::default()
            },
        };

        let output = simulate_live_dry_run_snapshot(snapshot).expect("snapshot should simulate");

        assert_eq!(output.action, SimulatedAction::UpdateBook);
        assert!(!output.live_transactions_enabled);
        assert_eq!(
            output
                .current_state
                .maker_book
                .as_ref()
                .unwrap()
                .sequence_number,
            7
        );
        assert_eq!(
            output.current_state.wallet.as_ref().unwrap().source,
            "rpc_token_accounts"
        );
    }

    #[test]
    fn live_dry_run_read_errors_fail_closed_without_transactions() {
        let snapshot = LiveDryRunSnapshot {
            policy: None,
            read_errors: vec![
                "market_intel_signal_url missing".to_string(),
                "maker_book_read: rpc unavailable".to_string(),
            ],
            current_state: CurrentState {
                mid_price: 100.0,
                cached_mid_ticks: 0,
                base_available: 0.0,
                quote_available: 0.0,
                maker_book: None,
                wallet: Some(WalletBalanceContext {
                    source: "rpc_token_accounts".to_string(),
                    native_sol: None,
                    base: None,
                    quote: None,
                    errors: vec!["wallet balance read failed".to_string()],
                }),
            },
            market_config: market_config(),
            config: SimulatorConfig {
                now_ms: Some(5_000),
                offline_preview: false,
                ..SimulatorConfig::default()
            },
        };

        let output = simulate_live_dry_run_snapshot(snapshot).expect("read errors return JSON");

        assert_eq!(output.action, SimulatedAction::Hold);
        assert!(!output.live_transactions_enabled);
        assert_eq!(
            output.reject_reasons,
            vec![
                "market_intel_signal_url missing".to_string(),
                "maker_book_read: rpc unavailable".to_string(),
                "quote_policy_missing".to_string()
            ]
        );
        assert_eq!(
            output.current_state.wallet.unwrap().errors,
            vec!["wallet balance read failed".to_string()]
        );
    }

    #[test]
    fn offline_preview_allows_missing_timestamp_but_caps_levels_and_notional() {
        let mut policy = policy();
        policy.timestamp_ms = None;
        policy.max_age_ms = None;
        policy.level_count = 20;
        policy.bid.spreads_bps = vec![1.0, 20.0, 30.0, 40.0];
        policy.bid.level_sizes_base = vec![2.0, 2.0, 2.0, 2.0];
        policy.ask.enabled = false;

        let output = simulate_quote_policy(SimulationInput {
            policy,
            state: CurrentState {
                mid_price: 100.0,
                cached_mid_ticks: 0,
                base_available: 10.0,
                quote_available: 60.0,
                maker_book: None,
                wallet: None,
            },
            market_config: market_config(),
            config: SimulatorConfig {
                now_ms: None,
                offline_preview: true,
                local_max_levels: 2,
                max_total_quote_notional: 50.0,
                max_quote_notional_per_level: 30.0,
                min_quote_notional: 5.0,
                min_spread_bps: 10.0,
                max_spread_bps: 100.0,
                min_base_reserve: 1.0,
                min_quote_reserve: 10.0,
                stale_hold_ms: 10_000,
            },
        })
        .expect("offline fixture preview should simulate without timestamp");

        assert_eq!(output.bid_levels.len(), 2);
        assert_eq!(output.ask_levels.len(), 0);
        assert!(output.bid_levels.iter().all(|level| level.notional <= 30.0));
        assert!(output.max_notional <= 50.0);
        assert!(output.capped_fields.contains(&"level_count:2".to_string()));
        assert!(
            output
                .capped_fields
                .contains(&"bid.spread_bps:10".to_string())
        );
    }

    #[test]
    fn fixture_deserializes_without_rpc_and_missing_policy_fails_closed() {
        let fixture = serde_json::json!({
            "policy": null,
            "state": {
                "mid_price": 100.0,
                "cached_mid_ticks": 0,
                "base_available": 10.0,
                "quote_available": 1_000.0
            },
            "market": {
                "base_atoms_per_base_lot": 1_000_000,
                "quote_atoms_per_quote_lot": 1_000,
                "tick_size_in_quote_atoms_per_base_unit": 10_000,
                "raw_base_units_per_base_unit": 1_000_000_000,
                "base_decimals": 9,
                "quote_decimals": 6
            },
            "config": {
                "now_ms": 1_000,
                "offline_preview": false,
                "local_max_levels": 16
            }
        });

        let fixture: SimulationFixture = serde_json::from_value(fixture).unwrap();
        let output = simulate_fixture(fixture).expect("missing policy should still return JSON");

        assert_eq!(
            output.reject_reasons,
            vec!["quote_policy_missing".to_string()]
        );
        assert!(output.applied_update.bid_levels.is_empty());
        assert!(output.applied_update.ask_levels.is_empty());
        assert!(!output.live_transactions_enabled);
    }
}
