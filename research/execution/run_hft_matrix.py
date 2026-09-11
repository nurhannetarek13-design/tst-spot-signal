#!/usr/bin/env python3
"""Run the complete execution-robustness matrix and evaluate it fail-closed."""
import argparse, json, subprocess, sys
from pathlib import Path

FEEDS=['L2_ONLY','L2_PLUS_BOOKTICKER']
QUEUES=['PROB_QUEUE_N1','PROB_QUEUE_N2','PROB_QUEUE_N3','RISK_AVERSE']
LAT=[1,2,3]

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--manifest',required=True,help='JSON containing symbols/data/plan/tick/lot')
    p.add_argument('--base-latency-ns',type=int,default=10_000_000)
    p.add_argument('--fee-rate',type=float,default=0.001)
    p.add_argument('--out-dir',default='validation/execution/run')
    a=p.parse_args()
    m=json.load(open(a.manifest))
    if not m.get('primaryEdgePass'):
        raise SystemExit('PRIMARY_EDGE_GATE_NOT_PASSED')
    assets=m.get('assets',[])
    if not assets: raise SystemExit('NO_ASSETS')
    out=Path(a.out_dir); out.mkdir(parents=True,exist_ok=True)
    rows=[]
    for x in assets:
        sym=x['symbol']; plan=x['plan']; tick=x['tickSize']; lot=x['lotSize']
        for feed in FEEDS:
            data=x['l2Data'] if feed=='L2_ONLY' else x['fusedData']
            if not Path(data).exists(): raise SystemExit(f'MISSING_CANONICAL_DATA:{data}')
            for q in QUEUES:
                for lm in LAT:
                    f=out/f'{sym}_{feed}_{q}_L{lm}.json'
                    cmd=[sys.executable,'research/execution/run_hft_candidate.py',
                         '--data',data,'--plan',plan,'--symbol',sym,'--feed',feed,'--queue',q,
                         '--latency-ns',str(a.base_latency_ns),'--latency-multiplier',str(lm),
                         '--tick-size',str(tick),'--lot-size',str(lot),'--fee-rate',str(a.fee_rate),
                         '--output',str(f)]
                    subprocess.run(cmd,check=True)
                    r=json.load(open(f)); r.pop('details',None); rows.append(r)
    scenarios=out/'scenarios.json'; json.dump(rows,open(scenarios,'w'),indent=2)
    verdict=out/'verdict.json'
    rc=subprocess.run([sys.executable,'research/execution/compare_hft_scenarios.py',str(scenarios),str(verdict)])
    print(json.dumps({'scenarioCount':len(rows),'verdict':str(verdict),'comparatorExit':rc.returncode},indent=2))
    raise SystemExit(rc.returncode)

if __name__=='__main__': main()
