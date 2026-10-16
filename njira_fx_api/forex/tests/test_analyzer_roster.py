"""
The analyzer roster is a shipping decision, so it is a tested fact.

``forex.tests.__init__`` opts every analyzer back in so the rest of the suite
keeps covering the retained code. These tests bypass that and assert the value
that actually ships in production.
"""
from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase

from forex.analysis.signal_engine import (
    MALAYSIAN_ANALYZERS,
    MALAYSIAN_ENABLED_ANALYZERS_DEFAULT,
    enabled_malaysian_analyzers,
)


def _with_env(value, fn):
    with mock.patch.dict(os.environ, {"SNR_MALAYSIAN_ANALYZERS": value}, clear=False):
        return fn()


class RosterTests(SimpleTestCase):
    def test_ships_marriage_snr_breakout_by_default(self):
        got = _with_env("", enabled_malaysian_analyzers)
        self.assertEqual(got, frozenset({"marriage", "snr", "breakout"}))

    def test_default_constant_matches_helper(self):
        self.assertEqual(
            frozenset(MALAYSIAN_ENABLED_ANALYZERS_DEFAULT), frozenset({"marriage", "snr", "breakout"})
        )

    def test_env_override_enables_more(self):
        got = _with_env("marriage,snr", enabled_malaysian_analyzers)
        self.assertEqual(got, frozenset({"marriage", "snr"}))

    def test_env_override_can_re_enable_everything(self):
        got = _with_env("snr,engulfing,marriage,breakout,flip,qml,confluence",
                        enabled_malaysian_analyzers)
        self.assertEqual(got, frozenset(MALAYSIAN_ANALYZERS))

    def test_unknown_name_is_ignored(self):
        got = _with_env("marriage,not_a_real_analyzer", enabled_malaysian_analyzers)
        self.assertEqual(got, frozenset({"marriage"}))

    def test_all_invalid_falls_back_to_safe_default(self):
        # A typo must never disable trading entirely nor crash the engine.
        got = _with_env("garbage,nonsense", enabled_malaysian_analyzers)
        self.assertEqual(got, frozenset({"marriage", "snr", "breakout"}))

    def test_empty_value_falls_back_to_safe_default(self):
        self.assertEqual(_with_env("   ", enabled_malaysian_analyzers),
                         frozenset({"marriage", "snr", "breakout"}))

    def test_case_and_separators_are_tolerated(self):
        got = _with_env("MARRIAGE; SNR , engulfing", enabled_malaysian_analyzers)
        self.assertEqual(got, frozenset({"marriage", "snr", "engulfing"}))

    def test_returns_frozenset(self):
        # Callers filter on membership; an immutable set prevents accidental edits.
        self.assertIsInstance(enabled_malaysian_analyzers(), frozenset)