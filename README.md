# TST Spot Signal

Signal-only Binance Spot scanner for liquid USDT pairs.

- No leverage
- No automatic trading or order placement
- Maximum planned position: 5 USDT
- Maximum planned risk: 0.50 USDT
- Automatic scan endpoint: `/cron/scan`
- Scheduled scan target: every 15 minutes
- Telegram alerts only when a confirmed BUY setup passes all configured checks
- SELL remains a spot exit signal in `/scan` and `/signal/:symbol`; it never opens a short

Runtime Telegram secrets are stored in Vercel Environment Variables and are never committed to GitHub.
