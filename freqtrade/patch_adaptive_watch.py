from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Adaptive Watch -> Confirm -> Signal layer.
# Candidates in the near-miss band are kept alive for a short period and
# rechecked every engine cycle. A lower watch score NEVER bypasses the existing
# ignition, microstructure, higher-timeframe, BTC, spread, sizing or execution
# gates. Only a candidate that reaches DIRECT_SCORE can continue to maybe_signal.
min_score_marker = "MIN_SCORE = float(os.getenv('FAST_MIN_SCORE', '82'))\n"
adaptive_constants = min_score_marker + (
    "WATCH_SCORE = float(os.getenv('FAST_WATCH_SCORE', '80'))\n"
    "DIRECT_SCORE = float(os.getenv('FAST_DIRECT_SCORE', '88'))\n"
    "WATCH_TTL_SEC = int(os.getenv('FAST_WATCH_TTL_SEC', '300'))\n"
    "# MIN_SCORE remains the single downstream signal gate; bind it to the\n"
    "# adaptive direct threshold so existing fail-closed checks stay intact.\n"
    "MIN_SCORE = DIRECT_SCORE\n"
)
if 'WATCH_SCORE = float(' not in s:
    if min_score_marker not in s:
        raise SystemExit('adaptive-watch patch failed: MIN_SCORE marker missing')
    s = s.replace(min_score_marker, adaptive_constants, 1)

state_marker = "execution_ready = False\n"
state_insert = state_marker + (
    "adaptive_watch: dict[str, dict] = {}\n"
    "near_miss_records: list[dict] = []\n"
)
if 'adaptive_watch: dict[str, dict]' not in s:
    if state_marker not in s:
        raise SystemExit('adaptive-watch patch failed: state marker missing')
    s = s.replace(state_marker, state_insert, 1)

candidate_marker = "\ndef candidate_symbols(scan: dict) -> list[tuple[str, float, float]]:\n"
helpers = r'''

def _watch_live_price(m: dict) -> float:
    return float(m.get('live_price') or m.get('last') or 0.0)


def register_adaptive_watch(symbol: str, score: float, change24: float, volume24: float, m: dict) -> None:
    now = time.time()
    price = _watch_live_price(m)
    row = adaptive_watch.get(symbol)
    if row is None:
        adaptive_watch[symbol] = {
            'first_at': now,
            'expires_at': now + WATCH_TTL_SEC,
            'first_score': float(score),
            'max_score': float(score),
            'change24': float(change24),
            'volume24': float(volume24),
            'first_price': price,
        }
        near_miss_records.append({
            'symbol': symbol,
            'start_at': now,
            'start_price': price,
            'start_score': float(score),
            'reported15': False,
            'reported60': False,
        })
        print(
            f'[adaptive-watch] START {symbol} score={score:.0f} price={price:.8g} '
            f'ttl={WATCH_TTL_SEC}s direct={DIRECT_SCORE:.0f}'
        )
        return

    row['expires_at'] = max(float(row.get('expires_at', 0.0)), now + WATCH_TTL_SEC)
    row['max_score'] = max(float(row.get('max_score', score)), float(score))
    row['change24'] = float(change24)
    row['volume24'] = float(volume24)


def active_watch_candidates() -> list[tuple[str, float, float]]:
    now = time.time()
    out: list[tuple[str, float, float]] = []
    for symbol, row in list(adaptive_watch.items()):
        if float(row.get('expires_at', 0.0)) <= now:
            print(
                f"[adaptive-watch] EXPIRE {symbol} first={float(row.get('first_score', 0)):.0f} "
                f"max={float(row.get('max_score', 0)):.0f}"
            )
            adaptive_watch.pop(symbol, None)
            continue
        out.append((symbol, float(row.get('change24', 0.0)), float(row.get('volume24', 0.0))))
    return out


def evaluate_near_miss_outcomes() -> None:
    """Log 15m/1h forward outcomes so thresholds can be tuned from evidence."""
    now = time.time()
    for rec in list(near_miss_records):
        age = now - float(rec.get('start_at', now))
        needs15 = age >= 15 * 60 and not bool(rec.get('reported15'))
        needs60 = age >= 60 * 60 and not bool(rec.get('reported60'))
        if not (needs15 or needs60):
            continue
        try:
            book = api('/ticker/bookTicker', {'symbol': rec['symbol']})
            bid = float(book.get('bidPrice') or 0.0)
            ask = float(book.get('askPrice') or 0.0)
            price = (bid + ask) / 2.0 if bid > 0 and ask > 0 else max(bid, ask)
            start = float(rec.get('start_price') or 0.0)
            ret = (price / start - 1.0) * 100.0 if price > 0 and start > 0 else 0.0
            if needs15:
                rec['reported15'] = True
                print(
                    f"[near-miss-outcome] 15m {rec['symbol']} start_score={float(rec.get('start_score', 0)):.0f} "
                    f"return={ret:+.3f}% start={start:.8g} now={price:.8g}"
                )
            if needs60:
                rec['reported60'] = True
                print(
                    f"[near-miss-outcome] 1h {rec['symbol']} start_score={float(rec.get('start_score', 0)):.0f} "
                    f"return={ret:+.3f}% start={start:.8g} now={price:.8g}"
                )
        except Exception as exc:
            print(f"[near-miss-outcome] {rec.get('symbol')} check failed: {type(exc).__name__}: {exc}")
        if bool(rec.get('reported60')):
            near_miss_records.remove(rec)

'''
if 'def register_adaptive_watch(' not in s:
    if candidate_marker not in s:
        raise SystemExit('adaptive-watch patch failed: candidate marker missing')
    s = s.replace(candidate_marker, helpers + candidate_marker, 1)

