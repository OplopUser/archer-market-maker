mod archer;
mod config;
mod engine;
mod feed;
mod quote_policy;
mod simulator;
mod state;
mod strategy;
mod tx;
mod volatility;

use std::sync::Arc;

use crate::archer::accounts::{
    active_ask_levels, active_bid_levels, maker_balances, parse_market_state,
};
use crate::archer::client::{ArcherClient, SendOptions};
use crate::archer::ix_builder::{
    build_clear_book_ix, build_deposit_ix, build_initialize_maker_book_ix,
    build_update_expiry_in_slots_ix, build_withdraw_ix,
};
use crate::archer::math::{base_amount_to_lots, base_lots_to_amount, quote_amount_to_lots};
use crate::archer::types::{MakerBook, PROGRAM_ID};
use anyhow::{Context, Result};
use clap::Parser;
use solana_client::nonblocking::rpc_client::RpcClient;
use solana_sdk::pubkey::Pubkey;
use solana_sdk::signature::{Keypair, read_keypair_file};
use solana_sdk::signer::Signer;
use tokio_util::sync::CancellationToken;

use crate::config::{Cli, load_config, resolve_path};
use crate::simulator::{SimulationFixture, simulate_fixture};
use crate::state::SharedState;
use crate::strategy::{IntelAdjustments, QuoteDecision, Strategy};
use crate::tx::{TxPriority, TxSender};

const LIVE_TRADING_ENV: &str = "ARCHER_ENABLE_LIVE_TRADING";
const CU_INIT_MAKER_BOOK: u32 = 25_000;
const CU_SET_EXPIRY: u32 = 5_000;
const CU_DEPOSIT: u32 = 30_000;
const CU_WITHDRAW: u32 = 100_000;
const CU_CLEAR_BOOK: u32 = 5_000;

async fn detect_token_program(rpc: &RpcClient, mint: &Pubkey) -> Result<Pubkey> {
    let account = rpc
        .get_account(mint)
        .await
        .with_context(|| format!("Failed to fetch mint account {mint}"))?;
    if account.owner == spl_token::id() {
        Ok(spl_token::id())
    } else if account.owner == spl_token_2022::id() {
        Ok(spl_token_2022::id())
    } else {
        anyhow::bail!("Mint {mint} owned by unknown program {}", account.owner)
    }
}

struct TokenPrograms {
    base: Pubkey,
    quote: Pubkey,
}

async fn resolve_token_programs(
    rpc: &RpcClient,
    base_mint: &Pubkey,
    quote_mint: &Pubkey,
) -> Result<TokenPrograms> {
    Ok(TokenPrograms {
        base: detect_token_program(rpc, base_mint).await?,
        quote: detect_token_program(rpc, quote_mint).await?,
    })
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli {
        Cli::Run {
            config,
            shadow,
            live,
        } => cmd_run(&config, shadow, live).await,
        Cli::Market { config } => cmd_market(&config).await,
        Cli::Preview {
            config,
            mid,
            base,
            quote,
            vol_bps,
            intel_spread_add_bps,
            intel_size_multiplier,
            intel_bid_size_multiplier,
            intel_ask_size_multiplier,
        } => {
            cmd_preview(
                &config,
                mid,
                base,
                quote,
                vol_bps,
                IntelAdjustments {
                    spread_add_bps: intel_spread_add_bps,
                    size_multiplier: intel_size_multiplier,
                    bid_size_multiplier: intel_bid_size_multiplier,
                    ask_size_multiplier: intel_ask_size_multiplier,
                },
            )
            .await
        }
        Cli::SimulatePolicy { fixture } => cmd_simulate_policy(&fixture).await,
        Cli::Init { config } => cmd_init(&config).await,
        Cli::Deposit {
            config,
            base,
            quote,
        } => cmd_deposit(&config, base, quote).await,
        Cli::Withdraw { config } => cmd_withdraw(&config).await,
        Cli::Kill { config } => cmd_kill(&config).await,
        Cli::Status { config } => cmd_status(&config).await,
        Cli::SetExpiry { config, slots } => cmd_set_expiry(&config, slots).await,
    }
}

async fn cmd_simulate_policy(fixture_path: &std::path::Path) -> Result<()> {
    let contents = std::fs::read_to_string(fixture_path)
        .with_context(|| format!("reading {}", fixture_path.display()))?;
    let fixture: SimulationFixture = serde_json::from_str(&contents)
        .with_context(|| format!("parsing {}", fixture_path.display()))?;
    let output = simulate_fixture(fixture)?;
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}

