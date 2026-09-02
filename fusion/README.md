# TST Fusion Master

One user-facing bot, three specialist engines:

1. **Hummingbot V2 / Condor** — live market data, order-book aware candidate generation, and eventual deterministic execution.
2. **Freqtrade** — primary strategy/backtest and dry-run validator.
3. **Jesse** — independent second validator so one backtest engine cannot approve itself.

The master decision layer is in `fusion/server.mjs`. It accepts fresh validator reports from Freqtrade and Jesse, then evaluates Hummingbot candidates under one shared risk policy.

## Safety state

This release is deliberately **PAPER_ONLY**. The master always returns `executorAllowed: false` and `liveTrading: false`. It cannot authorize real Binance orders.

Hard limits are preserved from the current project:

- Binance Spot / USDT / long only
- no Futures, leverage, or withdrawals
- max one open position
- max position 7 USDT
- max risk 0.20 USDT
- daily realized loss cap 0.50 USDT

## Decision flow

`Hummingbot candidate -> Fusion Master -> Freqtrade validation + Jesse validation + risk checks -> PAPER_APPROVED or NO_TRADE`

Both validators must refer to the same `strategyId`, be fresh, and pass the configured evidence checks. Missing or stale evidence fails closed.

## Run the master locally

```bash
export FUSION_INGEST_TOKEN="put-a-long-random-secret-in-the-host-secret-store"
node fusion/server.mjs
```

Health:

```bash
curl http://127.0.0.1:8787/health
```

Do not commit Binance, Telegram, Condor, Freqtrade, Jesse, or Fusion secrets to GitHub.
