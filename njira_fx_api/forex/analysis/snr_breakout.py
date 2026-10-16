"""
Malaysian SNR Step3-4: 15M box breakout + entry/SL/TP (runs alongside marriage).

Step3 — the 10x15M candles BEFORE the 4H validation form the local immediate
support/resistance as a body-close cluster: ``sup = min(closes)``,
``res = max(closes)`` over those 10 bars.

Step4 — breakout = the first 15M candle whose BODY closes beyond the box in the
SAME direction as the 4H rejection (BUY: close > res, SELL: close < sup — the
direction-alignment gate).  Entry is the next 15M open, SL sits beyond the
breakout candle's extreme + buffer (floored at the engine's minimum stop so a
sub-pip wick stop is never traded), TP is the next fresh SNR level or 2R.
RR shaping (sim/rr.py dynamic RR) and the MIN_RR 0.5 floor stay downstream in
the shared pipeline (``_apply_strength_rr`` + polisher) — this module only
produces the draft.

Pure helpers plus one driver.  The driver takes the engine as a collaborator so
all new logic lives here; ``SignalEngine._malaysian_candidates`` only calls it
(the integration point).  Engine constants/funnel access goes through a lazy
import to avoid a hard circular import (signal_engine imports this module).
"""
from __future__ import annotations

from typing import Optional

#: 15M bars forming the local box before the 4H validation.
BREAKOUT_BOX_BARS = 10
#: SL buffer beyond the breakout candle's extreme (fraction of price).
BREAKOUT_BUFFER_PCT = 0.0005
#: Take profit when no fresh SNR sits beyond the entry (book rule 1 fallback).
BREAKOUT_FALLBACK_RR = 2.0
#: Breakout-candle body/range fraction earning the strong-body bonus.
BREAKOUT_BODY_FRACTION = 0.5


def local_box(closes: list[float], lookback: int = BREAKOUT_BOX_BARS) -> tuple[float, float]:
    """
    Local immediate support/resistance from body closes.

    ``(sup, res) = (min, max)`` of the last ``lookback`` closes — the cluster
    the 10x15M box compresses into.  Needs at least one close.
    """
    window = [float(c) for c in closes[-lookback:]]
    if not window:
        raise ValueError("local_box needs at least one close")
    return min(window), max(window)


def find_body_breakout(
    candles: list[dict], start_index: int, sup: float, res: float, direction: str,
) -> Optional[int]:
    """
    First index >= ``start_index`` whose BODY closes beyond the box.

    BUY breaks up (close > res), SELL breaks down (close < sup) — wicks never
    count.  Returns ``None`` when no candle breaks the box.
    """
    is_buy = direction.upper() == "BUY"
    for j in range(max(0, start_index), len(candles)):
        close = float(candles[j]["close"])
        if (is_buy and close > res) or (not is_buy and close < sup):
            return j
    return None


