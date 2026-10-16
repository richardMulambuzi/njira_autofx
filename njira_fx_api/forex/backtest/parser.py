"""
CSV parsing + OHLC resampling for the backtest engine.

Two CSV shapes are supported:

**Format A (legacy/quotes-api):**

    ticker,datestamp,timestamp,open,high,low,close

where ``datestamp`` is ``YYYYMMDD`` and ``timestamp`` is ``HHMMSS`` (1-minute
bars).  ``ticker`` is the instrument symbol (NOT hardcoded — it is read from
the column so the same engine works for XAUUSD, EURUSD, etc.).  ``ticker`` may
also be named ``symbol`` / ``pair`` / ``instrument``.

**Format B (real MT4/MT5 export):**

    Date,open,high,low,close,tick_volume

where ``Date`` is a full datetime string (``YYYY-MM-DD HH:MM:SS`` for
sub-daily bars, ``YYYY-MM-DD`` for daily).  There is no ticker column, so a
``symbol`` override is required; ``tick_volume`` is a real volume column.

Volume handling:

* If the dataset has a ``volume`` / ``vol`` / ``tick_volume`` column the real
  value is carried into each candle.
* Otherwise a best-effort volume estimate is synthesized from the candle's
  range: ``max(1, round((high - low) / tick_size))`` where ``tick_size`` is
  derived from the symbol's price magnitude (price > 1000 -> 2 digits,
  > 100 -> 3 digits, else 5 digits).

``resample`` aggregates volume (sum) when candles carry it.
"""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

# Recognized symbol column headers (Gap 4 — pair auto-detect).
SYMBOL_COLUMN_NAMES = ("ticker", "symbol", "pair", "instrument")
# Recognized volume column headers (Gap 1 — volume normalization).
VOLUME_COLUMN_NAMES = ("volume", "vol", "tick_volume")

# Fail-fast magnitude bands live in _band_for below (XAU 500-10000, etc.)
# to catch a silent 1000x mis-scale.


def _band_for(symbol: str | None) -> tuple[float, float] | None:
    """Return the (lo, hi) close band for ``symbol``, or None when unknown."""
    sym = (symbol or "").strip().upper()
    if sym.startswith("XAU"):
        return (500.0, 10000.0)
    if sym.startswith("XAG"):
        return (5.0, 200.0)
    if sym == "EURUSD":
        return (0.5, 3.0)
    return None


def _assert_magnitudes(candles: list[dict], source: str) -> None:
    """Fail fast on a 1000x-scaled CSV: XAU 500<close<10000, etc.

    Raises ``ValueError`` naming file + row (1-based among parsed candles).
    Unknown symbols are unchecked.
    """
    for i, candle in enumerate(candles):
        band = _band_for(candle.get("symbol"))
        if band is None:
            continue
        lo, hi = band
        try:
            close = float(candle.get("close"))
        except (TypeError, ValueError):
            continue
        if not (lo < close < hi):
            raise ValueError(
                f"Price magnitude out of range: file={source} row={i + 1} "
                f"symbol={candle.get('symbol')} close={close} "
                f"expected {lo}<close<{hi} (possible 1000x scaling error)"
            )

# Timeframe unit -> seconds, from a filename token like ``h1`` / ``m15`` / ``d1`` / ``w1``.
_TF_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def infer_timeframe(filename: str) -> int | None:
    """
    Infer a timeframe (seconds) from a filename like ``AUDJPYh1.csv``.

    The token after the pair is parsed case-insensitively:
    ``d<num>`` -> days, ``h<num>`` -> hours, ``m<num>`` -> minutes,
    ``w<num>`` -> weeks (``AUDJPYd1`` -> 86400, ``AUDJPYm15`` -> 900, ...).
    Returns ``None`` when no time unit token is found (e.g. ``AUDJPY.csv``).
    """
    if not filename:
        return None
    stem = os.path.splitext(str(filename))[0]
    match = re.search(r"([dhwm])(\d{1,3})\s*$", stem, re.IGNORECASE)
    if not match:
        return None
    unit, num = match.group(1).lower(), int(match.group(2))
    return _TF_UNIT_SECONDS[unit] * num


def _parse_dt(datestamp: str, timestamp: str) -> datetime:
    """Build a timezone-aware UTC datetime from YYYYMMDD + HHMMSS (or HHMM)."""
    ds = datestamp.strip()
    ts = timestamp.strip()
    fmt = "%Y%m%d%H%M%S" if len(ts) == 6 else "%Y%m%d%H%M"
    return datetime.strptime(ds + ts, fmt).replace(tzinfo=timezone.utc)


def _parse_iso_datetime(value: str) -> datetime:
    """Parse the MT4 ``Date`` cell (``YYYY-MM-DD HH:MM:SS`` or ``YYYY-MM-DD``) as UTC."""
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _find_header(header: list[str] | None, candidates: tuple[str, ...]) -> str | None:
    """Return the actual header cell matching one of ``candidates`` (case-insensitive)."""
    lower_map = {h.lower(): h for h in header or []}
    for name in candidates:
        if name in lower_map:
            return lower_map[name]
    return None


