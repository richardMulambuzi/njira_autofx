"""Tests for the inducement (IDM) detector."""
from __future__ import annotations

import json
from dataclasses import replace

from forex.analysis.inducement import (
    CONCEPT_NAME,
    DEFAULT_INDUCEMENT_CONFIG,
    InducementConfig,
    InducementEngine,
    InducementEvent,
    InducementState,
    close_of,
    high_of,
    latest_inducement,
    low_of,
    open_of,
    swing_highs,
    swing_lows,
)

T0 = 1_700_000_000
TF = 900


def _bar(o, h, l, c, t=T0):
    return {"open": o, "high": h, "low": l, "close": c, "open_time": t, "timeframe": TF}


def _base():
    return [
        _bar(1.1000, 1.1010, 1.1000, 1.1005),
        _bar(1.1005, 1.1010, 1.0970, 1.1000),
        _bar(1.1000, 1.1012, 1.1008, 1.1010),
        _bar(1.1010, 1.1010, 1.0990, 1.0995),
        _bar(1.0995, 1.1000, 1.0985, 1.0990),
        _bar(1.0990, 1.1005, 1.0990, 1.1002),
        _bar(1.1002, 1.1008, 1.0995, 1.0998),
        _bar(1.0998, 1.1000, 1.0980, 1.0985),
    ]


def _pending():
    return _base()[:5] + [_bar(1.0990, 1.0990, 1.0988, 1.0989)]


def _ramp(n=8):
    """Strictly rising series: no bar is a swing, at any lookback."""
    out = []
    for i in range(n):
        o = 1.1000 + i * 0.002
        out.append(_bar(o, o + 0.0010, o - 0.0010, o + 0.0005))
    return out


def _analyse(candles, config=DEFAULT_INDUCEMENT_CONFIG, symbol="EURUSD"):
    engine = InducementEngine(config=config)
    engine.analyze(candles, tf=TF, symbol=symbol)
    return engine


def test_concept_name_and_sources():
    assert CONCEPT_NAME == "inducement"


def test_config_is_frozen():
    cfg = InducementConfig()
    try:
        cfg.pivot_lookback = 9
    except Exception as exc:
        assert exc.__class__.__name__ == "FrozenInstanceError"
    else:
        raise AssertionError("config should be frozen")


def test_default_config_values():
    cfg = DEFAULT_INDUCEMENT_CONFIG
    assert cfg.pivot_lookback == 3
    assert cfg.fallback_pivot_lookback == 1
    assert cfg.require_major_pool is True
    assert cfg.sweep_tolerance_ticks == 1.0
    assert cfg.hold_recovery_pips == 10.0
    assert cfg.min_gap_ticks == 5.0
    assert cfg.max_age_bars == 40
    assert cfg.pip_size == 0.0001


