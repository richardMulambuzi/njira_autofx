"""
Backtest API views (DRF).

    POST /api/backtest/upload/         upload a forex CSV, run the engine, persist result
    POST /api/backtest/upload/stream/  same, but stream progress + result as Server-Sent Events
    GET  /api/backtest/results/        list past backtest runs (summary)
    GET  /api/backtest/results/<id>/   full detail (trades + execution steps)

Auth: JWT (IsAuthenticated) — inherited from the project default.

Pair auto-detect: the parser reads the ``ticker``/``symbol``/``pair``/
``instrument`` column.  A ``symbol`` request param overrides it for every
candle; a dataset with no symbol column REQUIRES that param (400 otherwise).
"""
from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from forex.backtest.engine import TIMEFRAMES, BacktestEngine
from forex.backtest.parser import (
    has_symbol_column,
    infer_timeframe,
    parse_csv,
    parse_tf_csv,
)
from forex.models import BacktestResult as BacktestResultModel

logger = logging.getLogger("forex.backtest.views")

NO_PAIR_MESSAGE = "Could not detect pair from dataset — please provide a symbol"

# Where timestamped signal reports are written so the user can backtest/verify
# every engine run and finetune.  Defaults to <repo>/signals/ — override with
# BACKTEST_SIGNALS_DIR.
SIGNALS_DIR = Path(
    os.environ.get("BACKTEST_SIGNALS_DIR", str(Path(__file__).resolve().parent.parent.parent / "signals"))
)


def _write_signals_md(symbol: str, result: Any, payload: dict, csv_name: str) -> str:
    """Write every signal/trade from a backtest run to a timestamped markdown file.

    Returns the absolute path written (or "" if there were no trades).  The
    filename is ``<UTC timestamp>signals.md`` so each engine run produces a
    distinct, sortable report the user can diff while finetuning.
    """
    trades = payload.get("trades") or []
    if not trades:
        return ""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    path = SIGNALS_DIR / f"{ts}signals.md"

    lines: list[str] = []
    lines.append(f"# {symbol} — Backtest Signals")
    lines.append("")
    lines.append(f"- **Run time (UTC):** {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- **CSV:** {csv_name or '(uploaded)'}")
    lines.append(f"- **Trades:** {len(trades)}")
    lines.append(f"- **Overall win rate:** {result.overall_win_rate:.1f}%")
    lines.append(f"- **Best strategy:** {result.best_strategy or '—'}")
    lines.append(f"- **Final balance:** {result.final_balance:.2f}")
    lines.append(f"- **Total PnL:** {result.total_pnl:.2f}")
    lines.append("")
    lines.append("## Per-strategy")
    lines.append("")
    lines.append("| Strategy | Trades | Win rate | PnL | Passes 70% gate |")
    lines.append("|---|---|---|---|---|")
    for s in result.per_strategy:
        lines.append(
            f"| {s.strategy} | {s.total_trades} | {s.win_rate:.1f}% | "
            f"{s.total_pnl:.2f} | {'✅' if s.passes_70_gate else '❌'} |"
        )
    lines.append("")
    lines.append("## Signals")
    lines.append("")
    for i, t in enumerate(trades, 1):
        lines.append(f"### Signal {i} — {t.get('direction')} ({t.get('strength')})")
        lines.append("")
        lines.append(f"- **Strategy:** {t.get('strategy')} "
                     f"{'· COMBINED ' + ','.join(t.get('source_strategies') or []) if t.get('combined') else ''}")
        lines.append(f"- **Confidence:** {t.get('confidence')}/100")
        lines.append(f"- **Entry:** {t.get('entry')}")
        lines.append(f"- **Stop loss:** {t.get('stop_loss')}")
        lines.append(f"- **Take profit:** {t.get('take_profit')}")
        lines.append(f"- **Entry time:** {t.get('entry_time') or '—'}")
        lines.append(f"- **Exit:** {t.get('exit')} @ {t.get('exit_time') or '—'}")
        lines.append(f"- **Outcome:** {t.get('outcome')} · PnL {t.get('pnl')}")
        lines.append("")
        lines.append("**Why?**")
        lines.append("")
        lines.append("```")
        lines.append(str(t.get('explanation') or '—'))
        lines.append("```")
        lines.append("")
        lines.append("**Evidence:**")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(t.get('evidence') or {}, indent=2, default=str))
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("[BACKTEST] wrote %d signals to %s", len(trades), path)
    return str(path)


