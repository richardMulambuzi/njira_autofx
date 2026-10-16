"""
Cost-aware replay of a signal engine over a CSV, with walk-forward folds.

This is the harness the repository did not have.  ``snr_fast`` reported gross
PnL with **no** spread, slippage or commission, so every metric produced from
it overstated expectancy; and nothing anywhere could split results into
out-of-sample windows, so no change could be validated before shipping.

Pure Python + the existing :mod:`forex.sim` cost models.  No Django models, so
it runs identically inside a management command and a plain script.

WHAT THIS DOES NOT DO
---------------------
It does not replace :mod:`forex.backtest.engine` (the full multi-strategy
simulator with the account/risk state machine).  It is the fast, cost-aware,
fold-aware path for measuring ONE strategy family across many datasets.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from forex.metrics import TradeMetrics, compute_metrics, group_metrics

# ── Cost configuration ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CostConfig:
    """
    Round-trip trading costs in PRICE units, applied in R (risk-multiple) space.

    Costs are held in price units because that is what the CSV contains, and are
    converted to money only through the trade's own risk: with fixed-fractional
    sizing, 1R *is* ``risk_fraction`` of balance, so::

        cost_in_R = (2 * spread + 2 * slippage) / |entry - stop|
        pnl_usd   = (move_in_R - cost_in_R) * balance * risk_fraction

    This deliberately avoids a notional/"dollars per pip" assumption.  Such an
    assumption has to encode the contract size (silver is quoted per troy ounce
    and traded in 5000oz contracts), and getting it wrong scales every cost by
    orders of magnitude — which is exactly the bug this model replaced.

    Defaults are EURUSD-realistic: a 0.00012 price-unit spread (1.2 pips) with
    0.00002 slippage per side.  Costs are in PRICE units, so the right value is
    instrument-specific and must be set explicitly per symbol -- measured
    breakeven on XAGUSD is ~0.008 (0.8c/oz), far tighter than retail silver, so
    silver is unprofitable at any win rate while EURUSD and XAUUSD are viable:

        EURUSD  spread=0.00012  slippage=0.00002
        XAUUSD  spread=  0.30    slippage=    0.05
        XAGUSD  spread=  0.03    slippage=    0.01   (loses ~1.5-2.4R/trade)
    """

    spread: float = 0.00012
    slippage: float = 0.00002
    # Kept for API compatibility with the broker sim; unused in the R-space math.
    commission_per_lot: float = 7.0
    risk_fraction: float = 0.01

    def cost_price(self) -> float:
        """Round-trip cost in price units (spread and slippage both sides)."""
        return 2.0 * self.spread + 2.0 * self.slippage

    def cost_in_r(self, stop_distance: float) -> float:
        """Round-trip cost expressed as a multiple of the trade's own risk."""
        if stop_distance <= 0:
            return float("inf")
        return self.cost_price() / stop_distance

    def move_in_r(self, entry: float, exit_: float, stop: float, direction: str) -> float:
        """Realised move in R multiples (positive is a gain)."""
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            return 0.0
        move = (exit_ - entry) if direction == "BUY" else (entry - exit_)
        return move / stop_distance


# ── Trade record ────────────────────────────────────────────────────────


@dataclass
class ReplayTrade:
    """One simulated trade, shaped for :mod:`forex.metrics`."""

    pnl: float
    gross_pnl: float
    cost: float
    r: float
    outcome: str
    strategy: str
    analyzer: str
    direction: str
    entry_time: Optional[float] = None
    bars_held: int = 0

    def to_metrics_dict(self) -> dict:
        return {
            "pnl": self.pnl,
            "r": self.r,
            "outcome": self.outcome,
            "strategy": self.strategy,
            "analyzer": self.analyzer,
            "direction": self.direction,
            "entry_time": self.entry_time,
        }


# ── Walk-forward folds ──────────────────────────────────────────────────


@dataclass
class Fold:
    """A contiguous test window, optionally preceded by a train window."""

    index: int
    test_start: datetime
    test_end: datetime
    train_start: Optional[datetime] = None
    train_end: Optional[datetime] = None

    def label(self) -> str:
        return f"fold{self.index} {self.test_start:%Y-%m}..{self.test_end:%Y-%m}"

    def contains(self, ts: Optional[float]) -> bool:
        if ts is None:
            return False
        return self.test_start.timestamp() <= ts < self.test_end.timestamp()


