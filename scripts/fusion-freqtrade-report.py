#!/usr/bin/env python3
import json, sys, pathlib, datetime, zipfile, tempfile

MANIFEST=pathlib.Path("validation/fusion/frozen-parity-candidate.json")
FIXTURE=pathlib.Path("validation/fusion/execution-parity-fixture.json")

def find_report_json(p):
    p=pathlib.Path(p)
    if p.suffix==".zip":
        td=tempfile.mkdtemp()
        with zipfile.ZipFile(p) as z:z.extractall(td)
        cands=list(pathlib.Path(td).rglob("*.json"))
    else:cands=[p]
    for c in cands:
        try:
            d=json.loads(c.read_text())
            if isinstance(d,dict) and ("strategy" in d or "strategy_comparison" in d):return d
        except Exception:pass
    raise RuntimeError(f"no freqtrade backtest json found in {p}")

def trades_for(d):
    strategies=d.get("strategy") or {}
    s=strategies.get("UnifiedCandidateStrategy") or strategies.get("AdaptiveRegimeStrategy") or {}
    return s.get("trades") or []

def metrics(trades):
    p=[float(t.get("profit_abs",0) or 0) for t in trades]
    gp=sum(x for x in p if x>0); gl=-sum(x for x in p if x<0)
    eq=peak=dd=0.0
    for x in p:eq+=x;peak=max(peak,eq);dd=max(dd,peak-eq)
    n=len(p);net=sum(p)
    return {"trades":n,"wins":sum(1 for x in p if x>0),"winRate":sum(1 for x in p if x>0)/n if n else 0.0,"profitFactor":gp/gl if gl>0 else (999.0 if gp>0 else 0.0),"expectancyUSDT":net/n if n else 0.0,"netPnlUSDT":net,"maxDrawdownUSDT":dd}

def norm_ts(v):
    if not v:return None
    s=str(v).replace(" ","T")
    if s.endswith("Z"):s=s[:-1]+"+00:00"
    try:
        dt=datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:dt=dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).isoformat()
    except Exception:return str(v)

def ft_trade(t):
    return {"entryTs":norm_ts(t.get("open_date") or t.get("open_date_utc")),"exitTs":norm_ts(t.get("close_date") or t.get("close_date_utc")),"entryPrice":t.get("open_rate"),"exitPrice":t.get("close_rate"),"reason":t.get("exit_reason"),"pnlUSDT":t.get("profit_abs")}

def parity_diag(ft, fx):
    canon=fx.get("baseTrades") or fx.get("baseTradesFirst50") or []
    f=[ft_trade(t) for t in ft]
    first=None
    for i,(a,b) in enumerate(zip(canon,f)):
        if norm_ts(a.get("entryTs")) != b.get("entryTs"):
            first={"index":i,"type":"ENTRY_TIMESTAMP","canonical":a,"freqtrade":b};break
        if norm_ts(a.get("exitTs")) != b.get("exitTs"):
            first={"index":i,"type":"EXIT_TIMESTAMP","canonical":a,"freqtrade":b};break
    if first is None and len(f)!=fx.get("base",{}).get("trades"):
        first={"index":min(len(canon),len(f)),"type":"TRADE_COUNT","canonicalTotal":fx.get("base",{}).get("trades"),"freqtradeTotal":len(f)}
    return {
        "mode":"DIAGNOSTIC_ONLY",
        "canonicalTotal":fx.get("base",{}).get("trades"),
        "freqtradeTotal":len(f),
        "deltaTrades":len(f)-int(fx.get("base",{}).get("trades",0)),
        "firstDivergence":first,
        "normalizedTrades":f,
        "freqtradeFirst50":f[:50],
        "note":"Native Freqtrade execution is an independent validator. These diagnostics expose differences but are not a canonical PARITY_PASS claim."
    }

if len(sys.argv)!=4:raise SystemExit("usage: parser BASE_ZIP STRESS_ZIP OUT_JSON")
m=json.loads(MANIFEST.read_text())
fx=json.loads(FIXTURE.read_text())
base_trades=trades_for(find_report_json(sys.argv[1])); stress_trades=trades_for(find_report_json(sys.argv[2]))
base=metrics(base_trades);stress=metrics(stress_trades)
independent=base["trades"]>=30 and base["profitFactor"]>=1.15 and stress["profitFactor"]>=1.0 and base["expectancyUSDT"]>0 and stress["expectancyUSDT"]>0
passed=independent and base["trades"]>=100 and stress["trades"]>=100
out={
    "engine":"FREQTRADE",
    "validationMode":"NATIVE_INDEPENDENT",
    "canonicalParityStatus":"NOT_APPLICABLE_TO_NATIVE_EXECUTION",
    "strategyId":"TST_CANDIDATE_FREQTRADE_VALIDATOR_V1",
    "status":"PASS" if passed else "FAIL",
    "pass":passed,
    "independentEnginePass":independent,
    "candidateId":m.get("candidateId"),
    "candidateFingerprint":m.get("candidateFingerprint"),
    "symbol":m.get("symbol"),
    "family":m.get("family"),
    "timeframe":m.get("timeframe"),
    "dataset":{"firstTs":fx["dataset"]["firstTs"],"lastTs":fx["dataset"]["lastTs"],"sha256":fx["dataset"]["sha256"]},
    "base":base,
    "stress2x":stress,
    "parityDiagnostics":parity_diag(base_trades,fx),
    "authorization":"RESEARCH_ONLY",
    "liveTrading":False,
    "generatedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "notes":"Independent native Freqtrade Spot backtest of the frozen candidate. Canonical execution parity is validated separately by the canonical adapter and strict parity CI."
}
pathlib.Path(sys.argv[3]).write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
