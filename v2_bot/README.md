# TST Spot Bot V2 — Standalone

This is a clean, isolated V2 implementation. It does **not** import or depend on the legacy bot.

## Safety state

- Default mode: `shadow`
- `V2_LIVE_TRADING=false` by default
- Live execution is intentionally hard-locked until the protective Binance Spot exit-order adapter is implemented and tested.
- No withdrawals, futures, leverage, martingale, or dependency on Make/Railway execution logic.

## Current pipeline

`Binance public market data -> V2 scanner -> strategy score -> risk gates -> shadow/paper action -> optional Telegram alert`

### Current signal gates

- USDT market
- 24h quote volume >= 20M USDT
- spread <= 15 bps
- BTC 1h bullish regime
- target trend bullish on 15m + 1h + 4h
- 15m breakout above previous 20-candle high
- relative 15m quote volume >= 1.5x
- taker-buy quote ratio >= 56%
- minimum score >= 90/100

These are baseline deterministic rules for testing, **not evidence of a profitable edge**. They must pass paper/OOS validation before live trading is considered.

## Risk defaults

- Trade size: 10 USDT
- Daily realized loss cap: 2 USDT
- Max simultaneous paper positions: 1
- Paper TP: +0.90%
- Paper SL: -0.62%
- Paper fee model: 0.10% per side by default

All are configurable through environment variables.

## Run

```bash
pip install -r v2_bot/requirements.txt
python -m v2_bot.main --once
```

Continuous scan:

```bash
python -m v2_bot.main
```

Paper mode:

```bash
V2_MODE=paper python -m v2_bot.main
```

## Environment

Copy values from `v2_bot/.env.example` into the deployment environment. The program reads environment variables directly; it does not load `.env` files itself.

Telegram is optional. Set `V2_TELEGRAM_BOT_TOKEN` and `V2_TELEGRAM_CHAT_ID` only for the new V2 bot/channel.

## Live gate

Even if `V2_MODE=live` and `V2_LIVE_TRADING=true` are set, V2 will refuse to place a trade today because protected live execution is not implemented yet. This is deliberate: no unprotected market buy is allowed.
