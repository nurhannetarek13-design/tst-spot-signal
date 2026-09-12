# TST Spot Bot V2

Standalone Binance Spot research/trading engine. V2 is intentionally fail-closed and isolated from real-money execution unless every Live gate is deliberately satisfied.

## Current runtime state

- Mode: `paper`
- Live trading: `false`
- State backend: Neon/Postgres
- Cross-deploy persistence: proven
- Trade size: 10 USDT
- Max daily loss: 2 USDT
- Max open positions: 1
- Take profit: +0.90%
- Stop loss: -0.62%
- Paper fee model: 0.10% per side
- Telegram: enabled
- Private Binance credentials: not connected
- Private adapter: disabled
- Explicit Live authorization: false
- Live engine unlock: false
- Emergency flatten verification flag: false

## Entry requirements

An actionable entry must pass every gate independently:

- USDT Spot universe hygiene
- 24h quote volume >= 20M USDT
- spread <= 15 bps
- BTC 1h bullish regime
- target 15m bullish trend
- target 1h bullish trend
- target 4h bullish trend
- confirmed Breakout + Retest OR confirmed EMA20 Pullback + Reclaim
- relative quote volume >= 1.5x
- taker-buy ratio >= 56%
- score >= 90
- Binance Spot execution preflight for entry + TP + SL

Raw breakout is PRE-ALERT only and cannot enter a trade.

## Paper execution

Paper positions are persisted in Postgres. The engine models entry and exit fees, daily loss control, maximum open positions, TP/SL exits, and duplicate-signal suppression. After each Paper close, Telegram reports the trade result and cumulative metrics:

- closed trades
- win rate
- net PnL
- expectancy
- profit factor
- max drawdown
- max consecutive losses

Paper evidence is not considered promotion-ready until at least 60 closed trades exist with profit factor >= 1.20 and fee-aware expectancy > 0.

## Live execution safety

The private execution stack is present but dormant. It includes signed Binance Spot requests, deterministic client order IDs, execution journaling, BUY ambiguity reconciliation, post-fill protection viability checks, OCO protection, emergency flatten logic, and read-only startup reconciliation.

Paper and Shadow never construct the private Binance adapter. Live also remains blocked by independent gates for credentials, adapter enablement, evidence, recovery state, explicit authorization, engine unlock, and emergency-flatten verification.

No withdrawal functionality, Futures, leverage, or martingale exists in V2.

## Persistent state

Postgres schema migration:

`v2_bot/sql/001_postgres_state.sql`

State tables include positions, trades, emitted signals, runtime metadata, execution journal, and SHADOW outcomes.

SQLite remains the fail-closed local/container default. Postgres is selected only when `V2_STATE_BACKEND=postgres` and a valid `V2_DATABASE_URL` are explicitly configured.

## Operational rule

Do not loosen entry filters simply to increase trade frequency. No real-money promotion should occur until Paper evidence is sufficiently large and positive, private credentials are deliberately configured with Spot Trade permission only, recovery state is clear, and Live is explicitly authorized.
