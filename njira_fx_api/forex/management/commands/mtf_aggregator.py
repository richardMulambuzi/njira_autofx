"""
mtf_aggregator — background multi-timeframe signal aggregator.

Runs ``python manage.py mtf_aggregator`` and, every ``MTF_REFRESH_SECONDS``
(default 5), recomputes the macro alignment for each symbol in
``TRADING_SYMBOLS`` (default ``EURUSD``, comma-separated; the legacy
``PEPPERSTONE_SYMBOLS`` variable is honoured as a fallback for backward
compatibility) and publishes it to the Django cache as
``macro_signal_<SYMBOL>`` with a 30-second TTL.

The API layer reads the cached signal (``GET /api/mtf-status/``), so the
aggregator and the API share one source of truth. The loop is resilient:

* per-symbol failures are logged and never kill the loop;
* SIGINT / SIGTERM and ``KeyboardInterrupt`` stop the loop gracefully.
"""
from __future__ import annotations

import logging
import os
import signal
import time

from django.core.cache import cache
from django.core.management.base import BaseCommand

from forex.mtf_strategy import MultiTimeframeStrategy

logger = logging.getLogger("forex.mtf_aggregator")

CACHE_TTL_SECONDS = 30


class Command(BaseCommand):
    help = (
        "Recompute multi-timeframe macro signals every MTF_REFRESH_SECONDS "
        "and publish them to the Django cache (macro_signal_<SYMBOL>)."
    )

    def handle(self, *args: object, **options: object) -> None:
        raw_symbols = os.environ.get("TRADING_SYMBOLS") or os.environ.get(
            "PEPPERSTONE_SYMBOLS", "EURUSD"
        )
        symbols = [
            s.strip().upper() for s in raw_symbols.split(",") if s.strip()
        ]
        refresh_seconds = max(
            1, int(os.environ.get("MTF_REFRESH_SECONDS", "5"))
        )
        strategy = MultiTimeframeStrategy()

        self.stdout.write(
            self.style.SUCCESS(
                f"mtf_aggregator started: symbols={symbols} "
                f"refresh={refresh_seconds}s cache_ttl={CACHE_TTL_SECONDS}s"
            )
        )

        running = True

        def _stop(signum: int, frame: object) -> None:  # noqa: ARG001
            nonlocal running
            running = False
            self.stdout.write(
                self.style.WARNING(f"Received signal {signum} — shutting down…")
            )

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        try:
            while running:
                for symbol in symbols:
                    try:
                        signal_value = strategy.check_market_alignment(symbol)
                        cache.set(
                            f"macro_signal_{symbol}", signal_value, CACHE_TTL_SECONDS
                        )
                        self.stdout.write(
                            f"[{time.strftime('%H:%M:%S')}] {symbol}: "
                            f"{signal_value}"
                        )
                    except Exception as e:  # noqa: BLE001 — never kill the loop
                        logger.exception(
                            "mtf_aggregator failed for %s: %s", symbol, e
                        )
                        self.stderr.write(
                            self.style.ERROR(
                                f"Failed to aggregate {symbol}: {e}"
                            )
                        )
                time.sleep(refresh_seconds)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("KeyboardInterrupt — stopping…"))
        finally:
            self.stdout.write(self.style.SUCCESS("mtf_aggregator stopped."))