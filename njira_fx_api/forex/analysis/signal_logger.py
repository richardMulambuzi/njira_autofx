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
SIGNAL_MAIL_TO = os.environ.get("SIGNAL_MAIL_TO", "")


def _ev(draft: Any) -> dict:
    return (getattr(draft, "evidence", None) or {}) if draft is not None else {}


def _fmt(v: Any, places: int | None = None) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if places is not None:
            return f"{v:.{places}f}"
        # One rule for every instrument silently rounded FX quotes: 1.12168
        # became 1.12 and entry and stop loss printed identical.
        if abs(v) >= 1000:
            return f"{v:.2f}"
        if abs(v) >= 10:
            return f"{v:.3f}"
        return f"{v:.5f}"
    if isinstance(v, (list, tuple)):
        return "/".join(str(x) for x in v) if v else "—"
    return str(v)


CAT = datetime.timezone(datetime.timedelta(hours=2))


def _cat_str(epoch: Any) -> str:
    """Format an epoch-seconds stamp in Central Africa Time, or a dash when absent."""
    try:
        dt = datetime.datetime.fromtimestamp(float(epoch), tz=datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    return dt.astimezone(CAT).strftime("%Y-%m-%d %H:%M CAT")

def _cat_str_precise(epoch: Any) -> str:
    """CAT string to the second, for entry timing that has to be exact."""
    try:
        dt = datetime.datetime.fromtimestamp(float(epoch), tz=datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return "-"
    return dt.astimezone(CAT).strftime("%Y-%m-%d %H:%M:%S CAT")


# Plain (not f-string) so the CSS braces stay literal.  %-placeholders only.
_EMAIL_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#05070c;">
<div style="background-color:#05070c;background-image:radial-gradient(circle at 12%% 0%%,rgba(56,189,248,0.20),transparent 45%%),radial-gradient(circle at 88%% 6%%,rgba(168,85,247,0.20),transparent 42%%),radial-gradient(circle at 50%% 100%%,rgba(34,197,94,0.10),transparent 55%%);padding:34px 14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;">
  <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="max-width:620px;margin:0 auto;">
    <tr><td style="background:rgba(255,255,255,0.055);border:1px solid rgba(255,255,255,0.14);border-radius:26px;box-shadow:0 28px 70px -24px rgba(0,0,0,0.92),inset 0 1px 0 rgba(255,255,255,0.22);overflow:hidden;">

      <div style="height:3px;background:linear-gradient(90deg,%(accent)s,rgba(56,189,248,0.95),rgba(168,85,247,0.95));"></div>

      <div style="padding:26px 28px 20px;">
        <div style="font-size:10px;letter-spacing:3px;text-transform:uppercase;color:rgba(255,255,255,0.42);font-weight:700;">njira engine signal</div>
        <div style="margin-top:12px;font-size:34px;line-height:1;font-weight:800;color:#ffffff;letter-spacing:-1px;">%(symbol)s<span style="display:inline-block;vertical-align:middle;margin-left:10px;padding:5px 12px;border-radius:999px;font-size:11px;letter-spacing:1.6px;font-weight:800;color:#05070c;background:%(accent)s;">%(direction)s</span></div>
        <div style="margin-top:10px;font-size:12px;color:rgba(255,255,255,0.55);">%(status)s</div>
      </div>

      <div style="margin:0 18px 18px;padding:20px 22px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.09);border-radius:20px;">
        <div style="font-size:10px;letter-spacing:2.4px;text-transform:uppercase;color:rgba(255,255,255,0.42);font-weight:700;">entry window</div>
        <div style="margin-top:10px;font-size:29px;line-height:1.15;font-weight:800;color:#ffffff;letter-spacing:-0.6px;font-variant-numeric:tabular-nums;">%(entry_time)s</div>
<div style="font-size:10px;letter-spacing:2.4px;text-transform:uppercase;color:rgba(255,255,255,0.42);font-weight:700;">when to enter</div>
        <div style="margin-top:8px;font-size:12.5px;line-height:1.55;color:rgba(255,255,255,0.62);">%(entry_note)s</div>
        <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="margin-top:14px;border-collapse:collapse;">%(entry_rows)s</table>
      </div>

      <div style="margin:0 18px 18px;padding:20px 22px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.09);border-radius:20px;">
        <div style="font-size:10px;letter-spacing:2.4px;text-transform:uppercase;color:rgba(255,255,255,0.42);font-weight:700;">take profit ladder</div>
        <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px;border-collapse:collapse;">
          <tr>
            <th align="left" style="font-size:9.5px;letter-spacing:1.6px;text-transform:uppercase;color:rgba(255,255,255,0.38);font-weight:700;padding:0 6px 9px 0;border-bottom:1px solid rgba(255,255,255,0.10);">level</th>
            <th align="left" style="font-size:9.5px;letter-spacing:1.6px;text-transform:uppercase;color:rgba(255,255,255,0.38);font-weight:700;padding:0 6px 9px 0;border-bottom:1px solid rgba(255,255,255,0.10);">price</th>
            <th align="left" style="font-size:9.5px;letter-spacing:1.6px;text-transform:uppercase;color:rgba(255,255,255,0.38);font-weight:700;padding:0 6px 9px 0;border-bottom:1px solid rgba(255,255,255,0.10);">target</th>
            <th align="left" style="font-size:9.5px;letter-spacing:1.6px;text-transform:uppercase;color:rgba(255,255,255,0.38);font-weight:700;padding:0 6px 9px 0;border-bottom:1px solid rgba(255,255,255,0.10);">chance</th>
            <th align="left" style="font-size:9.5px;letter-spacing:1.6px;text-transform:uppercase;color:rgba(255,255,255,0.38);font-weight:700;padding:0 0 9px 0;border-bottom:1px solid rgba(255,255,255,0.10);">risk</th>
          </tr>
          %(ladder_rows)s
        </table>
        <div style="margin-top:16px;padding:13px 15px;background:rgba(255,255,255,0.03);border-left:3px solid %(accent)s;border-radius:10px;font-size:12.5px;line-height:1.6;color:rgba(255,255,255,0.70);">
          <div>%(ladder_summary)s</div>
          <div style="margin-top:8px;color:rgba(255,255,255,0.48);">%(ladder_notes)s</div>
        </div>
      </div>

      <div style="margin:0 18px 20px;padding:20px 22px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.09);border-radius:20px;">
        <div style="font-size:10px;letter-spacing:2.4px;text-transform:uppercase;color:rgba(255,255,255,0.42);font-weight:700;">the setup</div>
        <table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;border-collapse:collapse;">%(evidence_rows)s</table>
        <div style="margin-top:14px;font-size:12px;color:rgba(255,255,255,0.58);">Confluence <span style="color:#ffffff;font-weight:700;">%(confluence)s</span>. %(gate)s</div>
      </div>

      <div style="padding:16px 28px 24px;font-size:10.5px;line-height:1.7;color:rgba(255,255,255,0.34);border-top:1px solid rgba(255,255,255,0.07);">
        Generated %(generated)s by the njira engine. All times are Central Africa Time (UTC+2).<br>
        A signal is a setup, not a promise. The stop loss is where the idea is wrong.
      </div>

    </td></tr>
  </table>
</div>
</body></html>"""


def _mail_card(path: pathlib.Path, html: str) -> None:
    """Mail the finished card. A mail failure must never lose the card."""
    if not SIGNAL_MAIL_TO:
        logger.warning("signal_logger: SIGNAL_MAIL_TO unset, card %s not mailed", path.name)
        return
    try:
        from notifications.views import send_email
        body = path.read_text(encoding="utf-8")
        subject = body.splitlines()[0].lstrip("# ").strip()
        result = send_email(SIGNAL_MAIL_TO, subject, body, html=html or None)
        if not isinstance(result, dict) or not result.get("success"):
            logger.warning("signal_logger: mail failed for %s: %s", path.name, result)
    except Exception as exc:  # noqa: BLE001: mail must never block the card
        logger.warning("signal_logger: mail failed for %s: %s", path.name, exc)


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
    if entry_time is None:
        entry_time = ev.get("entry_time")
    if entry_time is None:
        # Confluence drafts carry no bar stamp. The card is written the moment
        # the setup confirms, so that instant is the entry, and a dash here
        # left the reader with no time to prepare.
        entry_time = now.timestamp()
    expiry_time = ev.get("expiry_time")
    if expiry_time is None:
        expiry_time = float(entry_time) + 30 * 60  # one M15 horizon
    ladder = ev.get("tp_ladder") if isinstance(ev.get("tp_ladder"), list) else []

    lines = [
        f"# Signal: {symbol} {direction} | {now.astimezone(CAT).strftime('%Y-%m-%d %H:%M CAT')}",
        "**Status:** PENDING",
        "",
        "## When To Enter",
        "| Field | Value |",
        "|---|---|",
        f"| Entry time | {_cat_str_precise(entry_time)} |",
        f"| Valid until | {_cat_str_precise(expiry_time)} |",
        f"| Entry | {_fmt(getattr(draft, 'entry', None))} |",
        f"| Stop Loss | {_fmt(getattr(draft, 'stop_loss', None))} |",
        f"| Risk:Reward | {rr:.2f} |",
        f"| Confidence | {_fmt(getattr(draft, 'confidence', None), places=2)} |",
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
    html_body = ""
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
            edge = "border-top:1px solid rgba(255,255,255,0.07);"
            return (
                f'<tr><td style="padding:7px 4px;{edge}"><b>{rung.get("name")}</b></td>'
                f'<td style="padding:7px 4px;{edge}font-variant-numeric:tabular-nums;">'
                f'{rung.get("price")}</td>'
                f'<td style="padding:7px 4px;{edge}color:rgba(255,255,255,0.72);'
                f'font-variant-numeric:tabular-nums;">'
                f'{float(rung.get("distance_pips") or 0):.1f} pips</td>'
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
            "entry_time": _cat_str_precise(entry_time),
            "entry_note": (
                f"Enter at the {('buy' if buy else 'sell')} price below when this "
                f"bar opens. Valid until {_cat_str_precise(expiry_time)}."
            ),
            "entry_rows": (
                _row("Entry price", _fmt(getattr(draft, "entry", None)))
                + _row("Stop loss", _fmt(getattr(draft, "stop_loss", None)))
                + _row("Risk : reward", f"{rr:.2f}")
                + _row("Confidence", _fmt(getattr(draft, "confidence", None), places=2))
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
            "generated": now.astimezone(CAT).strftime("%Y-%m-%d %H:%M CAT"),
        }
        html_path.write_text(html, encoding="utf-8")
        html_body = html
    except Exception as exc:  # noqa: BLE001: markup must never block the .md
        logger.warning("signal_logger: html render failed for %s: %s", symbol, exc)
    _mail_card(path, html_body)
    return path


def update_signal_outcome(path: pathlib.Path | str, outcome: str, pnl_r: float) -> None:
    """Append WIN/LOSS/CANCELLED outcome to an existing signal file."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## Outcome\n**Result:** {outcome} | **P&L:** {float(pnl_r):+.2f}R\n")
    except Exception as exc:  # noqa: BLE001 — logging must never break trading
        logger.warning("signal_logger: outcome update failed for %s: %s", path, exc)
