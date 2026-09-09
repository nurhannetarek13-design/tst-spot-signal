from pathlib import Path

engine_path = Path('/freqtrade/fast_entry_engine.py')
quality_path = Path('/freqtrade/entry_quality.py')

s = engine_path.read_text(encoding='utf-8')
q = quality_path.read_text(encoding='utf-8')

# ---------------------------------------------------------------------------
# 1) Conditional higher-timeframe gate for NORMAL ignition.
# Keep BTC regime, anti-chase, spread, liquidity, move-from-low and impulse
# protections hard. Only allow ONE weak HTF trend check to be overridden when
# live microstructure is exceptional. This avoids binary rejection by a hair.
# ---------------------------------------------------------------------------
old_quality = '''    if not trend['trend15_ok']:\n        return False, '15m-trend-not-confirmed', trend\n    if not trend['trend1h_ok']:\n        return False, '1h-trend-not-confirmed', trend\n\n    btc = _btc_regime()\n    context = {**trend, 'btc': btc, 'micro_reason': micro_reason}\n    if not btc['ok']:\n        return False, 'btc-regime-weak', context\n\n    return True, 'quality-ignition-ok' if micro_reason == 'ignition-ok' else 'quality-ok', context\n'''
new_quality = '''    btc = _btc_regime()\n    context = {**trend, 'btc': btc, 'micro_reason': micro_reason}\n    if not btc['ok']:\n        return False, 'btc-regime-weak', context\n\n    trend_failures = []\n    if not trend['trend15_ok']:\n        trend_failures.append('15m-trend-not-confirmed')\n    if not trend['trend1h_ok']:\n        trend_failures.append('1h-trend-not-confirmed')\n\n    if trend_failures:\n        strong_micro = (\n            float(m.get('volume_ratio') or 0.0) >= 1.35\n            and float(m.get('taker_buy_ratio') or 0.0) >= 0.68\n            and bool(m.get('volume_accel'))\n            and float(m.get('spread_pct') or 999.0) <= 0.08\n            and float(m.get('compression_ratio') or 999.0) <= 1.00\n            and float(m.get('wick_ratio') or 999.0) <= 1.80\n            and float(trend.get('move_from_2h_low') or 99.0) <= 0.022\n            and float(trend.get('max_recent_15m_impulse') or 99.0) <= 0.014\n        )\n        # Never override both HTF failures. One marginal HTF miss is allowed only\n        # when order flow is unusually strong and BTC is already healthy.\n        if len(trend_failures) == 1 and strong_micro:\n            context['trend_exception'] = trend_failures[0]\n            return True, 'quality-strong-micro-exception', context\n        return False, trend_failures[0], context\n\n    return True, 'quality-ignition-ok' if micro_reason == 'ignition-ok' else 'quality-ok', context\n'''
if 'quality-strong-micro-exception' not in q:
    if old_quality not in q:
        raise SystemExit('profit-engine-v2: entry_quality trend block marker missing')
    q = q.replace(old_quality, new_quality, 1)

# ---------------------------------------------------------------------------
# 2) MID-MOMENTUM lane: fills the 5%-25% 24h gap between first ignition and the
# existing 25%+ explosive continuation lane. It never buys a vertical candle;
# it requires a pullback, reclaim, renewed flow and BTC context.
# ---------------------------------------------------------------------------
const_marker = "EXTREME_STAKE_MULT = float(os.getenv('FAST_EXTREME_STAKE_MULT', '0.55'))\n"
mid_constants = (
    "MID_MIN_24H_PCT = float(os.getenv('FAST_MID_MIN_24H_PCT', '5'))\n"
    "MID_MAX_24H_PCT = float(os.getenv('FAST_MID_MAX_24H_PCT', '25'))\n"
    "MID_MIN_24H_QV = float(os.getenv('FAST_MID_MIN_24H_QV', '5000000'))\n"
    "MID_WATCH_SCORE = float(os.getenv('FAST_MID_WATCH_SCORE', '76'))\n"
    "MID_DIRECT_SCORE = float(os.getenv('FAST_MID_DIRECT_SCORE', '86'))\n"
    "MID_STAKE_MULT = float(os.getenv('FAST_MID_STAKE_MULT', '0.75'))\n"
)
if 'MID_MIN_24H_PCT' not in s:
    if const_marker not in s:
        raise SystemExit('profit-engine-v2: extreme constants marker missing')
    s = s.replace(const_marker, const_marker + mid_constants, 1)

