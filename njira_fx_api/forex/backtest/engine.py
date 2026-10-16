"""
Backtest engine.

Replays base-timeframe historical candles (1m by default, or a real MT4 M15
upload via ``base_timeframe``) through the SAME analysis pipeline the live
``TicksEngine`` uses (``CandleRegistry`` -> ``HotCache`` -> structure / liquidity
/ order-block / FVG / POC / microstructure engines -> ``MarketContextEngine`` ->
``SignalEngine``), then simulates each CONFIRMED signal as a trade and computes
per-strategy performance metrics including the 70% win-rate gate.

Wiring mirrors ``forex/pepperstone/ticks_engine.py:_on_candle_close`` (lines
412-534) so backtest results reflect production behaviour.  The signal engine is
the only component that may be injected (for tests); when omitted the full real
pipeline is built automatically.
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any, Callable, Optional

from forex.analysis.candle_engine import CandleRegistry
from forex.analysis.cache import HotCache
from forex.analysis.context import MarketContextEngine
from forex.analysis.fvg import FVGEngine
from forex.analysis.liquidity import LiquidityEngine
from forex.analysis.market_structure import MarketStructureEngine
from forex.analysis.microstructure import MicrostructureEngine
from forex.analysis.order_blocks import OrderBlockEngine
from forex.analysis.poc import POCEngine
from forex.analysis.retest import RetestEngine
from forex.analysis.session import SessionEngine
from forex.analysis.signal_engine import SignalEngine
from forex.analysis.signal_lifecycle import Signal, SignalState
from forex.analysis.spread import SpreadEngine
from forex.analysis.volatility import VolatilityEngine
from forex.analysis.volume_profile import VolumeProfileEngine

logger = logging.getLogger("forex.backtest.engine")

# Timeframes the live pipeline aggregates (seconds).  Must match
# ticks_engine.py:194 so higher-TF candles reach the intelligence engines.
TIMEFRAMES = (60, 300, 900, 3600, 14400, 86400)

# ponytail: SL is checked before TP on a single bar (whipsaws hit stop first).
# Conservative but deterministic; upgrade to intrabar sequencing if tick data
# is ever replayed instead of 1m bars.
GATE_WIN_RATE = 70.0


@dataclass
class StrategyBacktestResult:
    """Per-strategy aggregated metrics for one backtest run."""

    strategy: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    expired: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    expectancy: float = 0.0
    passes_70_gate: bool = False

    def __post_init__(self) -> None:
        if self.total_trades:
            self.win_rate = self.wins / self.total_trades * 100.0
        self.passes_70_gate = self.win_rate >= GATE_WIN_RATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "expired": self.expired,
            "win_rate": round(self.win_rate, 2),
            "profit_factor": round(self.profit_factor, 2),
            "total_pnl": round(self.total_pnl, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "expectancy": round(self.expectancy, 2),
            "passes_70_gate": self.passes_70_gate,
        }


@dataclass
class BacktestResult:
    """Full result of a backtest run."""

    symbol: str
    initial_balance: float
    final_balance: float
    total_pnl: float
    overall_win_rate: float
    per_strategy: list[StrategyBacktestResult] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    best_strategy: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "initial_balance": self.initial_balance,
            "final_balance": round(self.final_balance, 2),
            "total_pnl": round(self.total_pnl, 2),
            "overall_win_rate": round(self.overall_win_rate, 2),
            "best_strategy": self.best_strategy,
            "per_strategy": [s.to_dict() for s in self.per_strategy],
            "trades": self.trades,
        }


def build_execution_steps(sig: Any) -> list[str]:
    """
    Turn a signal's ``confidence_evidence`` + ``explanation`` into a readable,
    ordered list of execution steps (WHERE the signal was taken and WHY).
    """
    ev = getattr(sig, "confidence_evidence", None) or {}
    steps: list[str] = []

    simple = {
        "htf_bias": "HTF bias confirmed",
        "ob_quality": "Order block quality",
        "fvg_imbalance": "FVG imbalance detected",
        "liquidity_sweep": "Liquidity sweep",
        "mss": "Market structure shift (MSS)",
        "displacement": "Displacement",
        "poc_interaction": "POC interaction",
        "entry_confirmation": "Entry confirmation / microstructure trigger",
        "spread": "Spread",
        "data_quality": "Market data healthy",
    }
    for key, label in simple.items():
        if key in ev:
            val = ev[key]
            if isinstance(val, (int, float)) and val != 0:
                steps.append(f"{label} (+{val} conf)")
            else:
                steps.append(label)

    for key in ("ob_retest", "fvg_retest"):
        for item in ev.get(key) or []:
            if not isinstance(item, dict):
                continue
            reaction = item.get("reaction")
            zone = item.get("zone_top") or item.get("zone_high")
            label = key.split("_")[0].upper()
            step = f"{label} retest {reaction}"
            if zone is not None:
                step += f" at {zone}"
            steps.append(step)

    mtf = ev.get("mtf_breakdown")
    if isinstance(mtf, dict):
        for tf, direction in mtf.items():
            steps.append(f"{tf} alignment: {direction}")

    contra = ev.get("contradiction")
    if isinstance(contra, dict):
        rec = contra.get("recommendation", "none")
        sev = contra.get("severity")
        reasons = contra.get("reasons") or []
        step = f"Contradiction: {rec}"
        if sev:
            step += f" ({sev})"
        if reasons:
            step += ": " + "; ".join(str(r) for r in reasons)
        steps.append(step)

    qg = ev.get("quality_gate")
    if isinstance(qg, dict):
        steps.append(f"Quality gate passed: {qg.get('passed')}")

    if not steps:
        expl = getattr(sig, "explanation", "") or ""
        steps.append(expl if expl else "No evidence captured")
    return steps


class BacktestEngine:
    """Replay 1m candles through the analysis pipeline and simulate trades."""

    def __init__(self, signal_engine: Optional[SignalEngine] = None,
                 strategy_mode: str = "malysian", fast: bool = False) -> None:
        self._strategy_mode = strategy_mode
        self._signal_engine = signal_engine
        self._real_pipeline = signal_engine is None
        # ponytail: fast=True skips the per-candle intelligence engines
        # (volatility/context/OB/FVG/POC/microstructure) that only feed the
        # contradiction engine's market_state. The SNR family treats contradiction
        # as advisory and falls back to a neutral market_state when context is
        # absent, so those engines are dead weight for an SNR-only backtest —
        # they were ~40% of runtime.
        self._fast = fast

        self._hot_cache = HotCache()
        self._candle_registry = CandleRegistry(
            timeframes=TIMEFRAMES, on_candle_close=self._on_candle_close
        )

        # Real-pipeline engine slots (populated only when building the pipeline).
        self._microstructure = None
        self._spread_engine = None
        self._volatility = None
        self._session = None
        self._volume_profile = None
        self._structure_engines: dict[int, Any] = {}
        self._liquidity_engines: dict[int, Any] = {}
        self._ob_engines: dict[int, Any] = {}
        self._fvg_engines: dict[int, Any] = {}
        self._poc_engines: dict[int, Any] = {}
        self._context_engine = None
        self._retest_engine = None

        # Replay state.
        self._collected: list[tuple[int, Any, dict]] = []
        self._base_candles: list[dict] = []
        self._open_times: list[Any] = []
        self._symbol: Optional[str] = None
        self._base_timeframe: int = 60
        self._uploaded_tfs: set[int] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(
        self,
        candles_1m: list[dict],
        symbol: str,
        strategies: Optional[list[str]] = None,
        initial_balance: float = 10000.0,
        risk_per_trade: float = 0.01,
        progress_callback: Optional[Callable[[dict], None]] = None,
        higher_tf_candles: Optional[dict[int, list[dict]]] = None,
        base_timeframe: int = 60,
        funnel_only: bool = False,
    ) -> BacktestResult:
        """
        Run a backtest over ``candles_1m`` and return a ``BacktestResult``.

        ``candles_1m`` are the BASE candles (the finest timeframe available) —
        normally 1-minute bars, but a real MT4 M15 upload passes 900s bars.
        ``base_timeframe`` is the seconds-per-base-bar (default 60).  Ticks are
        reconstructed from the base OHLC at 0/25/50/75% of the bar and the
        CandleRegistry re-aggregates every timeframe from that base, so M15
        base data yields M15/M30/H1/H4/D1 correctly.

        ``higher_tf_candles`` (optional) maps timeframe-seconds to lists of
        REAL higher-timeframe bars (from :func:`parse_tf_csv`, each carrying
        ``symbol``/``open_time``/OHLC/``volume``/``timeframe``).  These are
        preloaded into the hot cache and override aggregated bars for the
        strategy read path; the CandleRegistry still aggregates every timeframe
        for the per-TF intelligence engines (structure/liquidity/OB/FVG).

        ``progress_callback`` (optional) is invoked roughly every 1% of candles
        with ``{"processed", "total", "percent", "signals_so_far", "symbol"}``
        for streaming/observation UI.  The engine runs inline in the caller's
        thread, so the callback is invoked on the same thread (no locking).

        ``funnel_only`` (optional) replays the candles exactly as normal (so the
        signal engine runs and the SNR funnel counters populate) but SKIPS the
        per-trade bar-by-bar simulation, returning an empty trade set.  This is
        the fast path for tuning the funnel gates: it reports where candidate
        setups die without paying for each simulated trade's forward scan.
        """
        self._uploaded_tfs = set()
        self._base_timeframe = int(base_timeframe)
        if not candles_1m:
            return BacktestResult(
                symbol=symbol, initial_balance=initial_balance,
                final_balance=initial_balance, total_pnl=0.0,
                overall_win_rate=0.0, per_strategy=[], trades=[],
                best_strategy=None,
            )

        if strategies:
            # ponytail: registry keys are "malysian" / "Moving_averages"; map the
            # common lowercase alias so a user-supplied "moving_averages" isn't
            # silently skipped (matches live evaluate()'s exact-match lookup).
            self._strategy_mode = ",".join(
                "Moving_averages" if s.lower() == "moving_averages" else s
                for s in strategies
            )

        if self._real_pipeline:
            self._build_real_pipeline()

        self._symbol = symbol
        self._base_candles = candles_1m
        self._open_times = [c["open_time"] for c in candles_1m]
        self._collected = []
        self._candle_registry.reset()
        self._hot_cache.reset()

        if higher_tf_candles:
            for tf, bars in higher_tf_candles.items():
                self._hot_cache.set_candles(symbol, int(tf), bars)
            self._uploaded_tfs = set(higher_tf_candles.keys())

        # Reconstruct ticks from the base OHLC so CandleRegistry rebuilds the
        # bars (open/high/low/close) and aggregates every timeframe.  A trailing
        # sentinel tick closes the final base bar so no data is dropped.  Tick
        # offsets are proportional to the base timeframe (0/25/50/75%).
        total = len(candles_1m)
        base = self._base_timeframe
        offsets = (0, base // 4, base // 2, base * 3 // 4)
        # Report progress ~once per 1% of candles (at least once for tiny CSVs).
        progress_step = max(1, total // 100)
        last_ts = 0
        for i, c in enumerate(candles_1m):
            t0 = int(c["open_time"].timestamp())
            last_ts = t0
            o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
            for price, offset in ((o, offsets[0]), (h, offsets[1]), (l, offsets[2]), (cl, offsets[3])):
                self._candle_registry.feed_tick(
                    symbol, price, price, (t0 + offset) * 1000
                )
            if progress_callback is not None and (
                (i + 1) % progress_step == 0 or i + 1 == total
            ):
                try:
                    progress_callback({
                        "processed": i + 1,
                        "total": total,
                        "percent": round((i + 1) / total * 100.0, 1),
                        "signals_so_far": len(self._collected),
                        "symbol": symbol,
                    })
                except Exception as exc:  # noqa: BLE001 — progress must never break replay
                    logger.debug("progress callback failed: %s", exc)
        if last_ts:
            self._candle_registry.feed_tick(
                symbol, candles_1m[-1]["close"], candles_1m[-1]["close"],
                (last_ts + base) * 1000,
            )

        return self._simulate(initial_balance, risk_per_trade, funnel_only=funnel_only)

    # ------------------------------------------------------------------
    # Candle-close callback (mirrors ticks_engine._on_candle_close)
    # ------------------------------------------------------------------
    def _on_candle_close(self, candle: dict) -> None:
        symbol = candle["symbol"]
        tf = int(candle["timeframe"])

        if tf in self._uploaded_tfs:
            # Real uploaded bars are authoritative for this timeframe — don't
            # clobber the hot-cache deque with aggregated bars.  Aggregation
            # still feeds the CandleRegistry + per-TF intelligence engines
            # (structure/liquidity/OB/FVG); ONLY the hot-cache read path
            # (signal engine's cache.get) is overridden.
            pass
        else:
            self._hot_cache.add(candle)

        if self._real_pipeline and not self._fast:
            if self._volatility is not None:
                try:
                    spread = 0.0
                    if self._microstructure is not None:
                        m = self._microstructure.get_metrics(symbol)
                        spread = m.spread_pips if m is not None else 0.0
                    self._volatility.update(symbol, candle, spread=spread)
                except Exception as exc:  # noqa: BLE001 — non-fatal
                    logger.debug("volatility update failed: %s", exc)

            if self._volume_profile is not None:
                try:
                    ot = candle.get("open_time")
                    if ot is not None:
                        # parser.py:325 guarantees timezone-aware UTC datetimes via
                        # datetime.fromtimestamp(bucket_start, tz=timezone.utc).
                        # No wall-clock fallback needed; none may be reintroduced.
                        ts_ms = int(ot.timestamp() * 1000)
                        self._volume_profile.feed_candle(
                            symbol, tf, float(candle["open"]), float(candle["high"]),
                            float(candle["low"]), float(candle["close"]), ts_ms,
                        )
                except Exception as exc:  # noqa: BLE001 — non-fatal
                    logger.debug("volume profile feed failed: %s", exc)

            # Gap-fill candles are synthetic — cache/persist but skip analysis.
            if candle.get("gap_filled"):
                return

            if tf not in self._structure_engines:
                self._structure_engines[tf] = MarketStructureEngine(swing_lookback=3)
                self._liquidity_engines[tf] = LiquidityEngine()
                self._ob_engines[tf] = OrderBlockEngine()
                self._fvg_engines[tf] = FVGEngine()
                self._poc_engines[tf] = POCEngine()

            if self._context_engine is not None:
                try:
                    self._context_engine.update(symbol, candle)
                except Exception as exc:  # noqa: BLE001 — non-fatal
                    logger.debug("context update failed: %s", exc)

        # Evaluate signals on every candle close (event-driven, like live).
        # Backtest perf: skip the strategy status reports (UI-only, 2x compute).
        # The signal engine itself gates MTF/confluence off 1M/5M closes, so
        # the expensive strategy work is already avoided on low-TF closes.
        try:
            signals = self._signal_engine.evaluate(
                symbol, self._strategy_mode, closed_tf=tf, skip_status=True
            )
        except Exception as exc:  # noqa: BLE001 — never break the replay
            logger.debug("signal evaluate failed: %s", exc)
            return

        for sig in signals:
            if sig.state == SignalState.CONFIRMED:
                idx = self._index_for_open_time(candle.get("open_time"))
                # Snapshot the levels NOW.  A refreshed signal keeps its
                # signal_id but is handed a new entry/SL/TP (and its evidence
                # dict is mutated in place), and _simulate keeps only the FIRST
                # collect per id — so without a snapshot the trade would be
                # entered on its first bar at the price of its last refresh.
                self._collected.append((idx, sig, {
                    "entry": float(sig.entry),
                    "stop_loss": float(sig.stop_loss),
                    "take_profit": float(sig.take_profit),
                    "evidence": dict(sig.confidence_evidence or {}),
                }))

    # ------------------------------------------------------------------
    # Trade simulation
    # ------------------------------------------------------------------
    def _simulate(self, initial_balance: float, risk_per_trade: float,
                  funnel_only: bool = False) -> BacktestResult:
        trades: list[dict] = []
        consumed: set[str] = set()
        balance = initial_balance

        self._collected.sort(key=lambda x: x[0])
        if funnel_only:
            # Replay already ran the signal engine (funnel counters are
            # populated); skip the O(n) forward scan per collected signal and
            # return an empty result.  Callers read the funnel, not trades.
            return BacktestResult(
                symbol=self._symbol or "",
                initial_balance=initial_balance,
                final_balance=initial_balance,
                total_pnl=0.0,
                overall_win_rate=0.0,
                per_strategy=[],
                trades=[],
                best_strategy=None,
            )
        for idx, sig, levels in self._collected:
            sid = sig.signal_id
            if sid in consumed:
                continue
            consumed.add(sid)
            trade = self._simulate_trade(sig, idx, levels, risk_per_trade, balance)
            trades.append(trade)
            balance += trade["pnl"]

        per_strategy = self._compute_metrics(trades)
        overall = self._overall_win_rate(trades)
        best = self._best_strategy(per_strategy)
        final_balance = initial_balance + sum(t["pnl"] for t in trades)

        return BacktestResult(
            symbol=self._symbol or "",
            initial_balance=initial_balance,
            final_balance=final_balance,
            total_pnl=final_balance - initial_balance,
            overall_win_rate=overall,
            per_strategy=per_strategy,
            trades=trades,
            best_strategy=best,
        )

    def _simulate_trade(self, sig: Any, idx: int, levels: Optional[dict] = None,
                        risk_per_trade: float = 0.01,
                        current_balance: float = 10000.0) -> dict:
        direction = sig.direction
        # ``levels`` is the snapshot taken when the signal was collected; fall
        # back to the live signal for direct/unit callers.
        levels = levels or {}
        entry = float(levels.get("entry", sig.entry))
        sl = float(levels.get("stop_loss", sig.stop_loss))
        tp = float(levels.get("take_profit", sig.take_profit))

        price_risk = abs(entry - sl)
        risk_amount = current_balance * risk_per_trade
        units = risk_amount / price_risk if price_risk > 0 else 0.0

        exit_price: Optional[float] = None
        exit_time: Optional[Any] = None
        outcome = "expired"

        # 1R partial + breakeven (mirrors live Signal.update_trailing_stop):
        # first touch of 1R banks 50% and moves SL to entry.  SL is still
        # checked first inside each bar (conservative SL-first sequencing).
        partial_level = entry + price_risk if direction == "BUY" else entry - price_risk
        partial_hit = False
        partial_exit: Optional[float] = None

        for j in range(idx + 1, len(self._base_candles)):
            c = self._base_candles[j]
            if direction == "BUY":
                if c["low"] <= sl:
                    exit_price, outcome, exit_time = sl, ("scratch" if partial_hit else "loss"), c["open_time"]
                    break
                if not partial_hit and c["high"] >= partial_level:
                    partial_hit, partial_exit = True, partial_level
                    sl = entry  # BE on the remainder
                if c["high"] >= tp:
                    exit_price, outcome, exit_time = tp, "win", c["open_time"]
                    break
            else:  # SELL
                if c["high"] >= sl:
                    exit_price, outcome, exit_time = sl, ("scratch" if partial_hit else "loss"), c["open_time"]
                    break
                if not partial_hit and c["low"] <= partial_level:
                    partial_hit, partial_exit = True, partial_level
                    sl = entry  # BE on the remainder
                if c["low"] <= tp:
                    exit_price, outcome, exit_time = tp, "win", c["open_time"]
                    break

        if exit_price is None:
            last = self._base_candles[-1]
            exit_price = float(last["close"])
            exit_time = last["open_time"]
            outcome = "expired"

        if partial_hit and partial_exit is not None:
            # 50% banked at 1R, remainder at the final exit.
            if direction == "BUY":
                pnl = 0.5 * (partial_exit - entry) * units + 0.5 * (exit_price - entry) * units
            else:
                pnl = 0.5 * (entry - partial_exit) * units + 0.5 * (entry - exit_price) * units
        elif direction == "BUY":
            pnl = (exit_price - entry) * units
        else:
            pnl = (entry - exit_price) * units

        entry_time = (
            self._base_candles[idx]["open_time"] if 0 <= idx < len(self._base_candles) else None
        )

        sources = list(getattr(sig, "source_strategies", None) or [])
        if not sources and sig.strategy:
            sources = [sig.strategy]
        combined = bool(getattr(sig, "combined", False)) or len(set(sources)) > 1

        return {
            "signal_id": sig.signal_id,
            "strategy": sig.strategy,
            "combined": combined,
            "source_strategies": sources,
            "direction": direction,
            "entry": entry,
            "stop_loss": sl,
            "take_profit": tp,
            "entry_time": entry_time.isoformat() if entry_time else None,
            "exit": exit_price,
            "exit_time": exit_time.isoformat() if exit_time else None,
            "pnl": round(pnl, 2),
            "outcome": outcome,
            "confidence": sig.confidence_score,
            "strength": sig.strength or Signal.classify_strength(sig.confidence_score),
            "explanation": sig.explanation,
            "evidence": levels.get("evidence") or sig.confidence_evidence,
            "execution_steps": build_execution_steps(sig),
        }

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def _compute_metrics(self, trades: list[dict]) -> list[StrategyBacktestResult]:
        by_strategy: dict[str, list[dict]] = {}
        for t in trades:
            by_strategy.setdefault(t["strategy"], []).append(t)

        results: list[StrategyBacktestResult] = []
        for strategy, strat_trades in by_strategy.items():
            total = len(strat_trades)
            wins = [t for t in strat_trades if t["outcome"] == "win"]
            losses = [t for t in strat_trades if t["outcome"] == "loss"]
            expired = [t for t in strat_trades if t["outcome"] == "expired"]

            win_rate = (len(wins) / total * 100.0) if total else 0.0
            wins_pnl = sum(t["pnl"] for t in wins)
            losses_pnl = sum(abs(t["pnl"]) for t in losses)
            total_pnl = sum(t["pnl"] for t in strat_trades)

            if losses_pnl > 0:
                profit_factor = wins_pnl / losses_pnl
            elif wins_pnl > 0:
                profit_factor = float("inf")
            else:
                profit_factor = 0.0

            avg_win = wins_pnl / len(wins) if wins else 0.0
            avg_loss = -losses_pnl / len(losses) if losses else 0.0
            expectancy = total_pnl / total if total else 0.0

            results.append(StrategyBacktestResult(
                strategy=strategy,
                total_trades=total,
                wins=len(wins),
                losses=len(losses),
                expired=len(expired),
                win_rate=win_rate,
                profit_factor=profit_factor,
                total_pnl=total_pnl,
                avg_win=avg_win,
                avg_loss=avg_loss,
                max_drawdown=self._max_drawdown(strat_trades),
                expectancy=expectancy,
            ))
        return results

    @staticmethod
    def _max_drawdown(trades: list[dict]) -> float:
        """Peak-to-trough on the cumulative pnl of this strategy's trades."""
        peak = 0.0
        trough = 0.0
        cum = 0.0
        for t in trades:
            cum += t["pnl"]
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > trough:
                trough = dd
        return trough

    @staticmethod
    def _overall_win_rate(trades: list[dict]) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t["outcome"] == "win")
        return wins / len(trades) * 100.0

    @staticmethod
    def _best_strategy(per_strategy: list[StrategyBacktestResult]) -> Optional[str]:
        if not per_strategy:
            return None
        passing = [s for s in per_strategy if s.passes_70_gate]
        pool = passing if passing else per_strategy
        return max(pool, key=lambda s: s.win_rate).strategy

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _index_for_open_time(self, open_time: Any) -> int:
        """Map a closed candle's open_time to the nearest preceding base-candle index."""
        if open_time is None or not self._open_times:
            return 0
        if open_time.tzinfo is None:
            open_time = open_time.replace(tzinfo=timezone.utc)
        pos = bisect.bisect_right(self._open_times, open_time)
        return max(0, pos - 1)

    def _build_real_pipeline(self) -> None:
        """Construct the full analysis pipeline (mirrors ticks_engine)."""
        self._microstructure = MicrostructureEngine()
        self._spread_engine = SpreadEngine()
        self._volatility = VolatilityEngine()
        self._session = SessionEngine()
        self._volume_profile = VolumeProfileEngine()
        self._structure_engines = {}
        self._liquidity_engines = {}
        self._ob_engines = {}
        self._fvg_engines = {}
        self._poc_engines = {}
        self._context_engine = MarketContextEngine(
            structure_engines=self._structure_engines,
            liquidity_engines=self._liquidity_engines,
            ob_engines=self._ob_engines,
            fvg_engines=self._fvg_engines,
            poc_engines=self._poc_engines,
            microstructure_engine=self._microstructure,
            volatility_engine=self._volatility,
            session_engine=self._session,
            spread_engine=self._spread_engine,
            volume_profile_engine=self._volume_profile,
            cache=self._hot_cache,
        )
        self._retest_engine = RetestEngine(self._hot_cache)
        self._signal_engine = SignalEngine(
            cache=self._hot_cache,
            structure_engines=self._structure_engines,
            liquidity_engines=self._liquidity_engines,
            ob_engines=self._ob_engines,
            fvg_engines=self._fvg_engines,
            poc_engines=self._poc_engines,
            microstructure_engine=self._microstructure,
            volatility_engine=self._volatility,
            session_engine=self._session,
            spread_engine=self._spread_engine,
            volume_profile_engine=self._volume_profile,
            context_engine=self._context_engine,
            retest_engine=self._retest_engine,
            min_confidence=30,
            min_rr=1.5,
        )
