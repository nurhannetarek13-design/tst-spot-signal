from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Early Reversal lane: catch a high-liquidity selloff after the first verified
# turn, before the normal MID continuation lane needs a +5%/+ move.  This is
# deliberately starter-size only: do not pyramid into an already-protected OCO
# position until atomic add-and-reprotect support exists.
const_marker = "EXTREME_STAKE_MULT = float(os.getenv('FAST_EXTREME_STAKE_MULT', '0.55'))\n"
if 'EARLY_REVERSAL_MIN_24H_QV' not in s:
    if const_marker not in s:
        raise SystemExit('early-reversal patch failed: extreme constants marker missing')
    s = s.replace(const_marker, const_marker + (
        "EARLY_REVERSAL_MIN_24H_QV = float(os.getenv('FAST_EARLY_REVERSAL_MIN_24H_QV', '20000000'))\n"
        "EARLY_REVERSAL_MIN_24H_PCT = float(os.getenv('FAST_EARLY_REVERSAL_MIN_24H_PCT', '-20'))\n"
        "EARLY_REVERSAL_MAX_24H_PCT = float(os.getenv('FAST_EARLY_REVERSAL_MAX_24H_PCT', '8'))\n"
        "EARLY_REVERSAL_WATCH_SCORE = float(os.getenv('FAST_EARLY_REVERSAL_WATCH_SCORE', '88'))\n"
        "EARLY_REVERSAL_DIRECT_SCORE = float(os.getenv('FAST_EARLY_REVERSAL_DIRECT_SCORE', '95'))\n"
        "EARLY_REVERSAL_STARTER_MULT = float(os.getenv('FAST_EARLY_REVERSAL_STARTER_MULT', '0.45'))\n"
        "EARLY_REVERSAL_MAX_STAKE = float(os.getenv('FAST_EARLY_REVERSAL_MAX_STAKE', '15'))\n"
    ), 1)

