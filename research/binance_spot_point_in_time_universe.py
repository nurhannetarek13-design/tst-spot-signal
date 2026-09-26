#!/usr/bin/env python3
"""Build a point-in-time Binance Spot USDT universe from Binance Vision listings.

Input files are produced by a pinned archive-listing tool in CI:
- symbols.txt: every USDT Spot symbol present in the remote archive
- files.txt: monthly 15m kline ZIP paths for those symbols
- exchangeInfo.json: current Binance Spot metadata

The resulting month->symbols map lets historical validation use only symbols
that actually had archive data in that month, including symbols no longer
currently trading. Research-only; no trading actions.
"""
from __future__ import annotations
import argparse,json,pathlib,re
from collections import defaultdict

FILE_RE=re.compile(r"(?:^|/)([A-Z0-9_]+USDT)(?:/15m)?/[^/]*?-15m-(\d{4}-\d{2})\.zip$")

STABLE_BASES={"USDC","FDUSD","TUSD","USDP","DAI","BUSD","USD1","RLUSD","USDE"}
LEV_SUFFIXES=("UP","DOWN","BULL","BEAR")


def valid_symbol(s):
    s=str(s).strip().upper()
    if not s.endswith("USDT") or s=="USDT":return False
    base=s[:-4]
    return bool(base) and base not in STABLE_BASES and not base.endswith(LEV_SUFFIXES)


def build(symbol_lines,file_lines,exchange_info):
    archived=sorted({str(x).strip().upper() for x in symbol_lines if valid_symbol(x)})
    current={
        str(x.get("symbol") or "").upper()
        for x in (exchange_info.get("symbols") or [])
        if x.get("status")=="TRADING"
        and x.get("quoteAsset")=="USDT"
        and x.get("isSpotTradingAllowed") is True
        and valid_symbol(x.get("symbol"))
    }

    months=defaultdict(set)
    first={}
    last={}
    matched_files=0
    for raw in file_lines:
        path=str(raw).strip().split()[-1] if str(raw).strip() else ""
        m=FILE_RE.search(path)
        if not m:continue
        sym,month=m.groups()
        if not valid_symbol(sym):continue
        months[month].add(sym);matched_files+=1
        first[sym]=min(first.get(sym,month),month)
        last[sym]=max(last.get(sym,month),month)

    all_with_files=sorted(set(first))
    delisted=sorted(set(all_with_files)-current)
    current_archived=sorted(set(all_with_files)&current)
    month_map={m:sorted(v) for m,v in sorted(months.items())}

    return {
        "engine":"BINANCE_VISION_POINT_IN_TIME_SPOT_UNIVERSE_V1",
        "authorization":"RESEARCH_ONLY","liveTrading":False,
        "granularity":"month",
        "source":"data.binance.vision Spot monthly 15m archive listing + current /api/v3/exchangeInfo",
        "archiveSymbolsListed":len(archived),
        "historicalSymbolCount":len(all_with_files),
        "currentTradingArchivedCount":len(current_archived),
        "delistedSymbolCount":len(delisted),
        "matchedMonthlyFiles":matched_files,
        "delistedSymbols":delisted,
        "currentTradingSymbols":current_archived,
        "symbolLifetimes":{s:{"firstMonth":first[s],"lastArchiveMonth":last[s],"currentTrading":s in current} for s in all_with_files},
        "pointInTimeUniverseByMonth":month_map,
        "pointInTimeUniverse":bool(month_map and all_with_files),
        "delistedCoverage":bool(delisted),
        "note":"Month membership is based on actual archive-file presence, not today's exchangeInfo. This is designed to reduce survivorship bias."
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbols",type=pathlib.Path,required=True)
    ap.add_argument("--files",type=pathlib.Path,required=True)
    ap.add_argument("--exchange-info",type=pathlib.Path,required=True)
    ap.add_argument("--output",type=pathlib.Path,required=True)
    a=ap.parse_args()
    out=build(
        a.symbols.read_text().splitlines(),
        a.files.read_text().splitlines(),
        json.loads(a.exchange_info.read_text()),
    )
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(out,indent=2,sort_keys=True))
    print(json.dumps({
        "historicalSymbolCount":out["historicalSymbolCount"],
        "delistedSymbolCount":out["delistedSymbolCount"],
        "months":len(out["pointInTimeUniverseByMonth"]),
        "pointInTimeUniverse":out["pointInTimeUniverse"],
        "delistedCoverage":out["delistedCoverage"],
    },indent=2))


if __name__=="__main__":
    main()
