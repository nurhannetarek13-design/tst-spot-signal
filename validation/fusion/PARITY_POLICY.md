# Execution Parity Policy

## Purpose
Parity validation verifies that a strategy translation implements the frozen canonical specification. It is separate from independent engine validation.

## Canonical parity requirements
A validator is `PARITY_PASS` only when all required fields agree with the canonical fixture:

- candidate fingerprint
- dataset SHA-256
- trade count
- entry timestamps
- exit timestamps
- entry prices within explicit tolerance
- exit prices within explicit tolerance
- exit reasons

Canonical execution semantics are frozen by the fixture. No engine-specific cooldown, ROI, or order-pricing tweak may be introduced solely to improve parity statistics. Any required translation belongs in an explicit adapter layer and must be regression-tested.

## Independent validation
Native Freqtrade, Jesse, NautilusTrader, and other external backtests remain independent checks. Their native execution results must not be represented as canonical parity results. Differences are retained and reported rather than tuned away.

## Research gate
Parity success is not evidence of profitability. A candidate that fails profitability/robustness gates remains rejected even with perfect parity.

SAHARAUSDT / TS_MOMENTUM is a parity fixture only and is not authorized for live trading. Current research remains `RESEARCH_ONLY` until a future candidate separately passes validation, forward testing, and live-review gates.
