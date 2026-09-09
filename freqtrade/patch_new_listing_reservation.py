from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

marker = "def symbol_ok(symbol: str) -> bool:\n    if not symbol.endswith('USDT') or symbol in EXCLUDE:\n        return False\n"
replacement = "def symbol_ok(symbol: str) -> bool:\n    reserved = (os.getenv('NEW_LISTING_SYMBOL') or '').strip()\n    start_iso = (os.getenv('NEW_LISTING_START_UTC') or '').strip()\n    if reserved and symbol == reserved and start_iso:\n        try:\n            start_dt = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))\n            if start_dt.tzinfo is None:\n                start_dt = start_dt.replace(tzinfo=timezone.utc)\n            reserve_until = start_dt.timestamp() + float(os.getenv('NEW_LISTING_MODE_TTL_HOURS', '24')) * 3600.0\n            if time.time() < reserve_until:\n                return False\n        except Exception:\n            # Fail closed for the configured listing if its schedule is malformed.\n            return False\n    if not symbol.endswith('USDT') or symbol in EXCLUDE:\n        return False\n"

if 'reserve_until = start_dt.timestamp()' not in s:
    if marker not in s:
        raise SystemExit('new listing reservation patch failed: symbol_ok marker missing')
    s = s.replace(marker, replacement, 1)

if 'NEW_LISTING_MODE_TTL_HOURS' not in s:
    raise SystemExit('new listing reservation patch failed: reservation logic missing')

path.write_text(s, encoding='utf-8')
print('[new-listing-reservation-patch] OK configured listing reserved from normal momentum scanner for dedicated mode')
