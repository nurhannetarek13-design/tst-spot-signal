# Frozen 4h Spot strategies — evidence and release decision (2026-09-17)

**Decision: NO STRATEGY APPROVED for real Binance trading or promotion from this research.** Existing V2 SHADOW monitor remains unchanged. No Binance account keys, orders, Railway deployment, Make changes or automatic paper activation were added in this branch.

## Reproducibility

- Successful CI research: https://github.com/nurhannetarek13-design/tst-spot-signal/actions/runs/35262260036
- Full per-trade ledger and all windows (GitHub artifact, expires after 30 days): https://github.com/nurhannetarek13-design/tst-spot-signal/actions/runs/35262260036/artifacts/10515790285
- `python -m unittest v2_bot.tests.test_frozen_trend_lab -v`: four PASS, including signal-bar isolation, channel lookback, cost stress, stop gap handling.
- `python -m v2_bot.frozen_trend_lab` compares three frozen variants `SMA200_STATE`, `DONCHIAN55_SMA200`, `DUAL_EMA50_200` on Binance public 4h Spot BTCUSDT, ETHUSDT, SOLUSDT, from Oct 2020 warmup. Chronological discovery 2021–2023, validation 2024, temporal holdout Jan 2025–Sep 17 2026. Candles checked for continuity. No parameter search performed after reading holdout results.
- Initial investment simulated USDT10 per position, no leverage. Binance fee assumed 0.10% on entry AND exit; slippage assumed 5bp each side base and 15bp each side stressed. Signals on finished bar, fill next bar open, fixed 3x ATR trailing exit / gap open conservative stop. Each symbol tested separately, so adding individual symbol PnLs is NOT a valid one-position portfolio simulation.
- Research gate frozen in script: for each strategy and EACH symbol BOTH 2024 validation and Jan2025+ holdout stress require at least 8 trades, PF >= 1.2 and positive net. It never calls a live trade API and always sets `live_approved=false`.

## Stress-cost result: latest untouched temporal holdout 2025-01-01 through 2026-09-17

All amounts simulated for *individual hypothetical USDT10 positions*, not account profits. PF is gross gains / gross losses.

| Fixed rule | BTC: trades / net USDT / PF | ETH: trades / net USDT / PF | SOL: trades / net USDT / PF | Research gate |
|---|---:|---:|---:|---|
| SMA200_STATE | 96 / -3.508 / 0.714 | 87 / +3.823 / 1.245 | 92 / -2.273 / 0.879 | FAIL |
| DONCHIAN55_SMA200 | 35 / -3.191 / 0.508 | 25 / +3.531 / 1.559 | 30 / -3.086 / 0.672 | FAIL |
| DUAL_EMA50_200 | 69 / -2.126 / 0.772 | 61 / +6.376 / 1.599 | 64 / -2.078 / 0.853 | FAIL |

**Why not pick ETH retroactively?** In the independently frozen 2024 validation window at stress costs, ETH `SMA200_STATE` lost -2.164 USDT (PF 0.832); ETH `DUAL_EMA50_200` lost -3.471 (PF 0.687); ETH `DONCHIAN55_SMA200` was +0.831 (PF 1.185, below 1.2), and its discovery period was -1.347 (PF .925). Selecting ETH after seeing holdout results would be post-hoc symbol picking, not untouched validation.

**Separate, older baseline:** `v2_bot/backtest_4h_sma200.py` fetched 16,895 4h BTC Spot bars from 2019, 0.1% fee each side, 5bp slippage each side. Its 70/30 chronological OOS produced 68 closed, +1.330 USDT PF 1.164 but drawdown 3.039 USDT, so its own promotion gate was false. This baseline uses different entry/exit and boundary semantics from the new three-variant lab and cannot be represented as a direct strategy-v-strategy winner.

## Limitations / remaining gates before a release

Historical 4h OHLCV contains no archival L1 bid-ask spread, queue priority or intrabar stop/target sequence. Return calculations include assumed slippage, NOT measured Binance live fills. Realized-trade drawdown omits intra-position floating drawdown. Boundary-mark exits at the end of each research period assume a closing price (not a guaranteed available next-bar fill). Current survivor symbols BTC/ETH/SOL are not an exhaustive delisted-aware universe. Daily realized-stop guard does not enforce a guarantee against stop gaps. Fixed rules still require new forward shadow signals with credible evidence, cost and latency checks, portfolio-level exposure/execution parity, exchange MIN_NOTIONAL/filters, OCO stop verification, persistent storage, recovery and explicit separate approval before *any* small live deployment. Zero guaranteed returns; do not lower gates after these results.

**Operational disposition:** do not patch the running V2 router to deploy one of the failed rules; keep 15m GH Actions V2 monitor in SHADOW without credentials. This branch improves the evidence/verification framework, not strategy profitability. A passing CI means that research completed and negative outcomes were honestly captured — it does NOT mean strategy qualified for paper or live.
