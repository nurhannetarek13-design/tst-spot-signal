#!/usr/bin/env python3
"""No-network build tests for execution-state and risk-ledger invariants."""
import os,tempfile,time
from pathlib import Path

root=Path(tempfile.mkdtemp(prefix='tst-exec-test-'))
os.environ['TST_TRADE_STATE_PATH']=str(root/'positions.json')
os.environ['TST_TRADE_EVENT_PATH']=str(root/'events.jsonl')
os.environ['TST_EXECUTION_RESERVATION_PATH']=str(root/'reservations.json')
os.environ['RISK_TIMEZONE']='Africa/Cairo'

import trade_state
from front_proxy import _client_id

sid='SOLUSDT-NORMAL-TEST-001'
body={'signal_id':sid,'symbol':'SOLUSDT','quote_amount_usdt':10.0,'take_profit_price':105.0,'stop_loss_price':98.0,
      'client_order_id':_client_id('TSTB-',sid)}

# 100 duplicate submissions -> exactly one reservation.
wins=0
for _ in range(100):
    ok,_row=trade_state.reserve_execution(body,'BUY')
    wins+=int(ok)
assert wins==1,wins
assert trade_state.get_reservation(sid,'BUY')['client_order_id']==body['client_order_id']

# Unknown transport state must remain blocked, not blindly retried.
trade_state.update_reservation(sid,'BUY',status='UNKNOWN')
ok,row=trade_state.reserve_execution(body,'BUY')
assert ok is False and row['status']=='UNKNOWN'

# Deterministic exchange IDs survive process/retry boundaries.
assert _client_id('TSTB-',sid)==_client_id('TSTB-',sid)
assert _client_id('TSTB-',sid)!=_client_id('TSTB-',sid+'x')
assert len(_client_id('TSTB-',sid))<=36

# Synthetic fill -> protection pending -> OCO active -> portfolio risk known.
buy={'ok':True,'status':'BUY_FILLED','signal_id':sid,'symbol':'SOLUSDT','order_id':123,'executed_qty':0.1,'quote_spent':10.0}
trade_state.record_buy(body,buy)
trade_state.update_position(sid,status='PROTECTION_PENDING')
pos=trade_state.position_for_signal(sid)
assert pos and pos['status']=='PROTECTION_PENDING' and abs(pos['entry']-100.0)<1e-12

oco_body={'signal_id':sid,'symbol':'SOLUSDT','quantity':0.1,'take_profit_price':105.0,'stop_loss_price':98.0,'stop_limit_price':97.7,
          'list_client_order_id':_client_id('TSTO-',sid),'stop_client_order_id':_client_id('TSTS-',sid),'limit_client_order_id':_client_id('TSTL-',sid)}
trade_state.reserve_execution(oco_body,'OCO')
trade_state.record_oco(oco_body,{'ok':True,'status':'OCO_PLACED','signal_id':sid,'symbol':'SOLUSDT','oco_order_list_id':456})
pos=trade_state.position_for_signal(sid)
assert pos and pos['status']=='OCO_ACTIVE' and pos['oco_order_list_id']==456
snap=trade_state.portfolio_snapshot()
assert snap['open_count']==1 and snap['incomplete_count']==0
assert abs(snap['stop_risk_usdt']-0.2)<1e-9,snap

# Reconciled close must persist actual PnL into the risk ledger and remove risk.
trade_state.close_position(sid,exit_price=95.0,exit_qty=0.1,exit_quote=9.5,
                           realized_pnl_usdt=-0.52,close_reason='STOP_LOSS',
                           exit_order_id=789,closed_at=time.time())
assert trade_state.portfolio_snapshot()['open_count']==0
perf=trade_state.performance_snapshot()
assert perf['closed_today']==1 and perf['consecutive_losses']==1,perf
assert abs(perf['realized_pnl_today_usdt']+0.52)<1e-9,perf
assert perf['current_realized_drawdown_usdt']>=0.52-1e-9,perf

# Persistence exists on disk and can be parsed after writes.
assert Path(os.environ['TST_TRADE_STATE_PATH']).exists()
assert Path(os.environ['TST_EXECUTION_RESERVATION_PATH']).exists()
print('[execution-safety-test] PASS duplicate=100 exact-once=1 unknown_retry=blocked lifecycle=protected realized_pnl=ok drawdown=ok persistence=ok')