main_marker = "\ndef main() -> None:\n"
helpers = r'''


def mid_candidates() -> list[tuple[str, float, float]]:
    rows = api('/ticker/24hr', {})
    out = []
    for row in rows if isinstance(rows, list) else []:
        symbol = str(row.get('symbol') or '')
        if not symbol_ok(symbol):
            continue
        ch = float(row.get('priceChangePercent') or 0.0)
        qv = float(row.get('quoteVolume') or 0.0)
        if qv >= MID_MIN_24H_QV and MID_MIN_24H_PCT <= ch < MID_MAX_24H_PCT:
            out.append((symbol, ch, qv))
    # Prefer stronger movers, then liquidity. This lane is continuation, not discovery.
    out.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return out[:14]


def score_mid(m: dict) -> tuple[float, list[str]]:
    # Continuation only after a real but controlled reset.
    if not (0.008 <= m['pullback'] <= 0.070): return 0.0, ['no-clean-pullback']
    if not (0.004 <= m['bounce'] <= 0.040): return 0.0, ['bounce-not-fresh']
    if not (-0.005 <= m['peak_dist'] <= 0.045): return 0.0, ['outside-reclaim-zone']
    if m['spread'] > 0.12: return 0.0, ['spread-too-wide']
    if m['taker'] < 0.57: return 0.0, ['buy-pressure-weak']
    if m['volx'] < 0.90: return 0.0, ['volume-not-ready']
    if m['mom5'] < -0.005 or m['mom5'] > 0.030: return 0.0, ['momentum-not-controlled']
    if m['impulse1m'] > 0.022: return 0.0, ['new-blowoff']
    if m['wick'] > 2.4: return 0.0, ['rejection-wick']
    if m['rsi'] > 74: return 0.0, ['rsi-overheated']

    score = 10.0
    reasons = []
    if 0.012 <= m['pullback'] <= 0.045:
        score += 18; reasons.append('healthy-reset')
    else:
        score += 10
    if m['reclaimed']:
        score += 20; reasons.append('reclaim-confirmed')
    if m['live'] >= m['ema9'] and m['ema9'] >= m['ema21'] * 0.998:
        score += 12; reasons.append('trend-recovered')
    if m['volx'] >= 1.50:
        score += 16; reasons.append('strong-volume-return')
    elif m['volx'] >= 1.20:
        score += 11; reasons.append('volume-return')
    else:
        score += 5
    if m['taker'] >= 0.70:
        score += 17; reasons.append('strong-taker-buy')
    elif m['taker'] >= 0.63:
        score += 12; reasons.append('taker-buy')
    else:
        score += 6
    if m['spread'] <= 0.06:
        score += 7; reasons.append('tight-spread')
    elif m['spread'] <= 0.10:
        score += 4
    if 50 <= m['rsi'] <= 68:
        score += 5; reasons.append('rsi-reset')
    if m['book'] >= 1.10:
        score += 4; reasons.append('bid-depth')
    if 0.001 <= m['mom5'] <= 0.020:
        score += 4; reasons.append('controlled-reacceleration')
    return min(100.0, score), reasons


def mid_context_ok(symbol: str, m: dict, score: float) -> tuple[bool, str]:
    now_ms = int(time.time() * 1000)
    r15 = [x for x in api('/klines', {'symbol': symbol, 'interval': '15m', 'limit': 80}) if int(x[6]) < now_ms]
    r1h = [x for x in api('/klines', {'symbol': symbol, 'interval': '1h', 'limit': 80}) if int(x[6]) < now_ms]
    if len(r15) < 60 or len(r1h) < 55:
        return False, 'insufficient-htf'

    c15 = [float(x[4]) for x in r15]
    c1h = [float(x[4]) for x in r1h]
    e20_15, e50_15 = ema(c15[-60:], 20), ema(c15[-75:], 50)
    e20_1h, e50_1h = ema(c1h[-60:], 20), ema(c1h[-75:], 50)

    if c15[-1] / c15[-2] - 1.0 > 0.035:
        return False, '15m-blowoff-active'
    if c15[-1] > e20_15 * 1.060:
        return False, '15m-too-extended'

    trend15 = c15[-1] >= e20_15 * 0.995 and e20_15 >= e50_15 * 0.990
    trend1h = c1h[-1] >= e20_1h * 0.985 and e20_1h >= e50_1h * 0.975
    if not trend15 and not trend1h:
        return False, 'both-htf-trends-weak'

    # One weak timeframe can pass only with exceptional renewed order flow.
    if not (trend15 and trend1h):
        exceptional = (
            score >= 92.0 and m['reclaimed'] and m['volx'] >= 1.35
            and m['taker'] >= 0.68 and m['spread'] <= 0.08
            and m['impulse1m'] <= 0.018 and m['wick'] <= 1.8
        )
        if not exceptional:
            return False, 'single-htf-needs-exceptional-flow'

    if not bool(entry_quality.runtime_preflight().get('btc_regime_ok')):
        return False, 'btc-regime-weak'
    return True, 'ok'


def maybe_mid_signal(symbol: str, ch24: float, volume24: float) -> bool:
    global last_global_signal, signals_today, execution_ready
    reset_day_counter(); now = time.time()
    if not execution_ready: return False
    if MAX_SIGNALS_PER_DAY > 0 and signals_today >= MAX_SIGNALS_PER_DAY: return False
    if now - last_global_signal < GLOBAL_COOLDOWN_SEC: return False
    if now - last_signal_by_pair.get(symbol, 0.0) < PAIR_COOLDOWN_SEC: return False

    m = explosive_metrics(symbol)
    score, reasons = score_mid(m)
    print(
        f"[mid-score] {symbol} score={score:.0f} ch24={ch24:+.1f}% "
        f"pullback={m['pullback']*100:.2f}% bounce={m['bounce']*100:.2f}% "
        f"reclaim={'Y' if m['reclaimed'] else 'N'} volx={m['volx']:.2f} "
        f"taker={m['taker']*100:.1f}% rsi={m['rsi']:.1f} spread={m['spread']:.3f}%"
    )
    if score < MID_DIRECT_SCORE or not m['reclaimed']:
        return False

    ok, why = mid_context_ok(symbol, m, score)
    if not ok:
        print(f'[mid-context] {symbol} BLOCKED reason={why}')
        return False

    sl_pct = clamp(max(0.007, m['atr'] * 3.0), 0.007, 0.014)
    tp_pct = clamp(sl_pct * 2.05, 0.016, 0.032)
    base_stake, free = dynamic_sizing.recommended_stake(sl_pct, max(score, 90.0))
    if base_stake is None:
        print(f'[mid-sizing] {symbol} blocked free_usdt={free:.2f}')
        return False

    min_stake = max(5.0, float(os.getenv('MIN_STAKE_USDT', '5.0')))
    mult = clamp(MID_STAKE_MULT, 0.40, 0.90)
    stake = math.floor(base_stake * mult * 100.0) / 100.0
    if stake < min_stake:
        stake = min(min_stake, base_stake)
    if stake < min_stake:
        return False

    entry = m['live']
    payload = {
        'id': f'{symbol}-MID-{int(now)}', 'symbol': symbol, 'entry': entry,
        'stop': entry * (1.0 - sl_pct), 'target': entry * (1.0 + tp_pct),
        'stakeUSDT': stake, 'score': round(score),
        'strategy': f"MID_MOMENTUM_CONTINUATION|score={score:.0f}|ch24={ch24:.1f}%|pullback={m['pullback']*100:.2f}%|volx={m['volx']:.2f}|taker={m['taker']*100:.1f}%|stake_mult={mult:.2f}",
        'dryRun': False,
    }
    row = fast_ingest(payload, timeout=30)
    if row.get('status') != 'FAST_SIGNAL_READY' or row.get('userConfirmationRequired') is not True or row.get('autoBuy') is not False:
        raise RuntimeError(f'unexpected mid ingest response: {row}')

    last_signal_by_pair[symbol] = now
    last_global_signal = now
    signals_today += 1
    print(f"[mid-continuation] one-tap ready {symbol[:-4]}/USDT score={score:.0f} amount={row.get('recommendedUSDT')} reasons={','.join(reasons)}")
    return True

'''
if 'def mid_candidates()' not in s:
    if main_marker not in s:
        raise SystemExit('profit-engine-v2: main marker missing')
    s = s.replace(main_marker, helpers + main_marker, 1)

