from pathlib import Path

# First patch the sizing module itself. The legacy Score/100 is heuristic, not a
# calibrated probability, so it must not increase real capital allocation.
sizing_path = Path('/freqtrade/dynamic_sizing.py')
ds = sizing_path.read_text(encoding='utf-8')
legacy_fraction = "    capital_fraction = _score_fraction(score, hard_fraction_cap)\n"
risk_fraction = """    # Score/100 is not yet statistically calibrated. Until the probability/EV
    # model passes frozen OOS + walk-forward validation, position size is driven
    # by stop-risk, balance and a concentration cap only.
    uncalibrated_cap = min(max(_env_float('MAX_UNCALIBRATED_STAKE_FRACTION', 0.50), 0.05), 0.80)
    capital_fraction = min(hard_fraction_cap, uncalibrated_cap)
"""
if legacy_fraction in ds:
    ds = ds.replace(legacy_fraction, risk_fraction, 1)
elif 'MAX_UNCALIBRATED_STAKE_FRACTION' not in ds:
    raise SystemExit('dynamic sizing patch failed: score-fraction marker missing')
legacy_comment = """    # Stronger signals can use more capital, but actual dollars-at-risk remain
    # bounded by the stop, per-trade risk and daily-loss limits.
"""
if legacy_comment in ds:
    ds = ds.replace(legacy_comment, """    # Heuristic score cannot increase capital. Dollars-at-risk remain bounded
    # by stop distance, per-trade risk and the hard portfolio limits.
""", 1)

# Durable safety ceilings. Environment variables may tighten these limits but
# cannot widen them accidentally. Keep the production contract at <=7 USDT
# stake and <=0.20 USDT stop-risk per trade.
legacy_max_risk = "    max_risk = max(0.01, _env_float('MAX_RISK_PER_TRADE_USDT', 0.50))\n"
hard_max_risk = "    max_risk = min(0.20, max(0.01, _env_float('MAX_RISK_PER_TRADE_USDT', 0.20)))\n"
if legacy_max_risk in ds:
    ds = ds.replace(legacy_max_risk, hard_max_risk, 1)
elif hard_max_risk not in ds:
    raise SystemExit('dynamic sizing patch failed: max-risk marker missing')
legacy_exec_cap = "    execution_cap = max(min_stake, _env_float('MAX_EXECUTION_STAKE_USDT', 40.0))\n"
hard_exec_cap = "    execution_cap = min(7.0, max(min_stake, _env_float('MAX_EXECUTION_STAKE_USDT', 7.0)))\n"
if legacy_exec_cap in ds:
    ds = ds.replace(legacy_exec_cap, hard_exec_cap, 1)
elif hard_exec_cap not in ds:
    raise SystemExit('dynamic sizing patch failed: execution-cap marker missing')

compile(ds, str(sizing_path), 'exec')
sizing_path.write_text(ds, encoding='utf-8')

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

# Normalize the actual payload sent to Cloudflare. Preflight uses the same 7 USDT
# ceiling as production, so diagnostics cannot advertise an amount execution
# would reject. Any live payload is clamped to the same hard ceiling.
raw_marker = "    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')\n"
raw_replacement = (
    "    if payload.get('dryRun') is True and payload.get('strategy') == 'FAST_EXECUTION_PREFLIGHT':\n"
    "        payload['stakeUSDT'] = 7.0\n"
    "    elif 'stakeUSDT' in payload:\n"
    "        payload['stakeUSDT'] = max(5.0, min(float(payload['stakeUSDT']), 7.0))\n"
    "    print(f\"[fast-ingest-request] url={FAST_INGEST_URL} symbol={payload.get('symbol')} stake={payload.get('stakeUSDT')} dryRun={payload.get('dryRun')}\", flush=True)\n"
    + raw_marker
)
if '[fast-ingest-request]' not in s:
    if raw_marker not in s:
        raise SystemExit('dynamic sizing patch failed: raw payload marker missing')
    s = s.replace(raw_marker, raw_replacement, 1)
else:
    # A prior version of this patch may already have inserted the normalization.
    s = s.replace("payload['stakeUSDT'] = 40.0", "payload['stakeUSDT'] = 7.0")
    s = s.replace("min(float(payload['stakeUSDT']), 100.0)", "min(float(payload['stakeUSDT']), 7.0)")

preflight_probe_old = (
    "        'stakeUSDT': 5.5,\n"
    "        'score': 100,\n"
    "        'strategy': 'FAST_EXECUTION_PREFLIGHT',\n"
)
preflight_probe_legacy = (
    "        'stakeUSDT': 40.0,\n"
    "        'score': 100,\n"
    "        'strategy': 'FAST_EXECUTION_PREFLIGHT',\n"
)
preflight_probe_new = (
    "        'stakeUSDT': 7.0,\n"
    "        'score': 100,\n"
    "        'strategy': 'FAST_EXECUTION_PREFLIGHT',\n"
)
if preflight_probe_old in s:
    s = s.replace(preflight_probe_old, preflight_probe_new, 1)
elif preflight_probe_legacy in s:
    s = s.replace(preflight_probe_legacy, preflight_probe_new, 1)

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
    "    print(f'[sizing] {symbol} free_usdt={free_usdt:.2f} score={score:.0f} stake_usdt={stake_usdt:.2f} sl_pct={sl_pct*100:.2f}% sizing=risk-only')\n\n"
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

compile(s, str(path), 'exec')
path.write_text(s)

# Harden the final execution relay too. This is deliberately independent from
# environment configuration: an accidental MAX_EXECUTION_STAKE_USDT=100 must
# still be unable to authorize a trade above 7 USDT.
proxy_path = Path('/freqtrade/front_proxy.py')
proxy = proxy_path.read_text(encoding='utf-8')
legacy_proxy = "    MAX_EXECUTION_STAKE_USDT=max(5.0,float(os.getenv('MAX_EXECUTION_STAKE_USDT','40')))\nexcept Exception:\n    MAX_EXECUTION_STAKE_USDT=40.0\n"
hard_proxy = "    MAX_EXECUTION_STAKE_USDT=min(7.0,max(5.0,float(os.getenv('MAX_EXECUTION_STAKE_USDT','7'))))\nexcept Exception:\n    MAX_EXECUTION_STAKE_USDT=7.0\n"
if legacy_proxy in proxy:
    proxy = proxy.replace(legacy_proxy, hard_proxy, 1)
elif hard_proxy not in proxy:
    raise SystemExit('front-proxy safety patch failed: stake ceiling marker missing')
compile(proxy, str(proxy_path), 'exec')
proxy_path.write_text(proxy, encoding='utf-8')

print('[live-safety-patch] OK quality gates + risk-only adaptive sizing + hard stake<=7 risk<=0.20 ceilings')
