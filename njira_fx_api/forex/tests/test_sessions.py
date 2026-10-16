"""
Intraday market-session timing unit tests.

Fixed UTC timestamps — no dependence on the machine clock or on a live feed.

Run: python manage.py test forex.tests.test_sessions
"""
from __future__ import annotations

from datetime import datetime, timezone

from django.test import SimpleTestCase

from forex.analysis.sessions import (
    in_trade_session,
    next_session_open,
    session_of,
    session_open,
)


def _ts(*args: int) -> float:
    """Epoch seconds for a fixed UTC datetime."""
    return datetime(*args, tzinfo=timezone.utc).timestamp()


# 2026-08-03 is a Monday; the reference day used across these tests.
T = lambda h, m=0: _ts(2026, 8, 3, h, m)  # noqa: E731


class SessionOfTests(SimpleTestCase):
    def test_session_of_tokyo_morning(self):
        """08:00 UTC overlaps tokyo (00–09) and london (07–16)."""
        self.assertEqual(session_of(T(8)), ["tokyo", "london"])

    def test_session_of_sydney_late(self):
        """23:00 UTC is sydney only (sydney 21:00–06:00 wraps midnight)."""
        self.assertEqual(session_of(T(23)), ["sydney"])

    def test_session_of_gap(self):
        """
        06:30 UTC — between sydney (ends 06:00) and london (starts 07:00) there
        is no OVERLAP, but tokyo (00:00–09:00) is still running, so the session
        list is not empty. The four session windows tile the full 24h.
        """
        self.assertEqual(session_of(T(6, 30)), ["tokyo"])
        self.assertFalse({"sydney", "london"} & set(session_of(T(6, 30))))

    def test_session_of_asia_overlap(self):
        """02:00 UTC is the sydney+tokyo overlap."""
        self.assertEqual(session_of(T(2)), ["sydney", "tokyo"])

    def test_session_of_london_ny_overlap(self):
        """14:00 UTC is the london+newyork peak-liquidity overlap."""
        self.assertEqual(session_of(T(14)), ["london", "newyork"])


class InTradeSessionTests(SimpleTestCase):
    def test_in_trade_session_default(self):
        """Default allowed = tokyo + sydney (AUDJPY home sessions)."""
        self.assertTrue(in_trade_session(T(8)))
        self.assertTrue(in_trade_session(T(2)))
        self.assertFalse(in_trade_session(T(14)))

    def test_in_trade_session_london_ny(self):
        """XAUUSD users pass the london/newyork pair."""
        self.assertTrue(in_trade_session(T(14), allowed=("london", "newyork")))
        # 08:00 is london-only, but 23:00 is neither london nor newyork.
        self.assertFalse(in_trade_session(T(23), allowed=("london", "newyork")))


class SessionOpenTests(SimpleTestCase):
    def test_session_open_london(self):
        """london opens 07:00 UTC; 3600s window covers 07:00–08:00 only."""
        self.assertTrue(session_open(T(7, 30), "london"))
        self.assertTrue(session_open(T(7), "london"))
        self.assertFalse(session_open(T(8), "london"))
        self.assertFalse(session_open(T(9), "london"))

    def test_session_open_wrapping_session(self):
        """sydney opens 21:00 UTC and wraps past midnight; only 21:00–22:00 counts."""
        self.assertTrue(session_open(T(21, 30), "sydney"))
        # 02:00 UTC is 5h past the 21:00 open — outside the 1h window.
        self.assertFalse(session_open(T(2), "sydney"))
        self.assertFalse(session_open(T(7), "sydney"))

    def test_session_open_custom_window(self):
        """tokyo opens 00:00 — a 10h window reaches exactly to 10:00 (exclusive)."""
        self.assertTrue(session_open(T(9, 59), "tokyo", window_seconds=36000))
        self.assertFalse(session_open(T(10), "tokyo", window_seconds=36000))
        self.assertFalse(session_open(T(10), "tokyo", window_seconds=1800))


class NextSessionOpenTests(SimpleTestCase):
    def test_next_session_open_rollover(self):
        """23:00 UTC → tokyo's next open is the following day 00:00 UTC."""
        self.assertEqual(next_session_open(T(23), "tokyo"), T(0) + 86400)

    def test_next_session_open_same_day(self):
        """03:00 UTC → tokyo opens later the same day at 00:00… already passed,
        so roll to tomorrow only when the open is ahead; tokyo at 03:00 is past,
        hence check a session that has not opened yet."""
        # london opens 07:00 — from 03:00 that is the same day.
        self.assertEqual(next_session_open(T(3), "london"), T(7))
        # tokyo (00:00) has already opened today → tomorrow.
        self.assertEqual(next_session_open(T(3), "tokyo"), T(0) + 86400)

    def test_next_session_open_wrapping_session(self):
        """sydney opens 21:00 — from 23:00 it rolls to tomorrow."""
        self.assertEqual(next_session_open(T(23), "sydney"), T(21) + 86400)
        self.assertEqual(next_session_open(T(2), "sydney"), T(21))
