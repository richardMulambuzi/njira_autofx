"""Per-signal Markdown logger (winrate plan Phase 1).

Every draft that survives the gates gets a full .md record; the outcome
(WIN/LOSS/CANCELLED) is appended later. Never raises — a logging failure
must not break the signal pipeline (caller also guards).
"""
from __future__ import annotations

import datetime
import logging
import os
import pathlib
from typing import Any

logger = logging.getLogger("forex.analysis.signal_logger")

SIGNAL_LOG_DIR = os.environ.get("SIGNAL_LOG_DIR", "signals")


def _ev(draft: Any) -> dict:
    return (getattr(draft, "evidence", None) or {}) if draft is not None else {}


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.5f}" if abs(v) > 100 else f"{v:.2f}"
    if isinstance(v, (list, tuple)):
        return "/".join(str(x) for x in v) if v else "—"
    return str(v)


def log_signal_to_md(
    symbol: str,
    draft: Any,
    quality_report: Any | None,
    confluence_score: int,
    confluence_breakdown: dict | None,
    confluence_required: int,
    session: str,
    htf_bias: str,
    signal_id: str,
) -> pathlib.Path:
    now = datetime.datetime.now(datetime.timezone.utc)
    folder = pathlib.Path(SIGNAL_LOG_DIR) / now.strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)
    direction = getattr(draft, "direction", "UNK") or "UNK"
    fname = f"{symbol}_{direction}_{now.strftime('%Y%m%d_%H%M%S')}_{str(signal_id or 'nosig')[:8]}.md"
    path = folder / fname

    ev = _ev(draft)
    rr = float(getattr(draft, "risk_reward", 0.0) or 0.0)
    rej = ev.get("snr_rejection") if isinstance(ev.get("snr_rejection"), dict) else {}

    def _pick(*keys: str) -> Any:
        for k in keys:
            if k in ev and ev[k] not in (None, ""):
                return ev[k]
        for k in keys:
            if k in rej and rej[k] not in (None, ""):
                return rej[k]
        return "—"

    try:
        from forex.analysis.quality_gate import QUESTIONS as _Q
    except Exception:  # noqa: BLE001 — logger must never break imports
        _Q = {i: f"Q{i}" for i in range(1, 16)}

    lines = [
        f"# Signal: {symbol} {direction} | {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "**Status:** PENDING",
        "",
        "## Entry",
        "| Field | Value |",
        "|---|---|",
        f"| Entry | {_fmt(getattr(draft, 'entry', None))} |",
        f"| Stop Loss | {_fmt(getattr(draft, 'stop_loss', None))} |",
        f"| Take Profit | {_fmt(getattr(draft, 'take_profit', None))} |",
        f"| Risk:Reward | {rr:.2f} |",
        f"| Confidence | {_fmt(getattr(draft, 'confidence', None))} |",
        "",
        "## Analyzer Evidence",
        "| Key | Value |",
        "|---|---|",
        f"| analyzer | {_fmt(ev.get('analyzer'))} |",
        f"| snr_fresh | {_fmt(_pick('snr_fresh', 'fresh'))} |",
        f"| snr_clean | {_fmt(_pick('snr_clean', 'clean'))} |",
        f"| snr_validation | {_fmt(_pick('snr_validation', 'validation'))} |",
        f"| snr_rule3_ok | {_fmt(_pick('snr_rule3_ok', 'rule3_ok'))} |",
        f"| snr_body_first | {_fmt(_pick('snr_body_first', 'body_first'))} |",
        f"| in_kill_zone | {_fmt(ev.get('in_kill_zone', ev.get('killzone', '—')))} |",
        f"| session | {_fmt(session or ev.get('session', '—'))} |",
        f"| htf_bias | {_fmt(htf_bias)} |",
        f"| confluence_score | {confluence_score}/{confluence_required} |",
        "",
        "## Quality Gate (15 Questions)",
        "| Q | Question | Answer | Pass |",
        "|---|---|---|---|",
    ]
    answers = getattr(quality_report, "answers", None) if quality_report is not None else None
    if answers:
        for a in answers:
            qid = getattr(a, "question_id", "?")
            q = getattr(a, "question", _Q.get(qid, f"Q{qid}"))
            tick = "✅" if getattr(a, "passed", False) else "❌"
            lines.append(f"| {qid} | {q} | {getattr(a, 'answer', '—')} | {tick} |")
    else:
        lines.append("| — | not evaluated (no quality gate report) | — | — |")
    lines += [
        "",
        "## Confluence Score",
        "| Factor | Points |",
        "|---|---|",
    ]
    for k, v in (confluence_breakdown or {}).items():
        lines.append(f"| {k} | {v} |")
    lines.append(f"| **Total** | **{confluence_score}** |")
    gate = "PASSED" if confluence_score >= confluence_required else "FAILED"
    lines += ["", f"**Confluence Gate:** {gate} (≥ {confluence_required} required)", "", "## Notes", "_Add manual observations here_", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def update_signal_outcome(path: pathlib.Path | str, outcome: str, pnl_r: float) -> None:
    """Append WIN/LOSS/CANCELLED outcome to an existing signal file."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## Outcome\n**Result:** {outcome} | **P&L:** {float(pnl_r):+.2f}R\n")
    except Exception as exc:  # noqa: BLE001 — logging must never break trading
        logger.warning("signal_logger: outcome update failed for %s: %s", path, exc)
