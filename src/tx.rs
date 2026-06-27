use std::str::FromStr;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use serde::Deserialize;
use solana_client::nonblocking::rpc_client::RpcClient;
use solana_client::rpc_config::RpcSendTransactionConfig;
use solana_sdk::commitment_config::CommitmentConfig;
use solana_sdk::compute_budget::ComputeBudgetInstruction;
use solana_sdk::hash::Hash;
use solana_sdk::instruction::Instruction;
use solana_sdk::pubkey::Pubkey;
use solana_sdk::signature::{Keypair, Signature};
use solana_sdk::signer::Signer;
use solana_sdk::transaction::Transaction;
use tokio::sync::RwLock;

use crate::state::SharedState;

#[derive(Debug, Copy, Clone)]
pub enum TxPriority {
    Normal,
    Emergency,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum TxPurpose {
    Update,
    ClearBook,
    RecoveryUpdate,
    SafetyClearBook,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum TxCircuitReason {
    TxRateExceeded = 1,
    ClearBookRateExceeded = 2,
    ClearBookCooldown = 3,
    ConsecutiveFailures = 4,
    UpdateRateExceeded = 5,
    PriorityFeeSamplingFailures = 6,
    RecoveryUpdateRateExceeded = 7,
    SafetyClearBookRateExceeded = 8,
    SafetyClearBookCooldown = 9,
}

#[derive(Debug, Clone)]
pub struct TxBudgetConfig {
    pub max_tx_per_minute: u64,
    pub max_update_tx_per_10min: u64,
    pub max_recovery_update_tx_per_10min: u64,
    pub max_clear_book_per_5min: u64,
    pub max_safety_clear_book_per_5min: u64,
    pub min_clear_book_interval: Duration,
    pub min_safety_clear_book_interval: Duration,
}

const BLOCKHASH_TTL: Duration = Duration::from_secs(2);
const TX_WINDOW: Duration = Duration::from_secs(60);
const UPDATE_WINDOW: Duration = Duration::from_secs(600);
const CLEAR_BOOK_WINDOW: Duration = Duration::from_secs(300);
const PRIORITY_FEE_SAMPLE_FAILURE_CIRCUIT_THRESHOLD: u64 = 3;

#[derive(Debug, Copy, Clone, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PriorityFeeMode {
    Off,
    Fixed,
    Dynamic,
}

impl Default for PriorityFeeMode {
    fn default() -> Self {
        Self::Dynamic
    }
}

#[derive(Debug, Clone)]
pub struct PriorityFeeConfig {
    pub mode: PriorityFeeMode,
    pub fixed_fee_microlamports: u64,
    pub min_fee_microlamports: u64,
    pub max_fee_microlamports: u64,
    pub fallback_fee_microlamports: u64,
    pub percentile: u8,
    pub cache_ttl: Duration,
    pub emergency_multiplier: u64,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub struct PriorityFeeResolution {
    pub fee_microlamports: u64,
    pub sampled: bool,
    pub sampling_failed: bool,
}

struct CachedBlockhash {
    hash: Hash,
    fetched_at: Instant,
}

#[derive(Debug, Deserialize)]
struct SharedBlockhashResponse {
    blockhash: String,
}

#[derive(Debug, Clone)]
struct CachedPriorityFee {
    fee_microlamports: u64,
    accounts: Vec<Pubkey>,
    fetched_at: Instant,
}

#[derive(Default)]
pub struct PriorityFeeCache {
    inner: RwLock<Option<CachedPriorityFee>>,
}

pub struct TxSender {
    rpc: Arc<RpcClient>,
    signer: Arc<Keypair>,
    priority_fee: PriorityFeeConfig,
    shadow_mode: bool,
    state: Arc<SharedState>,
    blockhash_cache: Arc<RwLock<Option<CachedBlockhash>>>,
    shared_blockhash_url: Option<String>,
    http_client: reqwest::Client,
    priority_fee_cache: Arc<PriorityFeeCache>,
    budget_config: TxBudgetConfig,
    budget: Arc<Mutex<TxBudget>>,
}

struct TxBudget {
    tx_window_started: Instant,
    tx_count: u64,
    update_window_started: Instant,
    update_count: u64,
    recovery_update_count: u64,
    clear_window_started: Instant,
    clear_count: u64,
    safety_clear_count: u64,
    last_clear_sent: Option<Instant>,
    last_safety_clear_sent: Option<Instant>,
}

impl Default for TxBudget {
    fn default() -> Self {
        let now = Instant::now();
        Self {
            tx_window_started: now,
            tx_count: 0,
            update_window_started: now,
            update_count: 0,
            recovery_update_count: 0,
            clear_window_started: now,
            clear_count: 0,
            safety_clear_count: 0,
            last_clear_sent: None,
            last_safety_clear_sent: None,
        }
    }
}

impl TxBudget {
    fn reserve(
        &mut self,
        now: Instant,
        purpose: TxPurpose,
        config: &TxBudgetConfig,
    ) -> std::result::Result<(), TxCircuitReason> {
        if now.duration_since(self.tx_window_started) >= TX_WINDOW {
            self.tx_window_started = now;
            self.tx_count = 0;
        }
        if now.duration_since(self.update_window_started) >= UPDATE_WINDOW {
            self.update_window_started = now;
            self.update_count = 0;
            self.recovery_update_count = 0;
        }
        if now.duration_since(self.clear_window_started) >= CLEAR_BOOK_WINDOW {
            self.clear_window_started = now;
            self.clear_count = 0;
            self.safety_clear_count = 0;
            self.last_clear_sent = None;
            self.last_safety_clear_sent = None;
        }

        if self.tx_count >= config.max_tx_per_minute {
            return Err(TxCircuitReason::TxRateExceeded);
        }

        if purpose == TxPurpose::Update {
            if self.update_count >= config.max_update_tx_per_10min {
                return Err(TxCircuitReason::UpdateRateExceeded);
            }
            self.update_count += 1;
        }

        if purpose == TxPurpose::RecoveryUpdate {
            if self.recovery_update_count >= config.max_recovery_update_tx_per_10min {
                return Err(TxCircuitReason::RecoveryUpdateRateExceeded);
            }
            self.recovery_update_count += 1;
        }

        if purpose == TxPurpose::ClearBook {
            if let Some(last_clear) = self.last_clear_sent {
                if now.duration_since(last_clear) < config.min_clear_book_interval {
                    return Err(TxCircuitReason::ClearBookCooldown);
                }
            }
            if self.clear_count >= config.max_clear_book_per_5min {
                return Err(TxCircuitReason::ClearBookRateExceeded);
            }
            self.clear_count += 1;
            self.last_clear_sent = Some(now);
        }

        if purpose == TxPurpose::SafetyClearBook {
            if let Some(last_clear) = self.last_safety_clear_sent {
                if now.duration_since(last_clear) < config.min_safety_clear_book_interval {
                    return Err(TxCircuitReason::SafetyClearBookCooldown);
                }
            }
            if self.safety_clear_count >= config.max_safety_clear_book_per_5min {
                return Err(TxCircuitReason::SafetyClearBookRateExceeded);
            }
            self.safety_clear_count += 1;
            self.last_safety_clear_sent = Some(now);
        }

        self.tx_count += 1;
        Ok(())
    }
}

impl TxSender {
    pub fn new(
        rpc: Arc<RpcClient>,
        signer: Arc<Keypair>,
        priority_fee: PriorityFeeConfig,
        budget_config: TxBudgetConfig,
        shadow_mode: bool,
        shared_blockhash_url: Option<String>,
        state: Arc<SharedState>,
    ) -> Self {
        Self {
            rpc,
            signer,
            priority_fee,
            shadow_mode,
            state,
            blockhash_cache: Arc::new(RwLock::new(None)),
            shared_blockhash_url: shared_blockhash_url
                .and_then(|url| (!url.trim().is_empty()).then(|| url.trim().to_string())),
            http_client: reqwest::Client::new(),
            priority_fee_cache: Arc::new(PriorityFeeCache::default()),
            budget_config,
            budget: Arc::new(Mutex::new(TxBudget::default())),
        }
    }

