"""
Pepperstone / cTrader Open API integration package.

Owned by the integration-engineer. Provides:

* ``client``            — credentials, client builder, handshake session,
                          price/spread helpers.
* ``ticks_engine``      — long-running spot-tick streamer (ClickHouse + HTTP).
* ``execution_engine``  — non-blocking market-order execution.

Backend-dev may import the public functions/classes below; nothing here
touches Django views, urls, or the clickhouse schema module.
"""
from forex.pepperstone.client import (
    DEFAULT_PEPPERSTONE_ACCOUNT_ID,
    PROTOBUF_DEMO_HOST,
    PROTOBUF_DEMO_PORT,
    PepperstoneSession,
    build_client,
    compute_real_price,
    compute_spread_pips,
    get_credentials,
    price_scale,
)
from forex.pepperstone.ticks_engine import MAX_RECONNECT_ATTEMPTS, TicksEngine
from forex.pepperstone.execution_engine import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    TradeExecutor,
)

__all__ = [
    "DEFAULT_PEPPERSTONE_ACCOUNT_ID",
    "PROTOBUF_DEMO_HOST",
    "PROTOBUF_DEMO_PORT",
    "PepperstoneSession",
    "build_client",
    "compute_real_price",
    "compute_spread_pips",
    "get_credentials",
    "price_scale",
    "MAX_RECONNECT_ATTEMPTS",
    "TicksEngine",
    "DEFAULT_EXECUTION_TIMEOUT_SECONDS",
    "TradeExecutor",
]