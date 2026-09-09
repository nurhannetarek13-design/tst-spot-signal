from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text(encoding='utf-8')

marker = "            ranked.sort(reverse=True, key=lambda x: x[0])\n"
insert = marker + "            if ranked:\n                preview = ', '.join(\n                    f\"{symbol}:score={score:.0f},ext={float(m.get('live_extension', -9))*100:+.3f}%,mom5={float(m.get('mom5', 0))*100:+.2f}%,volx={float(m.get('volume_ratio', 0)):.2f},taker={float(m.get('taker_buy_ratio', 0))*100:.1f}%,rsi={float(m.get('rsi', 0)):.1f}\"\n                    for score, symbol, change, volume, m in ranked[:5]\n                )\n                print(f'[scoreboard] {preview}')\n"

if '[scoreboard]' not in s:
    if marker not in s:
        raise SystemExit('scoreboard patch failed: ranking marker missing')
    s = s.replace(marker, insert, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[scoreboard-patch] OK internal top-score diagnostics; no Telegram messages')
