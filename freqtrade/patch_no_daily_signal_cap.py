from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

old_gate = "    if signals_today >= MAX_SIGNALS_PER_DAY:\n        return False\n"
new_gate = "    if MAX_SIGNALS_PER_DAY > 0 and signals_today >= MAX_SIGNALS_PER_DAY:\n        return False\n"
if old_gate in s:
    s = s.replace(old_gate, new_gate, 1)
elif new_gate not in s:
    raise SystemExit('no-daily-cap patch failed: signal-count gate marker missing')

old_log = "f'[fast-engine] ONLINE mode=ADAPTIVE_WATCH watch={WATCH_SCORE:.0f} direct={DIRECT_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '"
new_log = "f'[fast-engine] ONLINE mode=ADAPTIVE_WATCH watch={WATCH_SCORE:.0f} direct={DIRECT_SCORE:.0f} max/day={\"UNLIMITED\" if MAX_SIGNALS_PER_DAY <= 0 else MAX_SIGNALS_PER_DAY} '"
if old_log in s:
    s = s.replace(old_log, new_log, 1)
elif 'max/day={"UNLIMITED" if MAX_SIGNALS_PER_DAY <= 0 else MAX_SIGNALS_PER_DAY}' not in s:
    raise SystemExit('no-daily-cap patch failed: startup log marker missing')

const_marker = "WATCH_TTL_SEC = int(os.getenv('FAST_WATCH_TTL_SEC', '300'))\n"
if 'EXPLOSIVE_MIN_24H_PCT' not in s:
    if const_marker not in s:
        raise SystemExit('explosive patch failed: constants marker missing')
    s = s.replace(const_marker, const_marker + (
        "EXPLOSIVE_MIN_24H_PCT = float(os.getenv('FAST_EXPLOSIVE_MIN_24H_PCT', '25'))\n"
        "EXPLOSIVE_MAX_24H_PCT = float(os.getenv('FAST_EXPLOSIVE_MAX_24H_PCT', '120'))\n"
        "EXPLOSIVE_MIN_24H_QV = float(os.getenv('FAST_EXPLOSIVE_MIN_24H_QV', '10000000'))\n"
        "EXPLOSIVE_WATCH_SCORE = float(os.getenv('FAST_EXPLOSIVE_WATCH_SCORE', '72'))\n"
        "EXPLOSIVE_DIRECT_SCORE = float(os.getenv('FAST_EXPLOSIVE_DIRECT_SCORE', '86'))\n"
    ), 1)

