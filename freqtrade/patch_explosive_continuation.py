from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Separate momentum-continuation path for assets that are already +25% to +120%
# on the day. This does NOT relax the normal pre-momentum anti-chase path.
# It only allows a fresh signal after a real pullback + reclaim with strict
# liquidity, microstructure, trend and BTC-regime confirmation.
const_marker = "WATCH_TTL_SEC = int(os.getenv('FAST_WATCH_TTL_SEC', '300'))\n"
const_insert = const_marker + (
    "EXPLOSIVE_MIN_24H_PCT = float(os.getenv('FAST_EXPLOSIVE_MIN_24H_PCT', '25'))\n"
    "EXPLOSIVE_MAX_24H_PCT = float(os.getenv('FAST_EXPLOSIVE_MAX_24H_PCT', '120'))\n"
    "EXPLOSIVE_MIN_24H_QV = float(os.getenv('FAST_EXPLOSIVE_MIN_24H_QV', '10000000'))\n"
    "EXPLOSIVE_WATCH_SCORE = float(os.getenv('FAST_EXPLOSIVE_WATCH_SCORE', '72'))\n"
    "EXPLOSIVE_DIRECT_SCORE = float(os.getenv('FAST_EXPLOSIVE_DIRECT_SCORE', '86'))\n"
    "EXPLOSIVE_MAX_CANDIDATES = int(os.getenv('FAST_EXPLOSIVE_MAX_CANDIDATES', '10'))\n"
    "EXPLOSIVE_REFRESH_SEC = int(os.getenv('FAST_EXPLOSIVE_REFRESH_SEC', '45'))\n"
)
if 'EXPLOSIVE_MIN_24H_PCT = float(' not in s:
    if const_marker not in s:
        raise SystemExit('explosive patch failed: adaptive constants marker missing')
    s = s.replace(const_marker, const_insert, 1)

state_marker = "near_miss_records: list[dict] = []\n"
state_insert = state_marker + (
    "_explosive_cache_at = 0.0\n"
    "_explosive_cache: list[tuple[str, float, float]] = []\n"
    "_explosive_last_watch_log: dict[str, float] = {}\n"
)
if '_explosive_cache_at = 0.0' not in s:
    if state_marker not in s:
        raise SystemExit('explosive patch failed: adaptive state marker missing')
    s = s.replace(state_marker, state_insert, 1)

