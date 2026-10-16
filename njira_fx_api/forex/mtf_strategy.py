"""
MultiTimeframeStrategy — macro trend alignment across 7 timeframes.

The strategy computes a 10-period simple moving average of the close for each
timeframe (1M / 5M / 15M / 30M / 1H / 4H / 1D) and derives a single market
alignment signal:

* ``BUY``  — every close (from the first full 10-bar window onward) sits
  strictly ABOVE its rolling 10-SMA on every timeframe.
* ``SELL`` — every close sits strictly BELOW its rolling 10-SMA on every
  timeframe.
* ``HOLD`` — mixed alignment, a close touching its SMA, insufficient data,
  or empty bars.

Note on the "ALL closes > SMA" rule: comparing every close against a single
SMA of the same window is mathematically unsatisfiable (the window's own mean
can never be below every element), so the rolling per-bar comparison is used —
the standard "price consistently above its moving average" trend definition.

Bars come from ClickHouse via :func:`forex.clickhouse.schema.get_ohlc_bars`
(best-effort — returns ``[]`` on failure, which maps to ``HOLD``).
"""
from __future__ import annotations

import logging
from typing import Any

from .clickhouse.connection import get_clickhouse_client
from .clickhouse.schema import get_ohlc_bars

logger = logging.getLogger("forex.mtf")

# Timeframe label -> aggregation window in minutes (1D = 1440 minutes).
TIMEFRAMES: dict[str, int] = {
    "1M": 1,
    "5M": 5,
    "15M": 15,
    "30M": 30,
    "1H": 60,
    "4H": 240,
    "1D": 1440,
}

SMA_PERIOD = 10
BARS_PER_TIMEFRAME = 20