main_marker = "\ndef main() -> None:\n"
helpers = r'''

def explosive_candidates() -> list[tuple[str, float, float]]:
    rows = api('/ticker/24hr', {})
    out = []
    for row in rows if isinstance(rows, list) else []:
        symbol = str(row.get('symbol') or '')
        if not symbol_ok(symbol):
            continue
        ch = float(row.get('priceChangePercent') or 0.0)
        qv = float(row.get('quoteVolume') or 0.0)
        if qv >= EXPLOSIVE_MIN_24H_QV and EXPLOSIVE_MIN_24H_PCT <= ch <= EXPLOSIVE_MAX_24H_PCT:
            out.append((symbol, ch, qv))
    out.sort(key=lambda x: (x[2], x[1]), reverse=True)
    return out[:10]


def explosive_metrics(symbol: str) -> dict:
    rows = api('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 120})
    now_ms = int(time.time() * 1000)
    rows = [x for x in rows if int(x[6]) < now_ms]
    if len(rows) < 100:
        raise RuntimeError('insufficient 1m history')
    o = [float(x[1]) for x in rows]; h = [float(x[2]) for x in rows]
    l = [float(x[3]) for x in rows]; c = [float(x[4]) for x in rows]
    bv = [float(x[5]) for x in rows]; qv = [float(x[7]) for x in rows]
    tb = [float(x[9]) for x in rows]
    book = api('/ticker/bookTicker', {'symbol': symbol})
    bid = float(book.get('bidPrice') or 0.0); ask = float(book.get('askPrice') or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError('bad book')
    live = (bid + ask) / 2.0
    peak_start, peak_end = len(rows) - 60, len(rows) - 6
    peak_idx = max(range(peak_start, peak_end), key=lambda i: h[i])
    peak = h[peak_idx]
    post = list(range(peak_idx + 1, len(rows) - 1))
    if len(post) < 4:
        raise RuntimeError('pullback not mature')
    pull_idx = min(post, key=lambda i: l[i]); low = l[pull_idx]
    rs, re = max(pull_idx + 1, len(rows) - 9), len(rows) - 2
    reclaim = max(h[rs:re]) if re > rs else c[-3]
    ema9v, ema21v = ema(c[-40:], 9), ema(c[-60:], 21)
    recent_base = sum(bv[-5:]); recent_taker = sum(tb[-5:])
    recent_q = sum(qv[-5:]) / 5.0; prior_q = sum(qv[-25:-5]) / 20.0
    body = abs(c[-1] - o[-1]); upper = h[-1] - max(o[-1], c[-1])
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(-20, 0)]
    return {
        'live': live, 'peak': peak, 'low': low, 'reclaimed': live >= reclaim * 0.9995,
        'pullback': peak / low - 1.0, 'bounce': live / low - 1.0, 'peak_dist': peak / live - 1.0,
        'ema9': ema9v, 'ema21': ema21v, 'rsi': rsi(c, 14), 'mom5': live / c[-6] - 1.0,
        'volx': recent_q / prior_q if prior_q > 0 else 0.0,
        'taker': recent_taker / recent_base if recent_base > 0 else 0.0,
        'spread': (ask - bid) / live * 100.0,
        'book': float(book.get('bidQty') or 0.0) / max(float(book.get('askQty') or 0.0), 1e-12),
        'wick': upper / max(body, live * 0.0001),
        'impulse1m': max(c[i] / c[i-1] - 1.0 for i in range(len(c)-5, len(c))),
        'atr': (sum(trs) / len(trs)) / live,
    }


def score_explosive(m: dict) -> tuple[float, list[str]]:
    if not (0.015 <= m['pullback'] <= 0.10): return 0.0, ['no-valid-pullback']
    if not (0.006 <= m['bounce'] <= 0.065): return 0.0, ['bounce-not-fresh']
    if not (-0.008 <= m['peak_dist'] <= 0.055): return 0.0, ['outside-retest-zone']
    if m['spread'] > 0.15 or m['taker'] < 0.55: return 0.0, ['microstructure-weak']
    if m['mom5'] < -0.004 or m['mom5'] > 0.045 or m['impulse1m'] > 0.030: return 0.0, ['new-blowoff']
    if m['wick'] > 2.5: return 0.0, ['rejection-wick']
    score = 10.0; reasons = []
    if 0.02 <= m['pullback'] <= 0.07: score += 18; reasons.append('healthy-pullback')
    if m['reclaimed']: score += 20; reasons.append('reclaim')
    if m['live'] >= m['ema9'] and m['ema9'] >= m['ema21'] * 0.998: score += 12; reasons.append('trend-recovered')
    if m['volx'] >= 1.35: score += 16; reasons.append('volume-expansion')
    elif m['volx'] >= 1.10: score += 10
    if m['taker'] >= 0.68: score += 18; reasons.append('strong-taker-buy')
    elif m['taker'] >= 0.60: score += 13; reasons.append('taker-buy')
    else: score += 5
    if m['spread'] <= 0.08: score += 7; reasons.append('tight-spread')
    elif m['spread'] <= 0.12: score += 4
    if 52 <= m['rsi'] <= 76: score += 5
    if m['book'] >= 1.05: score += 4
    return min(100.0, score), reasons


def explosive_context_ok(symbol: str) -> tuple[bool, str]:
    now_ms = int(time.time() * 1000)
    r15 = [x for x in api('/klines', {'symbol': symbol, 'interval': '15m', 'limit': 80}) if int(x[6]) < now_ms]
    r1h = [x for x in api('/klines', {'symbol': symbol, 'interval': '1h', 'limit': 80}) if int(x[6]) < now_ms]
    if len(r15) < 60 or len(r1h) < 55: return False, 'insufficient-htf'
    c15 = [float(x[4]) for x in r15]; c1h = [float(x[4]) for x in r1h]
    e20_15, e50_15 = ema(c15[-60:], 20), ema(c15[-75:], 50)
    e20_1h, e50_1h = ema(c1h[-60:], 20), ema(c1h[-75:], 50)
    if c15[-1] < e20_15 * 0.992 or e20_15 < e50_15 * 0.985: return False, '15m-trend-failed'
    if c1h[-1] < e20_1h * 0.975 or e20_1h < e50_1h * 0.965: return False, '1h-trend-failed'
    if c15[-1] / c15[-2] - 1.0 > 0.060: return False, '15m-blowoff-active'
    if not bool(entry_quality.runtime_preflight().get('btc_regime_ok')): return False, 'btc-regime-weak'
    return True, 'ok'


def maybe_explosive_signal(symbol: str, ch24: float, volume24: float) -> bool:
    global last_global_signal, signals_today, execution_ready
    reset_day_counter(); now = time.time()
    if not execution_ready: return False
    if MAX_SIGNALS_PER_DAY > 0 and signals_today >= MAX_SIGNALS_PER_DAY: return False
    if now - last_global_signal < GLOBAL_COOLDOWN_SEC: return False
    if now - last_signal_by_pair.get(symbol, 0.0) < PAIR_COOLDOWN_SEC: return False
    m = explosive_metrics(symbol); score, reasons = score_explosive(m)
    print(f"[explosive-score] {symbol} score={score:.0f} ch24={ch24:+.1f}% pullback={m['pullback']*100:.2f}% bounce={m['bounce']*100:.2f}% reclaim={'Y' if m['reclaimed'] else 'N'} volx={m['volx']:.2f} taker={m['taker']*100:.1f}% rsi={m['rsi']:.1f} spread={m['spread']:.3f}%")
    if score < EXPLOSIVE_DIRECT_SCORE or not m['reclaimed']: return False
    ok, why = explosive_context_ok(symbol)
    if not ok:
        print(f'[explosive-context] {symbol} BLOCKED reason={why}'); return False
    sl_pct = clamp(max(0.010, m['atr'] * 3.2), 0.010, 0.022)
    tp_pct = clamp(sl_pct * 1.65, 0.017, 0.038)
    stake, free = dynamic_sizing.recommended_stake(sl_pct, max(score, 90.0))
    if stake is None:
        print(f'[explosive-sizing] {symbol} blocked free_usdt={free:.2f}'); return False
    entry = m['live']
    payload = {
        'id': f'{symbol}-EXP-{int(now)}', 'symbol': symbol, 'entry': entry,
        'stop': entry * (1.0 - sl_pct), 'target': entry * (1.0 + tp_pct),
        'stakeUSDT': stake, 'score': round(score),
        'strategy': f"EXPLOSIVE_CONTINUATION|score={score:.0f}|ch24={ch24:.1f}%|pullback={m['pullback']*100:.2f}%|volx={m['volx']:.2f}|taker={m['taker']*100:.1f}%",
        'dryRun': False,
    }
    row = fast_ingest(payload, timeout=30)
    if row.get('status') != 'FAST_SIGNAL_READY' or row.get('userConfirmationRequired') is not True or row.get('autoBuy') is not False:
        raise RuntimeError(f'unexpected explosive ingest response: {row}')
    last_signal_by_pair[symbol] = now; last_global_signal = now; signals_today += 1
    print(f"[explosive-continuation] one-tap ready {symbol[:-4]}/USDT score={score:.0f} amount={row.get('recommendedUSDT')} reasons={','.join(reasons)}")
    return True

'''
if 'def explosive_candidates()' not in s:
    if main_marker not in s: raise SystemExit('explosive patch failed: main marker missing')
    s = s.replace(main_marker, helpers + main_marker, 1)

