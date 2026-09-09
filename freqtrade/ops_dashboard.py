from __future__ import annotations

import html
import json
import os
import time
from collections import Counter
from pathlib import Path

import execution_health
import market_context
import trade_state

CANDIDATE_PATH = Path(os.getenv('TST_CANDIDATE_EVENT_PATH', '/data/tst_candidate_events.jsonl'))
OUTCOME_PATH = Path(os.getenv('TST_OUTCOME_PATH', '/data/tst_candidate_outcomes.jsonl'))
EV_REPORT_PATH = Path(os.getenv('TST_SHADOW_EV_REPORT_PATH', '/data/tst_shadow_ev_report.json'))
EV_APPROVAL_PATH = Path(os.getenv('TST_EV_LIVE_APPROVAL_PATH', '/data/tst_ev_live_approval.json'))
RESEARCH_REPORT_PATH = Path(os.getenv('TST_SHADOW_RESEARCH_REPORT_PATH', '/data/tst_shadow_research_report.json'))
LANE_HEALTH_PATH = Path(os.getenv('TST_LANE_HEALTH_PATH', '/data/tst_lane_health.json'))


def _read_json(path: Path) -> dict:
    try:
        x = json.loads(path.read_text(encoding='utf-8'))
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


def _jsonl(path: Path, limit: int) -> list[dict]:
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except Exception:
            pass
    return out


def snapshot() -> dict:
    state = trade_state.load_state()
    positions = list((state.get('positions') or {}).values())
    candidates = _jsonl(CANDIDATE_PATH, 2500)
    outcomes = _jsonl(OUTCOME_PATH, 2500)
    now = time.time()
    recent_candidates = [r for r in candidates if now - float(r.get('ts') or 0) <= 24 * 3600]
    decision_counts = Counter(str(r.get('decision') or 'UNKNOWN') for r in recent_candidates)
    reason_counts = Counter(str(r.get('reason') or 'UNKNOWN') for r in recent_candidates if str(r.get('decision') or '') in {'REJECT', 'SCANNED'})
    lane_counts = Counter(str(r.get('lane') or 'UNKNOWN') for r in recent_candidates)
    complete = {}
    for r in outcomes:
        if r.get('event_id') and r.get('complete') is True:
            complete[str(r['event_id'])] = r
    completed = list(complete.values())
    market = market_context.load_snapshot(max_age_sec=3600)
    top = []
    for symbol, row in (market.get('symbols') or {}).items():
        if not isinstance(row, dict):
            continue
        top.append({
            'symbol': symbol,
            'rank': row.get('opportunity_rank'),
            'opportunity_pct': row.get('opportunity_pct_shadow'),
            'rs_btc4h_pct': row.get('rs_btc4h_pct'),
            'r1h_pct': row.get('r1h_pct'),
            'r4h_pct': row.get('r4h_pct'),
            'liquidity_pct': row.get('liquidity_pct'),
        })
    top.sort(key=lambda x: (x.get('rank') if isinstance(x.get('rank'), int) else 999999, -(float(x.get('opportunity_pct') or 0))))
    perf = trade_state.performance_snapshot()
    port = trade_state.portfolio_snapshot()
    return {
        'generated_at': now,
        'system': {
            'execution_health': execution_health.snapshot(),
            'portfolio': port,
            'performance': perf,
            'persistent_state': str(trade_state.STATE_PATH).startswith('/data/'),
        },
        'market': {
            'regime': market.get('regime'),
            'breadth_1h': market.get('breadth_1h'),
            'breadth_4h': market.get('breadth_4h'),
            'universe_n': market.get('universe_n'),
            'generated_at': market.get('generated_at'),
            'top_opportunities': top[:10],
        },
        'model': {
            'ev_report': _read_json(EV_REPORT_PATH),
            'live_approval': _read_json(EV_APPROVAL_PATH),
            'research_report': _read_json(RESEARCH_REPORT_PATH),
        },
        'positions': positions,
        'telemetry_24h': {
            'candidate_events': len(recent_candidates),
            'decisions': dict(decision_counts),
            'lanes': dict(lane_counts),
            'top_reject_reasons': reason_counts.most_common(15),
            'completed_forward_outcomes_total': len(completed),
        },
        'lane_health': _read_json(LANE_HEALTH_PATH),
        'recent_candidates': recent_candidates[-40:][::-1],
        'recent_outcomes': completed[-30:][::-1],
    }


