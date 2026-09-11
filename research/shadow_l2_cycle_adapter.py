#!/usr/bin/env python3
"""Attach Gate G L2 as a research-only veto to frozen Gate A shadow decisions.

Control is copied from the existing shadow trader. Treatment can only keep or
veto an existing SHADOW_BUY. It can never promote a SKIP and never places an
order. L2/network failures fail closed for Treatment and leave Control intact.
"""
from __future__ import annotations

import csv
import json
import pathlib
import time

from research.shadow_l2_ablation import FIELDS, apply

SRC = pathlib.Path("validation/pro_stack/shadow_decisions.json")
OUT = pathlib.Path("validation/pro_stack/shadow_l2_ablation.csv")
SUMMARY = pathlib.Path("validation/pro_stack/shadow_l2_ablation_summary.json")


def adapt(row: dict, generated_at: int) -> dict:
    return {
        "ts": generated_at,
        "symbol": row["symbol"],
        "decision": row["action"],
        "reason": row.get("reason", ""),
        "p_tp_before_sl": row.get("pTpBeforeSl", ""),
        # Preserve the model's native fractional edge; do not rescale it here.
        "expected_net_pct": row.get("expectedNetEdge", ""),
        "entry_price": row.get("entryPrice", ""),
        "tp_pct": row.get("takeProfitPct", ""),
        "sl_pct": row.get("hardStopPct", ""),
    }


def l2_error_row(base: dict, exc: Exception) -> dict:
    out = {k: "" for k in FIELDS}
    out.update(
        {
            "ts": base.get("ts", ""),
            "symbol": base.get("symbol", ""),
            "base_decision": base.get("decision", "SKIP"),
            "base_reason": base.get("reason", ""),
            "control_decision": base.get("decision", "SKIP"),
            "treatment_decision": "SKIP",
            "treatment_reason": "L2_UNAVAILABLE",
            "p_tp_before_sl": base.get("p_tp_before_sl", ""),
            "expected_net_pct": base.get("expected_net_pct", ""),
            "entry_price": base.get("entry_price", ""),
            "tp_pct": base.get("tp_pct", ""),
            "sl_pct": base.get("sl_pct", ""),
        }
    )
    # Error text is deliberately not written into the stable CSV schema.
    print(json.dumps({"l2_warning": type(exc).__name__, "symbol": base.get("symbol", "")}))
    return out


def main() -> None:
    payload = json.loads(SRC.read_text())
    assert payload["authorization"] == "RESEARCH_ONLY"
    assert payload["liveTrading"] is False

    generated_at = int(payload.get("generatedAt") or time.time())
    rows = []
    for source_row in payload["rows"]:
        base = adapt(source_row, generated_at)
        # Critical invariant: SKIP short-circuits inside apply(), so L2 cannot
        # originate a trade and no network request is needed for rejected rows.
        try:
            result = apply(base)
        except Exception as exc:
            if base["decision"] != "SHADOW_BUY":
                raise
            result = l2_error_row(base, exc)
        assert not (
            result["base_decision"] != "SHADOW_BUY"
            and result["treatment_decision"] == "SHADOW_BUY"
        ), result
        rows.append(result)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    base_buys = sum(r["base_decision"] == "SHADOW_BUY" for r in rows)
    confirms = sum(r["treatment_reason"] == "L2_CONFIRM" for r in rows)
    vetoes = sum(r["treatment_reason"] == "L2_VETO" for r in rows)
    unavailable = sum(r["treatment_reason"] == "L2_UNAVAILABLE" for r in rows)
    promoted = sum(
        r["base_decision"] != "SHADOW_BUY" and r["treatment_decision"] == "SHADOW_BUY"
        for r in rows
    )
    summary = {
        "engine": "GATE_G_SHADOW_AB_V1",
        "authorization": "RESEARCH_ONLY",
        "liveTrading": False,
        "sourceEngine": payload.get("engine"),
        "rows": len(rows),
        "baseShadowBuys": base_buys,
        "l2Confirms": confirms,
        "l2Vetoes": vetoes,
        "l2Unavailable": unavailable,
        "illegalPromotions": promoted,
        "invariantPass": promoted == 0,
        "generatedAt": int(time.time()),
    }
    assert summary["invariantPass"] is True
    assert confirms + vetoes + unavailable == base_buys
    SUMMARY.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
