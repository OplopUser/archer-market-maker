use crate::archer::{
    config::MarketConfig,
    math::{
        BookUpdate, Quote, TwoSidedQuote, base_lots_to_amount, build_book_update, price_to_ticks,
        quote_lots_to_amount,
    },
};

use crate::config::{RiskSettings, StrategySettings};

pub enum QuoteDecision {
    ClearBook,
    UpdateMidOnly {
        new_mid_ticks: u64,
    },
    UpdateFull {
        book_update: BookUpdate,
        structure_hash: u64,
    },
}

pub struct Strategy {
    config: StrategySettings,
    risk: RiskSettings,
}

#[derive(Debug, Copy, Clone)]
pub struct IntelAdjustments {
    pub spread_add_bps: f64,
    pub size_multiplier: f64,
    pub bid_size_multiplier: f64,
    pub ask_size_multiplier: f64,
}

impl Default for IntelAdjustments {
    fn default() -> Self {
        Self {
            spread_add_bps: 0.0,
            size_multiplier: 1.0,
            bid_size_multiplier: 1.0,
            ask_size_multiplier: 1.0,
        }
    }
}

impl Strategy {
    pub fn new(config: &StrategySettings, risk: &RiskSettings) -> Self {
        Self {
            config: config.clone(),
            risk: risk.clone(),
        }
    }

    fn vol_multiplier(&self, volatility_bps: f64) -> f64 {
        let raw = (volatility_bps / self.config.vol_baseline_bps).max(1.0);
        raw.min(self.config.vol_max_multiplier)
    }

