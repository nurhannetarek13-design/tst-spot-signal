from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import fast_entry_engine as engine

SYMBOL = (os.getenv('NEW_LISTING_SYMBOL') or '').strip()
START_ISO = (os.getenv('NEW_LISTING_START_UTC') or '').strip()
OPENING_RANGE_MIN = int(os.getenv('NEW_LISTING_OPENING_RANGE_MINUTES', '60'))
STAKE_USDT = float(os.getenv('NEW_LISTING_INITIAL_STAKE_USDT', '5'))
MAX_OPEN_GAIN = float(os.getenv('NEW_LISTING_MAX_OPEN_GAIN', '0.25'))
USER_MAX_STOP_PCT = float(os.getenv('NEW_LISTING_MAX_STOP_PCT', '0.08'))
# Cloudflare's current hard live risk cap is $0.20. At a $5 minimum order that
# means a setup wider than 4% cannot be sent safely. Keep the stricter bound.
EXECUTION_RISK_CAP_USDT = float(os.getenv('NEW_LISTING_EXECUTION_RISK_CAP_USDT', '0.20'))
MAX_STOP_PCT = min(USER_MAX_STOP_PCT, EXECUTION_RISK_CAP_USDT / max(STAKE_USDT, 1e-9))
MIN_RR = float(os.getenv('NEW_LISTING_MIN_RR', '2.0'))
TARGET_RR = max(MIN_RR, float(os.getenv('NEW_LISTING_TARGET_RR', '2.2')))
MAX_SPREAD_PCT = float(os.getenv('NEW_LISTING_MAX_SPREAD_PCT', '0.50'))  # percent-valued
MIN_TAKER_RATIO = float(os.getenv('NEW_LISTING_MIN_TAKER_RATIO', '0.55'))
MIN_RECENT_QUOTE_VOL = float(os.getenv('NEW_LISTING_MIN_RECENT_QUOTE_VOL_USDT', '50000'))
MIN_BID_DEPTH = float(os.getenv('NEW_LISTING_MIN_BID_DEPTH_USDT', '5000'))
BREAKOUT_BUFFER = float(os.getenv('NEW_LISTING_BREAKOUT_BUFFER', '0.002'))
RETEST_TOUCH_ABOVE = float(os.getenv('NEW_LISTING_RETEST_TOUCH_ABOVE', '0.005'))
RETEST_MAX_UNDERCUT = float(os.getenv('NEW_LISTING_RETEST_MAX_UNDERCUT', '0.010'))
MAX_POST_RETEST_EXTENSION = float(os.getenv('NEW_LISTING_MAX_POST_RETEST_EXTENSION', '0.05'))
MODE_TTL_HOURS = float(os.getenv('NEW_LISTING_MODE_TTL_HOURS', '24'))
POLL_SEC = max(10, int(os.getenv('NEW_LISTING_POLL_SEC', '20')))


def _start_epoch() -> float:
    if not START_ISO:
        raise RuntimeError('NEW_LISTING_START_UTC missing')
    text = START_ISO.replace('Z', '+00:00')
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _closed(rows):
    now_ms = int(time.time() * 1000)
    return [x for x in rows if int(x[6]) < now_ms]


def _symbol_info() -> dict:
    row = engine.api('/exchangeInfo', {'symbol': SYMBOL})
    symbols = row.get('symbols') or []
    if not symbols:
        raise RuntimeError('spot symbol not active yet')
    return symbols[0]


def _min_notional(info: dict) -> float:
    out = 0.0
    for f in info.get('filters') or []:
        if f.get('filterType') in {'NOTIONAL', 'MIN_NOTIONAL'}:
            try:
                out = max(out, float(f.get('minNotional') or 0.0))
            except Exception:
                pass
    return out


