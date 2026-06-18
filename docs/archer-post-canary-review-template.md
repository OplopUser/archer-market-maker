# Archer Post-Canary Review Template

Use this after any Archer shadow or capped-live canary before approving the next
run mode. Uptime alone is not a promotion signal.

## Run Metadata

- Run id:
- Operator:
- Review timestamp:
- Mode reviewed: shadow / canary
- Market:
- Config path:
- Config checksum:
- Canary envelope path:
- Canary envelope checksum:
- Approval id:
- Artifact directory:
- Rollback command:
- Clean-book verification artifact:

## Required Decision

Select exactly one:

- Stop Archer.
- Repeat shadow.
- Repeat capped live canary.
- Run Archer alone on the scoped market.
- Run Manifest plus Archer with explicit coexistence controls.
- Promote with capped capital.

Decision:

Next permitted run mode:

Capital cap for next run:

Expiry for next run:

## Evidence Checklist

| Gate | Pass/Warn/Fail | Evidence artifact | Notes |
|------|----------------|-------------------|-------|
| Config and source checksum match approval | | | |
| Market-intel fresh and scoped to Archer | | | |
| MakerBook started clean | | | |
| MakerBook ended clean or rollback cleared it | | | |
| Wallet balances stayed inside envelope | | | |
| One level per side cap held | | | |
| Notional cap held | | | |
| Transaction budget held | | | |
| TX success and failure burst gate | | | |
| Stale data and route-quality gate | | | |
| Fills and no-fill exposure explained | | | |
| After-cost edge by side | | | |
| Toxicity and markout | | | |
| Hedge and inventory risk | | | |
| Manifest coexistence and self-quote guard | | | |
| Alert/runbook behavior | | | |
| Rollback proof retained | | | |

## Promotion Notes

- What Archer did as expected:
- What Archer did not do as expected:
- Unexpected fills, cancels, or MakerBook drift:
- Fee, priority-fee, or RPC issues:
- Market-intel or reference-price issues:
- Hedge/risk blocker:
- Manifest coexistence blocker:
- Follow-up tickets required:

## Sign-Off

- Reviewer:
- Approved decision:
- Conditions before next run:
- Link to retained artifacts:
