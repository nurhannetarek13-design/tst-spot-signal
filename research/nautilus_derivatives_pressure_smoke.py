#!/usr/bin/env python3
"""NautilusTrader smoke/parity harness for TST derivatives-pressure research.

Purpose:
- prove NautilusTrader 2.x can initialize a Binance Spot CASH backtest engine in CI;
- freeze the execution contract we intend to port next;
- keep live trading disabled.

This is deliberately a smoke/parity bootstrap, not a profitability claim.
"""
from __future__ import annotations

import json
import pathlib

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import AccountType, Currency, Money, OmsType, TraderId, Venue
from nautilus_trader.testkit.providers import TestInstrumentProvider

OUT = pathlib.Path("validation/engines/nautilus-derivatives-pressure-smoke.json")

STRATEGY_ID = "TST_DERIVATIVES_PRESSURE_V2_48H_EXECUTABLE_V1"
CONTRACT = {
    "strategyId": STRATEGY_ID,
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "venue": "BINANCE",
    "accountType": "CASH",
    "side": "LONG_ONLY",
    "signal": {
        "scoreMin": 80,
        "oiChange2hMinExclusive": 0.0,
        "takerBuySellRatio1hMin": 1.05,
        "onsetOnly": True,
    },
    "execution": {
        "entry": "next_15m_bar_open",
        "maxConcurrentPositions": 3,
        "sameSymbolOverlapAllowed": False,
        "canonicalHoldHours": 48,
        "roundTripCostScenariosBps": [20, 40, 60],
    },
}


def main() -> None:
    config = BacktestEngineConfig(
        trader_id=TraderId("TST-NAUTILUS-001"),
        logging=LoggerConfig(stdout_level=LogLevel.ERROR),
    )
    engine = BacktestEngine(config=config)

    binance = Venue("BINANCE")
    engine.add_venue(
        venue=binance,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=None,
        starting_balances=[
            Money(1000.0, Currency.from_str("USDT")),
            Money(1.0, Currency.from_str("ETH")),
        ],
    )

    # Use Nautilus' bundled Binance Spot instrument only to prove instrument/venue wiring.
    instrument = TestInstrumentProvider.ethusdt_binance()
    engine.add_instrument(instrument)

    report = {
        "schemaVersion": 1,
        "engine": "NautilusTrader",
        "engineBootstrapPassed": True,
        "instrumentId": str(instrument.id),
        "contract": CONTRACT,
        "nextStep": "port frozen derivatives-pressure events into Nautilus bar/custom-data replay and compare canonical fills/trade ledger",
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))

    engine.dispose()


if __name__ == "__main__":
    main()
