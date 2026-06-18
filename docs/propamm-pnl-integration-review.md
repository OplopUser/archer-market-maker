# PropAMM PnL Integration Review

Date: 2026-06-13

## Decision

Do not integrate Archer into the live PropAMM PnL system yet. Treat it as a
research/pilot bot until the current dirty worktree is stabilized and its own
edge, fills, costs, and operational risks are measured.

## Evidence

- The local Archer worktree has many modified files and untracked runtime,
  dashboard, config, Docker, docs, and script artifacts.
- Archer brings useful professional market-making ideas: structure hashing,
  explicit transaction path selection, shadow/live separation, and priority-fee
  discipline.
- Archer is a separate venue/protocol risk surface. Adding live capital before
  fill/markout logging and dashboard-compatible health output would make the
  combined PnL system harder to reason about.

## Go Criteria

- Dirty worktree is inventoried, committed, or intentionally split.
- Shadow or paper run produces fill, markout, cost, and inventory telemetry.
- Expected risk-adjusted return beats simply reallocating capital among
  SOL/USDC, SOL/USDT, SOL/USD1, and Phoenix hedge collateral.
- Dashboard output can be ingested by the same combined PnL ledger.
- Live runbook exists for pause, clear book, withdraw, rollback, and failure
  triage.

## No-Go Criteria

- No auditable fill/markout evidence.
- Unbounded live transaction path or unclear priority-fee behavior.
- No dashboard-compatible health/PnL state.
- Return case depends on optimistic fills or unmodeled venue incentives.

## Next Action

Complete a read-only Archer branch inventory and one shadow-mode validation
before opening a live-capital rollout task.
