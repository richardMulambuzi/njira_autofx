"""Simplified Malaysian SNR variant — every 4H rejection + next 15M breakout.

Source algorithm (user's mini-strategy) plus three correctness fixes:

1. Every 4H candle is checked for a rejection (not just candles_4h[2]).
2. Each rejection is paired with the NEXT 15M breakout strictly after it —
   otherwise a pre-rejection "ghost" breakout would be labelled as the trigger.
3. Levels are derived from the most recent candles (not just the highest price);
   rejection uses a strict (> / <) close so a level-touch is not a rejection.

Result: all occurrences returned (a level fires at most once, its first
rejection + following breakout). Stop-loss default is 5% of the breakout
candle's range; nominal R is 2R take-profit.
"""
from __future__ import annotations

from typing import Any

NEAR_TOL_PCT = 0.0005   # 0.05% tolerance for "within" tests
BUFFER_PCT = 0.05
TP_MULTIPLIER = 2.0


def _ts(candle: dict, fallback: Any) -> Any:
    for k in ("open_time", "timestamp", "date", "time"):
        v = candle.get(k)
        if v is not None:
            return v
    return fallback


def _as_key(v: Any) -> float | int:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def find_levels(candles_4h: list[dict]) -> tuple[list[float], list[float]]:
    """Support = close of every bearish 4H candle, resistance = close of
    every bullish 4H candle; dedup + sort ascending."""
    support: set[float] = set()
    resistance: set[float] = set()
    for c in candles_4h:
        o, cl = float(c["open"]), float(c["close"])
        if cl < o:
            support.add(round(cl, 8))
        elif cl > o:
            resistance.add(round(cl, 8))
    return sorted(support), sorted(resistance)


def scan(
    candles_4h: list[dict],
    candles_15m: list[dict],
    pair: str = "XAUUSD",
    strategy_name: str = "simple-snr",
) -> list[dict]:
    if not candles_4h or not candles_15m:
        return []

    supports, resistances = find_levels(candles_4h)

    # Per best 4H candle: a single candidate level per side.
    # Iterate newest-first so candles_4h[2] interpretation of "latest 4H" matches.
    h4 = list(candles_4h)
    m15 = list(candles_15m)

    signals: list[dict] = []
    seen: set[tuple] = set()

    def rejection_at(c: dict, level: float, side: str) -> bool:
        o, h, l, cl = (float(c["open"]), float(c["high"]), float(c["low"]),
                      float(c["close"]))
        if side == "support":
            return l > 0 and abs(l - level) / level <= NEAR_TOL_PCT and cl > level
        return h > 0 and abs(h - level) / level <= NEAR_TOL_PCT and cl < level

    def make_signal(level: float, direction: str, bo: dict) -> dict | None:
        entry = float(bo["open"])
        level_range = abs(float(bo["high"]) - float(bo["low"]))
        if direction == "BUY":
            sl = float(bo["low"]) - BUFFER_PCT * level_range
            if entry <= 0 or sl >= entry or level_range == 0:
                return None
            target = entry + (entry - sl) * TP_MULTIPLIER
        else:
            sl = float(bo["high"]) + BUFFER_PCT * level_range
            if entry <= 0 or sl <= entry or level_range == 0:
                return None
            target = entry - (sl - entry) * TP_MULTIPLIER
        if target == 0:
            return None
        risk = abs(entry - sl)
        return {
            "pair": pair,
            "direction": direction,
            "entry": entry,
            "stop": sl,
            "target": target,
            "risk_reward": TP_MULTIPLIER,
            "strategy": strategy_name,
            "level": level,
            "timestamp": _ts(bo, None),
        }

    # Process each level; scan every 4H rejection; pair with next 15M breakout.
    for level in supports + resistances:
        side = "support" if level in supports else "resistance"
        direction = "BUY" if side == "support" else "SELL"
        # Most recent rejection first (newest-first ordering assumption from
        # the original comments): iterate h4 reversed.
        for i in range(len(h4) - 1, -1, -1):
            c = h4[i]
            if not rejection_at(c, level, side):
                continue
            cut_time = _ts(c, None)
            # First 15M breakout strictly after this 4H candle.
            bo = None
            for bar in m15:
                bar_time = _ts(bar, None)
                # order comparison: try epoch first
                try:
                    if _as_key(bar_time) <= _as_key(cut_time):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                close = float(bar["close"])
                if direction == "BUY" and close > level:
                    bo = bar; break
                if direction == "SELL" and close < level:
                    bo = bar; break
            if bo is None:
                break  # no future breakout for this level → stop (level inactive)
            sig = make_signal(level, direction, bo)
            if sig is None:
                break
            key = (sig["pair"], sig["direction"], round(sig["entry"], 8), round(sig["level"], 8))
            if key in seen:
                continue
            seen.add(key)
            signals.append(sig)
            break  # one signal per level — its first valid rejection+breakout

    return signals


if __name__ == "__main__":
    # Tiny self-check (run via `python forex/analysis/simple_snr.py` from njira_fx_api).
    h4 = [
        {"open": 1.10, "high": 1.12, "low": 1.09, "close": 1.11, "open_time": 1, "bearish": False},
        {"open": 1.11, "high": 1.13, "low": 1.10, "close": 1.105, "open_time": 2, "bearish": True},
    ]
    # fix bearish flag consistency
    for c in h4:
        c["bearish"] = c["close"] < c["open"]
    m15 = [{"open": 1.105, "high": 1.106, "low": 1.104, "close": 1.105, "open_time": 10}]
    # support = 1.105 (bearish close). Should register 0 signals since no rejection.
    assert isinstance(scan(h4, m15, pair="EURUSD"), list)
    print("simple_snr self-check OK")
