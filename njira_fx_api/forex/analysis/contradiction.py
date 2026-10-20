"""
Contradiction Engine (m.md §37–38).

The system must actively search for reasons NOT to trade. A setup with strong
contradictions should become WAIT rather than BUY/SELL. This is a v1 set of
independent heuristic checks — each check returns None or a result dict, and
the engine aggregates them into a ContradictionReport.

Best-effort: every check is defensive and never raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ContradictionReport:
    """Analysis of contradictions in a potential setup."""
    supporting: dict[str, float]    # evidence for the trade (name → weight)
    contradicting: dict[str, float] # evidence against the trade (name → weight)
    net_confidence: float           # supporting - contradicting
    contradiction_count: int        # number of contradictions found
    severity: str                   # "NONE", "MILD", "MODERATE", "SEVERE"
    recommendation: str             # "TRADE", "WAIT", "INVALIDATE"
    reasons: list[str]              # human-readable contradiction reasons

    def to_dict(self) -> dict:
        """Serialize for storage in signal.confidence_evidence."""
        return {
            "supporting": self.supporting,
            "contradicting": self.contradicting,
            "net_confidence": self.net_confidence,
            "contradiction_count": self.contradiction_count,
            "severity": self.severity,
            "recommendation": self.recommendation,
            "reasons": self.reasons,
        }


class ContradictionEngine:
    def __init__(self):
        self._checks: list[callable] = [
            self._check_htf_ltf_alignment,
            self._check_spread_condition,
            self._check_displacement,
            self._check_fvg_validity,
            self._check_ob_validity,
            self._check_session_timing,
            self._check_volatility_regime,
            self._check_liquidity_context,
            self._check_profile_position,
            self._check_profile_entropy,
            self._check_profile_reliability,
            self._check_profile_migration,
            # ── Structural checks (improve.md §41) ──
            self._check_daily_high_proximity,
            self._check_untouched_liquidity,
            self._check_retest_break,
        ]

    def analyze(
        self,
        symbol: str,
        direction: str,  # "BUY" or "SELL"
        evidence: dict[str, float],
        market_state: dict,
    ) -> ContradictionReport:
        """
        Analyze a potential setup for contradictions.

        Args:
            symbol: trading symbol
            direction: proposed trade direction
            evidence: dict of supporting evidence from signal engine
            market_state: dict containing:
                - htf_bias: current higher-timeframe bias
                - ltf_bias: current lower-timeframe bias
                - spread_regime: current spread condition
                - has_displacement: whether displacement was detected
                - has_fvg: whether FVG exists
                - fvg_status: FVG lifecycle state
                - has_ob: whether order block exists
                - ob_status: OB lifecycle state
                - session: current market session
                - volatility_regime: current volatility
                - has_liquidity_sweep: whether sweep occurred
        """
        # Seed supporting evidence from the signal engine's own evidence.
        # (The contradiction checks only ever emit "contradict" results, so
        # without this the net confidence would always be <= 0 and every
        # setup would collapse to WAIT. The `evidence` arg is documented as
        # the supporting evidence, so we treat positive weights as support.)
        supporting: dict[str, float] = {
            k: float(v) for k, v in (evidence or {}).items()
            if isinstance(v, (int, float)) and v > 0
        }
        # MTF alignment evidence is a dict (per-timeframe breakdown), not a
        # float, so it previously contributed ZERO support and no signal could
        # ever reach TRADE. Convert aligned timeframes into real support weight.
        mtf = (evidence or {}).get("mtf_breakdown")
        if isinstance(mtf, dict):
            aligned = 0
            for tf_val in mtf.values():
                tv = str(tf_val).upper()
                if direction == "BUY" and tv in ("BUY", "BULLISH"):
                    aligned += 1
                elif direction == "SELL" and tv in ("SELL", "BEARISH"):
                    aligned += 1
            if aligned > 0:
                supporting["mtf_alignment"] = float(aligned) * 5.0
        contradicting: dict[str, float] = {}
        reasons: list[str] = []

        for check in self._checks:
            try:
                result = check(direction, evidence, market_state)
            except Exception:  # best-effort: never let a check break analysis
                result = None
            if result:
                if result["type"] == "support":
                    supporting[result["name"]] = result["weight"]
                elif result["type"] == "contradict":
                    contradicting[result["name"]] = result["weight"]
                    reasons.append(result["reason"])

        # Calculate net confidence
        total_support = sum(supporting.values())
        total_contradict = sum(contradicting.values())
        net = total_support - total_contradict

        # Determine severity
        if len(contradicting) == 0:
            severity = "NONE"
        elif len(contradicting) == 1:
            severity = "MILD"
        elif len(contradicting) <= 2:
            severity = "MODERATE"
        else:
            severity = "SEVERE"

        # Determine recommendation
        if severity == "SEVERE" or net < 20:
            recommendation = "WAIT"
        elif severity == "MODERATE" and net < 40:
            recommendation = "WAIT"
        elif severity == "MILD":
            recommendation = "TRADE"  # mild contradictions are acceptable
        else:
            recommendation = "TRADE"

        if recommendation == "WAIT":
            logger.info(
                "[CONTRA] %s %s WAIT sev=%s net=%.0f contradict=%s",
                symbol,
                direction,
                severity,
                net,
                ", ".join(
                    "%s:%g" % (name, weight)
                    for name, weight in sorted(contradicting.items(), key=lambda kv: -kv[1])
                ) or "none",
            )

        return ContradictionReport(
            supporting=supporting,
            contradicting=contradicting,
            net_confidence=net,
            contradiction_count=len(contradicting),
            severity=severity,
            recommendation=recommendation,
            reasons=reasons,
        )

    def _check_htf_ltf_alignment(self, direction, evidence, state):
        """HTF bullish + LTF bearish = contradiction."""
        htf = state.get("htf_bias", "")
        ltf = state.get("ltf_bias", "")
        if direction == "BUY" and htf == "bullish" and ltf == "bearish":
            return {
                "type": "contradict",
                "name": "htf_ltf_conflict",
                "weight": 15,
                "reason": "HTF bullish but LTF bearish — timeframe conflict"
            }
        if direction == "SELL" and htf == "bearish" and ltf == "bullish":
            return {
                "type": "contradict",
                "name": "htf_ltf_conflict",
                "weight": 15,
                "reason": "HTF bearish but LTF bullish — timeframe conflict"
            }
        return None

    def _check_spread_condition(self, direction, evidence, state):
        """Wide spread = contradiction."""
        spread = state.get("spread_regime", "NORMAL")
        if spread in ("WIDE", "EXTREME"):
            return {
                "type": "contradict",
                "name": "spread_abnormal",
                "weight": 8,
                "reason": f"Spread {spread} — execution risk elevated"
            }
        return None

    def _check_displacement(self, direction, evidence, state):
        """No displacement after liquidity sweep = contradiction."""
        has_sweep = state.get("has_liquidity_sweep", False)
        has_displacement = state.get("has_displacement", False)
        if has_sweep and not has_displacement:
            return {
                "type": "contradict",
                "name": "no_displacement",
                "weight": 10,
                "reason": "Liquidity swept but no displacement — weak confirmation"
            }
        return None

    def _check_fvg_validity(self, direction, evidence, state):
        """FVG exists but is filled/invalidated = contradiction."""
        has_fvg = state.get("has_fvg", False)
        fvg_status = state.get("fvg_status", "")
        if has_fvg and fvg_status in ("FILLED", "INVALIDATED"):
            return {
                "type": "contradict",
                "name": "fvg_invalid",
                "weight": 5,
                "reason": f"FVG is {fvg_status} — no valid imbalance"
            }
        return None

    def _check_ob_validity(self, direction, evidence, state):
        """OB exists but is mitigated/expired = contradiction."""
        has_ob = state.get("has_ob", False)
        ob_status = state.get("ob_status", "")
        if has_ob and ob_status in ("MITIGATED", "INVALIDATED", "EXPIRED"):
            return {
                "type": "contradict",
                "name": "ob_invalid",
                "weight": 8,
                "reason": f"Order block is {ob_status} — zone consumed"
            }
        return None

    def _check_session_timing(self, direction, evidence, state):
        """Trading in off-session = contradiction."""
        session = state.get("session", "")
        if session == "OFF_SESSION":
            return {
                "type": "contradict",
                "name": "off_session",
                "weight": 5,
                "reason": "Market in off-session — low liquidity"
            }
        return None

    def _check_volatility_regime(self, direction, evidence, state):
        """Extreme volatility = contradiction."""
        vol = state.get("volatility_regime", "NORMAL")
        if vol == "EXTREME":
            return {
                "type": "contradict",
                "name": "extreme_volatility",
                "weight": 10,
                "reason": "Volatility EXTREME — unpredictable price action"
            }
        return None

    def _check_liquidity_context(self, direction, evidence, state):
        """No liquidity interaction = mild contradiction (less confirmation).

        Only applies to signals whose strategy actually evaluates liquidity
        (the confluence engine). The MTF strategy does not analyse liquidity
        pools, so it must not be penalised for a missing sweep — otherwise
        every MTF signal collapses to WAIT.
        """
        if "mtf_breakdown" in (evidence or {}):
            return None
        has_sweep = state.get("has_liquidity_sweep", False)
        if not has_sweep:
            return {
                "type": "contradict",
                "name": "no_liquidity_sweep",
                "weight": 3,
                "reason": "No liquidity sweep detected — setup less confirmed"
            }
        return None

    def _check_profile_position(self, direction, evidence, state):
        """Price outside value area = chasing extended move."""
        try:
            pos = state.get("profile_position", "")
            reliability = float(state.get("profile_reliability", 1.0))
            if direction == "BUY" and pos == "above_vah":
                return {
                    "type": "contradict",
                    "name": "profile_chasing_high",
                    "weight": 12,
                    "reason": "Price above value area high — chasing extended move"
                }
            if direction == "SELL" and pos == "below_val":
                return {
                    "type": "contradict",
                    "name": "profile_chasing_low",
                    "weight": 12,
                    "reason": "Price below value area low — chasing extended move"
                }
            if pos in ("above_vah", "below_val") and reliability < 0.5:
                return {
                    "type": "contradict",
                    "name": "profile_unreliable_extended",
                    "weight": 15,
                    "reason": "Extended price with unreliable profile — high risk"
                }
        except Exception:
            return None
        return None

    def _check_profile_entropy(self, direction, evidence, state):
        """Diffuse profile = no clear value structure."""
        try:
            entropy = float(state.get("profile_entropy", 0.0))
            if entropy > 0.85:
                return {
                    "type": "contradict",
                    "name": "profile_diffuse",
                    "weight": 8,
                    "reason": "Profile diffuse (entropy high) — no clear value structure"
                }
        except Exception:
            return None
        return None

    def _check_profile_reliability(self, direction, evidence, state):
        """Low profile reliability = weak evidence."""
        try:
            reliability = float(state.get("profile_reliability", 1.0))
            if reliability < 0.3:
                return {
                    "type": "contradict",
                    "name": "profile_low_reliability",
                    "weight": 6,
                    "reason": "Profile reliability low — insufficient data"
                }
        except Exception:
            return None
        return None

    def _check_profile_migration(self, direction, evidence, state):
        """Trading against value migration = counter-trend."""
        try:
            migration = state.get("value_migration", "unknown")
            if direction == "SELL" and migration == "migrating_up":
                return {
                    "type": "contradict",
                    "name": "profile_counter_migration_up",
                    "weight": 10,
                    "reason": "Value migrating up — counter-trend sell"
                }
            if direction == "BUY" and migration == "migrating_down":
                return {
                    "type": "contradict",
                    "name": "profile_counter_migration_down",
                    "weight": 10,
                    "reason": "Value migrating down — counter-trend buy"
                }
        except Exception:
            return None
        return None

    def _check_daily_high_proximity(self, direction, evidence, state):
        """Price hugging the daily extreme = trading into known resistance/support.

        BUY near daily high (within 0.15% below) or SELL near daily low (within
        0.15% above) means the setup is pressing into the day's liquidity extreme
        — a structural reason NOT to enter (improve.md §41).
        """
        price = state.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            return None
        try:
            if direction == "BUY":
                dh = state.get("daily_high", 0.0) or 0.0
                if dh > 0 and price <= dh and (dh - price) / dh <= 0.0015:
                    return {
                        "type": "contradict",
                        "name": "daily_high_proximity",
                        "weight": 12,
                        "reason": "Near daily high — resistance"
                    }
            else:  # SELL
                dl = state.get("daily_low", 0.0) or 0.0
                if dl > 0 and price >= dl and (price - dl) / dl <= 0.0015:
                    return {
                        "type": "contradict",
                        "name": "daily_low_proximity",
                        "weight": 12,
                        "reason": "Near daily low — support"
                    }
        except Exception:
            return None
        return None

    def _check_untouched_liquidity(self, direction, evidence, state):
        """Untouched liquidity pool on the trade side = ungrabbed target.

        BUY with an untouched (state != SWEPT) high-type pool ABOVE price means
        sell-side liquidity still sits above — price would have to take it out.
        SELL with an untouched low-type pool BELOW price is the mirror image
        (improve.md §41). Pool types come from LiquidityType (liquidity.py:22):
        high pools end in "HIGH", low pools end in "LOW".
        """
        price = state.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            return None
        pools = state.get("liquidity_levels") or []
        if not pools:
            return None
        try:
            if direction == "BUY":
                for p in pools:
                    if (str(p.get("pool_type", "")).endswith("HIGH")
                            and str(p.get("state", "")) != "SWEPT"
                            and isinstance(p.get("price"), (int, float))
                            and p["price"] > price):
                        return {
                            "type": "contradict",
                            "name": "untouched_liquidity_above",
                            "weight": 10,
                            "reason": "Untouched sell-side liquidity above"
                        }
            else:  # SELL
                for p in pools:
                    if (str(p.get("pool_type", "")).endswith("LOW")
                            and str(p.get("state", "")) != "SWEPT"
                            and isinstance(p.get("price"), (int, float))
                            and p["price"] < price):
                        return {
                            "type": "contradict",
                            "name": "untouched_liquidity_below",
                            "weight": 10,
                            "reason": "Untouched buy-side liquidity below"
                        }
        except Exception:
            return None
        return None

    def _check_retest_break(self, direction, evidence, state):
        """A retest that was ACCEPTED means price broke through the zone.

        Retest evidence arrives via ``evidence["ob_retest"]`` /
        ``evidence["fvg_retest"]`` (RetestEvent.to_dict, retest.py:49). An
        ACCEPTED reaction means the level failed — a structural contradiction
        to entering in that direction (improve.md §41). Direction-agnostic:
        any ACCEPTED retest invalidates the setup.
        """
        if not evidence:
            return None
        retests = []
        retests.extend(evidence.get("ob_retest") or [])
        retests.extend(evidence.get("fvg_retest") or [])
        if not retests:
            return None
        try:
            for r in retests:
                if isinstance(r, dict) and str(r.get("reaction", "")).upper() == "ACCEPTED":
                    return {
                        "type": "contradict",
                        "name": "retest_accepted",
                        "weight": 12,
                        "reason": "Retest ACCEPTED — zone failed"
                    }
        except Exception:
            return None
        return None
