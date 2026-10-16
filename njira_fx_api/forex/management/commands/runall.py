"""
runall — start everything in one process.

Starts the Django HTTP server and the tick/streaming engine.
One Ctrl+C shuts them all down gracefully.

Usage:
    python manage.py runall                 # default port 8000
    python manage.py runall --port 9090     # custom port

Environment variables:
    RUNALL_PORT             HTTP server port. Default: 8000
    TICKS_LOG_LEVEL         Logging level. Default: INFO
"""
from __future__ import annotations

import logging
import os
import signal
import threading

from django.core.management.base import BaseCommand

logger = logging.getLogger("forex.runall")


class Command(BaseCommand):
    help = "Start Django server + tick engine in one process."

    def add_arguments(self, parser):
        parser.add_argument(
            "--port", type=int, default=None,
            help="HTTP server port (default: RUNALL_PORT env or 8000)",
        )

    def handle(self, *args, **options):
        logging.basicConfig(
            level=getattr(logging, os.environ.get("TICKS_LOG_LEVEL", "INFO").upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        port = options["port"] or int(os.environ.get("RUNALL_PORT", "8000"))

        shutdown = threading.Event()

        def _on_signal(signum, _frame):
            self.stdout.write(self.style.WARNING(f"\nSignal {signum} — shutting down all services…"))
            shutdown.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        # ── 1. Django HTTP server (daemon thread) ──────────────────────
        def _run_server():
            from django.core.management import call_command
            try:
                call_command("runserver", f"0.0.0.0:{port}", "--noreload", "--no-color", verbosity=0)
            except Exception as exc:
                if not shutdown.is_set():
                    logger.error("Django server died: %s", exc)

        server_thread = threading.Thread(target=_run_server, name="django-server", daemon=True)
        server_thread.start()
        self.stdout.write(self.style.SUCCESS(f"  Django server   → http://0.0.0.0:{port}"))

        # ── 2. Tick engine (main thread — blocking) ────────────────────
        self.stdout.write(self.style.SUCCESS("  Tick engine     → starting…"))
        self.stdout.write("")

        # Import and run the ticks engine in the main thread so signal
        # handling (SIGINT/SIGTERM) works correctly with Twisted's reactor.
        try:
            from forex.management.commands.ticks_engine import Command as TicksCommand
            ticks_cmd = TicksCommand()
            ticks_cmd.stdout = self.stdout
            ticks_cmd.stderr = self.stderr
            ticks_cmd.handle()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            if not shutdown.is_set():
                logger.error("Ticks engine died: %s", exc)

        # ── Shutdown ───────────────────────────────────────────────────
        shutdown.set()
        self.stdout.write(self.style.WARNING("All services stopped."))