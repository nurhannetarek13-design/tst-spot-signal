#!/usr/bin/env python3
"""Parse Freqtrade backtest exports from an external-strategy tournament.

Research-only. This does not authorize live trading.
Ranks strategies by robust net metrics and applies conservative minimum gates.
"""
from __future__ import annotations
import json, math, pathlib, statistics, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "tournament-results")
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "external-freqtrade-tournament-report.json")


def num(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def extract_strategy(payload, strategy_name):
    # Freqtrade exports have changed shape across releases; support common layouts.
    s = None
    if isinstance(payload, dict):
        if isinstance(payload.get("strategy"), dict):
            s = payload["strategy"].get(strategy_name)
            if s is None and len(payload["strategy"]) == 1:
                s = next(iter(payload["strategy"].values()))
        if s is None and isinstance(payload.get("strategy_comparison"), list):
            for row in payload["strategy_comparison"]:
                if row.get("key") == strategy_name or row.get("strategy_name") == strategy_name:
                    s = row
                    break
    return s if isinstance(s, dict) else {}


def normalize(name, src, payload):
    s = extract_strategy(payload, name)
    trades = int(num(s.get("trades") or s.get("total_trades"), 0) or 0)
    profit_pct = num(s.get("profit_total_pct"))
    if profit_pct is None:
        pr = num(s.get("profit_total"))
        profit_pct = pr * 100 if pr is not None else None
    max_dd = num(s.get("max_drawdown_account") or s.get("max_drawdown"))
    if max_dd is not None and abs(max_dd) <= 1.0:
        max_dd *= 100
    winrate = num(s.get("winrate") or s.get("win_rate"))
    if winrate is not None and winrate <= 1.0:
        winrate *= 100
    pf = num(s.get("profit_factor"))
    expectancy = num(s.get("expectancy"))
    sharpe = num(s.get("sharpe"))
    return {
        "strategy": name,
        "source": src,
        "trades": trades,
        "netProfitPct": profit_pct,
        "maxDrawdownPct": max_dd,
        "winRatePct": winrate,
        "profitFactor": pf,
        "expectancy": expectancy,
        "sharpe": sharpe,
    }

rows, failures = [], []
for meta in sorted(ROOT.glob("*.meta.json")):
    m = json.loads(meta.read_text())
    name, src = m["strategy"], m["source"]
    result_path = ROOT / m.get("result_file", "")
    if m.get("status") != "ok" or not result_path.exists():
        failures.append({"strategy": name, "source": src, "reason": m.get("reason", "failed")})
        continue
    try:
        payload = json.loads(result_path.read_text())
        rows.append(normalize(name, src, payload))
    except Exception as exc:
        failures.append({"strategy": name, "source": src, "reason": f"parse:{type(exc).__name__}:{exc}"})

# Screening gate, intentionally not tuned per strategy.
for r in rows:
    r["gate"] = {
        "minTrades": r["trades"] >= 30,
        "netPositive": r["netProfitPct"] is not None and r["netProfitPct"] > 0,
        "pfAtLeast1_2": r["profitFactor"] is not None and r["profitFactor"] >= 1.2,
        "ddBelow20": r["maxDrawdownPct"] is not None and r["maxDrawdownPct"] < 20,
    }
    r["screenPass"] = all(r["gate"].values())

# Score only for ordering; gate remains authoritative.
def score(r):
    p = r["netProfitPct"] or -999
    pf = r["profitFactor"] or 0
    dd = abs(r["maxDrawdownPct"] or 100)
    wr = r["winRatePct"] or 0
    return (1000 if r["screenPass"] else 0) + p + 10 * min(pf, 5) - dd + 0.05 * wr

rows.sort(key=score, reverse=True)
report = {
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "screeningGate": {"minTrades":30,"netProfit":">0%","profitFactor":">=1.2","maxDrawdown":"<20%"},
    "tested": len(rows),
    "failedOrIncompatible": len(failures),
    "screenPassCount": sum(1 for r in rows if r["screenPass"]),
    "ranking": rows,
    "failures": failures,
    "nextStep": "Only screen-pass strategies may enter longer OOS/cost/stress/cross-engine validation.",
}
OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"kind":"external_freqtrade_tournament_complete","tested":len(rows),"failed":len(failures),"passed":report["screenPassCount"],"top":rows[:10]}, separators=(",",":")))
