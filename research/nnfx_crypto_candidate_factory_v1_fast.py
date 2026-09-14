#!/usr/bin/env python3
"""Performance-only runner for the frozen 81-candidate V1 factory.

It replaces the row-by-row pandas execution loop with NumPy arrays. Candidate
logic, costs, risk, time split, gates, and output schema remain unchanged.
"""
import math
import numpy as np
import nnfx_crypto_candidate_factory_v1 as factory

factory.VOLATILITY = ["atr_normal"]


def fast_simulate(symbol, d, c, cost, risk):
    entry_mask, exit_mask = factory.candidate_masks(d, c)
    opens = d["open"].to_numpy(dtype=float)
    highs = d["high"].to_numpy(dtype=float)
    lows = d["low"].to_numpy(dtype=float)
    closes = d["close"].to_numpy(dtype=float)
    atrs = d["atr14"].to_numpy(dtype=float)
    ts = d["ts"].astype(str).to_numpy()

    trades = []
    position = None
    pending_entry = False
    pending_exit = False
    warmup = 90
    n = len(d)

    for i in range(warmup, n):
        if pending_exit and position is not None:
            exit_px = opens[i] * (1.0 - cost.slippage_rate)
            qty = position["qty"]
            gross = qty * (exit_px - position["entry"])
            fees = cost.fee_rate * qty * (position["entry"] + exit_px)
            pnl = gross - fees
            trades.append(factory.Trade(
                symbol=symbol,
                entry_ts=position["entry_ts"],
                exit_ts=ts[i],
                entry=position["entry"],
                exit=exit_px,
                qty=qty,
                pnl_usdt=pnl,
                return_pct=pnl / (qty * position["entry"]) * 100.0,
                bars_held=i - position["entry_i"],
                reason="signal_exit",
            ))
            position = None
            pending_exit = False

        if pending_entry and position is None:
            entry_px = opens[i] * (1.0 + cost.slippage_rate)
            atr = atrs[i - 1]
            if math.isfinite(atr) and atr > 0 and entry_px > 0:
                qty = risk.trade_usdt / entry_px
                position = {
                    "entry": entry_px,
                    "qty": qty,
                    "entry_i": i,
                    "entry_ts": ts[i],
                    "entry_atr": atr,
                }
            pending_entry = False

        if position is not None:
            entry = position["entry"]
            atr = position["entry_atr"]
            stop = entry - risk.stop_atr * atr
            target = entry + risk.target_atr * atr

            # Same conservative ambiguity rule as the canonical V1: stop first.
            if lows[i] <= stop:
                exit_px = stop * (1.0 - cost.slippage_rate)
                qty = position["qty"]
                gross = qty * (exit_px - entry)
                fees = cost.fee_rate * qty * (entry + exit_px)
                pnl = gross - fees
                trades.append(factory.Trade(
                    symbol, position["entry_ts"], ts[i], entry, exit_px, qty, pnl,
                    pnl / (qty * entry) * 100.0, i - position["entry_i"], "atr_stop"
                ))
                position = None
                pending_exit = False
                continue

            if highs[i] >= target:
                exit_px = target
                qty = position["qty"]
                gross = qty * (exit_px - entry)
                fees = cost.fee_rate * qty * (entry + exit_px)
                pnl = gross - fees
                trades.append(factory.Trade(
                    symbol, position["entry_ts"], ts[i], entry, exit_px, qty, pnl,
                    pnl / (qty * entry) * 100.0, i - position["entry_i"], "atr_target"
                ))
                position = None
                pending_exit = False
                continue

            if i - position["entry_i"] >= risk.max_bars or exit_mask[i]:
                pending_exit = True
            continue

        if i + 1 < n and entry_mask[i]:
            pending_entry = True

    if position is not None:
        i = n - 1
        exit_px = closes[i] * (1.0 - cost.slippage_rate)
        qty = position["qty"]
        gross = qty * (exit_px - position["entry"])
        fees = cost.fee_rate * qty * (position["entry"] + exit_px)
        pnl = gross - fees
        trades.append(factory.Trade(
            symbol, position["entry_ts"], ts[i], position["entry"], exit_px, qty, pnl,
            pnl / (qty * position["entry"]) * 100.0, i - position["entry_i"], "segment_end"
        ))
    return trades


factory.simulate = fast_simulate

if __name__ == "__main__":
    factory.main()