def _synth_volume(high: float, low: float, price: float) -> int:
    """Best-effort volume estimate from the candle range + price magnitude."""
    digits = 2 if price > 1000 else (3 if price > 100 else 5)
    tick_size = 10.0 ** (-digits)
    return max(1, round((high - low) / tick_size))


def _volume_for_row(raw: Any, high: float, low: float, price: float) -> int:
    """Real column value when present/valid, else the synthesized estimate."""
    if raw is not None and str(raw).strip():
        try:
            return round(float(raw))
        except (TypeError, ValueError):
            pass
    return _synth_volume(high, low, price)


def has_symbol_column(text: str) -> bool:
    """
    Best-effort header sniff: does this CSV declare a symbol column?

    Checks the first non-empty line for a ``ticker`` / ``symbol`` / ``pair`` /
    ``instrument`` header.  Used by the upload view to 400 when a dataset has
    neither a detectable pair nor a manual ``symbol`` override.
    """
    first = next((line for line in text.splitlines() if line.strip()), "")
    header = {cell.strip().lower() for cell in first.split(",")}
    return bool(header & set(SYMBOL_COLUMN_NAMES))


def _open_time_for_row(
    row: dict, date_col: str | None, ds_col: str | None, ts_col: str | None
) -> datetime:
    """Time from either the MT4 ``Date`` cell or the legacy datestamp+timestamp pair."""
    if date_col is not None:
        return _parse_iso_datetime(row.get(date_col) or "")
    return _parse_dt(row[ds_col], row[ts_col])


def _row_to_candle(
    row: dict, cols: tuple, symbol: str | None, use_override: bool,
    timeframe: int | None,
) -> dict[str, Any] | None:
    """Turn one CSV row into a candle dict (``None`` for malformed/unknown rows).

    ``cols`` is ``(symbol_col, volume_col, date_col, ds_col, ts_col)`` as
    detected from the header by :func:`_parse`.
    """
    symbol_col, volume_col, date_col, ds_col, ts_col = cols
    if symbol_col:
        ticker = (row.get(symbol_col) or "").strip()
    else:
        ticker = symbol if use_override else ""
    candle_symbol = symbol.strip() if use_override else ticker
    if not candle_symbol:
        return None
    try:
        open_, high, low, close = (
            float(row["open"]), float(row["high"]),
            float(row["low"]), float(row["close"]),
        )
        candle: dict[str, Any] = {
            "symbol": candle_symbol,
            "open_time": _open_time_for_row(row, date_col, ds_col, ts_col),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": _volume_for_row(
                row.get(volume_col) if volume_col else None, high, low, close
            ),
        }
    except (KeyError, ValueError, TypeError):
        # Skip malformed rows rather than aborting the whole upload.
        return None
    if timeframe is not None:
        candle["timeframe"] = timeframe
    return candle


def _is_histdata(text: str) -> bool:
    """Detect the HistData.com ASCII format: semicolon-delimited, headerless,
    first column ``YYYYMMDD HHMMSS`` (e.g. ``20260802 180000;4081.9;...``)."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return bool(re.match(r"^\d{8} \d{6};", first.strip()))


def _is_histdata_comma(text: str) -> bool:
    """Detect the comma-delimited HistData format: YYYY.MM.DD,HH:MM,O,H,L,C,V (no header)."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return bool(re.match(r"^\d{4}\.\d{2}\.\d{2},\d{2}:\d{2},", first.strip()))


def _parse_histdata(
    text: str, symbol: str | None, timeframe: int | None
) -> list[dict]:
    """Parse the HistData.com ASCII format (semicolon-delimited).

    Each line is ``YYYYMMDD HHMMSS;open;high;low;close;volume`` (semicolon
    delimited, no header).  The timestamp is a single combined column.
    """
    candles: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) < 5:
            continue
        try:
            dt = datetime.strptime(parts[0].strip(), "%Y%m%d %H%M%S").replace(
                tzinfo=timezone.utc
            )
            open_, high, low, close = (float(parts[i]) for i in range(1, 5))
            vol_raw = parts[5].strip() if len(parts) > 5 else ""
        except (ValueError, IndexError):
            continue
        candle: dict[str, Any] = {
            "symbol": symbol or "XAUUSD",
            "open_time": dt,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": _volume_for_row(vol_raw, high, low, close),
        }
        if timeframe is not None:
            candle["timeframe"] = timeframe
        candles.append(candle)
    return candles


def _parse_histdata_comma(
    text: str, symbol: str | None, timeframe: int | None
) -> list[dict]:
    """Parse the comma-delimited HistData format: YYYY.MM.DD,HH:MM,O,H,L,C,V (no header)."""
    candles: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            dt = datetime.strptime(f"{parts[0].strip()} {parts[1].strip()}", "%Y.%m.%d %H:%M").replace(
                tzinfo=timezone.utc
            )
            open_, high, low, close = (float(parts[i]) for i in range(2, 6))
            vol_raw = parts[6].strip() if len(parts) > 6 else ""
        except (ValueError, IndexError):
            continue
        candle: dict[str, Any] = {
            "symbol": symbol or "XAUUSD",
            "open_time": dt,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": _volume_for_row(vol_raw, high, low, close),
        }
        if timeframe is not None:
            candle["timeframe"] = timeframe
        candles.append(candle)
    return candles


