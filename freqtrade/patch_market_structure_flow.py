from pathlib import Path

engine_path = Path('/freqtrade/fast_entry_engine.py')
s = engine_path.read_text(encoding='utf-8')

import_marker = 'import spot_sniper_gate\n'
if 'import market_structure_flow\n' not in s:
    if import_marker not in s:
        raise SystemExit('market-structure-flow-patch: spot sniper import marker missing')
    s = s.replace(import_marker, import_marker + 'import market_structure_flow\n', 1)

anchor = """    ok, why = _portfolio_allows(payload)\n    if not ok:\n"""
flow_block = r'''    flow = market_structure_flow.evaluate(symbol, payload, str(decision.get('regime') or ''))
    payload['marketStructureFlow'] = flow
    fm = flow.get('metrics') or {}
    telemetry.update({
        'flow_structure_status': flow.get('status'),
        'flow_structure_score': flow.get('quality_score'),
        'flow_structure_required': flow.get('required_score'),
        'flow_structure_reason': flow.get('reason'),
        'flow_taker_buy_ratio': fm.get('taker_buy_ratio'),
        'flow_delta_ratio': fm.get('delta_ratio'),
        'flow_depth_imbalance': fm.get('depth_imbalance'),
        'flow_micro_bias_bps': fm.get('micro_bias_bps'),
        'volume_profile_location': fm.get('profile_location'),
        'volume_profile_poc': fm.get('poc'),
        'volume_profile_val': fm.get('val'),
        'volume_profile_vah': fm.get('vah'),
        'wyckoff_phase': fm.get('phase'),
    })
    if not bool(flow.get('passed')):
        why_flow = 'market-structure-flow:' + str(flow.get('reason') or flow.get('status') or 'reject')
        print(
            f"[structure-flow] {symbol} BLOCKED lane={lane} regime={decision.get('regime')} "
            f"quality={flow.get('quality_score')} required={flow.get('required_score')} "
            f"phase={fm.get('phase')} profile={fm.get('profile_location')} "
            f"taker={fm.get('taker_buy_ratio')} delta={fm.get('delta_ratio')} "
            f"depth={fm.get('depth_imbalance')} reason={why_flow}", flush=True,
        )
        _record_candidate(symbol, lane, score, price, 'REJECT', why_flow, **telemetry)
        return False

    try:
        old_stake = float(payload.get('stakeUSDT') or 0.0)
        new_stake = float(flow.get('adjusted_stake_usdt') or old_stake)
        if old_stake > 0 and 0 < new_stake < old_stake:
            payload['stakeUSDT'] = new_stake
            telemetry['stake_usdt'] = new_stake
            telemetry['flow_original_stake_usdt'] = old_stake
    except Exception:
        pass
    print(
        f"[structure-flow] {symbol} PASS lane={lane} regime={decision.get('regime')} "
        f"quality={flow.get('quality_score')}/{flow.get('required_score')} "
        f"phase={fm.get('phase')} profile={fm.get('profile_location')} "
        f"taker={float(fm.get('taker_buy_ratio') or 0)*100:.1f}% "
        f"delta={float(fm.get('delta_ratio') or 0):+.3f} depth={float(fm.get('depth_imbalance') or 0):+.3f} "
        f"stake={payload.get('stakeUSDT')}", flush=True,
    )

    ok, why = _portfolio_allows(payload)
    if not ok:
'''

if '[structure-flow] ' not in s:
    # Target the final Spot Sniper wrapper, not the legacy expert function.
    wrapper_pos = s.find('def _expert_pre_ingest(payload: dict) -> bool:\n')
    if wrapper_pos < 0:
        raise SystemExit('market-structure-flow-patch: final expert wrapper missing')
    anchor_pos = s.find(anchor, wrapper_pos)
    if anchor_pos < 0:
        raise SystemExit('market-structure-flow-patch: portfolio anchor missing in final wrapper')
    s = s[:anchor_pos] + flow_block + s[anchor_pos + len(anchor):]

startup_marker = "    last_chat_retry = 0.0\n"
startup = (
    "    print(f'[market-structure-flow] ONLINE order_flow=depth20+taker_delta "
    "volume_profile=90m/70pct wyckoff=objective risk_quality_sizing=ON "
    "fail_closed={market_structure_flow.FLOW_FAIL_CLOSED}')\n"
)
if '[market-structure-flow] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('market-structure-flow-patch: startup marker missing')
    s = s.replace(startup_marker, startup + startup_marker, 1)

for marker in [
    'import market_structure_flow',
    "payload['marketStructureFlow'] = flow",
    '[structure-flow] ',
    '[market-structure-flow] ONLINE',
]:
    if marker not in s:
        raise SystemExit(f'market-structure-flow-patch: missing marker {marker}')

compile(s, str(engine_path), 'exec')
engine_path.write_text(s, encoding='utf-8')
print('[market-structure-flow-patch] OK order-flow + volume-profile + objective-Wyckoff + quality-aware risk sizing wired before live BUY')
