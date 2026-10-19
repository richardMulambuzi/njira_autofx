"""
Signal Generation Engine (Phase 4 — improvements.md §31–40).

Orchestrates all intelligence engines (structure, liquidity, OB, POC,
microstructure) to produce BUY / SELL / NO_SIGNAL with transparent
confidence scoring and human-readable explanations.

Multi-timeframe confluence: 1D bias → 4H OB/liquidity → 15M structure
→ 5M entry → 1M trigger.  Higher-TF conflicts reduce confidence.

Runs on every candle close event, not on throttled Pusher stream.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from forex.analysis.market_structure import MarketStructureEngine, Bias, MarketRegime
from forex.analysis.liquidity import LiquidityEngine, PoolState
from forex.analysis.order_blocks import OrderBlockEngine, OBDirection, OBState
from forex.analysis.fvg import FVGEngine, FVGState
from forex.analysis.poc import POCEngine
from forex.analysis.microstructure import MicrostructureEngine, MicroMetrics
from forex.analysis.order_flow import OrderFlowEngine
from forex.analysis.spread import SpreadEngine, SpreadMetrics
from forex.analysis.volatility import VolatilityEngine
from forex.analysis.session import SessionEngine
from forex.analysis.context import MarketContextEngine
from forex.analysis.signal_lifecycle import (
    SignalLifecycleManager, Signal, SignalState,
)
from forex.analysis.contradiction import ContradictionEngine, ContradictionReport
from forex.analysis.cache import HotCache
from forex.analysis.retest import RetestEngine
from forex.analysis.quality_gate import QualityGate
from forex.analysis.polisher import SignalPolisher
from forex.analysis.strategy import StrategyRegistry, StrategyReport
from forex.analysis.ict_strategies import (  # import side effect registers the ict_* strategies
    confidence_of as ict_confidence_of,
    direction_of as ict_direction_of,
    is_actionable as ict_is_actionable,
    levels_of as ict_levels_of,
    payload_of as ict_payload_of,
    zones_of as ict_zones_of,
)
from forex.analysis.metrics import METRICS
from forex.analysis.latency import measure_latency
# Malaysian SNR course modules — pure functions, no engine imports.
from forex.analysis.engulfing import (
    detect_gap, detect_engulfing, engulfing_entry, find_engulfing_in_gap,
)
from forex.analysis.storyline import analyze_storyline, storyline_allows, tf_direction
from forex.analysis.sessions import in_trade_session, session_of, killzones_of
from forex.sim.rr import RRService, StrategyType, Regime
from forex.analysis.marriage import entry_at_point3, marriage_trendline, line_value
from forex.analysis.snr_breakout import analyze_breakout

logger = logging.getLogger("forex.analysis.signal_engine")


# ── Default scoring weights (configurable) ─────────────────────────────
@dataclass
class ScoringWeights:
    htf_bias: float = 18
    ob_quality: float = 16
    liquidity_sweep: float = 15
    mss: float = 12
    displacement: float = 10
    fvg_imbalance: float = 10
    poc_interaction: float = 7
    entry_confirmation: float = 8
    spread: float = 2
    data_quality: float = 1
    session_sweep: float = 8
    volume_profile_interaction: float = 8


DEFAULT_SCORING = ScoringWeights()
MIN_CONFIDENCE_TO_CONFIRM = 60.0
# Hard floor below which a setup has essentially no evidence and is genuine
# noise. Setups above this floor are EMITTED with their natural 5-tier strength
# (WEAK/RISKY) instead of being dropped (see _evaluate_confluence).
MIN_CONFIDENCE_HARD_FLOOR = 20.0
# Minimum acceptable risk:reward. Kept low (0.5) so counter-trend/risky fades
# that target a modest pull-back are still allowed to emit.
MIN_RR_RATIO = 0.5

# ── Malaysian SNR (Support & Resistance) strategy constants ──────────────
# Design decisions (bdf document): S/R levels come from CLOSING prices only,
# rejections are checked on the 3rd candle after the level-forming candle, and
# the 15M breakout must align with the 4H rejection direction.
# Note: closes within 0.1% of each other form ONE level whose price is the most
# recent close — which reduces to "the most recent qualifying close" when only
# the most recent level per side is kept (spec design §1).
SNR_TOUCH_PCT = 0.0005     # 0.05% tolerance on candle-low/high touching a level
SNR_CLEAN_PCT = 0.001      # clean rejection = close beyond level by > 0.1%
# Book p.20 / PDF Step 2: rejection (validation) candle = level index + 2 (c+2).
# Both the core flow (_snr_check_rejection) and the book-exact flow
# (_snr_book_validation) use the same offset so they agree with the source.
SNR_REJECTION_OFFSET = 2
SNR_BOOK_VALIDATION_OFFSET = SNR_REJECTION_OFFSET   # kept for back-compat imports
SNR_BOOK_MAX_SCAN = 10  # max candles to scan forward for rejection validation

# How many SNR levels to scan when looking for (a) the level to trade or (b) the
# next level to take profit at.  Book p.67: "Always look for fresh SNR level at
# M15 and above"; p.67 table FRESH SNR → BREAKOUT → TAKE PROFIT.
TP_LEVEL_LOOKBACK = 3
# An SNR level only trades while its N+3 rejection still sits inside the last
# ``SNR_H4_RECENT_BARS`` closed 4H candles — the same recency _evaluate_snr
# enforces.  Freshness therefore only ever breaks ties between LIVE levels, it
# can never hand a stale (and therefore untradable) level to the analyzer.
SNR_H4_RECENT_BARS = 2
# Minimum stop distance in pips.  A book stop sits below the structural lower
# low / above the higher high, which on M15 is comfortably wider than one
# candle's noise (a typical M15 bar moves 8–15 pips).  A retest-candle wick gave
# sub-pip "stops" that were taken out by ordinary noise, so clamp to a floor.
SNR_MIN_STOP_PIPS = 10.0
# A retest (QML peak / flipped-SNR retest) must be happening at the LIVE EDGE
# for the setup to be tradeable.  If the retest is older than this many 15M
# candles, the entry price is stale — on fast markets (gold) it was 30–46 pips
# from the current price, a fabricated fill.  2 bars ≈ 30 minutes.
SNR_RETEST_MAX_AGE = 2
# Same live-edge requirement for the core SNR's 15M breakout: the breakout close
# must be within this many 15M candles of the current bar, else the entry is a
# stale price (was up to 46 pips off the market on gold).
SNR_BREAKOUT_MAX_AGE = 4
# How many recent candles the level/trendline scans walk back through. Bounds the
# per-bar analyzer cost; a level older than this is not a live-edge setup.
SNR_LEVEL_SCAN = 120
# Realistic target cap: 1:2 risk:reward.  The book's "next fresh SNR" is often
# 9–21R away on M15, which is not a realistic target (never hit → win rate
# collapses).  Cap the reward at 2R so trades are tradable; a closer fresh level
# is still used as-is.
SNR_MAX_RR = 3.0
# Realistic floor for the strength-derived dynamic RR (trade.md §3): even a
# marginal valid setup targets ~1.5R; a strong confluence one runs to SNR_MAX_RR.
SNR_MIN_RR = 1.5
# How many recent 4H candles to scan for structural swing high/low levels
# when no fresh SNR exists beyond entry. Used as TP cap fallback.
STRUCTURAL_LOOKBACK = 20
# How many 15M candles after an engulfing closes we wait for price to RETEST
# the engulfing zone and reject from it before entering (book p.49 step 4 /
# p.73 step 5 — "ENTER AT RETEST OF LTF ENGULFING ZONE").
ENGULFING_RETEST_WINDOW = 6
MALAYSIAN_CONF_BASE = 50.0
MALAYSIAN_CONF_CLEAN = 25.0
MALAYSIAN_CONF_BREAKOUT_BODY = 25.0
# Counter-trend (against the H1 storyline) trades are the risky class: they are
# KEPT (the book trades both sides) but carry less confidence than a with-trend
# ("normal") setup, matching how MARRIAGE only ever wins with-trend.
MALAYSIAN_CONF_COUNTER = -20.0
# H1 candles that define the trend a setup is measured against.
SNR_TREND_H1_LOOKBACK = 20

# ── Analyzer roster (feature-flagged) ───────────────────────────────────

#: Every analyzer the family contains.  Disabling one does not remove it —
#: the code and its tests stay, so a flag can be flipped back on.
MALAYSIAN_ANALYZERS: tuple[str, ...] = (
    "snr", "engulfing", "marriage", "breakout", "flip", "qml", "confluence",
)

#: Ships enabled.  Cost-aware measurement (forex.metrics / walkforward) over
#: 2021-2024 EURUSD + 2021-2025 XAGUSD showed MARRIAGE is the only analyzer
#: with positive expectancy: EURUSD +0.246R/+0.293R/+0.415R in 2021-2023, but
#: −0.752R in 2024.  Engulfing (−0.076R), flip and qml were negative at scale.
#: The core SNR fires 2-7 times a year, far too few to measure at all.
#: snr+breakout ship enabled alongside marriage for funnel measurement;
#: re-evaluate on evidence.  Override with SNR_MALAYSIAN_ANALYZERS="marriage" etc.
MALAYSIAN_ENABLED_ANALYZERS_DEFAULT: tuple[str, ...] = ("marriage", "snr", "breakout")

#: EXPERIMENT (off by default): require MARRIAGE to agree with the H1 storyline.
#: Re-testable via SNR_MARRIAGE_WITH_TREND_ONLY=1. Marriage is
#: trend-continuation, so a setup firing AGAINST the H1 direction is
#: structurally counter-trend. Previously withdrawn because two harnesses
#: disagreed -- that disagreement was caused by a lookahead bug in
#: CostAwareReplay (expired trades priced at the dataset's final close), now
#: fixed. Being re-evaluated rather than assumed either way.
MALAYSIAN_MARRIAGE_WITH_TREND_ONLY_DEFAULT = False


def marriage_with_trend_only() -> bool:
    """Read SNR_MARRIAGE_WITH_TREND_ONLY (default: off)."""
    return os.environ.get("SNR_MARRIAGE_WITH_TREND_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def enabled_malaysian_analyzers() -> frozenset[str]:
    """
    The analyzers allowed to emit a signal, from ``SNR_MALAYSIAN_ANALYZERS``.

    Unknown tokens (including ``all`` — the roster has no such analyzer, list
    names explicitly) are warned about and dropped.  An empty value falls back
    to the safe default silently; a value with tokens but no
    recognised names falls back with a warning so the fallback is never silent.
    """
    raw = os.environ.get("SNR_MALAYSIAN_ANALYZERS", "").strip()
    if not raw:
        return frozenset(MALAYSIAN_ENABLED_ANALYZERS_DEFAULT)
    names = {
        part.strip().lower()
        for part in raw.replace(";", ",").split(",")
        if part.strip()
    }
    valid = names & set(MALAYSIAN_ANALYZERS)
    invalid = names - set(MALAYSIAN_ANALYZERS)
    if invalid:
        logger.warning(
            "SNR_MALAYSIAN_ANALYZERS: ignoring unknown analyzer(s) %s (valid: %s)",
            sorted(invalid), sorted(MALAYSIAN_ANALYZERS),
        )
    if valid:
        return frozenset(valid)
    logger.warning(
        "SNR_MALAYSIAN_ANALYZERS=%r has no valid analyzers — falling back to %s",
        raw, sorted(MALAYSIAN_ENABLED_ANALYZERS_DEFAULT),
    )
    return frozenset(MALAYSIAN_ENABLED_ANALYZERS_DEFAULT)


# How many same-direction LTF engulfing zones at the HTF level to consider when
# hunting the zone -> breakout -> retest sequence.  Only the freshest few can
# still produce a live-edge entry; older ones are stale by construction.
SNR_LTF_ZONE_SEARCH = 3
# Book rule 3 weights: a validating ENGULFING rejection carries full weight, a
# PIN BAR rejection (weaker confirmation) half.
SNR_WEIGHT_ENGULFING = 1.0
SNR_WEIGHT_PIN_BAR = 0.5
SNR_WEIGHT_FRESH = 1.0

# ── Malaysian SNR course extensions: MISS, pullback, sessions, storyline ────
# Cache timeframes the SNR family reads (seconds).
M15_TF_SECONDS = 900
H4_TF_SECONDS = 14400
H1_TF_SECONDS = 3600
D1_TF_SECONDS = 86400
# An entry more than this many 15M bars behind the last closed 15M candle is a
# played-out setup, not a live trade — the same ~4h window the SNR rejection
# guard already allows. Without it a gap/marriage setup re-fires on every
# subsequent 15M close for the rest of the run.
SNR_STALE_BARS = 16
# Home sessions per symbol (looked up case-insensitively).  AUDJPY is an Asia
# instrument, so it trades in tokyo + sydney; every other symbol defaults to
# SESSION_ALLOWED_DEFAULT, the london/newyork overlap (12:00-16:00 UTC) — the
# prime-liquidity window for FX and metals.  A single flat allow-list would
# block the whole NY session on the engine's default EURUSD, inverting the
# intent of the course rule.
SESSION_ALLOWED_BY_SYMBOL: dict[str, tuple[str, ...]] = {
    "AUDJPY": ("tokyo", "sydney"),
    "EURUSD": ("london", "newyork"),
    "XAUUSD": ("london", "newyork"),
}
SESSION_ALLOWED_DEFAULT: tuple[str, ...] = ("london", "newyork")
# Minimum d1 candles before the storyline gate is trusted: 2 weekly candles.
STORYLINE_MIN_D1 = 10

# ── Diagnostic funnel (measurement only — changes no trading behaviour) ───
# Counts where SNR-family setups die so a backtest run can be tuned on evidence
# instead of guesswork.  Module-level so the backtest driver can reset it once
# per run and dump it at the end; every counter is a plain ``+= 1``.
FUNNEL_STAGES: tuple[str, ...] = (
    "pairs_found",          # 4H A/V trendlines
    "rejections_found",     # wick rejection at anchor+3
    "breakouts_found",      # 15M close beyond the 15M line
    "stale_dropped",        # staleness guard (gap/marriage)
    "h4_rejection_stale",   # core-SNR rejection older than the last 2 H4 bars
    "session_dropped",      # entry outside the home sessions
    "storyline_dropped",    # change-of-storyline gate
    # Book rules 3/4/5 — "NO VALIDATION, NO TRADE"
    "validation_dropped",   # rejection candle is neither engulfing nor pin bar
    "body_first_dropped",   # first touch of the level was a BODY, not a wick
    "rule3_dropped",        # 2nd candle closed back beyond the SNR level
    "gates_passed",         # survived session + storyline
    "polish_dropped",       # polisher/quality gate/contradiction rejected
    "polish_hold",          # polisher said WAIT (held, not tradable yet)
    "dedup_merged",         # refreshed an already-active setup
    "setup_superseded",     # retired a stale setup to free the dedup slot
    "signals_emitted",      # final CONFIRMED signals
    "engulfing_gaps_found",
    "engulfing_no_retest",   # engulfing never got its zone retested (book entry rule)
    "engulfing_no_stop",     # no valid structural stop could be placed
    "engulfing_signals",
    "marriage_lines_found",
    "marriage_signals",
    "breakout_boxes_found",  # 15M 10-bar box formed before the 4H validation
    "breakout_signals",      # body-close breakout aligned with the 4H direction
    "flip_retests_found",    # flipped SNR (RBS/SBR) break + retest
    "flip_signals",
    "qml_peaks_found",       # QML/H&S engulfing peak + retest
    "qml_signals",
    "duplicate_fill_dropped",  # two analyzers fired the same fill in one direction
    "confluence_single_point",  # valid setup but < 2 converging book points
    "analyzer_disabled_dropped",  # filtered by SNR_MALAYSIAN_ANALYZERS
    "hard_drop",              # winrate plan Phase 3 pre-scoring drop
    "confluence_dropped",     # winrate plan Phase 2 score below required
    "confluence_passed",      # winrate plan Phase 2 score met required
)
SNR_FUNNEL: dict[str, dict[str, int]] = {}


def reset_funnel() -> None:
    """Clear every per-symbol counter. Called once at the start of a backtest run."""
    SNR_FUNNEL.clear()


def bump_funnel(symbol: str, stage: str, n: int = 1) -> None:
    """Increment ``stage`` for ``symbol``. Unknown stage names are ignored."""
    if stage not in FUNNEL_STAGES or not n:
        return
    bucket = SNR_FUNNEL.setdefault((symbol or "?").upper(), {})
    bucket[stage] = bucket.get(stage, 0) + n


def funnel_snapshot() -> dict[str, dict[str, int]]:
    """Per-symbol counts, every stage present (0 when never hit)."""
    return {
        sym: {stage: counts.get(stage, 0) for stage in FUNNEL_STAGES}
        for sym, counts in SNR_FUNNEL.items()
    }


def log_funnel() -> dict[str, dict[str, int]]:
    """Log the funnel at INFO and return the snapshot (empty run logs nothing)."""
    snapshot = funnel_snapshot()
    for sym, counts in snapshot.items():
        hit = {k: v for k, v in counts.items() if v}
        logger.info("[SNR-FUNNEL] %s %s", sym, " ".join(f"{k}={v}" for k, v in hit.items()))
    return snapshot


@dataclass
class SignalDraft:
    """Internal draft signal before confirmation."""
    direction: str  # "BUY" or "SELL"
    entry: float
    stop_loss: float
    take_profit: float
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    confidence: float = 0.0
    # Carries the float scoring terms plus nested context (analyzer, levels,
    # session, storyline) — the non-float entries are ignored by the sums.
    evidence: dict[str, Any] = field(default_factory=dict)
    explanation_parts: list[str] = field(default_factory=list)
    risk_reward: float = 0.0
    # Epoch seconds of the candle whose OPEN fills the trade. Drives the session
    # filter; None when the source candles carry no usable timestamp.
    entry_time: Optional[float] = None


class _FormingCacheView:
    """HotCache view that appends ONE still-open candle to its timeframe.

    The engine only ever reads candles through ``cache.get(symbol, tf)``, so
    merging the forming bar here lets the pre-alert run the real detectors
    against a candle that has not closed yet, with no per-detector changes.

    Single-threaded by design: the ticks engine swaps this in around one
    synchronous preview call and swaps it straight back out.
    """

    def __init__(self, inner, symbol: str, tf: int, forming: dict) -> None:
        self._inner = inner
        self._key = (symbol, tf)
        self._forming = forming
        self._open_time = forming.get("open_time")

    def get(self, symbol: str, tf: int, n: int = 0) -> list[dict]:
        base = self._inner.get(symbol, tf, n)
        if (symbol, tf) != self._key:
            return base
        # The closed cache may already hold a bar for this interval when the
        # forming candle straddles a boundary; drop it so the open bar wins.
        merged = [c for c in base if c.get("open_time") != self._open_time]
        merged.append(self._forming)
        return merged[-n:] if n > 0 else merged

    def __getattr__(self, name):
        return getattr(self._inner, name)


class SignalEngine:
    """
    Multi-factor confluence signal engine.

    Usage:
        engine = SignalEngine(cache, structure_engines, liquidityEngine, ...)
        signals = engine.evaluate(symbol, "EURUSD")
        # signals is a list of Signal objects (may be empty if NO_SIGNAL)
    """

    def __init__(
        self,
        cache: HotCache,
        structure_engines: dict[int, MarketStructureEngine] | None = None,
        liquidity_engines: dict[int, LiquidityEngine] | None = None,
        ob_engines: dict[int, OrderBlockEngine] | None = None,
        poc_engines: dict[int, POCEngine] | None = None,
        fvg_engines: dict[int, FVGEngine] | None = None,
        microstructure_engine: MicrostructureEngine | None = None,
        order_flow_engine: OrderFlowEngine | None = None,
        volatility_engine: VolatilityEngine | None = None,
        session_engine: SessionEngine | None = None,
        spread_engine: SpreadEngine | None = None,
        scoring_weights: ScoringWeights | None = None,
        min_confidence: float = MIN_CONFIDENCE_TO_CONFIRM,
        min_rr: float = MIN_RR_RATIO,
        noise_floor: float = MIN_CONFIDENCE_HARD_FLOOR,
        volume_profile_engine=None,
        context_engine: MarketContextEngine | None = None,
        retest_engine: RetestEngine | None = None,
        quality_gate: "QualityGate | None" = None,
    ) -> None:
        self._cache = cache
        # Careful: the caller may pass still-empty dicts that get populated AFTER
        # construction (backtest live-pipeline, ticks_engine). `or {}` would swap
        # an empty dict for a fresh one, silently losing the caller's reference
        # and starving every per-TF engine. Only fall back when None is passed.
        self._structure = {} if structure_engines is None else structure_engines
        self._liquidity = {} if liquidity_engines is None else liquidity_engines
        self._obs = {} if ob_engines is None else ob_engines
        self._poc = {} if poc_engines is None else poc_engines
        self._fvgs = {} if fvg_engines is None else fvg_engines
        self._micro = microstructure_engine
        self._order_flow = order_flow_engine
        self._vol = volatility_engine
        self._session = session_engine
        self._spread = spread_engine
        self._volume_profile = volume_profile_engine
        self._context_engine = context_engine
        self._retest = retest_engine
        self._quality_gate = quality_gate
        self._weights = scoring_weights or DEFAULT_SCORING
        self._min_confidence = min_confidence
        self._min_rr = min_rr
        self._noise_floor = noise_floor
        self._lifecycle = SignalLifecycleManager()
        from forex.analysis.signal_lifecycle import set_global_lifecycle
        set_global_lifecycle(self._lifecycle)
        self._contradiction = ContradictionEngine()
        self._polisher = SignalPolisher(
            min_confidence=self._min_confidence,
            min_rr=self._min_rr,
            noise_floor=self._noise_floor,
            quality_gate=self._quality_gate,
            contradiction=self._contradiction,
        )
        # Dynamic-RR engine (trade.md §3) — RR derived from signal strength.
        self._rr_service = RRService()
        self._recent_signals: list[dict] = []
        self._last_strategy_reports: dict[str, list[StrategyReport]] = {}

    def preview_malaysian(self, symbol: str, forming: dict) -> list[SignalDraft]:
        """Drafts the gates would test if the still-open ``forming`` bar closed now.

        This is the pre-alert surface: it runs the analysis and the entry
        gates only.  No polish, no confluence gate, no card, no lifecycle
        write, so it cannot disturb the confirmed signal feed.  The caller
        owns the decision to warn on a draft.

        ``forming`` is an open candle from CandleAggregator.get_forming(tf).
        """
        tf = int(forming.get("timeframe") or M15_TF_SECONDS)
        original = self._cache
        self._cache = _FormingCacheView(original, symbol, tf, forming)
        try:
            return self._best_malaysian_draft(symbol)
        finally:
            self._cache = original

    def has_active_setup(self, symbol: str, direction: str, analyzer: str) -> bool:
        """True when this analyzer already holds a live setup for the pair."""
        return self._lifecycle.find_active_setup(
            symbol, direction, f"malysian:{analyzer}") is not None

    def evaluate(
        self,
        symbol: str,
        strategy_mode: str = "malysian",
        closed_tf: int | None = None,
        skip_status: bool = False,
    ) -> list[Signal]:
        """
        Evaluate all timeframes for a symbol and produce signals.

        ``strategy_mode``: comma-separated list of strategies to run.
        Valid values: "malysian", "Moving_averages", "both".
        When multiple strategies are selected, each runs independently and
        the strongest signal (highest confidence) wins.
        Called on every candle close event.  Returns a list of signals
        (empty if NO_SIGNAL).

        ``closed_tf``: the timeframe (seconds) of the candle that just closed.
        Used for event-driven gating (improve.md §30-31): the MTF
        (Moving_averages) strategy is a higher-timeframe alignment concept and
        is skipped on 1M/5M closes, where only LTF entry/confirmation logic is
        needed. ``None`` (backward-compatible default) runs every strategy.

        ``skip_status``: when True, skip the per-strategy status reports
        (``for_signals=False``).  Those reports only feed the UI strategy
        panel (§45) and re-run the same confluence/MTF analysis the signal
        path below performs — pure 2x compute during a backtest replay.  The
        backtest engine passes ``skip_status=True``; live evaluation keeps the
        default so the UI still gets its status reports.
        """
        modes = [m.strip() for m in strategy_mode.split(",") if m.strip()]
        # "both" is a shortcut for running both
        if modes == ["both"]:
            modes = ["malysian", "Moving_averages"]

        # Re-evaluate held WAITING setups BEFORE running strategies so a held
        # setup gets a chance to confirm/invalidate on every candle close
        # (improve.md §33). Re-confirmed signals are broadcast downstream.
        reconfirmed = self._re_evaluate_waiting(symbol)

        # ── Data quality gate (improve.md §37) ──
        # Before ANY signal: if critical market data is missing/stale, emit
        # NO SIGNAL. Best-effort: only active when a quality gate is wired in.
        if self._quality_gate is not None:
            ok, issues = self._quality_gate.check_data_quality(symbol, None)
            if not ok:
                logger.warning(
                    "[SIGNAL] %s NO SIGNAL — data quality gate failed: %s",
                    symbol, issues,
                )
                return []

        all_signals: list[Signal] = list(reconfirmed)
        context = self._context_engine.get(symbol) if self._context_engine else None
        self._last_strategy_reports[symbol] = []
        for mode in modes:
            # Registry lookup replaces the hardcoded if/elif dispatch (§20).
            # Unknown modes are logged and skipped instead of crashing.
            strategy_cls = StrategyRegistry.get(mode)
            if strategy_cls is None:
                logger.warning(
                    "[SIGNAL] %s unknown strategy mode '%s' — skipped", symbol, mode
                )
                continue
            # Run the strategy for status/reporting (feeds get_strategy_reports,
            # UI §45 and strategy-agreement §21). Status must never break signal
            # production, so failures are swallowed and logged.
            # ponytail: status path re-runs the confluence/MTF analysis that the
            # legacy builder below also runs — 2x in-memory compute per candle.
            # The backtest engine passes skip_status=True to avoid this; live
            # evaluation keeps it so the UI strategy panel (§45) stays populated.
            if not skip_status:
                try:
                    report = strategy_cls().evaluate(symbol, context, self, for_signals=False)
                    self._last_strategy_reports[symbol].append(report)
                except Exception as exc:  # noqa: BLE001 — status must never break signals
                    logger.warning(
                        "[SIGNAL] %s strategy '%s' status failed: %s", symbol, mode, exc
                    )
            # Signal creation stays in SignalEngine via the legacy builders.
            if mode == "Moving_averages":
                # MTF alignment needs 1H+ context — skip on 1M/5M closes so we
                # don't run the expensive MTF strategy on every low-TF candle.
                if closed_tf in (60, 300):
                    continue
                all_signals.extend(self._evaluate_mtf(symbol))
            elif mode.startswith("ict_"):
                # ICT/SMC premium combo: emit via the generic combo builder.
                if closed_tf not in (60, 300):
                    all_signals.extend(self._evaluate_ict_strategy(symbol, mode))
            elif mode == "malysian":
                # Malaysian SNR needs a 4H rejection + an aligned 15M breakout.
                # On 1M/5M closes there is no new 15M/4H bar to evaluate, so
                # skip the expensive SNR build — the next 15M close
                # re-evaluates it.  This is the single biggest backtest win:
                # the engine fires _on_candle_close once per timeframe, and
                # without this gate the SNR path ran on every 1M/5M close too.
                if closed_tf in (60, 300):
                    continue
                all_signals.extend(self._evaluate_snr(symbol))

        if not all_signals:
            return []

        # De-duplicate by signal_id (a re-confirmed signal may also be returned
        # by the strategy that originally created it).
        unique: dict[str, Signal] = {}
        for s in all_signals:
            unique[s.signal_id] = s
        unique_signals = list(unique.values())

        # Attribution: every signal names the strategy(ies) it came from. The
        # MTF lifecycle key is stored lowercase ("moving_averages") — surface
        # the canonical registry name here.
        for s in unique_signals:
            if not s.source_strategies:
                s.source_strategies = [self._canonical_strategy_name(s.strategy)]
                s.combined = len(s.source_strategies) > 1

        # Same symbol + direction qualifying from multiple strategies = a
        # COMBINED signal (agreement). Merge into a single signal carrying
        # every source. Unlike the old behaviour (keep only the strongest),
        # ALL qualifying signals are returned.
        return self._merge_combined(unique_signals)

    @staticmethod
    def _canonical_strategy_name(strategy_field: str) -> str:
        """Map a stored lifecycle strategy key to its canonical registry name."""
        if not strategy_field:
            return "unknown"
        if strategy_field.lower() == "moving_averages":
            return "Moving_averages"
        return strategy_field

    @staticmethod
    def _merge_combined(signals: list[Signal]) -> list[Signal]:
        """Merge same symbol+direction signals into one combined attribution."""
        # Canonical display order for the source list (spec: malysian first).
        canonical_order = ("malysian", "Moving_averages")

        def _sort_key(name: str) -> tuple[int, str]:
            try:
                return (canonical_order.index(name), name)
            except ValueError:
                return (len(canonical_order), name)

        merged: dict[tuple[str, str], Signal] = {}
        order: list[tuple[str, str]] = []
        for s in signals:
            key = (s.symbol, s.direction)
            if key not in merged:
                merged[key] = s
                order.append(key)
                continue
            existing = merged[key]
            sources = sorted(set(existing.source_strategies) | set(s.source_strategies), key=_sort_key)
            # Keep the higher-confidence setup as the merged identity, then
            # absorb the other strategy's attribution onto it.
            if s.confidence_score > existing.confidence_score:
                winner: Signal = s
            else:
                winner = existing
            winner.source_strategies = sources
            winner.combined = len(sources) > 1
            if winner.combined:
                winner.strategy = ",".join(sources)
            merged[key] = winner
        return [merged[k] for k in order]

    def _re_evaluate_waiting(self, symbol: str) -> list[Signal]:
        """
        Re-check held WAITING setups with FRESH market state (improve.md §33).

        A WAITING signal was held because the contradiction engine previously
        said WAIT. On every candle close we re-run the contradiction analysis
        against current market context: if it now says TRADE the setup is
        promoted to CONFIRMED; if it says INVALIDATE the setup is closed. A
        continued WAIT leaves the setup held. Re-confirmed signals are returned
        for downstream broadcast.
        """
        reconfirmed: list[Signal] = []
        for sig in self._lifecycle.get_active(symbol):
            if sig.state != SignalState.WAITING:
                continue
            market_state = self._build_market_state(
                symbol, sig.direction, sig.confidence_evidence
            )
            report = self._contradiction.analyze(
                symbol, sig.direction, sig.confidence_evidence, market_state
            )
            sig.confidence_evidence["contradiction"] = report.to_dict()
            if report.recommendation == "TRADE":
                self._lifecycle.transition(
                    sig, SignalState.CONFIRMED,
                    reason="WAITING re-evaluated → TRADE (contradiction cleared)",
                    evidence=report.to_dict(),
                    score=sig.confidence_score,
                )
                METRICS.increment("signals_confirmed")
                reconfirmed.append(sig)
            elif report.recommendation == "INVALIDATE":
                self._lifecycle.transition(
                    sig, SignalState.INVALIDATED,
                    reason="WAITING re-evaluated → INVALIDATE",
                    evidence=report.to_dict(),
                    score=sig.confidence_score,
                )
                METRICS.increment("signals_invalidated")
                reconfirmed.append(sig)
            else:  # WAIT — leave held
                continue
        return reconfirmed

    def _evaluate_mtf(self, symbol: str) -> list[Signal]:
        """Run MultiTimeframeStrategy and produce a Signal — same validation as confluence."""
        from forex.mtf_strategy import MultiTimeframeStrategy
        mtf = MultiTimeframeStrategy(self._context_engine)
        direction, confidence, breakdown = mtf.check_market_alignment_with_score(symbol)
        if direction in ("HOLD", None):
            return []
        # Use the last cached 1H candle as entry reference
        setup = self._mtf_candidate_setup(symbol, direction)
        if setup is None:
            return []
        entry, sl, tp, rr = setup

        # Polish: run all threshold/quality/contradiction filters in one place.
        evidence = {"mtf_breakdown": breakdown}
        market_state = self._build_market_state(symbol, direction, evidence)
        result = self._polisher.polish(symbol, direction, confidence, rr, evidence, market_state, apply_min_confidence=True)

        if result.action == "DROP":
            return []

        if result.action == "HOLD":
            # Quality gate failure → hold in WAITING (reuse existing path).
            # The polisher's quality_gate_report is the same as what _hold_quality_gate_waiting expects.
            qg_report = result.quality_gate_report
            if qg_report is not None:
                held = self._hold_quality_gate_waiting(
                    symbol, direction, entry, sl, tp, confidence,
                    evidence,
                    self._mtf_explanation(direction, confidence, breakdown, entry, sl, tp, rr),
                    "moving_averages", qg_report,
                )
                # Attach polish trace for auditability
                held.confidence_evidence["polish_trace"] = [
                    {"filter_name": e.filter_name, "passed": e.passed, "reason": e.reason}
                    for e in result.trace
                ]
                return [held]

        # PASS path — continue with dedup / create / contradiction
        # Dedup: update the held setup instead of creating a duplicate
        # (improve.md §29, §32 — cooldown by setup identity).
        existing = self._lifecycle.find_active_setup(symbol, direction, "moving_averages")
        if existing is not None:
            existing.entry = entry
            existing.stop_loss = sl
            existing.take_profit = tp
            existing.confidence_score = confidence
            existing.confidence_evidence.update(evidence)
            existing.confidence_evidence["polish_trace"] = [
                {"filter_name": e.filter_name, "passed": e.passed, "reason": e.reason}
                for e in result.trace
            ]
            existing.explanation = self._mtf_explanation(
                direction, confidence, breakdown, entry, sl, tp, rr
            )
            existing.risk_reward_ratio = rr
            METRICS.increment("signals_generated")
            return [existing]

        signal = self._lifecycle.create(
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            evidence=evidence,
            explanation=self._mtf_explanation(
                direction, confidence, breakdown, entry, sl, tp, rr
            ),
            strategy="moving_averages",
            timeframe="multi",
            market_regime=self._get_regime_string(symbol),
        )
        signal.risk_reward_ratio = rr
        signal.confidence_evidence["polish_trace"] = [
            {"filter_name": e.filter_name, "passed": e.passed, "reason": e.reason}
            for e in result.trace
        ]
        METRICS.increment("signals_generated")

        # Contradiction analysis — reuse polisher's report if present to avoid double compute.
        contra_report = result.contradiction_report
        signal.confidence_evidence["contradiction"] = contra_report.to_dict()

        if contra_report.recommendation == "WAIT":
            self._lifecycle.transition(
                signal, SignalState.WAITING,
                reason=f"Contradiction engine → WAIT ({contra_report.severity}, net={contra_report.net_confidence:.0f})",
                evidence=contra_report.to_dict(),
                score=confidence,
            )
            METRICS.increment("signals_waiting")
        else:  # TRADE
            self._lifecycle.transition(
                signal, SignalState.CONFIRMED,
                reason="MTF SMA alignment confirmed",
                evidence=evidence,
                score=confidence,
            )
            METRICS.increment("signals_confirmed")
        self._recent_signals.append(signal.to_dict())
        if len(self._recent_signals) > 50:
            self._recent_signals = self._recent_signals[-50:]
        return [signal]

    def _mtf_explanation(
        self, direction: str, confidence: float, breakdown: dict,
        entry: float, sl: float, tp: float, rr: float,
    ) -> str:
        """Strategy-conforming explanation for an MTF SMA signal.

        Cites the per-timeframe alignment breakdown (which TFs agree) plus
        the tier label, instead of a bare one-liner.
        """
        tier = Signal.classify_strength(confidence)
        aligned = [f"{tf} {v}" for tf, v in breakdown.items() if v in ("BUY", "SELL")]
        lines = [f"{direction} — {tier} MTF SMA alignment "
                 f"(confidence {confidence:.0f}/100)"]
        lines.append("")
        lines.append("Why?")
        if aligned:
            lines.extend(f"- {tf} {v} SMA alignment" for tf, v in breakdown.items()
                         if v in ("BUY", "SELL"))
        else:
            lines.append("- multi-timeframe SMA alignment")
        lines.append(f"- R:R = {rr:.2f}" if rr > 0 else "- R:R = n/a")
        if entry > 0:
            lines.append(f"- entry {entry:.5f}")
        if sl > 0:
            lines.append(f"- SL {sl:.5f}")
        if tp > 0:
            lines.append(f"- TP {tp:.5f}")
        return "\n".join(lines)

    # ── Strategy status helpers (improve.md §20) ──────────────────────────
    # These compute a StrategyReport WITHOUT creating signals, so the UI and
    # strategy-agreement logic can inspect every strategy's full status against
    # the same shared context. Signal creation stays in _evaluate_mtf above.

    def _mtf_candidate_setup(
        self, symbol: str, direction: str
    ) -> "tuple[float, float, float, float] | None":
        """
        Return (entry, stop_loss, take_profit, risk_reward) for an MTF setup.

        Uses the last cached 1H candle as the entry reference and a percentage
        based SL/TP (works across forex/indices/gold). Returns ``None`` when no
        1H candle is available. Shared by _evaluate_mtf and _analyze_mtf.
        """
        candles = self._cache.get(symbol, 60)
        if not candles:
            return None
        last = candles[-1]
        entry = last["close"]
        # ponytail: percentage-based SL/TP — works for forex (1.10), indices (20000), gold (2300)
        sl_pct = 0.003   # 0.3%
        tp_pct = 0.006   # 0.6%  → R:R = 2.0
        sl_pip = entry * sl_pct
        tp_pip = entry * tp_pct
        if direction == "BUY":
            sl = entry - sl_pip
            tp = entry + tp_pip
        else:
            sl = entry + sl_pip
            tp = entry - tp_pip
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0
        return (entry, sl, tp, rr)

    def _analyze_mtf(self, symbol: str, for_signals: bool = False) -> StrategyReport:
        """Status-only report for the Moving_averages (MTF SMA) strategy."""
        active = self._context_engine is not None
        if not active:
            return StrategyReport(
                name="Moving_averages", active=False, applicable=False,
                direction="NEUTRAL",
                reason="no market context engine wired (MTF needs shared context)",
            )
        from forex.mtf_strategy import MultiTimeframeStrategy
        mtf = MultiTimeframeStrategy(self._context_engine)
        direction, confidence, breakdown = mtf.check_market_alignment_with_score(symbol)

        preconditions = [
            "shared market context available",
            "multi-timeframe bias map populated",
        ]
        confirmations = [
            f"{tf}: {v}" for tf, v in breakdown.items() if v in ("BUY", "SELL")
        ]
        missing = [
            tf for tf, v in breakdown.items() if v in ("HOLD", "NO_DATA", None)
        ]
        direction_label = (
            "BULLISH" if direction == "BUY" else "BEARISH" if direction == "SELL" else "NEUTRAL"
        )
        applicable = direction in ("BUY", "SELL")

        if direction in ("HOLD", None) or confidence < self._min_confidence:
            return StrategyReport(
                name="Moving_averages", active=active, applicable=applicable,
                direction=direction_label, preconditions=preconditions,
                confirmations=confirmations, missing_confirmations=missing,
                invalidations=["no multi-timeframe alignment"] if direction in (None, "HOLD") else [],
                confidence=confidence,
                reason=f"MTF alignment {direction} (conf {confidence:.0f}/100)",
            )

        setup = self._mtf_candidate_setup(symbol, direction)
        if setup is None:
            return StrategyReport(
                name="Moving_averages", active=active, applicable=False,
                direction=direction_label, preconditions=preconditions,
                confirmations=confirmations, missing_confirmations=missing,
                invalidations=["no 1H candle for entry reference"],
                confidence=confidence,
                reason="MTF aligned but no 1H candle to size the setup",
            )
        entry, sl, tp, rr = setup
        candidate_setup = {
            "entry": entry, "stop_loss": sl, "take_profit": tp, "risk_reward": rr,
        }
        return StrategyReport(
            name="Moving_averages", active=active, applicable=True,
            direction=direction_label, preconditions=preconditions,
            confirmations=confirmations, missing_confirmations=missing,
            invalidations=[], confidence=confidence,
            candidate_setup=candidate_setup,
            reason=f"10-SMA alignment: {direction} ({confidence:.0f}/100)",
        )

    def _build_confluence_draft(self, symbol: str) -> "Optional[SignalDraft]":
        """
        Run the intelligence engines and build the stronger of the BUY/SELL
        drafts (improve.md §20 malysian strategy). Returns ``None`` when no
        candidate can be built (no entry determined). Shared by both the signal
        path (_evaluate_confluence) and the status path (_analyze_confluence).
        """
        # ── 1. Run intelligence engines on cached candles ────────────────
        structure_events = self._run_structure(symbol)
        self._run_liquidity(symbol, structure_events)
        self._run_ob(symbol, structure_events)
        self._run_fvg(symbol)
        self._run_poc(symbol)

        # ── 2. Build drafts for BUY and SELL ─────────────────────────────
        buy_draft = self._build_draft(symbol, "BUY")
        sell_draft = self._build_draft(symbol, "SELL")

        # ── 2b. Retest reaction evidence (improve.md §34–§35) ────────────
        self._apply_retest_evidence(symbol, buy_draft)
        self._apply_retest_evidence(symbol, sell_draft)

        # ── 3. Pick the stronger direction ───────────────────────────────
        if buy_draft and sell_draft:
            if buy_draft.confidence >= sell_draft.confidence:
                sell_draft = None
            else:
                buy_draft = None

        return buy_draft or sell_draft

    def _confluence_preconditions(self) -> list[str]:
        """Required conditions for the malysian confluence strategy."""
        return [
            "HTF structure bias available",
            "Order block / FVG data available",
            "1M microstructure trigger available",
        ]

    def _confluence_expected(self) -> list[str]:
        """Evidence keys we expect a healthy confluence setup to confirm."""
        return [
            "htf_bias", "ob_quality", "fvg_imbalance", "liquidity_sweep",
            "mss", "displacement", "entry_confirmation", "poc_interaction",
            "spread", "data_quality",
        ]

    def _analyze_confluence(self, symbol: str, for_signals: bool = False) -> StrategyReport:
        """Status-only report for the malysian (multi-TF confluence) strategy."""
        active = bool(self._structure and self._obs and self._fvgs)
        draft = self._build_confluence_draft(symbol)
        if draft is None:
            return StrategyReport(
                name="malysian", active=active, applicable=False,
                direction="NEUTRAL",
                preconditions=self._confluence_preconditions(),
                missing_confirmations=self._confluence_expected(),
                reason="no candidate setup (insufficient confluence evidence)",
            )

        direction_label = "BULLISH" if draft.direction == "BUY" else "BEARISH"
        expected = self._confluence_expected()
        present = list(draft.evidence.keys())
        missing = [e for e in expected if e not in draft.evidence]
        confirmations = present

        # Invalidations come from the contradiction engine (improve.md §37–38).
        # Only run it on the explicit status-report path (for_signals=True) so
        # the hot signal path in evaluate() doesn't double-run the engine that
        # _evaluate_confluence runs again when building the actual signal.
        invalidations: list[str] = []
        if for_signals and self._contradiction is not None:
            market_state = self._build_market_state(symbol, draft.direction, draft.evidence)
            creport = self._contradiction.analyze(
                symbol, draft.direction, draft.evidence, market_state
            )
            if creport.recommendation == "INVALIDATE":
                invalidations = (
                    [f"contradiction: {r}" for r in creport.reasons]
                    or ["contradiction INVALIDATE"]
                )

        candidate_setup = {
            "entry": draft.entry,
            "stop_loss": draft.stop_loss,
            "take_profit": draft.take_profit,
            "entry_zone_low": draft.entry_zone_low,
            "entry_zone_high": draft.entry_zone_high,
        }
        return StrategyReport(
            name="malysian", active=active, applicable=draft.entry > 0,
            direction=direction_label, preconditions=self._confluence_preconditions(),
            confirmations=confirmations, missing_confirmations=missing,
            invalidations=invalidations, confidence=draft.confidence,
            candidate_setup=candidate_setup,
            reason=self._build_explanation(draft),
        )

    def _evaluate_confluence(self, symbol: str) -> list[Signal]:
        """Original multi-TF confluence (malysian) strategy."""
        signals: list[Signal] = []

        # ── 1-3. Build the stronger of the BUY/SELL drafts (shared with the
        # status report produced by _analyze_confluence). ──
        draft = self._build_confluence_draft(symbol)
        if draft is not None:
            # The original body iterated over at most one draft (the stronger
            # direction); retest evidence is already folded in by the helper.
            for draft in (draft,):
                # Polish: run all threshold/quality/contradiction filters in one place.
                # Confluence path skips min_confidence (only enforces noise_floor) —
                # matches original behavior where sub-60-confidence drafts emit as WEAK/RISKY.
                market_state = self._build_market_state(symbol, draft.direction, draft.evidence)
                result = self._polisher.polish(
                    symbol, draft.direction, draft.confidence, draft.risk_reward,
                    draft.evidence, market_state,
                    apply_min_confidence=False,
                )

                if result.action == "DROP":
                    continue

                if result.action == "HOLD":
                    # Two reasons for HOLD: quality gate failure OR contradiction WAIT.
                    # Handle both by creating/updating a signal in WAITING state.
                    qg_report = result.quality_gate_report
                    contra_report = result.contradiction_report

                    if qg_report is not None:
                        # Quality gate failure — reuse existing helper.
                        held = self._hold_quality_gate_waiting(
                            symbol, draft.direction, draft.entry, draft.stop_loss,
                            draft.take_profit, draft.confidence, draft.evidence,
                            self._build_explanation(draft), "malysian", qg_report,
                        )
                    elif contra_report is not None and contra_report.recommendation == "WAIT":
                        # Contradiction WAIT — use dedicated helper.
                        held = self._hold_contradiction_waiting(
                            symbol, draft.direction, draft.entry, draft.stop_loss,
                            draft.take_profit, draft.confidence, draft.evidence,
                            self._build_explanation(draft), "malysian", contra_report,
                        )
                    else:
                        # Should not happen — HOLD only from quality_gate or contradiction WAIT
                        continue

                    # Attach polish trace for auditability
                    held.confidence_evidence["polish_trace"] = [
                        {"filter_name": e.filter_name, "passed": e.passed, "reason": e.reason}
                        for e in result.trace
                    ]
                    if contra_report is not None:
                        held.confidence_evidence["contradiction"] = contra_report.to_dict()
                    signals.append(held)
                    continue

                # PASS path — attach polish trace and continue with dedup / create / contradiction
                draft.evidence["polish_trace"] = [
                    {"filter_name": e.filter_name, "passed": e.passed, "reason": e.reason}
                    for e in result.trace
                ]

                # Quality gate report already attached by polisher if it passed
                if result.quality_gate_report is not None:
                    draft.evidence["quality_gate"] = result.quality_gate_report.to_dict()

                # Contradiction analysis — reuse polisher's report if present to avoid double compute.
                contra_report = result.contradiction_report

                # ── 6. Dedup: update held setup instead of creating a duplicate ─
                # (improve.md §29, §32 — cooldown by setup identity). A held setup
                # (WAITING/CONFIRMED/...) is refreshed in place; the WAITING
                # re-evaluation in _re_evaluate_waiting() handles its transition.
                existing = self._lifecycle.find_active_setup(symbol, draft.direction, "malysian")
                if existing is not None:
                    existing.confidence_evidence.update(draft.evidence)
                    existing.confidence_score = draft.confidence
                    existing.explanation = self._build_explanation(draft)
                    existing.entry = draft.entry
                    existing.stop_loss = draft.stop_loss
                    existing.take_profit = draft.take_profit
                    existing.entry_zone_low = draft.entry_zone_low
                    existing.entry_zone_high = draft.entry_zone_high
                    existing.risk_reward_ratio = draft.risk_reward
                    existing.strength = Signal.classify_strength(draft.confidence)
                    signals.append(existing)
                    METRICS.increment("signals_generated")
                    self._recent_signals.append(existing.to_dict())
                    if len(self._recent_signals) > 50:
                        self._recent_signals = self._recent_signals[-50:]
                    continue

                # ── 6b. Create signal via lifecycle manager ──────────────────
                explanation = self._build_explanation(draft)
                signal = self._lifecycle.create(
                    symbol=symbol,
                    direction=draft.direction,
                    entry=draft.entry,
                    stop_loss=draft.stop_loss,
                    take_profit=draft.take_profit,
                    confidence=draft.confidence,
                    evidence=draft.evidence,
                    explanation=explanation,
                    strategy="malysian",
                    timeframe="multi",
                    market_regime=self._get_regime_string(symbol),
                )
                signal.entry_zone_low = draft.entry_zone_low
                signal.entry_zone_high = draft.entry_zone_high
                signal.risk_reward_ratio = draft.risk_reward
                signal.strength = Signal.classify_strength(draft.confidence)
                # Store contradiction evidence for downstream transparency (m.md §37)
                signal.confidence_evidence["contradiction"] = contra_report.to_dict()

                if contra_report.recommendation == "WAIT":
                    # Hold the setup in WAITING — do not force BUY/SELL (m.md §38)
                    reason = f"Contradiction engine → WAIT ({contra_report.severity}, net={contra_report.net_confidence:.0f})"
                    # Surface profile-specific contradictions in the WAIT reason (§39)
                    profile_reasons = [
                        r for r in contra_report.reasons
                        if "profile" in r.lower() or "value area" in r.lower()
                    ]
                    if profile_reasons:
                        reason = (
                            f"Contradiction engine → WAIT ({contra_report.severity}, "
                            f"net={contra_report.net_confidence:.0f}) + profile: {profile_reasons[0]}"
                        )
                    self._lifecycle.transition(
                        signal, SignalState.WAITING,
                        reason=reason,
                        evidence=contra_report.to_dict(),
                        score=draft.confidence,
                    )
                    METRICS.increment("signals_waiting")
                else:  # TRADE
                    # Immediately transition to CONFIRMED since we validated
                    self._lifecycle.transition(
                        signal, SignalState.CONFIRMED,
                        reason="Multi-TF confluence confirmed",
                        evidence=draft.evidence,
                        score=draft.confidence,
                    )
                    METRICS.increment("signals_confirmed")
                signals.append(signal)
                METRICS.increment("signals_generated")

                self._recent_signals.append(signal.to_dict())
                if len(self._recent_signals) > 50:
                    self._recent_signals = self._recent_signals[-50:]

        # ── 7. Order-block retest pair (additive, ICT/SMC). ─────────────
        # A bullish/bearish OB that price is extended away from produces a
        # counter-trend fade back to the OB; once price retests the OB and
        # confirms, it produces a continuation signal in the OB's direction.
        # Runs after the confluence loop so its higher-conviction same-direction
        # setup (fade/continuation levels) wins on the current candle; it also
        # fires independently when the confluence draft is empty.
        signals.extend(self._evaluate_order_block_retest(symbol))

        return signals

    # ── Malaysian SNR (Support & Resistance) strategy ────────────────────
    #
    # Three-step methodology (see bdf/spec):
    #   STEP 1 (4H levels): the levels are SLOPED TRENDLINES (TradingView
    #     "Extended Trendline", extend right), not horizontal prices.
    #       • Resistance (A shape): a line from a BULLISH candle's CLOSE to
    #         the NEXT BEARISH candle's OPEN.
    #       • Support (V shape): a line from a BEARISH candle's CLOSE to the
    #         NEXT BULLISH candle's OPEN.
    #     The most recent qualifying A/V pair wins; the line is extrapolated
    #     FORWARD beyond the second anchor.
    #   STEP 2 (4H rejection): the 3rd 4H candle AFTER the level-forming
    #     candle (N+3, where the level forms at the SECOND anchor). Buy: the
    #     candle's wick must touch the trendline value at that candle's time
    #     and the close must be ABOVE it. Sell: wick touch with the close
    #     BELOW. Touch tolerance 0.05%.
    #   STEP 3 (15M breakout): price must break the most immediate 15M
    #     trendline in the SAME direction as the 4H rejection (15M levels
    #     formed only from 15M candles that closed BEFORE the 4H rejection
    #     candle opened). Entry at the OPEN of the next 15M candle.
    #
    # Trade management: SL below the most recent 15M swing low (buy) / above
    # the most recent swing high (sell); TP = entry ± 2.0x risk.
    # Confidence: 50 base + 25 clean rejection + 25 strong breakout body, cap 100.

    @staticmethod
    def _epoch(open_time) -> "Optional[float]":
        """
        Epoch SECONDS for a cache candle's ``open_time``.

        The hot cache stores timezone-aware ``datetime`` objects; the Malaysian
        SNR course modules (``engulfing``/``storyline``/``sessions``/``marriage``)
        are pure and expect floats.  Returns None for anything unusable.
        """
        if open_time is None:
            return None
        try:
            if hasattr(open_time, "timestamp"):
                return float(open_time.timestamp())
            return float(open_time)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    # key: id(source_list) -> (strong_ref, src_len, first_obj, last_obj, converted)
    #
    # The strong ref makes a recycled id() impossible to confuse (a false hit
    # would hand back another list's candles).  first/last object identity plus
    # length let us convert INCREMENTALLY: on an append we convert one candle;
    # on a bounded-deque eviction (length unchanged, tail new) we drop the
    # converted head and convert the single new tail.  Only a non-tail change
    # (set_candles / big jump) triggers a full rebuild.  Values are identical to
    # a full rebuild, so signals cannot change.
    _module_cache: dict[int, tuple[list, int, object, object, list[dict]]] = {}

    @classmethod
    def _module_candles(cls, candles: list[dict]) -> list[dict]:
        """
        Adapt hot-cache candles to the pure-module candle format.

        The course modules take ``{"open_time": <epoch float>, "open", "high",
        "low", "close"}``; the cache carries datetimes plus extra keys.  Candles
        without a usable timestamp are dropped rather than guessed.

        Pure performance cache — output is identical to converting from scratch.
        """
        if not candles:
            return []
        n = len(candles)
        first, last = candles[0], candles[-1]
        key = id(candles)
        hit = cls._module_cache.get(key)
        if hit is not None and hit[0] is candles:
            _, cached_n, c_first, c_last, out = hit
            # Pure hit: window unchanged.
            if cached_n == n and c_last is last and c_first is first:
                return out
            # Appended one (window grew by exactly 1): convert just the new tail.
            if cached_n + 1 == n and c_last is candles[-2]:
                new = cls._convert_candle(last)
                if new is not None:
                    out.append(new)
                cls._module_cache[key] = (candles, n, first, last, out)
                return out
            # Bounded eviction: same length, new tail, head shifted.  Drop the
            # converted head and convert the single new tail.
            if cached_n == n and c_last is not last and len(out) == n:
                if out:
                    del out[0]
                new = cls._convert_candle(last)
                if new is not None:
                    out.append(new)
                cls._module_cache[key] = (candles, n, candles[0], last, out)
                return out

        out = []
        append = out.append
        for c in candles:
            new = cls._convert_candle(c)
            if new is not None:
                append(new)
        if len(cls._module_cache) > 8:
            cls._module_cache.clear()
        cls._module_cache[key] = (candles, n, first, last, out)
        return out

    @classmethod
    def _convert_candle(cls, c: dict) -> "Optional[dict]":
        """One cache candle -> pure-module candle dict, or None if unusable."""
        ts = cls._epoch(c.get("open_time"))
        if ts is None:
            return None
        try:
            return {
                "open_time": ts,
                "open": float(c["open"]), "high": float(c["high"]),
                "low": float(c["low"]), "close": float(c["close"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _snr_levels(
        cls, candles: list[dict], kind: str, max_levels: int = 3,
    ) -> list[dict]:
        """
        The last ``max_levels`` A/V SNR TRENDLINE levels of ``kind``, OLDEST FIRST.

        Same pair test as the single-level scan, but the walk keeps going past
        the first hit instead of stopping — :mod:`forex.analysis.marriage` needs
        two SNRs of the same kind to marry them into one sloped trendline.
        ``kind="resistance"`` (A shape): a bullish candle (close > open) whose
        NEXT candle is bearish (close < open) anchors ``(t_i, close_i)`` and
        ``(t_{i+1}, open_{i+1})``.  ``kind="support"`` (V shape) is the mirror.
        Every level carries its ``kind`` (marriage filters on it) and ``index``
        (the SECOND anchor candle, where the level forms).

        NOTE on GAP SNRs (p.13/29): a GAP SNR is a distinct OPEN-CLOSE decision
        ZONE, detected separately by :func:`detect_gap` and traded by the
        engulfing analyzer.  These A/V levels are the REGULAR SNRs a trendline
        is allowed to connect; the two structures never overlap, so the book's
        "never connect a GAP SNR to a trendline" holds by construction here.
        """
        if len(candles) < 2:
            return []
        first_bearish = kind == "support"
        levels: list[dict] = []
        # ponytail: scan only the most recent SNR_LEVEL_SCAN candles. The cache
        # holds 500; scanning all of it per analyzer per bar was the top backtest
        # hotspot. A level older than this can't yield a fresh, live-edge setup.
        start_i = max(0, len(candles) - 1 - SNR_LEVEL_SCAN)
        for i in range(len(candles) - 2, start_i - 1, -1):
            c0, c1 = candles[i], candles[i + 1]
            c0_bearish = float(c0["close"]) < float(c0["open"])
            c1_bearish = float(c1["close"]) < float(c1["open"])
            if c0_bearish != first_bearish or c1_bearish == first_bearish:
                continue
            t1 = cls._epoch(c0.get("open_time"))
            t2 = cls._epoch(c1.get("open_time"))
            if t1 is None or t2 is None or t2 == t1:
                # A missing/duplicate timestamp poisons every older pair too.
                break
            levels.append({
                "kind": kind,
                "t1": t1,
                "y1": float(c0["close"]),
                "t2": t2,
                "y2": float(c1["open"]),
                "index": i + 1,
            })
            if len(levels) >= max_levels:
                break
        levels.reverse()  # oldest first
        return levels

    @classmethod
    def _snr_trendline(cls, candles: list[dict], kind: str) -> "Optional[dict]":
        """
        Build the most recent A/V SNR TRENDLINE (Malaysian SNR course spec).

        ``kind="resistance"`` (A shape): a bullish candle (close > open) whose
        NEXT candle is bearish (close < open) anchors the line at
        ``(t_i, close_i)`` and ``(t_{i+1}, open_{i+1})``.  ``kind="support"``
        (V shape) is the mirror: a bearish candle followed by a bullish one.
        Candles are scanned from the most recent backwards and the FIRST
        qualifying pair wins.  The line is extrapolated forward from the two
        anchors (extend right).  ``t`` is the candle ``open_time`` in epoch
        seconds.  Returns ``{"kind", "t1", "y1", "t2", "y2", "index"}`` where
        ``index`` is the SECOND anchor candle (where the level forms), or
        ``None`` when no pair exists.
        """
        levels = cls._snr_levels(candles, kind, max_levels=1)
        return levels[-1] if levels else None

    @classmethod
    def _snr_line_value(cls, line: dict, candle: dict) -> float:
        """
        Value of ``line`` extrapolated to ``candle``'s open_time.

        Linear extension of the two anchor points: ``y1 + slope * (t - t1)``.
        Accepts either candle shape — cache candles (``datetime``) and the pure
        course modules' candles (epoch float) both work.
        """
        t = cls._epoch(candle.get("open_time"))
        if t is None:
            return float("nan")
        slope = (line["y2"] - line["y1"]) / (line["t2"] - line["t1"])
        return line["y1"] + slope * (t - line["t1"])

    @classmethod
    def _snr_miss(cls, candles: list[dict], level: dict, direction: str,
                  rejection_index: int) -> bool:
        """
        Did price MISS the level between the level forming and the rejection?

        A MISS is the course's level-validation rule: after the level forms
        (second anchor) price moves away and the candles in between never come
        back to the line — a real "no demand / no supply" level.  The rejection
        candle itself is excluded (it is the one that DOES touch).

        Touch test mirrors :meth:`_snr_check_rejection`: support counts a touch
        when the candle's LOW reaches the sloped line value (+ tolerance),
        resistance when its HIGH does.  Returns True (MISS → validated level)
        when none of the intervening candles touched.
        """
        if direction.upper() == "BUY":
            def _touched(c: dict, lv: float, tol: float) -> bool:
                return float(c["low"]) <= lv + tol
        else:
            def _touched(c: dict, lv: float, tol: float) -> bool:
                return float(c["high"]) >= lv - tol

        for i in range(level["index"] + 1, rejection_index):
            if i < 0 or i >= len(candles):
                continue
            lv = cls._snr_line_value(level, candles[i])
            if _touched(candles[i], lv, abs(lv) * SNR_TOUCH_PCT):
                return False
        return True

    @classmethod
    def _snr_freshness(
        cls, candles: list[dict], level: dict, upto: "Optional[int]" = None,
    ) -> bool:
        """
        Is ``level`` a FRESH SNR? (book p.15)

        "Fresh SNR is when a level is unbroken by candle's wick.  If an SNR is
        broken by candle's body, it is considered a fresh level.  Unfresh SNR is
        a level broken by candle's wick."  A BODY break can therefore RE-FRESH a
        level that a wick had already broken.

        Scans ``level["index"] + 1`` up to and including ``upto`` (default: the
        last candle).  A candle whose BODY closes beyond the level marks it
        fresh again (that is the book); a candle whose WICK (but not body)
        pierces it makes it unfresh.  The last decisive event wins, so a level
        wick-broken and later body-broken ends up FRESH.
        """
        end = len(candles) - 1 if upto is None else min(upto, len(candles) - 1)
        is_support = level["kind"] == "support"
        fresh = True
        for i in range(level["index"] + 1, end + 1):
            lv = cls._snr_line_value(level, candles[i])
            tol = abs(lv) * SNR_TOUCH_PCT
            c = candles[i]
            if is_support:
                wick_broken = float(c["low"]) < lv - tol
            else:
                wick_broken = float(c["high"]) > lv + tol
            # Book p.15: "Unfresh SNR is a level broken by candle's wick."
            # ANY penetration beyond the level (whether by wick alone or by a
            # full body closure) means the level has been violated and is no
            # longer fresh.  The book's "if broken by body, considered fresh"
            # refers to the break CREATING a new fresh level on the other side,
            # not re-freshening the level that was just broken through.
            if wick_broken:
                fresh = False
        return fresh

    @classmethod
    def _snr_first_touch_by_body(
        cls, candles: list[dict], level: dict, upto: int,
    ) -> bool:
        """
        Did price FIRST touch the level with a BODY? (book p.16 / p.20 rule 2)

        "First touch is candle's body not the wick.  So, it is INVALID setup."
        Returns True only when the FIRST candle that tagged the line closed
        beyond it; a wick-only first touch (a prior touch, not a body break) is
        allowed.  Only the first touch decides — later tags are irrelevant.
        """
        is_support = level["kind"] == "support"
        for i in range(level["index"] + 1, min(upto, len(candles) - 1) + 1):
            if i < 0 or i >= len(candles):
                continue
            lv = cls._snr_line_value(level, candles[i])
            tol = abs(lv) * SNR_TOUCH_PCT
            if is_support:
                if float(candles[i]["low"]) > lv + tol:
                    continue
                return float(candles[i]["close"]) < lv
            if float(candles[i]["high"]) < lv - tol:
                continue
            return float(candles[i]["close"]) > lv
        return False

    @classmethod
    def _snr_rejection_validation(
        cls, candles: list[dict], index: int, direction: str,
    ) -> "Optional[str]":
        """
        Book rule 3 — "NO VALIDATION, NO TRADE" (p.46, p.73 step 2).

        The rejection candle must be an ENGULFING (same-direction body engulf,
        via the course's own :func:`detect_engulfing`) or a PIN BAR (wick >= 2x
        body, closing in the rejection direction).  Returns "engulfing",
        "pin_bar", or None when the candle validates nothing.
        """
        if index < 1 or index >= len(candles):
            return None
        want = "bullish" if direction.upper() == "BUY" else "bearish"
        if detect_engulfing(cls._module_candles(candles[: index + 1]), index) == want:
            return "engulfing"
        c = candles[index]
        o, h, low, cl = (float(c["open"]), float(c["high"]),
                         float(c["low"]), float(c["close"]))
        body = abs(cl - o)
        if body <= 0:
            return None
        if direction.upper() == "BUY" and cl > o and (min(o, cl) - low) >= 2 * body:
            return "pin_bar"
        if direction.upper() == "SELL" and cl < o and (h - max(o, cl)) >= 2 * body:
            return "pin_bar"
        return None

    @classmethod
    def _snr_rule3(
        cls, candles: list[dict], level: dict, rejection_index: int, direction: str,
    ) -> tuple[bool, bool]:
        """
        Book rule 3 of the touch sequence (p.20): the candle AFTER the rejection
        must not close back beyond the SNR level.

        Returns ``(holds, pending)``.  ``pending`` is True when the next candle
        has not printed yet — the rejection candle is usually the most recent
        closed 4H bar, and an unconfirmed second candle must not veto the setup
        (:meth:`_snr_stale` then bounds how long the setup may live instead).
        """
        nxt = rejection_index + 1
        if nxt >= len(candles):
            return True, True
        lv = cls._snr_line_value(level, candles[nxt])
        close = float(candles[nxt]["close"])
        if direction.upper() == "BUY":
            return close >= lv, False
        return close <= lv, False

    @classmethod
    def _snr_tradable_trendline(
        cls, candles: list[dict], kind: str, max_levels: int = TP_LEVEL_LOOKBACK,
    ) -> "Optional[dict]":
        """
        The level of ``kind`` to trade: the most recent LIVE one, preferring a
        FRESH level (book rule 2 — a fresh SNR holds liquidity).

        "Live" means its N+3 rejection still sits in the last
        ``SNR_H4_RECENT_BARS`` closed candles, so preferring freshness can never
        promote a level whose rejection _evaluate_snr would drop as stale.
        The rejection candle must actually EXIST (``index + offset < len``) —
        otherwise a level forming near the end of the series has no rejection
        yet and cannot be traded.  Returns None when no live level of that kind
        exists.
        """
        last = len(candles) - 1
        live = [
            lv for lv in cls._snr_levels(candles, kind, max_levels=max_levels)
            if lv["index"] + SNR_REJECTION_OFFSET <= last
            and lv["index"] + SNR_REJECTION_OFFSET >= last - SNR_H4_RECENT_BARS + 1
        ]
        if not live:
            return None
        for lv in reversed(live):
            if cls._snr_freshness(candles, lv, lv["index"] + SNR_REJECTION_OFFSET):
                return lv
        return live[-1]

    @classmethod
    def _find_structural_level(
        cls, candles: list[dict], kind: str, entry: float, lookback: int = STRUCTURAL_LOOKBACK,
    ) -> "Optional[dict]":
        """
        Find the nearest structural swing level (recent swing high/low) in the
        target direction within the last ``lookback`` 4H candles.

        ``kind="resistance"`` → find nearest swing HIGH above entry (for BUY TP cap).
        ``kind="support"`` → find nearest swing LOW below entry (for SELL TP cap).

        This is a FALLBACK when no fresh SNR exists beyond entry. The structural
        level is not "fresh" by book definition but represents real market
        structure (swing points) that can act as a target ceiling.
        """
        if len(candles) < 3:
            return None
        # Scan the last ``lookback`` candles for swing highs/lows
        start = max(0, len(candles) - lookback)
        swings: list[tuple[float, dict]] = []  # (distance_from_entry, level_info)
        for i in range(start + 1, len(candles) - 1):
            c_prev = candles[i - 1]
            c_curr = candles[i]
            c_next = candles[i + 1]
            h_prev, h_curr, h_next = float(c_prev["high"]), float(c_curr["high"]), float(c_next["high"])
            l_prev, l_curr, l_next = float(c_prev["low"]), float(c_curr["low"]), float(c_next["low"])
            if kind == "resistance":
                # Swing high: current high > both neighbors
                if h_curr > h_prev and h_curr > h_next:
                    if h_curr > entry:  # only levels beyond entry
                        swings.append((h_curr - entry, {
                            "price": h_curr,
                            "index": i,
                            "time": c_curr.get("open_time"),
                            "kind": "structural_resistance",
                        }))
            else:  # support
                # Swing low: current low < both neighbors
                if l_curr < l_prev and l_curr < l_next:
                    if l_curr < entry:  # only levels beyond entry
                        swings.append((entry - l_curr, {
                            "price": l_curr,
                            "index": i,
                            "time": c_curr.get("open_time"),
                            "kind": "structural_support",
                        }))
        if not swings:
            return None
        # Return nearest structural level to entry
        swings.sort(key=lambda x: x[0])
        return swings[0][1]

    @classmethod
    def _snr_target(
        cls, candles: list[dict], kind: str, entry: float, probe: dict,
        upto: int, max_levels: int = TP_LEVEL_LOOKBACK,
    ) -> "Optional[tuple[float, bool, dict]]":
        """
        Nearest SNR level of ``kind`` sitting BEYOND ``entry`` (book rule 1).

        ``probe`` is the candle whose time the lines are extrapolated to (the
        entry candle); ``upto`` bounds which candles may have formed the levels
        (never the entry itself — no look-ahead).  Returns the closest
        ``(line_value, fresh, level)`` among FRESH levels only — the book takes
        profit at the next *fresh* SNR, so an unfresh level is not a valid
        target.  Returns None when no fresh level sits beyond the entry (the
        setup is then not tradable under the strict book).
        """
        hits = []
        for lv in cls._snr_levels(candles[:upto], kind, max_levels=max_levels):
            value = cls._snr_line_value(lv, probe)
            beyond = value > entry if kind == "resistance" else value < entry
            if beyond and cls._snr_freshness(candles[:upto], lv):
                hits.append((abs(value - entry), value, True, lv))
        if not hits:
            return None
        return min(hits, key=lambda h: h[0])

    def _calculate_proper_tp(
        self, symbol: str, direction: str, entry: float,
        candles_4h: list[dict], sl: float,
    ) -> "tuple[float, float, dict]":
        """
        Quantitative TP with proper math.

        The caller already computes SL from the wick / structural swing, so this
        method only derives the take profit:

            risk  = |entry − sl|  (passed-in stop, not recomputed)
            TP    = min(structural_level, entry ± 2R)
                    capped by reachability P(hit TP before SL) > 0.3

        Returns: ``(tp_price, risk_per_unit, evidence_dict)``.
        """
        risk = abs(entry - sl)
        if risk <= 0:
            risk = entry * 0.005  # absolute fallback

        # Nearest structural swing high/low beyond entry.
        structural_level = None
        for i in range(len(candles_4h) - 2, 0, -1):
            if i > 0 and i + 1 < len(candles_4h):
                if direction == "BUY":
                    if float(candles_4h[i]["high"]) > float(candles_4h[i - 1]["high"]) and \
                       float(candles_4h[i]["high"]) > float(candles_4h[i + 1]["high"]):
                        level = float(candles_4h[i]["high"])
                        if level > float(candles_4h[-1]["close"]):
                            structural_level = level
                            break
                else:
                    if float(candles_4h[i]["low"]) < float(candles_4h[i - 1]["low"]) and \
                       float(candles_4h[i]["low"]) < float(candles_4h[i + 1]["low"]):
                        level = float(candles_4h[i]["low"])
                        if level < float(candles_4h[-1]["close"]):
                            structural_level = level
                            break

        if direction == "BUY":
            theoretical_tp = entry + risk * 2.0
        else:
            theoretical_tp = entry - risk * 2.0

        if structural_level:
            if direction == "BUY":
                theoretical_tp = min(theoretical_tp, structural_level)
            else:
                theoretical_tp = max(theoretical_tp, structural_level)

        # Reachability filter: simplified first-passage probability for the
        # driftless case — P(hit TP before SL) ≈ risk / (risk + reward).
        # Require P > 0.3 for viability; reduce target when breached.
        reward = abs(theoretical_tp - entry)
        if reward > 0:
            prob = risk / (risk + reward)
            if prob < 0.3:
                max_reward = risk * (0.3 / 0.7)
                theoretical_tp = entry + max_reward if direction == "BUY" else entry - max_reward
                reward = abs(theoretical_tp - entry)

        tp = theoretical_tp

        evidence = {
            "atr_risk": round(risk, 5),
            "structural_level": structural_level,
            "reachability_prob": round(risk / (risk + reward), 2) if reward > 0 else 0,
        }

        return tp, risk, evidence

    @classmethod
    def _snr_take_profit(
        cls, candles: list[dict], direction: str, entry: float, sl: float,
        probe: dict, upto: int,
    ) -> "Optional[tuple[float, str, dict]]":
        """
        Legacy method - kept for compatibility.
        """
        risk = abs(entry - sl)
        if risk <= 0:
            return None
        kind = "resistance" if direction == "BUY" else "support"
        hit = cls._snr_target(candles, kind, entry, probe, upto)
        if hit is None:
            return None
        level = dict(hit[3])
        level["level_price"] = hit[1]
        tp = entry + risk * SNR_MAX_RR if direction == "BUY" else entry - risk * SNR_MAX_RR
        return tp, "level", level

    @classmethod
    def _snr_m15_confirmation(
        cls, m15: list[dict], level: dict, direction: str, not_before: float,
    ) -> "Optional[dict]":
        """
        M15 ENGULFING ZONE + BREAKOUT + RETEST — the book's LTF confirmation.

        Book p.73 "5 Action Plans", the canonical summary of the method:

            3) Price Breakout  — *HTF refinement: Go down one time frame
               lower.  LTF breakout pattern with EG zone inside HTF zone.*
            5) Entry Confirmation — *Retest of LTF EG zone.*

        and p.16 "HEAR THIS!": once the HTF has rejected off its SNR, *"JUST
        GO TO LOWER TIMEFRAME AND IDENTIFY A BREAKOUT TO ENTER POSITION."*

        So the LTF half of the core SNR is NOT "a wick rejection of the HTF
        line".  It is: find the most recent same-direction 15M ENGULFING at the
        HTF level, take the breakout beyond that engulfing zone, then require
        price to RETEST the zone and reject from it.  Entry is the open after
        that retest.

        This is deliberately the same shape the ENGULFING analyzer already
        uses for a GAP SNR (``_analyze_engulfing``); the only difference is the
        decision level, which here is the regular 4H A/V SNR line rather than
        an open/close gap.  Both were measured: entering at the bare next-open
        instead of the retest "lost 0-for-9 across the 2021+2022 backtests".

        Scanning BACKWARD from the live edge is what keeps the signal current —
        it always resolves to the zone/breakout/retest the market is printing
        now, never one from hours ago.

        ``not_before`` is the 4H rejection candle's open time: a 15M candle
        that opened before the HTF structure existed cannot confirm it.

        Returns the winning candidate dict, or None when no complete
        zone -> breakout -> retest sequence exists at the live edge.
        """
        if not m15 or not level or direction.upper() not in ("BUY", "SELL"):
            return None
        is_buy = direction.upper() == "BUY"
        want = "bullish" if is_buy else "bearish"

        # Collect same-direction engulfings that touch the HTF level.  Scanning
        # backwards means the freshest one wins, and each is scored on its own
        # breakout+retest so a recent complete sequence is never shadowed by an
        # older one that never printed a retest.
        candidates: list[dict] = []
        for i in range(len(m15) - 1, 0, -1):
            c = m15[i]
            t = cls._epoch(c.get("open_time"))
            if t is None:
                continue
            if t <= not_before:
                break
            if detect_engulfing(m15, i) != want:
                continue
            lv = cls._snr_line_value(level, c)
            tol = abs(lv) * SNR_TOUCH_PCT
            # The engulfing must sit AT the HTF level, not somewhere unrelated.
            if not (float(c["low"]) <= lv + tol and float(c["high"]) >= lv - tol):
                continue
            candidates.append({"index": i, "line_value": lv, "low": float(c["low"]),
                               "high": float(c["high"])})
            # Only the freshest handful can be live; older ones are stale by
            # construction and would just cost time.
            if len(candidates) >= SNR_LTF_ZONE_SEARCH:
                break

        for cand in candidates:  # newest first
            zi = cand["index"]
            z_low, z_high = cand["low"], cand["high"]
            # BREAKOUT beyond the engulfing zone, away from the HTF level.
            bo_i = None
            for j in range(zi + 1, len(m15)):
                c = m15[j]
                if is_buy and float(c["close"]) > z_high:
                    bo_i = j
                    break
                if not is_buy and float(c["close"]) < z_low:
                    bo_i = j
                    break
            if bo_i is None:
                continue
            # RETEST of the zone, rejected in the trade's direction.
            for k in range(bo_i + 1, len(m15)):
                c = m15[k]
                if is_buy:
                    if float(c["low"]) <= z_high and float(c["close"]) > z_high:
                        return {
                            "zone_index": zi,
                            "breakout_index": bo_i,
                            "retest_index": k,
                            "entry_index": k + 1,
                            "line_value": cand["line_value"],
                            "zone_low": z_low,
                            "zone_high": z_high,
                            "clean": float(c["close"]) > cand["line_value"],
                        }
                else:
                    if float(c["high"]) >= z_low and float(c["close"]) < z_low:
                        return {
                            "zone_index": zi,
                            "breakout_index": bo_i,
                            "retest_index": k,
                            "entry_index": k + 1,
                            "line_value": cand["line_value"],
                            "zone_low": z_low,
                            "zone_high": z_high,
                            "clean": float(c["close"]) < cand["line_value"],
                        }
        return None

    @classmethod
    def _snr_check_rejection(
        cls, candles: list[dict], level: dict, direction: str
    ) -> "Optional[dict]":
        """
        Check the Malaysian SNR rejection on the 3rd candle after the level.

        ``level`` is the output of :meth:`_snr_trendline`; the rejection candle
        is ``level["index"] + 3`` and the reference price is the trendline's
        value at that candle's time (NOT a flat level price).  ``direction`` is
        "BUY" (price rejects the support trendline from ABOVE) or "SELL"
        (rejects the resistance trendline from BELOW).  Touch = the candle's
        wick reaching the line within 0.05%; the close must be on the
        rejection side.  Returns ``None`` when there is no rejection, otherwise
        a dict with ``direction`` / ``price`` (the line value) /
        ``candle_index`` / ``open_time`` / ``clean`` (close beyond the line by
        > 0.1%) /         ``validated`` (True = a MISS validated the level; False =
        aggressive cSNR, price had already tagged the level before this
        rejection).  Both are tradable — the course allows either — but a
        validated level carries more supporting weight.

        The returned dict also carries the book's hard filters so the caller can
        drop the setup and count it: ``validation`` ("engulfing" / "pin_bar" /
        None — rule 3, NO VALIDATION NO TRADE), ``body_first`` (the first touch
        of the level was a body — p.16), ``rule3_ok`` / ``rule3_pending`` (the
        2nd candle after the touch must not close beyond the level — p.20) and
        ``fresh`` (no wick ever pierced the level — p.15).
        """

        if not level or not candles or direction.upper() not in ("BUY", "SELL"):
            return None
        idx = level["index"] + SNR_REJECTION_OFFSET
        if idx >= len(candles):
            return None
        c = candles[idx]
        price = cls._snr_line_value(level, c)
        tol = price * SNR_TOUCH_PCT
        if direction.upper() == "BUY":
            # Wick must touch the support trendline (within tolerance) and the
            # close must be ABOVE the line value at this candle's time.
            if not (float(c["low"]) <= price + tol and float(c["high"]) >= price - tol):
                return None
            if not float(c["close"]) > price:
                return None
            clean = float(c["close"]) > price * (1 + SNR_CLEAN_PCT)
        else:
            # Wick must touch the resistance trendline and the close must be
            # BELOW the line value at this candle's time.
            if not (float(c["high"]) >= price - tol and float(c["low"]) <= price + tol):
                return None
            if not float(c["close"]) < price:
                return None
            clean = float(c["close"]) < price * (1 - SNR_CLEAN_PCT)
        rule3_ok, rule3_pending = cls._snr_rule3(candles, level, idx, direction.upper())
        return {
            "direction": direction.upper(),
            "price": price,
            "candle_index": idx,
            "open_time": c.get("open_time"),
            "clean": clean,
            "validated": cls._snr_miss(candles, level, direction.upper(), idx),
            "validation": cls._snr_rejection_validation(candles, idx, direction),
            "body_first": cls._snr_first_touch_by_body(candles, level, idx),
            "rule3_ok": rule3_ok,
            "rule3_pending": rule3_pending,
            "fresh": cls._snr_freshness(candles, level, idx),
            "trendline": level,
        }

    @classmethod
    def _snr_book_validation(
        cls, candles: list[dict], level: dict, direction: str,
    ) -> "Optional[dict]":
        """
        Book-exact Step 2: validate ``level`` at c+2 on 4H.

        BUY (support/V): validation candle's LOW touched the line and its CLOSE
        is back above it.  SELL (resistance/A): HIGH touched, CLOSE back below.
        Strict body test on the sloped line value — no tolerance, no engulfing /
        pin-bar requirement.  Fresh filter: the level only validates on a MISS
        (no wick touched it between formation and c+2, via :meth:`_snr_miss`).
        Returns the validation dict or None.
        """
        if not level or not candles or direction.upper() not in ("BUY", "SELL"):
            return None
        idx = level["index"] + SNR_BOOK_VALIDATION_OFFSET
        if idx < 0 or idx >= len(candles):
            return None
        c = candles[idx]
        price = cls._snr_line_value(level, c)
        try:
            low, high, close = float(c["low"]), float(c["high"]), float(c["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if direction.upper() == "BUY":
            if not (low <= price and close > price):
                return None
            clean = close > price * (1 + SNR_CLEAN_PCT)
        else:
            if not (high >= price and close < price):
                return None
            clean = close < price * (1 - SNR_CLEAN_PCT)
        if not cls._snr_miss(candles, level, direction.upper(), idx):
            return None
        return {
            "direction": direction.upper(),
            "price": price,
            "candle_index": idx,
            "open_time": c.get("open_time"),
            "clean": clean,
            "validated": True,
            "trendline": level,
        }

    def _analyze_snr_book(self, symbol: str) -> "Optional[SignalDraft]":
        """
        Book-exact Malaysian SNR Step 1+2 → SignalDraft (analyzer="snr").

        Step 1 reuses :meth:`_snr_levels` (A-shape resistance = bullish close →
        next bearish open; V-shape support = bearish close → next bullish open;
        wicks ignored by construction).  Step 2 validates at c+2 via
        :meth:`_snr_book_validation` with the MISS fresh filter.  Runs alongside
        the core N+3 + M15 flow and marriage — neither is touched.  Entry is the
        validation close, SL the validation wick (floored at SNR_MIN_STOP_PIPS),
        TP a 2R placeholder that :meth:`_apply_strength_rr` re-derives.
        """
        h4 = self._cache.get(symbol, H4_TF_SECONDS)
        if not h4 or len(h4) < 8:
            return None
        pairs = (
            self._snr_levels(h4, "support") + self._snr_levels(h4, "resistance")
        )
        bump_funnel(symbol, "pairs_found", len(pairs))
        best: Optional[SignalDraft] = None
        for level in sorted(pairs, key=lambda lv: lv["index"]):
            direction = "BUY" if level["kind"] == "support" else "SELL"
            hit = self._snr_book_validation(h4, level, direction)
            if hit is None:
                continue
            idx = hit["candle_index"]
            # Live edge only: the validation must sit in the recent 4H window,
            # mirroring the core flow's recency guard.
            if idx < len(h4) - SNR_H4_RECENT_BARS:
                continue
            bump_funnel(symbol, "rejections_found")
            c = h4[idx]
            entry = float(c["close"])
            wick = float(c["low"]) if direction == "BUY" else float(c["high"])
            pip = 0.01 if entry > 50 else 0.0001
            floor = SNR_MIN_STOP_PIPS * pip
            if direction == "BUY":
                sl = min(wick, entry - floor)
                if sl >= entry:
                    continue
            else:
                sl = max(wick, entry + floor)
                if sl <= entry:
                    continue
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            
            # Use quantitative TP with structural cap + reachability (1R partial + trail in backtest)
            tp, risk, tp_evidence = self._calculate_proper_tp(
                symbol, direction, entry, h4, sl,
            )
            if tp is None:
                continue
            nxt = h4[idx + 1] if idx + 1 < len(h4) else None
            entry_time = self._epoch((nxt or c).get("open_time"))
            confidence = round(
                min(100.0, MALAYSIAN_CONF_BASE
                    + (MALAYSIAN_CONF_CLEAN if hit["clean"] else 0.0)), 1,
            )
            draft = SignalDraft(direction=direction, entry=entry,
                                stop_loss=sl, take_profit=tp)
            draft.confidence = confidence
            draft.risk_reward = round(abs(tp - entry) / risk, 2)
            draft.entry_time = float(entry_time) if entry_time is not None else None
            draft.evidence = {
                "analyzer": "snr",
                "snr_book_step12": 1.0,
                "snr_level_support": MALAYSIAN_CONF_BASE,
                "snr_rejection_clean": MALAYSIAN_CONF_CLEAN if hit["clean"] else 0.0,
                "snr_breakout_body": 0.0,
                "snr_risk_reward": draft.risk_reward,
                "snr_validated": 1.0,
                "snr_fresh": SNR_WEIGHT_FRESH,
                "snr_validation": SNR_WEIGHT_ENGULFING,
                "snr_tp_source": "risk_multiple",
                "snr_tp_r": draft.risk_reward,
                "snr_level": hit["price"],
                "snr_validation_time": hit["open_time"],
                "snr_4h_level": {
                    "kind": level["kind"],
                    "shape": "V" if level["kind"] == "support" else "A",
                    "t1": level["t1"], "y1": level["y1"],
                    "t2": level["t2"], "y2": level["y2"],
                    "index": level["index"],
                    "fresh": True, "price": hit["price"],
                },
                "snr_rejection": {
                    "clean": bool(hit["clean"]),
                    "validated": True,
                    "validation": "book_c2",
                    "body_first": False,
                    "rule3_ok": True,
                    "rule3_pending": False,
                    "fresh": True,
                    "candle_index": idx,
                    "open_time": hit["open_time"],
                    "price": hit["price"],
                },
                "entry_kind": "book_step12",
            }
            draft.explanation_parts = [
                f"book Step 1: 4H {level['kind']} ({level['y1']:.5f} → {level['y2']:.5f})",
                f"book Step 2: c+2 wick touch + body close back at {hit['price']:.5f}",
                f"fresh MISS — no wick touched the level since formation",
            ]
            # Structural TP cap: nearest swing beyond entry so _apply_strength_rr
            # never projects past real structure (Oct 2 SELL lesson: fixed 2R
            # TP 4017 never hit, price bottomed 4066 — cap at structure instead).
            tp_kind = "support" if direction == "SELL" else "resistance"
            structural = self._find_structural_level(h4, tp_kind, entry)
            if structural is not None:
                draft.evidence["snr_tp_level"] = {
                    "kind": structural["kind"],
                    "price": entry,
                    "level_price": structural["price"],
                    "source": "structural",
                }
                # Thin structure: nearest swing sits inside the 1.5R floor, so
                # the book target has no structural room — tag it (no scoring
                # change) so reviewers/consumers know the TP is a floor, and
                # the 1R-partial + breakeven is what protects the trade.
                cap_dist = abs(entry - structural["price"])
                if cap_dist < abs(entry - sl) * SNR_MIN_RR:
                    draft.evidence["thin_structure"] = True
            # Add quantitative TP evidence
            if tp_evidence:
                draft.evidence.update(tp_evidence)
            if best is None or draft.confidence > best.confidence:
                best = draft
        return best

    @staticmethod
    def _snr_swing(candles: list[dict], kind: str) -> "Optional[float]":
        """
        Most recent swing on ``candles[:n]``.

        ``kind="low"``: most recent candle whose low is lower than the 2 candles
        on each side; ``kind="high"``: mirror with highs.  Favours the most
        recent swing (searches from the right).  Returns ``None`` when fewer
        than 5 candles are available or no swing exists.
        """
        n = len(candles)
        if n < 5:
            return None
        key = "low" if kind == "low" else "high"
        for i in range(n - 3, 1, -1):
            v = float(candles[i][key])
            if kind == "low":
                if v < float(candles[i - 1]["low"]) and v < float(candles[i - 2]["low"]) \
                        and v < float(candles[i + 1]["low"]) and v < float(candles[i + 2]["low"]):
                    return v
            else:
                if v > float(candles[i - 1]["high"]) and v > float(candles[i - 2]["high"]) \
                        and v > float(candles[i + 1]["high"]) and v > float(candles[i + 2]["high"]):
                    return v
        return None

    def _structural_stop(
        self, m15: list[dict], direction: str, entry: float, entry_index: int,
        min_pips: float = 0.0,
    ) -> "Optional[float]":
        """
        Book stop: below the LOWER LOW for a buy, above the HIGHER HIGH for a
        sell — a STRUCTURAL swing, not the retest candle's wick.

        Reuses the core setup's stop logic (:meth:`_snr_swing` with a
        last-10-candles fallback) so every analyzer places a stop of meaningful
        size.  The retest-wick "stop" was a fraction of a pip — well inside one
        M15 candle's noise — which collapsed the win rate to near-zero.  Returns
        None when no valid stop can be placed (swing already past entry).
        """
        swing_kind = "low" if direction == "BUY" else "high"
        swing = self._snr_swing(m15[:entry_index], swing_kind)
        if swing is None:
            tail = m15[max(0, entry_index - 10): entry_index]
            if tail:
                swing = (
                    min(float(c["low"]) for c in tail)
                    if direction == "BUY"
                    else max(float(c["high"]) for c in tail)
                )
        if swing is None:
            return None
        if direction == "BUY":
            if swing >= entry:
                return None
        else:
            if swing <= entry:
                return None
        # Optional floor: widen a too-tight stop to a minimum pip distance so it
        # clears normal M15 noise (guards degenerate sub-pip swings).
        if min_pips > 0:
            pip = 0.01 if entry > 50 else 0.0001
            if abs(entry - swing) < min_pips * pip:
                adj = min_pips * pip
                swing = entry - adj if direction == "BUY" else entry + adj
        return swing

    def _build_snr_setup(self, m15: list[dict], rej: dict, symbol: str = "") -> "Optional[SignalDraft]":
        """
        Build a SignalDraft from a confirmed 4H rejection, verified on 15M.

        TOP-DOWN, THE WAY THE BOOK READS IT: the HIGHER timeframe (4H) carries
        the analysis — the SNR level and its N+3 rejection, already validated by
        :meth:`_snr_check_rejection` and :meth:`_snr_book_gate`.  The LOWER
        timeframe (15M) does the verification, the confirmation and the entry:
        :meth:`_snr_m15_confirmation` looks BACKWARD from the live edge for the
        freshest 15M candle that wicks the 4H line and closes back on the trade's
        side, and we fill at the next 15M open.

        ARCHITECTURE FIX: this used to build a SECOND trendline from 15M candles
        and enter on a BREAKOUT of it, i.e. it did its "analysis" on the entry
        timeframe and chased price.  Measured on XAGUSD that scored a 7-43% win
        rate.  MARRIAGE — the only setup with a real edge — analyses on 4H and
        enters on an M15 wick rejection; the core SNR now does the same.

        Why the backward scan keeps signals current: it always resolves to the
        confirmation candle the market is printing NOW.  A forward scan anchored
        on the 4H rejection latched onto the first 15M reaction hours earlier,
        and a stale breakout then shadowed every later tradeable one (~85% of
        valid setups were dropped).

        ENTRY — the open of the candle right after the 15M rejection.  SL is the
        most recent 15M swing low/high before that entry (a structural stop, never
        the rejection wick), floored at SNR_MIN_STOP_PIPS.  TP is the NEXT fresh
        SNR level in the trade direction (book rule 1).  Returns ``None`` when any
        step of the sequence is incomplete.
        """
        direction = rej["direction"]
        # The 4H SNR level IS the decision level — no second M15 trendline.
        h4_level = rej["trendline"]
        rej_time = self._epoch(rej.get("open_time")) or 0.0

        conf = self._snr_m15_confirmation(m15, h4_level, direction, rej_time)
        if conf is None:
            return None
        entry_index = int(conf["entry_index"])
        if entry_index >= len(m15):
            return None
        # The fill must happen at the live edge — an entry too far back is a
        # stale price the market no longer trades at.
        if entry_index < len(m15) - SNR_BREAKOUT_MAX_AGE:
            return None
        # Entry: the confirmation candle closes → fill at the OPEN of the next
        # 15M bar (book: enter immediately as the bar completes, not mid-bar).
        entry = float(m15[entry_index]["open"])
        entry_kind = "ltf_zone_retest"
        touch_index = int(conf["retest_index"])
        bump_funnel(symbol, "breakouts_found")

        # Protective stop: most recent 15M swing low (buy) / swing high (sell),
        # before the entry candle.  Fall back to the extreme of the last 10
        # closed candles when no clean swing exists.
        swing_kind = "low" if direction == "BUY" else "high"
        swing = self._snr_swing(m15[:entry_index], swing_kind)
        if swing is None:
            tail = m15[max(0, entry_index - 10): entry_index]
            if tail:
                swing = (
                    min(float(c["low"]) for c in tail)
                    if direction == "BUY"
                    else max(float(c["high"]) for c in tail)
                )
        if swing is None:
            return None

        if direction == "BUY":
            if swing >= entry:
                return None
            sl = swing
        else:
            if swing <= entry:
                return None
            sl = swing

        risk = abs(entry - sl)
        if risk <= 0:
            return None

        # Book rule 1 (strict): TP at the next fresh SNR level in the trade
        # direction.  No fresh level beyond the entry => no tradable target.
        #
        # Levels are formed from candles closing before the ENTRY candle, not
        # before the 4H rejection.  The entry moved later (it is now the M15
        # rejection at the live edge), so capping level formation at the old
        # rejection cutoff would forbid levels that had already formed by the
        # time we actually traded — no look-ahead either way.
        tp_result = self._snr_take_profit(
            m15, direction, entry, sl, m15[entry_index], entry_index,
        )
        if tp_result is None:
            return None
        tp, tp_source, tp_level = tp_result

        confidence = MALAYSIAN_CONF_BASE
        if rej.get("clean"):
            confidence += MALAYSIAN_CONF_CLEAN
        # Body strength of the M15 CONFIRMATION candle — the same "big body"
        # bonus the other setups award, now earned by the rejection that
        # actually triggers the entry.
        mc = m15[touch_index]
        m_range = float(mc["high"]) - float(mc["low"])
        m_body = abs(float(mc["close"]) - float(mc["open"]))
        body_bonus = MALAYSIAN_CONF_BREAKOUT_BODY if m_range > 0 and m_body / m_range > 0.5 else 0.0
        confidence += body_bonus
        confidence = round(min(100.0, confidence), 1)

        draft = SignalDraft(direction=direction, entry=entry, stop_loss=sl, take_profit=tp)
        draft.confidence = confidence
        draft.risk_reward = round(abs(tp - entry) / risk, 2)
        draft.entry_time = self._epoch(m15[entry_index].get("open_time"))
        if self._snr_stale(draft.entry_time, m15):
            bump_funnel(symbol, "stale_dropped")
            return None
        validated = bool(rej.get("validated"))
        fresh = bool(rej.get("fresh"))
        validation = rej.get("validation")
        draft.evidence = {
            "analyzer": "snr",
            "snr_level_support": MALAYSIAN_CONF_BASE,
            "snr_rejection_clean": MALAYSIAN_CONF_CLEAN if rej.get("clean") else 0.0,
            "snr_breakout_body": body_bonus,
            "snr_risk_reward": draft.risk_reward,
            "snr_validated": 1.0 if validated else 0.0,
            # Book rules 2/3 — float weights the contradiction engine sums.
            "snr_fresh": SNR_WEIGHT_FRESH if fresh else 0.0,
            "snr_validation": (SNR_WEIGHT_ENGULFING if validation == "engulfing"
                               else SNR_WEIGHT_PIN_BAR if validation == "pin_bar" else 0.0),
            # Book rule 1 — where the target came from.  String keys are ignored
            # by the contradiction engine; the float carries the size.
            "snr_tp_source": tp_source,
            "snr_tp_r": draft.risk_reward,
            "snr_4h_level": {"kind": "support" if direction == "BUY" else "resistance",
                             "shape": "V" if direction == "BUY" else "A",
                             "t1": rej["trendline"]["t1"], "y1": rej["trendline"]["y1"],
                             "t2": rej["trendline"]["t2"], "y2": rej["trendline"]["y2"],
                             "price": rej["price"],
                             "candle_index": rej["candle_index"],
                             "fresh": fresh},
            "snr_rejection": {"clean": bool(rej.get("clean")),
                              "validated": validated,
                              "candle_index": rej["candle_index"],
                              "validation": validation,
                              "fresh": fresh,
                              "body_first": bool(rej.get("body_first")),
                              "rule3_ok": bool(rej.get("rule3_ok")),
                              "rule3_pending": bool(rej.get("rule3_pending"))},
            # The LTF does the confirming, not the deciding: this is the 15M
            # wick-rejection at the 4H line that authorised the entry.
            "snr_m15_confirmation": {
                "zone_index": conf["zone_index"],
                "breakout_index": conf["breakout_index"],
                "retest_index": conf["retest_index"],
                "zone_low": conf["zone_low"],
                "zone_high": conf["zone_high"],
                "line_value": conf["line_value"],
                "clean": bool(conf["clean"]),
                "body_fraction": round(m_body / m_range, 3) if m_range > 0 else 0.0,
            },
            "snr_tp_level": (tp_level if tp_level is not None else None),
            "entry_kind": entry_kind,
            "entry_index": entry_index,
        }
        draft.explanation_parts = [
            f"4H {'support' if direction == 'BUY' else 'resistance'} trendline "
            f"({'V' if direction == 'BUY' else 'A'}-shape) @ {rej['price']:.5f} "
            f"(extended to 4H candle {rej['candle_index']})",
            "4H rejection "
            f"{'at support (low touched, close above)' if direction == 'BUY' else 'at resistance (high touched, close below)'}",
            "4H level validated by a MISS (price never tagged the line)" if validated
            else "aggressive cSNR (price had already tagged the level)",
            f"rejection candle is a {'bullish' if direction == 'BUY' else 'bearish'} "
            f"{'ENGULFING' if validation == 'engulfing' else 'PIN BAR'} (NO VALIDATION, NO TRADE)"
            if validation else "rejection candle validated nothing",
            "SNR level is FRESH (no wick break since it formed)" if fresh
            else "SNR level UNFRESH (a wick pierced it before the rejection)",
            f"15M {'bullish' if direction == 'BUY' else 'bearish'} ENGULFING zone at the "
            f"4H line @ {conf['line_value']:.5f} (15M candle {conf['zone_index']})",
            f"15M breakout beyond the zone (15M candle {conf['breakout_index']})",
            f"15M RETEST of the zone rejected (15M candle {conf['retest_index']})",
            f"entry at the next 15M open {entry:.5f} (candle {entry_index}), SL {sl:.5f}",
            f"TP {tp:.5f} at the next fresh SNR level",
        ]
        return draft

    def _build_snr_draft(self, symbol: str) -> "Optional[SignalDraft]":
        """
        Run the Malaysian SNR analysis and return the best complete setup.

        Shared by the status path (``_analyze_snr``) and the signal path
        (``_evaluate_snr``).  Needs >= 8 candles on both the 4H and 15M cache;
        computes the most recent LIVE 4H support/resistance (preferring a FRESH
        one), checks the N+3 rejection for each side, drops every rejection the
        book's hard filters refuse (NO VALIDATION NO TRADE, first touch must be
        a wick, the 2nd candle must not close beyond), then builds a setup from
        the aligned 15M breakout.  Returns the higher-confidence setup or None.
        """
        h4 = self._cache.get(symbol, 14400)
        m15 = self._cache.get(symbol, 900)
        if not h4 or len(h4) < 8 or not m15 or len(m15) < 8:
            return None

        support = self._snr_tradable_trendline(h4, "support")
        resistance = self._snr_tradable_trendline(h4, "resistance")
        bump_funnel(symbol, "pairs_found", bool(support) + bool(resistance))
        rejections = [
            rej for rej in (
                self._snr_check_rejection(h4, support, "BUY") if support else None,
                self._snr_check_rejection(h4, resistance, "SELL") if resistance else None,
            ) if rej is not None
        ]
        bump_funnel(symbol, "rejections_found", len(rejections))

        candidates: list[SignalDraft] = []
        for rej in rejections:
            dropped = self._snr_book_gate(symbol, rej)
            if dropped:
                bump_funnel(symbol, dropped)
                continue
            draft = self._build_snr_setup(m15, rej, symbol)
            if draft is not None:
                candidates.append(draft)
        if not candidates:
            return None
        candidates.sort(key=lambda d: d.confidence, reverse=True)
        return candidates[0]

    @staticmethod
    def _snr_book_gate(symbol: str, rej: dict) -> str:
        """
        The book's hard filters on a rejection — returns the funnel stage that
        killed it, or "" when the setup may be traded.

        1. NO VALIDATION, NO TRADE (p.46): the rejection candle must be an
           engulfing or a pin bar.
        2. First touch must be a SHADOW (p.16/p.20): a body-first touch is an
           invalid setup.
        3. The candle after the touch must not close back beyond the level
           (p.20).
        """
        if not rej.get("validation"):
            logger.info("[SNR] %s %s dropped — rejection candle is neither engulfing nor pin bar",
                        symbol, rej.get("direction"))
            return "validation_dropped"
        if rej.get("body_first"):
            logger.info("[SNR] %s %s dropped — first touch of the SNR level was a BODY",
                        symbol, rej.get("direction"))
            return "body_first_dropped"
        if not rej.get("rule3_ok", True):
            logger.info("[SNR] %s %s dropped — 2nd candle closed back beyond the SNR level",
                        symbol, rej.get("direction"))
            return "rule3_dropped"
        return ""

    # ── Malaysian SNR course extensions ───────────────────────────────────
    #   • ENGULFING  — an H4 GAP SNR refined into an LTF engulfing of the same
    #     direction ("H4 GAP SNR REFINED INTO LTF ENGULFING").  The gap is the
    #     H4 decision level; the LTF engulfing is the refinement that manifests
    #     the rejection, and it is the entry.
    #   • MARRIAGE   — two SNRs of one kind married into a sloped trendline
    #     (SNR + trendline); the trade is only taken at point #3, the extension
    #     past the second SNR, where a wick touches and rejects the line.
    #   Both build the same SignalDraft shape as the core SNR flow so they pass
    #   through the identical polish/contradiction/lifecycle path.

    @staticmethod
    def _snr_stale(
        entry_time: "Optional[float]", m15: list[dict], max_bars: int = SNR_STALE_BARS,
    ) -> bool:
        """
        True when the entry candle is too old to still be tradeable.

        The course analyzers scan the whole 15M cache, so without a recency
        window a gap/marriage setup from days ago re-fires on every subsequent
        15M close.  Mirrors the SNR rejection guard: at most ``max_bars`` 15M
        candles (~4h) may sit between the fill and the last closed bar.
        """
        if entry_time is None or not m15:
            return True
        last = SignalEngine._epoch(m15[-1].get("open_time"))
        if last is None:
            return True
        return (last - entry_time) >= max_bars * M15_TF_SECONDS

    @staticmethod
    def _funnel_symbol(*candle_lists: list[dict]) -> str:
        """Symbol off the first raw candle that carries one (funnel bucketing)."""
        for candles in candle_lists:
            for c in candles or ():
                sym = c.get("symbol")
                if sym:
                    return str(sym)
                break
        return ""

    def _analyze_engulfing(
        self, h4_candles: list[dict], m15_candles: list[dict],
    ) -> "Optional[SignalDraft]":
        """
        H4 GAP SNR refined into an LTF (15M) engulfing → SignalDraft.

        Takes the most recent H4 open/close gap as the decision level, refines it
        into the first 15M engulfing of the same direction inside that H4 gap
        candle's own window, and enters at the candle after the engulfing with
        the SL at the engulfing wick and TP at 2x risk.  Returns ``None`` when
        there is no recent gap, no matching engulfing, or the resulting entry is
        already stale.
        """
        h4 = self._module_candles(h4_candles)
        m15 = self._module_candles(m15_candles)
        if len(h4) < 3 or len(m15) < 3:
            return None
        symbol = self._funnel_symbol(h4_candles, m15_candles)

        gap_index: Optional[int] = None
        gap_dir: Optional[str] = None
        for i in range(len(h4) - 1, 0, -1):
            hit = detect_gap(h4, i)
            if hit:
                gap_index, gap_dir = i, hit
                break
        if gap_index is None:
            return None
        bump_funnel(symbol, "engulfing_gaps_found")

        refined = find_engulfing_in_gap(h4, gap_index, m15, M15_TF_SECONDS)
        if refined is None:
            return None
        setup = engulfing_entry(m15, refined["index"])
        if setup is None:
            return None
        eng_i = int(setup["candle_index"])

        direction = "BUY" if setup["direction"] == "bullish" else "SELL"
        # Book p.49/p.73: "ENTER AT RETEST OF LTF ENGULFING ZONE".  The zone is
        # the engulfing candle's own high-low range.  After the engulfing closes
        # we require price to RETEST that zone and reject from it (wick into the
        # zone, body back out in the trade direction) before entering — entering
        # at the bare next-open skipped this retest and lost 0-for-9 across the
        # 2021+2022 backtests.  No retest => the setup is not confirmed.
        eng_candle = m15[eng_i]
        zone_low, zone_high = float(eng_candle["low"]), float(eng_candle["high"])
        retest_i = None
        for j in range(eng_i + 1, min(eng_i + 1 + ENGULFING_RETEST_WINDOW, len(m15) - 1)):
            c = m15[j]
            if direction == "BUY":
                # pull back into the zone, close back above its top
                if float(c["low"]) <= zone_high and float(c["close"]) > zone_high:
                    retest_i = j
                    break
            else:
                if float(c["high"]) >= zone_low and float(c["close"]) < zone_low:
                    retest_i = j
                    break
        if retest_i is None:
            bump_funnel(symbol, "engulfing_no_retest")
            return None

        entry = float(m15[retest_i + 1]["open"])  # fill at next bar's open when retest closes
        # Book stop: structural swing low/high, not the retest wick (which was
        # sub-pip and inside M15 noise).
        sl = self._structural_stop(m15, direction, entry, retest_i + 1, min_pips=SNR_MIN_STOP_PIPS)
        if sl is None:
            bump_funnel(symbol, "engulfing_no_stop")
            return None
        risk = abs(entry - sl)
        if risk <= 0:
            return None
        entry_time = self._epoch(m15[retest_i + 1].get("open_time"))
        if entry_time is None or self._snr_stale(float(entry_time), m15):
            bump_funnel(symbol, "stale_dropped")
            return None

        # Book rule 1 (strict): target the next fresh SNR level beyond the entry.
        # No fresh level => no tradable target.
        tp_result = self._snr_take_profit(
            m15, direction, entry, sl, m15[retest_i + 1], retest_i + 1,
        )
        if tp_result is None:
            return None
        tp, tp_source, tp_level = tp_result

        rng = float(eng_candle["high"]) - float(eng_candle["low"])
        body = abs(float(eng_candle["close"]) - float(eng_candle["open"]))
        body_fraction = (body / rng) if rng > 0 else 0.0
        body_bonus = MALAYSIAN_CONF_BREAKOUT_BODY if body_fraction > 0.5 else 0.0

        draft = SignalDraft(direction=direction, entry=entry, stop_loss=sl, take_profit=tp)
        draft.confidence = round(min(100.0, MALAYSIAN_CONF_BASE + body_bonus), 1)
        draft.risk_reward = round(abs(tp - entry) / risk, 2)
        draft.entry_time = float(entry_time)
        draft.evidence = {
            "analyzer": "engulfing",
            "engulfing_gap": 1.0,
            "engulfing_body": 1.0,
            "engulfing_risk_reward": draft.risk_reward,
            "engulfing_breakout_body": body_bonus,
            "snr_tp_source": tp_source,
            "snr_tp_r": draft.risk_reward,
            "snr_tp_level": (tp_level if tp_level is not None else None),
            "engulfing_h4_gap": {
                "kind": "gap", "shape": gap_dir, "candle_index": gap_index,
                "open_time": h4[gap_index]["open_time"],
            },
            "engulfing_candle": {
                "candle_index": eng_i, "direction": setup["direction"],
                "body_fraction": round(body_fraction, 3),
            },
            "engulfing_retest": {
                "candle_index": retest_i, "zone_low": zone_low, "zone_high": zone_high,
            },
        }
        draft.explanation_parts = [
            f"H4 {gap_dir} GAP SNR (candle {gap_index}) — the decision level",
            f"15M {setup['direction']} engulfing refined inside the gap (candle {eng_i}, "
            f"body {body_fraction:.0%} of range)",
            f"retest of the engulfing zone rejected (15M candle {retest_i})",
            f"entry at next 15M open {entry:.5f}, SL {sl:.5f}, "
            f"TP {tp:.5f} at the next fresh SNR level (R:R {draft.risk_reward:.2f})",
        ]
        self._trend_tag(symbol, draft)
        bump_funnel(symbol, "engulfing_signals")
        return draft

    def _analyze_marriage(
        self, h4_candles: list[dict], m15_candles: list[dict],
    ) -> "Optional[SignalDraft]":
        """
        MARRIAGE CONCEPT — two 4H SNRs married into one trendline, traded at
        point #3 → SignalDraft.

        Collects the last few 4H support and 4H resistance SNR trendlines,
        marries each kind's two most recent SNRs into a single sloped line, and
        takes the first kind whose line prints a point-#3 wick touch + rejection
        on 15M.  The kind whose most recent level formed latest is tried first
        (freshest structure wins).  Returns ``None`` when no kind has two
        levels, no line holds, or the entry is already stale.
        """
        if len(h4_candles) < 8 or len(m15_candles) < 8:
            return None
        m15 = self._module_candles(m15_candles)
        symbol = self._funnel_symbol(h4_candles, m15_candles)

        # Most recent level first — its kind is the freshest structure.
        orders = sorted(
            (self._snr_levels(h4_candles, kind) for kind in ("support", "resistance")),
            key=lambda levels: levels[-1]["t2"] if levels else float("-inf"),
            reverse=True,
        )
        for levels in orders:
            if len(levels) < 2:
                continue
            # marriage_trendline picks levels[:2] as "the two most recent".
            line = marriage_trendline(list(reversed(levels)), levels[-1]["kind"])
            if line is None:
                continue
            bump_funnel(symbol, "marriage_lines_found")
            hit = entry_at_point3(line, m15, 0)
            if hit is None:
                continue

            entry = float(hit["entry"])
            sl = float(hit["sl"])
            # Enforce a minimum stop distance.  The point-#3 wick can sit a
            # fraction of a pip from entry (we measured 0.6–1.0 pips on
            # EURUSD) — below spread, so the stop is taken on noise and the win
            # is illusory.  Widen to at least SNR_MIN_STOP_PIPS.
            if abs(entry - sl) < abs(entry) * SNR_MIN_STOP_PIPS * 1e-4:
                adj = abs(entry) * SNR_MIN_STOP_PIPS * 1e-4
                sl = entry - adj if hit["direction"] == "buy" else entry + adj
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            touch_index = int(hit["touch_index"])
            entry_time = m15[touch_index + 1]["open_time"]
            if self._snr_stale(float(entry_time), m15):
                bump_funnel(symbol, "stale_dropped")
                continue

            direction = "BUY" if hit["direction"] == "buy" else "SELL"
            # Book rule 1 (strict): target the next fresh SNR level beyond the
            # entry.  No fresh level => not tradable, skip this point.
            tp_result = self._snr_take_profit(
                m15, direction, entry, sl, m15[touch_index + 1], touch_index + 1,
            )
            if tp_result is None:
                continue
            tp, tp_source, tp_level = tp_result

            # Clean point-#3 rejection: the touch candle closed decisively back
            # beyond the line, not just barely on the right side of it.
            lv = float(hit["line_value"])
            touch_close = float(m15[touch_index]["close"])
            clean = (touch_close > lv * (1 + SNR_CLEAN_PCT) if line["kind"] == "support"
                     else touch_close < lv * (1 - SNR_CLEAN_PCT))
            confidence = round(
                min(100.0, MALAYSIAN_CONF_BASE + (MALAYSIAN_CONF_CLEAN if clean else 0.0)), 1
            )
            draft = SignalDraft(direction=direction, entry=entry, stop_loss=sl, take_profit=tp)
            draft.confidence = confidence
            draft.risk_reward = round(abs(tp - entry) / risk, 2)
            draft.entry_time = float(entry_time)
            draft.evidence = {
                "analyzer": "marriage",
                "marriage_trendline": 1.0,
                "marriage_touch": 1.0,
                "marriage_risk_reward": draft.risk_reward,
                "snr_tp_source": tp_source,
                "snr_tp_r": draft.risk_reward,
                "snr_tp_level": (tp_level if tp_level is not None else None),
                "marriage_line": {
                    "kind": line["kind"],
                    "t1": line["t1"], "y1": line["y1"],
                    "t2": line["t2"], "y2": line["y2"],
                    "slope": line["slope"],
                    "levels_used": line["levels_used"],
                },
                "marriage_point3": {
                    "touch_index": hit["touch_index"],
                    "line_value": hit["line_value"],
                    "clean": bool(clean),
                },
            }
            draft.explanation_parts = [
                f"two 4H {line['kind']} SNRs married into one trendline "
                f"({line['y1']:.5f} → {line['y2']:.5f})",
                f"point #3 wick touch + rejection at {hit['line_value']:.5f} "
                f"(15M candle {hit['touch_index']})",
                f"entry at next 15M open {entry:.5f}, SL {sl:.5f}, "
                f"TP {tp:.5f} at the next fresh SNR level (R:R {draft.risk_reward:.2f})",
            ]
            bump_funnel(symbol, "marriage_signals")
            return draft
        return None

    # ── FLIPPED SNR (RBS / SBR) — book p.11-12 ────────────────────────────

    def _flip_retest(
        self, m15: list[dict], level: dict, direction: str,
    ) -> "Optional[dict]":
        """
        The break-then-retest sequence of a FLIPPED SNR.

        RBS (``direction`` "BUY"): the resistance ``level`` is decisively broken
        by a candle whose BODY closes above it (the flip to support), then price
        RETESTS it — a later candle wicks down to the line and closes back above
        (rejection from support).  SBR is the mirror (support broken, flips to
        resistance, retest rejects from above → SELL).

        Returns the break/retest indices and the line value at the retest, or
        None when the level never flips or is never retested.
        """
        to_support = direction == "BUY"
        broke_at: Optional[int] = None
        for i in range(level["index"] + 1, len(m15) - 1):
            lv = self._snr_line_value(level, m15[i])
            tol = abs(lv) * SNR_TOUCH_PCT
            close = float(m15[i]["close"])
            if broke_at is None:
                # The decisive BODY break is the flip.
                if to_support and close > lv + tol:
                    broke_at = i
                elif (not to_support) and close < lv - tol:
                    broke_at = i
                continue
            # After the flip, the retest must wick the line and reject from the
            # new side (wick touches, body closes back on the original side).
            # A tradeable retest is happening at the LIVE EDGE — only accept one
            # within SNR_RETEST_MAX_AGE candles of the current bar, otherwise the
            # entry is a stale price the market no longer trades at.
            if i < len(m15) - 1 - SNR_RETEST_MAX_AGE:
                break
            if to_support:
                if float(m15[i]["low"]) <= lv + tol and close > lv:
                    return {"break_index": broke_at, "retest_index": i, "line_value": lv}
            else:
                if float(m15[i]["high"]) >= lv - tol and close < lv:
                    return {"break_index": broke_at, "retest_index": i, "line_value": lv}
        return None

    def _analyze_flip(self, symbol: str) -> "Optional[SignalDraft]":
        """
        Book p.11-12 FLIPPED SNR (RBS resistance→support, SBR support→
        resistance).  A level that is decisively BODY-broken flips sides, and the
        trade is the RETEST of the flipped level.  TP is the next fresh SNR
        (strict book rule 1); no fresh level → not tradable.
        """
        m15 = self._cache.get(symbol, M15_TF_SECONDS)
        if not m15 or len(m15) < 8:
            return None
        for kind, direction in (("resistance", "BUY"), ("support", "SELL")):
            for level in self._snr_levels(m15, kind, max_levels=2):
                hit = self._flip_retest(m15, level, direction)
                if hit is None:
                    continue
                bump_funnel(symbol, "flip_retests_found")
                ri = int(hit["retest_index"])
                entry = float(m15[ri + 1]["open"])  # fill at next bar's open when retest closes
                # Book stop: structural swing low/high, not the retest wick.
                sl = self._structural_stop(m15, direction, entry, ri + 1, min_pips=SNR_MIN_STOP_PIPS)
                if sl is None:
                    continue
                risk = abs(entry - sl)
                if risk <= 0:
                    continue
                entry_time = self._epoch(m15[ri + 1].get("open_time"))
                if entry_time is None or self._snr_stale(float(entry_time), m15):
                    bump_funnel(symbol, "stale_dropped")
                    continue
                tp_result = self._snr_take_profit(m15, direction, entry, sl, m15[ri + 1], ri + 1)
                if tp_result is None:
                    continue
                tp, tp_source, tp_level = tp_result
                lv = float(hit["line_value"])
                tol = abs(lv) * SNR_TOUCH_PCT
                rclose = float(m15[ri]["close"])
                clean = (rclose > lv + tol) if direction == "BUY" else (rclose < lv - tol)
                confidence = round(
                    min(100.0, MALAYSIAN_CONF_BASE + (MALAYSIAN_CONF_CLEAN if clean else 0.0)), 1
                )
                draft = SignalDraft(direction=direction, entry=entry,
                                    stop_loss=sl, take_profit=tp)
                draft.confidence = confidence
                draft.risk_reward = round(abs(tp - entry) / risk, 2)
                draft.entry_time = float(entry_time)
                draft.evidence = {
                    "analyzer": "flip",
                    "flip_snr": 1.0,
                    "flip_retest": 1.0,
                    "flip_risk_reward": draft.risk_reward,
                    "snr_tp_source": tp_source,
                    "snr_tp_r": draft.risk_reward,
                    "snr_tp_level": (tp_level if tp_level is not None else None),
                    "flip_level": {
                        "kind": kind,
                        "flipped_to": "support" if direction == "BUY" else "resistance",
                        "t1": level["t1"], "y1": level["y1"],
                        "t2": level["t2"], "y2": level["y2"],
                        "line_value": lv,
                        "break_index": hit["break_index"],
                        "retest_index": ri,
                        # EPOCH anchors for the dedup fingerprint: break_open_time
                        # is stable per level/flip, retest_open_time changes only
                        # on a NEW retest → the same retest refreshes in place,
                        # a genuinely new retest supersedes and can trade.
                        "break_open_time": self._epoch(m15[int(hit["break_index"])].get("open_time")),
                        "retest_open_time": self._epoch(m15[ri].get("open_time")),
                        "clean": bool(clean),
                    },
                }
                draft.explanation_parts = [
                    f"15M {kind} body-broken → FLIPPED to "
                    f"{'support (RBS)' if direction == 'BUY' else 'resistance (SBR)'}",
                    f"retest wick touched {lv:.5f} and rejected "
                    f"(15M candle {ri})",
                    f"entry at next 15M open {entry:.5f}, SL {sl:.5f}, "
                    f"TP {tp:.5f} at the next fresh SNR level (R:R {draft.risk_reward:.2f})",
                ]
                self._trend_tag(symbol, draft)
                bump_funnel(symbol, "flip_signals")
                return draft
        return None

    # ── QML / Head-and-Shoulder — book p.42 ───────────────────────────────

    def _qml_peak(
        self, candles: list[dict], direction: str, lookback: int = 40,
    ) -> "Optional[dict]":
        """
        The most recent QML/H&S PEAK: a swing extreme (high for a bearish QML →
        SELL, low for a bullish QML → BUY) that stands above/below its two
        candles on each side.  The book requires the peak candle to be an
        ENGULFING before price may retest it ("peak of QML must be an engulfing
        for price to retest it", p.42), so this returns None when the peak
        candle is not an engulfing.
        """
        if len(candles) < 7:
            return None
        want = "bearish" if direction == "SELL" else "bullish"
        is_high = direction == "SELL"
        start = max(2, len(candles) - 1 - lookback)
        mod = self._module_candles(candles)
        for i in range(len(candles) - 3, start - 1, -1):  # most recent peak first
            if is_high:
                v = float(candles[i]["high"])
                if not all(float(candles[i + k]["high"]) < v for k in (-1, -2, 1, 2)):
                    continue
            else:
                v = float(candles[i]["low"])
                if not all(float(candles[i + k]["low"]) > v for k in (-1, -2, 1, 2)):
                    continue
            # Book: the peak must be an engulfing for a retest to be valid.
            if detect_engulfing(mod, i) != want:
                continue
            return {"index": i, "price": v}
        return None

    def _analyze_qml(self, symbol: str) -> "Optional[SignalDraft]":
        """
        Book p.42 QML SNR + engulfing.  The QML peak must be an engulfing; price
        then RETESTS that peak and rejects it (wick touches, body closes back
        away) → trade the rejection.  TP is the next fresh SNR (strict rule 1);
        no fresh level → not tradable.
        """
        m15 = self._cache.get(symbol, M15_TF_SECONDS)
        if not m15 or len(m15) < 8:
            return None
        for direction in ("SELL", "BUY"):
            peak = self._qml_peak(m15, direction)
            if peak is None:
                continue
            pi = int(peak["index"])
            pv = float(peak["price"])
            tol = abs(pv) * SNR_TOUCH_PCT
            # Retest at the LIVE EDGE: search BACKWARD from the current bar for
            # the most recent retest of the QML peak, and require it to be recent
            # (SNR_RETEST_MAX_AGE).  Searching forward from the peak found the
            # OLDEST retest — up to 4h old — producing an entry price 30–46 pips
            # from the current market (a fabricated fill).  A tradeable QML
            # retest must be happening NOW.
            ri = None
            for j in range(len(m15) - 2, pi, -1):
                if j < len(m15) - 1 - SNR_RETEST_MAX_AGE:
                    break
                if direction == "SELL":
                    if float(m15[j]["high"]) >= pv - tol and float(m15[j]["close"]) < pv:
                        ri = j
                        break
                else:
                    if float(m15[j]["low"]) <= pv + tol and float(m15[j]["close"]) > pv:
                        ri = j
                        break
            if ri is None:
                continue
            bump_funnel(symbol, "qml_peaks_found")
            entry = float(m15[ri + 1]["open"])  # fill at next bar's open when retest closes
            # Book stop: structural swing low/high, not the retest wick.
            sl = self._structural_stop(m15, direction, entry, ri + 1, min_pips=SNR_MIN_STOP_PIPS)
            if sl is None:
                continue
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            entry_time = self._epoch(m15[ri + 1].get("open_time"))
            if entry_time is None or self._snr_stale(float(entry_time), m15):
                bump_funnel(symbol, "stale_dropped")
                continue
            tp_result = self._snr_take_profit(m15, direction, entry, sl, m15[ri + 1], ri + 1)
            if tp_result is None:
                continue
            tp, tp_source, tp_level = tp_result
            confidence = round(min(100.0, MALAYSIAN_CONF_BASE), 1)
            draft = SignalDraft(direction=direction, entry=entry,
                                stop_loss=sl, take_profit=tp)
            draft.confidence = confidence
            draft.risk_reward = round(abs(tp - entry) / risk, 2)
            draft.entry_time = float(entry_time)
            draft.evidence = {
                "analyzer": "qml",
                "qml_peak_engulfing": 1.0,
                "qml_retest": 1.0,
                "qml_risk_reward": draft.risk_reward,
                "snr_tp_source": tp_source,
                "snr_tp_r": draft.risk_reward,
                "snr_tp_level": (tp_level if tp_level is not None else None),
                "qml_peak": {
                    "index": pi, "price": pv, "retest_index": ri,
                    # EPOCH anchors for the dedup fingerprint (see flip_level):
                    # stable per peak, changes per new retest.
                    "peak_open_time": self._epoch(m15[pi].get("open_time")),
                    "retest_open_time": self._epoch(m15[ri].get("open_time")),
                },
            }
            draft.explanation_parts = [
                f"QML/H&S {'peak' if direction == 'SELL' else 'trough'} at {pv:.5f} "
                f"is an ENGULFING (15M candle {pi})",
                f"retest rejected the QML peak (15M candle {ri})",
                f"entry at next 15M open {entry:.5f}, SL {sl:.5f}, "
                f"TP {tp:.5f} at the next fresh SNR level (R:R {draft.risk_reward:.2f})",
            ]
            self._trend_tag(symbol, draft)
            bump_funnel(symbol, "qml_signals")
            return draft
        return None

    # ── Shared gates: storyline (direction) + session (timing) ───────────

    # ── Trend class: normal (with H1 trend) vs counter (against it) ─────────

    def _trend_tag(self, symbol: str, draft: "SignalDraft") -> str:
        """
        Classify a finished draft against the H1 storyline and, when it is a
        COUNTER-trend trade, mark it risky (lower confidence).

        Why this exists: MARRIAGE only ever wins because it is structurally
        WITH-trend (it buys the retest of a rising H4 trendline, sells a falling
        one).  The other book setups fired in BOTH directions and bled.  So each
        setup is now labelled with the trend it is riding:

          * ``normal``  — direction agrees with the H1 storyline (the marriage
            pattern).  Full confidence.
          * ``counter`` — direction fights the H1 storyline (the very risky
            class).  Kept, because the book trades both sides, but discounted
            via MALAYSIAN_CONF_COUNTER so it ranks below a normal setup.
          * ``neutral`` — H1 is sideways or too thin to read; no trend to ride,
            confidence untouched.

        Mutates ``draft`` in place (evidence + confidence) and returns the class.
        """
        side = "bullish" if draft.direction == "BUY" else "bearish"
        h1 = self._module_candles(self._cache.get(symbol, H1_TF_SECONDS))
        h1_dir = tf_direction(h1, SNR_TREND_H1_LOOKBACK) if h1 and len(h1) >= 2 else "sideways"

        if h1_dir == "sideways":
            trend_class = "neutral"
        elif h1_dir == side:
            trend_class = "normal"
        else:
            trend_class = "counter"

        if trend_class == "counter":
            draft.confidence = round(max(0.0, draft.confidence + MALAYSIAN_CONF_COUNTER), 1)
            bump_funnel(symbol, f"{draft.evidence.get('analyzer', 'setup')}_counter")

        draft.evidence["trend_class"] = trend_class
        draft.evidence["h1_direction"] = h1_dir
        if trend_class == "counter":
            draft.explanation_parts.append(
                f"COUNTER-trend: against the H1 {h1_dir} storyline — risky, reduced confidence",
            )
        elif trend_class == "normal":
            draft.explanation_parts.append(
                f"with the H1 {h1_dir} storyline (normal, with-trend)",
            )
        return trend_class

    def _marriage_trend_class(self, symbol: str, direction: str) -> str:
        """``normal`` when direction agrees with the H1 storyline, else counter/neutral."""
        h1 = self._module_candles(self._cache.get(symbol, H1_TF_SECONDS))
        h1_dir = tf_direction(h1, SNR_TREND_H1_LOOKBACK) if h1 and len(h1) >= 2 else "sideways"
        side = "bullish" if direction == "BUY" else "bearish"
        if h1_dir == "sideways":
            return "neutral"
        return "normal" if h1_dir == side else "counter"

    def _storyline(self, symbol: str) -> "Optional[dict]":
        """
        Top-down storyline read (weekly/daily/h4/h1 + roadblocks), or None.

        Weekly candles are resampled from d1 inside the module.  Returns None
        when the d1/h1 history is too thin to trust a weekly direction — the
        caller then skips the filter rather than guessing a storyline.
        """
        d1 = self._module_candles(self._cache.get(symbol, D1_TF_SECONDS))
        h4 = self._module_candles(self._cache.get(symbol, H4_TF_SECONDS))
        h1 = self._module_candles(self._cache.get(symbol, H1_TF_SECONDS))
        if len(d1) < STORYLINE_MIN_D1 or not h4 or not h1:
            return None
        return analyze_storyline(d1, h4, h1)

    @staticmethod
    def _sessions_for(symbol: str) -> tuple[str, ...]:
        """Home sessions for ``symbol`` — case-insensitive, prime-liquidity default."""
        return SESSION_ALLOWED_BY_SYMBOL.get(symbol.upper(), SESSION_ALLOWED_DEFAULT)

    @staticmethod
    def _session_allowed(entry_time: "Optional[float]", symbol: str = "") -> bool:
        """
        Session gate on the fill time, against ``symbol``'s home sessions.

        Fails CLOSED on a missing timestamp, like :meth:`_snr_stale` on the same
        condition: an entry we cannot place in time is not one we can take.
        """
        if entry_time is None:
            return False
        return in_trade_session(float(entry_time), SignalEngine._sessions_for(symbol))

    def _entry_allowed(
        self, draft: SignalDraft, storyline: "Optional[dict]" = None, symbol: str = "",
    ) -> bool:
        """
        Storyline + session gate applied to a finished draft.

        STORYLINE — the course reads top-down and blocks a signal whose
        direction the weekly storyline cannot hold against the daily one
        (change-of-storyline).  SESSION — entries are only taken in the
        instrument's home sessions (AUDJPY: tokyo + sydney; everything else:
        london + newyork).
        """
        if not self._session_allowed(draft.entry_time, symbol):
            bump_funnel(symbol, "session_dropped")
            logger.info(
                "[SNR] %s %s dropped — entry outside %s",
                draft.evidence.get("analyzer", "setup"), draft.direction,
                self._sessions_for(symbol),
            )
            return False
        if storyline is not None:
            side = "bullish" if draft.direction == "BUY" else "bearish"
            if not storyline_allows(storyline["weekly"], storyline["daily"], side):
                bump_funnel(symbol, "storyline_dropped")
                logger.info(
                    "[SNR] %s %s dropped — storyline %s weekly vs %s daily",
                    draft.evidence.get("analyzer", "setup"), draft.direction,
                    storyline["weekly"], storyline["daily"],
                )
                return False
        return True

    def _book_confluence_points(
        self, symbol: str, price: float, direction: str, probe_t: float,
    ) -> list[str]:
        """
        Book p.39 — the CONVERGING POINTS that agree at ``price`` for a setup in
        ``direction``.  Each returned string is one structural point: an SNR
        level, a married trendline, a second SNR at the same price, or an
        engulfing zone overlapping it.  The book wants "two or three points of
        convergence giving the same direction"; the caller requires >= 2.
        """
        points: list[str] = []
        tol = abs(price) * SNR_TOUCH_PCT
        probe = {"open_time": probe_t}
        kind = "support" if direction == "BUY" else "resistance"

        h4 = self._cache.get(symbol, H4_TF_SECONDS)
        m15 = self._cache.get(symbol, M15_TF_SECONDS)

        # The primary fresh SNR level at the setup price (always one point).
        points.append("snr")

        # (a) SNR + Trendline — a married trendline of the trade's own kind whose
        #     value at the probe time converges on the SNR price.
        if h4:
            line = marriage_trendline(self._snr_levels(h4, kind, max_levels=3), kind)
            if line is not None and abs(line_value(line, probe_t) - price) <= tol * 2:
                points.append(f"trendline_{kind}")

        # (b) 2 SNRs converging at the same point — another level (either kind)
        #     whose extended value also lands on the SNR price.
        if h4:
            for other in ("support", "resistance"):
                for lvl in self._snr_levels(h4, other, max_levels=2):
                    if abs(self._snr_line_value(lvl, probe) - price) <= tol * 2:
                        points.append(f"snr2_{other}")
                        break

        # (c) HTF engulfing zone overlapping the SNR — the book pairs an
        #     engulfing zone with the SNR for confluence.
        if m15:
            want = "bullish" if direction == "BUY" else "bearish"
            for i in range(len(m15) - 1, 0, -1):
                if detect_engulfing(self._module_candles(m15[: i + 1]), i) == want:
                    c = m15[i]
                    if float(c["low"]) <= price + tol and float(c["high"]) >= price - tol:
                        points.append("engulfing_zone")
                    break

        return points

    def _book_confluence_draft(self, symbol: str) -> "Optional[SignalDraft]":
        """
        Book p.39 CONFLUENCE as a Malaysian-SNR candidate.

        The book's confluence is the *highest-probability* signal: a VALIDATED
        SNR setup (wick rejection, NO VALIDATION NO TRADE, LTF breakout, fresh
        SNR target — all enforced by the core :meth:`_build_snr_draft`) that ALSO
        has two or three book points converging at the level.  We therefore build
        the core setup and require >= 2 converging points at its price; without
        convergence it is a plain SNR trade, not a confluence.

        This replaces the old ICT OB/FVG/MSS/POC confluence, which is not the
        book's confluence at all.
        """
        core = self._build_snr_draft(symbol)
        if core is None:
            return None
        evidence = core.evidence or {}
        level = evidence.get("snr_4h_level") or {}
        price = float(level.get("price") or 0.0)
        if price <= 0:
            return None
        probe_t = core.entry_time if core.entry_time is not None else self._epoch(
            (self._cache.get(symbol, M15_TF_SECONDS) or [{}])[-1].get("open_time")
        )
        if probe_t is None:
            return None

        points = self._book_confluence_points(symbol, price, core.direction, float(probe_t))
        if len(points) < 2:
            bump_funnel(symbol, "confluence_single_point")
            return None

        core.evidence["analyzer"] = "confluence"
        core.evidence["confluence_points"] = points
        core.evidence["confluence_count"] = float(len(points))
        core.evidence["confluence"] = 1.0
        # A genuinely converged setup outranks a bare single-level SNR trade.
        core.confidence = round(min(100.0, core.confidence + 10.0 * (len(points) - 2)), 1)
        return core

    def _confluence_candidate(self, symbol: str) -> "Optional[SignalDraft]":
        """
        The CONFLUENCE analyzer as a Malaysian-SNR candidate (book p.39:
        "two or three points of convergence … SNR + Trendlines, or 2 SNRs
        converging at the same point").

        Built from the validated core SNR setup plus >= 2 converging book points
        (:meth:`_book_confluence_draft`) — the book's confluence, with no ICT
        OB/FVG scoring mixed in.
        """
        m15 = self._cache.get(symbol, M15_TF_SECONDS)
        if not m15:
            return None
        entry_time = self._epoch(m15[-1].get("open_time"))
        if self._snr_stale(entry_time, m15):
            bump_funnel(symbol, "stale_dropped")
            return None
        return self._book_confluence_draft(symbol)

    # Analyzer -> dynamic-RR strategy bucket (trade.md §3.2).  RR is then
    # derived from the setup's strength, not a fixed ratio.
    _RR_STRATEGY_BY_ANALYZER = {
        "snr": StrategyType.PULLBACK,
        "engulfing": StrategyType.REVERSAL,
        "marriage": StrategyType.TREND_CONTINUATION,
        "breakout": StrategyType.BREAKOUT,
        "flip": StrategyType.BREAKOUT,
        "qml": StrategyType.REVERSAL,
        "confluence": StrategyType.BREAKOUT,
    }

    def _apply_strength_rr(self, draft: SignalDraft) -> SignalDraft:
        """
        Re-derive the take profit from the SIGNAL STRENGTH (trade.md §3).

        A fixed 1:2 throws away signal quality: a 4-point confluence that the
        book rates as its highest-probability setup should be allowed to run
        further than a marginal one.  So map the draft's confidence to a
        0–1 strength, feed it through the dynamic-RR engine (power curve
        × volatility × regime × performance), and cap the reward at
        ``SNR_MAX_RR`` so a strong signal still has a realistic ceiling (we
        learned 9R+ targets never hit).  The RR is also capped by the
        structural capacity (nearest swing high/low or fresh SNR distance / risk).
        Returns the draft with take_profit and risk_reward updated.
        """
        risk = abs(draft.entry - draft.stop_loss)
        if risk <= 0:
            return draft
        analyzer = str((draft.evidence or {}).get("analyzer", "snr"))
        strategy = self._RR_STRATEGY_BY_ANALYZER.get(analyzer, StrategyType.PULLBACK)
        # Confidence (50 base + quality bonuses) -> 0–1 strength.
        strength = max(0.0, min(1.0, (float(draft.confidence) - MALAYSIAN_CONF_BASE) / 50.0))

        # Compute structural RR cap from the TP level (fresh SNR or structural swing)
        level = (draft.evidence or {}).get("snr_tp_level") or {}
        cap_price = level.get("level_price")
        structural_rr = None
        if isinstance(cap_price, (int, float)):
            is_buy = draft.direction == "BUY"
            cap_dist = (float(cap_price) - draft.entry) if is_buy else (draft.entry - float(cap_price))
            if cap_dist > 0:
                structural_rr = cap_dist / risk

        rr_calc = self._rr_service.calculate(
            strength=strength,
            strategy_type=strategy,
            atr_percentile=0.5,       # neutral volatility (no ATR feed in the SNR path)
            regime=Regime.WEAK_TREND,  # neutral regime
            recent_expectancy_r=None,
            historical_wr=None,       # no live history -> skip break-even gate
            structural_rr=structural_rr,  # cap dynamic RR at structural capacity
        )
        rr = rr_calc.final_rr if rr_calc.accepted else SNR_MIN_RR
        rr = max(SNR_MIN_RR, min(SNR_MAX_RR, rr))  # realistic 1.5–3.0 band
        is_buy = draft.direction == "BUY"
        tp = draft.entry + risk * rr if is_buy else draft.entry - risk * rr
        # The RRService already capped at structural_rr, but apply the level_price
        # cap again as a safety net (in case structural_rr was None but level exists).
        min_reward = risk * SNR_MIN_RR
        if isinstance(cap_price, (int, float)):
            cap_dist = (float(cap_price) - draft.entry) if is_buy else (draft.entry - float(cap_price))
            if cap_dist >= min_reward:  # cap is a real, tradeable target
                if is_buy:
                    tp = min(tp, float(cap_price))
                else:
                    tp = max(tp, float(cap_price))
        # Keep the target strictly beyond the entry (never at/inside it).
        if (is_buy and tp <= draft.entry) or (not is_buy and tp >= draft.entry):
            tp = draft.entry + min_reward if is_buy else draft.entry - min_reward
        draft.take_profit = tp
        draft.risk_reward = round(abs(tp - draft.entry) / risk, 2) if risk > 0 else rr
        draft.evidence["dynamic_rr"] = rr
        draft.evidence["dynamic_rr_strength"] = round(strength, 3)
        if structural_rr is not None:
            draft.evidence["structural_rr"] = round(structural_rr, 2)
        return draft

    def _htf_bias(self, symbol: str) -> str:
        """H4 EMA-50 bias for the confluence gate: 'buy' | 'sell' | '' (neutral).

        Price > 0.2% above EMA-50 → bullish → favour BUY; > 0.2% below →
        bearish → favour SELL; inside the band or < 50 H4 candles → neutral.
        """
        h4 = self._cache.get(symbol, H4_TF_SECONDS)
        if not h4 or len(h4) < 50:
            return ""
        try:
            closes = [float(c["close"]) for c in h4[-50:]]
        except (KeyError, TypeError, ValueError):
            return ""
        k = 2 / 51.0
        ema = closes[0]
        for c in closes[1:]:
            ema = c * k + ema * (1 - k)
        if not ema:
            return ""
        pct = (closes[-1] - ema) / ema
        if pct > 0.002:
            return "buy"
        if pct < -0.002:
            return "sell"
        return ""

    def _malaysian_candidates(self, symbol: str) -> list[SignalDraft]:
        """
        Every finished draft from the SNR-family analyzers, BEFORE the gates.

        Core SNR (4H rejection + 15M breakout), engulfing (H4 gap refined into
        a 15M engulfing), marriage (two SNRs married into a trendline, traded
        at point #3) and confluence (multi-TF convergence of 2–3 agreeing
        points).  Split out from :meth:`_best_malaysian_draft` so the status
        report can tell "no setup" apart from "setup blocked by a gate".
        Each analyzer enforces its own minimum history, so there is no shared
        length guard here.  Every finished draft then has its take profit
        re-derived from its SIGNAL STRENGTH (:meth:`_apply_strength_rr`) so the
        target scales with setup quality.
        """
        drafts = [d for d in (
            self._build_snr_draft(symbol),
            self._analyze_snr_book(symbol),
            self._analyze_engulfing(
                self._cache.get(symbol, H4_TF_SECONDS), self._cache.get(symbol, M15_TF_SECONDS),
            ),
            self._analyze_marriage(
                self._cache.get(symbol, H4_TF_SECONDS), self._cache.get(symbol, M15_TF_SECONDS),
            ),
            analyze_breakout(self, symbol),
            self._analyze_flip(symbol),
            self._analyze_qml(symbol),
            self._confluence_candidate(symbol),
        ) if d is not None]
        enabled = enabled_malaysian_analyzers()
        out: list[SignalDraft] = []
        for d in drafts:
            analyzer = (d.evidence or {}).get("analyzer")
            if analyzer not in enabled:
                bump_funnel(symbol, "analyzer_disabled_dropped")
                logger.debug(
                    "[SIGNAL] analyzer_disabled_dropped %s analyzer=%s direction=%s",
                    symbol, analyzer, d.direction,
                )
                continue
            out.append(self._apply_strength_rr(d))
        return out

    def _best_malaysian_draft(
        self, symbol: str, candidates: "Optional[list[SignalDraft]]" = None,
    ) -> list[SignalDraft]:
        """
        Every Malaysian-SNR-family setup for ``symbol`` that survives the gates.

        Runs each analyzer (core SNR / engulfing / marriage / flip / QML /
        confluence) and returns ALL surviving drafts, sorted by confidence
        descending.  The analyzers are INDEPENDENT setups — each emits its own
        signal and none suppresses the others — so this returns a LIST, not a
        single best draft.  Session/storyline gates are applied to each
        individually.  An empty list means no setup passed.
        """
        if candidates is None:
            candidates = self._malaysian_candidates(symbol)
        if not candidates:
            return []

        storyline = self._storyline(symbol)
        htf_bias = self._htf_bias(symbol)
        try:
            from forex.analysis.confluence_gate import check_hard_drop as _check_hard_drop
        except Exception:  # noqa: BLE001 — gate must never break the pipeline
            _check_hard_drop = None
        survivors: list[SignalDraft] = []
        for draft in candidates:
            if (draft.evidence or {}).get("analyzer") == "marriage" and marriage_with_trend_only():
                if self._marriage_trend_class(symbol, draft.direction) != "normal":
                    bump_funnel(symbol, "marriage_counter_trend_dropped")
                    continue
            if not self._entry_allowed(draft, storyline, symbol):
                continue
            if htf_bias:
                draft.evidence["htf_bias"] = htf_bias
            if _check_hard_drop is not None:
                try:
                    dropped, rule, reason = _check_hard_drop(draft, htf_bias)
                except Exception:  # noqa: BLE001 — a bad draft must not kill the run
                    dropped, rule, reason = False, "", ""
                if dropped:
                    bump_funnel(symbol, "hard_drop")
                    logger.info("[SNR] %s %s hard-dropped (%s): %s",
                                symbol, draft.direction, rule, reason)
                    continue
            # Non-float evidence keys are ignored by the contradiction engine
            # (it only sums numbers), so the storyline/session context rides
            # along for free.
            draft.evidence["session"] = (
                session_of(float(draft.entry_time)) if draft.entry_time is not None else []
            )
            # Book §H: a killzone fill (London KZ / NY Open / Asian range) is the
            # book's highest-probability timing — recorded as evidence (not a hard
            # gate; the home-session check is the hard requirement).
            if draft.entry_time is not None:
                kz = killzones_of(float(draft.entry_time))
                if kz:
                    draft.evidence["killzone"] = kz
            if storyline is not None:
                draft.evidence["storyline_weekly"] = storyline["weekly"]
                draft.evidence["storyline_daily"] = storyline["daily"]
                # Book §A: H1 "decides whether direction is valid" and must give
                # a wick/gap candle — recorded as evidence, not a hard drop.
                side = "bullish" if draft.direction == "BUY" else "bearish"
                key = "h1_confirms_buy" if side == "bullish" else "h1_confirms_sell"
                if storyline.get(key):
                    draft.evidence["h1_wick_gap"] = True
            survivors.append(draft)
            bump_funnel(symbol, "gates_passed")
        survivors.sort(key=lambda d: d.confidence, reverse=True)
        return self._one_entry_per_fill(survivors, symbol)

    # One live position per symbol per direction at the same fill — setups that
    # fire together (e.g. engulfing + marriage on the same 15M close) are ONE
    # trade idea, not two.  Taking both doubles risk on a single idea.
    def _one_entry_per_fill(
        self, drafts: list[SignalDraft], symbol: str = "",
    ) -> list[SignalDraft]:
        """
        Keep only the best (highest-confidence) draft per (direction, entry_time).

        Two analyzers confirming on the same candle in the same direction is one
        market idea; entering both would double the position size on it.  Keeps
        the strongest, drops the duplicate (higher confidence wins, already sorted).

        ``symbol`` is threaded in so the funnel counter lands on the symbol the
        duplicate actually belongs to.  It used to be hard-coded to ``"snr"``,
        which filed every duplicate-drop across every symbol under one phantom
        bucket and made the funnel useless for multi-symbol runs.
        """
        best: dict[tuple[str, Optional[float]], SignalDraft] = {}
        out: list[SignalDraft] = []
        for d in drafts:
            # Bucket by the fill candle so near-simultaneous fills collide.
            fill = round(float(d.entry_time), 0) if d.entry_time is not None else None
            key = (d.direction, fill)
            if key in best:
                bump_funnel(symbol, "duplicate_fill_dropped")
                continue
            best[key] = d
            out.append(d)
        return out

    def _analyze_snr(self, symbol: str, for_signals: bool = False) -> StrategyReport:
        """
        Status report for the malysian (Malaysian SNR) strategy.

        Covers the whole SNR family — the core 4H-rejection + 15M-confirmation flow
        plus the engulfing and marriage analyzers — and reports the merged best
        setup.  See :meth:`_best_malaysian_draft`.
        """
        h4 = self._cache.get(symbol, 14400)
        m15 = self._cache.get(symbol, 900)
        preconditions = ["4H candle history available", "15M candle history available"]
        if not h4 or len(h4) < 8 or not m15 or len(m15) < 8:
            return StrategyReport(
                name="malysian", active=False, applicable=False,
                direction="NEUTRAL", preconditions=preconditions,
                missing_confirmations=["insufficient 4H/15M candles (<8)"],
                reason="insufficient candle history for SNR analysis",
            )

        support = self._snr_tradable_trendline(h4, "support")
        resistance = self._snr_tradable_trendline(h4, "resistance")
        confirmations: list[str] = []
        if support:
            confirmations.append(f"4H support trendline (V-shape) from {support['y1']:.5f} to {support['y2']:.5f}")
        if resistance:
            confirmations.append(f"4H resistance trendline (A-shape) from {resistance['y1']:.5f} to {resistance['y2']:.5f}")

        buy_rej = self._snr_check_rejection(h4, support, "BUY") if support else None
        sell_rej = self._snr_check_rejection(h4, resistance, "SELL") if resistance else None
        if buy_rej:
            confirmations.append(f"4H buy rejection at support (candle {buy_rej['candle_index']})")
        if sell_rej:
            confirmations.append(f"4H sell rejection at resistance (candle {sell_rej['candle_index']})")

        core_draft = self._malaysian_candidates(symbol)
        survivors = self._best_malaysian_draft(symbol, core_draft)
        draft = survivors[0] if survivors else None
        if draft is None:
            if core_draft:
                # A setup existed but a gate removed it — say which, don't claim
                # a missing confirmation.
                missing = ["setup blocked by the storyline/session gate"]
                reason = "setup found but dropped by the storyline/session gate"
            elif not support and not resistance:
                missing = ["no 4H A/V trendline formed"]
                reason = "no 4H A/V trendline formed"
            elif not buy_rej and not sell_rej:
                missing = ["no 4H rejection 3 candles after the trendline"]
                reason = "no 4H rejection at the support/resistance trendline"
            elif not any(self._snr_book_gate(symbol, rej) == "" for rej in (buy_rej, sell_rej) if rej):
                missing = ["4H rejection candle failed the book validation "
                           "(engulfing/pin bar, shadow-first touch, rule 3)"]
                reason = "4H rejection rejected by the book rules — NO VALIDATION, NO TRADE"
            else:
                missing = ["no 15M confirmation (wick rejection) of the 4H level at the live edge"]
                reason = "4H rejection but no aligned 15M breakout"
            return StrategyReport(
                name="malysian", active=True, applicable=False,
                direction="NEUTRAL", preconditions=preconditions,
                confirmations=confirmations, missing_confirmations=missing,
                invalidations=[], confidence=0.0, reason=reason,
            )

        direction_label = "BULLISH" if draft.direction == "BUY" else "BEARISH"
        analyzer = (draft.evidence or {}).get("analyzer", "snr")
        invalidations: list[str] = []
        if for_signals and self._contradiction is not None:
            market_state = self._build_market_state(symbol, draft.direction, draft.evidence)
            creport = self._contradiction.analyze(
                symbol, draft.direction, draft.evidence, market_state
            )
            if creport.recommendation == "INVALIDATE":
                invalidations = (
                    [f"contradiction: {r}" for r in creport.reasons]
                    or ["contradiction INVALIDATE"]
                )

        candidate_setup = {
            "entry": draft.entry,
            "stop_loss": draft.stop_loss,
            "take_profit": draft.take_profit,
            "risk_reward": draft.risk_reward,
        }
        if analyzer == "snr":
            if draft.direction == "BUY":
                confirmations.append("15M breakout ABOVE immediate resistance")
            else:
                confirmations.append("15M breakout BELOW immediate support")
        else:
            confirmations.append(f"{analyzer} analyzer setup")
        session = (draft.evidence or {}).get("session")
        if session:
            confirmations.append(f"entry inside {'/'.join(session)} session")
        weekly = (draft.evidence or {}).get("storyline_weekly")
        if weekly:
            confirmations.append(f"storyline weekly {weekly}")
        return StrategyReport(
            name="malysian", active=True, applicable=True,
            direction=direction_label, preconditions=preconditions,
            confirmations=confirmations, missing_confirmations=[],
            invalidations=invalidations, confidence=draft.confidence,
            candidate_setup=candidate_setup,
            reason=f"SNR {direction_label} via {analyzer} (confidence {draft.confidence:.0f}/100)",
        )

    @staticmethod
    def _setup_fingerprint(evidence: "Optional[dict]") -> str:
        """
        Structural identity of an SNR-family setup, from its own evidence.

        The lifecycle dedup key is (symbol, direction, strategy), which is right
        for "do not re-emit the same setup" — but nothing anywhere retires a
        CONFIRMED signal, so on its own it also means "this direction can never
        produce a SECOND signal for the life of the process".  Every analyzer
        already stamps its draft with the candles it was built from, so folding
        those anchors into the identity lets a genuinely NEW setup (a fresh 4H
        rejection, a fresh gap, a fresh point-#3 touch) take the slot while the
        SAME setup keeps refreshing in place.

        Anchor keys are EPOCH seconds, never cache indices — a rolling index
        shifts as the deque slides and would fake a new setup every bar.
        Evidence with no anchors (synthetic drafts, the OB/MTF paths) returns
        "" and keeps the original collapse-everything behaviour.
        """
        ev = evidence or {}
        anchors: list[float] = []
        for key, field in (
            ("snr_4h_level", "t1"), ("snr_4h_level", "t2"),
            ("engulfing_h4_gap", "open_time"),
            ("marriage_line", "t1"), ("marriage_line", "t2"),
            # Flipped SNR + QML/H&S: WITHOUT these the fingerprint is empty,
            # _retire_superseded retires nothing, and the FIRST flip/qml signal
            # holds the dedup slot for the life of the run — collapsing ~1000
            # real drafts into a handful of trades.  Anchor on the structural
            # break/retest epoch, which differs per genuine setup.
            ("flip_level", "break_open_time"),
            ("flip_level", "retest_open_time"),
            ("qml_peak", "peak_open_time"),
            ("qml_peak", "retest_open_time"),
        ):
            value = (ev.get(key) or {}).get(field)
            if value is not None:
                anchors.append(float(value))
        return "|".join(f"{a:.0f}" for a in anchors)

    def _retire_superseded(self, symbol: str, direction: str, fingerprint: str,
                           strategy_key: str = "malysian") -> int:
        """
        EXPIRE every active setup for ``symbol``/``direction``/``strategy_key``
        whose structure anchors differ from ``fingerprint``.

        Without this the first CONFIRMED signal holds the dedup slot forever and
        every later setup is silently absorbed by ``dedup_merged`` — the funnel
        showed 267 merges against 1 emit over 10k bars.  An empty
        ``fingerprint`` retires nothing (no evidence that the setups differ).
        ``strategy_key`` scopes the retire to one analyzer so retiring a
        superseded engulfing never touches a live flip/QML signal.
        """
        if not fingerprint:
            return 0
        key = self._lifecycle.setup_key(symbol, direction, strategy_key)
        retired = 0
        for sig in self._lifecycle.get_active(symbol):
            if self._lifecycle.setup_key(sig.symbol, sig.direction, sig.strategy) != key:
                continue
            if self._setup_fingerprint(sig.confidence_evidence) == fingerprint:
                continue
            self._lifecycle.transition(
                sig, SignalState.EXPIRED,
                reason="superseded by a newer SNR setup (different structure anchors)",
                evidence={"superseded_fingerprint": fingerprint},
            )
            retired += 1
        return retired

    def _evaluate_snr(self, symbol: str) -> list[Signal]:
        """
        Malaysian SNR family — emit an INDEPENDENT Signal for every analyzer whose
        setup survives the gates.

        The six analyzers (core SNR, engulfing, marriage, flip, QML, confluence)
        are independent setups.  Each produces its own signal with its own dedup
        key (``malysian:<analyzer>``) so one analyzer never suppresses or merges
        into another.  This is what lets each setup be tested and traded on its
        own (book testing §8: "do not test the whole book as one strategy").
        """
        signals: list[Signal] = []

        for draft in self._best_malaysian_draft(symbol):
            signal = self._snr_signal_for_draft(symbol, draft)
            if signal is not None:
                signals.append(signal)

        # Order-block retest pair (additive, ICT/SMC) — runs alongside the SNR
        # family, mirroring the confluence path.
        signals.extend(self._evaluate_order_block_retest(symbol))

        return signals

    def _snr_signal_for_draft(self, symbol: str, draft: SignalDraft) -> "Optional[Signal]":
        """
        Turn ONE surviving SNR draft into a Signal (polish → hold/dedup/create).

        Split out of :meth:`_evaluate_snr` so each analyzer is processed
        independently with its own dedup key.  Returns the emitted signal, or None
        when the draft is dropped by the staleness guard or the polisher.
        """
        # Staleness guard on the 4H REJECTION, not the 15M breakout.  The
        # rejection is pinned to a fixed historical candle (level_index + 3), so
        # without a recency window every evaluate() re-confirms the same old setup
        # forever.  Requiring the rejection to sit within the last 2 closed 4H
        # candles blocks re-firing of played-out setups while still allowing the
        # 15M breakout to confirm up to ~4h after the rejection.
        h4 = self._cache.get(symbol, 14400)
        rej_idx = (draft.evidence or {}).get("snr_rejection", {}).get("candle_index")
        if h4 and rej_idx is not None and rej_idx < len(h4) - 2:
            bump_funnel(symbol, "h4_rejection_stale")
            return None

        # Per-analyzer dedup key — the six setups never collide with each other.
        analyzer = str((draft.evidence or {}).get("analyzer", "snr"))
        strategy_key = f"malysian:{analyzer}"

        # Polish: run all threshold/quality/contradiction filters in one place.
        # SNR path skips min_confidence (only enforces noise_floor) — matches the
        # confluence behavior where sub-60-confidence setups emit as WEAK/RISKY.
        market_state = self._build_market_state(symbol, draft.direction, draft.evidence)
        result = self._polisher.polish(
            symbol, draft.direction, draft.confidence, draft.risk_reward,
            draft.evidence, market_state,
            apply_min_confidence=False,
            # The ICT contradiction engine checks for FVG/OB/displacement the
            # book SNR setups don't carry, so it flags them MODERATE and (with
            # realistic R:R) held everything.  The book's OWN validation is the
            # filter here — treat contradiction as advisory, not a gate.
            hold_on_contradiction=False,
        )

        if result.action == "DROP":
            bump_funnel(symbol, "polish_dropped")
            return None

        if result.action == "HOLD":
            bump_funnel(symbol, "polish_hold")
            qg_report = result.quality_gate_report
            contra_report = result.contradiction_report
            if qg_report is not None:
                held = self._hold_quality_gate_waiting(
                    symbol, draft.direction, draft.entry, draft.stop_loss,
                    draft.take_profit, draft.confidence, draft.evidence,
                    self._build_explanation(draft), strategy_key, qg_report,
                )
            elif contra_report is not None and contra_report.recommendation == "WAIT":
                held = self._hold_contradiction_waiting(
                    symbol, draft.direction, draft.entry, draft.stop_loss,
                    draft.take_profit, draft.confidence, draft.evidence,
                    self._build_explanation(draft), strategy_key, contra_report,
                )
            else:
                return None
            held.confidence_evidence["polish_trace"] = [
                {"filter_name": e.filter_name, "passed": e.passed, "reason": e.reason}
                for e in result.trace
            ]
            if contra_report is not None:
                held.confidence_evidence["contradiction"] = contra_report.to_dict()
            METRICS.increment("signals_generated")
            return held

        # PASS path — attach polish trace and continue with dedup / create.
        draft.evidence["polish_trace"] = [
            {"filter_name": e.filter_name, "passed": e.passed, "reason": e.reason}
            for e in result.trace
        ]
        if result.quality_gate_report is not None:
            draft.evidence["quality_gate"] = result.quality_gate_report.to_dict()

        # Winrate plan Phases 2+1: confluence score gate, then markdown log.
        # Quality report is available here (polisher ran it), so the ordering
        # is quality → confluence → logger → emit, exactly per the plan.
        try:
            from forex.analysis.confluence_gate import (
                confluence_required as _conf_required,
                score_signal as _score_signal,
            )
            from forex.analysis.signal_logger import log_signal_to_md as _log_signal
            _qrep = result.quality_gate_report
            _qpassed = bool(_qrep.passed) if _qrep is not None else False
            _htf = str((draft.evidence or {}).get("htf_bias", "") or self._htf_bias(symbol))
            _sess_ev = (draft.evidence or {}).get("session", "")
            _sess = "/".join(_sess_ev) if isinstance(_sess_ev, (list, tuple)) else str(_sess_ev or "")
            _need = _conf_required()
            _score, _breakdown = _score_signal(draft, _qpassed, _htf, _sess)
            draft.evidence["confluence_score"] = _score
            draft.evidence["confluence_breakdown"] = _breakdown
            draft.evidence["confluence_required"] = _need
            if _score < _need:
                bump_funnel(symbol, "confluence_dropped")
                logger.info("[SNR] %s %s confluence-dropped (score %d < %d)",
                            symbol, draft.direction, _score, _need)
                return None
            bump_funnel(symbol, "confluence_passed")

            # The card is written only once the dedup verdict below says this is a
            # genuinely new or changed setup.  Writing it here meant every M15
            # close on a standing setup produced a fresh card, so the mailer
            # shipped the same signal repeatedly (observed: US500 SELL mailed at
            # 17:48 and 18:03 UTC, identical entry/SL/TP).
            def _write_card() -> None:
                try:
                    # One card carries every take-profit rung, so the same setup
                    # is never mailed once per target.  The ladder is pure
                    # geometry off the stop (TP1 = 1R ... TP7 = 5R).
                    from forex.analysis.tp_ladder import build_tp_ladder

                    draft.evidence["tp_ladder"] = build_tp_ladder(
                        draft.direction, draft.entry, draft.stop_loss,
                    )
                    if draft.entry_time is not None:
                        # Book rule: a setup older than SNR_STALE_BARS 15M bars
                        # is stale, so that is when the entry stops being valid.
                        draft.evidence["expiry_time"] = (
                            float(draft.entry_time) + SNR_STALE_BARS * M15_TF_SECONDS
                        )
                    draft.evidence["signal_log"] = str(_log_signal(
                        symbol, draft, _qrep, _score, _breakdown, _need,
                        _sess, _htf, uuid.uuid4().hex,
                    ))
                except Exception as exc:  # noqa: BLE001: logging never blocks emits
                    logger.warning("[SNR] signal log write failed for %s: %s", symbol, exc)
        except ImportError as exc:  # noqa: BLE001 — gate modules missing
            logger.warning("[SNR] confluence gate unavailable for %s: %s", symbol, exc)
            _write_card = None

        contra_report = result.contradiction_report

        # Dedup: update the held setup instead of creating a duplicate — but only
        # when it IS the same setup.  A different structure anchor means a new
        # setup, so retire the stale slot and fall through to create.  Scoped to
        # this analyzer's key so other analyzers are untouched.
        fingerprint = self._setup_fingerprint(draft.evidence)
        existing = self._lifecycle.find_active_setup(symbol, draft.direction, strategy_key)
        retired = 0
        if existing is not None:
            retired = self._retire_superseded(symbol, draft.direction, fingerprint,
                                              strategy_key)
        if existing is not None and not retired:
            # Same setup already held.  If its structure fingerprint AND entry are
            # unchanged, this is not a new signal — just the setup still standing
            # on the chart.  Return it WITHOUT re-emitting (no metrics bump, no
            # recent-signal append, no funnel 'dedup_merged' noise); otherwise a
            # held setup re-announces every bar (1.3k+ times over a session) and
            # floods the signal feed.  A genuine re-emit happens only when the
            # structure or the fill price actually moved.
            unchanged = (
                self._setup_fingerprint(existing.confidence_evidence) == fingerprint
                and abs(float(existing.entry) - draft.entry) < 1e-9
                and abs(float(existing.take_profit) - draft.take_profit) < 1e-9
            )
            if unchanged:
                return existing
            bump_funnel(symbol, "dedup_merged")
            if _write_card:
                _write_card()
            existing.confidence_evidence.update(draft.evidence)
            existing.confidence_score = draft.confidence
            existing.explanation = self._build_explanation(draft)
            existing.entry = draft.entry
            existing.stop_loss = draft.stop_loss
            existing.take_profit = draft.take_profit
            existing.entry_zone_low = draft.entry_zone_low
            existing.entry_zone_high = draft.entry_zone_high
            existing.risk_reward_ratio = draft.risk_reward
            existing.strength = Signal.classify_strength(draft.confidence)
            METRICS.increment("signals_generated")
            self._recent_signals.append(existing.to_dict())
            if len(self._recent_signals) > 50:
                self._recent_signals = self._recent_signals[-50:]
            return existing
        if retired:
            bump_funnel(symbol, "setup_superseded")
            self._recent_signals = [
                s for s in self._recent_signals if s["signal_id"] != existing.signal_id
            ]

        explanation = self._build_explanation(draft)
        if _write_card:
            _write_card()
        signal = self._lifecycle.create(
            symbol=symbol,
            direction=draft.direction,
            entry=draft.entry,
            stop_loss=draft.stop_loss,
            take_profit=draft.take_profit,
            confidence=draft.confidence,
            evidence=draft.evidence,
            explanation=explanation,
            strategy=strategy_key,
            timeframe="multi",
            market_regime=self._get_regime_string(symbol),
        )
        signal.entry_zone_low = draft.entry_zone_low
        signal.entry_zone_high = draft.entry_zone_high
        signal.risk_reward_ratio = draft.risk_reward
        signal.strength = Signal.classify_strength(draft.confidence)
        signal.confidence_evidence["contradiction"] = contra_report.to_dict()

        # The SNR family treats the ICT contradiction engine as ADVISORY (see the
        # hold_on_contradiction=False call above): it checks for FVG/OB/
        # displacement the book setups don't carry, so a WAIT here is recorded as
        # evidence but must NOT hold the signal.  The book's own validation is the
        # filter — a setup that reached here confirmed.
        self._lifecycle.transition(
            signal, SignalState.CONFIRMED,
            reason="Malaysian SNR setup confirmed (book validation; contradiction advisory)",
            evidence=draft.evidence,
            score=draft.confidence,
        )
        METRICS.increment("signals_confirmed")
        METRICS.increment("signals_generated")
        bump_funnel(symbol, "signals_emitted")

        self._recent_signals.append(signal.to_dict())
        if len(self._recent_signals) > 50:
            self._recent_signals = self._recent_signals[-50:]

        return signal

    # ── Private: order-block retest pair (counter-trend fade + confirmed continuation) ─

    def _evaluate_ict_strategy(self, symbol: str, mode: str) -> list[Signal]:
        """Generic signal emission for any registered ict_* strategy.

        Each premium combo is a detector, not a full signal builder. This
        adapter turns an active combo record into a trade signal, preferring the
        combo's own published entry/stop/target, then its zone, and only then an
        ATR fallback. A record that reports itself untradable is dropped. Any
        failure is swallowed so a single combo can never block the signal path.
        shortcut: a combo with no zone and no levels gets a generic 2R ATR stop,
        when to upgrade: when that combo's real edge needs its own rule.
        """
        signals: list[Signal] = []
        try:
            strategy_cls = StrategyRegistry.get(mode)
            if strategy_cls is None:
                return signals
            strategy = strategy_cls()
            records = strategy._records(symbol, self)
            if not records:
                return signals
            candles = self._cache.get(symbol, strategy.tf_seconds) if hasattr(self, "_cache") else None
            if not candles:
                return signals
            price = float(candles[-1]["close"])
            avg_range = sum(
                float(c["high"]) - float(c["low"]) for c in candles[-14:]
            ) / min(14, len(candles))
            atr = avg_range if avg_range > 0 else price * 0.001

            for rec in records:
                if not ict_is_actionable(rec):
                    continue
                direction = ict_direction_of(rec)
                if direction == "NEUTRAL":
                    continue
                is_buy = direction == "BULLISH"
                payload = ict_payload_of(rec)
                levels = ict_levels_of(rec)
                zone_low, zone_high = ict_zones_of(rec)

                if "entry" in levels and "stop" in levels:
                    entry, sl = levels["entry"], levels["stop"]
                elif zone_high > zone_low:
                    entry = (zone_high + zone_low) / 2.0
                    band = zone_high - zone_low
                    sl = zone_low - band * 0.5 if is_buy else zone_high + band * 0.5
                else:
                    entry = price
                    sl = price - atr * 1.5 if is_buy else price + atr * 1.5
                risk = abs(entry - sl) or atr
                tp = levels.get("target") or (
                    entry + risk * 2.0 if is_buy else entry - risk * 2.0
                )
                rr = abs(tp - entry) / risk
                confidence = ict_confidence_of(rec)
                signal = Signal(
                    signal_id=f"{symbol}-{mode}-{int(self._epoch(candles[-1].get('open_time', 0)) or 0)}",
                    symbol=symbol,
                    direction="BUY" if is_buy else "SELL",
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    entry_zone_low=zone_low if zone_high > zone_low else entry - atr * 0.5,
                    entry_zone_high=zone_high if zone_high > zone_low else entry + atr * 0.5,
                    confidence_score=confidence,
                    confidence_evidence={"combo": mode, "payload": payload},
                    explanation=f"{mode} active ({direction})",
                    strategy=mode,
                    timeframe=str(strategy.tf_seconds),
                    risk_reward_ratio=rr,
                    strength=Signal.classify_strength(confidence),
                )
                signals.append(signal)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SIGNAL] ict strategy %s failed for %s: %s", mode, symbol, exc)
        return signals

    def _evaluate_order_block_retest(self, symbol: str) -> list[Signal]:
        """
        Emit the OB retest pair around active higher-TF order blocks.

        Scenario (bullish OB; a bearish OB is its mirror):
          price runs UP away from an active bullish OB (resting buy orders sit
          in the zone below) and then prints a bearish rejection — that is a
          counter-trend fade SELL whose TP sits just above the OB, bankable if
          price pulls back to the OB.  When price then RETESTS the OB (enters
          the zone) and confirms (displacement / zone rejection / structure
          break) it produces a continuation BUY in the OB's direction.

        Both may fire on the same candle or at different times (fade first
        while extended, continuation after the retest confirms).  Additive on
        top of the confluence signal; any failure is swallowed.
        """
        signals: list[Signal] = []
        try:
            for tf in (14400, 3600):  # H4 then H1 order-block engines
                ob_engine = self._obs.get(tf)
                if ob_engine is None:
                    continue
                candles = self._cache.get(symbol, tf)
                if not candles or len(candles) < 6:
                    continue
                last = candles[-1]
                price = float(last["close"])
                avg_range = sum(
                    float(c["high"]) - float(c["low"]) for c in candles[-14:]
                ) / min(14, len(candles))

                for ob in ob_engine.get_active(limit=5):
                    if ob.state not in (OBState.VALID, OBState.PARTIALLY_MITIGATED):
                        continue
                    if ob.high <= 0 or ob.low <= 0 or ob.high <= ob.low:
                        continue
                    zone_height = ob.high - ob.low

                    if ob.direction == OBDirection.BULLISH:
                        # (a) price extended ABOVE the bullish OB + rejection.
                        if price > ob.high + zone_height * 0.25 and \
                                self._is_rejection_candle(candles, "BEARISH", soft=True):
                            sig = self._ob_fade_signal(
                                symbol, ob, tf, price, candles, avg_range, zone_height
                            )
                            if sig:
                                signals.append(sig)
                        # (b) price retests the bullish OB + confirmation.
                        if self._recent_zone_touch(candles, ob.low, ob.high) and \
                                self._has_bullish_confirmation(symbol, ob, candles, avg_range):
                            sig = self._ob_continuation_signal(
                                symbol, ob, tf, price, candles, avg_range, zone_height
                            )
                            if sig:
                                signals.append(sig)
                    elif ob.direction == OBDirection.BEARISH:
                        # (a) price extended BELOW the bearish OB + rejection.
                        if price < ob.low - zone_height * 0.25 and \
                                self._is_rejection_candle(candles, "BULLISH", soft=True):
                            sig = self._ob_fade_signal(
                                symbol, ob, tf, price, candles, avg_range, zone_height
                            )
                            if sig:
                                signals.append(sig)
                        # (b) price retests the bearish OB + confirmation.
                        if self._recent_zone_touch(candles, ob.low, ob.high) and \
                                self._has_bearish_confirmation(symbol, ob, candles, avg_range):
                            sig = self._ob_continuation_signal(
                                symbol, ob, tf, price, candles, avg_range, zone_height
                            )
                            if sig:
                                signals.append(sig)
        except Exception as exc:  # noqa: BLE001 — best-effort, never block confluence
            logger.debug("[SIGNAL] OB retest pair failed for %s: %s", symbol, exc)
        return signals

    @staticmethod
    def _is_rejection_candle(candles: list[dict], against: str, soft: bool = False) -> bool:
        """Is the last candle a rejection against the current extension?

        ``against="BEARISH"`` means price extended UP and the last candle is a
        solid bearish rejection (close under open, body >= half the range);
        ``against="BULLISH"`` is the mirror for an extension to the downside.
        With ``soft=True`` a weaker rejection is accepted instead: body >= a
        quarter of the range AND the close pushed into the bottom third
        (bearish) / top third (bullish) of the candle — so genuine pullbacks
        still qualify without requiring a full-bodied rejection.
        """
        if not candles:
            return False
        c = candles[-1]
        rng = float(c["high"]) - float(c["low"])
        if rng <= 0:
            return False
        body = abs(float(c["close"]) - float(c["open"]))
        against_bearish = against == "BEARISH"
        # Strong rejection: body >= half the range and closed against the open.
        if body / rng >= 0.5:
            return (float(c["close"]) < float(c["open"])) if against_bearish \
                else (float(c["close"]) > float(c["open"]))
        if not soft:
            return False
        # Soft rejection: body >= a quarter of the range and the close parked
        # in the bottom third (bearish) / top third (bullish) of the candle.
        if body / rng < 0.25:
            return False
        if against_bearish:
            return float(c["close"]) <= float(c["low"]) + rng / 3.0
        return float(c["close"]) >= float(c["high"]) - rng / 3.0

    @staticmethod
    def _recent_zone_touch(
        candles: list[dict], zone_low: float, zone_high: float, window: int = 2
    ) -> bool:
        """Did price overlap ``[zone_low, zone_high]`` within ``window`` candles?"""
        for c in candles[-window:]:
            if float(c["low"]) <= zone_high and float(c["high"]) >= zone_low:
                return True
        return False

    def _has_bullish_confirmation(
        self, symbol: str, ob, candles: list[dict], avg_range: float
    ) -> bool:
        """Bullish confirmation after a bullish-OB retest: displacement candle,
        rejection of the OB low (wick below, close back above the top), or an
        MSS_BULLISH structure break on the lower timeframes."""
        if not candles:
            return False
        last = candles[-1]
        rng = float(last["high"]) - float(last["low"])
        if float(last["close"]) > float(last["open"]) and rng >= avg_range * 1.2:
            return True
        if any(
            float(c["low"]) <= ob.low and float(c["close"]) > ob.high
            for c in candles[-2:]
        ):
            return True
        for tf in (900, 300):
            engine = self._structure.get(tf)
            if engine is None:
                continue
            for evt in engine.get_events(tf, n=5):
                if "MSS_BULLISH" in evt.get("event", ""):
                    return True
        return False

    def _has_bearish_confirmation(
        self, symbol: str, ob, candles: list[dict], avg_range: float
    ) -> bool:
        """Bearish confirmation after a bearish-OB retest: displacement candle,
        rejection of the OB high (wick above, close back below the bottom), or
        an MSS_BEARISH structure break on the lower timeframes."""
        if not candles:
            return False
        last = candles[-1]
        rng = float(last["high"]) - float(last["low"])
        if float(last["close"]) < float(last["open"]) and rng >= avg_range * 1.2:
            return True
        if any(
            float(c["high"]) >= ob.high and float(c["close"]) < ob.low
            for c in candles[-2:]
        ):
            return True
        for tf in (900, 300):
            engine = self._structure.get(tf)
            if engine is None:
                continue
            for evt in engine.get_events(tf, n=5):
                if "MSS_BEARISH" in evt.get("event", ""):
                    return True
        return False

    def _ob_fade_signal(
        self, symbol: str, ob, tf: int, price: float,
        candles: list[dict], avg_range: float, zone_height: float,
    ) -> Optional[Signal]:
        """Counter-trend fade back to an active OB (emitted while extended).

        A bullish OB that price is extended above prices RESTING BUY ORDERS
        below; the fade SELL captures the pull-back down to the OB and exits
        just above the zone (profitable even if the OB only partly retests).
        The bearish-OB mirror fades a bullish extension back UP to the zone.
        """
        is_bull = ob.direction == OBDirection.BULLISH
        direction = "SELL" if is_bull else "BUY"
        last = candles[-1]
        rng = float(last["high"]) - float(last["low"])
        body_fraction = abs(float(last["close"]) - float(last["open"])) / rng if rng > 0 else 0.0

        if is_bull:
            swing = max(float(c["high"]) for c in candles[-30:])
            sl = swing + zone_height * 0.1
            tp = ob.high + zone_height * 0.08  # just above the OB — fade paid at the zone top
            zone_label = f"bullish order block {ob.high:.5f}-{ob.low:.5f}"
            side = "above"
            rejection = "bearish rejection candle"
            swing_name = "high"
        else:
            swing = min(float(c["low"]) for c in candles[-30:])
            sl = swing - zone_height * 0.1
            tp = ob.low - zone_height * 0.08
            zone_label = f"bearish order block {ob.high:.5f}-{ob.low:.5f}"
            side = "below"
            rejection = "bullish rejection candle"
            swing_name = "low"

        risk = abs(price - sl)
        reward = abs(price - tp)
        if risk <= 0 or reward <= 0:
            return None
        risk_reward = reward / risk

        # Counter-trend conviction is intentionally low, capped under the RISKY
        # threshold (<35) so a fade is ALWAYS labelled RISKY — a counter-trend
        # trade against resting orders, never higher conviction than that.
        distance = (price - ob.high) if is_bull else (ob.low - price)
        ext = max(zone_height, avg_range) * 0.5
        quality_bonus = min(6.0, ob.quality_score * 0.06)
        rejection_bonus = min(6.0, body_fraction * 6.0)
        extension_bonus = min(6.0, distance / (2 * ext) * 6.0) if ext > 0 else 0.0
        fade_confidence = 22.0 + quality_bonus + rejection_bonus + extension_bonus
        confidence = round(min(34.9, fade_confidence), 1)

        explanation = (
            f"{direction} — RISKY counter-trend fade (confidence {confidence:.0f}/100)\n\n"
            f"Why?\n"
            f"- price extended {side} {zone_label} ({tf // 3600}H); "
            f"price {price:.5f} vs OB top {ob.high:.5f}\n"
            f"- {rejection} (body {body_fraction:.0%}) = price pulling back "
            f"toward the resting orders in the OB\n"
            f"- RISKY fade: entry {price:.5f}, TP {tp:.5f} just above the OB "
            f"(banking the retest), SL {sl:.5f} beyond the swing {swing_name}\n"
            f"- invalid if price breaks SL at {sl:.5f}\n"
            f"- R:R = {risk_reward:.2f}"
        )
        return self._emit_ob_signal(
            symbol, direction, price, sl, tp, ob.low, ob.high,
            confidence, explanation,
            {"ob_retest_pair": "fade", "ob_tf": tf, "ob_high": ob.high, "ob_low": ob.low},
            risk_reward,
        )

    def _ob_continuation_signal(
        self, symbol: str, ob, tf: int, price: float,
        candles: list[dict], avg_range: float, zone_height: float,
    ) -> Optional[Signal]:
        """Continuation signal after a confirmed OB retest.

        Price retested the OB zone and confirmed (displacement / zone rejection
        / structure break) — the trade now runs IN the OB's direction.  Entry
        is the OB zone, SL beyond the opposite side of the zone, TP a measured
        move at the recent swing on the far side.
        """
        is_bull = ob.direction == OBDirection.BULLISH
        direction = "BUY" if is_bull else "SELL"

        if is_bull:
            entry_zone_low, entry_zone_high = ob.low, ob.high
            entry = price if ob.low <= price <= ob.high else (ob.high + ob.low) / 2
            sl = ob.low - zone_height * 0.1
            swing = max(float(c["high"]) for c in candles[-60:])
            tp = max(swing, ob.high + 1.5 * zone_height)
            zone_label = f"bullish order block {ob.high:.5f}-{ob.low:.5f}"
        else:
            entry_zone_low, entry_zone_high = ob.low, ob.high
            entry = price if ob.low <= price <= ob.high else (ob.high + ob.low) / 2
            sl = ob.high + zone_height * 0.1
            swing = min(float(c["low"]) for c in candles[-60:])
            tp = min(swing, ob.low - 1.5 * zone_height)
            zone_label = f"bearish order block {ob.high:.5f}-{ob.low:.5f}"

        risk = abs(entry - sl)
        reward = abs(entry - tp)
        if risk <= 0 or reward <= 0:
            return None
        risk_reward = reward / risk

        # Confirmed continuation gets a high baseline; OB quality and the
        # specific confirmation edge it toward RECOMMENDED / STRONG.
        quality_bonus = min(10.0, ob.quality_score * 10.0)
        confirmation_bonus = 0.0
        confirm_kinds: list[str] = []
        last = candles[-1]
        rng = float(last["high"]) - float(last["low"])
        if float(last["close"]) > float(last["open"]) and rng >= avg_range * 1.2:
            confirmation_bonus += 6.0
            confirm_kinds.append("bullish displacement")
        if float(last["close"]) < float(last["open"]) and rng >= avg_range * 1.2:
            confirmation_bonus += 6.0
            confirm_kinds.append("bearish displacement")
        if is_bull and any(
            float(c["low"]) <= ob.low and float(c["close"]) > ob.high for c in candles[-2:]
        ):
            confirmation_bonus += 4.0
            confirm_kinds.append("rejection of OB low")
        if not is_bull and any(
            float(c["high"]) >= ob.high and float(c["close"]) < ob.low for c in candles[-2:]
        ):
            confirmation_bonus += 4.0
            confirm_kinds.append("rejection of OB high")
        if not confirm_kinds:  # a structure break confirmed it but not candle-wise
            confirmation_bonus += 4.0
            confirm_kinds.append("structure break")
        confidence = round(min(100.0, 68.0 + quality_bonus + confirmation_bonus), 1)
        tier = Signal.classify_strength(confidence)

        explanation = (
            f"{direction} — {tier} OB-retest continuation (confidence {confidence:.0f}/100)\n\n"
            f"Why?\n"
            f"- price retested {zone_label} ({tf // 3600}H); "
            f"entry {entry:.5f} in the OB zone ({entry_zone_low:.5f}-{entry_zone_high:.5f})\n"
            f"- confirmation: {'; '.join(confirm_kinds)}\n"
            f"- SL {sl:.5f} beyond the zone; TP {tp:.5f} (measured move / swing "
            f"{'high' if is_bull else 'low'})\n"
            f"- R:R = {risk_reward:.2f}"
        )
        return self._emit_ob_signal(
            symbol, direction, entry, sl, tp, entry_zone_low, entry_zone_high,
            confidence, explanation,
            {"ob_retest_pair": "continuation", "ob_tf": tf, "ob_high": ob.high, "ob_low": ob.low},
            risk_reward,
        )

    def _emit_ob_signal(
        self, symbol: str, direction: str, entry: float, sl: float, tp: float,
        zone_low: float, zone_high: float, confidence: float,
        explanation: str, evidence: dict, risk_reward: float,
    ) -> Optional[Signal]:
        """Create/refresh a malysian-strategy signal for the OB retest pair.

        Shares the lifecycle dedup identity with the confluence signals
        (symbol+direction+malysian): an existing active setup is refreshed in
        place, otherwise a fresh CONFIRMED signal is created.
        """
        existing = self._lifecycle.find_active_setup(symbol, direction, "malysian")
        if existing is not None:
            existing.confidence_evidence.update(evidence)
            existing.confidence_score = confidence
            existing.explanation = explanation
            existing.entry = entry
            existing.stop_loss = sl
            existing.take_profit = tp
            existing.entry_zone_low = zone_low
            existing.entry_zone_high = zone_high
            existing.risk_reward_ratio = risk_reward
            existing.strength = Signal.classify_strength(confidence)
            METRICS.increment("signals_generated")
            self._recent_signals.append(existing.to_dict())
            if len(self._recent_signals) > 50:
                self._recent_signals = self._recent_signals[-50:]
            return existing

        signal = self._lifecycle.create(
            symbol=symbol, direction=direction, entry=entry, stop_loss=sl,
            take_profit=tp, confidence=confidence, evidence=evidence,
            explanation=explanation, strategy="malysian", timeframe="multi",
            market_regime=self._get_regime_string(symbol),
        )
        signal.entry_zone_low = zone_low
        signal.entry_zone_high = zone_high
        signal.risk_reward_ratio = risk_reward
        signal.strength = Signal.classify_strength(confidence)
        signal.confidence_evidence["ob_retest_pair"] = evidence.get("ob_retest_pair")
        self._lifecycle.transition(
            signal, SignalState.CONFIRMED,
            reason=f"Order-block retest {evidence.get('ob_retest_pair', '')}",
            evidence=evidence, score=confidence,
        )
        METRICS.increment("signals_confirmed")
        METRICS.increment("signals_generated")
        self._recent_signals.append(signal.to_dict())
        if len(self._recent_signals) > 50:
            self._recent_signals = self._recent_signals[-50:]
        return signal

    def get_active_signals(self, symbol: str | None = None) -> list[Signal]:
        return self._lifecycle.get_active(symbol)

    def get_recent_signals(self, n: int = 10) -> list[dict]:
        return self._recent_signals[-n:]

    def get_lifecycle(self) -> SignalLifecycleManager:
        return self._lifecycle

    def get_strategy_reports(self, symbol: str) -> list[StrategyReport]:
        """
        Run every registered strategy against the shared market context and
        return their full status reports — WITHOUT creating signals.

        Feeds the UI (improve.md §45) and the strategy-agreement logic (§21):
        every strategy reports active/applicable/direction/confidence/etc. so
        disagreement between strategies is surfaced rather than hidden. A
        strategy that raises is reported as inactive instead of breaking the
        whole report batch.
        """
        context = self._context_engine.get(symbol) if self._context_engine else None
        reports: list[StrategyReport] = []
        for strategy_cls in StrategyRegistry.all():
            try:
                reports.append(strategy_cls().evaluate(symbol, context, self, for_signals=True))
            except Exception as exc:  # noqa: BLE001 — one bad strategy ≠ total failure
                logger.warning(
                    "[STRATEGY] %s '%s' report failed: %s",
                    symbol, strategy_cls.name, exc,
                )
                reports.append(StrategyReport(
                    name=strategy_cls.name, active=False, applicable=False,
                    direction="NEUTRAL", reason=f"error: {exc}",
                ))
        return reports

    # ── Private: run engines ────────────────────────────────────────────

    def _run_structure(self, symbol: str) -> list[dict]:
        all_events: list[dict] = []
        for tf, engine in self._structure.items():
            candles = self._cache.get(symbol, tf)
            if candles:
                events = engine.analyze(candles, tf)
                all_events.extend(events)
        return all_events

    def _run_liquidity(self, symbol: str, structure_events: list[dict]) -> None:
        for tf, engine in self._liquidity.items():
            candles = self._cache.get(symbol, tf)
            if candles:
                engine.analyze(candles, structure_events)

    def _run_ob(self, symbol: str, structure_events: list[dict]) -> None:
        for tf, engine in self._obs.items():
            candles = self._cache.get(symbol, tf)
            if candles:
                poc_price = self._poc[tf].get_poc(tf) if tf in self._poc else None
                engine.analyze(candles, structure_events, poc_price)

    def _run_poc(self, symbol: str) -> None:
        for tf, engine in self._poc.items():
            candles = self._cache.get(symbol, tf)
            if candles:
                engine.compute_from_candles(candles, tf)

    def _run_fvg(self, symbol: str) -> None:
        for tf, engine in self._fvgs.items():
            candles = self._cache.get(symbol, tf)
            if candles:
                engine.analyze(candles, tf)

    # ── Private: retest evidence (improve.md §34–§35) ──────────────────

    def _apply_retest_evidence(self, symbol: str, draft: Optional[SignalDraft]) -> None:
        """
        Fold retest-reaction evidence into a draft (additive, non-fatal).

        For the draft's trade direction, evaluate the reaction of recent candles
        to the active order-block and FVG zones. A REJECTED retest in the trade
        direction is strong confirmation that the level held, so it earns a small
        fixed confidence bump (+5 on the /100 scale). Best-effort: any failure
        is swallowed so signal scoring never breaks.
        """
        if draft is None or self._retest is None:
            return
        try:
            direction = "BULLISH" if draft.direction == "BUY" else "BEARISH"
            ob_dir = OBDirection.BULLISH if direction == "BULLISH" else OBDirection.BEARISH
            bump = 0.0

            # ── Active order blocks on their own timeframe candles ───────
            for tf, ob_engine in self._obs.items():
                candles = self._cache.get(symbol, tf)
                if not candles:
                    continue
                for ob in ob_engine.get_active(limit=3):
                    if ob.direction != ob_dir:
                        continue
                    zone = {"zone_top": ob.high, "zone_bottom": ob.low}
                    ev = self._retest.evaluate(
                        symbol, "order_block", zone, direction, candles
                    )
                    if ev is None:
                        continue
                    draft.evidence.setdefault("ob_retest", []).append(ev.to_dict())
                    if ev.reaction == "REJECTED":
                        bump += 5.0
                        draft.explanation_parts.append(
                            f"OB retest REJECTED ({ob.high:.5f}-{ob.low:.5f}, "
                            f"s={ev.reaction_strength:.2f})"
                        )

            # ── Active FVGs on their own timeframe candles ───────────────
            for tf, fvg_engine in self._fvgs.items():
                candles = self._cache.get(symbol, tf)
                if not candles:
                    continue
                for fvg in fvg_engine.get_active():
                    if fvg.direction != direction or fvg.fully_filled or fvg.invalidated:
                        continue
                    zone = {
                        "zone_top": fvg.upper_boundary,
                        "zone_bottom": fvg.lower_boundary,
                    }
                    ev = self._retest.evaluate(
                        symbol, "fvg", zone, direction, candles
                    )
                    if ev is None:
                        continue
                    draft.evidence.setdefault("fvg_retest", []).append(ev.to_dict())
                    if ev.reaction == "REJECTED":
                        bump += 5.0
                        draft.explanation_parts.append(
                            f"FVG retest REJECTED (s={ev.reaction_strength:.2f})"
                        )

            if bump > 0:
                draft.confidence = round(min(100.0, draft.confidence + bump), 1)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("[SIGNAL] retest evidence failed for %s: %s", symbol, exc)

    # ── Private: build draft ────────────────────────────────────────────

    def _build_draft(self, symbol: str, direction: str) -> Optional[SignalDraft]:
        """Build a signal draft by evaluating all timeframe evidence."""
        draft = SignalDraft(direction=direction, entry=0, stop_loss=0, take_profit=0)
        w = self._weights

        # ── 1. HTF bias (1D / 4H) ──────────────────────────────────────
        for tf in [86400, 14400]:
            if tf in self._structure:
                bias = self._structure[tf].get_bias(tf)
                if direction == "BUY" and bias == Bias.BULLISH:
                    draft.confidence += w.htf_bias
                    draft.evidence["htf_bias"] = w.htf_bias
                    draft.explanation_parts.append(f"{tf//3600}H bullish structure")
                    break
                elif direction == "SELL" and bias == Bias.BEARISH:
                    draft.confidence += w.htf_bias
                    draft.evidence["htf_bias"] = w.htf_bias
                    draft.explanation_parts.append(f"{tf//3600}H bearish structure")
                    break

        # ── 2. OB quality ───────────────────────────────────────────────
        for tf in [14400, 3600]:
            if tf in self._obs:
                active = self._obs[tf].get_active(limit=3)
                for ob in active:
                    if direction == "BUY" and ob.direction == OBDirection.BULLISH:
                        score = min(25, ob.quality_score * 0.25)
                        draft.confidence += score
                        draft.evidence["ob_quality"] = score
                        draft.explanation_parts.append(
                            f"{tf//3600}H bullish OB (score={ob.quality_score:.0f})"
                        )
                        # Entry from OB zone
                        draft.entry = (ob.high + ob.low) / 2
                        draft.entry_zone_low = ob.low
                        draft.entry_zone_high = ob.high
                        draft.stop_loss = ob.low - (ob.high - ob.low) * 0.1  # 10% below OB range
                        break
                    elif direction == "SELL" and ob.direction == OBDirection.BEARISH:
                        score = min(25, ob.quality_score * 0.25)
                        draft.confidence += score
                        draft.evidence["ob_quality"] = score
                        draft.explanation_parts.append(
                            f"{tf//3600}H bearish OB (score={ob.quality_score:.0f})"
                        )
                        draft.entry = (ob.high + ob.low) / 2
                        draft.entry_zone_low = ob.low
                        draft.entry_zone_high = ob.high
                        draft.stop_loss = ob.high + (ob.high - ob.low) * 0.1
                        break

        # ── 2b. FVG imbalance ────────────────────────────────────────────
        for tf in [3600, 900, 300]:
            if tf in self._fvgs:
                for fvg in self._fvgs[tf].get_active():
                    if fvg.fully_filled or fvg.invalidated:
                        continue
                    if direction == "BUY" and fvg.direction == "BULLISH":
                        draft.confidence += w.fvg_imbalance
                        draft.evidence["fvg_imbalance"] = w.fvg_imbalance
                        draft.explanation_parts.append(
                            f"{tf//60}M bullish FVG (fill={fvg.filled_percentage:.0%})"
                        )
                        # Prefer the FVG zone as entry if no OB entry set yet.
                        if draft.entry == 0:
                            draft.entry = (fvg.upper_boundary + fvg.lower_boundary) / 2
                            draft.entry_zone_low = fvg.lower_boundary
                            draft.entry_zone_high = fvg.upper_boundary
                            draft.stop_loss = fvg.lower_boundary - (fvg.size * 0.1)
                        break
                    elif direction == "SELL" and fvg.direction == "BEARISH":
                        draft.confidence += w.fvg_imbalance
                        draft.evidence["fvg_imbalance"] = w.fvg_imbalance
                        draft.explanation_parts.append(
                            f"{tf//60}M bearish FVG (fill={fvg.filled_percentage:.0%})"
                        )
                        if draft.entry == 0:
                            draft.entry = (fvg.upper_boundary + fvg.lower_boundary) / 2
                            draft.entry_zone_low = fvg.lower_boundary
                            draft.entry_zone_high = fvg.upper_boundary
                            draft.stop_loss = fvg.upper_boundary + (fvg.size * 0.1)
                        break

        # ── 3. Liquidity sweep ──────────────────────────────────────────
        for tf in [3600, 900]:
            if tf in self._liquidity:
                pools = self._liquidity[tf].get_pools(state="SWEPT")
                if pools:
                    draft.confidence += w.liquidity_sweep
                    draft.evidence["liquidity_sweep"] = w.liquidity_sweep
                    draft.explanation_parts.append("sell-side liquidity swept")
                    break

        # ── 4. MSS / CHoCH ─────────────────────────────────────────────
        for tf in [900, 300]:
            if tf in self._structure:
                events = self._structure[tf].get_events(tf, n=3)
                for evt in events:
                    evt_name = evt.get("event", "")
                    if direction == "BUY" and "MSS_BULLISH" in evt_name:
                        draft.confidence += w.mss
                        draft.evidence["mss"] = w.mss
                        draft.explanation_parts.append(f"{tf//60}M bullish MSS")
                        break
                    elif direction == "SELL" and "MSS_BEARISH" in evt_name:
                        draft.confidence += w.mss
                        draft.evidence["mss"] = w.mss
                        draft.explanation_parts.append(f"{tf//60}M bearish MSS")
                        break

        # ── 5. Displacement ─────────────────────────────────────────────
        for tf in [900, 300]:
            if tf in self._structure:
                events = self._structure[tf].get_events(tf, n=3)
                for evt in events:
                    if "BOS" in evt.get("event", ""):
                        draft.confidence += w.displacement
                        draft.evidence["displacement"] = w.displacement
                        draft.explanation_parts.append("displacement detected")
                        break

        # ── 5b. Volatility normalization (m.md §32) ─────────────────────
        # Normalize stop distance + displacement by ATR instead of fixed pips.
        self._apply_volatility_normalization(symbol, draft)

        # ── 5c. Session context (m.md §34) ─────────────────────────────
        # Use UTC session state to weight liquidity-sweep reversals and to
        # flag price sitting at a session extreme. Best-effort.
        self._apply_session_context(symbol, draft)

        # ── 6. POC interaction ──────────────────────────────────────────
        for tf in [3600, 900]:
            if tf in self._poc:
                poc_result = self._poc[tf].get_result(tf)
                if poc_result and draft.entry > 0:
                    dist = abs(draft.entry - poc_result.poc_price)
                    # ponytail: 0.1% proximity — works for forex (1.10), indices (20000), gold (2300)
                    if draft.entry > 0 and dist / draft.entry < 0.001:
                        draft.confidence += w.poc_interaction
                        draft.evidence["poc_interaction"] = w.poc_interaction
                        draft.explanation_parts.append(
                            f"POC reclaimed at {poc_result.poc_price:.5f}"
                        )
                        break

        # ── 7. Microstructure confirmation ──────────────────────────────
        if self._micro:
            metrics = self._micro.get_metrics(symbol)
            if metrics:
                if direction == "BUY" and metrics.momentum > 0.3:
                    draft.confidence += w.entry_confirmation
                    draft.evidence["entry_confirmation"] = w.entry_confirmation
                    draft.explanation_parts.append("1M bullish trigger")
                elif direction == "SELL" and metrics.momentum < -0.3:
                    draft.confidence += w.entry_confirmation
                    draft.evidence["entry_confirmation"] = w.entry_confirmation
                    draft.explanation_parts.append("1M bearish trigger")

        # ── 7b. Order flow confirmation (ICT/SMC — real order flow) ──────
        # Mirrors the microstructure trigger but reads genuine candle order
        # flow (delta / regime). Additive + non-fatal: any failure is swallowed
        # so signal scoring never breaks. Picks the finest timeframe the engine
        # has state for (entry-relevant).
        if self._order_flow:
            try:
                tfs = self._order_flow.get_timeframes(symbol)
                if tfs:
                    of_tf = min(tfs)
                    ofs = self._order_flow.get_state(symbol, of_tf)
                    regime = ofs.get("regime")
                    odir = ofs.get("direction")
                    draft_dir = "BULLISH" if direction == "BUY" else "BEARISH"
                    if regime == "IMBALANCE" and odir == draft_dir:
                        draft.confidence += 8.0
                        draft.evidence["order_flow_imbalance"] = 8.0
                        draft.explanation_parts.append(
                            f"{of_tf}s order flow imbalance ({odir})"
                        )
                    elif regime == "ABSORPTION" and odir not in (draft_dir, "NEUTRAL"):
                        draft.confidence -= 6.0
                        draft.evidence["order_flow_absorption"] = -6.0
                        draft.explanation_parts.append(
                            f"{of_tf}s order flow absorption against {direction}"
                        )
                    elif regime == "EXHAUSTION" and odir not in (draft_dir, "NEUTRAL"):
                        draft.confidence -= 6.0
                        draft.evidence["order_flow_exhaustion"] = -6.0
                        draft.explanation_parts.append(
                            f"{of_tf}s order flow exhaustion against {direction}"
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("[SIGNAL] order flow evidence failed for %s: %s", symbol, exc)

        # ── 8. Spread scoring (m.md §33) ───────────────────────────────
        # Dedicated spread engine: per-symbol, history-relative scoring.
        # Replaces the old flat "+2 for NORMAL regime" with a nuanced
        # bonus (tight spread) and penalty (wide / extreme spread).
        if self._spread:
            bonus = self._spread.get_spread_bonus(symbol)
            penalty = self._spread.get_spread_penalty(symbol)
            net = bonus + penalty
            if net != 0:
                draft.confidence += net
                draft.evidence["spread"] = net
                if net > 0:
                    draft.explanation_parts.append("spread tight (favorable)")
                else:
                    draft.explanation_parts.append("spread abnormal (downgraded)")

        # ── 9. Data quality ─────────────────────────────────────────────
        candles_1m = self._cache.get(symbol, 60)
        if candles_1m and len(candles_1m) >= 20:
            draft.confidence += w.data_quality
            draft.evidence["data_quality"] = w.data_quality
            draft.explanation_parts.append("market data healthy")

        # ── 9b. Volume profile (improve.md) ────────────────────────────
        if self._volume_profile:
            try:
                from forex.analysis.volume_profile import VolumeProfileEngine
                if isinstance(self._volume_profile, VolumeProfileEngine):
                    vp = self._volume_profile.get_profile(symbol)
                    if vp and vp.total_activity >= self._volume_profile._config.min_ticks:
                        pos = vp.price_position(draft.entry)
                        if (direction == "BUY" and pos in ("below_val", "at_poc")) or \
                           (direction == "SELL" and pos in ("above_vah", "at_poc")):
                            draft.confidence += w.volume_profile_interaction
                            draft.evidence["volume_profile"] = w.volume_profile_interaction
                            draft.explanation_parts.append(
                                f"price {pos} (POC={vp.poc:.5f})"
                            )
            except Exception:
                pass  # non-fatal

        # ── 9c. Volume profile confluence / magnet / breakout (improve.md §39) ──
        if self._volume_profile and draft.entry > 0:
            try:
                from forex.analysis.volume_profile import (
                    VolumeProfileEngine,
                    detect_breakout,
                    detect_reentry,
                    detect_rotation,
                )
                if isinstance(self._volume_profile, VolumeProfileEngine):
                    # Multi-timeframe confluence zones (2+ timeframes agree)
                    zones = self._volume_profile.get_confluence_zones(symbol, draft.entry)
                    best_zone = max(zones, key=lambda z: z["score"]) if zones else None
                    if best_zone and best_zone["score"] >= 2:
                        draft.confidence += w.volume_profile_interaction
                        draft.evidence["profile_confluence"] = best_zone["score"]
                        draft.explanation_parts.append(
                            f"VP confluence x{best_zone['score']:.0f} @ {best_zone['price_center']:.5f}"
                        )

                    # POC magnet attraction
                    magnet = self._volume_profile.get_magnet_score(symbol, draft.entry)
                    if magnet.get("score", 0.0) > 0.7:
                        draft.confidence += w.volume_profile_interaction / 2
                        draft.evidence["profile_magnet"] = magnet["score"]
                        draft.explanation_parts.append(
                            f"POC magnet {magnet['score']:.2f}"
                        )

                    # Breakout / reentry / rotation in the trade direction
                    bo = self._volume_profile.detect_breakout(symbol, draft.entry)
                    if bo and bo.get("confirmed"):
                        favorable = (direction == "BUY" and bo["type"] == "breakout_up") or \
                                    (direction == "SELL" and bo["type"] == "breakout_down")
                        if favorable:
                            draft.confidence += w.volume_profile_interaction
                            draft.evidence["profile_breakout"] = bo["strength"]
                            draft.explanation_parts.append(
                                f"VP breakout {bo['type']} (s={bo['strength']:.2f})"
                            )

                    re = self._volume_profile.detect_reentry(symbol, draft.entry)
                    if re:
                        favorable = (direction == "BUY" and re["type"] == "reentry_up") or \
                                    (direction == "SELL" and re["type"] == "reentry_down")
                        if favorable:
                            draft.confidence += w.volume_profile_interaction
                            draft.evidence["profile_reentry"] = re["strength"]
                            draft.explanation_parts.append(
                                f"VP reentry {re['type']} (s={re['strength']:.2f})"
                            )

                    rot = self._volume_profile.detect_rotation(symbol)
                    if rot:
                        draft.confidence += w.volume_profile_interaction / 2
                        draft.evidence["profile_rotation"] = rot["strength"]
                        draft.explanation_parts.append(
                            f"VP rotation (s={rot['strength']:.2f})"
                        )
            except Exception:
                pass  # non-fatal

        # ── 10. Calculate R:R ───────────────────────────────────────────
        if draft.entry > 0 and draft.stop_loss > 0:
            risk = abs(draft.entry - draft.stop_loss)
            if risk > 0:
                # Conservative: target = 2x risk
                if direction == "BUY":
                    draft.take_profit = draft.entry + risk * 2
                else:
                    draft.take_profit = draft.entry - risk * 2
                draft.risk_reward = abs(draft.take_profit - draft.entry) / risk

        # If no entry was determined, return None
        if draft.entry == 0:
            return None

        # Normalize confluence confidence to a /100 scale. The raw score is a
        # sum of ScoringWeights (max ≈ sum of all positive weights); the UI
        # displays it as /100, so scale it here. The theoretical max is derived
        # from the actual ScoringWeights instance so it stays correct if the
        # weights change. Strength thresholds (75/50) now operate on /100.
        max_score = sum(
            v for v in self._weights.__dict__.values()
            if isinstance(v, (int, float)) and v > 0
        )
        if max_score > 0:
            draft.confidence = round(min(100.0, draft.confidence / max_score * 100.0), 1)

        return draft

    # ── Private: explanation builder ────────────────────────────────────

    def _build_market_state(self, symbol: str, direction: str, evidence: dict) -> dict:
        """
        Gather market context for the contradiction engine (m.md §37).

        Best-effort: any engine/method that is missing or raises is skipped
        and the corresponding field falls back to a neutral default. The
        contradiction engine's vocabulary is normalized here (e.g. the
        microstructure spread_regime "EXPANDED" maps to "WIDE").
        """
        if self._context_engine is not None:
            return self._build_market_state_from_context(symbol, direction, evidence)
        state: dict = {
            "htf_bias": "",
            "ltf_bias": "",
            "spread_regime": "NORMAL",
            "has_displacement": "displacement" in evidence,
            "has_fvg": False,
            "fvg_status": "",
            "has_ob": False,
            "ob_status": "",
            "session": "",  # no session engine yet — left neutral
            "volatility_regime": "NORMAL",
            "has_liquidity_sweep": "liquidity_sweep" in evidence,
        }

        # HTF / LTF bias from the structure engine
        htf_tf = 14400 if 14400 in self._structure else (86400 if 86400 in self._structure else None)
        ltf_tf = 900 if 900 in self._structure else (
            300 if 300 in self._structure else (60 if 60 in self._structure else None)
        )
        if htf_tf is not None:
            try:
                b = self._structure[htf_tf].get_bias(htf_tf)
                state["htf_bias"] = b.value.lower() if b else ""
            except Exception:
                pass
        if ltf_tf is not None:
            try:
                b = self._structure[ltf_tf].get_bias(ltf_tf)
                state["ltf_bias"] = b.value.lower() if b else ""
            except Exception:
                pass

        # Spread regime (map micro vocabulary → contradiction vocabulary)
        if self._micro:
            try:
                m = self._micro.get_metrics(symbol)
                if m:
                    state["spread_regime"] = "WIDE" if m.spread_regime == "EXPANDED" else "NORMAL"
            except Exception:
                pass

        # Volatility regime
        if self._vol:
            try:
                vm = self._vol.get_metrics(symbol)
                if vm:
                    state["volatility_regime"] = vm.regime
            except Exception:
                pass

        # FVG status for the proposed direction
        fvg_dir = "BULLISH" if direction == "BUY" else "BEARISH"
        for engine in self._fvgs.values():
            try:
                for fvg in engine.get_active():
                    if fvg.direction == fvg_dir:
                        state["has_fvg"] = True
                        state["fvg_status"] = fvg.status.value
                        break
            except Exception:
                continue
            if state["has_fvg"]:
                break

        # OB status for the proposed direction
        ob_dir = OBDirection.BULLISH if direction == "BUY" else OBDirection.BEARISH
        for engine in self._obs.values():
            try:
                for ob in engine.get_active(limit=5):
                    if ob.direction == ob_dir:
                        state["has_ob"] = True
                        state["ob_status"] = ob.state.value
                        break
            except Exception:
                continue
            if state["has_ob"]:
                break

        # Liquidity sweep
        for engine in self._liquidity.values():
            try:
                if engine.get_pools(state="SWEPT"):
                    state["has_liquidity_sweep"] = True
                    break
            except Exception:
                continue

        # Volume profile state (improve.md §39) — feeds contradiction checks
        self._add_profile_state(symbol, state)

        return state

    def _build_market_state_from_context(
        self, symbol: str, direction: str, evidence: dict
    ) -> dict:
        """
        Build the contradiction-engine vocabulary from the shared context.

        Consumes ``MarketContextState`` instead of re-querying every engine.
        Direction-specific bits (FVG/OB status) are derived from the context's
        stored lists so the output matches the legacy rebuild exactly.
        """
        ctx = self._context_engine.get(symbol)
        htf_tf = (
            14400 if 14400 in self._structure
            else (86400 if 86400 in self._structure else None)
        )
        ltf_tf = (
            900 if 900 in self._structure
            else (300 if 300 in self._structure
                  else (60 if 60 in self._structure else None))
        )
        out: dict = {
            "htf_bias": ctx.multi_timeframe_context.get(htf_tf, ""),
            "ltf_bias": ctx.multi_timeframe_context.get(ltf_tf, ""),
            "spread_regime": ctx.spread_regime,
            "has_displacement": "displacement" in evidence,
            "has_fvg": False,
            "fvg_status": "",
            "has_ob": False,
            "ob_status": "",
            "session": ctx.session,
            "volatility_regime": ctx.volatility_regime,
            "has_liquidity_sweep": (
                "liquidity_sweep" in evidence or bool(ctx.liquidity_sweeps)
            ),
            # ── Structural fields (improve.md §41) ──
            "price": ctx.current_price,
            "daily_high": ctx.daily_high,
            "daily_low": ctx.daily_low,
            "liquidity_levels": ctx.liquidity_levels,
        }
        fvg_dir = "BULLISH" if direction == "BUY" else "BEARISH"
        for fvg in ctx.fvgs:
            if fvg.get("direction") == fvg_dir:
                out["has_fvg"] = True
                out["fvg_status"] = fvg.get("status", "")
                break
        ob_dir = OBDirection.BULLISH.value if direction == "BUY" else OBDirection.BEARISH.value
        for ob in ctx.order_blocks:
            if ob.get("direction") == ob_dir:
                out["has_ob"] = True
                out["ob_status"] = ob.get("state", "")
                break
        return out

    def _add_profile_state(self, symbol: str, state: dict) -> None:
        """Attach volume profile state to a market_state dict (best-effort)."""
        if not self._volume_profile:
            return
        try:
            from forex.analysis.volume_profile import calculate_entropy, calculate_reliability
            vp = self._volume_profile.get_profile(symbol)
            if vp and vp.total_activity >= self._volume_profile._config.min_ticks:
                state["profile_position"] = vp.price_position(state.get("last_price", vp.poc))
                state["profile_entropy"] = round(calculate_entropy(vp.bins), 4)
                state["profile_reliability"] = round(calculate_reliability(vp), 3)
                state["value_migration"] = self._volume_profile.get_value_migration(symbol)
        except Exception:
            pass

    def _apply_volatility_normalization(self, symbol: str, draft: SignalDraft) -> None:
        """
        Normalize stop distance + displacement by ATR (m.md §32).

        Replaces fixed-pip thresholds: a stop is judged tight/normal/wide by
        its ATR multiple, and a displacement is down-weighted when it is noise
        relative to current volatility. Best-effort: does nothing if the
        volatility engine or its metrics are unavailable.
        """
        if self._vol is None:
            return
        m = self._vol.get_metrics(symbol)
        if m is None:
            return

        # ── Stop distance: penalize stops that are too tight or too wide ──
        if draft.entry > 0 and draft.stop_loss > 0:
            raw_stop = abs(draft.entry - draft.stop_loss)
            atr_mult = self._vol.normalize_distance(symbol, raw_stop)
            if atr_mult < 0.5:
                draft.confidence -= 4
                draft.evidence["stop_vol"] = -4
                draft.explanation_parts.append(f"stop tight ({atr_mult:.1f} ATR)")
            elif atr_mult > 3.0:
                draft.confidence -= 2
                draft.evidence["stop_vol"] = -2
                draft.explanation_parts.append(f"stop wide ({atr_mult:.1f} ATR)")
            else:
                draft.evidence["stop_vol"] = 0
                draft.explanation_parts.append(f"stop {atr_mult:.1f} ATR (normal)")

        # ── Displacement: down-weight if the move is noise vs ATR ────────
        if "displacement" in draft.evidence:
            candles = self._cache.get(symbol, 60)
            if candles:
                c = candles[-1]
                body = abs(c["close"] - c["open"])
                d_mult = self._vol.normalize_distance(symbol, body)
                base = draft.evidence.get("displacement", 0.0)
                if d_mult < 0.3:
                    draft.confidence -= base * 0.5
                    draft.evidence["displacement_vol"] = -base * 0.5
                    draft.explanation_parts.append("displacement < 0.3 ATR (noise)")

    def _apply_session_context(self, symbol: str, draft: SignalDraft) -> None:
        """
        Fold UTC market-session context into the draft (m.md §34).

        - A liquidity sweep during the London/NY overlap (deepest liquidity)
          is a stronger reversal tell → confidence boost.
        - Price pinned at a session extreme is recorded as evidence so the
          explanation can distinguish breakout vs mean-reversion setups.
        Best-effort: any failure is swallowed so signal scoring never breaks.
        """
        if self._session is None:
            return
        try:
            m = self._session.get_metrics(symbol)
            if m is None:
                return
            w = self._weights

            if m.is_overlap and m.session_liquidity_sweep in ("HIGH", "LOW", "BOTH"):
                boost = w.session_sweep * 0.5
                draft.confidence += boost
                draft.evidence["session_sweep"] = boost
                draft.explanation_parts.append(
                    f"{m.current_session} liquidity sweep ({m.session_liquidity_sweep})"
                )

            if m.price_position >= 0.92:
                draft.evidence["session_position"] = "near_high"
                draft.explanation_parts.append("price near session high")
            elif m.price_position <= 0.08:
                draft.evidence["session_position"] = "near_low"
                draft.explanation_parts.append("price near session low")

            if m.session_expansion > 0:
                draft.evidence["session_expansion"] = round(m.session_expansion, 5)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("[SIGNAL] session context failed for %s: %s", symbol, exc)

    def _build_explanation(self, draft: SignalDraft) -> str:
        """Build a human-readable explanation from evidence.

        Always labels the direction with its five-tier strength so a bare
        one-liner is never the whole story. When ``explanation_parts`` is empty
        (a draft with no granular evidence lines), fall back to lines derived
        from ``draft.evidence`` (the per-factor confidence breakdown) so the
        signal still carries a convincing, strategy-conforming rationale.
        """
        tier = Signal.classify_strength(draft.confidence)
        lines = [f"{draft.direction} — {tier} (confidence {draft.confidence:.0f}/100)"]
        lines.append("")
        lines.append("Why?")
        parts = list(draft.explanation_parts)
        evidence_label = {
            "htf_bias": "HTF structure bias",
            "ob_quality": "order block quality",
            "fvg_imbalance": "fair-value gap imbalance",
            "liquidity_sweep": "liquidity swept",
            "mss": "market-structure shift",
            "displacement": "displacement",
            "poc_interaction": "point-of-control interaction",
            "spread": "favorable spread",
            "data_quality": "healthy market data",
            "entry_confirmation": "entry confirmation",
        }
        if not parts and draft.evidence:
            for key, label in evidence_label.items():
                if key in draft.evidence:
                    parts.append(label)
        for part in parts:
            lines.append(f"- {part}")
        if draft.risk_reward > 0:
            lines.append(f"- R:R = {draft.risk_reward:.1f}")
        if draft.entry > 0:
            entry_line = f"- entry {draft.entry:.5f}"
            if draft.entry_zone_low is not None and draft.entry_zone_high is not None:
                entry_line += f" (zone {draft.entry_zone_low:.5f}-{draft.entry_zone_high:.5f})"
            lines.append(entry_line)
        if draft.stop_loss > 0:
            lines.append(f"- SL {draft.stop_loss:.5f}")
        if draft.take_profit > 0:
            lines.append(f"- TP {draft.take_profit:.5f}")
        return "\n".join(lines)

    def _hold_quality_gate_waiting(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        confidence: float,
        evidence: dict,
        explanation: str,
        strategy: str,
        qg_report: "QualityGateReport",
    ) -> "Signal":
        """
        Hold a setup in WAITING because the signal quality gate failed (§38).

        Updates the existing active setup in place when one exists (dedup),
        otherwise creates a fresh signal, then transitions it to WAITING with
        the gate report attached. Returns the held signal so callers can
        include it in their returned list for downstream broadcast.
        """
        reason = "Signal quality gate failed: " + (
            qg_report.critical_failures[0] if qg_report.critical_failures else "unknown"
        )
        risk = abs(entry - stop_loss)
        rr = abs(take_profit - entry) / risk if risk > 0 else 0.0

        existing = self._lifecycle.find_active_setup(symbol, direction, strategy)
        if existing is not None:
            existing.confidence_evidence.update(evidence)
            existing.confidence_evidence["quality_gate"] = qg_report.to_dict()
            existing.confidence_score = confidence
            existing.entry = entry
            existing.stop_loss = stop_loss
            existing.take_profit = take_profit
            existing.explanation = explanation
            existing.risk_reward_ratio = rr
            existing.strength = Signal.classify_strength(confidence)
            if existing.state in (SignalState.CANDIDATE, SignalState.WATCHING, SignalState.WAITING):
                self._lifecycle.transition(
                    existing, SignalState.WAITING, reason=reason,
                    evidence=qg_report.to_dict(), score=confidence,
                )
                METRICS.increment("signals_waiting")
            return existing

        sig = self._lifecycle.create(
            symbol=symbol, direction=direction, entry=entry, stop_loss=stop_loss,
            take_profit=take_profit, confidence=confidence, evidence=evidence,
            explanation=explanation, strategy=strategy, timeframe="multi",
            market_regime=self._get_regime_string(symbol),
        )
        sig.risk_reward_ratio = rr
        sig.strength = Signal.classify_strength(confidence)
        sig.confidence_evidence["quality_gate"] = qg_report.to_dict()
        self._lifecycle.transition(
            sig, SignalState.WAITING, reason=reason,
            evidence=qg_report.to_dict(), score=confidence,
        )
        METRICS.increment("signals_waiting")
        return sig

    def _hold_contradiction_waiting(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        confidence: float,
        evidence: dict,
        explanation: str,
        strategy: str,
        contra_report: "ContradictionReport",
    ) -> "Signal":
        """
        Hold a setup in WAITING because the contradiction engine returned WAIT.

        Updates the existing active setup in place when one exists (dedup),
        otherwise creates a fresh signal, then transitions it to WAITING with
        the contradiction report attached. Returns the held signal so callers can
        include it in their returned list for downstream broadcast.
        """
        reason = f"Contradiction engine → WAIT ({contra_report.severity}, net={contra_report.net_confidence:.0f})"
        profile_reasons = [
            r for r in contra_report.reasons
            if "profile" in r.lower() or "value area" in r.lower()
        ]
        if profile_reasons:
            reason = (
                f"Contradiction engine → WAIT ({contra_report.severity}, "
                f"net={contra_report.net_confidence:.0f}) + profile: {profile_reasons[0]}"
            )
        risk = abs(entry - stop_loss)
        rr = abs(take_profit - entry) / risk if risk > 0 else 0.0

        existing = self._lifecycle.find_active_setup(symbol, direction, strategy)
        if existing is not None:
                existing.confidence_evidence.update(evidence)
                existing.confidence_score = confidence
                existing.explanation = explanation
                existing.entry = entry
                existing.stop_loss = stop_loss
                existing.take_profit = take_profit
                existing.risk_reward_ratio = rr
                existing.strength = Signal.classify_strength(confidence)
                if existing.state in (SignalState.CANDIDATE, SignalState.WATCHING):
                    self._lifecycle.transition(
                        existing, SignalState.WAITING, reason=reason,
                        evidence=contra_report.to_dict(), score=confidence,
                    )
                    METRICS.increment("signals_waiting")
                elif existing.state == SignalState.WAITING:
                    # Already in WAITING — just refresh evidence, don't re-transition
                    pass
                return existing

        sig = self._lifecycle.create(
            symbol=symbol, direction=direction, entry=entry, stop_loss=stop_loss,
            take_profit=take_profit, confidence=confidence, evidence=evidence,
            explanation=explanation, strategy=strategy, timeframe="multi",
            market_regime=self._get_regime_string(symbol),
        )
        sig.risk_reward_ratio = rr
        sig.strength = Signal.classify_strength(confidence)
        sig.confidence_evidence["contradiction"] = contra_report.to_dict()
        self._lifecycle.transition(
            sig, SignalState.WAITING, reason=reason,
            evidence=contra_report.to_dict(), score=confidence,
        )
        METRICS.increment("signals_waiting")
        return sig

    def _get_regime_string(self, symbol: str) -> str:
        """Get the dominant regime across timeframes."""
        for tf in [14400, 3600]:
            if tf in self._structure:
                regime = self._structure[tf].get_regime(tf)
                return regime.value
        return "UNKNOWN"

    def reset(self) -> None:
        self._lifecycle.reset()
        self._recent_signals.clear()
