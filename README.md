# TST Fusion Spot Research Bot

The project is now organized as **one master bot with three specialist engines**:

- **Hummingbot V2 + Condor** — market-data / order-book layer and future deterministic execution engine.
- **Freqtrade** — primary strategy, backtesting, and dry-run validator.
- **Jesse** — independent second validator.
- **Fusion Master** — final decision and risk gate that combines the three.

## Current safety status

Real-money execution is **disabled**.

The current release is:

```text
TST_FUSION_V1
mode: PAPER_ONLY
liveTrading: false
executorAllowed: false
```

The historical validation already performed on the older strategy family did not justify automatic live execution, so the new architecture starts fail-closed rather than inheriting a live flag.

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

## Fusion decision flow

```text
Hummingbot market candidate
        |
        v
Fusion Master
   |         |
   v         v
Freqtrade   Jesse
validator   validator
   \         /
    \       /
     v     v
Shared risk gate
        |
        +--> PAPER_APPROVED
        |
        +--> NO_TRADE
```

Both validators must refer to the same strategy release, be fresh, and pass the configured evidence checks. Missing evidence, stale evidence, risk-limit breaches, or disagreement fails closed.

## Main files

- `fusion/policy.json` — shared trading/risk policy
- `fusion/server.mjs` — master decision layer
- `hummingbot/controllers/directional_trading/tst_fusion_signal.py` — Hummingbot V2 candidate controller
- `freqtrade/user_data/strategies/AdaptiveRegimeStrategy.py` — Freqtrade strategy/validator
- `jesse/strategies/AdaptiveRegimeFusionValidator/__init__.py` — Jesse independent validator
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

## Deployment requirement

The three-engine stack needs a persistent Docker host for Hummingbot/Condor and Freqtrade. Cloudflare Worker / Vercel remain useful for lightweight services, but they are not a replacement for a long-running Hummingbot container.
