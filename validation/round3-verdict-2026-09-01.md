# Round 3 Strategy Discovery Verdict — 2026-09-01

## Verdict

**NO ROUND 3 CATEGORY PASSED THE RAW 1.50% EDGE GATE.**

No strategy construction, transaction-cost optimization, position sizing, unseen-symbol OOS promotion, or capital simulation is authorized for these definitions because the mandatory Step 2 gate failed first. `VALIDATED_STRATEGY_RELEASE` remains null and live trading remains fail-closed.

## Pre-registered raw gate

For a candidate horizon to survive discovery, all conditions had to hold before costs or strategy rules:

- Mean raw conditional forward return >= 1.50%.
- Median raw forward return > 0.
- Hit rate > 55%.
- Median(MFE) / Median(MAE) >= 2.0.
- Signal known at bar t close; return measurement begins at next-bar open.

## Priority 1 — Liquidity Crash Exhaustion

5-year discovery, 24 liquid USDT discovery symbols, 60 strict events.

| Horizon | Mean gross | Median | Hit rate | Median MFE/MAE | Decision |
|---|---:|---:|---:|---:|---|
| 24H | -0.853% | +0.080% | 50.0% | 0.359 | REJECT |
| 48H | -1.013% | -2.173% | 30.0% | 0.370 | REJECT |
| 72H | -1.953% | -2.191% | 38.3% | 0.340 | REJECT |

Interpretation: the frozen OHLCV panic/deceleration definition identifies crash continuation more often than structural exhaustion. Historical liquidation/order-book data was not fabricated to improve the signal.

## Priority 2 — Extreme Residual Dispersion Reversal

Rolling 24H residual return, beta estimated from prior 720H, residual Z from prior 1440H, previous Z < -3, residual improvement, positive 1H return, volume Z >= 2. 189 events.

| Horizon | Mean gross | Median | Hit rate | Median MFE/MAE | Decision |
|---|---:|---:|---:|---:|---|
| 24H | +0.746% | +1.298% | 57.1% | 1.620 | REJECT |
| 48H | +1.004% | +0.482% | 52.9% | 1.648 | REJECT |
| 72H | +0.890% | +0.687% | 54.0% | 1.346 | REJECT |

Interpretation: this was the strongest Round 3 family, but its unconditional raw recovery magnitude remains below the required 1.50%, and path asymmetry remains below 2.0. Thresholds were not loosened after viewing the result.

## Priority 3 — Market Breadth & Aggregated Lead-Lag

Definition was frozen before results: >=70% above 50H SMA, +15 percentage-point breadth impulse over 24H, BTC/ETH/BNB/SOL median 24H return >=3%, and >=70% of universe positive over 24H. 3,532 market events; asset-level observations exceed 65k.

| Horizon | Mean gross | Median | Hit rate | Median MFE/MAE | Decision |
|---|---:|---:|---:|---:|---|
| 48H | +0.631% | -0.186% | 48.3% | 0.992 | REJECT |
| 72H | +0.466% | -0.391% | 47.4% | 0.990 | REJECT |
| 120H | +0.220% | -0.969% | 45.7% | 0.958 | REJECT |
| 168H | +0.035% | -0.879% | 45.9% | 0.962 | REJECT |

Interpretation: strong breadth/leader impulse does not produce a sufficiently large long-only continuation edge in this definition. The longer hold dilutes neither the weak median nor adverse path symmetry.

## Pipeline consequence

Because Step 2 failed for every candidate, Step 3 freeze is retained as a research record but Steps 4 and 5 are intentionally not run for promotion. Running OOS/cost/capital tests after a failed economic-magnitude gate would waste the untouched holdout and invite post-selection bias.

The unseen-symbol set XLM, HBAR, ICP, ALGO, VET, THETA, RUNE, GRT therefore remains reserved for future genuinely surviving definitions rather than being consumed by failed candidates.

## Production status

- Live trading: OFF.
- Validated strategy release: null.
- No Round 3 strategy approved for paper or live promotion.
