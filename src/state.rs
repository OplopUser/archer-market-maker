use portable_atomic::AtomicF64;
use std::sync::atomic::{AtomicBool, AtomicU64};
use tokio::sync::Notify;

pub struct SharedState {
    pub mid_price: AtomicF64,
    pub price_timestamp_us: AtomicU64,
    pub feed_alive: AtomicBool,

    /// Feed signals the engine whenever a new price arrives.
    pub price_notify: Notify,

    pub cached_mid_ticks: AtomicU64,
    pub base_total_lots: AtomicU64,
    pub quote_total_lots: AtomicU64,
    pub active_bid_levels: AtomicU64,
    pub active_ask_levels: AtomicU64,
    pub onchain_sequence_number: AtomicU64,

    pub volatility_bps: AtomicF64,
    pub intel_spread_add_bps: AtomicF64,
    pub intel_size_multiplier: AtomicF64,
    pub intel_bid_size_multiplier: AtomicF64,
    pub intel_ask_size_multiplier: AtomicF64,

    pub consecutive_failures: AtomicU64,

    pub cycles_total: AtomicU64,
    pub updates_sent: AtomicU64,
    pub mid_only_updates: AtomicU64,
    pub book_updates: AtomicU64,
    pub clear_book_sends: AtomicU64,
    pub heartbeat_sends: AtomicU64,

    pub price_feed_stale_holding: AtomicBool,
    pub price_feed_stale_episodes: AtomicU64,
    pub tx_circuit_open: AtomicBool,
    pub tx_circuit_reason: AtomicU64,
    pub tx_budget_drops: AtomicU64,
    pub priority_fee_sampling_failures: AtomicU64,

    pub engine_alive: AtomicBool,
}

impl SharedState {
    pub fn new() -> Self {
        Self {
            mid_price: AtomicF64::new(0.0),
            price_timestamp_us: AtomicU64::new(0),
            feed_alive: AtomicBool::new(false),
            price_notify: Notify::new(),
            cached_mid_ticks: AtomicU64::new(0),
            base_total_lots: AtomicU64::new(0),
            quote_total_lots: AtomicU64::new(0),
            active_bid_levels: AtomicU64::new(0),
            active_ask_levels: AtomicU64::new(0),
            onchain_sequence_number: AtomicU64::new(0),
            volatility_bps: AtomicF64::new(0.0),
            intel_spread_add_bps: AtomicF64::new(0.0),
            intel_size_multiplier: AtomicF64::new(1.0),
            intel_bid_size_multiplier: AtomicF64::new(1.0),
            intel_ask_size_multiplier: AtomicF64::new(1.0),
            consecutive_failures: AtomicU64::new(0),
            cycles_total: AtomicU64::new(0),
            updates_sent: AtomicU64::new(0),
            mid_only_updates: AtomicU64::new(0),
            book_updates: AtomicU64::new(0),
            clear_book_sends: AtomicU64::new(0),
            heartbeat_sends: AtomicU64::new(0),
            price_feed_stale_holding: AtomicBool::new(false),
            price_feed_stale_episodes: AtomicU64::new(0),
            tx_circuit_open: AtomicBool::new(false),
            tx_circuit_reason: AtomicU64::new(0),
            tx_budget_drops: AtomicU64::new(0),
            priority_fee_sampling_failures: AtomicU64::new(0),
            engine_alive: AtomicBool::new(false),
        }
    }
}

pub fn now_us() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_micros() as u64
}
