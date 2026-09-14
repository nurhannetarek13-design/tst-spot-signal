from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

if 'def _auto_execute_if_window(' in s:
    print('[sleep-auto-trading] already applied')
    raise SystemExit(0)

# We deliberately keep this inside the already-finalized fast-entry engine so
# automatic execution can only happen after the existing signal pipeline has
# produced FAST_SIGNAL_READY. WATCH candidates and dry-run preflights can never
# reach the execution branch.
import_marker = 'from datetime import datetime, timezone\n'
if import_marker not in s:
    raise SystemExit('sleep-auto-trading: datetime import marker missing')
s = s.replace(import_marker, import_marker + 'from zoneinfo import ZoneInfo\n', 1)

fast_marker = '\ndef fast_ingest(payload: dict, timeout: int = 25) -> dict:\n'
helper = r'''

AUTO_TRADING_ENABLED = os.getenv('AUTO_TRADING_ENABLED', '0').strip() == '1'
AUTO_TRADING_TZ = (os.getenv('AUTO_TRADING_TZ') or 'Africa/Cairo').strip()
AUTO_TRADING_START = (os.getenv('AUTO_TRADING_START') or '00:00').strip()
AUTO_TRADING_END = (os.getenv('AUTO_TRADING_END') or '13:00').strip()
AUTO_TRADING_MAX_STAKE_USDT = max(5.0, float(os.getenv('AUTO_TRADING_MAX_STAKE_USDT', '20')))
AUTO_LOCAL_RELAY_PORT = int(os.getenv('PORT', '8080'))


def _auto_hhmm(value: str) -> int:
    hh, mm = str(value).strip().split(':', 1)
    h = int(hh); m = int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError('bad HH:MM')
    return h * 60 + m


def _auto_window_active(epoch: float | None = None) -> bool:
    if not AUTO_TRADING_ENABLED:
        return False
    try:
        local = datetime.fromtimestamp(float(epoch or time.time()), ZoneInfo(AUTO_TRADING_TZ))
        now_min = local.hour * 60 + local.minute
        start_min = _auto_hhmm(AUTO_TRADING_START)
        end_min = _auto_hhmm(AUTO_TRADING_END)
    except Exception as exc:
        print(f'[auto-window] invalid schedule: {type(exc).__name__}: {exc}', flush=True)
        return False
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def _auto_required_score(payload: dict) -> float:
    strategy = str(payload.get('strategy') or '').upper()
    if strategy.startswith('EARLY_REVERSAL_STARTER'):
        return float(globals().get('EARLY_REVERSAL_DIRECT_SCORE', 95.0))
    if strategy.startswith('EXTREME_CONTINUATION'):
        return float(globals().get('EXTREME_DIRECT_SCORE', 95.0))
    if strategy.startswith('EXPLOSIVE_CONTINUATION'):
        return float(globals().get('EXPLOSIVE_DIRECT_SCORE', 95.0))
    if strategy.startswith('MID_MOMENTUM_CONTINUATION'):
        return float(globals().get('MID_DIRECT_SCORE', 95.0))
    return float(globals().get('DIRECT_SCORE', 88.0))


def _auto_local_exec(body: dict, timeout: int = 55) -> tuple[int, dict]:
    token = (os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
    if not token:
        raise RuntimeError('TELEGRAM_BOT_TOKEN missing for local execution relay')
    raw = json.dumps(body, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    stamp = str(int(time.time() * 1000))
    signature = hmac.new(token.encode('utf-8'), stamp.encode('utf-8') + b'.' + raw, hashlib.sha256).hexdigest()
    req = Request(
        f'http://127.0.0.1:{AUTO_LOCAL_RELAY_PORT}/make-exec-relay',
        data=raw,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'X-Make-Relay-Timestamp': stamp,
            'X-Make-Relay-Signature': signature,
            'User-Agent': 'tst-sleep-auto/1.0',
        },
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            return int(r.status), json.loads(r.read() or b'{}')
    except Exception as exc:
        status = int(getattr(exc, 'code', 599) or 599)
        try:
            data = exc.read() or b'{}'
            row = json.loads(data)
        except Exception:
            row = {'ok': False, 'status': f'AUTO_RELAY_{type(exc).__name__.upper()}'}
        return status, row


def _auto_tg(text: str) -> None:
    try:
        bridge.tg_api('sendMessage', {'text': text, 'disable_web_page_preview': True})
    except Exception as exc:
        print(f'[auto-window] telegram warning {type(exc).__name__}: {str(exc)[:140]}', flush=True)


def _auto_execute_if_window(payload: dict, ingest_row: dict) -> dict | None:
    if not _auto_window_active():
        return None
    if payload.get('dryRun') is not False:
        return None
    if str(ingest_row.get('status') or '') != 'FAST_SIGNAL_READY':
        return None
    if ingest_row.get('userConfirmationRequired') is not True or ingest_row.get('autoBuy') is not False:
        print('[auto-window] blocked: ingest authorization contract changed', flush=True)
        return None
    if payload.get('manualFallback'):
        print('[auto-window] blocked manual-fallback candidate', flush=True)
        return None

    symbol = str(payload.get('symbol') or '').upper()
    signal_id = str(payload.get('id') or '').strip()
    try:
        score = float(payload.get('score') or 0.0)
        entry = float(payload.get('entry') or 0.0)
        target = float(payload.get('target') or 0.0)
        stop = float(payload.get('stop') or 0.0)
        requested = float(ingest_row.get('recommendedUSDT') or payload.get('stakeUSDT') or 0.0)
    except Exception:
        return None

    required = _auto_required_score(payload)
    if not signal_id or not symbol_ok(symbol) or score < required:
        print(f'[auto-window] blocked {symbol or "?"}: score/id/universe gate score={score:.0f} required={required:.0f}', flush=True)
        return None
    if not (entry > 0 and target > entry > stop > 0):
        print(f'[auto-window] blocked {symbol}: invalid TP/entry/SL geometry', flush=True)
        return None
    amount = min(requested, AUTO_TRADING_MAX_STAKE_USDT)
    if amount < 5.0:
        print(f'[auto-window] blocked {symbol}: stake below Binance-safe floor', flush=True)
        return None

    now_s = int(time.time())
    buy_body = {
        'signal_id': signal_id,
        'action': 'BUY',
        'symbol': symbol,
        'quote_amount_usdt': round(amount, 2),
        'take_profit_price': target,
        'model_take_profit_price': target,
        'stop_loss_price': stop,
        'confirmed': True,
        'dry_run': False,
        'timestamp': now_s,
        'execution_mode': 'AUTO_SLEEP_WINDOW',
    }
    code, buy = _auto_local_exec(buy_body)
    if not (code < 300 and buy.get('ok') is True and str(buy.get('status') or '') == 'BUY_FILLED'):
        print(f'[auto-exec] BUY not confirmed {symbol} signal={signal_id} status={buy.get("status") or code}', flush=True)
        _auto_tg(f'⚠️ AUTO BUY NOT EXECUTED — {symbol} — {buy.get("status") or code}')
        return {'status': 'BUY_NOT_CONFIRMED', 'symbol': symbol, 'relayStatus': buy.get('status') or code}

    try:
        qty = float(buy.get('executed_qty') or 0.0)
        quote_spent = float(buy.get('quote_spent') or amount)
        fill_entry = quote_spent / qty if qty > 0 else entry
    except Exception:
        qty = 0.0; quote_spent = amount; fill_entry = entry
    if qty <= 0:
        _auto_tg(f'🚨 AUTO BUY FILLED BUT QTY UNKNOWN — {symbol}. Recovery worker is protecting the position; new entries should remain blocked.')
        return {'status': 'BUY_FILLED_QTY_UNKNOWN', 'symbol': symbol}

    # Place protection immediately. front_proxy normalizes LOT_SIZE/ticks and
    # gives this OCO deterministic client IDs. If transport is ambiguous, the
    # existing recovery worker reconciles by client ID rather than blind retry.
    oco_body = {
        'signal_id': signal_id,
        'action': 'OCO',
        'symbol': symbol,
        'quantity': qty,
        'take_profit_price': target,
        'model_take_profit_price': target,
        'stop_loss_price': stop,
        'stop_limit_price': stop * 0.997,
        'confirmed': True,
        'dry_run': False,
        'timestamp': int(time.time()),
        'execution_mode': 'AUTO_SLEEP_WINDOW',
    }
    oco_code, oco = _auto_local_exec(oco_body)
    protected = oco_code < 300 and oco.get('ok') is True and str(oco.get('status') or '') == 'OCO_PLACED'
    if protected:
        print(f'[auto-exec] PROTECTED {symbol} signal={signal_id} score={score:.0f} stake={quote_spent:.2f}', flush=True)
        _auto_tg(
            '🤖 AUTO BUY — Spot\n'
            f'Pair: {symbol}\n'
            f'Executed: {quote_spent:.2f} USDT\n'
            f'Entry ≈ {fill_entry:.8g}\n'
            f'TP: {target:.8g}\n'
            f'SL: {stop:.8g}\n'
            f'Score: {score:.0f}\n'
            '🛡️ OCO protection ACTIVE\n'
            f'Auto window: {AUTO_TRADING_START}–{AUTO_TRADING_END} {AUTO_TRADING_TZ}'
        )
        return {'status': 'AUTO_BUY_PROTECTED', 'symbol': symbol, 'stakeUSDT': round(quote_spent, 2)}

    print(f'[auto-exec] OCO pending/recovery {symbol} signal={signal_id} status={oco.get("status") or oco_code}', flush=True)
    _auto_tg(
        f'🚨 AUTO BUY NEEDS PROTECTION RECOVERY — {symbol}\n'
        f'BUY filled ≈ {quote_spent:.2f} USDT, OCO status: {oco.get("status") or oco_code}.\n'
        'Recovery worker is handling the bot-owned position; do not place a duplicate order.'
    )
    return {'status': 'AUTO_BUY_PROTECTION_RECOVERY', 'symbol': symbol, 'stakeUSDT': round(quote_spent, 2)}

'''
if fast_marker not in s:
    raise SystemExit('sleep-auto-trading: fast_ingest marker missing')