    pub fn fire(
        &self,
        instructions: Vec<Instruction>,
        priority: TxPriority,
        purpose: TxPurpose,
        cu_limit: u32,
    ) {
        if self.shadow_mode {
            tracing::debug!(
                ix_count = instructions.len(),
                cu = cu_limit,
                "SHADOW: would send TX"
            );
            return;
        }

        if self.state.tx_circuit_open.load(Relaxed) {
            self.state.tx_budget_drops.fetch_add(1, Relaxed);
            tracing::warn!(
                reason = self.state.tx_circuit_reason.load(Relaxed),
                "TX circuit breaker open; dropping transaction"
            );
            return;
        }

        let reserve_result = self
            .budget
            .lock()
            .expect("tx budget mutex poisoned")
            .reserve(Instant::now(), purpose, &self.budget_config);
        if let Err(reason) = reserve_result {
            if tx_budget_rejection_opens_circuit(reason) {
                self.state.tx_circuit_open.store(true, Relaxed);
                self.state.tx_circuit_reason.store(reason as u64, Relaxed);
            }
            self.state.tx_budget_drops.fetch_add(1, Relaxed);
            tracing::warn!(
                ?reason,
                ?purpose,
                max_tx_per_minute = self.budget_config.max_tx_per_minute,
                max_update_tx_per_10min = self.budget_config.max_update_tx_per_10min,
                max_recovery_update_tx_per_10min =
                    self.budget_config.max_recovery_update_tx_per_10min,
                max_clear_book_per_5min = self.budget_config.max_clear_book_per_5min,
                max_safety_clear_book_per_5min = self.budget_config.max_safety_clear_book_per_5min,
                min_clear_book_interval_ms = self.budget_config.min_clear_book_interval.as_millis(),
                min_safety_clear_book_interval_ms = self
                    .budget_config
                    .min_safety_clear_book_interval
                    .as_millis(),
                "TX budget throttle; dropping transaction"
            );
            return;
        }

        let rpc = self.rpc.clone();
        let signer = self.signer.clone();
        let priority_fee = self.priority_fee.clone();
        let state = self.state.clone();
        let blockhash_cache = self.blockhash_cache.clone();
        let shared_blockhash_url = self.shared_blockhash_url.clone();
        let http_client = self.http_client.clone();
        let priority_fee_cache = self.priority_fee_cache.clone();

        tokio::spawn(async move {
            match build_and_send(
                &rpc,
                &signer,
                instructions,
                priority,
                cu_limit,
                &priority_fee,
                &blockhash_cache,
                shared_blockhash_url.as_deref(),
                &http_client,
                Some(&priority_fee_cache),
                &state,
            )
            .await
            {
                Ok(sig) => {
                    tracing::debug!(%sig, "TX sent");
                    state.consecutive_failures.store(0, Relaxed);
                }
                Err(e) => {
                    tracing::warn!("TX send failed: {e:#}");
                    state.consecutive_failures.fetch_add(1, Relaxed);
                }
            }
        });
    }
}

fn tx_budget_rejection_opens_circuit(reason: TxCircuitReason) -> bool {
    !matches!(
        reason,
        TxCircuitReason::TxRateExceeded
            | TxCircuitReason::ClearBookRateExceeded
            | TxCircuitReason::ClearBookCooldown
            | TxCircuitReason::UpdateRateExceeded
            | TxCircuitReason::RecoveryUpdateRateExceeded
            | TxCircuitReason::SafetyClearBookRateExceeded
            | TxCircuitReason::SafetyClearBookCooldown
    )
}

async fn get_or_refresh_blockhash(
    rpc: &RpcClient,
    cache: &RwLock<Option<CachedBlockhash>>,
    shared_blockhash_url: Option<&str>,
    http_client: &reqwest::Client,
) -> Result<Hash> {
    {
        let guard = cache.read().await;
        if let Some(ref cached) = *guard {
            if cached.fetched_at.elapsed() < BLOCKHASH_TTL {
                return Ok(cached.hash);
            }
        }
    }
    let mut guard = cache.write().await;
    if let Some(ref cached) = *guard {
        if cached.fetched_at.elapsed() < BLOCKHASH_TTL {
            return Ok(cached.hash);
        }
    }
    if let Some(url) = shared_blockhash_url {
        match fetch_shared_blockhash(http_client, url).await {
            Ok(hash) => {
                *guard = Some(CachedBlockhash {
                    hash,
                    fetched_at: Instant::now(),
                });
                return Ok(hash);
            }
            Err(e) => {
                tracing::warn!(%url, "shared blockhash fetch failed, falling back to RPC: {e:#}");
            }
        }
    }
    let hash = rpc
        .get_latest_blockhash_with_commitment(CommitmentConfig::processed())
        .await?
        .0;
    *guard = Some(CachedBlockhash {
        hash,
        fetched_at: Instant::now(),
    });
    Ok(hash)
}

async fn fetch_shared_blockhash(client: &reqwest::Client, url: &str) -> Result<Hash> {
    let response = client
        .get(url)
        .send()
        .await
        .with_context(|| format!("requesting shared blockhash from {url}"))?
        .error_for_status()
        .with_context(|| format!("shared blockhash endpoint returned an error: {url}"))?
        .json::<SharedBlockhashResponse>()
        .await
        .context("decoding shared blockhash response")?;
    Hash::from_str(&response.blockhash).context("shared blockhash response had invalid hash")
}

pub async fn resolve_priority_fee(
    rpc: &RpcClient,
    instructions: &[Instruction],
    priority: TxPriority,
    config: &PriorityFeeConfig,
    cache: Option<&PriorityFeeCache>,
) -> PriorityFeeResolution {
    let (base_fee, sampled, sampling_failed) = match config.mode {
        PriorityFeeMode::Off => {
            return PriorityFeeResolution {
                fee_microlamports: 0,
                sampled: false,
                sampling_failed: false,
            };
        }
        PriorityFeeMode::Fixed => (config.fixed_fee_microlamports, false, false),
        PriorityFeeMode::Dynamic => {
            sample_dynamic_priority_fee(rpc, instructions, config, cache).await
        }
    };
    PriorityFeeResolution {
        fee_microlamports: scale_priority_fee(base_fee, priority, config),
        sampled,
        sampling_failed,
    }
}

async fn sample_dynamic_priority_fee(
    rpc: &RpcClient,
    instructions: &[Instruction],
    config: &PriorityFeeConfig,
    cache: Option<&PriorityFeeCache>,
) -> (u64, bool, bool) {
    let writable_accounts = writable_accounts(instructions);
    if let Some(cache) = cache {
        if let Some(cached) = cache.inner.read().await.as_ref() {
            if cached.accounts == writable_accounts
                && cached.fetched_at.elapsed() < config.cache_ttl
            {
                return (cached.fee_microlamports, false, false);
            }
        }
    }

    let (fee, sampling_failed) = match rpc.get_recent_prioritization_fees(&writable_accounts).await
    {
        Ok(samples) => (
            percentile_fee(
                samples
                    .iter()
                    .map(|sample| sample.prioritization_fee)
                    .collect(),
                config,
            ),
            false,
        ),
        Err(e) => {
            tracing::warn!("priority fee sampling failed, using fallback: {e}");
            (
                clamp_dynamic_fee(config.fallback_fee_microlamports, config),
                true,
            )
        }
    };

    if let Some(cache) = cache {
        *cache.inner.write().await = Some(CachedPriorityFee {
            fee_microlamports: fee,
            accounts: writable_accounts,
            fetched_at: Instant::now(),
        });
    }

    (fee, true, sampling_failed)
}

fn apply_priority_fee_resolution(
    state: &SharedState,
    resolution: PriorityFeeResolution,
) -> std::result::Result<(), TxCircuitReason> {
    if resolution.sampling_failed {
        let failures = state.priority_fee_sampling_failures.fetch_add(1, Relaxed) + 1;
        if failures >= PRIORITY_FEE_SAMPLE_FAILURE_CIRCUIT_THRESHOLD {
            state.tx_circuit_open.store(true, Relaxed);
            state
                .tx_circuit_reason
                .store(TxCircuitReason::PriorityFeeSamplingFailures as u64, Relaxed);
            state.tx_budget_drops.fetch_add(1, Relaxed);
            tracing::error!(
                failures,
                threshold = PRIORITY_FEE_SAMPLE_FAILURE_CIRCUIT_THRESHOLD,
                "TX circuit breaker opened after repeated priority fee sampling failures"
            );
            return Err(TxCircuitReason::PriorityFeeSamplingFailures);
        }
    } else if resolution.sampled {
        state.priority_fee_sampling_failures.store(0, Relaxed);
    }
    Ok(())
}

fn writable_accounts(instructions: &[Instruction]) -> Vec<Pubkey> {
    let mut accounts = Vec::new();
    for ix in instructions {
        for meta in &ix.accounts {
            if meta.is_writable && !accounts.contains(&meta.pubkey) {
                accounts.push(meta.pubkey);
            }
        }
    }
    accounts
}

fn percentile_fee(mut values: Vec<u64>, config: &PriorityFeeConfig) -> u64 {
    if values.is_empty() {
        return clamp_dynamic_fee(config.fallback_fee_microlamports, config);
    }
    values.sort_unstable();
    let percentile = config.percentile.min(100) as usize;
    let index = if values.len() == 1 {
        0
    } else {
        (percentile * (values.len() - 1) + 50) / 100
    };
    clamp_dynamic_fee(values[index], config)
}

fn scale_priority_fee(fee: u64, priority: TxPriority, config: &PriorityFeeConfig) -> u64 {
    match priority {
        TxPriority::Normal => fee,
        TxPriority::Emergency => clamp_dynamic_fee(
            fee.saturating_mul(config.emergency_multiplier.max(1)),
            config,
        ),
    }
}

fn clamp_dynamic_fee(fee: u64, config: &PriorityFeeConfig) -> u64 {
    let max = config
        .max_fee_microlamports
        .max(config.min_fee_microlamports);
    fee.clamp(config.min_fee_microlamports, max)
}

async fn build_and_send(
    rpc: &RpcClient,
    signer: &Keypair,
    mut instructions: Vec<Instruction>,
    priority: TxPriority,
    cu_limit: u32,
    priority_fee_config: &PriorityFeeConfig,
    blockhash_cache: &RwLock<Option<CachedBlockhash>>,
    shared_blockhash_url: Option<&str>,
    http_client: &reqwest::Client,
    priority_fee_cache: Option<&PriorityFeeCache>,
    state: &SharedState,
) -> Result<Signature> {
    let priority_fee_resolution = resolve_priority_fee(
        rpc,
        &instructions,
        priority,
        priority_fee_config,
        priority_fee_cache,
    )
    .await;
    apply_priority_fee_resolution(state, priority_fee_resolution)
        .map_err(|reason| anyhow!("transaction circuit opened before send: {reason:?}"))?;
    let priority_fee = priority_fee_resolution.fee_microlamports;

    let mut all_ixs = Vec::with_capacity(instructions.len() + 2);
    all_ixs.push(ComputeBudgetInstruction::set_compute_unit_limit(cu_limit));
    if priority_fee > 0 {
        all_ixs.push(ComputeBudgetInstruction::set_compute_unit_price(
            priority_fee,
        ));
    }
    all_ixs.append(&mut instructions);

    let blockhash =
        get_or_refresh_blockhash(rpc, blockhash_cache, shared_blockhash_url, http_client).await?;
    let tx =
        Transaction::new_signed_with_payer(&all_ixs, Some(&signer.pubkey()), &[signer], blockhash);

    tracing::debug!(
        priority_fee_microlamports = priority_fee,
        cu_limit,
        "sending transaction"
    );

    let sig = rpc
        .send_transaction_with_config(
            &tx,
            RpcSendTransactionConfig {
                skip_preflight: true,
                max_retries: Some(0),
                ..Default::default()
            },
        )
        .await?;

    Ok(sig)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn budget_config() -> TxBudgetConfig {
        TxBudgetConfig {
            max_tx_per_minute: 3,
            max_update_tx_per_10min: 2,
            max_recovery_update_tx_per_10min: 2,
            max_clear_book_per_5min: 2,
            max_safety_clear_book_per_5min: 2,
            min_clear_book_interval: Duration::from_secs(30),
            min_safety_clear_book_interval: Duration::from_secs(5),
        }
    }

    #[test]
    fn budget_rejects_clear_book_cooldown() {
        let mut budget = TxBudget::default();
        let config = budget_config();
        let now = Instant::now();

        assert!(budget.reserve(now, TxPurpose::ClearBook, &config).is_ok());
        assert_eq!(
            budget.reserve(now + Duration::from_secs(1), TxPurpose::ClearBook, &config),
            Err(TxCircuitReason::ClearBookCooldown)
        );
    }

    #[test]
    fn budget_rejects_clear_book_window_limit() {
        let mut budget = TxBudget::default();
        let config = budget_config();
        let now = Instant::now();

        assert!(budget.reserve(now, TxPurpose::ClearBook, &config).is_ok());
        assert!(
            budget
                .reserve(now + Duration::from_secs(31), TxPurpose::ClearBook, &config)
                .is_ok()
        );
        assert_eq!(
            budget.reserve(now + Duration::from_secs(62), TxPurpose::ClearBook, &config),
            Err(TxCircuitReason::ClearBookRateExceeded)
        );
    }

    #[test]
    fn budget_rejects_tx_rate_limit() {
        let mut budget = TxBudget::default();
        let config = budget_config();
        let now = Instant::now();

        assert!(budget.reserve(now, TxPurpose::Update, &config).is_ok());
        assert!(
            budget
                .reserve(now + Duration::from_secs(1), TxPurpose::Update, &config)
                .is_ok()
        );
        let config = TxBudgetConfig {
            max_update_tx_per_10min: 10,
            ..config
        };
        assert!(
            budget
                .reserve(now + Duration::from_secs(2), TxPurpose::Update, &config)
                .is_ok()
        );
        assert_eq!(
            budget.reserve(now + Duration::from_secs(3), TxPurpose::Update, &config),
            Err(TxCircuitReason::TxRateExceeded)
        );
    }

    #[test]
    fn budget_rejects_slow_update_churn() {
        let mut budget = TxBudget::default();
        let config = budget_config();
        let now = Instant::now();

        assert!(budget.reserve(now, TxPurpose::Update, &config).is_ok());
        assert!(
            budget
                .reserve(now + Duration::from_secs(120), TxPurpose::Update, &config)
                .is_ok()
        );
        assert_eq!(
            budget.reserve(now + Duration::from_secs(240), TxPurpose::Update, &config),
            Err(TxCircuitReason::UpdateRateExceeded)
        );
    }

    #[test]
    fn recovery_update_budget_does_not_consume_normal_update_budget() {
        let mut budget = TxBudget::default();
        let config = TxBudgetConfig {
            max_update_tx_per_10min: 1,
            max_recovery_update_tx_per_10min: 2,
            max_tx_per_minute: 5,
            ..budget_config()
        };
        let now = Instant::now();

        assert!(budget.reserve(now, TxPurpose::Update, &config).is_ok());
        assert_eq!(
            budget.reserve(now + Duration::from_secs(1), TxPurpose::Update, &config),
            Err(TxCircuitReason::UpdateRateExceeded)
        );
        assert!(
            budget
                .reserve(
                    now + Duration::from_secs(2),
                    TxPurpose::RecoveryUpdate,
                    &config
                )
                .is_ok()
        );
    }

    #[test]
    fn normal_update_budget_does_not_block_recovery_update() {
        let mut budget = TxBudget::default();
        let config = TxBudgetConfig {
            max_update_tx_per_10min: 1,
            max_recovery_update_tx_per_10min: 1,
            max_tx_per_minute: 5,
            ..budget_config()
        };
        let now = Instant::now();

        assert!(budget.reserve(now, TxPurpose::Update, &config).is_ok());
        assert!(
            budget
                .reserve(
                    now + Duration::from_secs(1),
                    TxPurpose::RecoveryUpdate,
                    &config
                )
                .is_ok()
        );
    }

    #[test]
    fn safety_clear_book_budget_does_not_consume_normal_clear_budget() {
        let mut budget = TxBudget::default();
        let config = TxBudgetConfig {
            max_tx_per_minute: 5,
            max_clear_book_per_5min: 1,
            max_safety_clear_book_per_5min: 2,
            min_safety_clear_book_interval: Duration::from_secs(1),
            ..budget_config()
        };
        let now = Instant::now();

        assert!(budget.reserve(now, TxPurpose::ClearBook, &config).is_ok());
        assert_eq!(
            budget.reserve(now + Duration::from_secs(31), TxPurpose::ClearBook, &config),
            Err(TxCircuitReason::ClearBookRateExceeded)
        );
        assert!(
            budget
                .reserve(
                    now + Duration::from_secs(32),
                    TxPurpose::SafetyClearBook,
                    &config
                )
                .is_ok()
        );
    }

    #[test]
    fn budget_rejections_are_soft_throttles_not_hard_circuits() {
        assert!(!tx_budget_rejection_opens_circuit(
            TxCircuitReason::TxRateExceeded
        ));
        assert!(!tx_budget_rejection_opens_circuit(
            TxCircuitReason::ClearBookRateExceeded
        ));
        assert!(!tx_budget_rejection_opens_circuit(
            TxCircuitReason::ClearBookCooldown
        ));
        assert!(!tx_budget_rejection_opens_circuit(
            TxCircuitReason::UpdateRateExceeded
        ));
        assert!(!tx_budget_rejection_opens_circuit(
            TxCircuitReason::RecoveryUpdateRateExceeded
        ));
        assert!(!tx_budget_rejection_opens_circuit(
            TxCircuitReason::SafetyClearBookRateExceeded
        ));
        assert!(!tx_budget_rejection_opens_circuit(
            TxCircuitReason::SafetyClearBookCooldown
        ));
    }

    #[test]
    fn true_safety_reasons_still_open_hard_circuit() {
        assert!(tx_budget_rejection_opens_circuit(
            TxCircuitReason::ConsecutiveFailures
        ));
        assert!(tx_budget_rejection_opens_circuit(
            TxCircuitReason::PriorityFeeSamplingFailures
        ));
    }

    #[test]
    fn repeated_priority_fee_sampling_failures_open_circuit() {
        let state = SharedState::new();
        let failed = PriorityFeeResolution {
            fee_microlamports: 0,
            sampled: true,
            sampling_failed: true,
        };

        assert!(apply_priority_fee_resolution(&state, failed).is_ok());
        assert!(!state.tx_circuit_open.load(Relaxed));
        assert!(apply_priority_fee_resolution(&state, failed).is_ok());
        assert!(!state.tx_circuit_open.load(Relaxed));
        assert_eq!(
            apply_priority_fee_resolution(&state, failed),
            Err(TxCircuitReason::PriorityFeeSamplingFailures)
        );
        assert!(state.tx_circuit_open.load(Relaxed));
        assert_eq!(
            state.tx_circuit_reason.load(Relaxed),
            TxCircuitReason::PriorityFeeSamplingFailures as u64
        );
    }

    #[test]
    fn successful_priority_fee_sample_resets_failure_count() {
        let state = SharedState::new();
        let failed = PriorityFeeResolution {
            fee_microlamports: 0,
            sampled: true,
            sampling_failed: true,
        };
        let succeeded = PriorityFeeResolution {
            fee_microlamports: 0,
            sampled: true,
            sampling_failed: false,
        };

        assert!(apply_priority_fee_resolution(&state, failed).is_ok());
        assert_eq!(state.priority_fee_sampling_failures.load(Relaxed), 1);
        assert!(apply_priority_fee_resolution(&state, succeeded).is_ok());
        assert_eq!(state.priority_fee_sampling_failures.load(Relaxed), 0);
    }
}
