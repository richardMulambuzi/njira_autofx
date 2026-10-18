"""
Tests for the strict 300-bar H4 window (``forex.pepperstone.h4_window``).

These lock the four "Latest IN, Last OUT" eviction rules, the 300-bar
invariant, the integrity pipeline (validate -> gap-fill -> quarantine -> sweep)
and the Mon-Fri trading-day arithmetic. Pure, offline, no socket.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from forex.pepperstone.h4_window import (
    BACKFILL_CAL_DAYS,
    H4_BARS_PER_TRADING_DAY,
    H4_SECONDS,
    WINDOW_SIZE,
    H4Window,
    align_to_h4,
    backfill_start_ms,
    is_trading_day,
    trading_days_between,
    trading_days_covered,
)

STEP = H4_SECONDS * 1000
# 2026-01-05 is a Monday, midnight UTC, which lands exactly on the H4 grid.
BASE = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)


def bar(ts, open_=1.0, high=2.0, low=0.5, close=1.5, volume=10):
    return {
        "epoch": ts,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def window():
    win = H4Window(symbol="XAUUSD")
    for i in range(WINDOW_SIZE):
        win.ingest(bar(BASE + i * STEP))
    return win


def test_overwrite_forming_bar_does_not_evict():
    win = H4Window(symbol="XAUUSD")
    win.ingest(bar(BASE, close=1.5))
    win.ingest(bar(BASE, high=10.0, close=9.9))

    assert len(win) == 1
    assert win.latest()["close"] == 9.9
    assert win.get_metrics()["overwrite"] == 1


def test_append_on_next_bar_evicts_the_oldest():
    win = window()
    assert len(win) == WINDOW_SIZE

    win.ingest(bar(BASE + WINDOW_SIZE * STEP))

    assert len(win) == WINDOW_SIZE
    assert win.snapshot()[0]["epoch"] == BASE + STEP
    assert win.latest()["epoch"] == BASE + WINDOW_SIZE * STEP
    assert win.get_metrics()["append"] == WINDOW_SIZE + 1


def test_gap_flags_degraded_and_alerts_without_padding():
    win = H4Window(symbol="XAUUSD")
    win.ingest(bar(BASE))
    action = win.ingest(bar(BASE + 3 * STEP))

    assert action == "gap"
    assert win.degraded is True
    assert len(win) == 2
    alerts = win.drain_alerts()
    assert alerts[0]["kind"] == "gap"
    assert alerts[0]["missing"] == 2
    assert win.get_metrics()["open_gaps"] == 1


def test_stale_bar_is_ignored():
    win = H4Window(symbol="XAUUSD")
    win.ingest(bar(BASE))
    win.ingest(bar(BASE + STEP))
    before = win.snapshot()

    assert win.ingest(bar(BASE)) == "stale"
    assert win.snapshot() == before
    assert win.get_metrics()["stale"] == 1


def test_window_holds_exactly_300_bars_one_forming_299_closed():
    win = window()
    metrics = win.get_metrics()

    assert len(win) == WINDOW_SIZE
    assert metrics["forming"] == 1
    assert metrics["closed"] == WINDOW_SIZE - 1
    assert metrics["trading_days"] == 50
    assert win.is_ready() is True


def test_window_never_exceeds_300_over_a_long_stream():
    win = H4Window(symbol="XAUUSD")
    for i in range(WINDOW_SIZE * 2):
        win.ingest(bar(BASE + i * STEP))

    assert len(win) == WINDOW_SIZE
    assert win.latest()["epoch"] == BASE + (WINDOW_SIZE * 2 - 1) * STEP


def test_quarantine_rejects_inconsistent_ohlc():
    win = H4Window(symbol="XAUUSD")
    win.ingest(bar(BASE))

    action = win.ingest(bar(BASE + STEP, open_=5.0, high=2.0, low=0.5, close=1.5))

    assert action == "quarantined"
    assert len(win) == 1
    assert win.get_metrics()["quarantined"] == 1
    assert win.drain_alerts()[0]["kind"] == "quarantined"


def test_quarantine_rejects_missing_and_non_numeric_fields():
    win = H4Window(symbol="XAUUSD")

    assert win.ingest({"epoch": BASE, "open": 1.0}) == "quarantined"
    assert win.ingest(bar(BASE, close="nope")) == "quarantined"
    assert len(win) == 0


def test_backfill_merges_out_of_order_and_clears_the_gap():
    win = H4Window(symbol="XAUUSD")
    win.ingest(bar(BASE))
    win.ingest(bar(BASE + 3 * STEP))
    assert win.degraded is True

    accepted = win.backfill([bar(BASE + 2 * STEP), bar(BASE + STEP)])

    assert accepted == 2
    assert win.degraded is False
    assert len(win) == 4
    assert [b["epoch"] for b in win.snapshot()] == [
        BASE,
        BASE + STEP,
        BASE + 2 * STEP,
        BASE + 3 * STEP,
    ]


def test_backfill_trims_to_the_newest_window():
    win = H4Window(symbol="XAUUSD")
    win.backfill([bar(BASE + i * STEP) for i in range(WINDOW_SIZE + 25)])

    assert len(win) == WINDOW_SIZE
    assert win.snapshot()[0]["epoch"] == BASE + 25 * STEP


def test_sweep_drops_off_grid_and_out_of_order_bars():
    win = H4Window(symbol="XAUUSD")
    win.ingest(bar(BASE))
    win.ingest(bar(BASE + STEP))
    win._bars.append(bar(BASE + STEP + 1))
    win._bars.append(bar(BASE))

    dropped = win.sweep()

    assert dropped == 2
    assert len(win) == 2
    assert win.degraded is False


def test_is_trading_day_is_mon_to_fri_only():
    assert is_trading_day(date(2026, 1, 5)) is True
    assert is_trading_day(date(2026, 1, 9)) is True
    assert is_trading_day(date(2026, 1, 10)) is False
    assert is_trading_day(date(2026, 1, 11)) is False


def test_trading_days_between_skips_weekends():
    assert trading_days_between(date(2026, 1, 5), date(2026, 1, 11)) == 5
    assert trading_days_between(date(2026, 1, 10), date(2026, 1, 11)) == 0
    assert trading_days_between(date(2026, 1, 9), date(2026, 1, 5)) == 0


def test_300_bars_is_50_trading_days():
    assert H4_BARS_PER_TRADING_DAY == 6
    assert trading_days_covered(WINDOW_SIZE) == 50


def test_align_to_h4_snaps_to_the_bar_open():
    assert align_to_h4(BASE) == BASE
    assert align_to_h4(BASE + 3_599_000) == BASE
    assert align_to_h4(BASE + STEP + 1) == BASE + STEP


def test_backfill_window_is_91_calendar_days():
    assert backfill_start_ms(BASE) == BASE - BACKFILL_CAL_DAYS * 86_400 * 1000