def _coerce_strategies(raw: Any) -> list[str] | None:
    """Accept a comma-separated string or a list from form/data."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        items = [s.strip() for s in str(raw).split(",") if s.strip()]
    return items or None


def _read_csv_text(uploaded) -> str:
    """Read an uploaded file once (decoded) so it can be sniffed + re-parsed."""
    text = uploaded.read()
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text


def _timeframes_used(
    higher_tf_candles: dict[int, list[dict]], base_timeframe: int
) -> list[dict]:
    """
    Per-TF provenance for the response payload.

    The base timeframe is the raw input (labeled ``aggregated`` since it is
    re-aggregated by the engine); every higher timeframe is ``uploaded`` when a
    real file was provided, else ``aggregated``.  Timeframes below the base are
    not produced and are omitted.
    """
    out: list[dict] = []
    for tf in TIMEFRAMES:
        if tf < base_timeframe:
            continue
        if tf == base_timeframe:
            out.append({"timeframe": tf, "source": "aggregated"})
        else:
            out.append({
                "timeframe": tf,
                "source": "uploaded" if tf in higher_tf_candles else "aggregated",
            })
    return out


def _parse_higher_tf_files(
    request, symbol: str, base_timeframe: int
) -> dict[int, list[dict]]:
    """Parse every optional ``higher_tf_files`` upload, keyed by timeframe."""
    higher: dict[int, list[dict]] = {}
    for f in request.FILES.getlist("higher_tf_files"):
        try:
            text = _read_csv_text(f)
        except Exception:  # noqa: BLE001 — skip unreadable files
            continue
        try:
            tf = infer_timeframe(getattr(f, "name", "")) or 3600
            if tf == base_timeframe:
                continue  # same timeframe as the base — nothing extra to feed
            bars = parse_tf_csv(io.StringIO(text), symbol=symbol, timeframe=tf)
        except Exception as exc:  # noqa: BLE001 — surface a clean error
            logger.debug("higher-TF parse failed for %s: %s", f.name, exc)
            continue
        if bars:
            higher[tf] = bars  # identical-TF files: last one wins
    return higher


def _prepare_input(request):
    """Validate + parse the upload.  Returns (candles, symbol, strategies, higher, base_tf) or 400."""
    uploaded = request.FILES.get("file")
    if uploaded is None:
        return (
            None,
            Response(
                {"success": False, "message": "'file' field is required",
                 "errors": ["file"]},
                status=400,
            ),
        )

    symbol_override = (request.data.get("symbol") or request.POST.get("symbol") or "").strip() or None
    strategies = _coerce_strategies(request.data.get("strategies"))

    base_timeframe = infer_timeframe(getattr(uploaded, "name", "")) or 60
    text = _read_csv_text(uploaded)
    if not symbol_override and not has_symbol_column(text):
        return (
            None,
            Response(
                {"success": False, "message": NO_PAIR_MESSAGE,
                 "errors": ["symbol"]},
                status=400,
            ),
        )

    try:
        candles = parse_csv(io.StringIO(text), symbol=symbol_override)
    except Exception as exc:  # noqa: BLE001 — surface a clean error
        return (
            None,
            Response(
                {"success": False, "message": f"CSV parse error: {exc}",
                 "errors": [str(exc)]},
                status=400,
            ),
        )

    if not candles:
        return (
            None,
            Response(
                {"success": False, "message": "No valid rows parsed from CSV",
                 "errors": ["empty"]},
                status=400,
            ),
        )

    symbol = symbol_override or candles[0]["symbol"]
    higher = _parse_higher_tf_files(request, symbol, base_timeframe)
    return (candles, symbol, strategies, higher, base_timeframe), None


def _json_safe(value: Any) -> Any:
    """Recursively convert non-JSON-serializable values (datetime, Decimal,
    non-finite floats, etc.) to JSON-safe primitives so a payload can be
    stored in a JSONField / streamed as JSON.

    Non-finite floats (inf/-inf/nan) are coerced to ``None`` — Python's
    ``json.dumps`` emits them as ``Infinity``/``NaN`` which SQLite's
    ``JSON_VALID`` CHECK constraint rejects outright.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # nan / ±inf
            return None
        return value
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    # Decimal, UUID, numpy scalars, enums, etc. — stringify as a last resort.
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _persist(model_cls, request, uploaded, symbol, result, payload) -> Any:
    return model_cls.objects.create(
        symbol=symbol,
        csv_name=getattr(uploaded, "name", "") or "",
        initial_balance=result.initial_balance,
        final_balance=result.final_balance,
        total_pnl=result.total_pnl,
        overall_win_rate=result.overall_win_rate,
        best_strategy=result.best_strategy or "",
        per_strategy=_json_safe(payload["per_strategy"]),
        trades=_json_safe(payload["trades"]),
        created_by=request.user if request.user.is_authenticated else None,
    )


