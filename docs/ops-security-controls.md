# Ops Security Controls

Archer has a compatibility ops-security shim for live approval artifacts, runtime kill-switch proof files, hash-chained audit records, alert drills, and fire-drill templates.

Required live approval artifact fields:

```json
{
  "venue": "archer",
  "market": "SOL/USDC",
  "operator_id": "ops-lead",
  "ticket_ids": ["T-162"],
  "approved_at_ms": 1760000000000,
  "expires_at_ms": 1760000300000,
  "confirmation": "approve live archer SOL/USDC clear_book",
  "scope": "one guarded operation"
}
```

Drill helpers render retained artifacts only; they do not send external alerts. Keep provider tokens, wallet files, local logs, and live host transcripts outside the repo.
