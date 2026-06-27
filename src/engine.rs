use std::sync::Arc;
use std::sync::atomic::Ordering::Relaxed;
use std::time::{Duration, Instant};

use crate::archer::{
    config::MarketConfig,
    ix_builder::{build_clear_book_ix, build_update_instructions, build_update_mid_price_ix},
    math::{BookUpdate, base_lots_to_amount, quote_lots_to_amount},
};
use solana_sdk::{pubkey::Pubkey, signature::Keypair, signer::Signer};
use tokio_util::sync::CancellationToken;

use crate::{
    config::MMConfig,
    state::SharedState,
    strategy::{IntelAdjustments, QuoteDecision, Strategy},
    tx::{TxCircuitReason, TxPriority, TxPurpose, TxSender},
};

const CU_CLEAR_BOOK: u32 = 650;
const CU_MID_ONLY: u32 = 850;
const CU_FULL_UPDATE: u32 = 5600;
const UPDATE_LANDING_GRACE_SECS: u64 = 20;

fn full_update_sequence_count(book_update: &BookUpdate) -> u64 {
    if book_update.mid_price_changed { 2 } else { 1 }
}

fn has_known_clearable_book(active_level_count: u64, expected_level_count: u64) -> bool {
    active_level_count > 0 || expected_level_count > 0
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
enum StaleFeedAction {
    Fresh,
    EnterHoldClear,
    EnterHoldNoClear,
    Hold,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
enum MissingLevelAction {
    Ignore,
    AcceptReducedDepth,
    Refresh,
    Throttle,
    RecoveryRefresh,
    RecoveryThrottle,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
enum FilledSide {
    Bid,
    Ask,
}

#[derive(Debug, Clone)]
struct PendingFillMarkout {
    side: FilledSide,
    price: f64,
    check_at: Instant,
}

fn detect_filled_side(
    previous_base_lots: u64,
    previous_quote_lots: u64,
    current_base_lots: u64,
    current_quote_lots: u64,
) -> Option<FilledSide> {
    let base_delta = current_base_lots as i128 - previous_base_lots as i128;
    let quote_delta = current_quote_lots as i128 - previous_quote_lots as i128;
    if base_delta > 0 && quote_delta < 0 {
        Some(FilledSide::Bid)
    } else if base_delta < 0 && quote_delta > 0 {
        Some(FilledSide::Ask)
    } else {
        None
    }
}

fn unsigned_abs_i128(value: i128) -> u64 {
    if value < 0 {
        (-value) as u64
    } else {
        value as u64
    }
}

fn fill_price_from_lot_deltas(
    base_delta_lots: i128,
    quote_delta_lots: i128,
    config: &MarketConfig,
) -> Option<f64> {
    let base = base_lots_to_amount(unsigned_abs_i128(base_delta_lots), config);
    let quote = quote_lots_to_amount(unsigned_abs_i128(quote_delta_lots), config);
    (base > 0.0 && quote > 0.0).then_some(quote / base)
}

fn fill_markout_bps(side: FilledSide, fill_price: f64, current_mid_price: f64) -> Option<f64> {
    if !fill_price.is_finite()
        || !current_mid_price.is_finite()
        || fill_price <= 0.0
        || current_mid_price <= 0.0
    {
        return None;
    }
    let markout = match side {
        FilledSide::Bid => (current_mid_price - fill_price) / fill_price * 10_000.0,
        FilledSide::Ask => (fill_price - current_mid_price) / fill_price * 10_000.0,
    };
    Some(markout)
}

fn missing_level_action(
    active_bid_count: u64,
    active_ask_count: u64,
    expected_bid_count: u64,
    expected_ask_count: u64,
    update_landed: bool,
    refresh_too_soon: bool,
    recovery_too_soon: bool,
) -> MissingLevelAction {
    if !update_landed || expected_bid_count + expected_ask_count == 0 {
        return MissingLevelAction::Ignore;
    }
    if active_bid_count >= expected_bid_count && active_ask_count >= expected_ask_count {
        return MissingLevelAction::Ignore;
    }

    let active_level_count = active_bid_count + active_ask_count;
    if active_level_count > 0 {
        let bid_side_empty = expected_bid_count > 0 && active_bid_count == 0;
        let ask_side_empty = expected_ask_count > 0 && active_ask_count == 0;
        if bid_side_empty || ask_side_empty {
            return if recovery_too_soon {
                MissingLevelAction::RecoveryThrottle
            } else {
                MissingLevelAction::RecoveryRefresh
            };
        }
        return MissingLevelAction::AcceptReducedDepth;
    }
    if refresh_too_soon {
        return MissingLevelAction::Throttle;
    }
    MissingLevelAction::Refresh
}

fn should_continue_after_missing_level_action(
    action: MissingLevelAction,
    post_fill_cooldown_active: bool,
) -> bool {
    match action {
        MissingLevelAction::Ignore
        | MissingLevelAction::Refresh
        | MissingLevelAction::RecoveryRefresh => false,
        MissingLevelAction::AcceptReducedDepth
        | MissingLevelAction::Throttle
        | MissingLevelAction::RecoveryThrottle => !post_fill_cooldown_active,
    }
}

fn should_throttle_full_update(
    expected_bid_count: u64,
    expected_ask_count: u64,
    new_bid_count: u64,
    new_ask_count: u64,
    force_next_full_update: bool,
    full_refresh_interval_active: bool,
) -> bool {
    let expected_level_count = expected_bid_count + expected_ask_count;
    if expected_level_count == 0 || force_next_full_update || !full_refresh_interval_active {
        return false;
    }

    let reduces_exposure = new_bid_count < expected_bid_count || new_ask_count < expected_ask_count;
    let flips_empty_side = (expected_bid_count == 0
        && expected_ask_count > 0
        && new_bid_count > 0
        && new_ask_count == 0)
        || (expected_ask_count == 0
            && expected_bid_count > 0
            && new_ask_count > 0
            && new_bid_count == 0);
    let restores_empty_side = (expected_bid_count == 0 && new_bid_count > 0 && new_ask_count > 0)
        || (expected_ask_count == 0 && new_ask_count > 0 && new_bid_count > 0);
    !(restores_empty_side || (reduces_exposure && !flips_empty_side))
}

fn skip_mid_update(
    last_sent_mid_ticks: u64,
    mid_delta: u64,
    min_mid_update_ticks: u64,
    too_soon: bool,
) -> bool {
    let too_small = last_sent_mid_ticks > 0 && mid_delta < min_mid_update_ticks;
    let emergency_move =
        last_sent_mid_ticks > 0 && mid_delta >= min_mid_update_ticks.saturating_mul(4).max(1);
    (too_soon && !emergency_move) || too_small
}

#[derive(Debug, Default)]
struct StaleFeedGuard {
    holding: bool,
}

impl StaleFeedGuard {
    fn evaluate(
        &mut self,
        has_price: bool,
        price_age_us: u64,
        staleness_us: u64,
        has_clearable_book: bool,
    ) -> StaleFeedAction {
        let is_stale = has_price && price_age_us > staleness_us;
        if !is_stale {
            self.holding = false;
            return StaleFeedAction::Fresh;
        }
        if self.holding {
            return StaleFeedAction::Hold;
        }
        self.holding = true;
        if has_clearable_book {
            StaleFeedAction::EnterHoldClear
        } else {
            StaleFeedAction::EnterHoldNoClear
        }
    }
}

pub async fn run_engine(
    state: Arc<SharedState>,
    sdk_config: Arc<MarketConfig>,
    mm_config: Arc<MMConfig>,
    signer: Arc<Keypair>,
    maker_pubkey: Pubkey,
    market_pubkey: Pubkey,
    tx_sender: Arc<TxSender>,
    initial_sequence_number: u64,
    cancel: CancellationToken,
) {
    let strategy = Strategy::new(&mm_config.strategy, &mm_config.risk);
    let heartbeat = Duration::from_millis(mm_config.execution.heartbeat_interval_ms);
    let min_mid_update_interval =
        Duration::from_millis(mm_config.execution.min_mid_update_interval_ms);
    let min_full_refresh_interval =
        Duration::from_millis(mm_config.execution.min_full_refresh_interval_ms);
    let min_empty_side_recovery_interval =
        Duration::from_millis(mm_config.execution.min_empty_side_recovery_interval_ms);
    let min_mid_update_ticks = mm_config.execution.min_mid_update_ticks;
    let post_fill_cooldown = Duration::from_millis(mm_config.strategy.post_fill_cooldown_ms);
    let post_fill_side_size_multiplier = mm_config
        .strategy
        .post_fill_side_size_multiplier
        .clamp(0.0, 1.0);
    let post_fill_markout_check =
        Duration::from_millis(mm_config.strategy.post_fill_markout_check_ms);
    let post_fill_adverse_markout_bps = mm_config.strategy.post_fill_adverse_markout_bps.max(0.0);
    let post_fill_adverse_cooldown =
        Duration::from_millis(mm_config.strategy.post_fill_adverse_cooldown_ms);
    let signer_pubkey = signer.pubkey();
    let staleness_us = mm_config.feed.staleness_timeout_ms * 1000;

    let mut last_structure_hash: u64 = 0;
    let mut last_sent_mid_ticks: u64 = 0;
    let mut last_expected_bid_count: u64 = 0;
    let mut last_expected_ask_count: u64 = 0;
    let mut last_normal_update_at: Option<Instant> = None;
    let mut last_full_update_at: Option<Instant> = None;
    let mut last_missing_level_refresh_at: Option<Instant> = None;
    let mut force_next_full_update: bool = false;
    let mut force_recovery_update: bool = false;
    let mut needs_initial_book: bool = true;
    let mut local_seq: u64 = initial_sequence_number;
    let mut stale_feed_guard = StaleFeedGuard::default();
    let mut failure_clear_sent = false;
    let mut last_clear_book_at: Option<Instant> = None;
    let mut last_observed_base_lots: u64 = 0;
    let mut last_observed_quote_lots: u64 = 0;
    let mut bid_fill_cooldown_until: Option<Instant> = None;
    let mut ask_fill_cooldown_until: Option<Instant> = None;
    let mut pending_fill_markouts: Vec<PendingFillMarkout> = Vec::new();

    tracing::info!(
        %market_pubkey, %maker_pubkey,
        heartbeat_ms = mm_config.execution.heartbeat_interval_ms,
        min_mid_update_interval_ms = mm_config.execution.min_mid_update_interval_ms,
        min_mid_update_ticks,
        num_levels = mm_config.strategy.spread_levels_bps.len(),
        "Engine starting (event-driven + heartbeat)"
    );

    state.engine_alive.store(true, Relaxed);

    loop {
        // Wait for either a price update or the heartbeat timeout.
        let is_heartbeat = tokio::select! {
            _ = cancel.cancelled() => {
                state.engine_alive.store(false, Relaxed);
                let active_level_count = state.active_bid_levels.load(Relaxed)
                    + state.active_ask_levels.load(Relaxed);
                let expected_level_count = last_expected_bid_count + last_expected_ask_count;
                if has_known_clearable_book(active_level_count, expected_level_count) {
                    tracing::info!("Engine shutting down, clearing book");
                    local_seq += 1;
                    let ix = build_clear_book_ix(&signer_pubkey, &market_pubkey, &maker_pubkey, local_seq);
                    tx_sender.fire(
                        vec![ix],
                        TxPriority::Emergency,
                        TxPurpose::SafetyClearBook,
                        CU_CLEAR_BOOK,
                    );
                } else {
                    tracing::info!("Engine shutting down with no known live or expected book; skipping clear");
                }
                return;
            }
            _ = state.price_notify.notified() => false,
            _ = tokio::time::sleep(heartbeat) => true,
        };

        if cancel.is_cancelled() {
            continue; // will hit the cancel branch above
        }

        let active_bid_count = state.active_bid_levels.load(Relaxed);
        let active_ask_count = state.active_ask_levels.load(Relaxed);
        let active_level_count = active_bid_count + active_ask_count;
        let expected_level_count = last_expected_bid_count + last_expected_ask_count;
        let has_clearable_book = has_known_clearable_book(active_level_count, expected_level_count);
        let current_base_lots = state.base_total_lots.load(Relaxed);
        let current_quote_lots = state.quote_total_lots.load(Relaxed);

        if last_observed_base_lots == 0 && last_observed_quote_lots == 0 {
            last_observed_base_lots = current_base_lots;
            last_observed_quote_lots = current_quote_lots;
        } else if current_base_lots != last_observed_base_lots
            || current_quote_lots != last_observed_quote_lots
        {
            let base_delta_lots = current_base_lots as i128 - last_observed_base_lots as i128;
            let quote_delta_lots = current_quote_lots as i128 - last_observed_quote_lots as i128;
            if post_fill_cooldown > Duration::ZERO {
                match detect_filled_side(
                    last_observed_base_lots,
                    last_observed_quote_lots,
                    current_base_lots,
                    current_quote_lots,
                ) {
                    Some(FilledSide::Bid) => {
                        bid_fill_cooldown_until = Some(Instant::now() + post_fill_cooldown);
                        tracing::warn!(
                            base_delta_lots,
                            quote_delta_lots,
                            cooldown_ms = post_fill_cooldown.as_millis(),
                            "Bid-side fill detected; cooling down bid quotes"
                        );
                        if post_fill_markout_check > Duration::ZERO {
                            if let Some(price) = fill_price_from_lot_deltas(
                                base_delta_lots,
                                quote_delta_lots,
                                &sdk_config,
                            ) {
                                pending_fill_markouts.push(PendingFillMarkout {
                                    side: FilledSide::Bid,
                                    price,
                                    check_at: Instant::now() + post_fill_markout_check,
                                });
                            }
                        }
                    }
                    Some(FilledSide::Ask) => {
                        ask_fill_cooldown_until = Some(Instant::now() + post_fill_cooldown);
                        tracing::warn!(
                            base_delta_lots,
                            quote_delta_lots,
                            cooldown_ms = post_fill_cooldown.as_millis(),
                            "Ask-side fill detected; cooling down ask quotes"
                        );
                        if post_fill_markout_check > Duration::ZERO {
                            if let Some(price) = fill_price_from_lot_deltas(
                                base_delta_lots,
                                quote_delta_lots,
                                &sdk_config,
                            ) {
                                pending_fill_markouts.push(PendingFillMarkout {
                                    side: FilledSide::Ask,
                                    price,
                                    check_at: Instant::now() + post_fill_markout_check,
                                });
                            }
                        }
                    }
                    None => {}
                }
            }
            last_observed_base_lots = current_base_lots;
            last_observed_quote_lots = current_quote_lots;
        }

        if state.consecutive_failures.load(Relaxed) >= 10 {
            if !failure_clear_sent && has_clearable_book {
                local_seq += 1;
                let ix =
                    build_clear_book_ix(&signer_pubkey, &market_pubkey, &maker_pubkey, local_seq);
                tx_sender.fire(
                    vec![ix],
                    TxPriority::Emergency,
                    TxPurpose::SafetyClearBook,
                    CU_CLEAR_BOOK,
                );
                state.clear_book_sends.fetch_add(1, Relaxed);
                failure_clear_sent = true;
                last_clear_book_at = Some(Instant::now());
            }
            state.tx_circuit_open.store(true, Relaxed);
            state
                .tx_circuit_reason
                .store(TxCircuitReason::ConsecutiveFailures as u64, Relaxed);
            needs_initial_book = true;
            last_structure_hash = 0;
            last_expected_bid_count = 0;
            last_expected_ask_count = 0;
            tokio::select! {
                _ = cancel.cancelled() => return,
                _ = tokio::time::sleep(Duration::from_secs(1)) => {}
            }
            continue;
        }
        if state.consecutive_failures.load(Relaxed) == 0 {
            failure_clear_sent = false;
        }

        let price_timestamp_us = state.price_timestamp_us.load(Relaxed);
        let price_age_us = crate::state::now_us().saturating_sub(price_timestamp_us);
        let was_stale_holding = state.price_feed_stale_holding.load(Relaxed);
        match stale_feed_guard.evaluate(
            price_timestamp_us > 0,
            price_age_us,
            staleness_us,
            has_clearable_book,
        ) {
            StaleFeedAction::Fresh => {
                if was_stale_holding {
                    tracing::info!("Price feed fresh; leaving stale-feed hold");
                    state.price_feed_stale_holding.store(false, Relaxed);
                }
            }
            StaleFeedAction::EnterHoldClear => {
                tracing::warn!(
                    age_ms = price_age_us / 1000,
                    "Price feed stale, clearing book once and entering hold"
                );
                state.price_feed_stale_holding.store(true, Relaxed);
                state.price_feed_stale_episodes.fetch_add(1, Relaxed);
                local_seq += 1;
                let ix =
                    build_clear_book_ix(&signer_pubkey, &market_pubkey, &maker_pubkey, local_seq);
                tx_sender.fire(
                    vec![ix],
                    TxPriority::Emergency,
                    TxPurpose::SafetyClearBook,
                    CU_CLEAR_BOOK,
                );
                state.clear_book_sends.fetch_add(1, Relaxed);
                last_clear_book_at = Some(Instant::now());
                needs_initial_book = true;
                last_structure_hash = 0;
                last_expected_bid_count = 0;
                last_expected_ask_count = 0;
                continue;
            }
            StaleFeedAction::EnterHoldNoClear => {
                tracing::warn!(
                    age_ms = price_age_us / 1000,
                    "Price feed stale, entering hold with no active book to clear"
                );
                state.price_feed_stale_holding.store(true, Relaxed);
                state.price_feed_stale_episodes.fetch_add(1, Relaxed);
                needs_initial_book = true;
                last_structure_hash = 0;
                last_expected_bid_count = 0;
                last_expected_ask_count = 0;
                continue;
            }
            StaleFeedAction::Hold => {
                state.cycles_total.fetch_add(1, Relaxed);
                continue;
            }
        }

        let mid_price = state.mid_price.load(Relaxed);
        if mid_price <= 0.0 || !mid_price.is_finite() {
            state.cycles_total.fetch_add(1, Relaxed);
            continue;
        }

        let onchain_seq = state.onchain_sequence_number.load(Relaxed);
        if onchain_seq > local_seq {
            local_seq = onchain_seq;
            if !needs_initial_book {
                tracing::debug!(
                    onchain_seq,
                    active_bid_count,
                    active_ask_count,
                    "MakerBook sequence advanced; level health check will decide whether to refresh"
                );
            }
        }

        let now = Instant::now();
        if post_fill_adverse_cooldown > Duration::ZERO && !pending_fill_markouts.is_empty() {
            let mut remaining: Vec<PendingFillMarkout> =
                Vec::with_capacity(pending_fill_markouts.len());
            for pending in pending_fill_markouts.drain(..) {
                if pending.check_at > now {
                    remaining.push(pending);
                    continue;
                }

                if let Some(markout_bps) = fill_markout_bps(pending.side, pending.price, mid_price)
                {
                    if markout_bps <= -post_fill_adverse_markout_bps {
                        match pending.side {
                            FilledSide::Bid => {
                                bid_fill_cooldown_until = Some(now + post_fill_adverse_cooldown);
                            }
                            FilledSide::Ask => {
                                ask_fill_cooldown_until = Some(now + post_fill_adverse_cooldown);
                            }
                        }
                        tracing::warn!(
                            side = ?pending.side,
                            fill_price = pending.price,
                            mid_price,
                            markout_bps,
                            threshold_bps = post_fill_adverse_markout_bps,
                            cooldown_ms = post_fill_adverse_cooldown.as_millis(),
                            "Adverse fill markout detected; extending side cooldown"
                        );
                    } else {
                        tracing::info!(
                            side = ?pending.side,
                            fill_price = pending.price,
                            mid_price,
                            markout_bps,
                            threshold_bps = post_fill_adverse_markout_bps,
                            "Fill markout check passed"
                        );
                    }
                }
            }
            pending_fill_markouts = remaining;
        }
        let bid_fill_cooldown_active = bid_fill_cooldown_until
            .map(|until| until > now)
            .unwrap_or(false);
        let ask_fill_cooldown_active = ask_fill_cooldown_until
            .map(|until| until > now)
            .unwrap_or(false);
        let post_fill_cooldown_active = bid_fill_cooldown_active || ask_fill_cooldown_active;

        let update_landed = last_normal_update_at
            .map(|last| last.elapsed() > Duration::from_secs(UPDATE_LANDING_GRACE_SECS))
            .unwrap_or(true);
        if !needs_initial_book {
            let refresh_too_soon = last_missing_level_refresh_at
                .map(|last| last.elapsed() < min_full_refresh_interval)
                .unwrap_or(false);
            let recovery_too_soon = last_missing_level_refresh_at
                .map(|last| last.elapsed() < min_empty_side_recovery_interval)
                .unwrap_or(false);
            match missing_level_action(
                active_bid_count,
                active_ask_count,
                last_expected_bid_count,
                last_expected_ask_count,
                update_landed,
                refresh_too_soon,
                recovery_too_soon,
            ) {
                MissingLevelAction::Ignore => {}
                MissingLevelAction::AcceptReducedDepth => {
                    tracing::info!(
                        active_bid_count,
                        active_ask_count,
                        last_expected_bid_count,
                        last_expected_ask_count,
                        "Partial Archer fill detected; accepting reduced active depth to avoid refresh churn"
                    );
                    if should_continue_after_missing_level_action(
                        MissingLevelAction::AcceptReducedDepth,
                        post_fill_cooldown_active,
                    ) {
                        last_expected_bid_count = active_bid_count;
                        last_expected_ask_count = active_ask_count;
                        state.cycles_total.fetch_add(1, Relaxed);
                        continue;
                    }
                    tracing::info!(
                        bid_fill_cooldown_active,
                        ask_fill_cooldown_active,
                        "Post-fill cooldown active; applying risk-reducing quote update instead of accepting reduced depth"
                    );
                    force_next_full_update = true;
                    last_structure_hash = 0;
                }
                MissingLevelAction::Throttle => {
                    tracing::debug!(
                        active_bid_count,
                        active_ask_count,
                        last_expected_bid_count,
                        last_expected_ask_count,
                        min_full_refresh_interval_ms = min_full_refresh_interval.as_millis(),
                        "Detected missing Archer levels but full book refresh is throttled"
                    );
                    if should_continue_after_missing_level_action(
                        MissingLevelAction::Throttle,
                        post_fill_cooldown_active,
                    ) {
                        state.cycles_total.fetch_add(1, Relaxed);
                        continue;
                    }
                    tracing::info!(
                        bid_fill_cooldown_active,
                        ask_fill_cooldown_active,
                        "Post-fill cooldown active; bypassing missing-level throttle for risk reduction"
                    );
                    force_next_full_update = true;
                    last_structure_hash = 0;
                }
                MissingLevelAction::RecoveryRefresh => {
                    tracing::info!(
                        active_bid_count,
                        active_ask_count,
                        last_expected_bid_count,
                        last_expected_ask_count,
                        "Detected empty Archer quote side, forcing recovery refresh"
                    );
                    last_missing_level_refresh_at = Some(Instant::now());
                    force_next_full_update = true;
                    force_recovery_update = true;
                    needs_initial_book = true;
                    last_structure_hash = 0;
                }
                MissingLevelAction::RecoveryThrottle => {
                    tracing::debug!(
                        active_bid_count,
                        active_ask_count,
                        last_expected_bid_count,
                        last_expected_ask_count,
                        min_empty_side_recovery_interval_ms =
                            min_empty_side_recovery_interval.as_millis(),
                        "Detected empty Archer quote side but recovery refresh is throttled"
                    );
                    if should_continue_after_missing_level_action(
                        MissingLevelAction::RecoveryThrottle,
                        post_fill_cooldown_active,
                    ) {
                        state.cycles_total.fetch_add(1, Relaxed);
                        continue;
                    }
                    force_next_full_update = true;
                    force_recovery_update = true;
                    last_structure_hash = 0;
                }
                MissingLevelAction::Refresh => {
                    tracing::info!(
                        active_bid_count,
                        active_ask_count,
                        last_expected_bid_count,
                        last_expected_ask_count,
                        "Detected empty Archer quote side, forcing one recovery refresh"
                    );
                    last_missing_level_refresh_at = Some(Instant::now());
                    force_next_full_update = true;
                    needs_initial_book = true;
                    last_structure_hash = 0;
                }
            }
        }

        let cached_mid = state.cached_mid_ticks.load(Relaxed);
        let reference_mid = if cached_mid > 0 {
            cached_mid
        } else {
            last_sent_mid_ticks
        };
        let effective_hash = if needs_initial_book {
            0
        } else {
            last_structure_hash
        };

        let volatility_bps = state.volatility_bps.load(Relaxed);
        let mut intel = IntelAdjustments {
            spread_add_bps: state.intel_spread_add_bps.load(Relaxed),
            size_multiplier: state.intel_size_multiplier.load(Relaxed),
            bid_size_multiplier: state.intel_bid_size_multiplier.load(Relaxed),
            ask_size_multiplier: state.intel_ask_size_multiplier.load(Relaxed),
        };
        if bid_fill_cooldown_active {
            intel.bid_size_multiplier *= post_fill_side_size_multiplier;
        }
        if ask_fill_cooldown_active {
            intel.ask_size_multiplier *= post_fill_side_size_multiplier;
        }
        let (decision, _spread_bps) = strategy.compute(
            mid_price,
            reference_mid,
            effective_hash,
            &sdk_config,
            current_base_lots,
            current_quote_lots,
            volatility_bps,
            intel,
        );

        match decision {
            QuoteDecision::ClearBook => {
                let clear_recently_sent = last_clear_book_at
                    .map(|last| last.elapsed() < Duration::from_secs(UPDATE_LANDING_GRACE_SECS))
                    .unwrap_or(false);
                if !has_clearable_book {
                    tracing::debug!(
                        "Strategy requested clear but no live or expected book levels are known"
                    );
                } else if clear_recently_sent {
                    tracing::debug!(
                        active_bid_count,
                        active_ask_count,
                        expected_level_count,
                        "Strategy requested clear while a clear is still landing; suppressing duplicate"
                    );
                } else {
                    local_seq += 1;
                    let ix = build_clear_book_ix(
                        &signer_pubkey,
                        &market_pubkey,
                        &maker_pubkey,
                        local_seq,
                    );
                    tx_sender.fire(
                        vec![ix],
                        TxPriority::Normal,
                        TxPurpose::SafetyClearBook,
                        CU_CLEAR_BOOK,
                    );
                    state.clear_book_sends.fetch_add(1, Relaxed);
                    state.updates_sent.fetch_add(1, Relaxed);
                    last_clear_book_at = Some(Instant::now());
                }
                last_structure_hash = 0;
                last_expected_bid_count = 0;
                last_expected_ask_count = 0;
                last_normal_update_at = Some(Instant::now());
                needs_initial_book = true;
            }
            QuoteDecision::UpdateMidOnly { new_mid_ticks } => {
                // Skip if ticks unchanged (price moved but not enough to change tick).
                if new_mid_ticks == last_sent_mid_ticks {
                    state.cycles_total.fetch_add(1, Relaxed);
                    continue;
                }
                let mid_delta = new_mid_ticks.abs_diff(last_sent_mid_ticks);
                let too_soon = last_normal_update_at
                    .map(|last| last.elapsed() < min_mid_update_interval)
                    .unwrap_or(false);
                if skip_mid_update(
                    last_sent_mid_ticks,
                    mid_delta,
                    min_mid_update_ticks,
                    too_soon,
                ) {
                    tracing::debug!(
                        mid_delta,
                        min_mid_update_ticks,
                        min_mid_update_interval_ms = min_mid_update_interval.as_millis(),
                        "Skipping mid-only update below throttle threshold"
                    );
                    state.cycles_total.fetch_add(1, Relaxed);
                    continue;
                }
                local_seq += 1;
                let ix = build_update_mid_price_ix(
                    &signer_pubkey,
                    &market_pubkey,
                    &maker_pubkey,
                    new_mid_ticks,
                    local_seq,
                );
                tx_sender.fire(vec![ix], TxPriority::Normal, TxPurpose::Update, CU_MID_ONLY);
                state.mid_only_updates.fetch_add(1, Relaxed);
                state.updates_sent.fetch_add(1, Relaxed);
                if is_heartbeat {
                    state.heartbeat_sends.fetch_add(1, Relaxed);
                }
                last_sent_mid_ticks = new_mid_ticks;
                last_normal_update_at = Some(Instant::now());
            }
            QuoteDecision::UpdateFull {
                ref book_update,
                structure_hash,
            } => {
                let new_bid_count = book_update.bid_levels.len() as u64;
                let new_ask_count = book_update.ask_levels.len() as u64;
                let full_refresh_interval_active = last_full_update_at
                    .map(|last| last.elapsed() < min_full_refresh_interval)
                    .unwrap_or(false);
                let full_refresh_too_soon = should_throttle_full_update(
                    last_expected_bid_count,
                    last_expected_ask_count,
                    new_bid_count,
                    new_ask_count,
                    force_next_full_update,
                    full_refresh_interval_active,
                );
                if full_refresh_too_soon {
                    tracing::debug!(
                        min_full_refresh_interval_ms = min_full_refresh_interval.as_millis(),
                        bid_levels = book_update.bid_levels.len(),
                        ask_levels = book_update.ask_levels.len(),
                        "Skipping full book update below throttle threshold"
                    );
                    state.cycles_total.fetch_add(1, Relaxed);
                    continue;
                }
                if full_refresh_interval_active
                    && (new_bid_count < last_expected_bid_count
                        || new_ask_count < last_expected_ask_count)
                {
                    tracing::info!(
                        previous_bid_levels = last_expected_bid_count,
                        previous_ask_levels = last_expected_ask_count,
                        new_bid_levels = new_bid_count,
                        new_ask_levels = new_ask_count,
                        "Applying risk-reducing book update before full-refresh throttle"
                    );
                }

                let next_sequence_number = local_seq.saturating_add(1);
                let sequence_count = full_update_sequence_count(book_update);
                match build_update_instructions(
                    book_update,
                    &market_pubkey,
                    &maker_pubkey,
                    &signer_pubkey,
                    next_sequence_number,
                ) {
                    Ok(ixs) if !ixs.is_empty() => {
                        let purpose = if force_recovery_update {
                            TxPurpose::RecoveryUpdate
                        } else {
                            TxPurpose::Update
                        };
                        tx_sender.fire(ixs, TxPriority::Normal, purpose, CU_FULL_UPDATE);
                        local_seq = local_seq.saturating_add(sequence_count);
                        state.book_updates.fetch_add(1, Relaxed);
                        state.updates_sent.fetch_add(1, Relaxed);
                        last_sent_mid_ticks = book_update.new_mid_price_ticks;
                        last_structure_hash = structure_hash;
                        last_expected_bid_count = book_update.bid_levels.len() as u64;
                        last_expected_ask_count = book_update.ask_levels.len() as u64;
                        last_full_update_at = Some(Instant::now());
                        last_normal_update_at = Some(Instant::now());
                        force_next_full_update = false;
                        force_recovery_update = false;
                        needs_initial_book = false;
                    }
                    Ok(_) => {}
                    Err(e) => {
                        tracing::warn!("build_update_instructions error: {e}");
                    }
                }
            }
        }

        state.cycles_total.fetch_add(1, Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_feed_guard_clears_once_then_holds() {
        let mut guard = StaleFeedGuard::default();

        assert_eq!(
            guard.evaluate(true, 20_000, 15_000, true),
            StaleFeedAction::EnterHoldClear
        );
        assert_eq!(
            guard.evaluate(true, 21_000, 15_000, true),
            StaleFeedAction::Hold
        );
        assert_eq!(
            guard.evaluate(true, 22_000, 15_000, true),
            StaleFeedAction::Hold
        );
    }

    #[test]
    fn stale_feed_guard_rearms_after_fresh_price() {
        let mut guard = StaleFeedGuard::default();

        assert_eq!(
            guard.evaluate(true, 20_000, 15_000, true),
            StaleFeedAction::EnterHoldClear
        );
        assert_eq!(
            guard.evaluate(true, 1_000, 15_000, true),
            StaleFeedAction::Fresh
        );
        assert_eq!(
            guard.evaluate(true, 20_000, 15_000, true),
            StaleFeedAction::EnterHoldClear
        );
    }

    #[test]
    fn stale_feed_guard_skips_clear_without_known_live_book() {
        let mut guard = StaleFeedGuard::default();

        assert_eq!(
            guard.evaluate(true, 20_000, 15_000, false),
            StaleFeedAction::EnterHoldNoClear
        );
        assert_eq!(
            guard.evaluate(true, 21_000, 15_000, true),
            StaleFeedAction::Hold
        );
    }

    #[test]
    fn clear_decision_requires_known_live_or_expected_book() {
        assert!(!has_known_clearable_book(0, 0));
        assert!(has_known_clearable_book(1, 0));
        assert!(has_known_clearable_book(0, 1));
    }

    #[test]
    fn detects_bid_fill_from_balance_delta() {
        assert_eq!(
            detect_filled_side(1_000, 10_000, 1_100, 9_000),
            Some(FilledSide::Bid)
        );
    }

    #[test]
    fn detects_ask_fill_from_balance_delta() {
        assert_eq!(
            detect_filled_side(1_000, 10_000, 900, 11_000),
            Some(FilledSide::Ask)
        );
    }

    #[test]
    fn ignores_non_fill_balance_delta() {
        assert_eq!(detect_filled_side(1_000, 10_000, 1_100, 11_000), None);
        assert_eq!(detect_filled_side(1_000, 10_000, 900, 9_000), None);
    }

    #[test]
    fn computes_side_aware_fill_markout() {
        assert_eq!(
            fill_markout_bps(FilledSide::Bid, 100.0, 101.0).unwrap(),
            100.0
        );
        assert_eq!(
            fill_markout_bps(FilledSide::Ask, 100.0, 101.0).unwrap(),
            -100.0
        );
        assert_eq!(fill_markout_bps(FilledSide::Ask, 0.0, 101.0), None);
    }

    #[test]
    fn full_update_with_mid_change_consumes_two_sequence_numbers() {
        let update = BookUpdate {
            new_mid_price_ticks: 100,
            bid_levels: Vec::new(),
            ask_levels: Vec::new(),
            mid_price_changed: true,
        };

        assert_eq!(full_update_sequence_count(&update), 2);
    }

    #[test]
    fn full_update_without_mid_change_consumes_one_sequence_number() {
        let update = BookUpdate {
            new_mid_price_ticks: 100,
            bid_levels: Vec::new(),
            ask_levels: Vec::new(),
            mid_price_changed: false,
        };

        assert_eq!(full_update_sequence_count(&update), 1);
    }

    #[test]
    fn partial_fill_accepts_reduced_depth_instead_of_refreshing() {
        assert_eq!(
            missing_level_action(1, 1, 1, 2, true, false, false),
            MissingLevelAction::AcceptReducedDepth
        );
        assert!(should_continue_after_missing_level_action(
            MissingLevelAction::AcceptReducedDepth,
            false
        ));
        assert!(!should_continue_after_missing_level_action(
            MissingLevelAction::AcceptReducedDepth,
            true
        ));
    }

    #[test]
    fn empty_quote_side_requests_recovery_refresh_instead_of_accepting_reduced_depth() {
        assert_eq!(
            missing_level_action(1, 0, 1, 2, true, false, false),
            MissingLevelAction::RecoveryRefresh
        );
        assert!(!should_continue_after_missing_level_action(
            MissingLevelAction::RecoveryRefresh,
            false
        ));
        assert_eq!(
            missing_level_action(1, 0, 1, 2, true, false, true),
            MissingLevelAction::RecoveryThrottle
        );
    }

    #[test]
    fn fully_missing_book_requests_refresh_after_cooldown() {
        assert_eq!(
            missing_level_action(0, 0, 1, 2, true, false, false),
            MissingLevelAction::Refresh
        );
    }

    #[test]
    fn fully_missing_book_respects_refresh_throttle() {
        assert_eq!(
            missing_level_action(1, 0, 1, 2, true, true, true),
            MissingLevelAction::RecoveryThrottle
        );
        assert_eq!(
            missing_level_action(0, 0, 1, 2, true, true, false),
            MissingLevelAction::Throttle
        );
        assert!(!should_continue_after_missing_level_action(
            MissingLevelAction::Throttle,
            true
        ));
    }

    #[test]
    fn full_update_throttle_allows_risk_reduction() {
        assert!(!should_throttle_full_update(2, 2, 2, 0, false, true));
    }

    #[test]
    fn full_update_throttle_allows_restoring_two_sided_book_after_soft_pause() {
        assert!(!should_throttle_full_update(0, 1, 1, 1, false, true));
        assert!(!should_throttle_full_update(1, 0, 1, 1, false, true));
    }

    #[test]
    fn full_update_throttle_blocks_same_or_larger_exposure() {
        assert!(should_throttle_full_update(1, 1, 1, 1, false, true));
        assert!(should_throttle_full_update(1, 1, 2, 1, false, true));
        assert!(should_throttle_full_update(0, 1, 1, 0, false, true));
    }

    #[test]
    fn mid_update_requires_interval_unless_move_is_extreme() {
        assert!(skip_mid_update(10_000, 300, 260, true));
        assert!(!skip_mid_update(10_000, 1_100, 260, true));
        assert!(skip_mid_update(10_000, 100, 260, false));
        assert!(!skip_mid_update(10_000, 300, 260, false));
    }
}