# Insert MID lane after Extreme/Explosive evaluation, before the normal ignition ranking.
if 'mid_ranked = []' not in s:
    anchor = "            ranked = []\n"
    search_from = s.find('            extreme_ranked = []')
    pos = s.find(anchor, search_from if search_from >= 0 else 0)
    if pos < 0:
        raise SystemExit('profit-engine-v2: normal ranked-loop marker missing')
    mid_loop = """            mid_ranked = []
            for ms, mch, mv in mid_candidates():
                try:
                    mm = explosive_metrics(ms); mscore, _ = score_mid(mm)
                    mid_ranked.append((mscore, ms, mch, mv))
                    if mscore >= MID_WATCH_SCORE:
                        print(f"[mid-watch] {ms} score={mscore:.0f} ch24={mch:+.1f}% pullback={mm['pullback']*100:.2f}% reclaim={'Y' if mm['reclaimed'] else 'N'} volx={mm['volx']:.2f} taker={mm['taker']*100:.1f}%")
                except Exception as exc:
                    print(f'[mid-watch] {ms} metrics failed: {type(exc).__name__}: {exc}')
            mid_ranked.sort(reverse=True, key=lambda x: x[0])
            for mscore, ms, mch, mv in mid_ranked[:10]:
                if mscore < MID_DIRECT_SCORE: break
                if not telegram_ok or not execution_ready: break
                try:
                    if maybe_mid_signal(ms, mch, mv): break
                except Exception as exc:
                    print(f'[mid-continuation] {ms} signal failed: {type(exc).__name__}: {exc}')
                    execution_ready = False

"""
    s = s[:pos] + mid_loop + s[pos:]