async fn cmd_run(config_path: &std::path::Path, shadow: bool, live: bool) -> Result<()> {
    let mut mm_config = load_config(config_path)?;
    anyhow::ensure!(
        !(shadow && live),
        "--shadow and --live cannot be used together"
    );
    if shadow {
        mm_config.execution.shadow_mode = true;
    }
    if live {
        mm_config.execution.shadow_mode = false;
    }
    if !mm_config.execution.shadow_mode {
        require_live_tx_enabled("run live Archer maker")?;
    }
    let mm_config = Arc::new(mm_config);

    init_tracing(&mm_config.monitoring.log_level);
    tracing::info!("Archer Market Maker starting");
    if mm_config.execution.shadow_mode {
        tracing::warn!("SHADOW MODE — no transactions will be sent");
    }

    let maker_keypair = load_keypair(&mm_config.market.maker_keypair_path)?;
    let maker_pubkey = maker_keypair.pubkey();
    let signer = Arc::new(maker_keypair);
    let market_pubkey: Pubkey = mm_config
        .market
        .market_pubkey
        .parse()
        .context("Invalid market_pubkey")?;

    tracing::info!(%market_pubkey, %maker_pubkey);

    let rpc = Arc::new(
        solana_client::nonblocking::rpc_client::RpcClient::new_with_commitment(
            mm_config.connection.rpc_url.clone(),
            solana_sdk::commitment_config::CommitmentConfig::processed(),
        ),
    );

    let archer_client = ArcherClient::new(&mm_config.connection.rpc_url);
    let sdk_config = Arc::new(
        archer_client
            .get_market_config(&market_pubkey)
            .await
            .context("Failed to fetch MarketConfig")?,
    );
    tracing::info!(base_mint = %sdk_config.base_mint, quote_mint = %sdk_config.quote_mint, "MarketConfig loaded");

    let initial_book = archer_client
        .get_maker_book(&market_pubkey, &maker_pubkey)
        .await
        .context("Failed to fetch maker book — run `init` first.")?;

    let bal = maker_balances(&initial_book, &sdk_config);
    tracing::info!(
        base_free = bal.base_free,
        quote_free = bal.quote_free,
        "Initial balances"
    );

    let state = Arc::new(SharedState::new());
    state.cached_mid_ticks.store(
        initial_book.mid_price_ticks,
        std::sync::atomic::Ordering::Relaxed,
    );
    state.onchain_sequence_number.store(
        initial_book.last_updated_sequence_number,
        std::sync::atomic::Ordering::Relaxed,
    );
    state.base_total_lots.store(
        initial_book.base_free + initial_book.base_locked,
        std::sync::atomic::Ordering::Relaxed,
    );
    state.quote_total_lots.store(
        initial_book.quote_free + initial_book.quote_locked,
        std::sync::atomic::Ordering::Relaxed,
    );
    state.active_bid_levels.store(
        active_bid_levels(&initial_book) as u64,
        std::sync::atomic::Ordering::Relaxed,
    );
    state.active_ask_levels.store(
        active_ask_levels(&initial_book) as u64,
        std::sync::atomic::Ordering::Relaxed,
    );

    let tx_sender = Arc::new(TxSender::new(
        rpc.clone(),
        signer.clone(),
        mm_config.execution.priority_fee_config(),
        mm_config.execution.tx_budget_config(),
        mm_config.execution.shadow_mode,
        mm_config.connection.shared_blockhash_url.clone(),
        state.clone(),
    ));

    let cancel = CancellationToken::new();

    tokio::spawn(feed::run_feed(
        state.clone(),
        mm_config.feed.clone(),
        mm_config.strategy.vol_window,
        cancel.clone(),
    ));
    tokio::spawn(run_book_watcher(
        state.clone(),
        mm_config.connection.rpc_url.clone(),
        market_pubkey,
        maker_pubkey,
        mm_config.execution.maker_book_poll_interval_ms,
        cancel.clone(),
    ));

    tracing::info!("Waiting for price feed...");
    let mut waited = 0u64;
    while state.mid_price.load(std::sync::atomic::Ordering::Relaxed) <= 0.0 {
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        waited += 100;
        if waited > 60_000 {
            anyhow::bail!("Price feed did not connect within 60 seconds");
        }
    }
    tracing::info!(
        price = state.mid_price.load(std::sync::atomic::Ordering::Relaxed),
        "Price feed connected"
    );

    let engine_handle = tokio::spawn(engine::run_engine(
        state.clone(),
        sdk_config.clone(),
        mm_config.clone(),
        signer.clone(),
        maker_pubkey,
        market_pubkey,
        tx_sender.clone(),
        initial_book.last_updated_sequence_number,
        cancel.clone(),
    ));

    tracing::info!("Engine running. Press Ctrl+C to stop.");
    tokio::signal::ctrl_c().await?;
    tracing::info!("Shutting down");
    cancel.cancel();
    let _ = tokio::time::timeout(std::time::Duration::from_secs(5), engine_handle).await;
    tracing::info!("Stopped");
    Ok(())
}