class MultiTimeframeStrategy:
    """Computes macro trend alignment from multi-timeframe OHLC bars."""

    def __init__(self, context_engine: Any | None = None) -> None:
        self._context_engine = context_engine

    # ── Context-backed alignment (improve.md §17) ────────────────────────
    # Replaces the 7 blocking ClickHouse queries with the shared context's
    # per-timeframe structure bias.  Falls back to the CH path when no context
    # engine is supplied (backward compatible).

    def _nearest_tf(self, minutes: int, mtf: dict) -> int:
        if minutes in mtf:
            return minutes
        keys = list(mtf.keys())
        if not keys:
            return minutes
        return min(keys, key=lambda k: abs(k - minutes))

    def _context_alignment(self, symbol: str) -> str:
        mtf = (
            self._context_engine.get(symbol).multi_timeframe_context
            if self._context_engine is not None else {}
        )
        if not mtf:
            return "HOLD"
        signals: list[str] = []
        for minutes in TIMEFRAMES.values():
            bias = mtf.get(self._nearest_tf(minutes, mtf))
            if not bias or bias == "neutral":
                return "HOLD"
            signals.append("BUY" if bias == "bullish" else "SELL")
        if all(s == "BUY" for s in signals):
            return "BUY"
        if all(s == "SELL" for s in signals):
            return "SELL"
        return "HOLD"

    def _context_alignment_with_score(self, symbol: str) -> tuple[str, float, dict]:
        mtf = (
            self._context_engine.get(symbol).multi_timeframe_context
            if self._context_engine is not None else {}
        )
        results: dict[str, str] = {}
        buy = sell = total = 0
        for label, minutes in TIMEFRAMES.items():
            bias = mtf.get(self._nearest_tf(minutes, mtf))
            if bias == "bullish":
                results[label] = "BUY"; total += 1; buy += 1
            elif bias == "bearish":
                results[label] = "SELL"; total += 1; sell += 1
            else:
                results[label] = "HOLD"
        if total == 0:
            return "HOLD", 0.0, results
        if buy > sell and buy >= 4:
            return "BUY", round(buy / total * 89, 1), results
        if sell > buy and sell >= 4:
            return "SELL", round(sell / total * 89, 1), results
        return "HOLD", 0.0, results

    def calculate_simple_moving_average(
        self, bars: list[dict], period: int
    ) -> float | None:
        """
        Return the simple moving average of the last ``period`` closes.

        ``bars`` is a list of dicts ``{time, open, high, low, close}``.
        Returns ``None`` when there are fewer than ``period`` bars (the SMA
        is undefined — callers must treat ``None`` as "no signal").
        """
        closes = [b["close"] for b in bars]
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    def _rolling_alignment(self, bars: list[dict]) -> str | None:
        """
        Return ``'BUY'`` / ``'SELL'`` / ``'HOLD'`` for one timeframe's bars.

        Every close from the first full 10-bar window onward is compared with
        its rolling 10-SMA: all above -> BUY, all below -> SELL, any touch or
        mixed -> HOLD. Returns ``None`` when there are fewer than 10 bars.
        """
        closes = [b["close"] for b in bars]
        if len(closes) < SMA_PERIOD:
            return None
        above = below = 0
        for i in range(SMA_PERIOD - 1, len(closes)):
            window = closes[i - SMA_PERIOD + 1 : i + 1]
            sma = sum(window) / SMA_PERIOD
            if closes[i] > sma:
                above += 1
            elif closes[i] < sma:
                below += 1
            else:
                return "HOLD"  # close touched the SMA — no clean alignment
        if above and below == 0:
            return "BUY"
        if below and above == 0:
            return "SELL"
        return "HOLD"

    def check_market_alignment(self, symbol: str) -> str:
        """
        Return ``'BUY'`` | ``'SELL'`` | ``'HOLD'`` for ``symbol``.

        Fetches up to ``BARS_PER_TIMEFRAME`` bars per timeframe and requires
        every timeframe to agree on the same rolling-SMA alignment. Empty bars
        or insufficient history short-circuit to ``HOLD``.
        """
        if self._context_engine is not None:
            return self._context_alignment(symbol)
        client = get_clickhouse_client()
        signals: list[str] = []

        for timeframe, minutes in TIMEFRAMES.items():
            bars = get_ohlc_bars(client, symbol, minutes, limit=BARS_PER_TIMEFRAME)
            if not bars:
                logger.info(
                    "MTF %s: no bars for %s on %s — HOLD", symbol, timeframe, minutes
                )
                return "HOLD"
            signal_value = self._rolling_alignment(bars)
            if signal_value is None:
                logger.info(
                    "MTF %s: insufficient bars on %s (%d) — HOLD",
                    symbol,
                    timeframe,
                    len(bars),
                )
                return "HOLD"
            signals.append(signal_value)

        if all(s == "BUY" for s in signals):
            return "BUY"
        if all(s == "SELL" for s in signals):
            return "SELL"
        return "HOLD"

    def check_market_alignment_with_score(self, symbol: str) -> tuple[str, float, dict]:
        """
        Like ``check_market_alignment`` but returns ``(direction, confidence, breakdown)``.

        Confidence is the ratio of agreeing timeframes × 89 (max score).
        ``breakdown`` maps each TF label to its individual alignment.
        """
        if self._context_engine is not None:
            return self._context_alignment_with_score(symbol)
        client = get_clickhouse_client()
        results: dict[str, str] = {}
        buy_count = sell_count = total = 0

        for tf_label, minutes in TIMEFRAMES.items():
            bars = get_ohlc_bars(client, symbol, minutes, limit=BARS_PER_TIMEFRAME)
            if not bars:
                results[tf_label] = "NO_DATA"
                continue
            val = self._rolling_alignment(bars)
            results[tf_label] = val or "HOLD"
            total += 1
            if val == "BUY":
                buy_count += 1
            elif val == "SELL":
                sell_count += 1

        if total == 0:
            return "HOLD", 0.0, results

        if buy_count > sell_count and buy_count >= 4:
            direction = "BUY"
            score = (buy_count / total) * 89
        elif sell_count > buy_count and sell_count >= 4:
            direction = "SELL"
            score = (sell_count / total) * 89
        else:
            return "HOLD", 0.0, results

        return direction, round(score, 1), results