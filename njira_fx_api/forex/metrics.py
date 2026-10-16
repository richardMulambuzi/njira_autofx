"""
Trade-performance metrics (the numbers every backtest decision is made on).

Pure, deterministic, no I/O and no Django imports — usable from a management
command, a test, or a notebook alike.  This module exists because the engine
had **no** profit-factor / expectancy / Sharpe implementation at all: the only
statistic anywhere was ``_max_drawdown`` in ``backtest/engine.py``, which meant
no tuning decision could be made on evidence.

TRADE CONTRACT
--------------
A "trade" is a plain dict (or anything with the same attributes).  Recognised
keys, all optional except ``pnl``:

===========  =========================================================
``pnl``      float, REQUIRED.  Net profit/loss in account currency.
``r``        float.  Realised R multiple.  Enables R-based metrics.
``outcome``  ``"win" | "loss" | "breakeven"``.  Derived from ``pnl`` when
             absent (zero => breakeven).
``entry_time``  epoch seconds.  Required by :func:`monthly_returns` and
             :func:`max_consecutive_losses` windowed stats.
===========  =========================================================

Grouping
--------
:func:`group_metrics` splits trades on any key (``"family"``,
``"mode"``, ``"analyzer"``, ``"symbol"`` …).  Use it so a weak sub-population
can never hide inside an aggregate — reporting one blended number across
families or modes is how a losing segment gets shipped by accident.

Conventions
-----------
* Profit factor is ``gross_profit / gross_loss``.  Both zero => ``None``
  (undefined, not "perfect").
* Expectancy is the mean net PnL per trade, in currency; ``expectancy_r`` is the
  same in R multiples when ``r`` is present.
* Sharpe/Sharpe-sortino annualise per-trade returns with
  ``SQRT(trades_per_year)``.  Set ``periods_per_year`` explicitly when the
  sample is intraday and irregular — otherwise the annualisation is fiction.
* Drawdown is reported as a positive magnitude (a loss depth), not a negative
  number, so it reads directly as "how bad did it get".
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

# Trades per year used to annualise per-trade Sharpe when the caller does not
# say.  ~250 trading days x a few trades/day for an intraday book.
DEFAULT_PERIODS_PER_YEAR = 500.0

# A trade whose |pnl| is at or below this is treated as flat rather than a win
# or a loss.  Guards against float noise making a scratch exit count as a win.
FLAT_EPSILON = 1e-9


def _get(trade: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or an attribute-bearing object."""
    if isinstance(trade, dict):
        return trade.get(key, default)
    return getattr(trade, key, default)


def outcome_of(trade: Any) -> str:
    """``"win" | "loss" | "breakeven"`` for one trade."""
    explicit = _get(trade, "outcome")
    if isinstance(explicit, str) and explicit.lower() in ("win", "loss", "breakeven"):
        return explicit.lower()
    pnl = float(_get(trade, "pnl") or 0.0)
    if pnl > FLAT_EPSILON:
        return "win"
    if pnl < -FLAT_EPSILON:
        return "loss"
    return "breakeven"