async fn run_book_watcher(
    state: Arc<SharedState>,
    rpc_url: String,
    market_pubkey: Pubkey,
    maker_pubkey: Pubkey,
    poll_interval_ms: u64,
    cancel: CancellationToken,
) {
    let client = ArcherClient::new(&rpc_url);
    let poll_interval = std::time::Duration::from_millis(poll_interval_ms.max(1_000));
    loop {
        tokio::select! {
            _ = cancel.cancelled() => return,
            _ = tokio::time::sleep(poll_interval) => {}
        }

        match client.get_maker_book(&market_pubkey, &maker_pubkey).await {
            Ok(book) => {
                state
                    .cached_mid_ticks
                    .store(book.mid_price_ticks, std::sync::atomic::Ordering::Relaxed);
                state.onchain_sequence_number.store(
                    book.last_updated_sequence_number,
                    std::sync::atomic::Ordering::Relaxed,
                );
                state.base_total_lots.store(
                    book.base_free + book.base_locked,
                    std::sync::atomic::Ordering::Relaxed,
                );
                state.quote_total_lots.store(
                    book.quote_free + book.quote_locked,
                    std::sync::atomic::Ordering::Relaxed,
                );
                state.active_bid_levels.store(
                    active_bid_levels(&book) as u64,
                    std::sync::atomic::Ordering::Relaxed,
                );
                state.active_ask_levels.store(
                    active_ask_levels(&book) as u64,
                    std::sync::atomic::Ordering::Relaxed,
                );
            }
            Err(e) => {
                tracing::warn!("MakerBook watcher failed: {e:#}");
            }
        }
    }
}

