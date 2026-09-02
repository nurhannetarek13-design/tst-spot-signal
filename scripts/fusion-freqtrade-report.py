#!/usr/bin/env python3
import json, sys, pathlib, math, datetime, zipfile, tempfile

def find_report_json(p):
    p=pathlib.Path(p)
    if p.suffix==".zip":
        td=tempfile.mkdtemp()
        with zipfile.ZipFile(p) as z: z.extractall(td)
        cands=list(pathlib.Path(td).rglob("*.json"))
    else:
        cands=[p]
    for c in cands:
        try:
            d=json.loads(c.read_text())
            if isinstance(d,dict) and ("strategy" in d or "strategy_comparison" in d):
                return d
        except Exception:
            pass
    raise RuntimeError(f"no freqtrade backtest json found in {p}")

def trades_for(d):
    s=(d.get("strategy") or {}).get("AdaptiveRegimeStrategy") or {}
    trades=s.get("trades") or []
    return trades

def metrics(trades):
    pnls=[float(t.get("profit_abs",0) or 0) for t in trades]
    gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0)
    eq=peak=dd=0.0
    for p in pnls:
        eq+=p; peak=max(peak,eq); dd=max(dd,peak-eq)
    n=len(pnls); net=sum(pnls)
    return {
      "trades":n,
      "wins":sum(1 for x in pnls if x>0),
      "profitFactor": (gp/gl if gl>0 else (999.0 if gp>0 else 0.0)),
      "expectancyUSDT": (net/n if n else 0.0),
      "netPnlUSDT":net,
      "maxDrawdownUSDT":dd
    }

if len(sys.argv)!=4:
    raise SystemExit("usage: parser BASE_ZIP STRESS_ZIP OUT_JSON")
base=metrics(trades_for(find_report_json(sys.argv[1])))
stress=metrics(trades_for(find_report_json(sys.argv[2])))
passed=(base["trades"]>=100 and stress["trades"]>=100 and base["profitFactor"]>=1.15 and stress["profitFactor"]>=1.0 and base["expectancyUSDT"]>0 and stress["expectancyUSDT"]>0)
out={
  "engine":"FREQTRADE",
  "strategyId":"TST_ADAPTIVE_FUSION_V1",
  "status":"PASS" if passed else "FAIL",
  "pass":passed,
  "base":base,
  "stress2x":stress,
  "generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),
  "notes":"Binance Spot historical data; 15m; fees included by Freqtrade."
}
pathlib.Path(sys.argv[3]).write_text(json.dumps(out,indent=2))
print(json.dumps(out,indent=2))