def render_html() -> str:
    s = snapshot()
    market = s['market']
    system = s['system']
    model = s['model']
    tele = s['telemetry_24h']
    ev = model.get('ev_report') or {}
    approval = model.get('live_approval') or {}

    def esc(v):
        return html.escape(str(v if v is not None else '—'))

    opp_rows = ''.join(
        f"<tr><td>{esc(x.get('rank'))}</td><td>{esc(x.get('symbol'))}</td><td>{esc(x.get('opportunity_pct'))}</td><td>{esc(x.get('rs_btc4h_pct'))}</td><td>{esc(x.get('r1h_pct'))}</td><td>{esc(x.get('r4h_pct'))}</td></tr>"
        for x in market.get('top_opportunities') or []
    )
    pos_rows = ''.join(
        f"<tr><td>{esc(x.get('symbol'))}</td><td>{esc(x.get('status'))}</td><td>{esc(round(float(x.get('entry') or 0),8))}</td><td>{esc(round(float(x.get('stop') or 0),8))}</td><td>{esc(round(float(x.get('target') or 0),8))}</td><td>{esc(x.get('realized_pnl_usdt'))}</td></tr>"
        for x in s.get('positions') or []
    ) or '<tr><td colspan="6">No tracked positions</td></tr>'
    reason_rows = ''.join(f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in tele.get('top_reject_reasons') or []) or '<tr><td colspan="2">No reject telemetry yet</td></tr>'

    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>TST Spot Sniper</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;margin:20px;background:#0d1117;color:#e6edf3}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:14px}}h1,h2{{margin:0 0 10px}}.big{{font-size:28px;font-weight:700}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{padding:7px;border-bottom:1px solid #30363d;text-align:left}}.ok{{color:#3fb950}}.warn{{color:#d29922}}.bad{{color:#f85149}}code{{color:#79c0ff}}small{{color:#8b949e}}
</style></head><body><h1>TST Spot Sniper — Operations</h1><small>Updated {esc(time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(s['generated_at'])))}</small><br><br>
<div class='grid'>
<div class='card'><h2>Market</h2><div class='big'>{esc(market.get('regime'))}</div><p>Breadth 1h: <b>{esc(market.get('breadth_1h'))}</b><br>Breadth 4h: <b>{esc(market.get('breadth_4h'))}</b><br>Universe: {esc(market.get('universe_n'))}</p></div>
<div class='card'><h2>Probability / EV</h2><div class='big'>{esc(approval.get('status') or 'WARMUP')}</div><p>EV model: {esc(ev.get('status'))}<br>Forward samples: {esc(approval.get('forward_sample'))}/{esc(approval.get('minimum_forward_sample'))}<br>Evidence pass: {esc(ev.get('evidence_pass'))}</p></div>
<div class='card'><h2>Risk</h2><div class='big'>{esc(system['portfolio'].get('open_count'))} open</div><p>Open stop risk: {esc(round(float(system['portfolio'].get('stop_risk_usdt') or 0),4))} USDT<br>P&L today: {esc(round(float(system['performance'].get('realized_pnl_today_usdt') or 0),4))} USDT<br>Consecutive losses: {esc(system['performance'].get('consecutive_losses'))}</p></div>
<div class='card'><h2>Telemetry 24h</h2><div class='big'>{esc(tele.get('candidate_events'))}</div><p>Decision events<br><code>{esc(tele.get('decisions'))}</code><br>Completed outcomes total: {esc(tele.get('completed_forward_outcomes_total'))}</p></div>
</div><br>
<div class='card'><h2>Top cross-sectional opportunities</h2><table><tr><th>#</th><th>Pair</th><th>Opportunity</th><th>RS/BTC pct</th><th>1h pct</th><th>4h pct</th></tr>{opp_rows}</table></div><br>
<div class='card'><h2>Tracked positions</h2><table><tr><th>Pair</th><th>Status</th><th>Entry</th><th>Stop</th><th>Target</th><th>Realized P&L</th></tr>{pos_rows}</table></div><br>
<div class='card'><h2>Top reject / scan reasons</h2><table><tr><th>Reason</th><th>Count</th></tr>{reason_rows}</table></div>
</body></html>"""
