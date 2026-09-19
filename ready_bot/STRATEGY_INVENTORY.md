# Exact strategy integration inventory — 2026-09-19

**One shared PAPER wallet, no exchange order submission.** Every row distinguishes physical source presence from real execution. Native JS functions are imported and executed unchanged via `ready_bot/native_js_bridge.mjs`; all candidates, including original three, flow through `ready_bot/native_paper.py` and the same Python wallet/risk/position logic in `multi_bot.py`. This is NOT evidence of strategy profitability or execution parity with Binance Live.

## Evaluated for PAPER trades in the unified runner (5)

| ID | Source | Entry / exit | Status |
|---|---|---|---|
| `TREND_BREAKOUT` | `ready_bot/multi_bot.py` | Original closed 4h/1h breakout, 1.5 ATR stop, 2R exit | PAPER; unproven |
| `TREND_PULLBACK` | `ready_bot/multi_bot.py` | Original closed 4h/1h EMA20 reclaim, 1.5 ATR stop, 2R exit | PAPER; unproven |
| `RANGE_REVERSION` | `ready_bot/multi_bot.py` | Original lower-band reclaim, 1.5 ATR stop, its OWN mean target | PAPER; unproven |
| `REGIME_MOMENTUM_PAPER_2R_WRAPPER` | `src/strategies/regime-adaptive-momentum.mjs` | EXACT native JS scoring entry/stop. Original has **no exit**: new and separately named adapter uses 2R target and max 24 completed 1h bars | PAPER experimental VARIANT; source exit parity impossible; unproven |
| `SMALL_CAP_INTRADAY_MOMENTUM_V1` | `src/strategies/small-cap-intraday.mjs` | EXACT native JS scoring entry, native ATR stop/target and maximum 8 completed 15m bars | PAPER; unproven; 5.50 USDT maximum can be below Binance notional |

The JS runner feeds original scorers 1h or 15m closed candles, actual taker-buy quote share from public Binance kline field 10, 24h quote volume, current best bid/ask and rolling relative volume. No substitute for unknown taker flow. Scores never bypass shared risk: maximum 3 open positions, 0.20 USDT risk/trade, 0.60 USDT total modeled open stop risk, 2 USDT daily loss cutoff, real Spot symbol filters, fee/slippage assumptions. Source stake is a hard MAXIMUM; an exchange min-notional rejection does not trigger a stake increase. A 5-minute snapshot cannot guarantee exchange-like intra-run stop fills.

## Freqtrade source code present, **not executable in this runner** (5)

| ID | Source | Explicit reason / next dependency |
|---|---|---|
| `TST_ALLIGATOR_SMC_V2` | `freqtrade/user_data/strategies/AdaptiveRegimeStrategy.py` | 15m causal structure + TA-Lib/Freqtrade native environment and exact exits/protections must be adapted and independently tested. |
| `BASTION_JOAT_SPOT_V1` | `freqtrade/user_data/strategies/BastionJOATSpotV1.py` | Source explicitly says exit parity missing. |
| `BASTION_JOAT_SPOT_V2` | `freqtrade/user_data/strategies/BastionJOATSpotV2.py` | Inherits V1; custom ATR trailing, 2R, NYC session exit. No equivalent exit model in paper engine yet. |
| `NFI_PROTECTED_X7` | `freqtrade/user_data/strategies/NFIProtectedX7.py` | Imports missing `NostalgiaForInfinityX7` parent and always returns `False` to refuse auto-entry. Cannot invent signal. |
| `UNIFIED_CANDIDATE` | `freqtrade/user_data/strategies/UnifiedCandidateStrategy.py` | Manifest says VALIDATION_AND_FORWARD_PAPER_ONLY; OOS/fingerprint mismatch; L2 confirmation cannot be inferred from OHLCV. |

The runner publishes these five source states and blockers in `multi_state.json.source_registry`. They are deliberately excluded from `multi_config.json.strategies`: pretending they run would be incorrect and unsafe. A source file present in GitHub does not imply a working Freqtrade daemon.

## Separate historical archive / indicators

The user's earlier `v2_live_ready_final.zip` is a SEPARATE build, not committed to this repository: 8 `RESEARCH` V2 strategy specifications and 4 permanently `REJECTED` specifications. Not silently activated or rebranded. Similarly, TradingView Pine files such as JOAT, CVD, relative strength, variance ratio, relative volume at time, liquidity swings, Grid/DCA are different sources and often indicators rather than complete strategies. Required source revisions, execution assumptions, causality, rights, dependencies and OOS outcomes must be resolved separately. Previously rejected SAHARA and adaptive grid remain rejected for Live.

**No Live release:** Fail-closed mode `PAPER_ONLY`; no API keys or trade adapter in this runner. Success in CI tests code, not investment performance. Historical/OOS, real forward results, exchange native OCO and separate explicit authorization are required before any Live proposal.
