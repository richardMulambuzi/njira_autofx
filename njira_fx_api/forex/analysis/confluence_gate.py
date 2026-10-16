"""Confluence scoring gate + hard drop filters (winrate plan Phase 2+3).

Pure functions — no I/O, no signal_engine imports (avoids cycles).
Evidence adapter handles the engine's real shapes: floats-as-bools,
nested snr_rejection dict, session as list or string.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("forex.analysis.confluence_gate")

CONFLUENCE_WEIGHTS = {
    "snr_fresh": 2,
    "snr_clean": 1,
    "snr_validation_engulf": 2,
    "snr_validation_pin": 1,
    "snr_rule3_ok": 1,
    "in_kill_zone": 1,
    "london_or_ny_session": 1,
    "rr_2plus": 1,
    "rr_3plus": 1,
    "analyzer_confluence": 3,
    "analyzer_marriage": 1,
    "analyzer_snr": 0,
    "analyzer_breakout": 0,
    "analyzer_engulfing": 0,
    "htf_aligned": 2,
    "htf_neutral": 0,
    "htf_against": -3,
    "quality_gate_passed": 1,
}


def confluence_required() -> int:
    """Minimum score to allow a trade (env CONFLUENCE_REQUIRED, default 6)."""
    try:
        return max(0, int(os.environ.get("CONFLUENCE_REQUIRED", "6").strip()))
    except (TypeError, ValueError, AttributeError):
        return 6


CONFLUENCE_REQUIRED = confluence_required()


def _ev(draft: Any) -> dict:
    return (getattr(draft, "evidence", None) or {}) if draft is not None else {}


def _rej(ev: dict) -> dict:
    rej = ev.get("snr_rejection")
    return rej if isinstance(rej, dict) else {}


def _is_fresh(ev: dict) -> bool:
    rej = _rej(ev)
    for key in ("snr_fresh", "fresh"):
        v = ev.get(key, rej.get(key))
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v:
            return True
    return False


def _validation(ev: dict) -> str:
    """Normalize to 'engulfing' | 'pin_bar' | ''."""
    rej = _rej(ev)
    v = ev.get("snr_validation", rej.get("validation", ""))
    if isinstance(v, str):
        vl = v.strip().lower()
        if "engulf" in vl:
            return "engulfing"
        if "pin" in vl:
            return "pin_bar"
        if vl in ("engulfing", "pin_bar", "book_c2"):
            return "engulfing" if vl != "pin_bar" else "pin_bar"
        return ""
    if isinstance(v, (int, float)):
        if v >= 1.0:
            return "engulfing"
        if v >= 0.5:
            return "pin_bar"
    return ""


def _rule3_ok(ev: dict) -> bool:
    rej = _rej(ev)
    v = ev.get("snr_rule3_ok", rej.get("rule3_ok"))
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def _body_first(ev: dict) -> bool:
    rej = _rej(ev)
    v = ev.get("snr_body_first", rej.get("body_first"))
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def _sessions(ev: dict) -> list[str]:
    s = ev.get("session", ev.get("sessions", ""))
    if isinstance(s, (list, tuple)):
        return [str(x).lower() for x in s]
    if isinstance(s, str) and s:
        return [p.strip().lower() for p in s.replace(";", ",").split(",") if p.strip()]
    return []


def _in_kill_zone(ev: dict) -> bool:
    if ev.get("in_kill_zone") is True:
        return True
    kz = ev.get("killzone", ev.get("killzones", []))
    if isinstance(kz, (list, tuple)) and len(kz) > 0:
        return True
    if isinstance(kz, str) and kz.strip():
        return True
    return False


def score_signal(
    draft: Any, quality_passed: bool, htf_bias: str, session: str = "",
) -> tuple[int, dict]:
    """Score a draft per the plan weights. Returns (score, breakdown)."""
    ev = _ev(draft)
    score = 0
    breakdown: dict = {}

    if _is_fresh(ev):
        score += 2
        breakdown["fresh level"] = 2
    rej = _rej(ev)
    clean = ev.get("snr_clean", rej.get("clean", False))
    if (isinstance(clean, bool) and clean) or (isinstance(clean, (int, float)) and clean > 0):
        score += 1
        breakdown["clean rejection"] = 1
    val = _validation(ev)
    if val == "engulfing":
        score += 2
        breakdown["engulfing validation"] = 2
    elif val == "pin_bar":
        score += 1
        breakdown["pin_bar validation"] = 1
    if _rule3_ok(ev):
        score += 1
        breakdown["rule3 confirmed"] = 1

    if _in_kill_zone(ev):
        score += 1
        breakdown["in kill zone"] = 1
    sess = [s.lower() for s in _sessions(ev)]
    if session and session.strip().lower() not in sess:
        sess = sess + [session.strip().lower()]
    if "london" in sess or "newyork" in sess or "new_york" in sess:
        score += 1
        breakdown["london/ny session"] = 1

    rr = float(getattr(draft, "risk_reward", 0.0) or 0.0)
    if rr >= 3.0:
        score += 2
        breakdown["RR >= 3.0"] = 2
    elif rr >= 2.0:
        score += 1
        breakdown["RR >= 2.0"] = 1

    analyzer = str(ev.get("analyzer", "")).lower()
    if analyzer == "confluence":
        score += 3
        breakdown["confluence analyzer"] = 3
    elif analyzer == "marriage":
        score += 1
        breakdown["marriage analyzer"] = 1
        # Marriage-native evidence: a clean point-#3 rejection is the setup's
        # own validation (it carries no snr_validation field by design).
        p3 = ev.get("marriage_point3")
        if isinstance(p3, dict) and p3.get("clean") is True and "clean rejection" not in breakdown:
            score += 1
            breakdown["clean rejection"] = 1

    direction = str(getattr(draft, "direction", "") or "").lower()
    htf = str(htf_bias or "").strip().lower()
    if htf in ("buy", "bullish", "long"):
        htf = "buy"
    elif htf in ("sell", "bearish", "short"):
        htf = "sell"
    else:
        htf = ""
    if htf and direction:
        if htf == direction:
            score += 2
            breakdown["htf aligned"] = 2
        else:
            score -= 3
            breakdown["htf AGAINST"] = -3

    if quality_passed:
        score += 1
        breakdown["quality gate passed"] = 1

    return score, breakdown


HARD_DROP_RULES = [
    {"name": "no_validation", "reason": "No pin_bar or engulfing pattern — book hard rule"},
    {"name": "counter_trend_stale", "reason": "HTF against direction AND level not fresh"},
    {"name": "rr_too_low", "reason": "RR < 1.5 — below minimum viable"},
    {"name": "asian_low_quality", "reason": "Asian session + not in kill zone + stale level"},
    {"name": "body_first_stale", "reason": "First touch was by body on a stale level"},
]


def _htf_norm(htf_bias: str) -> str:
    h = str(htf_bias or "").strip().lower()
    if h in ("buy", "bullish", "long"):
        return "buy"
    if h in ("sell", "bearish", "short"):
        return "sell"
    return ""


def check_hard_drop(draft: Any, htf_bias: str = "") -> tuple[bool, str, str]:
    """Pre-scoring drops. Returns (dropped, rule_name, reason).

    SNR-evidence rules (no_validation, counter_trend_stale, asian_low_quality,
    body_first_stale) only apply to the core/book ``snr`` analyzer — it is the
    only one that produces snr_validation/freshness evidence. Marriage,
    breakout, flip, qml and confluence drafts carry no such fields; applying
    book rules to them drops every one of their signals (measured: killed all
    15 marriage winners on XAUUSD Oct 2026). Only ``rr_too_low`` is universal.
    """
    ev = _ev(draft)
    direction = str(getattr(draft, "direction", "") or "").lower()
    htf = _htf_norm(htf_bias)
    rr = float(getattr(draft, "risk_reward", 0.0) or 0.0)
    snr_like = str(ev.get("analyzer", "")).lower() == "snr"

    if snr_like and not _validation(ev):
        return True, "no_validation", HARD_DROP_RULES[0]["reason"]
    if htf and direction and htf != direction and snr_like and not _is_fresh(ev):
        return True, "counter_trend_stale", HARD_DROP_RULES[1]["reason"]
    if rr < 1.5:
        return True, "rr_too_low", HARD_DROP_RULES[2]["reason"]
    if not snr_like:
        return False, "", ""
    sess = _sessions(ev)
    if "asian" in sess and not _in_kill_zone(ev) and not _is_fresh(ev):
        return True, "asian_low_quality", HARD_DROP_RULES[3]["reason"]
    if _body_first(ev) and not _is_fresh(ev):
        return True, "body_first_stale", HARD_DROP_RULES[4]["reason"]
    return False, "", ""
