# V1 versus V2: controlled comparison (read-only)

Status: `BLOCKED_ON_MATCHED_REPLAY_DATA`. This is not a strategy approval and cannot turn on live trading.

## Version identity

- V1 candidate: `main` fast entry/scanner, `freqtrade/fast_entry_engine.py`, plus its complete upstream scanner / confirmation / exit route. Freeze the exact commit when replaying.
- V2 candidate: `bot-v2-standalone` runtime/strategy pool; freeze the exact commit and one explicitly named strategy or the frozen tournament routing policy. Do not mix legacy V2 strategies with newly registered research strategies.
- The existing `validation/backtest-365d.json` is an historical test of an unspecified runner and **is not a verified apples-to-apples V1 result**.
- A green CI job or successful webhook invocation indicates technical operation, not realized net profit.

## Freeze before evaluation

- Identical historical symbol eligibility rules, including historical listings/delistings, not today's surviving-symbol universe.
- Same start/end UTC, minimum 365-day evaluation, 70/30 chronological discovery/OOS split; independent untouched final holdout when enough history exists.
- Historical closed Binance Spot 1m OHLCV + taker data, and matching 15m/1h/4h series and BTC context for both. Replay actual timestamped spreads/bid/ask/order-book snapshots, where either release gates on those signals. **No present-day bookTicker substituted for historical L2.**
- Identical fee on each side, slippage on each side, starting capital, position cap, fill and rounding rules; next-bar fills and STOP_FIRST when both barriers hit in one candle.
- Exact strategy code pinned to SHAs, causal feature computation, all rejected signals and missing-data diagnostics retained. No threshold retuning on OOS.
- Separate comparable realized backtest trades from V1 manual Binance trades and V2 Shadow/Paper outcomes; do not combine unlike populations.
- Never set live flags, start execution workers, invoke buy/sell endpoints, or reactivate Make scenarios as part of this benchmark.

## Read-only comparison gate

Use `python validation/v1_v2_compare.py v1_manifest.json v1_closed_trades.csv v2_manifest.json v2_closed_trades.csv`.

Each JSON manifest must include `data_hash`, `start_utc`, `end_utc`, `oos_start_utc`, `symbols`, `fee_each_side`, `slippage_each_side`, `starting_capital_usdt`, `strategy_commit`, and boolean confirmations `closed_candle_only`, `next_bar_execution`, `stop_first`, `historical_l2_complete`, `paper_only`, `replay_complete`.

Each trade CSV must have `symbol,entry_time_utc,exit_time_utc,entry_price,exit_price,quote_size_usdt`. Timestamp values must be UTC ISO 8601. The gate recalculates net PnL after fees/slippage and reports trades, win rate, PF, expectancy, net PnL, and closed-equity drawdown for full and OOS periods. It returns `NOT_COMPARABLE` (nonzero exit) for mismatched inputs, unavailable L2, incomplete replay, or invalid trades. Zero OOS trades returns `INSUFFICIENT_TRADES` (nonzero exit).

Run self-tests without exchange access: `python -m unittest validation/test_v1_v2_compare.py -v`.

**Important:** This script compares completed independent replay outputs; it does not manufacture the replay datasets or replay the original strategies. The missing matched V1/V2 replay outputs and historical order-book evidence prevent a valid winner claim. If the full snapshots cannot be obtained, report that limitation rather than weakening this gate and calling a reduced strategy V1 or V2.
