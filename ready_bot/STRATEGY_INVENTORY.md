# Strategy and indicator inventory — integration gate

**Scope:** Inventory of identifiable sources, not a statement that every Pine script ever shared is in this repository. A file or indicator is not an executable trading strategy. This branch remains PAPER_ONLY and cannot place Binance orders.

## Executable in the new paper router

| ID | Entry logic (independent) | Exit logic | Implemented location | Status |
|---|---|---|---|---|
| TREND_BREAKOUT | EMA50 > EMA200 on closed 4h and 1h; close above previous 20 highs; relative volume >= 1.2 | Entry-derived 1.5 ATR stop and 2R target | `ready_bot/multi_bot.py` | Paper example only; profitability unverified |
| TREND_PULLBACK | Same trend context, independent EMA20 reclaim and previous high condition | Entry-derived 1.5 ATR stop and 2R target | `ready_bot/multi_bot.py` | Paper example only; profitability unverified |
| RANGE_REVERSION | EMA50/EMA200 higher-timeframe compression; lower-band reclaim; RSI < 40 | 1.5 ATR stop and previous 20-close mean target; must preserve its own target in executor | `ready_bot/multi_bot.py` | Paper example only; profitability unverified |

Common features currently calculated: SMA, EMA(20/50/200), ATR(14), RSI(14), rolling volume mean, rolling standard deviation, prior-high breakout. Indicators are **features**, not separately enabled trades.

## Additional strategy source files in this repository (NOT routed into this paper engine)

- `freqtrade/user_data/strategies/AdaptiveRegimeStrategy.py`
- `freqtrade/user_data/strategies/BastionJOATSpotV1.py`
- `freqtrade/user_data/strategies/BastionJOATSpotV2.py`
- `freqtrade/user_data/strategies/NFIProtectedX7.py`
- `freqtrade/user_data/strategies/UnifiedCandidateStrategy.py`
- `src/strategies/regime-adaptive-momentum.mjs`
- `src/strategies/small-cap-intraday.mjs`

These are distinct source implementations; do not claim they execute or profit merely because the files exist. Each requires a versioned adapter mapping native entry/exit semantics into the shared candidate contract, closed-candle verification, conflict/deduplication tests, fee/slippage testing and explicit paper enablement. Preserve prior failed verdicts; do not silently re-label a failed candidate as approved.

## Previously supplied TradingView names / scripts to reconcile

- Pine strategy candidates: `Bastion Execution Protocol [JOAT]` (Spot conversion sources exist in Freqtrade, but not imported into this router); `My daytrade study` (previously benchmark/reject); `Template Trailing Strategy (Backtester)` (generic execution template, needs entry strategy); Donchian, Supertrend, Volatility Breakout and Chandelier Exit ideas (source/version and executable parity to be verified); Grid/DCA candidates (different order-management semantics and may require more capital; do NOT silently enable).
- Indicator/feature candidates: `Multiple Moving Averages System (MMAS)`, DepthHouse `RVOL` (volume / SMA(volume,26)), `Comparative Relative Strength` vs BTC, `Cumulative Volume Delta` (OHLCV proxies are not actual trade delta), `Variance Ratio Regime Classifier`, `Liquidity Swings [LuxAlgo]`, RSI MTF, Kalman Trend Levels, Adaptive Trend Flow, SMC Lite, ATR, ADX and MACD. Some Pine sources are license- or data-dependent; full source/permissions and causal calculation must be checked before implementation. An indicator alone must never acquire autonomous buy/sell semantics by inference.
- Previous exclusions / failures stay excluded from automatic trading: SAHARA negative expectancy, adaptive grid failed OOS; linear-regression candles and momentum ZigZag unsuitable as standalone triggers in earlier reviews. Maintain audit trail rather than hiding failures.

## Mandatory integration contract for the next adapter

Each strategy must declare `id`, source/commit or source link and license, trading market/timeframe, exact entry predicate, exact exit/stop/target and fill semantics, no-lookahead checks, fixed parameters, required indicators and data, order restrictions, independent test evidence, and `state` (`CATALOG_ONLY`, `PAPER`, `DISABLED`, `LIVE_ELIGIBLE`). Default `CATALOG_ONLY`. A strategy failing validation does not execute, but its result remains visible in the inventory. No performance guarantees are inferred from TradingView popularity or historical screenshots.
