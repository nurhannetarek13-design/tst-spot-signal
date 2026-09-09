from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

# Imports used by the final runtime layer.
import_marker = 'import telegram_signal_bridge as bridge\n'
if 'import trade_state\n' not in s:
    if import_marker not in s:
        raise SystemExit('expert-v3: bridge import marker missing')
    s = s.replace(import_marker, import_marker + 'import trade_state\n', 1)

# Risk/telemetry constants. The old fixed 8m global cooldown becomes only a
# burst guard; portfolio stop-risk and position count become the real gate.
const_marker = 'execution_ready = False\n'
constants = '''
SIGNAL_BURST_GUARD_SEC = int(os.getenv('FAST_SIGNAL_BURST_GUARD_SEC', '90'))
PORTFOLIO_RISK_CAP_USDT = float(os.getenv('FAST_PORTFOLIO_RISK_CAP_USDT', '1.25'))
PORTFOLIO_MAX_POSITIONS = int(os.getenv('FAST_PORTFOLIO_MAX_POSITIONS', '3'))
CANDIDATE_EVENT_PATH = os.getenv('TST_CANDIDATE_EVENT_PATH', '/data/tst_candidate_events.jsonl')
LANE_HEALTH_PATH = os.getenv('TST_LANE_HEALTH_PATH', '/data/tst_lane_health.json')
DERIVATIVES_CACHE_SEC = int(os.getenv('FAST_DERIVATIVES_CACHE_SEC', '120'))
GLOBAL_COOLDOWN_SEC = SIGNAL_BURST_GUARD_SEC
_candidate_event_last = {}
_derivatives_cache = {}
'''
if 'PORTFOLIO_RISK_CAP_USDT' not in s:
    if const_marker not in s:
        raise SystemExit('expert-v3: state constants marker missing')
    s = s.replace(const_marker, const_marker + constants, 1)

