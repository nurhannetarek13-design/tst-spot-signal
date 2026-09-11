from __future__ import annotations

import argparse
import time

from .config import settings
from .engine import V2Engine


def run(once: bool) -> None:
    settings.validate()
    engine = V2Engine(settings)
    try:
        while True:
            summary = engine.scan_once()
            print(engine.dump_summary(summary), flush=True)
            if once:
                return
            time.sleep(max(15, settings.scan_interval_seconds))
    finally:
        engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated TST Spot Bot V2")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
    args = parser.parse_args()
    run(args.once)


if __name__ == "__main__":
    main()
