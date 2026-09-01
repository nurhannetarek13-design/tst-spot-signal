# TST Spot Signal

Paper-only, multi-factor Binance Spot scanner for liquid USDT pairs.

## Current operating mode

- `PAPER_ONLY`: no Binance order is created and no funds are used.
- No Futures, leverage, perpetuals, shorts, bStocks, tokenized stocks, leveraged tokens, or stablecoin-to-stablecoin trades.
- Surface-scan every eligible Binance Spot USDT pair every minute using Binance's bulk ticker and book data.
- Deep-scan seven pairs per run with a rotating cursor so the complete liquid universe is covered without exceeding Cloudflare's free request budget.
- Maximum paper position: 7 USDT.
- Maximum modeled risk: 0.20 USDT per trade.
- Daily paper loss stop: 0.50 USDT.
- Maximum open paper positions: one.

## Required confirmation

A Telegram paper signal needs a score of at least 90/100 plus all hard safety checks:

- BTC 1h/4h market regime allows Spot longs.
- Pair has at least 90 completed daily candles and 20M USDT 24h volume.
- Symbol trend agrees on 1h and 4h.
- Completed breakout/retest or confirmed trend pullback on 15m.
- Relative volume, taker-buy pressure, RSI, spread, depth imbalance, and nearby sell-wall checks.
- Net reward/risk of at least 3.0 after estimated entry and exit fees.
- Binance OCO support and both protected exit legs remain above the pair's live minimum notional.
- Revalidation after the Telegram paper button is pressed.

Scanner status: `/scanner-status` on the Cloudflare Worker.

Runtime secrets are stored in deployment environment variables and are never committed to GitHub.
