# TST Spot Signal

Fail-closed, multi-factor Binance Spot scanner and live executor for liquid USDT pairs.

## Release safety status

Real-money execution is fail-closed. There is currently **no validated strategy release**.
The 365-day Binance OHLCV audit in `validation/backtest-365d.json` failed the
pre-registered release gates (negative out-of-sample expectancy and Profit
Factor below 1.2), so environment variables alone cannot enable live orders.

```bash
npm ci
npm test
BACKTEST_DAYS=365 npm run backtest
```

Promotion requires at least 200 trades, positive out-of-sample expectancy,
out-of-sample Profit Factor above 1.2, acceptable walk-forward folds, then
shadow and tiny-live validation. A passing preflight proves API signing only;
it does not prove a trading edge.

## Current operating mode

- `CONFIRM_BEFORE_BUY`: a qualified setup sends a Telegram BUY button; no Spot order is created before the user presses it.
- No Futures, leverage, perpetuals, shorts, bStocks, tokenized stocks, leveraged tokens, or stablecoin-to-stablecoin trades.
- Surface-scan every eligible Binance Spot USDT pair every minute using Binance's bulk ticker and book data.
- Deep-scan seven pairs per run with a rotating cursor so the complete liquid universe is covered without exceeding Cloudflare's free request budget.
- Maximum live position: 7 USDT.
- Maximum live risk: 0.20 USDT per trade.
- Daily realized loss stop: 0.50 USDT.
- Maximum open positions: one; maximum six entries per day.

## Required confirmation

A live order needs a score of at least 90/100 plus all hard safety checks:

- BTC 1h/4h market regime allows Spot longs.
- Pair has at least 90 completed daily candles and 20M USDT 24h volume.
- Symbol trend agrees on 1h and 4h.
- Completed breakout/retest or confirmed trend pullback on 15m.
- Relative volume, taker-buy pressure, RSI, spread, depth imbalance, and nearby sell-wall checks.
- Net reward/risk of at least 3.0 after estimated entry and exit fees.
- Binance OCO support and both protected exit legs remain above the pair's live minimum notional.
- Revalidation immediately before the executor places the order.

Scanner status: `/scanner-status` on the Cloudflare Worker.

Runtime secrets are stored in deployment environment variables and are never committed to GitHub.
