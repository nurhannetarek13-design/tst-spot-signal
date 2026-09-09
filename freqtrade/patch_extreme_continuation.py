from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Keep the existing 25-120% explosive lane unchanged.  This adds a separate,
# stricter lane for very extended 24h movers so they are only considered after
# a real pullback/reclaim, with stronger microstructure checks and smaller size.
const_marker = "EXPLOSIVE_DIRECT_SCORE = float(os.getenv('FAST_EXPLOSIVE_DIRECT_SCORE', '86'))\n"
if 'EXTREME_MIN_24H_PCT' not in s:
    if const_marker not in s:
        raise SystemExit('extreme-continuation patch failed: explosive constants marker missing')
    s = s.replace(const_marker, const_marker + (
        "EXTREME_MIN_24H_PCT = float(os.getenv('FAST_EXTREME_MIN_24H_PCT', '120'))\n"
        "EXTREME_MAX_24H_PCT = float(os.getenv('FAST_EXTREME_MAX_24H_PCT', '200'))\n"
        "EXTREME_MIN_24H_QV = float(os.getenv('FAST_EXTREME_MIN_24H_QV', '15000000'))\n"
        "EXTREME_WATCH_SCORE = float(os.getenv('FAST_EXTREME_WATCH_SCORE', '82'))\n"
        "EXTREME_DIRECT_SCORE = float(os.getenv('FAST_EXTREME_DIRECT_SCORE', '92'))\n"
        "EXTREME_STAKE_MULT = float(os.getenv('FAST_EXTREME_STAKE_MULT', '0.55'))\n"
    ), 1)

