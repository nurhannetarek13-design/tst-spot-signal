#!/usr/bin/env python3
"""Fail-closed portfolio risk gate for shadow/production review.
No order placement is implemented here.
"""
from __future__ import annotations
from dataclasses import dataclass
import json

@dataclass(frozen=True)
class RiskPolicy:
    max_daily_loss_usdt: float = 2.0
    max_open_positions: int = 3
    max_stake_usdt: float = 10.0
    min_expected_net_pct: float = 0.20
    min_probability: float = 0.60
    max_spread_bps: float = 12.0
    max_pair_correlation: float = 0.85


def decide(*, p_tp: float, expected_net_pct: float, spread_bps: float, stake_usdt: float,
           daily_pnl_usdt: float, open_positions: int, max_existing_corr: float,
           policy: RiskPolicy = RiskPolicy()) -> dict:
    reasons=[]
    if daily_pnl_usdt <= -policy.max_daily_loss_usdt: reasons.append('DAILY_LOSS_CAP')
    if open_positions >= policy.max_open_positions: reasons.append('MAX_OPEN_POSITIONS')
    if stake_usdt > policy.max_stake_usdt: reasons.append('STAKE_TOO_HIGH')
    if p_tp < policy.min_probability: reasons.append('PROBABILITY_TOO_LOW')
    if expected_net_pct < policy.min_expected_net_pct: reasons.append('EDGE_TOO_LOW')
    if spread_bps > policy.max_spread_bps: reasons.append('SPREAD_TOO_WIDE')
    if max_existing_corr > policy.max_pair_correlation: reasons.append('CORRELATION_TOO_HIGH')
    return {'allow':not reasons,'reasons':reasons,'policy':policy.__dict__}

if __name__=='__main__':
    x=decide(p_tp=.68,expected_net_pct=.45,spread_bps=2,stake_usdt=10,daily_pnl_usdt=0,open_positions=0,max_existing_corr=.4)
    assert x['allow']; print(json.dumps({'selftest':'PASS','decision':x}))
