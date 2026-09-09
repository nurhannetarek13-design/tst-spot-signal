from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Shadow market context is deliberately telemetry-only until OOS validation.
if 'import market_context\n' not in s:
    marker = 'import trade_state\n'
    if marker not in s:
        raise SystemExit('expert-v5: trade_state import marker missing')
    s = s.replace(marker, marker + 'import market_context\n', 1)

old = """        row = {\n            'event_id': f'{symbol}-{lane}-{decision}-{int(now*1000)}',\n            'ts': now, 'symbol': symbol, 'lane': lane,\n            'score': round(float(score), 3), 'price': float(price),\n            'decision': decision, 'reason': reason, **extra,\n        }\n"""
new = """        ctx = market_context.symbol_context(symbol)\n        row = {\n            'event_id': f'{symbol}-{lane}-{decision}-{int(now*1000)}',\n            'ts': now, 'symbol': symbol, 'lane': lane,\n            'score': round(float(score), 3), 'price': float(price),\n            'decision': decision, 'reason': reason,\n            'regime': ctx.get('regime'),\n            'regime_trade_permission_shadow': ctx.get('trade_permission_shadow'),\n            'breadth_1h': ctx.get('breadth_1h'),\n            'breadth_4h': ctx.get('breadth_4h'),\n            'universe_n': ctx.get('universe_n'),\n            'rs_vs_btc_4h': ctx.get('rs_vs_btc_4h'),\n            'r1h_pct_rank': ctx.get('r1h_pct_rank'),\n            'r4h_pct_rank': ctx.get('r4h_pct_rank'),\n            'rs_btc4h_pct_rank': ctx.get('rs_btc4h_pct_rank'),\n            'accel1h_pct_rank': ctx.get('accel1h_pct_rank'),\n            'volume_expansion_pct_rank': ctx.get('volume_expansion_pct_rank'),\n            'liquidity_pct_rank': ctx.get('liquidity_pct_rank'),\n            'opportunity_pct_shadow': ctx.get('opportunity_pct_shadow'),\n            'context_generated_at': ctx.get('context_generated_at'),\n            **extra,\n        }\n"""
if old in s:
    s = s.replace(old, new, 1)
elif "'opportunity_pct_shadow': ctx.get('opportunity_pct_shadow')" not in s:
    raise SystemExit('expert-v5: candidate telemetry marker missing')

pre_old = """    telemetry = {\n        'target': payload.get('target'), 'stop': payload.get('stop'),\n        'stake_usdt': payload.get('stakeUSDT'), 'strategy': payload.get('strategy'),\n        'risk_pct': ((price-float(payload.get('stop') or price))/price) if price>0 else None,\n        'reward_pct': ((float(payload.get('target') or price)-price)/price) if price>0 else None,\n    }\n"""
pre_new = """    ctx = market_context.symbol_context(symbol)\n    telemetry = {\n        'target': payload.get('target'), 'stop': payload.get('stop'),\n        'stake_usdt': payload.get('stakeUSDT'), 'strategy': payload.get('strategy'),\n        'risk_pct': ((price-float(payload.get('stop') or price))/price) if price>0 else None,\n        'reward_pct': ((float(payload.get('target') or price)-price)/price) if price>0 else None,\n    }\n    if ctx:\n        print(f\"[opportunity-shadow] {symbol} regime={ctx.get('regime')} rsBTCpct={ctx.get('rs_btc4h_pct_rank')} oppPct={ctx.get('opportunity_pct_shadow')} breadth4h={ctx.get('breadth_4h')}\", flush=True)\n"""
if pre_old in s:
    s = s.replace(pre_old, pre_new, 1)
elif '[opportunity-shadow]' not in s:
    raise SystemExit('expert-v5: v4 telemetry marker missing')

startup_marker = "    last_chat_retry = 0.0\n"
startup = "    print('[expert-system-v5] ONLINE regime=SHADOW cross_sectional_RS=SHADOW live_gate=UNCHANGED research_context=PERSISTENT')\n"
if '[expert-system-v5] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('expert-v5: startup marker missing')
    s = s.replace(startup_marker, startup + startup_marker, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[expert-system-v5-patch] OK regime + breadth + cross-sectional RS attached to outcome telemetry; no unvalidated live gate enabled')
