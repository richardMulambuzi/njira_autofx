"""Tests for the candle range theory (CRT) sweep-and-deliver detector.

Synthetic candles only - no fixtures, no DB, no network. Every Config knob is
asserted to actually change behaviour, not merely exist.

Window note: the swept bar is ``candles[-2]`` (the last CLOSED bar, since the
caller may still be updating ``candles[-1]``), while the reference range comes
from ``candles[-(range_bars + 2):-2]``. Those two slices must not overlap: if
the swept bar were inside its own reference range, a close sweep would have to
exceed the bar's own high and could never fire at all. That was a real bug in
this module and the first test below pins it.

Arithmetic note: a close sweep needs ``close > range_high`` (or ``close <
range_low``) and ``tick_size`` is 0.00001, so a 0.0010 price move is 100 ticks.
``min_range_pips`` default 5.0 with ``pip_size`` 0.0001 rejects any reference
range under 0.0005.
"""
from datetime import datetime, timezone

from pytest import approx

from forex.analysis.candle_range_theory import (
    CONCEPT_NAME,
    DEFAULT_CRT_CONFIG,
    CrtConfig,
    CrtEngine,
    CrtEvent,
    CrtState,
    _compute_reference_range,
)

HOUR = 3600
DAY = 86400
MONDAY = int(datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc).timestamp())


def bar(hour, high, low, close=None, open_=None, day=0):
    ts = MONDAY + day * DAY + int(float(hour) * HOUR)
    open_ = low if open_ is None else open_
    close = open_ if close is None else close
    return {"open_time": ts, "open": open_, "high": high, "low": low, "close": close}


def flat(hour, price, day=0):
    """A zero-range bar: never a departure, never a base wild."""
    return bar(hour, price, price, close=price, open_=price, day=day)


def engine(**overrides):
    return CrtEngine(CrtConfig(**overrides))


def last(eng):
    events = eng.get_events(1)
    return events[-1] if events else None


def opener(day=0):
    return flat(0, 1.0805, day=day)


def rng(hour, day=0):
    """A 10-pip reference bar: high 1.0810, low 1.0800."""
    return bar(hour, 1.0810, 1.0800, close=1.0805, open_=1.0802, day=day)


def sweep_high(hour, day=0):
    """Closes at 1.0900, i.e. 900 ticks above the 1.0810 range high."""
    return bar(hour, 1.0900, 1.0800, close=1.0900, open_=1.0805, day=day)


def sweep_low(hour, day=0):
    """Closes at 1.0700, i.e. 1000 ticks below the 1.0800 range low."""
    return bar(hour, 1.0815, 1.0700, close=1.0700, open_=1.0808, day=day)


def wick_high(hour, day=0):
    """Pierces the range high on the wick only; the close stays inside."""
    return bar(hour, 1.0850, 1.0805, close=1.0808, open_=1.0805, day=day)


def pierce_both(hour, day=0):
    """Pierces both edges on the wicks; only a wick check can even see this."""
    return bar(hour, 1.0900, 1.0700, close=1.0805, open_=1.0805, day=day)


def delivers_down(hour, day=0):
    """Trades down to the 1.0800 range low without delivering up."""
    return bar(hour, 1.0820, 1.0790, close=1.0795, open_=1.0810, day=day)


def delivers_up(hour, day=0):
    """Trades up to the 1.0900 range high without delivering down."""
    return bar(hour, 1.0905, 1.0820, close=1.0900, open_=1.0805, day=day)


def idles(hour, day=0):
    """Stays strictly inside the range: neither sweeps nor delivers."""
    return bar(hour, 1.0809, 1.0801, close=1.0805, open_=1.0804, day=day)


def runup(day=0):
    """Seven bars: an opener then six reference bars.

    At seven bars the reference window (``candles[-7:-2]``) is indices 0..4,
    which already spans the full 10-pip range.
    """
    return [opener(day)] + [rng(h, day) for h in range(1, 7)]


def high_sweep_series(day=0):
    """Runup, a close sweep, a down delivery, then an inert pad.

    The pad keeps the delivery bar off ``candles[-1]``, so each event carries
    the timestamp of the bar that was actually examined.
    """
    return runup(day) + [sweep_high(7, day), delivers_down(8, day), idles(9, day)]


def low_sweep_series(day=0):
    return runup(day) + [sweep_low(7, day), delivers_up(8, day), idles(9, day)]


