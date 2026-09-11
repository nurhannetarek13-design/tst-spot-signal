# TST Spot Bot V2

Standalone Binance Spot research/execution engine, isolated from the legacy V1 execution chain.

## Current operating state

- Default mode: `SHADOW`
- `V2_LIVE_TRADING=false` by default
- Paper and Live require proven persistent state across different deployment revisions
- Live startup also blocks if the execution journal contains an unresolved execution
- No Binance private API credentials are required for SHADOW
- No Futures, leverage, withdrawals, or martingale

## Signal rules

A candidate is actionable only when every mandatory gate passes. Score cannot compensate for a failed gate.

- USDT Spot pair
- 24h quote volume >= 20M USDT
- spread <= configured limit
- BTC 1h bullish regime
- target 15m + 1h + 4h bullish structure
- entry setup is either confirmed Breakout + Retest or confirmed EMA20 Pullback + Reclaim
- raw breakout alone is PRE-ALERT only and explicitly means NO ENTRY
- relative quote volume >= 1.5x
- taker-buy quote ratio >= 56%
- score >= 90
- closed candles only

Stablecoin/fiat bases (including RLUSD) and non-ASCII/noisy symbols are excluded from the universe.

## Execution safety layers

Before any action that could become executable, V2 applies Binance Spot preflight to entry and protective exits using exchange filters including `PRICE_FILTER`, `LOT_SIZE`, and `NOTIONAL`.

A private Spot adapter now exists for test coverage only. It is **not wired into `V2Engine` and cannot be enabled by setting environment variables alone**. Its design is fail-closed:

1. Create a durable execution journal intent before the first private request.
2. Place MARKET BUY using a deterministic `newClientOrderId`.
3. If BUY returns a transport error or potentially-unknown 5xx result, query the same client order ID instead of retrying blindly.
4. Subtract commissions paid in the base asset and round quantity down to the symbol step size.
5. Re-check post-fill quantity/min-notional viability.
6. Place SELL OCO protection using TAKE_PROFIT + STOP_LOSS market-triggered legs.
7. If OCO result is uncertain, reconcile the order list by deterministic list client ID and do not place a replacement blindly.
8. If OCO is definitively rejected, attempt an emergency MARKET SELL to flatten.
9. Any unresolved BUY/OCO/unprotected execution remains pending in the journal and blocks future Live startup.

The private adapter remains deliberately disconnected until durable storage, account reconciliation, isolated deployment, explicit Live authorization, and forward strategy evidence are complete.

## SHADOW forward evidence

Confirmed SHADOW signals can be tracked in a research-only outcome ledger. It never places orders.

- entry uses executable-side ask rather than candle close
- only later closed 15m candles may resolve the outcome
- the signal candle cannot determine its own result
- TP/SL PnL includes modeled entry + exit fees
- if TP and SL are crossed in the same closed candle, the outcome is `AMBIGUOUS` rather than optimistically counted as a win
- ambiguous outcomes are excluded from decisive PnL/win-rate metrics and measured separately

The ledger reports decisive sample size, wins/losses, win rate, ambiguity rate, gross profit, gross loss, net PnL, fee-aware expectancy, average win/loss and profit factor. If no losing observation has occurred yet, profit factor remains **unproven (`None`)**, not infinity.

## Promotion gates

Paper and Live are intentionally different stages.

### Paper readiness

Paper is a testing mode. It does **not** require prior profitability evidence, but it does require safe mechanics:

- durable state enabled
- cross-deployment persistence proven
- deployment revision present
- Binance execution preflight available

### Live readiness

Live requires all Paper infrastructure gates plus execution/recovery readiness and independent SHADOW evidence.

The default strategy-evidence promotion guardrail requires:

- at least **60 decisive** forward outcomes
- **profit factor >= 1.20**
- **expectancy > 0 USDT/trade** after modeled fees
- **ambiguous outcome rate <= 10%**

These thresholds are a conservative promotion guardrail, not a guarantee of future profitability.

Live also requires zero unresolved execution-journal records, private API credentials, an intentionally wired private adapter, explicit Live authorization, deliberate removal of the engine hard-lock, and verified emergency-flatten behavior.

## State and persistence

SQLite stores:

- Paper open positions
- Paper trades and fee-aware PnL
- emitted signal dedupe keys
- cross-deploy persistence marker
- SHADOW forward outcomes
- future Live execution recovery journal

Paper/Live are fail-closed unless `V2_PERSISTENT_STATE=true`, `V2_DEPLOY_REV` is set, and the state database proves that it survived a different deployment revision. A merely writable file is not considered proof of persistence.

## Risk defaults

- trade size: 10 USDT
- max daily realized loss: 2 USDT
- max open positions: 1
- TP: +0.90%
- SL: -0.62%
- modeled Paper fee: 0.10% each side

These are safety/configuration defaults, not claims of profitability.

## Telegram

Telegram is optional. SHADOW can send:

- strict breakout PRE-ALERT (`NO ENTRY — waiting retest`)
- confirmed SHADOW signal after all strategy/risk/exchange-preflight gates pass

Telegram failures do not crash the market scanner.

## Running

One cycle:

```bash
python -m v2_bot.main --once
```

Continuous SHADOW worker:

```bash
V2_MODE=shadow V2_LIVE_TRADING=false python -m v2_bot.main
```

## Container defaults

`v2_bot/Dockerfile` defaults to:

- `V2_MODE=shadow`
- `V2_LIVE_TRADING=false`
- `V2_STATE_DB=/data/v2_state.sqlite3`

Attach persistent storage at `/data` before considering Paper. Do not consider Live until the persistence probe, execution/recovery gates and strategy-evidence promotion gate all pass.

## What green CI means

Green CI means the software mechanics, safety gates, tests, compilation, container build, and fail-closed defaults passed. It does **not** mean the strategy has a proven profitable edge or that Live trading is authorized.