loop_marker = "            scan = get_json(SCANNER_URL, timeout=25)\n            ranked = []\n"
loop_insert = """            scan = get_json(SCANNER_URL, timeout=25)
            exp_ranked = []
            for es, ech, ev in explosive_candidates():
                try:
                    em = explosive_metrics(es); escore, _ = score_explosive(em)
                    exp_ranked.append((escore, es, ech, ev))
                    if escore >= EXPLOSIVE_WATCH_SCORE:
                        print(f"[explosive-watch] {es} score={escore:.0f} ch24={ech:+.1f}% pullback={em['pullback']*100:.2f}% reclaim={'Y' if em['reclaimed'] else 'N'} volx={em['volx']:.2f} taker={em['taker']*100:.1f}%")
                except Exception as exc:
                    print(f'[explosive-watch] {es} metrics failed: {type(exc).__name__}: {exc}')
            exp_ranked.sort(reverse=True, key=lambda x: x[0])
            for escore, es, ech, ev in exp_ranked[:8]:
                if escore < EXPLOSIVE_DIRECT_SCORE: break
                if not telegram_ok or not execution_ready: break
                try:
                    if maybe_explosive_signal(es, ech, ev): break
                except Exception as exc:
                    print(f'[explosive-continuation] {es} signal failed: {type(exc).__name__}: {exc}')
                    execution_ready = False

            ranked = []
"""
if 'exp_ranked = []' not in s:
    if loop_marker not in s: raise SystemExit('explosive patch failed: loop marker missing')
    s = s.replace(loop_marker, loop_insert, 1)

startup_marker = "    last_chat_retry = 0.0\n"
if '[explosive-continuation] ONLINE' not in s:
    s = s.replace(startup_marker, "    print(f'[explosive-continuation] ONLINE range={EXPLOSIVE_MIN_24H_PCT:.0f}-{EXPLOSIVE_MAX_24H_PCT:.0f}% watch={EXPLOSIVE_WATCH_SCORE:.0f} direct={EXPLOSIVE_DIRECT_SCORE:.0f}')\n" + startup_marker, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[no-daily-signal-cap] OK unlimited qualified signals; explosive pullback/reclaim path enabled; normal anti-chase/risk gates unchanged')
