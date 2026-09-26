# Production Incident Runbook

Live master gate remains authoritative. Never enable live to recover an incident.

| Incident | New entries | Existing protection | Required action |
|---|---|---|---|
| Market WS down | BLOCK | Leave exchange OCO intact | REST reconcile; restore stream; verify sequence snapshot |
| User stream down | BLOCK | Leave OCO intact | REST reconcile orders/fills before resume |
| REST down | BLOCK | Leave OCO intact | No blind cancel/resubmit; wait/reconcile |
| 429/418/high latency | BLOCK | Preserve protection | Reserve API budget for protection/reconciliation |
| State/DB degraded | BLOCK | Preserve OCO | Verify ledger + Binance; restore state before resume |
| Unknown order outcome | BLOCK | Preserve/recover | Resolve deterministic client ID; never retry blindly |
| Protection lost | BLOCK | Recovery only | Rebuild OCO from confirmed position or manual incident |
| Schema/contract mismatch | BLOCK | Preserve protection | Update contract/test before resume |
| Drift/regime uncertain | BLOCK | Existing positions managed | Wait for stable evidence |
| Railway loss | BLOCK | Exchange OCO remains source of protection | Restore verified snapshot, then reconcile Binance |

Human controls are authenticated/idempotent: PAUSE_NEW_ENTRIES, PROTECTION_ONLY, RECONCILE_ONLY, READ_ONLY, EXIT_ALL.
Every incident must preserve event/ledger evidence and record code/config version.

DR targets: RPO 5 minutes, RTO 15 minutes. Local snapshots require independent/off-platform replication to satisfy full disaster recovery; a snapshot on the same lost volume is not DR.