def with_trailing(name, day=0):
    """Nine bars with `name` on ``candles[-2]``, i.e. the bar under test.

    The pad at index 8 is never examined - only the timestamp of ``candles[-1]``
    is read from it - so it cannot affect the sweep result.
    """
    return runup(day) + [name(7, day), idles(8, day)]


def tiny(day=0):
    """Seven bars whose reference span is 0.00005 - below the 5-pip gate."""
    return [flat(0, 1.08000, day=day)] + [
        bar(h, 1.08005, 1.08000, close=1.08002, open_=1.08001, day=day)
        for h in range(1, 7)
    ]


# ── Reference range ─────────────────────────────────────────────────────────


def test_reference_range_excludes_the_swept_bar():
    """Regression: the bar being tested must sit OUTSIDE its own range."""
    window = high_sweep_series()[:9]
    low, high = _compute_reference_range(window, 5)

    assert low == approx(1.0800)
    assert high == approx(1.0810)

    swept_bar = window[-2]
    assert swept_bar["high"] == approx(1.0900)
    assert swept_bar["high"] > high

    # Replay the pre-fix slice to prove the bug was fatal, not cosmetic: it
    # pulled the swept bar into its own window, so `close > range_high` could
    # never be satisfied by any bar whatsoever.
    buggy_window = window[-(5 + 1):-1]
    buggy_high = max(c["high"] for c in buggy_window)
    assert buggy_high == approx(1.0900)
    assert not (swept_bar["close"] > buggy_high)


def test_reference_range_returns_none_when_bars_do_not_exceed_the_window():
    assert _compute_reference_range(runup()[:5], 5) is None


def test_reference_range_returns_none_when_the_window_is_short():
    # Six bars clear the first guard but leave only four reference bars.
    assert _compute_reference_range(runup()[:6], 5) is None


def test_reference_range_returns_none_for_a_zero_range_window():
    flats = [flat(h, 1.0805) for h in range(7)]
    assert _compute_reference_range(flats, 5) is None


def test_reference_range_uses_the_requested_number_of_bars():
    # Three bars from a pure runup: indices 2..4, every one a 10-pip range bar.
    low, high = _compute_reference_range(runup(), 3)
    assert low == approx(1.0800)
    assert high == approx(1.0810)

    # Five bars from the same runup: indices 0..4, still the same range.
    low, high = _compute_reference_range(runup(), 5)
    assert low == approx(1.0800)
    assert high == approx(1.0810)

    # The window really is `range_bars` wide, not a fixed slice.
    low, high = _compute_reference_range(tiny(), 5)
    assert low == approx(1.08000)
    assert high == approx(1.08005)


# ── Config ──────────────────────────────────────────────────────────────────


def test_concept_name_and_defaults():
    assert CONCEPT_NAME == "candle_range_theory"
    assert DEFAULT_CRT_CONFIG == CrtConfig()
    assert DEFAULT_CRT_CONFIG.range_bars == 5
    assert DEFAULT_CRT_CONFIG.sweep_confirm == "close"
    assert DEFAULT_CRT_CONFIG.min_sweep_depth_ticks == approx(0.0)
    assert DEFAULT_CRT_CONFIG.max_bars_deliver == 20
    assert DEFAULT_CRT_CONFIG.tick_size == approx(0.00001)
    assert DEFAULT_CRT_CONFIG.pip_size == approx(0.0001)
    assert DEFAULT_CRT_CONFIG.min_range_pips == approx(5.0)


def test_enum_values_are_stable():
    assert CrtState.FORMING.value == "FORMING"
    assert CrtState.SWEPT_HIGH.value == "SWEPT_HIGH"
    assert CrtState.SWEPT_LOW.value == "SWEPT_LOW"
    assert CrtState.DELIVERED_UP.value == "DELIVERED_UP"
    assert CrtState.DELIVERED_DOWN.value == "DELIVERED_DOWN"
    assert CrtEvent.CRT_RANGE_SET.value == "CRT_RANGE_SET"
    assert CrtEvent.CRT_SWEPT_HIGH.value == "CRT_SWEPT_HIGH"
    assert CrtEvent.CRT_SWEPT_LOW.value == "CRT_SWEPT_LOW"
    assert CrtEvent.CRT_DELIVERED.value == "CRT_DELIVERED"


# ── Guards ──────────────────────────────────────────────────────────────────


def test_too_few_candles_returns_empty():
    eg = engine()
    assert eg.analyze([]) == []
    assert eg.analyze([opener()]) == []


