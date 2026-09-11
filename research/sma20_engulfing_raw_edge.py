import json, math, urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

SYMS=['BTCUSDT','ETHUSDT','SOLUSDT']; START=1704067200000; END=1788220800000; TF='15m'; LIMIT=1000
H={'1h':4,'2h':8,'4h':16,'8h':32}

def chunk(a,b):
    u=f'https://api.binance.com/api/v3/klines?symbol={b}&interval=15m&startTime={a}&limit=1000'
    with urllib.request.urlopen(u,timeout=20) as r:return json.loads(r.read())
def load(s):
    step=LIMIT*15*60*1000; starts=range(START,END,step)
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows=sum(ex.map(lambda a:chunk(a,s),starts),[])
    d={int(x[0]):x for x in rows if int(x[0])<END}; return [d[k] for k in sorted(d)]
def stats(v):
    if not v:return {'n':0}
    q=sorted(v); n=len(v); med=q[n//2] if n%2 else (q[n//2-1]+q[n//2])/2
    return {'n':n,'mean':sum(v)/n,'median':med,'hitRate':sum(x>0 for x in v)/n}
def run(s):
    x=load(s); o=[float(r[1]) for r in x]; h=[float(r[2]) for r in x]; l=[float(r[3]) for r in x]; c=[float(r[4]) for r in x]; ts=[int(r[0]) for r in x]
    sma=[]; z=0
    for i,v in enumerate(c):
        z+=v
        if i>=20:z-=c[i-20]
        sma.append(z/20 if i>=19 else math.nan)
    ev=[]
    for i in range(20,len(x)-33):
        bull=c[i]>o[i] and c[i-1]<o[i-1] and o[i]<=c[i-1] and c[i]>=o[i-1]
        if c[i]>sma[i] and bull:ev.append(i)
    split=ts[int(len(ts)*.7)]; out={}
    for name,pred in [('IS',lambda i:ts[i]<split),('OOS',lambda i:ts[i]>=split)]:
        ids=[i for i in ev if pred(i)]; out[name]={k:stats([(c[i+n]/c[i]-1) for i in ids]) for k,n in H.items()}
    return {'events':len(ev),'IS':out['IS'],'OOS':out['OOS']}
res={s:run(s) for s in SYMS}
qual=0
for s,r in res.items():
    good=sum(1 for k in H if r['OOS'][k].get('n',0)>=100 and r['OOS'][k]['mean']>0 and r['OOS'][k]['median']>0 and r['OOS'][k]['hitRate']>.5)
    if good>=2:qual+=1
out={'engine':'SMA20_BULLISH_ENGULFING_RAW_EDGE_V1','status':'PROMOTE_TO_EXECUTION_TEST' if qual>=2 else 'REJECT_RAW_EDGE','scope':'LONG_ONLY_PRICE_SETUP','symbols':SYMS,'timeframe':'15m','definition':{'sma':20,'entry':'close>SMA20 and bullish engulfing','videoSLPct':1.0,'videoTPPct':2.0,'note':'SL/TP not used in raw-edge gate'},'results':res,'qualifyingSymbols':qual,'passRawEdge':qual>=2,'nextStep':'TEST_1PCT_SL_2PCT_TP_WITH_FEES' if qual>=2 else 'DO_NOT_TUNE','authorization':'RESEARCH_ONLY','liveTrading':False,'generatedAt':datetime.now(timezone.utc).isoformat()}
open('validation/research/sma20-engulfing-raw-edge.json','w').write(json.dumps(out,indent=2)); print(json.dumps(out,indent=2))
