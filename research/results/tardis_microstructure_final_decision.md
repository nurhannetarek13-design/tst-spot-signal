# Tardis L2 Microstructure — Final Decision

Status: **COMPLETE**  
Authorization: **RESEARCH_ONLY**  
Live trading: **OFF**

## Infrastructure verdict

The Binance USD-M Tardis L2 reconstruction and microstructure feature pipeline is operational and CI-validated.

Canonical invariants:
- initial post-snapshot bridge uses `pu == snapshot.lastUpdateId`;
- every later depth update requires `pu == previous u`;
- reconstructed book may never cross;
- comparable `bookTicker` states are checked against reconstructed best bid/ask;
- fetches are bounded with connection/max runtime limits and retries;
- CI integration is a one-minute integrity smoke test only and cannot promote a trading edge.

Final bounded CI smoke (`2021-09-01`, offset 0):

| Symbol | L2 feature events | Sampled seconds | Comparable bookTicker | Exact | Mismatches | Match rate | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| BTCUSDT | 1,730 | 57 | 163 | 163 | 0 | 100% | PASS |
| ETHUSDT | 1,906 | 55 | 166 | 166 | 0 | 100% | PASS |
| SOLUSDT | 1,682 | 54 | 181 | 181 | 0 | 100% | PASS |

All three symbol jobs and the aggregate job passed. Safety gates also passed, including tests, syntax/compile checks, fail-closed trading policy, and unified candidate wiring.

## Research verdict

The infrastructure is approved for research, but **the tested L2 signal is not approved as a trading edge**.

The longer Gate G diagnostics showed horizon/sign instability rather than robust directional persistence. A subsequent frozen Gate A × L2 ablation across six independent SOLUSDT/ETHUSDT symbol-days accepted **0 of 26** baseline Gate A entries under the tested L2 confirmation rule.

Frozen Gate A baseline remained materially positive in that ablation:
- trades: 26
- PF: 2.7856
- hit rate: 57.69%
- mean net return/trade: +0.4388%
- median net return/trade: +0.3520%
- summed net return: +11.4076%
- max drawdown: 3.6889%

Because the L2 confirmation produced no usable accepted sample, it demonstrated no incremental value and cannot be promoted into Gate A, execution, position sizing, or live risk logic.

## Final production decision

**KEEP_GATE_A_UNCHANGED**

- Canonical Tardis L2 engine: **APPROVED FOR RESEARCH**
- Microstructure feature pipeline: **APPROVED FOR RESEARCH**
- CI integrity gate: **PASS**
- Tested L2 confirmation/edge: **REJECT / NOT PROMOTED**
- Gate A: **UNCHANGED**
- Live trading changes from this research: **NONE**
- Position sizing/risk changes from this research: **NONE**
- `liveTrading`: **false**

Any future L2 hypothesis must use a new frozen definition and fresh unseen data. Do not retune the rejected rule on the same OOS matrix.