def test_short_window_returns_empty():
    assert engine().analyze(runup()[:6]) == []


def test_span_below_min_range_pips_returns_empty():
    assert engine().analyze(tiny()) == []


def test_span_above_min_range_pips_proceeds():
    assert len(engine().analyze(runup())) == 1


# ── First look ──────────────────────────────────────────────────────────────


def test_first_look_emits_range_set_forming():
    events = engine().analyze(runup(), symbol="EURUSD", tf=60)

    assert len(events) == 1
    assert events[0]["event"] == CrtEvent.CRT_RANGE_SET.value
    assert events[0]["state"] == CrtState.FORMING.value
    assert events[0]["range_low"] == approx(1.0800)
    assert events[0]["range_high"] == approx(1.0810)
    assert events[0]["swept_side"] == ""
    assert events[0]["sweep_depth_ticks"] == approx(0.0)
    assert events[0]["delivery_target"] == approx(0.0)
    assert events[0]["bars_since_sweep"] == 0
    assert events[0]["symbol"] == "EURUSD"
    assert events[0]["timeframe"] == 60
    assert events[0]["timestamp"] == MONDAY + 6 * HOUR


def test_first_look_is_not_recorded_in_the_event_log():
    """First look returns early, so it bypasses the rolling event buffer."""
    eg = engine()
    eg.analyze(runup())
    assert eg.get_events(10) == []


def test_second_look_without_a_sweep_is_silent():
    eg = engine()
    eg.analyze(runup())
    assert eg.analyze(runup() + [idles(7)]) == []


def test_repeating_the_same_call_is_idempotent_while_forming():
    eg = engine()
    eg.analyze(runup())
    assert eg.analyze(runup()) == []
    assert eg.analyze(runup()) == []


# ── Sweeps ──────────────────────────────────────────────────────────────────


def test_high_sweep_event_payload():
    eg = engine()
    eg.analyze(runup(), symbol="EURUSD", tf=60)
    events = eg.analyze(with_trailing(sweep_high), symbol="EURUSD", tf=60)

    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == CrtEvent.CRT_SWEPT_HIGH.value
    assert ev["state"] == CrtState.SWEPT_HIGH.value
    assert ev["swept_side"] == "HIGH"
    assert ev["sweep_depth_ticks"] == approx(900.0)
    assert ev["delivery_target"] == approx(1.0800)
    assert ev["bars_since_sweep"] == 0
    assert ev["range_low"] == approx(1.0800)
    assert ev["range_high"] == approx(1.0810)
    assert ev["symbol"] == "EURUSD"
    assert ev["timeframe"] == 60
    assert ev["timestamp"] == MONDAY + 8 * HOUR


def test_low_sweep_event_payload():
    eg = engine()
    eg.analyze(runup(), symbol="EURUSD", tf=60)
    events = eg.analyze(with_trailing(sweep_low), symbol="EURUSD", tf=60)

    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == CrtEvent.CRT_SWEPT_LOW.value
    assert ev["state"] == CrtState.SWEPT_LOW.value
    assert ev["swept_side"] == "LOW"
    assert ev["sweep_depth_ticks"] == approx(1000.0)
    assert ev["delivery_target"] == approx(1.0810)
    assert ev["bars_since_sweep"] == 0
    assert ev["symbol"] == "EURUSD"
    assert ev["timeframe"] == 60
    assert ev["timestamp"] == MONDAY + 8 * HOUR


def test_sweep_payload_key_set_is_exact():
    series = high_sweep_series()
    eg = engine()
    eg.analyze(series[:7])
    ev = eg.analyze(series[:9])[0]

    assert set(ev) == {
        "event",
        "timestamp",
        "symbol",
        "timeframe",
        "range_low",
        "range_high",
        "swept_side",
        "sweep_depth_ticks",
        "delivery_target",
        "bars_since_sweep",
        "state",
    }
    assert "created_at" not in ev


def test_sweep_is_recorded_in_the_event_log():
    series = high_sweep_series()
    eg = engine()
    eg.analyze(series[:7])
    eg.analyze(series[:9])

    logged = eg.get_events(10)
    assert len(logged) == 1
    assert logged[0]["event"] == CrtEvent.CRT_SWEPT_HIGH.value


