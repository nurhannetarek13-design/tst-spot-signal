from pathlib import Path

engine_path = Path('/freqtrade/fast_entry_engine.py')
context_path = Path('/freqtrade/market_context.py')
s = engine_path.read_text(encoding='utf-8')
c = context_path.read_text(encoding='utf-8')

# ---------------------------------------------------------------------------
# 1) Persist an actual point-in-time 1..N opportunity rank.
# ---------------------------------------------------------------------------
rank_marker = "    breadth1 = sum(1 for r in feats if float(r['r1h']) > 0) / len(feats)\n"
rank_block = """    opportunity_sorted = sorted(feats, key=lambda r: float(r.get('opportunity_pct_shadow') or 0.0), reverse=True)
    for idx, r in enumerate(opportunity_sorted, 1):
        r['opportunity_rank'] = idx

"""
if "r['opportunity_rank'] = idx" not in c:
    if rank_marker not in c:
        raise SystemExit('spot-sniper-v1: market rank insertion marker missing')
    c = c.replace(rank_marker, rank_block + rank_marker, 1)

ctx_marker = "        'opportunity_pct_shadow': r.get('opportunity_pct_shadow'),\n"
if "'opportunity_rank': r.get('opportunity_rank')" not in c:
    if ctx_marker not in c:
        raise SystemExit('spot-sniper-v1: symbol-context marker missing')
    c = c.replace(ctx_marker, ctx_marker + "        'opportunity_rank': r.get('opportunity_rank'),\n", 1)

c = c.replace("'method': 'HEURISTIC_SHADOW_V1_NOT_LIVE_GATE'", "'method': 'POINT_IN_TIME_CROSS_SECTIONAL_CONTEXT_V2'", 1)
compile(c, str(context_path), 'exec')
context_path.write_text(c, encoding='utf-8')

# ---------------------------------------------------------------------------
# 2) Central Spot Sniper gate immediately before the existing execution path.
#    Existing portfolio/API/OCO hard gates remain hard and cannot be rescued.
# ---------------------------------------------------------------------------
import_marker = 'import market_context\n'
if 'import spot_sniper_gate\n' not in s:
    if import_marker not in s:
        raise SystemExit('spot-sniper-v1: market_context import marker missing')
    s = s.replace(import_marker, import_marker + 'import spot_sniper_gate\n', 1)

legacy_sig = 'def _expert_pre_ingest(payload: dict) -> bool:\n'
if 'def _legacy_expert_pre_ingest(payload: dict)' not in s:
    if legacy_sig not in s:
        raise SystemExit('spot-sniper-v1: expert pre-ingest marker missing')
    s = s.replace(legacy_sig, 'def _legacy_expert_pre_ingest(payload: dict) -> bool:\n', 1)

insert_marker = '\ndef validate_and_resolve_telegram_chat() -> bool:\n'
wrapper = r'''

def _expert_pre_ingest(payload: dict) -> bool:
    lane = _lane_from_payload(payload)
    symbol = str(payload.get('symbol') or '')
    score = float(payload.get('score') or 0.0)
    price = float(payload.get('entry') or 0.0)
    decision = spot_sniper_gate.evaluate(payload)
    payload['sniperGate'] = decision
    ev = decision.get('ev') or {}
    try:
        risk_pct = ((price - float(payload.get('stop') or price)) / price) if price > 0 else None
        reward_pct = ((float(payload.get('target') or price) - price) / price) if price > 0 else None
    except Exception:
        risk_pct = reward_pct = None
    telemetry = {
        'target': payload.get('target'), 'stop': payload.get('stop'),
        'stake_usdt': payload.get('stakeUSDT'), 'strategy': payload.get('strategy'),
        'risk_pct': risk_pct, 'reward_pct': reward_pct,
        'opportunity_rank': decision.get('opportunity_rank'),
        'prob_tp_before_sl': ev.get('prob_tp_before_sl'),
        'expected_net_pct': ev.get('expected_net_pct'),
        'expected_mfe_pct': ev.get('expected_mfe_pct'),
        'expected_mae_pct': ev.get('expected_mae_pct'),
        'expected_holding_min': ev.get('expected_holding_min'),
        'ev_gate_status': ev.get('status'),
        'sniper_gate_status': decision.get('status'),
    }
    if not bool(decision.get('passed')):
        why = 'sniper-' + str(decision.get('reason') or decision.get('status') or 'reject')
        print(f'[spot-sniper] {symbol} BLOCKED lane={lane} reason={why}', flush=True)
        _record_candidate(symbol, lane, score, price, 'REJECT', why, **telemetry)
        return False

    ok, why = _portfolio_allows(payload)
    if not ok:
        print(f'[portfolio-risk] {symbol} BLOCKED lane={lane} reason={why}', flush=True)
        _record_candidate(symbol, lane, score, price, 'REJECT', why, **telemetry)
        return False

    _record_candidate(symbol, lane, score, price, 'READY', 'spot-sniper-pass|' + str(why), **telemetry)
    print(
        f"[spot-sniper] {symbol} PASS lane={lane} regime={decision.get('regime')} "
        f"rank={decision.get('opportunity_rank')} ev={ev.get('expected_net_pct')} "
        f"p={ev.get('prob_tp_before_sl')} mode={decision.get('status')} {why}", flush=True
    )
    return True

'''
if '[spot-sniper] ' not in s:
    if insert_marker not in s:
        raise SystemExit('spot-sniper-v1: wrapper insertion marker missing')
    s = s.replace(insert_marker, wrapper + insert_marker, 1)

