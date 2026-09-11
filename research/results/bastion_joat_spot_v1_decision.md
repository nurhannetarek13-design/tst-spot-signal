# Bastion JOAT Spot V1 — Final Research Decision

## Decision

**REJECT_JOAT_V1**

Bastion Execution Protocol [JOAT] was ported as a Binance Spot, long-only candidate with the original Pine defaults frozen. The test used public Binance Vision 15m Spot klines and did not use exchange API access, account state, leverage, shorting, DCA, or live execution.

## Frozen execution semantics

- Signal on completed 15m bar close; enter at next bar open.
- Long-only Spot.
- Initial stop: signal close minus 1.5 ATR.
- Target: 2R.
- ATR trailing activates after >=1R and ratchets upward.
- Conservative same-bar collision rule: stop wins if both stop and target are touched.
- Weekday/session filters and EOD close retained.
- 10 USDT stake; max 3 simultaneous positions.
- No parameter optimization after results.

## Results

### Discovery — 2025, baseline cost (~0.28% round trip)

- Trades: 3,233
- Win rate: 33.56%
- Profit factor: 0.4632
- Net PnL: -110.46 USDT on 10 USDT fixed stake trades
- Portfolio-equivalent net return on 1,000 USDT reference wallet: -11.046%
- Expectancy: -0.03417 USDT/trade
- Max realized-equity drawdown: 11.28%
- Exit mix: 2,148 stop / 583 target / 502 EOD

### OOS — 2026-01-01 through 2026-09-01, baseline cost

- Trades: 2,009
- Win rate: 31.76%
- Profit factor: 0.4475
- Net PnL: -62.17 USDT
- Portfolio-equivalent net return on 1,000 USDT reference wallet: -6.217%
- Expectancy: -0.03095 USDT/trade
- Max realized-equity drawdown: 6.27%
- Exit mix: 1,223 stop / 419 target / 367 EOD

All 10 tested symbols were negative in OOS: BTC, ETH, BNB, SOL, XRP, ADA, DOGE, LINK, AVAX, DOT.

### OOS stress — 2026, ~0.80% round trip cost

- Trades: 2,009
- Win rate: 18.91%
- Profit factor: 0.1277
- Net PnL: -166.63 USDT
- Portfolio-equivalent net return on 1,000 USDT reference wallet: -16.663%
- Expectancy: -0.08294 USDT/trade
- Max realized-equity drawdown: 16.66%

## Gate

Required:
- OOS trades >= 30
- OOS PF > 1.20
- OOS net positive
- Stress PF > 1.00
- Stress net positive

Observed: failed every profitability gate.

## Interpretation

This is not a marginal miss. The source strategy has no demonstrated long-only Spot edge under the frozen defaults in this test. The failure is broad across symbols and persists out of sample; higher costs make it materially worse.

No threshold tuning, symbol cherry-picking, or optimization is authorized from these same samples. The candidate must remain excluded from Paper and Live.

## Authorization

- Research only: **YES**
- Paper candidate: **NO**
- Live ready: **NO**
- Live trading remains: **OFF**

Canonical workflow run: `34616928256`
Artifact: `bastion-joat-canonical-v1` (ID `10270552722`)
