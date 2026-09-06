# Microstructure & Liquidation Engine — Data Source Contract

Branch: `fix/unified-execution-parity`

This document freezes the approved historical data sources for derivatives research so future rounds do not silently mix USD-M, COIN-M, Spot, live WebSocket samples, or third-party archives.

## Source priority

| Feature | Primary source | Market scope | Notes |
|---|---|---|---|
| Spot OHLCV / forward returns | Binance Vision | Spot | Verified ZIP + SHA256 checksum |
| USD-M / COIN-M futures klines | Binance Vision | UM / CM | Verified ZIP + SHA256 checksum |
| Open interest | Binance Vision `metrics` | UM / CM | 5m snapshots; quality-gated for missing/duplicate timestamps |
| Taker long/short ratio | Binance Vision `metrics` | UM / CM | Use exact causal timestamp alignment |
| Top-trader ratios | Binance Vision `metrics` | UM / CM | Diagnostic/discovery features only until frozen |
| Funding rate | Binance Vision `fundingRate` | UM / CM | Monthly archives; never synthesize missing months |
| Mark price | Binance Vision `markPriceKlines` | UM / CM | Prefer 1m/15m depending on research horizon |
| Index price | Binance Vision `indexPriceKlines` | UM / CM | Same timestamp discipline as mark price |
| Premium index / basis proxy | Binance Vision `premiumIndexKlines` | UM / CM | Used for crowding/basis regimes |
| Aggressive trade flow | Binance Vision `aggTrades` | UM / CM | `isBuyerMaker` can derive aggressor side |
| USD-M historical liquidations | Tardis historical/sample data when available | UM | `forceOrder`; third-party source must pass timestamp/schema checks |
| L2 / book ticker | Tardis historical/sample data when available; Binance Vision where available | UM / CM | Do not assume full historical coverage |
| COIN-M liquidation snapshots | Binance Vision `liquidationSnapshot` | CM only | Never relabel as USD-M |
| Live future liquidation collector | Binance USD-M WebSocket `!forceOrder@arr` | UM | Forward collection only; not a replacement for historical archives |

## Hard guardrails

1. `liquidationSnapshot` from Binance Vision is COIN-M only. It must never be merged into a USD-M feature table under a USD-M label.
2. Funding archives are monthly. Missing months are quality failures, not zeros.
3. Third-party Tardis data is allowed only after schema, timezone, monotonicity, duplicates, and symbol-market mapping checks pass.
4. No nearest-neighbor lookahead. Feature rows must be aligned at or before the event timestamp.
5. Missing historical liquidation/L2 data must remain missing. Do not fabricate proxies and call them liquidations or depth.
6. Research output must remain `RESEARCH_ONLY`; this source contract does not authorize live trading.

## Discovery order after V2 rejection

`TST_DERIVATIVES_PRESSURE_V2_CORE_HISTORICAL` failed the long-window gate. New work must be fresh discovery, not threshold tuning of the rejected event.

Priority feature families:

1. OI acceleration + taker imbalance + funding regime.
2. OI acceleration + premium/basis crowding.
3. Top-trader divergence vs public long/short ratio.
4. AggTrades-derived aggressive flow exhaustion/continuation.
5. USD-M liquidation intensity around candidate events when validated Tardis history is available.
6. L2/book imbalance only on periods with defensible historical coverage.

Candidate definitions must be frozen before OOS validation. Live remains OFF until all validation gates pass.
