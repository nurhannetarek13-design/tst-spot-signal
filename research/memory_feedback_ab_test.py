"""Leakage-safe Raw-vs-Memory A/B harness for candidate research.

This implements the useful architecture from the AI-bot guide without pretending
that a markdown journal is ML. Memory decisions are learned ONLY from completed
prior trades in chronological order. No live trading and no future-data access.

Input JSONL schema (one candidate per line):
{
  "ts": 123, "symbol":"BTCUSDT", "setup":"...", "regime":"trend",
  "rvol_bucket":"high", "flow_bucket":"buy", "spread_bucket":"tight",
  "net_return": 0.01
}

Raw accepts every candidate. Memory accepts a candidate only after its exact
context has >= MIN_HISTORY completed prior observations and historical expectancy
and hit-rate pass fixed gates. Results compare Raw vs Memory on the same stream.
"""
import argparse, json
from collections import defaultdict

MIN_HISTORY=30
MIN_HIT=0.52
MIN_EXPECTANCY=0.0
KEYS=("symbol","setup","regime","rvol_bucket","flow_bucket","spread_bucket")

def key(x): return tuple(str(x.get(k,"NA")) for k in KEYS)
def metrics(xs):
    if not xs:return {"n":0,"expectancy":None,"hitRate":None,"netReturn":0.0}
    return {"n":len(xs),"expectancy":sum(xs)/len(xs),"hitRate":sum(v>0 for v in xs)/len(xs),"netReturn":sum(xs)}

def run(rows):
    rows=sorted(rows,key=lambda x:x["ts"]); hist=defaultdict(list); raw=[]; mem=[]; skipped=0
    decisions=[]
    for x in rows:
        r=float(x["net_return"]); h=hist[key(x)]
        raw.append(r)
        allow=len(h)>=MIN_HISTORY and (sum(h)/len(h))>MIN_EXPECTANCY and (sum(v>0 for v in h)/len(h))>=MIN_HIT
        if allow: mem.append(r)
        else: skipped+=1
        decisions.append({"ts":x["ts"],"symbol":x.get("symbol"),"memoryN":len(h),"memoryAllow":allow})
        h.append(r)  # update only AFTER decision => no lookahead
    a,b=metrics(raw),metrics(mem)
    improved=(b["n"]>=100 and b["expectancy"] is not None and a["expectancy"] is not None and b["expectancy"]>a["expectancy"] and b["netReturn"]>a["netReturn"])
    return {"engine":"MEMORY_FEEDBACK_AB_V1","raw":a,"memory":b,"skipped":skipped,"improved":improved,
            "rules":{"minHistory":MIN_HISTORY,"minHitRate":MIN_HIT,"minHistoricalExpectancy":MIN_EXPECTANCY,"contextKeys":KEYS},
            "authorization":"RESEARCH_ONLY","liveTrading":False,"note":"Memory is a causal filter, not an LLM claim and not live authorization."}

def main():
    p=argparse.ArgumentParser();p.add_argument("input");p.add_argument("output");a=p.parse_args()
    rows=[json.loads(z) for z in open(a.input) if z.strip()]
    out=run(rows);open(a.output,"w").write(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
if __name__=='__main__':main()