main_marker = "\ndef main() -> None:\n"
helpers = r'''

def explosive_candidates() -> list[tuple[str, float, float]]:
    """Return liquid +25%..+120% 24h movers for a separate continuation path."""
    global _explosive_cache_at, _explosive_cache
    now = time.time()
    if _explosive_cache and now - _explosive_cache_at < EXPLOSIVE_REFRESH_SEC:
        return list(_explosive_cache)
    try:
        rows = api('/ticker/24hr', {})
        if not isinstance(rows, list):
            raise RuntimeError('24h ticker universe was not a list')
        found: list[tuple[str, float, float]] = []
        for row in rows:
            symbol = str(row.get('symbol') or '')
            if not symbol_ok(symbol):
                continue
            change24 = float(row.get('priceChangePercent') or 0.0)
            qv24 = float(row.get('quoteVolume') or 0.0)
            if qv24 < EXPLOSIVE_MIN_24H_QV:
                continue
            if EXPLOSIVE_MIN_24H_PCT <= change24 <= EXPLOSIVE_MAX_24H_PCT:
                found.append((symbol, change24, qv24))
        # Liquidity first. A huge percentage move alone is never a reason to trade.
        found.sort(key=lambda x: (x[2], x[1]), reverse=True)
        _explosive_cache = found[:EXPLOSIVE_MAX_CANDIDATES]
        _explosive_cache_at = now
        preview = ', '.join(f'{sym}:{chg:+.1f}%' for sym, chg, _ in _explosive_cache[:6]) or 'none'
        print(f'[explosive-discovery] candidates={len(_explosive_cache)} range={EXPLOSIVE_MIN_24H_PCT:.0f}-{EXPLOSIVE_MAX_24H_PCT:.0f}% top={preview}')
        return list(_explosive_cache)
    except Exception as exc:
        print(f'[explosive-discovery] warning {type(exc).__name__}: {exc}')
        return list(_explosive_cache)


def explosive_metrics(symbol: str) -> dict:
    rows = api('/klines', {'symbol': symbol, 'interval': '1m', 'limit': 120})
    now_ms = int(time.time() * 1000)
    rows = [x for x in rows if int(x[6]) < now_ms]
    if not isinstance(rows, list) or len(rows) < 100:
        raise RuntimeError('insufficient closed 1m candles')

    opens = [float(x[1]) for x in rows]
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    closes = [float(x[4]) for x in rows]
    base_vol = [float(x[5]) for x in rows]
    quote_vol = [float(x[7]) for x in rows]
    taker_buy_base = [float(x[9]) for x in rows]

    book = api('/ticker/bookTicker', {'symbol': symbol})
    bid = float(book.get('bidPrice') or 0.0)
    ask = float(book.get('askPrice') or 0.0)
    bid_qty = float(book.get('bidQty') or 0.0)
    ask_qty = float(book.get('askQty') or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError('bad live book')
    live = (bid + ask) / 2.0
    spread_pct = (ask - bid) / live * 100.0
    top_book_ratio = bid_qty / ask_qty if ask_qty > 0 else 0.0

    # Find an established local peak, then require a pullback after that peak.
    peak_start = max(0, len(rows) - 60)
    peak_end = len(rows) - 6
    if peak_end - peak_start < 20:
        raise RuntimeError('insufficient peak window')
    peak_idx = max(range(peak_start, peak_end), key=lambda i: highs[i])
    peak = highs[peak_idx]
    if peak <= 0 or peak_idx >= len(rows) - 5:
        raise RuntimeError('no mature local peak')

    post_peak_indices = list(range(peak_idx + 1, len(rows) - 1))
    if len(post_peak_indices) < 4:
        raise RuntimeError('pullback not mature')
    pull_idx = min(post_peak_indices, key=lambda i: lows[i])
    pullback_low = lows[pull_idx]
    if pullback_low <= 0:
        raise RuntimeError('bad pullback low')

    reclaim_start = max(pull_idx + 1, len(rows) - 9)
    reclaim_end = len(rows) - 2
    if reclaim_end <= reclaim_start:
        reclaim_level = closes[-3]
    else:
        reclaim_level = max(highs[reclaim_start:reclaim_end])

    pullback_depth = peak / pullback_low - 1.0
    bounce_from_low = live / pullback_low - 1.0
    distance_from_peak = peak / live - 1.0
    reclaimed = live >= reclaim_level * 0.9995

    ema9 = ema(closes[-40:], 9)
    ema21 = ema(closes[-60:], 21)
    rsi14 = rsi(closes, 14)
    mom1 = live / closes[-2] - 1.0
    mom5 = live / closes[-6] - 1.0

    recent_qv = sum(quote_vol[-5:]) / 5.0
    prior_qv = sum(quote_vol[-25:-5]) / 20.0
    volume_ratio = recent_qv / prior_qv if prior_qv > 0 else 0.0
    recent_base = sum(base_vol[-5:])
    recent_taker = sum(taker_buy_base[-5:])
    taker_buy_ratio = recent_taker / recent_base if recent_base > 0 else 0.0

    latest_body = abs(closes[-1] - opens[-1])
    latest_upper = highs[-1] - max(opens[-1], closes[-1])
    wick_ratio = latest_upper / max(latest_body, live * 0.0001)
    recent_one_minute_impulse = max(closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - 5, len(closes)))

    trs = []
    for i in range(-20, 0):
        prev = closes[i - 1]
        tr = max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev))
        trs.append(tr)
    atr_pct = (sum(trs) / len(trs)) / live if trs else 0.0

    return {
        'live': live,
        'peak': peak,
        'pullback_low': pullback_low,
        'reclaim_level': reclaim_level,
        'pullback_depth': pullback_depth,
        'bounce_from_low': bounce_from_low,
        'distance_from_peak': distance_from_peak,
        'reclaimed': reclaimed,
        'ema9': ema9,
        'ema21': ema21,
        'rsi': rsi14,
        'mom1': mom1,
        'mom5': mom5,
        'volume_ratio': volume_ratio,
        'taker_buy_ratio': taker_buy_ratio,
        'spread_pct': spread_pct,
        'top_book_ratio': top_book_ratio,
        'wick_ratio': wick_ratio,
        'recent_one_minute_impulse': recent_one_minute_impulse,
        'atr_pct': atr_pct,
    }


def score_explosive_continuation(m: dict) -> tuple[float, list[str]]:
    reasons: list[str] = []

    # Hard anti-chase / fake-reclaim gates. A +100% day is not itself a buy signal.
    if not (0.015 <= m['pullback_depth'] <= 0.10):
        return 0.0, ['no-valid-pullback']
    if m['bounce_from_low'] < 0.006 or m['bounce_from_low'] > 0.065:
        return 0.0, ['bounce-not-fresh']
    if m['distance_from_peak'] < -0.008 or m['distance_from_peak'] > 0.055:
        return 0.0, ['too-far-from-retest-zone']
    if m['spread_pct'] > 0.15:
        return 0.0, ['spread-too-wide']
    if m['taker_buy_ratio'] < 0.55:
        return 0.0, ['buy-pressure-too-weak']
    if m['mom5'] < -0.004 or m['mom5'] > 0.045:
        return 0.0, ['reclaim-momentum-invalid']
    if m['recent_one_minute_impulse'] > 0.030:
        return 0.0, ['new-blowoff-candle']
    if m['wick_ratio'] > 2.5:
        return 0.0, ['rejection-wick']

    score = 0.0
    if 0.02 <= m['pullback_depth'] <= 0.07:
        score += 18; reasons.append('healthy-pullback')
    else:
        score += 10
    if m['reclaimed']:
        score += 20; reasons.append('reclaim')
    if m['live'] >= m['ema9'] and m['ema9'] >= m['ema21'] * 0.998:
        score += 12; reasons.append('trend-recovered')
    if m['volume_ratio'] >= 1.35:
        score += 16; reasons.append('volume-expansion')
    elif m['volume_ratio'] >= 1.10:
        score += 10
    if m['taker_buy_ratio'] >= 0.68:
        score += 18; reasons.append('strong-taker-buy')
    elif m['taker_buy_ratio'] >= 0.60:
        score += 13; reasons.append('taker-buy')
    else:
        score += 5
    if m['spread_pct'] <= 0.08:
        score += 7; reasons.append('tight-spread')
    elif m['spread_pct'] <= 0.12:
        score += 4
    if 52.0 <= m['rsi'] <= 76.0:
        score += 5; reasons.append('rsi-recovered-not-extreme')
    if m['top_book_ratio'] >= 1.05:
        score += 4; reasons.append('bid-support')

    return max(0.0, min(100.0, score)), reasons


def explosive_trend_context(symbol: str) -> tuple[bool, str, dict]:
    now_ms = int(time.time() * 1000)
    rows15 = api('/klines', {'symbol': symbol, 'interval': '15m', 'limit': 80})
    rows1h = api('/klines', {'symbol': symbol, 'interval': '1h', 'limit': 80})
    rows15 = [x for x in rows15 if int(x[6]) < now_ms]
    rows1h = [x for x in rows1h if int(x[6]) < now_ms]
    if len(rows15) < 60 or len(rows1h) < 55:
        return False, 'insufficient-higher-timeframe-data', {}

    c15 = [float(x[4]) for x in rows15]
    c1h = [float(x[4]) for x in rows1h]
    ema20_15 = ema(c15[-60:], 20)
    ema50_15 = ema(c15[-75:], 50)
    ema20_1h = ema(c1h[-60:], 20)
    ema50_1h = ema(c1h[-75:], 50)
    last15_ret = c15[-1] / c15[-2] - 1.0

    # Continuation must still be structurally bullish, but the current 15m candle
    # must not itself be another vertical blow-off.
    if c15[-1] < ema20_15 * 0.992 or ema20_15 < ema50_15 * 0.985:
        return False, '15m-continuation-trend-failed', {}
    if c1h[-1] < ema20_1h * 0.975 or ema20_1h < ema50_1h * 0.965:
        return False, '1h-continuation-trend-failed', {}
    if last15_ret > 0.060:
        return False, '15m-blowoff-still-active', {}

    btc = entry_quality.runtime_preflight()
    if not bool(btc.get('btc_regime_ok')):
        return False, 'btc-regime-weak', {'btc': btc}
    return True, 'continuation-context-ok', {
        'ema20_15': ema20_15,
        'ema50_15': ema50_15,
        'ema20_1h': ema20_1h,
        'ema50_1h': ema50_1h,
        'last15_ret': last15_ret,
        'btc': btc,
    }


def maybe_explosive_signal(symbol: str, change24: float, volume24: float) -> bool:
    global last_global_signal, signals_today, execution_ready
    reset_day_counter()
    now = time.time()
    if not execution_ready:
        return False
    if MAX_SIGNALS_PER_DAY > 0 and signals_today >= MAX_SIGNALS_PER_DAY:
        return False
    if now - last_global_signal < GLOBAL_COOLDOWN_SEC:
        return False
    if now - last_signal_by_pair.get(symbol, 0.0) < PAIR_COOLDOWN_SEC:
        return False

    m = explosive_metrics(symbol)
    score, reasons = score_explosive_continuation(m)
    print(
        f"[explosive-score] {symbol} score={score:.0f} ch24={change24:+.1f}% "
        f"pullback={m['pullback_depth']*100:.2f}% bounce={m['bounce_from_low']*100:.2f}% "
        f"reclaim={'Y' if m['reclaimed'] else 'N'} peakDist={m['distance_from_peak']*100:.2f}% "
        f"mom5={m['mom5']*100:+.2f}% volx={m['volume_ratio']:.2f} "
        f"taker={m['taker_buy_ratio']*100:.1f}% rsi={m['rsi']:.1f} spread={m['spread_pct']:.3f}%"
    )
    if score < EXPLOSIVE_DIRECT_SCORE:
        return False
    if not m['reclaimed']:
        return False

    context_ok, context_reason, context = explosive_trend_context(symbol)
    if not context_ok:
        print(f'[explosive-context] {symbol} BLOCKED reason={context_reason}')
        return False

    # Volatility-aware but still bounded. Dynamic sizing converts this stop into
    # a capped dollar risk and still respects the existing daily-loss budget.
    sl_pct = clamp(max(0.010, m['atr_pct'] * 3.2), 0.010, 0.022)
    tp_pct = clamp(sl_pct * 1.65, 0.017, 0.038)
    entry = m['live']
    tp = entry * (1.0 + tp_pct)
    sl = entry * (1.0 - sl_pct)

    stake_usdt, free_usdt = dynamic_sizing.recommended_stake(sl_pct, max(score, 90.0))
    if stake_usdt is None:
        print(f'[explosive-sizing] {symbol} blocked: free_usdt={free_usdt:.2f}')
        return False

    tag = (
        f"EXPLOSIVE_CONTINUATION|score={score:.0f}|ch24={change24:.1f}%|"
        f"pullback={m['pullback_depth']*100:.2f}%|bounce={m['bounce_from_low']*100:.2f}%|"
        f"volx={m['volume_ratio']:.2f}|taker={m['taker_buy_ratio']*100:.1f}%|"
        f"rsi={m['rsi']:.1f}|spread={m['spread_pct']:.3f}%"
    )
    payload = {
        'id': f'{symbol}-EXP-{int(now)}',
        'symbol': symbol,
        'entry': entry,
        'stop': sl,
        'target': tp,
        'stakeUSDT': stake_usdt,
        'score': round(score),
        'strategy': tag,
        'dryRun': False,
    }
    row = fast_ingest(payload, timeout=30)
    if row.get('status') != 'FAST_SIGNAL_READY' or row.get('userConfirmationRequired') is not True or row.get('autoBuy') is not False:
        raise RuntimeError(f'unexpected explosive one-tap ingest response: {row}')

    last_signal_by_pair[symbol] = now
    last_global_signal = now
    signals_today += 1
    print(
        f"[explosive-continuation] one-tap ready {symbol[:-4]}/USDT score={score:.0f} "
        f"amount={row.get('recommendedUSDT')} tp={tp_pct*100:.2f}% sl={sl_pct*100:.2f}% "
        f"reasons={','.join(reasons)}"
    )
    return True

'''
if 'def explosive_candidates()' not in s:
    if main_marker not in s:
        raise SystemExit('explosive patch failed: main marker missing')
    s = s.replace(main_marker, helpers + main_marker, 1)

