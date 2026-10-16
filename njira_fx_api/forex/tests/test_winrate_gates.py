"""Winrate plan gates: confluence scoring, hard drops, signal logger, HTF bias."""
from __future__ import annotations

import os
import pathlib
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from forex.analysis import confluence_gate as cg  # noqa: E402
from forex.analysis import signal_logger as sl  # noqa: E402


def _draft(**kw):
    base = {
        "direction": "BUY",
        "entry": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "confidence": 75.0,
        "risk_reward": 2.0,
        "evidence": {},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_strong_setup_scores_above_required():
    d = _draft(evidence={
        "analyzer": "marriage", "snr_fresh": 1.0, "snr_clean": True,
        "snr_validation": "engulfing", "snr_rule3_ok": True,
        "in_kill_zone": True, "session": ["london"],
    }, risk_reward=2.5)
    score, _ = cg.score_signal(d, True, "buy", "london")
    assert score >= cg.CONFLUENCE_REQUIRED


def test_counter_trend_penalised():
    ev = {"analyzer": "snr", "snr_fresh": 1.0, "snr_validation": "pin_bar",
          "snr_rule3_ok": True, "session": ["london"]}
    aligned, _ = cg.score_signal(_draft(evidence=ev, risk_reward=2.0), True, "buy", "london")
    against, bd = cg.score_signal(_draft(evidence=ev, risk_reward=2.0), True, "sell", "london")
    assert against == aligned - 5  # misses +2 aligned, takes -3 against
    assert bd["htf AGAINST"] == -3


def test_hard_drops():
    d = _draft(evidence={"analyzer": "snr"}, risk_reward=2.0)
    assert cg.check_hard_drop(d, "")[1] == "no_validation"  # snr-only rule
    assert cg.check_hard_drop(_draft(evidence={"analyzer": "marriage"}, risk_reward=2.0), "") == (False, "", "")  # scoped out
    d = _draft(evidence={"analyzer": "snr", "snr_validation": "engulfing"}, risk_reward=1.0)
    assert cg.check_hard_drop(d, "")[1] == "rr_too_low"
    d = _draft(evidence={"analyzer": "snr", "snr_validation": "pin_bar", "snr_body_first": True}, risk_reward=2.0)
    assert cg.check_hard_drop(d, "")[1] == "body_first_stale"
    d = _draft(evidence={"analyzer": "snr", "snr_validation": "engulfing", "snr_fresh": 1.0}, risk_reward=2.0)
    assert cg.check_hard_drop(d, "sell") == (False, "", "")  # BUY vs sell htf but fresh


def test_float_evidence_adapter():
    # Engine stores weights as floats + nested snr_rejection — must still score.
    d = _draft(evidence={
        "analyzer": "snr", "snr_fresh": 1.0, "snr_validation": 1.0,
        "snr_rejection": {"rule3_ok": True, "clean": True},
        "session": ["newyork"], "killzone": ["london_kz"],
    }, risk_reward=3.0)
    score, _ = cg.score_signal(d, True, "buy", "")
    assert score >= 6


def test_logger_roundtrip(tmp_path=None):
    tmp = tempfile.mkdtemp()
    sl.SIGNAL_LOG_DIR = tmp
    d = _draft(evidence={"analyzer": "marriage", "snr_fresh": True})
    path = sl.log_signal_to_md("XAUUSD", d, None, 8, {"fresh level": 2}, 6,
                               "london", "buy", "abcdef123456")
    text = pathlib.Path(path).read_text()
    assert "# Signal: XAUUSD BUY" in text and "confluence" in text.lower()
    sl.update_signal_outcome(path, "WIN", 2.0)
    assert "WIN" in pathlib.Path(path).read_text()


def test_htf_bias_neutral_without_history():
    from forex.analysis.cache import HotCache
    from forex.analysis.signal_engine import SignalEngine
    eng = SignalEngine(HotCache())
    assert eng._htf_bias("EURUSD") == ""
