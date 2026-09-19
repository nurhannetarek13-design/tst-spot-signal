# One runnable multi-strategy PAPER bot

This is a replacement for the original `ready_bot/bot.py` scheduler, **not** three separately funded bots. A run reads closed 1h/4h Binance candles for BTC/ETH/SOL Spot, evaluates three frozen mechanical strategies independently, arbitrates collisions deterministically using `multi_config.json` priority, checks one shared wallet and exchange filters, and simulates positions in `multi_state.json`.

## Exactly what executes here

- `TREND_BREAKOUT`: higher- and execution-timeframe trend, previous 20-bar high breakout, relative-volume condition; 1.5 ATR stop and 2R target.
- `TREND_PULLBACK`: same trend context, independently defined EMA20 reclaim and previous-high confirmation; 1.5 ATR stop and 2R target.
- `RANGE_REVERSION`: compressed higher-timeframe EMA ratio, lower-band reclaim and RSI condition; 1.5 ATR stop **and its own mean-reversion target**. The router does not overwrite this target with 2R.

All signal functions use completed candles. Every paper position and closed trade carries the originating `strategy` ID and exit reason. Signals on the same symbol are **not** combined as votes; at most one position per symbol is opened and repeated signals on the same bar are suppressed. The configured list determines deterministic priority, not claimed profitability.

**Other strategies and indicators are inventoried, not silently enabled.** See [`STRATEGY_INVENTORY.md`](STRATEGY_INVENTORY.md) for the exact existing paper/legacy/TradingView status and the adapter contract. Freqtrade and JS strategies are not automatically compatible with this Python paper router just because their source files exist.

## Risk and execution assumptions

Default simulated wallet: 20 USDT; target stake: 7 USDT; max 3 positions total; max 0.20 USDT modeled loss per position at stop; max **0.60 USDT aggregate modeled loss at all open stops**, and daily realized loss plus remaining modeled stop risk capped at 2 USDT. Fees are assumed 0.1% each side, slippage 0.05% each side; these are test assumptions, not verified personal Binance rates. Read public Binance Spot `NOTIONAL`/`MIN_NOTIONAL` and `LOT_SIZE` filters every run. A minimum cannot be circumvented by silently increasing stake.

Paper stop exits account for an adverse quote gap, and take-profit fills are capped at the strategy's target in the simulation. Exchange `ticker/price` is only a snapshot, not executable bid/ask, and a simulated stop checked every run is **not** an exchange-native protective stop. A market-data failure for an existing position halts new entries until the position can be priced again. No Binance API keys, production executor or real orders are used.

Run: `python ready_bot/multi_bot.py`; tests: `python -m unittest discover -s tests -p 'test_multi_bot*.py' -v`. `.github/workflows/multi-paper-ci.yml` runs contract tests on the feature branch; after reviewed merge, `.github/workflows/ready-paper-bot.yml` runs the simulation on main every five minutes and saves paper state. The original one-strategy bot remains for reference.

**Status:** Paper-only code in a draft PR; no new main deployment, real-money order or profit claim is implied by successful unit tests. A GitHub Actions `success` can coexist with zero signals or zero fills. Review `signals`, `blocked`, `positions`, `closed_trades`, fee-adjusted PnL and actual forward observations. Independent execution parity and native stop/order checks are required before any separately authorized live launch.
