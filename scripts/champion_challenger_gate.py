"""Fail-closed Champion/Challenger promotion gate.

This tool never promotes automatically. It only determines whether a challenger
is eligible for manual review after OOS replay + shadow evidence.
"""

from __future__ import annotations
import argparse, json
from pathlib import Path


def evaluate(policy, replay, shadow):
    checks = {}

    def add(name, passed, actual=None, required=None):
        checks[name] = {
            "pass": bool(passed),
            "actual": actual,
            "required": required,
        }

    add("oos_trades",
        int(replay.get("trades", 0)) >= int(policy["min_oos_trades"]),
        replay.get("trades"), policy["min_oos_trades"])
    add("oos_symbols",
        int(replay.get("symbol_count", 0)) >= int(policy["min_oos_symbols"]),
        replay.get("symbol_count"), policy["min_oos_symbols"])
    add("profit_factor",
        float(replay.get("profit_factor", 0) or 0) >= float(policy["min_profit_factor"]),
        replay.get("profit_factor"), policy["min_profit_factor"])
    add("net_expectancy",
        float(replay.get("net_expectancy", 0) or 0) > float(policy["min_net_expectancy_usdt"]),
        replay.get("net_expectancy"), f">{policy['min_net_expectancy_usdt']}")
    add("max_drawdown",
        float(replay.get("max_drawdown", 1) or 1) <= float(policy["max_drawdown_fraction"]),
        replay.get("max_drawdown"), f"<={policy['max_drawdown_fraction']}")

    if policy.get("require_zero_lookahead_violations", True):
        add("lookahead",
            int(replay.get("lookahead_violations", 1)) == 0,
            replay.get("lookahead_violations"), 0)

    if policy.get("require_survivorship_bias_check", True):
        add("survivorship_bias",
            replay.get("survivorship_bias_checked") is True,
            replay.get("survivorship_bias_checked"), True)
        source=str(replay.get("universe_source") or "").upper()
        add("historical_universe",
            bool(source) and source not in {"CURRENT_ONLY","CURRENT_LISTINGS_ONLY","UNKNOWN"},
            replay.get("universe_source"), "historical universe including delisted/removed pairs")

    add("shadow_trades",
        int(shadow.get("trades", 0)) >= int(policy["min_shadow_trades"]),
        shadow.get("trades"), policy["min_shadow_trades"])
    add("shadow_expectancy",
        float(shadow.get("net_expectancy", 0) or 0) > float(policy["min_shadow_net_expectancy_usdt"]),
        shadow.get("net_expectancy"), f">{policy['min_shadow_net_expectancy_usdt']}")
    add("shadow_safety_incidents",
        int(shadow.get("safety_incidents", 999999)) <= int(policy["max_shadow_safety_incidents"]),
        shadow.get("safety_incidents"), policy["max_shadow_safety_incidents"])

    eligible = all(x["pass"] for x in checks.values())
    return {
        "promotion_eligible_for_manual_review": eligible,
        "auto_promote": False,
        "decision": "MANUAL_REVIEW_ALLOWED" if eligible else "CHALLENGER_REJECTED_OR_MORE_EVIDENCE_REQUIRED",
        "checks": checks,
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--registry", default="validation/champion_challenger.json")
    ap.add_argument("--replay", required=True)
    ap.add_argument("--shadow", required=True)
    ap.add_argument("--out", required=True)
    args=ap.parse_args()

    registry=json.loads(Path(args.registry).read_text())
    replay=json.loads(Path(args.replay).read_text())
    shadow=json.loads(Path(args.shadow).read_text())
    result=evaluate(registry["promotion_policy"], replay, shadow)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps(result, indent=2))


if __name__=="__main__":
    main()
