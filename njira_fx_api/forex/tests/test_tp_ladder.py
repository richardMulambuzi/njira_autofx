"""Take-profit ladder maths and the signal card render."""
from __future__ import annotations

import types

import pytest

from forex.analysis import signal_logger
from forex.analysis.tp_ladder import (
    TP_LADDER_R,
    build_tp_ladder,
    digits_for,
    ladder_summary,
)


def test_ladder_has_seven_rungs_selling():
    ladder = build_tp_ladder("SELL", 7721.10, 7731.90)

    assert [r["name"] for r in ladder] == [f"TP{i}" for i in range(1, 8)]
    assert [r["r_multiple"] for r in ladder] == list(TP_LADDER_R)
    # Every rung sits below the entry on a sell, and further away each time.
    assert all(r["price"] < 7721.10 for r in ladder)
    assert [r["distance"] for r in ladder] == sorted(r["distance"] for r in ladder)


def test_ladder_mirrors_for_a_buy():
    ladder = build_tp_ladder("BUY", 7500.00, 7490.00)

    assert all(r["price"] > 7500.00 for r in ladder)
    assert ladder[0]["price"] == pytest.approx(7510.0)
    assert ladder[-1]["price"] == pytest.approx(7550.0)


def test_hit_chance_falls_as_the_target_stretches():
    ladder = build_tp_ladder("BUY", 7500.00, 7490.00)
    chances = [r["hit_chance"] for r in ladder]

    assert chances == sorted(chances, reverse=True)
    assert ladder[0]["hit_chance"] == pytest.approx(0.5)
    assert all(0.0 < c < 1.0 for c in chances)


def test_a_zero_risk_setup_has_no_ladder():
    assert build_tp_ladder("BUY", 7500.0, 7500.0) == []


def test_digits_follow_price_magnitude():
    assert digits_for(7721.10) == 2
    assert digits_for(150.5) == 3
    assert digits_for(1.0842) == 5


def test_summary_names_the_safest_and_widest_rungs():
    summary = ladder_summary(build_tp_ladder("SELL", 7721.10, 7731.90))

    assert "TP1" in summary and "TP7" in summary


def _draft():
    d = types.SimpleNamespace(
        direction="SELL", entry=7721.10, stop_loss=7731.90, take_profit=7704.90,
        risk_reward=1.50, confidence=50.0, entry_time=1776545700.0,
    )
    d.evidence = {
        "analyzer": "snr",
        "tp_ladder": build_tp_ladder("SELL", d.entry, d.stop_loss),
        "expiry_time": d.entry_time + 16 * 900,
    }
    return d


def test_card_leads_with_the_entry_time_and_the_ladder(tmp_path, monkeypatch):
    monkeypatch.setattr(signal_logger, "SIGNAL_LOG_DIR", str(tmp_path))

    path = signal_logger.log_signal_to_md(
        "US500", _draft(), None, 9, {"fresh level": 2}, 6, "london/newyork", "sell", "abcdef12",
    )

    card = path.read_text()
    assert "## When To Enter" in card
    assert "| Entry time |" in card
    assert "## Take Profit Ladder" in card
    assert all(f"| TP{i} " in card for i in range(1, 8))
    assert "Chance before SL" in card
    # The audit tables must not jump ahead of the time and the targets.
    assert card.index("Entry time") < card.index("Analyzer Evidence")


def test_card_writes_a_companion_html_for_the_email(tmp_path, monkeypatch):
    monkeypatch.setattr(signal_logger, "SIGNAL_LOG_DIR", str(tmp_path))

    path = signal_logger.log_signal_to_md(
        "US500", _draft(), None, 9, {"fresh level": 2}, 6, "london/newyork", "sell", "abcdef12",
    )
    html = path.with_suffix(".html")

    assert html.exists()
    body = html.read_text()
    assert "%(" not in body  # every template placeholder was filled
    assert "TP1" in body and "TP7" in body
    assert "when to enter" in body.lower() or "where to enter" in body.lower()


def test_ladder_pips_use_the_instrument_pip_size():
    """A pip belongs to the instrument: oil at 99 is not priced in FX pips."""
    ladder = build_tp_ladder("SELL", 99.172, 100.520, pip_size=0.01)

    assert ladder[0]["distance_pips"] == pytest.approx(134.8, abs=0.1)
