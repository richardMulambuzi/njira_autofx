"""
Malaysian SNR "MARRIAGE CONCEPT" (SNR + Trendline) — Ariff T.

A trendline is an ANGLED SNR: it maps the direction of SNR levels in a
trending market.  Two SNRs of the same kind are married into one sloped
line, and the trade is only taken at point #3 — the extension beyond the
second SNR — when a candle's wick touches and rejects it.

Course rules encoded here:
  1/2/3. SNR identification, gap-SNR exclusion and body-close anchoring are
         upstream concerns (``SignalEngine._snr_trendline`` builds the levels).
  4.     A trendline needs at least two SNRs → :func:`marriage_trendline`.
  5.     No candle may close beyond the line → invalidation in
         :func:`entry_at_point3`.
  6.     Entry is the next candle's open after a wick touch AND rejection.
  7.     Never at point #2; valid anywhere beyond point #3 while it holds.

Pure functions, no I/O.  Candles are dicts with ``open_time`` (epoch
seconds), ``open``, ``high``, ``low``, ``close``.
"""
from __future__ import annotations


def marriage_trendline(levels: list[dict], kind: str) -> "dict | None":
    """
    Marry the two most recent SNR levels of ``kind`` into one trendline.

    Rule 4: a trendline requires at least two SNRs.  Each level's
    representative point is its SECOND anchor ``(t2, y2)`` — the body
    close/open where the SNR forms (wicks ignored).  The two chosen points
    are ordered oldest-first so ``t1 < t2`` and point #3 is the extension
    beyond the newer SNR.

    Returns ``None`` when fewer than two levels of ``kind`` exist or the
    pair is degenerate (both form at the same timestamp).
    """
    matching = [lvl for lvl in levels if lvl.get("kind", kind) == kind]
    if len(matching) < 2:
        return None
    # The two most recent are levels[:2]; sort so the FIRST SNR is point #1.
    a, b = sorted(matching[:2], key=lambda lvl: lvl["t2"])
    t1, t2 = float(a["t2"]), float(b["t2"])
    if t2 == t1:
        return None
    y1, y2 = float(a["y2"]), float(b["y2"])
    return {
        "kind": kind,
        "t1": t1,
        "y1": y1,
        "t2": t2,
        "y2": y2,
        "slope": (y2 - y1) / (t2 - t1),
        "levels_used": 2,
    }


def line_value(line: dict, t: float) -> float:
    """``line`` extrapolated to time ``t`` (epoch seconds)."""
    return line["y1"] + line["slope"] * (t - line["t1"])


def entry_at_point3(
    line: dict, candles: list[dict], start_index: int,
) -> "dict | None":
    """
    Find the point #3 entry: wick touch + rejection beyond the second SNR.

    Only candles after ``line["t2"]`` are eligible, so point #2 never
    triggers a trade (rule 7).  Before a valid touch, any candle CLOSING
    beyond the line breaks the trendline and voids the setup (rule 5).
    Entry is the next candle's open (rule 6), SL sits at the touch candle's
    extreme, TP at 2x risk — SL is always on the losing side, so the one
    ``entry + 2 * (entry - sl)`` formula serves buys and sells alike.
    """
    is_support = line.get("kind", "support") == "support"
    for i in range(max(0, start_index), len(candles)):
        candle = candles[i]
        t = float(candle["open_time"])
        if t <= line["t2"]:
            continue
        lv = line_value(line, t)
        close = float(candle["close"])
        # Rule 5 — a close beyond the line breaks the trendline.
        if (close < lv) if is_support else (close > lv):
            return None
        low, high = float(candle["low"]), float(candle["high"])
        # Rule 6 — the wick must touch the line AND reject it.
        touches = low <= lv <= high
        rejects = close > lv if is_support else close < lv
        if not (touches and rejects) or i + 1 >= len(candles):
            continue
        entry = float(candles[i + 1]["open"])  # fill at next bar's open when point-#3 candle closes
        sl = low if is_support else high
        return {
            "direction": "buy" if is_support else "sell",
            "entry": entry,
            "sl": sl,
            "tp": entry + 2.0 * (entry - sl),
            "touch_index": i,
            "open_time": t,
            "line_value": lv,
        }
    return None