def _parse_and_run(request):
    """Shared parse + run for both endpoints.  Returns (Response|None, symbol, payload, model)."""
    prepared = _prepare_input(request)
    if prepared[0] is None:
        return prepared[1], None, None, None
    candles, symbol, strategies, higher, base_tf = prepared[0]

    engine = BacktestEngine(strategy_mode=",".join(strategies) if strategies else "malysian")
    result = engine.run(
        candles, symbol, strategies=strategies,
        base_timeframe=base_tf, higher_tf_candles=higher or None,
    )

    payload = result.to_dict()
    payload["timeframes_used"] = _timeframes_used(higher, base_tf)
    model = _persist(BacktestResultModel, request, request.FILES.get("file"),
                     symbol, result, payload)
    # Persist a timestamped signal report for offline backtest/verification.
    payload["signals_md"] = _write_signals_md(
        symbol, result, payload, getattr(request.FILES.get("file"), "name", "") or ""
    )
    return None, symbol, payload, model


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def backtest_upload(request):
    error, symbol, payload, model = _parse_and_run(request)
    if error is not None:
        return error
    return Response(
        {"success": True, "message": "Backtest complete",
         "data": {"id": model.id, **payload}},
        status=200,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def backtest_upload_stream(request):
    """
    Run a backtest and stream Server-Sent Events.

    Yields one ``data:`` event per progress tick (the execution flow as the
    data is processed) followed by a final ``event: done`` carrying the
    result.  Runs inline in the request thread — no threads/async (CPU-bound
    backtest); progress events are buffered as the engine runs, then streamed
    when the ``StreamingHttpResponse`` is iterated.
    """
    prepared = _prepare_input(request)
    if prepared[0] is None:
        return prepared[1]
    candles, symbol, strategies, higher, base_tf = prepared[0]

    engine = BacktestEngine(strategy_mode=",".join(strategies) if strategies else "malysian")

    def sse_events():
        progress_events: list[str] = []

        def on_progress(info: dict) -> None:
            progress_events.append(f"data: {json.dumps(info)}\n\n")

        result = engine.run(
            candles, symbol, strategies=strategies, progress_callback=on_progress,
            base_timeframe=base_tf, higher_tf_candles=higher or None,
        )
        for event in progress_events:
            yield event

        payload = result.to_dict()
        payload["timeframes_used"] = _timeframes_used(higher, base_tf)
        model = _persist(
            BacktestResultModel, request, request.FILES.get("file"),
            symbol, result, payload,
        )
        # Persist a timestamped signal report for offline backtest/verification.
        payload["signals_md"] = _write_signals_md(
            symbol, result, payload, getattr(request.FILES.get("file"), "name", "") or ""
        )
        final_data = {"success": True, "message": "Backtest complete",
                      "data": {"id": model.id, **payload}}
        # default=str: trade evidence carries datetime objects (touch_ts etc.)
        # that raw json.dumps cannot serialize — stringify them for the SSE frame.
        yield "data: " + json.dumps(final_data, default=str) + "\n\n"
        yield "event: done\ndata: " + json.dumps({"success": True}) + "\n\n"

    return StreamingHttpResponse(sse_events(), content_type="text/event-stream")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def backtest_results(request):
    qs = BacktestResultModel.objects.all().order_by("-created_at")
    data = [
        {
            "id": m.id,
            "symbol": m.symbol,
            "csv_name": m.csv_name,
            "created_at": m.created_at.isoformat(),
            "initial_balance": m.initial_balance,
            "final_balance": m.final_balance,
            "total_pnl": m.total_pnl,
            "overall_win_rate": m.overall_win_rate,
            "best_strategy": m.best_strategy,
            "total_trades": len(m.trades or []),
        }
        for m in qs
    ]
    return Response({"success": True, "data": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def backtest_result_detail(request, pk: int):
    try:
        m = BacktestResultModel.objects.get(pk=pk)
    except BacktestResultModel.DoesNotExist:
        return Response(
            {"success": False, "message": "Backtest result not found",
             "errors": ["not_found"]},
            status=404,
        )

    data = {
        "id": m.id,
        "symbol": m.symbol,
        "csv_name": m.csv_name,
        "initial_balance": m.initial_balance,
        "final_balance": m.final_balance,
        "total_pnl": m.total_pnl,
        "overall_win_rate": m.overall_win_rate,
        "best_strategy": m.best_strategy,
        "per_strategy": m.per_strategy,
        "trades": m.trades,
        "created_at": m.created_at.isoformat(),
        "created_by": m.created_by_id,
    }
    return Response({"success": True, "data": data})
