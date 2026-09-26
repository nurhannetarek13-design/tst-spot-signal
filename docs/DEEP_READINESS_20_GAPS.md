# Deep Readiness 20-Gap Closure Matrix

All items below have a deterministic implementation path and test/contract. "Data-backed" means the runtime collects or consumes the required evidence; it does not claim profitable edge.

1. Probability/EV calibration — native shadow EV model + calibration metrics + bucket calibration primitive; promotion remains evidence-gated.
2. Uncertainty/abstention — UNKNOWN/SKIP path; missing calibration/data/regime confidence abstains.
3. Regime transition — runtime transition worker; uncertain/transition blocks new execution.
4. Dynamic correlation — rolling pair/BTC/ETH correlation and effective-risk calculation.
5. Portfolio stress — BTC/liquidity/spread/slippage scenario loss projection.
6. Market impact/capacity — depth/flow/spread/volatility capacity ceiling.
7. Queue position — queue-ahead/cancel/trade-through expected-fill decision.
8. Fill toxicity — post-fill adverse movement at 100ms/500ms/1s/5s.
9. Exact PnL — Binance myTrades individual fills, VWAP and commissions; unknown fee FX never guessed.
10. Dust/orphan inventory — residual inventory explicitly classified and accounted separately.
11. Exchange degraded mode — matrix for WS/user stream/REST/DB/rate limit/latency/maintenance; new entries fail closed.
12. Disaster recovery — checksummed snapshots + verification + RPO/RTO targets + supervised runtime backup worker. Off-platform replication remains deployment-provider configuration.
13. E2E latency — stage-by-stage attribution and budget violations.
14. Data/feature drift — PSI drift states with promotion block.
15. Security — signed timestamped nonce webhooks, replay rejection, authenticated/idempotent emergency controls; existing secret/API separation retained.
16. Schema/API contracts — required field/type validation, fail closed on critical mismatch.
17. Baseline/control strategies — explicit net comparison against simple controls.
18. False discovery — hypothesis count + Benjamini-Hochberg FDR.
19. Strategy dependency — return/signal correlation graph flags nominal strategies sharing the same factor.
20. Human emergency controls — PAUSE_NEW_ENTRIES / PROTECTION_ONLY / RECONCILE_ONLY / READ_ONLY / EXIT_ALL, authenticated and auditable.

Additional hardening: deep spoofing/cancellation diagnostics and a production incident runbook.

No component in this tranche may enable Live or increase risk by itself.