main_marker = "\ndef main() -> None:\n"
helpers = r'''


def early_reversal_candidates() -> list[tuple[str, float, float]]:
    rows = api('/ticker/24hr', {})
    out = []
    for row in rows if isinstance(rows, list) else []:
        symbol = str(row.get('symbol') or '')
        if not symbol_ok(symbol):
            continue
        try:
            ch = float(row.get('priceChangePercent') or 0.0)
            qv = float(row.get('quoteVolume') or 0.0)
        except Exception:
            continue
        if qv < EARLY_REVERSAL_MIN_24H_QV:
            continue
        if not (EARLY_REVERSAL_MIN_24H_PCT <= ch <= EARLY_REVERSAL_MAX_24H_PCT):
            continue
        out.append((symbol, ch, qv))
    out.sort(key=lambda x: x[2], reverse=True)
    return out[:14]


def early_reversal_metrics(symbol: str) -> dict:
    rows = api('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 180})
    now_ms = int(time.time() * 1000)
    rows = [x for x in rows if int(x[6]) < now_ms]
    if len(rows) < 150:
        raise RuntimeError('insufficient-1m-history')

    o = [float(x[1]) for x in rows]
    h = [float(x[2]) for x in rows]
    l = [float(x[3]) for x in rows]
    c = [float(x[4]) for x in rows]
    bv = [float(x[5]) for x in rows]
    qv = [float(x[7]) for x in rows]
    tb = [float(x[9]) for x in rows]

    book = api('/ticker/bookTicker', {'symbol': symbol})
    bid = float(book.get('bidPrice') or 0.0)
    ask = float(book.get('askPrice') or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError('bad-book')
    live = (bid + ask) / 2.0

    low_start = len(rows) - 45
    low_end = len(rows) - 2
    low_idx = min(range(low_start, low_end), key=lambda i: l[i])
    low = l[low_idx]
    low_age = (len(rows) - 1) - low_idx
    high_start = max(0, low_idx - 90)
    if low_idx - high_start < 15:
        raise RuntimeError('selloff-history-too-short')
    pre_high = max(h[high_start:low_idx])
    drawdown = pre_high / low - 1.0 if low > 0 else 0.0
    bounce = live / low - 1.0 if low > 0 else 0.0

    ema9_now = ema(c[-45:], 9)
    ema21_now = ema(c[-70:], 21)
    ema9_prev = ema(c[-46:-1], 9)
    ema9_slope = ema9_now / ema9_prev - 1.0 if ema9_prev > 0 else -9.0

    recent_base = sum(bv[-5:])
    recent_taker = sum(tb[-5:])
    recent_q = sum(qv[-5:]) / 5.0
    prior_q = sum(qv[-35:-5]) / 30.0
    volx = recent_q / prior_q if prior_q > 0 else 0.0
    taker = recent_taker / recent_base if recent_base > 0 else 0.0
    spread = (ask - bid) / live * 100.0
    book_ratio = float(book.get('bidQty') or 0.0) / max(float(book.get('askQty') or 0.0), 1e-12)

    low_body = abs(c[low_idx] - o[low_idx])
    lower_wick = min(o[low_idx], c[low_idx]) - l[low_idx]
    rejection = lower_wick / max(low_body, live * 0.0001)

    mom1 = live / c[-2] - 1.0
    mom3 = live / c[-4] - 1.0
    mom5 = live / c[-6] - 1.0
    last_impulse = max(c[i] / c[i - 1] - 1.0 for i in range(len(c) - 4, len(c)))

    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(-20, 0)]
    atr = (sum(trs) / len(trs)) / live
    reclaimed = live >= ema9_now * 0.999 and ema9_slope >= -0.0005 and mom3 > 0

    return {
        'live': live, 'low': low, 'pre_high': pre_high,
        'drawdown': drawdown, 'bounce': bounce, 'low_age': low_age,
        'ema9': ema9_now, 'ema21': ema21_now, 'ema9_slope': ema9_slope,
        'reclaimed': reclaimed, 'rsi': rsi(c, 14),
        'volx': volx, 'taker': taker, 'spread': spread, 'book': book_ratio,
        'rejection': rejection, 'mom1': mom1, 'mom3': mom3, 'mom5': mom5,
        'last_impulse': last_impulse, 'atr': atr,
    }


def score_early_reversal(m: dict) -> tuple[float, list[str]]:
    if not (0.025 <= m['drawdown'] <= 0.16): return 0.0, ['selloff-not-valid']
    if not (2 <= int(m['low_age']) <= 28): return 0.0, ['low-not-recent']
    if not (0.006 <= m['bounce'] <= 0.042): return 0.0, ['bounce-not-fresh']
    if not m['reclaimed']: return 0.0, ['no-fast-reclaim']
    if m['volx'] < 1.20: return 0.0, ['volume-not-confirmed']
    if m['taker'] < 0.60: return 0.0, ['taker-buy-too-weak']
    if m['spread'] > 0.10: return 0.0, ['spread-too-wide']
    if m['last_impulse'] > 0.018 or m['mom5'] > 0.040: return 0.0, ['bounce-already-chasing']
    if m['mom3'] <= 0 or m['mom1'] < -0.006: return 0.0, ['reversal-lost']
    if not (34.0 <= m['rsi'] <= 66.0): return 0.0, ['rsi-outside-reversal-zone']

    score = 12.0
    reasons = []
    if 0.035 <= m['drawdown'] <= 0.11:
        score += 14; reasons.append('clean-selloff')
    else:
        score += 7
    if 0.009 <= m['bounce'] <= 0.030:
        score += 14; reasons.append('early-bounce')
    else:
        score += 8
    if m['rejection'] >= 1.0:
        score += 8; reasons.append('low-rejection')
    elif m['rejection'] >= 0.45:
        score += 4
    if m['ema9_slope'] > 0:
        score += 12; reasons.append('ema9-turned-up')
    else:
        score += 5
    if m['live'] >= m['ema9']:
        score += 10; reasons.append('ema9-reclaimed')
    if m['volx'] >= 2.0:
        score += 14; reasons.append('strong-volume-reversal')
    elif m['volx'] >= 1.45:
        score += 10; reasons.append('volume-reversal')
    else:
        score += 6
    if m['taker'] >= 0.72:
        score += 15; reasons.append('strong-taker-buy')
    elif m['taker'] >= 0.66:
        score += 11; reasons.append('taker-buy')
    else:
        score += 6
    if m['spread'] <= 0.05:
        score += 6; reasons.append('tight-spread')
    elif m['spread'] <= 0.08:
        score += 4
    if m['book'] >= 1.15:
        score += 5; reasons.append('bid-depth')
    elif m['book'] >= 1.0:
        score += 2
    if 40 <= m['rsi'] <= 60:
        score += 4; reasons.append('rsi-reset')
    return min(100.0, score), reasons


def early_reversal_context_ok(symbol: str) -> tuple[bool, str]:
    try:
        pf = entry_quality.runtime_preflight()
        if not bool(pf.get('btc_regime_ok')):
            return False, 'btc-regime-weak'
    except Exception as exc:
        return False, f'btc-preflight-failed:{type(exc).__name__}'

    now_ms = int(time.time() * 1000)
    r5 = [x for x in api('/klines', {'symbol': symbol, 'interval': '5m', 'limit': 60}) if int(x[6]) < now_ms]
    r15 = [x for x in api('/klines', {'symbol': symbol, 'interval': '15m', 'limit': 50}) if int(x[6]) < now_ms]
    if len(r5) < 40 or len(r15) < 35:
        return False, 'insufficient-htf'
    c5 = [float(x[4]) for x in r5]
    c15 = [float(x[4]) for x in r15]
    if c5[-1] / c5[-2] - 1.0 < -0.018:
        return False, '5m-waterfall-active'
    if c5[-1] / c5[-3] - 1.0 < -0.028:
        return False, '10m-waterfall-active'
    if c15[-1] / c15[-2] - 1.0 < -0.045:
        return False, '15m-capitulation-still-active'
    if c5[-1] / c5[-2] - 1.0 > 0.030:
        return False, '5m-bounce-too-extended'
    return True, 'ok'


def maybe_early_reversal_signal(symbol: str, ch24: float, volume24: float) -> bool:
    global last_global_signal, signals_today, execution_ready
    reset_day_counter(); now = time.time()
    if not execution_ready: return False
    if MAX_SIGNALS_PER_DAY > 0 and signals_today >= MAX_SIGNALS_PER_DAY: return False
    if now - last_global_signal < GLOBAL_COOLDOWN_SEC: return False
    if now - last_signal_by_pair.get(symbol, 0.0) < PAIR_COOLDOWN_SEC: return False

    m = early_reversal_metrics(symbol)
    score, reasons = score_early_reversal(m)
    print(f"[early-reversal-score] {symbol} score={score:.0f} ch24={ch24:+.1f}% dd={m['drawdown']*100:.2f}% bounce={m['bounce']*100:.2f}% low_age={m['low_age']}m reclaim={'Y' if m['reclaimed'] else 'N'} volx={m['volx']:.2f} taker={m['taker']*100:.1f}% rsi={m['rsi']:.1f} spread={m['spread']:.3f}%")
    if score < EARLY_REVERSAL_DIRECT_SCORE:
        return False

    ok, why = early_reversal_context_ok(symbol)
    if not ok:
        print(f'[early-reversal-context] {symbol} BLOCKED reason={why}')
        if '_record_candidate' in globals():
            _record_candidate(symbol, 'REVERSAL', score, m['live'], 'REJECT', why)
        return False

    low_buffer = clamp(m['atr'] * 0.45, 0.0020, 0.0050)
    structural_stop = m['low'] * (1.0 - low_buffer)
    sl_pct = 1.0 - structural_stop / m['live']
    if not (0.0075 <= sl_pct <= 0.0220):
        print(f'[early-reversal-risk] {symbol} BLOCKED structural_stop={sl_pct*100:.2f}%')
        return False
    tp_pct = clamp(max(0.015, sl_pct * 1.85), 0.015, 0.035)

    base_stake, free = dynamic_sizing.recommended_stake(sl_pct, max(score, 95.0))
    if base_stake is None:
        print(f'[early-reversal-sizing] {symbol} blocked free_usdt={free:.2f}')
        return False
    min_stake = max(5.0, float(os.getenv('MIN_STAKE_USDT', '5.0')))
    mult = clamp(EARLY_REVERSAL_STARTER_MULT, 0.25, 0.60)
    starter_cap = clamp(EARLY_REVERSAL_MAX_STAKE, min_stake, 20.0)
    stake = min(starter_cap, math.floor(base_stake * mult * 100.0) / 100.0)
    if stake < min_stake:
        stake = min(base_stake, min_stake)
    if stake < min_stake:
        return False

    entry = m['live']
    payload = {
        'id': f'{symbol}-REV-{int(now)}', 'symbol': symbol, 'entry': entry,
        'stop': entry * (1.0 - sl_pct), 'target': entry * (1.0 + tp_pct),
        'stakeUSDT': stake, 'score': round(score),
        'strategy': f"EARLY_REVERSAL_STARTER|score={score:.0f}|ch24={ch24:.1f}%|dd={m['drawdown']*100:.2f}%|bounce={m['bounce']*100:.2f}%|low_age={m['low_age']}m|volx={m['volx']:.2f}|taker={m['taker']*100:.1f}%|starter_mult={mult:.2f}",
        'dryRun': False,
    }

    # Shared expert/portfolio/Spot-Sniper authorization is injected later in
    # the build for every live fast_ingest path. Do not predeclare that helper
    # here, otherwise the legacy safety patch can incorrectly think it exists.
    row = fast_ingest(payload, timeout=30)
    if row.get('status') != 'FAST_SIGNAL_READY' or row.get('userConfirmationRequired') is not True or row.get('autoBuy') is not False:
        raise RuntimeError(f'unexpected early reversal ingest response: {row}')

    last_signal_by_pair[symbol] = now
    last_global_signal = now
    signals_today += 1
    print(f"[early-reversal] CONFIRMED-ready {symbol[:-4]}/USDT score={score:.0f} starter={row.get('recommendedUSDT')} requested={stake:.2f} reasons={','.join(reasons)}")
    return True

'''

