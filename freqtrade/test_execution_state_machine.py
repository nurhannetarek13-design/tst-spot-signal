#!/usr/bin/env python3
"""Offline execution-state safety tests. Never calls Binance or Make."""
from __future__ import annotations

import importlib
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def configure(tmp: str):
    os.environ['TST_TRADE_STATE_PATH']=str(Path(tmp)/'positions.json')
    os.environ['TST_TRADE_EVENT_PATH']=str(Path(tmp)/'events.jsonl')
    os.environ['TST_EXECUTION_RESERVATION_PATH']=str(Path(tmp)/'reservations.json')
    os.environ['TST_TRADE_LOCK_PATH']=str(Path(tmp)/'state.lock')


def main():
    with tempfile.TemporaryDirectory() as tmp:
        configure(tmp)
        import trade_state
        importlib.reload(trade_state)

        body={
            'signal_id':'TEST-SIGNAL-001','symbol':'SOLUSDT','quote_amount_usdt':10,
            'client_order_id':'TSTB-TEST001','take_profit_price':105,
            'stop_loss_price':98,'timestamp':1234567890,
        }

        # 100 duplicate callbacks may produce exactly one reservation.
        def attempt(_): return trade_state.reserve_execution(body,'BUY')[0]
        with ThreadPoolExecutor(max_workers=20) as ex:
            accepted=list(ex.map(attempt,range(100)))
        assert sum(bool(x) for x in accepted)==1, accepted
        rows=trade_state.reservations('BUY')
        assert len(rows)==1 and rows[0]['signal_id']=='TEST-SIGNAL-001'

        # Simulate a confirmed fill: until an exchange OCO exists, protection is incomplete.
        trade_state.record_buy(body,{
            'signal_id':'TEST-SIGNAL-001','symbol':'SOLUSDT','order_id':111,
            'client_order_id':'TSTB-TEST001','executed_qty':2.0,'quote_spent':200.0,
        })
        snap=trade_state.portfolio_snapshot()
        assert snap['open_count']==1
        assert snap['incomplete_count']==1

        # Process/module restart must preserve reservation + position state.
        importlib.reload(trade_state)
        assert trade_state.get_reservation('TEST-SIGNAL-001','BUY')['status']=='FILLED'
        snap2=trade_state.portfolio_snapshot()
        assert snap2['open_count']==1 and snap2['incomplete_count']==1

        # Add OCO lifecycle; the position must become protected.
        oco_body={
            'signal_id':'TEST-SIGNAL-001','symbol':'SOLUSDT','quantity':2.0,
            'take_profit_price':105.0,'stop_loss_price':98.0,'stop_limit_price':97.7,
            'list_client_order_id':'TSTO-TEST001','stop_client_order_id':'TSTS-TEST001',
            'limit_client_order_id':'TSTL-TEST001',
        }
        ok,_=trade_state.reserve_execution(oco_body,'OCO'); assert ok
        trade_state.record_oco(oco_body,{
            'signal_id':'TEST-SIGNAL-001','symbol':'SOLUSDT','oco_order_list_id':222,
        })
        snap3=trade_state.portfolio_snapshot()
        assert snap3['open_count']==1 and snap3['incomplete_count']==0

        # A second BUY reservation for the same signal remains impossible.
        ok,row=trade_state.reserve_execution(body,'BUY')
        assert ok is False and row['status']=='FILLED'

        # Reconstructed OCO with unknown entry/cost basis must still fail closed.
        state=trade_state.load_state()
        state['positions']['REC-UNKNOWN-RISK']={
            'signal_id':'REC-UNKNOWN-RISK','symbol':'DOGEUSDT','entry':0.0,
            'quantity':100.0,'stop':0.10,'target':0.12,
            'oco_order_list_id':333,'status':'OCO_ACTIVE','opened_at':1234567890,
        }
        trade_state.save_state(state)
        snap4=trade_state.portfolio_snapshot()
        assert snap4['open_count']==2
        assert snap4['incomplete_count']==1, snap4

        print('EXECUTION_STATE_MACHINE_TESTS_PASS duplicates=100 accepted=1 restart=persistent unprotected=blocked unknown_risk=blocked protected=1')


if __name__=='__main__': main()