def _draft_from_rejection(engine, se, symbol: str, m15: list[dict],
                          rej: dict, direction: str):
    """One breakout draft from a book-validated 4H rejection, or None."""
    rej_time = engine._epoch(rej.get("open_time"))
    if rej_time is None:
        return None
    # First 15M candle at/after the 4H validation; the box is the 10 bars before it.
    val_index = next(
        (i for i, c in enumerate(m15) if float(c["open_time"]) >= rej_time), None,
    )
    if val_index is None or val_index < BREAKOUT_BOX_BARS:
        return None
    sup, res = local_box([float(c["close"]) for c in m15[val_index - BREAKOUT_BOX_BARS:val_index]])
    se.bump_funnel(symbol, "breakout_boxes_found")

    # Direction-alignment gate: only scan the 4H rejection's own side.
    bo = find_body_breakout(m15, val_index, sup, res, direction)
    if bo is None or bo + 1 >= len(m15):
        return None
    # Live edge — a fill too far back is a stale price, not a trade.
    if bo + 1 < len(m15) - se.SNR_BREAKOUT_MAX_AGE:
        return None

    is_buy = direction == "BUY"
    entry = float(m15[bo + 1]["open"])  # fill at next bar's open when breakout candle closes
    extreme = float(m15[bo]["low"]) if is_buy else float(m15[bo]["high"])
    sl = extreme - abs(extreme) * BREAKOUT_BUFFER_PCT if is_buy \
        else extreme + abs(extreme) * BREAKOUT_BUFFER_PCT
    # Minimum stop floor — a wick hugging the entry is noise, not structure
    # (same rule as the marriage point-#3 stop).
    if abs(entry - sl) < abs(entry) * se.SNR_MIN_STOP_PIPS * 1e-4:
        adj = abs(entry) * se.SNR_MIN_STOP_PIPS * 1e-4
        sl = entry - adj if is_buy else entry + adj
    if (is_buy and sl >= entry) or (not is_buy and sl <= entry):
        return None
    risk = abs(entry - sl)

    entry_time = float(m15[bo + 1]["open_time"])
    if engine._snr_stale(entry_time, m15):
        se.bump_funnel(symbol, "stale_dropped")
        return None

    # Book rule 1 (strict): next fresh SNR beyond the entry; 2R when none sits there.
    tp_result = engine._snr_take_profit(m15, direction, entry, sl, m15[bo + 1], bo + 1)
    if tp_result is not None:
        tp, tp_source, tp_level = tp_result
    else:
        tp = entry + risk * BREAKOUT_FALLBACK_RR if is_buy else entry - risk * BREAKOUT_FALLBACK_RR
        tp_source, tp_level = "2R", None

    bc = m15[bo]
    b_range = float(bc["high"]) - float(bc["low"])
    b_body = abs(float(bc["close"]) - float(bc["open"]))
    bonus = se.MALAYSIAN_CONF_BREAKOUT_BODY \
        if b_range > 0 and b_body / b_range > BREAKOUT_BODY_FRACTION else 0.0

    draft = se.SignalDraft(direction=direction, entry=entry, stop_loss=sl, take_profit=tp)
    draft.confidence = round(min(100.0, se.MALAYSIAN_CONF_BASE + bonus), 1)
    draft.risk_reward = round(abs(tp - entry) / risk, 2)
    draft.entry_time = entry_time
    trendline = rej.get("trendline") or {}
    draft.evidence = {
        "analyzer": "breakout",
        "breakout_box": 1.0,
        "breakout_body": 1.0,
        "breakout_risk_reward": draft.risk_reward,
        "snr_breakout_body": bonus,
        "snr_tp_source": tp_source,
        "snr_tp_r": draft.risk_reward,
        "snr_tp_level": ({"kind": tp_level["kind"], "y1": tp_level["y1"], "y2": tp_level["y2"],
                           "price": tp, "level_price": tp_level.get("level_price", tp)}
                          if tp_level is not None else None),
        # 4H anchors so lifecycle dedup can tell setups apart (same scheme as snr).
        "snr_4h_level": {"kind": trendline.get("kind", "support" if is_buy else "resistance"),
                         "t1": trendline.get("t1"), "y1": trendline.get("y1"),
                         "t2": trendline.get("t2"), "y2": trendline.get("y2"),
                         "price": rej.get("price"), "candle_index": rej.get("candle_index")},
        "breakout_box_levels": {"sup": sup, "res": res, "bars": BREAKOUT_BOX_BARS,
                                "validation_open_time": rej_time, "breakout_index": bo},
        "entry_kind": "box_breakout",
        "entry_index": bo + 1,
    }
    draft.explanation_parts = [
        f"4H {direction} rejection (book-validated) — the 4H direction",
        f"15M box from the 10 bars before validation: sup {sup:.5f} / res {res:.5f}",
        f"15M body-close breakout {'above res' if is_buy else 'below sup'} "
        f"(15M candle {bo}) — aligned with the 4H {direction}",
        f"entry at next 15M open {entry:.5f} (candle {bo + 1}), "
        f"SL {sl:.5f} beyond the breakout extreme + buffer",
        f"TP {tp:.5f} ({'next fresh SNR level' if tp_source == 'level' else '2R fallback'})",
    ]
    engine._trend_tag(symbol, draft)
    se.bump_funnel(symbol, "breakout_signals")
    return draft


def analyze_breakout(engine, symbol: str):
    """
    Step3-4 breakout analyzer → SignalDraft (best side) or None.

    Reuses the engine's book-validated 4H rejections (Step1/2 output) as the 4H
    direction, then builds the 15M box + body-close breakout per side.  Mirrors
    the marriage analyzer's freshest-structure-first ordering.
    """
    from forex.analysis import signal_engine as se  # lazy: caller imports this module

    raw_h4 = engine._cache.get(symbol, se.H4_TF_SECONDS)
    raw_m15 = engine._cache.get(symbol, se.M15_TF_SECONDS)
    if not raw_h4 or len(raw_h4) < 8 or not raw_m15 or len(raw_m15) < BREAKOUT_BOX_BARS + 2:
        return None
    m15 = engine._module_candles(raw_m15)

    orders = sorted(
        (engine._snr_levels(raw_h4, kind) for kind in ("support", "resistance")),
        key=lambda levels: levels[-1]["t2"] if levels else float("-inf"),
        reverse=True,
    )
    best = None
    for levels in orders:
        if not levels:
            continue
        kind = levels[-1]["kind"]
        direction = "BUY" if kind == "support" else "SELL"
        level = engine._snr_tradable_trendline(raw_h4, kind)
        if level is None:
            continue
        rej = engine._snr_check_rejection(raw_h4, level, direction)
        if rej is None:
            continue
        dropped = engine._snr_book_gate(symbol, rej)
        if dropped:
            se.bump_funnel(symbol, dropped)
            continue
        draft = _draft_from_rejection(engine, se, symbol, m15, rej, direction)
        if draft is not None and (best is None or draft.confidence > best.confidence):
            best = draft
    return best
