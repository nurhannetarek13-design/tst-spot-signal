#!/usr/bin/env python3
"""Probe NautilusTrader bar-based market fill timing before full parity replay.
Research-only; no network orders and no live credentials.
"""
import json, pathlib
from decimal import Decimal

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig, StrategyConfig
from nautilus_trader.common import LogLevel
from nautilus_trader.model import AccountType, Bar, BarType, Currency, Money, OmsType, OrderSide, Price, Quantity, Venue
from nautilus_trader.testkit.providers import TestInstrumentProvider
from nautilus_trader.trading import Strategy

OUT = pathlib.Path('validation/engines/nautilus-market-fill-probe.json')
ETH = TestInstrumentProvider.ethusdt_binance()
BAR_TYPE = BarType.from_str('ETHUSDT.BINANCE-15-MINUTE-LAST-EXTERNAL')
BASE_TS = 1_800_000_000_000_000_000
STEP = 15 * 60 * 1_000_000_000

class ProbeConfig(StrategyConfig):
    def __init__(self, instrument_id, bar_type, **kwargs):
        super().__init__(**kwargs)
        self.instrument_id = instrument_id
        self.bar_type = bar_type

class Probe(Strategy):
    def __init__(self, config):
        super().__init__(config)
        self.seen = 0
    def on_start(self):
        self.subscribe_bars(self.config.bar_type)
    def on_bar(self, bar):
        self.seen += 1
        if self.seen == 1:
            order = self.order_factory.market(
                self.config.instrument_id,
                OrderSide.BUY,
                Quantity.from_str('0.100'),
            )
            self.submit_order(order)
        elif self.seen == 4 and self.portfolio.is_net_long(self.config.instrument_id):
            self.close_all_positions(self.config.instrument_id)

bars = []
prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
for i, op in enumerate(prices):
    ts = BASE_TS + i * STEP
    bars.append(Bar(
        bar_type=BAR_TYPE,
        open=Price.from_str(f'{op:.2f}'),
        high=Price.from_str(f'{op+0.50:.2f}'),
        low=Price.from_str(f'{op-0.50:.2f}'),
        close=Price.from_str(f'{op+0.25:.2f}'),
        volume=Quantity.from_int(1000),
        ts_event=ts,
        ts_init=ts,
    ))

engine = BacktestEngine(BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
engine.add_venue(
    venue=Venue('BINANCE'),
    oms_type=OmsType.NETTING,
    account_type=AccountType.CASH,
    base_currency=None,
    starting_balances=[Money(1_000_000.0, Currency.from_str('USDT'))],
)
engine.add_instrument(ETH)
engine.add_data(bars)
strategy = Probe(ProbeConfig(instrument_id=ETH.id, bar_type=BAR_TYPE))
engine.add_strategy(strategy)
engine.run()

fills = engine.generate_order_fills_report().reset_index().to_dict(orient='records')
positions = engine.generate_positions_report().reset_index().to_dict(orient='records')

def clean(rows):
    out=[]
    for r in rows:
        out.append({str(k): (str(v) if v is not None else None) for k,v in r.items()})
    return out

report = {
    'schemaVersion': 1,
    'engine': 'NautilusTrader',
    'engineVersionTarget': '2.x',
    'authorization': 'RESEARCH_ONLY',
    'liveTrading': False,
    'purpose': 'identify exact external-bar market-fill semantics before canonical parity replay',
    'inputBars': [{'tsEvent': b.ts_event, 'open': str(b.open), 'close': str(b.close)} for b in bars],
    'fills': clean(fills),
    'positions': clean(positions),
    'passed': len(fills) >= 1,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
engine.dispose()