async fn cmd_market(config_path: &std::path::Path) -> Result<()> {
    let mm_config = load_config(config_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let sdk_config = client.get_market_config(&market).await?;
    let rpc = RpcClient::new(mm_config.connection.rpc_url.clone());
    let market_account = rpc.get_account(&market).await?;
    let header = parse_market_state(&market_account.data)?;
    let mode = market_mode_label(header.mode);
    let owner_ok = market_account.owner == PROGRAM_ID;

    println!("=== Archer Market ===");
    println!("Market:                 {market}");
    println!("Owner:                  {}", market_account.owner);
    println!("Owner matches Archer:   {owner_ok}");
    println!("Mode:                   {mode}");
    println!("Base mint:              {}", sdk_config.base_mint);
    println!("Quote mint:             {}", sdk_config.quote_mint);
    println!("Base decimals:          {}", sdk_config.base_decimals);
    println!("Quote decimals:         {}", sdk_config.quote_decimals);
    println!(
        "Base atoms/base lot:    {}",
        sdk_config.base_atoms_per_base_lot
    );
    println!(
        "Quote atoms/quote lot:  {}",
        sdk_config.quote_atoms_per_quote_lot
    );
    println!("Maker fee ppm:          {}", sdk_config.maker_fee_ppm);
    println!("Taker fee ppm:          {}", sdk_config.taker_fee_ppm);
    println!(
        "Tick price increment:   {:.10}",
        sdk_config.ticks_to_price_factor()
    );
    Ok(())
}

async fn cmd_preview(
    config_path: &std::path::Path,
    mid: f64,
    base: f64,
    quote: f64,
    vol_bps: f64,
    intel: IntelAdjustments,
) -> Result<()> {
    anyhow::ensure!(mid.is_finite() && mid > 0.0, "--mid must be positive");
    anyhow::ensure!(base.is_finite() && base >= 0.0, "--base must be >= 0");
    anyhow::ensure!(quote.is_finite() && quote >= 0.0, "--quote must be >= 0");
    anyhow::ensure!(
        vol_bps.is_finite() && vol_bps >= 0.0,
        "--vol-bps must be >= 0"
    );

    let mm_config = load_config(config_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let sdk_config = client.get_market_config(&market).await?;

    let base_lots = base_amount_to_lots(base, &sdk_config)?;
    let quote_lots = quote_amount_to_lots(quote, &sdk_config)?;
    let strategy = Strategy::new(&mm_config.strategy, &mm_config.risk);
    let (decision, tightest_spread_bps) = strategy.compute(
        mid,
        0,
        0,
        &sdk_config,
        base_lots,
        quote_lots,
        vol_bps,
        intel,
    );

    println!("=== Archer Quote Preview ===");
    println!("Market:               {market}");
    println!("Input mid:            {:.8}", mid);
    println!("Input base/quote:     {:.6} / {:.4}", base, quote);
    println!("Vol bps:              {:.4}", vol_bps);
    println!("Intel spread add:     {:.4} bps", intel.spread_add_bps);
    println!(
        "Intel size mult:      {:.4} global / {:.4} bid / {:.4} ask",
        intel.size_multiplier, intel.bid_size_multiplier, intel.ask_size_multiplier
    );
    println!("Tightest spread bps:  {:.4}", tightest_spread_bps);
    println!("Live transactions:    disabled (preview only)");

    match decision {
        QuoteDecision::ClearBook => {
            println!("Decision:             clear/no quotes");
        }
        QuoteDecision::UpdateMidOnly { new_mid_ticks } => {
            println!("Decision:             mid-only");
            println!("New mid ticks:        {new_mid_ticks}");
        }
        QuoteDecision::UpdateFull { book_update, .. } => {
            println!("Decision:             full book update");
            println!("New mid ticks:        {}", book_update.new_mid_price_ticks);
            println!("Bids:");
            for (idx, level) in book_update.bid_levels.iter().enumerate() {
                let price = ticks_to_price(
                    book_update.new_mid_price_ticks,
                    level.price_offset_ticks,
                    &sdk_config,
                );
                let size = base_lots_to_amount(level.size_in_base_lots, &sdk_config);
                println!(
                    "  {:>2}. price={:.8} size={:.6} notional={:.4}",
                    idx + 1,
                    price,
                    size,
                    price * size
                );
            }
            println!("Asks:");
            for (idx, level) in book_update.ask_levels.iter().enumerate() {
                let price = ticks_to_price(
                    book_update.new_mid_price_ticks,
                    level.price_offset_ticks,
                    &sdk_config,
                );
                let size = base_lots_to_amount(level.size_in_base_lots, &sdk_config);
                println!(
                    "  {:>2}. price={:.8} size={:.6} notional={:.4}",
                    idx + 1,
                    price,
                    size,
                    price * size
                );
            }
        }
    }
    Ok(())
}

fn send_options(
    mm_config: &crate::config::MMConfig,
    priority: TxPriority,
    compute_unit_limit: u32,
) -> SendOptions {
    SendOptions::default()
        .with_dynamic_priority_fee(mm_config.execution.priority_fee_config(), priority)
        .with_compute_unit_limit(compute_unit_limit)
}

async fn cmd_init(config_path: &std::path::Path) -> Result<()> {
    let mm_config = load_config(config_path)?;
    init_tracing(&mm_config.monitoring.log_level);
    require_live_tx_enabled("initialize Archer maker book")?;
    let keypair = load_keypair(&mm_config.market.maker_keypair_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let ix = build_initialize_maker_book_ix(&keypair.pubkey(), &market);
    let sig = client
        .send_instructions(
            &[ix],
            &[&keypair],
            send_options(&mm_config, TxPriority::Normal, CU_INIT_MAKER_BOOK),
        )
        .await?;
    println!("Maker book initialized: {sig}");
    Ok(())
}

async fn cmd_set_expiry(config_path: &std::path::Path, slots: u64) -> Result<()> {
    let mm_config = load_config(config_path)?;
    init_tracing(&mm_config.monitoring.log_level);
    require_live_tx_enabled("set Archer maker book expiry")?;
    let keypair = load_keypair(&mm_config.market.maker_keypair_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let ix = build_update_expiry_in_slots_ix(&keypair.pubkey(), &market, slots);
    let sig = client
        .send_instructions(
            &[ix],
            &[&keypair],
            send_options(&mm_config, TxPriority::Normal, CU_SET_EXPIRY),
        )
        .await?;
    if slots == 0 {
        println!("expiry_in_slots set to 0 (disabled): {sig}");
    } else {
        println!("expiry_in_slots set to {slots}: {sig}");
    }
    Ok(())
}

async fn cmd_deposit(config_path: &std::path::Path, base: f64, quote: f64) -> Result<()> {
    let mm_config = load_config(config_path)?;
    init_tracing(&mm_config.monitoring.log_level);
    require_live_tx_enabled("deposit into Archer maker book")?;
    let keypair = load_keypair(&mm_config.market.maker_keypair_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let sdk_config = client.get_market_config(&market).await?;
    let rpc = RpcClient::new(mm_config.connection.rpc_url.clone());
    let programs =
        resolve_token_programs(&rpc, &sdk_config.base_mint, &sdk_config.quote_mint).await?;
    let maker_base_ata = spl_associated_token_account::get_associated_token_address_with_program_id(
        &keypair.pubkey(),
        &sdk_config.base_mint,
        &programs.base,
    );
    let maker_quote_ata =
        spl_associated_token_account::get_associated_token_address_with_program_id(
            &keypair.pubkey(),
            &sdk_config.quote_mint,
            &programs.quote,
        );
    let ix = build_deposit_ix(
        &keypair.pubkey(),
        &market,
        base,
        quote,
        &maker_base_ata,
        &maker_quote_ata,
        &programs.base,
        &programs.quote,
        &sdk_config,
    )?;
    let sig = client
        .send_instructions(
            &[ix],
            &[&keypair],
            send_options(&mm_config, TxPriority::Normal, CU_DEPOSIT),
        )
        .await?;
    println!("Deposited {base} base + {quote} quote: {sig}");
    Ok(())
}

async fn cmd_withdraw(config_path: &std::path::Path) -> Result<()> {
    let mm_config = load_config(config_path)?;
    init_tracing(&mm_config.monitoring.log_level);
    require_live_tx_enabled("withdraw from Archer maker book")?;
    let keypair = load_keypair(&mm_config.market.maker_keypair_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let sdk_config = client.get_market_config(&market).await?;
    let rpc = RpcClient::new(mm_config.connection.rpc_url.clone());
    let programs =
        resolve_token_programs(&rpc, &sdk_config.base_mint, &sdk_config.quote_mint).await?;
    let (maker_book_pda, _) = MakerBook::get_address(&market, &keypair.pubkey());
    let account = rpc
        .get_account(&maker_book_pda)
        .await
        .context("MakerBook not found")?;
    let book = MakerBook::load(&account.data)?;

    let total_base = book.base_free + book.base_locked;
    let total_quote = book.quote_free + book.quote_locked;
    if total_base == 0 && total_quote == 0 {
        println!("Nothing to withdraw.");
        return Ok(());
    }
    println!(
        "  Base:  {} free, {} locked",
        book.base_free, book.base_locked
    );
    println!(
        "  Quote: {} free, {} locked",
        book.quote_free, book.quote_locked
    );

    let mut ixs = Vec::new();
    if book.base_locked > 0 || book.quote_locked > 0 {
        println!("  Locked funds detected — prepending ClearBook");
        ixs.push(build_clear_book_ix(
            &keypair.pubkey(),
            &market,
            &keypair.pubkey(),
            book.last_updated_sequence_number + 1,
        ));
    }

    let maker_base_ata = spl_associated_token_account::get_associated_token_address_with_program_id(
        &keypair.pubkey(),
        &sdk_config.base_mint,
        &programs.base,
    );
    let maker_quote_ata =
        spl_associated_token_account::get_associated_token_address_with_program_id(
            &keypair.pubkey(),
            &sdk_config.quote_mint,
            &programs.quote,
        );

    let wb = if book.base_locked > 0 {
        total_base
    } else {
        book.base_free
    };
    let wq = if book.quote_locked > 0 {
        total_quote
    } else {
        book.quote_free
    };
    let wb_ui = (wb as f64) * (sdk_config.base_atoms_per_base_lot as f64)
        / 10f64.powi(sdk_config.base_decimals as i32);
    let wq_ui = (wq as f64) * (sdk_config.quote_atoms_per_quote_lot as f64)
        / 10f64.powi(sdk_config.quote_decimals as i32);

    if wb > 0 || wq > 0 {
        if wb > 0 {
            ixs.push(
                spl_associated_token_account::instruction::create_associated_token_account_idempotent(
                    &keypair.pubkey(),
                    &keypair.pubkey(),
                    &sdk_config.base_mint,
                    &programs.base,
                ),
            );
        }
        if wq > 0 {
            ixs.push(
                spl_associated_token_account::instruction::create_associated_token_account_idempotent(
                    &keypair.pubkey(),
                    &keypair.pubkey(),
                    &sdk_config.quote_mint,
                    &programs.quote,
                ),
            );
        }
        ixs.push(build_withdraw_ix(
            &keypair.pubkey(),
            &market,
            wb_ui,
            wq_ui,
            &maker_base_ata,
            &maker_quote_ata,
            &programs.base,
            &programs.quote,
            &sdk_config,
        )?);
    }
    let sig = client
        .send_instructions(
            &ixs,
            &[&keypair],
            send_options(&mm_config, TxPriority::Normal, CU_WITHDRAW),
        )
        .await?;
    println!("Withdrawn: {sig}");
    Ok(())
}

async fn cmd_kill(config_path: &std::path::Path) -> Result<()> {
    let mm_config = load_config(config_path)?;
    init_tracing(&mm_config.monitoring.log_level);
    require_live_tx_enabled("clear Archer maker book")?;
    let keypair = load_keypair(&mm_config.market.maker_keypair_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let book = client.get_maker_book(&market, &keypair.pubkey()).await?;
    let ix = build_clear_book_ix(
        &keypair.pubkey(),
        &market,
        &keypair.pubkey(),
        book.last_updated_sequence_number + 1,
    );
    let sig = client
        .send_instructions(
            &[ix],
            &[&keypair],
            send_options(&mm_config, TxPriority::Emergency, CU_CLEAR_BOOK),
        )
        .await?;
    println!("Book cleared: {sig}");
    Ok(())
}

async fn cmd_status(config_path: &std::path::Path) -> Result<()> {
    let mm_config = load_config(config_path)?;
    let market: Pubkey = mm_config.market.market_pubkey.parse()?;
    let keypair = load_keypair(&mm_config.market.maker_keypair_path)?;
    let client = ArcherClient::new(&mm_config.connection.rpc_url);
    let sdk_config = client.get_market_config(&market).await?;
    let book = client.get_maker_book(&market, &keypair.pubkey()).await?;
    let bal = maker_balances(&book, &sdk_config);
    let rpc = RpcClient::new(mm_config.connection.rpc_url.clone());
    let market_account = rpc.get_account(&market).await?;
    let header = parse_market_state(&market_account.data)?;
    let mode = market_mode_label(header.mode);

    println!("=== Archer Market Maker Status ===");
    println!("Market:       {market}");
    println!("Maker:        {}", keypair.pubkey());
    println!("Mode:         {mode}");
    println!("Mid ticks:    {}", book.mid_price_ticks);
    println!("Bid levels:   {}", active_bid_levels(&book));
    println!("Ask levels:   {}", active_ask_levels(&book));
    println!("Base free:    {:.6}", bal.base_free);
    println!("Base locked:  {:.6}", bal.base_locked);
    println!("Quote free:   {:.4}", bal.quote_free);
    println!("Quote locked: {:.4}", bal.quote_locked);
    Ok(())
}

fn market_mode_label(mode: u8) -> &'static str {
    match mode {
        0 => "Continuous",
        1 => "Asynchronous",
        2 => "Hybrid",
        _ => "Unknown",
    }
}

fn ticks_to_price(
    mid_ticks: u64,
    offset_ticks: i64,
    config: &crate::archer::config::MarketConfig,
) -> f64 {
    let ticks = (mid_ticks as i64).saturating_add(offset_ticks).max(0) as u64;
    ticks as f64 * config.ticks_to_price_factor()
}

fn require_live_tx_enabled(operation: &str) -> Result<()> {
    let enabled = std::env::var(LIVE_TRADING_ENV)
        .map(|value| value.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    anyhow::ensure!(
        enabled,
        "{operation} blocked: set {LIVE_TRADING_ENV}=true explicitly"
    );
    Ok(())
}

fn load_keypair(path: &str) -> Result<Keypair> {
    let resolved = resolve_path(path);
    read_keypair_file(&resolved)
        .map_err(|e| anyhow::anyhow!("Failed to load keypair from {}: {e}", resolved.display()))
}

fn init_tracing(level: &str) {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new(format!("archer_market_maker={level},warn")));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .with_timer(tracing_subscriber::fmt::time::uptime())
        .compact()
        .init();
}
