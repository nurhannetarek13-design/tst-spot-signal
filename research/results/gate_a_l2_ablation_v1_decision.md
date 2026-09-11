# Gate A x L2 Ablation V1 — Decision Record

Status: **COMPLETE — KEEP GATE A UNCHANGED**

## Scope

This test was research-only. It replayed the frozen Gate A baseline and applied the already-frozen Gate H-style L2 persistent-pressure confirmation only as an entry veto. No Gate A threshold was changed, no L2 threshold was tuned, no ML or position sizing was added, and live trading was not enabled.

Frozen Gate A: p >= 0.65, expected edge > 0, TP 1.20%, SL 0.70%, timeout 240 minutes, round-trip cost 0.28%.

Frozen L2 confirmation: 5 consecutive seconds, OBI10 >= 0.45, mean directional microprice >= 0.05 bps, using Bybit linear ob500 archives.

Historical matrix: SOLUSDT and ETHUSDT on 2025-01-15, 2025-02-15, and 2025-03-15 (6 independent symbol-days).

## CI execution

GitHub Actions run: `34598947373` (`Gate A x L2 Ablation V1`). The full six-cell matrix and aggregate job completed successfully.

## Aggregate result

Baseline Gate A produced 26 trades: profit factor 2.7856, hit rate 57.69%, mean net return +0.4388% per trade, median net return +0.3520%, summed net return +11.4076%, and maximum drawdown 3.6889%.

The frozen L2 confirmation accepted **0 of 26** Gate A entries. Therefore the L2-vetoed population was all 26 baseline trades and the L2-confirmed population was empty.

The aggregate information gate required at least 20 baseline trades and at least 10 L2-confirmed trades. Baseline sample size passed, but confirmed sample size was 0, so `informationSufficient=false`, `aggregateImproves=false`, `positiveCells=0`, and `pass=false`.

## Decision

**KEEP_GATE_A_UNCHANGED.** Do not promote this L2 confirmation rule into Gate A or the execution path. A rule that vetoes 100% of the observed Gate A entries cannot demonstrate incremental value on this frozen sample.

Do not retune the L2 thresholds against these same six symbol-days. That would turn this clean ablation into threshold fitting and weaken the evidence. Any future L2 hypothesis must be defined independently and validated on fresh data before it can affect Gate A.

No live-trading, sizing, execution, or risk-limit changes were made by this experiment.
