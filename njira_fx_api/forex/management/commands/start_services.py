"""
start_services — tick engine in one process.

Usage:
    python manage.py start_services
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading

from django.core.management.base import BaseCommand

logger = logging.getLogger("forex.start_services")


class Command(BaseCommand):
    help = "Start tick engine (subprocess)."

    def handle(self, *args, **options):
        logging.basicConfig(
            level=getattr(logging, os.environ.get("TICKS_LOG_LEVEL", "INFO").upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        shutdown = threading.Event()

        def _on_signal(signum, _frame):
            print(f"\nSignal {signum} — shutting down…")
            shutdown.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        # Tick engine (subprocess — it blocks)
        tick_proc = subprocess.Popen([sys.executable, "manage.py", "ticks_engine"])

        # Block until shutdown
        while not shutdown.is_set():
            shutdown.wait(1.0)

        if tick_proc.poll() is None:
            tick_proc.terminate()
            try:
                tick_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tick_proc.kill()
