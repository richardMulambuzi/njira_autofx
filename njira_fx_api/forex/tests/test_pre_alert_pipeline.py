"""Tests for the pre-alert helpers: history re-anchoring, rung timing, projection."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from forex.pepperstone.ticks_engine import (
    _pre_alert_lead_min,
    _projection_map,
    _reanchor_history,
)

HOUR_MS = 3_600_000
STEP = 900 * 1000  # one M15 bar in ms

BASE = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def candle(open_time, hours=1.0):
    return {
        "open_time": open_time,
        "close_time": open_time + timedelta(seconds=900),
        "high": 100.0 + hours,
        "low": 100.0,
        "close": 100.0 + hours / 2,
    }


class ReanchorHistoryTests(SimpleTestCase):
    def test_shift_applied_when_broker_labels_lag(self):
        # Newest bar stamped 6 h behind the boundary containing now.
        now_ms = int(BASE.timestamp() * 1000)
        stale = [candle(BASE - timedelta(hours=7)), candle(BASE - timedelta(hours=6))]
        out, shift = _reanchor_history(stale, 900, now_ms)
        self.assertEqual(shift, 6 * HOUR_MS)
        self.assertEqual(out[0]["open_time"], BASE - timedelta(hours=1))
        self.assertEqual(out[1]["open_time"], BASE)
        self.assertEqual(out[1]["close_time"], BASE + timedelta(seconds=900))

    def test_no_shift_when_already_aligned(self):
        now_ms = int(BASE.timestamp() * 1000)
        aligned = [candle(BASE - timedelta(seconds=900)), candle(BASE)]
        out, shift = _reanchor_history(aligned, 900, now_ms)
        self.assertEqual(shift, 0)
        self.assertEqual(out[1]["open_time"], BASE)

    def test_no_shift_when_offset_is_not_a_whole_bar(self):
        now_ms = int(BASE.timestamp() * 1000) + 1234
        odd = [candle(BASE - timedelta(seconds=900)), candle(BASE)]
        out, shift = _reanchor_history(odd, 900, now_ms)
        self.assertEqual(shift, 0)
        self.assertEqual(out[1]["open_time"], BASE)

    def test_no_shift_when_gap_exceeds_a_day(self):
        # Whole-bar gap, but far too large to be a server-time label skew.
        now_ms = int(BASE.timestamp() * 1000)
        ancient = [candle(BASE - timedelta(days=300)), candle(BASE - timedelta(days=299))]
        out, shift = _reanchor_history(ancient, 900, now_ms)
        self.assertEqual(shift, 0)
        self.assertEqual(out[1]["open_time"], BASE - timedelta(days=299))


class PreAlertLeadTests(SimpleTestCase):
    def lead(self, minute):
        return _pre_alert_lead_min(datetime(2026, 10, 19, 12, minute, tzinfo=timezone.utc))

    def test_rungs(self):
        self.assertEqual(self.lead(55), 20)  # SC Scout previews the next bar
        self.assertEqual(self.lead(0), 15)
        self.assertEqual(self.lead(5), 10)
        self.assertEqual(self.lead(10), 5)
        self.assertEqual(self.lead(20), 10)
        self.assertEqual(self.lead(25), 5)
        self.assertEqual(self.lead(35), 10)
        self.assertEqual(self.lead(40), 5)
        self.assertEqual(self.lead(50), 10)

    def test_off_rung_returns_zero(self):
        for minute in (1, 7, 11, 23, 44, 58):
            self.assertEqual(self.lead(minute), 0)

    def test_missing_minute_returns_zero(self):
        self.assertEqual(_pre_alert_lead_min(None), 0)

    def test_every_rung_has_a_label(self):
        from forex.pepperstone.ticks_engine import _PRE_ALERT_LABEL

        for minute in range(60):
            lead = self.lead(minute)
            if lead:
                self.assertIn(lead, _PRE_ALERT_LABEL)


class ProjectionMapTests(SimpleTestCase):
    def draft(self, entry, tp, sl):
        return SimpleNamespace(entry=entry, take_profit=tp, stop_loss=sl)

    def flat(self, count=4, spread=30.0):
        return [
            {"high": 100.0 + spread, "low": 100.0, "close": 100.0 + spread / 2}
            for _ in range(count)
        ]

    def test_reachable_entry_named_as_reachable(self):
        # spread 30 -> speed 2.0/min; entry 10 away -> 5 min to fill.
        lines = _projection_map(self.draft(110.0, 130.0, 95.0), {"close": 100.0}, self.flat())
        self.assertIn("Typical travel: 2.0000 per minute", lines[0])
        self.assertTrue(any("entry reachable" in line for line in lines))
        self.assertTrue(any("Invalidation:   95.00000" in line for line in lines))

    def test_distant_entry_reported_as_short(self):
        lines = _projection_map(self.draft(300.0, 400.0, 95.0), {"close": 100.0}, self.flat())
        self.assertTrue(any("still short" in line for line in lines))

    def test_entry_already_touched(self):
        lines = _projection_map(self.draft(100.0, 130.0, 95.0), {"close": 100.0}, self.flat())
        self.assertTrue(any("already at the entry" in line for line in lines))

    def test_unavailable_without_candles(self):
        lines = _projection_map(self.draft(100.0, 130.0, 95.0), {}, [])
        self.assertEqual(lines, ["Projection unavailable: no recent 15M candles."])

    def test_unavailable_without_range(self):
        flat = [{"high": 100.0, "low": 100.0, "close": 100.0} for _ in range(4)]
        lines = _projection_map(self.draft(100.0, 130.0, 95.0), {"close": 100.0}, flat)
        self.assertEqual(lines, ["Projection unavailable: no recent 15M range."])