    pub fn compute(
        &self,
        mid_price: f64,
        cached_mid_ticks: u64,
        last_structure_hash: u64,
        sdk_config: &MarketConfig,
        base_total_lots: u64,
        quote_total_lots: u64,
        volatility_bps: f64,
        intel: IntelAdjustments,
    ) -> (QuoteDecision, f64) {
        if !mid_price.is_finite() || mid_price <= 0.0 {
            return (QuoteDecision::ClearBook, 0.0);
        }

        let vol_mult = self.vol_multiplier(volatility_bps);
        let num_levels = self.config.spread_levels_bps.len();
        let inventory_fraction = self.config.inventory_pct / 100.0;

        let available_base = base_lots_to_amount(base_total_lots, sdk_config);
        let available_quote = quote_lots_to_amount(quote_total_lots, sdk_config);
        let quoteable_base = available_base * reserve_multiplier(self.risk.min_base_reserve_pct);
        let quoteable_quote = available_quote * reserve_multiplier(self.risk.min_quote_reserve_pct);
        let side_notional_cap = self.risk.max_total_quote_notional * 0.5;
        let raw_bid_notional_budget = (quoteable_quote * inventory_fraction).min(side_notional_cap);
        let raw_ask_notional_budget =
            (quoteable_base * mid_price * inventory_fraction).min(side_notional_cap);

        let intel_spread_add = self.intel_spread_add_bps(intel.spread_add_bps);
        let size_multiplier = bounded_intel_size_multiplier(
            intel.size_multiplier,
            self.config.min_intel_size_multiplier,
        );
        let base_notional = available_base * mid_price;
        let quote_notional = available_quote;
        let raw_bid_size_multiplier = if should_suppress_inventory_adding_bids(
            base_notional,
            quote_notional,
            intel.bid_size_multiplier,
            intel.ask_size_multiplier,
        ) {
            0.0
        } else {
            intel.bid_size_multiplier
        };
        let bid_size_multiplier = size_multiplier
            * bounded_intel_size_multiplier(
                raw_bid_size_multiplier,
                self.config.min_intel_size_multiplier,
            );
        let ask_size_multiplier = size_multiplier
            * bounded_intel_size_multiplier(
                intel.ask_size_multiplier,
                self.config.min_intel_size_multiplier,
            );
        let bid_side_spread_add = self.intel_side_spread_add_bps(bid_size_multiplier);
        let ask_side_spread_add = self.intel_side_spread_add_bps(ask_size_multiplier);
        let raw_tightest_bid_spread =
            self.config.spread_levels_bps[0] * vol_mult + intel_spread_add + bid_side_spread_add;
        let raw_tightest_ask_spread =
            self.config.spread_levels_bps[0] * vol_mult + intel_spread_add + ask_side_spread_add;
        let bid_spread_floor_shift = self.spread_floor_shift(raw_tightest_bid_spread);
        let ask_spread_floor_shift = self.spread_floor_shift(raw_tightest_ask_spread);
        let tightest_bid_spread = raw_tightest_bid_spread + bid_spread_floor_shift;
        let tightest_ask_spread = raw_tightest_ask_spread + ask_spread_floor_shift;
        let tightest_spread = tightest_bid_spread.min(tightest_ask_spread);
        let bid_notional_budget = raw_bid_notional_budget * bid_size_multiplier;
        let ask_notional_budget = raw_ask_notional_budget * ask_size_multiplier;

        let mut bids: Vec<Quote> = Vec::with_capacity(num_levels);
        let mut asks: Vec<Quote> = Vec::with_capacity(num_levels);
        let mut bid_sizes_q: Vec<u64> = Vec::with_capacity(num_levels);
        let mut ask_sizes_q: Vec<u64> = Vec::with_capacity(num_levels);

        let bid_levels = levels_for_budget(
            bid_notional_budget,
            num_levels,
            self.risk.max_quote_notional_per_level,
            self.risk.min_quote_notional,
        );
        let ask_levels = levels_for_budget(
            ask_notional_budget,
            num_levels,
            self.risk.max_quote_notional_per_level,
            self.risk.min_quote_notional,
        );

        for (level_index, &spread_bps) in self.config.spread_levels_bps.iter().enumerate() {
            let base_spread = spread_bps * vol_mult + intel_spread_add;
            let bid_spread = base_spread + bid_side_spread_add + bid_spread_floor_shift;
            let ask_spread = base_spread + ask_side_spread_add + ask_spread_floor_shift;
            let bid_clears_edge = self.clears_min_net_edge(bid_spread);
            let ask_clears_edge = self.clears_min_net_edge(ask_spread);
            let bid_price = mid_price * (1.0 - bid_spread / 10_000.0);
            let ask_price = mid_price * (1.0 + ask_spread / 10_000.0);

            let bid_notional = if level_index < bid_levels {
                per_level_notional(
                    bid_notional_budget,
                    bid_levels,
                    self.risk.max_quote_notional_per_level,
                    self.risk.min_quote_notional,
                )
            } else {
                0.0
            };
            let ask_notional = if level_index < ask_levels {
                per_level_notional(
                    ask_notional_budget,
                    ask_levels,
                    self.risk.max_quote_notional_per_level,
                    self.risk.min_quote_notional,
                )
            } else {
                0.0
            };

            let ask_size = if ask_price > 0.0 {
                ask_notional / ask_price
            } else {
                0.0
            };
            let bid_size = if bid_price > 0.0 {
                bid_notional / bid_price
            } else {
                0.0
            };

            let ask_q = quantize(ask_size);
            let bid_q = quantize(bid_size);

            if ask_q > 0.0 && ask_clears_edge {
                asks.push(Quote {
                    price: ask_price,
                    size: ask_size,
                });
            }
            ask_sizes_q.push((ask_q * 100.0) as u64);

            if bid_q > 0.0 && bid_clears_edge {
                bids.push(Quote {
                    price: bid_price,
                    size: bid_size,
                });
            }
            bid_sizes_q.push((bid_q * 100.0) as u64);
        }

        if bids.is_empty() && asks.is_empty() {
            return (QuoteDecision::ClearBook, tightest_spread);
        }

        let new_hash = structure_hash(
            num_levels,
            &bid_sizes_q,
            &ask_sizes_q,
            tightest_bid_spread,
            tightest_ask_spread,
        );

        if new_hash == last_structure_hash && last_structure_hash != 0 {
            let decision = match price_to_ticks(mid_price, sdk_config) {
                Ok(new_mid_ticks) => QuoteDecision::UpdateMidOnly { new_mid_ticks },
                Err(_) => QuoteDecision::ClearBook,
            };
            (decision, tightest_spread)
        } else {
            let live_mid_ticks = match price_to_ticks(mid_price, sdk_config) {
                Ok(t) if t > 0 => t,
                _ => return (QuoteDecision::ClearBook, tightest_spread),
            };
            let two_sided = !bids.is_empty() && !asks.is_empty();
            let reference_mid_ticks = if two_sided && cached_mid_ticks > 0 {
                cached_mid_ticks
            } else {
                live_mid_ticks
            };

            let mut quotes = TwoSidedQuote::new();
            for b in &bids {
                quotes = quotes.with_bid(b.price, b.size);
            }
            for a in &asks {
                quotes = quotes.with_ask(a.price, a.size);
            }

            let decision = match build_book_update(&quotes, reference_mid_ticks, sdk_config) {
                Ok(book_update) => QuoteDecision::UpdateFull {
                    book_update,
                    structure_hash: new_hash,
                },
                Err(e) => {
                    tracing::warn!("build_book_update failed: {e}, clearing book");
                    QuoteDecision::ClearBook
                }
            };
            (decision, tightest_spread)
        }
    }

