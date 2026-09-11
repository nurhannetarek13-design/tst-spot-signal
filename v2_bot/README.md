# TST Spot Bot V2 — Standalone

This is a clean, isolated V2 implementation. It does **not** import or depend on the legacy bot.

## Safety state

- Default mode: `shadow`
- `V2_LIVE_TRADING=false` by default
- Live execution is intentionally hard-locked until the protective Binance Spot exit-order adapter is implemented and tested.
- No withdrawals, futures, leverage, martingale, or dependency on Make/Railway execution logic.
- Strategy decisions use **closed Binance candles only**; the currently-forming kline is ignored to avoid repainting.
- Telegram failures are isolated from the scanner and cannot stop a scan cycle.
- Binance public market data automatically falls back to `data-api.binance.vision` if the primary public endpoint is unavailable or region-blocked; HTTP 429 is not bypassed.
- Stablecoin/fiat base pairs such as USDC/USDT, FDUSD/USDT, and EUR/USDT are excluded from the candidate universe.
- SHADOW alerts are deduplicated per symbol and closed 15m candle, so the same signal is not emitted every scan minute.
- Paper positions cannot silently overwrite an existing position, and a symbol closed at TP/SL cannot immediately re-enter in the same scan cycle.
- A continuous worker retries after transient public-market-data HTTP/network failures; one-shot smoke runs still fail loudly.

## Current pipeline

`Binance public market data -> V2 scanner -> strategy score -> mandatory entry gates -> risk gates -> shadow/paper action -> optional Telegram alert`

### Current signal gates

All of these must pass for a candidate to be eligible:

- USDT market
- 24h quote volume >= 20M USDT
- spread <= 15 bps
- BTC 1h bullish regime
- target trend bullish on 15m + 1h + 4h
- closed 15m candle breaks above the previous 20 closed-candle highs
- relative closed-15m quote volume >= 1.5x
- closed-15m taker-buy quote ratio >= 56%
- score >= 90/100

The score is retained for ranking and diagnostics, but it cannot compensate for a failed mandatory gate. For example, a 90/100 candidate with a failed 4h trend is **not eligible**.

These are baseline deterministic rules for testing, **not evidence of a profitable edge**. They must pass paper/OOS validation before live trading is considered.

## Risk defaults

- Trade size: 10 USDT
- Daily realized loss cap: 2 USDT
- Max simultaneous paper positions: 1
- Paper TP: +0.90%
- Paper SL: -0.62%
- Paper fee model: 0.10% per side by default

All are configurable through environment variables. Invalid safety-sensitive values fail validation instead of silently running.

Paper PnL is recorded net of modeled entry and exit fees. State is persisted in SQLite; a persistent deployment must therefore use persistent storage rather than an ephemeral CI filesystem.

## Verification

V2 has three independent checks on its isolated branch:

- `V2 CI`: unit/lifecycle tests, Python compilation, container build, and fail-closed container-default assertions.
- `V2 Shadow Smoke`: a read-only end-to-end scan against live public Binance market data with `V2_LIVE_TRADING=false`.
- repository `Safety gates`: existing fail-closed checks remain green.

The smoke workflow does not use Binance credentials and cannot place orders.

## Run

```bash
pip install -r v2_bot/requirements.txt
python -m v2_bot.main --once
```

Continuous SHADOW scan:

```bash
python -m v2_bot.main
```

Paper mode:

```bash
V2_MODE=paper python -m v2_bot.main
```

### Container worker

Build from the repository root:

```bash
docker build -f v2_bot/Dockerfile -t tst-v2 .
```

The image defaults to `V2_MODE=shadow`, `V2_LIVE_TRADING=false`, and `V2_STATE_DB=/data/v2_state.sqlite3`. For a persistent SHADOW/Paper worker, mount durable storage at `/data`. Do not deploy Paper with ephemeral storage because open positions, emitted-signal dedupe state, and realized PnL would be lost after a restart.

## Environment

Copy values from `v2_bot/.env.example` into the deployment environment. The program reads environment variables directly; it does not load `.env` files itself.

Telegram is optional. Set `V2_TELEGRAM_BOT_TOKEN` and `V2_TELEGRAM_CHAT_ID` only for the new V2 bot/channel.

## Live gate

Even if `V2_MODE=live` and `V2_LIVE_TRADING=true` are set, V2 will refuse to place a trade today because protected live execution is not implemented yet. This is deliberate: no unprotected market buy is allowed.
