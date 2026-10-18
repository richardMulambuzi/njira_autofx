"""
Pepperstone / cTrader Open API integration package.

Owned by the integration-engineer. Provides:

* ``client``            — credentials, client builder, handshake session,
                          price/spread helpers.
* ``ticks_engine``      — long-running spot-tick streamer (ClickHouse + HTTP).
* ``execution_engine``  — non-blocking market-order execution.
* ``h4_window``         the strict 300-bar H4 ring buffer.
* ``window_session``    one connection streaming H4 bars into one window
                          per symbol.

Backend-dev may import the public functions/classes below; nothing here
touches Django views, urls, or the clickhouse schema module.
"""
from forex.pepperstone.client import (
    DEFAULT_PEPPERSTONE_ACCOUNT_ID,
    PROTOBUF_DEMO_HOST,
    PROTOBUF_DEMO_PORT,
    PROTOBUF_LIVE_HOST,
    PepperstoneSession,
    build_client,
    compute_real_price,
    compute_spread_pips,
    get_credentials,
    price_scale,
    resolve_environment,
    resolve_host,
)
from forex.pepperstone.ticks_engine import MAX_RECONNECT_ATTEMPTS, TicksEngine
from forex.pepperstone.h4_window import (
    BACKFILL_CAL_DAYS,
    WINDOW_SIZE,
    H4Window,
)
from forex.pepperstone.window_session import WindowSession
from forex.pepperstone.execution_engine import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    TradeExecutor,
)

__all__ = [
    "DEFAULT_PEPPERSTONE_ACCOUNT_ID",
    "PROTOBUF_DEMO_HOST",
    "PROTOBUF_DEMO_PORT",
    "PROTOBUF_LIVE_HOST",
    "PepperstoneSession",
    "build_client",
    "compute_real_price",
    "compute_spread_pips",
    "get_credentials",
    "price_scale",
    "resolve_environment",
    "resolve_host",
    "MAX_RECONNECT_ATTEMPTS",
    "TicksEngine",
    "BACKFILL_CAL_DAYS",
    "WINDOW_SIZE",
    "H4Window",
    "WindowSession",
    "DEFAULT_EXECUTION_TIMEOUT_SECONDS",
    "TradeExecutor",
]