"""Server-rendered console pages for the njira FX API."""
from __future__ import annotations

import hmac
import os
import sqlite3
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

SESSION_KEY = "njira_dashboard_ok"
LATEST_SCAN_ROWS = 200
AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"
CONSOLE_USER = "console"

NAV = [
    ("dashboard", "Overview", "dashboard"),
    ("markets", "Markets", "console-markets"),
    ("signals", "Signals", "console-signals"),
    ("strategies", "Strategies", "console-strategies"),
    ("system", "System", "console-system"),
]


def _expected_password() -> str:
    return os.environ.get("NJIRA_DASHBOARD_PASSWORD", "")


def _tick_store_path() -> Path | None:
    raw = os.environ.get("NJIRA_TICK_STORE", "").strip()
    return Path(raw) if raw else None


def _signal_dir() -> Path:
    raw = os.environ.get("NJIRA_SIGNAL_DIR", "").strip()
    return Path(raw) if raw else Path(settings.BASE_DIR) / "signals"


def _tick_summary() -> dict:
    path = _tick_store_path()
    if path is None or not path.exists():
        return {"enabled": False}

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows, newest = conn.execute("SELECT COUNT(*), MAX(ts_ms) FROM ticks").fetchone()
            per_minute = 0
            if newest:
                per_minute = conn.execute(
                    "SELECT COUNT(*) FROM ticks WHERE ts_ms >= ?", (newest - 60_000,)
                ).fetchone()[0]
            latest: dict[str, dict] = {}
            for symbol, bid, ask, ts_ms in conn.execute(
                "SELECT symbol, bid, ask, ts_ms FROM ticks ORDER BY ts_ms DESC LIMIT ?",
                (LATEST_SCAN_ROWS,),
            ):
                if symbol not in latest:
                    latest[symbol] = {"bid": bid, "ask": ask, "ts_ms": ts_ms}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"enabled": True, "error": str(exc)}

    return {
        "enabled": True,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "rows": rows,
        "newest_ms": newest,
        "rows_last_minute": per_minute,
        "latest": latest,
    }


def _card_summary(limit: int = 40) -> dict:
    root = _signal_dir()
    if not root.is_dir():
        return {"dir": str(root), "exists": False, "count": 0, "cards": []}

    files = sorted(root.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    cards = []
    for path in files[:limit]:
        stat = path.stat()
        cards.append(
            {
                "name": path.name,
                "rel": str(path.relative_to(root)),
                "modified_ms": int(stat.st_mtime * 1000),
                "size": stat.st_size,
            }
        )
    return {"dir": str(root), "exists": True, "count": len(files), "cards": cards}


def _is_authed(request: HttpRequest) -> bool:
    return bool(_expected_password()) and bool(request.session.get(SESSION_KEY))


def _grant(request: HttpRequest) -> None:
    """Mark the session signed in and attach a real Django principal.

    The JSON API gates on IsAuthenticated, so without an actual user every
    provider, sync and backtest read answers 401 to an otherwise signed-in
    console. The user carries an unusable password: the console password is
    the only way in.
    """
    request.session[SESSION_KEY] = True
    user, created = get_user_model().objects.get_or_create(
        username=os.environ.get("NJIRA_DASHBOARD_USER", CONSOLE_USER)
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    auth_login(request, user, backend=AUTH_BACKEND)


def _gate(request: HttpRequest) -> dict:
    expected = _expected_password()
    error = None

    if request.method == "POST":
        supplied = request.POST.get("password", "")
        if expected and hmac.compare_digest(supplied, expected):
            _grant(request)
        else:
            request.session.pop(SESSION_KEY, None)
            error = "Wrong password."
            time.sleep(1)

    return {"authed": _is_authed(request), "configured": bool(expected), "error": error}


def _page(request: HttpRequest, template: str, active: str) -> HttpResponse:
    return render(request, template, {**_gate(request), "nav": NAV, "active": active})


@require_http_methods(["GET", "POST"])
def dashboard(request: HttpRequest) -> HttpResponse:
    return _page(request, "dashboard.html", "dashboard")


@require_http_methods(["GET", "POST"])
def console_markets(request: HttpRequest) -> HttpResponse:
    return _page(request, "markets.html", "markets")


@require_http_methods(["GET", "POST"])
def console_signals(request: HttpRequest) -> HttpResponse:
    return _page(request, "signals.html", "signals")


@require_http_methods(["GET", "POST"])
def console_strategies(request: HttpRequest) -> HttpResponse:
    return _page(request, "strategies.html", "strategies")


@require_http_methods(["GET", "POST"])
def console_system(request: HttpRequest) -> HttpResponse:
    return _page(request, "system.html", "system")


@require_http_methods(["GET", "POST"])
def dashboard_logout(request: HttpRequest) -> HttpResponse:
    auth_logout(request)
    return render(
        request,
        "dashboard.html",
        {"authed": False, "configured": bool(_expected_password()), "error": None, "nav": NAV, "active": "dashboard"},
    )


@require_http_methods(["GET"])
def dashboard_live(request: HttpRequest) -> JsonResponse:
    if not _is_authed(request):
        return JsonResponse({"error": "unauthorised"}, status=403)
    return JsonResponse({"ticks": _tick_summary(), "cards": _card_summary()})
