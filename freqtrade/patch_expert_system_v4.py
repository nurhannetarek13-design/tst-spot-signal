from pathlib import Path

path=Path('/freqtrade/fast_entry_engine.py')
s=path.read_text(encoding='utf-8')
old='''def _expert_pre_ingest(payload: dict) -> bool:\n    ok, why = _portfolio_allows(payload)\n    lane = _lane_from_payload(payload)\n    symbol = str(payload.get('symbol') or '')\n    score = float(payload.get('score') or 0)\n    price = float(payload.get('entry') or 0)\n    if not ok:\n        print(f'[portfolio-risk] {symbol} BLOCKED lane={lane} reason={why}', flush=True)\n        _record_candidate(symbol, lane, score, price, 'REJECT', why)\n        return False\n    _record_candidate(symbol, lane, score, price, 'READY', why)\n    print(f'[portfolio-risk] {symbol} PASS lane={lane} {why}', flush=True)\n    return True\n'''
new='''def _expert_pre_ingest(payload: dict) -> bool:\n    ok, why = _portfolio_allows(payload)\n    lane = _lane_from_payload(payload)\n    symbol = str(payload.get('symbol') or '')\n    score = float(payload.get('score') or 0)\n    price = float(payload.get('entry') or 0)\n    telemetry = {\n        'target': payload.get('target'), 'stop': payload.get('stop'),\n        'stake_usdt': payload.get('stakeUSDT'), 'strategy': payload.get('strategy'),\n        'risk_pct': ((price-float(payload.get('stop') or price))/price) if price>0 else None,\n        'reward_pct': ((float(payload.get('target') or price)-price)/price) if price>0 else None,\n    }\n    if not ok:\n        print(f'[portfolio-risk] {symbol} BLOCKED lane={lane} reason={why}', flush=True)\n        _record_candidate(symbol, lane, score, price, 'REJECT', why, **telemetry)\n        return False\n    _record_candidate(symbol, lane, score, price, 'READY', why, **telemetry)\n    print(f'[portfolio-risk] {symbol} PASS lane={lane} {why}', flush=True)\n    return True\n'''
if old not in s:
    if "'target': payload.get('target')" in s:
        print('[expert-system-v4-patch] already applied')
    else:
        raise SystemExit('expert-v4: pre-ingest marker missing')
else:
    s=s.replace(old,new,1)
    compile(s,str(path),'exec')
    path.write_text(s,encoding='utf-8')
    print('[expert-system-v4-patch] OK target/stop/risk/reward telemetry added for path-aware outcome research')