helper_marker = '\ndef validate_and_resolve_telegram_chat() -> bool:\n'
helpers = r'''

def _lane_from_payload(payload: dict) -> str:
    strategy = str(payload.get('strategy') or '').upper()
    if strategy.startswith('MID_MOMENTUM_CONTINUATION'): return 'MID'
    if strategy.startswith('EXPLOSIVE_CONTINUATION'): return 'EXPLOSIVE'
    if strategy.startswith('EXTREME_CONTINUATION'): return 'EXTREME'
    return 'NORMAL'


def _lane_enabled(lane: str) -> tuple[bool, str]:
    try:
        with open(LANE_HEALTH_PATH, 'r', encoding='utf-8') as f:
            row = json.load(f)
        h = ((row.get('lanes') or {}).get(lane) or {})
        until = float(h.get('disabled_until') or 0)
        if until > time.time():
            return False, f'performance-kill-switch-until-{int(until)}'
    except Exception:
        pass
    return True, 'ok'


def _record_candidate(symbol: str, lane: str, score: float, price: float, decision: str, reason: str, **extra) -> None:
    try:
        now = time.time()
        key = (symbol, lane, decision, reason)
        if now - float(_candidate_event_last.get(key) or 0) < 60:
            return
        _candidate_event_last[key] = now
        row = {
            'event_id': f'{symbol}-{lane}-{decision}-{int(now*1000)}',
            'ts': now, 'symbol': symbol, 'lane': lane,
            'score': round(float(score), 3), 'price': float(price),
            'decision': decision, 'reason': reason, **extra,
        }
        Path(CANDIDATE_EVENT_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(CANDIDATE_EVENT_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, separators=(',', ':'), ensure_ascii=False) + '\n')
    except Exception as exc:
        print(f'[outcome-event] warning {type(exc).__name__}: {exc}', flush=True)


def _portfolio_allows(payload: dict) -> tuple[bool, str]:
    lane = _lane_from_payload(payload)
    enabled, why = _lane_enabled(lane)
    if not enabled:
        return False, why
    snap = trade_state.portfolio_snapshot()
    try:
        entry = float(payload.get('entry') or 0)
        stop = float(payload.get('stop') or 0)
        stake = float(payload.get('stakeUSDT') or 0)
        new_risk = stake * max(0.0, (entry - stop) / entry) if entry > 0 else 999.0
    except Exception:
        new_risk = 999.0
    # A filled BUY without a known protective OCO is deliberately fail-closed.
    if int(snap.get('incomplete_count') or 0) > 0:
        return False, 'tracked-buy-awaiting-oco'
    if int(snap.get('open_count') or 0) >= PORTFOLIO_MAX_POSITIONS:
        return False, 'portfolio-position-cap'
    total = float(snap.get('stop_risk_usdt') or 0.0) + new_risk
    if total > PORTFOLIO_RISK_CAP_USDT + 1e-9:
        return False, f'portfolio-risk-cap-{total:.3f}>{PORTFOLIO_RISK_CAP_USDT:.3f}'
    return True, f'portfolio-risk-ok-{total:.3f}'


def _derivatives_adjust(symbol: str, score: float) -> tuple[float, str]:
    """Public USD-M data as Spot context only. No Futures orders, ever.

    This is intentionally a small +/- score adjustment, not a universal hard
    gate. Symbols without a Binance perpetual contract remain neutral.
    """
    now = time.time()
    cached = _derivatives_cache.get(symbol)
    if cached and now - cached[0] < DERIVATIVES_CACHE_SEC:
        return cached[1], cached[2]
    adjusted = float(score)
    reason = 'derivatives-neutral'
    try:
        base = 'https://www.binance.com'
        oi = get_json(base + '/futures/data/openInterestHist?' + urlencode({'symbol': symbol, 'period': '15m', 'limit': 8}), timeout=8)
        taker = get_json(base + '/futures/data/takerlongshortRatio?' + urlencode({'symbol': symbol, 'period': '15m', 'limit': 4}), timeout=8)
        prem = get_json(base + '/fapi/v1/premiumIndex?' + urlencode({'symbol': symbol}), timeout=8)
        if not isinstance(oi, list) or len(oi) < 2 or not isinstance(taker, list) or len(taker) < 2 or not isinstance(prem, dict):
            raise RuntimeError('no-usable-perpetual-data')
        oi0 = float(oi[0].get('sumOpenInterestValue') or oi[0].get('sumOpenInterest') or 0)
        oi1 = float(oi[-1].get('sumOpenInterestValue') or oi[-1].get('sumOpenInterest') or 0)
        oi_chg = oi1 / oi0 - 1.0 if oi0 > 0 else 0.0
        ratios = [float(x.get('buySellRatio') or 1.0) for x in taker[-4:]]
        taker_ratio = sum(ratios) / max(1, len(ratios))
        funding = float(prem.get('lastFundingRate') or 0.0)
        mark = float(prem.get('markPrice') or 0.0)
        index = float(prem.get('indexPrice') or 0.0)
        basis = mark / index - 1.0 if index > 0 else 0.0
        if oi_chg >= 0.02 and taker_ratio >= 1.08 and funding <= 0.0015 and abs(basis) <= 0.006:
            adjusted = min(100.0, adjusted + 2.0)
            reason = f'derivatives-support+2|oi={oi_chg:.3f}|taker={taker_ratio:.2f}|fund={funding:.5f}'
        elif oi_chg >= 0.04 and funding > 0.0015 and basis > 0.004 and taker_ratio < 1.0:
            adjusted = max(0.0, adjusted - 5.0)
            reason = f'derivatives-crowded-5|oi={oi_chg:.3f}|taker={taker_ratio:.2f}|fund={funding:.5f}|basis={basis:.4f}'
        else:
            reason = f'derivatives-neutral|oi={oi_chg:.3f}|taker={taker_ratio:.2f}|fund={funding:.5f}'
    except Exception:
        reason = 'derivatives-unavailable-neutral'
    _derivatives_cache[symbol] = (now, adjusted, reason)
    return adjusted, reason


def _expert_pre_ingest(payload: dict) -> bool:
    ok, why = _portfolio_allows(payload)
    lane = _lane_from_payload(payload)
    symbol = str(payload.get('symbol') or '')
    score = float(payload.get('score') or 0)
    price = float(payload.get('entry') or 0)
    if not ok:
        print(f'[portfolio-risk] {symbol} BLOCKED lane={lane} reason={why}', flush=True)
        _record_candidate(symbol, lane, score, price, 'REJECT', why)
        return False
    _record_candidate(symbol, lane, score, price, 'READY', why)
    print(f'[portfolio-risk] {symbol} PASS lane={lane} {why}', flush=True)
    return True

'''
if '_expert_pre_ingest' not in s:
    if helper_marker not in s:
        raise SystemExit('expert-v3: helper insertion marker missing')
    s = s.replace(helper_marker, helpers + helper_marker, 1)

