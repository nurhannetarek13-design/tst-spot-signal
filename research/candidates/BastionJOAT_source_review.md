# Bastion Execution Protocol [JOAT] — source intake review

Status: RESEARCH_ONLY / NOT_LIVE_READY
Source supplied by user: Pine Script v6, MPL-2.0, © officialjackofalltrades.

## What is actually in the source
- Regime: VWAP normalized slope + SMA20/50/200 + Bollinger squeeze.
- Structure: confirmed pivots / swing breaks + displacement candles.
- Momentum: RSI + SMI.
- Order-flow proxy: candle/volume-derived CVD (not exchange trade-flow CVD).
- Pattern confirmation: engulfing / pin bars with volume threshold.
- Session/day filters.
- Risk: ATR stop, configurable reward:risk, optional ATR trailing, max trades/day, regime-adaptive sizing.
- Both LONG and SHORT execution exist in the original Pine.

## Required adaptation for our bot
1. Binance SPOT only: disable/remove all short entries and short exits.
2. Do not port Pine position sizing. Our execution layer owns stake/risk constraints.
3. Preserve entry logic exactly for the first parity candidate; no parameter optimization.
4. Treat candle-derived CVD explicitly as a proxy, not true taker/order-book flow.
5. Use completed candles only; preserve confirmed-pivot semantics and the pivot confirmation delay.
6. Reproduce ATR SL/TP/trailing semantics exactly before any OOS comparison.
7. No DCA / martingale / pyramiding.
8. Candidate cannot authorize live trading. It must pass parity + OOS + stress first.

## Initial technical verdict
This is a real, complete strategy source rather than a black-box signal. It is suitable as an external candidate for translation/testing. It is NOT evidence of profitability by itself. The Pine defaults include short trading and dynamic equity/risk sizing, which conflict with our Spot-only execution contract and therefore must not be copied into production unchanged.

Decision: ACCEPT_SOURCE_FOR_PORTING_ONLY.