# Preserve watched candidates even if they briefly drop out of the rolling mover
# discovery queue. This is what makes the 3-5 minute recheck real rather than a
# cosmetic log-only watch state.
old_candidate_loop = "            ranked = []\n            for symbol, change, volume in candidate_symbols(scan):\n"
new_candidate_loop = """            ranked = []
            candidate_rows = candidate_symbols(scan)
            candidate_seen = {symbol for symbol, _, _ in candidate_rows}
            for watched in active_watch_candidates():
                if watched[0] not in candidate_seen:
                    candidate_rows.insert(0, watched)
                    candidate_seen.add(watched[0])
            for symbol, change, volume in candidate_rows:
"""
if 'candidate_rows = candidate_symbols(scan)' not in s:
    if old_candidate_loop not in s:
        raise SystemExit('adaptive-watch patch failed: ranked candidate loop marker missing')
    s = s.replace(old_candidate_loop, new_candidate_loop, 1)

# Register 80-87 near misses before the existing direct-signal loop. If a watched
# candidate reaches 88+, it is promoted, then the original maybe_signal path still
# performs the fresh-ignition and all fail-closed quality checks.
sort_marker = "            ranked.sort(reverse=True, key=lambda x: x[0])\n"
watch_block = sort_marker + """            for score, symbol, change, volume, m in ranked[:20]:
                if WATCH_SCORE <= score < DIRECT_SCORE:
                    register_adaptive_watch(symbol, score, change, volume, m)
                elif score >= DIRECT_SCORE and symbol in adaptive_watch:
                    watched = adaptive_watch.pop(symbol)
                    print(
                        f"[adaptive-watch] PROMOTE {symbol} score={score:.0f} "
                        f"from={float(watched.get('first_score', 0)):.0f} -> direct-gate"
                    )
            evaluate_near_miss_outcomes()
"""
if '[adaptive-watch] PROMOTE' not in s:
    if sort_marker not in s:
        raise SystemExit('adaptive-watch patch failed: ranked sort marker missing')
    s = s.replace(sort_marker, watch_block, 1)

# Make runtime diagnostics explicit.
for old in [
    "f'[fast-engine] ONLINE mode=MOMENTUM_GROWTH min_score={MIN_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '",
    "f'[fast-engine] ONLINE mode=MOMENTUM_IGNITION min_score={MIN_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '",
    "f'[fast-engine] ONLINE mode=PRE_MOMENTUM min_score={MIN_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '",
]:
    if old in s:
        s = s.replace(
            old,
            "f'[fast-engine] ONLINE mode=ADAPTIVE_WATCH watch={WATCH_SCORE:.0f} direct={DIRECT_SCORE:.0f} max/day={MAX_SIGNALS_PER_DAY} '",
            1,
        )
        break

for required in [
    'WATCH_SCORE = float(',
    'DIRECT_SCORE = float(',
    'MIN_SCORE = DIRECT_SCORE',
    'def register_adaptive_watch(',
    'def active_watch_candidates(',
    'def evaluate_near_miss_outcomes(',
    'candidate_rows = candidate_symbols(scan)',
    '[adaptive-watch] PROMOTE',
]:
    if required not in s:
        raise SystemExit(f'adaptive-watch patch failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[adaptive-watch-patch] OK watch>=80 direct>=88 + 5m persistence + 15m/1h outcome logging; hard gates unchanged')
