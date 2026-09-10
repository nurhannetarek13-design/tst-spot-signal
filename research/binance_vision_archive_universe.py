#!/usr/bin/env python3
"""Research-only Binance Spot archive universe audit.

Builds the symbol universe from Binance Vision's public S3 index instead of the
current exchange listing, then compares it with current Spot exchangeInfo.
This is infrastructure for removing current-listing survivorship bias from
strategy validation. It does not authorize or change live trading.
"""
from __future__ import annotations

import json
import pathlib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
API = "https://data-api.binance.vision"
PREFIX = "data/spot/monthly/klines/"
OUT = pathlib.Path("validation/edges/binance-vision-archive-universe.json")
UA = "tst-archive-universe-audit/1.0"
EXCLUDED_BASES = {
    "USDC","FDUSD","TUSD","USDP","DAI","BUSD","EUR","AEUR","TRY","BRL",
    "GBP","AUD","USD1","RLUSD","USDE","PAXG","XAUT","U",
}


def get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def get_json(url: str):
    return json.loads(get_bytes(url))


def archive_symbols() -> list[str]:
    symbols: set[str] = set()
    token = None
    while True:
        params = {
            "list-type": "2",
            "prefix": PREFIX,
            "delimiter": "/",
            "max-keys": "1000",
        }
        if token:
            params["continuation-token"] = token
        url = S3 + "?" + urllib.parse.urlencode(params)
        root = ET.fromstring(get_bytes(url))
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for el in root.findall("s3:CommonPrefixes/s3:Prefix", ns):
            p = (el.text or "").strip()
            if p.startswith(PREFIX):
                sym = p[len(PREFIX):].strip("/")
                if sym:
                    symbols.add(sym)
        truncated = (root.findtext("s3:IsTruncated", default="false", namespaces=ns) or "false").lower() == "true"
        token = root.findtext("s3:NextContinuationToken", default=None, namespaces=ns)
        if not truncated or not token:
            break
    return sorted(symbols)


def current_spot() -> dict[str, dict]:
    info = get_json(API + "/api/v3/exchangeInfo")
    out = {}
    for s in info.get("symbols", []):
        if s.get("quoteAsset") != "USDT" or not s.get("isSpotTradingAllowed"):
            continue
        out[str(s["symbol"])] = {
            "status": s.get("status"),
            "baseAsset": s.get("baseAsset"),
            "quoteAsset": s.get("quoteAsset"),
        }
    return out


def base_from_usdt_symbol(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else ""


def tradable_research_symbol(symbol: str) -> bool:
    base = base_from_usdt_symbol(symbol)
    if not base or base in EXCLUDED_BASES:
        return False
    return not base.endswith(("UP","DOWN","BULL","BEAR"))


def main() -> None:
    archived_all = archive_symbols()
    archived_usdt = sorted(s for s in archived_all if tradable_research_symbol(s))
    current = current_spot()
    current_research = sorted(s for s, meta in current.items() if meta.get("status") == "TRADING" and tradable_research_symbol(s))
    archive_set = set(archived_usdt)
    current_set = set(current_research)
    archived_only = sorted(archive_set - current_set)
    current_without_monthly_archive = sorted(current_set - archive_set)
    overlap = sorted(archive_set & current_set)

    payload = {
        "engine": "BINANCE_VISION_ARCHIVE_UNIVERSE_AUDIT",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "source": {
            "archiveIndex": S3,
            "archivePrefix": PREFIX,
            "currentMarketData": API + "/api/v3/exchangeInfo",
        },
        "counts": {
            "archiveAllSymbols": len(archived_all),
            "archiveResearchUSDT": len(archived_usdt),
            "currentResearchUSDT": len(current_research),
            "overlap": len(overlap),
            "archivedOnly": len(archived_only),
            "currentWithoutMonthlyArchive": len(current_without_monthly_archive),
        },
        "archivedOnlyUSDT": archived_only,
        "currentWithoutMonthlyArchive": current_without_monthly_archive,
        "archiveResearchUSDT": archived_usdt,
        "currentResearchUSDT": current_research,
        "nextGate": "Use archive-derived symbols plus point-in-time liquidity in OOS validation; do not promote from current-listing-only tests.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"engine": payload["engine"], **payload["counts"]}, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
