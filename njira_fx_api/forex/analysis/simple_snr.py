"""Canadian-style 4H / 15M hybrid strategy (pure spec implementation).

Implements the agreed Steps 3-5:

  Step 3 (rejection on the anchor 4H): support tested first (newest -> oldest);
  a Buy is the first support level where 4H low is within NEAR_TOL of the level
  AND 4H close is ABOVE it. Otherwise Sell is the first resistance level where
  4H high is within NEAR_TOL AND close is BELOW it. Otherwise no setup.

  Step 4 (15M level): take the 15 most recent closed 15M candles ending at
  the anchor close; search backward for the newest bearish candle (Buy) or
  newest bullish candle (Sell); its close is the 15M level. If none exists
  there is no setup.

  Step 5 (entry/stop/target):
    entry_mode:
      "close"     - enter at the close of the FIRST 15M candle after the anchor
      "next-open" - enter at the open of the candle AFTER that
    stop_mode:
      "window-level"        - 15M level, moved by buffer
      "window-extreme"      - lowest low (Buy) / highest high (Sell) across the
                              15-candle window, moved by buffer
      "entry-candle-extreme"- low (Buy) / high (Sell) of the entry candle,
                              moved by buffer
    buffer      = fixed price distance, default 0.0 (caller sets per pair)
    take profit = 2R (risk = |entry - stop|)
    validity    : stop must be on the correct side of entry or the setup is discarded.

  Step "Running across all occurrences": every 4H anchor in time order is
  evaluated independently; the function returns a signal per valid anchor.
  Each signal: anchor_time, 4h_level, 15m_level, entry, entry_time, stop, target.

Open decisions are exposed as parameters (defaults in parentheses above).
Known gap mitigation: 'price_near_level_pct' (>0 enables a check that price
at entry stays within that % of the 15M level).
"""
from __future__ import annotations

from typing import Any

NEAR_TOL_PCT = 0.0005      # 0.05% wick tolerance for the 4H rejection test
TP_MULTIPLIER = 2.0
WINDOW_15M = 15


def _val(candle: dict, *keys: str) -> Any:
    for k in keys:
        v = candle.get(k)
        if v is not None:
            return v
    return None


