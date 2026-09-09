from __future__ import annotations

"""Forward outcome evaluator for live candidate decisions.

Reads candidate events written by the scanner, measures forward MFE/MAE and
15m/30m/60m returns from public Binance 1m candles, and maintains a conservative
lane-health kill switch. It never places or modifies orders.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

CANDIDATE_PATH = Path(os.getenv('TST_CANDIDATE_EVENT_PATH', '/tmp/tst_candidate_events.jsonl'))
OUTCOME_PATH = Path(os.getenv('TST_OUTCOME_PATH', '/tmp/tst_candidate_outcomes.jsonl'))
HEALTH_PATH = Path(os.getenv('TST_LANE_HEALTH_PATH', '/tmp/tst_lane_health.json'))
STATE_PATH = Path(os.getenv('TST_OUTCOME_STATE_PATH', '/tmp/tst_outcome_state.json'))
POLL_SEC = max(20, int(os.getenv('OUTCOME_ENGINE_POLL_SEC', '45')))
KILL_MIN_SAMPLE = max(20, int(os.getenv('LANE_KILL_MIN_SAMPLE', '30')))
KILL_HOURS = max(1.0, float(os.getenv('LANE_KILL_HOURS', '2')))
PUBLIC_BASES = ['https://data-api.binance.vision/api/v3', 'https://api.binance.com/api/v3']


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def _write_json(path: Path, row) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(row, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def _public(path: str, params: dict):
    q = urllib.parse.urlencode(params)
    last = None
    for base in PUBLIC_BASES:
        try:
            req = urllib.request.Request(f'{base}{path}?{q}', headers={'User-Agent': 'tst-outcome-engine/1.0'})
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read() or b'[]')
        except Exception as exc:
            last = exc
    raise RuntimeError(last or 'public API unavailable')


def _events() -> list[dict]:
    if not CANDIDATE_PATH.exists():
        return []
    rows = []
    try:
        lines = CANDIDATE_PATH.read_text(encoding='utf-8').splitlines()[-1500:]
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get('event_id'):
                    rows.append(row)
            except Exception:
                pass
    except Exception:
        pass
    return rows


def _forward(event: dict) -> dict | None:
    ts = float(event.get('ts') or 0)
    price = float(event.get('price') or 0)
    symbol = str(event.get('symbol') or '')
    if ts <= 0 or price <= 0 or not symbol:
        return None
    age = time.time() - ts
    if age < 15 * 60:
        return None
    limit = 65 if age >= 60 * 60 else (35 if age >= 30 * 60 else 20)
    rows = _public('/klines', {'symbol': symbol, 'interval': '1m', 'startTime': int(ts * 1000), 'limit': limit})
    if not isinstance(rows, list) or len(rows) < 15:
        return None
    highs = [float(x[2]) for x in rows]
    lows = [float(x[3]) for x in rows]
    closes = [float(x[4]) for x in rows]
    result = {
        'event_id': event['event_id'], 'source_ts': ts, 'evaluated_at': time.time(),
        'symbol': symbol, 'lane': event.get('lane'), 'score': event.get('score'),
        'decision': event.get('decision'), 'reason': event.get('reason'), 'price': price,
        'mfe_pct': (max(highs) / price - 1.0) * 100.0,
        'mae_pct': (min(lows) / price - 1.0) * 100.0,
    }
    if len(closes) >= 15:
        result['ret15_pct'] = (closes[14] / price - 1.0) * 100.0
    if len(closes) >= 30:
        result['ret30_pct'] = (closes[29] / price - 1.0) * 100.0
    if len(closes) >= 60:
        result['ret60_pct'] = (closes[59] / price - 1.0) * 100.0
        result['complete'] = True
    else:
        result['complete'] = False
    return result


def _append_outcome(row: dict) -> None:
    OUTCOME_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTCOME_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, separators=(',', ':'), ensure_ascii=False) + '\n')


def _all_outcomes() -> list[dict]:
    if not OUTCOME_PATH.exists():
        return []
    out = []
    for line in OUTCOME_PATH.read_text(encoding='utf-8').splitlines()[-3000:]:
        try:
            row = json.loads(line)
            if isinstance(row, dict): out.append(row)
        except Exception:
            pass
    return out


def _health() -> None:
    # Only signal-ready decisions are allowed to trip the kill switch. Rejected
    # candidates are diagnostic, not evidence that a lane itself loses money.
    by_lane = defaultdict(list)
    latest_by_event = {}
    for row in _all_outcomes():
        latest_by_event[row.get('event_id')] = row
    for row in latest_by_event.values():
        if row.get('complete') and row.get('decision') == 'READY' and row.get('ret60_pct') is not None:
            by_lane[str(row.get('lane') or 'UNKNOWN')].append(row)

    old = _read_json(HEALTH_PATH, {'lanes': {}})
    now = time.time()
    health = {'updated_at': now, 'lanes': {}}
    for lane in {'NORMAL', 'MID', 'EXPLOSIVE', 'EXTREME'} | set(by_lane):
        rows = by_lane.get(lane, [])[-100:]
        sample = len(rows)
        avg = sum(float(r['ret60_pct']) for r in rows) / sample if sample else None
        hit = sum(1 for r in rows if float(r['ret60_pct']) > 0) / sample if sample else None
        prev = (old.get('lanes') or {}).get(lane, {})
        disabled_until = float(prev.get('disabled_until') or 0)
        triggered = False
        # Conservative: require a substantial live forward sample and both poor
        # expectancy AND poor hit-rate. This prevents reacting to a few losses.
        if sample >= KILL_MIN_SAMPLE and avg is not None and hit is not None and avg <= -0.30 and hit < 0.40:
            disabled_until = max(disabled_until, now + KILL_HOURS * 3600)
            triggered = True
        health['lanes'][lane] = {
            'sample60': sample,
            'avg60_pct': None if avg is None else round(avg, 4),
            'hit60': None if hit is None else round(hit, 4),
            'disabled_until': disabled_until,
            'disabled': disabled_until > now,
            'triggered_now': triggered,
        }
    _write_json(HEALTH_PATH, health)
    summary = ', '.join(f"{k}:n={v['sample60']} disabled={v['disabled']}" for k, v in health['lanes'].items())
    print(f'[outcome-engine] lane-health {summary}', flush=True)


def run_once() -> None:
    state = _read_json(STATE_PATH, {'done60': [], 'latest_eval': {}})
    done60 = set(state.get('done60') or [])
    latest_eval = dict(state.get('latest_eval') or {})
    changed = False
    for event in _events():
        event_id = str(event.get('event_id'))
        if event_id in done60:
            continue
        last = float(latest_eval.get(event_id) or 0)
        if time.time() - last < 10 * 60:
            continue
        row = _forward(event)
        if row is None:
            continue
        _append_outcome(row)
        latest_eval[event_id] = time.time()
        if row.get('complete'):
            done60.add(event_id)
        changed = True
        print(f"[outcome] {row['lane']} {row['symbol']} decision={row['decision']} MFE={row['mfe_pct']:+.2f}% MAE={row['mae_pct']:+.2f}% ret15={row.get('ret15_pct')} ret60={row.get('ret60_pct')}", flush=True)
    if changed:
        state = {'done60': list(done60)[-3000:], 'latest_eval': latest_eval}
        _write_json(STATE_PATH, state)
    _health()


def main() -> None:
    print(f'[outcome-engine] ONLINE horizons=15m,30m,60m kill_sample={KILL_MIN_SAMPLE} kill_hours={KILL_HOURS:g}', flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f'[outcome-engine] loop warning: {type(exc).__name__}: {str(exc)[:160]}', flush=True)
        time.sleep(POLL_SEC)


if __name__ == '__main__':
    main()