s = s.replace(fast_marker, helper + fast_marker, 1)

tail_old = '''    if not row.get('ok'):
        raise RuntimeError(f"fast ingest rejected: {row.get('status') or row}")
    return row
'''
tail_new = '''    if not row.get('ok'):
        raise RuntimeError(f"fast ingest rejected: {row.get('status') or row}")
    if payload.get('dryRun') is False and str(row.get('status') or '') == 'FAST_SIGNAL_READY':
        try:
            auto_result = _auto_execute_if_window(payload, row)
            if auto_result is not None:
                row['autoExecution'] = auto_result
        except Exception as exc:
            # Never turn an auto-execution transport problem into a second BUY
            # attempt. Exact-once recovery owns ambiguous submissions.
            print(f'[auto-exec] warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
    return row
'''
if tail_old not in s:
    raise SystemExit('sleep-auto-trading: fast_ingest tail marker missing')
s = s.replace(tail_old, tail_new, 1)

# Build-time schedule boundary checks independent of wall-clock time.
for required in [
    'AUTO_TRADING_ENABLED', 'AUTO_TRADING_TZ', 'AUTO_TRADING_START', 'AUTO_TRADING_END',
    'AUTO_TRADING_MAX_STAKE_USDT', 'def _auto_window_active(', 'def _auto_execute_if_window(',
    "'execution_mode': 'AUTO_SLEEP_WINDOW'", "row['autoExecution'] = auto_result",
    "ingest_row.get('userConfirmationRequired') is not True", "payload.get('manualFallback')",
]:
    if required not in s:
        raise SystemExit(f'sleep-auto-trading: missing marker {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[sleep-auto-trading] OK gated unattended Spot execution 00:00-13:00 Cairo; FAST_SIGNAL_READY only; manual fallback excluded; OCO immediate; exact-once relay')