def test_sweep_updates_the_active_record():
    series = high_sweep_series()
    eg = engine()
    eg.analyze(series[:7], symbol="EURUSD", tf=60)
    eg.analyze(series[:9], symbol="EURUSD", tf=60)

    active = eg.get_active()
    assert len(active) == 1
    assert active[0]["symbol"] == "EURUSD"
    assert active[0]["state"] == CrtState.SWEPT_HIGH.value
    assert active[0] == eg.get_all()[-1]


# ── Delivery ────────────────────────────────────────────────────────────────


def test_high_sweep_delivers_down():
    series = high_sweep_series()
    eg = engine()
    eg.analyze(series[:7])
    eg.analyze(series[:9])
    events = eg.analyze(series)

    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == CrtEvent.CRT_DELIVERED.value
    assert ev["state"] == CrtState.DELIVERED_DOWN.value
    assert ev["swept_side"] == "HIGH"
    assert ev["sweep_depth_ticks"] == approx(900.0)
    assert ev["delivery_target"] == approx(1.0800)
    assert ev["bars_since_sweep"] == 1
    assert ev["timestamp"] == MONDAY + 9 * HOUR


def test_low_sweep_delivers_up():
    series = low_sweep_series()
    eg = engine()
    eg.analyze(series[:7])
    eg.analyze(series[:9])
    events = eg.analyze(series)

    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == CrtEvent.CRT_DELIVERED.value
    assert ev["state"] == CrtState.DELIVERED_UP.value
    assert ev["swept_side"] == "LOW"
    assert ev["sweep_depth_ticks"] == approx(1000.0)
    assert ev["delivery_target"] == approx(1.0810)
    assert ev["bars_since_sweep"] == 1
    assert ev["timestamp"] == MONDAY + 9 * HOUR


def test_delivery_resets_to_forming():
    series = high_sweep_series()
    eg = engine()
    eg.analyze(series[:7])
    eg.analyze(series[:9])
    eg.analyze(series)
    events = eg.analyze(series)

    assert [e["event"] for e in events] == [CrtEvent.CRT_RANGE_SET.value]
    assert events[0]["state"] == CrtState.FORMING.value
    assert events[0]["swept_side"] == ""
    assert events[0]["bars_since_sweep"] == 0


# ── Sweep confirmation mode ─────────────────────────────────────────────────


def test_close_confirm_ignores_a_wick_only_break():
    eg = engine()
    eg.analyze(runup())
    assert eg.analyze(with_trailing(wick_high)) == []


def test_wick_confirm_detects_a_wick_only_break():
    eg = engine(sweep_confirm="wick")
    eg.analyze(runup())
    events = eg.analyze(with_trailing(wick_high))

    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == CrtEvent.CRT_SWEPT_HIGH.value
    assert ev["swept_side"] == "HIGH"
    assert ev["sweep_depth_ticks"] == approx(400.0)
    assert ev["delivery_target"] == approx(1.0800)


def test_both_edges_pierced_is_not_a_sweep():
    eg = engine(sweep_confirm="wick")
    eg.analyze(runup())
    assert eg.analyze(with_trailing(pierce_both)) == []


def test_close_confirm_still_fires_on_a_close_sweep():
    series = high_sweep_series()
    eg = engine()
    eg.analyze(series[:7])
    assert eg.analyze(series[:9])[0]["event"] == CrtEvent.CRT_SWEPT_HIGH.value


# ── Sweep depth gate ────────────────────────────────────────────────────────


def test_min_sweep_depth_ticks_blocks_a_shallow_sweep():
    # Needs close > 1.0810 + 1000*0.00001 = 1.0910; the bar closes at 1.0900.
    eg = engine(min_sweep_depth_ticks=1000.0)
    eg.analyze(runup())
    assert eg.analyze(with_trailing(sweep_high)) == []


def test_min_sweep_depth_ticks_allows_a_deep_enough_sweep():
    # Needs close > 1.0810 + 800*0.00001 = 1.0890; the bar closes at 1.0900.
    eg = engine(min_sweep_depth_ticks=800.0)
    eg.analyze(runup())
    events = eg.analyze(with_trailing(sweep_high))

    assert len(events) == 1
    assert events[0]["event"] == CrtEvent.CRT_SWEPT_HIGH.value
    assert events[0]["sweep_depth_ticks"] == approx(900.0)