startup_marker = "    last_chat_retry = 0.0\n"
if '[mid-continuation] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('profit-engine-v2: startup marker missing')
    startup = "    print(f'[mid-continuation] ONLINE range={MID_MIN_24H_PCT:.0f}-{MID_MAX_24H_PCT:.0f}% watch={MID_WATCH_SCORE:.0f} direct={MID_DIRECT_SCORE:.0f} stake_mult={clamp(MID_STAKE_MULT, 0.40, 0.90):.2f}')\n"
    s = s.replace(startup_marker, startup + startup_marker, 1)

for required in [
    'quality-strong-micro-exception',
    'MID_MIN_24H_PCT',
    'def mid_candidates()',
    'def score_mid(',
    'def mid_context_ok(',
    'def maybe_mid_signal(',
    'mid_ranked = []',
    '[mid-continuation] ONLINE',
]:
    target = q if required == 'quality-strong-micro-exception' else s
    if required not in target:
        raise SystemExit(f'profit-engine-v2: missing {required}')

compile(q, str(quality_path), 'exec')
compile(s, str(engine_path), 'exec')
quality_path.write_text(q, encoding='utf-8')
engine_path.write_text(s, encoding='utf-8')
print('[profit-engine-v2] OK mid-momentum 5-25% continuation + one-HTF strong-micro exception; hard BTC/risk/anti-blowoff protections preserved')