def _time_key(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    try:
        from datetime import datetime

        if isinstance(value, datetime):
            return value.timestamp()
    except Exception:  # noqa: BLE001
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            from datetime import datetime

            return datetime.strptime(str(value), fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def find_4h_levels(candles_4h: list[dict]) -> tuple[list[float], list[float]]:
    """Support = close of every bearish 4H candle; resistance = close of every
    bullish 4H candle; deduplicated then sorted ascending."""
    supports: set[float] = set()
    resistances: set[float] = set()
    for c in candles_4h:
        o, cl = float(c["open"]), float(c["close"])
        if cl < o:
            supports.add(round(cl, 8))
        elif cl > o:
            resistances.add(round(cl, 8))
    return sorted(supports), sorted(resistances)


def scan(
    candles_4h: list[dict],
    candles_15m: list[dict],
    pair: str = "XAUUSD",
    require_4h_touch: bool = True,
    entry_mode: str = "close",                  # "close" | "next-open"
    stop_mode: str = "window-level",          # "window-level" | "window-extreme" | "entry-candle-extreme"
    buffer: float = 0.0,
    price_near_level_pct: float = 0.0,        # >0 enables the known-gap mitigation
    strategy_name: str = "simple-snr-v2",
) -> list[dict]:
    """Return one signal per valid 4H anchor, in time order."""
    if not candles_4h or not candles_15m:
        return []

    h4 = list(candles_4h)
    m15 = list(candles_15m)
    m15_times = [_time_key(_val(b, "open_time", "openTime", "timestamp", "date", "time")) for b in m15]

    signals: list[dict] = []
    seen: set[tuple] = set()

    for anchor in h4:
        a_low = float(anchor["low"])
        a_high = float(anchor["high"])
        a_close = float(anchor["close"])
        anchor_time = _time_key(_val(anchor, "open_time", "openTime", "timestamp", "date", "time"))

        # levels from EARLIER 4H candles only
        earlier_idx = h4.index(anchor) if anchor in h4 else None
        # safer: positional via enumerated parents
        # rebuild slice by position
        _pos = None
        for j, x in enumerate(h4):
            if x is anchor:
                _pos = j
                break
        earlier = h4[:_pos] if _pos is not None else h4
        supports, resistances = find_4h_levels(earlier)

        # --- Step 3: rejection test (support first, then resistance), newest -> oldest ---
        direction = None
        hit_level = None
        for lvl in reversed(supports):
            touch_ok = (not require_4h_touch) or (a_low > 0 and abs(a_low - lvl) / lvl <= NEAR_TOL_PCT)
            if touch_ok and a_close > lvl:
                direction, hit_level = "BUY", lvl
                break
        if direction is None:
            for lvl in reversed(resistances):
                touch_ok = (not require_4h_touch) or (a_high > 0 and abs(a_high - lvl) / lvl <= NEAR_TOL_PCT)
                if touch_ok and a_close < lvl:
                    direction, hit_level = "SELL", lvl
                    break
        if direction is None or hit_level is None:
            continue

        # --- Step 4: 15 most recent closed 15M candles ending at anchor close ---
        if anchor_time is None:
            eligible = list(range(len(m15)))
        else:
            eligible = [i for i, b in enumerate(m15) if (m15_times[i] is None or m15_times[i] <= anchor_time)]
        if not eligible:
            continue
        window = [m15[i] for i in eligible[-WINDOW_15M:]]
        if not window:
            continue
        # search backward for newest bearish (Buy) / bullish (Sell) candle
        mid_level = None
        for b in reversed(window):
            o, cl = float(b["open"]), float(b["close"])
            if direction == "BUY" and cl < o:
                mid_level = cl
                break
            if direction == "SELL" and cl > o:
                mid_level = cl
                break
        if mid_level is None:
            continue

        # --- Step 5: entry from first 15M candle after the anchor ---
        if anchor_time is None:
            after_pos = len(m15)
            next_idx = None
        else:
            cand = [i for i, b in enumerate(m15) if m15_times[i] is not None and m15_times[i] > anchor_time]
            next_idx = cand[0] if cand else None
        if next_idx is None:
            continue  # cannot enter now
        if entry_mode == "close":
            entry_bar = m15[next_idx]
            entry = float(entry_bar["close"])
            entry_time = m15_times[next_idx]
        elif entry_mode == "next-open":
            ei = next_idx + 1
            if ei >= len(m15):
                continue
            entry_bar = m15[ei]
            entry = float(entry_bar["open"])
            entry_time = m15_times[ei]
        else:
            continue

        # known-gap mitigation (optional)
        if price_near_level_pct and mid_level and abs(entry - mid_level) / mid_level > price_near_level_pct:
            continue

        # stop
        if stop_mode == "window-level":
            sl = mid_level - buffer if direction == "BUY" else mid_level + buffer
        elif stop_mode == "window-extreme":
            if direction == "BUY":
                sl = min(float(b["low"]) for b in window) - buffer
            else:
                sl = max(float(b["high"]) for b in window) + buffer
        elif stop_mode == "entry-candle-extreme":
            if entry_mode == "close":
                eb = m15[next_idx]
            else:
                eb = m15[min(next_idx + 1, len(m15) - 1)]
            sl = (float(eb["low"]) - buffer) if direction == "BUY" else (float(eb["high"]) + buffer)
        else:
            continue

        if any(v == 0 for v in (entry, sl, hit_level, mid_level)):
            continue
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        # wrong-side validity check
        if direction == "BUY" and sl >= entry:
            continue
        if direction == "SELL" and sl <= entry:
            continue
        target = (entry + risk * TP_MULTIPLIER) if direction == "BUY" else (entry - risk * TP_MULTIPLIER)

        sig = {
            "pair": pair,
            "direction": direction,
            "anchor_time": anchor_time,
            "4h_level": hit_level,
            "15m_level": mid_level,
            "entry": entry,
            "entry_time": entry_time,
            "stop": sl,
            "target": target,
            "risk_reward": TP_MULTIPLIER,
            "entry_mode": entry_mode,
            "stop_mode": stop_mode,
            "buffer": buffer,
            "strategy": strategy_name,
        }
        # Per spec ("each 4H anchor evaluated independently"), keep at most
        # one signal per anchor time. Dedup only guards against the same 15M
        # entry bar being paired twice with the same direction.
        key = (pair, direction, anchor_time)
        if key in seen:
            continue
        seen.add(key)
        signals.append(sig)

    return signals


if __name__ == "__main__":
    # Self-check: synthetic data that satisfies the spec end-to-end.
    h4 = [
        # earlier 4H (levels built from these)
        {"open": 1.20, "high": 1.21, "low": 1.19, "close": 1.205, "open_time": 1},   # bullish -> resistance 1.205
        {"open": 1.12, "high": 1.125, "low": 1.09, "close": 1.10, "open_time": 2},    # bearish -> support 1.10
        # anchor at t=3: rejection at support 1.10 (low within tol, close above)
        {"open": 1.10, "high": 1.11, "low": 1.0995, "close": 1.105, "open_time": 3},
    ]
    # 15M window must INCLUDE candles ending at/before anchor close (t<=3).
    m15 = [
        {"open": 1.099, "high": 1.101, "low": 1.098, "close": 1.097, "open_time": 1},  # bearish in window
        {"open": 1.10,  "high": 1.101, "low": 1.099, "close": 1.10,  "open_time": 2},  # bearish in window
        {"open": 1.101, "high": 1.11,  "low": 1.104, "close": 1.106, "open_time": 4},  # first AFTER anchor -> entry (next-open)
    ]
    out = scan(h4, m15, pair="EURUSD", entry_mode="close", stop_mode="window-extreme")
    assert len(out) == 1, out
    s = out[0]
    assert s["direction"] == "BUY", s
    assert s["4h_level"] == 1.10
    assert s["15m_level"] == 1.097
    import math
    assert math.isclose(s["entry"], 1.106, rel_tol=1e-6, abs_tol=1e-9), s["entry"]
    # empties safe
    assert scan([], m15) == []
    assert scan(h4, []) == []
    # entry-mode close + extrema stops
    out2 = scan(h4, m15, entry_mode="close", stop_mode="window-level")
    assert len(out2) == 1
    print("simple_snr v2 self-check OK:", s["entry"], s["stop"], s["target"])
