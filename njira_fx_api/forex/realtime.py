"""
Server-side Pusher Channels broadcaster (no third-party SDK).

Implements the Pusher REST trigger API directly with :mod:`requests` plus the
standard library ``hmac`` / ``hashlib`` so no new pip dependency is required.

Contract (Pusher HTTP API, documented auth scheme):

    POST {scheme}://{host}:{port}/apps/{app_id}/events
    Body: {"name": <event>, "channels": [<channel>], "data": <json-string>}
    Query: auth_key, auth_timestamp, auth_version=1.0, auth_signature

    auth_signature = HMAC-SHA256(
        secret,
        "POST\n/apps/{app_id}/events\n"
        "auth_key={key}&auth_timestamp={ts}&auth_version=1.0\n"
        "{md5_of_body}"
    )

All configuration is read from the ``PUSHER_*`` environment variables. When any
required value is missing the broadcaster logs a warning **once** and returns
``False`` — it never raises into the caller (the tick ingest path must stay
resilient to a misconfigured realtime backend).

Public API:

    * :func:`broadcast_event` — low-level, single-event trigger (exposed for
      future signal events).
    * :func:`broadcast_tick` — throttled convenience wrapper for market ticks
      (max one broadcast per second per symbol) used by the tick ingest view.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger("forex.realtime")

# Pusher REST API version used in the auth signature query string.
_PUSHER_AUTH_VERSION = "1.0"

# Outbound HTTP timeout (seconds) — never block the caller indefinitely.
_REQUEST_TIMEOUT = 5

# Per-symbol throttle for :func:`broadcast_tick`: at most one broadcast per
# second per symbol so a high-frequency tick stream cannot flood Pusher.
_TICK_THROTTLE_SECONDS = 1.0
_tick_last_sent: dict[str, float] = {}

# Ensures the "missing config" warning is emitted at most once per process.
_warned_missing_config = False


def _read_config() -> dict[str, str] | None:
    """
    Read and validate the Pusher configuration from the environment.

    Returns a dict with ``scheme``, ``host``, ``port``, ``app_id``, ``key`` and
    ``secret`` when every required value is present, otherwise ``None`` (and
    logs a warning exactly once).
    """
    global _warned_missing_config

    scheme = os.environ.get("PUSHER_SCHEME") or "https"
    host = os.environ.get("PUSHER_HOST", "")
    port = os.environ.get("PUSHER_PORT", "443")
    app_id = os.environ.get("PUSHER_APP_ID", "")
    key = os.environ.get("PUSHER_APP_KEY", "")
    secret = os.environ.get("PUSHER_APP_SECRET", "")

    if not (host and app_id and key and secret):
        if not _warned_missing_config:
            logger.warning(
                "Pusher broadcaster disabled: missing one or more of "
                "PUSHER_HOST, PUSHER_APP_ID, PUSHER_APP_KEY, PUSHER_APP_SECRET"
            )
            _warned_missing_config = True
        return None

    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "app_id": app_id,
        "key": key,
        "secret": secret,
    }


def broadcast_event(channel: str, event: str, payload: dict[str, Any]) -> bool:
    """
    Trigger a single Pusher event.

    Parameters
    ----------
    channel:
        Target channel name (e.g. ``"njira-market"``).
    event:
        Event name (e.g. ``"tick"`` or ``"ping"``).
    payload:
        Arbitrary JSON-serialisable dict; it is stringified into the Pusher
        ``data`` field (Pusher requires ``data`` to be a JSON *string*).

    Returns
    -------
    bool
        ``True`` when Pusher accepted the event (HTTP 2xx), ``False`` on any
        configuration, network, or auth failure. Never raises.
    """
    config = _read_config()
    if config is None:
        return False

    # Pusher expects ``data`` to be a JSON string, not a nested object.
    body = json_dumps(
        {"name": event, "channels": [channel], "data": json_dumps(payload)}
    )

    # Pusher requires the MD5 of the request body as a query parameter, but it
    # is NOT part of the signed string for this endpoint (verified against the
    # live API: signatures that append the body MD5 are rejected, and the body
    # MD5 must instead be supplied as a separate ``body_md5`` query param).
    body_md5 = hashlib.md5(body.encode("utf-8")).hexdigest()

    auth_timestamp = int(time.time())
    query_string = (
        f"auth_key={config['key']}"
        f"&auth_timestamp={auth_timestamp}"
        f"&auth_version={_PUSHER_AUTH_VERSION}"
        f"&body_md5={body_md5}"
    )
    # Signed string = method + path + the auth query params (which include the
    # required body_md5). Verified against the live Pusher API.
    string_to_sign = f"POST\n/apps/{config['app_id']}/events\n{query_string}"
    auth_signature = hmac.new(
        config["secret"].encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    url = (
        f"{config['scheme']}://{config['host']}:{config['port']}"
        f"/apps/{config['app_id']}/events"
    )
    params = {
        "auth_key": config["key"],
        "auth_timestamp": auth_timestamp,
        "auth_version": _PUSHER_AUTH_VERSION,
        "body_md5": body_md5,
        "auth_signature": auth_signature,
    }

    try:
        response = requests.post(
            url,
            params=params,
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning("Pusher broadcast failed (%s/%s): %s", channel, event, exc)
        return False

    if not response.ok:
        logger.warning(
            "Pusher rejected %s/%s: HTTP %s %s",
            channel,
            event,
            response.status_code,
            response.text[:200],
        )
        return False

    return True


def broadcast_tick(symbol: str, payload: dict[str, Any]) -> bool:
    """
    Throttled broadcast of a market tick on the ``njira-market`` channel.

    Limits broadcasts to at most one per second per symbol to avoid flooding
    Pusher from a high-frequency tick ingest stream. Throttled ticks are
    treated as successfully handled (no-op) and return ``True``.

    Parameters
    ----------
    symbol:
        Instrument symbol (used for the per-symbol throttle key).
    payload:
        Tick payload forwarded to :func:`broadcast_event`.

    Returns
    -------
    bool
        ``True`` when the event was sent or throttled, ``False`` only on an
        actual broadcast failure.
    """
    now = time.monotonic()
    last = _tick_last_sent.get(symbol)
    if last is not None and (now - last) < _TICK_THROTTLE_SECONDS:
        return True  # throttled — not an error
    _tick_last_sent[symbol] = now
    return broadcast_event("njira-market", "tick", payload)


def json_dumps(value: Any) -> str:
    """Compact JSON serialisation (no whitespace) for Pusher bodies."""
    import json

    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