    fn intel_spread_add_bps(&self, raw: f64) -> f64 {
        if !raw.is_finite() {
            return 0.0;
        }
        let bounded = if raw >= 0.0 {
            raw.min(self.config.max_intel_spread_add_bps)
        } else {
            raw.max(-self.config.max_intel_spread_tighten_bps)
        };
        bounded * self.config.intel_spread_add_multiplier
    }

    fn intel_side_spread_add_bps(&self, size_multiplier: f64) -> f64 {
        if !size_multiplier.is_finite() || size_multiplier <= 0.0 {
            return self.config.max_intel_side_spread_add_bps;
        }
        ((1.0 - size_multiplier).max(0.0) * self.config.max_intel_side_spread_add_bps)
            .min(self.config.max_intel_side_spread_add_bps)
    }

    fn spread_floor_shift(&self, tightest_spread_bps: f64) -> f64 {
        (self.edge_floor_bps() - tightest_spread_bps).max(0.0)
    }

    fn edge_floor_bps(&self) -> f64 {
        self.config
            .min_effective_spread_bps
            .max(self.config.toxicity_buffer_bps + self.config.min_net_edge_bps)
    }

    fn expected_net_edge_bps(&self, spread_bps: f64) -> f64 {
        spread_bps - self.config.toxicity_buffer_bps
    }

    fn clears_min_net_edge(&self, spread_bps: f64) -> bool {
        self.expected_net_edge_bps(spread_bps) + f64::EPSILON >= self.config.min_net_edge_bps
    }
}

fn bounded_intel_size_multiplier(value: f64, minimum: f64) -> f64 {
    if !value.is_finite() {
        return 1.0;
    }
    if value <= 0.0 {
        0.0
    } else {
        value.max(minimum).min(1.0)
    }
}

fn should_suppress_inventory_adding_bids(
    base_notional: f64,
    quote_notional: f64,
    bid_size_multiplier: f64,
    ask_size_multiplier: f64,
) -> bool {
    if !base_notional.is_finite()
        || !quote_notional.is_finite()
        || !bid_size_multiplier.is_finite()
        || !ask_size_multiplier.is_finite()
    {
        return false;
    }
    let base_heavy = base_notional > quote_notional;
    let bids_more_restricted_than_asks = bid_size_multiplier < ask_size_multiplier;
    base_heavy && bids_more_restricted_than_asks
}

fn reserve_multiplier(reserve_pct: f64) -> f64 {
    (1.0 - reserve_pct / 100.0).clamp(0.0, 1.0)
}

fn per_level_notional(
    side_budget: f64,
    levels: usize,
    max_per_level: f64,
    min_notional: f64,
) -> f64 {
    if levels == 0 {
        return 0.0;
    }
    let notional = (side_budget / levels as f64).min(max_per_level);
    if notional >= min_notional {
        notional
    } else {
        0.0
    }
}

fn levels_for_budget(
    side_budget: f64,
    max_levels: usize,
    max_per_level: f64,
    min_notional: f64,
) -> usize {
    if max_levels == 0 || side_budget < min_notional {
        return 0;
    }

    for levels in (1..=max_levels).rev() {
        let notional = (side_budget / levels as f64).min(max_per_level);
        if notional >= min_notional {
            return levels;
        }
    }

    0
}

fn quantize(v: f64) -> f64 {
    (v * 20.0).round() / 20.0
}

