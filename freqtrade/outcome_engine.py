from __future__ import annotations

"""Forward outcome evaluator for live candidate decisions.

Research/monitoring only: measures MFE/MAE, TP-before-SL, simulated net return
including conservative execution costs, and maintains a lane-health kill switch.
It never places or modifies orders.
"""

import json,os,time,urllib.parse,urllib.request
from collections import defaultdict
from pathlib import Path

CANDIDATE_PATH=Path(os.getenv('TST_CANDIDATE_EVENT_PATH','/data/tst_candidate_events.jsonl'))
OUTCOME_PATH=Path(os.getenv('TST_OUTCOME_PATH','/data/tst_candidate_outcomes.jsonl'))
HEALTH_PATH=Path(os.getenv('TST_LANE_HEALTH_PATH','/data/tst_lane_health.json'))
STATE_PATH=Path(os.getenv('TST_OUTCOME_STATE_PATH','/data/tst_outcome_state.json'))
POLL_SEC=max(20,int(os.getenv('OUTCOME_ENGINE_POLL_SEC','45')))
KILL_MIN_SAMPLE=max(20,int(os.getenv('LANE_KILL_MIN_SAMPLE','30')))
KILL_HOURS=max(1.0,float(os.getenv('LANE_KILL_HOURS','2')))
ROUND_TRIP_FEE_PCT=max(0.0,float(os.getenv('OUTCOME_ROUND_TRIP_FEE_PCT','0.20')))
ROUND_TRIP_SLIPPAGE_PCT=max(0.0,float(os.getenv('OUTCOME_ROUND_TRIP_SLIPPAGE_PCT','0.08')))
BASE_COST_PCT=ROUND_TRIP_FEE_PCT+ROUND_TRIP_SLIPPAGE_PCT
PUBLIC_BASES=['https://data-api.binance.vision/api/v3','https://api.binance.com/api/v3']
CONTEXT_FIELDS=(
    'regime','regime_trade_permission_shadow','breadth_1h','breadth_4h','universe_n',
    'rs_vs_btc_4h','r1h_pct_rank','r4h_pct_rank','rs_btc4h_pct_rank','accel1h_pct_rank',
    'volume_expansion_pct_rank','liquidity_pct_rank','opportunity_pct_shadow','context_generated_at',
)

def _read_json(path,default):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except Exception:return default

def _write_json(path,row):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(json.dumps(row,separators=(',',':'),ensure_ascii=False),encoding='utf-8'); os.replace(tmp,path)

def _public(path,params):
    q=urllib.parse.urlencode(params); last=None
    for base in PUBLIC_BASES:
        try:
            req=urllib.request.Request(f'{base}{path}?{q}',headers={'User-Agent':'tst-outcome-engine/2.1'})
            with urllib.request.urlopen(req,timeout=12) as r:return json.loads(r.read() or b'[]')
        except Exception as exc:last=exc
    raise RuntimeError(last or 'public API unavailable')

def _events():
    if not CANDIDATE_PATH.exists():return []
    rows=[]
    try:
        for line in CANDIDATE_PATH.read_text(encoding='utf-8').splitlines()[-5000:]:
            try:
                row=json.loads(line)
                if isinstance(row,dict) and row.get('event_id'):rows.append(row)
            except Exception:pass
    except Exception:pass
    return rows

def _path_result(rows,entry,target,stop):
    if not (entry>0 and target>entry and 0<stop<entry):return None,None,None
    for i,x in enumerate(rows):
        hi=float(x[2]); lo=float(x[3])
        hit_tp=hi>=target; hit_sl=lo<=stop
        if hit_tp and hit_sl:
            # Intrabar ordering is unknowable from OHLC; assume adverse ordering.
            return 0,'SL',i
        if hit_sl:return 0,'SL',i
        if hit_tp:return 1,'TP',i
    return None,None,None

def _forward(event):
    ts=float(event.get('ts') or 0); price=float(event.get('price') or 0); symbol=str(event.get('symbol') or '')
    if ts<=0 or price<=0 or not symbol:return None
    age=time.time()-ts
    if age<15*60:return None
    limit=65 if age>=60*60 else (35 if age>=30*60 else 20)
    rows=_public('/klines',{'symbol':symbol,'interval':'1m','startTime':int(ts*1000),'limit':limit})
    if not isinstance(rows,list) or len(rows)<15:return None
    highs=[float(x[2]) for x in rows]; lows=[float(x[3]) for x in rows]; closes=[float(x[4]) for x in rows]
    result={
        'event_id':event['event_id'],'ts':ts,'source_ts':ts,'evaluated_at':time.time(),
        'symbol':symbol,'lane':event.get('lane'),'setup_type':event.get('strategy') or event.get('lane'),
        'score':event.get('score'),'decision':event.get('decision'),'reason':event.get('reason'),'price':price,
        'target':event.get('target'),'stop':event.get('stop'),'stake_usdt':event.get('stake_usdt'),
        'strategy':event.get('strategy'),'risk_pct':event.get('risk_pct'),'reward_pct':event.get('reward_pct'),
        'mfe_pct':(max(highs)/price-1)*100,'mae_pct':(min(lows)/price-1)*100,'assumed_cost_pct':BASE_COST_PCT,
    }
    for field in CONTEXT_FIELDS:
        result[field]=event.get(field)
    if len(closes)>=15:result['ret15_pct']=(closes[14]/price-1)*100
    if len(closes)>=30:result['ret30_pct']=(closes[29]/price-1)*100
    if len(closes)>=60:
        result['ret60_pct']=(closes[59]/price-1)*100; result['complete']=True
        try:target=float(event.get('target') or 0); stop=float(event.get('stop') or 0)
        except Exception:target=stop=0.0
        tp_before_sl,outcome,bar=_path_result(rows[:60],price,target,stop)
        result['tp_before_sl']=tp_before_sl; result['path_outcome']=outcome; result['path_bar']=bar
        if outcome=='TP': gross=(target/price-1)*100
        elif outcome=='SL': gross=(stop/price-1)*100
        else:gross=result['ret60_pct']
        result['sim_gross_pct']=gross; result['sim_net_pct']=gross-BASE_COST_PCT
        result['net_return_pct']=result['sim_net_pct']
        result['holding_min']=(int(bar)+1) if bar is not None else 60
    else:result['complete']=False
    return result

