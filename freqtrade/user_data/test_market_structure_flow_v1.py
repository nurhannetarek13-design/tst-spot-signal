from __future__ import annotations

import time
import fast_entry_engine as f


def _trades(strong: bool):
    out = []
    for i in range(240):
        p = 100.0 + (i % 30) * 0.008
        # strong=True => roughly 70% aggressive buyer flow (m=False).
        if strong:
            maker_buyer = (i % 10) < 3
        else:
            maker_buyer = (i % 10) < 8
        out.append({'p': f'{p:.4f}', 'q': '2.0', 'm': maker_buyer})
    return out


def _depth(strong: bool):
    bids = []
    asks = []
    for i in range(40):
        bp = 100.10 - i * 0.005
        ap = 100.11 + i * 0.005
        if strong:
            bq, aq = 10.0, 5.0
        else:
            bq, aq = 4.0, 12.0
        bids.append([f'{bp:.4f}', f'{bq:.4f}'])
        asks.append([f'{ap:.4f}', f'{aq:.4f}'])
    return {'bids': bids, 'asks': asks}


def _klines(strong: bool):
    now = int(time.time() * 1000)
    rows = []
    for i in range(90):
        o = 100.00
        h = 100.10
        l = 99.90
        c = 100.00
        qv = 100.0
        if i >= 86 and strong:
            h = 100.35
            l = 100.00
            c = 100.26
            qv = 180.0
        elif i >= 86 and not strong:
            h = 100.35
            l = 99.88
            c = 99.96
            qv = 180.0
        open_ms = now - (90 - i + 2) * 60_000
        close_ms = open_ms + 59_000
        rows.append([open_ms, str(o), str(h), str(l), str(c), '1', close_ms, str(qv), 10, '0.5', '50', '0'])
    return rows


def _run(strong: bool):
    original = f.api
    f._msf_cache.clear()
    try:
        def fake_api(path, params):
            if path == '/aggTrades':
                return _trades(strong)
            if path == '/depth':
                return _depth(strong)
            if path == '/klines':
                return _klines(strong)
            raise AssertionError(path)
        f.api = fake_api
        return f._market_structure_flow_gate('TESTUSDT', {'entry': 100.12, 'stakeUSDT': 10.0})
    finally:
        f.api = original
        f._msf_cache.clear()


def test_strong_flow_passes():
    ok, meta = _run(True)
    assert ok is True, meta
    assert float(meta['score']) >= f.MSF_MIN_SCORE, meta
    assert float(meta['delta']) > 0, meta
    assert float(meta['depth_imbalance']) > 1.0, meta
    assert str(meta['wyckoff']) in ('SOS', 'LPS', 'NEUTRAL'), meta
    assert 0 < float(meta['risk_mult']) <= 1.0, meta


def test_toxic_flow_is_blocked():
    ok, meta = _run(False)
    assert ok is False, meta
    assert str(meta['status']) in ('HARD_REJECT', 'QUALITY_REJECT'), meta
    assert float(meta.get('delta') or 0) < 0, meta
    assert float(meta.get('depth_imbalance') or 9) < 1.0, meta


if __name__ == '__main__':
    test_strong_flow_passes()
    test_toxic_flow_is_blocked()
    print('[test-market-structure-flow-v1] PASS strong-flow=allowed toxic-flow=blocked risk=reduce-only')
