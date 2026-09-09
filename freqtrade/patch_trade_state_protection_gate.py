from pathlib import Path

path = Path('/freqtrade/trade_state.py')
s = path.read_text(encoding='utf-8')
old = '''    for pos in positions:
        try:
            entry = float(pos.get('entry') or 0)
            stop = float(pos.get('stop') or 0)
            qty = float(pos.get('quantity') or 0)
            if entry > 0 and stop > 0 and qty > 0:
                risk += max(0.0, entry - stop) * qty
            else:
                incomplete += 1
        except Exception:
            incomplete += 1
'''
new = '''    for pos in positions:
        try:
            entry = float(pos.get('entry') or 0)
            stop = float(pos.get('stop') or 0)
            qty = float(pos.get('quantity') or 0)
            # Intended stop parameters are NOT protection. A confirmed BUY is
            # considered complete only after Binance has a known active OCO list.
            status = str(pos.get('status') or '').upper()
            try:
                oco_id = int(float(pos.get('oco_order_list_id') or 0))
            except Exception:
                oco_id = 0
            protected = status == 'OCO_ACTIVE' and oco_id > 0
            risk_fields_ok = entry > 0 and stop > 0 and qty > 0
            # Fail closed if protection is missing OR the reconciler could not
            # reconstruct enough entry/stop/quantity data to calculate risk.
            if not protected or not risk_fields_ok:
                incomplete += 1
            if risk_fields_ok:
                risk += max(0.0, entry - stop) * qty
        except Exception:
            incomplete += 1
'''
if new not in s:
    if old not in s:
        raise SystemExit('trade-state protection patch marker missing')
    s = s.replace(old, new, 1)
compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[trade-state-protection-patch] OK incomplete_count requires active OCO + complete risk fields')