def _append_outcome(row):
    OUTCOME_PATH.parent.mkdir(parents=True,exist_ok=True)
    with OUTCOME_PATH.open('a',encoding='utf-8') as f:f.write(json.dumps(row,separators=(',',':'),ensure_ascii=False)+'\n')

def _all_outcomes():
    if not OUTCOME_PATH.exists():return []
    out=[]
    for line in OUTCOME_PATH.read_text(encoding='utf-8').splitlines()[-8000:]:
        try:
            row=json.loads(line)
            if isinstance(row,dict):out.append(row)
        except Exception:pass
    return out

def _health():
    by_lane=defaultdict(list); latest={}
    for row in _all_outcomes():latest[row.get('event_id')]=row
    for row in latest.values():
        if row.get('complete') and row.get('decision')=='READY' and row.get('sim_net_pct') is not None:by_lane[str(row.get('lane') or 'UNKNOWN')].append(row)
    old=_read_json(HEALTH_PATH,{'lanes':{}}); now=time.time(); health={'updated_at':now,'cost_pct':BASE_COST_PCT,'lanes':{}}
    for lane in {'NORMAL','MID','EXPLOSIVE','EXTREME'}|set(by_lane):
        rows=by_lane.get(lane,[])[-150:]; n=len(rows); avg=sum(float(r['sim_net_pct']) for r in rows)/n if n else None; hit=sum(1 for r in rows if int(r.get('tp_before_sl') or 0)==1)/n if n else None
        prev=(old.get('lanes') or {}).get(lane,{}); disabled_until=float(prev.get('disabled_until') or 0); triggered=False
        # Require BOTH negative net expectancy and poor TP-before-SL rate.
        if n>=KILL_MIN_SAMPLE and avg is not None and hit is not None and avg<=-0.20 and hit<0.40:
            disabled_until=max(disabled_until,now+KILL_HOURS*3600); triggered=True
        health['lanes'][lane]={'sample':n,'avg_sim_net_pct':None if avg is None else round(avg,4),'tp_before_sl_rate':None if hit is None else round(hit,4),'disabled_until':disabled_until,'disabled':disabled_until>now,'triggered_now':triggered}
    _write_json(HEALTH_PATH,health); print('[outcome-engine] lane-health '+', '.join(f"{k}:n={v['sample']} net={v['avg_sim_net_pct']} disabled={v['disabled']}" for k,v in health['lanes'].items()),flush=True)

def run_once():
    state=_read_json(STATE_PATH,{'done60':[],'latest_eval':{}}); done60=set(state.get('done60') or []); latest_eval=dict(state.get('latest_eval') or {}); changed=False
    for event in _events():
        eid=str(event.get('event_id'))
        if eid in done60:continue
        if time.time()-float(latest_eval.get(eid) or 0)<10*60:continue
        row=_forward(event)
        if row is None:continue
        _append_outcome(row); latest_eval[eid]=time.time(); changed=True
        if row.get('complete'):done60.add(eid)
        print(f"[outcome] {row['lane']} {row['symbol']} regime={row.get('regime')} opp={row.get('opportunity_pct_shadow')} decision={row['decision']} MFE={row['mfe_pct']:+.2f}% MAE={row['mae_pct']:+.2f}% path={row.get('path_outcome')} net={row.get('sim_net_pct')}",flush=True)
    if changed:_write_json(STATE_PATH,{'done60':list(done60)[-8000:],'latest_eval':latest_eval})
    _health()

def main():
    print(f'[outcome-engine] ONLINE horizons=15m,30m,60m path_aware=True context_features=True baseline_cost={BASE_COST_PCT:.2f}% kill_sample={KILL_MIN_SAMPLE}',flush=True)
    while True:
        try:run_once()
        except Exception as exc:print(f'[outcome-engine] loop warning: {type(exc).__name__}: {str(exc)[:160]}',flush=True)
        time.sleep(POLL_SEC)

if __name__=='__main__':main()