# ---------------------------------------------------------------------------
# 3) Full scored-candidate telemetry, including candidates that never reach the
#    final threshold. _record_candidate already has a 60s dedupe guard.
# ---------------------------------------------------------------------------
replacements = [
    (
        "                    ranked.append((score, symbol, change, volume, m))\n",
        "                    ranked.append((score, symbol, change, volume, m))\n                    _record_candidate(symbol, 'NORMAL', score, float(m.get('live_price') or m.get('last') or 0), 'SCANNED', 'score-snapshot', volume_ratio=m.get('volume_ratio'), taker_buy_ratio=m.get('taker_buy_ratio'), spread_pct=m.get('spread_pct'), rsi=m.get('rsi'), change24=change, volume24=volume)\n",
    ),
    (
        "                    exp_ranked.append((escore, es, ech, ev))\n",
        "                    exp_ranked.append((escore, es, ech, ev))\n                    _record_candidate(es, 'EXPLOSIVE', escore, float(em.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=em.get('pullback'), reclaimed=em.get('reclaimed'), volume_ratio=em.get('volx'), taker_buy_ratio=em.get('taker'), spread_pct=em.get('spread'), rsi=em.get('rsi'), change24=ech, volume24=ev)\n",
    ),
    (
        "                    extreme_ranked.append((xscore, xs, xch, xv))\n",
        "                    extreme_ranked.append((xscore, xs, xch, xv))\n                    _record_candidate(xs, 'EXTREME', xscore, float(xm.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=xm.get('pullback'), reclaimed=xm.get('reclaimed'), volume_ratio=xm.get('volx'), taker_buy_ratio=xm.get('taker'), spread_pct=xm.get('spread'), rsi=xm.get('rsi'), change24=xch, volume24=xv)\n",
    ),
    (
        "                    mid_ranked.append((mscore, ms, mch, mv))\n",
        "                    mid_ranked.append((mscore, ms, mch, mv))\n                    _record_candidate(ms, 'MID', mscore, float(mm.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=mm.get('pullback'), reclaimed=mm.get('reclaimed'), volume_ratio=mm.get('volx'), taker_buy_ratio=mm.get('taker'), spread_pct=mm.get('spread'), rsi=mm.get('rsi'), change24=mch, volume24=mv)\n",
    ),
]
for old, new in replacements:
    if new not in s:
        if old in s:
            s = s.replace(old, new, 1)
        else:
            print(f'[spot-sniper-v1] telemetry marker absent (lane may be build-patched differently): {old[:45]}', flush=True)

# Capture major post-score context failures explicitly when the exact markers exist.
optional = [
    (
        "        print(f'[explosive-context] {symbol} BLOCKED reason={why}'); return False\n",
        "        print(f'[explosive-context] {symbol} BLOCKED reason={why}'); _record_candidate(symbol, 'EXPLOSIVE', score, m['live'], 'REJECT', why); return False\n",
    ),
    (
        "        print(f'[mid-context] {symbol} BLOCKED reason={why}')\n        return False\n",
        "        print(f'[mid-context] {symbol} BLOCKED reason={why}')\n        _record_candidate(symbol, 'MID', score, m['live'], 'REJECT', why)\n        return False\n",
    ),
]
for old, new in optional:
    if new not in s and old in s:
        s = s.replace(old, new, 1)

startup_marker = "    last_chat_retry = 0.0\n"
startup = "    print(f'[spot-sniper-v1] ONLINE top_n={spot_sniper_gate.TOP_N} panic_block={spot_sniper_gate.PANIC_BLOCK} ev_mode={spot_sniper_gate.live_ev_gate.MODE} forward_validation=REQUIRED')\n"
if '[spot-sniper-v1] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('spot-sniper-v1: startup marker missing')
    s = s.replace(startup_marker, startup + startup_marker, 1)

for required in [
    'import spot_sniper_gate',
    'def _legacy_expert_pre_ingest(payload: dict)',
    'def _expert_pre_ingest(payload: dict)',
    "payload['sniperGate'] = decision",
    '[spot-sniper-v1] ONLINE',
]:
    if required not in s:
        raise SystemExit(f'spot-sniper-v1: required runtime marker missing: {required}')

compile(s, str(engine_path), 'exec')
engine_path.write_text(s, encoding='utf-8')
print('[spot-sniper-v1-patch] OK centralized regime/rank/EV gate + full scored-candidate audit telemetry wired')
