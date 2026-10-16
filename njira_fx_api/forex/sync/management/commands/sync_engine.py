"""
sync_engine — run the tick synchronization engine from the CLI.

    python manage.py sync_engine [--symbols frxEURUSD,frxGBPUSD] [--days 60]
                                 [--force]

Starts the engine (spec §5.3): cold-start backfill (SYNCING_HISTORY) then
live subscription (LIVE). Prints progress events and periodic stats; Ctrl-C
(SIGINT/SIGTERM) shuts down gracefully (spec §9.4 e2e smoke).

Phase 0 (spec §10): when ``SYNC_ENGINE_ENABLED=false`` the command prints a
notice and exits 0 (the package is dead code until the flag flips). Pass
``--force`` to run anyway (used by the e2e smoke with the mock provider).

Symbols may be supplied with the Deriv-era ``frx`` prefix
(``frxEURUSD`` → ``EURUSD``). Stripping that prefix is a LEGACY-COMPAT
normalization only: stored settings (``bot_settings``), UI selectors, and
``analysis_module/constants.py`` still key symbols with the ``frx`` prefix, so
the engine converts them to plain provider symbols (``EURUSD``) for the
pepperstone / mock providers. No other transformation is applied — the engine
tracks plain app symbols thereafter.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from dataclasses import replace

from django.core.management.base import BaseCommand

from forex.sync.config import SyncConfig
from forex.sync.errors import SyncError

logger = logging.getLogger("forex.sync.management.commands.sync_engine")

#: Print a progress line for these events (others are logged at debug).
_PRINT_EVENTS = frozenset(
    {
        "recovery_state_changed",
        "gap_detected",
        "provider_switched",
        "backfill_progress",
    }
)


def _normalize_symbols(raw: str) -> list[str]:
    """Parse a comma-separated symbol list into plain app symbols.

    LEGACY-COMPAT normalization: callers (CLI, stored ``bot_settings``, UI
    selectors, ``analysis_module/constants.py``) still key symbols with the
    Deriv-era ``frx`` prefix (e.g. ``frxEURUSD``). We strip that prefix so the
    engine can track plain provider symbols (``EURUSD``) for pepperstone /
    mock. Only the ``FRX`` prefix is removed; no other transformation occurs.
    """
    symbols: list[str] = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if not symbol:
            continue
        # Legacy-compat: stored settings/analysis constants still use
        # Deriv-era frx* identifiers.
        if symbol.startswith("FRX"):
            symbol = symbol[3:]
        symbols.append(symbol)
    return symbols


class Command(BaseCommand):
    help = (
        "Run the tick synchronization engine (backfill → live) for the "
        "tracked symbols (SYNC_ENGINE_ENABLED, --symbols, --days)."
    )

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(
            "--symbols",
            default="frxEURUSD",
            help="Comma-separated symbols (default: frxEURUSD).",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Backfill window days (default: SYNC_WINDOW_MAX_DAYS=60).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even when SYNC_ENGINE_ENABLED=false (Phase 0 override).",
        )

    def handle(self, *args: object, **options: object) -> None:
        symbols = _normalize_symbols(str(options["symbols"]))
        days = options["days"]
        force = bool(options["force"])

        config = SyncConfig.from_env()
        if not config.engine_enabled and not force:
            self.stdout.write(
                self.style.WARNING(
                    "Sync engine is disabled (SYNC_ENGINE_ENABLED=false, "
                    "spec §10 Phase 0). Pass --force to run anyway."
                )
            )
            return
        if force and not config.engine_enabled:
            # Phase 0 override: run anyway (e2e smoke with the mock provider).
            config = replace(config, engine_enabled=True)

        # Imported lazily so Django app-load never pulls the engine chain.
        from forex.sync import create_engine
        from forex.sync.worker import BackfillWorker

        engine = create_engine(symbols, config=config)
        # T6: the BackfillWorker owns cold-start backfill, gap recovery and
        # provider-switch re-orchestration (rate-limited, progress events,
        # cooperative cancel). engine.start() starts it; engine.stop() stops
        # it (spec §5.5/§6.4).
        worker = BackfillWorker(engine, config=config, days=days)
        engine.attach_worker(worker)
        engine.subscribe(self._on_event)

        shutdown = threading.Event()

        def _on_signal(signum: int, frame: object) -> None:  # noqa: ARG001
            self.stdout.write(
                self.style.WARNING(
                    f"Received signal {signum} — shutting down gracefully…"
                )
            )
            shutdown.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        self.stdout.write(
            self.style.SUCCESS(
                f"sync_engine starting: symbols={symbols} "
                f"days={days or config.window_max_days} "
                f"provider=active (TRADING_PROVIDER) "
                f"worker=BackfillWorker({config.backfill_rate} req/s)"
            )
        )

        try:
            engine.start()
        except SyncError as exc:
            self.stderr.write(self.style.ERROR(f"sync_engine failed to start: {exc}"))
            sys.exit(1)

        self.stdout.write(
            self.style.SUCCESS(
                f"engine state={engine.engine_state.value} — Ctrl-C to stop. "
                f"stats={engine.stats()}"
            )
        )

        try:
            while not shutdown.wait(1.0):
                pass
        finally:
            engine.stop()
            self.stdout.write(
                self.style.SUCCESS(f"sync_engine stopped. final stats={engine.stats()}")
            )

    # ------------------------------------------------------------------
    # Event printing
    # ------------------------------------------------------------------
    def _on_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind in _PRINT_EVENTS:
            self.stdout.write(f"[sync] {event}")
        elif kind == "window_advanced":
            self.stdout.write(
                f"[sync] window_advanced {event.get('symbol')}: "
                f"evicted={event.get('evicted_count')} "
                f"total={event.get('total_count')}"
            )
        elif kind == "tick_committed":
            # Throttled: one line per second is plenty for the CLI.
            now = time.monotonic()
            if getattr(self, "_last_tick_print", 0.0) + 1.0 < now:
                self._last_tick_print = now
                self.stdout.write(
                    f"[sync] tick {event.get('symbol')} "
                    f"last={event.get('last')} @ {event.get('timestamp')}"
                )