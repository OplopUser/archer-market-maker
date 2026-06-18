# PropAMM Archer Runbook

Last updated: 2026-05-19

## Scope

This bot is an isolated Archer-native market maker. It should not be treated as
a drop-in replacement for `manifest-mm` because Archer uses per-maker
`MakerBook` accounts and cheap mid-price updates, while Manifest uses
cancel/replace batch updates against a shared market account.

## Safety Defaults

- `execution.shadow_mode = true` in `config/default.toml`.
- Live transaction submission requires `ARCHER_ENABLE_LIVE_TRADING=true`.
- `init`, `deposit`, `withdraw`, `kill`, `set-expiry`, and `run --live` are all
  blocked unless the live env gate is set.
- Start with small funds while Archer audits are still in progress.
- Priority fees default to dynamic account-local sampling with a `5000`
  microlamport/CU cap and a zero minimum, so the bot can send at 0 priority fee
  when recent writable-account fees are clearing at 0.
- Mid-only refreshes are throttled by default: moves below `25` ticks inside
  `5000` ms are skipped to avoid paying base Solana fees for noise.
- The active Archer web app SOL/USDC market is
  `u8tnfCb1JSSghuNFquQ2beStYgAN1kmd1f1Lhxbaec4`. The older
  `4G1A6nh...` account still decodes as a market, but live maker init rejected
  it during testing.

## First Checks

```bash
cd /Users/jeromehainaut/propamm/archer-market-maker
cargo run -- market
cargo run -- preview --mid 150 --base 1 --quote 150
cargo run -- run --shadow
```

`market` verifies the configured market account and prints mints, decimals,
fees, mode, and tick conversion. It does not require a maker book.

`preview` converts hypothetical base/quote balances into Archer MakerBook levels
using the configured spread and risk settings. It does not load a keypair or send
a transaction.

`run --shadow` requires an initialized maker book because it reads the on-chain
book state, but it does not submit transactions.

## Live Sequence

Default config uses four levels and needs enough notional to clear the `5`
quote-unit dust filter:

```bash
cd /Users/jeromehainaut/propamm/archer-market-maker
export ARCHER_ENABLE_LIVE_TRADING=true
cargo run --release -- init
cargo run --release -- deposit --base 0.7 --quote 60
cargo run --release -- status
cargo run --release -- run --live
```

Use `cargo run --release -- kill` from a second shell to clear the MakerBook.
The `kill` command also requires `ARCHER_ENABLE_LIVE_TRADING=true` because it is
still an on-chain transaction.

## Phoenix Wallet Smoke Test

Use this for the smallest live test with the Phoenix bot wallet:

```bash
cd /Users/jeromehainaut/propamm/archer-market-maker
export ARCHER_ENABLE_LIVE_TRADING=true
export ARCHER_MAKER_KEYPAIR_PATH=/path/to/live-wallet.json
export SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
cargo run --release -- init --config config/live-phoenix-smoke.toml
spl-token wrap 0.04 "$ARCHER_MAKER_KEYPAIR_PATH" \
  --fee-payer "$ARCHER_MAKER_KEYPAIR_PATH" \
  --url "$SOLANA_RPC_URL"
cargo run --release -- deposit --config config/live-phoenix-smoke.toml --base 0.037 --quote 5
cargo run --release -- run --config config/live-phoenix-smoke.toml --live
cargo run --release -- withdraw --config config/live-phoenix-smoke.toml
spl-token unwrap P5Nj5N8SMcA6qwKZMh1keoYkKTwjZc2NbMH182ff58L \
  "$ARCHER_MAKER_KEYPAIR_PATH" \
  --fee-payer "$ARCHER_MAKER_KEYPAIR_PATH" \
  --url "$SOLANA_RPC_URL"
```

The wrapped SOL deposit uses `0.037` rather than `0.04` because creating the
associated WSOL account consumes rent from the wrapped amount.

## Current Conservative Config

- Spreads: `10, 20, 35, 55` bps.
- Inventory quoted: `50%`.
- Reserve: `20%` base and `20%` quote left unquoted.
- Per-level cap: `25` quote units.
- Total notional cap: `200` quote units across both sides.
- Dust filter: skip quote levels below `5` quote units.
- Dynamic priority fee: p50 recent writable-account fee, `0` to `5000`
  microlamports/CU.
- Mid-only throttle: `25` ticks or `5000` ms.

## Profit-Seeking Validation

The stale-feed and update-churn fixes make the bot operationally safe, but they
do not prove profitability. The current live-safe profit probe is
`overnight_selective_edge_probe`: it quotes the historically safer `24, 42, 62`
bps band, uses small notional caps, and requires a `24` bps edge floor through
`min_effective_spread_bps`, `toxicity_buffer_bps`, and `min_net_edge_bps`.

Run supervised validation through `scripts/archer_supervised_validation.sh`.
The wrapper derives preflight and post-run gates from the selected profile, so a
selective profile is checked against its calibrated floor instead of the old
static `62` bps no-fill floor.

Before changing the probe floor, rerun the retained-fill calibration over the
historical samples:

```bash
python3 scripts/analyze_dashboard_samples.py \
  logs/adaptive-12h-20260526T145041Z \
  logs/adaptive-12h-20260527T044353Z \
  logs/adaptive-12h-20260528T055445Z \
  logs/adaptive-12h-20260528T135534Z \
  logs/adaptive-12h-20260528T144223Z \
  logs/adaptive-12h-20260528T151949Z \
  logs/adaptive-12h-20260528T161556Z \
  --calibrate-spread-floors 2.7,4.5,6.8,9.5,16,24,42,62
```

The calibration intentionally reports retained-fill replay, not only optimistic
repricing. This keeps a very wide floor from looking good when it probably would
not have filled.

## Next Build Steps

1. Add a local state snapshot file for every submitted or shadow update.
2. Add Prometheus or JSON health output compatible with the existing PropAMM dashboards.
3. Add a second reference source or medianized fair value before any meaningful size.
4. Add explicit markout logging once fills can be observed reliably from Archer.
