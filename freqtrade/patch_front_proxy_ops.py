from pathlib import Path

path = Path('/freqtrade/front_proxy.py')
s = path.read_text(encoding='utf-8')

if 'import ops_dashboard\n' not in s:
    marker = 'import trade_state\n'
    if marker not in s:
        raise SystemExit('ops-proxy: trade_state import marker missing')
    s = s.replace(marker, marker + 'import ops_dashboard\n', 1)

if 'OPS_DASHBOARD_TOKEN=' not in s:
    marker = "TELEGRAM_BOT_TOKEN=(os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()\n"
    if marker not in s:
        raise SystemExit('ops-proxy: token marker missing')
    s = s.replace(marker, marker + "OPS_DASHBOARD_TOKEN=(os.getenv('OPS_DASHBOARD_TOKEN') or '').strip()\n", 1)

helper_marker = '\n\ndef _json_bytes(payload):\n'
helpers = r'''

def _ops_authorized(handler) -> bool:
    if not OPS_DASHBOARD_TOKEN:
        return False
    auth = str(handler.headers.get('Authorization') or '')
    if auth.startswith('Bearer ') and hmac.compare_digest(auth[7:].strip(), OPS_DASHBOARD_TOKEN):
        return True
    try:
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(handler.path).query)
        supplied = str((q.get('token') or [''])[0])
        return bool(supplied and hmac.compare_digest(supplied, OPS_DASHBOARD_TOKEN))
    except Exception:
        return False

'''
if 'def _ops_authorized' not in s:
    if helper_marker not in s:
        raise SystemExit('ops-proxy: helper marker missing')
    s = s.replace(helper_marker, helpers + helper_marker, 1)

proxy_marker = "    def proxy(self):\n        if self.path=='/health' or self.path.startswith('/health?'):\n"
proxy_new = """    def proxy(self):
        if self.path.startswith('/ops.json'):
            if not _ops_authorized(self):
                return self.send_json(401, {'ok':False,'status':'OPS_AUTH_REQUIRED'})
            return self.send_json(200, ops_dashboard.snapshot())
        if self.path=='/ops' or self.path.startswith('/ops?'):
            if not _ops_authorized(self):
                return self.send_json(401, {'ok':False,'status':'OPS_AUTH_REQUIRED'})
            data=ops_dashboard.render_html().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Connection','close')
            self.end_headers(); self.wfile.write(data); return
        if self.path=='/health' or self.path.startswith('/health?'):
"""
if "self.path.startswith('/ops.json')" not in s:
    if proxy_marker not in s:
        raise SystemExit('ops-proxy: proxy marker missing')
    s = s.replace(proxy_marker, proxy_new, 1)