# Small public derivatives context adjustment only at final signal evaluation.
replacements = [
    (
        "    m = market_metrics(symbol)\n    score, reasons = score_setup(m)\n",
        "    m = market_metrics(symbol)\n    score, reasons = score_setup(m)\n    score, deriv_reason = _derivatives_adjust(symbol, score)\n    reasons = list(reasons) + [deriv_reason]\n",
        'NORMAL',
    ),
    (
        "    m = explosive_metrics(symbol); score, reasons = score_explosive(m)\n",
        "    m = explosive_metrics(symbol); score, reasons = score_explosive(m)\n    score, deriv_reason = _derivatives_adjust(symbol, score); reasons = list(reasons) + [deriv_reason]\n",
        'EXPLOSIVE',
    ),
    (
        "    m = explosive_metrics(symbol)\n    score, reasons = score_extreme(m)\n",
        "    m = explosive_metrics(symbol)\n    score, reasons = score_extreme(m)\n    score, deriv_reason = _derivatives_adjust(symbol, score)\n    reasons = list(reasons) + [deriv_reason]\n",
        'EXTREME',
    ),
    (
        "    m = explosive_metrics(symbol)\n    score, reasons = score_mid(m)\n",
        "    m = explosive_metrics(symbol)\n    score, reasons = score_mid(m)\n    score, deriv_reason = _derivatives_adjust(symbol, score)\n    reasons = list(reasons) + [deriv_reason]\n",
        'MID',
    ),
]
for old, new, lane in replacements:
    if new not in s:
        if old not in s:
            raise SystemExit(f'expert-v3: {lane} derivatives marker missing')
        s = s.replace(old, new, 1)

# Forward telemetry for score/reclaim rejects.
normal_gate = "    if score < MIN_SCORE:\n        return False\n"
normal_new = "    if score < MIN_SCORE:\n        _record_candidate(symbol, 'NORMAL', score, m['last'], 'REJECT', 'score-below-direct')\n        return False\n"
if normal_new not in s:
    if normal_gate not in s: raise SystemExit('expert-v3: normal score gate missing')
    s = s.replace(normal_gate, normal_new, 1)

mid_gate = "    if score < MID_DIRECT_SCORE or not m['reclaimed']:\n        return False\n"
mid_new = "    if score < MID_DIRECT_SCORE or not m['reclaimed']:\n        _record_candidate(symbol, 'MID', score, m['live'], 'REJECT', 'score-or-reclaim')\n        return False\n"
if mid_new not in s:
    if mid_gate not in s: raise SystemExit('expert-v3: mid score gate missing')
    s = s.replace(mid_gate, mid_new, 1)

exp_gate = "    if score < EXPLOSIVE_DIRECT_SCORE or not m['reclaimed']: return False\n"
exp_new = "    if score < EXPLOSIVE_DIRECT_SCORE or not m['reclaimed']:\n        _record_candidate(symbol, 'EXPLOSIVE', score, m['live'], 'REJECT', 'score-or-reclaim')\n        return False\n"
if exp_new not in s:
    if exp_gate not in s: raise SystemExit('expert-v3: explosive score gate missing')
    s = s.replace(exp_gate, exp_new, 1)

x_gate = "    if score < EXTREME_DIRECT_SCORE or not m['reclaimed']:\n        return False\n"
x_new = "    if score < EXTREME_DIRECT_SCORE or not m['reclaimed']:\n        _record_candidate(symbol, 'EXTREME', score, m['live'], 'REJECT', 'score-or-reclaim')\n        return False\n"
if x_new not in s:
    if x_gate not in s: raise SystemExit('expert-v3: extreme score gate missing')
    s = s.replace(x_gate, x_new, 1)

# Every live one-tap path now passes a shared portfolio stop-risk gate just before
# it can emit the confirmation signal. Do this for NORMAL/MID/EXPLOSIVE/EXTREME.
needle = "    row = fast_ingest(payload, timeout=30)\n"
count = s.count(needle)
if '[portfolio-risk]' in s and '_expert_pre_ingest(payload)' not in s:
    raise SystemExit('expert-v3: inconsistent portfolio patch state')
if '_expert_pre_ingest(payload)' not in s:
    # One preflight fast_ingest call does not use the exact indented live marker;
    # the four live strategy payloads do. At this build stage there must be >=4.
    if count < 4:
        raise SystemExit(f'expert-v3: expected >=4 live ingest markers, got {count}')
    s = s.replace(needle, "    if not _expert_pre_ingest(payload):\n        return False\n    row = fast_ingest(payload, timeout=30)\n")

# Startup diagnostic makes the risk-based change explicit.
startup_marker = "    last_chat_retry = 0.0\n"
startup = "    print(f'[expert-system-v3] ONLINE portfolio_cap={PORTFOLIO_RISK_CAP_USDT:.2f}USDT max_positions={PORTFOLIO_MAX_POSITIONS} burst_guard={SIGNAL_BURST_GUARD_SEC}s derivatives=PUBLIC_CONTEXT outcome=ON kill_switch=ON')\n"
if '[expert-system-v3] ONLINE' not in s:
    if startup_marker not in s: raise SystemExit('expert-v3: startup marker missing')
    s = s.replace(startup_marker, startup + startup_marker, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[expert-system-v3-patch] OK risk-based portfolio gate + public derivatives context + persistent forward outcome telemetry + conservative lane kill-switch enabled')
