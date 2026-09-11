from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable

import httpx

from .config import Settings, settings
from .engine import V2Engine
from .execution_journal import ExecutionJournal


def run(
    once: bool,
    *,
    runtime_settings: Settings = settings,
    engine_factory: Callable[[Settings], V2Engine] = V2Engine,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
) -> None:
    runtime_settings.validate()
    engine = engine_factory(runtime_settings)

    try:
        notifier = getattr(engine, "notifier", None)
        telegram_enabled = bool(getattr(notifier, "enabled", False))
        persistence_proven: bool | None = None
        persistence_reason: str | None = None
        persistence_previous_revision: str | None = None

        if runtime_settings.persistent_state:
            state = getattr(engine, "state", None)
            if state is None or not hasattr(state, "verify_persistence"):
                raise RuntimeError("PERSISTENT_STATE_BACKEND_MISSING")
            probe = state.verify_persistence(runtime_settings.deploy_revision)
            persistence_proven = bool(probe.proven)
            persistence_reason = probe.reason
            persistence_previous_revision = probe.previous_revision

            if runtime_settings.mode in {"paper", "live"} and not probe.proven:
                print(
                    json.dumps(
                        {
                            "event": "persistence_gate_blocked",
                            "mode": runtime_settings.mode,
                            "deploy_revision": runtime_settings.deploy_revision,
                            "persistent_state": runtime_settings.persistent_state,
                            "persistence_proven": False,
                            "persistence_reason": probe.reason,
                            "persistence_previous_revision": probe.previous_revision,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                raise RuntimeError(
                    "PERSISTENCE_NOT_PROVEN: state must survive a different deployment revision before Paper/Live can start"
                )

        if runtime_settings.mode == "live":
            journal = ExecutionJournal(runtime_settings.state_db)
            pending = journal.pending()
            if pending:
                print(
                    json.dumps(
                        {
                            "event": "live_recovery_gate_blocked",
                            "mode": runtime_settings.mode,
                            "pending_count": len(pending),
                            "pending": [
                                {
                                    "client_order_id": row.client_order_id,
                                    "symbol": row.symbol,
                                    "stage": row.stage,
                                }
                                for row in pending
                            ],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                raise RuntimeError(
                    "LIVE_RECOVERY_REQUIRED: unresolved execution journal entries must be reconciled before Live can start"
                )

        startup_alert_sent = False
        if runtime_settings.startup_alert and notifier is not None:
            startup_alert_sent = bool(
                notifier.send(
                    f"V2 {runtime_settings.mode.upper()} ONLINE\n"
                    f"Live trading: {'ON' if runtime_settings.live_trading else 'OFF'}"
                )
            )

        print(
            json.dumps(
                {
                    "event": "startup",
                    "mode": runtime_settings.mode,
                    "live_trading": runtime_settings.live_trading,
                    "persistent_state": runtime_settings.persistent_state,
                    "persistence_proven": persistence_proven,
                    "persistence_reason": persistence_reason,
                    "persistence_previous_revision": persistence_previous_revision,
                    "telegram_enabled": telegram_enabled,
                    "startup_alert_requested": runtime_settings.startup_alert,
                    "startup_alert_sent": startup_alert_sent,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        cycles = 0
        while True:
            try:
                summary = engine.scan_once()
                print(engine.dump_summary(summary), flush=True)
            except httpx.HTTPError as exc:
                error = {
                    "event": "market_data_error",
                    "error_type": type(exc).__name__,
                    "mode": runtime_settings.mode,
                }
                print(json.dumps(error, sort_keys=True), flush=True)
                # One-shot smoke/diagnostic runs must fail loudly. A continuous
                # worker may survive transient public-market-data outages and
                # try again on the next normal scan interval.
                if once:
                    raise

            cycles += 1
            if once or (max_cycles is not None and cycles >= max_cycles):
                return
            sleep_fn(max(15, runtime_settings.scan_interval_seconds))
    finally:
        engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated TST Spot Bot V2")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
    args = parser.parse_args()
    run(args.once)


if __name__ == "__main__":
    main()