if 'def early_reversal_candidates()' not in s:
    if main_marker not in s:
        raise SystemExit('early-reversal patch failed: main marker missing')
    s = s.replace(main_marker, helpers + main_marker, 1)

if 'reversal_ranked = []' not in s:
    anchor = "            ranked = []\n"
    pos = s.rfind(anchor)
    if pos < 0:
        raise SystemExit('early-reversal patch failed: base ranked marker missing')
    loop = """            reversal_ranked = []
            for rs, rch, rv in early_reversal_candidates():
                try:
                    rm = early_reversal_metrics(rs); rscore, _ = score_early_reversal(rm)
                    reversal_ranked.append((rscore, rs, rch, rv))
                    if '_record_candidate' in globals():
                        _record_candidate(rs, 'REVERSAL', rscore, float(rm.get('live') or 0), 'SCANNED', 'score-snapshot', drawdown=rm.get('drawdown'), bounce=rm.get('bounce'), low_age=rm.get('low_age'), reclaimed=rm.get('reclaimed'), volume_ratio=rm.get('volx'), taker_buy_ratio=rm.get('taker'), spread_pct=rm.get('spread'), rsi=rm.get('rsi'), change24=rch, volume24=rv)
                    if rscore >= EARLY_REVERSAL_WATCH_SCORE:
                        print(f"[early-reversal-watch] {rs} score={rscore:.0f} ch24={rch:+.1f}% dd={rm['drawdown']*100:.2f}% bounce={rm['bounce']*100:.2f}% low_age={rm['low_age']}m volx={rm['volx']:.2f} taker={rm['taker']*100:.1f}%")
                except Exception as exc:
                    print(f'[early-reversal-watch] {rs} metrics failed: {type(exc).__name__}: {exc}')
            reversal_ranked.sort(reverse=True, key=lambda x: x[0])
            for rscore, rs, rch, rv in reversal_ranked[:8]:
                if rscore < EARLY_REVERSAL_DIRECT_SCORE: break
                if not telegram_ok or not execution_ready: break
                try:
                    if maybe_early_reversal_signal(rs, rch, rv): break
                except Exception as exc:
                    print(f'[early-reversal] {rs} signal failed: {type(exc).__name__}: {exc}')
                    execution_ready = False

"""
    s = s[:pos] + loop + s[pos:]

