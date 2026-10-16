"""Django ORM models for the forex app.

Currently only chart drawings (TradingView-style trendlines / order blocks
drawn on the Markets page). Stored per symbol with replace-all semantics —
the client POSTs the full shape list for a symbol and the previous set is
replaced. Drawings are workspace annotations, not time-series data, so they
live in SQLite via the ORM rather than ClickHouse.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ChartDrawing(models.Model):
    """A single drawn shape (trendline or order-block rectangle) on a chart."""

    KIND_TREND = "trend"
    KIND_RECT = "rect"

    symbol = models.CharField(max_length=32, db_index=True)
    kind = models.CharField(max_length=8, choices=[(KIND_TREND, "trend"), (KIND_RECT, "rect")])
    # Chart coordinates: t = epoch seconds (bar time), p = price.
    t1 = models.FloatField()
    p1 = models.FloatField()
    t2 = models.FloatField()
    p2 = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forex_chart_drawings"
        ordering = ["created_at"]

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f"{self.symbol} {self.kind} @{self.t1}"


class BacktestResult(models.Model):
    """Persisted result of a backtest run (admin CSV upload)."""

    symbol = models.CharField(max_length=32, db_index=True)
    csv_name = models.CharField(max_length=255, blank=True, default="")
    initial_balance = models.FloatField()
    final_balance = models.FloatField()
    total_pnl = models.FloatField()
    overall_win_rate = models.FloatField()
    best_strategy = models.CharField(max_length=64, blank=True, default="")
    per_strategy = models.JSONField(default=list)
    trades = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    class Meta:
        db_table = "forex_backtest_results"
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f"{self.symbol} backtest {self.created_at}"
