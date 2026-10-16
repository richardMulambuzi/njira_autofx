"""
TickNormalizer — provider-native payload → canonical MarketTick (or drop).

Pure, stateless, thread-safe (spec §5.1 / contracts §3). The engine accepts
payloads directly from provider callback threads / backfill rows (spec §1.1
pipeline: Provider Adapter → Tick Normalizer → Engine).

Payload shapes handled (contracts §3):
   * Pepperstone: query-worker history row ``{"timestamp", "bid", "ask",
    "last"}`` (optionally carrying a tick/deal id for ``seq``).
  * Mock: internal ``MarketTick`` (pass-through) or a dict with the same
    field names.

Drop rules (return ``None``, reason logged — spec §5.1 / contracts §3):
  * non-finite or non-positive prices; ``bid > ask`` (epsilon 1e-9)
  * timestamp outside ``[SYNC_MIN_TS, now + SYNC_MAX_FUTURE_SKEW]``
  * symbol failing ``^[A-Z0-9_]{1,20}$`` (SYMBOL_PATTERN, ticks_views.py:46)
  * unparseable provider payload (missing quote/epoch, wrong shape)
"""
from __future__ import annotations

import logging
import math
import re
import struct
from datetime import datetime, timezone

from forex.broker.models import MarketTick

logger = logging.getLogger("forex.sync.normalizer")

#: Symbol allowlist — mirrors ticks_views.SYMBOL_PATTERN (ticks_views.py:46).
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]{1,20}$")

#: Epsilon for the bid > ask rejection (spec §5.1).
BID_ASK_EPSILON = 1e-9

#: Values >= this are treated as epoch milliseconds (int/float payloads).
_EPOCH_MS_THRESHOLD = 1_000_000_000_000  # 1e12


def float_bits(value: float) -> int:
    """IEEE-754 bit pattern of a double (big-endian) — exact float identity.

    ``struct.pack(">d", last)`` → int (spec §4.2). No epsilon games.
    """
    return struct.unpack(">Q", struct.pack(">d", float(value)))[0]


