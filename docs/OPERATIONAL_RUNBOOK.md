# TST Spot Bot Operational Runbook

This runbook is safety-first. It never authorizes Live trading.

## Control modes
- RUN: normal policy gates still apply.
- PAUSE_NEW_ENTRIES: reject new BUY; preserve exchange-side protection.
- PROTECTION_ONLY: no entries; only inspect/repair existing protection.
- RECONCILE_ONLY: read Binance state and repair local truth; no new orders.
- READ_ONLY: diagnostics only.
- EXIT_ALL: human-authenticated emergency action only; idempotent command ID required.

## Incident matrix
| Incident | New entries | Existing OCO/SL | Reconcile | Operator action |
|---|---|---|---|---|
| Market WS down | BLOCK | leave intact | REST reconcile | investigate feed |
| User stream down | BLOCK | leave intact | REST reconcile | restore stream |
| REST down | BLOCK | leave intact | wait/fail closed | do not blind retry |
| DB/storage degraded | BLOCK | leave intact | if state reliable | restore storage |
| 429/high rate use | BLOCK | leave intact | reserve critical budget | back off |
| extreme latency | BLOCK | leave intact | yes | diagnose stage trace |
| Binance maintenance | BLOCK | leave intact | when available | no blind cancel |
| unknown execution outcome | BLOCK | leave intact | client-ID recovery | never resend blindly |
| ledger/schema integrity failure | BLOCK | leave intact | investigate | restore verified state |

## Disaster recovery
Targets: RPO 5 minutes, RTO 15 minutes. Runtime creates checksum manifests. Production requires replication of snapshots outside the Railway volume. Restore only from a checksum-verified snapshot, then run reconciliation before enabling entries. A restored node starts with Live authorization OFF.

## Security
Execution credentials must have withdrawals disabled and minimum permissions. Prefer IP allowlisting when supported. Never log secrets. Rotate secrets after suspected exposure. Internal execution messages require signature + timestamp/nonce; duplicate command IDs are rejected. Telegram is not an execution authority by itself.

## Recovery sequence
1. PAUSE_NEW_ENTRIES.
2. Verify ledger/snapshot checksum.
3. Restore state if needed.
4. Reconcile Binance orders, fills, balances and OCO protection.
5. Resolve UNKNOWN reservations by deterministic client ID.
6. Verify no unprotected filled position.
7. Verify schema/contracts and data freshness.
8. Run health/watchdog.
9. Resume Paper/Shadow first. Live remains a separate explicit authorization.

## Research promotion
No strategy is promoted because it has the best backtest. Require calibrated forward evidence, baseline excess after costs, drift stable, multiple-testing/FDR record, strategy dependency review, and Champion/Challenger evidence.