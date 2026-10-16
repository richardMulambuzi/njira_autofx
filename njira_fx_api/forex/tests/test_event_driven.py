"""
Event-driven signal gating tests (improve.md §30-31).

Verifies that SignalEngine.evaluate() skips the expensive MTF
(Moving_averages) strategy on 1M/5M closes, and that the malysian SNR
strategy is ALSO skipped on 1M/5M closes (it needs 15M+ context — there is no
new 15M/4H bar to evaluate on a 1M/5M close, so running it is wasted compute).
``closed_tf=None`` preserves backward-compatible behavior (both strategies run).

Run: python manage.py test forex.tests.test_event_driven
"""
from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase

from forex.analysis.cache import HotCache
from forex.analysis.signal_engine import SignalEngine


def _make_engine() -> SignalEngine:
    """SignalEngine with no real sub-engines; market state is neutral."""
    eng = SignalEngine(cache=HotCache())
    eng._build_market_state = lambda symbol, direction, evidence: {}
    return eng


class EventDrivenGatingTests(SimpleTestCase):
    def test_mtf_skipped_on_1m_close(self):
        eng = _make_engine()
        eng._evaluate_mtf = MagicMock(return_value=[])
        eng._evaluate_snr = MagicMock(return_value=[])

        eng.evaluate("EURUSD", "both", closed_tf=60)

        eng._evaluate_mtf.assert_not_called()
        # SNR also skipped on 1M close — no new 15M/4H bar to evaluate.
        eng._evaluate_snr.assert_not_called()

    def test_mtf_skipped_on_5m_close(self):
        eng = _make_engine()
        eng._evaluate_mtf = MagicMock(return_value=[])
        eng._evaluate_snr = MagicMock(return_value=[])

        eng.evaluate("EURUSD", "both", closed_tf=300)

        eng._evaluate_mtf.assert_not_called()
        # SNR also skipped on 5M close — no new 15M/4H bar to evaluate.
        eng._evaluate_snr.assert_not_called()

    def test_mtf_runs_on_1h_close(self):
        eng = _make_engine()
        eng._evaluate_mtf = MagicMock(return_value=[])
        eng._evaluate_snr = MagicMock(return_value=[])

        eng.evaluate("EURUSD", "both", closed_tf=3600)

        eng._evaluate_mtf.assert_called_once()
        eng._evaluate_snr.assert_called_once()

    def test_mtf_runs_when_closed_tf_none(self):
        eng = _make_engine()
        eng._evaluate_mtf = MagicMock(return_value=[])
        eng._evaluate_snr = MagicMock(return_value=[])

        eng.evaluate("EURUSD", "both", closed_tf=None)

        eng._evaluate_mtf.assert_called_once()
        eng._evaluate_snr.assert_called_once()
