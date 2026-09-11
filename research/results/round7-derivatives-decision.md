# Round 7 — Derivatives Positioning Decision

Status: **CLOSED / REJECTED**

## Discovery

Frozen families tested on BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT and DOGEUSDT from 2026-03-01 through 2026-09-01:

- DELEVERAGING_EXHAUSTION_V1
- LEVERAGED_CONTINUATION_V1
- SMART_CROWD_DIVERGENCE_V1
- FUNDING_CROWDING_SQUEEZE_V1

The Round 7 scanner completed with `NO_EDGE_FOUND` and zero survivors under the predeclared gate.

The only promising diagnostic was `DELEVERAGING_EXHAUSTION_V1:DOWN_LONG` at 4h:

- n = 29
- mean = +1.5058%
- median = +1.1146%
- hit rate = 79.31%
- median MFE/MAE = 4.159
- q = 0.000455

It was **not accepted** because the frozen minimum sample requirement was n >= 40.

## Untouched OOS validation

The exact same definition was then frozen and tested on the untouched pre-discovery period 2025-09-01 through 2026-03-01.

Result:

- n = 51
- mean = +1.4679%
- median = +0.3883%
- hit rate = 62.75%
- median MFE/MAE = 1.182
- p(mean > 0) = 0.153
- decision = `REJECT_OOS`

The hypothesis therefore failed the predeclared OOS gate. No thresholds were relaxed and no symbol was cherry-picked after seeing results.

## Final decision

`ROUND7_REJECTED`

Do not promote any Round 7 family to the live bot, shadow execution filter, sizing model or entry veto. Keep `liveTrading=false` and `liveReady=false` for this research branch.