# Scan explosive continuations independently of the normal pre-momentum queue.
loop_marker = "            scan = get_json(SCANNER_URL, timeout=25)\n            ranked = []\n"
loop_insert = """            scan = get_json(SCANNER_URL, timeout=25)
            explosive_ranked = []
            for exp_symbol, exp_change, exp_volume in explosive_candidates():
                try:
                    exp_m = explosive_metrics(exp_symbol)
                    exp_score, _ = score_explosive_continuation(exp_m)
                    explosive_ranked.append((exp_score, exp_symbol, exp_change, exp_volume, exp_m))
                    if exp_score >= EXPLOSIVE_WATCH_SCORE:
                        last_log = _explosive_last_watch_log.get(exp_symbol, 0.0)
                        if time.time() - last_log >= 180:
                            _explosive_last_watch_log[exp_symbol] = time.time()
                            print(
                                f"[explosive-watch] {exp_symbol} score={exp_score:.0f} ch24={exp_change:+.1f}% "
                                f"pullback={exp_m['pullback_depth']*100:.2f}% reclaim={'Y' if exp_m['reclaimed'] else 'N'} "
                                f"volx={exp_m['volume_ratio']:.2f} taker={exp_m['taker_buy_ratio']*100:.1f}%"
                            )
                except Exception as exc:
                    print(f'[explosive-watch] {exp_symbol} metrics failed: {type(exc).__name__}: {exc}')
            explosive_ranked.sort(reverse=True, key=lambda x: x[0])
            for exp_score, exp_symbol, exp_change, exp_volume, _ in explosive_ranked[:8]:
                if exp_score < EXPLOSIVE_DIRECT_SCORE:
                    break
                if not telegram_ok or not execution_ready:
                    break
                try:
                    if maybe_explosive_signal(exp_symbol, exp_change, exp_volume):
                        break
                except Exception as exc:
                    print(f'[explosive-continuation] {exp_symbol} signal failed: {type(exc).__name__}: {exc}')
                    execution_ready = False

            ranked = []
"""
if 'explosive_ranked = []' not in s:
    if loop_marker not in s:
        raise SystemExit('explosive patch failed: main loop marker missing')
    s = s.replace(loop_marker, loop_insert, 1)

