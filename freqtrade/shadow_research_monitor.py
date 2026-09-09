from __future__ import annotations

"""Persistent research evidence monitor for live candidate outcomes.

READ-ONLY with respect to trading. It never blocks, places, cancels, or modifies
orders. It summarizes completed forward outcomes by regime and cross-sectional
rank so live heuristics are not promoted without evidence.
"""

import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

OUTCOME_PATH = Path(os.getenv('TST_OUTCOME_PATH', '/data/tst_candidate_outcomes.jsonl'))
REPORT_PATH = Path(os.getenv('TST_SHADOW_RESEARCH_REPORT_PATH', '/data/tst_shadow_research_report.json'))
POLL_SEC = max(60, int(os.getenv('SHADOW_RESEARCH_POLL_SEC', '300')))
MIN_MODEL_SAMPLE = max(100, int(os.getenv('SHADOW_RESEARCH_MIN_MODEL_SAMPLE', '300')))
BOOTSTRAP_N = max(100, min(2000, int(os.getenv('SHADOW_RESEARCH_BOOTSTRAP_N', '500'))))


def _atomic_write(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(row, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)


def _latest_complete() -> list[dict]:
    if not OUTCOME_PATH.exists():
        return []
    latest: dict[str, dict] = {}
    try:
        lines = OUTCOME_PATH.read_text(encoding='utf-8').splitlines()[-20000:]
    except Exception:
        return []
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or not row.get('event_id'):
                continue
            latest[str(row['event_id'])] = row
        except Exception:
            continue
    return [r for r in latest.values() if r.get('complete') is True and r.get('sim_net_pct') is not None]


def _f(row: dict, key: str) -> float | None:
    try:
        x = float(row.get(key))
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _bootstrap_mean_ci(xs: list[float]) -> list[float | None]:
    if len(xs) < 20:
        return [None, None]
    seed = len(xs) * 7919 + int(sum(xs) * 1000)
    rng = random.Random(seed)
    means = []
    n = len(xs)
    for _ in range(BOOTSTRAP_N):
        means.append(sum(xs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[max(0, int(0.025 * len(means)) - 1)]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    return [round(lo, 4), round(hi, 4)]


def _metrics(rows: list[dict]) -> dict:
    nets = [x for r in rows if (x := _f(r, 'sim_net_pct')) is not None]
    mfes = [x for r in rows if (x := _f(r, 'mfe_pct')) is not None]
    maes = [x for r in rows if (x := _f(r, 'mae_pct')) is not None]
    hits = [int(r.get('tp_before_sl') or 0) for r in rows if r.get('tp_before_sl') in {0, 1}]
    if not nets:
        return {'n': 0}
    mean_net = sum(nets) / len(nets)
    return {
        'n': len(nets),
        'mean_net_pct': round(mean_net, 4),
        'median_net_pct': round(_median(nets), 4),
        'mean_net_bootstrap95': _bootstrap_mean_ci(nets),
        'tp_before_sl_rate': round(sum(hits) / len(hits), 4) if hits else None,
        'median_mfe_pct': round(_median(mfes), 4) if mfes else None,
        'median_mae_pct': round(_median(maes), 4) if maes else None,
        # Outcome engine baseline is already 0.28%. These are incremental cost stresses.
        'mean_net_plus_0_14pct_cost': round(mean_net - 0.14, 4),
        'mean_net_plus_0_28pct_cost': round(mean_net - 0.28, 4),
    }


def _group(rows: list[dict], key: str) -> dict:
    groups = defaultdict(list)
    for r in rows:
        val = r.get(key)
        groups[str(val if val is not None else 'MISSING')].append(r)
    return {k: _metrics(v) for k, v in sorted(groups.items())}


def _quartile(v: float | None) -> str:
    if v is None:
        return 'MISSING'
    if v < 25:
        return 'Q1_0_25'
    if v < 50:
        return 'Q2_25_50'
    if v < 75:
        return 'Q3_50_75'
    return 'Q4_75_100'


def _rank_buckets(rows: list[dict], field: str) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[_quartile(_f(r, field))].append(r)
    return {k: _metrics(v) for k, v in sorted(groups.items())}


def _top_bottom_delta(table: dict) -> float | None:
    top = table.get('Q4_75_100') or {}
    bottom = table.get('Q1_0_25') or {}
    if int(top.get('n') or 0) < 20 or int(bottom.get('n') or 0) < 20:
        return None
    try:
        return round(float(top['mean_net_pct']) - float(bottom['mean_net_pct']), 4)
    except Exception:
        return None


def build_report() -> dict:
    rows = _latest_complete()
    ready = [r for r in rows if r.get('decision') == 'READY']
    rejected = [r for r in rows if r.get('decision') == 'REJECT']

    opp = _rank_buckets(rows, 'opportunity_pct_shadow')
    rs = _rank_buckets(rows, 'rs_btc4h_pct_rank')
    accel = _rank_buckets(rows, 'accel1h_pct_rank')
    ready_opp = _rank_buckets(ready, 'opportunity_pct_shadow')

    regime_counts = defaultdict(int)
    for r in rows:
        regime_counts[str(r.get('regime') or 'MISSING')] += 1
    regimes_with_25 = sum(1 for n in regime_counts.values() if n >= 25)

    sample_ready = len(rows) >= MIN_MODEL_SAMPLE and len(ready) >= max(60, MIN_MODEL_SAMPLE // 4)
    report = {
        'version': 1,
        'generated_at': time.time(),
        'status': 'SHADOW_RESEARCH_ONLY_NOT_LIVE_GATE',
        'complete_events': len(rows),
        'ready_events': len(ready),
        'rejected_events': len(rejected),
        'minimum_model_sample': MIN_MODEL_SAMPLE,
        'sample_ready_for_formal_modeling': sample_ready,
        'regimes_with_at_least_25_events': regimes_with_25,
        'overall': _metrics(rows),
        'ready_only': _metrics(ready),
        'rejected_only': _metrics(rejected),
        'by_regime': _group(rows, 'regime'),
        'by_lane': _group(rows, 'lane'),
        'opportunity_quartiles_all': opp,
        'opportunity_quartiles_ready': ready_opp,
        'rs_vs_btc_rank_quartiles': rs,
        'acceleration_rank_quartiles': accel,
        'evidence_deltas': {
            'opportunity_top_minus_bottom_mean_net_pct': _top_bottom_delta(opp),
            'rs_top_minus_bottom_mean_net_pct': _top_bottom_delta(rs),
            'acceleration_top_minus_bottom_mean_net_pct': _top_bottom_delta(accel),
        },
        'promotion_policy': {
            'automatic_live_promotion': False,
            'required_next_steps': [
                'chronological discovery/calibration/untouched-test',
                'walk-forward validation',
                'symbol holdout',
                'regime holdout',
                'cost stress',
                'bootstrap/Monte Carlo',
            ],
        },
    }
    return report


def run_once() -> dict:
    report = build_report()
    _atomic_write(REPORT_PATH, report)
    d = report['evidence_deltas']
    print(
        f"[shadow-research] complete={report['complete_events']} ready={report['ready_events']} "
        f"sampleReady={report['sample_ready_for_formal_modeling']} "
        f"oppDelta={d['opportunity_top_minus_bottom_mean_net_pct']} "
        f"rsDelta={d['rs_top_minus_bottom_mean_net_pct']}",
        flush=True,
    )
    return report


def main() -> None:
    print(f'[shadow-research] ONLINE read_only=True poll={POLL_SEC}s min_model_sample={MIN_MODEL_SAMPLE} bootstrap={BOOTSTRAP_N}', flush=True)
    while True:
        started = time.time()
        try:
            run_once()
        except Exception as exc:
            print(f'[shadow-research] warning {type(exc).__name__}: {str(exc)[:180]}', flush=True)
        time.sleep(max(10.0, POLL_SEC - (time.time() - started)))


if __name__ == '__main__':
    main()
