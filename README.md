# TST Fusion Spot Bot

The bot is organized as one master Binance Spot bot with specialist engines:

- **Hummingbot V2 + Condor** — market-data / order-book layer and deterministic execution path.
- **Freqtrade** — primary strategy and backtest validator.
- **Jesse** — independent validator.
- **Fusion Master** — final decision and risk gate.

## Active implemented strategy

`TST_ALLIGATOR_SMC_V2`

This version keeps the useful trigger stack from the first two sources, then adds the mechanical market-structure rules from the day-trading course:

1. **Williams Alligator + MACD + Parabolic SAR**
2. **Trend + Momentum + Relative Volume**
3. **Pre-existing demand zone**
4. **Liquidity sweep at demand**
5. **Bullish market shift / break of structure**
6. **Higher-timeframe bias**
7. **Structural stop + minimum 2R + risk-limited position sizing**
8. Existing Fusion confirmations: BTC regime, L2 bid share, taker flow, spread and visible liquidity.

The Williams Alligator remains causal: SMMA 13/8/5 is calculated without the classic forward plotting offsets.

## Mechanical 15m entry path

```text
Alligator bullish and widening
+ MACD bullish
+ PSAR below price
+ relative volume >= 1.20x prior 20-bar mean
+ close above EMA200
+ higher-time-horizon trend proxy positive
+ valid demand zone created BEFORE the current setup
+ liquidity sweep/reclaim at that demand zone
+ bullish market shift: close above prior 12-bar high after sweep
+ entry still within 3 ATR of demand-zone top
+ structural stop <= 3%
+ room for at least 2R
+ runtime actual 4h EMA20 bias positive
+ BTC regime / L2 / taker-flow / spread / liquidity gates
```

### Demand / liquidity rules

A demand zone is created from the prior bearish pivot candle when a later bullish displacement closes above the prior 12-bar high with body >= `0.80 ATR` and relative volume >= `1.10x`.

The zone cannot confirm itself on the same candle. It remains active until a close below its low or a 96-bar expiry. Price must later interact with the pre-existing zone, sweep the prior 12-bar low and reclaim it, then print a bullish market shift within 8 bars. The market-shift confirmation remains valid for 6 bars.

### Higher-timeframe rule

The validators use a causal 15m `EMA320` rising proxy to cover roughly the same time horizon. At runtime Hummingbot separately fetches actual Binance `4h` candles and requires the latest closed 4h candle to be above a rising `EMA20`.

### Risk / exit

The normal structural stop is `0.20 ATR` below the lower of the active demand-zone low and the swept-liquidity low. If that stop would be wider than 3% the setup is rejected. The target is at least `2R`.

Position notional is reduced automatically when the structural stop would otherwise exceed the shared `0.20 USDT` maximum risk. This keeps the existing small-account risk policy instead of forcing a fixed position size.

## Current safety status

Real-money execution is **disabled**.

```text
release: TST_FUSION_V5_ALLIGATOR_SMC
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
- `validation/fusion/alligator-smc-v2-manifest.json` — frozen mechanical definition
- `hummingbot/controllers/directional_trading/tst_alligator_tmv_signal.py` — runtime candidate controller, execution disabled
- `freqtrade/user_data/strategies/AdaptiveRegimeStrategy.py` — Freqtrade implementation
- `jesse/strategies/AdaptiveRegimeFusionValidator/__init__.py` — Jesse implementation
- `.github/workflows/fusion-validate.yml` — syntax, structure-gate and fail-closed CI checks

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
