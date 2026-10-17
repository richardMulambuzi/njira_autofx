"""
Strategy allowlist tests. Locks in env-gated ICT/SMC combo selection.

Locks in the two halves of the wiring fix: the base trio is always selectable
(prod behaviour unchanged) and ``ict_*`` registry names become selectable ONLY
when explicitly whitelisted via ``NJIRA_ICT_STRATEGIES``.

Run: python manage.py test forex.tests.test_strategy_allowlist
"""
from __future__ import annotations

import os
from unittest.mock import patch

from django.test import SimpleTestCase

BASE = ("Moving_averages", "malysian", "both")


def _allowlist(**env: str) -> tuple[str, ...]:
    from forex.pepperstone.ticks_engine import TicksEngine

    eng = TicksEngine(symbols=["EURUSD"])
    clean = {k: v for k, v in os.environ.items() if k != "NJIRA_ICT_STRATEGIES"}
    clean.update(env)
    with patch.dict(os.environ, clean, clear=True):
        return eng._strategy_allowlist()


class StrategyAllowlistTests(SimpleTestCase):
    def test_default_is_base_trio_only(self):
        self.assertEqual(_allowlist(), BASE)

    def test_blank_env_does_not_widen(self):
        self.assertEqual(_allowlist(NJIRA_ICT_STRATEGIES=" , ,"), BASE)

    def test_env_whitelists_ict_combos(self):
        self.assertEqual(
            _allowlist(NJIRA_ICT_STRATEGIES="ict_ote, ict_sweep_mss"),
            BASE + ("ict_ote", "ict_sweep_mss"),
        )
