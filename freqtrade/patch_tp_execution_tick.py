from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# RENDER post-mortem: model target 1.571300... was submitted to the OCO layer and
# the connector normalized it upward to 1.572. The market traded 1.571, so the
# sell never filled. Normalize SELL take-profit prices DOWN to Binance tick size
# before they ever leave the engine, and optionally leave one extra tick of fill
# buffer when that tick is economically tiny. This preserves the large-move
# target model while avoiding "missed by one tick" exits.

const_marker = "MAX_SPREAD_PCT = float(os.getenv('FAST_MAX_SPREAD_PCT', '0.20'))\n"
if 'TP_EXECUTION_BUFFER_TICKS' not in s:
    if const_marker not in s:
        raise SystemExit('tp-execution-tick patch failed: constants marker missing')
    s = s.replace(
        const_marker,
        const_marker + (
            "TP_EXECUTION_BUFFER_TICKS = max(0, int(os.getenv('FAST_TP_EXECUTION_BUFFER_TICKS', '1')))\n"
            "TP_MAX_BUFFER_PCT = float(os.getenv('FAST_TP_MAX_BUFFER_PCT', '0.0010'))\n"
            "_PRICE_TICK_CACHE: dict[str, float] = {}\n"
        ),
        1,
    )

helper_marker = "\ndef fast_ingest(payload: dict, timeout: int = 25) -> dict:\n"
helpers = r'''


def _symbol_tick_size(symbol: str) -> float:
    symbol = str(symbol or '').upper()
    if symbol in _PRICE_TICK_CACHE:
        return _PRICE_TICK_CACHE[symbol]
    row = api('/exchangeInfo', {'symbol': symbol})
    symbols = row.get('symbols') or [] if isinstance(row, dict) else []
    if not symbols:
        raise RuntimeError(f'no exchangeInfo for {symbol}')
    tick = 0.0
    for f in symbols[0].get('filters') or []:
        if f.get('filterType') == 'PRICE_FILTER':
            tick = float(f.get('tickSize') or 0.0)
            break
    if tick <= 0:
        raise RuntimeError(f'no PRICE_FILTER tickSize for {symbol}')
    _PRICE_TICK_CACHE[symbol] = tick
    return tick


def _floor_to_tick(value: float, tick: float) -> float:
    if value <= 0 or tick <= 0:
        return value
    # Tiny epsilon avoids floating-point values such as 1.571/0.001 becoming
    # 1570.999999999 and flooring one unintended tick too far.
    steps = math.floor((value / tick) + 1e-10)
    return steps * tick


def _normalize_tp_for_execution(payload: dict) -> dict:
    if payload.get('dryRun') is not False:
        return payload
    symbol = str(payload.get('symbol') or '').upper()
    try:
        entry = float(payload.get('entry') or 0.0)
        raw_target = float(payload.get('target') or 0.0)
    except Exception:
        return payload
    if not symbol or entry <= 0 or raw_target <= entry:
        return payload

    try:
        tick = _symbol_tick_size(symbol)
    except Exception as exc:
        # Fail closed on the strategy target rather than inventing precision.
        print(f'[tp-execution] {symbol} tick lookup failed: {type(exc).__name__}: {exc}', flush=True)
        return payload

    executable = _floor_to_tick(raw_target, tick)
    # One extra tick helps a LIMIT_MAKER TP fill before a fast reversal, but only
    # when one tick is <= 0.10% of price and the buffered target still locks a
    # meaningful profit above entry. Coarser-tick assets get floor-only behavior.
    if (
        TP_EXECUTION_BUFFER_TICKS > 0
        and tick / max(raw_target, 1e-12) <= TP_MAX_BUFFER_PCT
    ):
        candidate = executable - tick * TP_EXECUTION_BUFFER_TICKS
        if candidate >= entry * 1.005:
            executable = candidate

    executable = max(executable, entry * 1.005)
    out = dict(payload)
    out['target_model'] = raw_target
    out['target'] = executable
    out['target_tick_size'] = tick
    out['target_execution_buffer_ticks'] = TP_EXECUTION_BUFFER_TICKS
    print(
        f"[tp-execution] {symbol} model={raw_target:.12g} executable={executable:.12g} "
        f"tick={tick:.12g} buffer_ticks={TP_EXECUTION_BUFFER_TICKS}",
        flush=True,
    )
    return out

'''
if 'def _normalize_tp_for_execution' not in s:
    if helper_marker not in s:
        raise SystemExit('tp-execution-tick patch failed: fast_ingest marker missing')
    s = s.replace(helper_marker, helpers + helper_marker, 1)

raw_marker = "    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')\n"
if 'payload = _normalize_tp_for_execution(payload)' not in s:
    if raw_marker not in s:
        raise SystemExit('tp-execution-tick patch failed: raw payload marker missing')
    s = s.replace(
        raw_marker,
        "    payload = _normalize_tp_for_execution(payload)\n" + raw_marker,
        1,
    )

for required in [
    'TP_EXECUTION_BUFFER_TICKS',
    'def _symbol_tick_size',
    'def _normalize_tp_for_execution',
    'payload = _normalize_tp_for_execution(payload)',
    '[tp-execution]',
]:
    if required not in s:
        raise SystemExit(f'tp-execution-tick patch failed: missing {required}')

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[tp-execution-tick-patch] OK SELL TP floor-to-tick + one-tick fill buffer enabled; model target preserved in payload metadata')
