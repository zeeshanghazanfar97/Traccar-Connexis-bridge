"""Run with python -m adapter; SIGTERM stops the polling loop cleanly."""

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

from .bridge import Bridge, BridgeError, ConnexisClient, StateStore, StopRequested
from .config import Config, positive_number

LOG = logging.getLogger("adapter")


def write_health(path):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(str(time.time()), encoding="utf-8")
    temporary.replace(path)


def healthy(path, max_age):
    try:
        age = time.time() - float(path.read_text(encoding="utf-8"))
        return 0 <= age <= max_age
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Poll Connexis and send current positions to Traccar")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Poll once and exit nonzero on any failure")
    modes.add_argument("--dry-run", action="store_true", help="Poll once and print unsent OsmAnd payloads without forwarding")
    modes.add_argument("--list-assets", action="store_true", help="Print discovered plate identifiers and exit")
    modes.add_argument("--healthcheck", action="store_true", help="Check freshness of the last successful poll")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    operation = "initializing the adapter"
    try:
        if args.healthcheck:
            return 0 if healthy(Path(os.getenv("HEALTH_PATH", "/tmp/adapter-health")),
                                positive_number(os.getenv("HEALTH_MAX_AGE", "600"), "HEALTH_MAX_AGE")) else 1
        config = Config.from_env()
        if not config.verify_tls:
            LOG.warning("Connexis TLS certificate verification is disabled")
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda _signum, _frame: stop.set())
        # One-shot commands must not alter the running service's health marker.
        if not (args.once or args.dry_run or args.list_assets):
            operation = f"clearing health marker {config.health_path}"
            config.health_path.unlink(missing_ok=True)
        operation = f"opening state database {config.state_path}"
        state = StateStore(config.state_path)
        try:
            operation = "initializing HTTP/TLS clients"
            if args.list_assets:
                provider = ConnexisClient(config, state, stop)
                operation = f"discovering vehicles and updating request quota in {config.state_path}"
                print(json.dumps(provider.assets(), indent=2))
                return 0
            bridge = Bridge(config, state, stop)
            if args.once or args.dry_run:
                operation = f"polling and updating state database {config.state_path}"
                return 0 if bridge.poll(dry_run=args.dry_run) else 1
            LOG.info("Connexis adapter started; poll interval %.1fs, API limit %d requests/minute, input speed unit %s",
                     config.poll_interval, config.requests_per_minute, config.speed_unit)
            delay = config.poll_interval
            while not stop.is_set():
                try:
                    operation = f"polling and updating state database {config.state_path}"
                    success = bridge.poll()
                except BridgeError as exc:
                    LOG.error("Polling failed: %s", exc)
                    success = False
                if success:
                    operation = f"writing health marker {config.health_path}"
                    write_health(config.health_path)
                    delay = config.poll_interval
                else:
                    delay = min(delay * 2, config.max_backoff)
                    LOG.warning("Retrying in %.1f seconds", delay)
                stop.wait(delay)
            LOG.info("Adapter stopped")
        finally:
            state.close()
        return 0
    except StopRequested:
        LOG.info("Adapter stopped")
        return 0
    except (ValueError, BridgeError) as exc:
        LOG.error("%s", exc)
        return 1
    except (OSError, sqlite3.Error) as exc:
        LOG.error("Local I/O failure while %s (%s): %s", operation, type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
