"""
Engulfing pattern engine — Malaysian SNR strategy.

Course spec:
    "An Engulfing means a complete covering up of an object. That is, the second
     candlestick completely engulfs or covers up the first candlestick. This
     pattern represents price rejection in any financial market irrespective of
     any timeframe."

    "When HTF Gap has within a LTF Engulfing. The Hidden Engulfing becomes more
     obvious when refined in LTFs. The lower you go on a LTFs, the more is
     Engulfing made manifest."

    "H4 GAP SNR REFINED INTO LTF (H1) ENGULFING: Scalping for Maximum Profits!"

Pure functions only — no I/O, no engine imports.  Candles are dicts with
``open_time`` as epoch SECONDS (float) plus open/high/low/close.
"""
from __future__ import annotations

H4_TF_SECONDS = 14400


def detect_engulfing(candles: list[dict], index: int) -> str | None:
    """
    Body-only engulfing of candles[index-1] by candles[index] (wicks ignored).

    Returns "bullish", "bearish", or None.  Requires a strict body-size
    increase so a same-size body is not an engulfing.
    """
    if index < 1 or index >= len(candles):
        return None

    prev, cur = candles[index - 1], candles[index]
    prev_open, prev_close = prev["open"], prev["close"]
    cur_open, cur_close = cur["open"], cur["close"]

    # Body sizes compared as magnitudes: the book's rule is "the 2nd candle
    # completely engulfs the body of the 1st".  Signing the comparison made it
    # tautological against a prev candle of the opposite direction (a negative
    # RHS is beaten by any bullish LHS), so the size guard silently never fired.
    cur_body = abs(cur_close - cur_open)
    prev_body = abs(prev_close - prev_open)

    if cur_close > cur_open:  # bullish body
        if cur_open <= prev_close and cur_close >= prev_open and cur_body > prev_body:
            return "bullish"
    elif cur_close < cur_open:  # bearish body
        if cur_open >= prev_close and cur_close <= prev_open and cur_body > prev_body:
            return "bearish"
    return None


def detect_gap(candles: list[dict], index: int) -> str | None:
    """
    GAP (open-close) SNR between candles[index-1] and candles[index] — the
    "Decision" level.  Book p.13: the gap is where this candle OPENS relative
    to the previous candle's CLOSE.  Wicks are ignored, exactly like the A/V
    SNR lines, so this stays consistent with ``_snr_levels``.

    This used to compare the open against the prior HIGH/LOW, which is a
    conventional tick price-gap, not the Malaysian SNR.  Because it fired on
    almost any bar separation it produced a "gap" on ~9% of H4 candles; the
    open-close construction is the rare, significant one the book means.
    """
    if index < 1 or index >= len(candles):
        return None

    prev, cur = candles[index - 1], candles[index]
    if cur["open"] > prev["close"]:
        return "bullish"
    if cur["open"] < prev["close"]:
        return "bearish"
    return None


def find_engulfing_in_gap(
    h4_candles: list[dict],
    h4_gap_index: int,
    ltf_candles: list[dict],
    ltf_tf_seconds: int,
    max_ltf_candles: int = 48,
) -> dict | None:
    """
    Refine an H4 gap into an LTF engulfing of the same direction.

    The gap zone is the H4 gap candle's own window: [its open_time,
    open_time + 14400).  Scans the LTF candles inside that zone and returns the
    FIRST engulfing matching the gap direction, else None.
    """
    direction = detect_gap(h4_candles, h4_gap_index)
    if direction is None:
        return None

    gap_open_time = h4_candles[h4_gap_index]["open_time"]
    zone_end = gap_open_time + H4_TF_SECONDS
    # At most this many LTF bars can fall inside a 4h window; the smaller of the
    # two caps is the honest scan budget.
    limit = (min(max_ltf_candles, H4_TF_SECONDS // ltf_tf_seconds)
             if ltf_tf_seconds > 0 else max_ltf_candles)

    scanned = 0
    for i, candle in enumerate(ltf_candles):
        open_time = candle["open_time"]
        if open_time < gap_open_time:
            continue
        if open_time >= zone_end:
            break
        if scanned >= limit:
            break
        scanned += 1
        if detect_engulfing(ltf_candles, i) == direction:
            return {
                "direction": direction,
                "index": i,
                "open_time": open_time,
                "price": candle["close"],
            }
    return None


def engulfing_entry(candles: list[dict], index: int) -> dict | None:
    """
    Entry confirmed by the engulfing candle at `index`: fill at the NEXT candle's
    open, stop at the engulfing candle's wick, target at 2x risk.
    """
    if index < 0 or index + 1 >= len(candles):
        return None

    direction = detect_engulfing(candles, index)
    if direction is None:
        return None

    engulfing, nxt = candles[index], candles[index + 1]
    entry = nxt["open"]  # fill at next bar's open when engulfing candle closes
    if direction == "bullish":
        sl = engulfing["low"]
        tp = entry + 2.0 * (entry - sl)
    else:
        sl = engulfing["high"]
        tp = entry - 2.0 * (sl - entry)

    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "candle_index": index,
        "open_time": engulfing["open_time"],
    }