main_marker = "\ndef main() -> None:\n"
helpers = r'''


def extreme_candidates() -> list[tuple[str, float, float]]:
    rows = api('/ticker/24hr', {})
    out = []
    for row in rows if isinstance(rows, list) else []:
        symbol = str(row.get('symbol') or '')
        if not symbol_ok(symbol):
            continue
        ch = float(row.get('priceChangePercent') or 0.0)
        qv = float(row.get('quoteVolume') or 0.0)
        # Strictly above the normal explosive cap so the two lanes do not overlap.
        if qv >= EXTREME_MIN_24H_QV and EXTREME_MIN_24H_PCT < ch <= EXTREME_MAX_24H_PCT:
            out.append((symbol, ch, qv))
    out.sort(key=lambda x: (x[2], x[1]), reverse=True)
    return out[:8]


def score_extreme(m: dict) -> tuple[float, list[str]]:
    # A 120%+ mover must first cool off materially; never buy the vertical leg.
    if not (0.030 <= m['pullback'] <= 0.150): return 0.0, ['no-meaningful-pullback']
    if not (0.008 <= m['bounce'] <= 0.070): return 0.0, ['bounce-not-controlled']
    if not (0.010 <= m['peak_dist'] <= 0.120): return 0.0, ['not-in-second-wave-zone']
    if m['spread'] > 0.10 or m['taker'] < 0.62: return 0.0, ['microstructure-weak']
    if m['volx'] < 1.20: return 0.0, ['volume-not-confirmed']
    if m['mom5'] < -0.006 or m['mom5'] > 0.035 or m['impulse1m'] > 0.020: return 0.0, ['new-blowoff']
    if m['wick'] > 2.0: return 0.0, ['rejection-wick']
    if m['rsi'] > 76: return 0.0, ['rsi-overheated']

    score = 8.0
    reasons = []
    if 0.040 <= m['pullback'] <= 0.110:
        score += 20; reasons.append('deep-clean-pullback')
    else:
        score += 10
    if m['reclaimed']:
        score += 22; reasons.append('reclaim-confirmed')
    if m['live'] >= m['ema9'] and m['ema9'] >= m['ema21'] * 0.998:
        score += 14; reasons.append('trend-recovered')
    if m['volx'] >= 1.60:
        score += 18; reasons.append('strong-volume-expansion')
    elif m['volx'] >= 1.30:
        score += 12; reasons.append('volume-expansion')
    else:
        score += 6
    if m['taker'] >= 0.74:
        score += 18; reasons.append('very-strong-taker-buy')
    elif m['taker'] >= 0.68:
        score += 14; reasons.append('strong-taker-buy')
    else:
        score += 8
    if m['spread'] <= 0.05:
        score += 8; reasons.append('very-tight-spread')
    elif m['spread'] <= 0.08:
        score += 5
    else:
        score += 2
    if 52 <= m['rsi'] <= 70:
        score += 6; reasons.append('rsi-reset')
    if m['book'] >= 1.15:
        score += 6; reasons.append('bid-depth')
    elif m['book'] >= 1.0:
        score += 3
    if 0.002 <= m['mom5'] <= 0.025:
        score += 4; reasons.append('controlled-reacceleration')
    return min(100.0, score), reasons


def extreme_context_ok(symbol: str) -> tuple[bool, str]:
    ok, why = explosive_context_ok(symbol)
    if not ok:
        return False, why

    now_ms = int(time.time() * 1000)
    r5 = [x for x in api('/klines', {'symbol': symbol, 'interval': '5m', 'limit': 60}) if int(x[6]) < now_ms]
    if len(r5) < 40:
        return False, 'insufficient-5m'
    c5 = [float(x[4]) for x in r5]
    e9, e21 = ema(c5[-35:], 9), ema(c5[-45:], 21)
    last_moves = [c5[i] / c5[i - 1] - 1.0 for i in range(len(c5) - 2, len(c5))]
    if max(last_moves) > 0.040:
        return False, '5m-blowoff-active'
    if c5[-1] < e9 * 0.992 or e9 < e21 * 0.995:
        return False, '5m-recovery-not-confirmed'
    if c5[-1] > e21 * 1.080:
        return False, '5m-too-extended-after-reclaim'
    return True, 'ok'


def maybe_extreme_signal(symbol: str, ch24: float, volume24: float) -> bool:
    global last_global_signal, signals_today, execution_ready
    reset_day_counter(); now = time.time()
    if not execution_ready: return False
    if MAX_SIGNALS_PER_DAY > 0 and signals_today >= MAX_SIGNALS_PER_DAY: return False
    if now - last_global_signal < GLOBAL_COOLDOWN_SEC: return False
    if now - last_signal_by_pair.get(symbol, 0.0) < PAIR_COOLDOWN_SEC: return False

    m = explosive_metrics(symbol)
    score, reasons = score_extreme(m)
    print(f"[extreme-score] {symbol} score={score:.0f} ch24={ch24:+.1f}% pullback={m['pullback']*100:.2f}% bounce={m['bounce']*100:.2f}% peakdist={m['peak_dist']*100:.2f}% reclaim={'Y' if m['reclaimed'] else 'N'} volx={m['volx']:.2f} taker={m['taker']*100:.1f}% rsi={m['rsi']:.1f} spread={m['spread']:.3f}%")
    if score < EXTREME_DIRECT_SCORE or not m['reclaimed']:
        return False

    ok, why = extreme_context_ok(symbol)
    if not ok:
        print(f'[extreme-context] {symbol} BLOCKED reason={why}')
        return False

    sl_pct = clamp(max(0.012, m['atr'] * 3.6), 0.012, 0.026)
    tp_pct = clamp(sl_pct * 1.80, 0.022, 0.045)
    base_stake, free = dynamic_sizing.recommended_stake(sl_pct, max(score, 92.0))
    if base_stake is None:
        print(f'[extreme-sizing] {symbol} blocked free_usdt={free:.2f}')
        return False

    min_stake = max(5.0, float(os.getenv('MIN_STAKE_USDT', '5.0')))
    mult = clamp(EXTREME_STAKE_MULT, 0.25, 0.75)
    stake = math.floor(base_stake * mult * 100.0) / 100.0
    if stake < min_stake:
        stake = min(min_stake, base_stake)
    if stake < min_stake:
        print(f'[extreme-sizing] {symbol} blocked reduced_stake={stake:.2f} base={base_stake:.2f}')
        return False

    entry = m['live']
    payload = {
        'id': f'{symbol}-XTR-{int(now)}', 'symbol': symbol, 'entry': entry,
        'stop': entry * (1.0 - sl_pct), 'target': entry * (1.0 + tp_pct),
        'stakeUSDT': stake, 'score': round(score),
        'strategy': f"EXTREME_CONTINUATION|score={score:.0f}|ch24={ch24:.1f}%|pullback={m['pullback']*100:.2f}%|peakdist={m['peak_dist']*100:.2f}%|volx={m['volx']:.2f}|taker={m['taker']*100:.1f}%|stake_mult={mult:.2f}",
        'dryRun': False,
    }
    row = fast_ingest(payload, timeout=30)
    if row.get('status') != 'FAST_SIGNAL_READY' or row.get('userConfirmationRequired') is not True or row.get('autoBuy') is not False:
        raise RuntimeError(f'unexpected extreme ingest response: {row}')

    last_signal_by_pair[symbol] = now
    last_global_signal = now
    signals_today += 1
    print(f"[extreme-continuation] one-tap ready {symbol[:-4]}/USDT score={score:.0f} amount={row.get('recommendedUSDT')} requested={stake:.2f} reasons={','.join(reasons)}")
    return True

'''

