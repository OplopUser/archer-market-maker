# Archer Market Maker

[Archer](https://archer.exchange) is a fully on-chain order book exchange on Solana that eliminates adverse selection faced by market makers through sovereign maker books, parametric pricing, and pro-rata execution. Instead of a single shared order book, each market maker owns their own on-chain book — enabling zero write-lock contention, O(1) repricing, and incentives that reward depth over speed. [Read more about how Archer works](https://x.com/mmdhrumil/status/2026301400158810390).

> **Caution:** Archer Exchange smart contract audits are currently in progress. Please use this software at your own discretion and start with lower funds.

A simple market maker for the Archer Exchange.

This PropAMM workspace version adds conservative defaults around the upstream
starter: shadow mode is enabled by default, every transaction-sending command
requires `ARCHER_ENABLE_LIVE_TRADING=true`, and the strategy has explicit
notional caps, dust filters, and reserve buffers.

Places bid and ask orders on an Archer on-chain orderbook using Binance WebSocket prices as a reference, with optional cross-tick synthetic pricing. Designed to be **easy to understand** and **a starting point** for building your own strategy.

## How It Works

The bot is **event-driven** — it reacts instantly to WebSocket price changes instead of polling:

1. **Price change** — when the feed delivers a new mid price that changes the on-chain tick, the engine fires an update immediately
2. **Heartbeat** — if no price change occurs for `heartbeat_interval_ms` (default 100ms), a heartbeat update is sent with the freshest price
3. **Compute quotes** — places 8 bid/ask levels at volatility-adjusted bps offsets from mid
4. **Send transaction** — picks the cheapest Solana instruction type to update the on-chain book

```
Binance WebSocket                     Archer Exchange
  (live book ticker)                   (on-chain orderbook)
       │                                      ▲
       ▼                                      │
  ┌──────────┐  notify  ┌──────────┐   ┌────────────────┐
  │  Feed    │ ──────▶  │  Engine  │──▶│  TX Sender     │
  │ (stream) │          │ (event)  │   │ (fire & forget)│
  └──────────┘          └──────────┘   └────────────────┘
                     price change │ heartbeat timeout
                             Strategy
                         (vol-adjusted spreads)
```

### What gets placed on the book

Spreads widen automatically when volatility is high. The strategy tracks realized volatility (standard deviation of log returns) over the last 300 price samples and scales all spread levels by a multiplier:

```
  multiplier = max(1.0, realized_vol / baseline_vol)    (capped at vol_max_multiplier)
```

In calm markets (vol at or below baseline), spreads stay as configured. When vol rises above baseline, all levels widen proportionally:

```
  Asks:  mid + 25 bps × vol_mult  ─── Level 8
         mid + 20 bps × vol_mult  ─── Level 7
         mid + 15 bps × vol_mult  ─── Level 6
         mid + 12 bps × vol_mult  ─── Level 5
         mid + 10 bps × vol_mult  ─── Level 4
         mid +  7 bps × vol_mult  ─── Level 3
         mid +  5 bps × vol_mult  ─── Level 2
         mid +  2 bps × vol_mult  ─── Level 1 (tightest)
  ────── Mid price ──────────────────────────────
  Bids:  mid -  2 bps × vol_mult  ─── Level 1 (tightest)
         mid -  5 bps × vol_mult  ─── Level 2
         mid -  7 bps × vol_mult  ─── Level 3
         mid - 10 bps × vol_mult  ─── Level 4
         mid - 12 bps × vol_mult  ─── Level 5
         mid - 15 bps × vol_mult  ─── Level 6
         mid - 20 bps × vol_mult  ─── Level 7
         mid - 25 bps × vol_mult  ─── Level 8
```

Each level quotes an equal share of your deposited inventory.

### CU Optimization

Solana transactions cost compute units. The bot detects what changed since last cycle and picks the cheapest instruction:

| Instruction | CU Cost | When |
|-------------|---------|------|
| `UpdateMidPrice` | ~400 | Price moved but level structure unchanged (most cycles) |
| `UpdateBook` | ~5,000 | Level sizes or count changed |
| `ClearBook` | ~180 | Shutdown, error, or stale feed |

In practice, **~90% of cycles use the cheap mid-only path**, saving ~85% of CU.

## Quick Start

### Prerequisites

- [Rust](https://rustup.rs) 1.85+
- [Solana CLI](https://docs.anza.xyz/cli/install)
- An RPC endpoint ([Helius](https://helius.dev), [Triton](https://triton.one), or [QuickNode](https://quicknode.com))
- A funded Solana wallet

### 1. Build

```bash
git clone https://github.com/ArcherExchange/archer-market-maker.git
cd archer-market-maker
cargo build --release
```

### 2. Configure

Edit `config/default.toml`:

```toml
[market]
market_pubkey = "YOUR_MARKET_PUBKEY"
maker_keypair_path = "~/.config/solana/id.json"

[connection]
rpc_url = "https://mainnet.helius-rpc.com"

[feed]
binance_symbol = "SOLUSDT"
# Optional: derive a synthetic pair via cross-tick division
# cross_symbol = "BTCUSDT"   # price = SOLUSDT / BTCUSDT
```

### 3. Preflight and preview

These commands do not send transactions:

```bash
cargo run -- market
cargo run -- preview --mid 150 --base 1.0 --quote 150.0
cargo run -- run --shadow
```

### 4. Initialize and deposit

```bash
export ARCHER_ENABLE_LIVE_TRADING=true

# Create your maker book on-chain (one-time)
cargo run --release -- init

# Deposit tokens. This size clears the default four-level dust filter.
cargo run --release -- deposit --base 0.7 --quote 60.0
```

### 5. Run

```bash
# Shadow first. This is also the config default.
cargo run --release -- run --shadow

# Run for real. Requires ARCHER_ENABLE_LIVE_TRADING=true.
cargo run --release -- run --live
```

### 6. Stop

```bash
# Ctrl+C — clears the book on shutdown

# Or emergency kill from another terminal
export ARCHER_ENABLE_LIVE_TRADING=true
cargo run --release -- kill
```

## CLI Commands

```
archer-market-maker <COMMAND>

  run         Start the market maker
  market      Print market metadata without a maker book
  preview     Preview generated quotes without sending transactions
  init        Initialize maker book on-chain (one-time)
  deposit     Deposit base + quote tokens
  withdraw    Withdraw all funds
  kill        Emergency: clear all orders immediately
  status      Print on-chain book state
  set-expiry  Set expiry_in_slots (aggregator skips this book's quotes
              once `current_slot - last_updated_slot >= expiry_in_slots`;
              `--slots 0` disables the check)
```

## Configuration

All settings in `config/default.toml`:

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `market` | `market_pubkey` | — | Archer market public key |
| `market` | `maker_keypair_path` | — | Path to Solana keypair |
| `connection` | `rpc_url` | — | Solana RPC endpoint |
| `feed` | `binance_symbol` | — | Binance symbol (e.g. `SOLUSDT`) |
| `feed` | `cross_symbol` | `""` | Cross pair for synthetic pricing (e.g. `BTCUSDT`) |
| `feed` | `binance_ws_url` | `wss://stream.binance.com:9443/ws` | Binance WebSocket endpoint |
| `feed` | `staleness_timeout_ms` | `5000` | Pull quotes if feed stale |
| `strategy` | `spread_levels_bps` | `[10,20,35,55]` | Base bps offset per level |
| `strategy` | `inventory_pct` | `50` | % of inventory to quote |
| `strategy` | `vol_window` | `300` | Rolling window size (price samples) for volatility |
| `strategy` | `vol_baseline_bps` | `5.0` | Per-sample vol (bps) at which spreads are unchanged |
| `strategy` | `vol_max_multiplier` | `5.0` | Maximum spread multiplier from vol scaling |
| `risk` | `min_quote_notional` | `5.0` | Skip dust quote levels below this notional |
| `risk` | `max_quote_notional_per_level` | `25.0` | Per-level notional cap |
| `risk` | `max_total_quote_notional` | `200.0` | Total quote notional cap across both sides |
| `risk` | `min_base_reserve_pct` | `20.0` | Base inventory kept unquoted |
| `risk` | `min_quote_reserve_pct` | `20.0` | Quote inventory kept unquoted |
| `execution` | `heartbeat_interval_ms` | `100` | Max idle time before heartbeat update |
| `execution` | `priority_fee_mode` | `dynamic` | Sample account-local recent fees instead of always paying fixed CU price |
| `execution` | `priority_fee_microlamports` | `100` | Fixed-mode fee and dynamic fallback |
| `execution` | `priority_fee_max_microlamports` | `5000` | Dynamic fee cap |
| `execution` | `priority_fee_percentile` | `50` | Recent-fee percentile to pay |
| `execution` | `priority_fee_cache_ms` | `10000` | Fee sample cache TTL |
| `execution` | `min_mid_update_interval_ms` | `5000` | Minimum gap for tiny mid-only updates |
| `execution` | `min_mid_update_ticks` | `25` | Ignore mid-only moves below this tick delta inside the interval |
| `execution` | `shadow_mode` | `true` | Dry run mode |
| `monitoring` | `log_level` | `info` | Log verbosity |

## Project Structure

```
src/
├── main.rs          CLI + orchestration
├── config.rs        TOML config
├── feed.rs          Binance WebSocket price feed (with cross-tick support)
├── strategy.rs      Vol-adjusted spread levels + CU optimization
├── volatility.rs    Realized vol tracker (log returns, ring buffer)
├── engine.rs        Core loop: price → strategy → TX
├── state.rs         Shared atomic state
├── tx.rs            Fire-and-forget TX sender
└── archer/          Self-contained Archer protocol client
    ├── types.rs     On-chain account layouts (MakerBook, MarketStateHeader)
    ├── config.rs    MarketConfig with conversion factors
    ├── math.rs      Price/lot conversions + book update builder
    ├── ix_builder.rs  Instruction builders for all maker operations
    ├── accounts.rs  Account parsing + balance helpers
    └── client.rs    High-level RPC client
```

## Adding Your Own Strategy

Edit `strategy.rs`. The `compute()` method takes a mid price and inventory, returns a `QuoteDecision`. The engine and TX layers don't change.

Ideas to try:
- Lean quotes based on inventory (shift mid toward the side you want to offload)
- Add multiple price sources and take the median

## License

Apache-2.0