fn structure_hash(
    num_levels: usize,
    bid_sizes_q: &[u64],
    ask_sizes_q: &[u64],
    bid_spread_bps: f64,
    ask_spread_bps: f64,
) -> u64 {
    let mut h: u64 = num_levels as u64;
    for &s in bid_sizes_q {
        h = h.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(s);
    }
    for &s in ask_sizes_q {
        h = h
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(s.wrapping_add(1));
    }
    // Spread changes are intentionally coarse-grained. The engine can re-center
    // existing offsets with a mid-only update; tiny volatility changes should
    // not rebuild the whole book and spend another base fee.
    h = h
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add((bid_spread_bps / 25.0).round() as u64);
    h = h
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add((ask_spread_bps / 25.0).round() as u64);
    h
}

#[cfg(test)]
mod tests {
    use super::*;
    use solana_sdk::pubkey::Pubkey;

    fn market_config() -> MarketConfig {
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
        MarketConfig::from_header(
            market_pubkey,
            &header,
            9,
            6,
            spl_token::id(),
            spl_token::id(),
        )
    }

    fn strategy_settings() -> StrategySettings {
        StrategySettings {
            spread_levels_bps: vec![10.0, 20.0],
            inventory_pct: 50.0,
            vol_window: 300,
            vol_baseline_bps: 5.0,
            vol_max_multiplier: 5.0,
            intel_spread_add_multiplier: 1.0,
            max_intel_spread_add_bps: 80.0,
            max_intel_spread_tighten_bps: 0.0,
            max_intel_side_spread_add_bps: 40.0,
            min_effective_spread_bps: 0.0,
            min_net_edge_bps: 0.0,
            toxicity_buffer_bps: 0.0,
            min_intel_size_multiplier: 0.20,
            post_fill_cooldown_ms: 900_000,
            post_fill_side_size_multiplier: 0.0,
            post_fill_markout_check_ms: 900_000,
            post_fill_adverse_markout_bps: 12.0,
            post_fill_adverse_cooldown_ms: 3_600_000,
        }
    }

    #[test]
    fn skips_quotes_below_min_notional() {
        let risk = RiskSettings {
            min_quote_notional: 10.0,
            max_quote_notional_per_level: 25.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 20.0,
            min_quote_reserve_pct: 20.0,
        };
        let strategy = Strategy::new(&strategy_settings(), &risk);
        let (decision, _) = strategy.compute(
            100.0,
            0,
            0,
            &market_config(),
            100,
            100,
            0.0,
            IntelAdjustments::default(),
        );
        assert!(matches!(decision, QuoteDecision::ClearBook));
    }

