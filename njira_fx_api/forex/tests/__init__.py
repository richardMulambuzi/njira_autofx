"""
Test-suite bootstrap.

The Malaysian SNR analyzer roster is feature-flagged by
``SNR_MALAYSIAN_ANALYZERS`` and SHIPS as marriage+snr+breakout, because cost-aware
measurement showed every other analyzer had negative expectancy. The analyzers
themselves are retained (not deleted), so the suite must still exercise them.

Rather than silently disabling that coverage, the suite explicitly opts every
analyzer back in here, and
:mod:`forex.tests.test_analyzer_roster` separately asserts what actually
ships. That way "the default is marriage+snr+breakout" is a tested fact rather than an
assumption, and no analyzer becomes untested dead code.
"""
from __future__ import annotations

import os

os.environ.setdefault("SNR_MALAYSIAN_ANALYZERS", "snr,engulfing,marriage,breakout,flip,qml,confluence")