def _stddev(values: Sequence[float]) -> float:
    """Sample standard deviation (n-1).  Returns 0.0 for <2 samples."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


# ── Individual metrics ───────────────────────────────────────────────────

def gross_profit(trades: Iterable[Any]) -> float:
    return sum(t for t in (float(_get(x, "pnl") or 0.0) for x in trades) if t > FLAT_EPSILON)


def gross_loss(trades: Iterable[Any]) -> float:
    """Positive magnitude of losing PnL (losses are stored negative)."""
    return -sum(t for t in (float(_get(x, "pnl") or 0.0) for x in trades) if t < -FLAT_EPSILON)


def net_profit(trades: Iterable[Any]) -> float:
    return sum(float(_get(x, "pnl") or 0.0) for x in trades)


def win_rate(trades: Iterable[Any]) -> float:
    """Fraction of NON-flat trades that won, 0..1.  0.0 when there are none."""
    decided = [x for x in trades if outcome_of(x) != "breakeven"]
    if not decided:
        return 0.0
    return sum(1 for x in decided if outcome_of(x) == "win") / len(decided)


def profit_factor(trades: Iterable[Any]) -> Optional[float]:
    """``gross_profit / gross_loss``.  ``None`` when there is no loss to divide by."""
    trades = list(trades)
    loss = gross_loss(trades)
    if loss <= FLAT_EPSILON:
        return None
    return gross_profit(trades) / loss


def expectancy(trades: Iterable[Any]) -> float:
    """Mean net PnL per trade, in account currency."""
    trades = list(trades)
    if not trades:
        return 0.0
    return net_profit(trades) / len(trades)


def expectancy_r(trades: Iterable[Any]) -> Optional[float]:
    """Mean realised R per trade.  ``None`` when no trade carries ``r``."""
    rs = [float(_get(x, "r")) for x in trades if _get(x, "r") is not None]
    if not rs:
        return None
    return sum(rs) / len(rs)


def avg_win(trades: Iterable[Any]) -> float:
    wins = [float(_get(x, "pnl") or 0.0) for x in trades if outcome_of(x) == "win"]
    return sum(wins) / len(wins) if wins else 0.0


def avg_loss(trades: Iterable[Any]) -> float:
    """Average losing trade as a POSITIVE magnitude."""
    losses = [-float(_get(x, "pnl") or 0.0) for x in trades if outcome_of(x) == "loss"]
    return sum(losses) / len(losses) if losses else 0.0


def payoff_ratio(trades: Iterable[Any]) -> Optional[float]:
    """``avg_win / avg_loss``.  ``None`` until there is at least one loss."""
    al = avg_loss(trades)
    if al <= FLAT_EPSILON:
        return None
    return avg_win(trades) / al


def max_consecutive_losses(trades: Iterable[Any]) -> int:
    """Longest unbroken run of losing trades (breakevens do not break a run)."""
    run = best = 0
    for t in trades:
        if outcome_of(t) == "loss":
            run += 1
            best = max(best, run)
        elif outcome_of(t) == "win":
            run = 0
    return best


def max_drawdown(trades: Iterable[Any], equity: Optional[float] = None) -> float:
    """
    Peak-to-trough equity decline as a POSITIVE magnitude.

    ``equity`` seeds the running balance (default 0.0).  When supplied, the
    drawdown is returned as a PERCENTAGE of the peak equity; otherwise as an
    absolute currency amount.
    """
    peak = equity if equity is not None else 0.0
    worst = 0.0
    running = peak
    for t in trades:
        running += float(_get(t, "pnl") or 0.0)
        if running > peak:
            peak = running
        dd = peak - running
        if dd > worst:
            worst = dd
    if equity is not None and peak > FLAT_EPSILON:
        return worst / peak * 100.0
    return worst


def _returns(trades: Sequence[Any], equity: Optional[float] = None) -> list[float]:
    """
    Per-trade simple returns.

    Prefers the explicit ``r`` multiple.  Falls back to ``pnl / equity`` when a
    starting equity is supplied; without one, a trade's return is undefined
    (you cannot divide a PnL by an unknown base), so it is skipped rather than
    silently treated as 0.0 — which would deflate the mean and invent Sharpe.
    """
    out: list[float] = []
    for t in trades:
        r = _get(t, "r")
        if r is not None:
            out.append(float(r))
        elif equity:
            out.append(float(_get(t, "pnl") or 0.0) / equity)
    return out


def sharpe_ratio(
    trades: Iterable[Any],
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
    equity: Optional[float] = None,
) -> Optional[float]:
    """
    Annualised Sharpe from per-trade R (or PnL when seeded with ``equity``).

    ``None`` when returns are undefined or have no dispersion — a "Sharpe" of
    infinity from a flat series is a bug report, not a result.
    """
    trades = list(trades)
    rs = _returns(trades, equity)
    if len(rs) < 2:
        return None
    sd = _stddev(rs)
    if sd <= FLAT_EPSILON:
        return None
    mean = sum(rs) / len(rs)
    return (mean / sd) * math.sqrt(periods_per_year)


def sortino_ratio(
    trades: Iterable[Any],
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
    equity: Optional[float] = None,
) -> Optional[float]:
    """Like Sharpe but only downside deviation counts against the mean."""
    trades = list(trades)
    rs = _returns(trades, equity)
    if len(rs) < 2:
        return None
    downside = [min(0.0, r) for r in rs]
    dd = math.sqrt(sum(d * d for d in downside) / len(downside))
    if dd <= FLAT_EPSILON:
        return None
    mean = sum(rs) / len(rs)
    return (mean / dd) * math.sqrt(periods_per_year)


def monthly_returns(trades: Iterable[Any]) -> dict[str, float]:
    """Net PnL keyed by ``YYYY-MM`` — requires ``entry_time`` (epoch seconds)."""
    import datetime as _dt

    buckets: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = _get(t, "entry_time")
        if ts is None:
            continue
        month = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc).strftime("%Y-%m")
        buckets[month] += float(_get(t, "pnl") or 0.0)
    return dict(sorted(buckets.items()))


# ── Aggregate container ─────────────────────────────────────────────────

@dataclass
class TradeMetrics:
    """Every headline number for one population of trades."""

    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    breakeven_count: int = 0
    win_rate: float = 0.0
    net_profit: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: Optional[float] = None
    expectancy: float = 0.0
    expectancy_r: Optional[float] = None
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: Optional[float] = None
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_drawdown: float = 0.0
    max_consecutive_losses: int = 0
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR

    def to_dict(self) -> dict:
        return {
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "breakeven_count": self.breakeven_count,
            "win_rate": round(self.win_rate, 6),
            "net_profit": round(self.net_profit, 2),
            "gross_profit": round(self.gross_profit, 2),
            "gross_loss": round(self.gross_loss, 2),
            "profit_factor": None if self.profit_factor is None else round(self.profit_factor, 4),
            "expectancy": round(self.expectancy, 4),
            "expectancy_r": None if self.expectancy_r is None else round(self.expectancy_r, 4),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "payoff_ratio": None if self.payoff_ratio is None else round(self.payoff_ratio, 4),
            "sharpe": None if self.sharpe is None else round(self.sharpe, 4),
            "sortino": None if self.sortino is None else round(self.sortino, 4),
            "max_drawdown": round(self.max_drawdown, 2),
            "max_consecutive_losses": self.max_consecutive_losses,
        }

    def row(self) -> str:
        """One-line human summary for a backtest report."""
        pf = "n/a" if self.profit_factor is None else f"{self.profit_factor:.2f}"
        sh = "n/a" if self.sharpe is None else f"{self.sharpe:.2f}"
        return (
            f"n={self.trade_count:<4} wr={self.win_rate * 100:5.1f}%  "
            f"net={self.net_profit:>10.2f}  pf={pf:>5}  "
            f"exp={self.expectancy:>8.2f}  dd={self.max_drawdown:>9.2f}  "
            f"sharpe={sh:>5}  maxLossStreak={self.max_consecutive_losses}"
        )


def compute_metrics(
    trades: Iterable[Any],
    equity: Optional[float] = None,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
) -> TradeMetrics:
    """Compute every metric for one population of trades.

    ``equity`` seeds the drawdown so ``max_drawdown`` is a percentage of peak
    equity; leave it ``None`` for an absolute currency drawdown.
    """
    trades = list(trades)
    outcomes = [outcome_of(t) for t in trades]
    wins = sum(1 for o in outcomes if o == "win")
    losses = sum(1 for o in outcomes if o == "loss")
    flats = sum(1 for o in outcomes if o == "breakeven")
    decided = wins + losses
    return TradeMetrics(
        trade_count=len(trades),
        win_count=wins,
        loss_count=losses,
        breakeven_count=flats,
        win_rate=(wins / decided) if decided else 0.0,
        net_profit=net_profit(trades),
        gross_profit=gross_profit(trades),
        gross_loss=gross_loss(trades),
        profit_factor=profit_factor(trades),
        expectancy=expectancy(trades),
        expectancy_r=expectancy_r(trades),
        avg_win=avg_win(trades),
        avg_loss=avg_loss(trades),
        payoff_ratio=payoff_ratio(trades),
        sharpe=sharpe_ratio(trades, periods_per_year, equity),
        sortino=sortino_ratio(trades, periods_per_year, equity),
        max_drawdown=max_drawdown(trades, equity),
        max_consecutive_losses=max_consecutive_losses(trades),
        periods_per_year=periods_per_year,
    )


def group_metrics(
    trades: Iterable[Any],
    key: str = "analyzer",
    equity: Optional[float] = None,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
) -> dict[str, TradeMetrics]:
    """
    Split trades on ``key`` and compute metrics per bucket.

    This is the guard against blended reporting: per-family and per-mode
    numbers must be readable separately, so a losing segment cannot be
    averaged away by a winning one.
    """
    buckets: dict[str, list[Any]] = defaultdict(list)
    for t in trades:
        buckets[str(_get(t, key, "unknown"))].append(t)
    return {
        k: compute_metrics(v, equity, periods_per_year)
        for k, v in sorted(buckets.items())
    }


def print_metrics_table(
    metrics_by_group: dict[str, TradeMetrics],
    title: str = "",
    stream=None,
) -> None:
    """Render grouped metrics as aligned text rows."""
    if stream is None:
        import sys

        stream = sys.stdout
    if title:
        stream.write(f"\n{title}\n")
    stream.write(
        f"  {'group':<14}{'n':>5}{'wr%':>7}{'net':>12}{'pf':>7}"
        f"{'exp':>9}{'dd':>10}{'sharpe':>8}{'streak':>8}\n"
    )
    for name, m in metrics_by_group.items():
        pf = "n/a" if m.profit_factor is None else f"{m.profit_factor:.2f}"
        sh = "n/a" if m.sharpe is None else f"{m.sharpe:.2f}"
        stream.write(
            f"  {name:<14}{m.trade_count:>5}{m.win_rate * 100:>7.1f}"
            f"{m.net_profit:>12.2f}{pf:>7}{m.expectancy:>9.2f}"
            f"{m.max_drawdown:>10.2f}{sh:>8}{m.max_consecutive_losses:>8}\n"
        )


# ── Comparison (frozen baseline vs candidate) ───────────────────────────

@dataclass
class ComparisonVerdict:
    """Result of comparing a candidate population against the frozen baseline."""

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reasons": self.reasons,
            "improvements": self.improvements,
            "regressions": self.regressions,
        }


#: Metrics that must not get worse for a candidate to be accepted.
GUARDRAIL_METRICS = ("profit_factor", "expectancy", "max_drawdown", "max_consecutive_losses")


def compare_to_baseline(
    candidate: TradeMetrics,
    baseline: TradeMetrics,
    min_trades: int = 30,
) -> ComparisonVerdict:
    """
    Decide whether a candidate beats the frozen baseline.

    Rules, in order:
      1. A candidate with fewer than ``min_trades`` is REJECTED — a 4-trade
         90% win rate is not evidence, and accepting it is how overfitting
         gets shipped.
      2. Profit factor must not fall below the baseline.
      3. Expectancy must not fall below the baseline.
      4. Max drawdown and max consecutive losses must not increase.
    A strict tie is not an improvement, so it is rejected with a stated reason.
    """
    reasons: list[str] = []
    improvements: list[str] = []
    regressions: list[str] = []

    if candidate.trade_count < min_trades:
        reasons.append(
            f"sample too small: {candidate.trade_count} trades < {min_trades} required"
        )
        return ComparisonVerdict(False, reasons, improvements, regressions)

    if candidate.net_profit > baseline.net_profit:
        improvements.append(f"net profit {candidate.net_profit:.2f} > {baseline.net_profit:.2f}")
    elif candidate.net_profit < baseline.net_profit:
        regressions.append(f"net profit {candidate.net_profit:.2f} < {baseline.net_profit:.2f}")
    # An exact tie is neither an improvement nor a regression — it falls
    # through so the "no metric improved" rule below produces the real reason.

    for name in GUARDRAIL_METRICS:
        c, b = getattr(candidate, name), getattr(baseline, name)
        if name in ("max_drawdown", "max_consecutive_losses"):
            if c > b:
                regressions.append(f"{name} worsened: {c} > {b}")
            elif c < b:
                improvements.append(f"{name} improved: {c} < {b}")
            continue
        # profit_factor / expectancy: None means undefined for that population.
        if c is None and b is None:
            continue
        if c is None or b is None:
            # An undefined metric cannot be shown to beat a defined one.
            regressions.append(f"{name} undefined on one side (candidate={c}, baseline={b})")
            continue
        if c > b:
            improvements.append(f"{name} {c:.4f} > {b:.4f}")
        elif c < b:
            regressions.append(f"{name} {c:.4f} < {b:.4f}")

    if regressions:
        return ComparisonVerdict(False, reasons + regressions, improvements, regressions)
    if not improvements:
        reasons.append("no metric improved over the frozen baseline")
        return ComparisonVerdict(False, reasons, improvements, regressions)
    return ComparisonVerdict(True, reasons, improvements, regressions)


__all__ = [
    "DEFAULT_PERIODS_PER_YEAR",
    "FLAT_EPSILON",
    "GUARDRAIL_METRICS",
    "ComparisonVerdict",
    "TradeMetrics",
    "avg_loss",
    "avg_win",
    "compare_to_baseline",
    "compute_metrics",
    "expectancy",
    "expectancy_r",
    "gross_loss",
    "gross_profit",
    "group_metrics",
    "max_consecutive_losses",
    "max_drawdown",
    "monthly_returns",
    "net_profit",
    "outcome_of",
    "payoff_ratio",
    "print_metrics_table",
    "profit_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "win_rate",
]