from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable

import httpx

from .config import Settings, settings
from .engine import V2Engine


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
    notifier = getattr(engine, "notifier", None)
    telegram_enabled = bool(getattr(notifier, "enabled", False))
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
                "telegram_enabled": telegram_enabled,
                "startup_alert_requested": runtime_settings.startup_alert,
                "startup_alert_sent": startup_alert_sent,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    cycles = 0
    try:
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
