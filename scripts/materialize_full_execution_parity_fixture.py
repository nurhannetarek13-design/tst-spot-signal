#!/usr/bin/env python3
"""Enrich the canonical execution parity fixture with the full audit trail.

This deliberately imports and executes build_execution_parity_fixture.py unchanged,
then adds the complete signal/trade arrays from that same in-memory run. It does not
reimplement strategy or execution semantics.
"""
import importlib.util
import json
import pathlib

BUILDER = pathlib.Path(__file__).with_name("build_execution_parity_fixture.py")
spec = importlib.util.spec_from_file_location("execution_parity_builder", BUILDER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

out = dict(mod.out)
if out.get("status") == "READY":
    signal_rows = list(getattr(mod, "signal_rows", []))
    base_trades = list(getattr(mod, "base_trades", []))
    stress_trades = list(getattr(mod, "stress_trades", []))

    out.setdefault("signals", {})["all"] = signal_rows
    out["baseTrades"] = base_trades
    out["stressTrades"] = stress_trades
    out["auditTrail"] = {
        "fullSignalsStored": len(signal_rows),
        "fullBaseTradesStored": len(base_trades),
        "fullStressTradesStored": len(stress_trades),
        "strictParityReady": (
            len(base_trades) == int(out.get("base", {}).get("trades", -1))
            and len(stress_trades) == int(out.get("stress2x", {}).get("trades", -1))
        ),
    }

mod.OUT.write_text(json.dumps(out, indent=2))
print(json.dumps({
    "status": out.get("status"),
    "candidateFingerprint": out.get("candidateFingerprint"),
    "datasetSha256": out.get("dataset", {}).get("sha256"),
    "signals": len(out.get("signals", {}).get("all", [])),
    "baseTrades": len(out.get("baseTrades", [])),
    "stressTrades": len(out.get("stressTrades", [])),
    "strictParityReady": out.get("auditTrail", {}).get("strictParityReady", False),
}, indent=2))