def test_min_sweep_depth_ticks_applies_to_the_low_side():
    # Needs close < 1.0800 - 800*0.00001 = 1.0720; the bar closes at 1.0700.
    eg = engine(min_sweep_depth_ticks=800.0)
    eg.analyze(runup())
    events = eg.analyze(with_trailing(sweep_low))

    assert len(events) == 1
    assert events[0]["event"] == CrtEvent.CRT_SWEPT_LOW.value
    assert events[0]["sweep_depth_ticks"] == approx(1000.0)


# ── Delivery timeout ────────────────────────────────────────────────────────


def test_max_bars_deliver_resets_without_a_delivery():
    deep = runup() + [sweep_high(7), idles(8)]
    wider = deep + [idles(9)]

    eg = engine(max_bars_deliver=1)
    eg.analyze(runup())
    assert [e["event"] for e in eg.analyze(deep)] == [CrtEvent.CRT_SWEPT_HIGH.value]

    # `idles(8)` never reaches the 1.0800 target, so this is a pure timeout.
    events = eg.analyze(wider)
    assert [e["event"] for e in events] == [CrtEvent.CRT_RANGE_SET.value]
    assert events[0]["state"] == CrtState.FORMING.value


def test_max_bars_deliver_of_zero_resets_immediately():
    deep = runup() + [sweep_high(7), idles(8)]

    eg = engine(max_bars_deliver=0)
    eg.analyze(runup())
    events = eg.analyze(deep)
    assert [e["event"] for e in events] == [CrtEvent.CRT_SWEPT_HIGH.value]

    events = eg.analyze(deep + [idles(9)])
    assert [e["event"] for e in events] == [CrtEvent.CRT_RANGE_SET.value]


def test_bars_since_sweep_counts_up_until_the_timeout():
    # max_bars_deliver=3 -> two silent increments then a reset.
    deep = runup() + [sweep_high(7), idles(8)]

    eg = engine(max_bars_deliver=3)
    eg.analyze(runup())
    assert [e["event"] for e in eg.analyze(deep)] == [CrtEvent.CRT_SWEPT_HIGH.value]

    assert eg.analyze(deep + [idles(9)]) == []
    assert eg.analyze(deep + [idles(9), idles(10)]) == []

    events = eg.analyze(deep + [idles(9), idles(10), idles(11)])
    assert [e["event"] for e in events] == [CrtEvent.CRT_RANGE_SET.value]


# ── Isolation and reset ─────────────────────────────────────────────────────


def test_symbols_do_not_share_state():
    eg = engine()
    eg.analyze(runup(), symbol="EURUSD", tf=60)
    eg.analyze(runup(), symbol="XAUUSD", tf=60)
    assert len(eg.get_all()) == 2

    swept = eg.analyze(with_trailing(sweep_high), symbol="EURUSD", tf=60)
    assert swept[0]["event"] == CrtEvent.CRT_SWEPT_HIGH.value

    # The same candles leave XAUUSD untouched, still FORMING and silent.
    assert eg.analyze(runup(), symbol="XAUUSD", tf=60) == []


def test_timeframes_do_not_share_state():
    eg = engine()
    eg.analyze(runup(), symbol="EURUSD", tf=60)
    eg.analyze(runup(), symbol="EURUSD", tf=300)
    assert len(eg.get_all()) == 2

    swept = eg.analyze(with_trailing(sweep_high), symbol="EURUSD", tf=60)
    assert swept[0]["event"] == CrtEvent.CRT_SWEPT_HIGH.value

    assert eg.analyze(runup(), symbol="EURUSD", tf=300) == []


def test_reset_clears_state_and_events():
    series = high_sweep_series()
    eg = engine()
    eg.analyze(series[:7])
    eg.analyze(series[:9])
    assert eg.get_events(10)

    eg.reset()
    assert eg.get_events(10) == []
    assert eg.get_active() == []
    assert eg.get_all() == []

    events = eg.analyze(series[:7])
    assert [e["event"] for e in events] == [CrtEvent.CRT_RANGE_SET.value]


def test_fresh_engines_are_independent():
    first = engine()
    second = engine()
    first.analyze(runup())
    assert first.get_all()
    assert second.get_all() == []


def test_engine_uses_the_injected_config():
    eg = engine(range_bars=3)
    # Three reference bars land on indices 0..2, which are all flat - a
    # degenerate window, so `analyze` bails out before any state is created.
    candles = [flat(0, 1.0800), flat(1, 1.0800), flat(2, 1.0800), rng(3), rng(4)]
    assert eg.config.range_bars == 3
    assert eg.analyze(candles) == []
