# Ready Strategy Tournament — 2026-09-10

Status: **NO SAFE WINNER**
Live authorization: **FALSE**

Contract used for valid Freqtrade tournament runs:
- Binance Spot research only
- 50 USDT dry-run wallet
- 10 USDT stake
- max 3 open trades
- no DCA / position adjustment disabled
- config hard stop -3%
- 2025 base fee 0.20% per side
- 2026 base fee 0.20% per side
- 2026 stress fee 0.50% per side (~1.00% round-trip)

## Previously rejected

### NFI X8
Rejected after corrected V8 harness. No safe hard-stop variant; live remained disabled.

### BinHV45_Regime_5m
- 2025: 248 trades, PF 0.53, -41.32%, DD 42.93%
- 2026 base: 100 trades, PF 1.10, +2.43%, DD 7.09%
- 2026 stress: 101 trades, PF 0.71, -9.55%, DD 14.03%
Rejected. Lookahead gate also flagged bias.

## Official freqtrade/freqtrade-strategies tournament

### BinHV27 — REJECT
- 2025: 801 trades, PF 0.47, -77.83%, DD 78.14%, worst day -2.664 USDT, worst trade -3.39%
- 2026: 941 trades, PF 0.40, -78.33%, DD 79.06%, worst day -2.975 USDT, worst trade -3.39%
- Stress: 383 trades, PF 0.13, -78.02%, DD 78.31%, worst day -2.373 USDT, worst trade -3.97%

### SwingHighToSky — REJECT
- 2025: 361 trades, PF 0.27, -77.92%, DD 78.43%, worst day -8.915 USDT
- 2026: 376 trades, PF 0.18, -78.23%, DD 78.47%, worst day -11.690 USDT
- Stress: 260 trades, PF 0.08, -77.92%, DD 78.15%, worst day -13.381 USDT

### Bandtastic — REJECT
- 2025: 329 trades, PF 0.29, -78.01%, DD 78.20%
- 2026: 450 trades, PF 0.28, -78.37%, DD 78.79%
- Stress: 284 trades, PF 0.09, -78.51%, DD 78.54%

### BinHV45 — NOT FINANCIALLY JUDGED
Loads successfully but current backtest exits nonzero under the current Freqtrade runtime. Treat as incompatible until explicitly ported; do not infer profitability.

## MikeDiGriz tournament

### BuyOrDie — REJECT
- 2025: 71 trades, PF 0.13, -39.93%, DD 48.06%
- 2026: 52 trades, PF 0.86, -4.47%, DD 38.67%
- Stress: 51 trades, PF 0.41, -22.66%, DD 43.88%

### CCI_BB — REJECT
- 2025: 372 trades, PF 0.45, -78.34%, DD 79.40%
- 2026: 358 trades, PF 0.49, -67.68%, DD 71.53%
- Stress: 281 trades, PF 0.43, -67.21%, DD 68.71%

### RSI_BB — REJECT
- 2025: 249 trades, PF 0.30, -77.93%, DD 79.39%
- 2026: 270 trades, PF 0.25, -77.92%, DD 79.47%
- Stress: 196 trades, PF 0.14, -78.31%, DD 79.18%

### EasyInEasyOut / FisherHull — NOT FINANCIALLY JUDGED
Both load but their current backtests exit nonzero under the current Freqtrade runtime. Do not infer profitability.

## Historical flow survivor cost gate

ETHUSDT, flow_imbalance_quote LOW, z <= -2.0, 15m:
- gross: n=69, mean +0.0412%, median +0.0500%, hit rate 60.87%, PF 1.659 — gross gate passes
- normal cost 0.28% round-trip: mean -0.2388%, median -0.2300%, hit rate 13.04%, PF 0.061 — fails
- stress cost 0.50% round-trip: mean -0.4588%, median -0.4500%, hit rate 2.90%, PF 0.0028 — fails
- productionCandidate=false

## Conclusion

The ready-made public strategy batch produced **zero safe winners** under the user's small-account Spot contract. The hard-stop wrapper was effective on valid runs (worst individual trades clustered around -3.39% base and -3.97% stress), so the dominant failure is stale/negative edge after current-market data and costs, not missing stop enforcement.

Do not wire any candidate to live trading. The fastest defensible path is to stop random legacy-strategy hunting and use the already-built Binance Vision historical feature layer for current-data candidate discovery, then require chronological OOS, unseen-symbol holdout, stress costs, and forward shadow before any live authorization.
