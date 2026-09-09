from pathlib import Path

path = Path('/freqtrade/front_proxy.py')
s = path.read_text(encoding='utf-8')
old = "        if not re.fullmatch(r'[A-Z0-9]{1,20}USDT',symbol):\n            return self.send_json(400,{'ok':False,'status':'BAD_SYMBOL'})\n"
new = "        base_symbol = symbol[:-4] if symbol.endswith('USDT') else ''\n        valid_symbol = 1 <= len(base_symbol) <= 20 and all(ch.isalnum() for ch in base_symbol)\n        if not valid_symbol:\n            return self.send_json(400,{'ok':False,'status':'BAD_SYMBOL'})\n"

if 'valid_symbol = 1 <= len(base_symbol)' not in s:
    if old not in s:
        raise SystemExit('unicode symbol patch failed: front proxy symbol guard marker missing')
    s = s.replace(old, new, 1)

if 'valid_symbol = 1 <= len(base_symbol)' not in s:
    raise SystemExit('unicode symbol patch failed: Unicode-safe symbol validation missing')

path.write_text(s, encoding='utf-8')
print('[unicode-symbol-patch] OK Unicode/alphanumeric Binance base symbols allowed; USDT suffix remains mandatory')
