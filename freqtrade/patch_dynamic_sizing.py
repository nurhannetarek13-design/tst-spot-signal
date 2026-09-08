from pathlib import Path

path = Path('/freqtrade/fast_entry_engine.py')
s = path.read_text()

import_marker = 'import telegram_signal_bridge as bridge\n'
if 'import dynamic_sizing\n' not in s:
    if import_marker not in s:
        raise SystemExit('dynamic sizing patch failed: import marker missing')
    s = s.replace(import_marker, import_marker + 'import dynamic_sizing\n', 1)
if 'import entry_quality\n' not in s:
    if import_marker not in s:
        raise SystemExit('entry quality patch failed: import marker missing')
    s = s.replace(import_marker, import_marker + 'import entry_quality\n', 1)

preflight_marker = "        execution_ready = row.get('status') == 'FAST_SIGNAL_DRYRUN_OK' and row.get('userConfirmationRequired') is True and row.get('autoBuy') is False\n"
preflight_extra = (
    "        if execution_ready:\n"
    "            dynamic_sizing.free_usdt()\n"
    "            print('[dynamic-sizing] balance read OK')\n"
    "            quality_probe = entry_quality.runtime_preflight()\n"
    "            print(f\"[entry-quality-preflight] OK data=LIVE btc_regime={'OK' if quality_probe.get('btc_regime_ok') else 'BLOCK'} btc15={quality_probe.get('btc_mom15', 0.0)*100:+.2f}% btc1h={quality_probe.get('btc_mom1h', 0.0)*100:+.2f}%\")\n"
)
if '[entry-quality-preflight] OK' not in s:
    if preflight_marker not in s:
        raise SystemExit('quality preflight patch failed: preflight marker missing')
    # Remove the older dynamic-sizing-only injected block if present, then install the combined block.
    old_extra = "        if execution_ready:\n            dynamic_sizing.free_usdt()\n            print('[dynamic-sizing] balance read OK')\n"
    if old_extra in s:
        s = s.replace(old_extra, '', 1)
    s = s.replace(preflight_marker, preflight_marker + preflight_extra, 1)

quality_marker = "    m = market_metrics(symbol)\n    score, reasons = score_setup(m)\n"
quality_extra = (
    "    quality_ok, quality_reason, quality_ctx = entry_quality.validate_entry(symbol, m)\n"
    "    if not quality_ok:\n"
    "        move2h = float(quality_ctx.get('move_from_2h_low') or 0.0)\n"
    "        print(f\"[quality-gate] {symbol} BLOCKED reason={quality_reason} volx={m['volume_ratio']:.2f} taker={m['taker_buy_ratio']*100:.1f}% dist={m['distance_to_breakout']*100:.2f}% move2h={move2h*100:.2f}%\")\n"
    "        return False\n"
    "    btc = quality_ctx.get('btc') or {}\n"
    "    print(f\"[quality-gate] {symbol} PASS move2h={quality_ctx.get('move_from_2h_low', 0.0)*100:.2f}% btc15={btc.get('mom15', 0.0)*100:+.2f}% btc1h={btc.get('mom1h', 0.0)*100:+.2f}%\")\n"
)
if '[quality-gate] {symbol} BLOCKED' not in s:
    if quality_marker not in s:
        raise SystemExit('entry quality patch failed: maybe_signal marker missing')
    s = s.replace(quality_marker, quality_marker + quality_extra, 1)

rank_marker = (
    "                    m = market_metrics(symbol)\n"
    "                    score, _ = score_setup(m)\n"
    "                    ranked.append((score, symbol, change, volume, m))\n"
)
rank_replacement = (
    "                    m = market_metrics(symbol)\n"
    "                    micro_ok, _ = entry_quality.micro_gate(m)\n"
    "                    if not micro_ok:\n"
    "                        continue\n"
    "                    score, _ = score_setup(m)\n"
    "                    ranked.append((score, symbol, change, volume, m))\n"
)
if 'micro_ok, _ = entry_quality.micro_gate(m)' not in s:
    if rank_marker not in s:
        raise SystemExit('entry quality patch failed: ranking marker missing')
    s = s.replace(rank_marker, rank_replacement, 1)

payload_marker = "    payload = {\n        'id': f'{symbol}-{int(now)}',"
insert = (
    "    stake_usdt, free_usdt = dynamic_sizing.recommended_stake(sl_pct, score)\n"
    "    if stake_usdt is None:\n"
    "        print(f'[sizing] {symbol} blocked: free_usdt={free_usdt:.2f} below adaptive minimum/risk allowance')\n"
    "        return False\n"
    "    print(f'[sizing] {symbol} free_usdt={free_usdt:.2f} score={score:.0f} stake_usdt={stake_usdt:.2f} sl_pct={sl_pct*100:.2f}%')\n\n"
    + payload_marker
)
if '[sizing] {symbol} free_usdt=' not in s:
    if payload_marker not in s:
        raise SystemExit('dynamic sizing patch failed: live payload marker missing')
    s = s.replace(payload_marker, insert, 1)

live_tail = s.find(payload_marker)
if live_tail < 0:
    raise SystemExit('dynamic sizing patch failed: payload missing after insert')
idx = s.find("        'stakeUSDT': 5.5,", live_tail)
if idx < 0:
    if "        'stakeUSDT': stake_usdt," not in s[live_tail:]:
        raise SystemExit('dynamic sizing patch failed: live fixed stake missing')
else:
    s = s[:idx] + "        'stakeUSDT': stake_usdt," + s[idx + len("        'stakeUSDT': 5.5,"):]

path.write_text(s)
print('[live-safety-patch] OK quality preflight + hard gates + filtered ranking + adaptive sizing enabled')
