"""
Backfill 3 months of 1-minute OHLC candles from the active broker provider
into ClickHouse, then warm the analysis HotCache.

Pipeline (improve.md §2):
  symbols -> fetch ticks via provider.get_ticks_range (windowed, paged)
          -> aggregate ticks to 1M OHLC candles (bounded memory)
          -> validate (monotonic, OHLC, dedup, gap detection)
          -> normalize (UTC, epoch-ms -> DateTime)
          -> persist via insert_candle_batch (idempotent: skip existing)
          -> warm HotCache for the fetched 1M range
          -> per-symbol summary

The backward-paging + aggregation + idempotent-insert core now lives in
``forex.analysis.backfill.backfill_1m_range`` so this command and the startup
``ensure_continuity`` gap-filler share one implementation (no duplication).

Coded strictly against the TradingProvider interface (get_ticks_range returns
list[MarketTick]); never imports a provider class. Ticks are aggregated into
1-minute candles using the bid price (matching get_ohlc_bars / CandleAggregator
conventions). Bounded memory: at most one in-flight minute bucket plus a
flush batch are held in RAM at once.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from forex.broker.errors import BrokerError
from forex.broker.factory import TradingProviderFactory
from forex.analysis.startup import preload_history
from forex.analysis.cache import HotCache
from forex.analysis.requirements import get_requirements

logger = logging.getLogger("forex.management.backfill_history")


class Command(BaseCommand):
    help = (
        "Backfill ~N days of 1-minute historical candles from the active "
        "provider into ClickHouse (idempotent) and warm the analysis cache."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbols", nargs="*",
            help="Symbols to backfill. Default: CH `symbols` table, then "
                 "TRADING_SYMBOLS / PEPPERSTONE_SYMBOLS env.",
        )
        parser.add_argument(
            "--days", type=int, default=90,
            help="Lookback window in days (default 90).",
        )
        parser.add_argument(
            "--limit", type=int, default=1000,
            help="Page size for get_ticks_range (default 1000).",
        )
        parser.add_argument(
            "--batch", type=int, default=5000,
            help="Candles flushed to ClickHouse per batch (default 5000).",
        )
        parser.add_argument(
            "--no-warm-cache", action="store_true",
            help="Skip warming the HotCache after persisting.",
        )
        parser.add_argument(
            "--provider", default=None,
            help="Provider name to use (default: active provider).",
        )

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        symbols = opts["symbols"] or self._resolve_symbols()
        if not symbols:
            self.stderr.write(
                "No symbols resolved. Pass --symbols or set TRADING_SYMBOLS / "
                "populate the ClickHouse `symbols` table."
            )
            return

        days = max(1, opts["days"])
        page = max(1, min(int(opts["limit"]), 10000))
        batch = max(1, int(opts["batch"]))
        warm = not opts["no_warm_cache"]
        provider_name = opts["provider"]

        # One ClickHouse client for the whole run.
        try:
            from forex.clickhouse.connection import get_clickhouse_client
            client = get_clickhouse_client()
        except Exception as exc:  # noqa: BLE001 — connection is mandatory
            self.stderr.write(f"[FATAL] ClickHouse connection failed: {exc}")
            return

        # Active provider (interface only — never import a provider class).
        try:
            provider = (
                TradingProviderFactory.get_active()
                if not provider_name
                else TradingProviderFactory.create(provider_name)
            )
        except BrokerError as exc:
            self.stderr.write(f"[FATAL] Provider unavailable: {exc}")
            return

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        start_epoch = int(start.timestamp())
        end_epoch = int(end.timestamp())

        self.stdout.write(
            f"Backfill window: {_iso(start_epoch)} .. {_iso(end_epoch)} "
            f"({days}d) for {len(symbols)} symbol(s)."
        )

        for symbol in symbols:
            try:
                summary = self._backfill_symbol(
                    client, provider, symbol, start_epoch, end_epoch, page, batch
                )
            except Exception as exc:  # noqa: BLE001 — one symbol must not kill the run
                logger.exception("Backfill aborted for %s: %s", symbol, exc)
                self.stderr.write(f"[WARN] {symbol}: {exc} — skipped")
                summary = {"error": str(exc)}
            self._print_summary(symbol, summary)

        if warm:
            try:
                self.stdout.write("Warming analysis HotCache...")
                preload_history(client, HotCache(), symbols, get_requirements())
            except Exception as exc:  # noqa: BLE001 — cache warm is best-effort
                logger.warning("Cache warm failed: %s", exc)
                self.stderr.write(f"[WARN] cache warm failed: {exc}")

        self.stdout.write(self.style.SUCCESS("Backfill run complete."))

    # ------------------------------------------------------------------
    def _resolve_symbols(self) -> list[str]:
        """CH `symbols` table first, then TRADING_SYMBOLS / PEPPERSTONE_SYMBOLS."""
        try:
            from forex.clickhouse.connection import get_clickhouse_client
            client = get_clickhouse_client()
            rows = client.query(
                "SELECT DISTINCT symbol FROM symbols WHERE symbol != ''"
            ).result_set
            syms = [str(r[0]).upper() for r in rows if r and r[0]]
            if syms:
                return syms
        except Exception as exc:  # noqa: BLE001 — fall through to env
            logger.debug("CH symbols read failed: %s", exc)
        import os
        raw = os.environ.get("TRADING_SYMBOLS") or os.environ.get(
            "PEPPERSTONE_SYMBOLS", ""
        )
        return [s.strip().upper() for s in raw.split(",") if s.strip()]

    # ------------------------------------------------------------------
    def _backfill_symbol(self, client, provider, symbol, start_epoch, end_epoch,
                         page, batch) -> dict:
        """Backfill one symbol's 1M candles over [start_epoch, end_epoch].

        Delegates to the shared core in ``forex.analysis.backfill`` so the
        startup continuity check uses the exact same logic (no duplication).
        """
        from forex.analysis.backfill import backfill_1m_range

        return backfill_1m_range(
            client, provider, symbol, start_epoch, end_epoch, page=page, batch=batch
        )

    # ------------------------------------------------------------------
    def _print_summary(self, symbol: str, s: dict) -> None:
        if "error" in s:
            self.stdout.write(f"=== {symbol} ===  ERROR: {s['error']}")
            return
        self.stdout.write(
            f"=== {symbol} ===\n"
            f"  fetched ticks      : {s['fetched']}\n"
            f"  inserted candles   : {s['inserted']}\n"
            f"  skipped duplicates : {s['skipped_duplicates']}\n"
            f"  invalid (dropped)  : {s['invalid']}\n"
            f"  gaps found         : {s['gaps']}\n"
            f"  oldest ts          : {_iso(s['oldest']) if s['oldest'] else 'n/a'}\n"
            f"  newest ts          : {_iso(s['newest']) if s['newest'] else 'n/a'}"
        )


def _iso(epoch_s: int | None) -> str:
    if epoch_s is None:
        return "n/a"
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