def _book_snapshot() -> tuple[float, float, float]:
    top = engine.api('/ticker/bookTicker', {'symbol': SYMBOL})
    bid = float(top.get('bidPrice') or 0.0)
    ask = float(top.get('askPrice') or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError('bad order book top')
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0
    depth = engine.api('/depth', {'symbol': SYMBOL, 'limit': 20})
    bid_depth = sum(float(p) * float(q) for p, q in (depth.get('bids') or []))
    return ask, spread_pct, bid_depth


def _order_flow(rows_1m: list) -> tuple[float, float]:
    recent = rows_1m[-5:]
    base = sum(float(x[5]) for x in recent)
    taker = sum(float(x[9]) for x in recent)
    quote = sum(float(x[7]) for x in recent)
    return (taker / base if base > 0 else 0.0), quote


def _preflight() -> bool:
    payload = {
        'id': f'newlisting-preflight-{int(time.time())}',
        'symbol': SYMBOL,
        'entry': 100.0,
        'stop': 99.0,
        'target': 102.2,
        'stakeUSDT': STAKE_USDT,
        'score': 95,
        'strategy': 'NEW_LISTING_EXECUTION_PREFLIGHT',
        'dryRun': True,
    }
    try:
        row = engine.fast_ingest(payload, timeout=30)
        ok = (
            row.get('status') == 'FAST_SIGNAL_DRYRUN_OK'
            and row.get('userConfirmationRequired') is True
            and row.get('autoBuy') is False
            and float(row.get('recommendedUSDT') or 0) >= 5.0
        )
        print(
            f'[new-listing-preflight] {"OK" if ok else "FAIL"} symbol={SYMBOL} '
            f'recommended={row.get("recommendedUSDT")} autoBuy={row.get("autoBuy")}',
            flush=True,
        )
        return ok
    except Exception as exc:
        print(f'[new-listing-preflight] FAIL {type(exc).__name__}: {exc}', flush=True)
        return False


def _find_setup(start_ms: int, range_end_ms: int):
    rows = _closed(engine.api('/klines', {
        'symbol': SYMBOL,
        'interval': '1m',
        'startTime': start_ms,
        'limit': 500,
    }))
    opening = [x for x in rows if start_ms <= int(x[0]) < range_end_ms]
    if len(opening) < OPENING_RANGE_MIN:
        return None, f'opening-range-building candles={len(opening)}/{OPENING_RANGE_MIN}'

    opening = opening[:OPENING_RANGE_MIN]
    open_price = float(opening[0][1])
    range_high = max(float(x[2]) for x in opening)
    range_low = min(float(x[3]) for x in opening)
    if open_price <= 0 or range_high <= range_low:
        return None, 'bad-opening-range'

    # Permanent anti-chase rule: once the listing has printed >25% above its
    # opening price, this mode will not enter later on a delayed retest.
    if any(float(x[2]) > open_price * (1.0 + MAX_OPEN_GAIN) for x in rows):
        return 'CANCELLED', f'price-exceeded-open-limit maxGain>{MAX_OPEN_GAIN*100:.0f}%'

    bars15 = _closed(engine.api('/klines', {
        'symbol': SYMBOL,
        'interval': '15m',
        'startTime': range_end_ms,
        'limit': 100,
    }))
    breakout = None
    for b in bars15:
        if int(b[0]) < range_end_ms:
            continue
        if float(b[4]) > range_high * (1.0 + BREAKOUT_BUFFER):
            breakout = b
            break
    if breakout is None:
        return None, f'waiting-15m-breakout rangeHigh={range_high:.8g}'

    breakout_close_ms = int(breakout[6])
    after = [x for x in rows if int(x[0]) > breakout_close_ms]
    if not after:
        return None, 'breakout-confirmed-waiting-retest'

    retest = None
    for x in after:
        low = float(x[3])
        close = float(x[4])
        touched = low <= range_high * (1.0 + RETEST_TOUCH_ABOVE)
        not_failed = low >= range_high * (1.0 - RETEST_MAX_UNDERCUT)
        held = close >= range_high
        if touched and not_failed and held:
            retest = x
    if retest is None:
        return None, 'breakout-confirmed-waiting-successful-retest'

    info = _symbol_info()
    min_notional = _min_notional(info)
    if min_notional > STAKE_USDT + 1e-9:
        return 'CANCELLED', f'min-notional={min_notional:.4f}>stake={STAKE_USDT:.2f}'

    ask, spread_pct, bid_depth = _book_snapshot()
    if spread_pct > MAX_SPREAD_PCT:
        return None, f'spread-too-wide={spread_pct:.3f}%'
    if bid_depth < MIN_BID_DEPTH:
        return None, f'bid-depth-too-low={bid_depth:.0f}'

    taker_ratio, recent_quote = _order_flow(rows)
    if taker_ratio < MIN_TAKER_RATIO:
        return None, f'taker-too-weak={taker_ratio*100:.1f}%'
    if recent_quote < MIN_RECENT_QUOTE_VOL:
        return None, f'recent-liquidity-too-low={recent_quote:.0f}'

    if ask > open_price * (1.0 + MAX_OPEN_GAIN):
        return 'CANCELLED', 'live-price-above-open-limit'
    extension = ask / range_high - 1.0
    if extension > MAX_POST_RETEST_EXTENSION:
        return None, f'post-retest-extension-too-high={extension*100:.2f}%'

    retest_low = float(retest[3])
    stop = min(retest_low, range_high) * 0.995
    if not (0 < stop < ask):
        return None, 'invalid-stop'
    stop_pct = (ask - stop) / ask
    if stop_pct > MAX_STOP_PCT:
        return None, f'stop-too-wide={stop_pct*100:.2f}% max={MAX_STOP_PCT*100:.2f}%'

    target = ask + (ask - stop) * TARGET_RR
    rr = (target - ask) / (ask - stop)
    if rr < MIN_RR:
        return None, f'rr-too-low={rr:.2f}'

    return {
        'entry': ask,
        'stop': stop,
        'target': target,
        'rr': rr,
        'range_high': range_high,
        'range_low': range_low,
        'open_price': open_price,
        'spread_pct': spread_pct,
        'bid_depth': bid_depth,
        'taker_ratio': taker_ratio,
        'recent_quote': recent_quote,
        'breakout_open_ms': int(breakout[0]),
        'retest_open_ms': int(retest[0]),
        'min_notional': min_notional,
    }, 'READY'


def main() -> None:
    if not SYMBOL:
        print('[new-listing] DISABLED NEW_LISTING_SYMBOL missing', flush=True)
        return
    start = _start_epoch()
    range_end = start + OPENING_RANGE_MIN * 60
    expires = start + MODE_TTL_HOURS * 3600
    print(
        f'[new-listing] ARMED symbol={SYMBOL} startUTC={datetime.fromtimestamp(start, timezone.utc).isoformat()} '
        f'entryAfterUTC={datetime.fromtimestamp(range_end, timezone.utc).isoformat()} stake={STAKE_USDT:.2f} '
        f'maxStop={MAX_STOP_PCT*100:.2f}% rr>={MIN_RR:.1f} autoBuy=False confirmationRequired=True',
        flush=True,
    )

    preflight_ok = False
    last_preflight = 0.0
    last_status = ''
    signal_sent = False

    while time.time() < expires:
        now = time.time()
        try:
            if not preflight_ok and now - last_preflight >= 60:
                preflight_ok = _preflight()
                last_preflight = now

            if now < start:
                status = f'monitor-waiting-for-listing seconds={int(start-now)}'
            elif now < range_end:
                # No entry path exists in this branch. First hour is observation only.
                try:
                    rows = _closed(engine.api('/klines', {
                        'symbol': SYMBOL, 'interval': '1m', 'startTime': int(start*1000), 'limit': 90,
                    }))
                    opening = [x for x in rows if int(x[0]) < int(range_end*1000)]
                    if opening:
                        hi = max(float(x[2]) for x in opening)
                        lo = min(float(x[3]) for x in opening)
                        status = f'FIRST_HOUR_MONITOR_ONLY candles={len(opening)}/{OPENING_RANGE_MIN} high={hi:.8g} low={lo:.8g}'
                    else:
                        status = 'FIRST_HOUR_MONITOR_ONLY waiting-for-spot-candles'
                except Exception:
                    status = 'FIRST_HOUR_MONITOR_ONLY spot-not-active-yet'
            elif signal_sent:
                status = 'signal-already-sent-no-repeat'
            elif not preflight_ok:
                status = 'execution-preflight-not-ready'
            else:
                setup, reason = _find_setup(int(start * 1000), int(range_end * 1000))
                if setup == 'CANCELLED':
                    print(f'[new-listing] CANCELLED symbol={SYMBOL} reason={reason}', flush=True)
                    return
                if isinstance(setup, dict):
                    signal_id = f'newlisting-{setup["breakout_open_ms"]//1000}-{setup["retest_open_ms"]//1000}'
                    payload = {
                        'id': signal_id,
                        'symbol': SYMBOL,
                        'entry': setup['entry'],
                        'stop': setup['stop'],
                        'target': setup['target'],
                        'stakeUSDT': STAKE_USDT,
                        'score': 95,
                        'strategy': (
                            f'NEW_LISTING_ORB_RETEST|rr={setup["rr"]:.2f}|spread={setup["spread_pct"]:.3f}%|'
                            f'taker={setup["taker_ratio"]*100:.1f}%'
                        ),
                        'dryRun': False,
                    }
                    row = engine.fast_ingest(payload, timeout=30)
                    if row.get('status') != 'FAST_SIGNAL_READY' or row.get('userConfirmationRequired') is not True or row.get('autoBuy') is not False:
                        raise RuntimeError(f'unexpected live signal response: {row}')
                    signal_sent = True
                    print(
                        f'[new-listing] SIGNAL_READY symbol={SYMBOL} amount={row.get("recommendedUSDT")} '
                        f'entry={setup["entry"]:.8g} stop={setup["stop"]:.8g} target={setup["target"]:.8g} '
                        f'rr={setup["rr"]:.2f} spread={setup["spread_pct"]:.3f}% taker={setup["taker_ratio"]*100:.1f}% '
                        f'autoBuy=False confirmationRequired=True',
                        flush=True,
                    )
                    status = 'signal-ready-waiting-user-confirmation'
                else:
                    status = reason

            if status != last_status:
                print(f'[new-listing] {SYMBOL} {status}', flush=True)
                last_status = status
        except Exception as exc:
            status = f'watch-warning {type(exc).__name__}: {str(exc)[:180]}'
            if status != last_status:
                print(f'[new-listing] {SYMBOL} {status}', flush=True)
                last_status = status
        time.sleep(POLL_SEC)

    print(f'[new-listing] EXPIRED symbol={SYMBOL} no further dedicated listing entries', flush=True)


if __name__ == '__main__':
    main()