if 'def extreme_candidates()' not in s:
    if main_marker not in s:
        raise SystemExit('extreme-continuation patch failed: main marker missing')
    s = s.replace(main_marker, helpers + main_marker, 1)

if 'extreme_ranked = []' not in s:
    anchor = "            ranked = []\n"
    search_from = s.find('            exp_ranked = []')
    pos = s.find(anchor, search_from if search_from >= 0 else 0)
    if pos < 0:
        raise SystemExit('extreme-continuation patch failed: loop insertion marker missing')
    extreme_loop = """            extreme_ranked = []
            for xs, xch, xv in extreme_candidates():
                try:
                    xm = explosive_metrics(xs); xscore, _ = score_extreme(xm)
                    extreme_ranked.append((xscore, xs, xch, xv))
                    if xscore >= EXTREME_WATCH_SCORE:
                        print(f"[extreme-watch] {xs} score={xscore:.0f} ch24={xch:+.1f}% pullback={xm['pullback']*100:.2f}% peakdist={xm['peak_dist']*100:.2f}% reclaim={'Y' if xm['reclaimed'] else 'N'} volx={xm['volx']:.2f} taker={xm['taker']*100:.1f}%")
                except Exception as exc:
                    print(f'[extreme-watch] {xs} metrics failed: {type(exc).__name__}: {exc}')
            extreme_ranked.sort(reverse=True, key=lambda x: x[0])
            for xscore, xs, xch, xv in extreme_ranked[:6]:
                if xscore < EXTREME_DIRECT_SCORE: break
                if not telegram_ok or not execution_ready: break
                try:
                    if maybe_extreme_signal(xs, xch, xv): break
                except Exception as exc:
                    print(f'[extreme-continuation] {xs} signal failed: {type(exc).__name__}: {exc}')
                    execution_ready = False

"""
    s = s[:pos] + extreme_loop + s[pos:]

startup_marker = "    last_chat_retry = 0.0\n"
if '[extreme-continuation] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('extreme-continuation patch failed: startup marker missing')
    startup = "    print(f'[extreme-continuation] ONLINE range=>{EXTREME_MIN_24H_PCT:.0f}-{EXTREME_MAX_24H_PCT:.0f}% watch={EXTREME_WATCH_SCORE:.0f} direct={EXTREME_DIRECT_SCORE:.0f} stake_mult={clamp(EXTREME_STAKE_MULT, 0.25, 0.75):.2f}')\n"
    s = s.replace(startup_marker, startup + startup_marker, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[extreme-continuation-patch] OK separate 120-200% pullback/reclaim lane enabled; stricter gates + reduced stake; existing explosive lane unchanged')
