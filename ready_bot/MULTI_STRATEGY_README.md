# One unified multi-strategy PAPER bot

**Canonical command:** `python ready_bot/native_paper.py`. The existing `ready-paper-bot.yml` runs that exact command on `main` every 5 minutes after tests. This is **simulation only**: no Binance private API key, exchange order request, margin, futures or Live order.

## What is truly connected

`ready_bot/native_paper.py` evaluates the existing three mechanical strategies from `multi_bot.py` plus TWO original JS strategy scorers invoked by Node via `native_js_bridge.mjs` (without rewriting their entry logic):

- `TREND_BREAKOUT` — 1h / 4h trend breakout, its own 1.5 ATR stop and 2R target.
- `TREND_PULLBACK` — independent EMA20 reclaim, 1.5 ATR stop and 2R target.
- `RANGE_REVERSION` — lower-band reclaim and independently preserved prior-mean target.
- `REGIME_MOMENTUM_PAPER_2R_WRAPPER` — original `regime-adaptive-momentum.mjs` scorer supplies entry and ATR stop; its original source contains NO target or exit, so this **separate experimental paper adapter** adds 2R target and 24 completed 1h bar cap. NOT source-exit parity.
- `SMALL_CAP_INTRADAY_MOMENTUM_V1` — original `small-cap-intraday.mjs` scorer supplies entry/ATR stop/target, position notional and 8 completed 15m bar holding cap.

All five share ONE 20 USDT modeled wallet, up to 7 USDT global stake, source notional smaller when required, up to 3 positions, 0.20 USDT modeled risk/trade, 0.60 USDT modeled aggregate open-stop risk, 2 USDT daily loss threshold, Spot symbol min-notional and lot-size gates, modeled 0.1% fee per side and 0.05% slippage per side. A small-cap 5.50 USDT source cap is NOT increased to dodge Binance filters. Symbols are an explicit Spot USDT list in `multi_config.json`; the JS source applies its own liquidity, flow and BTC filters. It does not guarantee a certain number of trades.

`source_registry` inside `multi_state.json` explicitly lists the FIVE additional Freqtrade modules and why they CANNOT be simulated in this runner yet: absent TA-Lib/Freqtrade dependency + exit parity, JOAT V1 incomplete exits, NFI missing parent strategy and deliberate auto-entry veto, and the unapproved Unified Candidate fingerprint/OOS issues. An indicator or a strategy source is not executable just because a file exists. Full detail: [`STRATEGY_INVENTORY.md`](STRATEGY_INVENTORY.md).

## Run and verification

`python -m unittest discover -s tests -p 'test_multi_bot*.py' -v` and `python -m unittest discover -s tests -p 'test_native_paper.py' -v`; `node --check ready_bot/native_js_bridge.mjs`. `multi-paper-ci.yml` checks source adapters on PR/push. Both PR tests and the final production-branch first-run check should pass before claiming successful deployment. A passing test is NOT evidence of profitability. The simulated stop uses a periodic ticker snapshot, not exchange-native stops/OCO, and can miss intraperiod price extremes. The repository state is paper performance, not a verified trading statement.

**Never enable Live from this workflow.** Retain rejected strategy history, validate independent OOS after costs and execution parity, and require new explicit user authorization and separate exchange-side protective-order review for any future real-money release.