health_old = "'incompleteTrackedPositions':snap.get('incomplete_count',0)})\n"
health_new = "'incompleteTrackedPositions':snap.get('incomplete_count',0),'opsDashboardConfigured':bool(OPS_DASHBOARD_TOKEN)})\n"
if "'opsDashboardConfigured'" not in s:
    if health_old not in s:
        raise SystemExit('ops-proxy: health marker missing')
    s = s.replace(health_old, health_new, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')

# Candidate telemetry is injected into fast_entry_engine by the expert/Spot
# Sniper patches. Keep it executable and make every SCANNED observation carry a
# point-in-time research TP/SL so the forward EV model can actually get labels.
engine = Path('/freqtrade/fast_entry_engine.py')
e = engine.read_text(encoding='utf-8')
if 'from pathlib import Path\n' not in e:
    marker = 'import time\n'
    if marker not in e:
        raise SystemExit('ops-proxy: fast-entry Path import marker missing')
    e = e.replace(marker, marker + 'from pathlib import Path\n', 1)
if 'Path(CANDIDATE_EVENT_PATH)' not in e:
    raise SystemExit('ops-proxy: candidate telemetry Path usage missing')

barrier_marker = '\ndef validate_and_resolve_telegram_chat() -> bool:\n'
barrier_helper = r'''

def _research_barriers(lane: str, price: float, score: float, metrics: dict) -> dict:
    try:
        px = float(price or 0.0)
    except Exception:
        px = 0.0
    if px <= 0:
        return {'research_barrier_version': 'POINT_IN_TIME_V1_INVALID'}

    def _f(v, default=0.0):
        try:
            x = float(v)
            return x if x == x else float(default)
        except Exception:
            return float(default)

    def _clip(v, lo, hi):
        return min(float(hi), max(float(lo), float(v)))

    lane = str(lane or 'NORMAL').upper()
    atr = _f(metrics.get('atr_pct'), _f(metrics.get('atr'), 0.0020))
    taker = _f(metrics.get('taker_buy_ratio'), _f(metrics.get('taker'), 0.58))
    volx = _f(metrics.get('volume_ratio'), _f(metrics.get('volx'), 1.0))

    if lane == 'NORMAL':
        base_tp = max(0.015, atr * 6.0)
        score_strength = _clip((_f(score) - 90.0) / 10.0, 0.0, 1.0)
        pressure_strength = _clip((taker - 0.58) / 0.10, 0.0, 1.0)
        volume_strength = _clip((volx - 1.0) / 1.0, 0.0, 1.0)
        tp_pct = _clip(base_tp + 0.008 * score_strength + 0.006 * pressure_strength + 0.005 * volume_strength, 0.015, 0.035)
        sl_pct = _clip(tp_pct / 2.4, 0.0055, 0.0090)
        version = 'POINT_IN_TIME_V1_NORMAL_GROWTH'
    elif lane == 'EXPLOSIVE':
        sl_pct = _clip(max(0.010, atr * 3.2), 0.010, 0.022)
        tp_pct = _clip(sl_pct * 1.65, 0.017, 0.038)
        version = 'POINT_IN_TIME_V1_EXPLOSIVE'
    elif lane == 'EXTREME':
        sl_pct = _clip(max(0.012, atr * 3.6), 0.012, 0.026)
        tp_pct = _clip(sl_pct * 1.80, 0.022, 0.045)
        version = 'POINT_IN_TIME_V1_EXTREME'
    else:
        sl_pct = _clip(max(0.0085, atr * 3.0), 0.0085, 0.016)
        tp_pct = _clip(sl_pct * 1.90, 0.016, 0.032)
        version = 'POINT_IN_TIME_V1_MID'

    return {
        'target': px * (1.0 + tp_pct),
        'stop': px * (1.0 - sl_pct),
        'risk_pct': sl_pct,
        'reward_pct': tp_pct,
        'atr_pct': atr,
        'research_barrier_version': version,
    }

'''
if 'def _research_barriers(' not in e:
    if barrier_marker not in e:
        raise SystemExit('ops-proxy: research barrier insertion marker missing')
    e = e.replace(barrier_marker, barrier_helper + barrier_marker, 1)

telemetry_replacements = [
    (
        "                    _record_candidate(symbol, 'NORMAL', score, float(m.get('live_price') or m.get('last') or 0), 'SCANNED', 'score-snapshot', volume_ratio=m.get('volume_ratio'), taker_buy_ratio=m.get('taker_buy_ratio'), spread_pct=m.get('spread_pct'), rsi=m.get('rsi'), change24=change, volume24=volume)\n",
        "                    _research_px = float(m.get('live_price') or m.get('last') or 0)\n                    _research_b = _research_barriers('NORMAL', _research_px, score, m)\n                    _record_candidate(symbol, 'NORMAL', score, _research_px, 'SCANNED', 'score-snapshot', volume_ratio=m.get('volume_ratio'), taker_buy_ratio=m.get('taker_buy_ratio'), spread_pct=m.get('spread_pct'), rsi=m.get('rsi'), change24=change, volume24=volume, **_research_b)\n",
    ),
    (
        "                    _record_candidate(es, 'EXPLOSIVE', escore, float(em.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=em.get('pullback'), reclaimed=em.get('reclaimed'), volume_ratio=em.get('volx'), taker_buy_ratio=em.get('taker'), spread_pct=em.get('spread'), rsi=em.get('rsi'), change24=ech, volume24=ev)\n",
        "                    _research_px = float(em.get('live') or 0)\n                    _research_b = _research_barriers('EXPLOSIVE', _research_px, escore, em)\n                    _record_candidate(es, 'EXPLOSIVE', escore, _research_px, 'SCANNED', 'score-snapshot', pullback=em.get('pullback'), reclaimed=em.get('reclaimed'), volume_ratio=em.get('volx'), taker_buy_ratio=em.get('taker'), spread_pct=em.get('spread'), rsi=em.get('rsi'), change24=ech, volume24=ev, **_research_b)\n",
    ),
    (
        "                    _record_candidate(xs, 'EXTREME', xscore, float(xm.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=xm.get('pullback'), reclaimed=xm.get('reclaimed'), volume_ratio=xm.get('volx'), taker_buy_ratio=xm.get('taker'), spread_pct=xm.get('spread'), rsi=xm.get('rsi'), change24=xch, volume24=xv)\n",
        "                    _research_px = float(xm.get('live') or 0)\n                    _research_b = _research_barriers('EXTREME', _research_px, xscore, xm)\n                    _record_candidate(xs, 'EXTREME', xscore, _research_px, 'SCANNED', 'score-snapshot', pullback=xm.get('pullback'), reclaimed=xm.get('reclaimed'), volume_ratio=xm.get('volx'), taker_buy_ratio=xm.get('taker'), spread_pct=xm.get('spread'), rsi=xm.get('rsi'), change24=xch, volume24=xv, **_research_b)\n",
    ),
    (
        "                    _record_candidate(ms, 'MID', mscore, float(mm.get('live') or 0), 'SCANNED', 'score-snapshot', pullback=mm.get('pullback'), reclaimed=mm.get('reclaimed'), volume_ratio=mm.get('volx'), taker_buy_ratio=mm.get('taker'), spread_pct=mm.get('spread'), rsi=mm.get('rsi'), change24=mch, volume24=mv)\n",
        "                    _research_px = float(mm.get('live') or 0)\n                    _research_b = _research_barriers('MID', _research_px, mscore, mm)\n                    _record_candidate(ms, 'MID', mscore, _research_px, 'SCANNED', 'score-snapshot', pullback=mm.get('pullback'), reclaimed=mm.get('reclaimed'), volume_ratio=mm.get('volx'), taker_buy_ratio=mm.get('taker'), spread_pct=mm.get('spread'), rsi=mm.get('rsi'), change24=mch, volume24=mv, **_research_b)\n",
    ),
]
patched = 0
for old, new in telemetry_replacements:
    if new in e:
        patched += 1
    elif old in e:
        e = e.replace(old, new, 1)
        patched += 1
if patched != 4:
    raise SystemExit(f'ops-proxy: expected 4 forward-label telemetry lanes, got {patched}')
for marker in ["_research_barriers('NORMAL'", "_research_barriers('EXPLOSIVE'", "_research_barriers('MID'", "_research_barriers('EXTREME'", "'research_barrier_version': version"]:
    if marker not in e:
        raise SystemExit(f'ops-proxy: research telemetry marker missing: {marker}')

compile(e, str(engine), 'exec')
engine.write_text(e, encoding='utf-8')

print('[ops-dashboard-proxy-patch] OK authenticated ops dashboard + candidate Path fix + 4-lane point-in-time EV barriers')
