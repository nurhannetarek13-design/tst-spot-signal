# Mid-Momentum Archived-Only OOS — Final Result

Run: `34420898348`  
Artifact: `mid-momentum-archived-only-oos-report` (`10130918229`)  
Mode: `RESEARCH_ONLY` — no live promotion.

## Coverage

- Archived-only target symbols requested: **41**
- Archived-only target symbols loaded: **41**
- Frozen hypotheses tested: **9** (3 setup families × 3 TP/SL contracts)
- OOS replication winners: **0 / 9**

## Ranked OOS results

| Rank | Family | Contract | n | Mean net | PF | Stress mean net | Stress PF | Bootstrap p05 | OOS pass |
|---:|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | MID_REACCELERATION | TP15_SL10_4H | 33 | -0.3983% | 0.511 | -0.6183% | 0.352 | -0.7403% | NO |
| 2 | MID_PULLBACK_RECLAIM | TP15_SL10_4H | 88 | -0.4561% | 0.468 | -0.6761% | 0.328 | -0.6702% | NO |
| 3 | MID_REACCELERATION | TP22_SL13_8H | 33 | -0.5608% | 0.491 | -0.7808% | 0.378 | -1.0331% | NO |
| 4 | MID_PULLBACK_RECLAIM | TP22_SL13_8H | 88 | -0.6255% | 0.456 | -0.8455% | 0.354 | -0.8974% | NO |
| 5 | MID_PULLBACK_RECLAIM | TP30_SL17_12H | 88 | -0.6279% | 0.552 | -0.8479% | 0.456 | -1.0038% | NO |
| 6 | MID_REACCELERATION | TP30_SL17_12H | 33 | -0.6557% | 0.531 | -0.8757% | 0.438 | -1.3086% | NO |
| 7 | MID_RANGE_RESUME | TP30_SL17_12H | 11 | -0.7286% | 0.404 | -0.9486% | 0.324 | insufficient | NO |
| 8 | MID_RANGE_RESUME | TP15_SL10_4H | 11 | -0.8659% | 0.173 | -1.0859% | 0.115 | insufficient | NO |
| 9 | MID_RANGE_RESUME | TP22_SL13_8H | 11 | -0.9346% | 0.231 | -1.1546% | 0.173 | insufficient | NO |

## Frozen pass gate

A hypothesis passes only if it has `n >= 20`, positive mean net and `PF >= 1.05` under both normal and stress costs, and day-cluster bootstrap `p05 > 0`.

## Decision

**Reject the Mid-Momentum +5%..+25% family as a live candidate in its current frozen form.** The archived-only OOS sample did not rescue any of the nine variants; all nine had negative OOS mean net returns, and none passed the replication gate. No live bot rule should be loosened or promoted from this test.
