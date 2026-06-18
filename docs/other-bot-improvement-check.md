# Archer Starter Lessons For Existing Bots

Last updated: 2026-05-18

## Summary

The Archer starter has three useful ideas for the existing PropAMM bots:

1. Separate cheap price-only updates from expensive full book rebuilds.
2. Treat the quoted book shape as a hashable structure so unchanged size/level
   layouts do not trigger unnecessary churn.
3. Make every transaction path explicit about compute units, priority fee, and
   shadow/live behavior.

Not every idea transfers directly. Archer can do an O(1) `UpdateMidPrice`
because the protocol stores maker quotes as offsets from a maker-specific mid.
Manifest and Phoenix do not have the same primitive, so the improvement is to
copy the decision discipline, not the instruction itself.

## Manifest Bot

Relevant files:

- `/Users/jeromehainaut/propamm/manifest-mm/src/core/engine.rs`
- `/Users/jeromehainaut/propamm/manifest-mm/src/execution/builder.rs`
- `/Users/jeromehainaut/propamm/manifest-mm/src/execution/blockhash.rs`

What already exists:

- Cached blockhash and slot refresh.
- Dynamic or static priority fee source.
- Refresh threshold logic to avoid constant requotes.
- Cancel guards and two-phase replacement paths for safer live order state.
- Order expiry handling and tests around missing/extra/near-expiry live orders.

Useful carryover:

- Add clearer cost attribution by quote path: cancel-only, full replace,
  two-phase cancel, fallback asks-only, fallback bids-only.
- Add a structure hash metric for quote size/level changes, even if Manifest
  still has to use cancel/replace. This would separate "price moved" from
  "book shape changed" in telemetry.
- Consider per-variant compute-unit budgets if live data shows a meaningful fee
  difference between cancel-only and full replace.

Not a direct carryover:

- Archer `UpdateMidPrice` is not available on Manifest, so a true mid-only
  update cannot be ported without protocol support.

## Phoenix Perp Bot

Relevant files:

- `/Users/jeromehainaut/propamm/phoenix-perp-mm/src/phoenix_perp_mm/runtime.py`
- `/Users/jeromehainaut/propamm/phoenix-perp-mm/src/phoenix_perp_mm/risk.py`
- `/Users/jeromehainaut/propamm/phoenix-perp-mm/rise-executor/src/execute.ts`

What already exists:

- `PHOENIX_ENABLE_LIVE_TRADING=true` gate for live execution.
- Executor-level simulate/live distinction.
- Priority-fee planning.

High-value improvement from Archer:

- Stop treating every cycle as cancel/repost by default. The current Python
  runtime defaults `PHOENIX_CANCEL_BEFORE_PLACE` to true and the TypeScript
  executor defaults split order transactions to enabled unless
  `PHOENIX_SPLIT_ORDER_TXS=false`.
- Add a quote-structure hash and skip execution if symbol, side, price tick,
  size, reduce-only flag, and order count are unchanged.
- Default to single transaction submission unless a concrete Phoenix constraint
  requires splitting. This is especially important because prior live evidence
  tied SOL burn to repeated Phoenix cancel/repost fee churn.
- Emit per-cycle instruction count, split count, estimated priority lamports,
  and reason for execution.

Suggested implementation order:

1. Add a read-only dry-run metric that reports whether the cycle would execute
   or skip because the quote structure is unchanged.
2. Flip live defaults only after one dry-run session proves the skip logic is
   correct.
3. Keep emergency cancel paths separate from ordinary quote refresh paths.

## Archer Bot Itself

The new Archer app already has the first pass of these carryovers:

- Shadow mode is default.
- Live txs require `ARCHER_ENABLE_LIVE_TRADING=true`.
- Strategy config includes notional caps, dust filters, and reserves.
- `market` and `preview` commands allow setup checks without sending txs.

The next missing pieces are dashboard-compatible health output and fill/markout
logging once the Archer fill surface is verified.
