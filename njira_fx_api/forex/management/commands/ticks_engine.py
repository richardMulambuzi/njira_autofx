"""
ticks_engine — provider-independent tick streaming into ClickHouse.

Runs ``python manage.py ticks_engine``. The active provider is selected via
``TRADING_PROVIDER`` (default ``mock``: ``mock`` | ``pepperstone``)
and every tick is batched into ClickHouse
(``forex.clickhouse.schema.insert_ticks_batch``).

Symbols come from ``TRADING_SYMBOLS`` (comma-separated, default ``EURUSD``),
falling back to the legacy ``PEPPERSTONE_SYMBOLS`` for backward compatibility.

Provider paths:

* ``pepperstone`` — the legacy ``forex.pepperstone.ticks_engine.TicksEngine``
  is used unchanged (it emits legacy 6-tuples, which the ClickHouse schema
  helper accepts with ``provider`` defaulting to ``"pepperstone"``).
  Credentials are validated up front so the command fails fast with a clear
  message when any are missing (non-zero exit).
* ``mock`` — the active provider is resolved through
  ``TradingProviderFactory.get_active()`` and ``subscribe_to_market_data()``
  streams :class:`~forex.broker.models.MarketTick` objects into a batched
  ClickHouse sink. Ticks are normalized to the schema helper's preferred
  provider-aware 7-tuple shape ``(provider, symbol, bid, ask, spread, digits,
  timestamp)`` — the provider name comes from the tick itself, so ticks from
   the mock provider is stored under its own ``provider``
   values in the ticks table.

Environment variables:

    TRADING_PROVIDER         mock | pepperstone. Default: "mock"
    TRADING_SYMBOLS          Comma-separated symbols. Default: "EURUSD"
                             (legacy PEPPERSTONE_SYMBOLS used as fallback).
    TICKS_BATCH_SIZE         Rows per ClickHouse batch. Default: 100
    TICKS_FLUSH_SECONDS      Max seconds a partial batch may sit. Default: 1
    TICKS_INGEST_URL         Optional HTTP fallback sink (pepperstone only).
    TICKS_LOG_LEVEL          Optional logging level. Default: INFO

Pepperstone credentials (required only when ``TRADING_PROVIDER=pepperstone``):

    PEPPERSTONE_CLIENT_ID / PEPPERSTONE_CLIENT_SECRET / PEPPERSTONE_ACCESS_TOKEN

SIGINT/SIGTERM flush the pending buffer and exit 0.
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import threading
import time

from django.core.management.base import BaseCommand, CommandError

from forex.broker.base import TradingProvider
from forex.broker.config import TradingConfig
from forex.broker.errors import BrokerError
from forex.broker.factory import TradingProviderFactory
from forex.broker.models import MarketTick, ProviderName

logger = logging.getLogger("forex.ticks_engine")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_symbols() -> list[str]:
    """Provider-independent symbol list.

    Reads ``TRADING_SYMBOLS`` (comma-separated, default ``EURUSD``) and falls
    back to the legacy ``PEPPERSTONE_SYMBOLS`` variable for backward
    compatibility.
    """
    raw = os.environ.get("TRADING_SYMBOLS") or os.environ.get(
        "PEPPERSTONE_SYMBOLS", "EURUSD"
    )
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _read_batch_size() -> int:
    """Read ``TICKS_BATCH_SIZE`` (default 100, minimum 1)."""
    try:
        return max(1, int(os.environ.get("TICKS_BATCH_SIZE", "100")))
    except ValueError:
        return 100


def _read_flush_seconds() -> float:
    """Read ``TICKS_FLUSH_SECONDS`` (default 1.0, minimum 0.1)."""
    try:
        return max(0.1, float(os.environ.get("TICKS_FLUSH_SECONDS", "1")))
    except ValueError:
        return 1.0


def _estimate_digits(tick: MarketTick) -> int:
    """Estimate the tick's digit precision for the ClickHouse ``digits`` column.

    The provider contract does not expose digit counts, so the worker derives
    them from the quote's decimal precision (clamped to the ingest schema's
    1..8 range).
    """
    value = tick.last or tick.bid or tick.ask or 0.0
    text = f"{value:.10f}".rstrip("0")
    digits = len(text.partition(".")[2]) if "." in text else 0
    return max(1, min(digits, 8))


def _tick_to_row(tick: MarketTick) -> tuple:
    """Normalize a :class:`MarketTick` into the ClickHouse 7-tuple row shape.

    ``insert_ticks_batch`` prefers the provider-aware shape
    ``(provider, symbol, bid, ask, spread, digits, timestamp)``; the provider
    name is taken from the tick itself so market data from different brokers
    can coexist in one table and be filtered independently.
    """
    spread = round(max(0.0, tick.ask - tick.bid), 8)
    return (
        tick.provider,
        tick.symbol,
        tick.bid,
        tick.ask,
        spread,
        _estimate_digits(tick),
        tick.timestamp,
    )


class _StreamingTickSink:
    """Batched ClickHouse sink fed by a provider market-data subscription.

    Ticks arrive on the provider's subscription thread and are queued; one
    consumer thread flushes them in batches of ``batch_size`` rows or every
    ``flush_seconds``, mirroring the legacy Pepperstone engine's batching
    behaviour. ClickHouse is best-effort: when the client cannot be created
    or an insert fails, ticks are logged and dropped — the stream never
    crashes over storage problems.
    """

    def __init__(self, batch_size: int = 100, flush_seconds: float = 1.0) -> None:
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._shutdown = threading.Event()
        self._client = self._setup_clickhouse()

    @staticmethod
    def _setup_clickhouse():
        """Create the ClickHouse client + schema (non-fatal)."""
        try:
            from forex.clickhouse.connection import get_clickhouse_client
            from forex.clickhouse.schema import ensure_schema

            client = get_clickhouse_client()
            ensure_schema(client)
            logger.info("ClickHouse sink ready")
            return client
        except Exception as exc:  # noqa: BLE001 — ticks must survive CH outages
            logger.warning(
                "ClickHouse sink unavailable (ticks will be dropped): %s", exc
            )
            return None

    def callback(self, tick: MarketTick) -> None:
        """Provider subscription callback: normalize and enqueue one tick."""
        self._queue.put(_tick_to_row(tick))

    def start(self) -> threading.Thread:
        """Start the consumer thread and return it."""
        thread = threading.Thread(
            target=self._consumer_loop,
            name="ticks-sink-consumer",
            daemon=True,
        )
        thread.start()
        return thread

    def _consumer_loop(self) -> None:
        batch: list = []
        last_flush = time.monotonic()
        while not self._shutdown.is_set():
            try:
                item = self._queue.get(timeout=max(0.05, self.flush_seconds))
                batch.append(item)
            except queue.Empty:
                pass
            now = time.monotonic()
            if len(batch) >= self.batch_size or (
                batch and (now - last_flush) >= self.flush_seconds
            ):
                self._flush(batch)
                batch = []
                last_flush = now
        if batch:
            self._flush(batch)

    def _flush(self, rows: list) -> None:
        if not rows or self._client is None:
            return
        try:
            from forex.clickhouse.schema import insert_ticks_batch

            insert_ticks_batch(self._client, rows)
        except Exception as exc:  # noqa: BLE001 — never drop the stream
            logger.error(
                "ClickHouse insert failed for %d tick(s): %s", len(rows), exc
            )

    def drain(self) -> None:
        """Flush any ticks queued after the consumer loop stopped."""
        rows: list = []
        while True:
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if rows:
            self._flush(rows)

    def stop(self) -> None:
        """Signal the consumer loop to stop after its current cycle."""
        self._shutdown.set()


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = (
        "Stream spot ticks from the active trading provider "
        "(TRADING_PROVIDER: mock|pepperstone) into ClickHouse "
        "(TRADING_SYMBOLS, TICKS_BATCH_SIZE, TICKS_FLUSH_SECONDS, "
        "TICKS_INGEST_URL)."
    )

    def handle(self, *args: object, **options: object) -> None:
        level = os.environ.get("TICKS_LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        config = TradingConfig.from_env()
        provider_name = config.active_provider
        symbols = _load_symbols()

        if provider_name == ProviderName.PEPPERSTONE.value:
            self._run_pepperstone(symbols)
        elif provider_name == ProviderName.MOCK.value:
            self._run_factory_provider(config, provider_name, symbols)
        else:
            raise CommandError(
                f"ticks_engine: unsupported TRADING_PROVIDER={provider_name!r} "
                "(expected mock or pepperstone)"
            )

    # ------------------------------------------------------------------
    # Pepperstone path (legacy engine, fail-fast credentials)
    # ------------------------------------------------------------------
    def _run_pepperstone(self, symbols: list[str]) -> None:
        try:
            # Imported lazily so mock deployments never need the
            # ctrader SDK installed.
            from forex.pepperstone.client import get_credentials
            from forex.pepperstone.ticks_engine import TicksEngine, load_settings

            credentials = get_credentials()
            settings = load_settings()
            # Provider-independent symbol source (TRADING_SYMBOLS / legacy
            # fallback) overrides the engine's own PEPPERSTONE_SYMBOLS read.
            settings["symbols"] = symbols
            engine = TicksEngine(**settings, credentials=credentials)
        except (RuntimeError, ValueError) as exc:
            # Fail fast with a clear message — never a traceback.
            raise CommandError(f"ticks_engine: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"ticks_engine starting (provider=pepperstone): "
                f"symbols={engine.symbols} batch_size={engine.batch_size} "
                f"flush_seconds={engine.flush_seconds}"
            )
        )

        exit_code = engine.run()
        if exit_code != 0:
            self.stderr.write(
                self.style.ERROR(f"ticks_engine exited with code {exit_code}")
            )
            sys.exit(exit_code)
        self.stdout.write(self.style.SUCCESS("ticks_engine stopped."))

    # ------------------------------------------------------------------
    # Factory path (mock)
    # ------------------------------------------------------------------
    def _run_factory_provider(
        self, config: TradingConfig, provider_name: str, symbols: list[str]
    ) -> None:
        sink = _StreamingTickSink(
            batch_size=_read_batch_size(),
            flush_seconds=_read_flush_seconds(),
        )
        consumer = sink.start()

        shutdown = threading.Event()

        def _on_signal(signum: int, frame: object) -> None:  # noqa: ARG001
            self.stdout.write(
                self.style.WARNING(
                    f"Received signal {signum} — flushing and shutting down…"
                )
            )
            shutdown.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        provider: TradingProvider | None = None
        try:
            try:
                provider = TradingProviderFactory.get_active(config=config)
                subscription_id = provider.subscribe_to_market_data(
                    symbols, sink.callback
                )
            except BrokerError as exc:
                # e.g. mock auth failure or an unknown symbol — clear message,
                # non-zero exit, never a traceback.
                raise CommandError(
                    f"ticks_engine ({provider_name}): {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 — clear startup failures
                raise CommandError(
                    f"ticks_engine ({provider_name}): failed to start "
                    f"subscription: {exc}"
                ) from exc

            self.stdout.write(
                self.style.SUCCESS(
                    f"ticks_engine starting (provider={provider_name}): "
                    f"symbols={symbols} batch_size={sink.batch_size} "
                    f"flush_seconds={sink.flush_seconds} "
                    f"subscription={subscription_id}"
                )
            )

            while not shutdown.is_set():
                shutdown.wait(1.0)
        finally:
            if provider is not None:
                try:
                    provider.disconnect()
                except Exception:  # noqa: BLE001 — shutdown must never raise
                    logger.exception(
                        "Error disconnecting provider %s during shutdown",
                        provider_name,
                    )
            sink.stop()
            sink.drain()
            consumer.join(timeout=5)

        self.stdout.write(self.style.SUCCESS("ticks_engine stopped."))
