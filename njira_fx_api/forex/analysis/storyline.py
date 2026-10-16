"""
Storyline / Multi-Timeframe (MTF) structure analysis (Malaysian SNR course).

The storyline is read top-down, each timeframe holding its own structure:

    Weekly — main direction.
    Daily  — retracement / roadblocks for the weekly direction.
    H4     — confirmation / roadblocks (gap levels) for daily and weekly.
    H1     — decides whether the direction is valid; must give a wick/gap candle.

"Price moves from support to resistance and resistance to support, over and
over again."  Every timeframe also carries ROADBLOCKS: the internal structural
highs and lows price has to overcome.

Change-of-storyline rule: a weekly bullish storyline cannot continue bullish
while the daily storyline is bearish, and vice versa.  That opposition blocks
the *weekly* direction only — it does not forbid a counter-trend trade.

Pure functions, no I/O.  Candles are dicts:
    {"open_time": float, "open": float, "high": float, "low": float, "close": float}
"""
from __future__ import annotations

WEEK_DAYS = 5          # Mon-Fri trading week
ROADBLOCK_LIMIT = 10   # most recent roadblocks kept per timeframe
SWING_SIDE = 2         # candles each side required for a swing


def tf_direction(candles: list[dict], lookback: int = 20) -> str:
    """Direction of the last ``lookback`` candles: bullish / bearish / sideways.

    Bullish needs BOTH a net-rising close over the window AND more than half
    the candles closing above their open.  Bearish is the mirror.  Anything
    else is sideways (no storyline to ride).
    """
    window = candles[-lookback:] if lookback > 0 else list(candles)
    if len(window) < 2:
        return "sideways"

    bullish = sum(1 for c in window if c["close"] > c["open"])
    bearish = sum(1 for c in window if c["close"] < c["open"])

    if window[-1]["close"] > window[0]["close"] and bullish > len(window) / 2:
        return "bullish"
    if window[-1]["close"] < window[0]["close"] and bearish > len(window) / 2:
        return "bearish"
    return "sideways"


def resample_weekly(d1_candles: list[dict]) -> list[dict]:
    """Derive weekly candles from d1 by grouping consecutive 5-day weeks.

    Weekly candles are not stored upstream, so they are built here: the OHLC of
    each Mon-Fri block.  A trailing partial week is kept as-is.
    """
    weekly: list[dict] = []
    for start in range(0, len(d1_candles), WEEK_DAYS):
        block = d1_candles[start:start + WEEK_DAYS]
        if not block:
            continue
        weekly.append({
            "open_time": block[0]["open_time"],
            "open": block[0]["open"],
            "high": max(c["high"] for c in block),
            "low": min(c["low"] for c in block),
            "close": block[-1]["close"],
        })
    return weekly


_roadblock_cache: dict[int, tuple[list, list[dict]]] = {}


def roadblocks(candles: list[dict]) -> list[dict]:
    """Internal structural highs and lows — the obstacles on the way.

    A swing high's high is above the 2 candles before AND the 2 after it;
    a swing low is the mirror.  Edge candles are skipped (no full window).
    Returned most-recent first, capped at 10.

    ponytail: this is a pure function of the candle window but ran on every bar
    for every timeframe (~20% of SNR backtest time).  Memoise on the window's
    identity + length, holding a STRONG reference to the list so a recycled id()
    can never produce a false hit.  Returns a shallow copy so callers cannot
    mutate the cached value.
    """
    if not candles:
        return []
    n = len(candles)
    key = id(candles)
    hit = _roadblock_cache.get(key)
    if hit is not None and hit[0] is candles and hit[1] == n:
        return list(hit[2])
    found: list[dict] = []
    sides = [k for k in range(-SWING_SIDE, SWING_SIDE + 1) if k]  # neighbours of i
    for i in range(SWING_SIDE, n - SWING_SIDE):
        hi, lo = candles[i]["high"], candles[i]["low"]
        if all(candles[i + k]["high"] < hi for k in sides):
            found.append({"type": "high", "price": hi, "open_time": candles[i]["open_time"]})
        if all(candles[i + k]["low"] > lo for k in sides):
            found.append({"type": "low", "price": lo, "open_time": candles[i]["open_time"]})
    found.reverse()
    found = found[:ROADBLOCK_LIMIT]
    if len(_roadblock_cache) > 8:
        _roadblock_cache.clear()
    _roadblock_cache[key] = (candles, n, found)
    return list(found)


def storyline_allows(weekly_dir: str, daily_dir: str, direction: str) -> bool:
    """Change-of-storyline gate for a signal in ``direction``.

    Weekly bullish + daily bearish blocks bullish (the weekly storyline cannot
    continue until the daily bearish storyline ends); the mirror holds too.
    Any other pairing is allowed — a sideways weekly lets both sides trade.
    """
    if weekly_dir == "bullish" and daily_dir == "bearish" and direction == "bullish":
        return False
    if weekly_dir == "bearish" and daily_dir == "bullish" and direction == "bearish":
        return False
    return True


def h1_gives_wick_or_gap(candles: list[dict], direction: str) -> bool:
    """Book: H1 is "very special; price decides whether direction is valid.
    Must give wick/gap candle."

    The most recent H1 candle must either GAP in the trade direction (opened
    beyond the prior close) or print a rejection WICK in that direction (a
    lower/upper wick at least as long as the body, closing in the direction).
    ``direction`` is "bullish" or "bearish".  No candles → False.
    """
    if not candles:
        return False
    c = candles[-1]
    o, h, low, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
    if len(candles) >= 2:
        prev_close = float(candles[-2]["close"])
        if direction == "bullish" and o > prev_close:
            return True   # gapped up
        if direction == "bearish" and o < prev_close:
            return True   # gapped down
    body = abs(cl - o)
    if body <= 0:
        return False
    if direction == "bullish" and cl > o and (min(o, cl) - low) >= body:
        return True       # bullish rejection wick
    if direction == "bearish" and cl < o and (h - max(o, cl)) >= body:
        return True       # bearish rejection wick
    return False


def analyze_storyline(d1_candles: list[dict], h4_candles: list[dict],
                      h1_candles: list[dict]) -> dict:
    """Top-down storyline read: weekly direction + per-TF roadblocks + gates."""
    weekly_dir = tf_direction(resample_weekly(d1_candles))
    daily_dir = tf_direction(d1_candles)
    return {
        "weekly": weekly_dir,
        "daily": daily_dir,
        "h4": tf_direction(h4_candles),
        "h1": tf_direction(h1_candles),
        "roadblocks": {
            "daily": roadblocks(d1_candles),
            "h4": roadblocks(h4_candles),
            "h1": roadblocks(h1_candles),
        },
        "weekly_allows_buy": storyline_allows(weekly_dir, daily_dir, "bullish"),
        "weekly_allows_sell": storyline_allows(weekly_dir, daily_dir, "bearish"),
        # Book: H1 must give a wick/gap candle for the direction to be valid.
        "h1_confirms_buy": h1_gives_wick_or_gap(h1_candles, "bullish"),
        "h1_confirms_sell": h1_gives_wick_or_gap(h1_candles, "bearish"),
    }
