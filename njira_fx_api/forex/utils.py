"""
Utility helpers for the forex application.

DEPRECATED: `connect_to_clickhouse` is superseded by
`forex.clickhouse.connection.get_clickhouse_client`. It is kept as a thin,
env-driven wrapper for backwards compatibility — nothing in the codebase
imports it today (verified by grep).

The previous implementation hardcoded GCP ClickHouse Cloud credentials
(rerj5p7iz5.europe-west4.gcp.clickhouse.cloud), referenced the undefined
variable `client` in its verification print (it should have been
`clickhouse_client`), and imported the heavy `clickhouse_connect.Client`
class directly. All of that is removed; configuration now lives in one
place only.
"""
from .clickhouse.connection import get_clickhouse_client


def connect_to_clickhouse():
    """
    Return the cached, environment-driven ClickHouse client.

    Deprecated: use `forex.clickhouse.connection.get_clickhouse_client`
    instead. This wrapper exists solely to preserve the old call surface.
    """
    return get_clickhouse_client()