def to_epoch_ms(value: object) -> int | None:
    """Coerce a payload timestamp to epoch milliseconds UTC (int64).

    Accepts:
      * ``datetime`` — tz-aware (naive is assumed UTC, logged at debug)
      * ``int``/``float`` — epoch seconds (< 1e12) or epoch ms (>= 1e12)
      * ``str`` — ISO-8601 (``fromisoformat``, incl. ``Z``) or epoch number

    Returns ``None`` when the value cannot be coerced.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            logger.debug("Naive datetime %s treated as UTC", dt)
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if abs(number) >= _EPOCH_MS_THRESHOLD:
            return int(number)
        return int(number * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return to_epoch_ms(float(text))
            except (TypeError, ValueError):
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def ms_to_datetime(ms: int) -> datetime:
    """Epoch milliseconds → tz-aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _as_float(value: object) -> float | None:
    """Coerce to a finite float, else ``None``."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class _PayloadError(ValueError):
    """Internal: a provider payload could not be parsed (drop, not crash)."""


class TickNormalizer:
    """Provider → canonical :class:`MarketTick` (or ``None`` = drop).

    One instance per engine (pure, stateless, thread-safe — spec §5.1).
    Validation bounds come from :class:`~forex.sync.config.SyncConfig`.
    """

    def __init__(
        self,
        config: object | None = None,
        *,
        min_ts: int = 0,
        max_future_skew_seconds: float = 300.0,
        dedup_key: str = "ts_seq",
    ) -> None:
        # Imported lazily to keep this module importable without the config
        # chain when constructed with explicit bounds (tests).
        if config is not None:
            min_ts = int(getattr(config, "min_ts", min_ts))
            max_future_skew_seconds = float(
                getattr(config, "max_future_skew_seconds", max_future_skew_seconds)
            )
            dedup_key = str(getattr(config, "dedup_key", dedup_key))
        self._min_ts = int(min_ts)
        self._max_future_skew_ms = int(max_future_skew_seconds * 1000)
        self._dedup_key = dedup_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def normalize(
        self, raw: object, provider: str, symbol: str
    ) -> MarketTick | None:
        """Validate and canonicalize one provider-native tick payload.

        Returns a canonical :class:`MarketTick` or ``None`` when the payload
        must be dropped (invalid/unsupported). The returned tick's ``symbol``
        is the canonical app symbol (whitespace-trimmed and upper-cased — the
        ``frx`` prefix is NOT stripped here; any such stripping is the
        caller's legacy-compat concern, e.g. ``sync_engine``).
        """
        try:
            tick = self._extract(raw, provider, symbol)
        except _PayloadError as exc:
            logger.warning(
                "sync normalizer: drop %s/%s tick — %s", provider, symbol, exc
            )
            return None
        if tick is None:
            return None
        return self._validate(tick)

    def dedup_key(self, raw: object, tick: MarketTick) -> tuple[int, int]:
        """Dedup key for a normalized tick (spec §4.2 / contracts §3).

        ``(ts_ms, seq)`` when the provider payload carries a tick id /
        sequence (``seq != -1``); else ``(ts_ms, float_bits(last))``.
        """
        ts = to_epoch_ms(tick.timestamp)
        if ts is None:
            raise ValueError("tick timestamp is not coercible to epoch ms")
        seq = self.extract_seq(raw, tick)
        if self._dedup_key == "ts_price" or seq == -1:
            return (ts, float_bits(tick.last))
        return (ts, seq)

    def extract_seq(self, raw: object, tick: MarketTick) -> int:
        """Derive the provider tick id/sequence from the raw payload.

        ``-1`` when the provider carries no id (Mock → -1,
        Pepperstone → deal/tick id when present, else -1 — contracts §3).
        """
        if isinstance(raw, MarketTick):
            return -1
        if isinstance(raw, dict):
            for key in ("seq", "tick_id", "deal_id", "id"):
                value = raw.get(key)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        continue
        return -1

    def ts_ms(self, tick: MarketTick) -> int:
        """Epoch ms of a normalized tick (raises ValueError when invalid)."""
        ts = to_epoch_ms(tick.timestamp)
        if ts is None:
            raise ValueError("tick timestamp is not coercible to epoch ms")
        return ts

    # ------------------------------------------------------------------
    # Extraction (per-provider)
    # ------------------------------------------------------------------
    def _extract(
        self, raw: object, provider: str, symbol: str
    ) -> MarketTick | None:
        provider = (provider or "").strip().lower()
        if provider == "pepperstone":
            return self._extract_pepperstone(raw, symbol)
        if provider == "mock":
            return self._extract_mock(raw, symbol)
        # Unknown provider: try the generic MarketTick-shaped payload.
        return self._extract_generic(raw, symbol)

    def _extract_pepperstone(self, raw: object, symbol: str) -> MarketTick | None:
        if not isinstance(raw, dict):
            raise _PayloadError("Pepperstone payload is not a dict")
        bid = _as_float(raw.get("bid"))
        ask = _as_float(raw.get("ask"))
        last = _as_float(raw.get("last"))
        if bid is None or ask is None or last is None:
            raise _PayloadError("Pepperstone row missing bid/ask/last")
        ts = to_epoch_ms(raw.get("timestamp"))
        if ts is None:
            raise _PayloadError("Pepperstone row missing/invalid 'timestamp'")
        payload_symbol = raw.get("symbol")
        canonical = (
            self._canonical_symbol(payload_symbol, symbol)
            if isinstance(payload_symbol, str) and payload_symbol
            else symbol.upper()
        )
        return MarketTick(
            provider="pepperstone",
            symbol=canonical,
            bid=bid,
            ask=ask,
            last=last,
            timestamp=ms_to_datetime(ts),
        )

    def _extract_mock(self, raw: object, symbol: str) -> MarketTick | None:
        if isinstance(raw, MarketTick):
            return raw
        if isinstance(raw, dict):
            return self._extract_generic(raw, symbol)
        raise _PayloadError(f"Mock payload has unexpected type {type(raw).__name__}")

    def _extract_generic(self, raw: object, symbol: str) -> MarketTick | None:
        """MarketTick-shaped dict: {provider?, symbol?, bid, ask, last,
        timestamp, volume?, seq?}."""
        if isinstance(raw, MarketTick):
            return raw
        if not isinstance(raw, dict):
            raise _PayloadError(f"payload has unexpected type {type(raw).__name__}")
        bid = _as_float(raw.get("bid"))
        ask = _as_float(raw.get("ask"))
        last = _as_float(raw.get("last"))
        if bid is None or ask is None or last is None:
            raise _PayloadError("payload missing bid/ask/last")
        ts = to_epoch_ms(raw.get("timestamp"))
        if ts is None:
            raise _PayloadError("payload missing/invalid 'timestamp'")
        payload_symbol = raw.get("symbol")
        canonical = (
            self._canonical_symbol(payload_symbol, symbol)
            if isinstance(payload_symbol, str) and payload_symbol
            else symbol.upper()
        )
        provider = raw.get("provider")
        if not isinstance(provider, str) or not provider:
            provider = "unknown"
        volume = _as_float(raw.get("volume")) or 0.0
        return MarketTick(
            provider=provider,
            symbol=canonical,
            bid=bid,
            ask=ask,
            last=last,
            timestamp=ms_to_datetime(ts),
            volume=volume,
        )

    # ------------------------------------------------------------------
    # Validation (shared)
    # ------------------------------------------------------------------
    def _validate(self, tick: MarketTick) -> MarketTick | None:
        reason = self._validation_error(tick)
        if reason is not None:
            logger.warning(
                "sync normalizer: drop %s/%s tick — %s",
                tick.provider,
                tick.symbol,
                reason,
            )
            return None
        return tick

    def _validation_error(self, tick: MarketTick) -> str | None:
        for name, value in (("bid", tick.bid), ("ask", tick.ask), ("last", tick.last)):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return f"{name} is not numeric"
            if not math.isfinite(value):
                return f"{name} is not finite"
            if value <= 0:
                return f"{name} is not positive"
        if tick.bid > tick.ask + BID_ASK_EPSILON:
            return f"bid {tick.bid} > ask {tick.ask} (epsilon {BID_ASK_EPSILON})"
        ts = to_epoch_ms(tick.timestamp)
        if ts is None:
            return "timestamp is not coercible to epoch ms"
        if ts < self._min_ts:
            return f"timestamp {ts} < SYNC_MIN_TS {self._min_ts}"
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if ts > now_ms + self._max_future_skew_ms:
            return (
                f"timestamp {ts} exceeds now + SYNC_MAX_FUTURE_SKEW "
                f"({self._max_future_skew_ms} ms)"
            )
        if not SYMBOL_PATTERN.fullmatch(tick.symbol):
            return f"symbol {tick.symbol!r} fails ^[A-Z0-9_]{{1,20}}$"
        return None

    @staticmethod
    def _canonical_symbol(payload_symbol: str, hint: str) -> str:
        """Upper-case the symbol."""
        canonical = payload_symbol.strip().upper()
        if not canonical:
            canonical = hint.strip().upper()
        return canonical


__all__ = [
    "BID_ASK_EPSILON",
    "SYMBOL_PATTERN",
    "TickNormalizer",
    "float_bits",
    "ms_to_datetime",
    "to_epoch_ms",
]