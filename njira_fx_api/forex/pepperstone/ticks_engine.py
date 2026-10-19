"""
Pepperstone / cTrader tick streaming engine.

Streams live spot events from the cTrader Open API demo endpoint, converts
them to real prices using each symbol's digit precision, computes the spread
in pips, and batches them into ClickHouse (with an optional HTTP fallback
sink). Designed to run as the ``ticks_engine`` Django management command.

Environment variables:

    PEPPERSTONE_SYMBOLS      Comma-separated symbols. Default: "EURUSD"
    TICKS_BATCH_SIZE         Rows per ClickHouse/HTTP batch. Default: 100
    TICKS_FLUSH_SECONDS      Max seconds a partial batch may sit. Default: 1
    TICKS_INGEST_URL         Optional HTTP endpoint; batches are POSTed there
                             as a JSON list (timeout 2s, errors non-fatal).
    TICKS_LOG_LEVEL          Optional logging level. Default: INFO

Reconnect behaviour: the underlying ``ClientService`` retries with exponential
backoff; after ``MAX_RECONNECT_ATTEMPTS`` consecutive failed attempts the
engine gives up, flushes and exits non-zero. SIGINT/SIGTERM flush the pending
buffer, stop the reactor and exit 0.

No secrets are ever logged — only environment variable names.
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from ctrader_open_api.protobuf import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsReq
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAPayloadType,
    ProtoOATrendbarPeriod,
)

from forex.pepperstone.client import (
    PepperstoneSession,
    compute_spread_pips,
    get_credentials,
    price_scale,
)
from forex.pepperstone.h4_window import backfill_start_ms
from forex.pepperstone.window_session import decode_trendbars
from forex.analysis.candle_engine import CandleRegistry
from forex.analysis.cache import HotCache
from forex.analysis.tick_store import TickStore
from forex.analysis.gap_detector import GapDetector
from forex.analysis.tick_buffer import TickBuffer
from forex.analysis.market_structure import MarketStructureEngine
from forex.analysis.liquidity import LiquidityEngine
from forex.analysis.order_blocks import OrderBlockEngine
from forex.analysis.fvg import FVGEngine
from forex.analysis.poc import POCEngine
from forex.analysis.microstructure import MicrostructureEngine
from forex.analysis.spread import SpreadEngine
from forex.analysis.volatility import VolatilityEngine
from forex.analysis.session import SessionEngine
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.retest import RetestEngine
from forex.analysis.context import MarketContextEngine
from forex.analysis.requirements import get_requirements
from forex.analysis.persistence_buffer import PersistenceBuffer
from forex.analysis.batch_writer import BatchWriter
from forex.analysis.metrics import METRICS
from forex.analysis.latency import measure_latency
from forex.analysis.volume_profile import VolumeProfileEngine

logger = logging.getLogger("forex.pepperstone.ticks_engine")

MAX_RECONNECT_ATTEMPTS = 5

HISTORY_PERIODS: dict[int, int] = {
    ProtoOATrendbarPeriod.M15: 900,
    ProtoOATrendbarPeriod.H1: 3600,
    ProtoOATrendbarPeriod.H4: 14400,
}
HISTORY_COUNT = 1000

# The broker rate limits a burst of history requests and answers with a fatal
# payload error, so they are spaced out instead of sent in one loop.
HISTORY_REQUEST_INTERVAL = 0.5


def _trendbar_to_candle(
    symbol: str, tf: int, bar: dict, now_ms: int
) -> Optional[dict]:
    """
    Convert one decoded cTrader trendbar into the CandleRegistry candle shape.

    Returns ``None`` for the still-forming bar: history responses include the
    open bar, and presenting it as closed would let the analyzer read a
    partial 4H rejection as if it were settled.
    """
    open_ms = int(bar["epoch"])
    if open_ms + tf * 1000 > now_ms:
        return None
    open_time = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
    return {
        "symbol": symbol,
        "timeframe": tf,
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "volume_ticks": int(bar.get("volume") or 0),
        "open_time": open_time,
        "close_time": open_time + timedelta(seconds=tf),
        "is_closed": True,
    }


# Last fully-valid (bid, ask) seen per symbol. cTrader spot events can be
# one-sided — only bid OR only ask changes and the other side arrives as 0.
# We repair the missing side from this cache so the insert path never receives
# a zero-priced row. Keyed by symbol name; populated lazily as good ticks arrive.
LAST_GOOD_SIDES: dict[str, tuple[float, float]] = {}


def _repair_one_sided_tick(
    symbol: str, bid: float, ask: float
) -> Optional[tuple[float, float]]:
    """
    Repair a one-sided cTrader spot event.

    cTrader can emit a spot event where only one side changed and the other
    arrives as 0. We merge the missing side from the last known good tick for
    the symbol. Returns the merged ``(bid, ask)`` when both sides are positive,
    or ``None`` when the tick must be skipped (no prior good side to fill from,
    or both sides are zero/unrecoverable).
    """
    if bid > 0 and ask > 0:
        LAST_GOOD_SIDES[symbol] = (bid, ask)
        return (bid, ask)

    last = LAST_GOOD_SIDES.get(symbol)
    if last is None:
        # No prior good side to fill from — nothing valid to store.
        logger.debug(
            "Skipping one-sided tick for %s (no prior good side): "
            "bid=%.6f ask=%.6f",
            symbol,
            bid,
            ask,
        )
        return None

    if bid <= 0 and ask <= 0:
        # Both sides missing — cannot reconstruct a valid row.
        logger.debug(
            "Skipping fully-zero tick for %s (both sides <= 0)", symbol
        )
        return None

    merged_bid = bid if bid > 0 else last[0]
    merged_ask = ask if ask > 0 else last[1]
    if merged_bid <= 0 or merged_ask <= 0:
        logger.debug(
            "Skipping unrecoverable tick for %s: bid=%.6f ask=%.6f",
            symbol,
            merged_bid,
            merged_ask,
        )
        return None

    LAST_GOOD_SIDES[symbol] = (merged_bid, merged_ask)
    return (merged_bid, merged_ask)


def load_settings() -> dict:
    """Read engine settings from the environment with sane defaults."""
    symbols = [
        symbol.strip().upper()
        for symbol in os.environ.get("PEPPERSTONE_SYMBOLS", "EURUSD").split(",")
        if symbol.strip()
    ]
    try:
        batch_size = max(1, int(os.environ.get("TICKS_BATCH_SIZE", "100")))
    except ValueError:
        batch_size = 100
    try:
        flush_seconds = max(0.1, float(os.environ.get("TICKS_FLUSH_SECONDS", "1")))
    except ValueError:
        flush_seconds = 1.0
    ingest_url = os.environ.get("TICKS_INGEST_URL", "").strip() or None
    return {
        "symbols": symbols,
        "batch_size": batch_size,
        "flush_seconds": flush_seconds,
        "ingest_url": ingest_url,
    }


# Warning rungs keyed on the minutes left when the 1M bar closes.  A 15M bar
# closing at :45 is scouted at :25, set up at :30, watched at :35, acted at :40.
_PRE_ALERT_LABEL = {20: "SCOUT", 15: "SETUP", 10: "WATCH", 5: "ACT"}


def _pre_alert_lead_min(close_time) -> int:
    """
    Minutes between this 1M close and the M15 close it previews; 0 = not a rung.

    :55 previews the bar that opens at the next boundary, not the one already
    forming, which is what buys a full 20 minutes of lead.
    """
    minute = getattr(close_time, "minute", -1)
    if minute < 0:
        return 0
    if minute == 55:
        return 20
    left = 15 - (minute % 15)
    return left if left in _PRE_ALERT_LABEL else 0


def _projection_map(draft, forming: dict, candles_m15: list[dict],
                    horizons=(10, 15, 20)) -> list[str]:
    """Where this setup stands at +10/+15/+20 minutes.

    Built from the setup's own geometry and the symbol's typical travel, never
    from a price forecast.  There is no directional prediction here: it answers
    how long the entry needs to be reached, whether each horizon is long enough
    to get there, and which price kills the setup.
    """
    closes = [c["close"] for c in (candles_m15 or []) if c.get("close")]
    price = forming.get("close")
    if price is None and closes:
        price = closes[-1]
    if price is None or not closes:
        return ["Projection unavailable: no recent 15M candles."]

    ranges = [
        float(c["high"]) - float(c["low"]) for c in candles_m15
        if c.get("high") is not None and c.get("low") is not None
    ]
    speed = (sum(ranges) / len(ranges) / 15.0) if ranges else 0.0
    if speed <= 0:
        return ["Projection unavailable: no recent 15M range."]

    price = float(price)
    distance = abs(float(draft.entry) - price)
    fill_eta = distance / speed
    target_eta = abs(float(draft.take_profit) - float(draft.entry)) / speed

    lines = [
        f"Typical travel: {speed:.4f} per minute "
        f"({speed * 15:.4f} per 15M bar, mean of {len(ranges)} bars)",
        f"Price now:      {price:.5f}",
        f"Fill needs:     {distance:.5f} of movement, about {fill_eta:.0f} min at this pace",
        f"Target needs:   about {target_eta:.0f} min after the fill",
        f"Invalidation:   {float(draft.stop_loss):.5f} (this price kills the setup)",
    ]
    for h in horizons:
        if distance == 0:
            lines.append(f"+{h}m   price is already at the entry")
            continue
        reach = (speed * h) / distance
        if reach >= 1:
            lines.append(f"+{h}m   entry reachable (about {fill_eta:.0f} min needed)")
        else:
            lines.append(
                f"+{h}m   entry still short (covers {reach * 100:.0f}% of the distance)"
            )
    return lines


class TicksEngine:
    """
    Long-running tick streamer: reactor in the calling thread, ClickHouse /
    HTTP flushing in a dedicated consumer thread.
    """

    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        batch_size: int = 100,
        flush_seconds: float = 1.0,
        ingest_url: Optional[str] = None,
        credentials: Optional[dict] = None,
    ) -> None:
        self.symbols = [s.upper() for s in (symbols or ["EURUSD"])]
        self.batch_size = max(1, int(batch_size))
        self.flush_seconds = max(0.1, float(flush_seconds))
        self.ingest_url = ingest_url
        self.credentials = credentials

        # Bounded persistence buffer: repaired ticks are pushed here
        # (non-blocking, overflow-drops oldest) and drained by the BatchWriter
        # daemon thread that persists them to ClickHouse. Kept separate from the
        # broadcast queue so persistence backpressure never touches realtime.
        try:
            buffer_maxlen = max(
                1, int(os.environ.get("TICKS_BUFFER_MAXLEN", "50000"))
            )
        except ValueError:
            buffer_maxlen = 50_000
        self.buffer = PersistenceBuffer(maxlen=buffer_maxlen)
        self.writer: Optional[BatchWriter] = None
        # Realtime broadcast queue: repaired ticks are pushed here (best-effort,
        # bounded so a stalled broadcaster cannot grow memory without bound)
        # and drained by a daemon broadcaster thread that publishes to Pusher.
        self.broadcast_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=2000)
        self._broadcaster_started = False
        # Candle aggregation: one CandleRegistry shared across all symbols.
        # Fed every repaired tick; emits candle close events to Pusher.
        self._candle_registry = CandleRegistry(
            timeframes=(60, 300, 900, 3600, 14400, 86400),
            on_candle_close=self._on_candle_close,
        )
        # Hot cache: in-memory closed candles for analytical access.
        self._hot_cache = HotCache(max_size=500)
        self._tick_store = TickStore.from_env()
        self._local_sink = self._tick_store.write if self._tick_store else None
        # cTrader rejects a second subscribe for the same symbol (ALREADY_SUBSCRIBED)
        # and that error is fatal, so track what this session has asked for.
        self._subscribed_ids: set[int] = set()
        # Gap detector: logs warnings on tick stream gaps.
        self._gap_detector = GapDetector(threshold_seconds=300)
        # Tick dedup + OOO buffer: prevents duplicate/out-of-order processing.
        self._tick_buffer = TickBuffer()
        # Microstructure engine: bid/ask metrics from live ticks.
        self._microstructure = MicrostructureEngine()
        # Spread engine (m.md §33): dedicated per-symbol spread analytics.
        self._spread_engine = SpreadEngine()
        # Volatility engine: candle-driven ATR / range / spread volatility.
        self._volatility = VolatilityEngine()
        # Session engine (m.md §34): UTC-aware session open/high/low/range.
        self._session = SessionEngine()
        # Volume Profile engine (improve.md): tick-level profile building.
        self._volume_profile = VolumeProfileEngine()
        from forex.analysis.volume_profile import set_global_engine
        set_global_engine(self._volume_profile)
        # Intelligence engines per timeframe (lazy-init on first candle close).
        self._structure_engines: dict[int, MarketStructureEngine] = {}
        self._liquidity_engines: dict[int, LiquidityEngine] = {}
        self._ob_engines: dict[int, OrderBlockEngine] = {}
        self._fvg_engines: dict[int, FVGEngine] = {}
        self._poc_engines: dict[int, POCEngine] = {}
        # FVG persistence throttle: symbol -> last ClickHouse persist time (s).
        self._fvg_persist_ts: dict[str, float] = {}
        # Signal engine: orchestrates all intelligence on candle close.
        self._signal_engine: SignalEngine | None = None
        # Pre-alerts already sent: dedupe key -> send time (epoch seconds).
        self._pre_alerts: dict[str, float] = {}
        # Retest engine (improve.md §34–§35): evaluates price reaction to zones.
        self._retest_engine: RetestEngine | None = None
        # Shared market context engine (improve.md §17) — one snapshot all
        # strategies consume instead of rebuilding per evaluation.
        self._context_engine: MarketContextEngine | None = None
        # Strategy mode: read from userdetails ClickHouse table.
        self._strategy_mode: str = "malysian"
        self._strategy_refresh_ts: float = 0
        # User-selected symbols: read from `symbols` ClickHouse table.
        self._user_symbols: list[str] = list(self.symbols)
        self._symbols_refresh_ts: float = 0
        # Periodic broadcast throttles (provider_status every 15s, mtf every 5s)
        self._provider_broadcast_ts: float = 0
        self._mtf_broadcast_ts: float = 0
        # Intelligence broadcast tracking: per engine, last event count broadcast.
        # Only broadcast when new events are available (avoids re-broadcasting same events).
        self._structure_broadcast_count: dict[int, int] = {}
        self._liquidity_broadcast_count: dict[int, int] = {}
        self._ob_broadcast_count: dict[int, int] = {}
        self._fvg_broadcast_count: dict[int, int] = {}
        self.shutdown_event = threading.Event()
        self.exit_code = 0

        self.session: Optional[PepperstoneSession] = None
        self._ch_client = None
        self._disconnects = 0

    @classmethod
    def from_env(cls, credentials: Optional[dict] = None) -> "TicksEngine":
        """Build an engine from environment variables (see module docstring)."""
        settings = load_settings()
        return cls(
            symbols=settings["symbols"],
            batch_size=settings["batch_size"],
            flush_seconds=settings["flush_seconds"],
            ingest_url=settings["ingest_url"],
            credentials=credentials,
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self) -> int:
        """
        Run the engine until a signal, a fatal error, or reconnect give-up.

        Returns the process exit code (0 on clean shutdown).
        """
        if self.credentials is None:
            try:
                self.credentials = get_credentials()
            except RuntimeError as exc:
                logger.error("%s", exc)
                return 1

        self._setup_clickhouse()  # non-fatal

        # Phase 3 continuity (improve.md §48): backfill any gap between the last
        # persisted 1M candle and now so the live stream continues seamlessly.
        # Best-effort — never blocks startup on a ClickHouse / provider hiccup.
        self._ensure_startup_continuity()

        # BatchWriter daemon thread: drains the persistence buffer and flushes
        # to ClickHouse (with optional HTTP fallback). Never blocks the stream.
        self.writer = BatchWriter(
            self.buffer,
            self._ch_client,
            batch_size=self.batch_size,
            flush_interval=self.flush_seconds,
            ingest_url=self.ingest_url,
            local_sink=self._local_sink,
        )
        self.writer.start()
        self._start_broadcaster()

        def _on_signal(signum: int, frame: object) -> None:  # noqa: ARG001
            logger.info("Received signal %d — flushing and shutting down", signum)
            self.shutdown_event.set()
            from twisted.internet import reactor

            try:
                reactor.callFromThread(reactor.stop)
            except Exception:  # noqa: BLE001 — best-effort stop
                pass

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        self.session = PepperstoneSession(
            self.credentials,
            symbols=self.symbols,
            on_ready=self._on_ready,
            on_message=self._on_message,
            on_error=self._on_error,
            on_disconnected=self._on_disconnected,
            retry_policy=self._retry_policy,
        )
        self.session.start()

        from twisted.internet import reactor

        logger.info(
            "ticks_engine started: symbols=%s batch_size=%d flush=%.1fs "
            "ingest_url=%s",
            self.symbols,
            self.batch_size,
            self.flush_seconds,
            self.ingest_url or "none",
        )
        reactor.run(installSignalHandlers=False)

        logger.info("Reactor stopped — draining remaining ticks")
        self.shutdown_event.set()
        # Stop the writer (it drains + flushes whatever is left in the buffer),
        # then flush any stragglers returned by the buffer as a safety net.
        if self.writer is not None:
            self.writer.stop()
        remaining = self.buffer.flush_on_shutdown()
        if remaining and self.writer is not None:
            self.writer.flush_now(remaining)
        if self._tick_store is not None:
            self._tick_store.close()
        return self.exit_code

    # ------------------------------------------------------------------
    # ClickHouse sink (non-fatal)
    # ------------------------------------------------------------------
    def _setup_clickhouse(self) -> None:
        # Memory-only mode: ClickHouse disabled (fix later). BatchWriter
        # already skips persistence when client is None; Pusher + HotCache
        # keep working untouched.
        try:
            from forex.clickhouse.connection import get_clickhouse_client

            self._ch_client = get_clickhouse_client()
        except Exception as exc:  # noqa: BLE001 — ticks must survive CH outages
            logger.warning(
                "ClickHouse sink unavailable (ticks continue in memory / "
                "HTTP fallback): %s",
                exc,
            )
        self._ch_client = None

    # ------------------------------------------------------------------
    # Startup continuity (Phase 3 §48 — historical -> live gap backfill)
    # ------------------------------------------------------------------
    def _ensure_startup_continuity(self) -> None:
        """Backfill the gap between the last persisted 1M candle and now.

        Called once at startup (after ClickHouse is ready) for every user
        symbol. Uses ``ensure_continuity`` from ``forex.analysis.startup``,
        which reuses the same backfill core as the ``backfill_history`` command.
        Best-effort: any failure is logged and swallowed so startup never
        blocks on a storage or provider hiccup.
        """
        if self._ch_client is None:
            logger.warning(
                "[CONTINUITY] ClickHouse unavailable at startup — skipping gap backfill"
            )
            return
        try:
            from forex.broker.factory import TradingProviderFactory

            provider = TradingProviderFactory.get_active()
        except Exception as exc:  # noqa: BLE001 — provider is optional for backfill
            logger.warning(
                "[CONTINUITY] provider unavailable — skipping gap backfill: %s", exc
            )
            return
        for symbol in self._user_symbols:
            try:
                from forex.analysis.startup import ensure_continuity

                result = ensure_continuity(self._ch_client, provider, symbol)
                logger.info("[CONTINUITY] %s: %s", symbol, result)
            except Exception as exc:  # noqa: BLE001 — one symbol must not kill startup
                logger.warning(
                    "[CONTINUITY] gap check failed for %s: %s", symbol, exc
                )

    # ------------------------------------------------------------------
    # Pre-alert (advisory email ahead of the M15 close)
    # ------------------------------------------------------------------
    def _maybe_pre_alert(self, symbol: str, candle: dict) -> None:
        """Warn ahead of the M15 close when a Malaysian setup is shaping up.

        Driven by 1M closes 10 and 5 minutes before the 15M bar ends (a 1M bar
        closing at minute 5 or 10 past a quarter hour).  The setup is judged on
        the still-open 15M candle, so the alert lands before it confirms and
        there is time to stage a pending order.

        Best-effort: an alert failure must never break tick handling.
        """
        close_time = candle.get("close_time")
        lead_min = _pre_alert_lead_min(close_time)
        if not lead_min or symbol not in self._user_symbols:
            return
        if self._signal_engine is None:
            return
        try:
            registry = self._candle_registry.get_aggregator(symbol)
            forming = registry.get_forming(900) if registry is not None else None
            if forming is None:
                return
            drafts = self._signal_engine.preview_malaysian(symbol, forming)
        except Exception as exc:  # noqa: BLE001: advisory only
            logger.debug("Pre-alert preview failed for %s: %s", symbol, exc)
            return
        if not drafts:
            return

        # Recent closed 15M bars: the basis for the typical-travel pace the
        # projection map is built from.
        candles = self._hot_cache.get_candles(symbol, 900, limit=20)

        now = time.time()
        self._pre_alerts = {
            key: ts for key, ts in self._pre_alerts.items() if ts > now - 6 * 3600
        }
        for draft in drafts:
            analyzer = str((draft.evidence or {}).get("analyzer", "snr"))
            if self._signal_engine.has_active_setup(symbol, draft.direction, analyzer):
                continue  # already live, nothing left to prepare
            key = "|".join((
                symbol, draft.direction, analyzer,
                str(forming.get("open_time")), str(lead_min),
            ))
            if key in self._pre_alerts:
                continue
            self._pre_alerts[key] = now
            self._send_pre_alert(symbol, draft, analyzer, lead_min, forming, candles)

    def _send_pre_alert(self, symbol: str, draft, analyzer: str,
                        lead_min: int, forming: dict, candles: list[dict]) -> None:
        from django.conf import settings
        from notifications.views import send_email

        fmt = getattr(forming.get("close_time"), "strftime", None)
        closes_at = fmt("%H:%M UTC") if fmt else "the 15M close"
        projection = "\n".join(_projection_map(draft, forming, candles))
        try:
            from forex.analysis.tp_ladder import build_tp_ladder, ladder_summary
            ladder = build_tp_ladder(draft.direction, draft.entry, draft.stop_loss)
        except Exception:  # noqa: BLE001: advisory mail, never fatal
            ladder, ladder_summary = [], lambda _l: ""
        ladder_lines = "\n".join(
            f'  {r["name"]:<4} {r["price"]:<12} {r["r_multiple"]:>4.1f}R  '
            f'{r["hit_chance"] * 100:>3.0f}% reach  ({r["label"]})'
            for r in ladder
        )
        rung = _PRE_ALERT_LABEL.get(lead_min, "PRE-ALERT")
        body = (
            f"{symbol} {draft.direction} setup is FORMING on the 15M bar that "
            f"closes at {closes_at} (about {lead_min} minutes away).\n\n"
            f"RUNG           {rung} (T-{lead_min}m; SCOUT, SETUP, WATCH, ACT "
            "follow in that order)\n"
            f"ENTER AT       {closes_at}, when that bar opens\n"
            f"Analyzer:      {analyzer}\n"
            f"Entry:         {draft.entry:.5f}\n"
            f"Stop loss:     {draft.stop_loss:.5f}\n"
            f"Risk:Reward:   {draft.risk_reward:.2f}\n"
            f"Confidence:    {draft.confidence:.0f}\n\n"
            f"TAKE PROFIT LADDER\n{ladder_lines}\n"
            f"{ladder_summary(ladder)}\n\n"
            f"PROJECTION (next 20 minutes)\n{projection}\n\n"
            "This is a pre-alert, not a confirmed signal: the setup still has to "
            "survive the close. Get a pending order ready; the confirmed card "
            "follows when that bar closes.\n"
        )
        subject = f"njira pre-alert {rung}: {symbol} {draft.direction} (T-{lead_min}m)"
        for address in getattr(settings, "SIGNAL_EMAIL_TO", None) or []:
            try:
                result = send_email(address, subject, body)
                if result.get("success"):
                    logger.info("[PREALERT] %s %s T-%dm %s mailed", symbol,
                                draft.direction, lead_min, rung)
                else:
                    logger.warning("[PREALERT] %s send failed: %s", symbol,
                                   result.get("message"))
            except Exception as exc:  # noqa: BLE001: advisory only
                logger.warning("[PREALERT] %s send raised: %s", symbol, exc)

    # ------------------------------------------------------------------
    # Candle close callback (Phase 2-4: persist + cache + signal eval)
    # ------------------------------------------------------------------
    def _on_candle_close(self, candle: dict) -> None:
        """
        Called by CandleRegistry when a candle closes.

        1. Add to hot cache (synchronous, fast).
        2. Persist to ClickHouse in background (best-effort, non-fatal).
        3. Run signal engine evaluation (multi-TF confluence).
        4. Broadcast signal events if any.
        """
        symbol = candle["symbol"]
        tf = int(candle["timeframe"])

        # ── Candle persistence (hot cache + volatility + ClickHouse) ──────
        # Measured as candle_latency (m.md §51 metrics).
        with measure_latency(METRICS, "candle_latency"):
            # Hot cache — always succeeds
            self._hot_cache.add(candle)

            # ── Volatility engine (Phase 4 — m.md §32) ───────────────────────
            # Feed the closed candle + current spread (from microstructure) so the
            # volatility engine can track ATR / range / spread percentiles. Best-
            # effort: a bad candle must never break the candle-close pipeline.
            try:
                micro = self._microstructure.get_metrics(symbol)
                spread_now = micro.spread_pips if micro is not None else 0.0
                self._volatility.update(symbol, candle, spread=spread_now)
            except Exception as exc:  # noqa: BLE001 — non-fatal
                logger.debug("Volatility update failed for %s: %s", symbol, exc)

            # ── Volume profile multi-TF feed (improve.md §6) ────────────────
            # Feed closed candles into the profile engine so it builds
            # per-timeframe profiles (1M/5M/15M/1H/4H/1D) alongside the
            # tick-level developing profile. Best-effort, non-fatal.
            try:
                open_time = candle.get("open_time")
                ts_ms = int(open_time.timestamp() * 1000) if open_time else int(time.time() * 1000)
                self._volume_profile.feed_candle(
                    symbol, tf,
                    float(candle["open"]), float(candle["high"]),
                    float(candle["low"]), float(candle["close"]),
                    ts_ms,
                )
            except Exception as exc:  # noqa: BLE001 — non-fatal
                logger.debug("Volume profile candle feed failed for %s: %s", symbol, exc)

            # ClickHouse persistence — background, non-fatal
            if self._ch_client is not None:
                try:
                    from forex.clickhouse.schema import insert_candle_batch
                    insert_candle_batch(self._ch_client, [candle])
                except Exception as exc:
                    logger.debug(
                        "Candle CH insert failed for %s tf=%d: %s",
                        symbol, tf, exc,
                    )

        # Gap-fill candles are synthetic (no ticks) — cache/persist but skip analysis.
        if candle.get("gap_filled"):
            return

        # ── Signal engine evaluation (Phase 4) ──────────────────────────
        # Lazy-init intelligence engines on first candle close per TF.
        if tf not in self._structure_engines:
            self._structure_engines[tf] = MarketStructureEngine(swing_lookback=3)
            self._liquidity_engines[tf] = LiquidityEngine()
            self._ob_engines[tf] = OrderBlockEngine()
            self._fvg_engines[tf] = FVGEngine()
            self._poc_engines[tf] = POCEngine()

        # Lazy-init shared market context engine (improve.md §17) with the
        # same engine set. Holds references to the engine dicts so newly
        # added timeframes become visible automatically.
        if self._context_engine is None:
            self._context_engine = MarketContextEngine(
                structure_engines=self._structure_engines,
                liquidity_engines=self._liquidity_engines,
                ob_engines=self._ob_engines,
                fvg_engines=self._fvg_engines,
                poc_engines=self._poc_engines,
                microstructure_engine=self._microstructure,
                volatility_engine=self._volatility,
                session_engine=self._session,
                spread_engine=self._spread_engine,
                volume_profile_engine=self._volume_profile,
                cache=self._hot_cache,
            )

        # Refresh strategy mode + user symbols from ClickHouse every 60s.
        now = time.time()
        if now - self._strategy_refresh_ts > 60:
            self._strategy_refresh_ts = now
            self._strategy_mode = self._with_ict(self._read_user_strategy())
            # Also refresh user-selected symbols.
            new_syms = self._read_user_symbols()
            if new_syms != self._user_symbols:
                self._user_symbols = new_syms
                # Subscribe to any new symbols not yet covered.
                self._subscribe_new_symbols(new_syms)
                # Broadcast updated symbol list to frontend via Pusher.
                self._broadcast_symbol_list()

        # Lazy-init signal engine with all intelligence engines.
        if self._signal_engine is None:
            # Retest engine shares the hot cache for candle history.
            if self._retest_engine is None:
                self._retest_engine = RetestEngine(self._hot_cache)
            self._signal_engine = SignalEngine(
                cache=self._hot_cache,
                structure_engines=self._structure_engines,
                liquidity_engines=self._liquidity_engines,
                ob_engines=self._ob_engines,
                fvg_engines=self._fvg_engines,
                poc_engines=self._poc_engines,
                microstructure_engine=self._microstructure,
                volatility_engine=self._volatility,
                session_engine=self._session,
                spread_engine=self._spread_engine,
                volume_profile_engine=self._volume_profile,
                context_engine=self._context_engine,
                retest_engine=self._retest_engine,
                min_confidence=30,
                min_rr=1.5,
            )

        # Best-effort: refresh the shared context for this symbol on every
        # candle close so strategies consume it instead of rebuilding.
        try:
            self._context_engine.update(symbol, candle)
        except Exception as exc:  # noqa: BLE001 — non-fatal
            logger.debug("Market context update failed for %s: %s", symbol, exc)

        # Early warning: judge the still-open 15M bar 10 and 5 minutes before it
        # closes, so a pending order can be staged instead of chased. Advisory
        # only. The confirmed card still comes from the close below.
        if tf == 60:
            self._maybe_pre_alert(symbol, candle)

        # Evaluate signals for this symbol (only if user has it selected).
        if symbol not in self._user_symbols:
            return  # tick processed for candles/broadcast, but no signal analysis
        try:
            with measure_latency(METRICS, "signal_latency"):
                signals = self._signal_engine.evaluate(
                    symbol, self._strategy_mode, closed_tf=tf
                )

            # Daily rollover (1D close): invalidate the shared market context
            # so the new day rebuilds daily H/L + session context fresh on the
            # next close (improve.md §30). Best-effort, non-fatal.
            if tf == 86400 and self._context_engine is not None:
                try:
                    self._context_engine.invalidate(symbol)
                except Exception as exc:  # noqa: BLE001 — non-fatal
                    logger.debug(
                        "Context invalidate failed on daily rollover for %s: %s",
                        symbol, exc,
                    )
            for sig in signals:
                METRICS.increment("signals_generated")
                state_val = sig.state.value
                if state_val == "CONFIRMED":
                    METRICS.increment("signals_confirmed")
                elif state_val == "INVALIDATED":
                    METRICS.increment("signals_invalidated")
                elif state_val == "WAITING":
                    METRICS.increment("signals_waiting")
                logger.info(
                    "[SIGNAL] %s %s @ %.5f conf=%.0f RR=%.1f",
                    sig.symbol, sig.direction, sig.entry,
                    sig.confidence_score, sig.risk_reward_ratio,
                )
                # Broadcast signal to Pusher
                try:
                    from forex.realtime import broadcast_event
                    broadcast_event(
                        "njira-market", "signal", sig.to_dict()
                    )
                except Exception as exc:
                    logger.debug("Signal broadcast failed: %s", exc)
                # Shadow-log v1 envelope (read-only, flat path above untouched).
                try:
                    _v1 = sig.to_dict_v1()
                    logger.info(
                        "[SIGNAL_V1_SHADOW] emit id=%s key=%s status=%s",
                        _v1.get("id"), _v1.get("idempotencyKey"), _v1.get("status"),
                    )
                    logger.debug("Signal v1 payload: %s", _v1)
                except Exception as exc:
                    logger.debug("Signal v1 shadow-log failed: %s", exc)

            # Broadcast intelligence events (structure, liquidity, OB, FVG)
            # so the frontend can visualize them on the chart.
            # Only broadcast NEW events since last broadcast (avoid re-sending same events).
            try:
                from forex.realtime import broadcast_event
                # Structure events (per timeframe)
                for tf_key, tf_eng in self._structure_engines.items():
                    events = tf_eng.get_events(tf_key, n=1)
                    if events:
                        last_count = self._structure_broadcast_count.get(tf_key, 0)
                        current_count = len(tf_eng.get_events(tf_key))  # total events in buffer
                        if current_count > last_count:
                            # New events available — broadcast the latest
                            for evt in events:
                                broadcast_event("njira-market", "structure", evt)
                            self._structure_broadcast_count[tf_key] = current_count
                # Liquidity events (per timeframe)
                for tf_key, tf_eng in self._liquidity_engines.items():
                    events = tf_eng.get_events(n=1)
                    if events:
                        last_count = self._liquidity_broadcast_count.get(tf_key, 0)
                        current_count = len(tf_eng.get_events())
                        if current_count > last_count:
                            for evt in events:
                                broadcast_event("njira-market", "liquidity", evt)
                            self._liquidity_broadcast_count[tf_key] = current_count
                # Order block events (per timeframe)
                for tf_key, tf_eng in self._ob_engines.items():
                    events = tf_eng.get_events(n=1)
                    if events:
                        last_count = self._ob_broadcast_count.get(tf_key, 0)
                        current_count = len(tf_eng.get_events())
                        if current_count > last_count:
                            for evt in events:
                                broadcast_event("njira-market", "order_block", evt)
                            self._ob_broadcast_count[tf_key] = current_count
                # FVG events (per timeframe)
                for tf_key, tf_eng in self._fvg_engines.items():
                    events = tf_eng.get_events(n=1)
                    if events:
                        last_count = self._fvg_broadcast_count.get(tf_key, 0)
                        current_count = len(tf_eng.get_events())
                        if current_count > last_count:
                            for evt in events:
                                broadcast_event("njira-market", "fvg", evt)
                            self._fvg_broadcast_count[tf_key] = current_count
            except Exception as exc:
                logger.debug("Intelligence broadcast failed: %s", exc)

            # ── Broadcast live analysis snapshot (replaces HTTP /analyze/) ─
            try:
                from forex.realtime import broadcast_event
                analysis_evt = self._build_analysis_event(symbol)
                if analysis_evt:
                    broadcast_event("njira-market", "analysis", analysis_evt)
            except Exception as exc:
                logger.debug("Analysis broadcast failed: %s", exc)

            # ── Broadcast provider status (every 15s, replaces HTTP poll) ─
            now_ts = time.time()
            if now_ts - self._provider_broadcast_ts >= 15:
                self._provider_broadcast_ts = now_ts
                try:
                    from forex.realtime import broadcast_event
                    provider_evt = self._build_provider_status_event()
                    if provider_evt:
                        broadcast_event("njira-market", "provider_status", provider_evt)
                except Exception as exc:
                    logger.debug("Provider status broadcast failed: %s", exc)

            # ── Broadcast MTF status (every 5s, replaces HTTP poll) ─
            if now_ts - self._mtf_broadcast_ts >= 5:
                self._mtf_broadcast_ts = now_ts
                try:
                    from forex.realtime import broadcast_event
                    mtf_evt = self._build_mtf_status_event(symbol)
                    if mtf_evt:
                        broadcast_event("njira-market", "mtf_status", mtf_evt)
                except Exception as exc:
                    logger.debug("MTF status broadcast failed: %s", exc)

            # ── Broadcast volume profile (every 10s) ──────────────────
            if now_ts - getattr(self, '_vp_broadcast_ts', 0) >= 10:
                self._vp_broadcast_ts = now_ts
                try:
                    vp = self._volume_profile.get_profile(symbol)
                    if vp and vp.total_activity >= self._volume_profile._config.min_ticks:
                        from forex.realtime import broadcast_event
                        payload = vp.to_dict()
                        # Attach Wave 2 analytics (shape, entropy, reliability, migration)
                        from forex.analysis.volume_profile import (
                            calculate_entropy, classify_shape, calculate_reliability,
                        )
                        payload["shape"] = classify_shape(
                            vp.bins, vp.poc, vp.vah, vp.val,
                            vp.profile_high, vp.profile_low,
                        ).value
                        payload["entropy"] = round(calculate_entropy(vp.bins), 4)
                        payload["reliability"] = round(calculate_reliability(vp), 3)
                        payload["value_migration"] = self._volume_profile.get_value_migration(symbol)
                        broadcast_event("njira-market", "volume_profile", payload)
                except Exception as exc:
                    logger.debug("Volume profile broadcast failed: %s", exc)

            # ── Persist confirmed signals to ClickHouse (Phase 5) ───────
            if self._ch_client is not None and signals:
                try:
                    from forex.clickhouse.schema import insert_signal_v2
                    for sig in signals:
                        insert_signal_v2(self._ch_client, sig.to_dict())
                        # Shadow-log v1 envelope (read-only, flat persist above untouched).
                        try:
                            _v1 = sig.to_dict_v1()
                            logger.info(
                                "[SIGNAL_V1_SHADOW] persist id=%s key=%s status=%s",
                                _v1.get("id"), _v1.get("idempotencyKey"), _v1.get("status"),
                            )
                        except Exception as exc:
                            logger.debug("Signal v1 shadow-log failed: %s", exc)
                except Exception as exc:
                    logger.debug("Signal CH persist failed: %s", exc)
        except Exception as exc:
            logger.debug("Signal engine eval failed for %s: %s", symbol, exc)

        # ── Persist FVGs to ClickHouse (audit item 9 — best-effort, throttled) ──
        # FVGs are detected inside the signal engine above. Persist them so the
        # analytics layer can query historical gaps. Throttled to ~10s/symbol so
        # a 1M close storm never hammers ClickHouse. Never fatal to the pipeline.
        try:
            now = time.time()
            last = self._fvg_persist_ts.get(symbol, 0.0)
            if now - last >= 10.0:
                self._fvg_persist_ts[symbol] = now
                batch: list[dict] = []
                for fvg_eng in self._fvg_engines.values():
                    for fvg in fvg_eng.get_all():
                        if fvg.symbol == symbol:
                            batch.append(fvg.to_dict())
                if self._ch_client is not None and batch:
                    from forex.clickhouse.schema import insert_fvg_batch
                    insert_fvg_batch(self._ch_client, batch)
        except Exception as exc:  # noqa: BLE001 — non-fatal
            logger.debug("FVG CH persist failed for %s: %s", symbol, exc)

    def _strategy_allowlist(self) -> tuple[str, ...]:
        """ict_* combos stay unselectable until NJIRA_ICT_STRATEGIES whitelists them."""
        ict = tuple(
            s.strip()
            for s in os.environ.get("NJIRA_ICT_STRATEGIES", "").split(",")
            if s.strip()
        )
        return ("Moving_averages", "malysian", "both") + ict

    def _with_ict(self, base: str) -> str:
        """Add the env-whitelisted ict_* combos to the live mode string."""
        modes = ["malysian", "Moving_averages"] if base == "both" else [base]
        return ",".join(modes + [m for m in self._strategy_allowlist() if m.startswith("ict_")])

    def _read_user_strategy(self) -> str:
        """Read the user's chosen strategy from ClickHouse userdetails table.
        Returns 'malysian' (default) on any failure.

        The frontend saves strategy per-email (bot_settings/views.py writes to
        userdetails.email / userdetails.strategy). Read the strategy for the
        active user (NJIRA_ACTIVE_EMAIL) instead of the most-recently-created
        row across ALL users. Falls back to the latest row if no email is
        configured or the lookup yields nothing.
        """
        try:
            from forex.clickhouse.connection import get_clickhouse_client
            client = get_clickhouse_client()
            active_email = os.environ.get("NJIRA_ACTIVE_EMAIL")
            if active_email:
                result = client.query(
                    "SELECT strategy FROM userdetails WHERE email = %(email)s "
                    "ORDER BY created_at DESC LIMIT 1",
                    {"email": active_email},
                )
                rows = result.result_set
                if rows and rows[0][0]:
                    mode = rows[0][0]
                    if mode in self._strategy_allowlist():
                        return mode
                # No strategy for this user — fall through to global fallback.
            result = client.query(
                "SELECT strategy FROM userdetails ORDER BY created_at DESC LIMIT 1"
            )
            rows = result.result_set
            if rows and rows[0][0]:
                mode = rows[0][0]
                if mode in self._strategy_allowlist():
                    return mode
        except Exception as exc:
            logger.debug("Strategy read failed: %s", exc)
        return "malysian"

    def _read_user_symbols(self) -> list[str]:
        """Read user-selected symbols from ClickHouse `symbols` table.
        Returns env-var symbols as fallback."""
        try:
            from forex.clickhouse.connection import get_clickhouse_client
            client = get_clickhouse_client()
            # Ensure table exists
            client.command("""
                CREATE TABLE IF NOT EXISTS symbols (
                    email String, token String, symbol String, created_at DateTime
                ) ENGINE = MergeTree() ORDER BY (email);
            """)
            result = client.query(
                "SELECT DISTINCT symbol FROM symbols WHERE symbol != ''"
            )
            rows = result.result_set
            if rows:
                syms = [str(r[0]).upper() for r in rows if r[0]]
                if syms:
                    return syms
        except Exception as exc:
            logger.debug("User symbols read failed: %s", exc)
        return self.symbols  # fallback to env-var symbols

    def _subscribe_new_symbols(self, symbols: list[str]) -> None:
        """Subscribe to spot updates for any symbols not yet subscribed."""
        if not self.session or not self.session.symbols_map:
            return
        new_ids = []
        for sym in symbols:
            info = self.session.symbols_map.get(sym)
            if info and info[0] not in self._subscribed_ids:
                new_ids.append(info[0])
        if new_ids:
            try:
                self.session.subscribe_spots(new_ids)
                self._subscribed_ids.update(new_ids)
                logger.info("Subscribed to %d new symbol(s)", len(new_ids))
            except Exception as exc:
                logger.debug("New symbol subscription failed: %s", exc)

    def _broadcast_symbol_list(self) -> None:
        """Broadcast the full tradable symbol catalogue to frontend via Pusher."""
        try:
            from forex.realtime import broadcast_event
            # Get full catalogue from broker (cached in-memory).
            catalogue = self._get_broker_catalogue()
            broadcast_event(
                "njira-market", "symbols_catalogue",
                {"symbols": catalogue, "count": len(catalogue)}
            )
        except Exception as exc:
            logger.debug("Symbol list broadcast failed: %s", exc)

    def _get_broker_catalogue(self) -> list[dict]:
        """Return the full cTrader symbol list as simple dicts."""
        if not self.session or not self.session.symbols_map:
            return []
        return [
            {"symbol": name, "symbolId": sid, "digits": digits}
            for name, (sid, digits) in self.session.symbols_map.items()
        ]

    def _build_analysis_event(self, symbol: str) -> dict | None:
        """Build a live analysis snapshot from in-memory engines.

        Replaces the HTTP /api/market/analyze/ endpoint — no ClickHouse hit,
        no SMA recomputation. All data comes from engines already running in
        this process.
        """
        # Structure from the 15m engine (primary TF).
        tf_15 = 15 * 60  # seconds
        struct_eng = self._structure_engines.get(tf_15)
        bos = choch = ob_active = False
        regime = "RANGING"
        if struct_eng:
            state = struct_eng._states.get(symbol)
            if state:
                bos = bool(state.bos_events)
                choch = bool(state.choch_events)
                regime = state.regime.value if state.regime else "RANGING"
            ob_eng = self._ob_engines.get(tf_15)
            if ob_eng:
                obs = ob_eng._order_blocks.get(symbol, [])
                ob_active = any(ob.state == "active" for ob in obs)

        # Volatility from recent candle ranges in hot cache.
        candles = list(self._hot_cache.get_candles(symbol, tf_15, limit=20))
        volatility = "MEDIUM"
        if candles:
            ranges = [abs(c["high"] - c["low"]) for c in candles if c.get("high") and c.get("low")]
            if ranges:
                avg_range = sum(ranges) / len(ranges)
                last_range = ranges[-1]
                if last_range > avg_range * 1.5:
                    volatility = "HIGH"
                elif last_range < avg_range * 0.5:
                    volatility = "LOW"

        # Session context (m.md §34) — UTC-aware, no local clock.
        session = None
        try:
            sm = self._session.get_metrics(symbol)
            if sm is not None:
                session = sm.to_dict()
        except Exception as exc:
            logger.debug("Session metrics build failed: %s", exc)

        # SMA trend from cached closes.
        closes = [float(c["close"]) for c in candles if c.get("close")]
        sma20 = round(sum(closes[-20:]) / min(len(closes), 20), 5) if closes else None
        sma50 = round(sum(closes[-50:]) / min(len(closes), 50), 5) if closes else None
        trend = "flat"
        if sma20 and sma50:
            trend = "bullish" if sma20 > sma50 else "bearish"

        last_close = closes[-1] if closes else None

        # ── §45 observability fields (best-effort, never crash) ──────────────
        ctx = None
        if self._context_engine is not None:
            try:
                ctx = self._context_engine.get(symbol)
            except Exception as exc:  # noqa: BLE001 — context is optional enrichment
                logger.debug("Context state fetch failed: %s", exc)

        # Strategy reports from the signal engine (each report is a StrategyReport).
        strategy_reports: list[dict] = []
        if self._signal_engine is not None:
            try:
                strategy_reports = [
                    r.to_dict() for r in self._signal_engine.get_strategy_reports(symbol)
                ]
            except Exception as exc:  # noqa: BLE001 — never break the broadcast
                logger.debug("Strategy reports build failed: %s", exc)
                strategy_reports = []

        # Strategy agreement: count only reports that are active AND applicable.
        agreement = {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0}
        invalidation: list[str] = []
        for rep in strategy_reports:
            if rep.get("active") and rep.get("applicable"):
                agreement["total"] += 1
                direction = (rep.get("direction") or "NEUTRAL").upper()
                if direction == "BULLISH":
                    agreement["bullish"] += 1
                elif direction == "BEARISH":
                    agreement["bearish"] += 1
                else:
                    agreement["neutral"] += 1
            # First non-empty invalidations list across all reports (UI §45).
            if not invalidation:
                inv = rep.get("invalidations") or []
                if inv:
                    invalidation = list(inv)

        return {
            "symbol": symbol,
            "structure": {
                "bos": bos,
                "choch": choch,
                "order_block_active": ob_active,
            },
            "regime": regime,
            "volatility": volatility,
            "sma20": sma20,
            "sma50": sma50,
            "trend": trend,
            "last_close": last_close,
            "session": session,
            "current_price": ctx.current_price if ctx is not None else last_close,
            "day_high": ctx.daily_high if ctx is not None else None,
            "day_low": ctx.daily_low if ctx is not None else None,
            "data_freshness": ctx.data_freshness if ctx is not None else None,
            "strategy_agreement": agreement,
            "strategy_reports": strategy_reports,
            "invalidation": invalidation,
        }

    def _build_provider_status_event(self) -> dict | None:
        """Build provider health snapshot from the live session.

        Replaces the HTTP /api/provider/status/ endpoint.
        """
        try:
            from forex.broker.base import TradingProviderFactory
            from forex.broker.config import TradingConfig
            config = TradingConfig.from_env()
            provider = TradingProviderFactory.get_active(config=config)
            health = provider.health().to_dict()
            return {
                "provider": provider.name,
                "environment": provider.mode.value,
                "health": health,
                "credentials_valid": bool(
                    config.get_provider(config.active_provider) is not None
                    and config.get_provider(config.active_provider).credentials_valid
                ),
                "safety_warnings": config.safety_check(),
            }
        except Exception as exc:
            logger.debug("Provider status build failed: %s", exc)
            return None

    def _build_mtf_status_event(self, symbol: str) -> dict | None:
        """Build multi-timeframe alignment snapshot from cached data.

        Replaces the HTTP /api/mtf-status/ endpoint. Uses the hot cache
        instead of hitting ClickHouse — no DB round-trip.
        """
        try:
            from django.core.cache import cache as django_cache
            from forex.mtf_strategy import MultiTimeframeStrategy, SMA_PERIOD

            master_signal = django_cache.get(f"macro_signal_{symbol}", "HOLD")
            strategy = MultiTimeframeStrategy()

            MTF_TIMEFRAMES = {
                "1M": 1, "5M": 5, "15M": 15, "30M": 30,
                "1H": 60, "4H": 240, "1D": 1440,
            }

            timeframes = {}
            for label, minutes in MTF_TIMEFRAMES.items():
                tf_seconds = minutes * 60
                candles = list(self._hot_cache.get_candles(symbol, tf_seconds, limit=30))
                bars = [{"time": c.get("open_time", 0), "close": c.get("close", 0)} for c in candles]
                sma = strategy.calculate_simple_moving_average(bars, SMA_PERIOD)
                current_close = bars[-1]["close"] if bars else 0
                if master_signal == "BUY":
                    aligned = sma is not None and current_close > sma
                elif master_signal == "SELL":
                    aligned = sma is not None and current_close < sma
                else:
                    aligned = False
                timeframes[label] = {
                    "sma": sma,
                    "current_value": current_close,
                    "aligned": aligned,
                    "chart_history": bars,
                }

            return {
                "status": "success",
                "symbol": symbol,
                "masterSignal": master_signal,
                "timeframes": timeframes,
            }
        except Exception as exc:
            logger.debug("MTF status build failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Session callbacks
    # ------------------------------------------------------------------
    def _on_ready(self, symbols_map: dict) -> None:
        wanted = set(self.symbols)
        symbol_ids = [
            info[0] for name, info in symbols_map.items() if name in wanted
        ]
        missing = sorted(wanted - set(symbols_map))
        if missing:
            logger.warning(
                "Configured symbols not found in the account: %s", missing
            )
        if not symbol_ids:
            self._on_error(
                "No configured symbols are available in the account — "
                "cannot subscribe to spot updates."
            )
            return
        self.session.subscribe_spots(symbol_ids)
        self._subscribed_ids = set(symbol_ids)
        self._request_history(symbol_ids)
        if self._disconnects > 0:
            METRICS.increment("reconnect_count")
        self._disconnects = 0  # a fresh handshake means the link is healthy

        # ── Startup preload (Phase 2.6) ──────────────────────────────────
        # Load historical candles from ClickHouse into hot cache so the
        # analytical engines can function immediately.
        if self._ch_client is not None:
            try:
                from forex.analysis.startup import preload_history
                from forex.analysis.requirements import get_requirements

                stats = preload_history(
                    self._ch_client, self._hot_cache,
                    list(wanted & set(symbols_map.keys())),
                    get_requirements(),
                )
                for sym, tfs in stats.items():
                    loaded = ", ".join(f"tf={tf}s:{n}" for tf, n in tfs.items())
                    logger.info("[STARTUP] Preloaded %s: %s", sym, loaded)
            except Exception as exc:
                logger.warning("Startup preload failed (live candles will fill): %s", exc)

        # ── Load user-selected symbols + broadcast catalogue ────────────
        self._user_symbols = self._read_user_symbols()
        self._subscribe_new_symbols(self._user_symbols)
        self._broadcast_symbol_list()

    def _request_history(self, symbol_ids: list[int]) -> None:
        """
        Ask the broker for real trendbars on the open session.

        The engine holds one authenticated connection, so history costs no
        extra handshake and no extra process. The window is clamped to
        ``HISTORY_COUNT`` bars per timeframe; wider windows would silently
        return the oldest slice of the range rather than the newest. Requests
        are spread ``HISTORY_REQUEST_INTERVAL`` seconds apart because the
        broker rate limits a burst.
        """
        from twisted.internet import reactor

        now_ms = int(time.time() * 1000)
        floor_ms = backfill_start_ms(now_ms)
        delay = 0.0
        for symbol_id in symbol_ids:
            for period, tf in HISTORY_PERIODS.items():
                reactor.callLater(
                    delay,
                    self._send_history_request,
                    symbol_id,
                    period,
                    tf,
                    now_ms,
                    floor_ms,
                )
                delay += HISTORY_REQUEST_INTERVAL

    def _send_history_request(
        self, symbol_id: int, period: int, tf: int, now_ms: int, floor_ms: int
    ) -> None:
        """Send one trendbar request. Runs on the reactor thread."""
        span_ms = HISTORY_COUNT * tf * 1000
        self.session.client.send(
            ProtoOAGetTrendbarsReq(
                ctidTraderAccountId=self.credentials["account_id"],
                symbolId=symbol_id,
                fromTimestamp=max(floor_ms, now_ms - span_ms),
                toTimestamp=now_ms,
                period=period,
                count=HISTORY_COUNT,
            )
        )

    def _load_history(self, message: object) -> None:
        """Replace the hot cache entry for one timeframe with broker history."""
        try:
            response = Protobuf.extract(message)
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill us
            logger.warning("Could not parse trendbar response: %s", exc)
            return
        tf = HISTORY_PERIODS.get(response.period)
        if tf is None:
            return
        info = self.session.symbols_by_id.get(int(response.symbolId))
        if info is None:
            return
        symbol_name, digits = info
        now_ms = int(time.time() * 1000)
        bars = decode_trendbars(response.trendbar, symbol_name, int(digits))
        candles = [
            candle
            for candle in (
                _trendbar_to_candle(symbol_name, tf, bar, now_ms) for bar in bars
            )
            if candle is not None
        ]
        if not candles:
            logger.warning("[HISTORY] %s tf=%ds: no closed bars returned", symbol_name, tf)
            return
        self._hot_cache.set_candles(symbol_name, tf, candles)
        logger.info(
            "[HISTORY] %s tf=%ds: loaded %d candles (newest=%s)",
            symbol_name,
            tf,
            len(candles),
            candles[-1]["open_time"].strftime("%Y-%m-%d %H:%M UTC"),
        )

    def _on_message(self, client: object, message: object) -> None:
        if message.payloadType == ProtoOAPayloadType.PROTO_OA_GET_TRENDBARS_RES:
            self._load_history(message)
            return
        if message.payloadType != ProtoOAPayloadType.PROTO_OA_SPOT_EVENT:
            return
        try:
            event = Protobuf.extract(message)
        except Exception as exc:  # noqa: BLE001 — one bad tick must not kill us
            logger.warning("Could not parse spot event: %s", exc)
            return

        info = self.session.symbols_by_id.get(event.symbolId)
        if info is None:
            return
        symbol_name, digits = info

        bid = event.bid / price_scale(symbol_name, digits, event.bid)
        ask = event.ask / price_scale(symbol_name, digits, event.ask)

        # cTrader spot events can be one-sided: only bid OR only ask changes
        # and the other side arrives as 0. Repair from the last known good
        # side for this symbol; skip the tick entirely if it cannot be made
        # whole (no prior side, or both sides zero).
        METRICS.increment("ticks_received")
        repaired = _repair_one_sided_tick(symbol_name, bid, ask)
        if repaired is None:
            METRICS.increment("ticks_dropped")
            return
        bid, ask = repaired
        METRICS.increment("ticks_processed")
        # cTrader omits the timestamp on most spot events; without this the
        # dedup/staleness buffer discards the whole feed as duplicate zero-timestamp ticks.
        timestamp_ms = event.timestamp or int(time.time() * 1000)

        # ── Gap detection (Phase 2.7) ───────────────────────────────────
        self._gap_detector.check(symbol_name, timestamp_ms)

        # ── Dedup + OOO buffer (Phase 2.8) ──────────────────────────────
        deduped = self._tick_buffer.feed(
            symbol_name, bid, ask, 0.0, digits, timestamp_ms
        )
        if deduped is None:
            METRICS.increment("ticks_duplicate")
            return  # duplicate or buffered OOO tick

        spread = compute_spread_pips(bid, ask, digits)

        # ── Processing pipeline (microstructure + candle aggregation + signal eval) ──
        # Measured as processing_latency (m.md §51 metrics). Signal eval runs
        # synchronously inside candle aggregation via the _on_candle_close callback.
        with measure_latency(METRICS, "processing_latency"):
            # ── Feed microstructure (tick-by-tick, Phase 3E) ────────────────
            try:
                self._microstructure.feed(symbol_name, bid, ask, spread, timestamp_ms)
            except Exception:
                pass  # non-fatal
            # ── Feed dedicated spread engine (m.md §33) ─────────────────────
            try:
                self._spread_engine.update(symbol_name, spread)
            except Exception:
                pass  # non-fatal
            # ── Feed volume profile engine (improve.md §7) ────────────────
            try:
                self._volume_profile.feed_tick(symbol_name, bid, ask, timestamp_ms)
            except Exception:
                pass  # non-fatal
            # Spread must stay non-negative even after a merge fills a stale side.
            spread = max(spread, 0.0)

            # ── Candle aggregation (Phase 1) ────────────────────────────────
            # Feed every repaired tick to the multi-timeframe candle engine.
            # Closed candles are broadcast to Pusher immediately; the forming
            # candle is updated in-memory for analytical access.
            try:
                from forex.realtime import broadcast_event as _broadcast_event

                closed_candles = self._candle_registry.feed_tick(
                    symbol_name, bid, ask, timestamp_ms
                )
                for candle in closed_candles:
                    try:
                        _broadcast_event(
                            "njira-market",
                            "candle",
                            {
                                "symbol": candle["symbol"],
                                "timeframe": candle["timeframe"],
                                "open": candle["open"],
                                "high": candle["high"],
                                "low": candle["low"],
                                "close": candle["close"],
                                "volume_ticks": candle["volume_ticks"],
                                "open_time": candle["open_time"].isoformat()
                                if candle["open_time"]
                                else None,
                                "close_time": candle["close_time"].isoformat()
                                if candle["close_time"]
                                else None,
                                "is_closed": True,
                            },
                        )
                    except Exception:  # noqa: BLE001 — candle broadcast must not crash stream
                        logger.debug("Candle broadcast failed for %s", symbol_name)
            except Exception:  # noqa: BLE001 — candle engine must not crash tick stream
                logger.debug("Candle aggregation failed for %s", symbol_name)

            moment = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)

            # ── Session engine (m.md §34) ───────────────────────────────────
            # Feed the mid price with the UTC tick timestamp so session boundaries
            # are derived purely from broker UTC time (no local clock, no offsets).
            try:
                mid = (bid + ask) / 2.0
                self._session.update(symbol_name, mid, moment)
            except Exception:  # noqa: BLE001 — non-fatal
                pass

        logger.info(
            "[%s] %s bid=%.*f ask=%.*f spread=%.2f",
            moment.strftime("%H:%M:%S.%f")[:-3],
            symbol_name,
            digits,
            bid,
            digits,
            ask,
            spread,
        )
        with measure_latency(METRICS, "persistence_latency"):
            self.buffer.put(
                symbol_name, bid, ask, spread, digits, timestamp_ms
            )

            # Realtime: feed the Pusher broadcaster (best-effort, never blocks the
            # stream). The daemon broadcaster thread throttles per symbol and drops
            # excess so a high-frequency tick stream cannot flood Pusher.
            try:
                self.broadcast_queue.put_nowait(
                    (symbol_name, bid, ask, spread, digits, timestamp_ms)
                )
            except queue.Full:
                pass  # broadcaster is behind — drop, realtime is best-effort

    def _on_error(self, message: str) -> None:
        if "BLOCKED_PAYLOAD_TYPE" in message:
            # A rate limit is not fatal: the ClientService retries the
            # connection, and killing the engine here would stop the tick
            # stream entirely.
            logger.warning("Broker rate limited a request: %s", message)
            return
        logger.error("Fatal session error: %s", message)
        self.exit_code = 1
        self.shutdown_event.set()
        from twisted.internet import reactor

        try:
            reactor.callFromThread(reactor.stop)
        except Exception:  # noqa: BLE001 — best-effort stop
            pass

    def _on_disconnected(self, client: object, reason: object) -> None:
        # Reconnection timing is handled by the ClientService retry policy;
        # this callback only counts drops for observability.
        self._disconnects += 1
        METRICS.increment("stream_disconnects")
        logger.warning(
            "Connection lost (drop #%d) — reconnecting with backoff",
            self._disconnects,
        )

    # ------------------------------------------------------------------
    # Reconnect policy: exponential backoff, give up after N attempts
    # ------------------------------------------------------------------
    def _retry_policy(self, attempts: int) -> float:
        if attempts > MAX_RECONNECT_ATTEMPTS:
            logger.error(
                "Giving up after %d consecutive reconnect failures — "
                "the Pepperstone endpoint is unreachable.",
                attempts,
            )
            self.exit_code = 1
            self.shutdown_event.set()
            from twisted.internet import reactor

            try:
                reactor.callFromThread(reactor.stop)
            except Exception:  # noqa: BLE001 — best-effort stop
                pass
            return 30.0  # delay is irrelevant — the reactor is stopping
        return min(0.5 * (2 ** (attempts - 1)), 30.0)

    # ------------------------------------------------------------------
    # Realtime broadcaster thread: Pusher tick streaming
    # ------------------------------------------------------------------
    def _start_broadcaster(self) -> None:
        """Start the Pusher broadcaster daemon thread exactly once."""
        if self._broadcaster_started:
            return
        self._broadcaster_started = True
        thread = threading.Thread(
            target=self._broadcast_loop,
            name="ticks-broadcaster",
            daemon=True,
        )
        thread.start()

    def _broadcast_loop(self) -> None:
        """
        Consume repaired ticks and publish them to Pusher (channel
        ``njira-market``, event ``tick``) for live UI price display.

        Per-symbol throttle: at most ~4 events/sec; excess ticks are dropped
        (the UI also receives OHLC updates via existing polling). Broadcasting
        is fully best-effort — any failure is swallowed so the stream never
        stalls or crashes.
        """
        from forex.realtime import broadcast_event

        last_sent: dict[str, float] = {}
        min_interval = 0.25  # 4 events/sec per symbol
        while not self.shutdown_event.is_set():
            try:
                item = self.broadcast_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            symbol, bid, ask, spread, digits, timestamp_ms = item
            now = time.monotonic()
            last = last_sent.get(symbol)
            if last is not None and (now - last) < min_interval:
                continue  # drop excess
            last_sent[symbol] = now
            try:
                broadcast_event(
                    "njira-market",
                    "tick",
                    {
                        "symbol": symbol,
                        "bid": bid,
                        "ask": ask,
                        "spread": spread,
                        "digits": digits,
                        "timestamp_ms": timestamp_ms,
                    },
                )
            except Exception:  # noqa: BLE001 — realtime must never crash stream
                logger.debug("Pusher broadcast failed for %s", symbol)