from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'def _signal_visibility_alert(' in s:
    print('[signal-visibility-patch] already applied')
    raise SystemExit(0)

# Visibility is deliberately separate from authorization. WATCH / BLOCKED alerts
# can be disabled entirely. Confirmed BUY remains behind every existing
# execution, quality, regime, EV, portfolio, OCO and user-confirmation gate.
insert_marker = '\ndef _watch_live_price(m: dict) -> float:\n'
helper = r'''

_signal_visibility_last: dict[str, float] = {}


def _signal_visibility_alert(
    symbol: str,
    score: float,
    state: str,
    *,
    price: float = 0.0,
    reason: str = '',
    regime: str = '',
) -> None:
    if str(os.getenv('FAST_VISIBILITY_ENABLED', '1')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
        return
    now = time.time()
    key = f'{state}:{symbol}'
    cooldown = int(os.getenv('FAST_VISIBILITY_COOLDOWN_SEC', '600'))
    if now - float(_signal_visibility_last.get(key, 0.0)) < cooldown:
        return

    pair = f'{symbol[:-4]}/USDT' if symbol.endswith('USDT') else symbol
    if state == 'WATCH':
        text = (
            f'👀 WATCH — {pair}\n'
            f'Score: {score:.0f}/{DIRECT_SCORE:.0f}\n'
            f'Price ≈ {price:.8g}\n\n'
            'البوت بيراقب التأكيد دلوقتي. دي مش BUY لسه؛ '
            'لو وصلت شروط الدخول النهائية هتجيلك رسالة CONFIRMED BUY.'
        )
    else:
        safe_reason = (reason or 'final-safety-gate').replace('_', ' ')[:180]
        text = (
            f'🟡 HIGH-SCORE SETUP — {pair}\n'
            f'Score: {score:.0f}\n'
            f'Price ≈ {price:.8g}\n'
            f'Regime: {regime or "unknown"}\n'
            f'Safety gate: {safe_reason}\n\n'
            'الفرصة وصلت مرحلة الدخول، لكن التنفيذ ما اتفتحش لأن شرط أمان نهائي لسه ماعدّاش. '
            'مش هنسميها BUY مؤكدة قبل ما تعدّي الشرط.'
        )
    try:
        bridge.tg_api('sendMessage', {'text': text, 'disable_web_page_preview': True})
        _signal_visibility_last[key] = now
        print(f'[signal-visibility] SENT state={state} symbol={symbol} score={score:.0f}')
    except Exception as exc:
        print(f'[signal-visibility] FAILED state={state} symbol={symbol} err={type(exc).__name__}:{exc}')

'''
if insert_marker not in s:
    raise SystemExit('signal visibility patch failed: adaptive-watch helper marker missing')
s = s.replace(insert_marker, helper + insert_marker, 1)

# Notify once when a candidate first enters the adaptive WATCH band.
watch_marker = "            f'ttl={WATCH_TTL_SEC}s direct={DIRECT_SCORE:.0f}'\n        )\n        return\n"
watch_replacement = (
    "            f'ttl={WATCH_TTL_SEC}s direct={DIRECT_SCORE:.0f}'\n"
    "        )\n"
    "        _signal_visibility_alert(symbol, score, 'WATCH', price=price)\n"
    "        return\n"
)
if watch_marker not in s:
    raise SystemExit('signal visibility patch failed: WATCH insertion marker missing')
s = s.replace(watch_marker, watch_replacement, 1)

# If a direct-score setup is rejected by the final Spot Sniper authorization
# contract, surface it only when visibility alerts are explicitly enabled.
reject_marker = "        _record_candidate(symbol, lane, score, price, 'REJECT', why, **telemetry)\n        return False\n"
reject_replacement = (
    "        _record_candidate(symbol, lane, score, price, 'REJECT', why, **telemetry)\n"
    "        _signal_visibility_alert(symbol, score, 'DIRECT_BLOCKED', price=price, reason=why, regime=str(decision.get('regime') or ''))\n"
    "        return False\n"
)
if reject_marker not in s:
    raise SystemExit('signal visibility patch failed: Spot Sniper reject marker missing')
s = s.replace(reject_marker, reject_replacement, 1)

for required in [
    'def _signal_visibility_alert(',
    "FAST_VISIBILITY_ENABLED",
    "_signal_visibility_alert(symbol, score, 'WATCH'",
    "_signal_visibility_alert(symbol, score, 'DIRECT_BLOCKED'",
    '[signal-visibility] SENT',
]:
    if required not in s:
        raise SystemExit(f'signal visibility patch failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[signal-visibility-patch] OK optional WATCH/blocked visibility; execution authorization unchanged')
