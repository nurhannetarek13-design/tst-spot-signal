# TST Fusion Spot Bot

The bot is organized as one master Spot bot with three specialist engines:

- **Hummingbot V2 + Condor** — market-data / order-book layer and deterministic execution path.
- **Freqtrade** — primary strategy and backtest validator.
- **Jesse** — independent validator.
- **Fusion Master** — final decision and risk gate.

## Active implemented strategy

`TST_ALLIGATOR_TMV_V1`

The active paper strategy combines the useful parts taken from the two trading sources we reviewed:

1. **Trend + Momentum + Volume discipline**
2. **Williams Alligator + MACD + Parabolic SAR confirmation**
3. **ATR-based protective levels**
4. Existing Fusion confirmations: BTC regime, L2 bid share, taker flow, relative quote volume, spread and liquidity.

The Williams Alligator is implemented causally using SMMA 13/8/5. The classic forward plotting offsets are deliberately not used in the automated signal because they would create look-ahead bias.

Core 15m long entry requires:

```text
Alligator bullish and widening
+ MACD above signal / positive histogram
+ PSAR below price
+ relative volume >= 1.20x prior 20-bar mean
+ close above EMA200
+ BTC regime / L2 / flow / liquidity gates
```

Execution-side protection uses ATR(14): stop near `2 x ATR` and target near `2R`, bounded by the shared account risk policy.

## Current safety status

Real-money execution is **disabled**.

```text
release: TST_FUSION_V4_ALLIGATOR_TMV
mode: PAPER_ONLY
liveTrading: false
executorAllowed: false
```

## Shared account limits

- Binance Spot only
- USDT quote / long only
- no Futures
- no leverage
- no withdrawals
- capital baseline: 20.08 USDT
- maximum open positions: 1
- maximum position: 7 USDT
- maximum risk per trade: 0.20 USDT
- daily realized loss stop: 0.50 USDT

## Main files

- `fusion/policy.json` — shared strategy/risk policy
- `fusion/server.mjs` — master decision layer
- `validation/fusion/alligator-tmv-manifest.json` — frozen strategy definition
- `hummingbot/controllers/directional_trading/tst_alligator_tmv_signal.py` — live market candidate controller, execution disabled
- `freqtrade/user_data/strategies/AdaptiveRegimeStrategy.py` — Freqtrade implementation
- `jesse/strategies/AdaptiveRegimeFusionValidator/__init__.py` — Jesse implementation
- `.github/workflows/fusion-validate.yml` — syntax and fail-closed CI checks

## Run Fusion Master

```bash
npm ci
export FUSION_INGEST_TOKEN="store-this-as-a-secret-on-the-host"
npm run start:fusion
```

Status:

```bash
curl http://127.0.0.1:8787/status
```

Secrets must stay in the deployment secret store. Do not commit Binance, Telegram, Condor, Freqtrade, Jesse, or Fusion credentials to GitHub.
