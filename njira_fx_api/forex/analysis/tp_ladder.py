"""Take-profit ladder for a confirmed setup.

One setup gets one card and one email carrying every rung, so the same signal is
never mailed twice. Rungs are pure geometry off the stop: TP1 = 1R, TP7 = 5R.

Hit chances use the first-passage result for a driftless walk: the chance of
touching +kR before -1R is 1/(1+k). It is a shape, not a prophecy. Real edge
comes from the setup, not from the ladder; scaling out spreads the outcome.
"""
from __future__ import annotations

from typing import Optional

# R multiple of each rung, nearest first.
TP_LADDER_R = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0)

# Notes keyed by the rung index (1-based).
_NOTES = {
    1: "Safest. Price only has to move one risk unit; most likely to fill.",
    2: "Still cautious. A short continuation past the first rung.",
    3: "The standard target this engine already uses. Balanced.",
    4: "Needs a real move, no chop on the way.",
    5: "A trend day. Only take it if the higher timeframe agrees.",
    6: "Covers wide ground. Expect it to miss more often than it hits.",
    7: "Covers the most ground and is the least likely to be reached.",
}


def digits_for(price: float) -> int:
    """Quote precision by price magnitude, matching the backtest parser."""
    price = abs(float(price))
    if price > 1000:
        return 2
    if price > 100:
        return 3
    return 5


def build_tp_ladder(
    direction: str,
    entry: float,
    stop_loss: float,
    digits: Optional[int] = None,
    beyond: Optional[float] = None,
    pip_size: Optional[float] = None,
) -> list[dict]:
    """Rungs from entry to 5R, nearest first.

    ``beyond`` is the next fresh level or structural swing the setup has to
    clear. Rungs past it are flagged ``beyond_structure`` so the reader can see
    the rung depends on breaking a known level, not just on momentum.
    """
    entry = float(entry)
    stop_loss = float(stop_loss)
    buy = str(direction).upper() == "BUY"
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return []
    if digits is None:
        digits = digits_for(entry)
    # A pip is a property of the instrument, not of the price magnitude: oil at
    # 99 and EURUSD at 1.12 do not share a pip. Callers that know the symbol
    # pass its real size; the magnitude guess stays as the fallback.
    pip = float(pip_size) if pip_size else 10.0 ** (1 - digits)

    ladder: list[dict] = []
    for i, k in enumerate(TP_LADDER_R, start=1):
        price = entry + risk * k if buy else entry - risk * k
        if k <= 1:
            label = "most likely"
        elif k <= 2:
            label = "likely"
        elif k <= 3:
            label = "needs a run"
        else:
            label = "long shot"
        ladder.append({
            "name": f"TP{i}",
            "index": i,
            "r_multiple": k,
            "price": round(price, digits),
            "distance": abs(price - entry),
            "distance_pips": abs(price - entry) / pip,
            "hit_chance": 1.0 / (1.0 + k),
            "label": label,
            "note": _NOTES.get(i, ""),
            "beyond_structure": bool(
                beyond is not None and (
                    float(beyond) < price if buy else float(beyond) > price
                )
            ),
        })
    return ladder


def ladder_summary(ladder: list[dict]) -> str:
    """One line naming the safest, balanced and widest rungs."""
    if not ladder:
        return ""
    safest, balanced, widest = ladder[0], ladder[min(2, len(ladder) - 1)], ladder[-1]
    return (
        f"Safest {safest['name']} ({safest['hit_chance'] * 100:.0f}% reach), "
        f"balanced {balanced['name']} ({balanced['hit_chance'] * 100:.0f}%), "
        f"widest {widest['name']} ({widest['hit_chance'] * 100:.0f}%). "
        "Taking the widest rung alone rejects most of the setups that would "
        "have paid at the nearer rungs."
    )
