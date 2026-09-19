#!/usr/bin/env python3
"""PAPER-only production entrypoint: native strategy runner + audited state.

No exchange credentials, live mode, or private order endpoints. A repaired legacy
lock never erases real portfolio positions, executed trades or per-strategy
attempt history. All risk checks continue to run inside native_paper/core.
"""
import json

import multi_bot as core
import native_paper as runner
from paper_audit import reconcile_legacy_locks, record_run


def run(config):
    # Validate before touching persistent state. Do not repair on unsupported
    # configs, and never turn a blocked/unfinished source into an enabled trade.
    runner.validate(config)
    prior = core.read_state(config)
    closed_before = len(prior.get('closed_trades', []))
    repaired = reconcile_legacy_locks(prior)
    if repaired:
        core.save_state(prior)
    current = runner.run(config)
    report = record_run(current, closed_before, repaired)
    core.save_state(current)
    print(json.dumps(dict(paper_audit=report, legacy_locks_repaired=repaired), indent=2))
    return current


if __name__ == '__main__':
    run(json.loads(core.CONFIG_PATH.read_text()))
