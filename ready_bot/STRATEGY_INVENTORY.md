# Trading strategy and indicator integration inventory

**This file distinguishes inventory from execution.** It does not assert that every strategy/indicator ever shared is accessible, ported, profitable, or live-authorized. This branch cannot place real Binance orders. A Pine `indicator()` is never silently promoted to a trading strategy.

## Independent PAPER strategies fully implemented in `ready_bot/multi_bot.py`

| Strategy ID | Independent entry | Independent exit | Status |
|---|---|---|---|
| `TREND_BREAKOUT` | Completed 4h/1h EMA50 > EMA200, previous 20-bar high break, volume >= 1.2x | 1.5 ATR stop; 2R target | Paper starter; unproven |
| `TREND_PULLBACK` | Completed 4h/1h trend, EMA20 reclaim and prior-candle high confirmation | 1.5 ATR stop; 2R target | Paper starter; unproven |
| `RANGE_REVERSION` | Completed 4h EMA compression, lower-band reclaim, RSI < 40 | 1.5 ATR stop; *its own prior-window mean target*, not generic 2R | Paper starter; unproven |

The router uses EMA(20/50/200), ATR(14), RSI(14), rolling mean/std, relative volume and prior-high features. All indicator outputs are internal measurements. Separate code/test status is NOT a claim of historical edge.

## Identified repository implementations, present but NOT connected to this router

| File | Logic / missing requirement | Status |
|---|---|---|
| `freqtrade/user_data/strategies/AdaptiveRegimeStrategy.py` | Williams Alligator + MACD + SAR + SMC structure; needs native 15m Freqtrade signal/exit and 4h validation adapter | Catalog only |
| `freqtrade/user_data/strategies/BastionJOATSpotV1.py` | JOAT 15m port; exit parity explicitly incomplete | Catalog only |
| `freqtrade/user_data/strategies/BastionJOATSpotV2.py` | JOAT ATR stop, 2R target, ATR trail and NY end-of-day exit; needs temporal/execution parity | Catalog only |
| `freqtrade/user_data/strategies/NFIProtectedX7.py` | Needs Freqtrade dependency and independent parity/risk checks | Catalog only |
| `freqtrade/user_data/strategies/UnifiedCandidateStrategy.py` | Prior cross-engine fingerprint and negative-expectancy issues need resolution | Catalog only |
| `src/strategies/regime-adaptive-momentum.mjs` | Existing JS scorer requires bid/ask, 24h liquidity, 15m or selected candles, actual taker share and relative volume; no independent exit/TP contract yet | Catalog only |
| `src/strategies/small-cap-intraday.mjs` | JS 15m strategy needs separate small-cap universe (BTC/ETH/SOL excluded), bid/ask and flow; 8-bar time exit must be implemented; 5.50 USDT desired stake can fail current Binance minimum | Catalog only |

Do NOT rename a source module as a running strategy, invent absent market data, or change the universe and stake secretly. A strategy is not integrated unless it can produce its own reproducible entry, independent complete exits, risk reservation and paper trade record.

## Recovered prior V2 bundle (separate archive, NOT deployed in this repository)

The user's earlier `v2_live_ready_final.zip` includes a `v2_bot/strategy_pool.py` with 8 research-only specifications: `trend_momentum`, `compression_breakout`, `mean_reversion_extreme`, `volume_anomaly_reversal`, `btc_alt_leadlag`, `relative_strength_rotation`, `crash_exhaustion_reversal`, `range_reversion`. It separately records four rejected historical variants: `strict_current`, `breakout_continuation`, `volatility_expansion`, `htf_pullback_reclaim` (180-day OOS PF below 1). The archive also includes a comprehensive independent live-capable engine, but its report states deployment/account/evidence gates must pass. **Its 8 strategies are RESEARCH, not promoted to PAPER or ACTIVE**, and the code is not currently in `main`. Do not conflate this separate build with the present Python paper router.

## Previously shared TradingView scripts and indicators

Actual named Pine sources located in earlier uploads: `Variance Ratio Regime Classifier [PickMyTrade]` (Lo–MacKinlay multi-horizon diagnostic, not entry rules), `Relative Strength Rotation Map [AGPro Series]` (benchmark leadership / rotation), `Relative Volume at Time` (same-time-of-day volume comparison, not ordinary rolling RVOL), `Order Flow Microstructure Engine` (may use proprietary footprint data and market presets), `Bastion Execution Protocol [JOAT]` (MPL-2.0 strategy), and `Template Trailing Strategy (Backtester)` (CC BY-NC-SA 4.0, generic backtesting template, cannot supply an entry alone).

Other named supplied concepts/features: `Comparative Relative Strength` versus BTC, `Cumulative Volume Delta` (OHLCV estimate != native taker/trade delta), `Multiple Moving Averages System (MMAS)`, DepthHouse `RVOL`, `Liquidity Swings [LuxAlgo]`, RSI MTF, Kalman Trend Levels, Adaptive Trend Flow, SMC Lite, ATR, ADX, MACD, Donchian, Supertrend, Chandelier Exit, Grid and DCA. Some require exact source or subscription/data access. A license must be respected; non-commercial/restricted code must not be pasted blindly into a commercial or public GitHub repository.

## Rejected / unapproved results retained

SAHARA (negative expectancy), adaptive grid failed OOS, JOAT exit parity incomplete and original Unified Candidate fingerprint mismatches cannot be flipped to profitable by relabeling. Existing 4 rejected V2 strategies stay excluded. A successful CI job only verifies program behavior; it does not prove profitable trading.

## Mandatory adapter contract

For each new entry, record: `strategy_id`, license + immutable code revision, Spot symbol universe/timeframe, exact causal indicators/data sources, signal-bar timestamp and no-lookahead proof, precise independent entry, explicit stop/target/trailing/time exit, fee/slippage/fill rules, exposure correlation and minimum-notional eligibility, validated deterministic tests, and one of `CATALOG_ONLY`, `RESEARCH`, `PAPER`, `DISABLED`, `LIVE_ELIGIBLE`. Default `CATALOG_ONLY`. The shared router must not require unrelated strategies to agree, and portfolio risk overrides every candidate. No guarantee of profit is possible.
