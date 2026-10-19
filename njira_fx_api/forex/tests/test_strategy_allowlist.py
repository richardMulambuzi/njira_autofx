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


def _registered_ict() -> list[str]:
    import forex.analysis.ict_strategies  # noqa: F401  (registers the combos)
    from forex.analysis.signal_engine import StrategyRegistry

    return sorted(n for n in StrategyRegistry._registry if n.startswith("ict_"))


class IctReachabilityTests(SimpleTestCase):
    """Every registered ict_* combo must be selectable and reach evaluate()."""

    def test_all_registered_ict_strategies_are_allowlisted(self):
        names = _registered_ict()
        self.assertEqual(len(names), 14)
        self.assertEqual(_allowlist(NJIRA_ICT_STRATEGIES=",".join(names)), BASE + tuple(names))

    def test_with_ict_puts_every_combo_in_the_live_mode_string(self):
        from forex.pepperstone.ticks_engine import TicksEngine

        names = _registered_ict()
        eng = TicksEngine(symbols=["EURUSD"])
        clean = {k: v for k, v in os.environ.items() if k != "NJIRA_ICT_STRATEGIES"}
        clean["NJIRA_ICT_STRATEGIES"] = ",".join(names)
        with patch.dict(os.environ, clean, clear=True):
            modes = eng._with_ict("malysian").split(",")
        self.assertEqual(modes, ["malysian"] + names)

    def test_with_ict_expands_both(self):
        from forex.pepperstone.ticks_engine import TicksEngine

        eng = TicksEngine(symbols=["EURUSD"])
        clean = {k: v for k, v in os.environ.items() if k != "NJIRA_ICT_STRATEGIES"}
        clean["NJIRA_ICT_STRATEGIES"] = "ict_ote"
        with patch.dict(os.environ, clean, clear=True):
            modes = eng._with_ict("both").split(",")
        self.assertEqual(modes, ["malysian", "Moving_averages", "ict_ote"])
