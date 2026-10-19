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

from forex.analysis.tp_ladder import ladder_summary

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


def _utc_str(epoch: Any) -> str:
    """Format an epoch-seconds stamp as UTC, or a dash when absent."""
    try:
        dt = datetime.datetime.fromtimestamp(float(epoch), tz=datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# Plain (not f-string) so the CSS braces stay literal.  %-placeholders only.
_EMAIL_TEMPLATE = """<!doctype html>
<html><body style="margin:0;padding:0;background:#0e1116;">
<div style="max-width:640px;margin:0 auto;padding:24px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
            color:#e8ecf1;">

  <div style="background:%(accent)s;border-radius:14px 14px 0 0;padding:20px 22px;">
    <div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;
                opacity:.85;">njira signal</div>
    <div style="font-size:30px;font-weight:700;margin-top:6px;line-height:1.1;">
      %(symbol)s %(direction)s
    </div>
    <div style="font-size:14px;opacity:.9;margin-top:6px;">%(status)s</div>
  </div>

  <div style="background:#171c24;padding:20px 22px;">
    <div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;
                color:#8b98a9;">Where to enter</div>
    <div style="font-size:26px;font-weight:700;margin:8px 0 2px;">%(entry_time)s</div>
    <div style="font-size:13px;color:#8b98a9;">%(entry_note)s</div>
    <table style="width:100%%;border-collapse:collapse;margin-top:16px;font-size:14px;">
      %(entry_rows)s
    </table>
  </div>

  <div style="background:#141920;padding:20px 22px;">
    <div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;
                color:#8b98a9;">Take profit ladder</div>
    <table style="width:100%%;border-collapse:collapse;margin-top:12px;font-size:13px;">
      <tr style="color:#8b98a9;text-align:left;">
        <th style="padding:6px 4px;">Level</th>
        <th style="padding:6px 4px;">Price</th>
        <th style="padding:6px 4px;">Chance</th>
        <th style="padding:6px 4px;">Risk</th>
      </tr>
      %(ladder_rows)s
    </table>
    <div style="font-size:13px;color:#aab6c5;margin-top:14px;
                border-left:3px solid %(accent)s;padding-left:12px;">
      %(ladder_summary)s
    </div>
    %(ladder_notes)s
  </div>

  <div style="background:#11151b;padding:18px 22px;font-size:13px;color:#8b98a9;">
    <div style="font-size:12px;letter-spacing:2px;text-transform:uppercase;
                margin-bottom:8px;">The setup</div>
    <table style="width:100%%;border-collapse:collapse;">%(evidence_rows)s</table>
    <div style="margin-top:14px;">Confluence %(confluence)s. %(gate)s</div>
  </div>

  <div style="background:#0e1116;padding:14px 22px;font-size:11px;color:#5d6b7c;">
    Generated %(generated)s by the njira engine. A signal is a setup, not a
    promise: the stop loss is where the idea is wrong.
  </div>
</div>
</body></html>
"""


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

    entry_time = getattr(draft, "entry_time", None)
    ladder = ev.get("tp_ladder") if isinstance(ev.get("tp_ladder"), list) else []

    lines = [
        f"# Signal: {symbol} {direction} | {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "**Status:** PENDING",
        "",
        "## When To Enter",
        "| Field | Value |",
        "|---|---|",
        f"| Entry time | {_utc_str(entry_time)} |",
        f"| Valid until | {_utc_str(ev.get('expiry_time'))} |",
        f"| Entry | {_fmt(getattr(draft, 'entry', None))} |",
        f"| Stop Loss | {_fmt(getattr(draft, 'stop_loss', None))} |",
        f"| Risk:Reward | {rr:.2f} |",
        f"| Confidence | {_fmt(getattr(draft, 'confidence', None))} |",
        "",
        "## Take Profit Ladder",
        "| Level | Price | R | Distance | Chance before SL | Risk | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    if ladder:
        for rung in ladder:
            lines.append(
                f"| {rung.get('name')} | {rung.get('price')} | "
                f"{float(rung.get('r_multiple') or 0):.1f}R | "
                f"{float(rung.get('distance_pips') or 0):.1f} pips | "
                f"{float(rung.get('hit_chance') or 0) * 100:.0f}% | "
                f"{rung.get('label', '')} | {rung.get('note', '')} |"
            )
        lines += ["", ladder_summary(ladder)]
    else:
        lines.append("| - | - | - | - | - | - | ladder unavailable |")
        lines.append(f"Engine target: {_fmt(getattr(draft, 'take_profit', None))}")
    lines += [
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

    # Companion .html the mailer renders instead of reading the raw markup.
    # Everything the reader needs first (entry time, ladder) sits at the top;
    # the audit tables sink to the bottom.
    html_path = path.with_suffix(".html")
    try:
        accent = "#1f8f4e" if direction == "BUY" else "#b5382f"
        buy = direction == "BUY"

        def _row(label: str, value: str) -> str:
            return (
                f'<tr><td style="padding:5px 4px;color:#8b98a9;">{label}</td>'
                f'<td style="padding:5px 4px;text-align:right;">{value}</td></tr>'
            )

        def _ladder_row(rung: dict) -> str:
            edge = "border-top:1px solid #1f2730;"
            return (
                f'<tr><td style="padding:7px 4px;{edge}"><b>{rung.get("name")}</b></td>'
                f'<td style="padding:7px 4px;{edge}">{rung.get("price")}</td>'
                f'<td style="padding:7px 4px;{edge}">'
                f'{float(rung.get("hit_chance") or 0) * 100:.0f}%</td>'
                f'<td style="padding:7px 4px;{edge}color:#8b98a9;">'
                f'{rung.get("label", "")}</td></tr>'
            )

        best = ladder[0] if ladder else None
        balance = ladder[2] if len(ladder) > 2 else (ladder[-1] if ladder else None)
        notes = "".join(
            f'<div style="margin-top:6px;">'
            f'<b style="color:#e8ecf1;">{r.get("name")}</b> '
            f'<span style="color:#8b98a9;">{r.get("note", "")}</span></div>'
            for r in ladder
        )
        html = _EMAIL_TEMPLATE % {
            "accent": accent,
            "symbol": symbol,
            "direction": direction,
            "status": "PENDING (confirmation stands)",
            "entry_time": _utc_str(entry_time),
            "entry_note": (
                f"Enter at the {('buy' if buy else 'sell')} price below when this "
                f"bar opens. Valid until {_utc_str(ev.get('expiry_time'))}."
            ),
            "entry_rows": (
                _row("Entry price", _fmt(getattr(draft, "entry", None)))
                + _row("Stop loss", _fmt(getattr(draft, "stop_loss", None)))
                + _row("Risk : reward", f"{rr:.2f}")
                + _row("Confidence", _fmt(getattr(draft, "confidence", None)))
                + (
                    _row("Safest target", f'{best.get("name")} at {best.get("price")}')
                    if best else ""
                )
                + (
                    _row("Balanced target", f'{balance.get("name")} at {balance.get("price")}')
                    if balance else ""
                )
            ),
            "ladder_rows": "".join(_ladder_row(r) for r in ladder)
            or '<tr><td colspan="4" style="padding:10px 4px;color:#8b98a9;">'
               "Ladder unavailable for this setup.</td></tr>",
            "ladder_summary": ladder_summary(ladder) or "Ladder unavailable.",
            "ladder_notes": notes,
            "evidence_rows": (
                _row("Analyzer", _fmt(ev.get("analyzer")))
                + _row("Fresh level", _fmt(_pick("snr_fresh", "fresh")))
                + _row("Validation", _fmt(_pick("snr_validation", "validation")))
                + _row("Session", _fmt(session or ev.get("session", "-")))
                + _row("Higher timeframe", _fmt(htf_bias))
            ),
            "confluence": f"{confluence_score}/{confluence_required}",
            "gate": f"Confluence gate {gate}.",
            "generated": now.strftime("%Y-%m-%d %H:%M UTC"),
        }
        html_path.write_text(html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001: markup must never block the .md
        logger.warning("signal_logger: html render failed for %s: %s", symbol, exc)
    return path


def update_signal_outcome(path: pathlib.Path | str, outcome: str, pnl_r: float) -> None:
    """Append WIN/LOSS/CANCELLED outcome to an existing signal file."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## Outcome\n**Result:** {outcome} | **P&L:** {float(pnl_r):+.2f}R\n")
    except Exception as exc:  # noqa: BLE001 — logging must never break trading
        logger.warning("signal_logger: outcome update failed for %s: %s", path, exc)