startup_marker = "    last_chat_retry = 0.0\n"
if '[early-reversal] ONLINE' not in s:
    if startup_marker not in s:
        raise SystemExit('early-reversal patch failed: startup marker missing')
    startup = "    print(f'[early-reversal] ONLINE qv>={EARLY_REVERSAL_MIN_24H_QV:.0f} ch24={EARLY_REVERSAL_MIN_24H_PCT:.0f}..{EARLY_REVERSAL_MAX_24H_PCT:.0f}% watch={EARLY_REVERSAL_WATCH_SCORE:.0f} direct={EARLY_REVERSAL_DIRECT_SCORE:.0f} starter_mult={clamp(EARLY_REVERSAL_STARTER_MULT,0.25,0.60):.2f} max_stake={clamp(EARLY_REVERSAL_MAX_STAKE,5.0,20.0):.2f} confirmed_only=True')\n"
    s = s.replace(startup_marker, startup + startup_marker, 1)

for required in [
    'def early_reversal_candidates()',
    'def early_reversal_metrics(symbol: str)',
    'def score_early_reversal(m: dict)',
    'def maybe_early_reversal_signal(symbol: str, ch24: float, volume24: float)',
    'reversal_ranked = []',
    '[early-reversal] ONLINE',
]:
    if required not in s:
        raise SystemExit(f'early-reversal patch failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[early-reversal-patch] OK confirmed-only starter lane: recent selloff + fast reclaim + volume/taker confirmation + structural stop; shared live hard gates injected downstream; no OCO-unsafe pyramiding')