def _parse(
    file: Any, symbol: str | None = None, timeframe: int | None = None
) -> list[dict]:
    """Shared row->candle pipeline for :func:`parse_csv` / :func:`parse_tf_csv`."""
    if hasattr(file, "read"):
        text = file.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        lines: Iterable[str] = text.splitlines()
    else:
        lines = file

    # HistData.com ASCII: semicolon-delimited, headerless, ``YYYYMMDD HHMMSS``.
    full_text = text if isinstance(text, str) else "\n".join(lines)
    source = getattr(file, "name", None) or "csv"
    if _is_histdata(full_text):
        candles = _parse_histdata(full_text, symbol, timeframe)
        _assert_magnitudes(candles, source)
        return candles
    if _is_histdata_comma(full_text):
        candles = _parse_histdata_comma(full_text, symbol, timeframe)
        _assert_magnitudes(candles, source)
        return candles

    reader = csv.DictReader(lines)
    fieldnames = list(reader.fieldnames or [])
    symbol_col = _find_header(fieldnames, SYMBOL_COLUMN_NAMES)
    volume_col = _find_header(fieldnames, VOLUME_COLUMN_NAMES)
    date_col = _find_header(fieldnames, ("date",))
    ds_col = _find_header(fieldnames, ("datestamp",))
    ts_col = _find_header(fieldnames, ("timestamp",))
    use_override = symbol is not None and str(symbol).strip() != ""

    cols = (symbol_col, volume_col, date_col, ds_col, ts_col)
    candles: list[dict] = []
    for row in reader:
        if row is None:
            continue
        candle = _row_to_candle(row, cols, symbol, use_override, timeframe)
        if candle is not None:
            candles.append(candle)
    _assert_magnitudes(candles, getattr(file, "name", None) or "csv")
    return candles


def parse_csv(file: Any, symbol: str | None = None) -> list[dict]:
    """
    Parse the forex CSV into a list of 1-minute candle dicts.

    Returns ``[{symbol, open_time(datetime), open, high, low, close, volume, timeframe}, ...]``
    in file order.  Accepts a file-like object (``request.FILES``,
    ``io.StringIO``) or an iterable of text lines.

    Volume: a real ``volume``/``vol``/``tick_volume`` column is preserved;
    otherwise a range-based estimate is synthesized (see module docstring).

    Pair: the symbol is read from the ticker column when present.  Passing
    ``symbol`` overrides the detected pair for every candle (Gap 4).  Rows
    without any symbol are skipped.  The MT4 ``Date`` format has no ticker
    column, so ``symbol`` is required there (``has_symbol_column`` returns
    False for it).
    """
    return _parse(file, symbol, timeframe=60)


def parse_tf_csv(
    file: Any, symbol: str | None = None, timeframe: int | None = None
) -> list[dict]:
    """
    Like :func:`parse_csv` but each candle carries a ``timeframe`` key.

    When ``timeframe`` is None it is inferred from the file name (see
    :func:`infer_timeframe`); if nothing can be inferred it defaults to 3600
    (H1).  Pass an explicit ``timeframe`` to skip inference.
    """
    if timeframe is None:
        timeframe = infer_timeframe(getattr(file, "name", None)) or 3600
    return _parse(file, symbol, timeframe=int(timeframe))


def resample(candles: list[dict], timeframe_seconds: int) -> list[dict]:
    """
    Aggregate 1-minute candles into a higher timeframe via OHLC aggregation.

    ``candles`` must be the output of :func:`parse_csv` (sorted, 1m bars).
    Bars are bucketed by flooring ``open_time`` to ``timeframe_seconds``.
    Returns the same dict shape (symbol carried from the bucket's first bar).
    When the input candles carry ``volume``, the output bucket sums it.
    """
    if not candles or timeframe_seconds <= 0:
        return []

    buckets: dict[int, list[dict]] = {}
    order: list[int] = []
    for c in candles:
        ot: datetime = c["open_time"]
        epoch = ot.timestamp()
        bucket_start = int(epoch // timeframe_seconds) * timeframe_seconds
        if bucket_start not in buckets:
            buckets[bucket_start] = []
            order.append(bucket_start)
        buckets[bucket_start].append(c)

    out: list[dict] = []
    for bucket_start in order:
        group = buckets[bucket_start]
        first = group[0]
        last = group[-1]
        bar: dict[str, Any] = {
            "symbol": first["symbol"],
            "open_time": datetime.fromtimestamp(bucket_start, tz=timezone.utc),
            "open": float(first["open"]),
            "high": max(float(c["high"]) for c in group),
            "low": min(float(c["low"]) for c in group),
            "close": float(last["close"]),
        }
        if any("volume" in c for c in group):
            bar["volume"] = sum(int(c.get("volume") or 0) for c in group)
        out.append(bar)
    return out