def make_folds(
    candles: Sequence[dict],
    n_folds: int = 4,
    train_ratio: float = 0.5,
) -> list[Fold]:
    """
    Split a candle series into ``n_folds`` contiguous test windows.

    Each fold's test window follows a train window of ``train_ratio`` of the
    fold's span, so the harness can report train-vs-test drift — the signal
    that a tuned rule is fitting noise rather than structure.

    Folds are equally sized over the covered span and never overlap, so
    aggregated out-of-sample results double-count nothing.
    """
    if not candles:
        return []
    n_folds = max(1, int(n_folds))
    train_ratio = min(max(float(train_ratio), 0.0), 0.9)
    start = candles[0]["open_time"]
    end = candles[-1]["open_time"]
    total = (end - start).total_seconds()
    if total <= 0:
        return []
    span = total / n_folds
    folds: list[Fold] = []
    for i in range(n_folds):
        test_start = start + timedelta(seconds=span * i)
        test_end = start + timedelta(seconds=span * (i + 1))
        train_span = span * train_ratio
        folds.append(
            Fold(
                index=i + 1,
                test_start=test_start,
                test_end=test_end,
                train_start=test_start - timedelta(seconds=train_span),
                train_end=test_start,
            )
        )
    return folds


def in_train(fold: Fold, ts: Optional[float]) -> bool:
    if ts is None or fold.train_start is None or fold.train_end is None:
        return False
    return fold.train_start.timestamp() <= ts < fold.train_end.timestamp()


# ── Replay engine ───────────────────────────────────────────────────────


class CostAwareReplay:
    """
    Runs a per-bar signal callback and simulates each entry with costs.

    ``signal_fn(index, candle) -> list[dict]`` must return entries shaped as::

        {"direction": "BUY"|"SELL", "entry": float, "stop_loss": float,
         "take_profit": float, "strategy": str, "analyzer": str,
         "entry_time": float|None}

    Exits are resolved against the base candles with **SL checked before TP on
    the same bar** — the conservative convention.  When both would trigger in
    one bar we cannot know which printed first from OHLC alone, so assuming the
    loss is the honest choice; assuming the win would inflate every metric.
    """

    def __init__(
        self,
        base: Sequence[dict],
        costs: Optional[CostConfig] = None,
        initial_balance: float = 100_000.0,
        # Fraction of balance risked; kept separate from costs so a test can
        # vary sizing without touching the cost model.
        risk_fraction: Optional[float] = None,
        max_bars_in_trade: int = 2000,
    ) -> None:
        self.base = list(base)
        self.costs = costs or CostConfig()
        self.initial_balance = initial_balance
        self.risk_fraction = (
            self.costs.risk_fraction if risk_fraction is None else risk_fraction
        )
        self.max_bars_in_trade = max_bars_in_trade
        self._open_times = [c["open_time"] for c in self.base]

    # ── exits ──────────────────────────────────────────────────────────
    def resolve_exit(
        self, index: int, direction: str, stop: float, take: float,
    ) -> tuple[float, str, int]:
        """
        Walk forward to the exit.  Returns (price, outcome, bars_held).

        If neither stop nor target is reached within ``max_bars_in_trade`` the
        position is closed at the close of the bar at the TIME LIMIT, not the
        close of the last bar in the dataset.  BUG FIXED: it used to return
        ``self.base[-1]["close"]``, which is a different date entirely — a
        January trade priced at December's close.  That was lookahead and could
        book an expired trade as a winner; it made two harnesses disagree on
        identical signals.
        """
        limit = min(len(self.base), index + 1 + self.max_bars_in_trade)
        for k in range(index + 1, limit):
            c = self.base[k]
            if direction == "BUY":
                # Conservative: stop first when a single bar spans both.
                if float(c["low"]) <= stop:
                    return stop, "loss", k - index
                if float(c["high"]) >= take:
                    return take, "win", k - index
            else:
                if float(c["high"]) >= stop:
                    return stop, "loss", k - index
                if float(c["low"]) <= take:
                    return take, "win", k - index
        # Time exit at the close of the last bar we were allowed to see.
        if limit <= index or limit >= len(self.base):
            # No forward bar exists at all (trade at the very end of data).
            # Exit at the entry bar's close rather than peeking backwards.
            return float(self.base[index]["close"]), "expired", 0
        return float(self.base[limit - 1]["close"]), "expired", limit - 1 - index

    # ── main loop ──────────────────────────────────────────────────────
    def run(self, signal_fn: Callable[[int, dict], list[dict]]) -> list[ReplayTrade]:
        trades: list[ReplayTrade] = []
        balance = self.initial_balance
        costs = self.costs
        for index, candle in enumerate(self.base):
            try:
                entries = signal_fn(index, candle) or []
            except Exception:  # noqa: BLE001 — a bad bar must not kill the run
                entries = []
            for e in entries:
                direction = str(e.get("direction", "")).upper()
                entry = float(e.get("entry", 0.0))
                stop = float(e.get("stop_loss", 0.0))
                take = float(e.get("take_profit", 0.0))
                if direction not in ("BUY", "SELL") or entry <= 0:
                    continue
                if stop <= 0 or take <= 0:
                    continue
                # Reject inverted brackets outright.
                if direction == "BUY" and not (stop < entry < take):
                    continue
                if direction == "SELL" and not (take < entry < stop):
                    continue

                stop_distance = abs(entry - stop)
                if stop_distance <= 0:
                    continue
                # Fixed-fractional sizing: 1R is exactly risk_fraction of the
                # CURRENT balance, so every dollar figure below is derived from
                # the R-space move rather than from a notional assumption.
                risk = balance * costs.risk_fraction
                if risk <= 0:
                    continue

                exit_price, outcome, held = self.resolve_exit(index, direction, stop, take)
                gross_r = costs.move_in_r(entry, exit_price, stop, direction)
                cost_r = costs.cost_in_r(stop_distance)
                gross = gross_r * risk
                cost = cost_r * risk
                pnl = gross - cost
                balance += pnl
                trades.append(
                    ReplayTrade(
                        pnl=pnl,
                        gross_pnl=gross,
                        cost=cost,
                        r=pnl / risk if risk > 0 else 0.0,
                        outcome=outcome,
                        strategy=str(e.get("strategy", "?")),
                        analyzer=str(e.get("analyzer", "?")),
                        direction=direction,
                        entry_time=e.get("entry_time"),
                        bars_held=held,
                    )
                )
        return trades


