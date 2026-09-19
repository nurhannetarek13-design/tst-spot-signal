# One runnable multi-strategy PAPER bot

This is a replacement for the original `ready_bot/bot.py` scheduler, not three separately funded bots. One run reads **closed** 1h/4h Binance candles, evaluates three frozen mechanical signal functions on BTC/ETH/SOL Spot, arbitrates collisions by the order in `multi_config.json`, checks shared capital/risk/exchange filters, and simulates positions in `multi_state.json`.

Strategies (starting examples, **not proven profitable**): `TREND_BREAKOUT` (trend plus prior 20-bar high and volume), `TREND_PULLBACK` (trend plus EMA20 reclaim), `RANGE_REVERSION` (higher-timeframe EMA compression plus lower-band reclaim). Each logged signal and closed simulated trade carries its own strategy ID. New independent signals can be added to `STRATEGIES` without changing risk/accounting. A signal never needs every other strategy's indicators to agree.

The default paper wallet is 20 USDT; trade target is 7 USDT; total open positions at most 3; daily realized loss cap 2 USDT; per-position modeled risk at most 0.20 USDT; model round-trip taker fees 0.1% per side and slippage 0.05% per side. **These fee/slippage settings are assumptions, not verified account rates.** Paper orders are rejected when current Binance Spot `NOTIONAL`/`MIN_NOTIONAL` and `LOT_SIZE` filters make the requested size infeasible. Do not increase the stake automatically to get around a minimum.

Run: `python ready_bot/multi_bot.py`. No Binance key is needed because only public market data is read. The workflow `.github/workflows/ready-paper-bot.yml` compiles and tests, runs this program every five minutes and saves its paper state. It replaces the old one-strategy paper workflow after merging this branch; the original `ready_bot/bot.py` remains untouched for reference.

**No live orders are implemented**. A simulated stop is checked every run against a snapshot quote, not an exchange-native stop; it does not protect real capital between checks. The paper record is a smoke/forward-test signal log, not proof of profitability. A GitHub Actions `success` means the program ran, **not** that it opened a position or has an edge. Review `signals`, `blocked`, `positions`, `closed_trades`, and fee-adjusted PnL in `multi_state.json`.

Do not merge this into any production live executor or enable real-money trading based solely on CI passing. For real funds, verify execution parity, historical/forward metrics, protection at exchange, and current fee/min-notional constraints separately.
