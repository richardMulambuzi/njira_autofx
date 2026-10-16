"""
ClickHouse connection management for the forex application.

Configuration is environment-driven with sane defaults matching the verified
production ClickHouse instance (indemw.tech:8123).

Environment variables:
    CLICKHOUSE_HOST       Default: "indemarkets_db" — the Docker service name
                          (reachable inside the compose network). For local
                          development / outside Docker use "indemw.tech".
    CLICKHOUSE_PORT       Default: "8123" (native/HTTP interface)
    CLICKHOUSE_USER       Default: "admin"
    CLICKHOUSE_PASSWORD   REQUIRED — no default. MUST be provided via the
                          CLICKHOUSE_PASSWORD environment variable. It is
                          resolved lazily on first connect so that commands
                          which never touch ClickHouse (e.g. manage.py check)
                          do not need it set.
    CLICKHOUSE_DATABASE   Default: "default"
    CLICKHOUSE_CONNECT_TIMEOUT  Default: "5" (seconds) — keeps boot from hanging
                          when the server is unreachable.
    CLICKHOUSE_SECURE     Default: "false" — enables TLS transport encryption
                          (ClickHouse HTTPS on port 8443). Ops enables it by
                          setting CLICKHOUSE_SECURE=true + CLICKHOUSE_PORT=8443;
                          the password is then never transmitted in cleartext
                          (remediates security finding #14).
"""
import logging
import os
import re
import threading

import clickhouse_connect

logger = logging.getLogger("forex.clickhouse")

# ---------------------------------------------------------------------------
# Environment-driven configuration (defaults match the verified server)
# ---------------------------------------------------------------------------
CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "indemarkets_db")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "admin")
CLICKHOUSE_DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "default")
CLICKHOUSE_CONNECT_TIMEOUT = float(os.environ.get("CLICKHOUSE_CONNECT_TIMEOUT", "5"))

# TLS transport encryption. Ops turns this on (with CLICKHOUSE_PORT=8443) once
# the server exposes HTTPS — the password is then encrypted in transit instead
# of being sent in cleartext over HTTP (security finding #14).
CLICKHOUSE_SECURE = os.environ.get("CLICKHOUSE_SECURE", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# ClickHouse identifiers: letters, digits, underscore; cannot start with a digit.
_DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Thread-local ClickHouse client storage. A single shared client raises
# "Attempt to execute concurrent queries within the same session" when Django
# serves multiple requests on different threads (e.g. the UI polling
# /api/chart-data/ every second alongside /api/mtf-status/). Giving each thread
# its own lazily-created client eliminates that contention. The module global
# starts as None and is recreated as a threading.local() on first use (and
# whenever a test resets it to None), so the existing reset-to-None behaviour
# in auth_app/tests.py keeps working.
_clickhouse_client = None

# Ensures the target database exactly once per process (the DDL is idempotent
# and non-fatal). Guarded by a lock because the first client in *any* thread
# triggers it.
_db_lock = threading.Lock()
_db_ensured = False


def _ensure_database_exists(client) -> None:
    """
    Idempotently create the target database if it does not already exist.

    Non-fatal: a failure here only degrades features that depend on the
    database — the API must still boot.

    The database name is validated against a strict identifier allowlist
    before interpolation, so the env-provided value can never inject SQL.
    """
    try:
        if not _DATABASE_NAME_RE.match(CLICKHOUSE_DATABASE):
            raise ValueError(
                "CLICKHOUSE_DATABASE is not a valid identifier: "
                f"{CLICKHOUSE_DATABASE!r}"
            )
        # Identifier already allowlist-validated — safe to interpolate.
        client.command(
            "CREATE DATABASE IF NOT EXISTS `{db}`".format(db=CLICKHOUSE_DATABASE)
        )
    except Exception as e:  # noqa: BLE001 — never take boot down over DDL
        logger.warning(
            "Could not ensure ClickHouse database `%s` exists: %s",
            CLICKHOUSE_DATABASE,
            e,
        )


# ClickHouse disabled — memory-only mode (fix later). get_clickhouse_client
# stays importable but never attempts a connection (no DNS, no secrets).
_ch_disabled_logged = False


def get_clickhouse_client():
    """
    Disabled stub: always returns None (memory-only mode).

    Logs once; never touches the network so imports/boot cannot trigger
    DNS resolution (e.g. indemw.tech NameResolutionError).
    """
    global _ch_disabled_logged
    if not _ch_disabled_logged:
        _ch_disabled_logged = True
        logger.warning("ClickHouse disabled — running memory-only (no connection).")
    return None