    #[test]
    fn caps_quote_notional_per_level() {
        let risk = RiskSettings {
            min_quote_notional: 1.0,
            max_quote_notional_per_level: 12.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 0.0,
            min_quote_reserve_pct: 0.0,
        };
        let strategy = Strategy::new(&strategy_settings(), &risk);
        let (decision, _) = strategy.compute(
            100.0,
            0,
            0,
            &market_config(),
            10_000,
            100_000,
            0.0,
            IntelAdjustments::default(),
        );
        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 2);
                assert_eq!(book_update.ask_levels.len(), 2);
                assert!(book_update.bid_levels[0].size_in_base_lots <= 120);
                assert!(book_update.ask_levels[0].size_in_base_lots <= 120);
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn keeps_one_bid_when_quote_budget_cannot_support_all_levels() {
        let risk = RiskSettings {
            min_quote_notional: 8.0,
            max_quote_notional_per_level: 30.0,
            max_total_quote_notional: 130.0,
            min_base_reserve_pct: 20.0,
            min_quote_reserve_pct: 20.0,
        };
        let mut settings = strategy_settings();
        settings.inventory_pct = 35.0;
        let strategy = Strategy::new(&settings, &risk);

        let (decision, _) = strategy.compute(
            80.0,
            0,
            0,
            &market_config(),
            4102,
            53628,
            0.0,
            IntelAdjustments::default(),
        );

        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 1);
                assert_eq!(book_update.ask_levels.len(), 2);
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn one_sided_quotes_use_live_mid_instead_of_stale_cached_mid() {
        let risk = RiskSettings {
            min_quote_notional: 8.0,
            max_quote_notional_per_level: 30.0,
            max_total_quote_notional: 130.0,
            min_base_reserve_pct: 20.0,
            min_quote_reserve_pct: 20.0,
        };
        let mut settings = strategy_settings();
        settings.inventory_pct = 35.0;
        let strategy = Strategy::new(&settings, &risk);
        let config = market_config();
        let live_mid = 80.0;
        let stale_cached_mid_ticks = price_to_ticks(90.0, &config).unwrap();

        let (decision, _) = strategy.compute(
            live_mid,
            stale_cached_mid_ticks,
            0,
            &config,
            4_102,
            0,
            0.0,
            IntelAdjustments::default(),
        );

        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 0);
                assert_eq!(book_update.ask_levels.len(), 2);
                assert_eq!(
                    book_update.new_mid_price_ticks,
                    price_to_ticks(live_mid, &config).unwrap()
                );
                assert!(
                    book_update
                        .ask_levels
                        .iter()
                        .all(|level| level.price_offset_ticks > 0)
                );
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn market_intel_spread_add_widens_quote_offsets() {
        let risk = RiskSettings {
            min_quote_notional: 1.0,
            max_quote_notional_per_level: 12.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 0.0,
            min_quote_reserve_pct: 0.0,
        };
        let config = market_config();
        let strategy = Strategy::new(&strategy_settings(), &risk);

        let (neutral, _) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments::default(),
        );
        let (adjusted, _) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments {
                spread_add_bps: 30.0,
                ..IntelAdjustments::default()
            },
        );

        match (neutral, adjusted) {
            (
                QuoteDecision::UpdateFull {
                    book_update: neutral,
                    ..
                },
                QuoteDecision::UpdateFull {
                    book_update: adjusted,
                    ..
                },
            ) => {
                assert!(
                    adjusted.bid_levels[0].price_offset_ticks
                        < neutral.bid_levels[0].price_offset_ticks
                );
                assert!(
                    adjusted.ask_levels[0].price_offset_ticks
                        > neutral.ask_levels[0].price_offset_ticks
                );
            }
            _ => panic!("expected full updates"),
        }
    }

    #[test]
    fn market_intel_side_multiplier_reduces_ask_size_and_widens_ask() {
        let risk = RiskSettings {
            min_quote_notional: 1.0,
            max_quote_notional_per_level: 30.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 0.0,
            min_quote_reserve_pct: 0.0,
        };
        let config = market_config();
        let strategy = Strategy::new(&strategy_settings(), &risk);

        let (neutral, _) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments::default(),
        );
        let (decision, _) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments {
                size_multiplier: 0.75,
                bid_size_multiplier: 1.0,
                ask_size_multiplier: 0.55,
                spread_add_bps: 0.0,
            },
        );

        match (neutral, decision) {
            (
                QuoteDecision::UpdateFull {
                    book_update: neutral,
                    ..
                },
                QuoteDecision::UpdateFull { book_update, .. },
            ) => {
                assert_eq!(book_update.bid_levels.len(), 2);
                assert_eq!(book_update.ask_levels.len(), 2);
                assert!(
                    book_update.ask_levels[0].size_in_base_lots
                        < neutral.ask_levels[0].size_in_base_lots
                );
                assert!(
                    book_update.ask_levels[0].price_offset_ticks
                        > neutral.ask_levels[0].price_offset_ticks
                );
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn market_intel_bid_floor_suppresses_bids_when_base_heavy() {
        let risk = RiskSettings {
            min_quote_notional: 8.0,
            max_quote_notional_per_level: 20.0,
            max_total_quote_notional: 120.0,
            min_base_reserve_pct: 30.0,
            min_quote_reserve_pct: 30.0,
        };
        let mut settings = strategy_settings();
        settings.spread_levels_bps = vec![24.0, 42.0, 62.0];
        settings.inventory_pct = 24.0;
        settings.min_effective_spread_bps = 24.0;
        settings.min_net_edge_bps = 8.0;
        settings.toxicity_buffer_bps = 16.0;
        settings.max_intel_side_spread_add_bps = 25.0;
        let config = market_config();
        let strategy = Strategy::new(&settings, &risk);

        let (decision, _) = strategy.compute(
            79.58,
            0,
            0,
            &config,
            2_115,
            144_095,
            0.0,
            IntelAdjustments {
                size_multiplier: 0.75,
                bid_size_multiplier: 0.20,
                ask_size_multiplier: 1.0,
                spread_add_bps: 30.0,
            },
        );

        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 0);
                assert!(!book_update.ask_levels.is_empty());
            }
            _ => panic!("expected ask-only update"),
        }
    }

    #[test]
    fn market_intel_reduced_bid_side_suppresses_bids_when_base_heavy() {
        let risk = RiskSettings {
            min_quote_notional: 8.0,
            max_quote_notional_per_level: 20.0,
            max_total_quote_notional: 120.0,
            min_base_reserve_pct: 30.0,
            min_quote_reserve_pct: 30.0,
        };
        let mut settings = strategy_settings();
        settings.spread_levels_bps = vec![24.0, 42.0, 62.0];
        settings.inventory_pct = 24.0;
        settings.min_effective_spread_bps = 24.0;
        settings.min_net_edge_bps = 8.0;
        settings.toxicity_buffer_bps = 16.0;
        settings.max_intel_side_spread_add_bps = 25.0;
        let config = market_config();
        let strategy = Strategy::new(&settings, &risk);

        let (decision, _) = strategy.compute(
            79.64,
            0,
            0,
            &config,
            2_115,
            144_095,
            0.0,
            IntelAdjustments {
                size_multiplier: 0.75,
                bid_size_multiplier: 0.65,
                ask_size_multiplier: 1.0,
                spread_add_bps: 16.9,
            },
        );

        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 0);
                assert!(!book_update.ask_levels.is_empty());
            }
            _ => panic!("expected ask-only update"),
        }
    }

    #[test]
    fn market_intel_bid_floor_keeps_bids_when_quote_heavy() {
        let risk = RiskSettings {
            min_quote_notional: 8.0,
            max_quote_notional_per_level: 20.0,
            max_total_quote_notional: 120.0,
            min_base_reserve_pct: 30.0,
            min_quote_reserve_pct: 30.0,
        };
        let mut settings = strategy_settings();
        settings.spread_levels_bps = vec![24.0, 42.0, 62.0];
        settings.inventory_pct = 24.0;
        settings.min_effective_spread_bps = 24.0;
        settings.min_net_edge_bps = 8.0;
        settings.toxicity_buffer_bps = 16.0;
        settings.max_intel_side_spread_add_bps = 25.0;
        let config = market_config();
        let strategy = Strategy::new(&settings, &risk);

        let (decision, _) = strategy.compute(
            79.58,
            0,
            0,
            &config,
            1_000,
            500_000,
            0.0,
            IntelAdjustments {
                size_multiplier: 0.75,
                bid_size_multiplier: 0.20,
                ask_size_multiplier: 1.0,
                spread_add_bps: 30.0,
            },
        );

        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert!(!book_update.bid_levels.is_empty());
                assert!(!book_update.ask_levels.is_empty());
            }
            _ => panic!("expected two-sided update"),
        }
    }

    #[test]
    fn market_intel_scaled_budget_drops_sub_minimum_ask_level() {
        let risk = RiskSettings {
            min_quote_notional: 8.0,
            max_quote_notional_per_level: 18.0,
            max_total_quote_notional: 90.0,
            min_base_reserve_pct: 20.0,
            min_quote_reserve_pct: 20.0,
        };
        let mut settings = strategy_settings();
        settings.spread_levels_bps = vec![24.0, 42.0];
        settings.inventory_pct = 25.0;
        let config = market_config();
        let strategy = Strategy::new(&settings, &risk);

        let (decision, _) = strategy.compute(
            80.5,
            0,
            0,
            &config,
            1_825,
            167_415,
            0.0,
            IntelAdjustments {
                size_multiplier: 0.75,
                bid_size_multiplier: 1.0,
                ask_size_multiplier: 0.65,
                spread_add_bps: 15.0,
            },
        );

        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 2);
                assert_eq!(book_update.ask_levels.len(), 1);
                let ask_ticks = (book_update.new_mid_price_ticks as i64)
                    .saturating_add(book_update.ask_levels[0].price_offset_ticks)
                    .max(0) as u64;
                let ask_price = ask_ticks as f64 * config.ticks_to_price_factor();
                let ask_size =
                    base_lots_to_amount(book_update.ask_levels[0].size_in_base_lots, &config);
                assert!(ask_price * ask_size >= risk.min_quote_notional);
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn zero_ask_multiplier_removes_ask_side() {
        let risk = RiskSettings {
            min_quote_notional: 1.0,
            max_quote_notional_per_level: 30.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 0.0,
            min_quote_reserve_pct: 0.0,
        };
        let config = market_config();
        let strategy = Strategy::new(&strategy_settings(), &risk);

        let (decision, _) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments {
                ask_size_multiplier: 0.0,
                ..IntelAdjustments::default()
            },
        );

        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 2);
                assert_eq!(book_update.ask_levels.len(), 0);
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn minimum_effective_spread_floor_widens_tightest_level() {
        let risk = RiskSettings {
            min_quote_notional: 1.0,
            max_quote_notional_per_level: 30.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 0.0,
            min_quote_reserve_pct: 0.0,
        };
        let config = market_config();
        let mut settings = strategy_settings();
        settings.spread_levels_bps = vec![24.0, 42.0];
        settings.min_effective_spread_bps = 62.0;
        let strategy = Strategy::new(&settings, &risk);

        let (decision, tightest_spread) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments::default(),
        );

        assert_eq!(tightest_spread, 62.0);
        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                let bid_ticks = (book_update.new_mid_price_ticks as i64
                    + book_update.bid_levels[0].price_offset_ticks)
                    as u64;
                let second_bid_ticks = (book_update.new_mid_price_ticks as i64
                    + book_update.bid_levels[1].price_offset_ticks)
                    as u64;
                let bid_price = bid_ticks as f64 * config.ticks_to_price_factor();
                let second_bid_price = second_bid_ticks as f64 * config.ticks_to_price_factor();
                assert!(bid_price <= 99.38);
                assert!(second_bid_price < bid_price);
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn explicit_edge_budget_widens_tightest_level() {
        let risk = RiskSettings {
            min_quote_notional: 1.0,
            max_quote_notional_per_level: 30.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 0.0,
            min_quote_reserve_pct: 0.0,
        };
        let config = market_config();
        let mut settings = strategy_settings();
        settings.spread_levels_bps = vec![4.5, 9.5, 16.0];
        settings.min_effective_spread_bps = 0.0;
        settings.toxicity_buffer_bps = 1.5;
        settings.min_net_edge_bps = 1.2;
        let strategy = Strategy::new(&settings, &risk);

        let (decision, tightest_spread) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments::default(),
        );

        assert_eq!(tightest_spread, 4.5);
        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                assert_eq!(book_update.bid_levels.len(), 3);
                assert_eq!(book_update.ask_levels.len(), 3);
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn explicit_edge_budget_overrides_too_tight_configured_spread() {
        let risk = RiskSettings {
            min_quote_notional: 1.0,
            max_quote_notional_per_level: 30.0,
            max_total_quote_notional: 200.0,
            min_base_reserve_pct: 0.0,
            min_quote_reserve_pct: 0.0,
        };
        let config = market_config();
        let mut settings = strategy_settings();
        settings.spread_levels_bps = vec![1.0, 2.0];
        settings.min_effective_spread_bps = 0.0;
        settings.toxicity_buffer_bps = 1.5;
        settings.min_net_edge_bps = 1.2;
        let strategy = Strategy::new(&settings, &risk);

        let (decision, tightest_spread) = strategy.compute(
            100.0,
            0,
            0,
            &config,
            10_000,
            100_000,
            0.0,
            IntelAdjustments::default(),
        );

        assert!((tightest_spread - 2.7).abs() < 1e-9);
        match decision {
            QuoteDecision::UpdateFull { book_update, .. } => {
                let bid_ticks = (book_update.new_mid_price_ticks as i64
                    + book_update.bid_levels[0].price_offset_ticks)
                    as u64;
                let bid_price = bid_ticks as f64 * config.ticks_to_price_factor();
                assert!(bid_price <= 99.973);
            }
            _ => panic!("expected full update"),
        }
    }

    #[test]
    fn structure_hash_ignores_small_spread_bucket_changes() {
        let bid_sizes = vec![20, 0];
        let ask_sizes = vec![20, 20];

        assert_eq!(
            structure_hash(2, &bid_sizes, &ask_sizes, 24.0, 24.0),
            structure_hash(2, &bid_sizes, &ask_sizes, 29.0, 29.0)
        );
    }
}