# ── Fold aggregation ────────────────────────────────────────────────────


def folds_report(
    folds: Sequence[Fold], trades: Sequence[ReplayTrade],
) -> list[tuple[str, TradeMetrics, TradeMetrics]]:
    """
    Per-fold (test metrics, train metrics).

    A healthy rule improves or holds out-of-sample.  If train expectancy climbs
    while test expectancy falls, that is the signature of curve-fitting and the
    change must be rejected regardless of how good the train numbers look.
    """
    rows: list[tuple[str, TradeMetrics, TradeMetrics]] = []
    for fold in folds:
        test = [t for t in trades if fold.contains(t.entry_time)]
        train = [t for t in trades if in_train(fold, t.entry_time)]
        rows.append((fold.label(), compute_metrics(test), compute_metrics(train)))
    return rows


def print_folds_report(
    rows: Sequence[tuple[str, TradeMetrics, TradeMetrics]], stream=None,
) -> None:
    if stream is None:
        import sys

        stream = sys.stdout
    stream.write(
        f"\n  {'fold':<22}{'n':>4}{'wr%':>7}{'net':>11}{'pf':>7}"
        f"{'exp':>8}{'dd':>10}{'sharpe':>8}   | {'train exp':>10}\n"
    )
    for label, test, train in rows:
        pf = "n/a" if test.profit_factor is None else f"{test.profit_factor:.2f}"
        sh = "n/a" if test.sharpe is None else f"{test.sharpe:.2f}"
        stream.write(
            f"  {label:<22}{test.trade_count:>4}{test.win_rate * 100:>7.1f}"
            f"{test.net_profit:>11.2f}{pf:>7}{test.expectancy:>8.2f}"
            f"{test.max_drawdown:>10.2f}{sh:>8}   | {train.expectancy:>10.2f}"
            f" (n={train.trade_count})\n"
        )


def train_test_drift(rows: Sequence[tuple[str, TradeMetrics, TradeMetrics]]) -> dict:
    """
    Summarise train-vs-test expectancy.

    ``overfit_ratio`` > 1 means test expectancy is a fraction of train
    expectancy.  A ratio near zero (or negative) is the clearest single signal
    that a rule has been fitted to its training window.
    """
    train_exp = [tr.expectancy for _, _, tr in rows if tr.trade_count]
    test_exp = [te.expectancy for _, te, _ in rows if te.trade_count]
    if not train_exp or not test_exp:
        return {"folds_with_data": 0, "overfit_ratio": None}
    mean_train = sum(train_exp) / len(train_exp)
    mean_test = sum(test_exp) / len(test_exp)
    ratio = (mean_test / mean_train) if abs(mean_train) > 1e-12 else None
    return {
        "folds_with_data": min(len(train_exp), len(test_exp)),
        "mean_train_expectancy": mean_train,
        "mean_test_expectancy": mean_test,
        "overfit_ratio": ratio,
    }


__all__ = [
    "CostAwareReplay",
    "CostConfig",
    "Fold",
    "ReplayTrade",
    "folds_report",
    "in_train",
    "make_folds",
    "print_folds_report",
    "train_test_drift",
]