# Execution Validation Layer

This directory is the bridge between the existing edge-research pipeline and a future `hftbacktest` runner.

## Required order

1. Primary edge must pass independently. Execution simulation must never be used to rescue a failed edge.
2. Replay the same candidate trades on canonical Tardis L2 data.
3. Replay again after fusing top-of-book/bookTicker updates where timestamp-compatible data exists.
4. Use partial-fill simulation.
5. Stress queue assumptions with ProbQueue n=1, n=2, n=3, then RiskAverse.
6. Stress order latency at 1x, 2x, 3x measured latency (or a conservative constant until measured latency exists).
7. Include Binance Spot fees and report realized slippage/fill rate.
8. Feed scenario summaries into `compare_hft_scenarios.py`.
9. A pass means PAPER ELIGIBLE only. Live remains disabled.

## Scenario summary contract

The actual hftbacktest runner must emit a JSON array. Each row must contain at least:

```json
{
  "symbol": "BTCUSDT",
  "feed": "L2_ONLY",
  "queueModel": "PROB_QUEUE_N1",
  "latencyMultiplier": 1,
  "fillRate": 0.91,
  "netExpectancy": 0.0012,
  "profitFactor": 1.34,
  "slippageBps": 1.8
}
```

Do not populate these fields from candle backtests or assumptions. They must come from the execution replay.

## Capital resizing

Any material increase in order size requires a new execution-validation run. `hftbacktest` replays a historical book and does not model the strategy's own market impact, so a result at small size cannot be blindly extrapolated to larger size.