def test_candle_accessors():
    candle = {"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5}
    assert open_of(candle) == 1.0
    assert high_of(candle) == 2.0
    assert low_of(candle) == 0.5
    assert close_of(candle) == 1.5


def test_accessors_default_to_zero_on_garbage():
    for value in (None, 7, "nope", {}):
        assert open_of(value) == 0.0
        assert high_of(value) == 0.0
        assert low_of(value) == 0.0
        assert close_of(value) == 0.0


def test_swing_lows_finds_both_lows_at_pivot_one():
    lows = swing_lows(_base(), 1)
    assert [index for index, _ in lows] == [1, 4]
    assert lows[0][1] == 1.0970
    assert lows[1][1] == 1.0985


def test_swing_highs_finds_confirmed_highs_at_pivot_one():
    highs = swing_highs(_base(), 1)
    assert [index for index, _ in highs] == [2, 6]


def test_swing_helpers_return_nothing_when_window_is_too_wide():
    assert swing_lows(_base()[:6], 3) == []
    assert swing_highs(_base()[:6], 3) == []


def test_swing_helpers_tolerate_non_positive_pivot():
    assert swing_lows(_base(), 0) == swing_lows(_base(), 1)
    assert swing_highs(_base(), 0) == swing_highs(_base(), 1)


def test_latest_inducement_picks_the_bullish_minor_low():
    found = latest_inducement(_base(), DEFAULT_INDUCEMENT_CONFIG)
    assert found is not None
    assert found["direction"] == "BULLISH"
    assert found["minor_pool"] == 1.0985
    assert found["major_pool"] == 1.0970
    assert found["has_major_pool"] is True
    assert found["swing_index"] == 4


def test_latest_inducement_prefers_the_pool_nearest_price():
    found = latest_inducement(_base(), DEFAULT_INDUCEMENT_CONFIG)
    assert found["swing_index"] == 4


def test_require_major_pool_false_lets_the_bearish_nearest_swing_win():
    cfg = replace(DEFAULT_INDUCEMENT_CONFIG, require_major_pool=False)
    found = latest_inducement(_base(), cfg)
    assert found["direction"] == "BEARISH"
    assert found["minor_pool"] == 1.1008
    assert found["has_major_pool"] is False
    assert found["major_pool"] == 0.0


def test_major_pool_gate_skips_a_minor_high_with_nothing_above_it():
    found = latest_inducement(_base(), DEFAULT_INDUCEMENT_CONFIG)
    assert found["direction"] == "BULLISH"


def test_latest_inducement_returns_none_without_swings():
    assert latest_inducement(_ramp(), DEFAULT_INDUCEMENT_CONFIG) is None


def test_fallback_lookback_rescues_a_short_series():
    found = latest_inducement(_pending(), DEFAULT_INDUCEMENT_CONFIG)
    assert found is not None
    assert found["direction"] == "BULLISH"
    assert found["minor_pool"] == 1.0985


def test_latest_inducement_ignores_pools_inside_the_min_gap():
    candles = _pending()
    cfg = replace(DEFAULT_INDUCEMENT_CONFIG, min_gap_ticks=500.0)
    found = latest_inducement(candles, cfg)
    assert found["has_major_pool"] is False


def test_engine_returns_nothing_below_three_candles():
    engine = InducementEngine()
    assert engine.analyze([_bar(1.1, 1.1, 1.1, 1.1), _bar(1.1, 1.1, 1.1, 1.1)], tf=TF) == []
    assert engine.get_active() == []
    assert engine.get_events() == []


def test_engine_returns_nothing_without_a_swing():
    assert InducementEngine().analyze(_ramp(), tf=TF, symbol="EURUSD") == []


def test_swept_reading_names_the_real_move_and_the_extremes():
    record = _analyse(_base()).get_active()[-1]
    assert record["direction"] == "BULLISH"
    assert record["state"] == "SWEPT"
    assert record["swept"] is True
    assert record["held"] is False
    assert record["minor_pool"] == 1.0985
    assert record["major_pool"] == 1.0970
    assert record["sweep_price"] == 1.0980
    assert record["age_bars"] == 3
    assert record["recovery_pips"] == 23.0
    assert record["minor_to_major_ticks"] == 15.0


def test_held_reading_when_price_recovers_without_taking_the_pool():
    record = _analyse(_base()[:7]).get_active()[-1]
    assert record["state"] == "HELD"
    assert record["held"] is True
    assert record["swept"] is False
    assert record["recovery_pips"] == 23.0


def test_identified_reading_while_the_pool_is_still_untouched():
    record = _analyse(_pending()).get_active()[-1]
    assert record["state"] == "IDENTIFIED"
    assert record["swept"] is False
    assert record["held"] is False
    assert record["recovery_pips"] == 5.0


def test_hold_knob_decides_between_held_and_identified():
    cfg = replace(DEFAULT_INDUCEMENT_CONFIG, hold_recovery_pips=100.0)
    record = _analyse(_base()[:7], config=cfg).get_active()[-1]
    assert record["state"] == "IDENTIFIED"
    assert record["held"] is False


def test_expired_reading_past_max_age():
    cfg = replace(DEFAULT_INDUCEMENT_CONFIG, max_age_bars=1, hold_recovery_pips=100.0)
    record = _analyse(_base()[:7], config=cfg).get_active()[-1]
    assert record["state"] == "EXPIRED"
    assert record["age_bars"] == 2


def test_swept_takes_priority_over_held():
    record = _analyse(_base()).get_active()[-1]
    assert record["recovery_pips"] > DEFAULT_INDUCEMENT_CONFIG.hold_recovery_pips
    assert record["state"] == "SWEPT"


def test_first_analyse_emits_identified_then_swept():
    engine = InducementEngine()
    events = engine.analyze(_base(), tf=TF, symbol="EURUSD")
    names = [event["event"] for event in events]
    assert names == ["IDM_IDENTIFIED", "IDM_SWEPT"]


def test_held_emits_identified_then_held():
    events = InducementEngine().analyze(_base()[:7], tf=TF, symbol="EURUSD")
    assert [event["event"] for event in events] == ["IDM_IDENTIFIED", "IDM_HELD"]


def test_identified_alone_when_nothing_has_happened_yet():
    events = InducementEngine().analyze(_pending(), tf=TF, symbol="EURUSD")
    assert [event["event"] for event in events] == ["IDM_IDENTIFIED"]


def test_expiry_is_reported_once_as_an_event():
    cfg = replace(DEFAULT_INDUCEMENT_CONFIG, max_age_bars=1, hold_recovery_pips=100.0)
    engine = InducementEngine(config=cfg)
    engine.analyze(_base()[:6], tf=TF, symbol="EURUSD")
    events = engine.analyze(_base()[:7], tf=TF, symbol="EURUSD")
    assert [event["event"] for event in events] == ["IDM_EXPIRED"]


def test_repeat_call_on_the_same_candles_is_silent():
    engine = InducementEngine()
    engine.analyze(_base(), tf=TF, symbol="EURUSD")
    assert engine.analyze(_base(), tf=TF, symbol="EURUSD") == []


def test_every_event_carries_the_idm_prefix():
    engine = _analyse(_base())
    for event in engine.get_events():
        assert event["event"].startswith("IDM_")


def test_state_and_event_enum_values_are_prefixed():
    assert InducementState.SWEPT.value == "SWEPT"
    assert InducementEvent.SWEPT.value == "IDM_SWEPT"
    assert InducementEvent.IDENTIFIED.value == "IDM_IDENTIFIED"
    assert InducementEvent.HELD.value == "IDM_HELD"
    assert InducementEvent.EXPIRED.value == "IDM_EXPIRED"


def test_record_carries_symbol_and_timeframe():
    record = _analyse(_base(), symbol="GBPUSD").get_active()[-1]
    assert record["symbol"] == "GBPUSD"
    assert record["timeframe"] == TF


def test_timeframe_falls_back_to_the_candle_metadata():
    engine = InducementEngine()
    engine.analyze(_base(), symbol="EURUSD")
    assert engine.get_active()[-1]["timeframe"] == TF


def test_to_dict_drops_created_at_and_stays_json_safe():
    record = _analyse(_base()).get_active()[-1]
    assert "created_at" not in record
    assert json.loads(json.dumps(record, default=str))["state"] == "SWEPT"


def test_get_all_mirrors_get_active():
    engine = _analyse(_base())
    assert engine.get_all() == engine.get_active()


def test_get_events_respects_the_limit():
    engine = _analyse(_base())
    assert len(engine.get_events(n=1)) == 1


def test_reset_clears_state_and_events():
    engine = _analyse(_base())
    engine.reset()
    assert engine.get_active() == []
    assert engine.get_events() == []


def test_engine_keeps_one_record_per_symbol_and_timeframe():
    engine = InducementEngine()
    engine.analyze(_base(), tf=TF, symbol="EURUSD")
    engine.analyze(_base(), tf=TF, symbol="XAUUSD")
    assert {record["symbol"] for record in engine.get_active()} == {"EURUSD", "XAUUSD"}


def test_config_is_honoured_on_the_record():
    cfg = replace(DEFAULT_INDUCEMENT_CONFIG, pip_size=0.01, min_gap_ticks=0.0)
    record = _analyse(_base(), config=cfg).get_active()[-1]
    assert record["direction"] == "BEARISH"
    assert record["minor_pool"] == 1.1008
    assert record["major_pool"] == 1.1012
    assert record["minor_to_major_ticks"] == 0.04