startup_marker = "    last_chat_retry = 0.0\n"
startup_insert = (
    "    print(f'[explosive-continuation] ONLINE range={EXPLOSIVE_MIN_24H_PCT:.0f}-{EXPLOSIVE_MAX_24H_PCT:.0f}% '
"
    "          f'watch={EXPLOSIVE_WATCH_SCORE:.0f} direct={EXPLOSIVE_DIRECT_SCORE:.0f} min24hQV={EXPLOSIVE_MIN_24H_QV:.0f}')\n"
    + startup_marker
)
if '[explosive-continuation] ONLINE range=' not in s:
    if startup_marker not in s:
        raise SystemExit('explosive patch failed: startup marker missing')
    s = s.replace(startup_marker, startup_insert, 1)

for required in [
    'EXPLOSIVE_MIN_24H_PCT = float(',
    'def explosive_candidates()',
    'def explosive_metrics(',
    'def score_explosive_continuation(',
    'def explosive_trend_context(',
    'def maybe_explosive_signal(',
    '[explosive-watch]',
    '[explosive-continuation] one-tap ready',
]:
    if required not in s:
        raise SystemExit(f'explosive patch failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[explosive-continuation-patch] OK +25..+120% movers use pullback/reclaim continuation path; normal anti-chase gates unchanged')
